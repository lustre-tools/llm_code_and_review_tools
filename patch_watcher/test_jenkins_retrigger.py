import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jenkins_adapter import SNAPSHOT_SCHEMA
from jenkins_retrigger import (
    JENKINS_RETRIGGER_CAPABILITY,
    JenkinsRetriggerConflict,
    JenkinsRetriggerController,
    JenkinsRetriggerStore,
    JenkinsToolRetriggerWriter,
)


REVISION = "a" * 40
REF = "refs/changes/41/68541/3"
BUILD_URL = "https://build.whamcloud.com/job/lustre-reviews/120/"


def failure_snapshot(**changes):
    value = {
        "schema": SNAPSHOT_SCHEMA,
        "complete": True,
        "change": {
            "change_number": 68541,
            "patchset": 3,
            "revision_sha": REVISION,
            "revision_ref": REF,
            "project": "fs/lustre-release",
            "branch": "master",
        },
        "build": {
            "job_name": "lustre-reviews",
            "build_number": 120,
            "url": BUILD_URL,
            "result": "FAILURE",
            "started_at": "2026-09-04T12:00:00+00:00",
            "completed_at": "2026-09-04T12:01:00+00:00",
            "duration_ms": 60000,
        },
        "matrix_runs": [],
        "parent_console_tail": ["failed"],
        "parent_console": {
            "lines": ["failed"], "excerpt_sha256": "b" * 64,
            "bytes_read": 6, "truncated": False,
        },
        "failed_runs": [],
        "captured_at": "2026-09-04T12:02:00+00:00",
    }
    for section, section_changes in changes.items():
        if section in {"change", "build"}:
            value[section].update(section_changes)
        else:
            value[section] = section_changes
    stable = {
        key: item for key, item in value.items()
        if key not in {"captured_at", "snapshot_sha256"}
    }
    value["snapshot_sha256"] = hashlib.sha256(json.dumps(
        stable, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return value


def current_status(**changes):
    value = {
        "change_number": 68541,
        "patchset": 3,
        "revision_sha": REVISION,
        "project": "fs/lustre-release",
        "branch": "master",
        "status": "NEW",
    }
    value.update(changes)
    return value


class FakeWriter:
    def __init__(self, observations=(), *, fail=False):
        self.observations = list(observations)
        self.fail = fail
        self.retrigger_count = 0
        self.observation_count = 0

    def retrigger(self, **_request):
        self.retrigger_count += 1
        if self.fail:
            raise TimeoutError("outcome unknown")
        return "/queue/item/999/"

    def observe_matching_retrigger(self, **_request):
        self.observation_count += 1
        if not self.observations:
            return None
        return self.observations.pop(0)


class JenkinsRetriggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = JenkinsRetriggerStore(self.root / "jenkins.sqlite3")
        self.snapshot = failure_snapshot()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def request(**changes):
        value = {
            "idempotency_key": "retrigger-once",
            "run_id": "run-1",
            "session_id": "session-1",
            "requested_by": "operator",
            "action_budget": 1,
            "actions_used": 0,
        }
        value.update(changes)
        return value

    def controller(
        self, writer=None, *, statuses=None, snapshots=None,
        enabled=True, authorized=True,
    ):
        statuses = iter(statuses or [current_status()] * 8)
        snapshots = iter(snapshots or [self.snapshot] * 8)
        writer = writer or FakeWriter()
        controller = JenkinsRetriggerController(
            self.store,
            writer,
            status_fetcher=lambda _change: next(statuses),
            failure_fetcher=lambda _url, **_identity: next(snapshots),
            capability_check=lambda binding: (
                authorized
                and binding["capability"] == JENKINS_RETRIGGER_CAPABILITY
                and binding["run_id"] == "run-1"
            ),
            enabled=enabled,
        )
        return controller, writer

    def test_dedicated_kill_switch_and_capability_default_fail_closed(self):
        for options in ({"enabled": False}, {"authorized": False}):
            controller, writer = self.controller(**options)
            with self.assertRaises(JenkinsRetriggerConflict):
                controller.prepare(snapshot=self.snapshot, **self.request())
            self.assertEqual(writer.retrigger_count, 0)
            self.assertIsNone(self.store.get_by_run("run-1"))

    def test_budget_is_required_and_consumed_exactly_once(self):
        controller, _writer = self.controller()
        for budget, used in ((0, 0), (1, 1), (2, 2), (1, -1)):
            with self.assertRaises(JenkinsRetriggerConflict):
                controller.prepare(
                    snapshot=self.snapshot,
                    **self.request(action_budget=budget, actions_used=used),
                )
        plan = controller.prepare(
            snapshot=self.snapshot,
            **self.request(action_budget=3, actions_used=2),
        )
        self.assertEqual(plan.action_budget, 3)
        self.assertEqual(plan.action_ordinal, 3)

    def test_snapshot_digest_and_whamcloud_url_are_fail_closed(self):
        controller, writer = self.controller()
        tampered = failure_snapshot()
        tampered["build"]["build_number"] = 121
        with self.assertRaises(JenkinsRetriggerConflict):
            controller.prepare(snapshot=tampered, **self.request())
        hostile = failure_snapshot(build={
            "url": "https://evil.example/job/lustre-reviews/120/",
        })
        with self.assertRaises(JenkinsRetriggerConflict):
            controller.prepare(snapshot=hostile, **self.request())
        folder = failure_snapshot(build={
            "job_name": "folder/lustre-reviews",
            "url": "https://build.whamcloud.com/job/folder/job/lustre-reviews/120/",
        })
        with self.assertRaises(JenkinsRetriggerConflict):
            controller.prepare(snapshot=folder, **self.request())
        self.assertEqual(writer.retrigger_count, 0)

    def test_prepare_is_idempotent_and_original_build_is_one_use(self):
        controller, _writer = self.controller()
        first = controller.prepare(snapshot=self.snapshot, **self.request())
        second = controller.prepare(snapshot=self.snapshot, **self.request())
        self.assertEqual(first.action_id, second.action_id)
        self.assertEqual(
            self.store.get_by_build_url(BUILD_URL).action_id, first.action_id,
        )
        with self.assertRaises(JenkinsRetriggerConflict):
            controller.prepare(
                snapshot=self.snapshot,
                **self.request(
                    run_id="run-2", session_id="session-2",
                    idempotency_key="another-key",
                ),
            )

    def test_prepare_stale_result_requires_human_and_never_writes(self):
        controller, writer = self.controller(statuses=[
            current_status(patchset=4, revision_sha="c" * 40),
        ])
        result = controller.prepare(snapshot=self.snapshot, **self.request())
        self.assertEqual(result.state, "stale")
        self.assertTrue(result.requires_human)
        self.assertEqual(result.reason_code, "binding_changed")
        self.assertEqual(writer.retrigger_count, 0)

    def test_final_preflight_rechecks_exact_revision_and_snapshot(self):
        controller, writer = self.controller(statuses=[
            current_status(),
            current_status(patchset=4, revision_sha="c" * 40),
        ])
        plan = controller.prepare(snapshot=self.snapshot, **self.request())
        result = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "stale")
        self.assertEqual(writer.retrigger_count, 0)

    def test_new_exact_build_racing_after_prepare_blocks_dispatch(self):
        existing = {
            "build_number": 121,
            "url": "https://build.whamcloud.com/job/lustre-reviews/121/",
        }
        controller, writer = self.controller(FakeWriter([None, existing]))
        plan = controller.prepare(snapshot=self.snapshot, **self.request())
        result = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.reason_code, "already_retriggered")
        self.assertEqual(writer.retrigger_count, 0)

    def test_success_is_observed_and_repeat_execute_never_posts_twice(self):
        observed = {
            "build_number": 121,
            "url": "https://build.whamcloud.com/job/lustre-reviews/121/",
        }
        controller, writer = self.controller(FakeWriter([None, None, observed]))
        plan = controller.prepare(snapshot=self.snapshot, **self.request())
        result = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.retrigger_build_number, 121)
        again = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(again.state, "succeeded")
        self.assertEqual(writer.retrigger_count, 1)

    def test_timeout_after_post_reconciles_without_retry(self):
        observed = {
            "build_number": 121,
            "url": "https://build.whamcloud.com/job/lustre-reviews/121/",
        }
        controller, writer = self.controller(
            FakeWriter([None, None, observed], fail=True)
        )
        plan = controller.prepare(snapshot=self.snapshot, **self.request())
        result = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(writer.retrigger_count, 1)

    def test_ambiguous_dispatch_never_retries_and_can_later_reconcile(self):
        observed = {
            "build_number": 121,
            "url": "https://build.whamcloud.com/job/lustre-reviews/121/",
        }
        writer = FakeWriter([None, None, None, observed], fail=True)
        controller, writer = self.controller(writer)
        plan = controller.prepare(snapshot=self.snapshot, **self.request())
        first = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(first.state, "ambiguous")
        self.assertTrue(first.requires_human)
        self.assertEqual(first.reason_code, "not_observed")
        second = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(second.state, "succeeded")
        self.assertEqual(writer.retrigger_count, 1)

    def test_existing_newer_exact_build_blocks_duplicate_before_dispatch(self):
        existing = {
            "build_number": 121,
            "url": "https://build.whamcloud.com/job/lustre-reviews/121/",
        }
        controller, writer = self.controller(FakeWriter([existing]))
        result = controller.prepare(snapshot=self.snapshot, **self.request())
        self.assertEqual(result.state, "failed")
        self.assertTrue(result.requires_human)
        self.assertEqual(result.reason_code, "already_retriggered")
        self.assertEqual(writer.retrigger_count, 0)

    def test_confirmation_digest_and_execute_capability_are_rechecked(self):
        capability = {"enabled": True}
        writer = FakeWriter()
        controller = JenkinsRetriggerController(
            self.store, writer,
            status_fetcher=lambda _change: current_status(),
            failure_fetcher=lambda _url, **_identity: self.snapshot,
            capability_check=lambda binding: (
                capability["enabled"]
                and binding["capability"] == JENKINS_RETRIGGER_CAPABILITY
            ),
            enabled=True,
        )
        plan = controller.prepare(snapshot=self.snapshot, **self.request())
        with self.assertRaises(JenkinsRetriggerConflict):
            controller.execute(plan.action_id, expected_binding_digest="0" * 64)
        capability["enabled"] = False
        result = controller.execute(
            plan.action_id, expected_binding_digest=plan.binding_digest,
        )
        self.assertEqual(result.state, "failed")
        self.assertTrue(result.requires_human)
        self.assertEqual(result.reason_code, "capability_denied")
        self.assertEqual(writer.retrigger_count, 0)


class FakeJenkinsClient:
    def __init__(self):
        self.user = "controller-only"
        self.token = "must-not-serialize"
        self.retriggers = []

    def retrigger_build(self, job_name, build_number):
        self.retriggers.append((job_name, build_number))
        return "/queue/item/1/"

    def get_builds(self, job_name, limit):
        return [{"number": 119}, {"number": 121}, {"number": 122}]

    def get_build(self, job_name, number):
        params = {
            "GERRIT_CHANGE_NUMBER": "68541",
            "GERRIT_PATCHSET_NUMBER": "3",
            "GERRIT_PATCHSET_REVISION": REVISION,
            "GERRIT_REFSPEC": REF,
            "GERRIT_PROJECT": "fs/lustre-release",
            "GERRIT_BRANCH": "master",
        }
        if number == 122:
            params["GERRIT_PATCHSET_NUMBER"] = "4"
        return {
            "number": number,
            "url": f"https://build.whamcloud.com/job/{job_name}/{number}/",
            "actions": [{"parameters": [
                {"name": key, "value": value} for key, value in params.items()
            ]}],
        }


class JenkinsToolWriterTests(unittest.TestCase):
    def test_reuses_narrow_tool_calls_and_filters_exact_revision(self):
        client = FakeJenkinsClient()
        writer = JenkinsToolRetriggerWriter(client)
        self.assertEqual(
            writer.retrigger(job_name="lustre-reviews", build_number=120),
            "/queue/item/1/",
        )
        observed = writer.observe_matching_retrigger(
            job_name="lustre-reviews", original_build_number=120,
            change_number=68541, patchset=3, revision_sha=REVISION,
            revision_ref=REF, project="fs/lustre-release", branch="master",
        )
        self.assertEqual(observed["build_number"], 121)
        self.assertEqual(client.retriggers, [("lustre-reviews", 120)])
        self.assertNotIn(client.token, repr(writer))


if __name__ == "__main__":
    unittest.main()
