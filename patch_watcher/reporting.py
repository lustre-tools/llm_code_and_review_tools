"""Structured error logging and read-only Patch Watcher summaries."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable


DEFAULT_ERROR_LOG = (
    Path.home() / ".local" / "state" / "patch-watcher" / "errors.jsonl"
)


def log_structured_error(
    kind: str,
    message: str,
    patch_url: str = "",
    *,
    path: Path = DEFAULT_ERROR_LOG,
) -> None:
    """Append one bounded, secret-free JSON object to a private log."""
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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


def recent_error_events(
    *, path: Path = DEFAULT_ERROR_LOG, limit: int = 10
) -> list[dict[str, Any]]:
    """Read only the newest valid structured log entries."""
    if limit <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
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
    report_day = day or datetime.now(timezone.utc).date()
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
            f"- {patch.get('title') or patch.get('url', 'Unknown patch')}",
            f"  {patch.get('url', '')}",
            f"  {patch.get('lifecycle', '—')} / {patch.get('watch_state', '—')} / "
            f"{patch.get('review', '—')}",
            f"  CI: Jenkins {patch.get('jenkins', '—')}, Maloo {patch.get('maloo', '—')}",
            f"  Last change: {patch.get('change_summary', '—')}",
            f"  Recommendation: {patch.get('recommendation', '—')}",
        ])

    lines.extend(["", "Changes noticed", "---------------"])
    if not changes:
        lines.append("No status changes were recorded today.")
    for patch, event in changes[-25:]:
        lines.append(
            f"- {event.get('changed_at', 'unknown time')} "
            f"{patch.get('title', patch.get('url', 'patch'))}: "
            f"{event.get('summary', 'status changed')}"
        )

    lines.extend(["", "Retest automation", "-----------------"])
    if not retest_events:
        lines.append("No deterministic retest events were recorded.")
    for event in retest_events:
        lines.append(
            f"- {event.get('created_at', 'unknown time')} "
            f"change {event.get('patch_id', 'unknown')}: "
            f"{event.get('event_type', 'event')} — "
            f"{event.get('summary', 'Recorded')}"
        )

    lines.extend(["", "Recent errors", "-------------"])
    if not error_events:
        lines.append("No recent errors.")
    for event in error_events:
        lines.append(
            f"- {event.get('timestamp', 'unknown time')} "
            f"{event.get('kind', 'error')}: {event.get('message', '')} "
            f"{event.get('patch_url', '')}".rstrip()
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
        email = EmailMessage()
        email["To"] = recipient
        email["From"] = "patch-watcher@localhost"
        email["Subject"] = subject
        email.set_content(body)
        try:
            result = self.runner(
                [self.path, "-t", "-oi"],
                input=email.as_bytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
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
        f"Patch Watcher daily status — {datetime.now(timezone.utc).date().isoformat()}",
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
