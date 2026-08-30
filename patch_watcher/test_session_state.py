import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_state import (
    AGENT_ABSOLUTE_RUNTIME_CAP,
    AGENT_INACTIVITY_TIMEOUT,
    AGENT_RUNTIME_TIMEOUT,
    SessionStateStore,
)


START = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


class SessionStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "sessions.sqlite3"
        self.store = SessionStateStore(self.database)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def register(
        self,
        *,
        session_id="session-1",
        run_id="run-1",
        profile="engineering",
        state="preparing",
        pid=1234,
        started_at=START,
    ):
        return self.store.register_session(
            session_id,
            patch_id="LU-12345",
            run_id=run_id,
            profile=profile,
            state=state,
            pid=pid,
            started_at=started_at,
        )

    def test_sessions_activity_and_messages_persist_across_restart(self):
        self.register()
        activity_at = START + timedelta(minutes=8)
        self.store.record_activity("session-1", at=activity_at)
        self.store.record_message(
            "session-1", "agent", "still investigating", at=activity_at
        )

        reopened = SessionStateStore(self.database)
        sessions = reopened.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].patch_id, "LU-12345")
        self.assertEqual(sessions[0].run_id, "run-1")
        self.assertEqual(sessions[0].profile, "engineering")
        self.assertEqual(sessions[0].pid, 1234)
        self.assertEqual(sessions[0].last_qualifying_activity_at, activity_at)
        self.assertEqual(
            [message.body for message in reopened.recent_messages("session-1")],
            ["still investigating"],
        )
        self.assertEqual(os.stat(self.database).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.database.parent).st_mode & 0o777, 0o700)

    def test_store_secures_database_without_repermissioning_existing_parent(self):
        shared_parent = Path(self.temporary_directory.name) / "shared"
        shared_parent.mkdir(mode=0o755)
        database = shared_parent / "sessions.sqlite3"

        SessionStateStore(database)

        self.assertEqual(os.stat(database).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(shared_parent).st_mode & 0o777, 0o755)

    def test_schema_migrates_from_previous_private_version(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP TABLE pw_worker_admission")
            connection.execute("DROP TABLE pw_session_control_intent")
            connection.execute(
                "UPDATE pw_session_schema SET version = 1 WHERE singleton = 1"
            )

        migrated = SessionStateStore(self.database)
        version = sqlite3.connect(self.database).execute(
            "SELECT version FROM pw_session_schema WHERE singleton = 1"
        ).fetchone()[0]
        self.assertEqual(version, migrated.SCHEMA_VERSION)
        request = migrated.request_kill(
            self.register(session_id="session-2", run_id="run-2").session_id,
            "operator",
            requested_at=START + timedelta(minutes=1),
        )
        self.assertEqual(request.action, "kill")

    def test_triage_wall_limit_uses_inclusive_twenty_minute_boundary(self):
        self.register(profile="triage", state="running")
        before = self.store.evaluate_policy(
            "session-1", now=START + timedelta(minutes=20) - timedelta(microseconds=1)
        )
        at_limit = self.store.evaluate_policy(
            "session-1", now=START + timedelta(minutes=20)
        )
        self.assertIsNone(before.timeout)
        self.assertEqual(at_limit.timeout.code, AGENT_RUNTIME_TIMEOUT)
        self.assertEqual(at_limit.timeout.deadline_at, START + timedelta(minutes=20))

    def test_worker_admission_persists_provenance_across_restart(self):
        self.register(state="queued", pid=None)
        stored = self.store.record_worker_admission(
            "session-1",
            profile_id="host-unsandboxed-mac-v1",
            profile_hash="a" * 64,
            environment_instance_id="macbook-pro",
            status="degraded",
            isolation_profile="host-unsandboxed",
            network_profile="host-unrestricted",
            attestation={
                "schema": "patch-watcher-environment-attestation/v1",
                "warnings": [{"code": "tool_optional_missing"}],
            },
            instruction_hash="b" * 64,
            checked_at=START + timedelta(minutes=1),
        )
        self.assertEqual(stored.status, "degraded")
        self.assertEqual(stored.attestation["warnings"][0]["code"], "tool_optional_missing")

        reopened = SessionStateStore(self.database)
        persisted = reopened.get_worker_admission("session-1")
        self.assertEqual(persisted.profile_hash, "a" * 64)
        self.assertEqual(persisted.instruction_hash, "b" * 64)
        self.assertEqual(persisted.checked_at, START + timedelta(minutes=1))
        self.assertEqual(reopened.list_worker_admissions(), [persisted])

    def test_blocked_worker_admission_requires_precise_failure_code(self):
        self.register(state="queued", pid=None)
        arguments = dict(
            profile_id="host-unsandboxed-mac-v1",
            profile_hash="a" * 64,
            environment_instance_id="macbook-pro",
            status="blocked",
            isolation_profile="host-unsandboxed",
            network_profile="host-unrestricted",
            instruction_hash="b" * 64,
        )
        with self.assertRaisesRegex(ValueError, "requires failure_code"):
            self.store.record_worker_admission("session-1", **arguments)

        stored = self.store.record_worker_admission(
            "session-1",
            **arguments,
            failure_code="tool_version_mismatch",
            failure_summary="selected Python is too old",
        )
        self.assertEqual(stored.failure_code, "tool_version_mismatch")
        self.assertEqual(stored.failure_summary, "selected Python is too old")

    def test_worker_admission_rejects_non_json_or_oversized_attestation(self):
        self.register(state="queued", pid=None)
        arguments = dict(
            profile_id="host-unsandboxed-mac-v1",
            profile_hash="a" * 64,
            environment_instance_id="macbook-pro",
            status="ready",
            isolation_profile="host-unsandboxed",
            network_profile="host-unrestricted",
            instruction_hash="b" * 64,
        )
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            self.store.record_worker_admission(
                "session-1", **arguments, attestation={"bad": object()}
            )
        with self.assertRaisesRegex(ValueError, "256 KiB"):
            self.store.record_worker_admission(
                "session-1", **arguments, attestation={"large": "x" * 256_001}
            )

    def test_engineering_inactivity_boundary_and_qualifying_activity(self):
        self.register(state="running")
        self.store.record_activity(
            "session-1", at=START + timedelta(minutes=10)
        )
        before = self.store.evaluate_policy(
            "session-1",
            now=START + timedelta(minutes=40) - timedelta(microseconds=1),
        )
        at_limit = self.store.evaluate_policy(
            "session-1", now=START + timedelta(minutes=40)
        )
        self.assertIsNone(before.timeout)
        self.assertEqual(at_limit.timeout.code, AGENT_INACTIVITY_TIMEOUT)
        self.assertEqual(at_limit.timeout.deadline_at, START + timedelta(minutes=40))

    def test_waiting_states_suspend_inactivity_and_resume_with_fresh_interval(self):
        for index, state in enumerate(
            ("waiting_human", "waiting_external", "paused", "blocked")
        ):
            session_id = f"waiting-{index}"
            self.register(
                session_id=session_id,
                run_id=f"waiting-run-{index}",
                state="running",
            )
            self.store.set_state(
                session_id, state, changed_at=START + timedelta(minutes=10)
            )
            decision = self.store.evaluate_policy(
                session_id, now=START + timedelta(hours=3)
            )
            self.assertIsNone(decision.timeout, state)

            resumed_at = START + timedelta(hours=3)
            resumed = self.store.set_state(
                session_id, "running", changed_at=resumed_at
            )
            self.assertEqual(resumed.active_interval_started_at, resumed_at)
            self.assertIsNone(
                self.store.evaluate_policy(
                    session_id,
                    now=resumed_at + timedelta(minutes=30) - timedelta(microseconds=1),
                ).timeout,
                state,
            )
            self.assertEqual(
                self.store.evaluate_policy(
                    session_id, now=resumed_at + timedelta(minutes=30)
                ).timeout.code,
                AGENT_INACTIVITY_TIMEOUT,
                state,
            )

    def test_engineering_reminders_are_interval_idempotent_across_restart(self):
        self.register(state="waiting_human")
        before = self.store.evaluate_policy(
            "session-1", now=START + timedelta(hours=2) - timedelta(microseconds=1)
        )
        first = self.store.evaluate_policy(
            "session-1", now=START + timedelta(hours=2)
        ).reminder
        self.assertIsNone(before.reminder)
        self.assertEqual(first.interval_index, 1)
        self.assertTrue(
            self.store.mark_reminder_delivered(
                "session-1",
                first.interval_index,
                delivered_at=START + timedelta(hours=2),
                idempotency_key=first.idempotency_key,
            )
        )
        self.assertFalse(
            self.store.mark_reminder_delivered(
                "session-1",
                first.interval_index,
                delivered_at=START + timedelta(hours=2),
            )
        )

        reopened = SessionStateStore(self.database)
        self.assertIsNone(
            reopened.evaluate_policy(
                "session-1", now=START + timedelta(hours=3, minutes=59)
            ).reminder
        )
        second = reopened.evaluate_policy(
            "session-1", now=START + timedelta(hours=4)
        ).reminder
        self.assertEqual(second.interval_index, 2)
        self.assertNotEqual(second.idempotency_key, first.idempotency_key)

    def test_messages_are_bounded_by_count_and_length(self):
        store = SessionStateStore(
            self.database, max_recent_messages=3, max_message_chars=8
        )
        store.register_session(
            "bounded",
            patch_id="LU-9",
            run_id="bounded-run",
            profile="triage",
            state="running",
            started_at=START,
        )
        for index in range(5):
            store.record_message(
                "bounded",
                "agent",
                f"message-{index}-too-long",
                at=START + timedelta(seconds=index),
            )
        messages = store.recent_messages("bounded")
        self.assertEqual(
            [message.body for message in messages],
            ["message-", "message-", "message-"],
        )
        self.assertEqual(len(messages), 3)
        self.assertEqual([message.created_at.second for message in messages], [2, 3, 4])

        with sqlite3.connect(self.database) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM pw_session_message WHERE session_id = 'bounded'"
            ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_absolute_cap_is_universal_nonextendable_and_precedes_other_limits(self):
        self.register(profile="engineering", state="waiting_external")
        before = self.store.evaluate_policy(
            "session-1", now=START + timedelta(hours=48) - timedelta(microseconds=1)
        )
        at_limit = self.store.evaluate_policy(
            "session-1", now=START + timedelta(hours=48)
        )
        self.assertIsNone(before.timeout)
        self.assertEqual(at_limit.timeout.code, AGENT_ABSOLUTE_RUNTIME_CAP)
        self.assertEqual(at_limit.timeout.deadline_at, START + timedelta(hours=48))
        self.assertIsNone(at_limit.reminder)

        self.register(
            session_id="triage-48",
            run_id="triage-48-run",
            profile="triage",
            state="running",
        )
        triage_at_cap = self.store.evaluate_policy(
            "triage-48", now=START + timedelta(hours=48)
        )
        self.assertEqual(triage_at_cap.timeout.code, AGENT_ABSOLUTE_RUNTIME_CAP)

    def test_cancel_and_kill_confirmation_only_record_intent(self):
        self.register(state="running")
        cancel = self.store.request_cancellation(
            "session-1",
            "operator",
            requested_at=START + timedelta(minutes=1),
            request_id="cancel-token",
        )
        self.assertFalse(cancel.confirmed)
        confirmed_cancel = self.store.confirm_cancellation(
            "session-1",
            cancel.request_id,
            "operator",
            confirmed_at=START + timedelta(minutes=2),
        )
        self.assertTrue(confirmed_cancel.confirmed)

        kill = self.store.request_kill(
            "session-1",
            "operator",
            requested_at=START + timedelta(minutes=3),
            request_id="kill-token",
        )
        confirmed_kill = self.store.confirm_kill(
            "session-1",
            kill.request_id,
            "operator",
            confirmed_at=START + timedelta(minutes=4),
        )
        self.assertTrue(confirmed_kill.confirmed)
        self.assertEqual(self.store.get_session("session-1").state, "running")
        self.assertEqual(self.store.get_session("session-1").pid, 1234)

        reopened = SessionStateStore(self.database)
        intents = reopened.list_control_intents("session-1")
        self.assertEqual([intent.action for intent in intents], ["cancel", "kill"])
        self.assertTrue(all(intent.confirmed for intent in intents))


if __name__ == "__main__":
    unittest.main()
