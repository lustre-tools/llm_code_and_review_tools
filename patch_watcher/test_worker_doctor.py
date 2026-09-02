import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from worker_contract import (  # noqa: E402
    EnvironmentAttestation,
    WorkerProfile,
    build_run_envelope,
    content_hash,
    create_run_directories,
    hash_text,
    write_run_snapshot,
)
import pw_worker  # noqa: E402
from worker_doctor import (  # noqa: E402
    CommandResult,
    DoctorProbes,
    canonical_content_hash,
    doctor,
    version_satisfies,
)


REVISION = "a" * 40


def hashed(payload):
    value = dict(payload)
    value["content_hash"] = "sha256:" + "0" * 64
    value["content_hash"] = content_hash(value)
    return value


def profile_payload(**updates):
    payload = {
        "schema_version": "1.0",
        "profile_id": "test-worker-v1",
        "profile_version": 1,
        "description": "test profile",
        "supported_hosts": {
            "operating_systems": ["Darwin"],
            "architectures": ["arm64"],
        },
        "runtimes": {"python": ">=3.11,<4", "worker_protocol": "==1.0"},
        "tools": [
            {
                "name": "demo",
                "command": "demo",
                "required": True,
                "version_constraint": ">=1.2,<2",
                "api_version": "1",
            }
        ],
        "capabilities": ["read_source", "report_status", "start_ltvm"],
        "isolation_profiles": ["host_unsandboxed", "container_standard"],
        "network_profiles": ["host_ambient"],
        "logical_paths": {
            "/home/worker": "private_read_write",
            "/run/patch-watcher": "private_read_write",
            "/work/input": "private_read_only",
            "/work/output": "private_read_write",
            "/work/scratch": "private_read_write",
            "/work/source": "read_only",
        },
        "host_services": [
            "artifact_collector",
            "claude_runner",
            "resource_sampler",
            "worker_report_channel",
        ],
        "default_limits": {
            "cpu_count": 2,
            "memory_bytes": 1024,
            "disk_bytes": 2048,
            "process_count": 32,
            "runtime_seconds": 120,
            "inactivity_seconds": 30,
            "output_bytes": 4096,
            "action_count": 5,
        },
        "ambient_home_allowed": True,
        "security_label": "trusted_unsandboxed",
    }
    payload.update(updates)
    return hashed(payload)


class DoctorFixture:
    def __init__(self, test_case, *, capabilities=("read_source", "report_status")):
        self.test_case = test_case
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = WorkerProfile.from_dict(profile_payload())
        self.layout = create_run_directories(self.root / "runs", "run-1")
        self.envelope = build_run_envelope(
            run_id="run-1",
            change_id="68160",
            patchset=2,
            revision_sha=REVISION,
            profile=self.profile,
            task="read-only investigation",
            capabilities=list(capabilities),
            instructions_hash=hash_text("instructions"),
            created_at="2026-08-30T12:00:00+00:00",
            endpoints={
                "controller": "local://controller",
                "broker": "/run/patch-watcher/broker.sock",
                "heartbeat": "local://heartbeat",
                "report": "/work/output/worker-report.json",
                "artifact": "/work/output/artifacts",
            },
            ltvm_owner_id="run-1" if "start_ltvm" in capabilities else None,
        )
        paths = write_run_snapshot(self.layout, self.envelope, "instructions")
        self.envelope_path = paths["run_envelope"]
        # Admission verifies native-host read-only mappings, not just their
        # declaration in the envelope.
        os.chmod(self.layout.resolve("/work/source"), 0o500)
        os.chmod(self.layout.resolve("/work/input"), 0o500)
        self.command_results = {}
        self.which_results = {"demo": "/opt/test/bin/demo", "ltvm": "/opt/test/bin/ltvm"}
        self.environment = {"LTVM_OWNER_ID": "run-1"} if "start_ltvm" in capabilities else {}
        self.probes = DoctorProbes(
            system=lambda: "Darwin",
            machine=lambda: "arm64",
            hostname=lambda: "test-host",
            python_version=lambda: "3.12.13",
            which=lambda command: self.which_results.get(command),
            run=self.run,
            disk_free=lambda path: 10_000,
            memory_available=lambda: 20_000,
            endpoint=lambda endpoint: True,
            credential=lambda reference, environment: "available",
            environ=self.environment,
        )

    def run(self, command, timeout=10):
        command = list(command)
        if command[:2] == ["/opt/test/bin/demo", "--version"]:
            return CommandResult(0, "demo 1.2.3\n")
        if command[:2] == ["/opt/test/bin/ltvm", "--version"]:
            return CommandResult(0, "ltvm 0.20.0\n")
        if command[:3] == ["/opt/test/bin/ltvm", "list", "--json"]:
            return CommandResult(0, '[{"name":"one","owner_id":"run-1"}]')
        if command[0:2] == ["git", "-C"] and command[3:] == ["rev-parse", "HEAD"]:
            return CommandResult(0, REVISION + "\n")
        if command[0:2] == ["git", "-C"] and command[3:] == ["status", "--porcelain", "--untracked-files=all"]:
            return CommandResult(0, "")
        return self.command_results.get(tuple(command), CommandResult(127, "", "unavailable"))

    def attest(self, profile=None, envelope=None):
        return doctor(
            profile or self.profile,
            envelope or self.envelope,
            probes=self.probes,
            envelope_path=self.envelope_path,
        )

    def close(self):
        # Restore permissions so TemporaryDirectory can remove its contents.
        os.chmod(self.layout.resolve("/work/source"), 0o700)
        os.chmod(self.layout.resolve("/work/input"), 0o700)
        self.temporary.cleanup()


class WorkerDoctorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DoctorFixture(self)

    def tearDown(self):
        self.fixture.close()

    def test_ready_attestation_is_canonical_redacted_and_hash_valid(self):
        result = self.fixture.attest()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["admitted"])
        self.assertEqual(result["failure_codes"], [])
        self.assertEqual(result["worker_profile_hash"], self.fixture.profile.content_hash)
        self.assertEqual(result["run_envelope_hash"], self.fixture.envelope.content_hash)
        self.assertEqual(result["executables"][0]["name"], "demo")
        self.assertTrue(result["checkout"]["clean"])
        self.assertNotIn("test-host", json.dumps(result))
        self.assertEqual(canonical_content_hash(result), result["content_hash"])
        parsed = EnvironmentAttestation.from_dict(result)
        self.assertEqual(parsed.status, "ready")

    def test_required_missing_and_optional_missing_have_distinct_outcomes(self):
        required = self.fixture.attest()
        self.fixture.which_results.pop("demo")
        required = self.fixture.attest()
        self.assertEqual(required["status"], "blocked")
        self.assertIn("tool_missing", required["failure_codes"])

        optional_tools = list(self.fixture.profile.to_dict()["tools"])
        optional_tools[0] = dict(optional_tools[0], required=False)
        optional_profile = WorkerProfile.from_dict(profile_payload(tools=optional_tools))
        envelope_data = self.fixture.envelope.to_dict()
        envelope_data["worker_profile_hash"] = optional_profile.content_hash
        envelope_data = hashed(envelope_data)
        degraded = self.fixture.attest(optional_profile, envelope_data)
        self.assertEqual(degraded["status"], "degraded")
        self.assertTrue(degraded["admitted"])
        self.assertEqual(degraded["unavailable_optional_capabilities"], ["demo"])

    def test_unusable_and_wrong_version_are_blocked(self):
        original = self.fixture.run

        def unusable(command, timeout=10):
            if list(command)[:2] == ["/opt/test/bin/demo", "--version"]:
                return CommandResult(1, "", "failure with TOKEN=never-show")
            return original(command, timeout)

        self.fixture.probes.run = unusable
        result = self.fixture.attest()
        self.assertIn("tool_unusable", result["failure_codes"])
        self.assertNotIn("never-show", json.dumps(result))

        def old_version(command, timeout=10):
            if list(command)[:2] == ["/opt/test/bin/demo", "--version"]:
                return CommandResult(0, "demo 0.9")
            return original(command, timeout)

        self.fixture.probes.run = old_version
        result = self.fixture.attest()
        self.assertIn("tool_version_mismatch", result["failure_codes"])

    def test_profile_hash_and_platform_mismatches_block_admission(self):
        envelope = self.fixture.envelope.to_dict()
        envelope["worker_profile_hash"] = "sha256:" + "f" * 64
        envelope = hashed(envelope)
        result = self.fixture.attest(envelope=envelope)
        self.assertIn("profile_hash_mismatch", result["failure_codes"])

        self.fixture.probes.machine = lambda: "x86_64"
        result = self.fixture.attest()
        self.assertIn("unsupported_platform", result["failure_codes"])

    def test_checkout_revision_and_dirty_state_are_checked_exactly(self):
        original = self.fixture.run

        def bad_checkout(command, timeout=10):
            command = list(command)
            if command[0:2] == ["git", "-C"] and command[3:] == ["rev-parse", "HEAD"]:
                return CommandResult(0, "b" * 40 + "\n")
            if command[0:2] == ["git", "-C"] and command[3:] == ["status", "--porcelain", "--untracked-files=all"]:
                return CommandResult(0, "?? generated.out\n")
            return original(command, timeout)

        self.fixture.probes.run = bad_checkout
        result = self.fixture.attest()
        self.assertIn("checkout_revision_mismatch", result["failure_codes"])
        self.assertIn("checkout_dirty", result["failure_codes"])

    def test_path_privacy_writability_and_read_only_mode_are_enforced(self):
        source = self.fixture.layout.resolve("/work/source")
        os.chmod(source, 0o700)
        result = self.fixture.attest()
        self.assertIn("forbidden_mount", result["failure_codes"])
        os.chmod(source, 0o500)

        scratch = self.fixture.layout.resolve("/work/scratch")
        os.chmod(scratch, 0o755)
        result = self.fixture.attest()
        self.assertIn("path_not_private", result["failure_codes"])
        os.chmod(scratch, 0o700)

    def test_resource_headroom_failures_are_precise(self):
        self.fixture.probes.disk_free = lambda path: 1
        self.fixture.probes.memory_available = lambda: 1
        result = self.fixture.attest()
        self.assertIn("insufficient_disk", result["failure_codes"])
        self.assertIn("insufficient_memory", result["failure_codes"])

    def test_read_only_grant_does_not_require_an_unused_write_broker(self):
        self.fixture.probes.endpoint = lambda endpoint: False
        result = self.fixture.attest()
        self.assertNotIn("broker_unreachable", result["failure_codes"])

    def test_write_broker_and_report_channel_failures_are_distinct(self):
        fixture = DoctorFixture(self, capabilities=(
            "read_source", "report_status", "start_ltvm",
        ))
        fixture.probes.endpoint = lambda endpoint: False
        # Report is a writable logical file channel, so make its parent
        # inaccessible to exercise its independent failure code.
        output = fixture.layout.resolve("/work/output")
        os.chmod(output, 0o500)
        try:
            result = fixture.attest()
            self.assertIn("broker_unreachable", result["failure_codes"])
            self.assertIn("report_channel_failed", result["failure_codes"])
        finally:
            os.chmod(output, 0o700)
            fixture.close()

    def test_credential_references_are_never_serialized(self):
        self.fixture.probes.credential = lambda reference, environment: "unavailable"
        envelope = self.fixture.envelope.to_dict()
        envelope["credential_refs"] = [
            {"id": "gerrit-read", "reference": "broker:opaque-ref", "available": False}
        ]
        envelope = hashed(envelope)
        result = self.fixture.attest(envelope=envelope)
        self.assertIn("credential_unavailable", result["failure_codes"])
        serialized = json.dumps(result)
        self.assertNotIn("opaque-ref", serialized)

    def test_ltvm_owner_schema_and_owner_environment_are_required(self):
        fixture = DoctorFixture(self, capabilities=("read_source", "report_status", "start_ltvm"))
        try:
            result = fixture.attest()
            self.assertEqual(result["services"]["ltvm"]["healthy"], True)

            fixture.environment["LTVM_OWNER_ID"] = "some-other-run"
            result = fixture.attest()
            self.assertIn("ltvm_owner_mismatch", result["failure_codes"])

            original = fixture.run

            def old_ltvm(command, timeout=10):
                if list(command)[:3] == ["/opt/test/bin/ltvm", "list", "--json"]:
                    return CommandResult(0, '[{"name":"legacy"}]')
                return original(command, timeout)

            fixture.probes.run = old_ltvm
            result = fixture.attest()
            self.assertIn("ltvm_owner_unsupported", result["failure_codes"])
        finally:
            fixture.close()

    def test_forbidden_sensitive_environment_degrades_legacy_but_blocks_container(self):
        self.fixture.probes.environ = {"SOME_API_TOKEN": "never-serialize"}
        result = self.fixture.attest()
        self.assertEqual(result["status"], "degraded")
        self.assertNotIn("never-serialize", json.dumps(result))

        envelope = self.fixture.envelope.to_dict()
        envelope["isolation_mode"] = "container_standard"
        envelope = hashed(envelope)
        result = self.fixture.attest(envelope=envelope)
        self.assertIn("forbidden_environment", result["failure_codes"])


class WorkerDoctorUtilityTests(unittest.TestCase):
    def test_version_constraints(self):
        self.assertTrue(version_satisfies("tool v2.3.4", ">=2.3,<3"))
        self.assertTrue(version_satisfies("Python 3.12.13", ">=3.11,<4"))
        self.assertFalse(version_satisfies("tool 1.9", ">=2"))
        self.assertFalse(version_satisfies("not a version", ">=1"))

    def test_cli_emits_one_json_document_and_exit_reflects_blocking(self):
        result = {
            "schema_version": "1.0",
            "status": "blocked",
            "admitted": False,
            "failure_codes": ["tool_missing"],
        }
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.json"
            envelope_path = Path(directory) / "envelope.json"
            profile_path.write_text("{}")
            envelope_path.write_text("{}")
            os.chmod(envelope_path, 0o600)
            output = io.StringIO()
            with patch.object(pw_worker, "doctor", return_value=result), contextlib.redirect_stdout(output):
                code = pw_worker.main([
                    "doctor", "--profile", str(profile_path),
                    "--run-envelope", str(envelope_path), "--json",
                ])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["failure_codes"], ["tool_missing"])

    def test_cli_load_failure_is_still_a_canonical_redacted_attestation(self):
        result = pw_worker._load_failure_attestation("profile_unknown")
        parsed = EnvironmentAttestation.from_dict(result)
        self.assertEqual(parsed.status, "blocked")
        self.assertEqual(parsed.failure_codes, ["profile_unknown"])


if __name__ == "__main__":
    unittest.main()
