import json
from pathlib import Path
import stat
import tempfile
import unittest

from worker_contract import (
    ContractError,
    EnvironmentAttestation,
    RunEnvelope,
    WorkerProfile,
    build_run_envelope,
    canonical_json,
    content_hash,
    create_run_directories,
    generate_worker_instructions,
    hash_text,
    load_profile,
    redact_for_audit,
    validate_run_id,
    write_run_snapshot,
)


HERE = Path(__file__).resolve().parent
PROFILE_PATH = HERE / "worker_profiles" / "host-unsandboxed-mac-v1.json"
SCHEMA_DIRECTORY = HERE / "worker_schemas"
REVISION = "0123456789abcdef0123456789abcdef01234567"
NOW = "2026-08-30T18:00:00Z"


def profile_payload():
    with PROFILE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def make_profile():
    return WorkerProfile.from_dict(profile_payload())


def make_instructions(profile=None):
    profile = profile or make_profile()
    return generate_worker_instructions(
        profile,
        run_id="run-123",
        task="Inspect the pinned patch and report findings.\nDo not make external writes.",
        revision_sha=REVISION,
        capabilities=["read_source", "report_status"],
        organization_policy="Preserve unrelated user work.",
        repository_instructions="Run the repository's read-only checks.",
    )


def make_envelope(profile=None, instructions=None):
    profile = profile or make_profile()
    instructions = instructions or make_instructions(profile)
    return build_run_envelope(
        run_id="run-123",
        change_id="68160",
        patchset=4,
        revision_sha=REVISION,
        profile=profile,
        task="Inspect the pinned patch and report findings.",
        capabilities=["read_source", "report_status"],
        instructions_hash=hash_text(instructions),
        created_at=NOW,
    )


def attestation_payload(envelope=None):
    envelope = envelope or make_envelope()
    payload = {
        "schema_version": "1.0",
        "run_id": envelope.run_id,
        "content_hash": "sha256:" + "0" * 64,
        "created_at": NOW,
        "admitted": True,
        "status": "degraded",
        "worker_host": {
            "host_id": "mac-worker-1",
            "operating_system": "Darwin",
            "architecture": "arm64",
            "os_version": "26.6.2",
            "host_build_id": "25G83",
            "image_digest": "",
        },
        "worker_profile_id": envelope.worker_profile_id,
        "worker_profile_hash": envelope.worker_profile_hash,
        "run_envelope_hash": envelope.content_hash,
        "isolation_mode": envelope.isolation_mode,
        "network_mode": envelope.network_mode,
        "executables": [
            {
                "name": "git",
                "path": "/usr/bin/git",
                "version": "2.39.5",
                "required": True,
                "available": True,
                "api_version": "",
            },
            {
                "name": "lreview",
                "path": "",
                "version": "",
                "required": False,
                "available": False,
                "api_version": "0.1",
            },
        ],
        "services": {
            "worker_report_channel": {"healthy": True, "version": "1.0", "detail": "local"}
        },
        "checkout": {
            "path": "/work/source",
            "revision_sha": REVISION,
            "clean": True,
            "mount_mode": "read_only",
            "initial_state_hash": hash_text("clean checkout"),
            "free_bytes": 10_000_000,
        },
        "resource_limits": {"memory_bytes": 8_589_934_592, "disk_bytes": 21_474_836_480},
        "free_resources": {"memory_bytes": 0, "disk_bytes": 12_000_000_000},
        "config_schemas": {"gerrit": "0.2"},
        "warnings": ["Optional lreview command is unavailable."],
        "deviations": ["Ambient home state is allowed by this legacy profile."],
        "unavailable_optional_capabilities": ["lreview"],
        "failure_codes": [],
    }
    payload["content_hash"] = content_hash(payload)
    return payload


class WorkerProfileTests(unittest.TestCase):
    def test_checked_in_profile_loads_and_hashes(self):
        profile = load_profile("host-unsandboxed-mac-v1")
        self.assertEqual(profile.profile_id, "host-unsandboxed-mac-v1")
        self.assertEqual(profile.content_hash, content_hash(profile.to_dict()))
        self.assertEqual(profile.security_label, "trusted_unsandboxed")
        self.assertTrue(profile.ambient_home_allowed)
        self.assertNotIn("start_ltvm", profile.capabilities)
        self.assertNotIn("/Users/patrick", canonical_json(profile.to_dict()))

    def test_profile_round_trip_is_lossless(self):
        profile = make_profile()
        self.assertEqual(WorkerProfile.from_dict(profile.to_dict()).to_dict(), profile.to_dict())

    def test_profile_hash_mismatch_is_rejected(self):
        payload = profile_payload()
        payload["description"] += " changed"
        with self.assertRaisesRegex(ContractError, "content_hash_mismatch"):
            WorkerProfile.from_dict(payload)

    def test_profile_unknown_field_is_rejected(self):
        payload = profile_payload()
        payload["surprise"] = True
        payload["content_hash"] = content_hash(payload)
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            WorkerProfile.from_dict(payload)

    def test_profile_duplicate_capability_is_rejected(self):
        payload = profile_payload()
        payload["capabilities"].append(payload["capabilities"][0])
        payload["content_hash"] = content_hash(payload)
        with self.assertRaisesRegex(ContractError, "duplicates"):
            WorkerProfile.from_dict(payload)

    def test_unknown_profile_does_not_escape_profile_directory(self):
        with self.assertRaisesRegex(ContractError, "path_escape"):
            load_profile("../secrets")


class RunEnvelopeTests(unittest.TestCase):
    def test_generated_envelope_is_deterministic(self):
        profile = make_profile()
        instructions = make_instructions(profile)
        first = make_envelope(profile, instructions)
        second = make_envelope(profile, instructions)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.content_hash, content_hash(first.to_dict()))

    def test_envelope_round_trip_is_lossless(self):
        envelope = make_envelope()
        self.assertEqual(RunEnvelope.from_dict(envelope.to_dict()).to_dict(), envelope.to_dict())

    def test_envelope_hash_mismatch_is_rejected(self):
        payload = make_envelope().to_dict()
        payload["patchset"] += 1
        with self.assertRaisesRegex(ContractError, "content_hash_mismatch"):
            RunEnvelope.from_dict(payload)

    def test_profile_capability_is_enforced(self):
        with self.assertRaisesRegex(ContractError, "capability_not_supported"):
            build_run_envelope(
                run_id="run-123",
                change_id="68160",
                patchset=1,
                revision_sha=REVISION,
                profile=make_profile(),
                task="Do a forbidden thing.",
                capabilities=["write_gerrit"],
                instructions_hash=hash_text("instructions"),
                created_at=NOW,
            )

    def test_bad_revision_is_rejected(self):
        payload = make_envelope().to_dict()
        payload["revision_sha"] = "main"
        payload["content_hash"] = content_hash(payload)
        with self.assertRaisesRegex(ContractError, "hexadecimal revision"):
            RunEnvelope.from_dict(payload)

    def test_unsupported_schema_is_rejected_before_dispatch(self):
        payload = make_envelope().to_dict()
        payload["schema_version"] = "2.0"
        payload["content_hash"] = content_hash(payload)
        with self.assertRaisesRegex(ContractError, "unsupported_schema_version"):
            RunEnvelope.from_dict(payload)


class AttestationTests(unittest.TestCase):
    def test_attestation_round_trip_and_zero_free_resource(self):
        payload = attestation_payload()
        attestation = EnvironmentAttestation.from_dict(payload)
        self.assertEqual(attestation.status, "degraded")
        self.assertEqual(attestation.free_resources["memory_bytes"], 0)
        self.assertEqual(attestation.to_dict(), payload)

    def test_attestation_hash_mismatch_is_rejected(self):
        payload = attestation_payload()
        payload["warnings"].append("new warning")
        with self.assertRaisesRegex(ContractError, "content_hash_mismatch"):
            EnvironmentAttestation.from_dict(payload)

    def test_blocked_status_must_not_be_admitted(self):
        payload = attestation_payload()
        payload["status"] = "blocked"
        payload["content_hash"] = content_hash(payload)
        with self.assertRaisesRegex(ContractError, "admitted must be false"):
            EnvironmentAttestation.from_dict(payload)

    def test_unknown_attestation_status_is_rejected(self):
        payload = attestation_payload()
        payload["status"] = "probably-fine"
        payload["content_hash"] = content_hash(payload)
        with self.assertRaisesRegex(ContractError, "ready, degraded, or blocked"):
            EnvironmentAttestation.from_dict(payload)

    def test_blocked_status_requires_failure_code(self):
        payload = attestation_payload()
        payload["status"] = "blocked"
        payload["admitted"] = False
        payload["content_hash"] = content_hash(payload)
        with self.assertRaisesRegex(ContractError, "requires at least one failure code"):
            EnvironmentAttestation.from_dict(payload)


class RunDirectoryTests(unittest.TestCase):
    def test_private_run_layout_and_snapshot_permissions(self):
        profile = make_profile()
        instructions = make_instructions(profile)
        envelope = make_envelope(profile, instructions)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "workers"
            layout = create_run_directories(base, envelope.run_id)
            paths = write_run_snapshot(layout, envelope, instructions)
            self.assertEqual(stat.S_IMODE(layout.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o700)
            for directory in layout.logical_paths.values():
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for path in paths.values():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            stored = json.loads(paths["run_envelope"].read_text(encoding="utf-8"))
            self.assertEqual(stored, envelope.to_dict())

    def test_run_id_path_escapes_are_rejected(self):
        for run_id in ("../escape", "/absolute", "two/parts", "..", ""):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ContractError):
                    validate_run_id(run_id)

    def test_logical_path_escape_and_unknown_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = create_run_directories(Path(temporary) / "workers", "safe-run")
            with self.assertRaisesRegex(ContractError, "path_escape"):
                layout.resolve("/work/source/../../etc/passwd")
            with self.assertRaisesRegex(ContractError, "path_unknown"):
                layout.resolve("/etc/passwd")

    def test_existing_run_directory_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "workers"
            create_run_directories(base, "same-run")
            with self.assertRaises(FileExistsError):
                create_run_directories(base, "same-run")

    def test_instruction_hash_mismatch_is_rejected(self):
        envelope = make_envelope()
        with tempfile.TemporaryDirectory() as temporary:
            layout = create_run_directories(Path(temporary) / "workers", envelope.run_id)
            with self.assertRaisesRegex(ContractError, "instruction_hash_mismatch"):
                write_run_snapshot(layout, envelope, "different instructions")


class DeterminismAndRedactionTests(unittest.TestCase):
    def test_canonical_json_ignores_mapping_insertion_order(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))
        self.assertEqual(content_hash({"content_hash": "old", "b": 2, "a": 1}), content_hash({"a": 1, "b": 2}))

    def test_instruction_generation_is_deterministic_and_portable(self):
        first = make_instructions()
        second = make_instructions()
        self.assertEqual(first, second)
        self.assertIn("/work/source", first)
        self.assertNotIn("/Users/patrick", first)
        self.assertIn(REVISION, first)

    def test_redaction_removes_nested_secrets_and_url_userinfo(self):
        value = {
            "token": "never-log-me",
            "nested": {"password": "also-secret", "safe": "visible"},
            "endpoint": "https://user:pass@example.test/path?token=abc&view=full",
        }
        audit = redact_for_audit(value)
        serialized = canonical_json(audit)
        self.assertNotIn("never-log-me", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertNotIn("user", audit["endpoint"])
        self.assertNotIn("pass", audit["endpoint"])
        self.assertNotIn("abc", audit["endpoint"])
        self.assertIn("view=full", audit["endpoint"])
        self.assertEqual(audit["nested"]["safe"], "visible")

    def test_redaction_removes_inline_secret_assignments(self):
        audit = redact_for_audit({"detail": "GERRIT_PASS=oops token: also-oops harmless=yes"})
        self.assertNotIn("oops", audit["detail"])
        self.assertIn("GERRIT_PASS=[REDACTED]", audit["detail"])

    def test_model_audit_serialization_is_a_copy(self):
        envelope = make_envelope()
        audit = envelope.audit_dict()
        audit["task"] = "mutated"
        self.assertNotEqual(envelope.task, audit["task"])

    def test_checked_in_schemas_are_valid_json_and_versioned(self):
        expected = {
            "worker-profile-v1.schema.json",
            "run-envelope-v1.schema.json",
            "environment-attestation-v1.schema.json",
        }
        self.assertEqual({path.name for path in SCHEMA_DIRECTORY.glob("*.json")}, expected)
        for name in expected:
            with self.subTest(name=name):
                with (SCHEMA_DIRECTORY / name).open("r", encoding="utf-8") as handle:
                    schema = json.load(handle)
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
