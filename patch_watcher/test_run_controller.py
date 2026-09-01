import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from claude_runner import (
    ProcessIdentity,
    RunnerEvent,
    RunnerHandle,
    RunnerSnapshot,
)
from ltvm_resources import LTVMInventory
from run_controller import RunController, RunControllerError
from session_state import SessionAlreadyExists, SessionStateStore
from worker_contract import load_profile


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


class FakeRunner:
    def __init__(self):
        self.starts = []
        self.events_by_session = {}
        self.adoptions = 0
        self.guidance = []
        self.interrupts = []
        self.terminations = []
        self.kills = []
        self.alive = True

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
        return SimpleNamespace(
            alive=self.alive,
            adoptable=self.alive,
            reason="ok" if self.alive else "host_process_missing",
        )

    def adopt(self, handle):
        self.adoptions += 1
        return RunnerSnapshot(
            handle, "running", "idle", 1_788_000_000.0,
            1_788_000_000.0, 0, "", None,
        )

    def events(self, handle, *, after_cursor=0, limit=100):
        return [
            event for event in self.events_by_session.get(handle.session_id, [])
            if event.cursor > after_cursor
        ][:limit]

    def queue_guidance(self, handle, delivery_id, text):
        self.guidance.append((handle.session_id, delivery_id, text))
        return SimpleNamespace(state="queued", duplicate=False)

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


def ready_doctor(profile, envelope, **_kwargs):
    return {
        "status": "ready",
        "failure_codes": [],
        "worker_host": {"host_id": "test-worker"},
        "isolation_mode": "host_unsandboxed",
        "network_mode": "host_ambient",
    }


class RunControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SessionStateStore(self.root / "sessions.sqlite3")
        self.profile = load_profile("host-unsandboxed-mac-v1")
        self.runner = FakeRunner()
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        self.alerts = []
        self.controller = RunController(
            self.store,
            self.profile,
            runs_directory=self.root / "runs",
            runner=self.runner,
            checkout=lambda destination, _revision: destination,
            doctor_fn=ready_doctor,
            clock=lambda: self.now,
            alert_sender=self._alert,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _alert(self, session, reason, messages, url):
        self.alerts.append((session.session_id, reason, len(messages), url))
        return True

    def start_run(self):
        session = self.controller.request_investigation(patch_record())
        self.controller.tick()
        return self.store.get_session(session.session_id)

    def test_request_requires_exact_open_revision_and_reserves_patch_once(self):
        with self.assertRaisesRegex(RunControllerError, "refresh"):
            self.controller.request_investigation(patch_record(revision_sha=""))
        first = self.controller.request_investigation(patch_record())
        with self.assertRaises(SessionAlreadyExists):
            self.controller.request_investigation(patch_record())
        self.assertEqual(first.revision, REVISION)
        self.assertEqual(first.patchset, 4)

    def test_dispatch_persists_admission_and_starts_strict_read_only_runner(self):
        session = self.start_run()
        self.assertEqual(session.state, "running")
        self.assertEqual(len(self.runner.starts), 1)
        spec = self.runner.starts[0]
        self.assertEqual(spec.session_id, session.session_id)
        self.assertEqual(Path(spec.cwd).stat().st_mode & 0o777, 0o500)
        admission = self.store.get_worker_admission(session.session_id)
        self.assertEqual(admission.status, "ready")
        envelope = next(
            (self.root / "runs" / session.run_id).rglob("run-envelope.json")
        ).read_text()
        self.assertIn('"read_source"', envelope)
        for forbidden in ("request_retest", "comment_gerrit", "upload_patchset", "start_ltvm"):
            self.assertNotIn(forbidden, envelope)

    def test_controller_restart_adopts_persisted_host_identity(self):
        session = self.start_run()
        replacement = RunController(
            self.store,
            self.profile,
            runs_directory=self.root / "runs",
            runner=self.runner,
            checkout=lambda destination, _revision: destination,
            doctor_fn=ready_doctor,
            clock=lambda: self.now,
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


if __name__ == "__main__":
    unittest.main()
