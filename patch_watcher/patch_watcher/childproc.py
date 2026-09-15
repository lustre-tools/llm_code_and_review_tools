"""Spawn helper tools so they cannot outlive this process.

The unit runs with ``KillMode=process`` on purpose: a restart must not
SIGKILL a run's agent and the guests it built, so systemd signals only the
main process and leaves every child alone.  That protects the agents, and it
also spared the short-lived tool subprocesses -- maloo, gerrit, ltvm, git --
whose only deadline lives in the parent that spawned them.  A `maloo review`
left behind by a restart ran for two hours and forty minutes and reached
1.4 GB before it was killed by hand; three more were accumulating behind it.

A helper tool exists to answer one caller.  When that caller is gone the
answer has nowhere to go, so the kernel is asked to end it: PR_SET_PDEATHSIG
fires the moment the parent dies, whatever killed it, including SIGKILL,
which no cleanup in this process could have survived anyway.

Two things about PDEATHSIG matter at the call sites:

It is delivered when the spawning THREAD exits, not when the process does.
Every caller here waits for its child on the thread that started it, so the
thread cannot go first.  A caller that means to outlive its child must not
come through here.

It is also lost in the window between fork and prctl: a parent that dies in
that instant is never noticed.  The child compares its parent against the one
it was forked from and exits if they differ, which closes it.

``start_new_session`` is the marker for a child that is MEANT to survive --
the agent hosts are spawned that way, precisely so no signal to this process
reaches them.  Such a child never has PDEATHSIG set on it here, so routing
one through this module by accident cannot kill a live run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

PR_SET_PDEATHSIG = 1

_prctl = None
if sys.platform.startswith("linux"):  # pragma: no branch - one platform here
    try:
        import ctypes

        # Resolved here rather than in the child: after fork only this thread
        # exists, and a dlopen there can block on a lock another thread held.
        _prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
    except (ImportError, OSError):  # pragma: no cover - no libc to bind
        _prctl = None


def _die_with(parent_pid: int):
    """Build the after-fork hook that binds a child to ``parent_pid``."""

    def child_setup() -> None:
        _prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent_pid:
            os._exit(127)

    return child_setup


def _bind_to_parent(kwargs: dict[str, Any]) -> dict[str, Any]:
    if _prctl is None:
        return kwargs
    if kwargs.get("start_new_session") or kwargs.get("preexec_fn") is not None:
        return kwargs
    bound = dict(kwargs)
    bound["preexec_fn"] = _die_with(os.getpid())
    return bound


def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a tool that must not outlive this process."""
    return subprocess.run(*args, **_bind_to_parent(kwargs))


def popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` for a tool that must not outlive this process."""
    return subprocess.Popen(*args, **_bind_to_parent(kwargs))
