import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ltvm_resources import (
    CleanupAction,
    LTVMAdapter,
    LTVMCapacityStateStore,
    LTVMInventory,
    LTVMInventoryError,
    ResourceExhaustionReport,
    ResourceExhaustionValidationError,
    SessionResourceRecord,
    UnsafeCleanupError,
    owner_id_for_session,
    reconcile_session_resources,
    session_id_from_owner,
    target_guidance,
)


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
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


class TargetGuidanceTests(unittest.TestCase):
    def test_guidance_checks_published_target_then_validates_exact_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            guide = target_guidance("rocky9", directory, arch="x86_64")
        self.assertEqual(
            guide.list_local_argv,
            ("ltvm", "target", "list", "local", "--arch", "x86_64", "--json"),
        )
        self.assertEqual(
            guide.list_remote_argv,
            ("ltvm", "target", "list", "remote", "--arch", "x86_64", "--json"),
        )
        self.assertEqual(
            guide.fetch_argv,
            ("ltvm", "target", "fetch", "rocky9", "--arch", "x86_64", "--json"),
        )
        self.assertEqual(guide.validate_argv[:4], ("ltvm", "target", "validate", "rocky9"))
        self.assertIn("--lustre-tree", guide.validate_argv)
        self.assertEqual(guide.default_guest_memory_mib, 2048)
        with self.assertRaises(ValueError):
            target_guidance("rocky9; destroy everything", "/tmp")


class ResourceExhaustionTests(unittest.TestCase):
    def setUp(self):
        self.observed = inventory(vm("partial"), vm("unrelated", owner="patch-watcher:other"))
        self.payload = {
            "state": "resource_exhausted",
            "error_code": "ltvm_resource_exhausted",
            "failed_operation": "ltvm cluster create",
            "requested_resources": {
                "topology": "mgs+mds:1 oss:3",
                "guest_memory_mib_per_node": 2048,
            },
            "evidence": "insufficient memory: requested 8192 MiB, available 4096 MiB",
            "owned_resource_names": ["partial"],
        }

    def test_report_requires_specific_capacity_evidence_and_exact_owned_resources(self):
        report = ResourceExhaustionReport.validate(
            self.payload, expected_owner_id=OWNER, inventory=self.observed
        )
        self.assertEqual(report.owned_resource_names, ("partial",))
        for update in (
            {"state": "blocked"},
            {"error_code": "ltvm_create_failed"},
            {"failed_operation": "ltvm deploy-lustre"},
            {"requested_resources": {}},
            {"requested_resources": {"memory": object()}},
            {"evidence": "command failed for unknown reason"},
            {"owned_resource_names": ["unrelated"]},
            {"owned_resource_names": ["missing"]},
        ):
            with self.subTest(update=update):
                bad = dict(self.payload)
                bad.update(update)
                with self.assertRaises(ResourceExhaustionValidationError):
                    ResourceExhaustionReport.validate(
                        bad, expected_owner_id=OWNER, inventory=self.observed
                    )

    def test_capacity_failure_cleanup_cooldown_and_email_are_idempotent(self):
        report = ResourceExhaustionReport.validate(
            self.payload, expected_owner_id=OWNER, inventory=self.observed
        )
        cleanup = reconcile_session_resources(
            "session-7f9c", self.observed, cleanup_requested=True
        )
        self.assertEqual(cleanup.cleanup_actions, (CleanupAction("vm", "partial", OWNER),))

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capacity.sqlite3"
            store = LTVMCapacityStateStore(
                database,
                initial_cooldown=timedelta(minutes=10),
                maximum_cooldown=timedelta(minutes=25),
            )
            first = store.record_exhaustion("change-1", "run-1", OWNER, report, at=NOW)
            duplicate = store.record_exhaustion("change-1", "run-1", OWNER, report, at=NOW)
            self.assertEqual(first, duplicate)
            self.assertEqual(first.exhaustion_count, 1)
            self.assertEqual(first.retry_not_before, NOW + timedelta(minutes=10))
            self.assertTrue(store.in_cooldown("change-1", at=NOW + timedelta(minutes=9)))
            self.assertFalse(store.in_cooldown("change-1", at=NOW + timedelta(minutes=10)))

            claim = store.claim_email("run-1", "mailer", at=NOW)
            second_claim = store.claim_email("run-1", "mailer", at=NOW)
            self.assertTrue(claim.should_send)
            self.assertFalse(second_claim.should_send)
            self.assertEqual(claim.idempotency_key, "ltvm-resource-exhausted:run-1")

            reopened = LTVMCapacityStateStore(
                database,
                initial_cooldown=timedelta(minutes=10),
                maximum_cooldown=timedelta(minutes=25),
            )
            self.assertFalse(reopened.claim_email("run-1", "other-mailer").should_send)
            sent = reopened.finish_email("run-1", "mailer", sent=True, at=NOW)
            self.assertEqual(sent.email_status, "sent")
            self.assertEqual(
                reopened.finish_email("run-1", "mailer", sent=True, at=NOW), sent
            )
            cleaned = reopened.finish_cleanup("run-1", succeeded=True, at=NOW)
            self.assertEqual(cleaned.cleanup_status, "succeeded")
            self.assertEqual(os.stat(database).st_mode & 0o777, 0o600)

    def test_repeated_failures_back_off_per_patch_and_cap_cooldown(self):
        report = ResourceExhaustionReport.validate(
            self.payload, expected_owner_id=OWNER, inventory=self.observed
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LTVMCapacityStateStore(
                Path(directory) / "capacity.sqlite3",
                initial_cooldown=timedelta(minutes=10),
                maximum_cooldown=timedelta(minutes=25),
            )
            decisions = [
                store.record_exhaustion(
                    "change-1", f"run-{index}", OWNER, report,
                    at=NOW + timedelta(hours=index - 1),
                )
                for index in range(1, 5)
            ]
        durations = [
            decision.retry_not_before - (NOW + timedelta(hours=index))
            for index, decision in enumerate(decisions)
        ]
        self.assertEqual(
            durations,
            [
                timedelta(minutes=10),
                timedelta(minutes=20),
                timedelta(minutes=25),
                timedelta(minutes=25),
            ],
        )
        self.assertEqual([decision.exhaustion_count for decision in decisions], [1, 2, 3, 4])

    def test_same_run_cannot_be_reused_for_a_different_patch_or_report(self):
        report = ResourceExhaustionReport.validate(
            self.payload, expected_owner_id=OWNER, inventory=self.observed
        )
        changed = ResourceExhaustionReport(
            report.failed_operation,
            {"guest_memory_mib_per_node": 4096},
            report.evidence,
            report.owned_resource_names,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LTVMCapacityStateStore(Path(directory) / "capacity.sqlite3")
            store.record_exhaustion("change-1", "run-1", OWNER, report, at=NOW)
            with self.assertRaises(ValueError):
                store.record_exhaustion("change-2", "run-1", OWNER, report, at=NOW)
            with self.assertRaises(ValueError):
                store.record_exhaustion("change-1", "run-1", OWNER, changed, at=NOW)


if __name__ == "__main__":
    unittest.main()
