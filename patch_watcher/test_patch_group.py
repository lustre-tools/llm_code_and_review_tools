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

    def test_a_series_keeps_the_operators_order(self):
        """Order is what lets a report say "this belongs in the second patch,
        not the third", so it is preserved exactly as declared."""

        group = PatchGroup(
            "68763", ["68763", "68764", "68844", "68845"],
            kind="series", label="fsx",
        )
        self.assertTrue(group.ordered)
        self.assertEqual(group.base, "68763")
        self.assertEqual(group.tip, "68845")
        self.assertEqual(group.position("68844"), 3)
        self.assertEqual(group.position("35302"), 0)
        self.assertTrue(group.contains(68764))
        self.assertFalse(group.contains("35302"))

    def test_a_flock_has_no_order_to_invent(self):
        """Several changes on one ticket with no dependency between them.  Any
        order a flock appeared to have would be an accident of how it was
        typed, and an agent told it had a base would reason from an ordering
        nobody asserted."""

        flock = PatchGroup("68845", ["68845", "68763", "68844"], kind="flock")
        self.assertFalse(flock.ordered)
        # Held as a set, sorted, so the representation implies nothing.
        self.assertEqual(flock.members, ("68763", "68844", "68845"))
        self.assertEqual(flock.position("68763"), 0)
        self.assertEqual(flock.position("68845"), 0)
        for attribute in ("base", "tip"):
            with self.assertRaises(PatchGroupError) as caught:
                getattr(flock, attribute)
            self.assertIn("do not depend on each other", str(caught.exception))
        # Membership is exactly as meaningful as for a series.
        self.assertTrue(flock.contains("68844"))
        self.assertFalse(flock.contains("35302"))

    def test_a_ticket_group_is_rooted_in_the_issue_and_may_be_empty(self):
        """The root is the ticket, so that is the handle, and a ticket with no
        patches yet is a perfectly good thing to watch."""

        group = PatchGroup("LU-20724", kind="ticket", label="fsx")
        self.assertEqual(group.group_id, "LU-20724")
        self.assertEqual(group.ticket, "LU-20724")
        self.assertEqual(group.members, ())
        self.assertTrue(group.discovered)
        self.assertFalse(group.ordered)

    def test_ticket_membership_is_discovered_and_may_grow(self):
        """Patches appear on a ticket over time.  A declared group must not be
        rewritten the same way: that would undo the one thing the operator
        actually asserted."""

        store = self.store()
        saved = store.save(PatchGroup("LU-20724", kind="ticket"))
        grown = store.save(
            saved.with_members(["68764", "68763"]), expected_version=saved.version
        )
        # Sorted, because a ticket's patches have no order of their own.
        self.assertEqual(grown.members, ("68763", "68764"))
        self.assertEqual(store.for_change("68763").group_id, "LU-20724")

        declared = PatchGroup("68844", ["68844", "68845"], kind="series")
        with self.assertRaises(PatchGroupError) as caught:
            declared.with_members(["68844"])
        self.assertIn("declared, not discovered", str(caught.exception))

    def test_a_ticket_key_must_look_like_one(self):
        """So a stray subject line or URL cannot become a group id."""

        for bad in ("", "LU", "lu-", "20724", "LU-0", "not a key",
                    "https://jira.whamcloud.com/browse/LU-20724"):
            with self.assertRaises(PatchGroupError, msg=bad):
                PatchGroup(bad or "x", kind="ticket")
        # Case is normalised rather than rejected.
        self.assertEqual(PatchGroup("lu-20724", kind="ticket").ticket, "LU-20724")

    def test_only_a_ticket_group_carries_a_ticket(self):
        """A series with a ticket attached would have two roots and no rule
        for which one decides membership."""

        with self.assertRaises(PatchGroupError):
            PatchGroup("68763", ["68763", "68764"], kind="series", ticket="LU-20724")

    def test_the_kind_must_be_one_we_know(self):
        with self.assertRaises(PatchGroupError):
            PatchGroup("68763", ["68763", "68764"], kind="pile")

    def test_the_kind_survives_a_round_trip(self):
        store = self.store()
        store.save(PatchGroup("68763", ["68763", "68764"], kind="flock"))
        self.assertEqual(store.for_change("68764").kind, "flock")
        self.assertEqual(store.get("68763").kind, "flock")

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
