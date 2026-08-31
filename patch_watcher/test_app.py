import tempfile
import re
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import app
from failure_actions import FailureActionController, FailurePatchRevision
from maloo_adapter import (
    MalooBugLinks,
    MalooLinkBugResult,
)


class PatchWatcherTests(unittest.TestCase):
    def setUp(self):
        app.PATCHES.clear()

    def tearDown(self):
        if app.AUTOMATION_OBSERVER is not None:
            app.AUTOMATION_OBSERVER.stop()
        if app.RUN_CONTROLLER is not None:
            app.RUN_CONTROLLER.stop()
        app.RUN_CONTROLLER = None
        app.RETEST_CONTROLLER = None
        app.FAILURE_ACTION_CONTROLLER = None
        app.AUTOMATION_OBSERVER = None
        app.AUTOMATION_STORE = None
        app.SESSION_STORE = None
        app.WORKER_PROFILE = None
        app.RESOURCE_COLLECTION_ENABLED = False
        app._RESOURCE_SNAPSHOT = None
        app._RESOURCE_SNAPSHOT_MONOTONIC = 0.0

    def test_accepts_change_url_and_defaults_title(self):
        patch_record, error = app.add_patch(" https://review.whamcloud.com/c/123/ ")
        self.assertIsNone(error)
        self.assertEqual(patch_record["url"], "https://review.whamcloud.com/c/123")
        self.assertEqual(patch_record["title"], "123")
        self.assertEqual(patch_record["status"], "Pending")
        self.assertEqual(patch_record["lifecycle"], "Open")
        self.assertIn("last_updated", patch_record)

    def test_accepts_full_canonical_change_url(self):
        value = "https://review.whamcloud.com/c/fs/lustre-release/+/61965/3"
        self.assertTrue(app.valid_url(value))
        patch_record, _ = app.add_patch(value)
        self.assertEqual(patch_record["title"], "61965")

    def test_rejects_non_whamcloud_urls_and_non_change_paths(self):
        for url in (
            "http://review.whamcloud.com/c/1",
            "https://example.com/c/1",
            "https://review.whamcloud.com/changes/1",
            "https://review.whamcloud.com/c/",
            "https://review.whamcloud.com/c/fs/lustre-release/+not-a-change",
        ):
            self.assertFalse(app.valid_url(url), url)

    def test_duplicate_is_rejected(self):
        app.add_patch("https://review.whamcloud.com/c/1", "First")
        patch_record, error = app.add_patch(
            "https://review.whamcloud.com/c/1", "Again"
        )
        self.assertIsNone(patch_record)
        self.assertIn("already", error)

    def test_page_escapes_user_values(self):
        app.add_patch("https://review.whamcloud.com/c/1", "<unsafe>")
        rendered = app.page()
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertNotIn("<unsafe>", rendered)

    def test_post_parser_rejects_unsupported_oversized_and_invalid_forms(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        requests = [
            Request(
                base + "/add",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            Request(
                base + "/add",
                data=b"x" * (app.MAX_FORM_BODY_BYTES + 1),
                method="POST",
            ),
            Request(
                base + "/add",
                data=b"url=\xff",
                method="POST",
            ),
        ]
        try:
            for request, expected in zip(requests, (415, 413, 400)):
                with self.subTest(expected=expected):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request)
                    self.assertEqual(caught.exception.code, expected)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_page_displays_review_and_ci_criteria_as_links(self):
        patch_record, _ = app.add_patch(
            "https://review.whamcloud.com/c/7", "LU-12345 improve watcher"
        )
        patch_record.update(
            patchset=3,
            wip=True,
            review="Ready",
            review_votes=[{"name": "Reviewer", "value": 1}],
            unresolved=2,
            jenkins="PASS",
            jenkins_url="https://build.whamcloud.com/job/lustre-reviews/42/",
            maloo="RUNNING",
            maloo_url="https://testing.whamcloud.com/test_sessions/related?jobs=x&amp=y",
        )
        rendered = app.page()
        for value in (
            "3", "WIP", "Ready", "Reviewer +1",
            "2 unresolved", "PASS", "RUNNING",
        ):
            self.assertIn(value, rendered)
        self.assertIn("href='https://review.whamcloud.com/c/7'", rendered)
        self.assertIn("href='https://jira.whamcloud.com/browse/LU-12345'", rendered)
        self.assertIn("jobs=x&amp;amp=y", rendered)

    def test_review_chips_distinguish_ready_clean_needs_and_veto(self):
        ready = app._review_chip({"review": "Ready"})
        clean = app._review_chip({
            "review": "Pending", "jenkins": "PASS", "maloo": "PASS",
            "unresolved": 0,
        })
        needs = app._review_chip({
            "review": "Pending", "jenkins": "RUNNING", "maloo": "—",
            "unresolved": 0,
        })
        veto = app._review_chip({"review": "Veto"})
        self.assertIn("✓ Ready", ready)
        self.assertIn("tone-good", ready)
        self.assertIn("✓ Clean", clean)
        self.assertIn("tone-info", clean)
        self.assertIn("! Needs", needs)
        self.assertIn("tone-warn", needs)
        self.assertIn("✕ Veto", veto)
        self.assertIn("tone-bad", veto)

    def test_ci_chips_include_service_state_text_and_tone(self):
        passed = app._ci_chip("Jenkins", "PASS")
        failed = app._ci_chip("Maloo", "FAIL")
        running = app._ci_chip("Maloo", "RUNNING")
        self.assertIn("✓ Jenkins pass", passed)
        self.assertIn("tone-good", passed)
        self.assertIn("✕ Maloo fail", failed)
        self.assertIn("tone-bad", failed)
        self.assertIn("… Maloo running", running)
        self.assertIn("tone-warn", running)

    def test_watch_state_chips_are_accessibly_labelled(self):
        attention = app._watch_chip("needs-attention")
        ready = app._watch_chip("ready")
        waiting = app._watch_chip("awaiting-ci")
        merged = app._watch_chip("merged")
        abandoned = app._watch_chip("abandoned")
        self.assertIn("✕ Needs Attention", attention)
        self.assertIn("tone-bad", attention)
        self.assertIn("✓ Ready", ready)
        self.assertIn("! Awaiting Ci", waiting)
        self.assertIn("Merged", merged)
        self.assertIn("tone-good", merged)
        self.assertIn("Abandoned", abandoned)
        self.assertIn("tone-bad", abandoned)

    def test_table_folds_lifecycle_ci_and_patchset_into_compact_columns(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/13")
        patch_record.update(patchset=7, wip=False, jenkins="PASS", maloo="RUNNING")
        rendered = app.page()
        self.assertNotIn("<th>Lifecycle</th>", rendered)
        self.assertNotIn("<th>Jenkins / Maloo</th>", rendered)
        self.assertNotIn("<th>Patchset</th>", rendered)
        self.assertIn("<th>Watch state / CI</th>", rendered)
        self.assertIn("PS 7", rendered)
        self.assertNotIn(">Active<", rendered)
        self.assertIn("Jenkins pass", rendered)
        self.assertIn("Maloo running", rendered)

    def test_wip_is_shown_only_when_set(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/14")
        patch_record.update(patchset=3, wip=True)
        self.assertIn("! WIP", app.page())

    def test_table_has_only_global_refresh_and_overall_checked_time(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/11")
        patch_record["last_checked"] = "2026-08-29T21:00:00+00:00"
        rendered = app.page()
        self.assertIn("Overall last checked: 2026-08-29T21:00:00+00:00", rendered)
        self.assertEqual(rendered.count("action='/refresh-all'"), 1)
        self.assertNotIn("action='/refresh'", rendered)
        self.assertNotIn("<th>Last checked</th>", rendered)

    def test_add_form_accepts_url_only(self):
        rendered = app.page()
        self.assertIn("name='url'", rendered)
        self.assertNotIn("name='title'", rendered)

    def test_page_includes_disabled_review_handling_stubs(self):
        rendered = app.page()
        self.assertIn("Handle reviews", rendered)
        self.assertIn("Handle simple comments", rendered)
        self.assertIn("Handle all comments", rendered)
        self.assertIn("Stub · disabled", rendered)
        self.assertEqual(rendered.count("aria-disabled='true'"), 2)
        self.assertNotIn("action='/handle-review", rendered)

    def test_retest_automation_defaults_globally_and_per_patch_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(Path(temp_dir) / "automation.sqlite3")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160,
                patchset=4,
                revision_sha="d" * 40,
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            rendered = app.page()
            global_enabled = store.get_global_automation().enabled
            policy_mode = store.get_policy("68160").mode
        self.assertFalse(global_enabled)
        self.assertEqual(policy_mode, "disabled")
        self.assertIn("Global execution: Disabled", rendered)
        self.assertIn("Test failure handling: <strong>Disabled", rendered)

    def test_global_automation_enable_get_is_display_only_then_post_mutates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(Path(temp_dir) / "automation.sqlite3")
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                body = urlopen(base + "/automation/global/confirm-enable").read().decode()
                self.assertIn("Enable automatic Maloo retests?", body)
                self.assertFalse(store.get_global_automation().enabled)
                request = Request(
                    base + "/automation/global/enable",
                    data=urlencode({"csrf_token": app.CSRF_TOKEN}).encode(),
                    method="POST",
                )
                urlopen(request).read()
                self.assertTrue(store.get_global_automation().enabled)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_automatic_patch_policy_requires_separate_confirmation_post(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(Path(temp_dir) / "automation.sqlite3")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160,
                patchset=4,
                revision_sha="d" * 40,
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            values = {
                "csrf_token": app.CSRF_TOKEN,
                "change_number": "68160",
                "revision_sha": "d" * 40,
                "max_actions": "1",
            }
            try:
                request = Request(
                    base + "/automation/policy",
                    data=urlencode({**values, "mode": "automatic"}).encode(),
                    method="POST",
                )
                body = urlopen(request).read().decode()
                self.assertIn("Set this patch to Automatic?", body)
                self.assertEqual(store.get_policy("68160").mode, "disabled")
                request = Request(
                    base + "/automation/policy/confirm",
                    data=urlencode(values).encode(),
                    method="POST",
                )
                urlopen(request).read()
                self.assertEqual(store.get_policy("68160").mode, "automatic")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_unknown_failure_research_defaults_disabled_and_is_visible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(
                change_number=68160,
                project="fs/lustre-release",
                patchset=4,
                revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            rendered = app.page()
            policy = store.get_research_policy("68160")
        self.assertEqual(policy.mode, "disabled")
        self.assertEqual(policy.run_budget, 0)
        self.assertIn("Research trigger policy", rendered)
        self.assertIn("Unknown-failure investigation", rendered)
        self.assertIn("Read-only", rendered)

    def test_automatic_research_policy_uses_display_only_get_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(
                change_number=68160,
                project="fs/lustre-release",
                patchset=4,
                revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            version = store.get_research_policy("68160").version
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            values = {
                "csrf_token": app.CSRF_TOKEN,
                "change_number": "68160",
                "patchset": "4",
                "revision_sha": "d" * 40,
                "research_mode": "automatic",
                "per_revision_run_budget": "2",
                "expected_policy_version": version,
                "idempotency_token": "policy-proposal-1",
            }
            try:
                request = Request(
                    base + "/research/policy/prepare",
                    data=urlencode(values).encode(),
                    method="POST",
                )
                confirmation = urlopen(request).read().decode()
                self.assertIn("Confirm automatic unknown-failure research", confirmation)
                self.assertEqual(store.get_research_policy("68160").mode, "disabled")
                confirm_token = re.search(
                    r"name='confirmation_token' value='([^']+)'", confirmation
                ).group(1)
                final = Request(
                    base + "/research/policy/confirm",
                    data=urlencode({
                        **values,
                        "confirmation_token": confirm_token,
                    }).encode(),
                    method="POST",
                )
                urlopen(final).read()
                policy = store.get_research_policy("68160")
                self.assertEqual(policy.mode, "automatic")
                self.assertEqual(policy.run_budget, 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_manual_unknown_failure_starts_with_pinned_normalized_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(
                change_number=68160,
                project="fs/lustre-release",
                patchset=4,
                revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            store.set_research_policy(
                "68160", mode="manual", run_budget=1, updated_by="operator"
            )
            store.record_observation(
                "68160",
                revision="d" * 40,
                source="gerrit+maloo",
                kind="maloo_retest_evaluation",
                fingerprint="sha256:" + "a" * 64,
                payload={
                    "snapshot": {
                        "maloo_state_complete": True,
                        "maloo_failures": [{
                            "session_id": "session-1",
                            "test_group": "review-dne-part-1",
                            "suite": "sanity",
                            "enforced": True,
                            "linked_bugs": [],
                            "failing_subtests": ["101"],
                            "remote_failure_id": "suite-1",
                        }],
                    }
                },
            )

            class FakeRunController:
                def __init__(self):
                    self.calls = []

                def stop(self):
                    return None

                def request_unknown_failure_investigation(
                    self, evidence, *, attempt_id, trigger
                ):
                    self.calls.append((evidence, attempt_id, trigger))
                    return SimpleNamespace(
                        run_id="research-1", session_id="session-1", created=True
                    )

            controller = FakeRunController()
            app.RUN_CONTROLLER = controller
            request = app._start_unknown_failure_research(
                patch_record, automatic=False
            )
            evidence, attempt_id, trigger = controller.calls[0]
            admission = store.list_research_admissions(
                patch_id="68160", revision="d" * 40
            )[0]
            self.assertEqual(admission.state, "registered")
            self.assertEqual(admission.session_id, "session-1")

            store.set_research_policy(
                "68160", mode="manual", run_budget=2, updated_by="operator"
            )

            class FailingRunController:
                def request_unknown_failure_investigation(self, *args, **kwargs):
                    raise RuntimeError("session database unavailable")

            app.RUN_CONTROLLER = FailingRunController()
            with self.assertRaisesRegex(RuntimeError, "session database"):
                app._start_unknown_failure_research(
                    patch_record, automatic=False, attempt_id="manual-retry-2"
                )
            released = store.list_research_admissions(
                patch_id="68160", revision="d" * 40
            )[-1]
            self.assertEqual(released.state, "released")
            self.assertIn("RuntimeError", released.failure_summary)

            store.set_research_policy(
                "68160", mode="manual", run_budget=3, updated_by="operator"
            )

            class ReconciledRunController:
                def __init__(self):
                    self.calls = 0

                def stop(self):
                    return None

                def request_unknown_failure_investigation(self, *args, **kwargs):
                    self.calls += 1
                    return SimpleNamespace(
                        run_id="research-3",
                        session_id="session-3",
                        created=self.calls == 1,
                    )

            app.RUN_CONTROLLER = ReconciledRunController()
            register = store.register_research_admission
            failures = [True]

            def fail_registration_once(*args, **kwargs):
                if failures.pop():
                    raise OSError("admission database interrupted")
                return register(*args, **kwargs)

            store.register_research_admission = fail_registration_once
            with self.assertRaisesRegex(OSError, "admission database"):
                app._start_unknown_failure_research(
                    patch_record, automatic=False, attempt_id="manual-retry-3"
                )
            store.register_research_admission = register
            reconciled = app._start_unknown_failure_research(
                patch_record, automatic=False, attempt_id="manual-retry-3"
            )
            self.assertFalse(reconciled.created)
            admission = store.list_research_admissions(
                patch_id="68160", revision="d" * 40
            )[-1]
            self.assertEqual(admission.state, "registered")
            self.assertEqual(admission.session_id, "session-3")
        self.assertEqual(request.run_id, "research-1")
        self.assertEqual(evidence["revision_sha"], "d" * 40)
        self.assertEqual(evidence["records"][0]["payload"]["suite"], "sanity")
        self.assertTrue(attempt_id.startswith("manual:"))
        self.assertEqual(trigger["kind"], "manual")

    def test_automatic_research_respects_global_execution_kill_switch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(
                change_number=68160,
                project="fs/lustre-release",
                patchset=4,
                revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            store.set_research_policy(
                "68160", mode="automatic", run_budget=1, updated_by="operator"
            )
            store.record_observation(
                "68160",
                revision="d" * 40,
                source="gerrit+maloo",
                kind="maloo_retest_evaluation",
                fingerprint="sha256:" + "b" * 64,
                payload={"snapshot": {
                    "maloo_state_complete": True,
                    "maloo_failures": [{
                        "session_id": "session-1",
                        "test_group": "review-dne-part-1",
                        "suite": "sanity",
                        "enforced": True,
                        "linked_bugs": [],
                        "remote_failure_id": "suite-1",
                    }],
                }},
            )

            class FakeResearchController:
                def __init__(self):
                    self.calls = []

                def stop(self):
                    return None

                def request_unknown_failure_investigation(
                    self, evidence, *, attempt_id, trigger
                ):
                    created = not self.calls
                    self.calls.append((evidence, attempt_id, trigger))
                    return SimpleNamespace(
                        run_id="research-1", session_id="session-1", created=created
                    )

            class FakeRetestController:
                def tick_patch(self, patch, **options):
                    return SimpleNamespace(patch_id="68160")

            research = FakeResearchController()
            app.RUN_CONTROLLER = research
            app.RETEST_CONTROLLER = FakeRetestController()
            app._observe_patch_automation(patch_record)
            self.assertEqual(research.calls, [])
            store.set_global_automation(
                True, changed_by="operator", reason="test"
            )
            app._observe_patch_automation(patch_record)
            self.assertEqual(len(research.calls), 1)
            app._observe_patch_automation(patch_record)
            self.assertEqual(len(research.calls), 2)
            decisions = [
                item for item in store.list_observations("68160")
                if item.kind == "unknown_failure_research_trigger_decision"
            ]
            self.assertEqual(decisions[-1].payload["status"], "already_exists")

    def test_approved_failure_route_plans_inertly_then_executes_one_link(self):
        class FakeMaloo:
            def __init__(self):
                self.link_calls = []

            def get_bug_links(self, suite_id, related=False):
                return MalooBugLinks(suite_id, ())

            def get_enforced_failures(self, change_number, patchset):
                return (SimpleNamespace(
                    session=SimpleNamespace(
                        session_id="11111111-2222-3333-4444-555555555555",
                        test_group="review-dne-part-1",
                    ),
                    failures=SimpleNamespace(failed_suites=(SimpleNamespace(
                        suite_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        suite="sanity",
                    ),)),
                ),)

            def link_bug(self, suite_id, jira_ticket, **options):
                self.link_calls.append((suite_id, jira_ticket, options))
                return MalooLinkBugResult(
                    suite_id, jira_ticket, "TestSet", "accepted", True, "OK"
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(
                change_number=68160,
                project="fs/lustre-release",
                patchset=4,
                revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            store.set_policy(
                "68160", mode="approval", action_budget=2,
                delivery_budget=0, updated_by="operator",
            )
            store.record_observation(
                "68160",
                revision="d" * 40,
                source="gerrit+maloo",
                kind="maloo_retest_evaluation",
                fingerprint="sha256:" + "c" * 64,
                payload={"snapshot": {
                    "maloo_state_complete": True,
                    "maloo_failures": [{
                        "session_id": "11111111-2222-3333-4444-555555555555",
                        "test_group": "review-dne-part-1",
                        "suite": "sanity",
                        "enforced": True,
                        "linked_bugs": [],
                        "remote_failure_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    }],
                }},
            )
            maloo = FakeMaloo()
            fresh = FailurePatchRevision(
                patch_id="68160",
                gerrit_url=patch_record["url"],
                change_number=68160,
                patchset_number=4,
                revision_sha="d" * 40,
            )
            app.FAILURE_ACTION_CONTROLLER = FailureActionController(
                store, maloo, revalidate=lambda _url: fresh,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            values = {
                "csrf_token": app.CSRF_TOKEN,
                "change_number": "68160",
                "patchset": "4",
                "revision_sha": "d" * 40,
                "session_id": "11111111-2222-3333-4444-555555555555",
                "test_group": "review-dne-part-1",
                "suite_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "jira_ticket": "LU-19487",
            }
            try:
                incomplete_values = dict(values)
                incomplete_values["session_id"] = ""
                incomplete = Request(
                    base + "/failure-actions/plan",
                    data=urlencode(incomplete_values).encode(),
                    method="POST",
                )
                rejected = urlopen(incomplete).read().decode()
                self.assertIn(
                    "That failure is not present in the latest complete",
                    rejected,
                )
                self.assertEqual(store.list_runs(), [])

                tampered_values = dict(values)
                tampered_values["suite_id"] = (
                    "ffffffff-ffff-ffff-ffff-ffffffffffff"
                )
                tampered = Request(
                    base + "/failure-actions/plan",
                    data=urlencode(tampered_values).encode(),
                    method="POST",
                )
                rejected = urlopen(tampered).read().decode()
                self.assertIn(
                    "That failure is not present in the latest complete",
                    rejected,
                )
                self.assertEqual(store.list_runs(), [])
                self.assertEqual(maloo.link_calls, [])

                request = Request(
                    base + "/failure-actions/plan",
                    data=urlencode(values).encode(),
                    method="POST",
                )
                confirmation = urlopen(request).read().decode()
                self.assertIn("Confirm JIRA association", confirmation)
                self.assertEqual(maloo.link_calls, [])
                action = store.list_actions(store.list_runs()[0].run_id)[0]
                confirmation_token = re.search(
                    r"name='confirmation_token' value='([^']+)'", confirmation
                ).group(1)
                approve = Request(
                    base + f"/approvals/{action.action_id}/approve",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "revision_sha": "d" * 40,
                        "confirmation_token": confirmation_token,
                    }).encode(),
                    method="POST",
                )
                urlopen(approve).read()
                self.assertEqual(maloo.link_calls, [])
                self.assertIn("Queued for execution", app.page())
                app._advance_failure_action_runs("68160")
                self.assertEqual(len(maloo.link_calls), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_failure_action_button_is_disabled_for_incomplete_observed_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(
                change_number=68160,
                project="fs/lustre-release",
                patchset=4,
                revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            store.set_policy(
                "68160", mode="approval", action_budget=2,
                delivery_budget=0, updated_by="operator",
            )
            store.record_observation(
                "68160",
                revision="d" * 40,
                source="gerrit+maloo",
                kind="maloo_retest_evaluation",
                fingerprint="sha256:" + "e" * 64,
                payload={"snapshot": {
                    "maloo_state_complete": True,
                    "maloo_failures": [{
                        "session_id": "",
                        "test_group": "review-dne-part-1",
                        "suite": "sanity",
                        "enforced": True,
                        "linked_bugs": [],
                        "remote_failure_id": (
                            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                        ),
                    }],
                }},
            )
            rendered = app.page()
        self.assertIn(
            "The exact Maloo session, test group, suite name, or suite ID is unavailable.",
            rendered,
        )
        self.assertRegex(
            rendered,
            r"<button type='submit' disabled aria-disabled='true'>Plan association</button>",
        )

    def test_refreshed_patch_offers_exact_read_only_investigation(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160,
            project="fs/lustre-release",
            patchset=4,
            revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4",
        )
        rendered = app.page()
        self.assertIn("Manual read-only investigation", rendered)
        self.assertIn("action='/runs/investigate'", rendered)
        self.assertIn("name='revision_sha'", rendered)
        self.assertIn("Read-only:", rendered)
        self.assertNotIn("name='revision_sha' value=''", rendered)

    def test_kill_confirmation_get_is_display_only_and_final_post_uses_one_time_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            store.register_pinned_session(
                "pw-session-1",
                patch_id="68160",
                run_id="run-1",
                revision="d" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                body = urlopen(base + "/runs/run-1/confirm?intent=kill").read().decode()
                self.assertIn("No action has been taken", body)
                self.assertEqual(store.list_control_intents("pw-session-1"), [])

                request = Request(
                    base + "/runs/run-1/confirm",
                    data=urlencode({
                        "intent": "kill", "csrf_token": app.CSRF_TOKEN,
                    }).encode(),
                    method="POST",
                )
                confirmation = urlopen(request).read().decode()
                intents = store.list_control_intents("pw-session-1")
                self.assertEqual(len(intents), 1)
                self.assertEqual(intents[0].status, "recorded")
                token = re.search(r"name='confirmation_token' value='([^']+)'", confirmation).group(1)
                request_id = re.search(r"name='idempotency_token' value='([^']+)'", confirmation).group(1)
                final = Request(
                    base + "/runs/run-1/kill",
                    data=urlencode({
                        "confirmation_token": token,
                        "idempotency_token": request_id,
                        "csrf_token": app.CSRF_TOKEN,
                    }).encode(),
                    method="POST",
                )
                urlopen(final).read()
                self.assertEqual(
                    store.list_control_intents("pw-session-1")[0].status,
                    "confirmed",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_page_places_live_resource_summary_before_patch_controls(self):
        snapshot = {
            "host_memory": {
                "sampled_at": "2026-08-30T18:00:00Z",
                "quality": "good",
                "total_bytes": 24 * 1024 ** 3,
                "used_bytes": 16 * 1024 ** 3,
                "available_bytes": 8 * 1024 ** 3,
            },
            "ltvm": {
                "vms": [{
                    "name": "worker-vm",
                    "state": "running",
                    "configured_guest_memory_bytes": 2 * 1024 ** 3,
                    "host_rss_bytes": 512 * 1024 ** 2,
                }],
            },
        }
        app.RESOURCE_COLLECTION_ENABLED = True
        with patch("app.collect_resource_snapshot", return_value=snapshot):
            rendered = app.page()
        self.assertIn("Worker host memory", rendered)
        self.assertIn("24 GiB", rendered)
        self.assertIn("worker-vm", rendered)
        self.assertIn("Configured guest memory", rendered)
        self.assertLess(rendered.index("Worker host memory"), rendered.index("Add a patch"))
        self.assertIn("action='/resources/refresh'", rendered)

    def test_page_shows_declared_worker_profile_before_patch_controls(self):
        rendered = app.page()
        self.assertIn("Worker admission and provenance", rendered)
        self.assertIn("Admission: Not checked", rendered)
        self.assertIn("host-unsandboxed-mac-v1", rendered)
        self.assertIn("Declared only isolation: Unsandboxed host worker", rendered)
        self.assertIn("Declared only network: General network access", rendered)
        self.assertLess(
            rendered.index("Worker admission and provenance"),
            rendered.index("Add a patch"),
        )

    def test_page_shows_persisted_worker_admission_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            store.register_session(
                "pw-session-1",
                patch_id="LU-12345",
                run_id="run-1",
                profile="engineering",
                state="queued",
            )
            store.record_worker_admission(
                "pw-session-1",
                profile_id="host-unsandboxed-mac-v1",
                profile_hash="sha256:" + "a" * 64,
                environment_instance_id="worker-build-7",
                status="blocked",
                isolation_profile="host_unsandboxed",
                network_profile="host_ambient",
                attestation={
                    "failure_codes": ["tool_version_mismatch"],
                    "warnings": [],
                    "executables": [],
                },
                instruction_hash="sha256:" + "b" * 64,
                failure_code="tool_version_mismatch",
                failure_summary="Python is older than the selected profile permits",
            )
            rendered = app.page()
        self.assertIn("Admission: Blocked", rendered)
        self.assertIn("worker-build-7", rendered)
        self.assertIn("tool_version_mismatch", rendered)
        self.assertIn("Python is older than", rendered)

    def test_resource_snapshot_is_cached_until_forced(self):
        snapshot = {"host_memory": {}, "ltvm": {"vms": []}}
        app.RESOURCE_COLLECTION_ENABLED = True
        with patch("app.collect_resource_snapshot", return_value=snapshot) as collect:
            self.assertIs(app.refresh_resource_status(), snapshot)
            self.assertIs(app.refresh_resource_status(), snapshot)
            self.assertIs(app.refresh_resource_status(force=True), snapshot)
        self.assertEqual(collect.call_count, 2)

    def test_managed_sessions_and_recent_messages_render_from_private_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            store.register_session(
                "pw-session-1",
                patch_id="LU-12345",
                run_id="run-1",
                profile="engineering",
                state="waiting_human",
            )
            store.record_message("pw-session-1", "agent", "Need a human decision")
            app._RESOURCE_SNAPSHOT = {"host_memory": {}, "ltvm": {"vms": []}}
            app._RESOURCE_SNAPSHOT_MONOTONIC = app.time.monotonic()
            rendered = app.resource_dashboard_html()
        self.assertIn("Active managed sessions (1)", rendered)
        self.assertIn("LU-12345", rendered)
        self.assertIn("Need a human decision", rendered)
        self.assertIn("State: Waiting human", rendered)
        # Resource inventory is observation-only. Phase 0C controls live on
        # the revision-pinned run detail page with token confirmation.
        self.assertNotIn("action='/sessions/guidance'", rendered)
        self.assertNotIn("action='/sessions/kill'", rendered)

    def test_ticket_requires_leading_issue_key(self):
        self.assertEqual(app.ticket_from_title("LU-12345: fix pages"), "LU-12345")
        self.assertEqual(app.ticket_from_title("EX-9 work"), "EX-9")
        self.assertEqual(app.ticket_from_title("fix mentions LU-1 later"), "")
        self.assertEqual(app.ticket_from_title("<LU-1>"), "")

    def test_jira_base_and_title_are_escaped(self):
        app.add_patch(
            "https://review.whamcloud.com/c/9", "LU-9 <script>alert(1)</script>"
        )
        rendered = app.page(jira_base="https://jira.example/browse?next=<bad>")
        self.assertIn("LU-9 &lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("next=&lt;bad&gt;/LU-9", rendered)

    def test_seed_file_loads_urls_and_refreshes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed = Path(temp_dir) / "patches.txt"
            seed.write_text(
                "# recent patches\n"
                "https://review.whamcloud.com/c/1\tLU-1 first\n"
                "https://review.whamcloud.com/c/2\n",
                encoding="utf-8",
            )
            with patch("app.refresh_patch") as refresh:
                loaded = app.load_seed_file(seed)
        self.assertEqual([item["url"] for item in loaded], [
            "https://review.whamcloud.com/c/1",
            "https://review.whamcloud.com/c/2",
        ])
        self.assertEqual(refresh.call_count, 2)

    def test_seed_file_rejects_bad_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed = Path(temp_dir) / "patches.txt"
            seed.write_text("https://example.com/c/1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid seed entry"):
                app.load_seed_file(seed)

    def test_watch_file_persists_urls_privately_and_reloads(self):
        app.add_patch("https://review.whamcloud.com/c/1", "Temporary title")
        app.add_patch("https://review.whamcloud.com/c/2")
        with tempfile.TemporaryDirectory() as temp_dir:
            watch_file = Path(temp_dir) / "config" / "patches.txt"
            app.save_watch_file(watch_file)
            self.assertEqual(
                watch_file.read_text(encoding="utf-8"),
                "https://review.whamcloud.com/c/1\n"
                "https://review.whamcloud.com/c/2\n",
            )
            self.assertEqual(watch_file.stat().st_mode & 0o777, 0o600)
            app.PATCHES.clear()
            with patch("app.refresh_patch") as refresh:
                loaded = app.load_seed_file(watch_file)
        self.assertEqual(
            [item["url"] for item in loaded],
            [
                "https://review.whamcloud.com/c/1",
                "https://review.whamcloud.com/c/2",
            ],
        )
        self.assertEqual(refresh.call_count, 2)


if __name__ == "__main__":
    unittest.main()
