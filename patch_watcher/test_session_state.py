import contextlib
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from patch_watcher.session_state import (
    AGENT_ABSOLUTE_RUNTIME_CAP,
    AGENT_INACTIVITY_TIMEOUT,
    AGENT_RUNTIME_TIMEOUT,
    InvalidSessionOperation,
    SessionAlreadyExists,
    SessionStateError,
    SessionStateStore,
)

START = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


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

    def test_list_deliveries_filters_by_kind_oldest_first(self):
        from datetime import timedelta

        self.store.register_pinned_session(
            "ledger-session", patch_id="68160", run_id="ledger-run", revision="a" * 40,
            patchset=1, profile="engineering", started_at=START,
        )
        for index, (kind, key) in enumerate((
            ("human_notice", "human-notice:q1:email"),
            ("session_alert", "session-alert:x"),
            ("human_notice", "human-notice:q1:gerrit"),
        )):
            self.store.ensure_delivery(
                "ledger-session", kind=kind, idempotency_key=key,
                payload={"channel": key.rsplit(":", 1)[-1]},
                at=START + timedelta(seconds=index),
            )
        every = self.store.list_deliveries("ledger-session")
        self.assertEqual(
            [item.idempotency_key for item in every],
            ["human-notice:q1:email", "session-alert:x", "human-notice:q1:gerrit"],
        )
        notices = self.store.list_deliveries("ledger-session", kind="human_notice")
        self.assertEqual([item.payload["channel"] for item in notices], ["email", "gerrit"])
        self.assertEqual({item.status for item in notices}, {"pending"})

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
        request, _token = migrated.request_destructive_control(
            self.register(session_id="session-2", run_id="run-2").session_id,
            "kill",
            "operator",
            requested_at=START + timedelta(minutes=1),
        )
        self.assertEqual(request.action, "kill")
        legacy = migrated.list_control_intents("legacy-session")
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].status, "confirmed")
        self.assertTrue(legacy[0].confirmed)

    def test_queue_order_survives_a_backward_wall_clock_step(self):
        """FIFO must follow insertion order, not the host's wall clock.

        created_at/requested_at are the host clock and the primary keys are
        random UUIDs, so under a backward step (WSL2 NTP drift does this) a
        wall-clock ORDER BY hands the consumer the newer row first: a fresh
        operator answer jumps an older pending one, and a cancel requested
        after a kill is applied before it.
        """
        self.register(state="running")
        first = self.store.enqueue_guidance(
            "session-1", "answer one", idempotency_key="g1", at=START
        )
        # The host clock steps back a minute between the two enqueues.
        second = self.store.enqueue_guidance(
            "session-1",
            "answer two",
            idempotency_key="g2",
            at=START - timedelta(minutes=1),
        )
        self.assertEqual(first.created_at, START)
        self.assertGreater(first.created_at, second.created_at)
        self.assertEqual(
            [item.guidance_id for item in self.store.list_guidance("session-1")],
            [first.guidance_id, second.guidance_id],
        )
        claimed = self.store.claim_next_guidance("session-1", "controller")
        self.assertEqual(claimed.guidance_id, first.guidance_id)
        self.store.finish_guidance_delivery(
            first.guidance_id, "controller", delivered=True
        )
        self.assertEqual(
            self.store.claim_next_guidance("session-1", "controller").guidance_id,
            second.guidance_id,
        )

        kill, _kill_token = self.store.request_destructive_control(
            "session-1", "kill", "operator", requested_at=START
        )
        cancel, _cancel_token = self.store.request_destructive_control(
            "session-1",
            "cancel",
            "operator",
            requested_at=START - timedelta(minutes=1),
        )
        self.assertGreater(kill.requested_at, cancel.requested_at)
        self.assertEqual(
            [item.request_id for item in self.store.list_control_intents("session-1")],
            [kill.request_id, cancel.request_id],
        )

    def test_populated_version_six_database_migrates_to_ordered_queues(self):
        """A real v6 database keeps every row and gains its queue ordering."""
        self.database.unlink()
        epoch = START.timestamp()
        guidance_rows = [
            # (guidance_id, created_at) deliberately inserted out of order, so
            # the backfill's ROW_NUMBER has to do real work.
            ("guidance-c", epoch + 30),
            ("guidance-a", epoch + 10),
            ("guidance-b", epoch + 20),
        ]
        intent_rows = [
            ("intent-b", "cancel", epoch + 25),
            ("intent-a", "pause", epoch + 5),
        ]
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE pw_session_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                )
                """
            )
            for version in range(1, 7):
                for statement in SessionStateStore._MIGRATIONS[version]:
                    connection.execute(statement)
            connection.execute(
                """
                INSERT INTO pw_session_schema(singleton, version) VALUES (1, 6)
                """
            )
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
                (epoch,) * 6,
            )
            for guidance_id, created_at in guidance_rows:
                connection.execute(
                    """
                    INSERT INTO pw_outbound_guidance(
                        guidance_id, session_id, body, status,
                        idempotency_key, created_at
                    ) VALUES (?, 'legacy-session', ?, 'pending', ?, ?)
                    """,
                    (guidance_id, "body " + guidance_id, "key-" + guidance_id, created_at),
                )
            for request_id, action, requested_at in intent_rows:
                connection.execute(
                    """
                    INSERT INTO pw_session_control_intent(
                        request_id, session_id, action, requested_by, requested_at
                    ) VALUES (?, 'legacy-session', ?, 'patrick', ?)
                    """,
                    (request_id, action, requested_at),
                )
            before = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "pw_managed_session",
                    "pw_outbound_guidance",
                    "pw_session_control_intent",
                )
            }

        migrated = SessionStateStore(self.database)
        self.assertEqual(migrated.SCHEMA_VERSION, 7)

        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM pw_session_schema WHERE singleton = 1"
                ).fetchone()[0],
                7,
            )
            after = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in before
            }
            self.assertEqual(after, before)
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            # Existing rows keep the order the wall-clock query gave them.
            self.assertEqual(
                connection.execute(
                    "SELECT guidance_id, sequence FROM pw_outbound_guidance"
                    " ORDER BY sequence"
                ).fetchall(),
                [("guidance-a", 1), ("guidance-b", 2), ("guidance-c", 3)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT request_id, sequence FROM pw_session_control_intent"
                    " ORDER BY sequence"
                ).fetchall(),
                [("intent-a", 1), ("intent-b", 2)],
            )
            # The retired action value is gone from the rebuilt constraint.
            self.assertNotIn(
                "follow_up",
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name ="
                    " 'pw_session_control_intent'"
                ).fetchone()[0],
            )

        # A row appended after the migration sorts after every migrated row.
        appended = migrated.enqueue_guidance(
            "legacy-session",
            "appended",
            idempotency_key="key-appended",
            at=START - timedelta(hours=1),
        )
        self.assertEqual(
            [item.guidance_id for item in migrated.list_guidance("legacy-session")],
            ["guidance-a", "guidance-b", "guidance-c", appended.guidance_id],
        )

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

    def test_resume_racing_a_reap_cannot_split_the_two_state_rows(self):
        """set_state's terminal guard and its write must be one transaction.

        sqlite3's legacy isolation opens a transaction only before DML, so a
        SELECT-then-UPDATE spans two of them. A resume that read "paused" and
        a controller that finished the run in between produced a terminal
        result of "failed" alongside a session row of "running" -- an active
        run that could never be cleaned up, so its checkout was never
        released.
        """

        splits = []
        for index in range(40):
            session_id = f"race-{index}"
            self.register(session_id=session_id, run_id=f"run-{index}", state="paused")
            barrier = threading.Barrier(2)

            # Whichever thread loses may legitimately be refused; what must
            # never happen is that both succeed against stale snapshots and
            # leave the two rows disagreeing.
            def resume(session_id=session_id, barrier=barrier):
                barrier.wait()
                with contextlib.suppress(SessionStateError):
                    self.store.set_state(session_id, "running")

            def reap(session_id=session_id, barrier=barrier):
                barrier.wait()
                with contextlib.suppress(SessionStateError):
                    self.store.finish_session(
                        session_id, "failed", failure_code="runner_lost"
                    )

            threads = [
                threading.Thread(target=resume),
                threading.Thread(target=reap),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            session = self.store.get_session(session_id)
            terminal = self.store.get_terminal_result(session_id)
            if terminal is not None and session.state != terminal.state:
                splits.append((index, session.state, terminal.state))

        self.assertEqual(splits, [], "session row and terminal result disagree")

    def test_finish_session_heals_a_database_already_split_apart(self):
        self.register(state="running")
        self.store.finish_session("session-1", "failed", failure_code="runner_lost")
        # Reproduce what the old race left behind: a terminal result recorded,
        # but the session row still claiming to be active.
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE pw_managed_session SET state = 'running' WHERE session_id = ?",
                ("session-1",),
            )
        self.assertEqual(self.store.get_session("session-1").state, "running")
        self.assertIn(
            "session-1",
            [
                item.session_id
                for item in self.store.list_sessions(include_terminal=False)
            ],
        )

        self.store.finish_session("session-1", "failed", failure_code="runner_lost")

        self.assertEqual(self.store.get_session("session-1").state, "failed")
        self.assertNotIn(
            "session-1",
            [
                item.session_id
                for item in self.store.list_sessions(include_terminal=False)
            ],
        )

    def test_a_backward_clock_step_does_not_hide_or_delete_the_newest_message(self):
        """Message retention was keyed on the runner's wall clock.

        `created_at` comes from the runner's `time.time()`, so after a
        backward step every new message sorts OLDEST. A small step hid the
        newest message from `recent_messages`, freezing the operator's
        "Latest message" on stale text while the agent kept working; a step
        larger than the retained window's span made the INSERT's own row be
        pruned by its own DELETE, tripping the assert that follows and
        aborting the whole ingestion pass -- once per message, for as long as
        the skew lasted.
        """

        self.register(state="running")
        for index in range(60):
            self.store.record_message(
                "session-1", "agent", f"before-{index}",
                at=START + timedelta(minutes=index),
            )

        # The clock steps back an hour; the agent carries on talking.
        stepped = self.store.record_message(
            "session-1", "agent", "after the step", at=START - timedelta(hours=1)
        )
        self.assertIsNotNone(stepped)

        # recent_messages returns the bounded tail in chronological order, so
        # the newest is last -- and app._run_projection reads messages[-1] for
        # the operator's "Latest message".
        recent = self.store.recent_messages("session-1", limit=5)
        self.assertEqual(
            recent[-1].body, "after the step",
            "the newest message was hidden by an older timestamp",
        )
        # And it survives the next few messages rather than being pruned.
        for index in range(5):
            self.store.record_message(
                "session-1", "agent", f"still-here-{index}",
                at=START - timedelta(hours=1),
            )
        bodies = [item.body for item in self.store.recent_messages("session-1")]
        self.assertIn("after the step", bodies)

    def test_out_of_order_activity_still_clamps(self):
        self.register(state="running")
        self.store.record_activity("session-1", at=START + timedelta(minutes=10))
        session = self.store.record_activity(
            "session-1", at=START + timedelta(minutes=8)
        )
        self.assertEqual(
            session.last_qualifying_activity_at, START + timedelta(minutes=10)
        )

    def test_backward_clock_step_reanchors_the_inactivity_deadline(self):
        """A stale future anchor must not make a session un-timeout-able.

        Clamping with MAX() alone pins the anchor to the highest timestamp
        ever recorded, so one timestamp from the future -- what a host clock
        step produces -- holds the inactivity deadline out of reach until real
        time catches up. A wedged session would then keep its checkout until
        the 48h absolute cap rather than the 30m inactivity limit.
        """

        self.register(state="running")
        self.store.record_activity("session-1", at=START + timedelta(hours=1))
        session = self.store.record_activity(
            "session-1", at=START + timedelta(minutes=5)
        )
        self.assertEqual(
            session.last_qualifying_activity_at, START + timedelta(minutes=5)
        )
        self.assertIsNone(
            self.store.evaluate_policy(
                "session-1", now=START + timedelta(minutes=34)
            ).timeout
        )
        self.assertEqual(
            self.store.evaluate_policy(
                "session-1", now=START + timedelta(minutes=35)
            ).timeout.code,
            AGENT_INACTIVITY_TIMEOUT,
        )
        with sqlite3.connect(self.database) as connection:
            updated_at = connection.execute(
                "SELECT updated_at FROM pw_managed_session WHERE session_id = ?",
                ("session-1",),
            ).fetchone()[0]
        # The anchor may move backwards; updated_at orders listings and must not.
        self.assertGreaterEqual(updated_at, (START + timedelta(hours=1)).timestamp())

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


class RowColumnAccessTests(unittest.TestCase):
    """Guard against a lint autofix that silently drops persisted columns.

    Ruff's SIM118 rewrites `"col" in row.keys()` to `"col" in row`. For a
    sqlite3.Row that is not equivalent: __contains__ tests VALUES, so every
    column check returns False and the field reads back as None. Applying that
    fix once made `register_pinned_session` appear to store no revision, which
    silently broke patch ownership.
    """

    def test_sqlite_row_membership_tests_values_not_keys(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT 'abc' AS revision").fetchone()
        self.assertIn("revision", row.keys())
        self.assertNotIn("revision", row)  # the trap
        self.assertIn("abc", row)

    def test_a_pinned_session_reads_its_revision_back(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStateStore(Path(directory) / "sessions.sqlite3")
            store.register_pinned_session(
                "sess-1", patch_id="68160", run_id="run-1",
                revision="d" * 40, patchset=4, profile="engineering",
                state="running",
            )
            self.assertEqual(store.get_session("sess-1").revision, "d" * 40)
            listed = store.list_sessions(include_terminal=True)
            self.assertEqual(listed[0].revision, "d" * 40)
            self.assertEqual(listed[0].patchset, 4)


class ResourceCleanupFailureReasonTests(unittest.TestCase):
    """A retried cleanup failure must record its own reason.

    `mark_resource_cleanup` early-returned whenever the state already equalled
    the target, so the second and later `cleanup_failed` writes were dropped and
    the row kept the oldest reason -- including when the controller finally gave
    up. The give-up reason then existed only in the event log, not on the row
    the dashboard renders.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SessionStateStore(Path(self.temporary.name) / "sessions.sqlite3")
        self.store.register_session(
            "sess-1", patch_id="68160", run_id="run-1", profile="engineering",
        )
        self.resource = self.store.register_owned_resource(
            "sess-1", owner_id="owner-1", resource_type="ltvm_vm",
            external_id="co3-sanity",
        )
        # Resources enter cleanup_pending when the session terminalizes.
        self.store.finish_session(
            "sess-1", "failed", failure_code="controller_error",
        )

    def mark_failed(self, summary):
        return self.store.mark_resource_cleanup(
            self.resource.resource_id, succeeded=False, failure_summary=summary,
        )

    def test_a_later_failure_reason_replaces_the_earlier_one(self):
        self.assertEqual(self.mark_failed("attempt 1: timeout").cleanup_failure,
                         "attempt 1: timeout")
        updated = self.mark_failed("gave up after 3 attempts")
        self.assertEqual(updated.cleanup_failure, "gave up after 3 attempts")
        self.assertEqual(updated.state, "cleanup_failed")

    def test_an_identical_repeat_is_still_a_no_op(self):
        first = self.mark_failed("attempt 1: timeout")
        again = self.mark_failed("attempt 1: timeout")
        self.assertEqual(again.cleanup_failure, first.cleanup_failure)

    def test_marking_cleaned_twice_remains_a_no_op(self):
        cleaned = self.store.mark_resource_cleanup(
            self.resource.resource_id, succeeded=True)
        again = self.store.mark_resource_cleanup(
            self.resource.resource_id, succeeded=True)
        self.assertEqual(again.state, "cleaned")
        self.assertEqual(again.cleanup_completed_at, cleaned.cleanup_completed_at)


class PayloadSizeBoundaryTests(unittest.TestCase):
    """The store must accept everything the runner can emit.

    The runner truncates its own payloads above 256 KiB (262,144 bytes); the
    store rejected above 256,000. That 6,144-byte window let the runner emit an
    event the store refused, and ingestion could never advance past that
    cursor -- so a COMPLETED run whose report sat further along was discarded
    on every retry. The error also called 256,000 bytes "256 KiB".
    """

    def test_the_store_limit_is_at_least_the_runner_limit(self):
        from patch_watcher.claude_runner import MAX_EVENT_BYTES
        from patch_watcher.session_state import MAX_JSON_TEXT_BYTES

        self.assertGreaterEqual(MAX_JSON_TEXT_BYTES, MAX_EVENT_BYTES)

    def test_a_payload_at_the_runner_cap_is_storable(self):
        from patch_watcher.claude_runner import MAX_EVENT_BYTES, _bounded_payload

        # Build something the runner would pass through untruncated.
        payload = {"type": "claude_event", "text": "x" * (MAX_EVENT_BYTES - 200)}
        bounded = _bounded_payload(payload)
        self.assertNotIn("truncated", bounded, "fixture must not be pre-truncated")

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStateStore(Path(directory) / "sessions.sqlite3")
            store.register_session(
                "s1", patch_id="68160", run_id="run-1", profile="engineering",
            )
            event = store.append_event("s1", "runner_event", dict(bounded))
            self.assertEqual(event.event_type, "runner_event")

    def test_an_oversized_payload_reports_real_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStateStore(Path(directory) / "sessions.sqlite3")
            store.register_session(
                "s1", patch_id="68160", run_id="run-1", profile="engineering",
            )
            with self.assertRaises(ValueError) as caught:
                store.append_event("s1", "runner_event", {"x": "y" * 400_000})
            message = str(caught.exception)
            self.assertIn("bytes", message)
            self.assertNotIn("KiB", message, "the old message mislabelled the unit")


class EventTypeScopedReadTests(unittest.TestCase):
    """Marker lookups must not scan every runner_event in the session.

    A run accumulates one `runner_event` per Claude stream line -- thousands --
    while the markers the controller looks for number a handful. Four hot-path
    helpers did unfiltered scans, decoding every payload: measured at 127 ms
    per scan for a 20k-event run, several times per tick against a 1.0s poll
    interval, so tick cost grew with history until it stretched past the
    interval and the event-ingest loop began dropping events.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SessionStateStore(Path(self.temporary.name) / "sessions.sqlite3")
        self.store.register_session(
            "s1", patch_id="68160", run_id="run-1", profile="engineering",
        )
        self.store.append_event("s1", "runner-handle", {"handle": {"x": 1}})
        for index in range(50):
            self.store.append_event(
                "s1", "runner_event", {"runner_cursor": index},
                idempotency_key=f"k{index}",
            )
        self.store.append_event("s1", "worker-report-applied", {"runner_cursor": 49})

    def test_a_scoped_read_returns_only_the_requested_types(self):
        events = self.store.list_events("s1", event_types=("runner-handle",))
        self.assertEqual([event.event_type for event in events], ["runner-handle"])

    def test_several_types_can_be_requested_at_once(self):
        events = self.store.list_events(
            "s1", event_types=("runner-handle", "worker-report-applied")
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["runner-handle", "worker-report-applied"],
        )

    def test_ordering_is_preserved_within_a_scoped_read(self):
        events = self.store.list_events("s1", event_types=("runner_event",))
        self.assertEqual(len(events), 50)
        self.assertEqual(
            [event.event_id for event in events],
            sorted(event.event_id for event in events),
        )

    def test_an_empty_type_list_returns_nothing_rather_than_everything(self):
        # Failing open here would silently restore the full scan.
        self.assertEqual(self.store.list_events("s1", event_types=()), [])

    def test_an_unfiltered_read_still_returns_everything(self):
        self.assertEqual(len(self.store.list_events("s1")), 52)

    def test_the_scoped_read_uses_the_type_index(self):
        with self.store._connection() as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM pw_session_event "
                "WHERE session_id = ? AND event_id > ? AND event_type IN (?) "
                "ORDER BY event_id",
                ("s1", 0, "runner-handle"),
            ).fetchall()
        self.assertTrue(
            any("pw_session_event_type_idx" in str(tuple(row)) for row in plan),
            f"the type index is not being used: {[tuple(r) for r in plan]}",
        )
