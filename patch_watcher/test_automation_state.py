import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation_state import (
    AutomationConflict,
    AutomationStateStore,
    BudgetExhausted,
    GlobalAutomationDisabled,
)


START = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


class AutomationStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "automation.sqlite3"
        self.store = AutomationStateStore(self.database)
        self.patch()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def patch(
        self,
        patch_id="68160",
        revision="revision-1",
        patchset=1,
        at=START,
    ):
        return self.store.upsert_patch(
            patch_id,
            gerrit_url=f"https://review.whamcloud.com/c/fs/lustre-release/+/{patch_id}",
            change_number=int(patch_id),
            revision=revision,
            patchset=patchset,
            at=at,
        )

    def policy(
        self,
        mode="approval",
        action_budget=3,
        delivery_budget=2,
        patch_id="68160",
    ):
        return self.store.set_policy(
            patch_id,
            mode=mode,
            action_budget=action_budget,
            delivery_budget=delivery_budget,
            updated_by="patrick",
            at=START + timedelta(seconds=1),
        )

    def trigger(self, suffix="1", patch_id="68160", revision="revision-1"):
        return self.store.create_trigger(
            patch_id,
            revision=revision,
            kind="maloo_failure",
            fingerprint=f"trigger-fingerprint-{suffix}",
            payload={"failure": suffix},
            trigger_id=f"trigger-{suffix}",
            created_at=START + timedelta(seconds=2),
        )

    def make_run(self, suffix="1", mode="approval"):
        self.policy(mode=mode)
        trigger = self.trigger(suffix)
        return self.store.create_run(
            trigger.trigger_id,
            deterministic_key=f"run-key-{suffix}",
            run_id=f"run-{suffix}",
            at=START + timedelta(seconds=3),
        )

    def test_private_wal_database_and_restart_persistence(self):
        policy = self.policy(mode="advise", action_budget=4, delivery_budget=1)
        observation, created = self.store.record_observation(
            "68160",
            revision="revision-1",
            source="gerrit",
            kind="change_updated",
            fingerprint="observation-1",
            payload={"uploader": "developer"},
            observed_at=START + timedelta(minutes=1),
        )
        self.assertTrue(created)
        reopened = AutomationStateStore(self.database)
        self.assertEqual(reopened.get_policy("68160"), policy)
        self.assertEqual(reopened.list_observations("68160"), [observation])
        self.assertEqual(os.stat(self.database).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.database.parent).st_mode & 0o777, 0o700)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_observation_and_trigger_fingerprints_are_idempotent(self):
        first, created = self.store.record_observation(
            "68160",
            revision="revision-1",
            source="maloo",
            kind="test_failure",
            fingerprint="failure-a",
            payload={"test": "sanity"},
            observation_id="observation-a",
        )
        repeated, repeated_created = self.store.record_observation(
            "68160",
            revision="revision-1",
            source="maloo",
            kind="test_failure",
            fingerprint="failure-a",
            payload={"test": "sanity"},
        )
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(first, repeated)
        with self.assertRaises(AutomationConflict):
            self.store.record_observation(
                "68160",
                revision="revision-1",
                source="maloo",
                kind="test_failure",
                fingerprint="failure-a",
                payload={"test": "different"},
            )

        trigger = self.trigger("same")
        self.assertEqual(
            self.store.create_trigger(
                "68160",
                revision="revision-1",
                kind="maloo_failure",
                fingerprint="trigger-fingerprint-same",
                payload={"failure": "same"},
            ),
            trigger,
        )

    def test_trigger_claim_is_atomic_and_restart_reuses_consumer_identity(self):
        trigger = self.trigger("claim")
        barrier = threading.Barrier(2)
        claims = []

        def claim(worker):
            barrier.wait()
            claims.append(
                AutomationStateStore(self.database).claim_next_trigger(worker)
            )

        threads = [
            threading.Thread(target=claim, args=(f"consumer-{index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [item for item in claims if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].trigger_id, trigger.trigger_id)
        self.assertEqual(
            AutomationStateStore(self.database)
            .claim_next_trigger(claimed[0].claimed_by)
            .trigger_id,
            trigger.trigger_id,
        )

    def test_global_execution_defaults_off_is_audited_and_gates_only_automatic(self):
        default = self.store.get_global_automation()
        self.assertFalse(default.enabled)
        automatic = self.make_run(mode="automatic")
        with self.assertRaises(GlobalAutomationDisabled):
            self.store.claim_run(automatic.run_id, "worker")

        enabled = self.store.set_global_automation(
            True,
            changed_by="patrick",
            reason="enable controlled trial",
            at=START + timedelta(minutes=1),
        )
        self.assertTrue(enabled.enabled)
        self.assertEqual(
            self.store.list_global_automation_audit()[0].reason,
            "enable controlled trial",
        )
        claimed = self.store.claim_run(automatic.run_id, "worker")
        self.assertEqual(claimed.status, "executing")

        self.store.finish_run(automatic.run_id, "succeeded")
        self.patch("68161")
        self.policy(mode="approval", patch_id="68161")
        approval_trigger = self.store.create_trigger(
            "68161",
            revision="revision-1",
            kind="manual",
            fingerprint="approval-trigger",
            payload={},
        )
        approval_run = self.store.create_run(
            approval_trigger.trigger_id,
            deterministic_key="approval-run-key",
        )
        self.store.set_global_automation(
            False, changed_by="patrick", reason="stop automatic execution"
        )
        self.assertEqual(
            self.store.claim_run(approval_run.run_id, "approval-worker").status,
            "executing",
        )

    def test_approval_action_requires_exact_durable_revision_and_policy(self):
        run = self.make_run(mode="approval")
        self.store.claim_run(run.run_id, "controller")
        action = self.store.plan_action(
            run.run_id,
            action_type="maloo_retest",
            request={"test": "sanity"},
            idempotency_key="retest-action",
        )
        with self.assertRaisesRegex(AutomationConflict, "requires explicit approval"):
            self.store.claim_next_action(run.run_id, "executor")
        with self.assertRaisesRegex(AutomationConflict, "revision is stale"):
            self.store.approve_action(
                action.action_id,
                approved_by="patrick",
                expected_revision="wrong",
                expected_policy_mode="approval",
            )
        approval = self.store.approve_action(
            action.action_id,
            approved_by="patrick",
            expected_revision="revision-1",
            expected_policy_mode="approval",
            at=START + timedelta(minutes=2),
        )
        self.assertEqual(approval.policy_snapshot["mode"], "approval")
        self.assertEqual(self.store.get_action_approval(action.action_id), approval)
        claimed = self.store.claim_next_action(run.run_id, "executor")
        self.assertEqual(claimed.status, "executing")
        self.assertEqual(claimed.claimed_by, "executor")

    def test_approval_is_rejected_if_policy_changed_after_run_snapshot(self):
        run = self.make_run(mode="approval")
        self.store.claim_run(run.run_id, "controller")
        action = self.store.plan_action(
            run.run_id,
            action_type="maloo_retest",
            request={},
            idempotency_key="policy-change-action",
        )
        self.store.set_policy(
            "68160",
            mode="approval",
            action_budget=9,
            delivery_budget=2,
            updated_by="patrick",
            at=START + timedelta(minutes=5),
        )
        with self.assertRaisesRegex(AutomationConflict, "policy no longer matches"):
            self.store.approve_action(
                action.action_id,
                approved_by="patrick",
                expected_revision="revision-1",
                expected_policy_mode="approval",
            )

    def test_advise_mode_can_record_plan_but_cannot_execute_action(self):
        run = self.make_run(mode="advise")
        self.store.claim_run(run.run_id, "advisor")
        self.store.plan_action(
            run.run_id,
            action_type="recommend_retest",
            request={},
            idempotency_key="advice-1",
        )
        with self.assertRaisesRegex(AutomationConflict, "advise-mode"):
            self.store.claim_next_action(run.run_id, "executor")

    def test_one_active_run_per_patch_is_race_safe_and_deterministic(self):
        self.policy()
        triggers = [self.trigger(str(index)) for index in range(2)]
        barrier = threading.Barrier(2)
        successes = []
        conflicts = []

        def create(index):
            barrier.wait()
            try:
                successes.append(
                    AutomationStateStore(self.database).create_run(
                        triggers[index].trigger_id,
                        deterministic_key=f"race-key-{index}",
                        run_id=f"race-run-{index}",
                    )
                )
            except AutomationConflict as exc:
                conflicts.append(exc)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        winner = successes[0]
        self.assertEqual(
            self.store.create_run(
                winner.trigger_id,
                deterministic_key=winner.deterministic_key,
                run_id="ignored-idempotent-id",
            ).run_id,
            winner.run_id,
        )

    def test_atomic_action_claims_and_independent_budgets(self):
        self.policy(mode="automatic", action_budget=1, delivery_budget=1)
        self.store.set_global_automation(
            True, changed_by="patrick", reason="test", at=START
        )
        trigger = self.trigger()
        run = self.store.create_run(
            trigger.trigger_id, deterministic_key="budget-run", run_id="budget-run"
        )
        self.store.claim_run(run.run_id, "controller")
        action = self.store.plan_action(
            run.run_id,
            action_type="maloo_retest",
            request={},
            idempotency_key="budget-action",
        )
        self.assertEqual(
            self.store.plan_action(
                run.run_id,
                action_type="maloo_retest",
                request={},
                idempotency_key="budget-action",
            ).action_id,
            action.action_id,
        )
        with self.assertRaises(BudgetExhausted):
            self.store.plan_action(
                run.run_id,
                action_type="another_action",
                request={},
                idempotency_key="budget-action-2",
            )
        delivery = self.store.plan_action(
            run.run_id,
            action_type="status_email",
            request={},
            idempotency_key="budget-delivery",
            budget_bucket="delivery",
        )
        self.assertEqual(delivery.budget_bucket, "delivery")

        barrier = threading.Barrier(2)
        claims = []

        def claim(worker):
            barrier.wait()
            claims.append(
                AutomationStateStore(self.database).claim_next_action(
                    run.run_id, worker
                )
            )

        threads = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        executing = [claim for claim in claims if claim is not None]
        self.assertEqual(len(executing), 2)
        self.assertEqual(
            {claim.action_id for claim in executing},
            {action.action_id, delivery.action_id},
        )

    def test_action_budget_cannot_be_overspent_by_concurrent_planners(self):
        self.policy(mode="automatic", action_budget=1, delivery_budget=0)
        trigger = self.trigger()
        run = self.store.create_run(
            trigger.trigger_id,
            deterministic_key="concurrent-budget-run",
            run_id="concurrent-budget-run",
        )
        barrier = threading.Barrier(2)
        planned = []
        exhausted = []

        def plan(index):
            barrier.wait()
            try:
                planned.append(
                    AutomationStateStore(self.database).plan_action(
                        run.run_id,
                        action_type="retest",
                        request={"index": index},
                        idempotency_key=f"concurrent-action-{index}",
                    )
                )
            except BudgetExhausted as exc:
                exhausted.append(exc)

        threads = [
            threading.Thread(target=plan, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(planned), 1)
        self.assertEqual(len(exhausted), 1)
        self.assertEqual(self.store.get_run(run.run_id).action_count, 1)

    def test_revision_change_stales_run_trigger_and_uncertain_action(self):
        self.policy(mode="automatic")
        self.store.set_global_automation(True, changed_by="patrick", reason="test")
        old_unclaimed = self.trigger("unclaimed")
        active_trigger = self.trigger("active")
        run = self.store.create_run(
            active_trigger.trigger_id,
            deterministic_key="stale-run-key",
            run_id="stale-run",
        )
        self.store.claim_run(run.run_id, "controller")
        action = self.store.plan_action(
            run.run_id,
            action_type="retest",
            request={},
            idempotency_key="stale-action",
        )
        self.store.claim_next_action(run.run_id, "executor")

        self.patch(
            revision="revision-2",
            patchset=2,
            at=START + timedelta(minutes=10),
        )
        self.assertEqual(self.store.get_run(run.run_id).status, "stale")
        self.assertEqual(self.store.get_action(action.action_id).status, "ambiguous")
        self.assertEqual(self.store.get_trigger(old_unclaimed.trigger_id).state, "stale")
        stale_trigger = self.store.create_trigger(
            "68160",
            revision="revision-1",
            kind="old_event",
            fingerprint="old-event-after-update",
            payload={},
        )
        self.assertEqual(stale_trigger.state, "stale")

    def test_crash_recovery_marks_executing_run_and_action_ambiguous(self):
        self.policy(mode="automatic")
        self.store.set_global_automation(True, changed_by="patrick", reason="test")
        trigger = self.trigger()
        run = self.store.create_run(
            trigger.trigger_id, deterministic_key="recovery-run", run_id="recovery-run"
        )
        self.store.claim_run(run.run_id, "controller", at=START + timedelta(minutes=1))
        action = self.store.plan_action(
            run.run_id,
            action_type="retest",
            request={},
            idempotency_key="recovery-action",
        )
        self.store.claim_next_action(
            run.run_id, "executor", at=START + timedelta(minutes=2)
        )
        reopened = AutomationStateStore(self.database)
        run_ids, action_ids = reopened.recover_executing_as_ambiguous(
            before=START + timedelta(minutes=3),
            at=START + timedelta(minutes=4),
        )
        self.assertEqual(run_ids, [run.run_id])
        self.assertEqual(action_ids, [action.action_id])
        self.assertEqual(reopened.get_run(run.run_id).status, "ambiguous")
        self.assertEqual(reopened.get_action(action.action_id).status, "ambiguous")

    def test_timeline_is_append_only_and_idempotent(self):
        run = self.make_run(mode="advise")
        event = self.store.append_timeline(
            run.run_id,
            "decision",
            {"recommendation": "retest"},
            idempotency_key="timeline-1",
            at=START + timedelta(minutes=1),
        )
        self.assertEqual(
            self.store.append_timeline(
                run.run_id,
                "decision",
                {"recommendation": "retest"},
                idempotency_key="timeline-1",
            ),
            event,
        )
        with sqlite3.connect(self.database) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("UPDATE pw_automation_timeline SET event_type='bad'")

    def test_schema_migrates_v1_data_to_delivery_budgets_and_approvals(self):
        self.database.unlink()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE pw_automation_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                )
                """
            )
            for statement in AutomationStateStore._MIGRATIONS[1]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO pw_automation_schema(singleton, version) VALUES (1, 1)"
            )
            epoch = START.timestamp()
            connection.execute(
                """
                INSERT INTO pw_automation_patch(
                    patch_id, gerrit_url, change_number, current_revision,
                    current_patchset, status, created_at, updated_at
                ) VALUES ('legacy', 'https://review/1', 1, 'old', 1, 'open', ?, ?)
                """,
                (epoch, epoch),
            )
            connection.execute(
                """
                INSERT INTO pw_automation_policy(
                    patch_id, mode, action_budget, updated_by, updated_at
                ) VALUES ('legacy', 'approval', 7, 'patrick', ?)
                """,
                (epoch,),
            )

        migrated = AutomationStateStore(self.database)
        self.assertEqual(migrated.get_policy("legacy").action_budget, 7)
        self.assertEqual(migrated.get_policy("legacy").delivery_budget, 0)
        with sqlite3.connect(self.database) as connection:
            version = connection.execute(
                "SELECT version FROM pw_automation_schema WHERE singleton=1"
            ).fetchone()[0]
            approval_table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='pw_automation_action_approval'
                """
            ).fetchone()
        self.assertEqual(version, migrated.SCHEMA_VERSION)
        self.assertIsNotNone(approval_table)


if __name__ == "__main__":
    unittest.main()
