"""Exact-owner broker for bounded command execution inside LTVM guests.

The broker deliberately has no local process runner and no SSH implementation.
It can only hand a command to an injected transport which promises that the
operation is dispatched inside the named guest, rechecks the expected owner at
dispatch, has no host fallback, and exposes no service credentials.  Command
content is intentionally open-ended once an engineering-run capability has
been granted; the security boundary is the owned guest, not an executable
allowlist.

Execution manifests remain useful immutable plans and evidence, but are not an
allowlist.  Callers may also submit ad-hoc argv or guest-side command text.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from engineering_state import ExecutionManifest
from ltvm_resources import LTVMInventory, owner_id_for_session


GUEST_EXECUTION_CAPABILITY = "execute_ltvm_guest_commands"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_LTVM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CAPACITY_CATEGORIES = frozenset(
    {"memory", "disk", "cpu", "address", "slot", "quota", "other"}
)


def _identifier(label: str, value: object) -> str:
    result = str(value).strip()
    if not _IDENTIFIER.fullmatch(result):
        raise ValueError(f"{label} is not a safe identifier")
    return result


def _revision(value: object) -> str:
    result = str(value).strip()
    if not _REVISION.fullmatch(result):
        raise ValueError("revision_sha must be a 40-64 digit lowercase hex digest")
    return result


def _ltvm_name(label: str, value: object) -> str:
    result = str(value).strip()
    if not _LTVM_NAME.fullmatch(result):
        raise ValueError(f"{label} is not a safe LTVM name")
    return result


def _bounded_text(value: object, limit: int) -> str:
    text = str(value).replace("\x00", "")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class GuestExecutionError(RuntimeError):
    """Base class for guest-execution boundary failures."""


class GuestCapacityExhausted(GuestExecutionError):
    """Typed capacity failure reported by the guest transport.

    Free-form stderr is never interpreted as capacity exhaustion.  A transport
    must raise this explicit type after classifying its machine-readable error.
    """

    def __init__(
        self,
        category: str,
        evidence: str,
        *,
        requested: Mapping[str, int] | None = None,
    ) -> None:
        if category not in _CAPACITY_CATEGORIES:
            raise ValueError("unknown capacity category")
        normalized: dict[str, int] = {}
        for key, value in (requested or {}).items():
            if not _ENVIRONMENT_KEY.fullmatch(str(key)):
                raise ValueError("invalid requested-resource key")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("requested-resource values must be nonnegative integers")
            normalized[str(key)] = value
        self.category = category
        self.evidence = _bounded_text(evidence, 2_000)
        self.requested = normalized
        super().__init__(self.evidence or f"{category} capacity exhausted")


class GuestTransportCancelled(GuestExecutionError):
    """The transport safely stopped an active guest operation."""


@dataclass(frozen=True)
class GuestTransportBoundary:
    """Properties a transport must enforce below the broker's trust boundary."""

    protocol: str = "ltvm-owner-checked-guest-exec/v1"
    guest_exec_only: bool = True
    host_fallback: bool = False
    owner_checked_at_dispatch: bool = True
    argv_supported: bool = True
    text_supported: bool = True
    cancellation_supported: bool = True
    output_limit_enforced: bool = True
    service_credentials_absent: bool = True
    gerrit_writes_blocked: bool = True

    def validate(self, command_mode: str) -> None:
        if self.protocol != "ltvm-owner-checked-guest-exec/v1":
            raise GuestExecutionError("guest transport protocol is not supported")
        required = (
            self.guest_exec_only,
            not self.host_fallback,
            self.owner_checked_at_dispatch,
            self.cancellation_supported,
            self.output_limit_enforced,
            self.service_credentials_absent,
            self.gerrit_writes_blocked,
        )
        if not all(required):
            raise GuestExecutionError("guest transport does not enforce the required boundary")
        if command_mode == "argv" and not self.argv_supported:
            raise GuestExecutionError("guest transport does not support argv execution")
        if command_mode == "text" and not self.text_supported:
            raise GuestExecutionError("guest transport does not support guest command text")


@dataclass(frozen=True)
class EngineeringExecutionAuthorization:
    """An exact, active capability assertion supplied by durable run state."""

    session_id: str
    run_id: str
    revision_sha: str
    capability: str = GUEST_EXECUTION_CAPABILITY
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _identifier("session_id", self.session_id))
        object.__setattr__(self, "run_id", _identifier("run_id", self.run_id))
        object.__setattr__(self, "revision_sha", _revision(self.revision_sha))
        object.__setattr__(self, "capability", _identifier("capability", self.capability))


class EngineeringRunAuthorizer(Protocol):
    def authorization_for(
        self, *, session_id: str, run_id: str, revision_sha: str
    ) -> EngineeringExecutionAuthorization | None:
        """Return current durable authorization, or ``None`` to deny."""


@dataclass(frozen=True)
class GuestTarget:
    """One VM, optionally bound to an authoritative exact-owner cluster."""

    vm_name: str
    cluster_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vm_name", _ltvm_name("vm_name", self.vm_name))
        if self.cluster_name is not None:
            object.__setattr__(
                self, "cluster_name", _ltvm_name("cluster_name", self.cluster_name)
            )

    def to_dict(self) -> dict[str, Any]:
        return {"vm_name": self.vm_name, "cluster_name": self.cluster_name}


@dataclass(frozen=True)
class GuestCommand:
    """An arbitrary command whose interpretation is confined to the guest.

    Exactly one of ``argv`` and ``text`` is present.  Text is interpreted by
    the guest transport; it is never handed to a shell on the Patch Watcher
    host.  Size limits protect controller memory and audit records, not command
    semantics.
    """

    command_id: str
    argv: tuple[str, ...] | None = None
    text: str | None = None
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int = 3_600
    expected_exit_codes: tuple[int, ...] = (0,)
    label: str = ""
    evidence_role: str = "other"

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", _identifier("command_id", self.command_id))
        if (self.argv is None) == (self.text is None):
            raise ValueError("exactly one of argv and text is required")
        if self.argv is not None:
            if isinstance(self.argv, (str, bytes)) or not isinstance(self.argv, Sequence):
                raise ValueError("argv must be an array")
            argv = tuple(self.argv)
            if not argv or len(argv) > 256 or any(
                not isinstance(item, str)
                or not item
                or "\x00" in item
                or len(item.encode("utf-8")) > 16_384
                for item in argv
            ):
                raise ValueError("argv is empty, invalid, or oversized")
            if sum(len(item.encode("utf-8")) for item in argv) > 131_072:
                raise ValueError("argv is oversized")
            object.__setattr__(self, "argv", argv)
        else:
            if not isinstance(self.text, str) or not self.text or "\x00" in self.text:
                raise ValueError("guest command text is empty or invalid")
            if len(self.text.encode("utf-8")) > 131_072:
                raise ValueError("guest command text is oversized")
        if not isinstance(self.cwd, str) or not self.cwd or "\x00" in self.cwd:
            raise ValueError("guest cwd is empty or invalid")
        if len(self.cwd.encode("utf-8")) > 4_096:
            raise ValueError("guest cwd is oversized")
        raw_env: Any = self.env.items() if isinstance(self.env, Mapping) else self.env
        if isinstance(raw_env, (str, bytes)) or not isinstance(raw_env, Sequence):
            raw_env = tuple(raw_env) if not isinstance(raw_env, (str, bytes)) else raw_env
        if isinstance(raw_env, (str, bytes)) or not isinstance(raw_env, Sequence):
            raise ValueError("env must be key/value pairs")
        normalized_env: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in raw_env:
            if (
                isinstance(item, (str, bytes))
                or not isinstance(item, Sequence)
                or len(item) != 2
            ):
                raise ValueError("env must contain key/value pairs")
            key, value = item
            if not isinstance(key, str) or not _ENVIRONMENT_KEY.fullmatch(key) or key in seen:
                raise ValueError("environment key is invalid or duplicated")
            if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > 16_384:
                raise ValueError("environment value is invalid or oversized")
            seen.add(key)
            normalized_env.append((key, value))
        object.__setattr__(self, "env", tuple(sorted(normalized_env)))
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 86_400:
            raise ValueError("timeout_seconds must be between 1 and 86400")
        if isinstance(self.expected_exit_codes, (str, bytes)) or not isinstance(
            self.expected_exit_codes, Sequence
        ):
            raise ValueError("expected_exit_codes must be an array")
        codes = tuple(self.expected_exit_codes)
        if not codes or any(
            isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255
            for code in codes
        ):
            raise ValueError("expected_exit_codes contains invalid values")
        object.__setattr__(self, "expected_exit_codes", tuple(sorted(set(codes))))
        if "\x00" in self.label or len(self.label.encode("utf-8")) > 500:
            raise ValueError("label is invalid or oversized")
        role = str(self.evidence_role).strip().lower()
        if role not in {"test", "build", "diagnostic", "other"}:
            raise ValueError("evidence_role must be test, build, diagnostic, or other")
        object.__setattr__(self, "evidence_role", role)

    @property
    def mode(self) -> str:
        return "argv" if self.argv is not None else "text"

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "command_id": self.command_id,
            "mode": self.mode,
            "cwd": self.cwd,
            "env": dict(self.env),
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_codes": list(self.expected_exit_codes),
            "label": self.label,
        }
        if include_content:
            value["argv" if self.argv is not None else "text"] = (
                list(self.argv) if self.argv is not None else self.text
            )
        # Keep existing command identities stable when no explicit role exists.
        if self.evidence_role != "other":
            value["evidence_role"] = self.evidence_role
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode()).hexdigest()


@dataclass(frozen=True)
class GuestExecutionRequest:
    request_id: str
    session_id: str
    run_id: str
    revision_sha: str
    target: GuestTarget
    commands: tuple[GuestCommand, ...]
    source_manifest_id: str | None = None
    source_manifest_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier("request_id", self.request_id))
        object.__setattr__(self, "session_id", _identifier("session_id", self.session_id))
        object.__setattr__(self, "run_id", _identifier("run_id", self.run_id))
        object.__setattr__(self, "revision_sha", _revision(self.revision_sha))
        if not isinstance(self.target, GuestTarget):
            raise ValueError("target must be a GuestTarget")
        commands = tuple(self.commands)
        if not commands or len(commands) > 256 or not all(
            isinstance(command, GuestCommand) for command in commands
        ):
            raise ValueError("commands must contain 1-256 GuestCommand objects")
        if len({command.command_id for command in commands}) != len(commands):
            raise ValueError("command IDs must be unique")
        object.__setattr__(self, "commands", commands)
        if self.source_manifest_id is not None:
            object.__setattr__(
                self,
                "source_manifest_id",
                _identifier("source_manifest_id", self.source_manifest_id),
            )
        if self.source_manifest_digest is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.source_manifest_digest
        ):
            raise ValueError("source_manifest_digest must be a SHA-256 digest")

    @classmethod
    def from_manifest(
        cls,
        *,
        request_id: str,
        session_id: str,
        target: GuestTarget,
        manifest: ExecutionManifest,
    ) -> "GuestExecutionRequest":
        return cls(
            request_id=request_id,
            session_id=session_id,
            run_id=manifest.run_id,
            revision_sha=manifest.revision_sha,
            target=target,
            commands=tuple(
                GuestCommand(
                    command_id=command.step_id,
                    argv=command.argv,
                    cwd=command.cwd,
                    env=command.env,
                    timeout_seconds=command.timeout_seconds,
                    expected_exit_codes=command.expected_exit_codes,
                    label=command.label,
                    evidence_role=command.evidence_role,
                )
                for command in manifest.commands
            ),
            source_manifest_id=manifest.manifest_id,
            source_manifest_digest=manifest.digest,
        )

    @property
    def digest(self) -> str:
        value = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "revision_sha": self.revision_sha,
            "target": self.target.to_dict(),
            "commands": [command.to_dict() for command in self.commands],
            "source_manifest_id": self.source_manifest_id,
            "source_manifest_digest": self.source_manifest_digest,
        }
        return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class GuestTransportResult:
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    cancelled: bool = False
    stdout_observed_bytes: int | None = None
    stderr_observed_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool)
            or not isinstance(self.exit_code, int)
            or not 0 <= self.exit_code <= 255
        ):
            raise ValueError("exit_code is invalid")
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise ValueError("transport output must be bytes")
        for observed, actual in (
            (self.stdout_observed_bytes, len(self.stdout)),
            (self.stderr_observed_bytes, len(self.stderr)),
        ):
            if observed is not None and (
                isinstance(observed, bool)
                or not isinstance(observed, int)
                or observed < actual
            ):
                raise ValueError("observed output size is invalid")


class GuestExecutionTransport(Protocol):
    def boundary(self) -> GuestTransportBoundary:
        """Describe enforced transport guarantees."""

    def execute_guest(
        self,
        *,
        target: GuestTarget,
        expected_owner_id: str,
        command: GuestCommand,
        timeout_seconds: int,
        max_output_bytes: int,
        cancelled: Callable[[], bool],
    ) -> GuestTransportResult:
        """Execute only in the guest, with an atomic owner check at dispatch."""


@dataclass(frozen=True)
class GuestExecutionPolicy:
    max_commands: int = 64
    max_step_seconds: int = 3_600
    max_total_seconds: int = 7_200
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_commands", self.max_commands, 256),
            ("max_step_seconds", self.max_step_seconds, 86_400),
            ("max_total_seconds", self.max_total_seconds, 172_800),
            ("max_output_bytes", self.max_output_bytes, 16 * 1_048_576),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside its safe bound")


@dataclass(frozen=True)
class OutputArtifact:
    artifact_id: str
    stream: str
    sha256: str
    captured_bytes: int
    observed_bytes: int
    truncated: bool
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "stream": self.stream,
            "sha256": self.sha256,
            "captured_bytes": self.captured_bytes,
            "observed_bytes": self.observed_bytes,
            "truncated": self.truncated,
            "media_type": "text/plain; charset=utf-8",
            "text": self.text,
        }


@dataclass(frozen=True)
class GuestCommandAudit:
    command_id: str
    command_digest: str
    mode: str
    status: str
    code: str
    exit_code: int | None
    timeout_seconds: int
    started_at: datetime
    finished_at: datetime
    artifacts: tuple[OutputArtifact, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_digest": self.command_digest,
            "mode": self.mode,
            "status": self.status,
            "code": self.code,
            "exit_code": self.exit_code,
            "timeout_seconds": self.timeout_seconds,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class CapacityExhaustion:
    category: str
    operation: str
    requested: Mapping[str, int]
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "operation": self.operation,
            "requested": dict(self.requested),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class GuestExecutionResult:
    request_id: str
    request_digest: str
    run_id: str
    session_id: str
    expected_owner_id: str
    target: GuestTarget
    status: str
    code: str
    detail: str
    commands: tuple[GuestCommandAudit, ...] = ()
    capacity: CapacityExhaustion | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "patch-watcher-ltvm-guest-exec/v1",
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "expected_owner_id": self.expected_owner_id,
            "target": self.target.to_dict(),
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "commands": [command.to_dict() for command in self.commands],
            "capacity": self.capacity.to_dict() if self.capacity else None,
        }


class LTVMGuestExecutionBroker:
    """Authorize and dispatch bounded work only to an exact-owner LTVM guest."""

    def __init__(
        self,
        *,
        inventory_provider: Any,
        authorizer: EngineeringRunAuthorizer,
        transport: GuestExecutionTransport,
        policy: GuestExecutionPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inventory_provider = inventory_provider
        self.authorizer = authorizer
        self.transport = transport
        self.policy = policy or GuestExecutionPolicy()
        self.clock = clock
        self.monotonic = monotonic

    def execute(
        self,
        request: GuestExecutionRequest,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> GuestExecutionResult:
        if not isinstance(request, GuestExecutionRequest):
            raise TypeError("request must be a GuestExecutionRequest")
        owner_id = owner_id_for_session(request.session_id)
        audits: list[GuestCommandAudit] = []
        if len(request.commands) > self.policy.max_commands:
            return self._result(
                request, owner_id, "blocked", "command_limit", "command count exceeds policy"
            )
        started = self.monotonic()
        for command in request.commands:
            if cancelled():
                return self._result(
                    request, owner_id, "cancelled", "cancelled", "execution was cancelled", audits
                )
            failure = self._preflight_operation(request, command, owner_id)
            if failure is not None:
                status, code, detail = failure
                return self._result(request, owner_id, status, code, detail, audits)
            elapsed = max(0.0, self.monotonic() - started)
            remaining = int(self.policy.max_total_seconds - elapsed)
            if remaining <= 0:
                return self._result(
                    request,
                    owner_id,
                    "failed",
                    "total_timeout",
                    "total execution deadline was reached",
                    audits,
                )
            timeout = min(command.timeout_seconds, self.policy.max_step_seconds, remaining)
            step_started = self.clock()
            try:
                transport_result = self.transport.execute_guest(
                    target=request.target,
                    expected_owner_id=owner_id,
                    command=command,
                    timeout_seconds=timeout,
                    max_output_bytes=self.policy.max_output_bytes,
                    cancelled=cancelled,
                )
            except GuestTransportCancelled:
                audit = self._audit(
                    request, command, "cancelled", "cancelled", None, timeout, step_started, ()
                )
                audits.append(audit)
                return self._result(
                    request, owner_id, "cancelled", "cancelled", "guest command was cancelled", audits
                )
            except GuestCapacityExhausted as exc:
                audit = self._audit(
                    request,
                    command,
                    "resource_exhausted",
                    "ltvm_resource_exhausted",
                    None,
                    timeout,
                    step_started,
                    (),
                )
                audits.append(audit)
                capacity = CapacityExhaustion(
                    exc.category, "guest_command", dict(exc.requested), exc.evidence
                )
                return self._result(
                    request,
                    owner_id,
                    "resource_exhausted",
                    "ltvm_resource_exhausted",
                    "guest transport reported resource exhaustion",
                    audits,
                    capacity,
                )
            except Exception as exc:
                audit = self._audit(
                    request, command, "failed", "transport_error", None, timeout, step_started, ()
                )
                audits.append(audit)
                return self._result(
                    request,
                    owner_id,
                    "failed",
                    "transport_error",
                    _bounded_text(exc, 1_000),
                    audits,
                )
            if not isinstance(transport_result, GuestTransportResult):
                return self._result(
                    request,
                    owner_id,
                    "failed",
                    "invalid_transport_result",
                    "guest transport returned an invalid result",
                    audits,
                )
            artifacts = self._artifacts(request, command, transport_result)
            if transport_result.cancelled or cancelled():
                step_status, step_code = "cancelled", "cancelled"
            elif transport_result.timed_out:
                step_status, step_code = "failed", "step_timeout"
            elif transport_result.exit_code in command.expected_exit_codes:
                step_status, step_code = "succeeded", "expected_exit"
            else:
                step_status, step_code = "failed", "unexpected_exit"
            audits.append(
                self._audit(
                    request,
                    command,
                    step_status,
                    step_code,
                    transport_result.exit_code,
                    timeout,
                    step_started,
                    artifacts,
                )
            )
            if step_status != "succeeded":
                return self._result(
                    request,
                    owner_id,
                    step_status,
                    step_code,
                    "guest command did not complete successfully",
                    audits,
                )
        return self._result(
            request, owner_id, "succeeded", "completed", "all guest commands completed", audits
        )

    def _preflight_operation(
        self, request: GuestExecutionRequest, command: GuestCommand, owner_id: str
    ) -> tuple[str, str, str] | None:
        authorization = self.authorizer.authorization_for(
            session_id=request.session_id,
            run_id=request.run_id,
            revision_sha=request.revision_sha,
        )
        if authorization is None:
            return "blocked", "capability_denied", "engineering guest execution is not authorized"
        if (
            not isinstance(authorization, EngineeringExecutionAuthorization)
            or not authorization.active
            or authorization.session_id != request.session_id
            or authorization.run_id != request.run_id
            or authorization.revision_sha != request.revision_sha
            or authorization.capability != GUEST_EXECUTION_CAPABILITY
        ):
            return "blocked", "capability_denied", "engineering authorization is not exact and active"
        try:
            boundary = self.transport.boundary()
            if not isinstance(boundary, GuestTransportBoundary):
                raise GuestExecutionError("guest transport returned no valid boundary")
            boundary.validate(command.mode)
        except Exception as exc:
            return "blocked", "unsafe_transport", _bounded_text(exc, 1_000)
        try:
            inventory = self.inventory_provider.inventory()
        except Exception as exc:
            return "blocked", "inventory_unavailable", _bounded_text(exc, 1_000)
        if not isinstance(inventory, LTVMInventory):
            return "blocked", "inventory_unavailable", "inventory provider returned invalid data"
        return self._validate_target(inventory, request.target, owner_id)

    @staticmethod
    def _validate_target(
        inventory: LTVMInventory, target: GuestTarget, owner_id: str
    ) -> tuple[str, str, str] | None:
        matches = inventory.named_vms(target.vm_name)
        if len(matches) != 1:
            return "blocked", "ambiguous_target", "target VM is missing or ambiguous"
        vm = matches[0]
        if vm.owner_id != owner_id:
            return "blocked", "owner_mismatch", "target VM does not have the exact session owner"
        if vm.state != "running":
            return "blocked", "target_not_running", "target VM is not running"
        if target.cluster_name is None:
            return None
        if not inventory.clusters_authoritative:
            return "blocked", "cluster_inventory_unavailable", "cluster inventory is not authoritative"
        clusters = inventory.named_clusters(target.cluster_name)
        if len(clusters) != 1:
            return "blocked", "ambiguous_cluster", "target cluster is missing or ambiguous"
        cluster = clusters[0]
        if cluster.owner_id != owner_id:
            return "blocked", "owner_mismatch", "target cluster does not have the exact session owner"
        if target.vm_name not in cluster.member_names:
            return "blocked", "cluster_member_mismatch", "target VM is not a declared cluster member"
        if vm.cluster_name not in (None, target.cluster_name):
            return "blocked", "cluster_member_mismatch", "target VM names a different cluster"
        if not cluster.member_names:
            return "blocked", "cluster_member_mismatch", "target cluster has no members"
        for member_name in cluster.member_names:
            members = inventory.named_vms(member_name)
            if len(members) != 1 or members[0].owner_id != owner_id:
                return (
                    "blocked",
                    "partial_cluster",
                    "not every cluster member has the exact session owner",
                )
        return None

    def _artifacts(
        self,
        request: GuestExecutionRequest,
        command: GuestCommand,
        result: GuestTransportResult,
    ) -> tuple[OutputArtifact, ...]:
        remaining = self.policy.max_output_bytes
        artifacts: list[OutputArtifact] = []
        for stream, raw, observed in (
            ("stdout", result.stdout, result.stdout_observed_bytes),
            ("stderr", result.stderr, result.stderr_observed_bytes),
        ):
            captured = raw[:remaining]
            remaining -= len(captured)
            observed_size = max(len(raw), observed if observed is not None else len(raw))
            artifacts.append(
                OutputArtifact(
                    artifact_id=f"{request.request_id}:{command.command_id}:{stream}",
                    stream=stream,
                    sha256=hashlib.sha256(captured).hexdigest(),
                    captured_bytes=len(captured),
                    observed_bytes=observed_size,
                    truncated=observed_size > len(captured),
                    text=captured.decode("utf-8", "replace"),
                )
            )
        return tuple(artifacts)

    def _audit(
        self,
        request: GuestExecutionRequest,
        command: GuestCommand,
        status: str,
        code: str,
        exit_code: int | None,
        timeout: int,
        started_at: datetime,
        artifacts: tuple[OutputArtifact, ...],
    ) -> GuestCommandAudit:
        return GuestCommandAudit(
            command_id=command.command_id,
            command_digest=command.digest,
            mode=command.mode,
            status=status,
            code=code,
            exit_code=exit_code,
            timeout_seconds=timeout,
            started_at=started_at,
            finished_at=self.clock(),
            artifacts=artifacts,
        )

    @staticmethod
    def _result(
        request: GuestExecutionRequest,
        owner_id: str,
        status: str,
        code: str,
        detail: str,
        audits: Sequence[GuestCommandAudit] = (),
        capacity: CapacityExhaustion | None = None,
    ) -> GuestExecutionResult:
        return GuestExecutionResult(
            request_id=request.request_id,
            request_digest=request.digest,
            run_id=request.run_id,
            session_id=request.session_id,
            expected_owner_id=owner_id,
            target=request.target,
            status=status,
            code=code,
            detail=_bounded_text(detail, 1_000),
            commands=tuple(audits),
            capacity=capacity,
        )


__all__ = [
    "CapacityExhaustion",
    "EngineeringExecutionAuthorization",
    "EngineeringRunAuthorizer",
    "GUEST_EXECUTION_CAPABILITY",
    "GuestCapacityExhausted",
    "GuestCommand",
    "GuestCommandAudit",
    "GuestExecutionPolicy",
    "GuestExecutionRequest",
    "GuestExecutionResult",
    "GuestExecutionTransport",
    "GuestTarget",
    "GuestTransportBoundary",
    "GuestTransportCancelled",
    "GuestTransportResult",
    "LTVMGuestExecutionBroker",
    "OutputArtifact",
]
