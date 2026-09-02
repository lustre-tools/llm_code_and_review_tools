"""Safe LTVM ownership, cleanup, target, and capacity primitives.

This module is deliberately independent of the web application and runner.
It consumes LTVM's machine-readable inventory, plans cleanup only when a
resource still carries the exact durable session owner, and keeps the command
adapter small enough to audit.  The adapter never invokes a shell.

Configured guest memory is capacity requested from a guest.  Host RSS is an
observation of the QEMU process.  They are intentionally separate fields and
are never added together here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PATCH_WATCHER_OWNER_PREFIX = "patch-watcher:"
MAX_OWNER_ID_LENGTH = 255
_MIB = 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CAPACITY_TERMS = (
    "insufficient memory",
    "not enough memory",
    "out of memory",
    "insufficient disk",
    "no space left",
    "insufficient cpu",
    "capacity",
    "address exhausted",
    "no addresses",
    "slot exhausted",
    "resource limit",
    "resource exhausted",
)


Runner = Callable[..., subprocess.CompletedProcess]


class LTVMInventoryError(ValueError):
    """Machine-readable LTVM inventory was unavailable or invalid."""


class LTVMCommandError(RuntimeError):
    """A bounded LTVM adapter operation failed."""


class UnsafeCleanupError(LTVMCommandError):
    """Destruction was refused because exact ownership was not provable."""


class ResourceExhaustionValidationError(ValueError):
    """A worker's resource-exhaustion report was not trustworthy enough."""


def _required_text(label: str, value: Any, *, limit: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    result = value.strip()
    if len(result) > limit or any(character in result for character in "\x00\r\n"):
        raise ValueError(f"{label} is invalid")
    return result


def _safe_name(label: str, value: Any) -> str:
    name = _required_text(label, value, limit=128)
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"{label} is not a safe LTVM name")
    return name


def owner_id_for_session(session_id: str) -> str:
    """Return the one durable owner value Patch Watcher assigns a session."""

    session = _required_text("session_id", session_id, limit=MAX_OWNER_ID_LENGTH)
    owner_id = PATCH_WATCHER_OWNER_PREFIX + session
    if len(owner_id) > MAX_OWNER_ID_LENGTH:
        raise ValueError("session_id is too long for an LTVM owner ID")
    return owner_id


def session_id_from_owner(owner_id: str | None) -> str | None:
    if not owner_id or not owner_id.startswith(PATCH_WATCHER_OWNER_PREFIX):
        return None
    session_id = owner_id[len(PATCH_WATCHER_OWNER_PREFIX) :]
    return session_id or None


def _optional_owner(value: Any) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        return None, "owner_id is not a string"
    if len(value) > MAX_OWNER_ID_LENGTH or any(ch in value for ch in "\x00\r\n"):
        return None, "owner_id is invalid"
    return value, None


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        return int(value)
    return None


def _configured_memory(row: Mapping[str, Any]) -> int | None:
    for key in ("configured_guest_memory_bytes", "memory_bytes"):
        if key in row:
            return _nonnegative_integer(row[key])
    for key in ("configured_guest_memory_mb", "memory_mb", "mem_mb", "mem"):
        if key in row:
            value = _nonnegative_integer(row[key])
            return value * _MIB if value is not None else None
    return None


@dataclass(frozen=True)
class InventoryIssue:
    code: str
    resource: str | None
    detail: str


@dataclass(frozen=True)
class VMInventoryRecord:
    name: str
    owner_id: str | None
    state: str
    configured_guest_memory_bytes: int | None
    host_rss_bytes: int | None = None
    vcpus: int | None = None
    cluster_name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def session_id(self) -> str | None:
        return session_id_from_owner(self.owner_id)


@dataclass(frozen=True)
class ClusterInventoryRecord:
    name: str
    owner_id: str | None
    member_names: tuple[str, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def session_id(self) -> str | None:
        return session_id_from_owner(self.owner_id)


@dataclass(frozen=True)
class LTVMInventory:
    vms: tuple[VMInventoryRecord, ...]
    clusters: tuple[ClusterInventoryRecord, ...] = ()
    issues: tuple[InventoryIssue, ...] = ()
    clusters_authoritative: bool = False

    def named_vms(self, name: str) -> tuple[VMInventoryRecord, ...]:
        return tuple(vm for vm in self.vms if vm.name == name)

    def named_clusters(self, name: str) -> tuple[ClusterInventoryRecord, ...]:
        return tuple(cluster for cluster in self.clusters if cluster.name == name)

    def vms_owned_by(self, owner_id: str) -> tuple[VMInventoryRecord, ...]:
        return tuple(vm for vm in self.vms if vm.owner_id == owner_id)

    @property
    def configured_guest_memory_bytes(self) -> int | None:
        values = [vm.configured_guest_memory_bytes for vm in self.vms]
        if any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    @property
    def known_host_rss_bytes(self) -> int:
        return sum(vm.host_rss_bytes or 0 for vm in self.vms)

    @classmethod
    def from_json(cls, document: str | bytes | Mapping[str, Any]) -> "LTVMInventory":
        if isinstance(document, bytes):
            try:
                document = document.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LTVMInventoryError("LTVM inventory was not UTF-8") from exc
        if isinstance(document, str):
            try:
                payload: Any = json.loads(document)
            except json.JSONDecodeError as exc:
                raise LTVMInventoryError("LTVM inventory was not valid JSON") from exc
        else:
            payload = document
        while isinstance(payload, Mapping) and isinstance(
            payload.get("data"), Mapping
        ):
            payload = payload["data"]
        if not isinstance(payload, Mapping) or not isinstance(payload.get("vms"), list):
            raise LTVMInventoryError("LTVM inventory has no vms list")

        issues: list[InventoryIssue] = []
        vms: list[VMInventoryRecord] = []
        for index, raw in enumerate(payload["vms"]):
            if not isinstance(raw, Mapping):
                issues.append(InventoryIssue("invalid_vm", None, f"VM row {index} is not an object"))
                continue
            try:
                name = _safe_name("VM name", raw.get("name"))
            except ValueError as exc:
                issues.append(InventoryIssue("invalid_vm_name", None, str(exc)))
                continue
            owner_id, owner_problem = _optional_owner(raw.get("owner_id"))
            if owner_problem:
                issues.append(InventoryIssue("invalid_owner_id", name, owner_problem))
            state_value = raw.get("status", raw.get("state", "unknown"))
            state = state_value.strip().casefold() if isinstance(state_value, str) else "unknown"
            host_rss = _nonnegative_integer(raw.get("host_rss_bytes"))
            vcpus = _nonnegative_integer(raw.get("vcpus"))
            cluster_raw = raw.get("cluster_name", raw.get("cluster"))
            cluster_name = None
            if cluster_raw not in (None, ""):
                try:
                    cluster_name = _safe_name("cluster name", cluster_raw)
                except ValueError as exc:
                    issues.append(InventoryIssue("invalid_cluster_name", name, str(exc)))
            vms.append(
                VMInventoryRecord(
                    name=name,
                    owner_id=owner_id,
                    state=state,
                    configured_guest_memory_bytes=_configured_memory(raw),
                    host_rss_bytes=host_rss,
                    vcpus=vcpus,
                    cluster_name=cluster_name,
                    raw=dict(raw),
                )
            )

        clusters: list[ClusterInventoryRecord] = []
        clusters_authoritative = isinstance(payload.get("clusters"), list)
        cluster_rows = payload.get("clusters", [])
        if cluster_rows is not None and not isinstance(cluster_rows, list):
            issues.append(InventoryIssue("invalid_clusters", None, "clusters is not a list"))
            cluster_rows = []
        for index, raw in enumerate(cluster_rows):
            if not isinstance(raw, Mapping):
                issues.append(InventoryIssue("invalid_cluster", None, f"cluster row {index} is not an object"))
                continue
            try:
                name = _safe_name("cluster name", raw.get("name"))
            except ValueError as exc:
                issues.append(InventoryIssue("invalid_cluster_name", None, str(exc)))
                continue
            owner_id, owner_problem = _optional_owner(raw.get("owner_id"))
            if owner_problem:
                issues.append(InventoryIssue("invalid_owner_id", name, owner_problem))
            raw_members = raw.get("member_names", raw.get("members", raw.get("nodes", [])))
            if not isinstance(raw_members, list):
                issues.append(InventoryIssue("invalid_cluster_members", name, "members is not a list"))
                raw_members = []
            members: list[str] = []
            for member in raw_members:
                if isinstance(member, Mapping):
                    member = member.get("name")
                try:
                    members.append(_safe_name("cluster member", member))
                except ValueError as exc:
                    issues.append(InventoryIssue("invalid_cluster_member", name, str(exc)))
            clusters.append(
                ClusterInventoryRecord(name, owner_id, tuple(members), dict(raw))
            )

        for name in {vm.name for vm in vms}:
            if sum(vm.name == name for vm in vms) > 1:
                issues.append(InventoryIssue("duplicate_vm_name", name, "VM name is ambiguous"))
        for name in {cluster.name for cluster in clusters}:
            if sum(cluster.name == name for cluster in clusters) > 1:
                issues.append(InventoryIssue("duplicate_cluster_name", name, "cluster name is ambiguous"))
        return cls(
            tuple(vms), tuple(clusters), tuple(issues), clusters_authoritative
        )


@dataclass(frozen=True)
class SessionResourceRecord:
    resource_type: str
    name: str
    owner_id: str
    member_names: tuple[str, ...] = ()
    lifecycle_state: str = "creating"

    def __post_init__(self) -> None:
        if self.resource_type not in {"vm", "cluster"}:
            raise ValueError("resource_type must be vm or cluster")
        _safe_name("resource name", self.name)
        _required_text("owner_id", self.owner_id, limit=MAX_OWNER_ID_LENGTH)
        for member in self.member_names:
            _safe_name("cluster member", member)


@dataclass(frozen=True)
class ReconciledResource:
    resource_type: str
    name: str
    owner_id: str | None
    lifecycle_state: str
    discovered: bool
    detail: str | None = None


@dataclass(frozen=True)
class CleanupAction:
    resource_type: str
    name: str
    owner_id: str
    member_names: tuple[str, ...] = ()

    @property
    def argv(self) -> tuple[str, ...]:
        if self.resource_type == "vm":
            return ("ltvm", "destroy", self.name, "--json")
        # Current LTVM cluster destruction requires root.  ``-n`` preserves
        # the controller's non-interactive contract and fails closed when the
        # host privilege boundary has not been configured.
        return ("sudo", "-n", "ltvm", "cluster", "--json", "destroy", self.name)


@dataclass(frozen=True)
class ReconciliationResult:
    expected_owner_id: str
    resources: tuple[ReconciledResource, ...]
    cleanup_actions: tuple[CleanupAction, ...]
    issues: tuple[InventoryIssue, ...]


def reconcile_session_resources(
    session_id: str,
    inventory: LTVMInventory,
    *,
    recorded: Sequence[SessionResourceRecord] = (),
    cleanup_requested: bool = False,
) -> ReconciliationResult:
    """Associate and optionally plan cleanup for exactly one session owner.

    Newly discovered exact-owner VMs are adopted as *observations* so partial
    cluster creation can be cleaned.  Missing, malformed, duplicate, unowned,
    and differently owned entries never become cleanup actions.
    """

    expected = owner_id_for_session(session_id)
    issues = list(inventory.issues)
    resources: list[ReconciledResource] = []
    actions: list[CleanupAction] = []
    recorded_by_key = {(item.resource_type, item.name): item for item in recorded}
    for item in recorded:
        if item.owner_id != expected:
            issues.append(
                InventoryIssue(
                    "recorded_owner_mismatch",
                    item.name,
                    "record does not carry the session's exact owner ID",
                )
            )

    protected_members: set[str] = set()
    for cluster in inventory.clusters:
        matches = inventory.named_clusters(cluster.name)
        if len(matches) != 1:
            continue
        known = recorded_by_key.get(("cluster", cluster.name))
        if cluster.owner_id != expected:
            if known is not None:
                issues.append(InventoryIssue("owner_mismatch", cluster.name, "cluster ownership is missing or different"))
                resources.append(ReconciledResource("cluster", cluster.name, cluster.owner_id, "ownership_ambiguous", True))
            continue
        member_names = cluster.member_names or (known.member_names if known else ())
        member_rows = [inventory.named_vms(name) for name in member_names]
        complete = bool(member_names) and all(
            len(rows) == 1 and rows[0].owner_id == expected for rows in member_rows
        )
        state = "cleanup_pending" if cleanup_requested else "active"
        if not complete:
            state = "orphaned"
            issues.append(InventoryIssue("partial_cluster", cluster.name, "cluster members are missing, ambiguous, or differently owned"))
        resources.append(ReconciledResource("cluster", cluster.name, expected, state, True))
        if cleanup_requested and complete:
            actions.append(CleanupAction("cluster", cluster.name, expected, member_names))
            protected_members.update(member_names)

    for vm in inventory.vms:
        matches = inventory.named_vms(vm.name)
        if len(matches) != 1:
            continue
        known = recorded_by_key.get(("vm", vm.name))
        if vm.owner_id != expected:
            if known is not None:
                issues.append(InventoryIssue("owner_mismatch", vm.name, "VM ownership is missing or different"))
                resources.append(ReconciledResource("vm", vm.name, vm.owner_id, "ownership_ambiguous", True))
            continue
        state = "cleanup_pending" if cleanup_requested else "active"
        if known is None:
            state = "orphaned" if not cleanup_requested else "cleanup_pending"
        resources.append(ReconciledResource("vm", vm.name, expected, state, True))
        if cleanup_requested and vm.name not in protected_members:
            actions.append(CleanupAction("vm", vm.name, expected))

    observed_keys = {(item.resource_type, item.name) for item in resources}
    for item in recorded:
        key = (item.resource_type, item.name)
        if key in observed_keys:
            continue
        absent_is_authoritative = (
            item.resource_type == "vm" or inventory.clusters_authoritative
        )
        if cleanup_requested and item.owner_id == expected and absent_is_authoritative:
            state = "destroyed"
            detail = "not present in current inventory"
        elif cleanup_requested and item.resource_type == "cluster":
            state = "cleanup_pending"
            detail = "machine-readable cluster absence is not available"
            issues.append(
                InventoryIssue(
                    "cluster_inventory_unavailable", item.name, detail
                )
            )
        else:
            state = "creating"
            detail = "not present in current inventory"
        resources.append(
            ReconciledResource(
                item.resource_type, item.name, item.owner_id, state, False, detail
            )
        )

    actions.sort(key=lambda item: (item.resource_type, item.name))
    resources.sort(key=lambda item: (item.resource_type, item.name, item.lifecycle_state))
    return ReconciliationResult(expected, tuple(resources), tuple(actions), tuple(issues))


@dataclass(frozen=True)
class TargetGuidance:
    target: str
    lustre_tree: str
    arch: str | None
    list_local_argv: tuple[str, ...]
    list_remote_argv: tuple[str, ...]
    fetch_argv: tuple[str, ...]
    validate_argv: tuple[str, ...]
    default_guest_memory_mib: int = 2048


def target_guidance(target: str, lustre_tree: str | Path, *, arch: str | None = None) -> TargetGuidance:
    """Describe the required list -> fetch -> validate target preflight."""

    target_name = _required_text("target", target, limit=128)
    if not _SAFE_TARGET.fullmatch(target_name):
        raise ValueError("target is invalid")
    tree = str(Path(lustre_tree).expanduser().resolve())
    if arch is not None:
        arch = _required_text("arch", arch, limit=32)
        if not _SAFE_TARGET.fullmatch(arch):
            raise ValueError("arch is invalid")
    arch_args = ("--arch", arch) if arch else ()
    return TargetGuidance(
        target_name,
        tree,
        arch,
        ("ltvm", "target", "list", "local", *arch_args, "--json"),
        ("ltvm", "target", "list", "remote", *arch_args, "--json"),
        ("ltvm", "target", "fetch", target_name, *arch_args, "--json"),
        ("ltvm", "target", "validate", target_name, "--lustre-tree", tree, *arch_args, "--json"),
    )


class LTVMAdapter:
    """Narrow, injected, shell-free adapter for inventory and cleanup."""

    def __init__(self, runner: Runner = subprocess.run, *, timeout: float = 30.0):
        self.runner = runner
        self.timeout = timeout

    def _run(self, argv: Sequence[str]) -> subprocess.CompletedProcess:
        try:
            result = self.runner(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LTVMCommandError(f"LTVM command could not run: {type(exc).__name__}") from exc
        if result.returncode:
            raise LTVMCommandError(f"LTVM command exited with status {result.returncode}")
        return result

    def inventory(self) -> LTVMInventory:
        result = self._run(("ltvm", "list", "--json"))
        return LTVMInventory.from_json(result.stdout)

    def cleanup(self, action: CleanupAction) -> None:
        """Re-read inventory, prove ownership, then issue one exact destroy."""

        inventory = self.inventory()
        if action.resource_type == "vm":
            matches = inventory.named_vms(action.name)
            safe = len(matches) == 1 and matches[0].owner_id == action.owner_id
        elif action.resource_type == "cluster":
            matches = inventory.named_clusters(action.name)
            safe = len(matches) == 1 and matches[0].owner_id == action.owner_id
            members = matches[0].member_names if safe else ()
            safe = safe and bool(members) and members == action.member_names
            safe = safe and all(
                len(inventory.named_vms(name)) == 1
                and inventory.named_vms(name)[0].owner_id == action.owner_id
                for name in members
            )
        else:
            safe = False
        if not safe:
            raise UnsafeCleanupError("exact LTVM resource ownership could not be verified")
        self._run(action.argv)


@dataclass(frozen=True)
class ResourceExhaustionReport:
    failed_operation: str
    requested_resources: Mapping[str, Any]
    evidence: str
    owned_resource_names: tuple[str, ...]

    @classmethod
    def validate(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_owner_id: str,
        inventory: LTVMInventory,
    ) -> "ResourceExhaustionReport":
        if payload.get("state") != "resource_exhausted":
            raise ResourceExhaustionValidationError("state is not resource_exhausted")
        if payload.get("error_code") != "ltvm_resource_exhausted":
            raise ResourceExhaustionValidationError("error code is not ltvm_resource_exhausted")
        operation = payload.get("failed_operation")
        if operation not in {"ltvm create", "ltvm cluster create"}:
            raise ResourceExhaustionValidationError("failed operation is not an LTVM create")
        requested = payload.get("requested_resources")
        if not isinstance(requested, Mapping) or not requested:
            raise ResourceExhaustionValidationError("requested resources are missing")
        try:
            requested_json = json.dumps(
                dict(requested), sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ResourceExhaustionValidationError(
                "requested resources are not JSON data"
            ) from exc
        if len(requested_json.encode("utf-8")) > 16_384:
            raise ResourceExhaustionValidationError("requested resources are too large")
        evidence = payload.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ResourceExhaustionValidationError("capacity evidence is missing")
        evidence = evidence.strip()[:4000]
        lowered = evidence.casefold()
        if not any(term in lowered for term in _CAPACITY_TERMS):
            raise ResourceExhaustionValidationError("evidence does not establish resource exhaustion")
        names = payload.get("owned_resource_names", [])
        if not isinstance(names, list):
            raise ResourceExhaustionValidationError("owned_resource_names is not a list")
        normalized: list[str] = []
        for value in names:
            try:
                name = _safe_name("owned resource name", value)
            except ValueError as exc:
                raise ResourceExhaustionValidationError(str(exc)) from exc
            matches = inventory.named_vms(name)
            if len(matches) != 1 or matches[0].owner_id != expected_owner_id:
                raise ResourceExhaustionValidationError(
                    f"resource {name!r} is not uniquely owned by this session"
                )
            if name not in normalized:
                normalized.append(name)
        return cls(operation, dict(requested), evidence, tuple(normalized))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.timestamp()


def _datetime(value: float) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


@dataclass(frozen=True)
class CapacityDecision:
    patch_id: str
    run_id: str
    exhaustion_count: int
    retry_not_before: datetime
    email_idempotency_key: str
    email_status: str
    cleanup_status: str


@dataclass(frozen=True)
class EmailClaim:
    should_send: bool
    idempotency_key: str
    status: str


class LTVMCapacityStateStore:
    """Durable per-patch cooldown and at-most-one email-attempt decisions."""

    def __init__(
        self,
        path: str | Path,
        *,
        initial_cooldown: timedelta = timedelta(minutes=30),
        maximum_cooldown: timedelta = timedelta(hours=8),
    ) -> None:
        if initial_cooldown <= timedelta(0) or maximum_cooldown < initial_cooldown:
            raise ValueError("cooldown bounds are invalid")
        self.path = Path(path)
        self.initial_cooldown = initial_cooldown
        self.maximum_cooldown = maximum_cooldown
        existed_parent = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existed_parent:
            os.chmod(self.path.parent, 0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pw_ltvm_patch_capacity (
                    patch_id TEXT PRIMARY KEY,
                    exhaustion_count INTEGER NOT NULL,
                    retry_not_before REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pw_ltvm_capacity_event (
                    run_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    report_hash TEXT NOT NULL,
                    exhaustion_count INTEGER NOT NULL,
                    retry_not_before REAL NOT NULL,
                    email_idempotency_key TEXT NOT NULL UNIQUE,
                    email_status TEXT NOT NULL CHECK (
                        email_status IN ('pending', 'claimed', 'sent', 'failed')
                    ),
                    email_claimed_by TEXT,
                    cleanup_status TEXT NOT NULL CHECK (
                        cleanup_status IN ('pending', 'succeeded', 'failed')
                    ),
                    cleanup_detail TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _decision(row: sqlite3.Row) -> CapacityDecision:
        return CapacityDecision(
            row["patch_id"], row["run_id"], row["exhaustion_count"],
            _datetime(row["retry_not_before"]), row["email_idempotency_key"],
            row["email_status"], row["cleanup_status"],
        )

    def record_exhaustion(
        self,
        patch_id: str,
        run_id: str,
        owner_id: str,
        report: ResourceExhaustionReport,
        *,
        at: datetime | None = None,
    ) -> CapacityDecision:
        patch = _required_text("patch_id", patch_id)
        run = _required_text("run_id", run_id)
        owner = _required_text("owner_id", owner_id, limit=MAX_OWNER_ID_LENGTH)
        now = at or _utc_now()
        now_epoch = _epoch(now)
        report_doc = json.dumps(
            {
                "failed_operation": report.failed_operation,
                "requested_resources": report.requested_resources,
                "evidence": report.evidence,
                "owned_resource_names": report.owned_resource_names,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        report_hash = hashlib.sha256(report_doc.encode()).hexdigest()
        email_key = f"ltvm-resource-exhausted:{run}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                if (
                    existing["patch_id"] != patch
                    or existing["owner_id"] != owner
                    or existing["report_hash"] != report_hash
                ):
                    raise ValueError("run_id was already used for a different exhaustion event")
                return self._decision(existing)
            prior = connection.execute(
                "SELECT * FROM pw_ltvm_patch_capacity WHERE patch_id = ?", (patch,)
            ).fetchone()
            count = (prior["exhaustion_count"] if prior else 0) + 1
            seconds = min(
                self.initial_cooldown.total_seconds() * (2 ** (count - 1)),
                self.maximum_cooldown.total_seconds(),
            )
            retry_epoch = now_epoch + seconds
            if prior is not None:
                retry_epoch = max(retry_epoch, prior["retry_not_before"])
            connection.execute(
                """
                INSERT INTO pw_ltvm_patch_capacity(
                    patch_id, exhaustion_count, retry_not_before, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(patch_id) DO UPDATE SET
                    exhaustion_count = excluded.exhaustion_count,
                    retry_not_before = excluded.retry_not_before,
                    updated_at = excluded.updated_at
                """,
                (patch, count, retry_epoch, now_epoch),
            )
            connection.execute(
                """
                INSERT INTO pw_ltvm_capacity_event(
                    run_id, patch_id, owner_id, report_hash, exhaustion_count,
                    retry_not_before, email_idempotency_key, email_status,
                    cleanup_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', ?, ?)
                """,
                (run, patch, owner, report_hash, count, retry_epoch, email_key, now_epoch, now_epoch),
            )
            row = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._decision(row)

    def claim_email(self, run_id: str, consumer_id: str, *, at: datetime | None = None) -> EmailClaim:
        run = _required_text("run_id", run_id)
        consumer = _required_text("consumer_id", consumer_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(run)
            should_send = row["email_status"] == "pending"
            if should_send:
                connection.execute(
                    """
                    UPDATE pw_ltvm_capacity_event
                    SET email_status = 'claimed', email_claimed_by = ?, updated_at = ?
                    WHERE run_id = ? AND email_status = 'pending'
                    """,
                    (consumer, _epoch(at or _utc_now()), run),
                )
                status = "claimed"
            else:
                status = row["email_status"]
            connection.commit()
        return EmailClaim(should_send, row["email_idempotency_key"], status)

    def finish_email(
        self,
        run_id: str,
        consumer_id: str,
        *,
        sent: bool,
        at: datetime | None = None,
    ) -> CapacityDecision:
        with self._connect() as connection, connection:
            row = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            target = "sent" if sent else "failed"
            if row["email_status"] == target:
                return self._decision(row)
            if row["email_status"] != "claimed" or row["email_claimed_by"] != consumer_id:
                raise ValueError("email is not claimed by this consumer")
            connection.execute(
                "UPDATE pw_ltvm_capacity_event SET email_status = ?, updated_at = ? WHERE run_id = ?",
                (target, _epoch(at or _utc_now()), run_id),
            )
            row = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert row is not None
        return self._decision(row)

    def finish_cleanup(
        self,
        run_id: str,
        *,
        succeeded: bool,
        detail: str | None = None,
        at: datetime | None = None,
    ) -> CapacityDecision:
        target = "succeeded" if succeeded else "failed"
        with self._connect() as connection, connection:
            row = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["cleanup_status"] == target:
                return self._decision(row)
            if row["cleanup_status"] != "pending":
                raise ValueError("cleanup already has a different terminal result")
            connection.execute(
                """
                UPDATE pw_ltvm_capacity_event
                SET cleanup_status = ?, cleanup_detail = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (target, str(detail or "")[:1000] or None, _epoch(at or _utc_now()), run_id),
            )
            row = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert row is not None
        return self._decision(row)

    def in_cooldown(self, patch_id: str, *, at: datetime | None = None) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT retry_not_before FROM pw_ltvm_patch_capacity WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        return row is not None and _epoch(at or _utc_now()) < row["retry_not_before"]

    def get_event(self, run_id: str) -> CapacityDecision:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pw_ltvm_capacity_event WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._decision(row)


__all__ = [
    "CapacityDecision", "CleanupAction", "ClusterInventoryRecord", "EmailClaim",
    "InventoryIssue", "LTVMAdapter", "LTVMCapacityStateStore", "LTVMCommandError",
    "LTVMInventory", "LTVMInventoryError", "PATCH_WATCHER_OWNER_PREFIX",
    "ReconciledResource", "ReconciliationResult", "ResourceExhaustionReport",
    "ResourceExhaustionValidationError", "SessionResourceRecord", "TargetGuidance",
    "UnsafeCleanupError", "VMInventoryRecord", "owner_id_for_session",
    "reconcile_session_resources", "session_id_from_owner", "target_guidance",
]
