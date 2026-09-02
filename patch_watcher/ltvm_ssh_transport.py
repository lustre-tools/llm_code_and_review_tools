"""Concrete bounded SSH transport for :mod:`ltvm_guest_exec`.

OpenSSH is launched directly with an argv vector and a minimal local
environment.  The host never interprets guest command text.  The transport
re-reads LTVM inventory itself immediately before launching SSH and refuses a
missing, ambiguous, stopped, or differently owned target.

LTVM currently has no owner-checked exec RPC.  Therefore this adapter's owner
check and SSH launch are adjacent rather than atomic.  The expected owner is
also part of the transport call so a future LTVM exec RPC can close that small
TOCTOU window without changing the broker API.
"""

from __future__ import annotations

import ipaddress
import os
import selectors
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from ltvm_guest_exec import (
    GuestCommand,
    GuestExecutionError,
    GuestTarget,
    GuestTransportBoundary,
    GuestTransportResult,
    LTVMGuestExecutionBroker,
)
from ltvm_resources import LTVMInventory


@dataclass(frozen=True)
class GuestIsolationAssertion:
    """Provisioning facts required before a VM may run engineering commands."""

    service_credentials_absent: bool
    gerrit_writes_blocked: bool

    def __post_init__(self) -> None:
        if not self.service_credentials_absent or not self.gerrit_writes_blocked:
            raise ValueError(
                "guest isolation must exclude service credentials and Gerrit writes"
            )


ProcessRunner = Callable[..., GuestTransportResult]


def confined_ssh_options() -> tuple[str, ...]:
    """Exclude ambient SSH config and every local command/proxy hook."""

    return (
        "-F", "/dev/null",
        "-o", "BatchMode=yes",
        "-o", "CanonicalizeHostname=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ForwardAgent=no",
        "-o", "IdentityAgent=none",
        "-o", "PermitLocalCommand=no",
        "-o", "ProxyCommand=none",
        "-o", "ProxyJump=none",
        "-o", "RequestTTY=no",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
    )


def verified_vm_address(vm: object) -> str:
    """Return only a literal inventory IP, never a configurable SSH hostname."""

    raw = getattr(vm, "raw", None)
    value = raw.get("ip") if isinstance(raw, Mapping) else None
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError as exc:
        raise GuestExecutionError("owned VM has no valid inventory IP address") from exc


def run_bounded_process(
    argv: Sequence[str],
    *,
    timeout_seconds: int,
    max_output_bytes: int,
    cancelled: Callable[[], bool],
    environment: Mapping[str, str],
    pass_fds: Sequence[int] = (),
    process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> GuestTransportResult:
    """Run local transport argv with bounded capture, deadline, and cancellation.

    This function runs the SSH *transport process*, never the engineering
    command itself.  ``shell`` is fixed false.  The transport gets a private
    process group so cancellation can terminate SSH/scp and every descendant.
    The MCP signal handlers translate controller termination into the same
    cancellation callback.  Proxy and local-command helpers are disabled.
    """

    if not argv or isinstance(argv, (str, bytes)):
        raise ValueError("transport argv must be a non-empty argument array")
    if timeout_seconds < 1 or max_output_bytes < 1:
        raise ValueError("transport bounds must be positive")
    inherited = tuple(pass_fds)
    if any(isinstance(fd, bool) or not isinstance(fd, int) or fd < 0 for fd in inherited):
        raise ValueError("pass_fds contains an invalid descriptor")
    process = process_factory(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        env=dict(environment),
        pass_fds=inherited,
        bufsize=0,
    )
    if process.stdout is None or process.stderr is None:
        try:
            process.kill()
        finally:
            process.wait()
        raise GuestExecutionError("transport process has no output pipes")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    observed = {"stdout": 0, "stderr": 0}
    deadline = time.monotonic() + timeout_seconds
    stopping_at: float | None = None
    was_cancelled = False
    timed_out = False

    def stop(sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    while selector.get_map() or process.poll() is None:
        now = time.monotonic()
        if stopping_at is None and cancelled():
            was_cancelled = True
            stopping_at = now
            stop(signal.SIGTERM)
        elif stopping_at is None and now >= deadline:
            timed_out = True
            stopping_at = now
            stop(signal.SIGTERM)
        elif stopping_at is not None and process.poll() is None and now - stopping_at >= 1.0:
            stop(signal.SIGKILL)

        for key, _ in selector.select(timeout=0.05):
            try:
                chunk = os.read(key.fileobj.fileno(), 65_536)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            stream = key.data
            observed[stream] += len(chunk)
            remaining = max_output_bytes - len(captured["stdout"]) - len(captured["stderr"])
            if remaining > 0:
                captured[stream].extend(chunk[:remaining])

    return_code = process.wait()
    exit_code = return_code if 0 <= return_code <= 255 else None
    return GuestTransportResult(
        exit_code=exit_code,
        stdout=bytes(captured["stdout"]),
        stderr=bytes(captured["stderr"]),
        timed_out=timed_out,
        cancelled=was_cancelled,
        stdout_observed_bytes=observed["stdout"],
        stderr_observed_bytes=observed["stderr"],
    )


class LTVMSSHGuestTransport:
    """Use only the literal address from exact-owner LTVM inventory."""

    def __init__(
        self,
        *,
        inventory_provider: object,
        isolation: GuestIsolationAssertion,
        runner: ProcessRunner = run_bounded_process,
        ssh_binary: str = "ssh",
        local_path: str | None = None,
    ) -> None:
        if not ssh_binary or "\x00" in ssh_binary:
            raise ValueError("ssh_binary is invalid")
        self.inventory_provider = inventory_provider
        self.isolation = isolation
        self.runner = runner
        self.ssh_binary = ssh_binary
        self.local_path = local_path or os.environ.get("PATH", "/usr/bin:/bin")

    def boundary(self) -> GuestTransportBoundary:
        return GuestTransportBoundary(
            service_credentials_absent=self.isolation.service_credentials_absent,
            gerrit_writes_blocked=self.isolation.gerrit_writes_blocked,
        )

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
        if cancelled():
            return GuestTransportResult(None, cancelled=True)
        inventory = self.inventory_provider.inventory()
        if not isinstance(inventory, LTVMInventory):
            raise GuestExecutionError("inventory provider returned invalid data")
        ownership_failure = LTVMGuestExecutionBroker._validate_target(
            inventory, target, expected_owner_id
        )
        if ownership_failure is not None:
            raise GuestExecutionError(
                "transport refused target ownership: " + ownership_failure[1]
            )
        vm = inventory.named_vms(target.vm_name)[0]
        address = verified_vm_address(vm)

        remote_command = self._remote_command(command)
        transport_argv = (
            self.ssh_binary,
            "-T",
            *confined_ssh_options(),
            "-l", "root",
            address,
            remote_command,
        )
        return self.runner(
            transport_argv,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancelled=cancelled,
            environment={"PATH": self.local_path, "LANG": "C", "LC_ALL": "C"},
        )

    @staticmethod
    def _remote_command(command: GuestCommand) -> str:
        prefix = "cd -- " + shlex.quote(command.cwd) + " && "
        environment = ""
        if command.env:
            environment = "env " + " ".join(
                f"{key}={shlex.quote(value)}" for key, value in command.env
            ) + " "
        if command.argv is not None:
            body = "exec " + environment + shlex.join(command.argv)
        else:
            # The shell is explicitly the guest's shell.  The entire resulting
            # string is one SSH argv element and is never parsed on the host.
            body = environment + str(command.text)
        return prefix + body


__all__ = [
    "GuestIsolationAssertion",
    "LTVMSSHGuestTransport",
    "confined_ssh_options",
    "run_bounded_process",
    "verified_vm_address",
]
