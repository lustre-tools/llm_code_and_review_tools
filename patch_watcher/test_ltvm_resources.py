import json
import subprocess
import unittest
from datetime import UTC, datetime

from patch_watcher.ltvm_resources import (
    CleanupAction,
    LTVMAdapter,
    LTVMInventory,
    LTVMInventoryError,
    SessionResourceRecord,
    UnsafeCleanupError,
    cluster_belongs_to_checkout,
    owner_id_for_session,
    reconcile_session_resources,
    session_id_from_owner,
    vm_belongs_to_checkout,
)

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
OWNER = "patch-watcher:session-7f9c"


def inventory(*vms, clusters=None):
    payload = {"vms": list(vms)}
    if clusters is not None:
        payload["clusters"] = list(clusters)
    return LTVMInventory.from_json(payload)


def vm(name, owner=OWNER, **updates):
    value = {
        "name": name,
        "status": "running",
        "owner_id": owner,
        "mem": 2048,
        "host_rss_bytes": 700_000_000,
        "vcpus": 2,
    }
    value.update(updates)
    return value


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        stdout, returncode = response
        if isinstance(stdout, str):
            stdout = stdout.encode()
        return subprocess.CompletedProcess(command, returncode, stdout, b"private details")


class OwnershipAndInventoryTests(unittest.TestCase):
    def test_owner_id_is_exact_and_restart_stable(self):
        self.assertEqual(owner_id_for_session("session-7f9c"), OWNER)
        self.assertEqual(session_id_from_owner(OWNER), "session-7f9c")
        self.assertIsNone(session_id_from_owner("pid:123"))
        self.assertIsNone(session_id_from_owner("patch-watcher:"))
        with self.assertRaises(ValueError):
            owner_id_for_session("bad\nsession")

    def test_machine_inventory_keeps_guest_memory_separate_from_host_rss(self):
        observed = inventory(vm("co1-single", mem=3072, host_rss_bytes=880_000_000))

        record = observed.vms[0]
        self.assertEqual(record.configured_guest_memory_bytes, 3072 * 1024 * 1024)
        self.assertEqual(record.host_rss_bytes, 880_000_000)
        self.assertNotEqual(record.configured_guest_memory_bytes, record.host_rss_bytes)
        self.assertEqual(observed.configured_guest_memory_bytes, 3072 * 1024 * 1024)
        self.assertEqual(observed.known_host_rss_bytes, 880_000_000)

    def test_unknown_configured_memory_does_not_manufacture_a_total(self):
        observed = inventory(vm("one"), vm("two", mem=None))
        self.assertIsNone(observed.configured_guest_memory_bytes)
        self.assertEqual(observed.known_host_rss_bytes, 1_400_000_000)

    def test_invalid_and_duplicate_rows_remain_ambiguous(self):
        observed = LTVMInventory.from_json(
            {
                "vms": [
                    "bad",
                    vm("duplicate"),
                    vm("duplicate", owner="somebody-else"),
                    {"name": "invalid-owner", "owner_id": {"bad": True}},
                ]
            }
        )
        self.assertEqual(len(observed.named_vms("duplicate")), 2)
        self.assertIsNone(observed.named_vms("invalid-owner")[0].owner_id)
        self.assertIn("duplicate_vm_name", {issue.code for issue in observed.issues})
        self.assertIn("invalid_owner_id", {issue.code for issue in observed.issues})
        with self.assertRaises(LTVMInventoryError):
            LTVMInventory.from_json({"machines": []})


class ReconciliationSafetyTests(unittest.TestCase):
    def test_cleanup_never_deletes_unrelated_or_unowned_vms(self):
        observed = inventory(
            vm("ours"),
            vm("other-session", owner="patch-watcher:session-other"),
            vm("legacy", owner=None),
            vm("pid-owned", owner="pid:7788"),
        )

        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True
        )

        self.assertEqual(
            [(action.resource_type, action.name) for action in result.cleanup_actions],
            [("vm", "ours")],
        )
        self.assertNotIn("other-session", {item.name for item in result.resources})
        self.assertNotIn("legacy", {item.name for item in result.resources})

    def test_same_name_with_missing_or_different_owner_is_never_cleanup_eligible(self):
        recorded = [SessionResourceRecord("vm", "expected", OWNER)]
        for candidate in (
            inventory(vm("expected", owner=None)),
            inventory(vm("expected", owner="patch-watcher:someone-else")),
            inventory(vm("expected"), vm("expected", owner=None)),
        ):
            with self.subTest(candidate=candidate):
                result = reconcile_session_resources(
                    "session-7f9c",
                    candidate,
                    recorded=recorded,
                    cleanup_requested=True,
                )
                self.assertEqual(result.cleanup_actions, ())
                self.assertTrue(
                    {"owner_mismatch", "duplicate_vm_name"}
                    & {issue.code for issue in result.issues}
                )

    def test_exact_owner_discovers_orphan_from_partial_create_and_cleans_it(self):
        result = reconcile_session_resources(
            "session-7f9c",
            inventory(vm("partial-mds")),
            recorded=[
                SessionResourceRecord(
                    "cluster",
                    "partial",
                    OWNER,
                    ("partial-mds", "partial-oss"),
                )
            ],
            cleanup_requested=True,
        )
        self.assertEqual(
            [(action.resource_type, action.name) for action in result.cleanup_actions],
            [("vm", "partial-mds")],
        )
        self.assertIn(
            ("cluster", "partial", "cleanup_pending"),
            {(item.resource_type, item.name, item.lifecycle_state) for item in result.resources},
        )
        self.assertIn(
            "cluster_inventory_unavailable", {issue.code for issue in result.issues}
        )

    def test_complete_authoritative_cluster_uses_one_cluster_cleanup(self):
        observed = inventory(
            vm("co2-mds", cluster_name="co2"),
            vm("co2-oss", cluster_name="co2"),
            clusters=[
                {
                    "name": "co2",
                    "owner_id": OWNER,
                    "members": ["co2-mds", "co2-oss"],
                }
            ],
        )
        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True
        )
        self.assertEqual(
            result.cleanup_actions,
            (CleanupAction("cluster", "co2", OWNER, ("co2-mds", "co2-oss")),),
        )

    def test_partial_or_ambiguous_cluster_cleans_only_proven_member_vms(self):
        observed = inventory(
            vm("co3-mds", cluster_name="co3"),
            vm("co3-oss", owner=None, cluster_name="co3"),
            clusters=[
                {
                    "name": "co3",
                    "owner_id": OWNER,
                    "members": ["co3-mds", "co3-oss"],
                }
            ],
        )
        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True
        )
        self.assertEqual(
            result.cleanup_actions,
            (CleanupAction("vm", "co3-mds", OWNER),),
        )
        self.assertIn("partial_cluster", {issue.code for issue in result.issues})


    def test_discovered_vm_is_orphaned_until_cleanup_is_actually_requested(self):
        """A guest we can claim but never recorded must be reported, not destroyed.

        Reconciliation runs in two very different modes over the same guest.
        Reporting must file an unrecorded-but-claimable VM as `orphaned` so an
        operator sees a leak; only an explicit cleanup request may move it to
        `cleanup_pending` and plan a destroy.  Nothing pinned which way round
        that is, so the two could swap: reporting would quietly schedule
        destruction of a guest nobody asked to remove, or cleanup would file
        the guest as an orphan and leave it running -- holding its `co<N>-`
        checkout index against every later run.
        """
        observed = inventory(vm("ours"))

        reported = reconcile_session_resources("session-7f9c", observed)
        planned = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True
        )

        self.assertEqual(
            [(item.name, item.lifecycle_state) for item in reported.resources],
            [("ours", "orphaned")],
        )
        self.assertEqual(reported.cleanup_actions, ())
        self.assertEqual(
            [(item.name, item.lifecycle_state) for item in planned.resources],
            [("ours", "cleanup_pending")],
        )
        self.assertEqual(
            [(action.resource_type, action.name) for action in planned.cleanup_actions],
            [("vm", "ours")],
        )

    def test_recorded_cluster_member_is_still_discovered_without_adoption(self):
        """Terminal cleanup must still reach the members of a cluster it wrote down.

        With adoption disabled -- what a terminal run must pass, because its
        `co<N>-` namespace may already have been re-issued -- discovery narrows
        to the session's own record plus the named members of a cluster it
        recorded.  That member exemption is what makes a partial cluster create
        cleanable at all; if the visibility check treats a member VM as a
        cluster it stops applying, the half-created guest is never destroyed
        and leaks with the checkout index it holds.  Everything outside that
        record must stay untouched even though its owner id matches.
        """
        result = reconcile_session_resources(
            "session-7f9c",
            inventory(vm("partial-mds"), vm("never-recorded")),
            recorded=[
                SessionResourceRecord(
                    "cluster",
                    "partial",
                    OWNER,
                    ("partial-mds", "partial-oss"),
                )
            ],
            cleanup_requested=True,
            adopt_unrecorded=False,
        )

        self.assertEqual(
            [(action.resource_type, action.name) for action in result.cleanup_actions],
            [("vm", "partial-mds")],
        )
        self.assertNotIn("never-recorded", {item.name for item in result.resources})

    def test_duplicate_cluster_name_is_ambiguous_and_falls_back_to_member_vms(self):
        """Two inventory rows for one cluster name must not authorise a cluster destroy.

        Duplicate VM names were covered; duplicate cluster names were not, even
        though a cluster destroy takes every member with it.  An ambiguous
        cluster row must be reported as ambiguous and skipped, leaving only the
        individually proven member VMs eligible.
        """
        observed = inventory(
            vm("co4-mds", cluster_name="co4"),
            clusters=[
                {"name": "co4", "owner_id": OWNER, "members": ["co4-mds"]},
                {"name": "co4", "owner_id": OWNER, "members": ["co4-mds"]},
            ],
        )

        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True
        )

        self.assertIn(
            "duplicate_cluster_name", {issue.code for issue in result.issues}
        )
        self.assertEqual(
            result.cleanup_actions, (CleanupAction("vm", "co4-mds", OWNER),)
        )


class CheckoutOwnershipTests(unittest.TestCase):
    """The checkout index is the ownership model; the trailing dash is load-bearing."""

    def test_prefix_match_is_exact_with_dash(self):
        for name, owned in (
            ("co3-sanity", True),
            ("co3-ec-recovery", True),
            ("co31-sanity", False),
            ("co3", False),
            ("sanity-co3", False),
            ("xco3-sanity", False),
        ):
            with self.subTest(name=name):
                self.assertEqual(vm_belongs_to_checkout(name, 3), owned)
        # ``ltvm cluster create co2 ...`` names the cluster itself ``co2``.
        self.assertTrue(cluster_belongs_to_checkout("co2", 2))
        self.assertTrue(cluster_belongs_to_checkout("co2-recovery", 2))
        self.assertFalse(cluster_belongs_to_checkout("co21", 2))

    def test_inventory_lookup_selects_only_the_checkout_namespace(self):
        observed = inventory(
            vm("co3-sanity", owner=None),
            vm("co31-sanity", owner=None),
            vm("co4-sanity", owner=None),
            clusters=[
                {"name": "co3", "owner_id": None, "members": ["co3-sanity"]},
                {"name": "co31", "owner_id": None, "members": ["co31-sanity"]},
            ],
        )
        self.assertEqual(
            [item.name for item in observed.vms_named_for_checkout(3)], ["co3-sanity"]
        )
        self.assertEqual(
            [item.name for item in observed.clusters_named_for_checkout(3)], ["co3"]
        )

    def test_an_owner_id_we_cannot_read_is_an_owner_not_an_absence(self):
        """An unparseable owner_id used to be indistinguishable from none.

        `_optional_owner` returns None for a JSON number, a list, a bool, an
        over-long string or one carrying a NUL -- the same value it returns
        when no owner was declared at all. The prefix claim reads "owner_id is
        None" as "unowned", so a guest belonging to another session was
        adopted by name and destroyed. Measured before the fix: all six of
        these rows were destroyed. Only the genuinely unowned one may be.
        """

        observed = inventory(
            vm("co3-mine", owner=None),
            vm("co3-intowner", owner=42),
            vm("co3-listowner", owner=["a"]),
            vm("co3-boolowner", owner=True),
            vm("co3-nulowner", owner="x\x00y"),
            vm("co3-longowner", owner="z" * 300),
            vm("co3-str-other", owner="patch-watcher:someone-else"),
        )

        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True, checkout_index=3
        )

        self.assertEqual(
            sorted(action.name for action in result.cleanup_actions), ["co3-mine"]
        )
        self.assertIn(
            "invalid_owner_id", {issue.code for issue in result.issues}
        )

    def test_the_destroy_itself_refuses_a_row_whose_owner_is_unreadable(self):
        """The re-prove before the irreversible destroy needs the same rule.

        `LTVMAdapter.cleanup` re-reads the inventory and proves ownership
        again. Without this the plan could be correct and the destroy still
        wrong, since the second read is what actually gates the command.
        """

        calls = []

        def runner(argv, **kwargs):
            calls.append(tuple(argv))
            return subprocess.CompletedProcess(
                argv, 0,
                json.dumps({"vms": [vm("co3-sanity", owner=42)]}),
                "",
            )

        adapter = LTVMAdapter(runner=runner)
        action = CleanupAction("vm", "co3-sanity", OWNER, checkout_index=3)
        with self.assertRaises(UnsafeCleanupError):
            adapter.cleanup(action)
        self.assertNotIn(
            ("ltvm", "destroy", "co3-sanity", "--json"), calls,
            "a guest with an unreadable owner was destroyed",
        )

    def test_checkout_claim_cleans_its_prefix_and_nothing_adjacent(self):
        observed = inventory(
            vm("co3-sanity", owner=None),
            vm("co31-sanity", owner=None),
            vm("co4-sanity", owner=None),
        )

        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True, checkout_index=3
        )

        self.assertEqual(
            [(action.name, action.checkout_index) for action in result.cleanup_actions],
            [("co3-sanity", 3)],
        )

    def test_no_checkout_means_no_claim_rather_than_every_vm(self):
        observed = inventory(
            vm("co3-sanity", owner=None), vm("co31-sanity", owner=None)
        )

        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True
        )

        self.assertEqual(result.cleanup_actions, ())
        self.assertEqual(result.resources, ())

    def test_prefixed_vm_owned_by_another_session_is_never_claimed(self):
        observed = inventory(vm("co3-sanity", owner="patch-watcher:session-other"))

        result = reconcile_session_resources(
            "session-7f9c",
            observed,
            recorded=[SessionResourceRecord("vm", "co3-sanity", OWNER)],
            cleanup_requested=True,
            checkout_index=3,
        )

        self.assertEqual(result.cleanup_actions, ())
        self.assertIn("owner_mismatch", {issue.code for issue in result.issues})

    def test_prefixed_cluster_cleanup_covers_its_members(self):
        observed = inventory(
            vm("co2-mds", owner=None, cluster_name="co2"),
            vm("co2-oss", owner=None, cluster_name="co2"),
            clusters=[{"name": "co2", "owner_id": None, "members": ["co2-mds", "co2-oss"]}],
        )

        result = reconcile_session_resources(
            "session-7f9c", observed, cleanup_requested=True, checkout_index=2
        )

        self.assertEqual(
            result.cleanup_actions,
            (CleanupAction("cluster", "co2", OWNER, ("co2-mds", "co2-oss"), 2),),
        )

    def test_adapter_reproves_a_prefix_claim_before_destroying(self):
        action = CleanupAction("vm", "co3-sanity", OWNER, checkout_index=3)
        payload = {"vms": [vm("co3-sanity", owner=None)]}
        runner = FakeRunner([(json.dumps(payload), 0), ("{}", 0)])

        LTVMAdapter(runner).cleanup(action)

        self.assertEqual(runner.calls[1][0], ["ltvm", "destroy", "co3-sanity", "--json"])
        for unsafe in (
            {"vms": [vm("co3-sanity", owner="patch-watcher:someone-else")]},
            {"vms": [vm("co3-sanity", owner=None), vm("co3-sanity", owner=None)]},
            {"vms": []},
        ):
            with self.subTest(unsafe=unsafe):
                refusing = FakeRunner([(json.dumps(unsafe), 0)])
                with self.assertRaises(UnsafeCleanupError):
                    LTVMAdapter(refusing).cleanup(action)
                self.assertEqual(len(refusing.calls), 1)

    def test_adapter_refuses_a_prefix_claim_for_a_foreign_name(self):
        # A record whose name is outside the checkout namespace cannot be
        # destroyed on a prefix claim, however it got into the ledger.
        runner = FakeRunner([(json.dumps({"vms": [vm("co31-sanity", owner=None)]}), 0)])
        with self.assertRaises(UnsafeCleanupError):
            LTVMAdapter(runner).cleanup(
                CleanupAction("vm", "co31-sanity", OWNER, checkout_index=3)
            )
        self.assertEqual(len(runner.calls), 1)


class AdapterTests(unittest.TestCase):
    def test_inventory_and_destroy_are_argv_only_and_noninteractive(self):
        payload = json.dumps({"vms": [vm("owned")]})
        runner = FakeRunner([(payload, 0), ("{}", 0)])
        adapter = LTVMAdapter(runner)

        adapter.cleanup(CleanupAction("vm", "owned", OWNER))

        self.assertEqual(runner.calls[0][0], ["ltvm", "list", "--json"])
        self.assertEqual(runner.calls[1][0], ["ltvm", "destroy", "owned", "--json"])
        for command, kwargs in runner.calls:
            self.assertIsInstance(command, list)
            self.assertNotIn("shell", kwargs)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(kwargs["check"])

    def test_adapter_rechecks_and_refuses_changed_missing_or_ambiguous_owner(self):
        unsafe_payloads = (
            {"vms": [vm("owned", owner=None)]},
            {"vms": [vm("owned", owner="patch-watcher:other")]},
            {"vms": [vm("owned"), vm("owned")]},
            {"vms": []},
        )
        for payload in unsafe_payloads:
            with self.subTest(payload=payload):
                runner = FakeRunner([(json.dumps(payload), 0)])
                with self.assertRaises(UnsafeCleanupError):
                    LTVMAdapter(runner).cleanup(CleanupAction("vm", "owned", OWNER))
                self.assertEqual(len(runner.calls), 1)

    def test_cluster_cleanup_requires_cluster_and_every_member_exact_owner(self):
        payload = {
            "vms": [vm("c-mds"), vm("c-oss", owner="patch-watcher:other")],
            "clusters": [
                {"name": "c", "owner_id": OWNER, "members": ["c-mds", "c-oss"]}
            ],
        }
        runner = FakeRunner([(json.dumps(payload), 0)])
        with self.assertRaises(UnsafeCleanupError):
            LTVMAdapter(runner).cleanup(
                CleanupAction("cluster", "c", OWNER, ("c-mds", "c-oss"))
            )
        self.assertEqual(len(runner.calls), 1)

    def test_proven_cluster_cleanup_uses_noninteractive_sudo_argv(self):
        payload = {
            "vms": [vm("c-mds"), vm("c-oss")],
            "clusters": [
                {"name": "c", "owner_id": OWNER, "members": ["c-mds", "c-oss"]}
            ],
        }
        runner = FakeRunner([(json.dumps(payload), 0), ("{}", 0)])
        LTVMAdapter(runner).cleanup(
            CleanupAction("cluster", "c", OWNER, ("c-mds", "c-oss"))
        )
        self.assertEqual(
            runner.calls[1][0],
            ["sudo", "-n", "ltvm", "cluster", "--json", "destroy", "c"],
        )

    def test_adapter_failure_is_bounded_and_does_not_leak_stderr(self):
        runner = FakeRunner([("", 9)])
        with self.assertRaises(Exception) as raised:
            LTVMAdapter(runner).inventory()
        self.assertNotIn("private details", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
