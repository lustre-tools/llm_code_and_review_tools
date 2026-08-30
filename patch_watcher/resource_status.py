"""Dependency-free host and LTVM resource collection for Patch Watcher.

The collector intentionally keeps two different VM memory concepts separate:
``configured_guest_memory_bytes`` is the amount passed to QEMU, while
``host_rss_bytes`` is a best-effort observation of the QEMU process on the
host.  The former must never be presented as physical memory consumption.

All operating-system I/O is injectable.  Callers can supply a subprocess-like
``runner`` and a text ``reader`` for deterministic tests or for collection in
a restricted service process.
"""

from __future__ import annotations

import json
import platform
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


Runner = Callable[..., subprocess.CompletedProcess[str]]
Reader = Callable[[str], str]
Clock = Callable[[], datetime]

_MIB = 1024 * 1024
_MEMINFO_LINE_RE = re.compile(r"^([A-Za-z_()]+):\s*(\d+)\s*([A-Za-z]*)\s*$")
_VM_STAT_HEADER_RE = re.compile(r"page size of\s+(\d+)\s+bytes", re.IGNORECASE)
_VM_STAT_LINE_RE = re.compile(r"^([^:]+):\s*([0-9]+)\.?")
_SWAP_USAGE_RE = re.compile(
    r"total\s*=\s*([0-9.]+)([KMGTP]?)\s+"
    r"used\s*=\s*([0-9.]+)([KMGTP]?)\s+"
    r"free\s*=\s*([0-9.]+)([KMGTP]?)",
    re.IGNORECASE,
)
_PATCH_WATCHER_OWNER_PREFIX = "patch-watcher:"
_QEMU_NAMES = ("qemu-system-", "qemu-kvm")


def _default_reader(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sample_time(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class CollectionError:
    """A bounded, display-safe collection problem."""

    scope: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"scope": self.scope, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class HostMemoryStatus:
    """One host-memory observation.

    Quality is ``good`` when the OS provides an available-memory figure,
    ``estimated`` when it is derived from reclaimable counters, ``partial``
    when only some requested fields could be read, and ``unavailable`` when
    no usable total is available.
    """

    sampled_at: datetime
    source: str
    quality: str
    total_bytes: int | None = None
    available_bytes: int | None = None
    used_bytes: int | None = None
    swap_total_bytes: int | None = None
    swap_free_bytes: int | None = None
    swap_used_bytes: int | None = None
    errors: tuple[CollectionError, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampled_at": _iso_timestamp(self.sampled_at),
            "source": self.source,
            "quality": self.quality,
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes,
            "used_bytes": self.used_bytes,
            "swap_total_bytes": self.swap_total_bytes,
            "swap_free_bytes": self.swap_free_bytes,
            "swap_used_bytes": self.swap_used_bytes,
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class LTVMVMStatus:
    """Normalized status for one LTVM VM."""

    name: str
    state: str
    owner_id: str | None
    patch_watcher_session_id: str | None
    configured_guest_memory_bytes: int | None
    host_rss_bytes: int | None
    process_id: int | None
    vcpus: int | None
    ip: str | None
    host_memory_source: str | None
    quality: str
    errors: tuple[CollectionError, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "owner_id": self.owner_id,
            "patch_watcher_session_id": self.patch_watcher_session_id,
            "configured_guest_memory_bytes": self.configured_guest_memory_bytes,
            "host_rss_bytes": self.host_rss_bytes,
            "process_id": self.process_id,
            "vcpus": self.vcpus,
            "ip": self.ip,
            "host_memory_source": self.host_memory_source,
            "quality": self.quality,
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class LTVMInventory:
    """A resilient projection of ``ltvm list --json``."""

    sampled_at: datetime
    source: str
    quality: str
    vms: tuple[LTVMVMStatus, ...] = field(default_factory=tuple)
    errors: tuple[CollectionError, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        configured_values = [
            vm.configured_guest_memory_bytes
            for vm in self.vms
            if vm.configured_guest_memory_bytes is not None
        ]
        configured_known = sum(configured_values)
        configured = (
            configured_known
            if len(configured_values) == len(self.vms)
            else None
        )

        running = [vm for vm in self.vms if vm.state == "running"]
        measured_values = [
            vm.host_rss_bytes for vm in running if vm.host_rss_bytes is not None
        ]
        measured_known = sum(measured_values)
        states_complete = all(vm.state in {"running", "stopped"} for vm in self.vms)
        measured = (
            measured_known
            if states_complete and len(measured_values) == len(running)
            else None
        )
        return {
            "sampled_at": _iso_timestamp(self.sampled_at),
            "source": self.source,
            "quality": self.quality,
            "vm_count": len(self.vms),
            "configured_guest_memory_bytes": configured,
            "known_configured_guest_memory_bytes": configured_known,
            "configured_memory_known_vm_count": len(configured_values),
            "measured_host_rss_bytes": measured,
            "known_host_rss_bytes": measured_known,
            "host_rss_measured_vm_count": len(measured_values),
            "running_vm_count": len(running),
            "vms": [vm.to_dict() for vm in self.vms],
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class ProcessTreeMemoryStatus:
    """RSS observation for one managed process and all of its descendants.

    Each PID is counted at most once.  RSS can still overlap for shared pages,
    so a complete result is labelled ``estimated`` rather than ``good``.
    ``total_rss_bytes`` is ``None`` if any member could not be measured;
    ``known_rss_bytes`` remains available as an explicitly partial subtotal.
    """

    sampled_at: datetime
    source: str
    quality: str
    root_pid: int
    root_command: str | None = None
    total_rss_bytes: int | None = None
    known_rss_bytes: int = 0
    process_count: int = 0
    measured_process_count: int = 0
    process_ids: tuple[int, ...] = field(default_factory=tuple)
    errors: tuple[CollectionError, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampled_at": _iso_timestamp(self.sampled_at),
            "source": self.source,
            "quality": self.quality,
            "root_pid": self.root_pid,
            "root_command": self.root_command,
            "total_rss_bytes": self.total_rss_bytes,
            "known_rss_bytes": self.known_rss_bytes,
            "process_count": self.process_count,
            "measured_process_count": self.measured_process_count,
            "process_ids": list(self.process_ids),
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class ResourceSnapshot:
    """UI-ready host and VM resource snapshot."""

    sampled_at: datetime
    source: str
    quality: str
    host_memory: HostMemoryStatus
    ltvm: LTVMInventory
    errors: tuple[CollectionError, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampled_at": _iso_timestamp(self.sampled_at),
            "source": self.source,
            "quality": self.quality,
            "host_memory": self.host_memory.to_dict(),
            "ltvm": self.ltvm.to_dict(),
            "errors": [error.to_dict() for error in self.errors],
        }


def _run(
    runner: Runner,
    command: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(command),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _command_error(
    scope: str,
    code: str,
    command: Sequence[str],
    result: subprocess.CompletedProcess[str],
) -> CollectionError:
    detail = (result.stderr or "").strip().splitlines()
    suffix = f": {detail[0][:200]}" if detail else ""
    return CollectionError(
        scope,
        code,
        f"{' '.join(command)} exited with status {result.returncode}{suffix}",
    )


def _read_command(
    runner: Runner,
    command: Sequence[str],
    *,
    scope: str,
    code: str,
    timeout: float,
) -> tuple[str | None, CollectionError | None]:
    try:
        result = _run(runner, command, timeout=timeout)
    except FileNotFoundError:
        return None, CollectionError(scope, code, f"{command[0]} is not installed")
    except subprocess.TimeoutExpired:
        return None, CollectionError(
            scope, code, f"{' '.join(command)} timed out after {timeout:g}s"
        )
    except OSError as exc:
        return None, CollectionError(
            scope, code, f"could not run {command[0]}: {str(exc)[:200]}"
        )
    if result.returncode != 0:
        return None, _command_error(scope, code, command, result)
    return result.stdout, None


def _meminfo_bytes(value: str, unit: str) -> int:
    multipliers = {"": 1, "b": 1, "kb": 1024, "mb": _MIB, "gb": 1024 * _MIB}
    try:
        multiplier = multipliers[unit.casefold()]
    except KeyError as exc:
        raise ValueError(f"unsupported memory unit {unit!r}") from exc
    return int(value) * multiplier


def _parse_linux_meminfo(text: str) -> tuple[dict[str, int], list[str]]:
    values: dict[str, int] = {}
    malformed: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _MEMINFO_LINE_RE.match(line)
        if not match:
            if line.startswith(("Mem", "Swap", "Buffers", "Cached", "SReclaimable", "Shmem")):
                malformed.append(line[:120])
            continue
        try:
            values[match.group(1)] = _meminfo_bytes(match.group(2), match.group(3))
        except ValueError:
            malformed.append(line[:120])
    return values, malformed


def _bounded_memory_values(
    total: int | None,
    available: int | None,
    *,
    scope: str,
    errors: list[CollectionError],
) -> tuple[int | None, int | None]:
    if total is None or total <= 0:
        return None, None
    if available is None:
        return None, None
    if available < 0 or available > total:
        errors.append(
            CollectionError(
                scope,
                "invalid_available_memory",
                "available memory was outside the range 0..total and was clamped",
            )
        )
        available = min(total, max(0, available))
    return available, total - available


def _collect_linux_memory(
    *,
    sampled_at: datetime,
    reader: Reader,
) -> HostMemoryStatus:
    errors: list[CollectionError] = []
    try:
        text = reader("/proc/meminfo")
    except (OSError, UnicodeError) as exc:
        errors.append(
            CollectionError("host_memory", "meminfo_unavailable", str(exc)[:200])
        )
        return HostMemoryStatus(
            sampled_at, "/proc/meminfo", "unavailable", errors=tuple(errors)
        )

    values, malformed = _parse_linux_meminfo(text)
    for line in malformed:
        errors.append(
            CollectionError(
                "host_memory", "malformed_meminfo", f"could not parse: {line}"
            )
        )

    total = values.get("MemTotal")
    if total is None or total <= 0:
        errors.append(
            CollectionError(
                "host_memory", "missing_memtotal", "/proc/meminfo has no valid MemTotal"
            )
        )
        return HostMemoryStatus(
            sampled_at, "/proc/meminfo", "unavailable", errors=tuple(errors)
        )

    available = values.get("MemAvailable")
    estimated = False
    if available is None:
        fallback_fields = ("MemFree", "Buffers", "Cached", "SReclaimable")
        if any(field in values for field in fallback_fields):
            available = sum(values.get(field, 0) for field in fallback_fields)
            available -= values.get("Shmem", 0)
            estimated = True
            errors.append(
                CollectionError(
                    "host_memory",
                    "memavailable_estimated",
                    "MemAvailable was absent; used reclaimable-memory counters",
                )
            )
        else:
            errors.append(
                CollectionError(
                    "host_memory",
                    "missing_memavailable",
                    "/proc/meminfo has no available-memory counters",
                )
            )

    available, used = _bounded_memory_values(
        total, available, scope="host_memory", errors=errors
    )
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    swap_used = None
    if swap_total is not None and swap_free is not None:
        swap_free = min(swap_total, max(0, swap_free))
        swap_used = swap_total - swap_free
    elif swap_total is not None:
        errors.append(
            CollectionError(
                "host_memory", "missing_swapfree", "/proc/meminfo has no SwapFree"
            )
        )

    if available is None:
        quality = "partial"
    elif estimated:
        quality = "estimated"
    elif errors:
        quality = "partial"
    else:
        quality = "good"
    return HostMemoryStatus(
        sampled_at=sampled_at,
        source="/proc/meminfo",
        quality=quality,
        total_bytes=total,
        available_bytes=available,
        used_bytes=used,
        swap_total_bytes=swap_total,
        swap_free_bytes=swap_free,
        swap_used_bytes=swap_used,
        errors=tuple(errors),
    )


def _parse_vm_stat(text: str) -> tuple[int, dict[str, int]]:
    header = _VM_STAT_HEADER_RE.search(text)
    if not header:
        raise ValueError("vm_stat did not report a page size")
    page_size = int(header.group(1))
    if page_size <= 0:
        raise ValueError("vm_stat reported an invalid page size")
    pages: dict[str, int] = {}
    for line in text.splitlines():
        match = _VM_STAT_LINE_RE.match(line.strip())
        if match:
            pages[match.group(1).strip()] = int(match.group(2))
    return page_size, pages


def _scaled_number(value: str, unit: str) -> int:
    power = "KMGTP".find(unit.upper()) + 1 if unit else 0
    return int(float(value) * (1024**power))


def _collect_macos_memory(
    *,
    sampled_at: datetime,
    runner: Runner,
    timeout: float,
) -> HostMemoryStatus:
    errors: list[CollectionError] = []
    total: int | None = None
    available: int | None = None
    swap_total: int | None = None
    swap_free: int | None = None
    swap_used: int | None = None

    output, error = _read_command(
        runner,
        ("sysctl", "-n", "hw.memsize"),
        scope="host_memory",
        code="sysctl_memsize_failed",
        timeout=timeout,
    )
    if error:
        errors.append(error)
    else:
        try:
            total = int((output or "").strip())
            if total <= 0:
                raise ValueError
        except ValueError:
            total = None
            errors.append(
                CollectionError(
                    "host_memory", "invalid_memsize", "hw.memsize was not a positive integer"
                )
            )

    output, error = _read_command(
        runner,
        ("vm_stat",),
        scope="host_memory",
        code="vm_stat_failed",
        timeout=timeout,
    )
    if error:
        errors.append(error)
    else:
        try:
            page_size, pages = _parse_vm_stat(output or "")
            available_pages = sum(
                pages.get(key, 0)
                for key in ("Pages free", "Pages inactive", "Pages speculative")
            )
            available = available_pages * page_size
        except ValueError as exc:
            errors.append(
                CollectionError("host_memory", "invalid_vm_stat", str(exc))
            )

    output, error = _read_command(
        runner,
        ("sysctl", "-n", "vm.swapusage"),
        scope="host_memory",
        code="swapusage_failed",
        timeout=timeout,
    )
    if error:
        errors.append(error)
    else:
        match = _SWAP_USAGE_RE.search(output or "")
        if match:
            swap_total = _scaled_number(match.group(1), match.group(2))
            swap_used = _scaled_number(match.group(3), match.group(4))
            swap_free = _scaled_number(match.group(5), match.group(6))
        else:
            errors.append(
                CollectionError(
                    "host_memory", "invalid_swapusage", "could not parse vm.swapusage"
                )
            )

    available, used = _bounded_memory_values(
        total, available, scope="host_memory", errors=errors
    )
    if total is None:
        quality = "unavailable"
    elif available is None:
        quality = "partial"
    elif errors:
        quality = "partial"
    else:
        # vm_stat counters are an estimate of readily reclaimable memory.
        quality = "estimated"
    return HostMemoryStatus(
        sampled_at=sampled_at,
        source="sysctl hw.memsize; vm_stat; sysctl vm.swapusage",
        quality=quality,
        total_bytes=total,
        available_bytes=available,
        used_bytes=used,
        swap_total_bytes=swap_total,
        swap_free_bytes=swap_free,
        swap_used_bytes=swap_used,
        errors=tuple(errors),
    )


def collect_host_memory(
    *,
    system: str | None = None,
    runner: Runner = subprocess.run,
    reader: Reader = _default_reader,
    clock: Clock = _utc_now,
    timeout: float = 5.0,
) -> HostMemoryStatus:
    """Collect host memory without raising for an unsupported or broken OS."""

    sampled_at = _sample_time(clock)
    os_name = (system or platform.system()).casefold()
    if os_name == "linux":
        return _collect_linux_memory(sampled_at=sampled_at, reader=reader)
    if os_name in {"darwin", "macos"}:
        return _collect_macos_memory(
            sampled_at=sampled_at, runner=runner, timeout=timeout
        )
    error = CollectionError(
        "host_memory", "unsupported_platform", f"unsupported platform: {system or platform.system()}"
    )
    return HostMemoryStatus(
        sampled_at, system or platform.system(), "unavailable", errors=(error,)
    )


def _extract_vm_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        raise ValueError("LTVM JSON root must be an object or list")

    # Current LTVM response and plausible API aliases.
    for key in ("vms", "virtual_machines", "machines", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    # Standard tool envelope: {ok, data, meta}.  Some command wrappers use a
    # result/payload key, so unwrap those without recursively walking arbitrary
    # metadata lists.
    for key in ("data", "result", "payload"):
        value = payload.get(key)
        if isinstance(value, (Mapping, list)):
            return _extract_vm_rows(value)
    raise ValueError("LTVM JSON contains no VM list")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        return int(value)
    return None


def _memory_bytes(row: Mapping[str, Any]) -> int | None:
    containers: list[Mapping[str, Any]] = [row]
    for key in ("config", "configuration", "resources"):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)

    for container in containers:
        for key in (
            "configured_guest_memory_bytes",
            "configured_memory_bytes",
            "memory_bytes",
        ):
            if key in container:
                return _integer(container.get(key))
    for container in containers:
        for key in (
            "configured_guest_memory_mb",
            "configured_memory_mb",
            "memory_mb",
            "mem_mb",
            "memory_mib",
            "mem_mib",
            "mem",
        ):
            if key in container:
                value = _integer(container.get(key))
                return value * _MIB if value is not None else None
    return None


def _has_memory_field(row: Mapping[str, Any]) -> bool:
    keys = {
        "configured_guest_memory_bytes",
        "configured_memory_bytes",
        "memory_bytes",
        "configured_guest_memory_mb",
        "configured_memory_mb",
        "memory_mb",
        "mem_mb",
        "memory_mib",
        "mem_mib",
        "mem",
    }
    if any(key in row for key in keys):
        return True
    return any(
        isinstance(row.get(container), Mapping)
        and any(key in row[container] for key in keys)
        for container in ("config", "configuration", "resources")
    )


def _normalize_state(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    state = value.strip().casefold().replace("_", "-")
    if state in {"up", "active", "started"}:
        return "running"
    if state in {"down", "inactive", "shutoff", "shut-off"}:
        return "stopped"
    return state


def _is_qemu_command(command: str) -> bool:
    # Linux ``comm`` contains only the executable name; macOS ``ps command``
    # contains the full command line.  LTVM/QEMU paths do not contain spaces,
    # so selecting the first argv-like field is conservative and avoids
    # accepting a later argument merely containing "qemu-system".
    fields = command.strip().split("\0", 1)[0].split(None, 1)
    if not fields:
        return False
    first_field = fields[0]
    executable = Path(first_field).name.casefold()
    return executable.startswith(_QEMU_NAMES)


def _command_argv(command: str) -> list[str]:
    if "\0" in command:
        return [part for part in command.split("\0") if part]
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _qemu_command_matches_vm(command: str, vm_name: str) -> bool:
    if not _is_qemu_command(command):
        return False
    argv = _command_argv(command)
    for index, argument in enumerate(argv[:-1]):
        if argument == "-name":
            # QEMU also permits comma-separated name options.  LTVM passes
            # the plain VM name, but accepting that documented spelling keeps
            # discovery compatible without weakening the exact name check.
            return argv[index + 1].split(",", 1)[0] == vm_name
    return False


def _linux_process_rss(
    pid: int,
    *,
    vm_name: str,
    reader: Reader,
) -> tuple[int | None, str | None, CollectionError | None]:
    scope = f"ltvm_vm:{pid}"
    try:
        command = reader(f"/proc/{pid}/comm").strip()
    except (OSError, UnicodeError) as exc:
        return None, None, CollectionError(
            scope, "process_identity_unavailable", str(exc)[:200]
        )
    if not _is_qemu_command(command):
        return None, None, CollectionError(
            scope,
            "process_identity_mismatch",
            f"PID {pid} is not a recognized QEMU process",
        )
    try:
        command_line = reader(f"/proc/{pid}/cmdline")
    except (OSError, UnicodeError) as exc:
        return None, None, CollectionError(
            scope, "process_identity_unavailable", str(exc)[:200]
        )
    if not _qemu_command_matches_vm(command_line, vm_name):
        return None, None, CollectionError(
            scope,
            "process_identity_mismatch",
            f"PID {pid} is not QEMU for VM {vm_name!r}",
        )
    try:
        status = reader(f"/proc/{pid}/status")
    except (OSError, UnicodeError) as exc:
        return None, None, CollectionError(
            scope, "process_memory_unavailable", str(exc)[:200]
        )
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            match = re.fullmatch(r"VmRSS:\s*(\d+)\s*kB\s*", line)
            if match:
                return int(match.group(1)) * 1024, f"/proc/{pid}/status VmRSS", None
            break
    return None, None, CollectionError(
        scope, "process_memory_invalid", f"PID {pid} has no valid VmRSS"
    )


def _ps_process_rss(
    pid: int,
    *,
    vm_name: str,
    runner: Runner,
    timeout: float,
) -> tuple[int | None, str | None, CollectionError | None]:
    scope = f"ltvm_vm:{pid}"
    output, error = _read_command(
        runner,
        # macOS truncates ``command`` when it is not the final output column.
        # Keeping it last retains argv[0], QEMU's ``-name``, and the VM name.
        ("ps", "-p", str(pid), "-o", "rss=", "-o", "command="),
        scope=scope,
        code="process_memory_unavailable",
        timeout=timeout,
    )
    if error:
        return None, None, error
    fields = (output or "").strip().split(None, 1)
    if len(fields) != 2 or not _qemu_command_matches_vm(fields[1], vm_name):
        return None, None, CollectionError(
            scope,
            "process_identity_mismatch",
            f"PID {pid} is not a recognized QEMU process",
        )
    try:
        rss = int(fields[0]) * 1024
    except ValueError:
        return None, None, CollectionError(
            scope, "process_memory_invalid", f"PID {pid} has an invalid RSS value"
        )
    return rss, "ps rss", None


def _process_rss(
    pid: int,
    *,
    vm_name: str,
    system: str,
    runner: Runner,
    reader: Reader,
    timeout: float,
) -> tuple[int | None, str | None, CollectionError | None]:
    if system == "linux":
        return _linux_process_rss(pid, vm_name=vm_name, reader=reader)
    return _ps_process_rss(
        pid, vm_name=vm_name, runner=runner, timeout=timeout
    )


def _session_id(owner_id: str | None) -> str | None:
    if owner_id and owner_id.startswith(_PATCH_WATCHER_OWNER_PREFIX):
        session_id = owner_id[len(_PATCH_WATCHER_OWNER_PREFIX) :]
        return session_id or None
    return None


def _parse_vm_row(
    row: Mapping[str, Any],
    *,
    index: int,
    system: str,
    runner: Runner,
    reader: Reader,
    timeout: float,
) -> LTVMVMStatus:
    name_value = row.get("name")
    if not isinstance(name_value, str) or not name_value.strip():
        raise ValueError(f"VM row {index} has no valid name")
    name = name_value.strip()
    scope = f"ltvm_vm:{name}"
    errors: list[CollectionError] = []

    raw_owner = row.get("owner_id", row.get("owner"))
    if raw_owner is None:
        owner_id = None
    elif isinstance(raw_owner, str):
        owner_id = raw_owner or None
    else:
        owner_id = None
        errors.append(
            CollectionError(scope, "invalid_owner_id", "owner_id was not a string")
        )

    state = _normalize_state(row.get("status", row.get("state")))
    if state == "unknown":
        errors.append(
            CollectionError(scope, "missing_vm_state", "VM state was unavailable")
        )
    configured = _memory_bytes(row)
    if configured is None and _has_memory_field(row):
        errors.append(
            CollectionError(
                scope, "invalid_configured_memory", "configured guest memory was invalid"
            )
        )
    elif configured is None:
        errors.append(
            CollectionError(
                scope,
                "missing_configured_memory",
                "configured guest memory was unavailable",
            )
        )

    raw_pid = row.get("pid", row.get("process_id", row.get("qemu_pid")))
    process_id = _integer(raw_pid)
    if process_id == 0:
        process_id = None
    if raw_pid not in (None, 0, "0") and process_id is None:
        errors.append(CollectionError(scope, "invalid_pid", "VM PID was invalid"))

    host_rss = None
    memory_source = None
    if state == "running":
        if process_id is None:
            errors.append(
                CollectionError(scope, "missing_pid", "running VM has no usable PID")
            )
        else:
            host_rss, memory_source, error = _process_rss(
                process_id,
                vm_name=name,
                system=system,
                runner=runner,
                reader=reader,
                timeout=timeout,
            )
            if error:
                errors.append(
                    CollectionError(scope, error.code, error.message)
                )

    vcpus = _integer(row.get("vcpus", row.get("cpus")))
    raw_ip = row.get("ip", row.get("address"))
    ip = raw_ip.strip() if isinstance(raw_ip, str) and raw_ip.strip() else None
    return LTVMVMStatus(
        name=name,
        state=state,
        owner_id=owner_id,
        patch_watcher_session_id=_session_id(owner_id),
        configured_guest_memory_bytes=configured,
        host_rss_bytes=host_rss,
        process_id=process_id,
        vcpus=vcpus,
        ip=ip,
        host_memory_source=memory_source,
        quality="partial" if errors else "good",
        errors=tuple(errors),
    )


def collect_ltvm_inventory(
    *,
    system: str | None = None,
    runner: Runner = subprocess.run,
    reader: Reader = _default_reader,
    clock: Clock = _utc_now,
    timeout: float = 10.0,
) -> LTVMInventory:
    """Inventory every LTVM VM, returning partial data instead of raising."""

    sampled_at = _sample_time(clock)
    source = "ltvm list --json"
    command = ("ltvm", "list", "--json")
    output, error = _read_command(
        runner,
        command,
        scope="ltvm",
        code="ltvm_list_failed",
        timeout=timeout,
    )
    if error:
        return LTVMInventory(
            sampled_at, source, "unavailable", errors=(error,)
        )
    try:
        payload = json.loads(output or "")
    except json.JSONDecodeError as exc:
        error = CollectionError(
            "ltvm", "invalid_json", f"ltvm returned invalid JSON: {exc.msg}"
        )
        return LTVMInventory(
            sampled_at, source, "unavailable", errors=(error,)
        )
    try:
        rows = _extract_vm_rows(payload)
    except ValueError as exc:
        error = CollectionError("ltvm", "invalid_payload", str(exc))
        return LTVMInventory(
            sampled_at, source, "unavailable", errors=(error,)
        )

    os_name = (system or platform.system()).casefold()
    vms: list[LTVMVMStatus] = []
    errors: list[CollectionError] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            errors.append(
                CollectionError(
                    "ltvm",
                    "invalid_vm_row",
                    f"VM row {index} was not an object and was skipped",
                )
            )
            continue
        try:
            vm = _parse_vm_row(
                row,
                index=index,
                system=os_name,
                runner=runner,
                reader=reader,
                timeout=timeout,
            )
        except (TypeError, ValueError) as exc:
            errors.append(CollectionError("ltvm", "invalid_vm_row", str(exc)[:200]))
            continue
        vms.append(vm)
        errors.extend(vm.errors)

    return LTVMInventory(
        sampled_at=sampled_at,
        source=source,
        quality="partial" if errors else "good",
        vms=tuple(vms),
        errors=tuple(errors),
    )


def _expected_process_matches(command: str, expected_command: str) -> bool:
    """Match a persisted expected executable/command without regex semantics."""

    expected = expected_command.strip()
    if not expected:
        return False
    argv = _command_argv(command)
    if not argv:
        return False
    if any(character.isspace() for character in expected):
        return command == expected or command.startswith(expected + " ")
    if "/" in expected:
        return argv[0] == expected
    return any(Path(argument).name == expected for argument in argv)


def _parse_ps_process_table(
    text: str,
) -> tuple[dict[int, tuple[int, int | None, str]], list[CollectionError], bool]:
    rows: dict[int, tuple[int, int | None, str]] = {}
    errors: list[CollectionError] = []
    incomplete = False
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split(None, 3)
        if len(fields) < 3:
            incomplete = True
            errors.append(
                CollectionError(
                    "process_tree",
                    "malformed_process_row",
                    f"could not parse process-table line {line_number}",
                )
            )
            continue
        try:
            process_id = int(fields[0])
            parent_id = int(fields[1])
        except ValueError:
            incomplete = True
            errors.append(
                CollectionError(
                    "process_tree",
                    "malformed_process_row",
                    f"invalid PID on process-table line {line_number}",
                )
            )
            continue
        if process_id <= 0 or parent_id < 0:
            incomplete = True
            errors.append(
                CollectionError(
                    "process_tree",
                    "malformed_process_row",
                    f"out-of-range PID on process-table line {line_number}",
                )
            )
            continue
        rss_kib: int | None
        try:
            rss_kib = int(fields[2])
            if rss_kib < 0:
                raise ValueError
        except ValueError:
            rss_kib = None
            errors.append(
                CollectionError(
                    f"process:{process_id}",
                    "invalid_process_rss",
                    f"invalid RSS on process-table line {line_number}",
                )
            )
        command = fields[3] if len(fields) == 4 else ""
        if process_id in rows:
            errors.append(
                CollectionError(
                    f"process:{process_id}",
                    "duplicate_process_row",
                    f"PID {process_id} appeared more than once and was counted once",
                )
            )
            continue
        rows[process_id] = (parent_id, rss_kib, command)
    return rows, errors, incomplete


def collect_process_tree_rss(
    pid: int,
    *,
    expected_command: str | None = None,
    runner: Runner = subprocess.run,
    clock: Clock = _utc_now,
    timeout: float = 5.0,
) -> ProcessTreeMemoryStatus:
    """Measure a managed process tree from one bounded ``ps`` snapshot.

    ``expected_command`` should be the persisted executable path, basename, or
    full command captured when the managed session was launched.  A mismatch
    is treated as PID reuse and no memory is attributed.  Omitting it still
    yields a useful subtotal, but quality is ``partial`` because identity was
    not independently verified.

    The process table is snapshotted once, descendants are traversed by PPID,
    and a visited set ensures that duplicate rows or corrupt cycles cannot
    double-count a PID.
    """

    sampled_at = _sample_time(clock)
    source = "ps pid/ppid/rss/command (RSS sum; shared pages may overlap)"
    root_pid = _integer(pid)
    if root_pid is None or root_pid == 0:
        error = CollectionError(
            "process_tree", "invalid_root_pid", "root PID must be a positive integer"
        )
        return ProcessTreeMemoryStatus(
            sampled_at, source, "unavailable", int(pid) if isinstance(pid, int) else 0,
            errors=(error,),
        )

    output, error = _read_command(
        runner,
        ("ps", "-axo", "pid=,ppid=,rss=,command="),
        scope="process_tree",
        code="process_table_failed",
        timeout=timeout,
    )
    if error:
        return ProcessTreeMemoryStatus(
            sampled_at, source, "unavailable", root_pid, errors=(error,)
        )

    rows, errors, table_incomplete = _parse_ps_process_table(output or "")
    root = rows.get(root_pid)
    if root is None:
        errors.append(
            CollectionError(
                "process_tree",
                "root_process_not_found",
                f"root PID {root_pid} was not present in the process snapshot",
            )
        )
        return ProcessTreeMemoryStatus(
            sampled_at, source, "unavailable", root_pid, errors=tuple(errors)
        )

    root_command = root[2]
    if expected_command is not None:
        if not _expected_process_matches(root_command, expected_command):
            errors.append(
                CollectionError(
                    "process_tree",
                    "root_identity_mismatch",
                    f"PID {root_pid} no longer matches the managed process identity",
                )
            )
            return ProcessTreeMemoryStatus(
                sampled_at,
                source,
                "unavailable",
                root_pid,
                root_command=root_command,
                errors=tuple(errors),
            )
    else:
        errors.append(
            CollectionError(
                "process_tree",
                "root_identity_unverified",
                "no expected command was supplied for PID-reuse protection",
            )
        )

    children: dict[int, list[int]] = {}
    for process_id, (parent_id, _rss, _command) in rows.items():
        children.setdefault(parent_id, []).append(process_id)

    visited: set[int] = set()
    pending = [root_pid]
    ordered: list[int] = []
    while pending:
        process_id = pending.pop()
        if process_id in visited:
            continue
        visited.add(process_id)
        ordered.append(process_id)
        pending.extend(reversed(children.get(process_id, ())))

    rss_values = [rows[process_id][1] for process_id in ordered]
    known_rss = sum(value or 0 for value in rss_values) * 1024
    measured_count = sum(value is not None for value in rss_values)
    complete = measured_count == len(ordered) and not table_incomplete
    total_rss = known_rss if complete else None
    quality = "estimated" if complete and not errors else "partial"
    return ProcessTreeMemoryStatus(
        sampled_at=sampled_at,
        source=source,
        quality=quality,
        root_pid=root_pid,
        root_command=root_command,
        total_rss_bytes=total_rss,
        known_rss_bytes=known_rss,
        process_count=len(ordered),
        measured_process_count=measured_count,
        process_ids=tuple(ordered),
        errors=tuple(errors),
    )


def collect_resource_snapshot(
    *,
    system: str | None = None,
    runner: Runner = subprocess.run,
    reader: Reader = _default_reader,
    clock: Clock = _utc_now,
    host_timeout: float = 5.0,
    ltvm_timeout: float = 10.0,
) -> ResourceSnapshot:
    """Collect a timestamp-aligned host/LTVM snapshot suitable for JSON UI APIs."""

    sampled_at = _sample_time(clock)
    fixed_clock = lambda: sampled_at
    host_memory = collect_host_memory(
        system=system,
        runner=runner,
        reader=reader,
        clock=fixed_clock,
        timeout=host_timeout,
    )
    ltvm = collect_ltvm_inventory(
        system=system,
        runner=runner,
        reader=reader,
        clock=fixed_clock,
        timeout=ltvm_timeout,
    )
    errors = host_memory.errors + ltvm.errors
    if host_memory.quality == "unavailable" and ltvm.quality == "unavailable":
        quality = "unavailable"
    elif errors:
        quality = "partial"
    elif host_memory.quality == "estimated":
        quality = "estimated"
    else:
        quality = "good"
    return ResourceSnapshot(
        sampled_at=sampled_at,
        source="host OS; ltvm list --json",
        quality=quality,
        host_memory=host_memory,
        ltvm=ltvm,
        errors=errors,
    )
