import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engineering_state import (
    ArtifactMetadata,
    EngineeringConflict,
    EngineeringStateStore,
    ExecutionManifest,
    SafeCommand,
    ValidationCommandAudit,
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

    def test_command_cwd_resolution_rejects_symlink_escape(self):
        checkout = self.checkouts / "run-1"
        checkout.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (checkout / "escape").symlink_to(outside, target_is_directory=True)
        command = SafeCommand("test", ["pytest"], cwd="escape")
        with self.assertRaisesRegex(ValueError, "escapes"):
            command.resolve_cwd(checkout)

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

    def test_quarantine_is_terminal_for_automatic_release(self):
        allocation = self.plan()
        quarantined = self.store.quarantine_checkout(
            allocation.allocation_id,
            run_id=allocation.run_id,
            owner_id=allocation.owner_id,
            revision_sha=REVISION,
            reason="cleanup verification failed",
        )
        self.assertEqual(quarantined.state, "quarantined")
        with self.assertRaises(EngineeringConflict):
            self.store.release_checkout(
                allocation.allocation_id,
                run_id=allocation.run_id,
                owner_id=allocation.owner_id,
                revision_sha=REVISION,
            )

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

    def test_validation_command_audit_persists_explicit_evidence_role(self):
        command = ValidationCommandAudit(
            "guest-test", argv=("make", "test"), evidence_role="test"
        )
        restored = ValidationCommandAudit.from_command(command.to_dict())
        self.assertEqual(restored.evidence_role, "test")
        self.assertEqual(restored.digest, command.digest)

        historical = ValidationCommandAudit("historical", argv=("true",))
        self.assertNotIn("evidence_role", historical.to_dict())
        self.assertEqual(
            ValidationCommandAudit.from_command(historical.to_dict()).evidence_role,
            "other",
        )

    def test_guest_command_audit_is_open_ended_immutable_and_artifact_bound(self):
        allocation, manifest, execution, attempt = self.approve_and_claim()
        running = self.store.mark_validation_attempt_running(
            attempt.attempt_id, worker_id=attempt.worker_id
        )
        self.assertEqual(
            self.store.get_active_validation_capability(
                session_id=allocation.session_id,
                run_id=allocation.run_id,
                revision_sha=allocation.revision_sha,
            ).execution_id,
            execution.execution_id,
        )
        self.assertIsNone(
            self.store.get_active_validation_capability(
                session_id=allocation.session_id,
                run_id=allocation.run_id,
                revision_sha="e" * 40,
            )
        )
        artifact = ArtifactMetadata(
            artifact_id="guest-output-1",
            run_id=allocation.run_id,
            revision_sha=allocation.revision_sha,
            kind="test-output",
            relative_path="validation/output.txt",
            sha256="f" * 64,
            size_bytes=42,
            media_type="text/plain",
        )
        self.store.register_artifact(allocation.allocation_id, artifact)
        started = datetime(2026, 9, 1, tzinfo=timezone.utc)
        command = ValidationCommandAudit(
            "ad-hoc-shell",
            text='for t in tests/*; do custom-runner "$t"; done',
            cwd="/work/source",
            env={"CUSTOM_FLAG": "anything"},
        )
        self.assertNotEqual(command.command_id, manifest.commands[0].step_id)
        claim = self.store.claim_validation_command(
            running.attempt_id,
            worker_id=running.worker_id,
            command=command,
            now=started,
        )
        self.assertTrue(claim.should_dispatch)
        result = self.store.record_validation_step_result(
            running.attempt_id,
            worker_id=running.worker_id,
            command=command,
            state="succeeded",
            summary="guest command passed",
            artifact_ids=(artifact.artifact_id,),
            exit_code=0,
            started_at=started,
            finished_at=started + timedelta(seconds=3),
        )
        self.assertEqual(result.command.text, command.text)
        self.assertEqual(result.command_sha256, command.digest)
        self.assertEqual(result.artifact_ids, (artifact.artifact_id,))
        foreign_allocation = self.make_active("2")
        foreign_artifact = ArtifactMetadata(
            artifact_id="foreign-output",
            run_id=foreign_allocation.run_id,
            revision_sha=foreign_allocation.revision_sha,
            kind="test-output",
            relative_path="foreign.txt",
            sha256="a" * 64,
            size_bytes=1,
            media_type="text/plain",
        )
        self.store.register_artifact(foreign_allocation.allocation_id, foreign_artifact)
        foreign_command = ValidationCommandAudit("foreign", argv=("true",))
        self.store.claim_validation_command(
            running.attempt_id,
            worker_id=running.worker_id,
            command=foreign_command,
            now=started,
        )
        with self.assertRaisesRegex(EngineeringConflict, "attempt revision"):
            self.store.record_validation_step_result(
                running.attempt_id,
                worker_id=running.worker_id,
                command=foreign_command,
                state="succeeded",
                summary="invalid foreign evidence",
                artifact_ids=(foreign_artifact.artifact_id,),
                exit_code=0,
                started_at=started,
                finished_at=started + timedelta(seconds=1),
            )
        finished = self.store.finish_validation_attempt(
            running.attempt_id,
            worker_id=running.worker_id,
            state="succeeded",
            summary="validation passed",
            now=started + timedelta(seconds=4),
        )
        self.assertEqual(finished.state, "succeeded")
        self.assertIsNone(
            self.store.get_active_validation_capability(
                session_id=allocation.session_id,
                run_id=allocation.run_id,
                revision_sha=allocation.revision_sha,
            )
        )
        replay = self.store.record_validation_step_result(
            running.attempt_id,
            worker_id=running.worker_id,
            command=command,
            state="succeeded",
            summary="guest command passed",
            artifact_ids=(artifact.artifact_id,),
            exit_code=0,
            started_at=started,
            finished_at=started + timedelta(seconds=3),
        )
        self.assertEqual(replay, result)
        with sqlite3.connect(self.database) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE pw_validation_step_result SET summary = 'changed'"
                )
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

    def test_capacity_exhaustion_cooldown_and_single_use_retry_are_durable(self):
        started = datetime(2026, 9, 1, tzinfo=timezone.utc)
        allocation, _, execution, attempt = self.approve_and_claim("1", now=started)
        attempt = self.store.mark_validation_attempt_running(
            attempt.attempt_id, worker_id=attempt.worker_id, now=started
        )
        exhausted = ValidationCommandAudit("capacity", argv=("make", "-j128"))
        self.store.claim_validation_command(
            attempt.attempt_id,
            worker_id=attempt.worker_id,
            command=exhausted,
            now=started,
        )
        self.store.record_validation_step_result(
            attempt.attempt_id,
            worker_id=attempt.worker_id,
            command=exhausted,
            state="resource_exhausted",
            summary="guest memory exhausted",
            started_at=started,
            finished_at=started + timedelta(seconds=1),
        )
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
                idempotency_key="retry-without-grant",
                expected_revision=allocation.revision_sha,
                expected_owner_id=allocation.owner_id,
                now=started + timedelta(seconds=2),
            )
        grant = self.store.authorize_capacity_retry(
            execution.execution_id,
            expected_revision=allocation.revision_sha,
            approved_by="operator",
            idempotency_key="capacity-override-1",
            now=started + timedelta(seconds=2),
        )
        retry = self.store.claim_validation_attempt(
            execution.execution_id,
            worker_id="worker-retry",
            idempotency_key="retry-with-grant",
            expected_revision=allocation.revision_sha,
            expected_owner_id=allocation.owner_id,
            retry_grant_id=grant.grant_id,
            now=started + timedelta(seconds=2),
        )
        consumed = self.store.get_validation_retry_grant(grant.grant_id)
        self.assertEqual(consumed.consumed_by_attempt_id, retry.attempt_id)
        replayed_grant = self.store.authorize_capacity_retry(
            execution.execution_id,
            expected_revision=allocation.revision_sha,
            approved_by="operator",
            idempotency_key="capacity-override-1",
            now=started + timedelta(seconds=3),
        )
        self.assertEqual(replayed_grant, consumed)
        with self.assertRaises(EngineeringConflict):
            self.store.claim_validation_attempt(
                execution.execution_id,
                worker_id="another-worker",
                idempotency_key="reuse-capacity-grant",
                expected_revision=allocation.revision_sha,
                expected_owner_id=allocation.owner_id,
                retry_grant_id=grant.grant_id,
                now=started + timedelta(seconds=3),
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
                "pw_validation_command_claim",
                "pw_validation_retry_grant",
                "pw_validation_step_result",
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
        self.assertEqual(restarted.SCHEMA_VERSION, 3)
        self.assertIn("pw_validation_execution", tables)
        self.assertIn("pw_validation_step_result", tables)
        self.assertIn("pw_validation_command_claim", tables)

    def test_command_claim_is_durable_pre_dispatch_and_never_auto_replays(self):
        started = datetime(2026, 9, 1, tzinfo=timezone.utc)
        _, _, _, attempt = self.approve_and_claim("1", now=started)
        attempt = self.store.mark_validation_attempt_running(
            attempt.attempt_id, worker_id=attempt.worker_id, now=started
        )
        command = ValidationCommandAudit(
            "non-idempotent", text="touch marker && trigger-side-effect"
        )
        first = self.store.claim_validation_command(
            attempt.attempt_id,
            worker_id=attempt.worker_id,
            command=command,
            now=started,
        )
        self.assertEqual(first.disposition, "dispatch")
        restarted = EngineeringStateStore(
            self.database, checkout_root=self.checkouts
        )
        uncertain = restarted.claim_validation_command(
            attempt.attempt_id,
            worker_id=attempt.worker_id,
            command=command,
            now=started + timedelta(seconds=1),
        )
        self.assertEqual(uncertain.disposition, "already_reserved")
        self.assertFalse(uncertain.should_dispatch)
        with self.assertRaisesRegex(EngineeringConflict, "different immutable"):
            restarted.claim_validation_command(
                attempt.attempt_id,
                worker_id=attempt.worker_id,
                command=ValidationCommandAudit(
                    "non-idempotent", text="different-side-effect"
                ),
                now=started + timedelta(seconds=1),
            )
        restarted.record_validation_step_result(
            attempt.attempt_id,
            worker_id=attempt.worker_id,
            command=command,
            state="succeeded",
            summary="completed once",
            exit_code=0,
            started_at=started,
            finished_at=started + timedelta(seconds=2),
        )
        completed = restarted.claim_validation_command(
            attempt.attempt_id,
            worker_id=attempt.worker_id,
            command=command,
            now=started + timedelta(seconds=3),
        )
        self.assertEqual(completed.disposition, "completed")
        self.assertFalse(completed.should_dispatch)
        self.assertEqual(
            restarted.list_validation_command_claims(attempt.attempt_id)[0],
            completed,
        )
        with self.assertRaisesRegex(EngineeringConflict, "not exactly reserved"):
            restarted.record_validation_step_result(
                attempt.attempt_id,
                worker_id=attempt.worker_id,
                command=ValidationCommandAudit("unreserved", argv=("true",)),
                state="succeeded",
                summary="must not be accepted",
                exit_code=0,
                started_at=started,
                finished_at=started,
            )


if __name__ == "__main__":
    unittest.main()
