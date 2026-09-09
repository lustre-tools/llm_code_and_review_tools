"""Thin adapter over Claude Code's own background-session subsystem.

Claude Code 2.1.263 ships everything Patch Watcher used to build by hand:
``claude --bg`` starts a detached session, ``claude agents --json`` lists
sessions with their state, ``stop``/``rm`` control the lifecycle, and each
session writes a structured JSONL transcript.  Background sessions also get
Remote Control for free, so a human can reply from the mobile or desktop app.

This module therefore owns no session state machine, no process supervision,
and no transcript capture.  It translates between Patch Watcher's vocabulary
and the CLI's, and nothing more.  What Patch Watcher still has to remember --
which patch, revision, playbook and checkout a session belongs to -- lives in
its own store, keyed by ``session_id``.

**Not wired in.** Nothing imports this module: the live session path is
``claude_runner.ClaudeHost``, which supervises its own subprocess. This
adapter is validated by its tests and kept for the migration to background
sessions, and it is the only module in the package in that state. Said
explicitly because the docstring above reads as a description of how the tool
works today, and a reader cannot otherwise tell it apart from debris left by
the carve-down.

Facts it encodes that were measured against the real CLI, and that a future
migration must not rediscover the hard way: ``status`` is null both when a
session is stopped AND when it is working, so liveness has to probe the pid;
resuming a live session forks a copy, so ``send()`` reports whether it forked;
and the project slug replaces ``_`` as well as ``/`` and ``.``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
SHORT_ID_RE = re.compile(r"\b([0-9a-f]{8})\b")
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
# States the CLI reports while a turn is still in progress.
LIVE_STATES = frozenset({"working", "running", "busy"})
Runner = Callable[..., subprocess.CompletedProcess]


class CliSessionError(RuntimeError):
    """The Claude CLI could not start, list, or control a session."""


@dataclass(frozen=True)
class SendResult:
    """What a delivered message actually produced.

    ``forked`` means the CLI started a copy instead of continuing the original.
    The copy carries the prior conversation but writes a different transcript,
    so the caller must adopt ``short_id``/``session_id`` from here.
    """

    short_id: str
    session_id: str
    forked: bool


@dataclass(frozen=True)
class CliSession:
    """One session as the CLI reports it."""

    short_id: str
    session_id: str
    cwd: str
    kind: str
    name: str
    status: str | None
    state: str
    pid: int | None
    started_at: int | None

    @property
    def running(self) -> bool:
        """True while the session's process is alive.

        Measured behaviour, not documented behaviour: ``status`` is "idle" for
        a live session waiting for input, but **null both for a stopped session
        and for one actively working** (observed with ``state`` "working").
        Trusting ``status`` alone therefore reports a busy session as stopped,
        and resuming it forks a copy.  The pid is authoritative -- these are
        local processes -- so probe it and fall back to the reported fields
        only when there is no pid to check.
        """

        if self.pid:
            try:
                os.kill(int(self.pid), 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # it exists, we just may not signal it
            except (OverflowError, TypeError, ValueError):
                pass  # unusable pid: fall through to the reported fields
            else:
                return True
        return self.status is not None or self.state in LIVE_STATES

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CliSession:
        return cls(
            short_id=str(value.get("id") or ""),
            session_id=str(value.get("sessionId") or ""),
            cwd=str(value.get("cwd") or ""),
            kind=str(value.get("kind") or ""),
            name=str(value.get("name") or ""),
            status=value.get("status"),
            state=str(value.get("state") or ""),
            pid=value.get("pid"),
            started_at=value.get("startedAt"),
        )


def project_slug(cwd: Path | str) -> str:
    """Return the CLI's directory slug for a working directory.

    The CLI replaces every path separator, dot AND underscore with a dash, so
    ``/tmp/a.b/c_d`` becomes ``-tmp-a-b-c-d``.  The underscore matters: both
    default pool roots contain one (``lustre_checkouts/master_checkouts``), so
    omitting it made every transcript read resolve to a nonexistent file and
    return "the agent said nothing", silently and permanently.
    """

    resolved = str(Path(cwd).expanduser().resolve())
    return re.sub(r"[/._]", "-", resolved)


class ClaudeCli:
    """Runs the ``claude`` executable and parses what it reports."""

    def __init__(
        self,
        *,
        binary: str = "claude",
        runner: Runner = subprocess.run,
        projects_root: Path = DEFAULT_PROJECTS_ROOT,
        timeout: float = 120.0,
    ) -> None:
        self.binary = binary
        self.runner = runner
        self.projects_root = Path(projects_root).expanduser()
        self.timeout = timeout

    def _run(self, args: Sequence[str], *, cwd: Path | None = None) -> str:
        command = [self.binary, *args]
        try:
            result = self.runner(
                command,
                cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CliSessionError(f"claude {args[0]} failed: {type(exc).__name__}") from exc
        stdout = _text(result.stdout)
        if result.returncode:
            raise CliSessionError(
                f"claude {args[0]} exited with status {result.returncode}: "
                + _text(result.stderr).strip()[:200]
            )
        return stdout

    def start(
        self,
        prompt: str,
        *,
        cwd: Path,
        model: str = "",
        effort: str = "",
        permission_mode: str = "bypassPermissions",
        json_schema: Mapping[str, Any] | None = None,
        name: str = "",
    ) -> str:
        """Start a detached session and return its short id.

        ``bypassPermissions`` is the default because the working agent's
        boundary is its prompt and its checkout, not a permission prompt that
        nobody is present to answer.
        """

        if not prompt.strip():
            raise CliSessionError("prompt must not be empty")
        args = ["--bg", "--permission-mode", permission_mode]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        if name:
            args += ["--name", name]
        if json_schema is not None:
            args += ["--json-schema", json.dumps(json_schema, sort_keys=True, separators=(",", ":"))]
        args.append(prompt)
        output = self._run(args, cwd=cwd)
        return self._short_id(output)

    @staticmethod
    def _short_id(output: str) -> str:
        """Extract the session id from a launch confirmation.

        Only the "backgrounded" line is trusted.  Scanning the whole output as
        a fallback happily matches any 8-hex token -- a cache path, a warning
        containing a digest -- and returns a plausible but wrong id, which is
        worse than failing: the caller would poll a session that never existed.
        """

        for line in output.splitlines():
            if "backgrounded" not in line:
                continue
            match = SHORT_ID_RE.search(line)
            if match:
                return match.group(1)
        raise CliSessionError(
            "no launch confirmation in the CLI output: " + output.strip()[:200]
        )

    def sessions(
        self, *, cwd: Path | None = None, include_finished: bool = True
    ) -> tuple[CliSession, ...]:
        args = ["agents", "--json"]
        if include_finished:
            args.append("--all")
        if cwd is not None:
            args += ["--cwd", str(Path(cwd).expanduser().resolve())]
        try:
            payload = json.loads(self._run(args) or "[]")
        except ValueError as exc:
            raise CliSessionError("claude agents did not return JSON") from exc
        if not isinstance(payload, list):
            raise CliSessionError("claude agents did not return a JSON array")
        return tuple(CliSession.from_json(item) for item in payload if isinstance(item, Mapping))

    def session(self, short_id: str, *, cwd: Path | None = None) -> CliSession | None:
        return next(
            (item for item in self.sessions(cwd=cwd) if item.short_id == short_id), None
        )

    def stop(self, short_id: str) -> None:
        self._run(["stop", short_id])

    def remove(self, short_id: str) -> None:
        self._run(["rm", short_id])

    def wait_until_stopped(
        self,
        short_id: str,
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
        interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Poll until the session's process is gone.

        ``claude stop`` returns before the process has actually exited, and a
        resume issued in that window forks a copy instead of waking the
        original.  Every caller that stops then resumes must wait here first.
        """

        deadline = clock() + timeout
        while True:
            found = self.session(short_id, cwd=cwd)
            if found is None or not found.running:
                return True
            if clock() >= deadline:
                return False
            sleep(interval)

    def send(
        self,
        short_id: str,
        session_id: str,
        message: str,
        *,
        cwd: Path,
        stop_timeout: float = 30.0,
    ) -> SendResult:
        """Deliver ``message`` to an existing session.

        A live session must be stopped first: resuming one whose process is
        still alive forks a copy under a new id rather than continuing it.  The
        result reports whether that happened anyway, because a fork writes to a
        different transcript and the caller must follow the new session id
        rather than keep polling the old one.
        """

        if not message.strip():
            raise CliSessionError("message must not be empty")
        found = self.session(short_id, cwd=cwd)
        if found is not None and found.running:
            self.stop(short_id)
            if not self.wait_until_stopped(short_id, cwd=cwd, timeout=stop_timeout):
                raise CliSessionError(
                    f"session {short_id} did not stop; refusing to resume and fork a copy"
                )
        output = self._run(["--bg", "--resume", session_id, message], cwd=cwd)
        new_short = self._short_id(output)
        forked = "started a copy" in output or new_short != short_id
        if not forked:
            return SendResult(short_id=new_short, session_id=session_id, forked=False)
        replacement = self.session(new_short, cwd=cwd)
        if replacement is None or not replacement.session_id:
            # Returning the new short id beside the OLD session id would send
            # the caller to a transcript that will never update again -- the
            # precise failure this class exists to prevent. Say so instead.
            raise CliSessionError(
                f"session {short_id} forked to {new_short}, but that copy is not "
                "listed yet; re-list before reading its transcript"
            )
        return SendResult(
            short_id=new_short, session_id=replacement.session_id, forked=True
        )

    def transcript_path(self, session_id: str, cwd: Path) -> Path:
        return self.projects_root / project_slug(cwd) / f"{session_id}.jsonl"

    def transcript(self, session_id: str, cwd: Path) -> tuple[Mapping[str, Any], ...]:
        """Return the session's structured events, or () before it has any."""

        path = self.transcript_path(session_id, cwd)
        if not path.exists():
            return ()
        events = []
        for raw in path.read_text(errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a partially flushed final line during a live read
            if isinstance(event, Mapping):
                events.append(event)
        return tuple(events)

    def assistant_text(self, session_id: str, cwd: Path) -> tuple[str, ...]:
        """Return the assistant's visible replies, oldest first."""

        said = []
        for event in self.transcript(session_id, cwd):
            if event.get("type") != "assistant":
                continue
            for block in event.get("message", {}).get("content", []) or ():
                if isinstance(block, Mapping) and block.get("type") == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        said.append(text)
        return tuple(said)

    def structured_result(self, session_id: str, cwd: Path) -> Mapping[str, Any] | None:
        """Return the newest ``--json-schema`` result, if the session produced one.

        The CLI delivers it as a StructuredOutput tool_use block rather than on
        stdout, so it is read back from the transcript.
        """

        result = None
        for event in self.transcript(session_id, cwd):
            if event.get("type") != "assistant":
                continue
            for block in event.get("message", {}).get("content", []) or ():
                if (
                    isinstance(block, Mapping)
                    and block.get("type") == "tool_use"
                    and block.get("name") == STRUCTURED_OUTPUT_TOOL
                    and isinstance(block.get("input"), Mapping)
                ):
                    result = dict(block["input"])
        return result


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


__all__ = [
    "LIVE_STATES",
    "STRUCTURED_OUTPUT_TOOL",
    "ClaudeCli",
    "CliSession",
    "CliSessionError",
    "SendResult",
    "project_slug",
]
