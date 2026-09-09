"""Tests for the execution seam and the checkout pool."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from patch_watcher import workspace
from patch_watcher.workspace import (
    Checkout,
    CheckoutPool,
    CheckoutPoolError,
    WorkspaceError,
    create_run_directories,
    hash_text,
)


class RunDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_layout_is_private_and_logical_paths_resolve(self):
        layout = create_run_directories(self.base, "run-1")
        source = layout.resolve("/work/source")
        self.assertTrue(source.is_dir())
        self.assertEqual(source.stat().st_mode & 0o777, 0o700)
        self.assertEqual(layout.resolve("/work/source/lustre").name, "lustre")

    def test_paths_cannot_escape_the_run_root(self):
        layout = create_run_directories(self.base, "run-2")
        for candidate in ("/work/../etc", "work/source", "/nope", "/work/source/../.."):
            with self.subTest(candidate=candidate), self.assertRaises(WorkspaceError):
                layout.resolve(candidate)

    def test_a_run_id_is_claimed_exactly_once(self):
        create_run_directories(self.base, "run-3")
        with self.assertRaises(FileExistsError):
            create_run_directories(self.base, "run-3")

    def test_invalid_run_ids_are_rejected(self):
        for candidate in ("", "../escape", "run/3", "a" * 65):
            with self.subTest(candidate=candidate), self.assertRaises(WorkspaceError):
                create_run_directories(self.base, candidate)

    def test_hash_text_is_stable_and_prefixed(self):
        self.assertEqual(hash_text("abc"), hash_text("abc"))
        self.assertTrue(hash_text("abc").startswith("sha256:"))
        self.assertNotEqual(hash_text("abc"), hash_text("abd"))


class CheckoutPoolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkouts"
        for index in range(1, 5):
            (self.root / str(index)).mkdir(parents=True)
        self.database = Path(self.temporary.name) / "pool.sqlite3"

    def pool(self, indices=(1, 2, 3, 4)):
        return CheckoutPool(self.root, indices, database=self.database)

    def test_checkout_index_is_the_vm_ownership_prefix(self):
        checkout = Checkout(index=7, path=Path("/tmp/7"))
        self.assertEqual(checkout.vm_prefix, "co7-")
        self.assertTrue(checkout.owns_vm("co7-sanity"))
        self.assertFalse(checkout.owns_vm("co71-sanity"))
        self.assertFalse(checkout.owns_vm("co1-sanity"))

    def test_allocation_is_exclusive_and_released(self):
        pool = self.pool()
        first = pool.allocate("session-a")
        second = pool.allocate("session-b")
        self.assertNotEqual(first.index, second.index)
        self.assertEqual(pool.allocations()[first.index], "session-a")
        pool.release("session-a")
        self.assertNotIn(first.index, pool.allocations())

    def test_repeated_allocation_for_one_owner_returns_the_same_checkout(self):
        pool = self.pool()
        self.assertEqual(pool.allocate("session-a").index, pool.allocate("session-a").index)
        self.assertEqual(len(pool.allocations()), 1)

    def test_an_exhausted_pool_refuses_rather_than_sharing(self):
        pool = self.pool(indices=(1,))
        pool.allocate("session-a")
        with self.assertRaises(CheckoutPoolError):
            pool.allocate("session-b")

    def test_concurrent_allocation_never_double_assigns(self):
        pool = self.pool()
        results = {}
        barrier = threading.Barrier(8)

        def claim(name):
            barrier.wait()
            try:
                results[name] = pool.allocate(name).index
            except CheckoutPoolError:
                results[name] = None

        threads = [threading.Thread(target=claim, args=(f"s{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [index for index in results.values() if index is not None]
        self.assertEqual(len(claimed), 4, results)
        self.assertEqual(len(set(claimed)), 4, "a checkout was handed out twice")

    def test_a_missing_checkout_directory_is_skipped(self):
        pool = self.pool(indices=(9, 2))
        self.assertEqual(pool.allocate("session-a").index, 2)

    def test_an_unconfigured_pool_is_empty_rather_than_greedy(self):
        # The operator usually works in one of these checkouts; defaulting to
        # "all of them" would let an agent clobber a live working tree.
        # $CO is pinned to this fixture's root so the assertion is about the
        # empty membership and not about whether the machine running the suite
        # happens to have a checkouts directory in one of the default places.
        config = Path(self.temporary.name) / "absent.json"
        with patch.dict(os.environ, {"CO": str(self.root)}):
            pool = CheckoutPool.from_config(config, database=self.database)
        self.assertEqual(pool.indices, ())
        with self.assertRaises(CheckoutPoolError):
            pool.allocate("session-a")

    def test_configuration_declares_root_and_membership(self):
        config = Path(self.temporary.name) / "pool.json"
        config.write_text(json.dumps({"root": str(self.root), "checkouts": [2, 3]}))
        pool = CheckoutPool.from_config(config, database=self.database)
        self.assertEqual(pool.indices, (2, 3))
        self.assertEqual(pool.root, self.root.resolve())
        self.assertEqual(sorted(pool.free()), [2, 3])
        with self.assertRaises(CheckoutPoolError):
            pool.checkout(1)

    def test_malformed_configuration_is_refused(self):
        config = Path(self.temporary.name) / "bad.json"
        for payload in ("not json", json.dumps([1, 2]), json.dumps({"checkouts": "1,2"})):
            with self.subTest(payload=payload):
                config.write_text(payload)
                with self.assertRaises(CheckoutPoolError):
                    CheckoutPool.from_config(config, database=self.database)

    def test_allocations_survive_a_restart(self):
        self.pool().allocate("session-a")
        reopened = self.pool()
        self.assertEqual(reopened.allocation_for("session-a").index, 1)


if __name__ == "__main__":
    unittest.main()


class ConcurrentSameOwnerTests(unittest.TestCase):
    """Two processes racing an allocation for the SAME owner.

    Proved bug: the owner-UNIQUE violation was treated like an index
    collision, so the loser walked the whole pool and raised "no free
    checkout" with most of it free -- a correct request rejected with a false
    diagnosis. Triggered by a restarted app overlapping the old one.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkouts"
        for index in range(1, 5):
            (self.root / str(index)).mkdir(parents=True)
        self.database = Path(self.temporary.name) / "pool.sqlite3"

    def test_racing_the_same_owner_returns_one_checkout_not_an_error(self):
        pool = CheckoutPool(self.root, (1, 2, 3, 4), database=self.database)
        results = {}
        barrier = threading.Barrier(4)

        def claim(slot):
            barrier.wait()
            try:
                results[slot] = pool.allocate("one-owner")
            except CheckoutPoolError as exc:
                results[slot] = exc

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        errors = [v for v in results.values() if isinstance(v, CheckoutPoolError)]
        self.assertEqual(errors, [], f"a same-owner race was rejected: {errors}")
        indices = {v.index for v in results.values()}
        self.assertEqual(len(indices), 1, f"one owner got several checkouts: {indices}")
        self.assertEqual(len(pool.allocations()), 1)


class ResolveReturnsTheCheckedPathTests(unittest.TestCase):
    """`resolve()` must return exactly the path it validated.

    It used to resolve once inside the check and again for the return value. A
    concurrent symlink swap between the two calls returned a path OUTSIDE the
    run root having passed the check -- measured at roughly a third of calls
    under a race. Callers chmod and write to what they get back.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.layout = create_run_directories(self.base, "run-resolve")

    def test_the_returned_path_is_already_resolved(self):
        returned = self.layout.resolve("/work/source")
        self.assertEqual(returned, returned.resolve(),
                         "resolve() returned a path that still resolves elsewhere")

    def test_a_symlinked_component_pointing_outside_is_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        source = self.layout.resolve("/work/source")
        swapped = source / "link"
        swapped.symlink_to(outside)
        with self.assertRaises(WorkspaceError):
            self.layout.resolve("/work/source/link/evidence.txt")

    def test_a_symlink_inside_the_root_is_still_allowed(self):
        source = self.layout.resolve("/work/source")
        scratch = self.layout.resolve("/work/scratch")
        (source / "inward").symlink_to(scratch)
        returned = self.layout.resolve("/work/source/inward")
        self.assertEqual(returned, scratch.resolve())

    def test_the_check_helper_does_not_re_resolve_its_argument(self):
        # Structural: the checker used by resolve() must accept an
        # already-resolved path, or the double-resolution gap comes back.
        import inspect

        source = inspect.getsource(workspace._assert_resolved_beneath)
        self.assertNotIn("resolved.resolve()", source)
