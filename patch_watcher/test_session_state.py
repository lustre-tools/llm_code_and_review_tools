import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_state import (
    AGENT_ABSOLUTE_RUNTIME_CAP,
    AGENT_INACTIVITY_TIMEOUT,
    AGENT_RUNTIME_TIMEOUT,
    InvalidSessionOperation,
    SessionAlreadyExists,
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
        patch_id=None,
        revision=None,
        patchset=None,
    ):
        if patch_id is None:
            patch_id = "LU-12345" if session_id == "session-1" else f"LU-{session_id}"
        return self.store.register_session(
            session_id,
            patch_id=patch_id,
            run_id=run_id,
            profile=profile,
            state=state,
            pid=pid,
            started_at=started_at,
            revision=revision,
            patchset=patchset,
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
        self.database.unlink()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE pw_session_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                )
                """
            )
            for version in range(1, 4):
                for statement in SessionStateStore._MIGRATIONS[version]:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO pw_session_schema(singleton, version)
                    VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET version = excluded.version
                    """,
                    (version,),
                )
            epoch = START.timestamp()
            connection.execute(
                """
                INSERT INTO pw_managed_session(
                    session_id, patch_id, run_id, profile, state, pid,
                    started_at, last_qualifying_activity_at,
                    active_interval_started_at, state_changed_at,
                    created_at, updated_at
                ) VALUES (
                    'legacy-session', 'LU-legacy', 'legacy-run', 'engineering',
                    'running', 42, ?, ?, ?, ?, ?, ?
                )
                """,
                (epoch, epoch, epoch, epoch, epoch, epoch),
            )
            connection.execute(
                """
                INSERT INTO pw_session_control_intent(
                    request_id, session_id, action, requested_by, requested_at,
                    confirmed_by, confirmed_at
                ) VALUES (
                    'legacy-kill', 'legacy-session', 'kill', 'patrick', ?,
                    'patrick', ?
                )
                """,
                (epoch, epoch + 1),
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
        legacy = migrated.list_control_intents("legacy-session")
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].status, "confirmed")
        self.assertTrue(legacy[0].confirmed)

        with sqlite3.connect(self.database) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(pw_session_control_intent)"
                )
            }
        self.assertIn("confirmation_token_hash", columns)
        self.assertIn("revision", {
            row[1]
            for row in sqlite3.connect(self.database).execute(
                "PRAGMA table_info(pw_managed_session)"
            )
        })

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

    def test_one_active_pinned_session_per_patch_is_race_safe(self):
        barrier = threading.Barrier(2)
        successes = []
        failures = []

        def attempt(index):
            store = SessionStateStore(self.database)
            barrier.wait()
            try:
                successes.append(
                    store.register_pinned_session(
                        f"race-{index}",
                        patch_id="68160",
                        run_id=f"race-run-{index}",
                        revision=f"deadbeef{index}",
                        patchset=13,
                        profile="engineering",
                        started_at=START,
                    )
                )
            except SessionAlreadyExists as exc:
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        winner = successes[0]
        self.assertEqual(winner.patchset, 13)
        self.assertTrue(winner.revision.startswith("deadbeef"))

        self.store.finish_session(winner.session_id, "succeeded", finished_at=START)
        replacement = self.store.register_pinned_session(
            "replacement",
            patch_id="68160",
            run_id="replacement-run",
            revision="feedface",
            patchset=14,
            profile="engineering",
            started_at=START + timedelta(minutes=1),
        )
        self.assertEqual(replacement.patchset, 14)

    def test_append_only_events_are_idempotent_and_survive_restart(self):
        self.register(state="running")
        event = self.store.append_event(
            "session-1",
            "assistant_message",
            {"text": "analysis complete"},
            idempotency_key="transport-event-1",
            at=START + timedelta(minutes=1),
        )
        repeated = self.store.append_event(
            "session-1",
            "assistant_message",
            {"text": "analysis complete"},
            idempotency_key="transport-event-1",
            at=START + timedelta(minutes=2),
        )
        self.assertEqual(repeated.event_id, event.event_id)
        with self.assertRaises(InvalidSessionOperation):
            self.store.append_event(
                "session-1",
                "assistant_message",
                {"text": "different"},
                idempotency_key="transport-event-1",
            )
        with sqlite3.connect(self.database) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE pw_session_event SET event_type = 'tampered'"
                )
        reopened = SessionStateStore(self.database)
        self.assertEqual(reopened.list_events("session-1"), [event])

    def test_guidance_outbox_has_atomic_single_claim_and_exact_terminal_state(self):
        self.register(state="running")
        guidance = self.store.enqueue_guidance(
            "session-1",
            "Please inspect the newest failure.",
            idempotency_key="human-guidance-1",
            at=START + timedelta(minutes=1),
        )
        self.assertEqual(
            self.store.enqueue_guidance(
                "session-1",
                "Please inspect the newest failure.",
                idempotency_key="human-guidance-1",
            ).guidance_id,
            guidance.guidance_id,
        )
        barrier = threading.Barrier(2)
        claims = []

        def claim(consumer):
            barrier.wait()
            claims.append(
                SessionStateStore(self.database).claim_next_guidance(
                    "session-1", consumer, at=START + timedelta(minutes=2)
                )
            )

        threads = [threading.Thread(target=claim, args=(f"runner-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [item for item in claims if item is not None]
        self.assertEqual(len(claimed), 1)
        consumer = claimed[0].claimed_by
        self.assertEqual(
            SessionStateStore(self.database).claim_next_guidance(
                "session-1", consumer, at=START + timedelta(minutes=2, seconds=1)
            ).guidance_id,
            guidance.guidance_id,
        )
        delivered = SessionStateStore(self.database).finish_guidance_delivery(
            guidance.guidance_id,
            consumer,
            delivered=True,
            at=START + timedelta(minutes=3),
        )
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(
            SessionStateStore(self.database).finish_guidance_delivery(
                guidance.guidance_id,
                consumer,
                delivered=True,
                at=START + timedelta(minutes=4),
            ),
            delivered,
        )

    def test_runner_transport_exact_identity_can_be_adopted_after_restart(self):
        self.register(state="running", pid=None)
        transport = self.store.attach_runner_transport(
            "session-1",
            transport="claude-stream-json",
            transport_session_id="claude-session-77",
            pid=777,
            process_started_at=START,
            process_fingerprint="pid777:start123:exeabc",
            attached_at=START + timedelta(seconds=1),
        )
        self.assertEqual(transport.adoption_state, "attached")
        reopened = SessionStateStore(self.database)
        with self.assertRaisesRegex(InvalidSessionOperation, "fingerprint mismatch"):
            reopened.adopt_runner_transport(
                "session-1", process_fingerprint="wrong"
            )
        adopted = reopened.adopt_runner_transport(
            "session-1",
            process_fingerprint="pid777:start123:exeabc",
            at=START + timedelta(minutes=2),
        )
        self.assertEqual(adopted.adoption_state, "adopted")
        self.assertEqual(reopened.get_session("session-1").pid, 777)

    def test_waiting_human_answer_atomically_resumes_and_queues_guidance(self):
        self.register(state="running")
        question = self.store.ask_human(
            "session-1",
            "Which branch should I compare?",
            question_id="question-1",
            at=START + timedelta(minutes=4),
        )
        self.assertEqual(self.store.get_session("session-1").state, "waiting_human")
        with self.assertRaisesRegex(InvalidSessionOperation, "already has"):
            self.store.ask_human("session-1", "Another question")

        answered, guidance = self.store.answer_human_question(
            "session-1",
            question.question_id,
            answered_by="patrick",
            answer="Compare against master.",
            at=START + timedelta(minutes=8),
        )
        session = self.store.get_session("session-1")
        self.assertEqual(answered.status, "answered")
        self.assertEqual(guidance.status, "pending")
        self.assertEqual(guidance.body, "Compare against master.")
        self.assertEqual(session.state, "running")
        self.assertEqual(session.active_interval_started_at, START + timedelta(minutes=8))
        with self.assertRaises(InvalidSessionOperation):
            self.store.answer_human_question(
                "session-1",
                question.question_id,
                answered_by="patrick",
                answer="Again",
            )

    def test_terminal_result_stale_guard_and_owner_cleanup_are_durable(self):
        self.register(
            state="running",
            revision="old-revision",
            patchset=8,
        )
        resource = self.store.register_owned_resource(
            "session-1",
            owner_id="run-1",
            resource_type="ltvm-vm",
            external_id="pw-run-1-client",
            metadata={"configured_memory_mib": 2048},
            at=START + timedelta(minutes=1),
        )
        stale = self.store.mark_stale_for_revision(
            "session-1",
            observed_revision="new-revision",
            observed_patchset=9,
            at=START + timedelta(minutes=2),
        )
        self.assertEqual(stale.state, "stale")
        self.assertEqual(stale.failure_code, "patch_revision_changed")
        pending = self.store.list_owned_resources(session_id="session-1")[0]
        self.assertEqual(pending.state, "cleanup_pending")
        cleaned = self.store.mark_resource_cleanup(
            resource.resource_id,
            succeeded=True,
            at=START + timedelta(minutes=3),
        )
        self.assertEqual(cleaned.state, "cleaned")
        with self.assertRaises(InvalidSessionOperation):
            self.store.record_activity("session-1")
        with self.assertRaises(InvalidSessionOperation):
            self.store.finish_session(
                "session-1", "failed", failure_code="different"
            )
        reopened = SessionStateStore(self.database)
        self.assertEqual(reopened.get_terminal_result("session-1"), stale)
        self.assertEqual(
            reopened.list_owned_resources(owner_id="run-1")[0].state,
            "cleaned",
        )

    def test_destructive_confirmation_token_is_hashed_expiring_and_one_time(self):
        self.register(state="running")
        intent, token = self.store.request_destructive_control(
            "session-1",
            "kill",
            "operator",
            requested_at=START,
            expires_in=timedelta(minutes=5),
            request_id="kill-with-token",
        )
        self.assertFalse(intent.confirmed)
        with sqlite3.connect(self.database) as connection:
            stored = connection.execute(
                """
                SELECT confirmation_token_hash
                FROM pw_session_control_intent WHERE request_id = ?
                """,
                (intent.request_id,),
            ).fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertNotIn(token, self.database.read_bytes().decode("latin1"))
        with self.assertRaisesRegex(InvalidSessionOperation, "already used"):
            self.store.request_destructive_control(
                "session-1",
                "kill",
                "operator",
                requested_at=START,
                expires_in=timedelta(minutes=5),
                request_id="kill-with-token",
            )
        with self.assertRaisesRegex(InvalidSessionOperation, "invalid"):
            self.store.confirm_control_with_token(
                "session-1",
                intent.request_id,
                "incorrect-token",
                "operator",
                confirmed_at=START + timedelta(minutes=1),
            )
        confirmed = self.store.confirm_control_with_token(
            "session-1",
            intent.request_id,
            token,
            "operator",
            confirmed_at=START + timedelta(minutes=2),
        )
        self.assertEqual(confirmed.status, "confirmed")
        with self.assertRaisesRegex(InvalidSessionOperation, "already used"):
            self.store.confirm_control_with_token(
                "session-1",
                intent.request_id,
                token,
                "operator",
                confirmed_at=START + timedelta(minutes=3),
            )
        executed = self.store.finish_control_intent(
            "session-1",
            intent.request_id,
            succeeded=True,
            executed_at=START + timedelta(minutes=4),
        )
        self.assertEqual(executed.status, "executed")
        self.assertEqual(self.store.get_session("session-1").state, "running")

        self.register(session_id="session-2", run_id="run-2", state="running")
        expiring, expiring_token = self.store.request_destructive_control(
            "session-2",
            "cancel",
            "operator",
            requested_at=START,
            expires_in=timedelta(seconds=1),
        )
        with self.assertRaisesRegex(InvalidSessionOperation, "expired"):
            self.store.confirm_control_with_token(
                "session-2",
                expiring.request_id,
                expiring_token,
                "operator",
                confirmed_at=START + timedelta(seconds=2),
            )

    def test_generic_notification_delivery_is_idempotent_across_restart(self):
        self.register(state="running")
        record = self.store.ensure_delivery(
            "session-1",
            kind="timeout_email",
            idempotency_key="timeout:session-1:inactivity:1",
            payload={"reason": "no activity"},
            at=START,
        )
        reopened = SessionStateStore(self.database)
        self.assertEqual(
            reopened.ensure_delivery(
                "session-1",
                kind="timeout_email",
                idempotency_key=record.idempotency_key,
                payload={"reason": "no activity"},
            ),
            record,
        )
        delivered = reopened.finish_delivery(
            record.idempotency_key,
            delivered=True,
            at=START + timedelta(minutes=1),
        )
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(
            reopened.finish_delivery(
                record.idempotency_key,
                delivered=True,
                at=START + timedelta(minutes=2),
            ),
            delivered,
        )


if __name__ == "__main__":
    unittest.main()
