import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

from patch_watcher.engineering_state import (
    ArtifactMetadata,
    EngineeringConflict,
    EngineeringStateStore,
    ExecutionManifest,
    SafeCommand,
    resolve_confined_path,
)

REVISION = "d" * 40


class EngineeringStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checkouts = self.root / "checkouts"
        self.checkouts.mkdir()
        self.database = self.root / "engineering.sqlite3"
        self.store = EngineeringStateStore(self.database, checkout_root=self.checkouts)

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self, suffix="1", **updates):
        values = {
            "allocation_id": f"checkout-{suffix}",
            "run_id": f"run-{suffix}",
            "session_id": f"session-{suffix}",
            "patch_id": f"6816{suffix}",
            "patchset": 4,
            "revision_sha": REVISION,
            "repository_url": "https://review.whamcloud.com/fs/lustre-release",
            "base_branch": "master",
            "checkout_path": f"run-{suffix}",
            "owner_id": f"patch-watcher:run-{suffix}",
        }
        values.update(updates)
        return self.store.plan_checkout(**values)

    def make_active(self, suffix="1", **updates):
        allocation = self.plan(suffix, **updates)
        allocation.checkout_path.mkdir()
        self.store.mark_allocated(
            allocation.allocation_id,
            run_id=allocation.run_id,
            owner_id=allocation.owner_id,
            revision_sha=allocation.revision_sha,
        )
        (allocation.checkout_path / ".git").mkdir()
        return self.store.activate_checkout(
            allocation.allocation_id,
            run_id=allocation.run_id,
            owner_id=allocation.owner_id,
            revision_sha=allocation.revision_sha,
            observed_revision=allocation.revision_sha,
            initial_dirty=False,
        )

    def make_validation(self, suffix="1", *, admission_state="awaiting_approval"):
        allocation = self.make_active(suffix)
        manifest = ExecutionManifest(
            f"manifest-{suffix}", allocation.run_id, allocation.revision_sha,
            (SafeCommand("planned", ["make", "check"]),),
        )
        self.store.save_manifest(allocation.allocation_id, manifest)
        execution = self.store.create_validation_execution(
            allocation.allocation_id,
            execution_id=f"validation-{suffix}",
            idempotency_key=f"validation-request-{suffix}",
            requested_by="requester",
            admission_state=admission_state,
            disabled_reason="feature disabled" if admission_state == "disabled" else None,
            manifest_id=manifest.manifest_id,
        )
        return allocation, manifest, execution

    def approve_and_claim(self, suffix="1", *, now=None):
        allocation, manifest, execution = self.make_validation(suffix)
        execution = self.store.approve_validation_execution(
            execution.execution_id,
            expected_revision=allocation.revision_sha,
            expected_owner_id=allocation.owner_id,
            approved_by="approver",
            now=now,
        )
        attempt = self.store.claim_validation_attempt(
            execution.execution_id,
            attempt_id=f"attempt-{suffix}-1",
            worker_id=f"worker-{suffix}",
            idempotency_key=f"attempt-request-{suffix}-1",
            expected_revision=allocation.revision_sha,
            expected_owner_id=allocation.owner_id,
            now=now,
        )
        return allocation, manifest, execution, attempt

    def test_checkout_root_rejects_traversal_outside_and_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.checkouts / "linked").symlink_to(outside, target_is_directory=True)
        for path in ("../outside", outside / "run", self.checkouts, "nested/run", "linked"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.plan(str(path).replace("/", "-"), checkout_path=path)

    def test_run_owner_and_path_are_exclusive_even_after_restart(self):
        first = self.plan()
        duplicates = (
            {"run_id": first.run_id},
            {"owner_id": first.owner_id},
            {"checkout_path": first.checkout_path},
        )
        for index, update in enumerate(duplicates, 2):
            with self.subTest(update=update), self.assertRaises(EngineeringConflict):
                self.plan(str(index), **update)
        restarted = EngineeringStateStore(self.database, checkout_root=self.checkouts)
        self.assertEqual(restarted.get_checkout(first.allocation_id).owner_id, first.owner_id)

    def test_only_independent_clean_exact_revision_checkout_becomes_active(self):
        allocation = self.plan()
        allocation.checkout_path.mkdir()
        self.store.mark_allocated(
            allocation.allocation_id,
            run_id=allocation.run_id,
            owner_id=allocation.owner_id,
            revision_sha=REVISION,
        )
        (allocation.checkout_path / ".git").write_text("gitdir: elsewhere")
        with self.assertRaisesRegex(EngineeringConflict, "independent full clone"):
            self.store.activate_checkout(
                allocation.allocation_id,
                run_id=allocation.run_id,
                owner_id=allocation.owner_id,
                revision_sha=REVISION,
                observed_revision=REVISION,
                initial_dirty=False,
            )
        (allocation.checkout_path / ".git").unlink()
        (allocation.checkout_path / ".git").mkdir()
        with self.assertRaisesRegex(EngineeringConflict, "stale"):
            self.store.activate_checkout(
                allocation.allocation_id,
                run_id=allocation.run_id,
                owner_id=allocation.owner_id,
                revision_sha=REVISION,
                observed_revision="e" * 40,
                initial_dirty=False,
            )
        with self.assertRaisesRegex(EngineeringConflict, "dirty"):
            self.store.activate_checkout(
                allocation.allocation_id,
                run_id=allocation.run_id,
                owner_id=allocation.owner_id,
                revision_sha=REVISION,
                observed_revision=REVISION,
                initial_dirty=True,
            )

    def test_cancellation_is_durable_and_release_cannot_cross_runs(self):
        allocation = self.make_active()
        pending = self.store.request_cleanup(
            allocation.allocation_id,
            run_id=allocation.run_id,
            owner_id=allocation.owner_id,
            revision_sha=REVISION,
            reason="run_cancelled",
        )
        self.assertEqual(pending.state, "cleanup_pending")
        restarted = EngineeringStateStore(self.database, checkout_root=self.checkouts)
        decisions = restarted.reconcile_after_restart({})
        self.assertEqual(decisions[0].action, "resume_cleanup")
        with self.assertRaisesRegex(EngineeringConflict, "ownership"):
            restarted.release_checkout(
                allocation.allocation_id,
                run_id="run-other",
                owner_id=allocation.owner_id,
                revision_sha=REVISION,
            )
        with self.assertRaisesRegex(EngineeringConflict, "still exists"):
            restarted.release_checkout(
                allocation.allocation_id,
                run_id=allocation.run_id,
                owner_id=allocation.owner_id,
                revision_sha=REVISION,
            )
        (allocation.checkout_path / ".git").rmdir()
        allocation.checkout_path.rmdir()
        released = restarted.release_checkout(
            allocation.allocation_id,
            run_id=allocation.run_id,
            owner_id=allocation.owner_id,
            revision_sha=REVISION,
        )
        self.assertEqual(released.state, "released")

    def test_restart_retains_exact_binding_and_cleans_orphan_and_stale_runs(self):
        exact = self.plan("1")
        orphan = self.plan("2")
        stale = self.plan("3", revision_sha="c" * 40)
        decisions = self.store.reconcile_after_restart({
            exact.run_id: exact.revision_sha,
            stale.run_id: "b" * 40,
        })
        by_run = {decision.run_id: decision for decision in decisions}
        self.assertEqual(by_run[exact.run_id].action, "retain")
        self.assertEqual(by_run[orphan.run_id].reason, "restart_orphaned")
        self.assertEqual(by_run[stale.run_id].reason, "restart_stale_revision")
        self.assertEqual(self.store.get_checkout(exact.allocation_id).state, "planned")
        self.assertEqual(self.store.get_checkout(orphan.allocation_id).state, "cleanup_pending")
        self.assertEqual(self.store.get_checkout(stale.allocation_id).state, "cleanup_pending")

    def test_safe_command_is_immutable_exec_argv_with_confined_cwd_and_env(self):
        command = SafeCommand(
            "unit-tests", ["pytest", "-q"], cwd="src/tests", env={"CI": "1"}
        )
        self.assertEqual(command.argv, ("pytest", "-q"))
        self.assertEqual(command.env, (("CI", "1"),))
        with self.assertRaises(FrozenInstanceError):
            command.cwd = "other"
        invalid = (
            lambda: SafeCommand("x", "pytest -q"),
            lambda: SafeCommand("x", ["pytest", 1]),
            lambda: SafeCommand("x", ["bash", "-c", "pytest"]),
            lambda: SafeCommand("x", ["pytest"], cwd="../other"),
            lambda: SafeCommand("x", ["pytest"], env={"PATH": "/tmp/bin"}),
            lambda: SafeCommand("x", ["pytest"], execution_target="guest\nother"),
            lambda: SafeCommand("x", ["pytest"], evidence_role="probably-test"),
        )
        for factory in invalid:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_confined_path_resolution_rejects_symlink_escape(self):
        checkout = self.checkouts / "run-1"
        checkout.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (checkout / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            resolve_confined_path(checkout, "escape")

    def test_manifest_is_revision_bound_durable_and_immutable(self):
        allocation = self.plan()
        manifest = ExecutionManifest(
            "manifest-1", allocation.run_id, REVISION,
            (SafeCommand("build", ["make", "-j2"]),),
        )
        digest = self.store.save_manifest(allocation.allocation_id, manifest)
        self.assertEqual(digest, manifest.digest)
        self.assertEqual(self.store.save_manifest(allocation.allocation_id, manifest), digest)
        self.assertEqual(self.store.get_manifest(allocation.run_id), manifest)
        stale = ExecutionManifest(
            "manifest-stale", allocation.run_id, "e" * 40,
            (SafeCommand("build", ["make"]),),
        )
        with self.assertRaisesRegex(EngineeringConflict, "does not match"):
            self.store.save_manifest(allocation.allocation_id, stale)
        with sqlite3.connect(self.database) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE pw_execution_manifest SET manifest_json = '{}' WHERE manifest_id = 'manifest-1'"
                )

    def test_artifact_metadata_bounds_paths_digests_and_revision(self):
        allocation = self.make_active()
        invalid = (
            {"relative_path": "../secret"},
            {"relative_path": "..\\secret"},
            {"relative_path": "/absolute"},
            {"sha256": "abc"},
            {"size_bytes": 2_147_483_649},
        )
        base = {
            "artifact_id": "artifact-1",
            "run_id": allocation.run_id,
            "revision_sha": REVISION,
            "kind": "patch",
            "relative_path": "patches/proposed.patch",
            "sha256": "a" * 64,
            "size_bytes": 123,
            "media_type": "text/x-diff",
        }
        for update in invalid:
            values = dict(base)
            values.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                ArtifactMetadata(**values)
        artifact = ArtifactMetadata(**base)
        self.store.register_artifact(allocation.allocation_id, artifact)
        self.assertEqual(self.store.list_artifacts(allocation.run_id), (artifact,))
        stale = ArtifactMetadata(**{**base, "artifact_id": "artifact-2", "revision_sha": "e" * 40})
        with self.assertRaisesRegex(EngineeringConflict, "does not match"):
            self.store.register_artifact(allocation.allocation_id, stale)

    def test_read_projections_are_bounded_and_filterable(self):
        first = self.plan("1")
        second = self.plan("2")
        self.store.request_cleanup(
            second.allocation_id,
            run_id=second.run_id,
            owner_id=second.owner_id,
            revision_sha=second.revision_sha,
            reason="cancelled",
        )
        self.assertEqual(self.store.get_allocation_by_run(first.run_id), first)
        self.assertIsNone(self.store.get_allocation_by_run("missing-run"))
        pending = self.store.list_allocations(states={"cleanup_pending"}, limit=1)
        self.assertEqual([item.allocation_id for item in pending], [second.allocation_id])
        with self.assertRaises(ValueError):
            self.store.list_allocations(limit=501)

    def test_memory_store_remains_available_across_read_connections(self):
        store = EngineeringStateStore(":memory:", checkout_root=self.checkouts)
        allocation = store.plan_checkout(
            allocation_id="memory-checkout",
            run_id="memory-run",
            session_id="memory-session",
            patch_id="68160",
            patchset=4,
            revision_sha=REVISION,
            repository_url="https://review.whamcloud.com/fs/lustre-release",
            base_branch="master",
            checkout_path="memory-run",
        )
        self.assertEqual(store.get_checkout(allocation.allocation_id), allocation)

    def test_validation_capability_requires_exact_approval_and_replays_after_approval(self):
        allocation, manifest, execution = self.make_validation()
        with self.assertRaisesRegex(EngineeringConflict, "exact revision and owner"):
            self.store.approve_validation_execution(
                execution.execution_id,
                expected_revision="e" * 40,
                expected_owner_id=allocation.owner_id,
                approved_by="approver",
            )
        approved = self.store.approve_validation_execution(
            execution.execution_id,
            expected_revision=allocation.revision_sha,
            expected_owner_id=allocation.owner_id,
            approved_by="approver",
        )
        self.assertEqual(approved.admission_state, "approved")
        replay = self.store.create_validation_execution(
            allocation.allocation_id,
            idempotency_key="validation-request-1",
            requested_by="requester",
            manifest_id=manifest.manifest_id,
        )
        self.assertEqual(replay, approved)
        with self.assertRaisesRegex(EngineeringConflict, "different actor"):
            self.store.approve_validation_execution(
                execution.execution_id,
                expected_revision=allocation.revision_sha,
                expected_owner_id=allocation.owner_id,
                approved_by="somebody-else",
            )

        disabled_allocation, _, disabled = self.make_validation(
            "2", admission_state="disabled"
        )
        self.assertEqual(disabled.disabled_by, "requester")
        self.assertIsNotNone(disabled.disabled_at)
        with self.assertRaisesRegex(EngineeringConflict, "cannot be approved"):
            self.store.approve_validation_execution(
                disabled.execution_id,
                expected_revision=disabled_allocation.revision_sha,
                expected_owner_id=disabled_allocation.owner_id,
                approved_by="approver",
            )
        with self.assertRaisesRegex(EngineeringConflict, "not approved"):
            self.store.claim_validation_attempt(
                disabled.execution_id,
                worker_id="worker-2",
                idempotency_key="disabled-attempt",
                expected_revision=disabled_allocation.revision_sha,
                expected_owner_id=disabled_allocation.owner_id,
            )

    def test_attempt_succeeds_on_report_evidence_without_step_results(self):
        """A run's evidence is its report and diff, not per-command rows.

        The broker that recorded one row per guest command is gone, so an
        attempt that demanded such rows could never succeed by any path.
        """

        allocation, _, execution, attempt = self.approve_and_claim()
        running = self.store.mark_validation_attempt_running(
            attempt.attempt_id, worker_id=attempt.worker_id
        )
        self.assertEqual(
            self.store.get_validation_execution_by_run(
                allocation.run_id
            ).execution_id,
            execution.execution_id,
        )
        self.assertEqual(
            self.store.get_validation_execution(execution.execution_id).state,
            "running",
        )
        self.assertEqual(
            [
                (item.state, item.revision_sha)
                for item in self.store.list_validation_attempts(
                    execution.execution_id
                )
            ],
            [("running", allocation.revision_sha)],
        )
        finished = self.store.finish_validation_attempt(
            running.attempt_id,
            worker_id=running.worker_id,
            state="succeeded",
            summary="sanity passed in co3-sanity",
        )
        self.assertEqual(finished.state, "succeeded")
        self.assertIsNone(finished.failure_code)
        self.assertEqual(
            self.store.get_validation_execution(execution.execution_id).state,
            "succeeded",
        )
        self.assertEqual(
            [
                item.state
                for item in self.store.list_validation_attempts(
                    execution.execution_id
                )
            ],
            ["succeeded"],
        )

    def test_finished_attempt_result_is_immutable_and_durable(self):
        _, _, _, attempt = self.approve_and_claim()
        running = self.store.mark_validation_attempt_running(
            attempt.attempt_id, worker_id=attempt.worker_id
        )
        self.store.finish_validation_attempt(
            running.attempt_id,
            worker_id=running.worker_id,
            state="failed",
            summary="the reported guest validation failed",
            failure_code="guest_validation_failed",
        )
        replay = self.store.finish_validation_attempt(
            running.attempt_id,
            worker_id=running.worker_id,
            state="failed",
            summary="the reported guest validation failed",
            failure_code="guest_validation_failed",
        )
        self.assertEqual(replay.state, "failed")
        with self.assertRaisesRegex(EngineeringConflict, "immutable"):
            self.store.finish_validation_attempt(
                running.attempt_id,
                worker_id=running.worker_id,
                state="succeeded",
                summary="rewriting history",
            )
        with sqlite3.connect(self.database) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "durable"):
                connection.execute(
                    "DELETE FROM pw_validation_attempt WHERE attempt_id = ?",
                    (running.attempt_id,),
                )

    def test_attempt_claim_is_exact_owner_scoped_and_requires_active_checkout(self):
        allocation, _, execution = self.make_validation()
        self.store.approve_validation_execution(
            execution.execution_id,
            expected_revision=allocation.revision_sha,
            expected_owner_id=allocation.owner_id,
            approved_by="approver",
        )
        with self.assertRaisesRegex(EngineeringConflict, "exact revision and owner"):
            self.store.claim_validation_attempt(
                execution.execution_id,
                worker_id="worker",
                idempotency_key="wrong-owner-attempt",
                expected_revision=allocation.revision_sha,
                expected_owner_id="other-owner",
            )
        self.store.request_cleanup(
            allocation.allocation_id,
            run_id=allocation.run_id,
            owner_id=allocation.owner_id,
            revision_sha=allocation.revision_sha,
            reason="cancelled",
        )
        with self.assertRaisesRegex(EngineeringConflict, "active owner session"):
            self.store.claim_validation_attempt(
                execution.execution_id,
                worker_id="worker",
                idempotency_key="inactive-checkout-attempt",
                expected_revision=allocation.revision_sha,
                expected_owner_id=allocation.owner_id,
            )

    def test_validation_restart_reconciliation_never_replays_running_commands(self):
        _, _, first_execution, first = self.approve_and_claim("1")
        _, _, second_execution, second = self.approve_and_claim("2")
        second = self.store.mark_validation_attempt_running(
            second.attempt_id, worker_id=second.worker_id
        )
        third_allocation, _, third_execution, third = self.approve_and_claim("3")
        third = self.store.mark_validation_attempt_running(
            third.attempt_id, worker_id=third.worker_id
        )
        self.store.disable_validation_execution(
            third_execution.execution_id,
            expected_revision=third_allocation.revision_sha,
            expected_owner_id=third_allocation.owner_id,
            disabled_by="operator",
            reason="stop requested",
        )

        decisions = self.store.reconcile_validation_after_restart(
            {third.attempt_id: third.worker_id}
        )
        by_attempt = {decision.attempt_id: decision for decision in decisions}
        self.assertEqual(by_attempt[first.attempt_id].action, "retry_safe")
        self.assertEqual(
            self.store.get_validation_execution(first_execution.execution_id).state,
            "planned",
        )
        self.assertEqual(by_attempt[second.attempt_id].action, "manual_reconciliation")
        self.assertEqual(
            self.store.get_validation_execution(second_execution.execution_id).state,
            "ambiguous",
        )
        self.assertEqual(by_attempt[third.attempt_id].action, "stop_required")
        repeated = self.store.reconcile_validation_after_restart(
            {third.attempt_id: third.worker_id}
        )
        self.assertEqual([item.attempt_id for item in repeated], [third.attempt_id])

    def test_capacity_exhaustion_cooldown_is_durable_and_blocks_reclaim(self):
        started = datetime(2026, 9, 1, tzinfo=UTC)
        allocation, _, execution, attempt = self.approve_and_claim("1", now=started)
        attempt = self.store.mark_validation_attempt_running(
            attempt.attempt_id, worker_id=attempt.worker_id, now=started
        )
        # The exhaustion is reported by the run itself; the cooldown it writes
        # was unreachable while a matching step row was required.
        self.store.finish_validation_attempt(
            attempt.attempt_id,
            worker_id=attempt.worker_id,
            state="resource_exhausted",
            failure_code="guest_memory_exhausted",
            summary="guest memory exhausted",
            now=started + timedelta(seconds=1),
        )
        cooldown = self.store.get_capacity_cooldown(allocation.patch_id)
        self.assertEqual(cooldown.consecutive_exhaustions, 1)
        self.assertEqual(cooldown.total_exhaustions, 1)
        self.assertEqual(cooldown.not_before, started + timedelta(seconds=901))
        with self.assertRaisesRegex(EngineeringConflict, "cooldown"):
            self.store.claim_validation_attempt(
                execution.execution_id,
                worker_id="worker-retry",
                idempotency_key="reclaim-during-cooldown",
                expected_revision=allocation.revision_sha,
                expected_owner_id=allocation.owner_id,
                now=started + timedelta(seconds=2),
            )

    def test_validation_reads_are_bounded_and_schema_is_current(self):
        self.make_validation("1")
        self.make_validation("2", admission_state="disabled")
        awaiting = self.store.list_validation_executions(
            admission_states={"awaiting_approval"}, limit=1
        )
        self.assertEqual(len(awaiting), 1)
        with self.assertRaises(ValueError):
            self.store.list_validation_executions(limit=501)
        with sqlite3.connect(self.database) as connection:
            version = connection.execute(
                "SELECT version FROM pw_engineering_schema WHERE singleton = 1"
            ).fetchone()[0]
        self.assertEqual(version, self.store.SCHEMA_VERSION)

    def test_version_one_database_migrates_validation_schema_on_restart(self):
        with sqlite3.connect(self.database) as connection:
            for table in (
                "pw_validation_attempt",
                "pw_validation_capacity_cooldown",
                "pw_validation_execution",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute(
                "UPDATE pw_engineering_schema SET version = 1 WHERE singleton = 1"
            )
        restarted = EngineeringStateStore(
            self.database, checkout_root=self.checkouts
        )
        with sqlite3.connect(self.database) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(restarted.SCHEMA_VERSION, 4)
        self.assertIn("pw_validation_execution", tables)
        self.assertIn("pw_validation_attempt", tables)
        self.assertIn("pw_validation_capacity_cooldown", tables)
        # The retired per-command ledger is not recreated for a fresh or
        # migrated database, and the version-3 step is a no-op that still
        # stamps the current schema version.
        self.assertNotIn("pw_validation_step_result", tables)
        self.assertNotIn("pw_validation_command_claim", tables)
        self.assertNotIn("pw_validation_retry_grant", tables)
        with sqlite3.connect(self.database) as connection:
            version = connection.execute(
                "SELECT version FROM pw_engineering_schema WHERE singleton = 1"
            ).fetchone()[0]
        self.assertEqual(version, 4)

    def test_database_carrying_the_retired_ledger_tables_still_opens(self):
        """Retiring the tables must not break a database that already has them."""

        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE pw_validation_step_result (
                    attempt_id TEXT NOT NULL, step_id TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, step_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO pw_validation_step_result VALUES ('attempt-1', 'step-1')"
            )
        reopened = EngineeringStateStore(self.database, checkout_root=self.checkouts)
        self.assertEqual(reopened.SCHEMA_VERSION, 4)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM pw_validation_step_result"
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()


class PooledCheckoutReuseTests(unittest.TestCase):
    """A pool checkout must be allocatable again after its run finishes.

    The original schema declared `checkout_path TEXT NOT NULL UNIQUE`, which is
    right for a per-run clone and fatal for a shared pool tree: the second run
    handed $CO/N could never allocate, terminalized as a controller error,
    released the index, and the next run failed identically -- a permanent loop
    that survived restarts.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.clone_root = base / "engineering-checkouts"
        self.clone_root.mkdir()
        self.pool_root = base / "co"
        (self.pool_root / "1" / ".git").mkdir(parents=True)
        self.store = EngineeringStateStore(
            base / "eng.sqlite3",
            checkout_root=self.clone_root,
            pool_root=self.pool_root,
        )

    def allocate(self, run_id, owner):
        return self.store.plan_checkout(
            run_id=run_id, session_id=f"sess-{run_id}", patch_id="68160",
            patchset=4, revision_sha="a" * 40,
            repository_url="https://review.whamcloud.com/fs/lustre-release",
            base_branch="refs/changes/60/68160/4",
            checkout_path=self.pool_root / "1", owner_id=owner,
        )

    def finish(self, allocation, run_id, owner):
        self.store.mark_allocated(
            allocation.allocation_id, run_id=run_id, owner_id=owner,
            revision_sha="a" * 40,
        )
        self.store.activate_checkout(
            allocation.allocation_id, run_id=run_id, owner_id=owner,
            revision_sha="a" * 40, observed_revision="a" * 40, initial_dirty=False,
        )
        self.store.request_cleanup(
            allocation.allocation_id, run_id=run_id, owner_id=owner,
            revision_sha="a" * 40, reason="run finished",
        )
        return self.store.release_checkout(
            allocation.allocation_id, run_id=run_id, owner_id=owner,
            revision_sha="a" * 40,
        )

    def test_a_released_pool_checkout_can_be_allocated_again(self):
        first = self.allocate("run-1", "owner-1")
        released = self.finish(first, "run-1", "owner-1")
        self.assertEqual(released.state, "released")
        # The whole point: a second run gets the same tree.
        second = self.allocate("run-2", "owner-2")
        self.assertEqual(second.checkout_path, self.pool_root / "1")

    def test_two_live_allocations_cannot_share_one_checkout(self):
        self.allocate("run-1", "owner-1")
        with self.assertRaises(EngineeringConflict):
            self.allocate("run-2", "owner-2")

    def test_releasing_a_pool_tree_does_not_require_it_to_be_deleted(self):
        # A per-run clone is deleted on cleanup; a pool tree is reset in place
        # and must still exist afterwards.
        first = self.allocate("run-1", "owner-1")
        self.finish(first, "run-1", "owner-1")
        self.assertTrue((self.pool_root / "1").is_dir())


class PopulatedMigrationTests(unittest.TestCase):
    """Migration 4 must survive a database that has real data in it.

    It rebuilds `pw_checkout_allocation` via create/copy/drop/rename with
    `PRAGMA foreign_keys = ON`. DROP TABLE does an implicit DELETE, and four
    tables reference that allocation id -- and every `plan_checkout()` writes a
    `pw_checkout_event` child row. So ANY database that had ever planned one
    checkout could not be opened: the store raised, the controller raised, and
    the dashboard would not start. A fresh database has no rows, which is
    exactly why the suite could not see it.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "co"
        (self.root / "1").mkdir(parents=True)
        self.database = self.base / "engineering.sqlite3"

    def store(self):
        return EngineeringStateStore(self.database, checkout_root=self.root)

    def populate(self):
        return self.store().plan_checkout(
            run_id="run-1", session_id="s1", patch_id="68160", patchset=4,
            revision_sha="a" * 40,
            repository_url="https://review.whamcloud.com/fs/lustre-release",
            base_branch="refs/changes/60/68160/4",
            checkout_path=self.root / "1", owner_id="owner-1",
        )

    def rewind_to(self, version):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE pw_engineering_schema SET version = ?", (version,)
            )

    def test_a_populated_database_migrates_without_losing_rows(self):
        allocation = self.populate()
        with sqlite3.connect(self.database) as connection:
            children = connection.execute(
                "SELECT COUNT(*) FROM pw_checkout_event"
            ).fetchone()[0]
        self.assertGreater(children, 0, "the fixture must have a referencing row")

        self.rewind_to(3)
        reopened = self.store()  # must not raise

        self.assertEqual(reopened.get_checkout(allocation.allocation_id).run_id, "run-1")
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM pw_checkout_event"
                ).fetchone()[0],
                children,
                "the rebuild dropped referencing rows",
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )

    def test_foreign_keys_are_enforced_again_after_the_rebuild(self):
        self.populate()
        self.rewind_to(3)
        store = self.store()
        with store._connection() as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0], 1,
                "FK enforcement must be restored after the rebuild",
            )

    def test_the_partial_uniqueness_index_survives_the_rebuild(self):
        self.populate()
        self.rewind_to(3)
        self.store()
        with sqlite3.connect(self.database) as connection:
            names = [
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='pw_checkout_allocation'"
                )
            ]
        self.assertIn("pw_checkout_active_path", names)
