import contextlib
import errno
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path
from types import SimpleNamespace

from patch_watcher import claude_runner
from patch_watcher.claude_runner import (
    ENGINEERING_REPORT_SCHEMA,
    MAX_EVENT_TAIL,
    PROTOCOL_VERSION,
    READ_ONLY_REPORT_SCHEMA,
    ClaudeHost,
    ClaudeRunner,
    ClaudeRunnerError,
    ProcessIdentity,
    ReadOnlyRunSpec,
    RunnerHandle,
    RunnerIdentityError,
    RunnerProtocolError,
    RunnerStateError,
    _safe_environment,
    build_read_only_claude_command,
    request_host_stop,
    validate_engineering_report,
    validate_read_only_report,
)


class FakeStdin:
    def __init__(self):
        self.writes = []
        self.flushes = 0

    def write(self, value):
        self.writes.append(value)
        return len(value)

    def flush(self):
        self.flushes += 1


class FakeStdout:
    _END = object()

    def __init__(self):
        self.values = queue.Queue()

    def feed(self, value):
        self.values.put(value)

    def close(self):
        self.values.put(self._END)

    def __iter__(self):
        return self

    def __next__(self):
        value = self.values.get()
        if value is self._END:
            raise StopIteration
        return value


class FakeProcess:
    def __init__(self, pid=42420):
        self.pid = pid
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.returncode = None
        self.command = None
        self.options = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise TimeoutError()
        return self.returncode


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class ControlSocketPathTests(unittest.TestCase):
    """The socket used to live under the run's runtime directory, where a real
    engineering run id made it 108 characters -- one over the AF_UNIX limit --
    so every such run died with "AF_UNIX path too long" before its socket
    existed."""

    def test_a_real_run_id_fits_and_is_stable_and_private(self):
        from patch_watcher.claude_runner import _SUN_PATH_MAX, control_socket_path

        with tempfile.TemporaryDirectory() as short:
            with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": short}):
                path = control_socket_path("pw-engineer-35302-ps4-8e7bad2a5334")
                self.assertLessEqual(len(str(path)), _SUN_PATH_MAX)
                self.assertEqual(
                    path, control_socket_path("pw-engineer-35302-ps4-8e7bad2a5334"),
                )
                self.assertNotEqual(path, control_socket_path("pw-engineer-other"))
                self.assertEqual(path.parent.name, "patch-watcher")

    def test_a_runtime_dir_too_long_is_a_clear_error_not_an_oserror(self):
        from patch_watcher.claude_runner import ClaudeRunnerError, control_socket_path

        with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/" + "d" * 120}):
            with self.assertRaisesRegex(ClaudeRunnerError, "AF_UNIX limit"):
                control_socket_path("pw-engineer-1")


class ClaudeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.servers = []
        self.hosts = []
        self.processes = []
        self.base = Path(self.temporary.name)
        self.cwd = self.base / "checkout"
        self.cwd.mkdir()
        self.runtime = self.base / "runtime"
        self.session_id = str(uuid.uuid4())

    def tearDown(self):
        for host, process, thread in reversed(self.servers):
            self._stop_server(host, process, thread)
        for process in self.processes:
            if process.returncode is None:
                process.returncode = -9
                process.stdout.close()
        for host in self.hosts:
            if host._reader_thread is not None:
                host._reader_thread.join(timeout=1)
        self.temporary.cleanup()

    def spec(self, **overrides):
        values = {
            "run_id": "run-17",
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "runtime_dir": str(self.runtime),
            "prompt": "Read the pinned evidence and report findings. Do not modify anything.",
            "name": "pw-68160-ps4-run17",
            "model": "fable",
            "effort": "high",
        }
        values.update(overrides)
        return ReadOnlyRunSpec(**values)

    def identity_reader(self, pid):
        if pid == os.getpid():
            return ProcessIdentity(pid, "host-start", os.getpgid(pid))
        if pid == 42420:
            return ProcessIdentity(pid, "claude-start", 42420)
        if pid == 51234:
            return ProcessIdentity(pid, "launcher-start", 51234)
        raise ProcessLookupError(pid)

    def make_host(self):
        process = FakeProcess()
        self.processes.append(process)
        calls = []

        def factory(command, **options):
            process.command = command
            process.options = options
            return process

        def signal_group(pgid, signum):
            calls.append((pgid, signum))
            if signum in {signal.SIGTERM, signal.SIGKILL}:
                process.returncode = -signum
                process.stdout.close()

        host = ClaudeHost(
            self.spec(), process_factory=factory, identity_reader=self.identity_reader,
            signal_group=signal_group,
        )
        self.hosts.append(host)
        return host, process, calls

    def start_server(self):
        host, process, calls = self.make_host()
        thread = threading.Thread(target=host.serve, daemon=True)
        thread.start()
        def control_ready():
            if not host.socket_path.exists() or host.handle is None:
                return False
            try:
                ClaudeRunner(identity_reader=self.identity_reader).status(host.handle)
                return True
            except (OSError, RunnerProtocolError):
                return False

        wait_for(control_ready)
        self.servers.append((host, process, thread))
        return host, process, calls, thread

    @staticmethod
    def _stop_server(host, process, thread):
        if thread.is_alive():
            with contextlib.suppress(RunnerStateError, RunnerIdentityError):
                host.request_stop(True)
            process.returncode = process.returncode if process.returncode is not None else -9
            process.stdout.close()
            thread.join(timeout=2)

    def test_a_signal_never_downgrades_an_operator_forced_stop(self):
        """SIGTERM used to clear stop_force, undoing an explicit kill.

        The operator's kill sets stop_force over the control socket and the
        controller's stop ladder then signals this host too, so the two arrive
        in that order routinely. Clearing the flag sent the serve-loop
        finalizer down the graceful branch and made it wait out a five second
        grace the operator had declined.
        """

        host = SimpleNamespace(stopping=False, stop_force=True)
        request_host_stop(host)
        self.assertTrue(host.stopping)
        self.assertTrue(host.stop_force, "a signal downgraded a forced stop")

    def test_a_signal_alone_still_asks_for_a_graceful_stop(self):
        host = SimpleNamespace(stopping=False, stop_force=False)
        request_host_stop(host)
        self.assertTrue(host.stopping)
        self.assertFalse(host.stop_force, "a bare signal must not force")

    def test_run_spec_rejects_home_and_invalid_session(self):
        with self.assertRaisesRegex(ValueError, "home directory"):
            self.spec(cwd=str(Path.home())).validate()
        with self.assertRaisesRegex(ValueError, "UUID"):
            self.spec(session_id="not-a-session").validate()
        with self.assertRaisesRegex(ValueError, "Claude Code executable"):
            self.spec(claude_binary="/bin/sh").validate()

    def test_read_only_command_has_no_shell_or_write_tools(self):
        command = build_read_only_claude_command(self.spec())
        self.assertEqual(command[0], "claude")
        for required in (
            "--input-format", "stream-json", "--output-format", "--restricted",
            "--strict-mcp-config", "--safe-mode", "dontAsk", "Read,Glob,Grep",
        ):
            self.assertIn(required, command)
        command_text = " ".join(command)
        self.assertNotIn("Bash", command_text)
        self.assertNotIn("Edit", command_text)
        self.assertNotIn("Write", command_text)
        self.assertNotIn(self.spec().prompt, command_text)
        schema_index = command.index("--json-schema") + 1
        self.assertEqual(json.loads(command[schema_index]), READ_ONLY_REPORT_SCHEMA)

    def test_no_profile_accepts_an_mcp_server(self):
        """The only server ever brokered was pw_ltvm, whose module is gone."""
        config = json.dumps({"mcpServers": {
            "pw_ltvm": {
                "command": "/usr/bin/python3",
                "args": ["/private/pw_ltvm_mcp.py"],
            }
        }})
        for profile, report in (("read_only", "read_only"), ("full", "engineering")):
            with self.subTest(profile=profile), self.assertRaisesRegex(
                ValueError, "MCP is unavailable"
            ):
                self.spec(
                    report_kind=report,
                    capability_profile=profile,
                    mcp_config_json=config,
                ).validate()

    def test_engineering_report_validation(self):
        report = validate_engineering_report({
            "schema": "patch-watcher-engineering-report/v1",
            "state": "complete",
            "summary": "  Implemented the bounded change. ",
            "changed_files": ["src/file.c"],
            "validation_requests": [{
                "name": "unit tests", "target": "rocky9-arm64",
                "argv": ["make", "test"],
                "evidence_role": "test",
            }],
        })
        self.assertEqual(report["summary"], "Implemented the bounded change.")
        self.assertEqual(report["validation_requests"][0]["argv"], ["make", "test"])
        self.assertEqual(report["validation_requests"][0]["evidence_role"], "test")
        with self.assertRaisesRegex(RunnerProtocolError, "checkout-relative"):
            validate_engineering_report({
                **report, "changed_files": ["../outside"],
            })

    def test_review_engineering_report_binds_comment_results(self):
        report = validate_engineering_report({
            "schema": "patch-watcher-engineering-report/v1",
            "state": "complete", "summary": "Handled one comment",
            "changed_files": ["src/file.c"], "validation_requests": [],
            "review_mode": "simple",
            "review_snapshot_sha256": "a" * 64,
            "comment_results": [{
                "comment_id": "comment-1", "assessment": "simple",
                "disposition": "addressed",
                "summary": "Renamed the local variable",
                "reply_draft": "Done in the next patchset.",
                "changed_files": ["src/file.c"],
            }],
        })
        self.assertEqual(report["review_mode"], "simple")
        self.assertEqual(report["comment_results"][0]["comment_id"], "comment-1")
        with self.assertRaisesRegex(RunnerProtocolError, "review snapshot"):
            validate_engineering_report({
                **report, "review_snapshot_sha256": "bad",
            })

    def test_build_engineering_report_binds_failure_resolution(self):
        report = validate_engineering_report({
            "schema": "patch-watcher-engineering-report/v1",
            "state": "complete", "summary": "Fixed the compile failure",
            "changed_files": ["src/file.c"], "validation_requests": [],
            "jenkins_snapshot_sha256": "b" * 64,
            "jenkins_resolution": {
                "build_id": "lustre-reviews/123",
                "classification": "patch_caused_fixed",
                "diagnosis": "A missing declaration caused the failed build.",
            },
        })
        self.assertEqual(
            report["jenkins_resolution"]["classification"], "patch_caused_fixed"
        )
        with self.assertRaisesRegex(RunnerProtocolError, "Jenkins snapshot"):
            validate_engineering_report({
                **report, "jenkins_snapshot_sha256": "bad",
            })

    def test_full_capability_and_report_kind_must_match(self):
        with self.assertRaisesRegex(ValueError, "requires an engineering report"):
            self.spec(capability_profile="full").validate()
        with self.assertRaisesRegex(ValueError, "engineering reports require"):
            self.spec(report_kind="engineering").validate()

    def test_read_only_report_validation(self):
        report = validate_read_only_report({
            "schema": "patch-watcher-read-only-report/v1",
            "state": "complete",
            "summary": "  The evidence is consistent.  ",
            "findings": ["  No write was attempted.  "],
        })
        self.assertEqual(report["summary"], "The evidence is consistent.")
        self.assertEqual(report["findings"], ["No write was attempted."])
        with self.assertRaisesRegex(RunnerProtocolError, "requires question"):
            validate_read_only_report({
                "schema": "patch-watcher-read-only-report/v1",
                "state": "needs_input", "summary": "Need a choice", "findings": [],
            })

    def test_stream_validates_structured_report(self):
        host, process, _calls, _thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        process.stdout.feed(json.dumps({
            "type": "result", "result": "Finished",
            "structured_output": {
                "schema": "patch-watcher-read-only-report/v1",
                "state": "complete", "summary": "Finished safely", "findings": ["Read only"],
            },
        }) + "\n")
        wait_for(lambda: any(event.type == "worker_report" for event in runner.events(host.handle)))
        report = next(event for event in runner.events(host.handle) if event.type == "worker_report")
        self.assertEqual(report.payload["state"], "complete")

        process.stdout.feed(json.dumps({
            "type": "result", "result": "Need input",
            "structured_output": {
                "schema": "patch-watcher-read-only-report/v1",
                "state": "needs_input", "summary": "Missing question", "findings": [],
            },
        }) + "\n")
        wait_for(lambda: any(event.type == "worker_report_invalid" for event in runner.events(host.handle)))
        with self.assertRaisesRegex(RunnerProtocolError, "unknown fields"):
            validate_read_only_report({
                "schema": "patch-watcher-read-only-report/v1",
                "state": "failed", "summary": "Failed", "findings": [], "command": "rm",
            })

    def test_result_without_structured_report_is_invalid(self):
        host, process, _calls, _thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        process.stdout.feed(json.dumps({"type": "result", "result": "Plain prose"}) + "\n")
        wait_for(lambda: any(event.type == "worker_report_invalid" for event in runner.events(host.handle)))
        invalid = next(event for event in runner.events(host.handle) if event.type == "worker_report_invalid")
        self.assertEqual(invalid.payload["reason"], "missing_structured_output")

    def test_environment_removes_external_service_credentials(self):
        environment = _safe_environment({
            "PATH": "/bin", "GERRIT_PASS": "secret", "JENKINS_TOKEN": "secret",
            "JIRA_API_KEY": "secret", "ANTHROPIC_API_KEY": "model-secret",
        })
        self.assertNotIn("GERRIT_PASS", environment)
        self.assertNotIn("JENKINS_TOKEN", environment)
        self.assertNotIn("JIRA_API_KEY", environment)
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "model-secret")
        self.assertEqual(environment["PATCH_WATCHER_CAPABILITY_PROFILE"], "read_only")

    def test_host_launch_is_shell_free_and_paths_are_private(self):
        host, process, _calls = self.make_host()
        handle = host.start()
        self.addCleanup(process.stdout.close)
        self.assertFalse(process.options["shell"])
        self.assertTrue(process.options["start_new_session"])
        self.assertEqual(process.options["cwd"], str(self.cwd.resolve()))
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(handle.event_log_path).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(handle.state_path).stat().st_mode & 0o777, 0o600)
        initial = json.loads(process.stdin.writes[0])
        self.assertEqual(initial["type"], "user")
        self.assertIn("pinned evidence", initial["message"]["content"][0]["text"])

    def test_socket_is_private_and_new_runner_adopts_live_host(self):
        host, _process, _calls, _thread = self.start_server()
        self.assertEqual(host.socket_path.stat().st_mode & 0o777, 0o600)
        first_controller = ClaudeRunner(identity_reader=self.identity_reader)
        first = first_controller.adopt(host.handle)
        second_controller = ClaudeRunner(identity_reader=self.identity_reader)
        adopted = second_controller.adopt(RunnerHandle.from_dict(first.handle.to_dict()))
        self.assertEqual(adopted.handle.session_id, self.session_id)
        self.assertEqual(adopted.state, "running")
        self.assertTrue(second_controller.probe(host.handle).adoptable)

    def test_guidance_is_queued_at_turn_boundary_and_duplicate_rejected(self):
        host, process, _calls, _thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        delivery = runner.queue_guidance(host.handle, "message:42", "Please check the second log.")
        duplicate = runner.queue_guidance(host.handle, "message:42", "This is ignored.")
        self.assertEqual(delivery.state, "queued")
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(process.stdin.writes), 1)
        process.stdout.feed(json.dumps({"type": "result", "result": "First turn done"}) + "\n")
        wait_for(lambda: len(process.stdin.writes) == 2)
        sent = json.loads(process.stdin.writes[1])
        self.assertEqual(sent["message"]["content"][0]["text"], "Please check the second log.")
        later_duplicate = runner.queue_guidance(host.handle, "message:42", "Still ignored.")
        self.assertEqual(later_duplicate.state, "sent")
        self.assertTrue(later_duplicate.duplicate)
        self.assertEqual(len(process.stdin.writes), 2)

    def test_a_new_message_never_overtakes_one_already_queued(self):
        """Idle is not enough to deliver immediately; the queue must drain first.

        The reader thread marks the turn idle and only reaches
        _deliver_next_pending several fsyncing appends later. A message
        accepted in that window was written straight to stdin ahead of one
        that had been waiting since the previous turn -- both delivered, both
        marked delivered, and the agent read them in the wrong order.
        """

        host, process, _calls, _thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        first = runner.queue_guidance(host.handle, "message:first", "FIRST do this.")
        self.assertEqual(first.state, "queued")

        # Reproduce the window: the turn is idle, but the queued message has
        # not been flushed yet.
        host.turn_state = "idle"
        second = runner.queue_guidance(host.handle, "message:second", "SECOND do that.")
        self.assertEqual(
            second.state, "queued", "a later message jumped one already waiting"
        )
        self.assertEqual(len(process.stdin.writes), 1)

        process.stdout.feed(
            json.dumps({"type": "result", "result": "turn done"}) + "\n"
        )
        wait_for(lambda: len(process.stdin.writes) == 2)
        process.stdout.feed(
            json.dumps({"type": "result", "result": "turn done"}) + "\n"
        )
        wait_for(lambda: len(process.stdin.writes) == 3)

        delivered = [
            json.loads(write)["message"]["content"][0]["text"]
            for write in process.stdin.writes[1:]
        ]
        self.assertEqual(delivered, ["FIRST do this.", "SECOND do that."])

    def test_delivery_id_is_durable_in_private_log(self):
        host, _process, _calls, _thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        runner.queue_guidance(host.handle, "message:durable", "Queue this once")
        lines = [json.loads(line) for line in Path(host.handle.event_log_path).read_text().splitlines()]
        ids = [line["payload"].get("delivery_id") for line in lines]
        self.assertIn("message:durable", ids)
        log_text = Path(host.handle.event_log_path).read_text()
        self.assertNotIn("Queue this once", log_text)
        self.assertIn("content_sha256", log_text)
        self.assertEqual(Path(host.handle.event_log_path).stat().st_mode & 0o777, 0o600)

    def test_stream_events_are_cursor_bounded_and_redacted(self):
        host, process, _calls, _thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        process.stdout.feed(json.dumps({
            "type": "assistant", "authorization": "secret",
            "message": {"content": [{"type": "text", "text": "Working"}]},
        }) + "\n")
        process.stdout.feed(json.dumps({"type": "result", "result": "Done"}) + "\n")
        wait_for(lambda: runner.status(host.handle).last_message == "Done")
        events = runner.events(host.handle, after_cursor=0, limit=MAX_EVENT_TAIL + 50)
        self.assertLessEqual(len(events), MAX_EVENT_TAIL)
        self.assertEqual([event.cursor for event in events], sorted(event.cursor for event in events))
        assistant_event = next(event for event in events if event.payload.get("type") == "assistant")
        self.assertEqual(assistant_event.payload["authorization"], "<redacted>")
        after = runner.events(host.handle, after_cursor=assistant_event.cursor, limit=10)
        self.assertTrue(all(event.cursor > assistant_event.cursor for event in after))

    def test_invalid_stream_line_records_protocol_error_without_content(self):
        host, process, _calls, _thread = self.start_server()
        process.stdout.feed("not-json-with-secret\n")
        wait_for(lambda: any(event.type == "protocol_error" for event in host.event_tail()))
        log = Path(host.handle.event_log_path).read_text()
        self.assertNotIn("not-json-with-secret", log)

    def test_interrupt_targets_verified_claude_process_group(self):
        host, _process, calls, _thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        runner.interrupt(host.handle)
        self.assertIn((42420, signal.SIGINT), calls)
        self.assertEqual(runner.status(host.handle).turn_state, "interrupting")

    def test_pid_reuse_prevents_signal(self):
        host, _process, calls = self.make_host()
        host.start()
        self.addCleanup(host.process.stdout.close)

        def reused(pid):
            identity = self.identity_reader(pid)
            if pid == 42420:
                return ProcessIdentity(pid, "different-start", identity.process_group_id)
            return identity

        host.identity_reader = reused
        with self.assertRaisesRegex(RunnerIdentityError, "reused PID"):
            host.interrupt()
        self.assertEqual(calls, [])

    def test_terminate_and_kill_are_distinct(self):
        host, _process, calls, thread = self.start_server()
        runner = ClaudeRunner(identity_reader=self.identity_reader)
        runner.terminate(host.handle)
        wait_for(lambda: not thread.is_alive())
        self.assertIn((42420, signal.SIGTERM), calls)

        self.runtime = self.base / "runtime-two"
        host2, _process2, calls2, thread2 = self.start_server()
        runner.kill(host2.handle)
        wait_for(lambda: not thread2.is_alive())
        self.assertIn((42420, signal.SIGKILL), calls2)

    def test_control_protocol_rejects_wrong_version_and_unknown_request(self):
        host, process, _calls = self.make_host()
        host.start()
        self.addCleanup(process.stdout.close)
        with self.assertRaisesRegex(RunnerProtocolError, "unsupported"):
            host.handle_request({"protocol": "wrong", "type": "status"})
        with self.assertRaisesRegex(RunnerProtocolError, "unknown"):
            host.handle_request({"protocol": PROTOCOL_VERSION, "type": "dance"})

    def test_probe_detects_missing_and_reused_host_pid(self):
        handle = RunnerHandle(
            run_id="run-17", session_id=self.session_id,
            socket_path=str(self.runtime / "missing.sock"),
            event_log_path=str(self.runtime / "events.jsonl"),
            state_path=str(self.runtime / "host-state.json"),
            host_identity=ProcessIdentity(9000, "old", 9000),
        )
        missing = ClaudeRunner(identity_reader=lambda _pid: (_ for _ in ()).throw(ProcessLookupError()))
        self.assertEqual(missing.probe(handle).reason, "host_process_missing")
        reused = ClaudeRunner(identity_reader=lambda pid: ProcessIdentity(pid, "new", pid))
        self.assertEqual(reused.probe(handle).reason, "host_pid_reused")

    def test_controller_start_launches_private_host_without_shell(self):
        launcher = FakeProcess(pid=51234)
        captured = {}

        def launch(command, **options):
            captured["command"] = command
            captured["options"] = options
            return launcher

        host_identity = ProcessIdentity(51234, "launcher-start", 51234)
        claude_identity = ProcessIdentity(42420, "claude-start", 42420)
        handle = RunnerHandle(
            run_id="run-17", session_id=self.session_id,
            socket_path=str(self.runtime / "claude.sock"),
            event_log_path=str(self.runtime / "events.jsonl"),
            state_path=str(self.runtime / "host-state.json"),
            host_identity=host_identity, claude_identity=claude_identity,
        )
        snapshot = {
            "handle": handle.to_dict(), "state": "running", "turn_state": "running",
            "started_at": 1.0, "last_event_at": 1.0, "last_cursor": 2,
            "last_message": "", "returncode": None,
        }

        def request(_socket, message, _timeout):
            self.assertEqual(message["type"], "status")
            return {"ok": True, "snapshot": snapshot}

        runner = ClaudeRunner(
            host_launcher=launch, identity_reader=self.identity_reader, requester=request,
            ready_timeout=0.1,
        )
        result = runner.start(self.spec())
        self.assertEqual(result.handle, handle)
        self.assertFalse(captured["options"]["shell"])
        self.assertTrue(captured["options"]["start_new_session"])
        self.assertEqual(captured["command"][1], str(Path(__file__).parent / "patch_watcher" / "claude_runner.py"))
        spec_path = self.runtime / "launch-spec.json"
        self.assertEqual(spec_path.stat().st_mode & 0o777, 0o600)

    def test_host_handle_round_trip_preserves_process_identities(self):
        host, process, _calls = self.make_host()
        handle = host.start()
        self.addCleanup(process.stdout.close)
        self.assertEqual(RunnerHandle.from_dict(handle.to_dict()), handle)


if __name__ == "__main__":
    unittest.main()


class FullCapabilityProfileTests(unittest.TestCase):
    """The ``full`` profile is the working agent's environment.

    Its boundary is the rendered prompt and its checkout, not a capability
    grant, so these tests pin the two things that make that true: an
    unrestricted launch, and an inherited environment.  The bounded profiles
    are asserted alongside so a future change cannot quietly relax them.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.cwd = root / "checkout"
        self.cwd.mkdir()
        self.runtime = root / "runtime"
        self.runtime.mkdir()

    def spec(self, **overrides):
        values = {
            "run_id": "run-91",
            "session_id": str(uuid.uuid4()),
            "cwd": str(self.cwd),
            "runtime_dir": str(self.runtime),
            "prompt": "Fix the patch, build it in an LTVM guest, and report.",
            "report_kind": "engineering",
            "capability_profile": "full",
        }
        values.update(overrides)
        return ReadOnlyRunSpec(**values)

    def test_full_profile_launches_without_tool_allowlist_or_hardening(self):
        command = build_read_only_claude_command(self.spec())
        self.assertNotIn("--tools", command)
        self.assertNotIn("--mcp-config", command)
        for flag in (
            "--safe-mode", "--restricted", "--strict-mcp-config",
            "--disable-slash-commands",
        ):
            self.assertNotIn(flag, command)
        self.assertEqual(
            command[command.index("--permission-mode") + 1], "bypassPermissions"
        )
        schema_index = command.index("--json-schema") + 1
        self.assertEqual(
            json.loads(command[schema_index]), ENGINEERING_REPORT_SCHEMA
        )

    def test_bounded_profiles_keep_their_allowlist_and_hardening(self):
        for profile, expected, report in (
            ("read_only", "Read,Glob,Grep", "read_only"),
        ):
            with self.subTest(profile=profile):
                command = build_read_only_claude_command(
                    self.spec(capability_profile=profile, report_kind=report)
                )
                self.assertEqual(command[command.index("--tools") + 1], expected)
                self.assertNotIn("Bash", command[command.index("--tools") + 1])
                for flag in ("--safe-mode", "--restricted", "--strict-mcp-config"):
                    self.assertIn(flag, command)
                self.assertEqual(
                    command[command.index("--permission-mode") + 1], "dontAsk"
                )

    def test_full_profile_inherits_real_service_credentials(self):
        source = {
            "PATH": "/usr/bin",
            "HOME": "/home/operator",
            "GERRIT_PASS": "http-password",
            "MALOO_TOKEN": "maloo-token",
            "ANTHROPIC_API_KEY": "model-key",
        }
        environment = _safe_environment(source, capability_profile="full")
        self.assertEqual(environment["GERRIT_PASS"], "http-password")
        self.assertEqual(environment["MALOO_TOKEN"], "maloo-token")
        self.assertEqual(environment["HOME"], "/home/operator")
        self.assertNotIn("CLAUDE_CODE_SAFE_MODE", environment)
        self.assertEqual(environment["PATCH_WATCHER_CAPABILITY_PROFILE"], "full")

    def test_bounded_profiles_still_strip_service_credentials(self):
        source = {
            "PATH": "/usr/bin",
            "HOME": "/home/operator",
            "GERRIT_PASS": "http-password",
            "MALOO_TOKEN": "maloo-token",
            "ANTHROPIC_API_KEY": "model-key",
        }
        for profile in ("read_only",):
            with self.subTest(profile=profile):
                environment = _safe_environment(source, capability_profile=profile)
                self.assertNotIn("GERRIT_PASS", environment)
                self.assertNotIn("MALOO_TOKEN", environment)
                self.assertEqual(environment["ANTHROPIC_API_KEY"], "model-key")
                self.assertEqual(environment["HOME"], "/home/operator")
                self.assertEqual(environment["CLAUDE_CODE_SAFE_MODE"], "1")

    def test_home_is_never_overridden_so_tool_configs_resolve(self):
        # The LLM tools read ~/.config/<tool>/.env, so a rewritten HOME would
        # silently strand every credential the full profile depends on.
        for profile in ("read_only", "full"):
            with self.subTest(profile=profile):
                environment = _safe_environment(
                    {"HOME": "/home/operator", "PATH": "/usr/bin"},
                    capability_profile=profile,
                )
                self.assertEqual(environment["HOME"], "/home/operator")

    def test_unsupported_profile_is_still_rejected(self):
        with self.assertRaises(ValueError):
            _safe_environment({"PATH": "/usr/bin"}, capability_profile="bogus")
        with self.assertRaises(ValueError):
            self.spec(capability_profile="bogus").validate()

    def test_full_profile_requires_an_engineering_report(self):
        with self.assertRaises(ValueError):
            self.spec(report_kind="read_only").validate()


class ReusedPidProbeTests(unittest.TestCase):
    """A recycled host pid must read as "our worker is gone", not "alive".

    Reporting alive=True wedged terminal cleanup: `_cleanup_session` and
    `_reconcile_ltvm_resources` both gate on `alive`, so a run whose pid had
    been recycled never released its checkout or destroyed its guests. The
    identity guards in terminate/kill independently prevent signalling the
    process that now holds the pid, so calling it dead is safe.
    """

    def handle(self):
        return RunnerHandle(
            run_id="run-1",
            session_id="11111111-1111-4111-8111-111111111111",
            socket_path="/tmp/does-not-matter/claude.sock",
            event_log_path="/tmp/does-not-matter/events.jsonl",
            state_path="/tmp/does-not-matter/host-state.json",
            host_identity=ProcessIdentity(4242, "start-token-a", 4242),
        )

    def test_a_reused_pid_is_not_alive_and_says_why(self):
        runner = ClaudeRunner(
            identity_reader=lambda pid: ProcessIdentity(pid, "start-token-B", pid)
        )
        probe = runner.probe(self.handle())
        self.assertFalse(probe.alive, "a recycled pid wedged cleanup when reported alive")
        self.assertFalse(probe.adoptable)
        self.assertEqual(probe.reason, "host_pid_reused")

    def test_a_missing_process_is_still_not_alive(self):
        def missing(pid):
            raise ProcessLookupError(pid)

        probe = ClaudeRunner(identity_reader=missing).probe(self.handle())
        self.assertFalse(probe.alive)
        self.assertEqual(probe.reason, "host_process_missing")


class EventCursorPaginationTests(unittest.TestCase):
    """`event_tail` is a cursor read and must move FORWARD from the cursor.

    It collected into a `deque(maxlen=limit)`, which keeps the LAST `limit`
    matches. The consumer advances its cursor with `max(after, event.cursor)`
    and stops when a page comes back short, so everything between the cursor
    and the newest page was silently discarded -- measured at 400 of 500
    pending events, never stored and never turned into messages.

    Nothing caught it because `test_run_controller.FakeRunner.events` slices
    `[:limit]` from the front: the fake modelled the INTENDED behaviour, so it
    was more correct than the code it stood in for.
    """

    def host_with(self, count):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        log = directory / "events.jsonl"
        with log.open("w", encoding="utf-8") as handle:
            for cursor in range(1, count + 1):
                handle.write(json.dumps({
                    "cursor": cursor, "type": "claude_event",
                    "timestamp": 0.0, "payload": {"n": cursor},
                }) + "\n")
        host = ClaudeHost.__new__(ClaudeHost)
        host.event_log_path = log
        return host

    def test_the_first_page_is_the_oldest_events_not_the_newest(self):
        host = self.host_with(500)
        page = host.event_tail(after_cursor=0, limit=100)
        self.assertEqual([page[0].cursor, page[-1].cursor], [1, 100])

    def test_paging_with_a_cursor_loses_nothing(self):
        host = self.host_with(500)
        seen, after = [], 0
        while True:
            batch = host.event_tail(after_cursor=after, limit=100)
            if not batch:
                break
            seen.extend(event.cursor for event in batch)
            after = max(event.cursor for event in batch)
        self.assertEqual(seen, list(range(1, 501)))

    def test_a_short_page_means_there_is_nothing_left(self):
        # The consumer stops on a short page, so a short page must not be
        # returned while events remain.
        host = self.host_with(150)
        first = host.event_tail(after_cursor=0, limit=100)
        self.assertEqual(len(first), 100)
        second = host.event_tail(after_cursor=100, limit=100)
        self.assertEqual(len(second), 50)
        self.assertEqual(host.event_tail(after_cursor=150, limit=100), [])

    def test_a_corrupt_line_is_skipped_without_losing_the_rest(self):
        host = self.host_with(10)
        with host.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
            handle.write(json.dumps({
                "cursor": 11, "type": "claude_event",
                "timestamp": 0.0, "payload": {},
            }) + "\n")
        self.assertEqual(
            [event.cursor for event in host.event_tail(after_cursor=0, limit=100)],
            list(range(1, 12)),
        )


class AbandonedHostTests(unittest.TestCase):
    """A `start` that fails must not leave the host it launched running.

    The detached host owns a `claude` child launched with
    ``--permission-mode bypassPermissions`` inside a real checkout, so an
    orphan here is not a tidiness problem.  These tests drive real processes
    and assert on the process table, because the whole defect was that the
    cleanup code existed and simply never ran -- a mock's call list would have
    agreed with the broken version.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cwd = self.base / "checkout"
        self.cwd.mkdir()
        self.session_id = str(uuid.uuid4())

    def spec(self, name="run-17"):
        return ReadOnlyRunSpec(
            run_id=name,
            session_id=self.session_id,
            cwd=str(self.cwd),
            runtime_dir=str(self.base / name),
            prompt="Read the pinned evidence and report findings.",
        )

    def launch(self, source):
        """Start a real, session-detached child that stands in for the host."""
        process = subprocess.Popen(
            [sys.executable, "-c", source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            shell=False,
        )
        self.addCleanup(self._reap, process)
        return process

    @staticmethod
    def _reap(process):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                process.wait(timeout=5)

    def test_every_failing_start_path_takes_the_host_down(self):
        handle = RunnerHandle(
            run_id="a-different-run", session_id=str(uuid.uuid4()),
            socket_path="/nonexistent.sock", event_log_path="/nonexistent.jsonl",
            state_path="/nonexistent.json",
            host_identity=ProcessIdentity(1, "other", 1),
        )
        foreign = {
            "handle": handle.to_dict(), "state": "running", "turn_state": "running",
            "started_at": 1.0, "last_event_at": 1.0, "last_cursor": 0,
            "last_message": "", "returncode": None,
        }
        cases = (
            # RunnerIdentityError is a sibling of RunnerProtocolError, so the
            # retry tuple never caught it and the raise escaped the loop.
            ("identity_mismatch", {"ok": True, "snapshot": foreign}, RunnerIdentityError),
            # A response with no "snapshot" raises KeyError from status().
            ("missing_snapshot", {"ok": True}, KeyError),
            # A malformed snapshot raises out of RunnerSnapshot.from_dict.
            ("malformed_snapshot", {"ok": True, "snapshot": {"state": "running"}}, KeyError),
        )
        for name, response, expected in cases:
            with self.subTest(case=name):
                process = self.launch("import time\ntime.sleep(300)\n")
                runner = ClaudeRunner(
                    host_launcher=lambda *_a, _p=process, **_k: _p,
                    requester=lambda *_a, _r=response, **_k: _r,
                    ready_timeout=2.0,
                    stop_timeout=5.0,
                )
                with self.assertRaises(expected):
                    runner.start(self.spec(name.replace("_", "-")))
                self.assertEqual(process.poll(), -signal.SIGTERM)

    def test_a_host_that_ignores_sigterm_is_escalated_to_sigkill(self):
        # The ready-timeout path used to send one SIGTERM and assume it worked.
        process = self.launch(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(300)\n"
        )

        def refuse(*_args, **_kwargs):
            raise ConnectionRefusedError("no control socket yet")

        runner = ClaudeRunner(
            host_launcher=lambda *_args, **_kwargs: process,
            requester=refuse,
            ready_timeout=0.2,
            stop_timeout=1.0,
        )
        with self.assertRaisesRegex(ClaudeRunnerError, "did not become ready"):
            runner.start(self.spec("sigterm-ignored"))
        self.assertEqual(process.poll(), -signal.SIGKILL)

    def test_a_recycled_host_pid_is_never_signalled(self):
        # The abandon path must keep the identity guarantee the rest of the
        # controller relies on: a PID whose start token no longer matches
        # belongs to somebody else and must not be signalled.
        process = self.launch("import time\ntime.sleep(300)\n")
        tokens = iter(("proc:first", "proc:second", "proc:second"))

        def drifting_identity(pid):
            return ProcessIdentity(pid, next(tokens, "proc:second"), pid)

        runner = ClaudeRunner(
            host_launcher=lambda *_args, **_kwargs: process,
            identity_reader=drifting_identity,
            requester=lambda *_args, **_kwargs: {"ok": True},
            ready_timeout=1.0,
            stop_timeout=1.0,
        )
        with self.assertRaises(KeyError):
            runner.start(self.spec("recycled-pid"))
        self.assertIsNone(process.poll(), "a reused PID must not be signalled")


class AtomicJsonDescriptorTests(unittest.TestCase):
    """`_atomic_private_json` must not leak the descriptor it opened.

    ``os.fdopen`` only adopts the descriptor when it returns; the original
    ``finally`` unlinked the temporary file and left the raw descriptor open.
    The host calls this on every state persist, which is once per stream event.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "host-state.json"

    @staticmethod
    def _open_count(path):
        """How many of this process's descriptors currently name ``path``.

        Scoped to the one file rather than counting the whole table: the rest
        of the suite opens and closes files while this runs, and a total is
        noise around the number we care about.
        """
        total = 0
        for entry in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(entry)
            except OSError:
                continue  # the descriptor closed while we were walking
            if target in (str(path), f"{path} (deleted)"):
                total += 1
        return total

    def test_a_failing_fdopen_closes_the_raw_descriptor(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("descriptor accounting needs /proc")

        class FailingFdopen:
            """Real ``os`` for everything except the call under test."""

            def __getattr__(self, name):
                return getattr(os, name)

            def fdopen(self, *_args, **_kwargs):
                raise OSError(errno.ENOMEM, "cannot allocate memory")

        attempts = 200
        temporary = str(self.path) + ".tmp"
        self.assertEqual(self._open_count(temporary), 0)
        with unittest.mock.patch.object(claude_runner, "os", FailingFdopen()):
            for _ in range(attempts):
                with self.assertRaises(OSError):
                    claude_runner._atomic_private_json(self.path, {"state": "running"})
        leaked = self._open_count(temporary)
        self.assertEqual(
            leaked, 0, f"leaked {leaked / attempts:.2f} descriptors per failed write"
        )
