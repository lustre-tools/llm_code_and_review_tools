import os
import subprocess
import sys
import time
import unittest

from ltvm_guest_exec import GuestCommand, GuestExecutionError, GuestTarget, GuestTransportResult
from ltvm_resources import LTVMInventory, owner_id_for_session
from ltvm_ssh_transport import (
    GuestIsolationAssertion,
    LTVMSSHGuestTransport,
    run_bounded_process,
)


SESSION = "session-ssh-1"
OWNER = owner_id_for_session(SESSION)


def inventory(owner=OWNER, *, state="running"):
    return LTVMInventory.from_json(
        {
            "vms": [
                {
                    "name": "owned-vm",
                    "owner_id": owner,
                    "status": state,
                    "mem": 2048,
                    "ip": "192.0.2.10",
                }
            ]
        }
    )


class Provider:
    def __init__(self, observed):
        self.observed = observed
        self.calls = 0

    def inventory(self):
        self.calls += 1
        return self.observed


class RecordingRunner:
    def __init__(self, result=None):
        self.result = result or GuestTransportResult(0, b"ok")
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.result


class ConcreteSSHTransportTests(unittest.TestCase):
    def test_argv_is_local_shell_free_and_command_is_one_guest_argument(self):
        provider = Provider(inventory())
        runner = RecordingRunner()
        transport = LTVMSSHGuestTransport(
            inventory_provider=provider,
            isolation=GuestIsolationAssertion(True, True),
            runner=runner,
            ssh_binary="/usr/bin/ssh",
            local_path="/usr/bin:/bin",
        )
        command = GuestCommand(
            "build",
            argv=("custom tool", "argument with spaces", "$(host-must-not-run)"),
            cwd="/work/source tree",
            env=(("BUILD_MODE", "deep check"),),
        )

        result = transport.execute_guest(
            target=GuestTarget("owned-vm"),
            expected_owner_id=OWNER,
            command=command,
            timeout_seconds=44,
            max_output_bytes=1234,
            cancelled=lambda: False,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(provider.calls, 1)
        argv, kwargs = runner.calls[0]
        self.assertIsInstance(argv, tuple)
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertIn("PermitLocalCommand=no", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertIn("ProxyCommand=none", argv)
        self.assertIn("ProxyJump=none", argv)
        self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")
        self.assertEqual(argv[-4:-1], ("-l", "root", "192.0.2.10"))
        self.assertIn("'$(host-must-not-run)'", argv[-1])
        self.assertIn("cd -- '/work/source tree'", argv[-1])
        self.assertEqual(kwargs["timeout_seconds"], 44)
        self.assertEqual(kwargs["max_output_bytes"], 1234)
        self.assertEqual(kwargs["environment"], {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})

    def test_text_is_preserved_for_guest_shell_not_interpreted_by_host_runner(self):
        runner = RecordingRunner()
        transport = LTVMSSHGuestTransport(
            inventory_provider=Provider(inventory()),
            isolation=GuestIsolationAssertion(True, True),
            runner=runner,
        )
        text = "for f in *.ko; do test -s \"$f\" || exit 9; done"
        command = GuestCommand("diagnostic", text=text, cwd="/tmp")

        transport.execute_guest(
            target=GuestTarget("owned-vm"),
            expected_owner_id=OWNER,
            command=command,
            timeout_seconds=20,
            max_output_bytes=200,
            cancelled=lambda: False,
        )

        argv, _ = runner.calls[0]
        self.assertEqual(argv[-1], "cd -- /tmp && " + text)
        self.assertNotIn("sh", argv[:-1])

    def test_transport_itself_rechecks_exact_owner_immediately_before_ssh(self):
        for observed in (inventory(None), inventory("patch-watcher:other"), inventory(state="stopped")):
            with self.subTest(observed=observed):
                runner = RecordingRunner()
                transport = LTVMSSHGuestTransport(
                    inventory_provider=Provider(observed),
                    isolation=GuestIsolationAssertion(True, True),
                    runner=runner,
                )
                with self.assertRaises(GuestExecutionError):
                    transport.execute_guest(
                        target=GuestTarget("owned-vm"),
                        expected_owner_id=OWNER,
                        command=GuestCommand("test", argv=("true",)),
                        timeout_seconds=20,
                        max_output_bytes=200,
                        cancelled=lambda: False,
                    )
                self.assertEqual(runner.calls, [])

    def test_transport_refuses_non_literal_or_missing_inventory_address(self):
        value = inventory()
        row = dict(value.vms[0].raw)
        row["ip"] = "owned-vm.example.test"
        observed = LTVMInventory.from_json({"vms": [row]})
        runner = RecordingRunner()
        transport = LTVMSSHGuestTransport(
            inventory_provider=Provider(observed),
            isolation=GuestIsolationAssertion(True, True),
            runner=runner,
        )
        with self.assertRaisesRegex(GuestExecutionError, "valid inventory IP"):
            transport.execute_guest(
                target=GuestTarget("owned-vm"),
                expected_owner_id=OWNER,
                command=GuestCommand("test", argv=("true",)),
                timeout_seconds=20,
                max_output_bytes=200,
                cancelled=lambda: False,
            )
        self.assertEqual(runner.calls, [])

    def test_isolation_assertion_cannot_enable_credentials_or_gerrit_writes(self):
        with self.assertRaises(ValueError):
            GuestIsolationAssertion(False, True)
        with self.assertRaises(ValueError):
            GuestIsolationAssertion(True, False)

    def test_pre_cancel_does_not_read_inventory_or_start_ssh(self):
        provider = Provider(inventory())
        runner = RecordingRunner()
        transport = LTVMSSHGuestTransport(
            inventory_provider=provider,
            isolation=GuestIsolationAssertion(True, True),
            runner=runner,
        )
        result = transport.execute_guest(
            target=GuestTarget("owned-vm"),
            expected_owner_id=OWNER,
            command=GuestCommand("test", argv=("long-test",)),
            timeout_seconds=20,
            max_output_bytes=200,
            cancelled=lambda: True,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(runner.calls, [])


class BoundedProcessRunnerTests(unittest.TestCase):
    def test_real_transport_process_capture_is_combined_bounded(self):
        script = "import sys;sys.stdout.write('A'*10000);sys.stderr.write('B'*10000)"
        result = run_bounded_process(
            (sys.executable, "-c", script),
            timeout_seconds=5,
            max_output_bytes=777,
            cancelled=lambda: False,
            environment={"PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.stdout) + len(result.stderr), 777)
        self.assertEqual(result.stdout_observed_bytes + result.stderr_observed_bytes, 20000)

    def test_real_transport_process_times_out_and_is_terminated(self):
        result = run_bounded_process(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            timeout_seconds=1,
            max_output_bytes=100,
            cancelled=lambda: False,
            environment={"PATH": os.environ.get("PATH", "")},
        )
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)

    def test_real_transport_process_can_be_cancelled_while_active(self):
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 3

        result = run_bounded_process(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            timeout_seconds=20,
            max_output_bytes=100,
            cancelled=cancelled,
            environment={"PATH": os.environ.get("PATH", "")},
        )
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.exit_code)

    def test_cancellation_terminates_transport_descendants(self):
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 4

        script = (
            "import subprocess,time;"
            "p=subprocess.Popen(['sleep','10']);"
            "print(p.pid,flush=True);time.sleep(10)"
        )
        result = run_bounded_process(
            (sys.executable, "-c", script),
            timeout_seconds=20,
            max_output_bytes=100,
            cancelled=cancelled,
            environment={"PATH": os.environ.get("PATH", "")},
        )
        child_pid = int(result.stdout.decode().strip())
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("cancelled transport descendant remained alive")


if __name__ == "__main__":
    unittest.main()
