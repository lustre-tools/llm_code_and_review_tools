import json
import os
import queue
import signal
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from claude_runner import (
    MAX_EVENT_TAIL,
    PROTOCOL_VERSION,
    ClaudeHost,
    ClaudeRunner,
    ProcessIdentity,
    ReadOnlyRunSpec,
    RunnerHandle,
    RunnerIdentityError,
    RunnerProtocolError,
    RunnerStateError,
    READ_ONLY_REPORT_SCHEMA,
    _safe_environment,
    build_read_only_claude_command,
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
            try:
                host.request_stop(True)
            except (RunnerStateError, RunnerIdentityError):
                pass
            process.returncode = process.returncode if process.returncode is not None else -9
            process.stdout.close()
            thread.join(timeout=2)

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
        self.addCleanup(lambda: process.stdout.close())
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

    def test_delivery_id_is_durable_in_private_log(self):
        host, process, _calls, _thread = self.start_server()
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
        self.addCleanup(lambda: host.process.stdout.close())

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
        self.addCleanup(lambda: process.stdout.close())
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
        self.assertEqual(captured["command"][1], str(Path(__file__).with_name("claude_runner.py")))
        spec_path = self.runtime / "launch-spec.json"
        self.assertEqual(spec_path.stat().st_mode & 0o777, 0o600)

    def test_host_handle_round_trip_preserves_process_identities(self):
        host, process, _calls = self.make_host()
        handle = host.start()
        self.addCleanup(lambda: process.stdout.close())
        self.assertEqual(RunnerHandle.from_dict(handle.to_dict()), handle)


if __name__ == "__main__":
    unittest.main()
