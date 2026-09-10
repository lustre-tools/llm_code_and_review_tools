import contextlib
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from patch_watcher import run_controller
from patch_watcher.claude_runner import (
    GuidanceDelivery,
    ProcessIdentity,
    ReconciliationProbe,
    RunnerEvent,
    RunnerHandle,
    RunnerSnapshot,
    build_read_only_claude_command,
)
from patch_watcher.ltvm_resources import LTVMInventory
from patch_watcher.run_controller import (
    UNREACHABLE_PROBE_LIMIT,
    RunController,
    RunControllerError,
)
from patch_watcher.session_state import SessionAlreadyExists, SessionStateStore
from patch_watcher.workspace import hash_text

REVISION = "d" * 40


def patch_record(**updates):
    value = {
        "change_number": 68160,
        "project": "fs/lustre-release",
        "patchset": 4,
        "revision_sha": REVISION,
        "revision_ref": "refs/changes/60/68160/4",
        "lifecycle": "Open",
    }
    value.update(updates)
    return value


def read_only_report(**updates):
    value = {
        "schema": "patch-watcher-read-only-report/v1",
        "state": "complete",
        "summary": "Pinned revision inspected.",
        "findings": ["One coverage gap."],
    }
    value.update(updates)
    return value


class FakeRunner:
    """A stand-in for `ClaudeRunner` that can express its real failure shapes.

    Two divergences used to hide production bugs from every test in this
    suite:

    * `probe()` returned `adoptable=self.alive`, so the runner's real "the
      process is alive but its control socket is unreachable" verdict
      (`alive=True, adoptable=False`) could not occur.  The controller
      terminalizes on `adoptable`, and nothing could tell the two apart.
      It now builds the same `ReconciliationProbe` the real runner returns,
      from two independent knobs, `alive` and `control_reachable`.
    * `events()` could be drained forever without complaint, so an ingest
      loop whose cursor never advanced looked like a hang rather than a
      failure.  It now refuses to serve the same non-empty page more than
      `max_identical_pages` times in a row.
    """

    max_identical_pages = 16

    def __init__(self):
        self.starts = []
        self.events_by_session = {}
        self.adoptions = 0
        self.guidance = []
        self.interrupts = []
        self.terminations = []
        self.kills = []
        self.alive = True
        self.control_reachable = True
        self.event_pages = 0
        self._last_page_request = None
        self._identical_pages = 0

    def start(self, spec):
        self.starts.append(spec)
        identity = ProcessIdentity(4242, "host-start", 4242)
        handle = RunnerHandle(
            spec.run_id, spec.session_id,
            str(Path(spec.runtime_dir) / "claude.sock"),
            str(Path(spec.runtime_dir) / "events.jsonl"),
            str(Path(spec.runtime_dir) / "host-state.json"),
            identity,
            ProcessIdentity(4343, "claude-start", 4343),
        )
        return RunnerSnapshot(
            handle, "running", "running", 1_788_000_000.0,
            1_788_000_000.0, 0, "", None,
        )

    def probe(self, handle):
        # The three shapes `ClaudeRunner.probe` actually returns.
        if not self.alive:
            return ReconciliationProbe(
                False, False, False, False, "host_process_missing"
            )
        if not self.control_reachable:
            return ReconciliationProbe(
                True, True, False, False, "control_socket_unreachable"
            )
        return ReconciliationProbe(True, True, True, True, "adoptable")

    def adopt(self, handle):
        self.adoptions += 1
        return RunnerSnapshot(
            handle, "running", "idle", 1_788_000_000.0,
            1_788_000_000.0, 0, "", None,
        )

    def events(self, handle, *, after_cursor=0, limit=100):
        page = [
            event for event in self.events_by_session.get(handle.session_id, [])
            if event.cursor > after_cursor
        ][:limit]
        self.event_pages += 1
        request = (handle.session_id, after_cursor)
        if page:
            # Asking for the same non-empty page again means the consumer did
            # not record the cursor it just read.  In production that spins
            # the controller tick thread forever re-ingesting one page; here
            # it would simply hang the suite, so say what went wrong instead.
            if request == self._last_page_request:
                self._identical_pages += 1
            else:
                self._last_page_request = request
                self._identical_pages = 1
            if self._identical_pages > self.max_identical_pages:
                raise AssertionError(
                    f"runner.events(after_cursor={after_cursor}) served the "
                    f"same non-empty page {self._identical_pages} times: the "
                    "ingest cursor is not advancing"
                )
        return page

    def queue_guidance(self, handle, delivery_id, text):
        self.guidance.append((handle.session_id, delivery_id, text))
        return GuidanceDelivery(delivery_id, "queued", False)

    def interrupt(self, handle):
        self.interrupts.append(handle.session_id)

    def terminate(self, handle):
        self.terminations.append(handle.session_id)

    def kill(self, handle):
        self.kills.append(handle.session_id)


class FakeLTVMAdapter:
    def __init__(self, payload):
        self.payload = payload
        self.cleanup_actions = []
        self.inventory_calls = 0

    def inventory(self):
        self.inventory_calls += 1
        if isinstance(self.payload, BaseException):
            raise self.payload
        return LTVMInventory.from_json(self.payload)

    def cleanup(self, action):
        self.cleanup_actions.append(action)
        self.payload["vms"] = [
            vm for vm in self.payload["vms"] if vm.get("name") != action.name
        ]


class RunControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SessionStateStore(self.root / "sessions.sqlite3")
        self.runner = FakeRunner()
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        # Accumulated host-clock steps. Advancing self.now alone is ordinary
        # elapsed time; step() moves the wall clock WITHOUT moving monotonic
        # time, which is exactly what an NTP correction does and the only
        # thing that distinguishes one from the other.
        self.clock_offset = 0.0
        self.alerts = []
        self.controller = RunController(
            self.store,
            runs_directory=self.root / "runs",
            runner=self.runner,
            checkout=lambda destination, _revision: destination,
            clock=lambda: self.now,
            # Fast-forwarding self.now is elapsed time, not a clock step, so
            # the monotonic source has to move with it. Tests that mean to
            # simulate a real step move one without the other.
            # Inlining to `self.now.timestamp` would bind the datetime
            # object that exists right now; these tests rebind self.now.
            monotonic=lambda: self.now.timestamp() - self.clock_offset,
            alert_sender=self._alert,
            human_notifier=self._notify,
        )
        self.notices = []
        self.notify_outcomes = {"email": (True, "sent"), "gerrit": (True, "change message posted")}

    def tearDown(self):
        self.temporary.cleanup()

    def _alert(self, session, reason, messages, url):
        self.alerts.append((session.session_id, reason, len(messages), url))
        return True

    def _notify(self, session, question, run_url):
        self.notices.append((session.session_id, question.question, run_url))
        if isinstance(self.notify_outcomes, Exception):
            raise self.notify_outcomes
        return dict(self.notify_outcomes)

    def pause_on_question(self, question="Should this include legacy behavior?"):
        session = self.start_run()
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", {
                "schema": "patch-watcher-read-only-report/v1",
                "state": "needs_input",
                "summary": "A choice is required.",
                "findings": [],
                "question": question,
            },
        )]
        self.controller.tick()
        return self.store.get_session(session.session_id)

    def notice_ledger(self, session):
        return {
            item.payload["channel"]: (item.status, item.failure_summary)
            for item in self.store.list_deliveries(session.session_id, kind="human_notice")
        }

    def step_clock(self, delta):
        """Move the host clock without moving real time."""

        self.now += delta
        self.clock_offset += delta.total_seconds()

    def start_run(self):
        session = self.controller.request_investigation(patch_record())
        self.controller.tick()
        return self.store.get_session(session.session_id)

    def test_a_backward_clock_step_does_not_make_a_wedged_run_immortal(self):
        """Stored stamps end up in the future, holding the deadline out of reach.

        record_activity re-anchors last_qualifying_activity_at, but
        active_interval_started_at is a SECOND anchor that set_state writes
        from the wall clock and nothing else ever moves. evaluate_policy takes
        the max() of the two, so after a backward step the state-transition
        stamp won and the inactivity deadline was unreachable for the size of
        the step plus 30 minutes -- a wedged run holding its checkout, its
        guests and its run directory, up to the 48h cap.
        """

        session = self.start_run()
        self.now += timedelta(minutes=10)
        self.controller.tick()
        self.assertEqual(
            self.store.get_session(session.session_id).state, "running"
        )

        self.step_clock(timedelta(hours=-6))
        self.controller.tick()

        # The step itself must not terminate anything: this tick measured
        # across a discontinuity.
        self.assertEqual(
            self.store.get_session(session.session_id).state, "running"
        )
        self.assertIn(
            "clock_step_detected",
            [
                event.event_type
                for event in self.store.list_events(session.session_id)
            ],
        )

        # Real time now passes, with the clock behaving. The wedged run must
        # time out on the ordinary 30 minute limit, not six hours later.
        self.now += timedelta(minutes=31)
        self.controller.tick()

        terminal = self.store.get_terminal_result(session.session_id)
        self.assertIsNotNone(terminal, "a wedged run survived a backward step")
        self.assertEqual(terminal.failure_code, "agent_inactivity_timeout")

    def test_a_forward_clock_step_does_not_kill_a_healthy_run(self):
        """45 minutes of wall time, one second of real time, run still working.

        Every deadline here is a wall-clock difference, and there was no
        tolerance at all for a forward move of `now`. A single NTP correction
        larger than the inactivity limit terminated every engineering run
        mid-build, one tick after the run was demonstrably healthy.
        """

        session = self.start_run()
        self.step_clock(timedelta(minutes=45))
        self.controller.tick()

        self.assertEqual(
            self.store.get_session(session.session_id).state, "running"
        )
        self.assertIsNone(self.store.get_terminal_result(session.session_id))

        # And the run is still governed afterwards -- the step buys one tick
        # of amnesty, not immunity.
        self.now += timedelta(minutes=31)
        self.controller.tick()
        self.assertEqual(
            self.store.get_terminal_result(session.session_id).failure_code,
            "agent_inactivity_timeout",
        )

    def test_an_ordinary_slow_tick_is_not_mistaken_for_a_clock_step(self):
        """Wall and monotonic move together, so nothing is re-anchored."""

        session = self.start_run()
        self.now += timedelta(minutes=10)
        self.controller.tick()
        self.assertNotIn(
            "clock_step_detected",
            [
                event.event_type
                for event in self.store.list_events(session.session_id)
            ],
        )

    def test_request_requires_exact_open_revision_and_reserves_patch_once(self):
        with self.assertRaisesRegex(RunControllerError, "refresh"):
            self.controller.request_investigation(patch_record(revision_sha=""))
        first = self.controller.request_investigation(patch_record())
        with self.assertRaises(SessionAlreadyExists):
            self.controller.request_investigation(patch_record())
        self.assertEqual(first.revision, REVISION)
        self.assertEqual(first.patchset, 4)

    def test_dispatch_records_instructions_and_starts_read_only_runner(self):
        session = self.start_run()
        self.assertEqual(session.state, "running")
        self.assertEqual(len(self.runner.starts), 1)
        spec = self.runner.starts[0]
        self.assertEqual(spec.session_id, session.session_id)
        self.assertEqual(spec.capability_profile, "read_only")
        self.assertEqual(spec.report_kind, "read_only")
        self.assertEqual(Path(spec.cwd).stat().st_mode & 0o777, 0o500)
        command = build_read_only_claude_command(spec)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("Bash", command)
        instructions_path = (
            self.root / "runs" / session.run_id / "work" / "input" / "INSTRUCTIONS.md"
        )
        instructions = instructions_path.read_text(encoding="utf-8")
        self.assertEqual(instructions_path.stat().st_mode & 0o777, 0o400)
        self.assertIn("Do not modify files, run commands", instructions)
        recorded = next(
            event for event in self.store.list_events(session.session_id)
            if event.event_type == "run_instructions"
        )
        self.assertEqual(recorded.payload["instructions_hash"], hash_text(instructions))

    def test_controller_restart_adopts_persisted_host_identity(self):
        session = self.start_run()
        replacement = RunController(
            self.store,
            runs_directory=self.root / "runs",
            runner=self.runner,
            checkout=lambda destination, _revision: destination,
            clock=lambda: self.now,
            # Fast-forwarding self.now is elapsed time, not a clock step, so
            # the monotonic source has to move with it. Tests that mean to
            # simulate a real step move one without the other.
            # Inlining to `self.now.timestamp` would bind the datetime
            # object that exists right now; these tests rebind self.now.
            monotonic=lambda: self.now.timestamp(),  # noqa: PLW0108
        )
        replacement.tick()
        transport = self.store.get_runner_transport(session.session_id)
        self.assertEqual(transport.adoption_state, "adopted")
        self.assertEqual(self.runner.adoptions, 1)

    def test_new_patchset_stales_run_and_supervisor_stops_its_runner(self):
        session = self.start_run()
        changed = patch_record(
            patchset=5,
            revision_sha="e" * 40,
            revision_ref="refs/changes/60/68160/5",
        )
        stale = self.controller.reconcile_patch_revision(changed)
        self.assertEqual(stale, [session.run_id])
        self.assertEqual(self.store.get_session(session.session_id).state, "stale")
        self.controller.tick()
        self.assertEqual(self.runner.terminations, [session.session_id])

    def test_valid_report_finishes_and_invalid_report_fails(self):
        complete = self.start_run()
        self.runner.events_by_session[complete.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", {
                "schema": "patch-watcher-read-only-report/v1",
                "state": "complete",
                "summary": "Pinned revision inspected.",
                "findings": ["One coverage gap."],
            },
        )]
        self.controller.tick()
        self.assertEqual(self.store.get_session(complete.session_id).state, "succeeded")

        other = self.controller.request_investigation(patch_record(change_number=68161, revision_ref="refs/changes/61/68161/4"))
        self.controller.tick()
        self.runner.events_by_session[other.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report_invalid", {"reason": "missing"},
        )]
        self.controller.tick()
        self.assertEqual(self.store.get_session(other.session_id).state, "failed")
        self.assertEqual(
            self.store.get_terminal_result(other.session_id).failure_code,
            "worker_report_invalid",
        )

    def test_a_paused_run_tells_the_human_once_per_channel(self):
        """needs_input must reach someone who is not watching the console.

        The open question is the in-console label by itself; email and a
        Gerrit change message are the channels that reach out. Each gets one
        ledger entry keyed on the question, so nothing is sent twice."""

        session = self.pause_on_question("Drop the legacy fallback?")
        self.assertEqual(session.state, "waiting_human")
        self.assertEqual(len(self.notices), 1)
        session_id, question, url = self.notices[0]
        self.assertEqual(session_id, session.session_id)
        self.assertEqual(question, "Drop the legacy fallback?")
        self.assertEqual(url, f"http://127.0.0.1:8080/runs/{session.run_id}")
        self.assertEqual(
            self.notice_ledger(session),
            {"email": ("delivered", None), "gerrit": ("delivered", None)},
        )
        summaries = [
            event.payload.get("summary") for event in self.store.list_events(session.session_id)
            if event.event_type == "human_notice"
        ]
        self.assertEqual(summaries, ["Asked the human: email sent, gerrit sent"])
        # Another controller pass changes nothing: the ledger already answers.
        self.controller.tick()
        self.assertEqual(len(self.notices), 1)

    def test_a_failed_channel_is_recorded_and_never_retried_into_a_duplicate(self):
        self.notify_outcomes = {"email": (False, "Email is disabled; the notice was recorded only."),
                                "gerrit": (False, "Gerrit rejected the configured credentials.")}
        session = self.pause_on_question()
        self.assertEqual(
            self.notice_ledger(session),
            {
                "email": ("failed", "Email is disabled; the notice was recorded only."),
                "gerrit": ("failed", "Gerrit rejected the configured credentials."),
            },
        )
        self.controller.tick()
        self.assertEqual(len(self.notices), 1)
        # The run itself is untouched: still waiting, still answerable.
        self.assertEqual(self.store.get_session(session.session_id).state, "waiting_human")

    def test_a_notifier_bug_cannot_take_the_run_down(self):
        self.notify_outcomes = RuntimeError("sendmail exploded")
        session = self.pause_on_question()
        self.assertEqual(session.state, "waiting_human")
        ledger = self.notice_ledger(session)
        self.assertEqual({status for status, _ in ledger.values()}, {"failed"})
        self.assertIn("notifier raised RuntimeError: sendmail exploded", ledger["email"][1])

    def test_without_a_notifier_the_channels_are_recorded_as_not_sent(self):
        self.controller.human_notifier = None
        session = self.pause_on_question()
        self.assertEqual(session.state, "waiting_human")
        self.assertEqual(
            self.notice_ledger(session),
            {"email": ("failed", "no notifier configured"),
             "gerrit": ("failed", "no notifier configured")},
        )

    def test_waiting_question_answer_is_delivered_exactly_once(self):
        session = self.start_run()
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", {
                "schema": "patch-watcher-read-only-report/v1",
                "state": "needs_input",
                "summary": "A choice is required.",
                "findings": [],
                "question": "Should this include legacy behavior?",
            },
        )]
        self.controller.tick()
        question = self.store.list_human_questions(session.session_id)[0]
        self.assertEqual(self.store.get_session(session.session_id).state, "waiting_human")
        self.store.answer_human_question(
            session.session_id,
            question.question_id,
            answered_by="operator",
            answer="Yes, preserve it.",
            at=self.now,
        )
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(len(self.runner.guidance), 1)
        self.assertEqual(self.runner.guidance[0][2], "Yes, preserve it.")

    def test_inactivity_timeout_terminates_and_alerts_exactly_once(self):
        session = self.start_run()
        self.now += timedelta(minutes=31)
        self.controller.tick()
        self.controller.tick()
        terminal = self.store.get_terminal_result(session.session_id)
        self.assertEqual(terminal.failure_code, "agent_inactivity_timeout")
        self.assertEqual(self.runner.terminations.count(session.session_id), 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("/confirm?intent=kill", self.alerts[0][3])

    def test_one_failed_tick_never_permanently_stops_dispatch(self):
        """A transient store error must cost one tick, not the controller.

        `tick()` guarded only the INSIDE of the per-session loop.
        `_reconcile_ltvm_resources()` and `list_sessions()` run outside it, and
        both touch SQLite databases a second process can hold open, so one
        `database is locked` escaped and killed the supervisor thread for good:
        the web app kept serving, queued runs sat at `queued` forever, no
        terminal session was ever cleaned, no checkout was ever released, and
        nothing was written anywhere an operator would look.
        """

        session = self.controller.request_investigation(patch_record())
        real_list_sessions = self.store.list_sessions
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return real_list_sessions(**kwargs)

        self.store.list_sessions = flaky

        self.controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "queued")
        # Not swallowed: the failure is durably on record where an operator
        # can see it, with the scope that says it was not any one run's fault.
        self.assertEqual(
            [
                (item["scope"], item["error_type"], item["summary"], item["count"])
                for item in self.controller.controller_failures()
            ],
            [("tick", "OperationalError", "database is locked", 1)],
        )

        # A fault that recurs every tick must stay one counted row, not flood.
        self.controller.tick()
        self.assertEqual(
            [(item["scope"], item["count"]) for item in self.controller.controller_failures()],
            [("tick", 2)],
        )

        # And the next healthy tick dispatches: one failure is not permanent.
        self.controller.tick()
        self.assertEqual(self.store.get_session(session.session_id).state, "running")

    def test_the_supervisor_thread_outlives_a_tick_that_raises(self):
        """The thread that dispatches every run may not exit on an exception."""

        ticks = []
        started = threading.Event()

        def exploding_tick():
            ticks.append(1)
            started.set()
            raise sqlite3.OperationalError("database is locked")

        self.controller.tick = exploding_tick
        self.controller.poll_seconds = 0.001
        self.controller.start()
        try:
            deadline = time.monotonic() + 5.0
            while len(ticks) < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
        finally:
            self.controller.stop()

        self.assertGreaterEqual(
            len(ticks), 3, "the supervisor thread died on its first failure"
        )
        self.assertEqual(
            [item["scope"] for item in self.controller.controller_failures()],
            ["supervise"],
        )

    def test_ltvm_reconciliation_registers_and_cleans_only_exact_owner(self):
        session = self.controller.request_engineering(patch_record())
        self.store.set_state(session.session_id, "running", changed_at=self.now)
        owner = "patch-watcher:" + session.session_id
        adapter = FakeLTVMAdapter({
            "vms": [
                {"name": "owned-vm", "owner_id": owner, "status": "running", "mem": 2048},
                {
                    "name": "other-vm",
                    "owner_id": "patch-watcher:another-session",
                    "status": "running",
                    "mem": 4096,
                },
                {"name": "legacy-vm", "owner_id": None, "status": "stopped", "mem": 1024},
            ]
        })
        self.controller.ltvm_adapter = adapter

        self.controller._reconcile_ltvm_resources()
        resources = self.store.list_owned_resources(session_id=session.session_id)
        self.assertEqual(
            [(item.resource_type, item.external_id, item.owner_id) for item in resources],
            [("ltvm_vm", "owned-vm", owner)],
        )

        self.store.finish_session(
            session.session_id,
            "failed",
            failure_code="test_failure",
            failure_summary="test",
            finished_at=self.now,
        )
        self.controller._reconcile_ltvm_resources()
        self.assertEqual(
            [(item.resource_type, item.name, item.owner_id) for item in adapter.cleanup_actions],
            [("vm", "owned-vm", owner)],
        )
        cleaned = self.store.list_owned_resources(session_id=session.session_id)[0]
        self.assertEqual(cleaned.state, "cleaned")
        self.assertEqual(
            {vm["name"] for vm in adapter.payload["vms"]},
            {"other-vm", "legacy-vm"},
        )

    def test_ltvm_cleanup_refuses_record_when_inventory_owner_becomes_ambiguous(self):
        session = self.controller.request_engineering(patch_record())
        self.store.set_state(session.session_id, "running", changed_at=self.now)
        owner = "patch-watcher:" + session.session_id
        adapter = FakeLTVMAdapter({
            "vms": [
                {"name": "owned-vm", "owner_id": owner, "status": "running", "mem": 2048}
            ]
        })
        self.controller.ltvm_adapter = adapter
        self.controller._reconcile_ltvm_resources()
        self.store.finish_session(
            session.session_id,
            "failed",
            failure_code="test_failure",
            failure_summary="test",
            finished_at=self.now,
        )
        adapter.payload = {
            "vms": [
                {"name": "owned-vm", "owner_id": None, "status": "running", "mem": 2048}
            ]
        }

        self.controller._reconcile_ltvm_resources()

        self.assertEqual(adapter.cleanup_actions, [])
        resource = self.store.list_owned_resources(session_id=session.session_id)[0]
        self.assertEqual(resource.state, "cleanup_failed")
        self.assertIn("ownership", resource.cleanup_failure)

    def test_ltvm_inventory_failure_retains_cleanup_pending(self):
        session = self.controller.request_engineering(patch_record())
        self.store.set_state(session.session_id, "running", changed_at=self.now)
        owner = "patch-watcher:" + session.session_id
        adapter = FakeLTVMAdapter({
            "vms": [
                {"name": "owned-vm", "owner_id": owner, "status": "running", "mem": 2048}
            ]
        })
        self.controller.ltvm_adapter = adapter
        self.controller._reconcile_ltvm_resources()
        self.store.finish_session(
            session.session_id,
            "failed",
            failure_code="test_failure",
            failure_summary="test",
            finished_at=self.now,
        )
        adapter.payload = RuntimeError("ltvm unavailable")

        self.controller._reconcile_ltvm_resources()
        self.controller._cleanup_session(
            self.store.get_session(session.session_id)
        )

        resource = self.store.list_owned_resources(session_id=session.session_id)[0]
        self.assertEqual(resource.state, "cleanup_pending")
        self.assertEqual(adapter.cleanup_actions, [])

    def test_ltvm_cleanup_waits_until_terminal_worker_is_confirmed_dead(self):
        session = self.controller.request_engineering(patch_record())
        self.store.set_state(session.session_id, "running", changed_at=self.now)
        snapshot = self.runner.start(SimpleNamespace(
            run_id=session.run_id,
            session_id=session.session_id,
            runtime_dir=str(self.root / "fake-runtime"),
        ))
        self.controller._persist_handle(session, snapshot)
        owner = "patch-watcher:" + session.session_id
        adapter = FakeLTVMAdapter({
            "vms": [
                {"name": "owned-vm", "owner_id": owner, "status": "running", "mem": 2048}
            ]
        })
        self.controller.ltvm_adapter = adapter
        self.controller._reconcile_ltvm_resources()
        self.store.finish_session(
            session.session_id,
            "failed",
            failure_code="test_failure",
            failure_summary="test",
            finished_at=self.now,
        )

        self.controller._reconcile_ltvm_resources()
        self.assertEqual(adapter.cleanup_actions, [])

        self.runner.alive = False
        self.controller._reconcile_ltvm_resources()
        self.assertEqual([action.name for action in adapter.cleanup_actions], ["owned-vm"])

    def _terminal_session_with_a_live_worker(self):
        """A terminal run whose worker keeps reporting itself alive."""

        session = self.controller.request_engineering(patch_record())
        self.store.set_state(session.session_id, "running", changed_at=self.now)
        snapshot = self.runner.start(SimpleNamespace(
            run_id=session.run_id,
            session_id=session.session_id,
            runtime_dir=str(self.root / "fake-runtime"),
        ))
        self.controller._persist_handle(session, snapshot)
        self.store.finish_session(
            session.session_id,
            "cancelled",
            result={"reason": "operator_cancelled"},
            finished_at=self.now,
        )
        return session

    def test_a_worker_that_ignores_stop_is_escalated_to_kill_then_abandoned(self):
        """TERM once and wait forever is not a cleanup path.

        The fake runner never dies, which is the host-wrapper-ignores-stop
        case.  Cleanup has to climb to `kill`, stop signalling after a bounded
        number of attempts, and leave the give-up somewhere an operator sees.
        """

        session = self._terminal_session_with_a_live_worker()

        for _tick in range(50):
            self.controller.tick()

        self.assertEqual(self.runner.terminations, [session.session_id])
        # Escalated at all, and then bounded rather than once per tick.
        self.assertTrue(self.runner.kills)
        self.assertLess(len(self.runner.kills), 50)
        self.assertEqual(
            self.runner.kills,
            [session.session_id] * (
                run_controller.RUNNER_STOP_ATTEMPT_LIMIT
                - run_controller.RUNNER_STOP_TERM_ATTEMPTS
            ),
        )
        events = [
            event.event_type
            for event in self.store.list_events(session.session_id)
        ]
        self.assertIn("runner_stop_abandoned", events)
        self.assertEqual(
            events.count("runner_stop_attempt"),
            run_controller.RUNNER_STOP_ATTEMPT_LIMIT,
        )
        self.assertIn(
            (session.session_id, "runner_stop_abandoned"),
            [(alert[0], alert[1]) for alert in self.alerts],
        )

    def test_a_terminate_that_raises_does_not_block_escalation(self):
        """A dead control socket must not freeze the ladder at its first rung.

        `ClaudeRunner.terminate` raises before the stop is recorded, so nothing
        marks the attempt as spent; counting only successful signals would
        retry TERM forever and never reach `kill`.
        """

        session = self._terminal_session_with_a_live_worker()

        def raising_terminate(handle):
            self.runner.terminations.append(handle.session_id)
            raise OSError("control socket is gone")

        self.runner.terminate = raising_terminate

        for _tick in range(50):
            self.controller.tick()

        self.assertTrue(self.runner.kills)
        attempts = [
            event
            for event in self.store.list_events(session.session_id)
            if event.event_type == "runner_stop_attempt"
        ]
        self.assertEqual(
            len(attempts), run_controller.RUNNER_STOP_ATTEMPT_LIMIT
        )
        self.assertEqual(attempts[0].payload["failure_type"], "OSError")
        self.assertIn(
            "runner_stop_abandoned",
            [
                event.event_type
                for event in self.store.list_events(session.session_id)
            ],
        )

    # ------------------------------------------------------------------
    # Operator stop controls.  Every one of these used to be checked only
    # by reading the intent row back out of the store, so `_execute_controls`
    # -- the single place that turns a recorded intent into a signal the
    # runner receives -- could have been replaced by `return` and the whole
    # suite would still have passed.  The dashboard would show "confirmed"
    # forever while the runaway agent kept going.
    # ------------------------------------------------------------------

    def _control_status(self, session, intent):
        return {
            item.request_id: item.status
            for item in self.store.list_control_intents(session.session_id)
        }[intent.request_id]

    def test_a_requested_pause_stops_the_controller_driving_the_runner(self):
        """Pause has to reach the worker, not just the intent table.

        Pause is the one control with no signal of its own: what makes it
        real is that the paused run stops being fed. Queued guidance
        delivered to a "paused" run is a pause that did nothing.
        """

        session = self.start_run()
        self.store.enqueue_guidance(
            session.session_id,
            "keep going",
            idempotency_key="guidance-1",
            at=self.now,
        )
        intent = self.store.request_pause(
            session.session_id, "operator", requested_at=self.now
        )

        self.controller.tick()

        self.assertEqual(
            self.runner.guidance, [],
            "a paused run was still being fed guidance",
        )
        self.assertEqual(self.store.get_session(session.session_id).state, "paused")
        self.assertEqual(self._control_status(session, intent), "executed")

    def test_a_requested_interrupt_reaches_the_runner(self):
        """The operator's "stop what you are doing" must arrive.

        Asserting only that the intent row reached its terminal status tests
        the store, not the caller: the interrupt never has to be delivered.
        """

        session = self.start_run()
        intent = self.store.request_interrupt(
            session.session_id, "operator", requested_at=self.now
        )

        self.controller.tick()

        self.assertEqual(self.runner.interrupts, [session.session_id])
        self.assertEqual(self._control_status(session, intent), "executed")

    def test_a_confirmed_cancel_terminates_the_runner_and_ends_the_run(self):
        """Cancel is TERM plus a terminal session, in that order."""

        session = self.start_run()
        intent, token = self.store.request_destructive_control(
            session.session_id, "cancel", "operator", requested_at=self.now
        )
        self.store.confirm_control_with_token(
            session.session_id,
            intent.request_id,
            token,
            "operator",
            confirmed_at=self.now,
        )

        self.controller.tick()

        self.assertEqual(self.runner.terminations, [session.session_id])
        self.assertEqual(self.runner.kills, [])
        self.assertEqual(
            self.store.get_session(session.session_id).state, "cancelled"
        )
        self.assertEqual(self._control_status(session, intent), "executed")

    def test_a_confirmed_kill_force_stops_the_runner_and_ends_the_run(self):
        """Kill is the escalated form: KILL, not TERM."""

        session = self.start_run()
        intent, token = self.store.request_destructive_control(
            session.session_id, "kill", "operator", requested_at=self.now
        )
        self.store.confirm_control_with_token(
            session.session_id,
            intent.request_id,
            token,
            "operator",
            confirmed_at=self.now,
        )

        self.controller.tick()

        self.assertEqual(self.runner.kills, [session.session_id])
        self.assertEqual(self.runner.terminations, [])
        self.assertEqual(
            self.store.get_session(session.session_id).state, "cancelled"
        )
        self.assertEqual(self._control_status(session, intent), "executed")

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def test_every_event_of_a_busy_run_is_ingested_exactly_once(self):
        """The paged drain has to finish the stream, not just its first page.

        No test ever fed more than a page of events, so the `while True`
        drain always exited on its first pass and its page bound, its break
        condition and its cursor advance were all unobserved. A cursor that
        fails to advance spins the tick thread forever re-ingesting one page;
        a mismatched page bound silently strands the rest. An almost identical
        bug -- a bounded buffer that kept the newest N while the consumer
        advanced past everything -- lost 400 of 500 events here recently, and
        the fake hid it by being more correct than the code it stood in for.
        """

        session = self.start_run()
        total = 250
        self.runner.events_by_session[session.session_id] = [
            RunnerEvent(cursor, self.now.timestamp(), "claude_event", {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "auster -s sanity"}},
                ]},
            })
            for cursor in range(1, total + 1)
        ]

        self.controller.tick()

        stored = [
            event.payload["runner_cursor"]
            for event in self.store.list_events(
                session.session_id, event_types=("runner_event",)
            )
        ]
        self.assertEqual(stored, list(range(1, total + 1)))
        self.assertGreater(
            self.runner.event_pages, 2,
            "the drain never came back for a second page",
        )
        self.assertEqual(self.store.get_session(session.session_id).state, "running")

    def test_one_unstorable_event_does_not_wedge_the_rest_of_the_stream(self):
        """A payload the store refuses must cost that event and nothing more.

        The ValueError used to propagate, so ingestion could never advance
        past the bad cursor -- and a COMPLETED run whose report sat further
        along the stream was discarded on every retry, forever.
        """

        session = self.start_run()
        self.runner.events_by_session[session.session_id] = [
            RunnerEvent(1, self.now.timestamp(), "claude_event", {
                "type": "assistant",
                "message": {"content": [
                    # Far past the store's per-payload byte limit.
                    {"type": "tool_result", "content": "x" * (400 * 1024)},
                ]},
            }),
            RunnerEvent(
                2, self.now.timestamp(), "worker_report", read_only_report()
            ),
        ]

        self.controller.tick()

        self.assertEqual(
            self.store.get_session(session.session_id).state, "succeeded",
            "the completed report behind the unstorable event was lost",
        )
        dropped = [
            event.payload
            for event in self.store.list_events(
                session.session_id, event_types=("runner_event",)
            )
            if (event.payload.get("runner_payload") or {}).get("dropped")
        ]
        self.assertEqual([item["runner_cursor"] for item in dropped], [1])
        self.assertIn("over the", dropped[0]["runner_payload"]["reason"])

    def test_a_silent_tool_use_event_keeps_the_inactivity_clock_alive(self):
        """A long tool call is a working agent, not an idle one.

        The inactivity clock used to advance only on assistant *text*, so a
        run inside one `ltvm build lustre` or `auster` call -- which emits a
        tool_use block and then nothing for far longer than the 30 minute
        limit -- was terminated mid-build and its VMs destroyed. The guard
        that replaced this one read the controller's own source and asserted
        `record_activity` appeared before `record_message`, which stays true
        if the call is moved inside the text-only branch.
        """

        session = self.start_run()
        self.now += timedelta(minutes=20)
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "claude_event", {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "ltvm build lustre rocky9"}},
                ]},
            },
        )]
        self.controller.tick()

        # 45 minutes into the run, but only 25 since the agent last proved it
        # was alive -- inside the 30 minute inactivity limit.
        self.now += timedelta(minutes=25)
        self.controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "running")
        self.assertIsNone(self.store.get_terminal_result(session.session_id))
        self.assertEqual(self.runner.terminations, [])
        # Proof of life is not something a human reads: no text, no message.
        self.assertEqual(self.store.recent_messages(session.session_id), [])

    # ------------------------------------------------------------------
    # Crash-recovery bookkeeping for terminal reports
    # ------------------------------------------------------------------

    def _record_worker_report_event(self, session, cursor, report):
        """Put the durable `runner_event` a crash would have left behind."""

        self.store.append_event(
            session.session_id,
            "runner_event",
            {
                "runner_cursor": cursor,
                "runner_type": "worker_report",
                "runner_payload": report,
            },
            idempotency_key=f"runner-event:{session.run_id}:{cursor}",
            at=self.now,
        )

    def test_a_report_that_failed_to_apply_is_not_offered_for_recovery_again(self):
        """A report that has had its turn must not be retried every tick.

        The marker is written in the `except Exception:` arm precisely so a
        failed apply is spent rather than repeated, and the recovery scan has
        to read those markers back. Drop either half and a report that failed
        once is re-applied on every tick, forever.
        """

        session = self.start_run()
        self._record_worker_report_event(session, 1, read_only_report())
        managed = self.store.get_session(session.session_id)
        self.assertEqual(self.controller._unapplied_worker_report(managed)[0], 1)

        def exploding_apply(_session, _handle, _value):
            raise RuntimeError("evidence capture failed")

        self.controller._apply_report = exploding_apply

        with self.assertRaises(RuntimeError):
            self.controller._apply_report_once(
                managed, None, read_only_report(), 1
            )

        self.assertIsNone(
            self.controller._unapplied_worker_report(managed),
            "a report that already had its turn was offered for recovery again",
        )

    def test_a_failed_report_is_applied_once_however_often_it_is_supervised(self):
        """The same fact, driven through the real supervision path."""

        session = self.start_run()
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", read_only_report(),
        )]
        attempts = []

        def exploding_apply(_session, _handle, value):
            attempts.append(value)
            raise RuntimeError("evidence capture failed")

        self.controller._apply_report = exploding_apply

        for _pass in range(5):
            # Straight at `_supervise_session`: `tick` would terminalize the
            # session on the first failure and never supervise it again, which
            # hides the retry loop rather than fixing it.
            with contextlib.suppress(RuntimeError):
                self.controller._supervise_session(
                    self.store.get_session(session.session_id)
                )

        self.assertEqual(
            len(attempts), 1,
            "the report was re-applied on every supervision pass",
        )
        self.assertEqual(
            [
                event.payload["runner_cursor"]
                for event in self.store.list_events(
                    session.session_id,
                    event_types=(run_controller.WORKER_REPORT_APPLIED_EVENT,),
                )
            ],
            [1],
        )

    def test_a_crash_mid_apply_leaves_the_report_recoverable(self):
        """A dying process is the one failure that must NOT spend the report.

        `_ingest_runner_events` records the validated report before
        `_apply_report` stops the worker, captures the diff and finishes the
        session -- a window of about ten seconds. A crash inside it left the
        report on the record with nothing that ever read it back, and the
        agent's uncommitted work was destroyed by the next run's
        `git reset --hard` on that checkout.
        """

        session = self.start_run()
        self.runner.events_by_session[session.session_id] = [RunnerEvent(
            1, self.now.timestamp(), "worker_report", read_only_report(),
        )]
        applied = []
        real_apply = self.controller._apply_report

        def crashing_apply(managed, handle, value):
            applied.append(value)
            if len(applied) == 1:
                raise KeyboardInterrupt  # the process going away mid-apply
            return real_apply(managed, handle, value)

        self.controller._apply_report = crashing_apply
        with self.assertRaises(KeyboardInterrupt):
            self.controller.tick()

        # What a restart finds: the report is durable and still unapplied.
        self.controller.tick()

        self.assertEqual(len(applied), 2)
        self.assertEqual(
            self.store.get_session(session.session_id).state, "succeeded"
        )

    # ------------------------------------------------------------------
    # Runner liveness
    # ------------------------------------------------------------------

    def test_a_live_worker_with_an_unreachable_socket_is_a_lost_runner(self):
        """`alive` and `adoptable` are different verdicts.

        The real probe returns `alive=True, adoptable=False,
        reason="control_socket_unreachable"` for a process that is running but
        whose control socket cannot be reached -- a run the controller can no
        longer drive. The fake tied `adoptable` to `alive`, so that state
        could not occur and nothing distinguished the two: gating on `alive`
        here would leave an undrivable run supervised forever.
        """

        session = self.start_run()
        self.runner.control_reachable = False
        self.assertTrue(self.runner.probe(object()).alive)

        # A dead process is a verdict; an unreachable socket on a LIVE process
        # is evidence. Declaring the run lost on the first such probe threw
        # away a run whose agent was still working -- and left it working,
        # because the Claude process and its VMs outlive the session row that
        # was supposed to own them.
        for _tick in range(UNREACHABLE_PROBE_LIMIT - 1):
            self.controller.tick()
            self.assertEqual(
                self.store.get_session(session.session_id).state, "running"
            )

        self.controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "failed")
        terminal = self.store.get_terminal_result(session.session_id)
        self.assertEqual(terminal.failure_code, "runner_lost")
        self.assertEqual(terminal.failure_summary, "control_socket_unreachable")
        self.assertIn(
            (session.session_id, "runner_lost"),
            [(alert[0], alert[1]) for alert in self.alerts],
        )

    def test_a_socket_blip_that_recovers_does_not_lose_the_run(self):
        """The tolerance has to actually reset, or it only delays the loss."""

        session = self.start_run()
        for _blip in range(UNREACHABLE_PROBE_LIMIT * 3):
            self.runner.control_reachable = False
            self.controller.tick()
            self.runner.control_reachable = True
            self.controller.tick()
            self.assertEqual(
                self.store.get_session(session.session_id).state, "running"
            )
        self.assertIsNone(self.store.get_terminal_result(session.session_id))

    def test_a_dead_worker_is_lost_immediately_without_waiting(self):
        """Tolerance applies to an unreachable socket, never to a dead process."""

        session = self.start_run()
        self.runner.alive = False

        self.controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "failed")
        self.assertEqual(
            self.store.get_terminal_result(session.session_id).failure_code,
            "runner_lost",
        )

    def test_a_healthy_probe_leaves_the_run_supervised(self):
        """The companion: a reachable worker is never declared lost."""

        session = self.start_run()

        self.controller.tick()

        self.assertEqual(self.store.get_session(session.session_id).state, "running")
        self.assertIsNone(self.store.get_terminal_result(session.session_id))


if __name__ == "__main__":
    unittest.main()


class InactivityLivenessTests(unittest.TestCase):
    """A long-running tool call must not read as an inactive agent.

    The inactivity clock used to advance only on assistant *text*. An agent
    inside one `ltvm build lustre` or `auster` call emits a tool_use block and
    then nothing for well over the 30-minute limit, so a healthy run was
    terminated mid-build and its VMs destroyed.
    """

    def test_a_tool_use_event_counts_as_activity(self):
        self.assertEqual(run_controller._assistant_text({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash",
                                     "input": {"command": "ltvm build lustre"}}]},
        }), "")

    def test_only_text_blocks_become_human_readable_messages(self):
        text = run_controller._assistant_text({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": "building now"},
            ]},
        })
        self.assertEqual(text, "building now")

    # The end-to-end guard lives in `RunControllerTests` as
    # `test_a_silent_tool_use_event_keeps_the_inactivity_clock_alive`: it
    # ingests a real tool_use-only event and checks the run survives past the
    # 30-minute limit.  It replaced a test that read
    # `inspect.getsource(RunController)` and asserted `record_activity`
    # appeared before `record_message` -- source order that stays true if the
    # call is moved inside the `if text:` branch, which is precisely the
    # regression it was meant to catch.


class StopRunnerOnceTests(unittest.TestCase):
    """"Once" must mean one attempt, not one success.

    The stop event was appended only after a successful signal, so a terminate
    that always raises (a dead control socket) never recorded anything and every
    later call re-signalled and re-raised forever, never advancing.
    """

    def controller(self, runner):
        events = []

        class Store:
            """The subset of `SessionStateStore` `_stop_runner_once` uses.

            It mirrors the real signatures on purpose: a fake that accepts
            `event_types` and ignores it would let a caller scope a read to
            the wrong types and still pass, which is the same class of bug
            as a fake that cannot express a real failure at all.
            """

            def list_events(self, session_id, *, after_event_id=0, event_types=None):
                if after_event_id < 0:
                    raise ValueError("after_event_id must not be negative")
                selected = [
                    event for event in events if event.event_id > after_event_id
                ]
                if event_types is not None:
                    wanted = {str(item) for item in event_types}
                    selected = [
                        event for event in selected if event.event_type in wanted
                    ]
                return selected

            def append_event(
                self, session_id, event_type, payload=None, *,
                idempotency_key=None, at=None,
            ):
                if idempotency_key is not None:
                    for existing in events:
                        if existing.idempotency_key == idempotency_key:
                            return existing
                event = SimpleNamespace(
                    event_id=len(events) + 1,
                    event_type=event_type,
                    payload=payload or {},
                    idempotency_key=idempotency_key,
                )
                events.append(event)
                return event

        return SimpleNamespace(
            runner=runner, store=Store(), clock=lambda: None,
        ), events

    def test_a_raising_terminate_is_still_recorded_as_attempted(self):
        class Exploding:
            calls = 0

            def terminate(self, handle):
                Exploding.calls += 1
                raise OSError("control socket is gone")

        controller, events = self.controller(Exploding())
        session = SimpleNamespace(session_id="s1", run_id="run-1")

        with self.assertRaises(OSError):
            RunController._stop_runner_once(controller, session, object())
        self.assertEqual([e.event_type for e in events], ["runner-stop"])
        self.assertFalse(events[0].payload["signalled"])

        # The second call must NOT re-signal: the attempt is on record.
        RunController._stop_runner_once(controller, session, object())
        self.assertEqual(Exploding.calls, 1, "a recorded attempt was repeated")

    def test_a_successful_stop_records_that_it_signalled(self):
        class Quiet:
            def terminate(self, handle):
                pass

        controller, events = self.controller(Quiet())
        session = SimpleNamespace(session_id="s1", run_id="run-1")
        RunController._stop_runner_once(controller, session, object())
        self.assertTrue(events[0].payload["signalled"])
