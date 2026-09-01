import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from engineering_state import (
    ArtifactMetadata,
    EngineeringConflict,
    EngineeringStateStore,
    ExecutionManifest,
    SafeCommand,
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


if __name__ == "__main__":
    unittest.main()
