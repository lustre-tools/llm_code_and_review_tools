#!/usr/bin/env python3
"""Patch Watcher worker-side command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from worker_doctor import as_mapping, canonical_content_hash, doctor, load_json


def _load_profile(specification: str) -> dict[str, Any]:
    path = Path(specification).expanduser()
    if path.is_file():
        return load_json(path)

    # worker_contract is intentionally optional here: this CLI also runs in a
    # bootstrap environment that may only have a serialized profile file.
    try:
        import worker_contract  # type: ignore
    except ImportError:
        raise ValueError(f"unknown worker profile: {specification}") from None
    for name in ("load_worker_profile", "load_profile", "get_profile"):
        loader = getattr(worker_contract, name, None)
        if callable(loader):
            try:
                return as_mapping(loader(specification))
            except (KeyError, ValueError, FileNotFoundError):
                pass
    for name in ("BUILTIN_PROFILES", "WORKER_PROFILES", "PROFILES"):
        registry = getattr(worker_contract, name, None)
        if isinstance(registry, Mapping) and specification in registry:
            return as_mapping(registry[specification])
    raise ValueError(f"unknown worker profile: {specification}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pw-worker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subcommands.add_parser("doctor", help="admit a worker environment")
    doctor_parser.add_argument("--profile", required=True, help="worker profile ID or JSON path")
    doctor_parser.add_argument("--run-envelope", required=True, type=Path, help="private run-envelope JSON path")
    doctor_parser.add_argument("--json", action="store_true", help="emit one JSON attestation")
    return parser


def _load_failure_attestation(code: str) -> dict[str, Any]:
    """Return a canonical, secret-free attestation when contracts cannot load."""

    digest = "sha256:" + "0" * 64
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "content_hash": digest,
        "created_at": "1970-01-01T00:00:00+00:00",
        "run_id": "admission-load-error",
        "admitted": False,
        "status": "blocked",
        "worker_host": {
            "host_id": "unknown",
            "operating_system": "unknown",
            "architecture": "unknown",
            "os_version": "unknown",
            "host_build_id": "unknown",
            "image_digest": "",
        },
        "worker_profile_id": "unknown",
        "worker_profile_hash": digest,
        "run_envelope_hash": digest,
        "isolation_mode": "unknown",
        "network_mode": "unknown",
        "executables": [],
        "services": {},
        "checkout": {
            "path": "unavailable",
            "revision_sha": "0" * 40,
            "clean": False,
            "mount_mode": "unknown",
            "initial_state_hash": digest,
            "free_bytes": 0,
        },
        "resource_limits": {},
        "free_resources": {},
        "config_schemas": {},
        "warnings": [],
        "deviations": [code],
        "unavailable_optional_capabilities": [],
        "failure_codes": [code],
    }
    result["content_hash"] = canonical_content_hash(result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = _load_profile(args.profile)
        envelope_path = args.run_envelope.expanduser()
        envelope = load_json(envelope_path)
        result = doctor(profile, envelope, envelope_path=envelope_path)
    except ValueError as exc:
        result = _load_failure_attestation(
            "profile_unknown" if "profile" in str(exc) else "envelope_invalid"
        )
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(f"Worker admission: {result['status']}")
        for code in result.get("failure_codes", []):
            print(f"- {code}")
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
