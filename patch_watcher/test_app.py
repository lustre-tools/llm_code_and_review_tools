import contextlib
import hashlib
import hmac
import inspect
import io
import json
import re
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from patch_watcher import app, reporting
from patch_watcher.engineering_views import render_engineering_start_control
from patch_watcher.failure_actions import FailureActionController, FailurePatchRevision
from patch_watcher.maloo_adapter import (
    MalooBugLinks,
    MalooLinkBugResult,
)
from patch_watcher.run_views import render_investigate_control, render_run_detail

_HIDDEN_INPUT_RE = re.compile(
    r"<input type='hidden' name='([^']+)' value='([^']*)'>"
)


def hidden_fields(html):
    """Collect the hidden inputs a rendered confirmation page displays.

    Escalating confirmations now carry a signed proposal, an expiry, and a
    one-time idempotency token alongside the CSRF token, so tests submit what
    the page actually shows rather than a hand-built subset.
    """

    return {
        name: value.replace("&amp;", "&")
        for name, value in _HIDDEN_INPUT_RE.findall(html)
    }


class _NoRedirect(HTTPRedirectHandler):
    """Return the 3xx itself, so a test can assert on Location and status."""

    def redirect_request(self, *args, **kwargs):
        return None


_WATCH_FILE_SANDBOX = {}


def setUpModule():
    """Keep the suite off the operator's real watch file.

    `POST /add` and `POST /remove` persist through the module global
    `app.ACTIVE_WATCH_FILE`, which defaults to the real
    ~/.config/patch-watcher/patches.txt. Tests that drive those handlers were
    therefore rewriting -- and, since a removal test leaves PATCHES empty,
    emptying -- the watch list of whoever ran `make test`.
    """

    sandbox = tempfile.TemporaryDirectory()
    _WATCH_FILE_SANDBOX["directory"] = sandbox
    app.ACTIVE_WATCH_FILE = Path(sandbox.name) / "patches.txt"
    # Handlers log every fault, and several tests provoke faults deliberately,
    # so without this the suite appends to the operator's real error log.
    _WATCH_FILE_SANDBOX["error_log"] = reporting.DEFAULT_ERROR_LOG
    reporting.DEFAULT_ERROR_LOG = Path(sandbox.name) / "errors.jsonl"


def tearDownModule():
    app.ACTIVE_WATCH_FILE = app.DEFAULT_SEED_FILE
    if "error_log" in _WATCH_FILE_SANDBOX:
        reporting.DEFAULT_ERROR_LOG = _WATCH_FILE_SANDBOX.pop("error_log")
    sandbox = _WATCH_FILE_SANDBOX.pop("directory", None)
    if sandbox is not None:
        sandbox.cleanup()


class AppGlobalsIsolated(unittest.TestCase):
    """Restore the app's service globals around every test in the class.

    These tests point module globals at temporary databases. Leaving one
    behind aims a LATER class at a deleted temp file, which surfaces as an
    unrelated 500 in full-suite order only -- so the failure gets blamed on
    the test that exposed it rather than the one that caused it, and it moves
    whenever a class is added, because unittest runs classes alphabetically.

    Wrapping `run` rather than `setUp` on purpose: it holds regardless of
    whether a subclass defines setUp or remembers to call super().
    """

    _SERVICE_GLOBALS = (
        "SESSION_STORE",
        "RUN_CONTROLLER",
        "AUTOMATION_STORE",
        "RETEST_CONTROLLER",
        "STANDING_POLICY_STORE",
        "AUTONOMOUS_LANE_STORE",
        "AUTONOMOUS_LANE_HISTORY",
        "AUTONOMOUS_LANE_RUNTIME",
        "AUTOMATION_OBSERVER",
    )

    def run(self, result=None):
        saved = {name: getattr(app, name, None) for name in self._SERVICE_GLOBALS}
        try:
            return super().run(result)
        finally:
            for name, value in saved.items():
                setattr(app, name, value)


class PatchWatcherTests(AppGlobalsIsolated):
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
        self.assertIn("Unattended actions", rendered)
        self.assertIn("Unattended actions: Disabled", rendered)
        self.assertIn("deterministic-test-retest", rendered)
        self.assertIn("Remote writes per exact revision", rendered)

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
                data=b"url=x",
                headers={
                    "Content-Length": str(app.MAX_FORM_BODY_BYTES + 1),
                },
                method="POST",
            ),
            Request(
                base + "/add",
                data=b"url=\xff",
                method="POST",
            ),
        ]
        try:
            for request, expected in zip(requests, (415, 413, 400), strict=False):
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
        self.assertIn("✕ Needs attention", attention)
        self.assertIn("tone-bad", attention)
        self.assertIn("✓ Ready", ready)
        self.assertIn("! Awaiting CI", waiting)
        self.assertIn("Merged", merged)
        self.assertIn("tone-good", merged)
        self.assertIn("Abandoned", abandoned)
        self.assertIn("tone-bad", abandoned)

    def test_watch_state_labels_do_not_mangle_initialisms(self):
        # Caught in a browser: the label was derived with .title(), so the page
        # showed "Ci Failed" and "Awaiting Ci". These are read by a human.
        self.assertIn("CI failed", app._watch_chip("ci-failed"))
        self.assertIn("Awaiting CI", app._watch_chip("awaiting-ci"))
        for state in ("ci-failed", "awaiting-ci"):
            self.assertNotIn("Ci ", app._watch_chip(state))

    def test_an_unknown_watch_state_still_renders_readably(self):
        chip = app._watch_chip("some-new-state")
        self.assertIn("Some new state", chip)
        self.assertIn("tone-neutral", chip)

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
        patch_record["refreshed_at"] = "2026-08-29T21:00:00+00:00"
        rendered = app.page()
        self.assertIn(
            "Last successful check: 2026-08-29T21:00:00+00:00", rendered
        )
        self.assertIn(
            "Last check attempt: 2026-08-29T21:00:00+00:00", rendered
        )
        self.assertEqual(rendered.count("action='/refresh-all'"), 1)
        self.assertNotIn("action='/refresh'", rendered)
        self.assertNotIn("<th>Last checked</th>", rendered)

    def test_failing_refresh_does_not_report_a_fresh_successful_check(self):
        """refresh_patch stamps last_checked on failure too, so max() of it
        read as a fresh check on a host where every refresh had failed."""

        good, _ = app.add_patch("https://review.whamcloud.com/c/11")
        good["last_checked"] = "2026-08-29T21:00:00+00:00"
        good["refreshed_at"] = "2026-08-29T21:00:00+00:00"
        bad, _ = app.add_patch("https://review.whamcloud.com/c/12")
        bad["last_checked"] = "2026-08-30T09:00:00+00:00"
        bad["status_error"] = "Gerrit returned HTTP 502"
        bad["check_count"] = 7
        bad["errors"] = [
            {"checked_at": "2026-08-30T09:00:00+00:00",
             "message": "Gerrit returned HTTP 502"},
        ]
        self.assertEqual(
            app.overall_last_successful_check(), "2026-08-29T21:00:00+00:00"
        )
        self.assertEqual(
            app.overall_last_checked(), "2026-08-30T09:00:00+00:00"
        )
        self.assertEqual(
            app.refresh_failure_summary(), "1 of 2 patches failed to refresh."
        )
        rendered = app.page()
        self.assertIn(
            "Last successful check: 2026-08-29T21:00:00+00:00", rendered
        )
        self.assertIn(
            "Last check attempt: 2026-08-30T09:00:00+00:00", rendered
        )
        self.assertIn("1 of 2 patches failed to refresh.", rendered)
        # check_count and the stored errors list were never rendered anywhere.
        self.assertIn("Refresh errors (1 of 7 checks)", rendered)
        self.assertIn("Gerrit returned HTTP 502", rendered)

    def test_failed_run_detail_shows_the_stored_failure_reason(self):
        """failure_code and failure_summary are recorded on the terminal
        result and were projected by no view, so a failed run's page said only
        "Run: Failed / Current step: Failed / Timeline (0)"."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "pw-session-failed",
                patch_id="68160",
                run_id="pw-review-68160-ps4-abc",
                revision="a" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            store.finish_session(
                "pw-session-failed",
                "failed",
                failure_code="checkout_unavailable",
                failure_summary=(
                    "no checkout available for this run: no free checkout "
                    "in the pool"
                ),
            )
            session = store.get_session("pw-session-failed")
            projection = app._run_projection(session)
            rendered = app.run_detail_html(session)

        self.assertEqual(projection["failure_code"], "checkout_unavailable")
        self.assertIn("no free checkout in the pool", projection["failure_summary"])
        self.assertIn("Why this run ended", rendered)
        self.assertIn("no free checkout in the pool", rendered)
        self.assertIn("checkout_unavailable", rendered)

    def test_follow_up_control_names_the_kind_of_run_it_does_not_start(self):
        """The follow-up handler always calls request_investigation, so on a
        review run the only terminal control started a different kind."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "pw-session-review",
                patch_id="68160",
                run_id="pw-review-68160-ps4-abc",
                revision="a" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            store.finish_session("pw-session-review", "succeeded")
            session = store.get_session("pw-session-review")
            projection = app._run_projection(session)
            rendered = app.run_detail_html(session)

        self.assertEqual(projection["run_kind"], "review comment")
        self.assertIn("new read-only investigation", rendered)
        self.assertIn("does not start another review comment run", rendered)
        self.assertNotIn(">Start follow-up run<", rendered)

    def test_the_real_snapshot_dataclass_yields_its_guests(self):
        """`refresh_resource_status` returns a `ResourceSnapshot` when sampling
        works and a dict only when it is disabled or broken.  Every caller
        tested `isinstance(snapshot, Mapping)`, so the guest tables and orphan
        warnings were empty exactly on a healthy host -- and every test that
        patched this function to return a dict missed it."""

        from patch_watcher.resource_status import (
            HostMemoryStatus,
            LTVMInventory,
            LTVMVMStatus,
            ResourceSnapshot,
        )

        sampled_at = datetime.now(UTC)
        guest = LTVMVMStatus(
            name="co3-sanity", state="running", owner_id=None,
            patch_watcher_session_id=None,
            configured_guest_memory_bytes=None, host_rss_bytes=None,
            process_id=None, vcpus=None, ip=None, host_memory_source=None,
            quality="good",
        )
        snapshot = ResourceSnapshot(
            sampled_at=sampled_at,
            source="test",
            quality="good",
            host_memory=HostMemoryStatus(
                sampled_at=sampled_at, source="test", quality="good",
            ),
            ltvm=LTVMInventory(
                sampled_at=sampled_at, source="test", quality="good",
                vms=(guest,),
            ),
        )
        with patch(
            "patch_watcher.app.refresh_resource_status", return_value=snapshot
        ):
            self.assertEqual(
                [vm["name"] for vm in app._snapshot_ltvm_vms()], ["co3-sanity"]
            )
        # The degraded paths really do hand back a plain dict.
        with patch(
            "patch_watcher.app.refresh_resource_status",
            return_value={"ltvm": {"vms": [{"name": "co4-mds"}]}},
        ):
            self.assertEqual(
                [vm["name"] for vm in app._snapshot_ltvm_vms()], ["co4-mds"]
            )

    def test_engineering_detail_moved_onto_the_run_page(self):
        """Folding the engineering card into the Runs list had to move its
        per-run detail somewhere, or the only view of a run's checkout, owned
        guests and prompt manifest would have been deleted with the card.  A
        review run's page must not grow that section."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            # One active session per patch is enforced by a partial unique
            # index, so the two runs must watch different patches.
            for session_id, patch_id, run_id in (
                ("pw-session-engineer", "68160", "pw-engineer-68160-ps4-abc"),
                ("pw-session-review", "68161", "pw-review-68161-ps4-def"),
            ):
                store.register_pinned_session(
                    session_id,
                    patch_id=patch_id,
                    run_id=run_id,
                    revision="a" * 40,
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

                def _request_payload(self, session):
                    return {}

                def stop(self):
                    return None

            app.RUN_CONTROLLER = FakeEngineeringController()
            with patch(
                "patch_watcher.app.refresh_resource_status",
                return_value={"ltvm": {"vms": [
                    {"name": "co1-mds", "owner_id": None, "state": "running"},
                ]}},
            ):
                engineering = app.run_detail_html(
                    store.get_session("pw-session-engineer")
                )
                review = app.run_detail_html(
                    store.get_session("pw-session-review")
                )
                index = app.runs_html()

        self.assertIn("<article class='engineering-run'", engineering)
        self.assertIn("Session-owned LTVM guests", engineering)
        self.assertIn("Isolated full checkout", engineering)
        self.assertIn("/runs/pw-engineer-68160-ps4-abc/guidance", engineering)
        self.assertNotIn("<article class='engineering-run'", review)
        # The index lists runs and links to them; it no longer inlines any of
        # this, which is what made three panels say the same thing three ways.
        self.assertNotIn("<article class='engineering-run'", index)
        self.assertIn("href='/runs/pw-engineer-68160-ps4-abc'", index)

    def test_the_panel_says_first_what_is_happening_now(self):
        """Is anything running?  What runs without asking?  When was it last
        looked at?  The panel used to answer none of these; a run's absence
        could only be inferred from a chip that was not there."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = app.initialize_session_store(root / "sessions.sqlite3")
            automation = app.initialize_automation_store(root / "automation.sqlite3")
            standing = app.initialize_standing_policy_store(root / "standing.json")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/35302")
            patch_record.update(change_number=35302, patchset=4, revision_sha="b" * 40,
                                last_checked="2026-09-10 12:00:00")
            app.RUN_CONTROLLER = None

            quiet = app._patch_now_html(patch_record)
            self.assertIn("<strong>No run</strong> on this patch", quiet)
            self.assertIn("Level <strong>Watch only</strong>: nothing runs unattended", quiet)
            self.assertIn("last checked 2026-09-10 12:00:00", quiet)
            self.assertNotIn("Last run", quiet)

            standing.save(app.PatchAutomationPolicy.for_preset("35302", "retest"))
            gated = app._patch_now_html(patch_record)
            self.assertIn("Level <strong>Known retests</strong>, but the global kill switch is off", gated)
            automation.set_global_automation(True, changed_by="test", reason="test")
            live = app._patch_now_html(patch_record)
            self.assertIn("Level <strong>Known retests</strong>: acts unattended", live)

            store.register_pinned_session(
                "pw-session-done", patch_id="35302", run_id="pw-review-35302-ps4-old",
                revision="b" * 40, patchset=4, profile="engineering", state="running",
            )
            store.finish_session("pw-session-done", "succeeded")
            finished = app._patch_now_html(patch_record)
            self.assertIn("<strong>No run</strong> on this patch", finished)
            self.assertIn("Last run: <a href='/runs/pw-review-35302-ps4-old'>", finished)
            self.assertIn("succeeded", finished)

            store.register_pinned_session(
                "pw-session-live", patch_id="35302", run_id="pw-engineer-35302-ps4-new",
                revision="b" * 40, patchset=4, profile="engineering", state="running",
            )
            running = app._patch_now_html(patch_record)
            self.assertIn("<strong>A run is running:</strong>", running)
            self.assertIn("href='/runs/pw-engineer-35302-ps4-new'", running)
            self.assertIn("(engineering)", running)
            self.assertNotIn("waiting for you", running)
            store.ask_human("pw-session-live", "Convert vvp_object.c too?")
            waiting = app._patch_now_html(patch_record)
            self.assertIn("A run is waiting human", waiting)
            self.assertIn("<strong>It is waiting for you.</strong>", waiting)

    def test_a_run_waiting_on_you_is_labelled_counted_and_explained(self):
        """The in-console channel: a paused run shows on the patch row, links
        to itself, is counted in the header, and the run page says how you
        were told on the other channels."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/35302")
            patch_record.update(change_number=35302, patchset=4, revision_sha="b" * 40)
            store.register_pinned_session(
                "pw-session-paused", patch_id="35302", run_id="pw-review-35302-ps4-abc",
                revision="b" * 40, patchset=4, profile="engineering", state="running",
            )
            question = store.ask_human("pw-session-paused", "Convert vvp_object.c too?")
            for channel, delivered, why in (
                ("email", False, "Email is disabled; the notice was recorded only."),
                ("gerrit", True, None),
            ):
                key = f"human-notice:{question.question_id}:{channel}"
                store.ensure_delivery(
                    "pw-session-paused", kind="human_notice", idempotency_key=key,
                    payload={"channel": channel, "question_id": question.question_id},
                )
                store.finish_delivery(key, delivered=delivered, failure_summary=why)
            app.RUN_CONTROLLER = None
            with patch("patch_watcher.app.refresh_resource_status",
                       return_value={"ltvm": {"vms": []}}):
                page = app.page()
                detail = app.run_detail_html(store.get_session("pw-session-paused"))

        self.assertIn("Needs you", page)
        self.assertIn("href='/runs/pw-review-35302-ps4-abc'", page)
        self.assertIn("1 needs you", page)
        self.assertIn("Waiting for your decision", detail)
        self.assertIn("Convert vvp_object.c too?", detail)
        self.assertIn("How you were told", detail)
        self.assertIn("email: not sent", detail)
        self.assertIn("Email is disabled; the notice was recorded only.", detail)
        self.assertIn("gerrit: sent", detail)

    def test_finished_runs_of_every_kind_are_discoverable(self):
        """Before the three run panels became one, the engineering panel listed
        only pw-engineer- runs and the agent-run panel only non-terminal ones,
        so a finished review run left no trace and its /runs/<id> URL became
        unfindable."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            for index, (session_id, run_id, state) in enumerate((
                ("pw-session-review", "pw-review-68160-ps4-abc", "succeeded"),
                ("pw-session-build", "pw-build-68160-ps4-def", "failed"),
                ("pw-session-manual", "pw-68160-ps4-0123456789", "cancelled"),
                ("pw-session-live", "pw-engineer-68160-ps4-ghi", "running"),
            )):
                store.register_pinned_session(
                    session_id,
                    patch_id="68160",
                    run_id=run_id,
                    revision=chr(ord("a") + index) * 40,
                    patchset=4,
                    profile="engineering",
                    state="running",
                )
                if state != "running":
                    store.finish_session(
                        session_id, state,
                        failure_code="worker_failed" if state == "failed" else None,
                        failure_summary=(
                            "the worker exited before publishing"
                            if state == "failed" else None
                        ),
                    )
            with patch(
                "patch_watcher.app.refresh_resource_status",
                return_value={"ltvm": {"vms": []}},
            ):
                rendered = app.runs_html()
                page = app.page()

        self.assertIn("Finished runs", rendered)
        for run_id in ("pw-review-68160-ps4-abc", "pw-build-68160-ps4-def",
                       "pw-68160-ps4-0123456789"):
            self.assertIn(f"href='/runs/{run_id}'", rendered)
            self.assertIn(f"href='/runs/{run_id}'", page)
        self.assertIn("review comment run", rendered)
        self.assertIn("build repair run", rendered)
        self.assertIn("the worker exited before publishing", rendered)
        # A running run gets one card above the finished table, and no row in
        # it: the panels this replaced listed such a run in two places at once.
        finished_table = rendered.split("<details class='finished-runs'", 1)[1]
        self.assertNotIn("pw-engineer-68160-ps4-ghi", finished_table)
        self.assertEqual(rendered.count("<article class='run-summary'"), 1)
        self.assertIn("Engineering run", rendered)

    def test_recorded_cleanup_failure_reaches_the_runs_card(self):
        """The warnings derive from the LTVM inventory, so a pw_owned_resource
        row marked cleanup_failed rendered a clean bill of health."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "engineering-session-dirty",
                patch_id="68160",
                run_id="pw-engineer-68160-ps4-dirty",
                revision="a" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            resource = store.register_owned_resource(
                "engineering-session-dirty",
                owner_id=app.owner_id_for_session("engineering-session-dirty"),
                resource_type="ltvm_vm",
                external_id="pw-engineer-68160-oss",
            )
            store.mark_resource_cleanup(
                resource.resource_id,
                succeeded=False,
                failure_summary="ltvm destroy timed out; cleanup abandoned",
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
                "patch_watcher.app.refresh_resource_status",
                return_value={"ltvm": {"vms": []}},
            ):
                rendered = app.runs_html()

        self.assertNotIn(
            "Unmatched or orphan LTVM resources: none reported", rendered
        )
        self.assertIn("pw-engineer-68160-oss", rendered)
        self.assertIn("ltvm destroy timed out; cleanup abandoned", rendered)

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
        self.assertEqual(rendered.count("<summary>Actions for this patch</summary>"), 2)
        self.assertLess(rendered.index("Build failures"), rendered.index("Test failures"))
        self.assertLess(rendered.index("Test failures"), rendered.index("Review comments"))
        self.assertIn("Handle simple comments", rendered)
        self.assertIn("Handle all comments", rendered)
        self.assertIn("Each bails to you when judgment is required", rendered)
        self.assertIn("action='/review-runs/prepare'", rendered)
        self.assertIn("Handle build failure", rendered)
        self.assertIn("action='/build-runs/prepare'", rendered)
        self.assertNotIn("aria-labelledby='handle-reviews-title'", rendered)
        self.assertIn("method='post' action='/standing-policy'", rendered)
        # One level select replaced the four per-kind selects.
        self.assertIn("name='preset'", rendered)
        self.assertIn("What Patch Watcher may do", rendered)
        for old_field in ("name='test_failures'", "name='build_failures'",
                          "name='review_comments'", "name='trigger_mode'"):
            self.assertNotIn(old_field, rendered)
        self.assertIn("method='post' action='/automation/dry-run'", rendered)
        self.assertIn("method='post' action='/runs/investigate'", rendered)
        self.assertIn("method='post' action='/engineering-runs/prepare'", rendered)
        # The panel leads with what is happening; the "Available" chips, which
        # meant "installed on this host" and read as "something to do", are gone.
        self.assertLess(rendered.index("No run</strong> on this patch"),
                        rendered.index("name='preset'"))
        self.assertLess(rendered.index("name='preset'"), rendered.index("Start a run by hand"))
        # Every rung is described where it is chosen, not only once saved.
        for level in app.PRESET_LEVELS:
            self.assertIn(app.PRESET_SUMMARIES[level][:40], rendered, level)
        self.assertIn("<li class='current'><strong>Watch only</strong>", rendered)
        self.assertNotIn("class='availability'>Available", rendered)
        self.assertNotIn("Commands are open-ended", rendered)

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
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
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
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

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

    def test_bot_feedback_and_above_rebase_a_cherry_pick_veto_before_anything_else(self):
        """A patchset checkpatch cannot cherry-pick is rebased first: a build
        repair or a review reply on a revision that will never land is wasted,
        and the rebase makes a new patchset that resets those signals anyway.
        The veto is bot feedback, so this starts at "bots"; below that it is
        left to a human."""

        def run_at(level, change):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                automation = app.initialize_automation_store(root / "automation.sqlite3")
                standing = app.initialize_standing_policy_store(root / "standing.json")
                patch_record, _ = app.add_patch(f"https://review.whamcloud.com/c/{change}")
                patch_record.update(
                    change_number=change, patchset=4, revision_sha="d" * 40,
                    revision_ref=f"refs/changes/{str(change)[-2:]}/{change}/4",
                    project="fs/lustre-release", lifecycle="Open", unresolved=1,
                    jenkins="FAIL", jenkins_url="https://build.whamcloud.com/job/x/4/",
                    rebase_needed=True,
                    review_blockers=[{
                        "name": "wc-checkpatch", "value": -1, "patchset": 4,
                        "message": "This change cannot be cherry-picked to master.",
                    }],
                )
                app.sync_automation_patch(patch_record)
                standing.save(app.PatchAutomationPolicy.for_preset(str(change), level))
                automation.set_global_automation(True, changed_by="test", reason="test")

                class FakeSessions:
                    def list_sessions(self, include_terminal=True):
                        return []

                    def append_event(self, *args, **kwargs):
                        return None

                class FakeRuns:
                    def __init__(self):
                        self.engineering = []
                        self.review_calls = 0
                        self.build_calls = 0

                    def stop(self):
                        return None

                    def request_engineering(self, patch, **kwargs):
                        self.engineering.append(kwargs)
                        return SimpleNamespace(session_id="rebase-session", run_id="rebase-run")

                    def request_review_comments(self, *args, **kwargs):
                        self.review_calls += 1
                        return SimpleNamespace(session_id="review-session", run_id="review-run")

                    def request_build_failure(self, *args, **kwargs):
                        self.build_calls += 1
                        return SimpleNamespace(session_id="build-session", run_id="build-run")

                class FakeGerrit:
                    def fetch_review_snapshot(self, *args, **kwargs):
                        return {"snapshot_sha256": "a" * 64, "threads": []}

                runs = FakeRuns()
                app.SESSION_STORE = FakeSessions()
                app.RUN_CONTROLLER = runs
                with patch.object(app.GerritStatusClient, "configured", return_value=FakeGerrit()), \
                     patch.object(app, "_capture_build_failure_snapshot",
                                  return_value={"snapshot_sha256": "b" * 64}):
                    first = app._apply_standing_policy(patch_record)
                    second = app._apply_standing_policy(patch_record)
                return runs, first, second

        runs, first, second = run_at("own", 35302)
        self.assertEqual(first.run_id, "rebase-run")
        self.assertEqual(runs.engineering[0]["task"], "rebase")
        self.assertEqual(runs.engineering[0]["request_id"].split(":")[0], "standing")
        # The same veto on the same revision is one event: no second run, and
        # the build repair is still not attempted underneath it.
        self.assertIsNone(second)
        self.assertEqual(len(runs.engineering), 1)
        self.assertEqual(runs.build_calls, 0)

        for level, change in (("bots", 35303), ("all", 35304)):
            runs, first, _ = run_at(level, change)
            self.assertEqual(first.run_id, "rebase-run", level)
            self.assertEqual(runs.engineering[0]["task"], "rebase", level)
            self.assertEqual(runs.review_calls, 0, level)

        # Below "bots" the veto is a human's: nothing at all runs on a
        # revision that cannot land, and nothing rebases it.
        runs, first, _ = run_at("investigate", 35305)
        self.assertEqual(runs.engineering, [])
        self.assertEqual(runs.review_calls, 0)
        self.assertEqual(runs.build_calls, 0)
        self.assertIsNone(first)

    def test_succeeded_run_owns_its_revision_only_until_the_next_poll(self):
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "pw-session-1",
                patch_id="68160",
                run_id="pw-review-68160-ps4-owner",
                revision="d" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            patch = {"change_number": 68160, "revision_sha": "d" * 40}

            # A non-terminal session owns the patch on any revision.
            self.assertEqual(
                app._revision_owner_session(
                    {"change_number": 68160, "revision_sha": "e" * 40}
                ).session_id,
                "pw-session-1",
            )

            store.set_state("pw-session-1", "succeeded")
            finished = store.get_session("pw-session-1").state_changed_at

            # Before Gerrit has been polled again we cannot yet tell whether the
            # agent uploaded, so the run still owns its revision. Starting a
            # second agent here means two of them pushing to one change.
            owner = app._revision_owner_session(patch)
            self.assertEqual(owner.session_id, "pw-session-1")
            self.assertEqual(owner.state, "succeeded")

            stale = dict(patch)
            stale["last_checked"] = (finished - timedelta(minutes=1)).isoformat()
            self.assertIsNotNone(app._revision_owner_session(stale))

            # Once Gerrit has been polled AFTER the run finished and the
            # revision is unchanged, the run published nothing. Holding the
            # patch forever would block every future automatic action on it --
            # a review run that only posts replies is a normal outcome.
            polled = dict(patch)
            polled["last_checked"] = (finished + timedelta(minutes=1)).isoformat()
            self.assertIsNone(app._revision_owner_session(polled))

            # A new revision releases it regardless of poll timing.
            uploaded = {"change_number": 68160, "revision_sha": "e" * 40}
            self.assertIsNone(app._revision_owner_session(uploaded))

    def test_a_naive_last_checked_timestamp_is_treated_as_utc(self):
        # Stored timestamps have historically been written both with and
        # without an offset; a naive one must not raise on comparison.
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            store.register_pinned_session(
                "pw-session-2", patch_id="70000",
                run_id="pw-review-70000-ps1-owner", revision="a" * 40,
                patchset=1, profile="engineering", state="running",
            )
            store.set_state("pw-session-2", "succeeded")
            finished = store.get_session("pw-session-2").state_changed_at
            naive = (finished + timedelta(minutes=1)).replace(tzinfo=None).isoformat()
            self.assertIsNone(app._revision_owner_session({
                "change_number": 70000, "revision_sha": "a" * 40,
                "last_checked": naive,
            }))

    def test_unparseable_last_checked_keeps_the_run_owning_its_revision(self):
        # Fail safe: if we cannot tell when we last polled, assume we have not,
        # and keep the patch held rather than risk two agents on one change.
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(Path(temp_dir) / "sessions.sqlite3")
            store.register_pinned_session(
                "pw-session-3", patch_id="70001",
                run_id="pw-review-70001-ps1-owner", revision="b" * 40,
                patchset=1, profile="engineering", state="running",
            )
            store.set_state("pw-session-3", "succeeded")
            self.assertIsNotNone(app._revision_owner_session({
                "change_number": 70001, "revision_sha": "b" * 40,
                "last_checked": "not a timestamp",
            }))

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
                    data=urlencode(hidden_fields(body)).encode(),
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
                    data=urlencode(hidden_fields(body)).encode(),
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
        self.assertIn("What Patch Watcher may do", rendered)
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
                displayed = hidden_fields(confirmation)
                final = Request(
                    base + "/research/policy/confirm",
                    data=urlencode({
                        **values,
                        "confirmation_token": displayed["confirmation_token"],
                        "confirmation_expires_at": displayed[
                            "confirmation_expires_at"
                        ],
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
                displayed = hidden_fields(confirmation)
                approve = Request(
                    base + f"/approvals/{action.action_id}/approve",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "revision_sha": "d" * 40,
                        "confirmation_token": displayed["confirmation_token"],
                        "confirmation_expires_at": displayed[
                            "confirmation_expires_at"
                        ],
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

            def request_engineering(
                self, patch_value, *, request_id=None, model="", effort=""
            ):
                # Mirrors RunController.request_engineering. A fake that
                # lags the real signature turns a wiring bug into a 500 that
                # only this test sees.
                self.calls.append((dict(patch_value), request_id, model, effort))
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
            "model": "claude-opus-5",
            "effort": "high",
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
                "Gerrit upload:</strong> available with real credentials",
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
            # The choice made on the prepare form is the choice the run gets.
            self.assertEqual(controller.calls[0][2], "claude-opus-5")
            self.assertEqual(controller.calls[0][3], "high")

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

    def test_review_start_approval_is_one_confirmation_for_a_self_uploading_run(self):
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
                self.assertIn(
                    "The controller does not upload on its behalf", confirmation
                )
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
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_build_start_binds_failure_to_a_single_confirmation(self):
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
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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

    def test_engineering_run_page_uses_live_run_routes_and_disables_upload(self):
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
            # The capability profile is derived from this immutable request
            # event, not from the session profile, and the page states the
            # boundary it implies. Without it the run is honestly read-only.
            store.append_event(
                "engineering-session-1",
                "engineering_run_requested",
                {"request_kind": "engineering"},
                idempotency_key="request:pw-engineer-68160-ps4-view",
                at=datetime.now(UTC),
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
                "patch_watcher.app.refresh_resource_status",
                return_value={"ltvm": {"vms": []}},
            ):
                rendered = app._engineering_detail_html(
                    store.get_session("engineering-session-1")
                )

        self.assertIn("Engineering run", rendered)
        self.assertIn("f" * 40, rendered)
        self.assertIn(
            "Gerrit upload:</strong> available with real credentials", rendered
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
                    "Gerrit upload:</strong> available with real credentials",
                    final_confirmation,
                )
                self.assertEqual(controller.calls, [])
                fields = dict(re.findall(
                        r"name='([^']+)' value='([^']*)'", final_confirmation
                    ))

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
                fields = dict(re.findall(
                        r"name='([^']+)' value='([^']*)'", final_confirmation
                    ))
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

    def test_page_leads_with_the_watch_list_then_the_resource_summary(self):
        """The watch list is what the tool is for, so it goes first.

        This previously asserted the opposite -- host memory above the patch
        controls -- which pushed the actual work below a screenful of host
        diagnostics.
        """
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
        with patch("patch_watcher.app.collect_resource_snapshot", return_value=snapshot):
            rendered = app.page()
        self.assertIn("Worker host memory", rendered)
        self.assertIn("24 GiB", rendered)
        self.assertIn("worker-vm", rendered)
        self.assertIn("Configured guest memory", rendered)
        self.assertLess(
            rendered.index("Watched patches"), rendered.index("Worker host memory")
        )
        self.assertLess(
            rendered.index("Add a patch"), rendered.index("Worker host memory")
        )
        self.assertIn("action='/resources/refresh'", rendered)
        # The headline carries the one number an operator opens the page for;
        # the rest of the breakdown sits behind a disclosure.
        self.assertIn("available of", rendered)
        self.assertIn("Full memory breakdown", rendered)

    def test_resource_snapshot_is_cached_until_forced(self):
        snapshot = {"host_memory": {}, "ltvm": {"vms": []}}
        app.RESOURCE_COLLECTION_ENABLED = True
        with patch("patch_watcher.app.collect_resource_snapshot", return_value=snapshot) as collect:
            self.assertIs(app.refresh_resource_status(), snapshot)
            self.assertIs(app.refresh_resource_status(), snapshot)
            self.assertIs(app.refresh_resource_status(force=True), snapshot)
        self.assertEqual(collect.call_count, 2)

    def test_engineering_run_page_uses_cached_vm_rss_and_exact_owner(self):
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
                "patch_watcher.app.collect_resource_snapshot",
                side_effect=AssertionError("cached projection must not repoll LTVM"),
            ):
                run_card = app._engineering_detail_html(
                    store.get_session("engineering-session-1")
                )
                # The guest nobody owns is reported by the Runs card instead.
                orphan_section = app.runs_html().split(
                    "<section class='orphan-vms'", 1
                )[1]

        self.assertIn("owned-vm", run_card)
        self.assertIn("2 GiB", run_card)
        self.assertIn("640 MiB", run_card)
        self.assertNotIn(">unrelated-vm<", run_card)
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
            future = datetime(2099, 1, 1, tzinfo=UTC)

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
            with patch("patch_watcher.app.refresh_patch") as refresh:
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
            with patch("patch_watcher.app.refresh_patch") as refresh:
                loaded = app.load_seed_file(watch_file)
        self.assertEqual(
            [item["url"] for item in loaded],
            [
                "https://review.whamcloud.com/c/1",
                "https://review.whamcloud.com/c/2",
            ],
        )
        self.assertEqual(refresh.call_count, 2)

    # ---- Regression: display-only "confirmations" on escalating routes ----

    def _serve(self):
        """Start the handler on a loopback port and stop it after the test."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_csrf_only_post_cannot_enable_the_global_automation_gate(self):
        # The primary global automation gate used to flip on a POST carrying
        # nothing but the CSRF token: no proposal binding, no expiry, no
        # replay protection. The durable audit row then recorded "Explicitly
        # confirmed from the dashboard" for a confirmation the code had never
        # verified.
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            base = self._serve()
            blind = Request(
                base + "/automation/global/enable",
                data=urlencode({"csrf_token": app.CSRF_TOKEN}).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as refused:
                urlopen(blind)
            self.assertEqual(refused.exception.code, 403)
            self.assertFalse(store.get_global_automation().enabled)
            # Nothing may claim a confirmation the code did not verify.
            self.assertEqual(store.list_global_automation_audit(), [])

            confirm_page = urlopen(
                base + "/automation/global/confirm-enable"
            ).read().decode()
            displayed = hidden_fields(confirm_page)
            self.assertIn("confirmation_token", displayed)
            self.assertIn("confirmation_expires_at", displayed)
            confirmed = Request(
                base + "/automation/global/enable",
                data=urlencode(displayed).encode(), method="POST",
            )
            urlopen(confirmed).read()
            setting = store.get_global_automation()
            self.assertTrue(setting.enabled)
            self.assertEqual(
                setting.reason, "Explicitly confirmed from the dashboard"
            )
            # The same signed proposal cannot be replayed.
            with self.assertRaises(HTTPError) as replayed:
                urlopen(Request(
                    base + "/automation/global/enable",
                    data=urlencode(displayed).encode(), method="POST",
                ))
            self.assertEqual(replayed.exception.code, 403)
            self.assertEqual(len(store.list_global_automation_audit()), 1)

    def test_csrf_only_post_cannot_confirm_an_automatic_retest_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4,
                revision_sha="d" * 40, lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            base = self._serve()
            values = {
                "csrf_token": app.CSRF_TOKEN,
                "change_number": "68160",
                "revision_sha": "d" * 40,
                "max_actions": "1",
            }
            blind = Request(
                base + "/automation/policy/confirm",
                data=urlencode(values).encode(), method="POST",
            )
            with self.assertRaises(HTTPError) as refused:
                urlopen(blind)
            self.assertEqual(refused.exception.code, 403)
            self.assertEqual(store.get_policy("68160").mode, "disabled")

            proposal = urlopen(Request(
                base + "/automation/policy",
                data=urlencode({**values, "mode": "automatic"}).encode(),
                method="POST",
            )).read().decode()
            displayed = hidden_fields(proposal)
            self.assertIn("confirmation_token", displayed)
            urlopen(Request(
                base + "/automation/policy/confirm",
                data=urlencode(displayed).encode(), method="POST",
            )).read()
            self.assertEqual(store.get_policy("68160").mode, "automatic")

            # Tampering with the bound budget invalidates the signature.
            tampered = dict(displayed, max_actions="20")
            with self.assertRaises(HTTPError) as rejected:
                urlopen(Request(
                    base + "/automation/policy/confirm",
                    data=urlencode(tampered).encode(), method="POST",
                ))
            self.assertEqual(rejected.exception.code, 403)
            # And the exact proposal is one-time.
            with self.assertRaises(HTTPError) as replayed:
                urlopen(Request(
                    base + "/automation/policy/confirm",
                    data=urlencode(displayed).encode(), method="POST",
                ))
            self.assertEqual(replayed.exception.code, 403)

    def test_csrf_only_post_cannot_approve_a_planned_retest_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_automation_store(
                Path(temp_dir) / "automation.sqlite3"
            )
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4,
                revision_sha="d" * 40, lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            store.set_policy(
                "68160", mode="approval", action_budget=3,
                delivery_budget=2, updated_by="operator",
            )
            trigger = store.create_trigger(
                "68160", revision="d" * 40, kind="maloo_failure",
                fingerprint="trigger-approval-1", payload={},
            )
            run = store.create_run(
                trigger.trigger_id, deterministic_key="run-approval-1"
            )
            store.claim_run(run.run_id, "controller")
            action = store.plan_action(
                run.run_id,
                action_type="maloo_retest",
                request={
                    "session_id": "11111111-2222-3333-4444-555555555555",
                    "jira_ticket": "LU-19487",
                },
                idempotency_key="retest-approval-1",
            )
            base = self._serve()
            approve_url = base + f"/automation/actions/{action.action_id}/approve"
            blind = Request(
                approve_url,
                data=urlencode({
                    "csrf_token": app.CSRF_TOKEN, "revision_sha": "d" * 40,
                }).encode(),
                method="POST",
            )
            body = urlopen(blind).read().decode()
            self.assertIn("Retest approval was not recorded", body)
            self.assertIsNone(store.get_action_approval(action.action_id))

            confirm_page = urlopen(
                base + f"/automation/actions/{action.action_id}/confirm"
            ).read().decode()
            displayed = hidden_fields(confirm_page)
            self.assertIn("confirmation_token", displayed)
            self.assertIn("confirmation_expires_at", displayed)
            urlopen(Request(
                approve_url, data=urlencode(displayed).encode(), method="POST",
            )).read()
            self.assertIsNotNone(store.get_action_approval(action.action_id))

    # ---- Regression: a render failure must not become a blank HTTP 200 ----

    def test_a_dashboard_render_failure_is_a_visible_error_not_a_blank_200(self):
        # do_GET used to send "200 OK" and the headers BEFORE calling page(),
        # so any rendering exception produced a successful status with an empty
        # body: no error on screen, and no CSRF token left to recover with.
        base = self._serve()
        with patch.object(app, "log_structured_error"), patch.object(
            app, "page", side_effect=RuntimeError("malformed project value"),
        ):
            with self.assertRaises(HTTPError) as failed:
                urlopen(base + "/")
        self.assertEqual(failed.exception.code, 500)
        body = failed.exception.read().decode()
        self.assertNotEqual(body, "")
        self.assertIn("malformed project value", body)

    def test_a_malformed_gerrit_project_no_longer_blanks_the_dashboard(self):
        # autonomous_lane._identifier raises a plain ValueError for anything
        # outside its charset; _patch_lane_html only caught AutonomousLaneError.
        with tempfile.TemporaryDirectory() as temp_dir:
            app.initialize_autonomous_lanes(
                Path(temp_dir) / "lanes.json",
                Path(temp_dir) / "lane-history.jsonl",
            )
            good, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            good.update(
                change_number=68160, project="fs/lustre-release", patchset=4,
                revision_sha="d" * 40, lifecycle="Open",
            )
            bad, _ = app.add_patch("https://review.whamcloud.com/c/68161")
            bad.update(
                change_number=68161, project="fs/lustre release", patchset=1,
                revision_sha="e" * 40, lifecycle="Open",
            )
            base = self._serve()
            response = urlopen(base + "/")
            body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertNotEqual(body, "")
        self.assertIn("https://review.whamcloud.com/c/68160", body)
        self.assertIn("https://review.whamcloud.com/c/68161", body)
        self.assertIn(app.CSRF_TOKEN, body)
        self.assertIn("</html>", body)

    def test_one_unrenderable_patch_row_degrades_only_that_row(self):
        good, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        good.update(
            change_number=68160, project="fs/lustre-release", patchset=4,
            revision_sha="d" * 40, lifecycle="Open",
        )
        bad, _ = app.add_patch("https://review.whamcloud.com/c/68161")
        bad.update(
            change_number=68161, project="fs/lustre-release", patchset=1,
            revision_sha="e" * 40, lifecycle="Open",
        )
        real_row = app._patch_row

        def explode(patch_value, jira_base=app.JIRA_BASE_URL):
            if str(patch_value.get("change_number")) == "68161":
                raise RuntimeError("row is unrenderable")
            return real_row(patch_value, jira_base)

        with patch.object(app, "log_structured_error") as logged, patch.object(
            app, "_patch_row", side_effect=explode,
        ):
            body = app.page()
        self.assertIn("https://review.whamcloud.com/c/68160", body)
        self.assertIn("Actions", body)
        self.assertIn("This patch could not be rendered", body)
        self.assertIn("row is unrenderable", body)
        # The degraded row still offers the one control that clears the state.
        self.assertIn(
            "<input type='hidden' name='url' "
            "value='https://review.whamcloud.com/c/68161'>",
            body,
        )
        self.assertEqual(logged.call_args[0][0], "patch_row_render_failed")

    # ---- Regression: credentialed fetches before confirmation is verified ----

    def test_build_confirm_start_verifies_before_any_credentialed_fetch(self):
        # GET carries no CSRF requirement, so capturing the Jenkins/Gerrit
        # snapshot first let any page the operator visited spend their
        # credentials until the services rate-limited or locked them out.
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160, project="fs/lustre-release", branch="master",
            patchset=4, revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4", lifecycle="Open",
        )
        base = self._serve()
        query = urlencode({
            "change_number": "68160", "patchset": "4",
            "revision_sha": "d" * 40, "build_job": "lustre-reviews",
            "build_number": "123", "snapshot_sha256": "b" * 64,
            "confirmation_token": "f" * 64,
            "idempotency_token": "forged",
            "confirmation_expires_at": str(int(time.time()) + 600),
        })
        snapshot = {
            "complete": True,
            "build": {"job_name": "lustre-reviews", "build_number": 123},
            "snapshot_sha256": "b" * 64,
        }
        with patch.object(
            app, "_capture_build_failure_snapshot", return_value=snapshot,
        ) as capture:
            with self.assertRaises(HTTPError) as refused:
                urlopen(base + "/build-runs/confirm-start?" + query)
        self.assertEqual(refused.exception.code, 403)
        capture.assert_not_called()

    def test_review_confirm_start_verifies_before_any_credentialed_fetch(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        patch_record.update(
            change_number=68160, project="fs/lustre-release", branch="master",
            patchset=4, revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4", lifecycle="Open",
        )
        base = self._serve()
        query = urlencode({
            "change_number": "68160", "patchset": "4",
            "revision_sha": "d" * 40, "review_mode": "simple",
            "snapshot_sha256": "b" * 64,
            "confirmation_token": "f" * 64,
            "idempotency_token": "forged",
            "confirmation_expires_at": str(int(time.time()) + 600),
        })
        client = SimpleNamespace(
            fetch_review_snapshot=lambda *args, **options: {
                "complete": True, "snapshot_sha256": "b" * 64,
            },
        )
        with patch.object(
            app.GerritStatusClient, "configured", return_value=client,
        ) as configured:
            with self.assertRaises(HTTPError) as refused:
                urlopen(base + "/review-runs/confirm-start?" + query)
        self.assertEqual(refused.exception.code, 403)
        configured.assert_not_called()

    def test_request_logging_drops_query_strings_carrying_tokens(self):
        class LoggingOnly(app.Handler):
            def __init__(self):
                pass

            def address_string(self):
                return "127.0.0.1"

            def log_date_time_string(self):
                return "07/Sep/2026 00:00:00"

        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            LoggingOnly().log_message(
                '"%s" %s %s',
                "GET /build-runs/confirm-start?confirmation_token=s3cret"
                " HTTP/1.1",
                200,
                4096,
            )
            # log_error() feeds an int status through a "%d" format, so the
            # redactor must leave non-string arguments alone.
            LoggingOnly().log_message("code %d, message %s", 403, "Stale")
        logged = stream.getvalue()
        self.assertNotIn("s3cret", logged)
        self.assertIn("/build-runs/confirm-start?<redacted> HTTP/1.1", logged)
        self.assertIn("code 403, message Stale", logged)
        self.assertEqual(app._redact_request_line(403), 403)
        self.assertEqual(
            app._redact_request_line("GET /runs/pw-1 HTTP/1.1"),
            "GET /runs/pw-1 HTTP/1.1",
        )

    @contextlib.contextmanager
    def serving(self):
        """Run the real handler on loopback for one route-level test."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_remove_confirms_and_says_what_happens_to_an_active_run(self):
        """Remove was the only one-click mutation, styled exactly like the
        twice-confirmed "Kill session", and it silently detached a live run."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "pw-session-live",
                patch_id="68160",
                run_id="pw-engineer-68160-ps4-live",
                revision="f" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            patch_record, _ = app.add_patch(
                "https://review.whamcloud.com/c/68160"
            )
            patch_record.update(change_number=68160, patchset=4)
            with self.serving() as base:
                first = urlopen(Request(
                    base + "/remove",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "url": patch_record["url"],
                    }).encode(),
                    method="POST",
                )).read().decode()

                # Nothing removed yet: the first POST only describes the change.
                self.assertEqual(
                    len(app.PATCHES), 1,
                    "the first POST removed the patch with no confirmation",
                )
                self.assertIn("Confirm removing a watched patch", first)
                self.assertIn("pw-engineer-68160-ps4-live", first)
                self.assertIn("does NOT stop it", first)
                self.assertIn("never be marked stale", first)
                self.assertIn("/confirm?intent=kill", first)

                fields = hidden_fields(first)
                confirmed = urlopen(Request(
                    base + "/remove",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "url": patch_record["url"],
                        "confirmation_token": fields["confirmation_token"],
                        "confirmation_expires_at": fields[
                            "confirmation_expires_at"
                        ],
                    }).encode(),
                    method="POST",
                ))
                self.assertEqual(confirmed.status, 200)
                self.assertEqual(app.PATCHES, [])

    def test_first_confirmation_page_describes_the_action_it_leads_to(self):
        """Step 1 said only "No action has been taken"; the description of
        what would happen appeared only on step 2."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "pw-session-1",
                patch_id="68160",
                run_id="run-1",
                revision="d" * 40,
                patchset=4,
                profile="engineering",
                state="running",
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
            with self.serving() as base:
                kill = urlopen(
                    base + "/runs/run-1/confirm?intent=kill"
                ).read().decode()
                cancel = urlopen(
                    base + "/runs/run-1/confirm?intent=cancel"
                ).read().decode()
                retry = urlopen(
                    base
                    + "/runs/pw-engineer-68160-ps4-old/confirm?intent=retry"
                ).read().decode()
            # A display-only GET must not have recorded any control intent.
            self.assertEqual(store.list_control_intents("pw-session-1"), [])

        self.assertIn("forcibly stops the Claude process", kill)
        self.assertIn("← Keep session running", kill)
        self.assertIn("requests an orderly stop", cancel)
        self.assertIn("← Keep session running", cancel)
        self.assertIn("starts a NEW isolated engineering run", retry)
        # "Keep session running" is wrong for a terminal run: nothing is.
        self.assertNotIn("← Keep session running", retry)
        self.assertIn("← Back to this finished run", retry)
        for body in (kill, cancel, retry):
            self.assertIn("No action has been taken", body)

    def test_run_control_error_renders_inside_the_document(self):
        """The explanation was appended AFTER </main>, and .notice had no CSS
        rule in the standalone document at all."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "pw-session-terminal",
                patch_id="68160",
                run_id="run-terminal",
                revision="d" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            store.finish_session("pw-session-terminal", "cancelled")
            with self.serving() as base:
                body = urlopen(Request(
                    base + "/runs/run-terminal/guidance",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "message": "please continue",
                        "delivery_mode": "safe_boundary",
                    }).encode(),
                    method="POST",
                )).read().decode()

        notice = body.index("class='notice'")
        self.assertLess(
            notice, body.index("</main>"),
            "the explanation is rendered outside the document body",
        )
        self.assertNotIn("</main><p class='notice'>", body)
        self.assertIn(".notice{", body)
        self.assertIn("That control could not be applied to this run", body)
        self.assertIn("The run itself is unchanged", body)

    def test_rejected_guidance_leaves_the_run_exactly_as_it_was(self):
        """"The run itself is unchanged" has to be true when we say it.

        The handler resumed or interrupted the run first and only then let
        enqueue_guidance reject the message, so a whitespace-only message --
        which the textarea's `required` happily accepts -- resumed a paused
        agent with no new instruction while the error page asserted the
        opposite.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.initialize_session_store(
                Path(temp_dir) / "sessions.sqlite3"
            )
            store.register_pinned_session(
                "pw-session-paused",
                patch_id="68160",
                run_id="run-paused",
                revision="d" * 40,
                patchset=4,
                profile="engineering",
                state="running",
            )
            store.set_state("pw-session-paused", "paused")
            with self.serving() as base:
                body = urlopen(Request(
                    base + "/runs/run-paused/guidance",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "message": "   ",
                        "delivery_mode": "resume_with_message",
                    }).encode(),
                    method="POST",
                )).read().decode()

                self.assertIn("The run itself is unchanged", body)
                self.assertEqual(
                    store.get_session("pw-session-paused").state, "paused"
                )
                self.assertEqual(
                    store.list_guidance("pw-session-paused"), []
                )

                # An interrupt is a control intent, and it is just as durable.
                urlopen(Request(
                    base + "/runs/run-paused/guidance",
                    data=urlencode({
                        "csrf_token": app.CSRF_TOKEN,
                        "message": "",
                        "delivery_mode": "interrupt_and_send",
                    }).encode(),
                    method="POST",
                )).read()
                self.assertEqual(
                    store.list_control_intents("pw-session-paused"), []
                )

                # An unknown mode is refused outright rather than falling
                # through and queueing the message as ordinary guidance.
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(
                        base + "/runs/run-paused/guidance",
                        data=urlencode({
                            "csrf_token": app.CSRF_TOKEN,
                            "message": "do the thing",
                            "delivery_mode": "totally-bogus",
                        }).encode(),
                        method="POST",
                    ))
                self.assertEqual(caught.exception.code, 400)
                self.assertEqual(
                    store.list_guidance("pw-session-paused"), []
                )

    def test_error_responses_render_a_page_with_a_way_back(self):
        """106 send_error() calls rendered http.server's bare page: no styling
        and no way back but the browser Back button."""

        with self.serving() as base:
            with self.assertRaises(HTTPError) as stale_token:
                urlopen(Request(
                    base + "/add",
                    data=urlencode({
                        "csrf_token": "stale-token-from-an-old-tab",
                        "url": "https://review.whamcloud.com/c/1",
                    }).encode(),
                    method="POST",
                ))
            token_body = stale_token.exception.read().decode()

            with self.assertRaises(HTTPError) as missing:
                urlopen(base + "/no-such-page")
            missing_body = missing.exception.read().decode()

        self.assertEqual(stale_token.exception.code, 403)
        self.assertIn("Invalid request token", token_body)
        self.assertIn("href='/'", token_body)
        self.assertIn("Return to Patch Watcher", token_body)
        self.assertIn("Reload the dashboard", token_body)
        self.assertNotIn("Error response", token_body)
        self.assertEqual(missing.exception.code, 404)
        self.assertIn("Return to Patch Watcher", missing_body)
        self.assertNotIn("Error response", missing_body)


class AddPatchConcurrencyTests(unittest.TestCase):
    """The server is threaded, so add_patch must check and append atomically."""

    def setUp(self):
        app.PATCHES.clear()
        self.addCleanup(app.PATCHES.clear)

    def test_concurrent_adds_of_one_url_produce_one_patch(self):
        url = "https://review.whamcloud.com/c/fs/lustre-release/+/68160"
        errors = []
        barrier = threading.Barrier(8)

        def add():
            barrier.wait()
            _, error = app.add_patch(url)
            if error:
                errors.append(error)

        threads = [threading.Thread(target=add) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(app.PATCHES), 1, "the watch list gained duplicates")
        self.assertEqual(len(errors), 7, "every loser should be told it is a duplicate")

    def test_a_second_sequential_add_is_still_refused(self):
        url = "https://review.whamcloud.com/c/fs/lustre-release/+/68161"
        self.assertIsNone(app.add_patch(url)[1])
        self.assertIsNotNone(app.add_patch(url)[1])


class SaveWatchFileConcurrencyTests(unittest.TestCase):
    """Both callers of save_watch_file are HTTP handlers on worker threads."""

    def setUp(self):
        app.PATCHES.clear()
        self.addCleanup(app.PATCHES.clear)
        app.PATCHES.extend(
            {"url": f"https://review.whamcloud.com/c/fs/lustre-release/+/{60000 + i}"}
            for i in range(40)
        )

    def test_concurrent_saves_never_publish_a_truncated_watch_file(self):
        """A shared fixed temp name let one writer truncate another's file.

        The published list then went to zero lines: the second writer's open()
        truncated the file the first was about to rename into place. Before the
        fix this loop published a short file 69 times and raised
        FileNotFoundError on 678 of 1200 saves.
        """

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "patches.txt"
            short_reads = []
            failures = []
            barrier = threading.Barrier(3)

            def save():
                barrier.wait()
                for _ in range(150):
                    try:
                        app.save_watch_file(target)
                    except OSError as exc:
                        failures.append(exc)
                        continue
                    try:
                        lines = target.read_text(encoding="utf-8").splitlines()
                    except OSError as exc:
                        failures.append(exc)
                        continue
                    if len(lines) != 40:
                        short_reads.append(len(lines))

            threads = [threading.Thread(target=save) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            self.assertEqual(short_reads, [])
            self.assertEqual(
                len(target.read_text(encoding="utf-8").splitlines()), 40
            )
            leftovers = [item.name for item in Path(directory).iterdir()]
            self.assertEqual(leftovers, ["patches.txt"], "a temp file was left behind")

    def test_the_watch_file_is_private_and_holds_only_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "patches.txt"
            app.save_watch_file(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                target.read_text(encoding="utf-8").splitlines()[0],
                "https://review.whamcloud.com/c/fs/lustre-release/+/60000",
            )


class MainArgumentTests(unittest.TestCase):
    """`main(argv)` used to ignore argv entirely, so nothing could test init."""

    def test_main_parses_the_argv_it_is_given(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit) as caught:
            app.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--port", out.getvalue())

    def test_an_unknown_option_is_rejected_from_argv(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            app.main(["--no-such-option"])
        self.assertNotEqual(caught.exception.code, 0)


class ConfirmationKeySeparationTests(unittest.TestCase):
    """The confirmation signing key must never be readable from a page.

    CSRF_TOKEN was also the HMAC key, and it is rendered as a hidden field in
    every page -- so anyone who could read one page body could mint a valid
    confirmation for any purpose with attacker-chosen values, reducing the
    prepare/confirm/start flow to the CSRF check it already had. Proved by
    forging an /engineering-runs/start with no prepare and no confirm page.
    """

    def test_the_signing_key_is_not_the_page_token(self):
        self.assertNotEqual(app._CONFIRMATION_KEY, app.CSRF_TOKEN.encode("utf-8"))

    def test_the_signing_key_is_never_rendered(self):
        rendered = app.page()
        self.assertIn(app.CSRF_TOKEN, rendered, "the CSRF token is still needed in forms")
        self.assertNotIn(app._CONFIRMATION_KEY.hex(), rendered)
        self.assertNotIn(
            app._CONFIRMATION_KEY.decode("utf-8", errors="replace"), rendered,
        )

    def test_a_confirmation_cannot_be_forged_from_the_page_token(self):
        forged = hmac.new(
            app.CSRF_TOKEN.encode("utf-8"),
            json.dumps(["engineering-start", "1"], separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertFalse(app._verify_confirmation(forged, "engineering-start", "1"))
        self.assertTrue(
            app._verify_confirmation(
                app._signed_confirmation("engineering-start", "1"),
                "engineering-start", "1",
            )
        )


class LoopbackHostTests(unittest.TestCase):
    """Only requests addressed to this loopback service by name are served.

    Without this the server answers to any Host, so a page whose DNS rebinds to
    127.0.0.1 is same-origin to the browser and can READ the dashboard -- and
    with it the CSRF token -- rather than merely posting to it blind.
    """

    def test_loopback_names_are_accepted_with_and_without_a_port(self):
        for host in ("127.0.0.1", "127.0.0.1:8080", "localhost:8080", "[::1]:8080"):
            with self.subTest(host=host):
                self.assertTrue(app._allowed_host(host, 8080))

    def test_foreign_names_and_wrong_ports_are_refused(self):
        for host in ("evil.example.com", "evil.example.com:8080",
                     "127.0.0.1:9999", "127.0.0.1.evil.com", "", None):
            with self.subTest(host=host):
                self.assertFalse(app._allowed_host(host, 8080))

    def test_a_rebound_host_is_refused_over_http(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            request = Request(f"http://127.0.0.1:{port}/")
            request.add_header("Host", "evil.example.com")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=10)
            self.assertEqual(caught.exception.code, 421)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class AgentModelAndEffortTests(unittest.TestCase):
    """The operator chooses the model and effort a run spends.

    The CLI spec accepted --model and --effort all along, but nothing fed
    them: no store column, no form field, no handler wiring. Every run used
    one process-wide default and the run page reported the literal text
    "Configured default".
    """

    def _serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _post(self, url, fields):
        request = Request(url, data=urlencode(fields).encode(), method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        opener = build_opener(_NoRedirect)
        try:
            return opener.open(request)
        except HTTPError as exc:
            return exc

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # These initializers assign module globals. Leaving a RUN_CONTROLLER
        # behind that points at this test's deleted temp directory breaks
        # every later test in the file that renders the dashboard.
        previous = (app.SESSION_STORE, app.AUTOMATION_STORE, app.RUN_CONTROLLER)

        def restore():
            app.SESSION_STORE, app.AUTOMATION_STORE, app.RUN_CONTROLLER = previous

        self.addCleanup(restore)
        root = Path(self.temporary.name)
        app.initialize_session_store(root / "sessions.sqlite3")
        app.initialize_automation_store(root / "automation.sqlite3")
        app.initialize_run_controller(
            runs_directory=root / "runs", start=False,
            model="claude-sonnet-5", effort="high",
        )
        app.PATCHES.clear()
        self.addCleanup(app.PATCHES.clear)
        self.patch, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        self.patch.update(
            change_number=68160, patchset=4, revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4", project="fs/lustre-release",
            lifecycle="Open", engineering_eligible=True,
        )

    def _prepare(self, base, **choice):
        response = self._post(base + "/engineering-runs/prepare", {
            "csrf_token": app.CSRF_TOKEN, "change_number": "68160",
            "patchset": "4", "revision_sha": "d" * 40, **choice,
        })
        self.assertEqual(response.code, 303)
        query = parse_qs(urlparse(response.headers["Location"]).query)
        return {
            "csrf_token": app.CSRF_TOKEN, "change_number": "68160",
            "patchset": "4", "revision_sha": "d" * 40,
            "confirmation_token": query["confirmation_token"][0],
            "idempotency_token": query["idempotency_token"][0],
            "confirmation_expires_at": query["confirmation_expires_at"][0],
        }, query

    def test_the_chosen_model_and_effort_reach_the_run_and_the_run_page(self):
        base = self._serve()
        signed, query = self._prepare(base, model="claude-opus-5", effort="max")
        self.assertEqual(query["model"], ["claude-opus-5"])
        self.assertEqual(query["effort"], ["max"])

        confirmation = urlopen(
            base + "/engineering-runs/confirm-start?" + urlencode(
                {k: v[0] for k, v in query.items()}
            )
        ).read().decode()
        self.assertIn("claude-opus-5", confirmation)
        self.assertIn("max", confirmation)

        started = self._post(base + "/engineering-runs/start", dict(
            signed, model="claude-opus-5", effort="max"
        ))
        self.assertEqual(started.code, 303)

        session = app.SESSION_STORE.list_sessions()[0]
        self.assertEqual(session.model, "claude-opus-5")
        self.assertEqual(session.effort, "max")
        projection = app._run_projection(session)
        self.assertEqual(projection["model"], "claude-opus-5")
        self.assertEqual(projection["effort"], "max")
        self.assertIn("Reasoning effort", render_run_detail(projection))

    def test_a_run_started_without_a_choice_falls_back_to_the_default(self):
        base = self._serve()
        signed, _ = self._prepare(base)
        self.assertEqual(
            self._post(base + "/engineering-runs/start", signed).code, 303
        )
        session = app.SESSION_STORE.list_sessions()[0]
        self.assertEqual(session.model, "")
        projection = app._run_projection(session)
        # Falls back to what the controller was configured with, and says so.
        self.assertEqual(projection["model"], "claude-sonnet-5")
        self.assertEqual(projection["effort"], "high")

    def test_the_confirmation_signature_covers_the_choice(self):
        """A confirmation page showing one model must not start another."""

        base = self._serve()
        signed, _ = self._prepare(base, model="claude-opus-5", effort="max")
        swapped = self._post(base + "/engineering-runs/start", dict(
            signed, model="claude-haiku-4-5-20251001", effort="max"
        ))
        self.assertEqual(swapped.code, 403)
        self.assertEqual(app.SESSION_STORE.list_sessions(), [])

    def test_a_value_that_was_never_offered_is_refused(self):
        base = self._serve()
        signed, _ = self._prepare(base, model="claude-opus-5", effort="max")
        for field, value in (
            ("effort", "ludicrous"),
            ("model", "a; rm -rf /"),
            ("model", "../../etc/passwd"),
            ("model", "-flag"),
        ):
            with self.subTest(field=field, value=value):
                fields = dict(signed, model="claude-opus-5", effort="max")
                fields[field] = value
                refused = self._post(base + "/engineering-runs/start", fields)
                # These reach a subprocess argument list, so they are rejected
                # rather than escaped.
                self.assertEqual(refused.code, 400)
        self.assertEqual(app.SESSION_STORE.list_sessions(), [])

    def test_the_start_forms_actually_offer_the_control(self):
        rendered = render_engineering_start_control(
            self.patch, csrf_token=app.CSRF_TOKEN, idempotency_token="t"
        )
        self.assertIn("name='model'", rendered)
        self.assertIn("name='effort'", rendered)
        for level in app.AGENT_EFFORTS:
            self.assertIn(f"<option value='{level}'", rendered)
        investigate = render_investigate_control(
            self.patch, csrf_token=app.CSRF_TOKEN, idempotency_token="t"
        )
        self.assertIn("name='model'", investigate)
        self.assertIn("name='effort'", investigate)


class RunVersionGuardTests(unittest.TestCase):
    """Five run-control forms submitted expected_version and nothing read it.

    The projection also hardcoded "version": 0, so the optimistic-concurrency
    guard the forms advertise could never have discriminated anything: two
    tabs open on the same run each applied their control against a stale view
    with no conflict detection.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        previous = (app.SESSION_STORE, app.RUN_CONTROLLER)

        def restore():
            app.SESSION_STORE, app.RUN_CONTROLLER = previous

        self.addCleanup(restore)
        self.store = app.initialize_session_store(
            Path(self.temporary.name) / "sessions.sqlite3"
        )
        self.store.register_pinned_session(
            "pw-session-version",
            patch_id="68160",
            run_id="run-version",
            revision="d" * 40,
            patchset=4,
            profile="engineering",
            state="running",
        )

    @contextlib.contextmanager
    def serving(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _version(self):
        return str(
            app._run_version(self.store.get_session("pw-session-version"))
        )

    def test_the_rendered_version_changes_when_the_run_does(self):
        first = self._version()
        self.store.set_state("pw-session-version", "paused")
        second = self._version()
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, "0")
        projection = app._run_projection(
            self.store.get_session("pw-session-version")
        )
        self.assertEqual(str(projection["version"]), second)

    def test_a_control_carrying_a_stale_version_is_refused(self):
        stale = self._version()
        self.store.set_state("pw-session-version", "paused")
        with self.serving() as base:
            request = Request(
                base + "/runs/run-version/guidance",
                data=urlencode({
                    "csrf_token": app.CSRF_TOKEN,
                    "message": "please continue",
                    "delivery_mode": "queue",
                    "expected_version": stale,
                }).encode(),
                method="POST",
            )
            request.add_header(
                "Content-Type", "application/x-www-form-urlencoded"
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 409)
            # Refused before anything was applied.
            self.assertEqual(self.store.list_guidance("pw-session-version"), [])

            # The current version is accepted.
            request = Request(
                base + "/runs/run-version/guidance",
                data=urlencode({
                    "csrf_token": app.CSRF_TOKEN,
                    "message": "please continue",
                    "delivery_mode": "queue",
                    "expected_version": self._version(),
                }).encode(),
                method="POST",
            )
            request.add_header(
                "Content-Type", "application/x-www-form-urlencoded"
            )
            self.assertEqual(urlopen(request).status, 200)
        self.assertEqual(len(self.store.list_guidance("pw-session-version")), 1)


class RemovedPatchForgetsItsPolicyTests(AppGlobalsIsolated):
    """Removing a patch must withdraw its automation consent too.

    The standing policy is keyed by change number and outlived the watch-list
    entry, so re-adding the same change -- the normal way to resume after a
    rebase or a mistaken removal -- silently reactivated whatever it had been
    set to, up to and including trigger_mode=automatic, with no confirmation
    and nothing on screen saying so.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        app.initialize_automation_store(root / "automation.sqlite3")
        self.policies = app.initialize_standing_policy_store(root / "standing.json")
        app.PATCHES.clear()
        self.addCleanup(app.PATCHES.clear)
        app.ACTIVE_WATCH_FILE = root / "patches.txt"
        self.patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        self.patch_record.update(
            change_number=68160, patchset=4, revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4", project="fs/lustre-release",
            lifecycle="Open",
        )
        app.sync_automation_patch(self.patch_record)

    @contextlib.contextmanager
    def serving(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _remove(self, base, url):
        # The removal is twice-confirmed: the first POST renders the
        # confirmation page carrying a signed one-use token.
        first = urlopen(Request(
            base + "/remove",
            data=urlencode({"csrf_token": app.CSRF_TOKEN, "url": url}).encode(),
            method="POST",
        )).read().decode()
        token = re.search(
            r"name='confirmation_token' value='([^']+)'", first
        ).group(1)
        expires = re.search(
            r"name='confirmation_expires_at' value='([^']+)'", first
        ).group(1)
        urlopen(Request(
            base + "/remove",
            data=urlencode({
                "csrf_token": app.CSRF_TOKEN, "url": url,
                "confirmation_token": token,
                "confirmation_expires_at": expires,
            }).encode(),
            method="POST",
        )).read()
        return first

    def test_removing_a_patch_clears_its_standing_policy(self):
        saved = self.policies.save(app.PatchAutomationPolicy(
            "68160", trigger_mode="automatic", test_failures="deterministic",
        ))
        self.assertEqual(saved.trigger_mode, "automatic")

        with self.serving() as base:
            confirmation = self._remove(base, self.patch_record["url"])

        # get() returns a fresh default rather than None when nothing is
        # stored, so "gone" means back to version 0 with automation off.
        after = self.policies.get("68160")
        self.assertEqual(after.version, 0)
        self.assertEqual(after.trigger_mode, "manual")
        self.assertEqual(after.test_failures, "off")
        # And the confirmation page said so before doing it.
        self.assertIn("clears", confirmation)
        self.assertIn("standing automation policy", confirmation)

        # Re-adding starts from the defaults, not from "automatic".
        again, error = app.add_patch("https://review.whamcloud.com/c/68160")
        self.assertIsNone(error)
        again.update(change_number=68160, patchset=4, revision_sha="d" * 40)
        self.assertNotEqual(
            getattr(app._standing_policy(again), "trigger_mode", None), "automatic"
        )

    def test_a_failing_policy_removal_never_blocks_the_removal(self):
        self.policies.save(app.PatchAutomationPolicy("68160", trigger_mode="manual"))

        def explode(*args, **kwargs):
            raise OSError("policy file is unwritable")

        with patch.object(app.STANDING_POLICY_STORE, "remove", explode), \
                self.serving() as base:
            self._remove(base, self.patch_record["url"])

        # The patch the operator asked to remove is gone regardless.
        self.assertEqual(app.PATCHES, [])


class OperatorGuestControlTests(AppGlobalsIsolated):
    """Shut down and destroy act on guests Patch Watcher does not own.

    That is deliberate -- the panel they live in is exactly the guests with no
    owner match -- so the controller's ownership proof does not apply and the
    authority is the human at the dashboard. Which means the CSRF token and,
    for the irreversible one, an explicit confirmation are the only gates, and
    both have to actually hold.
    """

    def setUp(self):
        self.calls = []

        class FakeAdapter:
            def __init__(inner):
                pass

            def operator_stop(inner, name):
                self.calls.append(("stop", name))

            def operator_destroy(inner, name):
                self.calls.append(("destroy", name))

        self.patcher = patch.object(app, "LTVMAdapter", FakeAdapter)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(app._ENGINEERING_USED_CONFIRMATIONS.clear)

    @contextlib.contextmanager
    def serving(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _post(self, base, path, fields):
        request = Request(base + path, data=urlencode(fields).encode(), method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            return urlopen(request)
        except HTTPError as exc:
            return exc

    def test_shutdown_reaches_the_adapter_with_the_exact_name(self):
        with self.serving() as base:
            response = self._post(base, "/vms/stop", {
                "csrf_token": app.CSRF_TOKEN, "name": "co1-diotests",
            })
        self.assertEqual(response.code, 200)
        self.assertEqual(self.calls, [("stop", "co1-diotests")])

    def test_destroy_asks_first_and_only_acts_on_the_confirmed_name(self):
        with self.serving() as base:
            first = self._post(base, "/vms/destroy", {
                "csrf_token": app.CSRF_TOKEN, "name": "co9-bench2",
            }).read().decode()
            # Nothing has happened yet -- the first POST is the question.
            self.assertEqual(self.calls, [])
            self.assertIn("cannot be undone", first)
            self.assertIn("co9-bench2", first)

            token = re.search(
                r"name='confirmation_token' value='([^']+)'", first
            ).group(1)
            expires = re.search(
                r"name='confirmation_expires_at' value='([^']+)'", first
            ).group(1)

            # A token minted for one guest must not destroy another.
            self._post(base, "/vms/destroy", {
                "csrf_token": app.CSRF_TOKEN, "name": "co1-diotests",
                "confirmation_token": token, "confirmation_expires_at": expires,
            })
            self.assertEqual(self.calls, [])

            self._post(base, "/vms/destroy", {
                "csrf_token": app.CSRF_TOKEN, "name": "co9-bench2",
                "confirmation_token": token, "confirmation_expires_at": expires,
            })
        self.assertEqual(self.calls, [("destroy", "co9-bench2")])

    def test_a_confirmation_is_one_use(self):
        with self.serving() as base:
            first = self._post(base, "/vms/destroy", {
                "csrf_token": app.CSRF_TOKEN, "name": "co9-ior",
            }).read().decode()
            token = re.search(r"name='confirmation_token' value='([^']+)'", first).group(1)
            expires = re.search(
                r"name='confirmation_expires_at' value='([^']+)'", first
            ).group(1)
            fields = {
                "csrf_token": app.CSRF_TOKEN, "name": "co9-ior",
                "confirmation_token": token, "confirmation_expires_at": expires,
            }
            self._post(base, "/vms/destroy", fields)
            self._post(base, "/vms/destroy", fields)
        self.assertEqual(self.calls, [("destroy", "co9-ior")])

    def test_a_name_that_could_be_an_argument_is_refused(self):
        with self.serving() as base:
            for hostile in ("-rf", "co1-a;rm -rf /", "../../etc/passwd",
                            "co1 a", "", "co1-a\nco2-b"):
                with self.subTest(hostile):
                    response = self._post(base, "/vms/stop", {
                        "csrf_token": app.CSRF_TOKEN, "name": hostile,
                    })
                    self.assertEqual(response.code, 400)
        self.assertEqual(self.calls, [])

    def test_neither_control_works_without_the_request_token(self):
        with self.serving() as base:
            for path in ("/vms/stop", "/vms/destroy"):
                with self.subTest(path):
                    response = self._post(base, path, {"name": "co1-single"})
                    self.assertEqual(response.code, 403)
        self.assertEqual(self.calls, [])


class ArtifactDownloadTests(unittest.TestCase):
    """The download route had no test at all, and it buffered whole files.

    An artifact may be up to MAX_ARTIFACT_BYTES (2 GiB). Reading it into
    memory to discover it was the wrong size meant K concurrent fetches could
    hold K x 2 GiB resident on a server with no concurrency limit.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        previous = (app.SESSION_STORE, app.RUN_CONTROLLER)

        def restore():
            app.SESSION_STORE, app.RUN_CONTROLLER = previous

        self.addCleanup(restore)
        store = app.initialize_session_store(self.root / "sessions.sqlite3")
        store.register_pinned_session(
            "pw-session-artifact",
            patch_id="68160",
            run_id="pw-engineer-68160-ps4-artifact",
            revision="d" * 40,
            patchset=4,
            profile="engineering",
        )
        self.artifact_root = (
            self.root / "runs" / "engineering-artifacts"
            / "pw-engineer-68160-ps4-artifact"
        )
        self.artifact_root.mkdir(parents=True)
        # Comfortably larger than the 1 MiB streaming chunk, so the loop runs
        # more than once.
        self.payload = (b"diff --git a/lustre b/lustre\n" * 120_000)
        (self.artifact_root / "salvaged.patch").write_bytes(self.payload)
        self.metadata = SimpleNamespace(
            artifact_id="salvaged-diff",
            relative_path="salvaged.patch",
            size_bytes=len(self.payload),
            sha256=hashlib.sha256(self.payload).hexdigest(),
            media_type="text/x-diff",
        )
        artifacts = [self.metadata]
        app.RUN_CONTROLLER = SimpleNamespace(
            runs_directory=self.root / "runs",
            engineering_store=SimpleNamespace(
                list_artifacts=lambda run_id: artifacts
            ),
            stop=lambda: None,
        )

    @contextlib.contextmanager
    def serving(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _url(self, base, artifact_id="salvaged-diff"):
        return (
            f"{base}/runs/pw-engineer-68160-ps4-artifact/artifacts/{artifact_id}"
        )

    def test_a_verified_artifact_is_served_whole_and_byte_exact(self):
        with self.serving() as base:
            response = urlopen(self._url(base))
            body = response.read()
        self.assertEqual(body, self.payload)
        self.assertEqual(
            int(response.headers["Content-Length"]), len(self.payload)
        )
        self.assertEqual(response.headers["Content-Type"], "text/x-diff")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_a_file_that_no_longer_matches_its_ledger_is_refused(self):
        for corruption, description in (
            (self.payload + b"extra", "grown"),
            (self.payload[:-10], "truncated"),
        ):
            with self.subTest(description):
                (self.artifact_root / "salvaged.patch").write_bytes(corruption)
                with self.serving() as base, self.assertRaises(HTTPError) as caught:
                    urlopen(self._url(base))
                self.assertEqual(caught.exception.code, 409)

        # Same length, different bytes: only the digest catches this one.
        swapped = bytearray(self.payload)
        swapped[5] = swapped[5] ^ 0xFF
        (self.artifact_root / "salvaged.patch").write_bytes(bytes(swapped))
        with self.serving() as base, self.assertRaises(HTTPError) as caught:
            urlopen(self._url(base))
        self.assertEqual(caught.exception.code, 409)

    def test_an_unknown_artifact_and_an_escaping_path_are_refused(self):
        with self.serving() as base:
            with self.assertRaises(HTTPError) as caught:
                urlopen(self._url(base, "no-such-artifact"))
            self.assertEqual(caught.exception.code, 404)

            # A ledger row that points outside the run's own artifact
            # directory must not be served, however it got there.
            secret = self.root / "secret.txt"
            secret.write_bytes(b"not yours")
            self.metadata.relative_path = "../../../secret.txt"
            self.metadata.size_bytes = 9
            self.metadata.sha256 = hashlib.sha256(b"not yours").hexdigest()
            with self.assertRaises(HTTPError) as caught:
                urlopen(self._url(base))
            self.assertEqual(caught.exception.code, 404)


class HandlerFaultGuardTests(AppGlobalsIsolated):
    """Any handler fault must produce a readable page, not a dropped socket.

    Only `GET /` rendered inside a try. Every other route -- and nearly every
    POST, which re-renders the whole dashboard after it has already mutated
    state -- answered an unreadable store or a failing `ltvm` by unwinding into
    socketserver, so the browser got ERR_EMPTY_RESPONSE and the operator could
    not tell whether the click had taken effect.
    """

    def _serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_a_failing_get_answers_500_rather_than_closing_the_connection(self):
        port = self._serve()
        with patch.object(app, "page", side_effect=RuntimeError("store is unreadable")):
            with self.assertRaises(HTTPError) as caught:
                urlopen(f"http://127.0.0.1:{port}/runs/pw-does-not-exist", timeout=10)
        self.assertEqual(caught.exception.code, 404)

        with patch.object(
            app, "resource_dashboard_html", side_effect=RuntimeError("ltvm is unreadable")
        ), self.assertRaises(HTTPError) as caught:
            urlopen(f"http://127.0.0.1:{port}/", timeout=10)
        self.assertEqual(caught.exception.code, 500)
        body = caught.exception.read().decode()
        self.assertIn("ltvm is unreadable", body)

    def test_a_failing_post_answers_500_rather_than_closing_the_connection(self):
        port = self._serve()
        payload = urlencode({"csrf_token": app.CSRF_TOKEN}).encode()
        request = Request(f"http://127.0.0.1:{port}/refresh-all", data=payload)
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with patch.object(
            app, "refresh_watched_patch", side_effect=RuntimeError("gerrit is unreachable")
        ), patch.object(app, "PATCHES", [{"url": "https://review.whamcloud.com/c/1"}]):
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 500)
        self.assertIn("gerrit is unreachable", caught.exception.read().decode())

    def test_a_refusal_message_outside_latin_1_still_reaches_the_browser(self):
        """send_error puts its message in the latin-1 status line.

        A single non-Latin character in exception text -- and these messages
        quote Gerrit values and operator input -- raised UnicodeEncodeError
        inside send_response_only, dropping the connection with no error page.
        """

        self.assertEqual(app._status_line_text("caf\u00e9 \u2603 snowman"), "caf\u00e9 ? snowman")
        self.assertEqual(app._status_line_text("two\nlines\ttabbed"), "two lines tabbed")
        port = self._serve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app.initialize_automation_store(root / "automation.sqlite3")
            app.initialize_standing_policy_store(root / "standing.json")
            app.PATCHES.clear()
            self.addCleanup(app.PATCHES.clear)
            patch_record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
            patch_record.update(
                change_number=68160, patchset=4, revision_sha="d" * 40,
                revision_ref="refs/changes/60/68160/4",
                project="fs/lustre-release", lifecycle="Open",
            )
            app.sync_automation_patch(patch_record)
            payload = urlencode({
                "csrf_token": app.CSRF_TOKEN,
                "change_number": "68160", "patchset": "4",
                "revision_sha": "d" * 40, "expected_version": "0",
                "trigger_mode": "manual", "test_failures": "\u2603snowman",
                "build_failures": "repair", "review_comments": "simple",
            }).encode()
            request = Request(f"http://127.0.0.1:{port}/standing-policy", data=payload)
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=10)
            self.assertEqual(caught.exception.code, 409)
            self.assertIn("test_failures", caught.exception.reason)
            self.assertTrue(caught.exception.read(), "the refusal had no body")


class AddPatchRoutePersistenceTests(unittest.TestCase):
    """POST /add must persist the watch list, not fall through a stray return.

    A lint-driven statement split hoisted the `return` out of `if error:`,
    making the refresh, the retest tick and `save_watch_file` unreachable: an
    added patch responded with zero bytes and was lost on restart.
    """

    def test_a_successful_add_reaches_the_watch_file_write(self):
        source = inspect.getsource(app.Handler._dispatch_post)
        marker = source.index('elif path == "/add":')
        block = source[marker:marker + 700]
        self.assertIn("save_watch_file", block)
        add_body = block[:block.index("elif path ==", 10)]
        self.assertIn("refresh_watched_patch", add_body)
        # The `return` must be indented deeper than the `if error:` it belongs to.
        lines = [line for line in add_body.split("\n") if line.strip()]
        error_line = next(i for i, line in enumerate(lines) if line.strip() == "if error:")
        indent = len(lines[error_line]) - len(lines[error_line].lstrip())
        following = lines[error_line + 1:error_line + 3]
        for line in following:
            self.assertGreater(
                len(line) - len(line.lstrip()), indent,
                f"{line.strip()!r} escaped the `if error:` body",
            )



class ControllerFailuresPanelTests(unittest.TestCase):
    """Controller faults must be visible in the UI, not only on disk.

    These are the failures that stop dispatch entirely -- a locked database, an
    unreadable LTVM inventory -- so they are deliberately recorded outside the
    session store the controller may be unable to read. Without a panel the
    operator sees a tool that has silently stopped working.
    """

    def setUp(self):
        self.original = app.RUN_CONTROLLER
        self.addCleanup(lambda: setattr(app, "RUN_CONTROLLER", self.original))

    def install(self, failures):
        app.RUN_CONTROLLER = SimpleNamespace(
            controller_failures=lambda: failures
        )

    def test_nothing_is_rendered_when_there_are_no_failures(self):
        self.install([])
        self.assertEqual(app.controller_failures_html(), "")

    def test_a_failure_names_its_scope_type_and_summary(self):
        self.install([{
            "scope": "ltvm_inventory", "error_type": "OSError",
            "summary": "ltvm list failed", "count": 1,
            "last_seen": "2026-09-07T18:00:00+00:00",
        }])
        html = app.controller_failures_html()
        self.assertIn("ltvm_inventory", html)
        self.assertIn("OSError", html)
        self.assertIn("ltvm list failed", html)
        self.assertIn("role='alert'", html)

    def test_a_repeated_failure_shows_its_count(self):
        self.install([{
            "scope": "tick", "error_type": "OperationalError",
            "summary": "database is locked", "count": 42,
            "last_seen": "2026-09-07T18:00:00+00:00",
        }])
        self.assertIn("seen 42", app.controller_failures_html())

    def test_untrusted_failure_text_is_escaped(self):
        self.install([{
            "scope": "<script>x</script>", "error_type": "E",
            "summary": "<img src=x onerror=alert(1)>", "count": 1,
            "last_seen": "now",
        }])
        html = app.controller_failures_html()
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)

    def test_an_unreadable_record_does_not_break_the_page(self):
        def explode():
            raise OSError("record is corrupt")

        app.RUN_CONTROLLER = SimpleNamespace(controller_failures=explode)
        html = app.controller_failures_html()
        self.assertIn("could not be read", html)

    def test_no_controller_renders_nothing(self):
        app.RUN_CONTROLLER = None
        self.assertEqual(app.controller_failures_html(), "")

    def test_the_panel_reaches_the_dashboard(self):
        self.install([{
            "scope": "tick", "error_type": "OperationalError",
            "summary": "database is locked", "count": 3, "last_seen": "now",
        }])
        self.assertIn("Controller failures", app.page())


class TimelineEventSummaryTests(unittest.TestCase):
    """The number an operator is watching for must survive into the timeline.

    The destroy ladder and the runner stop ladder both record the SINGULAR
    ``attempt``; a summary that looked only for the plural ``attempts`` threw
    away the one fact that says how close a failing guest is to being given up
    on, and two writers that record no explanatory key at all rendered as the
    bare word "Recorded".
    """

    def test_ltvm_destroy_ladder_shows_the_guest_the_failure_and_the_rung(self):
        summary = app._event_summary({
            "resource_type": "vm", "name": "co3-sanity",
            "failure_type": "TimeoutError", "attempt": 3,
        })
        self.assertIn("co3-sanity", summary)
        self.assertIn("TimeoutError", summary)
        self.assertIn("3", summary)

    def test_stuck_cleanup_shows_its_attempt_count(self):
        summary = app._event_summary({
            "resource_type": "vm", "name": "co3-sanity", "attempt": 2,
        })
        self.assertIn("co3-sanity", summary)
        self.assertIn("2", summary)

    def test_runner_stop_attempt_is_not_the_word_recorded(self):
        first = app._event_summary(
            {"attempt": 1, "force": False, "failure_type": None}
        )
        self.assertNotEqual(first, "Recorded")
        self.assertIn("1", first)
        escalated = app._event_summary(
            {"attempt": 3, "force": True, "failure_type": "BrokenPipeError"}
        )
        self.assertIn("BrokenPipeError", escalated)
        self.assertIn("3", escalated)

    def test_salvaged_diff_reports_the_size_it_recorded(self):
        summary = app._event_summary({"size_bytes": 41273, "quiesced": False})
        self.assertNotEqual(summary, "Recorded")
        self.assertIn("41273", summary)

    def test_give_up_events_keep_their_plural_counter_and_explanation(self):
        summary = app._event_summary({
            "resource_type": "vm", "name": "co3-sanity", "member_names": [],
            "attempts": 3, "detail": "destroy kept failing",
        })
        self.assertIn("destroy kept failing", summary)
        self.assertIn("3", summary)

    def test_a_payload_with_nothing_to_say_still_says_recorded(self):
        self.assertEqual(app._event_summary({}), "Recorded")
        self.assertEqual(app._event_summary(None), "Recorded")
        self.assertEqual(app._event_summary({"force": True}), "Recorded")


class RunProjectionCapabilityAndProcessTests(AppGlobalsIsolated):
    """What the run page says about capability and process must be the run's.

    Both were read from the wrong source: the SESSION profile, which says
    "engineering" for a read-only investigation, and the host process id,
    which is Patch Watcher's own and identical for every concurrent run.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = app.initialize_session_store(
            Path(self.temp.name) / "sessions.sqlite3"
        )
        self.addCleanup(lambda: setattr(app, "SESSION_STORE", None))
        app.RUN_CONTROLLER = None
        self.addCleanup(lambda: setattr(app, "RUN_CONTROLLER", None))
        self.counter = 0

    def session(self, event_type, payload):
        self.counter += 1
        session_id = f"session-capability-{self.counter}"
        run_id = f"pw-engineer-6816{self.counter}-ps4-cap"
        self.store.register_pinned_session(
            session_id, patch_id=f"6816{self.counter}", run_id=run_id,
            revision="a" * 40, patchset=4, profile="engineering",
            state="running",
        )
        if event_type is not None:
            self.store.append_event(
                session_id, event_type, payload,
                idempotency_key=f"request:{run_id}", at=datetime.now(UTC),
            )
        return self.store.get_session(session_id)

    def test_manual_investigation_is_projected_read_only(self):
        session = self.session("investigation_requested", {
            "change_number": 68160, "patchset": 4, "revision": "a" * 40,
            "project": "fs/lustre-release",
        })
        self.assertEqual(session.profile, "engineering")
        projection = app._run_projection(session)
        self.assertEqual(projection["capability_profile"], "read_only")
        rendered = render_run_detail(projection)
        self.assertIn("Read-only run:", rendered)
        self.assertNotIn("Engineering boundary", rendered)

    def test_agent_run_kinds_are_projected_with_full_capability(self):
        for event_type, kind in (
            ("engineering_run_requested", "engineering"),
            ("review_comment_run_requested", "review_comments"),
            ("jenkins_build_failure_run_requested", "build_failure"),
        ):
            session = self.session(event_type, {"request_kind": kind})
            projection = app._run_projection(session)
            self.assertEqual(projection["capability_profile"], "full", kind)
            self.assertIn("Engineering boundary", render_run_detail(projection))

    def test_failure_research_is_projected_read_only(self):
        session = self.session("unknown_failure_research_requested", {
            "request_kind": "unknown_failure_research",
        })
        self.assertEqual(
            app._run_projection(session)["capability_profile"], "read_only"
        )

    def test_process_projection_names_the_runs_own_agent_not_the_host(self):
        """The projection must name the agent, not the wrapper supervising it.

        `attach_runner_transport` records ``handle.host_identity.pid``. That
        is per-run -- ClaudeHost.start runs inside the spawned host process,
        so its ``os.getpid()`` is that wrapper rather than Patch Watcher --
        but it measures the wrapper's whole tree, which is the agent plus its
        supervision. An operator asking what a run costs wants the Claude
        process, which is ``claude_identity`` on the same durable handle.
        """
        session = self.session("engineering_run_requested",
                               {"request_kind": "engineering"})
        host_pid, agent_pid = 4242, 4343
        self.store.append_event(
            session.session_id, "runner_attached",
            {"handle": {
                "run_id": session.run_id, "session_id": session.session_id,
                "socket_path": "/run/sock", "event_log_path": "/run/events",
                "state_path": "/run/state",
                "host_identity": {"pid": host_pid, "start_token": "host",
                                  "process_group_id": host_pid},
                "claude_identity": {"pid": agent_pid, "start_token": "agent",
                                    "process_group_id": agent_pid},
            }},
            idempotency_key="runner-attached:" + session.run_id,
            at=datetime.now(UTC),
        )
        self.store.attach_runner_transport(
            session.session_id, transport="claude-stream-json-v1",
            transport_session_id="transport-1", pid=host_pid,
            process_started_at=datetime.now(UTC),
            process_fingerprint="sha256:" + "f" * 64,
        )
        refreshed = self.store.get_session(session.session_id)
        self.assertEqual(refreshed.pid, host_pid)
        projection = app._run_projection(refreshed)
        self.assertEqual(projection["process_pid"], agent_pid)
        self.assertEqual(projection["pid"], agent_pid)
        records, _ = app._session_dashboard_records()
        self.assertEqual(
            [record["process_id"] for record in records], [agent_pid]
        )

    def test_a_run_that_never_started_projects_no_process(self):
        session = self.session("investigation_requested", {})
        projection = app._run_projection(session)
        self.assertIsNone(projection["process_pid"])
        self.assertIsNone(projection["process_memory_bytes"])


class EngineeringProjectionOwnershipTests(AppGlobalsIsolated):
    """The view cannot recompute the run's guest name prefix; project it.

    The checkout index lives only in the immutable ``checkout_allocated``
    event, and the ``co<N>-`` prefix it yields is what actually establishes
    guest ownership -- nothing stamps an LTVM owner id.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = app.initialize_session_store(
            Path(self.temp.name) / "sessions.sqlite3"
        )
        self.addCleanup(lambda: setattr(app, "SESSION_STORE", None))
        app.RUN_CONTROLLER = None
        self.addCleanup(lambda: setattr(app, "RUN_CONTROLLER", None))

    def session(self, session_id, patch_id, checkout_index=None):
        run_id = f"pw-engineer-{patch_id}-ps4-own"
        self.store.register_pinned_session(
            session_id, patch_id=patch_id, run_id=run_id, revision="a" * 40,
            patchset=4, profile="engineering", state="running",
        )
        if checkout_index is not None:
            self.store.append_event(
                session_id, "checkout_allocated",
                {"checkout_index": checkout_index,
                 "vm_prefix": f"co{checkout_index}-",
                 "checkout_path": f"/co/{checkout_index}"},
                idempotency_key="checkout-allocated:" + run_id,
                at=datetime.now(UTC),
            )
        return self.store.get_session(session_id)

    def test_pooled_run_projects_its_reserved_guest_prefix(self):
        session = self.session("session-pooled", "68160", checkout_index=3)
        projection = app._engineering_projection(session)
        self.assertEqual(projection["checkout_index"], 3)
        self.assertEqual(projection["vm_prefix"], "co3-")

    def test_run_without_a_pool_checkout_reserves_no_prefix(self):
        session = self.session("session-clone", "68161")
        projection = app._engineering_projection(session)
        self.assertIsNone(projection["checkout_index"])
        self.assertEqual(projection["vm_prefix"], "")

    def test_the_projected_prefix_nests_a_sampled_guest_in_its_run(self):
        """End to end: no VM the sampler produces carries an owner id."""
        session = self.session("session-render", "68162", checkout_index=3)
        projection = app._engineering_projection(session)
        sampled = {
            "name": "co3-sanity", "state": "running", "owner_id": None,
            "patch_watcher_session_id": None,
            "configured_guest_memory_bytes": 4 * 1024 ** 3,
            "host_rss_bytes": 750 * 1024 ** 2, "process_id": 4242,
            "vcpus": 2, "ip": "192.168.100.11",
            "host_memory_source": "/proc/4242/status VmRSS",
            "quality": "good", "errors": [],
        }
        from patch_watcher import engineering_views
        card = engineering_views.render_engineering_run(projection, vms=[sampled])
        orphans = engineering_views.render_unmatched_resources(
            [projection], [sampled]
        )
        self.assertIn("Session-owned LTVM guests (1)", card)
        self.assertIn(">co3-sanity<", card)
        self.assertNotIn("co3-sanity", orphans)


class OperatorConsentTextTests(AppGlobalsIsolated):
    """The automation consent page must describe the writes it authorises."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        app.PATCHES.clear()
        self.addCleanup(app.PATCHES.clear)
        app.initialize_automation_store(root / "automation.sqlite3")
        app.initialize_standing_policy_store(root / "standing.json")
        self.addCleanup(lambda: setattr(app, "AUTOMATION_STORE", None))
        self.addCleanup(lambda: setattr(app, "STANDING_POLICY_STORE", None))
        record, _ = app.add_patch("https://review.whamcloud.com/c/68160")
        record.update(
            change_number=68160, patchset=4, revision_sha="d" * 40,
            revision_ref="refs/changes/60/68160/4",
            project="fs/lustre-release", lifecycle="Open",
        )
        app.sync_automation_patch(record)

    def confirmation_page(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}/standing-policy",
                data=urlencode({
                    "csrf_token": app.CSRF_TOKEN, "change_number": "68160",
                    "patchset": "4", "revision_sha": "d" * 40,
                    "expected_version": "0", "trigger_mode": "automatic",
                    "test_failures": "deterministic",
                    "build_failures": "repair", "review_comments": "simple",
                }).encode(), method="POST",
            )
            return urlopen(request).read().decode()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_consent_states_the_agent_publishes_its_own_gerrit_writes(self):
        body = self.confirmation_page()
        for expected in (
            "your own service credentials",
            "posts its own Gerrit replies",
            "uploads its own patchset",
            "Maloo retests",
        ):
            self.assertIn(expected, body)

    def test_consent_claims_no_switch_that_holds_writes_back(self):
        """gerrit_reply.py and jenkins_retrigger.py no longer exist."""
        body = self.confirmation_page()
        self.assertNotIn("controller switches", body)
        self.assertNotIn("remain independently disabled", body)


class AutonomousLaneReplayScopeTests(AppGlobalsIsolated):
    """The replay control must replay the scope its label promises.

    The per-patch button is labelled "Replay this exact revision" and posts
    one; the handler ignored it and replayed the whole recorded history,
    reporting a count about a different question entirely.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        app.initialize_autonomous_lanes(
            root / "lanes.json", root / "lane-audit.jsonl"
        )

    def _replay(self, **fields):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}"
                "/autonomous-lanes/replay",
                data=urlencode(
                    {"csrf_token": app.CSRF_TOKEN, **fields}
                ).encode(),
                method="POST",
            )
            return urlopen(request).read().decode()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_posted_revision_scopes_the_replay_to_that_revision(self):
        body = self._replay(
            change_number="68160", patchset="4", revision_sha="d" * 40
        )
        self.assertIn("d" * 40, body)
        self.assertNotIn("whole recorded decision history", body)

    def test_no_posted_revision_replays_the_whole_history(self):
        body = self._replay()
        self.assertIn("whole recorded decision history", body)

    def test_a_revision_with_no_recorded_decisions_says_so(self):
        body = self._replay(revision_sha="e" * 40)
        self.assertIn("No lane decisions are recorded", body)
        self.assertIn("e" * 40, body)


class LastResortRenderEncodingTests(unittest.TestCase):
    """One unencodable character must not take the whole dashboard down.

    The value is usually persisted -- an operator-typed patch title, an agent
    message, a guest name -- so a bare ``body.encode()`` did not fail one
    request, it failed every request from then on.
    """

    # Rendering the whole dashboard touches every store, and classes earlier
    # in this module leave some of them pointing at deleted temporary
    # databases. Start from a known-empty set so this test measures encoding.
    GLOBALS = (
        "AUTOMATION_STORE", "SESSION_STORE", "STANDING_POLICY_STORE",
        "AUTONOMOUS_LANE_STORE", "AUTONOMOUS_LANE_HISTORY",
        "AUTONOMOUS_LANE_RUNTIME", "RUN_CONTROLLER", "RETEST_CONTROLLER",
        "FAILURE_ACTION_CONTROLLER",
    )

    def setUp(self):
        app.PATCHES.clear()
        self.addCleanup(app.PATCHES.clear)
        saved = {name: getattr(app, name) for name in self.GLOBALS}
        for name in self.GLOBALS:
            setattr(app, name, None)
        self.addCleanup(lambda: [
            setattr(app, name, value) for name, value in saved.items()
        ])

    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return urlopen(f"http://127.0.0.1:{server.server_address[1]}/").read()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_lone_surrogate_in_a_patch_title_degrades_one_glyph(self):
        app.add_patch(
            "https://review.whamcloud.com/c/68160",
            title="LU-12345 broken \udcff title",
        )
        self.assertIn("\udcff", app.PATCHES[0]["title"])
        body = self.serve()
        self.assertIn(b"LU-12345 broken", body)
        self.assertIn(b"\\udcff", body)

    def test_respond_encodes_an_unencodable_body_rather_than_raising(self):
        captured = {}

        class Recorder:
            def send_response(self, status):
                captured["status"] = status

            def send_header(self, name, value):
                captured.setdefault("headers", {})[name] = value

            def end_headers(self):
                captured["ended"] = True

            wfile = io.BytesIO()

        recorder = Recorder()
        app.Handler.respond(recorder, "guest co3-\udcffsanity")
        self.assertEqual(captured["status"], 200)
        written = recorder.wfile.getvalue()
        self.assertIn(b"co3-", written)
        self.assertEqual(
            captured["headers"]["Content-Length"], str(len(written))
        )


class ResourceSnapshotStartupGuardTests(unittest.TestCase):
    """`main()` samples resources before binding the socket."""

    def setUp(self):
        self.original = app.collect_resource_snapshot
        self.enabled = app.RESOURCE_COLLECTION_ENABLED
        app.RESOURCE_COLLECTION_ENABLED = True
        app._RESOURCE_SNAPSHOT = None
        app._RESOURCE_SNAPSHOT_MONOTONIC = 0.0

        def restore():
            app.collect_resource_snapshot = self.original
            app.RESOURCE_COLLECTION_ENABLED = self.enabled
            app._RESOURCE_SNAPSHOT = None
            app._RESOURCE_SNAPSHOT_MONOTONIC = 0.0

        self.addCleanup(restore)

    def explode(self, *args, **kwargs):
        raise RuntimeError("ltvm inventory blew up")

    def test_a_failing_collector_degrades_instead_of_raising(self):
        app.collect_resource_snapshot = self.explode
        snapshot = app.refresh_resource_status(force=True)
        self.assertEqual(snapshot["host_memory"]["quality"], "unavailable")
        self.assertEqual(snapshot["ltvm"]["vms"], [])

    def test_the_operator_is_told_what_failed(self):
        app.collect_resource_snapshot = self.explode
        snapshot = app.refresh_resource_status(force=True)
        messages = " ".join(
            str(error.get("message"))
            for error in snapshot["host_memory"]["errors"]
        )
        self.assertIn("ltvm inventory blew up", messages)
        self.assertIn("RuntimeError", messages)

    def test_the_degraded_snapshot_still_renders_the_dashboard(self):
        app.collect_resource_snapshot = self.explode
        rendered = app.resource_dashboard_html()
        self.assertIn("ltvm inventory blew up", rendered)


if __name__ == "__main__":
    unittest.main()
