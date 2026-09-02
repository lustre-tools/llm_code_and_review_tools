#!/usr/bin/env python3
"""Private MCP bridge for one Patch Watcher engineering session.

The bridge exposes LTVM lifecycle operations plus open-ended command execution
*inside* exact-owner guests.  It never exposes a host shell.  The context file
is controller-written, private, and immutably binds the bridge to one
session/run/revision/checkout/validation attempt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from engineering_state import EngineeringStateStore
from ltvm_guest_exec import (
    EngineeringExecutionAuthorization,
    GuestCommand,
    GuestExecutionPolicy,
    GuestExecutionRequest,
    GuestTarget,
    LTVMGuestExecutionBroker,
)
from ltvm_resources import (
    CleanupAction,
    LTVMAdapter,
    LTVMCommandError,
    LTVMInventory,
    owner_id_for_session,
)
from ltvm_ssh_transport import (
    GuestIsolationAssertion,
    LTVMSSHGuestTransport,
    confined_ssh_options,
    run_bounded_process,
    verified_vm_address,
)


CONTEXT_SCHEMA = "patch-watcher-ltvm-mcp-context/v1"
AUDIT_SCHEMA = "patch-watcher-ltvm-mcp-audit/v1"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_DISK = re.compile(r"^[1-9][0-9]{0,4}[MG]$")
_CAPACITY_TERMS = (
    "not enough memory", "insufficient memory", "out of memory",
    "no space left", "address exhausted", "no addresses",
    "resource exhausted", "resource limit", "capacity",
)
_MAX_RESULT_BYTES = 1_048_576
_MAX_INVENTORY_BYTES = 8 * 1_048_576
_MAX_SOURCE_LIST_BYTES = 8 * 1_048_576
_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_REQUEST_BYTES = 1_048_576


class MCPServiceError(RuntimeError):
    """A tool call was refused or failed."""


def _safe(label: str, value: object) -> str:
    result = str(value).strip()
    if not _SAFE.fullmatch(result):
        raise MCPServiceError(f"{label} is invalid")
    return result


def _bounded(value: object, limit: int = 16_384) -> str:
    raw = str(value).replace("\x00", "").encode("utf-8", "replace")
    return raw[:limit].decode("utf-8", "ignore")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LTVMMCPContext:
    session_id: str
    run_id: str
    revision_sha: str
    owner_id: str
    checkout_path: Path
    checkout_root: Path
    engineering_database: Path
    execution_id: str
    attempt_id: str
    worker_id: str
    audit_path: Path
    name_prefix: str
    max_owned_vms: int = 12
    max_vm_memory_mib: int = 65_536
    max_vm_vcpus: int = 32

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LTVMMCPContext":
        if value.get("schema") != CONTEXT_SCHEMA:
            raise ValueError("unsupported LTVM MCP context schema")
        session_id = str(value.get("session_id", ""))
        uuid.UUID(session_id)
        run_id = str(value.get("run_id", ""))
        revision = str(value.get("revision_sha", ""))
        if not _RUN.fullmatch(run_id) or not _REVISION.fullmatch(revision):
            raise ValueError("invalid run or revision identity")
        owner_id = str(value.get("owner_id", ""))
        if owner_id != owner_id_for_session(session_id):
            raise ValueError("context owner does not match session")
        checkout_root = Path(str(value.get("checkout_root", ""))).expanduser().resolve()
        checkout_path = Path(str(value.get("checkout_path", ""))).expanduser().resolve()
        if (
            not checkout_root.is_absolute()
            or checkout_path.parent != checkout_root
            or checkout_path.name != run_id
            or not checkout_path.is_dir()
        ):
            raise ValueError("context checkout is outside the exact run allocation")
        database = Path(str(value.get("engineering_database", ""))).expanduser().resolve()
        audit_path = Path(str(value.get("audit_path", ""))).expanduser().resolve()
        if not database.is_absolute() or not audit_path.is_absolute():
            raise ValueError("context state paths must be absolute")
        identifiers = {
            name: str(value.get(name, ""))
            for name in ("execution_id", "attempt_id", "worker_id")
        }
        if any(not _RUN.fullmatch(item) for item in identifiers.values()):
            raise ValueError("invalid execution identity")
        prefix = str(value.get("name_prefix", ""))
        if not _SAFE.fullmatch(prefix) or len(prefix) > 48:
            raise ValueError("invalid LTVM name prefix")
        limits = {}
        for name, default, maximum in (
            ("max_owned_vms", 12, 64),
            ("max_vm_memory_mib", 65_536, 262_144),
            ("max_vm_vcpus", 32, 256),
        ):
            item = value.get(name, default)
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= maximum:
                raise ValueError(f"{name} is invalid")
            limits[name] = item
        return cls(
            session_id=session_id, run_id=run_id, revision_sha=revision,
            owner_id=owner_id, checkout_path=checkout_path,
            checkout_root=checkout_root, engineering_database=database,
            execution_id=identifiers["execution_id"],
            attempt_id=identifiers["attempt_id"], worker_id=identifiers["worker_id"],
            audit_path=audit_path, name_prefix=prefix, **limits,
        )

    @classmethod
    def load(cls, path: str | Path) -> "LTVMMCPContext":
        context_path = Path(path).expanduser().resolve()
        stat = context_path.stat()
        if stat.st_mode & 0o077:
            raise ValueError("LTVM MCP context must not be group/world accessible")
        value = json.loads(context_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("LTVM MCP context must be an object")
        return cls.from_mapping(value)


class StateRunAuthorizer:
    """Adapt the durable validation ledger to the guest broker contract."""

    def __init__(self, store: EngineeringStateStore, context: LTVMMCPContext):
        self.store = store
        self.context = context

    def authorization_for(
        self, *, session_id: str, run_id: str, revision_sha: str
    ) -> EngineeringExecutionAuthorization | None:
        try:
            execution = self.store.get_active_validation_capability(
                session_id=session_id, run_id=run_id,
                revision_sha=revision_sha,
            )
            attempt = self.store.get_validation_attempt(self.context.attempt_id)
        except Exception:
            return None
        exact = execution is not None and (
            execution.execution_id == self.context.execution_id
            and
            execution.run_id == run_id == self.context.run_id
            and execution.session_id == session_id == self.context.session_id
            and execution.revision_sha == revision_sha == self.context.revision_sha
            and execution.owner_id == self.context.owner_id
            and execution.admission_state == "approved"
            and attempt.execution_id == execution.execution_id
            and attempt.worker_id == self.context.worker_id
            and attempt.state == "running"
        )
        if not exact:
            return None
        return EngineeringExecutionAuthorization(session_id, run_id, revision_sha)


HostRunner = Callable[..., Any]


class SessionLTVMService:
    """Tool implementation; dependency-injected so tests never create VMs."""

    def __init__(
        self,
        context: LTVMMCPContext,
        *,
        store: EngineeringStateStore | None = None,
        adapter: LTVMAdapter | None = None,
        broker: LTVMGuestExecutionBroker | None = None,
        runner: HostRunner = run_bounded_process,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        self.context = context
        self.runner = runner
        self.cancelled = cancelled
        self.store = store or EngineeringStateStore(
            context.engineering_database, checkout_root=context.checkout_root
        )
        self.adapter = adapter or LTVMAdapter(runner=self._adapter_runner, timeout=30)
        authorizer = StateRunAuthorizer(self.store, context)
        self.broker = broker or LTVMGuestExecutionBroker(
            inventory_provider=self.adapter,
            authorizer=authorizer,
            transport=LTVMSSHGuestTransport(
                inventory_provider=self.adapter,
                isolation=GuestIsolationAssertion(True, True),
                ssh_binary="/usr/bin/ssh",
                local_path="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            ),
            policy=GuestExecutionPolicy(
                max_commands=64, max_step_seconds=7_200,
                max_total_seconds=43_200, max_output_bytes=_MAX_RESULT_BYTES,
            ),
        )

    @staticmethod
    def _controller_environment(owner_id: str) -> Mapping[str, str]:
        return {
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C", "LC_ALL": "C", "LTVM_OWNER_ID": owner_id,
        }

    def _adapter_runner(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        # The adapter may perform a final inventory read and then a destroy;
        # revalidate the capability before each of those concrete subprocesses.
        self._authorization()
        timeout = max(1, int(kwargs.get("timeout", 30)))
        result = self.runner(
            tuple(argv), timeout_seconds=timeout,
            max_output_bytes=_MAX_INVENTORY_BYTES,
            cancelled=self.cancelled,
            environment=self._controller_environment(self.context.owner_id),
        )
        if result.timed_out:
            raise subprocess.TimeoutExpired(list(argv), timeout)
        return subprocess.CompletedProcess(
            list(argv), result.exit_code if result.exit_code is not None else 1,
            result.stdout, result.stderr,
        )

    def _authorization(self) -> None:
        authorizer = StateRunAuthorizer(self.store, self.context)
        if authorizer.authorization_for(
            session_id=self.context.session_id,
            run_id=self.context.run_id,
            revision_sha=self.context.revision_sha,
        ) is None:
            raise MCPServiceError("engineering guest capability is not active")

    def _inventory(self) -> LTVMInventory:
        try:
            return self.adapter.inventory()
        except Exception as exc:
            raise MCPServiceError("LTVM inventory is unavailable") from exc

    def _owned_vm(self, name: object):
        actual = _safe("VM name", name)
        matches = self._inventory().named_vms(actual)
        if len(matches) != 1 or matches[0].owner_id != self.context.owner_id:
            raise MCPServiceError("VM is not uniquely owned by this session")
        return matches[0]

    def _actual_name(self, suffix: object) -> str:
        item = _safe("name suffix", suffix)
        actual = f"{self.context.name_prefix}-{item}"
        if len(actual) > 128 or not _SAFE.fullmatch(actual):
            raise MCPServiceError("derived LTVM name is invalid")
        return actual

    def _invoke(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        max_output_bytes: int = _MAX_RESULT_BYTES,
        pass_fds: Sequence[int] = (),
    ) -> Any:
        self._authorization()
        try:
            result = self.runner(
                tuple(argv), timeout_seconds=timeout,
                max_output_bytes=max_output_bytes,
                cancelled=self.cancelled,
                environment=self._controller_environment(self.context.owner_id),
                pass_fds=pass_fds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MCPServiceError(f"controller operation failed: {type(exc).__name__}") from exc
        if not all(hasattr(result, name) for name in (
            "exit_code", "stdout", "stderr", "timed_out", "cancelled",
            "stdout_observed_bytes", "stderr_observed_bytes",
        )):
            raise MCPServiceError("controller runner returned an invalid result")
        return result

    def _claim_command(self, command: GuestCommand) -> None:
        """Durably reserve an immutable command before any dispatch."""

        self._authorization()
        try:
            claim = self.store.claim_validation_command(
                self.context.attempt_id,
                worker_id=self.context.worker_id,
                command=command,
            )
        except Exception as exc:
            raise MCPServiceError("command authorization could not be reserved") from exc
        if not bool(getattr(claim, "should_dispatch", False)):
            disposition = _bounded(getattr(claim, "disposition", "denied"), 100)
            self._audit(
                "dispatch_suppressed",
                {"command_id": command.command_id, "disposition": disposition},
            )
            raise MCPServiceError(
                f"command was not dispatched because its claim is {disposition}"
            )

    def _record_command_result(
        self,
        command: GuestCommand,
        *,
        state: str,
        summary: str,
        exit_code: int | None,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        try:
            self.store.record_validation_step_result(
                self.context.attempt_id,
                worker_id=self.context.worker_id,
                command=command,
                state=state,
                summary=summary,
                exit_code=exit_code,
                artifact_ids=(),
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception as exc:
            raise MCPServiceError("command result could not be durably recorded") from exc

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        command_id: str | None = None,
        max_output_bytes: int = _MAX_RESULT_BYTES,
        pass_fds: Sequence[int] = (),
    ) -> Mapping[str, Any]:
        command = GuestCommand(
            command_id=command_id or ("host-" + uuid.uuid4().hex[:20]),
            argv=tuple(argv),
            cwd="/",
            timeout_seconds=timeout,
        )
        self._claim_command(command)
        started_at = _utc_now()
        result = self._invoke(
            argv, timeout=timeout, max_output_bytes=max_output_bytes,
            pass_fds=pass_fds,
        )
        finished_at = _utc_now()
        stdout_bytes = bytes(result.stdout or b"")
        stderr_bytes = bytes(result.stderr or b"")
        stdout_observed = (
            len(stdout_bytes)
            if result.stdout_observed_bytes is None
            else int(result.stdout_observed_bytes)
        )
        stderr_observed = (
            len(stderr_bytes)
            if result.stderr_observed_bytes is None
            else int(result.stderr_observed_bytes)
        )
        stdout = stdout_bytes.decode("utf-8", "replace")
        stderr = stderr_bytes.decode("utf-8", "replace")
        record = {
            "command_id": command.command_id,
            "argv": list(argv),
            "returncode": result.exit_code,
            "stdout": stdout, "stderr": stderr,
            "stdout_observed_bytes": stdout_observed,
            "stderr_observed_bytes": stderr_observed,
            "output_truncated": (
                stdout_observed > len(stdout_bytes)
                or stderr_observed > len(stderr_bytes)
            ),
        }
        if result.cancelled or self.cancelled():
            state, summary = "cancelled", "controller_operation_cancelled"
        elif result.timed_out:
            state, summary = "failed", "controller_operation_timed_out"
        elif result.exit_code != 0:
            evidence = (stderr or stdout).casefold()
            if any(term in evidence for term in _CAPACITY_TERMS):
                record["error_code"] = "ltvm_resource_exhausted"
                state, summary = "resource_exhausted", "ltvm_resource_exhausted"
            else:
                state, summary = "failed", "controller_operation_failed"
        else:
            state, summary = "succeeded", "controller_operation_succeeded"

        self._record_command_result(
            command,
            state=state,
            summary=summary,
            exit_code=result.exit_code,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._audit("controller_operation", record)
        if state != "succeeded":
            if state == "cancelled":
                raise MCPServiceError("controller operation was cancelled")
            if result.timed_out:
                raise MCPServiceError("controller operation timed out")
            raise MCPServiceError(
                "LTVM operation failed" + (
                    " (resource exhausted)" if record.get("error_code") else ""
                ) + f": {_bounded(stderr or stdout, 2000)}"
            )
        return record

    def _audit(self, kind: str, payload: Mapping[str, Any]) -> None:
        record = {
            "schema": AUDIT_SCHEMA, "at": _utc_now().isoformat(),
            "session_id": self.context.session_id, "run_id": self.context.run_id,
            "revision_sha": self.context.revision_sha,
            "execution_id": self.context.execution_id,
            "attempt_id": self.context.attempt_id, "kind": kind,
            "payload": payload,
        }
        encoded = _json(record) + "\n"
        path = self.context.audit_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise MCPServiceError("audit destination is not a regular file")
            remaining = memoryview(encoded.encode("utf-8"))
            while remaining:
                written = os.write(descriptor, remaining)
                if written < 1:
                    raise MCPServiceError("audit record could not be fully written")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def list(self, _: Mapping[str, Any]) -> Mapping[str, Any]:
        self._authorization()
        owned = self._inventory().vms_owned_by(self.context.owner_id)
        return {
            "owner_id": self.context.owner_id,
            "vms": [
                {
                    "name": vm.name, "state": vm.state,
                    "memory_bytes": vm.configured_guest_memory_bytes,
                    "vcpus": vm.vcpus, "ip": vm.raw.get("ip"),
                }
                for vm in owned
            ],
        }

    def target_list(self, _: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._run(("/usr/local/bin/ltvm", "target", "list", "--all-arches", "--json"), timeout=60)

    def target_fetch(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        target = _safe("target", arguments.get("target"))
        argv = ["/usr/local/bin/ltvm", "target", "fetch", target, "--json"]
        if arguments.get("arch"):
            argv.extend(("--arch", _safe("arch", arguments["arch"])))
        if arguments.get("variant"):
            argv.extend(("--variant", _safe("variant", arguments["variant"])))
        return self._run(argv, timeout=3_600)

    def _capacity(self, additional: int) -> None:
        count = len(self._inventory().vms_owned_by(self.context.owner_id))
        if count + additional > self.context.max_owned_vms:
            raise MCPServiceError("session LTVM VM limit would be exceeded")

    def create(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self._capacity(1)
        name = self._actual_name(arguments.get("name"))
        target = _safe("target", arguments.get("target"))
        memory = int(arguments.get("memory_mib", 2048))
        vcpus = int(arguments.get("vcpus", 2))
        if not 256 <= memory <= self.context.max_vm_memory_mib:
            raise MCPServiceError("memory_mib is outside the session bound")
        if not 1 <= vcpus <= self.context.max_vm_vcpus:
            raise MCPServiceError("vcpus is outside the session bound")
        argv = [
            "/usr/local/bin/ltvm", "create", name, target, "--json",
            "--owner", self.context.owner_id, "--mem", str(memory),
            "--vcpus", str(vcpus),
        ]
        for key, flag in (("arch", "--arch"), ("variant", "--variant")):
            if arguments.get(key):
                argv.extend((flag, _safe(key, arguments[key])))
        if arguments.get("disk_size"):
            disk = str(arguments["disk_size"])
            if not _DISK.fullmatch(disk):
                raise MCPServiceError("disk_size must use M or G")
            argv.extend(("--disk-size", disk))
        self._run(
            argv,
            timeout=600,
            command_id="create-" + uuid.uuid4().hex[:16],
        )
        vm = self._owned_vm(name)
        return {"name": name, "state": vm.state, "owner_id": vm.owner_id, "ip": vm.raw.get("ip")}

    def cluster_create(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raw_nodes = arguments.get("nodes")
        if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= 12:
            raise MCPServiceError("nodes must contain 1..12 node definitions")
        self._capacity(len(raw_nodes))
        cluster = self._actual_name(arguments.get("name"))
        target = _safe("target", arguments.get("target"))
        memory = int(arguments.get("memory_mib", 2048))
        vcpus = int(arguments.get("vcpus", 2))
        if not 256 <= memory <= self.context.max_vm_memory_mib or not 1 <= vcpus <= self.context.max_vm_vcpus:
            raise MCPServiceError("cluster memory or vCPU request is outside the session bound")
        specs = []
        members = []
        for raw in raw_nodes:
            if not isinstance(raw, Mapping):
                raise MCPServiceError("cluster node must be an object")
            roles = raw.get("roles")
            if not isinstance(roles, list) or not roles or any(
                role not in {"mgs", "mds", "oss", "client"} for role in roles
            ):
                raise MCPServiceError("cluster node roles are invalid")
            name = self._actual_name(raw.get("name"))
            disks = int(raw.get("disks", 0))
            if not 0 <= disks <= 64:
                raise MCPServiceError("cluster disk count is invalid")
            specs.append("+".join(dict.fromkeys(roles)) + f":{name}:{disks}")
            members.append(name)
        argv = [
            "/usr/bin/sudo", "-n", "/usr/local/bin/ltvm", "cluster", "--json",
            "create", cluster, "--target", target, "--owner-id", self.context.owner_id,
            "--mem", str(memory), "--vcpus", str(vcpus), *specs,
        ]
        if arguments.get("arch"):
            argv[-len(specs):-len(specs)] = [
                "--arch", _safe("arch", arguments["arch"])
            ]
        self._run(
            argv,
            timeout=900,
            command_id="cluster-create-" + uuid.uuid4().hex[:16],
        )
        for member in members:
            self._owned_vm(member)
        return {"name": cluster, "owner_id": self.context.owner_id, "members": members}

    def _source_paths(self) -> tuple[str, ...]:
        record = self._run(
            (
                "/usr/bin/git", "-C", str(self.context.checkout_path),
                "-c", "core.quotepath=false", "ls-files", "-z", "--cached",
                "--others", "--exclude-standard",
            ),
            timeout=60,
            max_output_bytes=_MAX_SOURCE_LIST_BYTES,
        )
        encoded_stdout = str(record["stdout"]).encode("utf-8")
        if record["stdout_observed_bytes"] > len(encoded_stdout):
            raise MCPServiceError("source path list exceeds its controller bound")
        paths: list[str] = []
        for raw in encoded_stdout.split(b"\0"):
            if not raw:
                continue
            try:
                relative = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MCPServiceError("source path is not valid UTF-8") from exc
            path = Path(relative)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", "..", ".git"} for part in path.parts)
                or "\x00" in relative
            ):
                raise MCPServiceError("source path escapes the confined worktree")
            paths.append(path.as_posix())
        if len(paths) > 100_000:
            raise MCPServiceError("source worktree contains too many files")
        return tuple(dict.fromkeys(paths))

    def _open_confined_source(
        self, relative: str
    ) -> tuple[int, os.stat_result] | None:
        parts = Path(relative).parts
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        root_fd = os.open(self.context.checkout_path, directory_flags)
        current_fd = root_fd
        try:
            for component in parts[:-1]:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(file_fd)
                raise MCPServiceError("source transfer refuses symlinks and non-regular files")
            return file_fd, metadata
        except FileNotFoundError:
            # The index may still name a legitimately deleted tracked path.
            # A concurrent deletion is equally safe: nothing is transferred.
            return None
        except (OSError, ValueError) as exc:
            raise MCPServiceError(
                "source transfer refuses symlinks, races, and path escapes"
            ) from exc
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)

    def _build_source_archive(self) -> tuple[Path, int, int, str]:
        paths = self._source_paths()
        descriptor, archive_name = tempfile.mkstemp(
            prefix=".pw-source-", suffix=".tar", dir=str(self.context.checkout_root)
        )
        os.fchmod(descriptor, 0o600)
        archive_path = Path(archive_name)
        total = 0
        added_files = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(os.dup(descriptor), "wb", closefd=True) as archive_file:
                archive = tarfile.open(
                    fileobj=archive_file, mode="w", format=tarfile.PAX_FORMAT
                )
                try:
                    added_directories: set[str] = set()
                    for relative in paths:
                        opened = self._open_confined_source(relative)
                        if opened is None:
                            continue
                        file_fd, metadata = opened
                        added_files += 1
                        total += int(metadata.st_size)
                        if total > _MAX_SOURCE_BYTES:
                            os.close(file_fd)
                            raise MCPServiceError("source worktree exceeds the transfer bound")
                        parent = Path(relative).parent
                        parents = [] if parent == Path(".") else list(reversed(parent.parents)) + [parent]
                        for directory in parents:
                            name = directory.as_posix()
                            if name == "." or name in added_directories:
                                continue
                            info = tarfile.TarInfo(name)
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            info.mtime = 0
                            info.uid = info.gid = 0
                            info.uname = info.gname = ""
                            archive.addfile(info)
                            added_directories.add(name)
                        info = tarfile.TarInfo(relative)
                        info.size = int(metadata.st_size)
                        info.mode = stat.S_IMODE(metadata.st_mode) & 0o777
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        with os.fdopen(file_fd, "rb", closefd=True) as source:
                            archive.addfile(info, source)
                finally:
                    archive.close()
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
                while True:
                    chunk = handle.read(1_048_576)
                    if not chunk:
                        break
                    digest.update(chunk)
            self._audit(
                "source_archive",
                {
                    "file_count": added_files, "source_bytes": total,
                    "archive_bytes": os.fstat(descriptor).st_size,
                    "sha256": digest.hexdigest(),
                },
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            return archive_path, descriptor, added_files, digest.hexdigest()
        except Exception:
            os.close(descriptor)
            archive_path.unlink(missing_ok=True)
            raise

    def push_source(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        requested_vm = _safe("VM name", arguments.get("vm_name"))
        destination = f"/root/patch-watcher/{self.context.run_id}/source"
        remote_archive = f"/root/patch-watcher/{self.context.run_id}/source.tar"
        archive_path, archive_fd, file_count, archive_sha256 = self._build_source_archive()
        prepare = GuestCommand(
            command_id="prepare-source-" + uuid.uuid4().hex[:12],
            argv=(
                "/bin/bash", "-lc",
                f"rm -rf -- {destination} {remote_archive} && mkdir -p -- {destination}",
            ),
            cwd="/root", timeout_seconds=120,
        )
        try:
            vm = self._owned_vm(requested_vm)
            self._claim_command(prepare)
            result = self.broker.execute(
                GuestExecutionRequest(
                    request_id="push-source-prepare-" + uuid.uuid4().hex[:12],
                    session_id=self.context.session_id, run_id=self.context.run_id,
                    revision_sha=self.context.revision_sha, target=GuestTarget(vm.name),
                    commands=(prepare,),
                ),
                cancelled=self.cancelled,
            )
            prepare_payload = result.to_dict()
            self._audit("guest_execution", prepare_payload)
            self._record_result(prepare, prepare_payload)
            if result.status != "succeeded":
                raise MCPServiceError("could not prepare guest source directory")

            # Revalidate immediately before copying and use only the literal
            # address returned by that exact inventory observation.
            vm = self._owned_vm(requested_vm)
            address = verified_vm_address(vm)
            scp_address = f"[{address}]" if ":" in address else address
            copy = self._run((
                "/usr/bin/scp", "-q", *confined_ssh_options(),
                f"/dev/fd/{archive_fd}", f"root@{scp_address}:{remote_archive}",
            ), timeout=1_800, pass_fds=(archive_fd,))
            self._owned_vm(requested_vm)

            extract = GuestCommand(
                command_id="extract-source-" + uuid.uuid4().hex[:12],
                argv=(
                    "/bin/bash", "-lc",
                    f"tar -xf {remote_archive} -C {destination} && rm -f -- {remote_archive}",
                ),
                cwd="/root", timeout_seconds=600,
            )
            self._claim_command(extract)
            extracted = self.broker.execute(
                GuestExecutionRequest(
                    request_id="push-source-extract-" + uuid.uuid4().hex[:12],
                    session_id=self.context.session_id, run_id=self.context.run_id,
                    revision_sha=self.context.revision_sha,
                    target=GuestTarget(requested_vm), commands=(extract,),
                ),
                cancelled=self.cancelled,
            )
            extract_payload = extracted.to_dict()
            self._audit("guest_execution", extract_payload)
            self._record_result(extract, extract_payload)
            if extracted.status != "succeeded":
                raise MCPServiceError("could not extract guest source archive")
            return {
                "vm_name": requested_vm, "guest_source_path": destination,
                "file_count": file_count, "archive_sha256": archive_sha256,
                "copy": copy,
            }
        finally:
            os.close(archive_fd)
            archive_path.unlink(missing_ok=True)

    def exec(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        vm = self._owned_vm(arguments.get("vm_name"))
        raw_argv = arguments.get("argv")
        text = arguments.get("text")
        command = GuestCommand(
            command_id=str(arguments.get("command_id") or ("command-" + uuid.uuid4().hex[:12])),
            argv=tuple(raw_argv) if isinstance(raw_argv, list) else None,
            text=str(text) if text is not None else None,
            cwd=str(arguments.get("cwd") or "/root"),
            env=tuple((str(k), str(v)) for k, v in dict(arguments.get("env") or {}).items()),
            timeout_seconds=int(arguments.get("timeout_seconds", 3600)),
            expected_exit_codes=tuple(arguments.get("expected_exit_codes") or (0,)),
            label=str(arguments.get("label") or ""),
        )
        request = GuestExecutionRequest(
            request_id="guest-exec-" + uuid.uuid4().hex[:16],
            session_id=self.context.session_id, run_id=self.context.run_id,
            revision_sha=self.context.revision_sha, target=GuestTarget(vm.name),
            commands=(command,),
        )
        self._claim_command(command)
        result = self.broker.execute(request, cancelled=self.cancelled)
        payload = result.to_dict()
        self._audit("guest_execution", payload)
        self._record_result(command, payload)
        return payload

    def cluster_exec(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        names = arguments.get("vm_names")
        if not isinstance(names, list) or not 1 <= len(names) <= self.context.max_owned_vms:
            raise MCPServiceError("vm_names must contain owned VM names")
        results = []
        for name in names:
            child = dict(arguments)
            child["vm_name"] = name
            child["command_id"] = str(arguments.get("command_id") or "cluster-command") + "-" + _safe("VM name", name)
            results.append(self.exec(child))
            if results[-1].get("status") != "succeeded":
                break
        return {"results": results}

    def destroy(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        vm = self._owned_vm(arguments.get("vm_name"))
        # Inventory observation does not itself authorize a mutation. Re-read
        # the durable grant immediately before the adapter's own owner recheck.
        action = CleanupAction("vm", vm.name, self.context.owner_id)
        command = GuestCommand(
            command_id="destroy-" + uuid.uuid4().hex[:16],
            argv=action.argv,
            cwd="/",
            timeout_seconds=60,
        )
        self._claim_command(command)
        started_at = _utc_now()
        try:
            self._authorization()
            self.adapter.cleanup(action)
        except Exception:
            self._record_command_result(
                command, state="failed", summary="ltvm_destroy_failed",
                exit_code=None, started_at=started_at, finished_at=_utc_now(),
            )
            raise
        self._record_command_result(
            command, state="succeeded", summary="ltvm_destroy_succeeded",
            exit_code=0, started_at=started_at, finished_at=_utc_now(),
        )
        self._audit("destroy", {"vm_name": vm.name})
        return {"destroyed": vm.name}

    def _record_result(self, command: GuestCommand, payload: Mapping[str, Any]) -> None:
        """Persist via the state API when available; JSONL remains canonical audit."""
        records = payload.get("commands")
        if not isinstance(records, list) or len(records) != 1:
            return
        record = records[0]
        if not isinstance(record, Mapping):
            return
        try:
            state = str(record.get("status") or "failed")
            if state not in {
                "succeeded", "failed", "cancelled", "resource_exhausted"
            }:
                state = "failed"
            started = datetime.fromisoformat(str(record["started_at"]))
            finished = datetime.fromisoformat(str(record["finished_at"]))
            self.store.record_validation_step_result(
                self.context.attempt_id, worker_id=self.context.worker_id,
                command=command, state=state,
                summary=str(record.get("code") or payload.get("detail") or state),
                exit_code=record.get("exit_code"), artifact_ids=(),
                started_at=started, finished_at=finished,
            )
        except Exception as exc:
            raise MCPServiceError("guest result could not be persisted") from exc

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        method = getattr(self, name, None)
        if name.startswith("_") or name not in TOOL_SCHEMAS or not callable(method):
            raise MCPServiceError("unknown tool")
        if not isinstance(arguments, Mapping):
            raise MCPServiceError("tool arguments must be an object")
        allowed = set(TOOL_SCHEMAS[name]["properties"])
        unknown = set(arguments) - allowed
        if unknown:
            raise MCPServiceError(
                "unknown tool arguments: " + ", ".join(sorted(unknown))
            )
        return method(arguments)


def _schema(properties: Mapping[str, Any], required: Sequence[str] = ()) -> Mapping[str, Any]:
    return {
        "type": "object", "properties": dict(properties),
        "required": list(required), "additionalProperties": False,
    }


_STRING = {"type": "string", "minLength": 1}
TOOL_SCHEMAS = {
    "list": _schema({}),
    "target_list": _schema({}),
    "target_fetch": _schema({"target": _STRING, "arch": _STRING, "variant": _STRING}, ("target",)),
    "create": _schema({
        "name": _STRING, "target": _STRING, "arch": _STRING, "variant": _STRING,
        "memory_mib": {"type": "integer"}, "vcpus": {"type": "integer"},
        "disk_size": _STRING,
    }, ("name", "target")),
    "cluster_create": _schema({
        "name": _STRING, "target": _STRING, "arch": _STRING,
        "memory_mib": {"type": "integer"}, "vcpus": {"type": "integer"},
        "nodes": {"type": "array", "items": {"type": "object"}},
    }, ("name", "target", "nodes")),
    "push_source": _schema({"vm_name": _STRING}, ("vm_name",)),
    "exec": _schema({
        "vm_name": _STRING, "command_id": _STRING,
        "argv": {"type": "array", "items": {"type": "string"}},
        "text": _STRING, "cwd": _STRING,
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "timeout_seconds": {"type": "integer"},
        "expected_exit_codes": {"type": "array", "items": {"type": "integer"}},
        "label": {"type": "string"},
    }, ("vm_name",)),
    "cluster_exec": _schema({
        "vm_names": {"type": "array", "items": _STRING}, "command_id": _STRING,
        "argv": {"type": "array", "items": {"type": "string"}},
        "text": _STRING, "cwd": _STRING,
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "timeout_seconds": {"type": "integer"},
        "expected_exit_codes": {"type": "array", "items": {"type": "integer"}},
        "label": {"type": "string"},
    }, ("vm_names",)),
    "destroy": _schema({"vm_name": _STRING}, ("vm_name",)),
}


def tool_list() -> list[Mapping[str, Any]]:
    descriptions = {
        "list": "List only LTVM guests owned by this engineering session.",
        "target_list": "List locally available and published LTVM targets.",
        "target_fetch": "Fetch a published LTVM target through the controller.",
        "create": "Create one exact-owner LTVM guest; the controller prefixes its name.",
        "cluster_create": "Create an exact-owner multi-node LTVM cluster.",
        "push_source": "Copy this run's exact writable checkout into an owned guest.",
        "exec": "Run arbitrary argv or guest shell text inside one exact-owner guest.",
        "cluster_exec": "Run arbitrary argv or guest shell text across owned guests.",
        "destroy": "Destroy one exact-owner guest created for this session.",
    }
    return [
        {"name": name, "description": descriptions[name], "inputSchema": schema}
        for name, schema in TOOL_SCHEMAS.items()
    ]


class StdioMCPServer:
    def __init__(
        self,
        service: SessionLTVMService,
        *,
        cancellation_event: threading.Event | None = None,
    ):
        self.service = service
        self.cancellation_event = cancellation_event or threading.Event()

    def dispatch(self, request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                params = request.get("params") or {}
                result = {
                    "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "patch-watcher-ltvm", "version": "1.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_list()}
            elif method == "tools/call":
                params = request.get("params") or {}
                value = self.service.call(str(params.get("name", "")), params.get("arguments") or {})
                text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
                result = {"content": [{"type": "text", "text": text}], "structuredContent": value}
            else:
                return self._error(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return self._error(request_id, -32000, _bounded(exc, 2000))

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Mapping[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _read_requests(self, stream: Any, pending: queue.Queue) -> None:
        try:
            while True:
                raw = stream.readline(_MAX_REQUEST_BYTES + 1)
                if not raw:
                    return
                encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                if len(encoded) > _MAX_REQUEST_BYTES:
                    # Discard the rest of this one line without retaining it.
                    while not encoded.endswith(b"\n"):
                        raw = stream.readline(_MAX_REQUEST_BYTES + 1)
                        if not raw:
                            break
                        encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                    pending.put(MCPServiceError("MCP request exceeds the input bound"))
                    continue
                pending.put(encoded)
        finally:
            # EOF means the controller/model has gone away. Active SSH sees
            # this event through the broker cancellation callback.
            self.cancellation_event.set()
            pending.put(None)

    def serve(self, *, input_stream: Any = None, output_stream: Any = None) -> None:
        stream = input_stream if input_stream is not None else sys.stdin.buffer
        output = output_stream if output_stream is not None else sys.stdout
        pending: queue.Queue = queue.Queue(maxsize=16)
        reader = threading.Thread(
            target=self._read_requests,
            args=(stream, pending),
            name="pw-ltvm-mcp-input",
            daemon=True,
        )
        reader.start()
        while True:
            try:
                item = pending.get(timeout=0.1)
            except queue.Empty:
                if self.cancellation_event.is_set():
                    break
                continue
            if item is None:
                break
            try:
                if isinstance(item, Exception):
                    raise item
                request = json.loads(item)
                if not isinstance(request, Mapping):
                    raise ValueError("request is not an object")
                response = self.dispatch(request)
            except Exception as exc:
                response = self._error(None, -32700, _bounded(exc, 1000))
            if response is not None:
                output.write(_json(response) + "\n")
                output.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    args = parser.parse_args(argv)
    context = LTVMMCPContext.load(args.context)
    cancellation = threading.Event()

    def cancel(_signum: int, _frame: object) -> None:
        cancellation.set()

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    service = SessionLTVMService(context, cancelled=cancellation.is_set)
    StdioMCPServer(service, cancellation_event=cancellation).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
