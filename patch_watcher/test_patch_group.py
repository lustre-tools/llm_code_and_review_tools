import tempfile
import unittest
from pathlib import Path

from patch_watcher.patch_group import (
    PatchGroup,
    PatchGroupConflict,
    PatchGroupError,
    PatchGroupStore,
)


class PatchGroupTests(unittest.TestCase):
    def store(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        return PatchGroupStore(Path(self.temp.name) / "patch-groups.json")

    def test_order_is_the_operators_and_positions_are_readable(self):
        """Order is what lets a report say "this belongs in the second patch,
        not the third", so it is preserved exactly as declared."""

        group = PatchGroup("68763", ["68763", "68764", "68844", "68845"], label="fsx")
        self.assertEqual(group.base, "68763")
        self.assertEqual(group.tip, "68845")
        self.assertEqual(group.position("68844"), 3)
        self.assertEqual(group.position("35302"), 0)
        self.assertTrue(group.contains(68764))
        self.assertFalse(group.contains("35302"))

    def test_a_group_is_at_least_two_changes_with_no_repeats(self):
        with self.assertRaises(PatchGroupError):
            PatchGroup("68763", ["68763"])
        with self.assertRaises(PatchGroupError):
            PatchGroup("68763", ["68763", "68763"])
        with self.assertRaises(PatchGroupError):
            PatchGroup("68763", ["68763", "not-a-change"])

    def test_the_id_must_be_one_of_its_own_changes(self):
        """Otherwise the id names a change the group does not contain, and
        `for_change` and `get` disagree about what exists."""

        with self.assertRaises(PatchGroupError):
            PatchGroup("11111", ["68763", "68764"])

    def test_a_change_belongs_to_one_group_only(self):
        """Two groups sharing a change means two agents each believing they
        own it, and exclusive ownership is what every run rests on."""

        store = self.store()
        store.save(PatchGroup("68763", ["68763", "68764"]))
        with self.assertRaises(PatchGroupError) as caught:
            store.save(PatchGroup("68844", ["68844", "68764"]))
        self.assertIn("already in group 68763", str(caught.exception))

    def test_lookup_by_member_finds_the_group(self):
        store = self.store()
        store.save(PatchGroup("68763", ["68763", "68764", "68845"], label="fsx"))
        found = store.for_change("68845")
        self.assertIsNotNone(found)
        self.assertEqual(found.group_id, "68763")
        self.assertEqual(found.label, "fsx")
        self.assertIsNone(store.for_change("35302"))
        self.assertIsNone(store.for_change("nonsense"))

    def test_a_concurrent_edit_is_refused_rather_than_lost(self):
        store = self.store()
        saved = store.save(PatchGroup("68763", ["68763", "68764"]))
        self.assertEqual(saved.version, 1)
        store.save(saved, expected_version=1)
        with self.assertRaises(PatchGroupConflict):
            store.save(saved, expected_version=1)

    def test_deleting_releases_its_changes_for_another_group(self):
        store = self.store()
        saved = store.save(PatchGroup("68763", ["68763", "68764"]))
        self.assertTrue(store.delete("68763", expected_version=saved.version))
        self.assertFalse(store.delete("68763"))
        # Now free to regroup differently.
        store.save(PatchGroup("68764", ["68764", "68844"]))
        self.assertEqual(store.for_change("68764").group_id, "68764")

    def test_a_missing_or_corrupt_file_reads_as_no_groups(self):
        """Watching and reporting must work before any group is declared, and
        a damaged file must not take the console down with it."""

        store = self.store()
        self.assertEqual(store.list(), ())
        store.path.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(PatchGroupError):
            store.list()
        store.path.write_text('{"groups": {"x": {"bad": true}}}', encoding="utf-8")
        self.assertEqual(store.list(), ())

    def test_the_file_is_private(self):
        store = self.store()
        store.save(PatchGroup("68763", ["68763", "68764"]))
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
