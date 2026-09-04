import copy
import json
import unittest

from jenkins_adapter import (
    MAX_CONSOLE_BYTES,
    MAX_CONSOLE_LINES,
    JenkinsSnapshotClient,
    JenkinsSnapshotError,
)


REVISION = "d" * 40
BUILD_URL = "https://build.whamcloud.com/job/lustre-reviews/123/"


def build_value(**updates):
    value = {
        "number": 123, "url": BUILD_URL, "result": "FAILURE",
        "building": False, "timestamp": 1700000000000, "duration": 4000,
        "actions": [{"parameters": [
            {"name": "GERRIT_CHANGE_NUMBER", "value": "68541"},
            {"name": "GERRIT_PATCHSET_NUMBER", "value": "3"},
            {"name": "GERRIT_PATCHSET_REVISION", "value": REVISION},
            {"name": "GERRIT_REFSPEC", "value": "refs/changes/41/68541/3"},
            {"name": "GERRIT_PROJECT", "value": "fs/lustre-release"},
            {"name": "GERRIT_BRANCH", "value": "master"},
            {"name": "SECRET_TOKEN", "value": "must-not-persist"},
        ]}],
        "runs": [{
            "number": 123,
            "url": "https://build.whamcloud.com/job/lustre-reviews/arch=aarch64/123/",
            "result": "FAILURE", "building": False,
            "fullDisplayName": "lustre-reviews #123 » arch=aarch64",
            "builtOn": "builder-1",
        }],
    }
    value.update(updates)
    return value


class FakeTransport:
    def __init__(self, build=None):
        self.build = build or build_value()
        self.urls = []
        self.requests = []

    def __call__(self, request, _timeout):
        self.urls.append(request.full_url)
        self.requests.append(request)
        if "/api/json?" in request.full_url:
            return json.dumps(self.build).encode()
        if "arch=aarch64" in request.full_url:
            return b"configure\nERROR: failed unit\n"
        return b"parent\nFAILURE\n"


class ChangingTransport(FakeTransport):
    def __init__(self, first, second):
        super().__init__(first)
        self.builds = [first, second]
        self.api_reads = 0

    def __call__(self, request, timeout):
        if "/api/json?" in request.full_url:
            self.urls.append(request.full_url)
            self.requests.append(request)
            value = self.builds[min(self.api_reads, len(self.builds) - 1)]
            self.api_reads += 1
            return json.dumps(value).encode()
        return super().__call__(request, timeout)


class JenkinsAdapterTests(unittest.TestCase):
    def fetch(self, transport=None, **updates):
        values = {
            "change_number": 68541, "patchset": 3,
            "revision_sha": REVISION,
            "revision_ref": "refs/changes/41/68541/3",
            "project": "fs/lustre-release",
        }
        values.update(updates)
        return JenkinsSnapshotClient(transport=transport or FakeTransport()).fetch_failure_snapshot(
            BUILD_URL, **values,
        )

    def test_exact_failure_is_normalized_and_hashed_stably(self):
        first = self.fetch()
        second = self.fetch()
        self.assertTrue(first["complete"])
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(first["build"]["job_name"], "lustre-reviews")
        self.assertEqual(first["failed_runs"][0]["node"], "builder-1")
        self.assertNotIn("must-not-persist", json.dumps(first))
        self.assertEqual(first["matrix_runs"][0]["configuration"], "arch=aarch64")
        self.assertEqual(first["failed_runs"][0]["console"]["truncated"], False)

    def test_snapshot_digest_is_independent_of_matrix_run_order(self):
        successful = {
            "number": 123,
            "url": "https://build.whamcloud.com/job/lustre-reviews/arch=x86_64/123/",
            "result": "SUCCESS", "building": False, "duration": 2000,
            "fullDisplayName": "lustre-reviews #123 » arch=x86_64",
            "builtOn": "builder-2",
        }
        failed = build_value()["runs"][0]
        first = self.fetch(FakeTransport(build_value(runs=[successful, failed])))
        second = self.fetch(FakeTransport(build_value(runs=[failed, successful])))
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(
            [item["configuration"] for item in first["matrix_runs"]],
            ["arch=aarch64", "arch=x86_64"],
        )

    def test_rejects_old_patchset_and_wrong_revision(self):
        with self.assertRaisesRegex(JenkinsSnapshotError, "exact current"):
            self.fetch(patchset=4, revision_ref="refs/changes/41/68541/4")
        with self.assertRaisesRegex(JenkinsSnapshotError, "exact current"):
            self.fetch(revision_sha="e" * 40)

    def test_rejects_incomplete_or_malformed_exact_identity(self):
        for updates in (
            {"change_number": 0},
            {"patchset": True},
            {"revision_ref": "refs/changes/41/68541/2"},
            {"project": ""},
        ):
            with self.subTest(updates=updates), self.assertRaisesRegex(
                JenkinsSnapshotError, "identity is malformed"
            ):
                self.fetch(**updates)
        raw = build_value()
        raw["actions"][0]["parameters"] = [
            item for item in raw["actions"][0]["parameters"]
            if item["name"] != "GERRIT_BRANCH"
        ]
        with self.assertRaisesRegex(JenkinsSnapshotError, "incomplete"):
            self.fetch(FakeTransport(raw))

    def test_rejects_running_success_and_hostile_urls(self):
        for raw in (
            build_value(building=True, result=None),
            build_value(result="SUCCESS"),
        ):
            with self.subTest(raw=raw), self.assertRaises(JenkinsSnapshotError):
                self.fetch(FakeTransport(raw))
        for url in (
            "http://build.whamcloud.com/job/x/1/",
            "https://evil.example/job/x/1/",
            "https://user:pass@build.whamcloud.com/job/x/1/",
            "https://build.whamcloud.com/job/x/not-a-number/",
        ):
            with self.subTest(url=url), self.assertRaises(JenkinsSnapshotError):
                JenkinsSnapshotClient(transport=FakeTransport()).fetch_failure_snapshot(
                    url, change_number=68541, patchset=3, revision_sha=REVISION,
                    revision_ref="refs/changes/41/68541/3",
                    project="fs/lustre-release",
                )

    def test_matrix_child_url_is_validated_before_fetch(self):
        raw = build_value(runs=[{
            "number": 123, "url": "https://evil.example/job/x/123/",
            "result": "FAILURE", "building": False,
        }])
        transport = FakeTransport(raw)
        with self.assertRaises(JenkinsSnapshotError):
            self.fetch(transport)
        self.assertFalse(any("evil.example" in url for url in transport.urls))

    def test_matrix_child_must_belong_to_exact_parent_job(self):
        raw = build_value(runs=[{
            "number": 123,
            "url": "https://build.whamcloud.com/job/other-reviews/arch=aarch64/123/",
            "result": "FAILURE", "building": False,
        }])
        with self.assertRaisesRegex(JenkinsSnapshotError, "exact build job"):
            self.fetch(FakeTransport(raw))

    def test_rejects_matrix_url_as_parent_and_accepts_folder_job(self):
        with self.assertRaisesRegex(JenkinsSnapshotError, "parent build"):
            JenkinsSnapshotClient(transport=FakeTransport()).fetch_failure_snapshot(
                "https://build.whamcloud.com/job/lustre-reviews/arch=aarch64/123/",
                change_number=68541, patchset=3, revision_sha=REVISION,
                revision_ref="refs/changes/41/68541/3", project="fs/lustre-release",
            )
        folder_url = "https://build.whamcloud.com/job/reviews/job/lustre/123/"
        raw = build_value(url=folder_url, runs=[])
        result = JenkinsSnapshotClient(transport=FakeTransport(raw)).fetch_failure_snapshot(
            folder_url, change_number=68541, patchset=3, revision_sha=REVISION,
            revision_ref="refs/changes/41/68541/3", project="fs/lustre-release",
        )
        self.assertEqual(result["build"]["job_name"], "reviews/lustre")

    def test_duplicate_identity_parameter_is_rejected_even_when_equal(self):
        raw = build_value()
        raw["actions"][0]["parameters"].append(
            {"name": "GERRIT_PATCHSET_REVISION", "value": REVISION}
        )
        with self.assertRaisesRegex(JenkinsSnapshotError, "duplicate"):
            self.fetch(FakeTransport(raw))

    def test_non_allowlisted_parameters_never_enter_snapshot(self):
        raw = build_value()
        raw["actions"][0]["parameters"].extend([
            {"name": "GERRIT_CHANGE_SUBJECT", "value": "private-subject-marker"},
            {"name": "PASSWORD", "value": "private-password-marker"},
        ])
        snapshot = self.fetch(FakeTransport(raw))
        encoded = json.dumps(snapshot)
        self.assertNotIn("private-subject-marker", encoded)
        self.assertNotIn("private-password-marker", encoded)

    def test_every_failed_child_log_is_fetched_and_sorted(self):
        runs = []
        for arch in ("x86_64", "aarch64", "ppc64le"):
            runs.append({
                "number": 123,
                "url": f"https://build.whamcloud.com/job/lustre-reviews/arch={arch}/123/",
                "result": "FAILURE", "building": False, "duration": 1,
                "fullDisplayName": f"arch={arch}", "builtOn": arch,
            })
        transport = FakeTransport(build_value(runs=runs))
        snapshot = self.fetch(transport)
        self.assertEqual(len(snapshot["failed_runs"]), 3)
        console_reads = [url for url in transport.urls if url.endswith("consoleText")]
        self.assertEqual(len(console_reads), 4)  # parent plus every failed child
        self.assertEqual(
            [item["configuration"] for item in snapshot["failed_runs"]],
            ["arch=aarch64", "arch=ppc64le", "arch=x86_64"],
        )

    def test_rejects_incomplete_current_child_but_ignores_neighbor_build(self):
        current = {
            "number": 123,
            "url": "https://build.whamcloud.com/job/lustre-reviews/arch=x86_64/123/",
            "result": None, "building": False,
        }
        with self.assertRaisesRegex(JenkinsSnapshotError, "unknown terminal"):
            self.fetch(FakeTransport(build_value(runs=[current])))
        previous = dict(current, number=122, building=True)
        snapshot = self.fetch(FakeTransport(build_value(runs=[previous])))
        self.assertEqual(snapshot["matrix_runs"], [])
        with self.assertRaisesRegex(JenkinsSnapshotError, "collection is malformed"):
            self.fetch(FakeTransport(build_value(runs=123)))

    def test_build_semantics_must_stay_stable_across_log_capture(self):
        first = build_value()
        second = copy.deepcopy(first)
        second["runs"][0]["builtOn"] = "different-builder"
        with self.assertRaisesRegex(JenkinsSnapshotError, "changed"):
            self.fetch(ChangingTransport(first, second))

    def test_console_excerpt_is_bounded_and_redacted(self):
        class LargeConsoleTransport(FakeTransport):
            def __call__(self, request, timeout):
                if "/api/json?" in request.full_url:
                    return super().__call__(request, timeout)
                self.urls.append(request.full_url)
                self.requests.append(request)
                line = b"JENKINS_TOKEN=top-secret " + (b"x" * 2048) + b"\n"
                return line * (MAX_CONSOLE_LINES + 20)

        snapshot = self.fetch(LargeConsoleTransport())
        encoded = json.dumps(snapshot)
        self.assertNotIn("top-secret", encoded)
        self.assertTrue(snapshot["parent_console"]["truncated"])
        self.assertLessEqual(snapshot["parent_console"]["bytes_read"], MAX_CONSOLE_BYTES)
        self.assertLessEqual(len(snapshot["parent_console_tail"]), MAX_CONSOLE_LINES)
        self.assertTrue(all(len(line) <= 2000 for line in snapshot["parent_console_tail"]))

    def test_requests_are_get_only_and_do_not_carry_credentials(self):
        transport = FakeTransport()
        self.fetch(transport)
        self.assertTrue(transport.requests)
        for request in transport.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertNotIn("Authorization", request.headers)
            self.assertTrue(request.full_url.startswith("https://build.whamcloud.com/"))


if __name__ == "__main__":
    unittest.main()
