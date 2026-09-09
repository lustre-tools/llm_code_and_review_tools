"""Safe LTVM ownership, cleanup, target, and capacity primitives.

This module is deliberately independent of the web application and runner.
It consumes LTVM's machine-readable inventory, plans cleanup only for
resources a session can prove are its own -- by the reserved ``co<N>-``
checkout name prefix, or by an exact durable owner id where one exists -- and
keeps the command adapter small enough to audit.  The adapter never invokes a
shell.

Configured guest memory is capacity requested from a guest.  Host RSS is an
observation of the QEMU process.  They are intentionally separate fields and
are never added together here.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PATCH_WATCHER_OWNER_PREFIX = "patch-watcher:"
MAX_OWNER_ID_LENGTH = 255
_MIB = 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
Runner = Callable[..., subprocess.CompletedProcess]


class LTVMInventoryError(ValueError):
    """Machine-readable LTVM inventory was unavailable or invalid."""


class LTVMCommandError(RuntimeError):
    """A bounded LTVM adapter operation failed."""


class UnsafeCleanupError(LTVMCommandError):
    """Destruction was refused because exact ownership was not provable."""


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


def checkout_vm_prefix(checkout_index: int) -> str:
    """Return the one VM name prefix a pool checkout owns."""

    return f"co{int(checkout_index)}-"


def vm_belongs_to_checkout(name: Any, checkout_index: int) -> bool:
    """True when a VM name is inside the checkout's reserved namespace.

    The trailing dash is the whole point: without it checkout 3 would claim
    ``co31-sanity``, which belongs to checkout 31.
    """

    return isinstance(name, str) and name.startswith(checkout_vm_prefix(checkout_index))


def cluster_belongs_to_checkout(name: Any, checkout_index: int) -> bool:
    """True when a cluster name is inside the checkout's namespace.

    ``ltvm cluster create co2 mgs+mds:co2-mds:1`` names the cluster ``co2``
    and its members ``co2-<role>``, so both spellings are the checkout's and
    neither may spill into ``co21``.
    """

    if not isinstance(name, str):
        return False
    return name == f"co{int(checkout_index)}" or vm_belongs_to_checkout(
        name, checkout_index
    )


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
    # The inventory declared an owner this parser could not read. Distinct
    # from "no owner declared": an unreadable owner is evidence the guest
    # belongs to someone, not evidence that it does not.
    owner_unreadable: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def session_id(self) -> str | None:
        return session_id_from_owner(self.owner_id)


@dataclass(frozen=True)
class ClusterInventoryRecord:
    name: str
    owner_id: str | None
    member_names: tuple[str, ...]
    owner_unreadable: bool = False
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

    def vms_named_for_checkout(self, checkout_index: int) -> tuple[VMInventoryRecord, ...]:
        """Return the VMs belonging to a pool checkout, by name prefix.

        This is the ownership model now that no broker stamps an owner id: an
        agent holding checkout N names its VMs ``co<N>-<role>``, which is
        already the mandatory convention in CLAUDE.md.  The trailing dash
        matters -- without it checkout 3 would claim ``co31-sanity``.
        """

        return tuple(
            vm for vm in self.vms if vm_belongs_to_checkout(vm.name, checkout_index)
        )

    def clusters_named_for_checkout(
        self, checkout_index: int
    ) -> tuple[ClusterInventoryRecord, ...]:
        """Return the clusters belonging to a pool checkout, by name."""

        return tuple(
            cluster for cluster in self.clusters
            if cluster_belongs_to_checkout(cluster.name, checkout_index)
        )

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
    def from_json(cls, document: str | bytes | Mapping[str, Any]) -> LTVMInventory:
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
                    owner_unreadable=bool(owner_problem),
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
            for raw_member in raw_members:
                # A member arrives either as a bare name or as an object carrying one.
                member = (
                    raw_member.get("name") if isinstance(raw_member, Mapping)
                    else raw_member
                )
                try:
                    members.append(_safe_name("cluster member", member))
                except ValueError as exc:
                    issues.append(InventoryIssue("invalid_cluster_member", name, str(exc)))
            clusters.append(
                ClusterInventoryRecord(
                    name=name,
                    owner_id=owner_id,
                    member_names=tuple(members),
                    owner_unreadable=bool(owner_problem),
                    raw=dict(raw),
                )
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
    """One destruction the session may prove it is entitled to perform.

    ``owner_id`` always names the session that claimed the resource.  How that
    claim is *proved* depends on ``checkout_index``: when it is set the claim
    rests on the reserved ``co<N>-`` name prefix (the current model, since
    nothing stamps an LTVM owner id any more), and otherwise on an exact owner
    id carried by the inventory row itself.
    """

    resource_type: str
    name: str
    owner_id: str
    member_names: tuple[str, ...] = ()
    checkout_index: int | None = None

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
    checkout_index: int | None = None,
    adopt_unrecorded: bool = True,
) -> ReconciliationResult:
    """Associate and optionally plan cleanup for exactly one session.

    A resource is this session's when the inventory row carries its exact
    owner id, or -- the live model, since nothing stamps an owner id any more
    -- when its name is inside the reserved ``co<N>-`` namespace of the
    checkout this run holds.  ``checkout_index`` of ``None`` means the run has
    no reserved prefix and therefore claims nothing by name; there is
    deliberately no "everything else" fallback.

    Newly discovered VMs are adopted as *observations* so partial cluster
    creation can be cleaned.  Missing, malformed, duplicate, unowned, and
    differently owned entries never become cleanup actions.

    ``adopt_unrecorded=False`` narrows that discovery to what the session
    already wrote down: the resources it recorded, plus the named members of a
    cluster it recorded, which is the partial-create case discovery exists
    for.  A caller planning *destruction* for a session that can no longer
    record anything -- a terminal run -- must pass it, because by then the
    ``co<N>-`` namespace may already have been re-issued and every name in it
    would otherwise be re-adopted from whatever the host happens to be running
    now.
    """

    expected = owner_id_for_session(session_id)
    index = int(checkout_index) if checkout_index is not None else None
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

    def claimed(
        name: str, owner_id: str | None, *, cluster: bool, unreadable: bool = False
    ) -> bool:
        """True when this session may act on the named resource.

        A prefix claim is refused when the row carries some *other* owner id:
        the name says the resource is ours while the inventory says it is
        someone else's, which is exactly the ambiguity cleanup must not
        resolve by guessing.

        An owner id that is present but unreadable -- a JSON number, a list,
        an over-long string -- is refused for the same reason. It used to
        parse to None, which is indistinguishable from "no owner declared",
        so a guest belonging to another session was claimed by prefix and
        destroyed. Unreadable is evidence of an owner, not absence of one.
        """

        if owner_id == expected:
            return True
        if index is None or owner_id is not None or unreadable:
            return False
        return (
            cluster_belongs_to_checkout(name, index) if cluster
            else vm_belongs_to_checkout(name, index)
        )

    recorded_members = {
        name
        for item in recorded
        if item.resource_type == "cluster"
        for name in item.member_names
    }

    def visible(name: str, known: SessionResourceRecord | None, *, cluster: bool) -> bool:
        """True when this inventory row is one the session may reason about.

        With adoption enabled every row the session can claim is in scope.
        With it disabled only the session's own written record is, so an
        inventory that changed after the session stopped being able to record
        anything cannot enlarge what cleanup will destroy.
        """

        if adopt_unrecorded or known is not None:
            return True
        return not cluster and name in recorded_members

    protected_members: set[str] = set()
    for cluster in inventory.clusters:
        matches = inventory.named_clusters(cluster.name)
        if len(matches) != 1:
            continue
        known = recorded_by_key.get(("cluster", cluster.name))
        if not visible(cluster.name, known, cluster=True):
            continue
        if not claimed(
            cluster.name, cluster.owner_id, cluster=True,
            unreadable=cluster.owner_unreadable,
        ):
            if known is not None:
                issues.append(InventoryIssue("owner_mismatch", cluster.name, "cluster ownership is missing or different"))
                resources.append(ReconciledResource("cluster", cluster.name, cluster.owner_id, "ownership_ambiguous", True))
            continue
        member_names = cluster.member_names or (known.member_names if known else ())
        member_rows = [inventory.named_vms(name) for name in member_names]
        complete = bool(member_names) and all(
            len(rows) == 1 and claimed(
                rows[0].name, rows[0].owner_id, cluster=False,
                unreadable=rows[0].owner_unreadable,
            )
            for rows in member_rows
        )
        state = "cleanup_pending" if cleanup_requested else "active"
        if not complete:
            state = "orphaned"
            issues.append(InventoryIssue("partial_cluster", cluster.name, "cluster members are missing, ambiguous, or differently owned"))
        resources.append(ReconciledResource("cluster", cluster.name, expected, state, True))
        if cleanup_requested and complete:
            owner_proved = cluster.owner_id == expected and all(
                inventory.named_vms(name)[0].owner_id == expected
                for name in member_names
            )
            actions.append(CleanupAction(
                "cluster", cluster.name, expected, member_names,
                checkout_index=None if owner_proved else index,
            ))
            protected_members.update(member_names)

    for vm in inventory.vms:
        matches = inventory.named_vms(vm.name)
        if len(matches) != 1:
            continue
        known = recorded_by_key.get(("vm", vm.name))
        if not visible(vm.name, known, cluster=False):
            continue
        if not claimed(
            vm.name, vm.owner_id, cluster=False, unreadable=vm.owner_unreadable
        ):
            if known is not None:
                issues.append(InventoryIssue("owner_mismatch", vm.name, "VM ownership is missing or different"))
                resources.append(ReconciledResource("vm", vm.name, vm.owner_id, "ownership_ambiguous", True))
            continue
        state = "cleanup_pending" if cleanup_requested else "active"
        if known is None:
            state = "orphaned" if not cleanup_requested else "cleanup_pending"
        resources.append(ReconciledResource("vm", vm.name, expected, state, True))
        if cleanup_requested and vm.name not in protected_members:
            actions.append(CleanupAction(
                "vm", vm.name, expected,
                checkout_index=None if vm.owner_id == expected else index,
            ))

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

    def operator_stop(self, name: str) -> None:
        """Stop one guest because a human asked, not because a run owns it.

        Deliberately NOT routed through `cleanup`/`_proves_ownership`. Those
        guard the CONTROLLER acting on its own initiative, where acting on a
        guest it cannot prove it owns is the bug. This is the operator acting
        on their own machine from their own dashboard, and the guests they
        most need to reach are exactly the ones with no owner -- the ones the
        ownership proof would refuse.

        The name is still validated: it comes from an `ltvm list --json` the
        tool did not write, and it becomes an argv entry.
        """

        self._run(("sudo", "-n", "ltvm", "stop", _safe_name("VM name", name)))

    def operator_destroy(self, name: str) -> None:
        """Destroy one guest because a human asked. Irreversible.

        Same reasoning as `operator_stop`, and the caller is responsible for
        having taken an explicit confirmation first.
        """

        self._run(("sudo", "-n", "ltvm", "destroy", _safe_name("VM name", name)))

    @staticmethod
    def _proves_ownership(
        action: CleanupAction,
        name: str,
        owner_id: str | None,
        *,
        cluster: bool = False,
        unreadable: bool = False,
    ) -> bool:
        """Re-prove one row's ownership under the action's own claim basis."""

        if action.checkout_index is None:
            return owner_id == action.owner_id
        # Same rule as the plan-time claim: an owner id we cannot read is an
        # owner, and this is the last check before an irreversible destroy.
        if unreadable:
            return False
        if owner_id is not None and owner_id != action.owner_id:
            return False
        return (
            cluster_belongs_to_checkout(name, action.checkout_index) if cluster
            else vm_belongs_to_checkout(name, action.checkout_index)
        )

    def cleanup(self, action: CleanupAction) -> None:
        """Re-read inventory, prove ownership, then issue one exact destroy."""

        inventory = self.inventory()
        if action.resource_type == "vm":
            matches = inventory.named_vms(action.name)
            safe = len(matches) == 1 and self._proves_ownership(
                action, matches[0].name, matches[0].owner_id,
                unreadable=matches[0].owner_unreadable,
            )
        elif action.resource_type == "cluster":
            matches = inventory.named_clusters(action.name)
            safe = len(matches) == 1 and self._proves_ownership(
                action, matches[0].name, matches[0].owner_id, cluster=True,
                unreadable=matches[0].owner_unreadable,
            )
            members = matches[0].member_names if safe else ()
            safe = safe and bool(members) and members == action.member_names
            safe = safe and all(
                len(inventory.named_vms(name)) == 1
                and self._proves_ownership(
                    action, name, inventory.named_vms(name)[0].owner_id,
                    unreadable=inventory.named_vms(name)[0].owner_unreadable,
                )
                for name in members
            )
        else:
            safe = False
        if not safe:
            raise UnsafeCleanupError("exact LTVM resource ownership could not be verified")
        self._run(action.argv)


__all__ = [
    "PATCH_WATCHER_OWNER_PREFIX",
    "CleanupAction",
    "ClusterInventoryRecord",
    "InventoryIssue",
    "LTVMAdapter",
    "LTVMCommandError",
    "LTVMInventory",
    "LTVMInventoryError",
    "ReconciledResource",
    "ReconciliationResult",
    "SessionResourceRecord",
    "UnsafeCleanupError",
    "VMInventoryRecord",
    "checkout_vm_prefix",
    "cluster_belongs_to_checkout",
    "owner_id_for_session",
    "reconcile_session_resources",
    "session_id_from_owner",
    "vm_belongs_to_checkout",
]
