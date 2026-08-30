"""Admission checks for Patch Watcher worker environments.

The doctor deliberately accepts plain mappings.  ``worker_contract`` owns the
canonical profile and run-envelope models, while this module remains useful to
bootstrap and recovery code that only has their serialized JSON form.

Checks never read credential contents and never perform remote writes.  The
default probes execute local version/readiness commands and read local
metadata; tests can inject every operation that could vary by host.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse


ATTESTATION_SCHEMA = "patch-watcher-environment-attestation/v1"
SUPPORTED_PROFILE_SCHEMA_VERSION = 1
SUPPORTED_ENVELOPE_SCHEMA_VERSION = 1
_HASH_FIELDS = frozenset({"content_hash"})
_SECRET_NAME_RE = re.compile(
    r"(?:PASS(?:WORD)?|TOKEN|SECRET|PRIVATE_KEY|AUTH_COOKIE|API_KEY)$",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){0,3})")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _default_run(command: Sequence[str], timeout: float = 10.0) -> CommandResult:
    """Run one bounded local command without a shell."""

    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(127, "", type(exc).__name__)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _default_memory_available() -> int | None:
    """Return host-available memory using dependency-free OS interfaces."""

    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        result = _default_run(["/usr/bin/vm_stat"])
        if result.returncode:
            return None
        page_size = 4096
        match = re.search(r"page size of (\d+) bytes", result.stdout)
        if match:
            page_size = int(match.group(1))
        counts: dict[str, int] = {}
        for line in result.stdout.splitlines():
            match = re.match(r"Pages (free|inactive|speculative):\s+(\d+)\.", line)
            if match:
                counts[match.group(1)] = int(match.group(2))
        if counts:
            return sum(counts.values()) * page_size
    return None


def _default_endpoint(endpoint: str, timeout: float = 0.5) -> bool:
    """Probe endpoint transport only; send no application request."""

    if not endpoint:
        return False
    if endpoint.startswith("unix://"):
        address = endpoint[7:]
        family = socket.AF_UNIX
    elif endpoint.startswith("/"):
        address = endpoint
        family = socket.AF_UNIX
    else:
        parsed = urlparse(endpoint)
        if parsed.scheme == "local":
            # ``local://`` is a logical endpoint, not evidence of a live
            # transport.  A controller-aware injected probe must verify it.
            return False
        if parsed.scheme not in {"http", "https", "tcp"} or not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((parsed.hostname, port), timeout=timeout):
                return True
        except OSError:
            return False
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(address)
        return True
    except OSError:
        return False


def _default_credential(reference: Any, environment: Mapping[str, str]) -> str:
    """Return available/expired/unavailable/unverified without reading a secret."""

    if isinstance(reference, Mapping):
        if reference.get("expired") is True:
            return "expired"
        expires = reference.get("expires_at")
        if expires:
            try:
                expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if expiry <= datetime.now(timezone.utc):
                    return "expired"
            except ValueError:
                return "unverified"
        if isinstance(reference.get("available"), bool):
            return "available" if reference["available"] else "unavailable"
        reference = reference.get("reference") or reference.get("ref") or ""
    text = str(reference)
    if text.startswith("env:"):
        return "available" if text[4:] in environment else "unavailable"
    if text.startswith("file:"):
        path = Path(text[5:]).expanduser()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return "unavailable"
        return "available" if mode & 0o077 == 0 else "unavailable"
    # An opaque broker/keychain reference can only be verified by its broker.
    return "unverified"


@dataclass
class DoctorProbes:
    system: Callable[[], str] = platform.system
    machine: Callable[[], str] = platform.machine
    hostname: Callable[[], str] = socket.gethostname
    python_version: Callable[[], str] = platform.python_version
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[Sequence[str], float], CommandResult] = _default_run
    disk_free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free
    memory_available: Callable[[], int | None] = _default_memory_available
    endpoint: Callable[[str], bool] = _default_endpoint
    credential: Callable[[Any, Mapping[str, str]], str] = _default_credential
    environ: Mapping[str, str] | None = None

    def environment(self) -> Mapping[str, str]:
        return os.environ if self.environ is None else self.environ


def as_mapping(value: Any) -> dict[str, Any]:
    """Convert a contract dataclass/model/mapping into a JSON-like mapping."""

    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    for method_name in ("to_dict", "as_dict", "model_dump"):
        method = getattr(value, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, Mapping):
                return dict(result)
    raise TypeError("contract object must be a mapping or serializable model")


def _without_hash_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_hash_fields(item)
            for key, item in value.items()
            if str(key) not in _HASH_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_without_hash_fields(item) for item in value]
    return value


def canonical_content_hash(value: Mapping[str, Any] | Any) -> str:
    """Hash one contract object without circular embedded hash fields."""

    mapping = as_mapping(value)
    unhashed = dict(mapping)
    unhashed.pop("content_hash", None)
    payload = json.dumps(
        unhashed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _declared_hash(mapping: Mapping[str, Any]) -> str | None:
    for key in ("content_hash", "hash", "sha256"):
        if mapping.get(key):
            value = str(mapping[key])
            return value if value.startswith("sha256:") else "sha256:" + value
    return None


def _schema_version(schema: Any, expected_kind: str) -> int | None:
    # Canonical worker_contract objects carry ``schema_version: "1.0"``;
    # callers pass that scalar here when no descriptive schema URI exists.
    if isinstance(schema, (str, int, float)) and re.fullmatch(r"\d+(?:\.\d+)?", str(schema)):
        return int(float(str(schema)))
    if isinstance(schema, Mapping):
        kind = str(schema.get("kind", "")).lower().replace("_", "-")
        version = schema.get("version")
        if expected_kind.replace("_", "-") not in kind:
            return None
        try:
            return int(version)
        except (TypeError, ValueError):
            return None
    text = str(schema or "").lower().replace("_", "-")
    pieces = expected_kind.split("_")
    if not all(piece in text for piece in pieces):
        return None
    match = re.search(r"(?:/|[-])v?(\d+)$", text)
    return int(match.group(1)) if match else None


def _first(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _nested(mapping: Mapping[str, Any], *names: str) -> dict[str, Any]:
    value = _first(mapping, *names, default={})
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(item, id=key) if isinstance(item, Mapping) else {"id": key, "value": item}
                for key, item in value.items()]
    if isinstance(value, (str, bytes)):
        return [value]
    return list(value)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.search(text)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _pad_versions(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def version_satisfies(version_output: str, constraint: str | None) -> bool:
    """Evaluate simple comma-separated numeric constraints dependency-free."""

    if not constraint or str(constraint).strip() in {"*", "any"}:
        return _version_tuple(version_output) is not None
    actual = _version_tuple(version_output)
    if actual is None:
        return False
    for part in str(constraint).split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|=|>|<|~=|\^)?\s*v?(\d+(?:\.\d+){0,3})\s*", part)
        if not match:
            return False
        operator = match.group(1) or "=="
        required = tuple(int(item) for item in match.group(2).split("."))
        left, right = _pad_versions(actual, required)
        if operator in {"=", "=="} and left != right:
            return False
        if operator == ">=" and left < right:
            return False
        if operator == "<=" and left > right:
            return False
        if operator == ">" and left <= right:
            return False
        if operator == "<" and left >= right:
            return False
        if operator == "~=" and not (left >= right and left[: max(1, len(required) - 1)] == right[: max(1, len(required) - 1)]):
            return False
        if operator == "^" and not (left >= right and left[0] == right[0]):
            return False
    return True


def _redact_path(value: str) -> str:
    home = str(Path.home())
    if value == home:
        return "$HOME"
    if value.startswith(home + os.sep):
        return "$HOME" + value[len(home):]
    return value


def _safe_version(output: str) -> str:
    """Return only a short version-shaped excerpt, never arbitrary output."""

    match = _VERSION_RE.search(output)
    return match.group(1) if match else "unknown"


class _AttestationBuilder:
    def __init__(self, profile: Mapping[str, Any], envelope: Mapping[str, Any], probes: DoctorProbes):
        self.profile = profile
        self.envelope = envelope
        self.probes = probes
        self.checks: list[dict[str, Any]] = []
        self.failures: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []
        self.tools: list[dict[str, Any]] = []
        self.services: dict[str, dict[str, Any]] = {}
        self.checkout: dict[str, Any] = {}
        self.free_resources: dict[str, int] = {}
        self.unavailable_optional: list[str] = []

    def record(self, check: str, status_value: str, code: str, message: str, **details: Any) -> None:
        item: dict[str, Any] = {
            "check": check,
            "status": status_value,
            "code": code,
            "message": message,
        }
        if details:
            item["details"] = details
        self.checks.append(item)
        summary = {"code": code, "check": check, "message": message}
        if status_value == "blocked":
            self.failures.append(summary)
        elif status_value == "degraded":
            self.warnings.append(summary)

    def ok(self, check: str, message: str, **details: Any) -> None:
        self.record(check, "ready", "ok", message, **details)

    def block(self, check: str, code: str, message: str, **details: Any) -> None:
        self.record(check, "blocked", code, message, **details)

    def warn(self, check: str, code: str, message: str, **details: Any) -> None:
        self.record(check, "degraded", code, message, **details)

    def result(self) -> dict[str, Any]:
        overall = "blocked" if self.failures else ("degraded" if self.warnings else "ready")
        profile_id = str(_first(self.profile, "profile_id", "id", "name", default="unknown"))
        run_id = str(_first(self.envelope, "run_id", "id", default="unknown"))
        profile_hash = canonical_content_hash(self.profile)
        envelope_hash = canonical_content_hash(self.envelope)
        result = {
            "schema_version": "1.0",
            "content_hash": "sha256:" + "0" * 64,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": overall,
            "admitted": not self.failures,
            "run_id": run_id,
            "worker_host": {
                "host_id": hashlib.sha256(self.probes.hostname().encode()).hexdigest()[:16],
                "operating_system": self.probes.system().lower(),
                "architecture": self.probes.machine().lower(),
                "os_version": platform.release(),
                "host_build_id": hashlib.sha256(platform.version().encode()).hexdigest()[:16],
                "image_digest": str(_first(self.profile, "image_digest", default="")),
            },
            "worker_profile_id": profile_id,
            "worker_profile_hash": profile_hash,
            "run_envelope_hash": envelope_hash,
            "isolation_mode": str(_first(self.envelope, "isolation_mode", "isolation", default="unknown")),
            "network_mode": str(_first(self.envelope, "network_mode", "network", "network_profile", default="unknown")),
            "executables": self.tools,
            "services": self.services,
            "checkout": self.checkout,
            "resource_limits": {
                str(key): int(value)
                for key, value in _nested(self.envelope, "budgets", "resource_limits", "limits").items()
                if isinstance(value, int) and not isinstance(value, bool)
            },
            "free_resources": self.free_resources,
            "config_schemas": {
                "worker_profile": str(_first(self.profile, "schema_version", default="unknown")),
                "run_envelope": str(_first(self.envelope, "schema_version", default="unknown")),
                "worker_report": "patch-watcher-worker/v1",
            },
            "warnings": list(dict.fromkeys(item["code"] + ": " + item["message"] for item in self.warnings)),
            "deviations": list(dict.fromkeys(item["code"] + ": " + item["message"] for item in self.failures)),
            "unavailable_optional_capabilities": sorted(set(self.unavailable_optional)),
            "failure_codes": list(dict.fromkeys(item["code"] for item in self.failures)),
        }
        result["content_hash"] = canonical_content_hash(result)
        return result


def _check_contract(builder: _AttestationBuilder) -> None:
    profile, envelope = builder.profile, builder.envelope
    profile_version = _schema_version(_first(profile, "schema", "schema_version"), "worker_profile")
    envelope_version = _schema_version(_first(envelope, "schema", "schema_version"), "run_envelope")
    if profile_version != SUPPORTED_PROFILE_SCHEMA_VERSION:
        builder.block("profile_schema", "profile_schema_incompatible", "worker profile schema is not supported")
    else:
        builder.ok("profile_schema", "worker profile schema is supported", version=profile_version)
    if envelope_version != SUPPORTED_ENVELOPE_SCHEMA_VERSION:
        builder.block("envelope_schema", "envelope_schema_incompatible", "run envelope schema is not supported")
    else:
        builder.ok("envelope_schema", "run envelope schema is supported", version=envelope_version)

    profile_id = str(_first(profile, "profile_id", "id", "name", default=""))
    expected_id = str(_first(envelope, "worker_profile_id", "profile_id", default=""))
    if not profile_id or (expected_id and expected_id != profile_id):
        builder.block("profile_identity", "profile_unknown", "run envelope does not identify this worker profile")
    else:
        builder.ok("profile_identity", "run envelope identifies the selected profile")

    declared_profile_hash = _declared_hash(profile)
    actual_profile_hash = canonical_content_hash(profile)
    expected_hash = _first(envelope, "worker_profile_hash", "expected_profile_hash", "profile_hash")
    if expected_hash and not str(expected_hash).startswith("sha256:"):
        expected_hash = "sha256:" + str(expected_hash)
    if declared_profile_hash and declared_profile_hash != actual_profile_hash:
        builder.block("profile_hash", "profile_hash_invalid", "worker profile content does not match its declared hash")
    elif not expected_hash:
        builder.block("profile_hash", "profile_hash_missing", "run envelope has no expected worker profile hash")
    elif expected_hash != actual_profile_hash:
        builder.block("profile_hash", "profile_hash_mismatch", "run envelope expects a different worker profile")
    else:
        builder.ok("profile_hash", "worker profile hash matches the run envelope")

    declared_envelope_hash = _declared_hash(envelope)
    if declared_envelope_hash and declared_envelope_hash != canonical_content_hash(envelope):
        builder.block("envelope_hash", "envelope_hash_mismatch", "run envelope content does not match its declared hash")
    else:
        builder.ok("envelope_hash", "run envelope hash is internally consistent")


def _check_platform(builder: _AttestationBuilder) -> None:
    profile = builder.profile
    actual_os = builder.probes.system().lower()
    actual_arch = builder.probes.machine().lower()
    platform_section = _nested(profile, "supported_hosts", "platform", "host")
    allowed_os = _list(_first(profile, "supported_os", "os", default=_first(platform_section, "operating_systems", "os", "systems", default=[])))
    allowed_arch = _list(_first(profile, "supported_architectures", "architectures", "arch", default=_first(platform_section, "architectures", "arch", default=[])))
    allowed_os = [str(item).lower() for item in allowed_os]
    allowed_arch = [str(item).lower() for item in allowed_arch]
    arch_aliases = {"aarch64": "arm64", "amd64": "x86_64"}
    normalized_arch = arch_aliases.get(actual_arch, actual_arch)
    normalized_allowed = [arch_aliases.get(item, item) for item in allowed_arch]
    if allowed_os and actual_os not in allowed_os:
        builder.block("platform", "unsupported_platform", "host operating system is not allowed", actual_os=actual_os)
    elif normalized_allowed and normalized_arch not in normalized_allowed:
        builder.block("platform", "unsupported_platform", "host architecture is not allowed", actual_arch=actual_arch)
    else:
        builder.ok("platform", "host platform is compatible", os=actual_os, architecture=actual_arch)


def _command_specs(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    runtime = _nested(profile, "runtime")
    raw = _first(profile, "commands", "tools", default=_first(runtime, "commands", "tools", default=[]))
    specs = _list(raw)
    normalized: list[dict[str, Any]] = []
    for spec in specs:
        if isinstance(spec, str):
            normalized.append({"id": spec, "command": spec, "required": True})
        elif isinstance(spec, Mapping):
            normalized.append(dict(spec))
    return normalized


def _check_commands(builder: _AttestationBuilder) -> None:
    for spec in _command_specs(builder.profile):
        tool_id = str(_first(spec, "id", "name", default=_first(spec, "command", "executable", default="tool")))
        command = str(_first(spec, "command", "executable", default=tool_id))
        required = bool(spec.get("required", not spec.get("optional", False)))
        path = builder.probes.which(command)
        if not path:
            builder.tools.append({
                "name": tool_id,
                "path": "",
                "version": "",
                "required": required,
                "available": False,
                "api_version": str(spec.get("api_version", "")),
            })
            if not required:
                builder.unavailable_optional.append(tool_id)
            method = builder.block if required else builder.warn
            method("tool:" + tool_id, "tool_missing", f"required tool {tool_id} is not available" if required else f"optional tool {tool_id} is not available")
            continue
        version_args = _list(_first(spec, "version_args", default=["--version"]))
        result = builder.probes.run([path, *map(str, version_args)], float(spec.get("timeout_seconds", 10)))
        if result.returncode != 0:
            builder.tools.append({
                "name": tool_id,
                "path": _redact_path(str(Path(path).resolve())),
                "version": "",
                "required": required,
                "available": False,
                "api_version": str(spec.get("api_version", "")),
            })
            if not required:
                builder.unavailable_optional.append(tool_id)
            method = builder.block if required else builder.warn
            method("tool:" + tool_id, "tool_unusable", f"tool {tool_id} exists but its probe failed")
            continue
        output = (result.stdout + "\n" + result.stderr).strip()
        constraint = _first(spec, "version_constraint", "constraint", "version", default=None)
        if constraint and not version_satisfies(output, str(constraint)):
            builder.tools.append({
                "name": tool_id,
                "path": _redact_path(str(Path(path).resolve())),
                "version": _safe_version(output),
                "required": required,
                "available": False,
                "api_version": str(spec.get("api_version", "")),
            })
            if not required:
                builder.unavailable_optional.append(tool_id)
            method = builder.block if required else builder.warn
            method("tool:" + tool_id, "tool_version_mismatch", f"tool {tool_id} does not satisfy its version constraint", actual=_safe_version(output), required=str(constraint))
            continue
        resolved = str(Path(path).resolve())
        expected_path = spec.get("expected_path")
        if expected_path and resolved != str(Path(str(expected_path)).expanduser().resolve()):
            builder.tools.append({
                "name": tool_id,
                "path": _redact_path(resolved),
                "version": _safe_version(output),
                "required": required,
                "available": False,
                "api_version": str(spec.get("api_version", "")),
            })
            if not required:
                builder.unavailable_optional.append(tool_id)
            method = builder.block if required else builder.warn
            method("tool:" + tool_id, "tool_path_mismatch", f"tool {tool_id} resolved from an unexpected installation")
            continue
        builder.tools.append({
            "name": tool_id,
            "path": _redact_path(resolved),
            "version": _safe_version(output),
            "required": required,
            "available": True,
            "api_version": str(spec.get("api_version", "")),
        })
        builder.ok("tool:" + tool_id, f"tool {tool_id} is usable")

    # The doctor interpreter is itself part of the trusted selected runtime.
    python_constraint = _first(_nested(builder.profile, "runtimes", "runtime"), "python", "python_version", default=None)
    if isinstance(python_constraint, Mapping):
        python_constraint = _first(python_constraint, "version_constraint", "constraint", "version")
    selected_python = builder.probes.python_version()
    if python_constraint and not version_satisfies(selected_python, str(python_constraint)):
        builder.block("selected_python", "tool_version_mismatch", "selected doctor Python does not satisfy the profile", actual=selected_python, required=str(python_constraint))
    else:
        builder.ok("selected_python", "selected doctor Python is compatible", version=selected_python)


def _run_root(envelope_path: Path | None) -> Path | None:
    if envelope_path is None:
        return None
    resolved = envelope_path.expanduser().resolve()
    # create_run_directories writes ROOT/run/patch-watcher/run-envelope.json.
    if resolved.parent.name == "patch-watcher" and resolved.parent.parent.name == "run":
        return resolved.parent.parent.parent
    return resolved.parent


def _physical_logical_path(logical: str, envelope_path: Path | None) -> Path | None:
    root = _run_root(envelope_path)
    if root is None or not logical.startswith("/"):
        return None
    return root.joinpath(*Path(logical).parts[1:])


def _checkout(envelope: Mapping[str, Any], envelope_path: Path | None = None) -> dict[str, Any]:
    checkout = _nested(envelope, "checkout", "source_checkout")
    paths = _nested(envelope, "paths", "logical_paths")
    if "path" not in checkout:
        source = _first(paths, "source", "checkout")
        if isinstance(source, Mapping):
            checkout["path"] = _first(source, "path", "host_path", "value")
        elif source:
            checkout["path"] = source
    if "path" not in checkout and "/work/source" in paths:
        physical = _physical_logical_path("/work/source", envelope_path)
        if physical is not None:
            checkout["path"] = str(physical)
    if "revision" not in checkout:
        checkout["revision"] = _first(envelope, "revision_sha", "revision", "commit_sha")
    return checkout


def _check_checkout(builder: _AttestationBuilder, envelope_path: Path | None) -> None:
    checkout = _checkout(builder.envelope, envelope_path)
    path_value = checkout.get("path")
    revision = _first(checkout, "revision", "revision_sha", "commit_sha")
    if not path_value or not revision:
        builder.block("checkout", "checkout_missing", "run envelope has no exact checkout path and revision")
        builder.checkout = {
            "path": "unavailable",
            "revision_sha": str(revision or "0" * 40).lower(),
            "clean": False,
            "mount_mode": str(_first(builder.envelope, "checkout_mode", default="unknown")),
            "initial_state_hash": "sha256:" + hashlib.sha256(b"missing").hexdigest(),
            "free_bytes": 0,
        }
        return
    path = Path(str(path_value)).expanduser()
    mount_mode = str(_first(checkout, "mode", "mount_mode", default=_first(builder.envelope, "checkout_mode", default="unknown")))
    if not path.is_dir():
        builder.block("checkout", "checkout_missing", "checkout directory is unavailable")
        builder.checkout = {
            "path": _redact_path(str(path)),
            "revision_sha": str(revision),
            "clean": False,
            "mount_mode": mount_mode,
            "initial_state_hash": "sha256:" + hashlib.sha256(b"missing").hexdigest(),
            "free_bytes": 0,
        }
        return
    head = builder.probes.run(["git", "-C", str(path), "rev-parse", "HEAD"], 10)
    if head.returncode or head.stdout.strip() != str(revision):
        builder.block("checkout_revision", "checkout_revision_mismatch", "checkout is not at the pinned revision")
    else:
        builder.ok("checkout_revision", "checkout is at the pinned revision", revision=str(revision))
    dirty = builder.probes.run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"], 10)
    if dirty.returncode:
        builder.block("checkout_clean", "checkout_unusable", "checkout cleanliness probe failed")
    elif dirty.stdout.strip():
        builder.block("checkout_clean", "checkout_dirty", "checkout contains initial modifications or untracked files")
    else:
        builder.ok("checkout_clean", "checkout initial state is clean")
    try:
        free_bytes = builder.probes.disk_free(path)
    except OSError:
        free_bytes = 0
    state_material = (head.stdout.strip() + "\0" + dirty.stdout).encode()
    builder.checkout = {
        "path": _redact_path(str(path.resolve())),
        "revision_sha": str(revision).lower(),
        "clean": dirty.returncode == 0 and not dirty.stdout.strip(),
        "mount_mode": mount_mode,
        "initial_state_hash": "sha256:" + hashlib.sha256(state_material).hexdigest(),
        "free_bytes": free_bytes,
    }


def _path_specs(profile: Mapping[str, Any], envelope: Mapping[str, Any], envelope_path: Path | None) -> list[dict[str, Any]]:
    profile_paths = _nested(profile, "logical_paths", "paths")
    envelope_paths = _nested(envelope, "logical_paths", "paths")
    result: list[dict[str, Any]] = []
    for name, value in envelope_paths.items():
        policy = profile_paths.get(name, {})
        policy = dict(policy) if isinstance(policy, Mapping) else {"mode": policy}
        if str(name).startswith("/") and isinstance(value, str) and value in {
            "read_only", "read_write", "private_read_only", "private_read_write"
        }:
            item = {"mode": value, "private": value.startswith("private_")}
            physical = _physical_logical_path(str(name), envelope_path)
            path_value = str(physical) if physical is not None else None
        elif isinstance(value, Mapping):
            item = dict(value)
            path_value = _first(item, "path", "host_path", "value")
        else:
            item = {}
            path_value = value
        item.update({key: val for key, val in policy.items() if key not in item})
        item["name"] = name
        item["path"] = path_value
        result.append(item)
    return result


def _check_paths(builder: _AttestationBuilder, envelope_path: Path | None) -> None:
    environment = builder.probes.environment()
    forbidden_roots = [Path(str(item)).expanduser() for item in _list(_first(builder.profile, "forbidden_mounts", "forbidden_paths", default=[]))]
    for spec in _path_specs(builder.profile, builder.envelope, envelope_path):
        name = str(spec["name"])
        path_value = spec.get("path")
        if not path_value:
            builder.block("path:" + name, "path_missing", f"logical path {name} has no host mapping")
            continue
        path = Path(str(path_value)).expanduser()
        expected_kind = str(spec.get("kind", "directory"))
        exists = path.is_file() if expected_kind == "file" else path.is_dir()
        if not exists:
            builder.block("path:" + name, "path_missing", f"logical path {name} is unavailable")
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            builder.block("path:" + name, "path_unreadable", f"logical path {name} cannot be inspected")
            continue
        private = bool(spec.get("private", name in {"scratch", "artifacts", "logs", "output", "runtime", "run_envelope"}))
        if private and mode & 0o077:
            builder.block("path:" + name, "path_not_private", f"logical path {name} permits group or other access")
        else:
            builder.ok("path_privacy:" + name, f"logical path {name} has acceptable privacy")
        declared_mode = str(_first(spec, "mode", "access", default="read_write"))
        writable = os.access(path, os.W_OK)
        wants_write = declared_mode in {"rw", "read_write", "private_read_write"}
        wants_read_only = declared_mode in {"ro", "read-only", "readonly", "read_only", "private_read_only"}
        if wants_write and not writable:
            builder.block("path_writable:" + name, "path_not_writable", f"logical path {name} is not writable")
        elif wants_read_only and writable:
            builder.block("path_read_only:" + name, "forbidden_mount", f"logical path {name} is writable but must be read-only")
        else:
            builder.ok("path_access:" + name, f"logical path {name} access matches its declared mode")
        resolved = path.resolve()
        for forbidden in forbidden_roots:
            try:
                resolved.relative_to(forbidden.resolve())
            except ValueError:
                continue
            builder.block("path_mount:" + name, "forbidden_mount", f"logical path {name} is inside a forbidden host path")
            break

    if envelope_path is not None:
        try:
            mode = stat.S_IMODE(envelope_path.stat().st_mode)
        except OSError:
            builder.block("run_envelope_privacy", "envelope_unreadable", "run envelope cannot be inspected")
        else:
            if mode & 0o077:
                builder.block("run_envelope_privacy", "envelope_not_private", "run envelope is not mode 0600 or stricter")
            else:
                builder.ok("run_envelope_privacy", "run envelope is private")

    forbidden_names = set(map(str, _list(_first(builder.profile, "forbidden_environment", "forbidden_env", default=[]))))
    forbidden_names.update(name for name in environment if _SECRET_NAME_RE.search(name))
    allowed = set(map(str, _list(_first(builder.profile, "allowed_environment", "allowed_env", default=[]))))
    present = sorted(name for name in forbidden_names - allowed if name in environment)
    if present:
        isolation = str(_first(builder.envelope, "isolation_mode", "isolation", default=_first(builder.profile, "security_label", "isolation", default="")))
        status_method = builder.warn if "unsandboxed" in isolation else builder.block
        status_method("forbidden_environment", "forbidden_environment", "sensitive or forbidden ambient environment names are present", names=present)
    else:
        builder.ok("forbidden_environment", "no forbidden ambient environment names are present")


def _resource_limits(profile: Mapping[str, Any], envelope: Mapping[str, Any]) -> dict[str, Any]:
    profile_limits = _nested(profile, "resources", "resource_limits", "limits")
    envelope_limits = _nested(envelope, "resources", "resource_limits", "limits", "budgets")
    merged = dict(profile_limits)
    merged.update(envelope_limits)
    return merged


def _check_resources(builder: _AttestationBuilder, envelope_path: Path | None) -> None:
    limits = _resource_limits(builder.profile, builder.envelope)
    checkout = _checkout(builder.envelope, envelope_path)
    disk_path = Path(str(checkout.get("path") or ".")).expanduser()
    min_disk = int(_first(limits, "minimum_free_disk_bytes", "min_free_disk_bytes", "disk_headroom_bytes", "disk_bytes", default=0) or 0)
    try:
        free_disk = builder.probes.disk_free(disk_path)
    except OSError:
        builder.block("disk_headroom", "disk_probe_failed", "free disk space could not be measured")
    else:
        builder.free_resources["disk_bytes"] = max(0, int(free_disk))
        if free_disk < min_disk:
            builder.block("disk_headroom", "insufficient_disk", "worker has insufficient free disk space", available_bytes=free_disk, required_bytes=min_disk)
        else:
            builder.ok("disk_headroom", "worker has sufficient free disk space", available_bytes=free_disk, required_bytes=min_disk)
    min_memory = int(_first(limits, "minimum_available_memory_bytes", "min_available_memory_bytes", "memory_headroom_bytes", "memory_bytes", default=0) or 0)
    available_memory = builder.probes.memory_available()
    if available_memory is None:
        builder.block("memory_headroom", "memory_probe_failed", "available memory could not be measured")
    elif available_memory < min_memory:
        builder.free_resources["memory_bytes"] = max(0, int(available_memory))
        builder.block("memory_headroom", "insufficient_memory", "worker has insufficient available memory", available_bytes=available_memory, required_bytes=min_memory)
    else:
        builder.free_resources["memory_bytes"] = max(0, int(available_memory))
        builder.ok("memory_headroom", "worker has sufficient available memory", available_bytes=available_memory, required_bytes=min_memory)


def _endpoints(envelope: Mapping[str, Any], envelope_path: Path | None) -> dict[str, str]:
    raw = _nested(envelope, "endpoints", "channels")
    result: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(value, Mapping):
            value = _first(value, "url", "endpoint", "address", "path")
        if value:
            text = str(value)
            physical = _physical_logical_path(text, envelope_path)
            result[str(name)] = str(physical) if physical is not None and text.startswith(("/work/", "/run/patch-watcher/")) else text
    for name in ("broker", "report", "heartbeat", "artifact", "controller"):
        value = _first(envelope, name + "_endpoint", name + "_channel")
        if value:
            text = str(value)
            physical = _physical_logical_path(text, envelope_path)
            result.setdefault(name, str(physical) if physical is not None and text.startswith(("/work/", "/run/patch-watcher/")) else text)
    return result


def _check_endpoints(builder: _AttestationBuilder, envelope_path: Path | None) -> None:
    endpoints = _endpoints(builder.envelope, envelope_path)
    services = _list(_first(builder.profile, "required_host_services", "host_services", "services", default=[]))
    service_endpoint = {
        "artifact_collector": "artifact",
        "claude_runner": "controller",
        "resource_sampler": "heartbeat",
        "worker_report_channel": "report",
        "report_channel": "report",
        "tool_broker": "broker",
        "ltvm_bridge": "broker",
    }
    # Read-only Phase 0C runs have no typed external action to broker.  Making
    # a broker mandatory in that case creates a bootstrap deadlock (and, more
    # importantly, claims a write path exists when it deliberately does not).
    # The broker becomes mandatory as soon as the grant includes an operation
    # which can mutate external state or create worker resources.
    broker_capabilities = {
        "start_ltvm",
        "request_retest",
        "comment_gerrit",
        "vote_gerrit",
        "upload_patchset",
        "write_external",
    }
    required: dict[str, str] = {"report": "report"}
    if _capabilities(builder.envelope) & broker_capabilities:
        required["broker"] = "broker"
    for service in services:
        if isinstance(service, Mapping):
            if service.get("required", True):
                name = str(_first(service, "id", "name", "type", default=""))
                required[name] = service_endpoint.get(name, name)
        else:
            name = str(service)
            required[name] = service_endpoint.get(name, name)
    for name in sorted(required):
        if not name:
            continue
        endpoint_name = required[name]
        endpoint = endpoints.get(endpoint_name)
        failure_code = "report_channel_failed" if endpoint_name == "report" else ("broker_unreachable" if endpoint_name == "broker" else "endpoint_unreachable")
        reachable = False
        if endpoint:
            endpoint_path = Path(endpoint)
            if endpoint_name == "artifact":
                reachable = endpoint_path.is_dir() and os.access(endpoint_path, os.W_OK)
            elif endpoint_name == "report":
                reachable = endpoint_path.parent.is_dir() and os.access(endpoint_path.parent, os.W_OK)
            else:
                reachable = builder.probes.endpoint(endpoint)
        if not endpoint:
            builder.services[name] = {"healthy": False, "version": "", "detail": failure_code}
            builder.block("endpoint:" + name, failure_code, f"required {name} endpoint is not declared")
        elif not reachable:
            builder.services[name] = {"healthy": False, "version": "", "detail": failure_code}
            builder.block("endpoint:" + name, failure_code, f"required {name} endpoint is unreachable")
        else:
            builder.services[name] = {"healthy": True, "version": "", "detail": "transport reachable"}
            builder.ok("endpoint:" + name, f"required {name} endpoint is reachable")


def _check_credentials(builder: _AttestationBuilder) -> None:
    refs = _list(_first(builder.envelope, "credential_references", "credential_refs", "credentials", default=[]))
    environment = builder.probes.environment()
    for index, reference in enumerate(refs):
        required = True
        credential_id = f"credential-{index + 1}"
        if isinstance(reference, Mapping):
            credential_id = str(_first(reference, "id", "name", "capability", default=credential_id))
            required = bool(reference.get("required", not reference.get("optional", False)))
        status_value = builder.probes.credential(reference, environment)
        if status_value == "available":
            builder.ok("credential:" + credential_id, f"credential reference {credential_id} is available")
        elif status_value == "expired":
            builder.block("credential:" + credential_id, "credential_expired", f"credential reference {credential_id} is expired")
        elif status_value == "unverified" and not required:
            builder.warn("credential:" + credential_id, "credential_unverified", f"optional credential reference {credential_id} could not be verified")
        else:
            builder.block("credential:" + credential_id, "credential_unavailable", f"credential reference {credential_id} is unavailable")


def _capabilities(envelope: Mapping[str, Any]) -> set[str]:
    raw = _first(envelope, "capabilities", "capability_grant", "grants", default=[])
    if isinstance(raw, Mapping):
        return {str(name) for name, enabled in raw.items() if enabled is True or (isinstance(enabled, str) and enabled not in {"disabled", "false"})}
    return set(map(str, _list(raw)))


def _check_ltvm(builder: _AttestationBuilder) -> None:
    capabilities = _capabilities(builder.envelope)
    services = {str(_first(item, "id", "name", "type", default="")) if isinstance(item, Mapping) else str(item)
                for item in _list(_first(builder.profile, "host_services", "required_host_services", default=[]))}
    if not ({"start_ltvm", "run_vm_tests"} & capabilities or "ltvm" in services or "ltvm_bridge" in services):
        return
    path = builder.probes.which("ltvm")
    if not path:
        builder.services["ltvm"] = {"healthy": False, "version": "", "detail": "ltvm_unavailable"}
        builder.block("ltvm", "ltvm_unavailable", "LTVM capability is granted but ltvm is unavailable")
        return
    version = builder.probes.run([path, "--version"], 10)
    inventory = builder.probes.run([path, "list", "--json"], 15)
    if version.returncode or inventory.returncode:
        builder.services["ltvm"] = {"healthy": False, "version": "", "detail": "ltvm_unavailable"}
        builder.block("ltvm", "ltvm_unavailable", "LTVM health probe failed")
        return
    try:
        data = json.loads(inventory.stdout)
        vms = data.get("vms", data) if isinstance(data, Mapping) else data
        owner_supported = isinstance(vms, list) and all(isinstance(vm, Mapping) and "owner_id" in vm for vm in vms)
    except (ValueError, TypeError):
        owner_supported = False
    if not owner_supported:
        builder.services["ltvm"] = {"healthy": False, "version": _safe_version(version.stdout + version.stderr), "detail": "ltvm_owner_unsupported"}
        builder.block("ltvm_owner", "ltvm_owner_unsupported", "LTVM inventory does not expose owner_id attribution")
        return
    expected_owner = _first(builder.envelope, "ltvm_owner_id", "LTVM_OWNER_ID")
    actual_owner = builder.probes.environment().get("LTVM_OWNER_ID")
    if not expected_owner or actual_owner != str(expected_owner):
        builder.services["ltvm"] = {"healthy": False, "version": _safe_version(version.stdout + version.stderr), "detail": "ltvm_owner_mismatch"}
        builder.block("ltvm_owner", "ltvm_owner_mismatch", "LTVM owner identity is missing or does not match the run envelope")
    else:
        builder.services["ltvm"] = {"healthy": True, "version": _safe_version(version.stdout + version.stderr), "detail": "owner attribution available"}
        builder.ok("ltvm_owner", "LTVM health and owner attribution are available", version=_safe_version(version.stdout + version.stderr))


def doctor(
    profile: Mapping[str, Any] | Any,
    envelope: Mapping[str, Any] | Any,
    *,
    probes: DoctorProbes | None = None,
    envelope_path: Path | None = None,
) -> dict[str, Any]:
    """Run admission checks and return a fully redacted attestation mapping."""

    profile_mapping = as_mapping(profile)
    envelope_mapping = as_mapping(envelope)
    probes = probes or DoctorProbes()
    builder = _AttestationBuilder(profile_mapping, envelope_mapping, probes)
    _check_contract(builder)
    _check_platform(builder)
    _check_commands(builder)
    _check_checkout(builder, envelope_path)
    _check_paths(builder, envelope_path)
    _check_resources(builder, envelope_path)
    _check_endpoints(builder, envelope_path)
    _check_credentials(builder)
    _check_ltvm(builder)
    return builder.result()


def load_json(path: Path) -> dict[str, Any]:
    """Load a private contract JSON object with a precise, value-free error."""

    try:
        value = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError(f"cannot read contract file: {exc.__class__.__name__}") from None
    except json.JSONDecodeError:
        raise ValueError("contract file is not valid JSON") from None
    if not isinstance(value, Mapping):
        raise ValueError("contract JSON root must be an object")
    return dict(value)
