"""Notice when somebody assigns a ticket to the bot, and say so.

Assignment is the deliberate, revocable, auditable way to hand the bot work.
It beats mail in the other direction for the same reason: the signal carries
its own context, and taking it back is one click rather than an apology.

Two things this is careful about.

The mail is best effort and the poll is not.  A watcher that stopped watching
because its notifier was misconfigured would be trading the whole job for the
announcement of it, so a send failure is recorded, retried a bounded number of
times, and otherwise ignored.  Mail is expected to fail outright until the
bot's own Unix account exists; that must remain merely annoying.

A ticket leaving the inbox is forgotten.  Unassigning and reassigning is how a
person says "look at this again", so it has to notify again -- which it only
can if nothing remembers the first time forever.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from patch_watcher import bot_mail

DEFAULT_STATE = Path.home() / ".local" / "state" / "patch-watcher" / "bot-inbox.json"
# A notice nobody can deliver is not worth retrying forever; after this the
# ticket stays known and unannounced, and the console is how you find it.
MAX_NOTIFY_ATTEMPTS = 5


@dataclass(frozen=True)
class InboxChange:
    """One ticket that has just appeared in the bot's inbox."""

    key: str
    summary: str
    status: str
    notified: bool
    detail: str


class BotInbox:
    """Durable memory of which assigned tickets have been announced."""

    def __init__(
        self,
        path: Path = DEFAULT_STATE,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.clock = clock

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            # Unreadable state means "announce it again", which is noisy but
            # never silent.  The opposite failure -- losing the ticket -- is
            # the one that matters.
            return {}
        seen = value.get("seen") if isinstance(value, dict) else None
        return seen if isinstance(seen, dict) else {}

    def _write(self, seen: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_name(self.path.name + ".new")
            temporary.write_text(
                json.dumps({"schema": "patch-watcher-bot-inbox/v1", "seen": seen},
                           indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError:
            # Losing the record means re-announcing later, which is the
            # tolerable direction.  It must not end the poll.
            return

    def reconcile(self, issues: Sequence[dict]) -> list[dict]:
        """Update what is remembered, and return what still needs announcing.

        Called with the whole inbox rather than a delta on purpose: the
        authority on what is assigned is JIRA, and anything this file believes
        that JIRA does not is stale by definition.
        """
        seen = self._read()
        now = float(self.clock())
        present = {}
        pending = []
        for issue in issues:
            key = str(issue.get("key") or "")
            if not key:
                continue
            record = dict(seen.get(key) or {})
            if not record:
                record = {"first_seen_at": now, "notified": False, "attempts": 0}
            present[key] = record
            if not record.get("notified") and int(record.get("attempts") or 0) < MAX_NOTIFY_ATTEMPTS:
                pending.append(dict(issue))
        # Anything no longer assigned is dropped, so reassignment announces
        # again.  That is the behaviour a person expects from "look at this
        # again", and keeping the record would silently deny it.
        self._write(present)
        return pending

    def record_result(self, key: str, *, notified: bool) -> None:
        seen = self._read()
        record = dict(seen.get(key) or {})
        if not record:
            return
        if notified:
            record["notified"] = True
            record["notified_at"] = float(self.clock())
        else:
            record["attempts"] = int(record.get("attempts") or 0) + 1
        seen[key] = record
        self._write(seen)


def poll(
    client,
    *,
    inbox: BotInbox | None = None,
    send: Callable[[str, str], object] | None = None,
) -> list[InboxChange]:
    """Read the bot's inbox and announce anything new.

    ``client`` is anything with ``assigned_issues()``; ``send`` is anything
    that takes a subject and a body.  Neither failure is fatal: JIRA being
    unreachable is a poll that found nothing, and mail failing is a ticket
    that stays unannounced and is retried.
    """
    inbox = inbox or BotInbox()
    send = send or bot_mail.send
    try:
        issues = client.assigned_issues()
    except Exception:
        # The next poll asks again.  A tracker that is down is not an event.
        return []

    announced = []
    for issue in inbox.reconcile(issues):
        key = issue.get("key", "")
        summary = str(issue.get("summary") or "")
        subject = f"{key} was assigned to the bot"
        body = (
            f"{key}  {summary}\n"
            f"status: {issue.get('status') or 'unknown'}\n"
            f"updated: {issue.get('updated') or 'unknown'}\n\n"
            "Somebody assigned this to the bot account. Patch Watcher will "
            "treat it as work once the ticket is being watched; adding it is "
            "still yours to do.\n"
        )
        try:
            outcome = send(subject, body)
            sent = bool(getattr(outcome, "sent", outcome))
            detail = str(getattr(outcome, "detail", ""))
        except Exception as exc:
            sent, detail = False, f"notifier raised: {exc}"
        inbox.record_result(key, notified=sent)
        announced.append(
            InboxChange(key, summary, str(issue.get("status") or ""), sent, detail)
        )
    return announced
