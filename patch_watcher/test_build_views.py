import unittest

from patch_watcher.build_views import (
    render_build_result,
    render_build_start_confirmation,
    render_build_start_control,
)


class BuildViewsTests(unittest.TestCase):
    def patch(self, **updates):
        value = {
            "change_number": 68541,
            "patchset": 3,
            "revision_sha": "d" * 40,
            "revision_ref": "refs/changes/41/68541/3",
            "project": "fs/lustre-release",
            "lifecycle": "Open",
            "active_run_id": "",
        }
        value.update(updates)
        return value

    def snapshot(self, **updates):
        value = {
            "complete": True,
            "change": {
                "change_number": 68541,
                "patchset": 3,
                "revision_sha": "d" * 40,
            },
            "build": {
                "job_name": "lustre-reviews",
                "build_number": 42,
                "build_url": "https://build.whamcloud.com/job/lustre-reviews/42/",
                "result": "FAILURE",
            },
            "snapshot_sha256": "a" * 64,
        }
        value.update(updates)
        return value

    def test_start_control_is_one_exact_explicitly_eligible_action(self):
        html = render_build_start_control(
            self.patch(), self.snapshot(), csrf_token="csrf",
            idempotency_token="request", build_eligible=True,
        )

        self.assertEqual(html.count("<form"), 1)
        self.assertIn("Handle build failure…", html)
        self.assertIn("lustre-reviews", html)
        self.assertIn("#42", html)
        self.assertIn("d" * 40, html)
        self.assertIn("a" * 64, html)
        self.assertIn("action='/build-runs/prepare'", html)
        self.assertIn("open-ended", html)
        self.assertIn("LTVM guests it owns", html)
        self.assertNotIn(" disabled", html)

    def test_start_control_fails_closed_on_flags_snapshot_and_active_owner(self):
        cases = [
            ({}, self.snapshot(), False, "not explicitly eligible"),
            ({"active_run_id": "pw-build-active"}, self.snapshot(), True, "already owns"),
            ({}, self.snapshot(complete=False), True, "complete failed Jenkins"),
            ({}, self.snapshot(change={"change_number": 68541, "patchset": 3,
                                       "revision_sha": "e" * 40}), True, "complete failed Jenkins"),
        ]
        for patch_updates, snapshot, eligible, reason in cases:
            with self.subTest(reason=reason):
                html = render_build_start_control(
                    self.patch(**patch_updates), snapshot, csrf_token="csrf",
                    idempotency_token="request", build_eligible=eligible,
                )
                self.assertIn(" disabled aria-disabled='true'", html)
                self.assertIn(reason, html)

    def test_only_allowlisted_https_jenkins_url_becomes_a_link(self):
        safe = render_build_start_control(
            self.patch(), self.snapshot(), csrf_token="csrf", idempotency_token="request",
            build_eligible=True,
        )
        self.assertIn("href='https://build.whamcloud.com/job/lustre-reviews/42/'", safe)

        for build_url in ("javascript:alert(1)", "https://build.whamcloud.com.evil/job/x/42/"):
            with self.subTest(build_url=build_url):
                malicious = self.snapshot(build={
                    "job_name": "<script>bad()</script>", "build_number": 42,
                    "build_url": build_url, "result": "FAILURE",
                })
                unsafe = render_build_start_control(
                    self.patch(), malicious, csrf_token="<csrf>", idempotency_token="request",
                    build_eligible=True,
                )
                self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", unsafe)
                self.assertNotIn("<script>", unsafe)
                self.assertNotIn(build_url, unsafe)
                self.assertNotIn("href=", unsafe)
                self.assertIn("value='&lt;csrf&gt;'", unsafe)

    def test_confirmation_is_single_approval_with_exact_boundary_and_binding(self):
        html = render_build_start_confirmation(
            self.patch(), self.snapshot(), confirmation_token="signed",
            idempotency_token="request", confirmation_expires_at="123",
            csrf_token="csrf",
        )
        self.assertIn("lustre-reviews", html)
        self.assertIn("#42", html)
        self.assertIn("d" * 40, html)
        self.assertIn("a" * 64, html)
        self.assertIn("single approval", html)
        self.assertIn("open-ended commands", html)
        self.assertIn("a host shell, the installed LLM tools, and real service credentials", html)
        self.assertIn("its own LTVM guests", html)
        self.assertIn("uploads the new patchset itself with the gerrit CLI", html)
        self.assertIn("The controller does not upload on its behalf", html)
        self.assertIn("There is no later upload confirmation", html)
        self.assertEqual(html.count("<form"), 1)
        self.assertIn("action='/build-runs/start'", html)

    def test_confirmation_rejects_a_mismatched_or_nonfailed_snapshot(self):
        for snapshot in (
            self.snapshot(complete=False),
            self.snapshot(build={
                "job_name": "lustre-reviews", "build_number": 42,
                "build_url": "https://build.whamcloud.com/job/lustre-reviews/42/",
                "result": "SUCCESS",
            }),
            self.snapshot(change={
                "change_number": 68541, "patchset": 4, "revision_sha": "d" * 40,
            }),
        ):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                render_build_start_confirmation(
                    self.patch(), snapshot, confirmation_token="signed",
                    idempotency_token="request", confirmation_expires_at="123",
                    csrf_token="csrf",
                )

    def test_result_displays_diagnosis_validation_and_escalation(self):
        request = {
            "request_kind": "build_failure",
            "build_snapshot": self.snapshot(),
        }
        report = {
            "state": "needs_human",
            "diagnosis": "Compiler rejected <bad>.",
            "validation": {
                "state": "passed",
                "summary": "Relevant build passed.",
                "evidence": [{"label": "make check", "summary": "12 tests <passed>"}],
            },
            "human_escalation": {
                "reason": "Rebuild outcome is <uncertain>.",
                "question": "Reconcile manually?",
                "recommended_default": "Do not retry.",
            },
        }

        html = render_build_result(request, report)

        self.assertIn("Compiler rejected &lt;bad&gt;.", html)
        self.assertIn("Status: <strong>passed</strong>", html)
        self.assertIn("12 tests &lt;passed&gt;", html)
        self.assertIn("role='alert'", html)
        self.assertIn("Rebuild outcome is &lt;uncertain&gt;.", html)
        self.assertIn("Do not retry.", html)
        self.assertNotIn("<bad>", html)
        self.assertNotIn("/uploads/", html)
        self.assertNotIn("Confirm upload", html)

    def test_result_is_rendered_only_for_a_build_failure_run(self):
        report = {"state": "needs_human", "diagnosis": "Compiler rejected the patch."}
        # Positive control: the same report against the right request kind has
        # to produce the section, or "" would be a correct answer everywhere
        # and the exclusions below would prove nothing.
        rendered = render_build_result(
            {"request_kind": "build_failure", "build_snapshot": self.snapshot()},
            report,
        )
        self.assertIn("Build-failure handling", rendered)
        self.assertIn("Compiler rejected the patch.", rendered)
        for request in ({"request_kind": "engineering"}, {"request_kind": "research"},
                        {"request_kind": ""}, {}, None):
            with self.subTest(request=request):
                self.assertEqual(render_build_result(request, report), "")


if __name__ == "__main__":
    unittest.main()
