import json
import io
import os
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from ltvm_mcp_server import (
    CONTEXT_SCHEMA,
    LTVMMCPContext,
    MCPServiceError,
    SessionLTVMService,
    StateRunAuthorizer,
    StdioMCPServer,
    tool_list,
)
from ltvm_resources import LTVMInventory, owner_id_for_session
from ltvm_guest_exec import GuestTransportResult


SESSION = str(uuid.UUID("c966e533-2359-4b50-91d7-996135885f80"))
RUN = "pw-engineer-42-ps1-test"
REVISION = "a" * 40
OWNER = owner_id_for_session(SESSION)


class FakeStore:
    def __init__(self, *, active=True):
        self.active = active
        self.records = []
        self.claims = []
        self.claim_disposition = "dispatch"
        self.execution = SimpleNamespace(
            execution_id="validation-1", run_id=RUN, session_id=SESSION,
            revision_sha=REVISION, owner_id=OWNER, admission_state="approved",
        )
        self.attempt = SimpleNamespace(
            attempt_id="attempt-1", execution_id="validation-1",
            worker_id="worker-1", state="running",
        )

    def get_active_validation_capability(self, **identity):
        expected = {
            "session_id": SESSION, "run_id": RUN, "revision_sha": REVISION,
        }
        return self.execution if self.active and identity == expected else None

    def get_validation_attempt(self, attempt_id):
        if attempt_id != "attempt-1":
            raise KeyError(attempt_id)
        return self.attempt

    def record_validation_step_result(self, attempt_id, **kwargs):
        self.records.append((attempt_id, kwargs))

    def claim_validation_command(self, attempt_id, **kwargs):
        self.claims.append((attempt_id, kwargs))
        return SimpleNamespace(
            should_dispatch=self.claim_disposition == "dispatch",
            disposition=self.claim_disposition,
        )


class FakeAdapter:
    def __init__(self, inventory):
        self.value = inventory
        self.cleanup_calls = []

    def inventory(self):
        return self.value

    def cleanup(self, action):
        self.cleanup_calls.append(action)


class FakeBrokerResult:
    def __init__(self, payload):
        self.payload = payload
        self.status = payload["status"]

    def to_dict(self):
        return self.payload


class FakeBroker:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {
            "status": "succeeded", "detail": "done",
            "commands": [{
                "status": "succeeded", "code": "expected_exit", "exit_code": 0,
                "started_at": "2026-09-01T10:00:00+00:00",
                "finished_at": "2026-09-01T10:00:01+00:00",
            }],
        }

    def execute(self, request, **kwargs):
        self.calls.append(request)
        return FakeBrokerResult(self.payload)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.results = []
        self.archive_members = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv and str(argv[0]).endswith("scp"):
            with tarfile.open(argv[-2], "r:") as archive:
                self.archive_members = archive.getnames()
        if self.results:
            return self.results.pop(0)
        return GuestTransportResult(0, b'{"ok":true}\n')


def inventory(*rows):
    return LTVMInventory.from_json({"vms": list(rows)})


class MCPFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        checkout_root = (root / "checkouts").resolve()
        checkout = checkout_root / RUN
        checkout.mkdir(parents=True)
        self.context = LTVMMCPContext(
            session_id=SESSION, run_id=RUN, revision_sha=REVISION,
            owner_id=OWNER, checkout_path=checkout,
            checkout_root=checkout_root, engineering_database=root / "state.sqlite3",
            execution_id="validation-1", attempt_id="attempt-1",
            worker_id="worker-1", audit_path=root / "audit.jsonl",
            name_prefix="pw-c966e533",
        )
        self.store = FakeStore()
        self.adapter = FakeAdapter(inventory({
            "name": "pw-c966e533-node", "owner_id": OWNER,
            "status": "running", "mem": 2048, "vcpus": 2,
            "ip": "192.0.2.10",
        }))
        self.broker = FakeBroker()
        self.runner = FakeRunner()
        self.service = SessionLTVMService(
            self.context, store=self.store, adapter=self.adapter,
            broker=self.broker, runner=self.runner,
        )

    def tearDown(self):
        self.temp.cleanup()


class ContextAndAuthorizationTests(MCPFixture):
    def test_private_context_loads_and_binds_exact_checkout_owner(self):
        path = Path(self.temp.name) / "context.json"
        value = {
            "schema": CONTEXT_SCHEMA, "session_id": SESSION, "run_id": RUN,
            "revision_sha": REVISION, "owner_id": OWNER,
            "checkout_path": str(self.context.checkout_path),
            "checkout_root": str(self.context.checkout_root),
            "engineering_database": str(self.context.engineering_database),
            "execution_id": "validation-1", "attempt_id": "attempt-1",
            "worker_id": "worker-1", "audit_path": str(self.context.audit_path),
            "name_prefix": "pw-c966e533",
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        loaded = LTVMMCPContext.load(path)
        self.assertEqual(loaded.owner_id, OWNER)
        self.assertEqual(loaded.checkout_path, self.context.checkout_path)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "group/world"):
            LTVMMCPContext.load(path)

    def test_authorizer_fails_closed_when_attempt_or_grant_is_not_active(self):
        authorizer = StateRunAuthorizer(self.store, self.context)
        self.assertIsNotNone(authorizer.authorization_for(
            session_id=SESSION, run_id=RUN, revision_sha=REVISION,
        ))
        self.store.attempt.state = "failed"
        self.assertIsNone(authorizer.authorization_for(
            session_id=SESSION, run_id=RUN, revision_sha=REVISION,
        ))


class LifecycleToolTests(MCPFixture):
    def test_create_uses_controller_prefix_exact_owner_and_no_shell(self):
        result = self.service.create({
            "name": "node", "target": "rocky9", "arch": "aarch64",
            "memory_mib": 4096, "vcpus": 4,
        })
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(result["name"], "pw-c966e533-node")
        self.assertIn("--owner", argv)
        self.assertEqual(argv[argv.index("--owner") + 1], OWNER)
        self.assertIn("aarch64", argv)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["environment"]["LTVM_OWNER_ID"], OWNER)
        self.assertEqual(len(self.store.claims), 1)
        self.assertEqual(len(self.store.records), 1)

    def test_cluster_create_places_arch_as_a_complete_flag_pair(self):
        self.adapter.value = inventory(
            {"name": "pw-c966e533-mds", "owner_id": OWNER, "status": "running"},
            {"name": "pw-c966e533-client", "owner_id": OWNER, "status": "running"},
        )
        result = self.service.cluster_create({
            "name": "cluster", "target": "rocky9", "arch": "aarch64",
            "nodes": [
                {"name": "mds", "roles": ["mgs", "mds"], "disks": 1},
                {"name": "client", "roles": ["client"], "disks": 0},
            ],
        })
        argv = self.runner.calls[0][0]
        self.assertEqual(result["members"], ["pw-c966e533-mds", "pw-c966e533-client"])
        self.assertEqual(argv[:6], [
            "/usr/bin/sudo", "-n", "/usr/local/bin/ltvm", "cluster", "--json", "create",
        ])
        arch = argv.index("--arch")
        self.assertEqual(argv[arch:arch + 2], ["--arch", "aarch64"])
        self.assertIn("mgs+mds:pw-c966e533-mds:1", argv)

    def test_unknown_arguments_and_other_owner_destroy_fail_closed(self):
        with self.assertRaisesRegex(MCPServiceError, "unknown tool arguments"):
            self.service.call("target_fetch", {"target": "rocky9", "url": "https://evil"})
        with self.assertRaisesRegex(MCPServiceError, "not uniquely owned"):
            self.service.destroy({"vm_name": "somebody-elses-vm"})
        self.assertEqual(self.adapter.cleanup_calls, [])


class OpenEndedGuestToolTests(MCPFixture):
    def test_exec_accepts_arbitrary_guest_shell_and_persists_audit(self):
        value = self.service.exec({
            "vm_name": "pw-c966e533-node", "command_id": "diagnose",
            "text": "make -j8 && for t in tests/*; do ./run \"$t\"; done",
            "cwd": "/root/patch-watcher/source", "timeout_seconds": 7200,
        })
        self.assertEqual(value["status"], "succeeded")
        command = self.broker.calls[0].commands[0]
        self.assertIn("for t in tests", command.text)
        self.assertIsNone(command.argv)
        self.assertEqual(self.store.records[0][0], "attempt-1")
        self.assertEqual(self.store.records[0][1]["worker_id"], "worker-1")
        self.assertEqual(self.store.claims[0][1]["command"].command_id, "diagnose")
        lines = self.context.audit_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["kind"], "guest_execution")

    def test_exec_accepts_arbitrary_argv_without_host_bash(self):
        self.service.exec({
            "vm_name": "pw-c966e533-node", "command_id": "argv-test",
            "argv": ["/bin/bash", "-lc", "custom-tool --anything"],
        })
        self.assertEqual(
            self.broker.calls[0].commands[0].argv,
            ("/bin/bash", "-lc", "custom-tool --anything"),
        )
        self.assertEqual(self.runner.calls, [])

    def test_completed_claim_never_replays_guest_command(self):
        self.store.claim_disposition = "completed"
        with self.assertRaisesRegex(MCPServiceError, "not dispatched"):
            self.service.exec({
                "vm_name": "pw-c966e533-node", "command_id": "prior",
                "argv": ["/bin/true"],
            })
        self.assertEqual(self.broker.calls, [])


class SourceTransferTests(MCPFixture):
    def test_push_source_archives_only_enumerated_regular_files(self):
        (self.context.checkout_path / "tracked.txt").write_text("tracked", encoding="utf-8")
        (self.context.checkout_path / "dir").mkdir()
        (self.context.checkout_path / "dir" / "new.txt").write_text("new", encoding="utf-8")
        (self.context.checkout_path / ".git").mkdir()
        (self.context.checkout_path / ".git" / "secret").write_text("secret", encoding="utf-8")
        self.runner.results = [
            GuestTransportResult(0, b"tracked.txt\0dir/new.txt\0"),
            GuestTransportResult(0),
        ]

        result = self.service.push_source({"vm_name": "pw-c966e533-node"})

        self.assertEqual(result["file_count"], 2)
        self.assertEqual(
            self.runner.archive_members,
            ["tracked.txt", "dir", "dir/new.txt"],
        )
        scp_argv = self.runner.calls[1][0]
        self.assertIn("-F", scp_argv)
        self.assertIn("ProxyCommand=none", scp_argv)
        self.assertEqual(scp_argv[-1].split(":", 1)[0], "root@192.0.2.10")
        self.assertEqual(len(self.store.claims), 4)  # enumerate, prepare, copy, extract
        self.assertEqual(len(self.store.records), 4)

    def test_push_source_refuses_tracked_symlink_without_copying(self):
        outside = Path(self.temp.name) / "outside-secret"
        outside.write_text("do not copy", encoding="utf-8")
        os.symlink(outside, self.context.checkout_path / "link")
        self.runner.results = [GuestTransportResult(0, b"link\0")]

        with self.assertRaisesRegex(MCPServiceError, "symlinks"):
            self.service.push_source({"vm_name": "pw-c966e533-node"})

        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.broker.calls, [])

    def test_push_source_refuses_dot_git_even_if_enumerator_returns_it(self):
        self.runner.results = [GuestTransportResult(0, b".git/config\0")]
        with self.assertRaisesRegex(MCPServiceError, "escapes"):
            self.service.push_source({"vm_name": "pw-c966e533-node"})
        self.assertEqual(self.broker.calls, [])

    def test_push_source_safely_skips_deleted_tracked_path(self):
        (self.context.checkout_path / "present").write_text("ok", encoding="utf-8")
        self.runner.results = [
            GuestTransportResult(0, b"deleted\0present\0"),
            GuestTransportResult(0),
        ]
        result = self.service.push_source({"vm_name": "pw-c966e533-node"})
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(self.runner.archive_members, ["present"])


class DurableHostDispatchTests(MCPFixture):
    def test_capacity_failure_is_claimed_and_durably_recorded(self):
        self.runner.results = [GuestTransportResult(1, b"", b"insufficient memory")]
        with self.assertRaisesRegex(MCPServiceError, "resource exhausted"):
            self.service.create({"name": "node", "target": "rocky9"})
        self.assertEqual(len(self.store.claims), 1)
        self.assertEqual(self.store.records[0][1]["state"], "resource_exhausted")
        self.assertEqual(self.adapter.cleanup_calls, [])

    def test_destroy_reauthorizes_after_claim_and_records_result(self):
        original_claim = self.store.claim_validation_command

        def deactivate_after_claim(attempt_id, **kwargs):
            claim = original_claim(attempt_id, **kwargs)
            self.store.active = False
            return claim

        self.store.claim_validation_command = deactivate_after_claim
        with self.assertRaisesRegex(MCPServiceError, "not active"):
            self.service.destroy({"vm_name": "pw-c966e533-node"})
        self.assertEqual(self.adapter.cleanup_calls, [])
        self.assertEqual(self.store.records[0][1]["state"], "failed")


class ProtocolTests(MCPFixture):
    def test_tools_are_explicit_and_protocol_errors_are_bounded(self):
        names = {item["name"] for item in tool_list()}
        self.assertEqual(names, {
            "list", "target_list", "target_fetch", "create", "cluster_create",
            "push_source", "exec", "cluster_exec", "destroy",
        })
        server = StdioMCPServer(self.service)
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "exec", "arguments": {
                "vm_name": "pw-c966e533-node", "text": "uname -a",
            }},
        })
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["result"]["structuredContent"]["status"], "succeeded")
        missing = server.dispatch({"jsonrpc": "2.0", "id": 8, "method": "nope"})
        self.assertEqual(missing["error"]["code"], -32601)

    def test_stdio_rejects_oversized_request_line(self):
        event = __import__("threading").Event()
        server = StdioMCPServer(self.service, cancellation_event=event)
        output = io.StringIO()
        server.serve(
            input_stream=io.BytesIO(b"{" + b"x" * 1_048_576 + b"}\n"),
            output_stream=output,
        )
        response = json.loads(output.getvalue())
        self.assertEqual(response["error"]["code"], -32700)
        self.assertTrue(event.is_set())


if __name__ == "__main__":
    unittest.main()
