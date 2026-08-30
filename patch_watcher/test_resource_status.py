import json
import subprocess
import unittest
from datetime import datetime, timezone

import resource_status as resources


NOW = datetime(2026, 8, 30, 17, 12, 13, tzinfo=timezone.utc)


def completed(command, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, responses):
        self.responses = {tuple(key): value for key, value in responses.items()}
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        response = self.responses[tuple(command)]
        if isinstance(response, BaseException):
            raise response
        return response


class FakeReader:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        response = self.responses[path]
        if isinstance(response, BaseException):
            raise response
        return response


class HostMemoryTests(unittest.TestCase):
    def test_linux_uses_memavailable_and_reports_swap(self):
        reader = FakeReader({
            "/proc/meminfo": (
                "MemTotal:       16384000 kB\n"
                "MemFree:         1000000 kB\n"
                "MemAvailable:    6144000 kB\n"
                "Buffers:          100000 kB\n"
                "Cached:          4000000 kB\n"
                "SwapTotal:       2097152 kB\n"
                "SwapFree:        1572864 kB\n"
            )
        })

        result = resources.collect_host_memory(
            system="Linux", reader=reader, clock=lambda: NOW
        )

        self.assertEqual(result.quality, "good")
        self.assertEqual(result.total_bytes, 16384000 * 1024)
        self.assertEqual(result.available_bytes, 6144000 * 1024)
        self.assertEqual(result.used_bytes, (16384000 - 6144000) * 1024)
        self.assertEqual(result.swap_total_bytes, 2097152 * 1024)
        self.assertEqual(result.swap_used_bytes, 524288 * 1024)
        self.assertEqual(result.errors, ())

    def test_linux_estimates_available_on_older_kernel(self):
        reader = FakeReader({
            "/proc/meminfo": (
                "MemTotal: 10000 kB\n"
                "MemFree: 1000 kB\n"
                "Buffers: 200 kB\n"
                "Cached: 3000 kB\n"
                "SReclaimable: 500 kB\n"
                "Shmem: 100 kB\n"
            )
        })

        result = resources.collect_host_memory(
            system="linux", reader=reader, clock=lambda: NOW
        )

        self.assertEqual(result.quality, "estimated")
        self.assertEqual(result.available_bytes, 4600 * 1024)
        self.assertEqual(result.used_bytes, 5400 * 1024)
        self.assertEqual(result.errors[0].code, "memavailable_estimated")

    def test_linux_malformed_or_out_of_range_data_is_bounded(self):
        reader = FakeReader({
            "/proc/meminfo": (
                "MemTotal: 1000 kB\n"
                "MemAvailable: 2000 kB\n"
                "SwapTotal: not-a-number kB\n"
            )
        })

        result = resources.collect_host_memory(
            system="Linux", reader=reader, clock=lambda: NOW
        )

        self.assertEqual(result.available_bytes, result.total_bytes)
        self.assertEqual(result.used_bytes, 0)
        self.assertEqual(result.quality, "partial")
        self.assertEqual(
            {error.code for error in result.errors},
            {"malformed_meminfo", "invalid_available_memory"},
        )

    def test_linux_reader_failure_returns_unavailable_sample(self):
        result = resources.collect_host_memory(
            system="Linux",
            reader=FakeReader({"/proc/meminfo": PermissionError("denied")}),
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "unavailable")
        self.assertIsNone(result.total_bytes)
        self.assertEqual(result.errors[0].code, "meminfo_unavailable")
        self.assertEqual(result.sampled_at, NOW)

    def test_macos_parses_sysctl_vm_stat_and_swap(self):
        runner = FakeRunner({
            ("sysctl", "-n", "hw.memsize"): completed(
                [], "17179869184\n"
            ),
            ("vm_stat",): completed(
                [],
                "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                "Pages free:                               100.\n"
                "Pages active:                             500.\n"
                "Pages inactive:                           300.\n"
                "Pages speculative:                         50.\n",
            ),
            ("sysctl", "-n", "vm.swapusage"): completed(
                [], "total = 4096.00M  used = 512.50M  free = 3583.50M\n"
            ),
        })

        result = resources.collect_host_memory(
            system="Darwin", runner=runner, clock=lambda: NOW
        )

        self.assertEqual(result.quality, "estimated")
        self.assertEqual(result.total_bytes, 17179869184)
        self.assertEqual(result.available_bytes, 450 * 4096)
        self.assertEqual(result.used_bytes, 17179869184 - 450 * 4096)
        self.assertEqual(result.swap_total_bytes, 4096 * 1024 * 1024)
        self.assertEqual(result.swap_used_bytes, int(512.5 * 1024 * 1024))
        self.assertEqual(result.errors, ())
        self.assertTrue(all(call[1]["check"] is False for call in runner.calls))

    def test_macos_keeps_partial_total_when_vm_stat_and_swap_fail(self):
        runner = FakeRunner({
            ("sysctl", "-n", "hw.memsize"): completed([], "8589934592\n"),
            ("vm_stat",): completed([], stderr="not permitted", returncode=1),
            ("sysctl", "-n", "vm.swapusage"): FileNotFoundError(),
        })

        result = resources.collect_host_memory(
            system="macos", runner=runner, clock=lambda: NOW
        )

        self.assertEqual(result.quality, "partial")
        self.assertEqual(result.total_bytes, 8589934592)
        self.assertIsNone(result.available_bytes)
        self.assertEqual(
            {error.code for error in result.errors},
            {"vm_stat_failed", "swapusage_failed"},
        )

    def test_unsupported_platform_is_explicit(self):
        result = resources.collect_host_memory(system="Plan9", clock=lambda: NOW)
        self.assertEqual(result.quality, "unavailable")
        self.assertEqual(result.errors[0].code, "unsupported_platform")


class LTVMInventoryTests(unittest.TestCase):
    def _runner_for_payload(self, payload, extra=None):
        responses = {
            ("ltvm", "list", "--json"): completed([], json.dumps(payload))
        }
        responses.update(extra or {})
        return FakeRunner(responses)

    def test_current_ltvm_shape_keeps_configured_ram_separate_from_rss(self):
        payload = {
            "vms": [
                {
                    "name": "pw-vm",
                    "status": "running",
                    "pid": 4321,
                    "vcpus": 4,
                    "mem": 2048,
                    "ip": "192.0.2.4",
                    "owner_id": "patch-watcher:session-7f9c",
                }
            ],
            "totals": {"mem_used_mb": 2048},
        }
        reader = FakeReader({
            "/proc/4321/comm": "qemu-system-x86_64\n",
            "/proc/4321/cmdline": "\0".join(
                ["/usr/bin/qemu-system-x86_64", "-name", "pw-vm", "-m", "2048", ""]
            ),
            "/proc/4321/status": "Name:\tqemu\nVmRSS:\t 777000 kB\n",
        })

        inventory = resources.collect_ltvm_inventory(
            system="Linux",
            runner=self._runner_for_payload(payload),
            reader=reader,
            clock=lambda: NOW,
        )

        self.assertEqual(inventory.quality, "good")
        vm = inventory.vms[0]
        self.assertEqual(vm.patch_watcher_session_id, "session-7f9c")
        self.assertEqual(vm.configured_guest_memory_bytes, 2048 * 1024 * 1024)
        self.assertEqual(vm.host_rss_bytes, 777000 * 1024)
        self.assertNotEqual(vm.configured_guest_memory_bytes, vm.host_rss_bytes)
        self.assertEqual(vm.host_memory_source, "/proc/4321/status VmRSS")

    def test_accepts_plain_list_and_nested_standard_envelope(self):
        shapes = [
            [{"name": "one", "state": "stopped", "memory_mb": "1024"}],
            {
                "ok": True,
                "data": {
                    "result": {
                        "machines": [
                            {
                                "name": "one",
                                "state": "down",
                                "configured_guest_memory_bytes": 1073741824,
                            }
                        ]
                    }
                },
                "meta": {"warnings": []},
            },
        ]
        for payload in shapes:
            with self.subTest(payload=payload):
                result = resources.collect_ltvm_inventory(
                    system="Linux",
                    runner=self._runner_for_payload(payload),
                    reader=FakeReader({}),
                    clock=lambda: NOW,
                )
                self.assertEqual(result.quality, "good")
                self.assertEqual(len(result.vms), 1)
                self.assertEqual(
                    result.vms[0].configured_guest_memory_bytes, 1024**3
                )

    def test_malformed_rows_are_skipped_without_losing_valid_vms(self):
        payload = {
            "vms": [
                "not-an-object",
                {"status": "stopped", "mem": 1000},
                {
                    "name": "valid",
                    "status": "stopped",
                    "pid": "bad",
                    "mem": "bad",
                    "owner_id": {"unexpected": True},
                },
            ]
        }

        result = resources.collect_ltvm_inventory(
            system="Linux",
            runner=self._runner_for_payload(payload),
            reader=FakeReader({}),
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "partial")
        self.assertEqual([vm.name for vm in result.vms], ["valid"])
        self.assertEqual(result.vms[0].quality, "partial")
        codes = [error.code for error in result.errors]
        self.assertEqual(codes.count("invalid_vm_row"), 2)
        self.assertIn("invalid_owner_id", codes)
        self.assertIn("invalid_configured_memory", codes)
        self.assertIn("invalid_pid", codes)

    def test_stopped_vm_does_not_probe_stale_pid(self):
        payload = {
            "vms": [
                {"name": "old", "status": "stopped", "pid": 999, "mem": 2048}
            ]
        }
        reader = FakeReader({})

        result = resources.collect_ltvm_inventory(
            system="Linux",
            runner=self._runner_for_payload(payload),
            reader=reader,
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "good")
        self.assertEqual(result.vms[0].process_id, 999)
        self.assertIsNone(result.vms[0].host_rss_bytes)
        self.assertEqual(reader.calls, [])

    def test_pid_reuse_is_not_misattributed_to_vm(self):
        payload = {
            "vms": [{"name": "vm", "status": "running", "pid": 55, "mem": 2048}]
        }
        reader = FakeReader({"/proc/55/comm": "python3\n"})

        result = resources.collect_ltvm_inventory(
            system="Linux",
            runner=self._runner_for_payload(payload),
            reader=reader,
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "partial")
        self.assertIsNone(result.vms[0].host_rss_bytes)
        self.assertEqual(
            result.vms[0].errors[0].code, "process_identity_mismatch"
        )
        self.assertNotIn("/proc/55/status", reader.calls)

    def test_macos_ps_uses_full_command_to_avoid_truncated_comm(self):
        payload = {
            "vms": [{"name": "arm-vm", "status": "up", "pid": 77, "mem": 2048}]
        }
        runner = self._runner_for_payload(
            payload,
            {
                ("ps", "-p", "77", "-o", "rss=", "-o", "command="): completed(
                    [],
                    "65536 /opt/qemu/bin/qemu-system-aarch64 -name arm-vm -m 2048\n",
                )
            },
        )

        result = resources.collect_ltvm_inventory(
            system="Darwin", runner=runner, reader=FakeReader({}), clock=lambda: NOW
        )

        self.assertEqual(result.quality, "good")
        self.assertEqual(result.vms[0].host_rss_bytes, 65536 * 1024)
        self.assertEqual(result.vms[0].host_memory_source, "ps rss")

    def test_missing_ltvm_is_a_sample_not_an_exception(self):
        runner = FakeRunner({
            ("ltvm", "list", "--json"): FileNotFoundError()
        })

        result = resources.collect_ltvm_inventory(
            system="Linux", runner=runner, reader=FakeReader({}), clock=lambda: NOW
        )

        self.assertEqual(result.quality, "unavailable")
        self.assertEqual(result.vms, ())
        self.assertEqual(result.errors[0].code, "ltvm_list_failed")
        self.assertIn("not installed", result.errors[0].message)

    def test_invalid_json_and_unknown_payload_are_reported(self):
        invalid_json_runner = FakeRunner({
            ("ltvm", "list", "--json"): completed([], "{broken")
        })
        invalid_payload_runner = self._runner_for_payload({"totals": {}})

        invalid_json = resources.collect_ltvm_inventory(
            runner=invalid_json_runner, clock=lambda: NOW
        )
        invalid_payload = resources.collect_ltvm_inventory(
            runner=invalid_payload_runner, clock=lambda: NOW
        )

        self.assertEqual(invalid_json.errors[0].code, "invalid_json")
        self.assertEqual(invalid_payload.errors[0].code, "invalid_payload")
        self.assertEqual(invalid_json.quality, "unavailable")
        self.assertEqual(invalid_payload.quality, "unavailable")

    def test_external_and_legacy_owners_are_not_adopted(self):
        payload = {
            "vms": [
                {
                    "name": "legacy",
                    "status": "stopped",
                    "owner_id": None,
                    "mem": 1024,
                },
                {
                    "name": "other",
                    "status": "stopped",
                    "owner_id": "pid:42",
                    "mem": 1024,
                },
                {
                    "name": "empty-session",
                    "status": "stopped",
                    "owner_id": "patch-watcher:",
                    "mem": 1024,
                },
            ]
        }

        result = resources.collect_ltvm_inventory(
            runner=self._runner_for_payload(payload), clock=lambda: NOW
        )

        self.assertEqual(
            [vm.patch_watcher_session_id for vm in result.vms], [None, None, None]
        )

    def test_aggregate_totals_do_not_turn_unknown_measurements_into_zero(self):
        payload = {
            "vms": [
                {"name": "known", "status": "stopped", "mem": 1024},
                {"name": "unknown-config", "status": "stopped"},
                {"name": "unknown-rss", "status": "running", "pid": 55, "mem": 512},
            ]
        }
        reader = FakeReader({"/proc/55/comm": "not-qemu\n"})

        result = resources.collect_ltvm_inventory(
            system="Linux",
            runner=self._runner_for_payload(payload),
            reader=reader,
            clock=lambda: NOW,
        ).to_dict()

        self.assertIsNone(result["configured_guest_memory_bytes"])
        self.assertEqual(result["known_configured_guest_memory_bytes"], 1536 * 1024**2)
        self.assertEqual(result["configured_memory_known_vm_count"], 2)
        self.assertIsNone(result["measured_host_rss_bytes"])
        self.assertEqual(result["known_host_rss_bytes"], 0)
        self.assertEqual(result["host_rss_measured_vm_count"], 0)
        self.assertEqual(result["running_vm_count"], 1)


class ProcessTreeTests(unittest.TestCase):
    COMMAND = ("ps", "-axo", "pid=,ppid=,rss=,command=")

    def test_sums_root_and_all_descendants_once(self):
        runner = FakeRunner({
            self.COMMAND: completed(
                [],
                "100 1 10000 /usr/local/bin/claude --session abc\n"
                "101 100 2000 /usr/bin/node child.js\n"
                "102 100 3000 /usr/bin/helper\n"
                "103 101 4000 /usr/bin/grandchild\n"
                "104 999 9000 /usr/bin/unrelated\n",
            )
        })

        result = resources.collect_process_tree_rss(
            100,
            expected_command="claude",
            runner=runner,
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "estimated")
        self.assertEqual(result.total_rss_bytes, 19000 * 1024)
        self.assertEqual(result.known_rss_bytes, 19000 * 1024)
        self.assertEqual(result.process_count, 4)
        self.assertEqual(result.measured_process_count, 4)
        self.assertEqual(result.process_ids, (100, 101, 103, 102))
        self.assertEqual(result.errors, ())
        self.assertNotIn(104, result.process_ids)

    def test_duplicate_pid_and_cycle_cannot_double_count(self):
        runner = FakeRunner({
            self.COMMAND: completed(
                [],
                "100 1 100 /bin/claude\n"
                "101 100 20 /bin/child\n"
                "101 100 20 /bin/child-duplicate\n"
                "100 101 100 /bin/claude-duplicate\n",
            )
        })

        result = resources.collect_process_tree_rss(
            100,
            expected_command="claude",
            runner=runner,
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "partial")
        self.assertEqual(result.total_rss_bytes, 120 * 1024)
        self.assertEqual(result.process_ids, (100, 101))
        self.assertEqual(result.process_count, 2)
        self.assertEqual(
            [error.code for error in result.errors],
            ["duplicate_process_row", "duplicate_process_row"],
        )

    def test_invalid_descendant_rss_exposes_only_known_subtotal(self):
        runner = FakeRunner({
            self.COMMAND: completed(
                [],
                "100 1 100 /bin/claude\n"
                "101 100 - /bin/child\n"
                "200 1 500 /bin/unrelated\n",
            )
        })

        result = resources.collect_process_tree_rss(
            100,
            expected_command="/bin/claude",
            runner=runner,
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "partial")
        self.assertIsNone(result.total_rss_bytes)
        self.assertEqual(result.known_rss_bytes, 100 * 1024)
        self.assertEqual(result.process_count, 2)
        self.assertEqual(result.measured_process_count, 1)
        self.assertEqual(result.errors[0].code, "invalid_process_rss")

    def test_expected_identity_blocks_reused_root_pid(self):
        runner = FakeRunner({
            self.COMMAND: completed([], "100 1 99999 /usr/bin/python server.py\n")
        })

        result = resources.collect_process_tree_rss(
            100,
            expected_command="claude",
            runner=runner,
            clock=lambda: NOW,
        )

        self.assertEqual(result.quality, "unavailable")
        self.assertIsNone(result.total_rss_bytes)
        self.assertEqual(result.known_rss_bytes, 0)
        self.assertEqual(result.errors[-1].code, "root_identity_mismatch")

    def test_missing_identity_is_partial_but_still_measured(self):
        runner = FakeRunner({
            self.COMMAND: completed([], "100 1 321 /usr/bin/claude\n")
        })

        result = resources.collect_process_tree_rss(
            100, runner=runner, clock=lambda: NOW
        )

        self.assertEqual(result.quality, "partial")
        self.assertEqual(result.total_rss_bytes, 321 * 1024)
        self.assertEqual(result.errors[0].code, "root_identity_unverified")

    def test_missing_process_and_command_failure_are_unavailable(self):
        missing = resources.collect_process_tree_rss(
            100,
            expected_command="claude",
            runner=FakeRunner({self.COMMAND: completed([], "200 1 1 /bin/other\n")}),
            clock=lambda: NOW,
        )
        failed = resources.collect_process_tree_rss(
            100,
            expected_command="claude",
            runner=FakeRunner({
                self.COMMAND: completed([], stderr="ps failed", returncode=1)
            }),
            clock=lambda: NOW,
        )

        self.assertEqual(missing.quality, "unavailable")
        self.assertEqual(missing.errors[-1].code, "root_process_not_found")
        self.assertEqual(failed.quality, "unavailable")
        self.assertEqual(failed.errors[0].code, "process_table_failed")

    def test_projection_is_json_serializable(self):
        runner = FakeRunner({
            self.COMMAND: completed([], "100 1 10 /bin/claude\n")
        })
        projected = resources.collect_process_tree_rss(
            100,
            expected_command="claude",
            runner=runner,
            clock=lambda: NOW,
        ).to_dict()

        self.assertEqual(projected["process_ids"], [100])
        self.assertEqual(projected["sampled_at"], "2026-08-30T17:12:13Z")
        json.dumps(projected)


class SnapshotProjectionTests(unittest.TestCase):
    def test_snapshot_is_timestamp_aligned_and_ui_ready(self):
        meminfo = (
            "MemTotal: 8192 kB\nMemAvailable: 4096 kB\n"
            "SwapTotal: 0 kB\nSwapFree: 0 kB\n"
        )
        runner = FakeRunner({
            ("ltvm", "list", "--json"): completed(
                [],
                json.dumps({
                    "vms": [
                        {"name": "a", "status": "stopped", "mem": 2},
                        {"name": "b", "status": "stopped", "mem": 3},
                    ]
                }),
            )
        })
        clock_calls = []

        def clock():
            clock_calls.append(True)
            return NOW

        snapshot = resources.collect_resource_snapshot(
            system="Linux",
            runner=runner,
            reader=FakeReader({"/proc/meminfo": meminfo}),
            clock=clock,
        )
        projected = snapshot.to_dict()

        self.assertEqual(len(clock_calls), 1)
        self.assertEqual(snapshot.sampled_at, snapshot.host_memory.sampled_at)
        self.assertEqual(snapshot.sampled_at, snapshot.ltvm.sampled_at)
        self.assertEqual(projected["sampled_at"], "2026-08-30T17:12:13Z")
        self.assertEqual(projected["quality"], "good")
        self.assertEqual(projected["host_memory"]["used_bytes"], 4096 * 1024)
        self.assertEqual(projected["ltvm"]["vm_count"], 2)
        self.assertEqual(
            projected["ltvm"]["configured_guest_memory_bytes"], 5 * 1024 * 1024
        )
        self.assertEqual(projected["ltvm"]["measured_host_rss_bytes"], 0)
        json.dumps(projected)  # Public projection is JSON serializable.


if __name__ == "__main__":
    unittest.main()
