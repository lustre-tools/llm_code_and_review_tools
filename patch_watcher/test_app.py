import tempfile
import re
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from http.server import ThreadingHTTPServer

import app
from failure_actions import FailureActionController, FailurePatchRevision
from maloo_adapter import (
    MalooBugLinks,
    MalooLinkBugResult,
)
from gerrit_upload import UploadStateStore


class PatchWatcherTests(unittest.TestCase):
    def setUp(self):
        app.PATCHES.clear()
        app._ENGINEERING_USED_CONFIRMATIONS.clear()
        app.AUTONOMOUS_LANE_STORE = None
        app.AUTONOMOUS_LANE_HISTORY = None
        app.AUTONOMOUS_LANE_RUNTIME = None

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
        app.ENGINEERING_WORKER_PROFILE = None
        app.GERRIT_UPLOAD_CONTROLLER = None
        app.GERRIT_REPLY_CONTROLLER = None
        app.JENKINS_RETRIGGER_CONTROLLER = None
        app.STANDING_POLICY_STORE = None
        app.AUTONOMOUS_LANE_STORE = None
        app.AUTONOMOUS_LANE_HISTORY = None
        app.AUTONOMOUS_LANE_RUNTIME = None
        app._ENGINEERING_USED_CONFIRMATIONS.clear()
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

    def test_autonomous_lane_dashboard_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            app.initialize_autonomous_lanes(
                Path(directory) / "lanes.json",
                Path(directory) / "history.jsonl",
            )
            rendered = app.page()
        self.assertIn("Autonomous lanes", rendered)
        self.assertIn("Global kill switch: Disabled", rendered)
        self.assertIn("deterministic-test-retest", rendered)
        self.assertIn("Remote writes per exact revision", rendered)

    def test_lane_global_enable_uses_bound_one_time_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            app.initialize_autonomous_lanes(
                Path(directory) / "lanes.json",
                Path(directory) / "history.jsonl",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                proposal = urlencode({
                    "csrf_token": app.CSRF_TOKEN,
                    "mode": "enabled",
                    "expected_generation": 0,
                }).encode()
                confirmation = urlopen(Request(
                    base + "/autonomous-lanes/global", data=proposal, method="POST"
                )).read().decode()
                self.assertFalse(app.AUTONOMOUS_LANE_STORE.load().global_enabled)
                fields = dict(re.findall(
                    r"name='([^']+)' value='([^']*)'", confirmation
                ))
                body = urlencode(fields).encode()
                response = urlopen(Request(
                    base + "/autonomous-lanes/global/confirm", data=body, method="POST"
                ))
                self.assertEqual(response.status, 200)
                self.assertTrue(app.AUTONOMOUS_LANE_STORE.load().global_enabled)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(
                        base + "/autonomous-lanes/global/confirm", data=body, method="POST"
                    ))
                self.assertEqual(caught.exception.code, 403)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

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

    def test_upload_confirmation_get_is_display_only_and_token_bound(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diff = root / "proposed.patch"
            diff.write_text("diff --git a/a b/a\n", encoding="utf-8")
            import hashlib
            store = UploadStateStore(root / "uploads.sqlite3")
            plan = store.prepare(
                idempotency_key="plan-once", run_id="run-1", session_id="session-1",
                change_number=68541, project="fs/lustre-release", branch="master",
                change_id="I" + "1" * 40,
                patchset=3, revision_sha="a" * 40,
                revision_ref="refs/changes/41/68541/3", diff_path=str(diff),
                diff_artifact_id="diff-1",
                diff_sha256=hashlib.sha256(diff.read_bytes()).hexdigest(),
                evidence_sha256="c" * 64, requested_by="operator",
            )
            plan = store.transition(
                plan.upload_id, expected={"prepared"}, state="commit_ready",
                local_commit_sha="b" * 40,
            )
            app.GERRIT_UPLOAD_CONTROLLER = SimpleNamespace(store=store)
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}"
                    f"/uploads/{plan.upload_id}/confirm"
                ).read().decode()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
            final_state = store.get(plan.upload_id).state
        self.assertIn("Confirm new Gerrit patchset", body)
        self.assertIn("method='post' action='/uploads/", body)
        self.assertIn(plan.binding_digest, body)
        self.assertEqual(final_state, "commit_ready")

    def test_external_write_confirmation_routes_fail_closed_when_disabled(self):
        app.GERRIT_REPLY_CONTROLLER = SimpleNamespace(enabled=False)
        app.JENKINS_RETRIGGER_CONTROLLER = SimpleNamespace(enabled=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            for route in (
                "/review-replies/reply-1/confirm",
                "/jenkins-retriggers/action-1/confirm",
            ):
                with self.subTest(route=route):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(base + route)
                    self.assertEqual(caught.exception.code, 503)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_disabled_external_write_switches_still_allow_read_only_reconciliation(self):
        reply = SimpleNamespace(
            reply_id="reply-1", run_id="review-run", state="ambiguous",
            summary="Reply outcome uncertain.",
        )
        retrigger = SimpleNamespace(
            action_id="action-1", state="ambiguous",
            summary="Retrigger outcome uncertain.",
        )

        class FakeStore:
            def __init__(self, value):
                self.value = value

            def get(self, _identity):
                return self.value

        class FakeController:
            enabled = False

            def __init__(self, value):
                self.store = FakeStore(value)
                self.calls = 0

            def reconcile(self, _identity):
                self.calls += 1
                return self.store.value

        reply_controller = FakeController(reply)
        jenkins_controller = FakeController(retrigger)
        app.GERRIT_REPLY_CONTROLLER = reply_controller
        app.JENKINS_RETRIGGER_CONTROLLER = jenkins_controller
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            for route in (
                "/review-replies/reply-1/reconcile",
                "/jenkins-retriggers/action-1/reconcile",
            ):
                request = Request(
                    base + route,
                    data=urlencode({"csrf_token": app.CSRF_TOKEN}).encode(),
                    method="POST",
                )
                self.assertIn("ambiguous", urlopen(request).read().decode())
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(reply_controller.calls, 1)
        self.assertEqual(jenkins_controller.calls, 1)

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

    def test_patch_actions_are_compact_ordered_and_truthful(self):
        first, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        first.update(
            change_number=68160, patchset=4, revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4",
            project="fs/lustre-release", lifecycle="Open",
        )
        second, _ = app.add_patch("https://review.whamcloud.com/c/68161")
        second.update(change_number=68161, patchset=2)

        rendered = app.page()

        self.assertIn("id='patch-actions-68160-4'", rendered)
        self.assertIn("id='patch-actions-68161-2'", rendered)
        self.assertEqual(rendered.count("<summary>Actions</summary>"), 2)
        self.assertLess(rendered.index("Build failures"), rendered.index("Test failures"))
        self.assertLess(rendered.index("Test failures"), rendered.index("Review comments"))
        self.assertIn("Handle simple comments", rendered)
        self.assertIn("Handle all comments", rendered)
        self.assertIn("Both bail to human when judgment is required", rendered)
        self.assertIn("action='/review-runs/prepare'", rendered)
        self.assertIn("upload one new patchset automatically", rendered)
        self.assertIn("separate controller action", rendered)
        self.assertIn("Handle build failure", rendered)
        self.assertIn("action='/build-runs/prepare'", rendered)
        self.assertNotIn("aria-labelledby='handle-reviews-title'", rendered)
        self.assertIn("method='post' action='/standing-policy'", rendered)
        self.assertIn("name='test_failures'", rendered)
        self.assertIn("name='build_failures'", rendered)
        self.assertIn("name='review_comments'", rendered)
        self.assertIn("method='post' action='/automation/dry-run'", rendered)
        self.assertIn("method='post' action='/runs/investigate'", rendered)
        self.assertIn("method='post' action='/engineering-runs/prepare'", rendered)

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
        self.assertIn("<strong>Build failures</strong>", rendered)
        self.assertIn("<strong>Review comments</strong>", rendered)

    def test_standing_policy_post_persists_all_capabilities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app.initialize_automation_store(root / "automation.sqlite3")
            store = app.initialize_standing_policy_store(root / "standing.json")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                project="fs/lustre-release", lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                request = Request(
                    base + "/standing-policy",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "change_number": "68160", "patchset": "4",
                        "revision_sha": "d" * 40, "expected_version": "0",
                        "trigger_mode": "manual", "test_failures": "investigate",
                        "build_failures": "repair", "review_comments": "simple",
                    }).encode(), method="POST",
                )
                urlopen(request).read()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
            policy = store.get("68160")
        self.assertEqual(policy.test_failures, "investigate")
        self.assertEqual(policy.build_failures, "repair")
        self.assertEqual(policy.review_comments, "simple")
        self.assertEqual(policy.trigger_mode, "manual")

    def test_standing_automatic_policy_requires_exact_one_use_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app.initialize_automation_store(root / "automation.sqlite3")
            store = app.initialize_standing_policy_store(root / "standing.json")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                project="fs/lustre-release", lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            values = {
                "csrf_token": app.CSRF_TOKEN,
                "change_number": "68160", "patchset": "4",
                "revision_sha": "d" * 40, "expected_version": "0",
                "trigger_mode": "automatic", "test_failures": "investigate",
                "build_failures": "repair", "review_comments": "simple",
            }
            try:
                proposal = Request(
                    base + "/standing-policy", data=urlencode(values).encode(),
                    method="POST",
                )
                confirmation = urlopen(proposal).read().decode()
                self.assertIn("Confirm automatic patch handlers", confirmation)
                self.assertEqual(store.get("68160").trigger_mode, "manual")
                token = re.search(
                    r"name='confirmation_token' value='([^']+)'", confirmation
                ).group(1)
                expires = re.search(
                    r"name='confirmation_expires_at' value='([^']+)'", confirmation
                ).group(1)
                final_values = {
                    **values,
                    "confirmation_token": token,
                    "confirmation_expires_at": expires,
                }
                final = Request(
                    base + "/standing-policy/confirm",
                    data=urlencode(final_values).encode(), method="POST",
                )
                urlopen(final).read()
                self.assertEqual(store.get("68160").trigger_mode, "automatic")
                replay = Request(
                    base + "/standing-policy/confirm",
                    data=urlencode(final_values).encode(), method="POST",
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(replay)
                self.assertIn(caught.exception.code, {403, 409})
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_observer_syncs_standing_policy_before_legacy_retest_tick(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            automation = app.initialize_automation_store(root / "automation.sqlite3")
            standing = app.initialize_standing_policy_store(root / "standing.json")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                project="fs/lustre-release", lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            automation.set_policy(
                "68160", mode="automatic", action_budget=1,
                delivery_budget=1, updated_by="old-process",
            )
            standing.save(app.PatchAutomationPolicy("68160"))

            class FakeRetestController:
                def tick_patch(self, patch, **options):
                    self.mode_seen = automation.get_policy("68160").mode
                    return SimpleNamespace(patch_id="68160")

            retest = FakeRetestController()
            app.RETEST_CONTROLLER = retest
            app.RUN_CONTROLLER = SimpleNamespace(stop=lambda: None)
            app._observe_patch_automation(patch_record)
        self.assertEqual(retest.mode_seen, "disabled")

    def test_standing_policy_sync_repairs_budget_even_when_mode_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            automation = app.initialize_automation_store(root / "automation.sqlite3")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                project="fs/lustre-release", lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            automation.set_policy(
                "68160", mode="approval", action_budget=1,
                delivery_budget=2, updated_by="stale-budget",
            )
            app._sync_standing_test_policy(
                patch_record,
                app.PatchAutomationPolicy(
                    "68160", test_failures="deterministic", trigger_mode="manual",
                ),
            )
            repaired = automation.get_policy("68160")
        self.assertEqual(repaired.mode, "approval")
        self.assertEqual(repaired.action_budget, 4)
        self.assertEqual(repaired.delivery_budget, 4)

    def test_active_managed_run_suppresses_automatic_retest_in_same_patch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            automation = app.initialize_automation_store(root / "automation.sqlite3")
            standing = app.initialize_standing_policy_store(root / "standing.json")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                project="fs/lustre-release", lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            standing.save(app.PatchAutomationPolicy(
                "68160", test_failures="deterministic", trigger_mode="automatic",
            ))
            automation.set_global_automation(True, changed_by="test", reason="test")
            active = SimpleNamespace(
                patch_id="68160", state="running", run_id="review-run",
            )

            class FakeSessions:
                def list_sessions(self, include_terminal=False):
                    return [active]

            class ForbiddenRetest:
                def tick_patch(self, *args, **kwargs):
                    raise AssertionError("active patch owner must suppress retest")

            app.SESSION_STORE = FakeSessions()
            app.RETEST_CONTROLLER = ForbiddenRetest()
            app.RUN_CONTROLLER = SimpleNamespace(stop=lambda: None)
            app._observe_patch_automation(patch_record)

    def test_standing_review_event_is_consumed_then_build_can_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            automation = app.initialize_automation_store(root / "automation.sqlite3")
            standing = app.initialize_standing_policy_store(root / "standing.json")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                project="fs/lustre-release", lifecycle="Open", unresolved=1,
                jenkins="FAIL", jenkins_url="https://build.whamcloud.com/job/x/4/",
            )
            app.sync_automation_patch(patch_record)
            standing.save(app.PatchAutomationPolicy(
                "68160", build_failures="repair", review_comments="simple",
                trigger_mode="automatic",
            ))
            automation.set_global_automation(True, changed_by="test", reason="test")

            class FakeSessions:
                def list_sessions(self, include_terminal=True):
                    return []

                def append_event(self, *args, **kwargs):
                    return None

            class FakeRuns:
                def __init__(self):
                    self.review_calls = 0
                    self.build_calls = 0

                def stop(self):
                    return None

                def request_review_comments(self, *args, **kwargs):
                    self.review_calls += 1
                    return SimpleNamespace(session_id="review-session", run_id="review-run")

                def request_build_failure(self, *args, **kwargs):
                    self.build_calls += 1
                    return SimpleNamespace(session_id="build-session", run_id="build-run")

            class FakeGerrit:
                def fetch_review_snapshot(self, *args, **kwargs):
                    return {"snapshot_sha256": "a" * 64}

            runs = FakeRuns()
            app.SESSION_STORE = FakeSessions()
            app.RUN_CONTROLLER = runs
            app.GERRIT_UPLOAD_CONTROLLER = SimpleNamespace(enabled=True)
            review_configured = patch.object(
                app.GerritStatusClient, "configured", return_value=FakeGerrit()
            )
            build_snapshot = patch.object(
                app, "_capture_build_failure_snapshot",
                return_value={"snapshot_sha256": "b" * 64},
            )
            with review_configured, build_snapshot:
                self.assertEqual(app._apply_standing_policy(patch_record).run_id, "review-run")
                self.assertEqual(app._apply_standing_policy(patch_record).run_id, "build-run")
                self.assertIsNone(app._apply_standing_policy(patch_record))
        self.assertEqual(runs.review_calls, 1)
        self.assertEqual(runs.build_calls, 1)

    def test_standing_build_and_review_require_upload_kill_switch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            automation = app.initialize_automation_store(root / "automation.sqlite3")
            standing = app.initialize_standing_policy_store(root / "standing.json")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                project="fs/lustre-release", lifecycle="Open", unresolved=1,
                jenkins="FAIL", jenkins_url="https://build.whamcloud.com/job/x/4/",
            )
            app.sync_automation_patch(patch_record)
            standing.save(app.PatchAutomationPolicy(
                "68160", build_failures="repair", review_comments="all",
                trigger_mode="automatic",
            ))
            automation.set_global_automation(True, changed_by="test", reason="test")
            app.RUN_CONTROLLER = SimpleNamespace(stop=lambda: None)
            app.GERRIT_UPLOAD_CONTROLLER = SimpleNamespace(enabled=False)
            self.assertIsNone(app._apply_standing_policy(patch_record))

    def test_global_automation_enable_get_is_display_only_then_post_mutates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(Path(temp_dir) / "automation.sqlite3")
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                body = urlopen(base + "/automation/global/confirm-enable").read().decode()
                self.assertIn("Enable automatic patch actions?", body)
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
        self.assertIn("Standing automation", rendered)
        self.assertIn("Trigger policy", rendered)
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
        self.assertIn("Start a read-only investigation pinned to this exact revision", rendered)
        self.assertIn("action='/runs/investigate'", rendered)
        self.assertIn("name='revision_sha'", rendered)
        self.assertIn(">Investigate</button>", rendered)
        self.assertNotIn("name='revision_sha' value=''", rendered)

    def test_engineering_start_http_flow_is_inert_until_one_exact_final_post(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160,
            project="fs/lustre-release",
            patchset=4,
            revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4",
            lifecycle="Open",
            title="LU-12345 controlled repair",
        )

        class FakeEngineeringController:
            def __init__(self):
                self.calls = []

            def stop(self):
                return None

            def request_engineering(self, patch_value, *, request_id=None):
                self.calls.append((dict(patch_value), request_id))
                return SimpleNamespace(run_id="pw-engineer-68160-ps4-test")

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                return None

        controller = FakeEngineeringController()
        app.RUN_CONTROLLER = controller
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        no_redirect = build_opener(NoRedirect)
        values = {
            "csrf_token": app.CSRF_TOKEN,
            "change_number": "68160",
            "patchset": "4",
            "revision_sha": "d" * 40,
            "idempotency_token": "engineering-start-once",
        }
        try:
            prepare = Request(
                base + "/engineering-runs/prepare",
                data=urlencode(values).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as prepared:
                no_redirect.open(prepare)
            self.assertEqual(prepared.exception.code, 303)
            confirmation_location = prepared.exception.headers["Location"]
            self.assertEqual(controller.calls, [])
            self.assertEqual(app._ENGINEERING_USED_CONFIRMATIONS, {})

            confirmation = urlopen(base + confirmation_location).read().decode()
            self.assertEqual(controller.calls, [])
            self.assertEqual(app._ENGINEERING_USED_CONFIRMATIONS, {})
            self.assertIn("Confirm controlled engineering run", confirmation)
            self.assertIn("d" * 40, confirmation)
            self.assertIn(
                "Gerrit upload:</strong> disabled for this subphase",
                confirmation,
            )
            confirmation_token = re.search(
                r"name='confirmation_token' value='([^']+)'", confirmation
            ).group(1)
            confirmation_expires_at = re.search(
                r"name='confirmation_expires_at' value='([^']+)'", confirmation
            ).group(1)

            final_values = {
                **values,
                "confirmation_token": confirmation_token,
                "confirmation_expires_at": confirmation_expires_at,
            }
            final = Request(
                base + "/engineering-runs/start",
                data=urlencode(final_values).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as started:
                no_redirect.open(final)
            self.assertEqual(started.exception.code, 303)
            self.assertEqual(
                started.exception.headers["Location"],
                "/runs/pw-engineer-68160-ps4-test",
            )
            self.assertEqual(len(controller.calls), 1)
            self.assertEqual(controller.calls[0][0]["revision_sha"], "d" * 40)
            self.assertEqual(controller.calls[0][0]["patchset"], 4)
            self.assertEqual(controller.calls[0][1], "engineering-start-once")

            replay = Request(
                base + "/engineering-runs/start",
                data=urlencode(final_values).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as replayed:
                urlopen(replay)
            self.assertEqual(replayed.exception.code, 409)
            self.assertEqual(len(controller.calls), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_review_start_approval_preauthorizes_auto_upload_without_second_confirmation(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160, project="fs/lustre-release", patchset=4,
            revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4", lifecycle="Open",
            unresolved=1,
        )
        snapshot = {
            "schema": "patch-watcher-review-snapshot/v1",
            "change": {"change_number": 68160, "patchset": 4,
                       "revision_sha": "d" * 40},
            "complete": True, "snapshot_sha256": "a" * 64,
            "threads": [{"thread_id": "t1", "comments": [
                {"comment_id": "c1"}
            ]}],
        }

        class FakeReviewController:
            def __init__(self):
                self.calls = []

            def stop(self):
                return None

            def request_review_comments(self, patch_value, snapshot_value, *, mode, request_id):
                self.calls.append((dict(patch_value), snapshot_value, mode, request_id))
                return SimpleNamespace(run_id="pw-review-68160-ps4-test")

        class FakeStatusClient:
            def fetch_review_snapshot(self, _url, *, expected_revision=None):
                self.expected_revision = expected_revision
                return snapshot

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                return None

        controller = FakeReviewController()
        app.RUN_CONTROLLER = controller
        app.GERRIT_UPLOAD_CONTROLLER = SimpleNamespace(enabled=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        no_redirect = build_opener(NoRedirect)
        values = {
            "csrf_token": app.CSRF_TOKEN, "change_number": "68160",
            "patchset": "4", "revision_sha": "d" * 40,
            "review_mode": "simple", "idempotency_token": "review-start-once",
        }
        try:
            with patch.object(
                app.GerritStatusClient, "configured", return_value=FakeStatusClient()
            ):
                prepare = Request(
                    base + "/review-runs/prepare",
                    data=urlencode(values).encode(), method="POST",
                )
                with self.assertRaises(HTTPError) as prepared:
                    no_redirect.open(prepare)
                self.assertEqual(prepared.exception.code, 303)
                confirmation = urlopen(
                    base + prepared.exception.headers["Location"]
                ).read().decode()
                self.assertIn("no later upload confirmation", confirmation)
                self.assertIn("separate controller action", confirmation)
                token = re.search(
                    r"name='confirmation_token' value='([^']+)'", confirmation
                ).group(1)
                expires = re.search(
                    r"name='confirmation_expires_at' value='([^']+)'", confirmation
                ).group(1)
                final_values = {
                    **values, "snapshot_sha256": "a" * 64,
                    "confirmation_token": token,
                    "confirmation_expires_at": expires,
                }
                final = Request(
                    base + "/review-runs/start",
                    data=urlencode(final_values).encode(), method="POST",
                )
                with self.assertRaises(HTTPError) as started:
                    no_redirect.open(final)
                self.assertEqual(started.exception.code, 303)
                self.assertEqual(len(controller.calls), 1)
                self.assertEqual(controller.calls[0][2:], ("simple", "review-start-once"))
                with self.assertRaises(HTTPError) as replayed:
                    no_redirect.open(final)
                self.assertEqual(replayed.exception.code, 409)
                self.assertEqual(len(controller.calls), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_build_start_binds_failure_and_preauthorizes_upload_once(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160, project="fs/lustre-release", branch="master",
            patchset=4, revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4", lifecycle="Open",
            jenkins="FAIL",
            jenkins_url="https://build.whamcloud.com/job/lustre-reviews/123/",
        )
        snapshot = {
            "schema": "patch-watcher-jenkins-failure-snapshot/v1",
            "complete": True,
            "change": {
                "change_number": 68160, "patchset": 4,
                "revision_sha": "d" * 40,
                "revision_ref": "refs/changes/60/68160/4",
                "project": "fs/lustre-release", "branch": "master",
            },
            "build": {
                "job_name": "lustre-reviews", "build_number": 123,
                "url": "https://build.whamcloud.com/job/lustre-reviews/123/",
                "result": "FAILURE",
            },
            "snapshot_sha256": "b" * 64,
        }

        class FakeBuildController:
            def __init__(self):
                self.calls = []

            def stop(self):
                return None

            def request_build_failure(self, patch_value, snapshot_value, *, request_id):
                self.calls.append((dict(patch_value), snapshot_value, request_id))
                return SimpleNamespace(run_id="pw-build-68160-ps4-test")

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                return None

        controller = FakeBuildController()
        app.RUN_CONTROLLER = controller
        app.GERRIT_UPLOAD_CONTROLLER = SimpleNamespace(enabled=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        no_redirect = build_opener(NoRedirect)
        values = {
            "csrf_token": app.CSRF_TOKEN, "change_number": "68160",
            "patchset": "4", "revision_sha": "d" * 40,
            "idempotency_token": "build-start-once",
        }
        try:
            with patch.object(
                app, "_capture_build_failure_snapshot", return_value=snapshot,
            ):
                prepare = Request(
                    base + "/build-runs/prepare",
                    data=urlencode(values).encode(), method="POST",
                )
                with self.assertRaises(HTTPError) as prepared:
                    no_redirect.open(prepare)
                self.assertEqual(prepared.exception.code, 303)
                confirmation = urlopen(
                    base + prepared.exception.headers["Location"]
                ).read().decode()
                self.assertIn("no later upload confirmation", confirmation)
                self.assertIn("lustre-reviews", confirmation)
                token = re.search(
                    r"name='confirmation_token' value='([^']+)'", confirmation
                ).group(1)
                expires = re.search(
                    r"name='confirmation_expires_at' value='([^']+)'", confirmation
                ).group(1)
                final_values = {
                    **values, "build_job": "lustre-reviews", "build_number": "123",
                    "build_snapshot_sha256": "b" * 64,
                    "confirmation_token": token,
                    "confirmation_expires_at": expires,
                }
                final = Request(
                    base + "/build-runs/start",
                    data=urlencode(final_values).encode(), method="POST",
                )
                with self.assertRaises(HTTPError) as started:
                    no_redirect.open(final)
                self.assertEqual(started.exception.code, 303)
                self.assertEqual(controller.calls[0][2], "build-start-once")
                with self.assertRaises(HTTPError) as replayed:
                    no_redirect.open(final)
                self.assertEqual(replayed.exception.code, 409)
                self.assertEqual(len(controller.calls), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_review_completion_dispatches_prepared_upload_without_confirmation(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160, patchset=4, revision_sha="d" * 40,
            lifecycle="Open",
        )
        events = []

        class FakeRunController:
            engineering_store = SimpleNamespace(
                list_artifacts=lambda _run_id: [SimpleNamespace(kind="diff", size_bytes=12)]
            )

            def stop(self):
                return None

            def stop(self):
                return None

            def _request_payload(self, _session):
                return {"request_kind": "review_comments"}

            def review_upload_inputs(self, run_id, patch_value, snapshot_value):
                self.inputs = (run_id, patch_value, snapshot_value)
                return {"run_id": run_id, "diff_path": "/tmp/proposed.patch"}

        class FakeUploadController:
            def __init__(self):
                self.executions = []

            def prepare(self, **_values):
                return SimpleNamespace(
                    upload_id="upload-1", state="commit_ready",
                    binding_digest="binding", new_patchset=None,
                    new_revision_sha=None,
                )

            def execute(self, upload_id, *, expected_binding_digest):
                self.executions.append((upload_id, expected_binding_digest))
                return SimpleNamespace(
                    upload_id=upload_id, state="succeeded", change_number=68160,
                    patchset=4, new_patchset=5, new_revision_sha="e" * 40,
                )

        class FakeStore:
            def append_event(self, session_id, event_type, payload, **_kwargs):
                events.append((session_id, event_type, payload))

        run_controller = FakeRunController()
        upload_controller = FakeUploadController()
        app.RUN_CONTROLLER = run_controller
        app.GERRIT_UPLOAD_CONTROLLER = upload_controller
        app.SESSION_STORE = FakeStore()
        session = SimpleNamespace(
            session_id="session-1", run_id="pw-review-68160-ps4-test",
            patch_id="68160", patchset=4, revision="d" * 40,
        )
        snapshot = {"complete": True, "snapshot_sha256": "a" * 64}
        with patch.object(
            app.GerritStatusClient, "configured",
            return_value=SimpleNamespace(
                fetch_review_snapshot=lambda *_args, **_kwargs: snapshot
            ),
        ), patch.object(app, "refresh_watched_patch") as refresh:
            app._process_review_completion(session)

        self.assertEqual(upload_controller.executions, [("upload-1", "binding")])
        self.assertEqual(run_controller.inputs[0], session.run_id)
        self.assertTrue(any(item[1] == "review_auto_upload_succeeded" for item in events))
        refresh.assert_called_once_with(patch_record)

    def test_build_completion_dispatches_prepared_upload_without_confirmation(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160, patchset=4, revision_sha="d" * 40,
            lifecycle="Open", jenkins="FAIL",
        )
        events = []

        class FakeRunController:
            engineering_store = SimpleNamespace(
                list_artifacts=lambda _run_id: [
                    SimpleNamespace(kind="diff", size_bytes=12)
                ]
            )

            def stop(self):
                return None

            def _request_payload(self, _session):
                return {"request_kind": "build_failure"}

            def build_failure_upload_inputs(self, run_id, patch_value, snapshot_value):
                self.inputs = (run_id, patch_value, snapshot_value)
                return {"run_id": run_id, "diff_path": "/tmp/proposed.patch"}

        class FakeUploadController:
            def prepare(self, **_values):
                return SimpleNamespace(
                    upload_id="upload-build", state="commit_ready",
                    binding_digest="binding", new_patchset=None,
                    new_revision_sha=None,
                )

            def execute(self, upload_id, *, expected_binding_digest):
                self.executed = (upload_id, expected_binding_digest)
                return SimpleNamespace(
                    upload_id=upload_id, state="succeeded", change_number=68160,
                    patchset=4, new_patchset=5, new_revision_sha="e" * 40,
                )

        class FakeStore:
            def append_event(self, session_id, event_type, payload, **_kwargs):
                events.append((session_id, event_type, payload))

        run_controller = FakeRunController()
        upload_controller = FakeUploadController()
        app.RUN_CONTROLLER = run_controller
        app.GERRIT_UPLOAD_CONTROLLER = upload_controller
        app.SESSION_STORE = FakeStore()
        session = SimpleNamespace(
            session_id="session-build", run_id="pw-build-68160-ps4-test",
            patch_id="68160", patchset=4, revision="d" * 40,
        )
        snapshot = {"complete": True, "snapshot_sha256": "b" * 64}
        with patch.object(
            app, "_capture_build_failure_snapshot", return_value=snapshot,
        ), patch.object(app, "refresh_watched_patch") as refresh:
            app._process_build_failure_completion(session)

        self.assertEqual(upload_controller.executed, ("upload-build", "binding"))
        self.assertEqual(run_controller.inputs[0], session.run_id)
        self.assertTrue(any(item[1] == "build_auto_upload_succeeded" for item in events))
        refresh.assert_called_once_with(patch_record)

    def test_engineering_confirmation_rejects_tampering_and_revision_staleness(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160,
            project="fs/lustre-release",
            patchset=4,
            revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4",
            lifecycle="Open",
        )

        class FakeEngineeringController:
            def __init__(self):
                self.calls = []

            def stop(self):
                return None

            def request_engineering(self, patch_value, *, request_id=None):
                self.calls.append((dict(patch_value), request_id))
                return SimpleNamespace(run_id="unexpected")

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                return None

        controller = FakeEngineeringController()
        app.RUN_CONTROLLER = controller
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        no_redirect = build_opener(NoRedirect)
        values = {
            "csrf_token": app.CSRF_TOKEN,
            "change_number": "68160",
            "patchset": "4",
            "revision_sha": "d" * 40,
            "idempotency_token": "tamper-test",
        }
        try:
            request = Request(
                base + "/engineering-runs/prepare",
                data=urlencode(values).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as prepared:
                no_redirect.open(request)
            location = prepared.exception.headers["Location"]
            query = parse_qs(urlparse(location).query)
            signed = query["confirmation_token"][0]
            confirmation_expires_at = query["confirmation_expires_at"][0]

            tampered_query = {
                key: item[0] for key, item in query.items()
            }
            tampered_query["confirmation_token"] = "0" * len(signed)
            with self.assertRaises(HTTPError) as bad_get:
                urlopen(
                    base + "/engineering-runs/confirm-start?"
                    + urlencode(tampered_query)
                )
            self.assertEqual(bad_get.exception.code, 403)

            tampered_final = Request(
                base + "/engineering-runs/start",
                data=urlencode({
                    **values,
                    "idempotency_token": "different-nonce",
                    "confirmation_token": signed,
                    "confirmation_expires_at": confirmation_expires_at,
                }).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as bad_post:
                urlopen(tampered_final)
            self.assertEqual(bad_post.exception.code, 403)

            expired_at = str(int(app.time.time()) - 1)
            expired_token = app._signed_confirmation(
                "engineering-start", 68160, 4, "d" * 40,
                "expired-nonce", expired_at,
            )
            expired_final = Request(
                base + "/engineering-runs/start",
                data=urlencode({
                    **values,
                    "idempotency_token": "expired-nonce",
                    "confirmation_token": expired_token,
                    "confirmation_expires_at": expired_at,
                }).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as expired_post:
                urlopen(expired_final)
            self.assertEqual(expired_post.exception.code, 403)

            # The same signed identity becomes stale as soon as the watched
            # patch advances, on both the confirmation GET and final POST.
            patch_record.update(
                patchset=5,
                revision_sha="e" * 40,
                revision_ref="refs/changes/60/68160/5",
            )
            with self.assertRaises(HTTPError) as stale_get:
                urlopen(base + location)
            self.assertEqual(stale_get.exception.code, 403)
            stale_final = Request(
                base + "/engineering-runs/start",
                data=urlencode({
                    **values,
                    "confirmation_token": signed,
                    "confirmation_expires_at": confirmation_expires_at,
                }).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as stale_post:
                urlopen(stale_final)
            self.assertEqual(stale_post.exception.code, 409)
            self.assertEqual(controller.calls, [])
            self.assertEqual(app._ENGINEERING_USED_CONFIRMATIONS, {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_engineering_dashboard_uses_live_run_routes_and_disables_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "engineering-session-1",
                patch_id="68160",
                run_id="pw-engineer-68160-ps4-view",
                revision="f" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )

            class FakeEngineeringState:
                def get_allocation_by_run(self, run_id):
                    return None

                def get_manifest(self, run_id):
                    return None

                def list_artifacts(self, run_id):
                    return []

            class FakeEngineeringController:
                engineering_store = FakeEngineeringState()
                model = "test-model"

                def stop(self):
                    return None

            app.RUN_CONTROLLER = FakeEngineeringController()
            with patch(
                "app.refresh_resource_status",
                return_value={"ltvm": {"vms": []}},
            ):
                rendered = app.engineering_runs_html()

        self.assertIn("Controlled engineering runs", rendered)
        self.assertIn("f" * 40, rendered)
        self.assertIn(
            "Gerrit upload:</strong> disabled for this subphase", rendered
        )
        self.assertIn(
            "method='post' action='/runs/pw-engineer-68160-ps4-view/guidance'",
            rendered,
        )
        self.assertIn(
            "/runs/pw-engineer-68160-ps4-view/confirm?intent=cancel",
            rendered,
        )
        self.assertIn(
            "/runs/pw-engineer-68160-ps4-view/confirm?intent=kill",
            rendered,
        )
        self.assertNotIn(
            "action='/runs/pw-engineer-68160-ps4-view/cancel'", rendered
        )
        self.assertNotIn("Upload patch", rendered)

    def test_engineering_retry_get_is_inert_and_final_post_starts_one_new_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "engineering-session-retry",
                patch_id="68160",
                run_id="pw-engineer-68160-ps4-old",
                revision="f" * 40,
                patchset=4,
                profile="engineering",
                state="failed",
            )
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(
                change_number=68160,
                project="fs/lustre-release",
                patchset=4,
                revision_sha="f" * 40,
                revision_ref="refs/changes/60/68160/4",
                lifecycle="Open",
            )

            class FakeEngineeringState:
                def get_allocation_by_run(self, run_id):
                    return None

                def get_manifest(self, run_id):
                    return None

                def list_artifacts(self, run_id):
                    return []

            class FakeEngineeringController:
                engineering_store = FakeEngineeringState()
                model = "test-model"

                def __init__(self):
                    self.calls = []

                def stop(self):
                    return None

                def request_engineering(self, patch_value, *, request_id=None):
                    self.calls.append((dict(patch_value), request_id))
                    return SimpleNamespace(
                        run_id="pw-engineer-68160-ps4-new"
                    )

            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                    return None

            controller = FakeEngineeringController()
            app.RUN_CONTROLLER = controller
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            no_redirect = build_opener(NoRedirect)
            try:
                review = urlopen(
                    base
                    + "/runs/pw-engineer-68160-ps4-old/confirm?intent=retry"
                ).read().decode()
                self.assertIn("No action has been taken", review)
                self.assertIn("name='intent' value='retry'", review)
                self.assertEqual(controller.calls, [])
                self.assertEqual(
                    store.get_session("engineering-session-retry").state,
                    "failed",
                )

                prepare_final = Request(
                    base + "/runs/pw-engineer-68160-ps4-old/confirm",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "intent": "retry",
                    }).encode(),
                    method="POST",
                )
                final_confirmation = urlopen(prepare_final).read().decode()
                self.assertIn("Confirm retry as a new run", final_confirmation)
                self.assertIn("does not revive this checkout", final_confirmation)
                self.assertIn("f" * 40, final_confirmation)
                self.assertIn(
                    "Gerrit upload:</strong> disabled for this subphase",
                    final_confirmation,
                )
                self.assertEqual(controller.calls, [])
                fields = {
                    name: value for name, value in re.findall(
                        r"name='([^']+)' value='([^']*)'", final_confirmation
                    )
                }

                # Advancing the watched patch makes this exact retry proposal
                # stale before the final mutation boundary.
                patch_record.update(
                    patchset=5,
                    revision_sha="a" * 40,
                    revision_ref="refs/changes/60/68160/5",
                )
                stale = Request(
                    base + "/runs/pw-engineer-68160-ps4-old/retry",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        **fields,
                    }).encode(),
                    method="POST",
                )
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(stale)
                self.assertEqual(rejected.exception.code, 409)
                self.assertEqual(controller.calls, [])

                # A fresh confirmation after the exact revision is current
                # receives a new stable request identity for the new run.
                patch_record.update(
                    patchset=4,
                    revision_sha="f" * 40,
                    revision_ref="refs/changes/60/68160/4",
                )
                final_confirmation = urlopen(prepare_final).read().decode()
                fields = {
                    name: value for name, value in re.findall(
                        r"name='([^']+)' value='([^']*)'", final_confirmation
                    )
                }
                final = Request(
                    base + "/runs/pw-engineer-68160-ps4-old/retry",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        **fields,
                    }).encode(),
                    method="POST",
                )
                with self.assertRaises(HTTPError) as started:
                    no_redirect.open(final)
                self.assertEqual(started.exception.code, 303)
                self.assertEqual(
                    started.exception.headers["Location"],
                    "/runs/pw-engineer-68160-ps4-new",
                )
                self.assertEqual(len(controller.calls), 1)
                self.assertEqual(
                    controller.calls[0][0]["revision_sha"], "f" * 40
                )
                self.assertEqual(
                    controller.calls[0][1], fields["idempotency_token"]
                )
                self.assertTrue(controller.calls[0][1])
                self.assertEqual(
                    store.get_session("engineering-session-retry").state,
                    "failed",
                )

                replay = Request(
                    base + "/runs/pw-engineer-68160-ps4-old/retry",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        **fields,
                    }).encode(),
                    method="POST",
                )
                with self.assertRaises(HTTPError) as replayed:
                    urlopen(replay)
                self.assertEqual(replayed.exception.code, 409)
                self.assertEqual(len(controller.calls), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

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

    def test_engineering_dashboard_uses_cached_vm_rss_and_exact_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            store.register_pinned_session(
                "engineering-session-1",
                patch_id="68160",
                run_id="pw-engineer-68160-ps4-test",
                revision="d" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            app._RESOURCE_SNAPSHOT = {
                "host_memory": {},
                "ltvm": {
                    "vms": [
                        {
                            "name": "owned-vm",
                            "owner_id": "patch-watcher:engineering-session-1",
                            "state": "running",
                            "configured_guest_memory_bytes": 2 * 1024 ** 3,
                            "host_rss_bytes": 640 * 1024 ** 2,
                        },
                        {
                            "name": "unrelated-vm",
                            "owner_id": "patch-watcher:somebody-else",
                            "state": "running",
                            "configured_guest_memory_bytes": 4 * 1024 ** 3,
                            "host_rss_bytes": 900 * 1024 ** 2,
                        },
                    ]
                },
            }
            app._RESOURCE_SNAPSHOT_MONOTONIC = app.time.monotonic()
            with patch(
                "app.collect_resource_snapshot",
                side_effect=AssertionError("cached projection must not repoll LTVM"),
            ):
                rendered = app.engineering_runs_html()

        run_card = rendered.split("<article class='engineering-run'", 1)[1]
        self.assertIn("owned-vm", run_card)
        self.assertIn("2 GiB", run_card)
        self.assertIn("640 MiB", run_card)
        self.assertNotIn(">unrelated-vm<", run_card)
        orphan_section = rendered.split("<section class='orphan-vms'", 1)[1]
        self.assertIn("unrelated-vm", orphan_section)

    def test_engineering_projection_maps_exhaustion_and_cooldown_for_views(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            store.register_pinned_session(
                "engineering-session-capacity",
                patch_id="68160",
                run_id="pw-engineer-68160-ps4-capacity",
                revision="d" * 40,
                patchset=4,
                profile="engineering",
                state="resource_exhausted",
            )
            session = store.get_session("engineering-session-capacity")
            future = app.datetime(2099, 1, 1, tzinfo=app.timezone.utc)

            class Cooldown:
                not_before = future
                consecutive_exhaustions = 3

                def active_at(self, observed_at):
                    return observed_at < self.not_before

            execution = SimpleNamespace(
                execution_id="validation-execution-1",
                state="resource_exhausted",
                admission_state="approved",
                approved_by="local-dashboard-user",
                approved_at=future,
                revision_sha="d" * 40,
                owner_id="patch-watcher:engineering-session-capacity",
                manifest_id="manifest-1",
                manifest_sha256="e" * 64,
            )
            attempt = SimpleNamespace(
                attempt_id="attempt-1",
                state="resource_exhausted",
                failure_code="ltvm_resource_exhausted",
                summary="insufficient host memory",
            )

            class FakeEngineeringState:
                def get_allocation_by_run(self, run_id):
                    return None

                def get_manifest(self, run_id):
                    return None

                def list_artifacts(self, run_id):
                    return []

                def get_validation_execution_by_run(self, run_id):
                    return execution

                def list_validation_attempts(self, execution_id):
                    return (attempt,)

                def list_validation_step_results(self, attempt_id):
                    return ()

                def get_capacity_cooldown(self, patch_id):
                    return Cooldown()

            class FakeEngineeringController:
                engineering_store = FakeEngineeringState()
                model = "test-model"

                def stop(self):
                    return None

            app.RUN_CONTROLLER = FakeEngineeringController()
            projection = app._engineering_projection(session)

        validation = projection["validation"]
        self.assertEqual(
            validation["resource_exhaustion"],
            {
                "error_code": "ltvm_resource_exhausted",
                "operation": "session-owned guest validation",
                "requested_resources": "exact-owner LTVM guest capacity",
                "evidence": "insufficient host memory",
            },
        )
        self.assertEqual(validation["cooldown"]["state"], "active")
        self.assertEqual(
            validation["cooldown"]["retry_not_before"], future.isoformat()
        )
        self.assertTrue(validation["cooldown"]["automation_suppressed"])
        self.assertEqual(validation["cooldown"]["exhaustion_count"], 3)

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
