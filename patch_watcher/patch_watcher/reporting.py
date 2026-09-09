"""Structured error logging and read-only Patch Watcher summaries."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

DEFAULT_ERROR_LOG = (
    Path.home() / ".local" / "state" / "patch-watcher" / "errors.jsonl"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_SUMMARY_FIELD = 200


def _mail_safe(value: Any, *, limit: int | None = None) -> str:
    """Return text that always survives ``str.encode()``.

    Gerrit JSON may legally contain a lone surrogate such as ``"\\ud800"``.
    ``EmailMessage.set_content`` encodes the body, so one surrogate anywhere in
    a watched patch's title made the nightly summary raise UnicodeEncodeError
    -- and because the title is persisted, it did so on every run from then on.
    """

    text = "" if value is None else str(value)
    text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    text = _CONTROL_RE.sub("\ufffd", text)
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def _summary_field(value: Any, *, limit: int = MAX_SUMMARY_FIELD) -> str:
    """Return one bounded single-line field for the plain-text report.

    A Gerrit subject is free text.  Interpolated raw it could carry newlines,
    and a multi-line one injected convincing fake section headings into the
    operator's mail; it could also be arbitrarily long.
    """

    return _mail_safe(" ".join(str("" if value is None else value).split()), limit=limit)


def log_structured_error(
    kind: str,
    message: str,
    patch_url: str = "",
    *,
    path: Path | None = None,
) -> None:
    """Append one bounded, secret-free JSON object to a private log.

    The destination is resolved when the call is made, not when this module is
    imported. Binding DEFAULT_ERROR_LOG as a parameter default froze it at
    import time, so nothing could redirect it afterwards -- which meant the
    test suite, whose handlers log here on any fault, wrote into the real
    operator's ~/.local/state/patch-watcher/errors.jsonl.
    """

    path = Path(path) if path is not None else DEFAULT_ERROR_LOG
    event = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "message": message[:500],
        "patch_url": patch_url,
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(json.dumps(event, sort_keys=True) + "\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _tail_lines(path: Path, limit: int, *, chunk_size: int = 1 << 16) -> list[str]:
    """Return at most `limit` final lines without reading the whole file.

    The error log is append-only and unbounded, and the dashboard asks it for
    ten lines on every render. Reading it whole to slice off the tail costs
    the full file each time -- seconds, and the file's size in resident
    memory, once it has been running for a few weeks.
    """

    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        blocks: list[bytes] = []
        newlines = 0
        while position > 0 and newlines <= limit:
            step = min(chunk_size, position)
            position -= step
            stream.seek(position)
            block = stream.read(step)
            blocks.append(block)
            newlines += block.count(b"\n")
    raw = b"".join(reversed(blocks))
    # The first line in the window is usually a fragment, and a chunk boundary
    # can split a multi-byte character. Both land in the leading entry, which
    # the slice below drops: the loop only stops once it has seen more line
    # terminators than the caller asked for.
    return raw.decode("utf-8", errors="replace").splitlines()[-limit:]


def recent_error_events(
    *, path: Path | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Read only the newest valid structured log entries."""
    path = Path(path) if path is not None else DEFAULT_ERROR_LOG
    if limit <= 0 or not path.exists():
        return []
    lines = _tail_lines(path, limit)
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def compose_daily_summary(
    patches: list[dict[str, Any]],
    *,
    day: date | None = None,
    errors: list[dict[str, Any]] | None = None,
    automation_events: list[dict[str, Any]] | None = None,
) -> str:
    """Create a concise plain-text status report from in-memory history."""
    report_day = day or datetime.now(UTC).date()
    day_prefix = report_day.isoformat()
    changes = [
        (patch, event)
        for patch in patches
        for event in (patch.get("history") or [])
        if event.get("checked_at", "").startswith(day_prefix)
    ]
    checks = sum(int(patch.get("check_count", 0)) for patch in patches)
    error_events = list(errors or [])[-10:]
    retest_events = list(automation_events or [])[-25:]

    lines = [
        f"Patch Watcher daily status — {report_day.isoformat()}",
        "",
        f"Watched patches: {len(patches)}",
        f"Checks performed in this process: {checks}",
        f"Changes noticed today: {len(changes)}",
        f"Recent errors included: {len(error_events)}",
        f"Retest automation events included: {len(retest_events)}",
        "",
        "Current status",
        "--------------",
    ]
    if not patches:
        lines.append("No patches are being watched.")
    for patch in patches:
        lines.extend([
            f"- {_summary_field(patch.get('title') or patch.get('url') or 'Unknown patch')}",
            f"  {_summary_field(patch.get('url', ''), limit=500)}",
            f"  {_summary_field(patch.get('lifecycle', '—'), limit=40)} / "
            f"{_summary_field(patch.get('watch_state', '—'), limit=40)} / "
            f"{_summary_field(patch.get('review', '—'), limit=40)}",
            f"  CI: Jenkins {_summary_field(patch.get('jenkins', '—'), limit=40)}, "
            f"Maloo {_summary_field(patch.get('maloo', '—'), limit=40)}",
            f"  Last change: {_summary_field(patch.get('change_summary', '—'), limit=300)}",
            f"  Recommendation: {_summary_field(patch.get('recommendation', '—'), limit=300)}",
        ])

    lines.extend(["", "Changes noticed", "---------------"])
    if not changes:
        lines.append("No status changes were recorded today.")
    for patch, event in changes[-25:]:
        lines.append(
            f"- {_summary_field(event.get('changed_at', 'unknown time'), limit=64)} "
            f"{_summary_field(patch.get('title') or patch.get('url') or 'patch')}: "
            f"{_summary_field(event.get('summary', 'status changed'), limit=300)}"
        )

    lines.extend(["", "Retest automation", "-----------------"])
    if not retest_events:
        lines.append("No deterministic retest events were recorded.")
    for event in retest_events:
        lines.append(
            f"- {_summary_field(event.get('created_at', 'unknown time'), limit=64)} "
            f"change {_summary_field(event.get('patch_id', 'unknown'), limit=64)}: "
            f"{_summary_field(event.get('event_type', 'event'), limit=64)} — "
            f"{_summary_field(event.get('summary', 'Recorded'), limit=300)}"
        )

    lines.extend(["", "Recent errors", "-------------"])
    if not error_events:
        lines.append("No recent errors.")
    for event in error_events:
        lines.append(
            f"- {_summary_field(event.get('timestamp', 'unknown time'), limit=64)} "
            f"{_summary_field(event.get('kind', 'error'), limit=64)}: "
            f"{_summary_field(event.get('message', ''), limit=500)} "
            f"{_summary_field(event.get('patch_url', ''), limit=500)}".rstrip()
        )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class MailResult:
    sent: bool
    message: str


Runner = Callable[..., subprocess.CompletedProcess]


class SendmailMailer:
    """Small sendmail adapter; never invokes a shell."""

    def __init__(self, path: str, *, runner: Runner = subprocess.run) -> None:
        self.path = path
        self.runner = runner

    def send(self, recipient: str, subject: str, body: str) -> MailResult:
        try:
            email = EmailMessage()
            email["To"] = recipient
            email["From"] = "patch-watcher@localhost"
            # Message construction is inside the guard on purpose: it encodes,
            # and an unencodable character used to raise straight out of the
            # nightly cron summary rather than being reported as a mail
            # failure.  ``_mail_safe`` removes the cause; the guard keeps any
            # remaining one from ending the process.
            email["Subject"] = _mail_safe(subject, limit=500)
            email.set_content(_mail_safe(body))
            result = self.runner(
                [self.path, "-t", "-oi"],
                input=email.as_bytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            message = f"Could not invoke sendmail: {exc}"
            log_structured_error("email", message)
            return MailResult(False, message)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
            message = f"sendmail exited with status {result.returncode}"
            if detail:
                message += f": {detail}"
            log_structured_error("email", message)
            return MailResult(False, message)
        return MailResult(True, f"Status email sent to {recipient}.")


def send_daily_summary(
    patches: list[dict[str, Any]],
    config: Any,
    *,
    runner: Runner = subprocess.run,
    error_log: Path = DEFAULT_ERROR_LOG,
    automation_events: list[dict[str, Any]] | None = None,
) -> MailResult:
    """Compose and optionally send today's report.

    Email is a dry run unless ``EMAIL_ENABLED`` is true in the private config.
    """
    errors = recent_error_events(path=error_log, limit=10)
    body = compose_daily_summary(
        patches,
        errors=errors,
        automation_events=automation_events,
    )
    if not config.email_enabled:
        return MailResult(
            False,
            "Email is disabled; set EMAIL_ENABLED=true in the private config to send.",
        )
    return SendmailMailer(config.sendmail_path, runner=runner).send(
        config.email_to,
        f"Patch Watcher daily status — {datetime.now(UTC).date().isoformat()}",
        body,
    )


def compose_automation_alert(
    *,
    patch_id: str,
    revision: str,
    state: str,
    summary: str,
    timeline: list[Any] | None = None,
) -> str:
    """Compose a bounded operator notice for a deterministic retest run."""

    lines = [
        "Patch Watcher deterministic-retest notice",
        "",
        f"Patch: {str(patch_id)[:200]}",
        f"Revision: {str(revision)[:80]}",
        f"State: {str(state)[:80]}",
        f"Summary: {str(summary)[:500]}",
        "",
        "Recent timeline",
        "---------------",
    ]
    bounded = list(timeline or [])[-8:]
    if not bounded:
        lines.append("No timeline events were recorded.")
    for event in bounded:
        if isinstance(event, dict):
            created_at = event.get("created_at", "unknown time")
            event_type = event.get("event_type", "event")
            detail = event.get("summary", "Recorded")
        else:
            created_at = getattr(event, "created_at", "unknown time")
            event_type = getattr(event, "event_type", "event")
            payload = getattr(event, "payload", {}) or {}
            detail = payload.get("summary", "Recorded")
        lines.append(
            f"- {str(created_at)[:80]} {str(event_type)[:80]}: "
            f"{' '.join(str(detail).split())[:500]}"
        )
    return "\n".join(lines) + "\n"


def send_automation_alert(
    config: Any,
    *,
    patch_id: str,
    revision: str,
    state: str,
    summary: str,
    timeline: list[Any] | None = None,
    runner: Runner = subprocess.run,
) -> MailResult:
    """Send one immediate deterministic-retest notice through sendmail."""

    if not config.email_enabled:
        return MailResult(False, "Email is disabled; the retest notice was recorded only.")
    body = compose_automation_alert(
        patch_id=patch_id,
        revision=revision,
        state=state,
        summary=summary,
        timeline=timeline,
    )
    return SendmailMailer(config.sendmail_path, runner=runner).send(
        config.email_to,
        f"Patch Watcher retest notice — {str(patch_id)[:120]}",
        body,
    )


def compose_session_alert(
    *,
    session_id: str,
    patch_id: str,
    state: str,
    reason: str,
    messages: list[Any],
    confirmation_url: str = "",
) -> str:
    """Compose one bounded operator alert for a managed Claude session.

    The URL is a confirmation page only. It is deliberately described that
    way so mail scanners and ordinary GET requests cannot stop a session.
    """

    lines = [
        "Patch Watcher managed-session alert",
        "",
        f"Patch: {str(patch_id)[:200]}",
        f"Session: {str(session_id)[:200]}",
        f"State: {str(state)[:80]}",
        f"Reason: {str(reason)[:500]}",
        "",
        "Recent messages",
        "---------------",
    ]
    bounded = list(messages)[-8:]
    if not bounded:
        lines.append("No recent messages were recorded.")
    for message in bounded:
        if isinstance(message, dict):
            author = message.get("author", "agent")
            body = message.get("body", "")
        else:
            author = getattr(message, "author", "agent")
            body = getattr(message, "body", "")
        clean = " ".join(str(body).split())[:500]
        lines.append(f"- {str(author)[:80]}: {clean}")
    if confirmation_url:
        lines.extend([
            "",
            "Stop this session",
            "-----------------",
            "Opening this link does not stop anything. It opens a confirmation page:",
            str(confirmation_url)[:2_000],
        ])
    return "\n".join(lines) + "\n"


def send_session_alert(
    config: Any,
    *,
    session_id: str,
    patch_id: str,
    state: str,
    reason: str,
    messages: list[Any],
    confirmation_url: str = "",
    runner: Runner = subprocess.run,
) -> MailResult:
    """Send one managed-session alert through the configured host sendmail."""

    if not config.email_enabled:
        return MailResult(False, "Email is disabled; the session alert was recorded only.")
    body = compose_session_alert(
        session_id=session_id,
        patch_id=patch_id,
        state=state,
        reason=reason,
        messages=messages,
        confirmation_url=confirmation_url,
    )
    return SendmailMailer(config.sendmail_path, runner=runner).send(
        config.email_to,
        f"Patch Watcher session alert — {str(patch_id)[:120]}",
        body,
    )
