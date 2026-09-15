"""One-destination mail, safe to call from a space you do not trust.

This exists to be handed to an untrusted caller.  The agent runs as its own
Unix user with its own service accounts, and this is the one way it can reach
a human who is not looking at the console.  Everything about it is shaped by
the assumption that the caller may be fully compromised.

What a caller cannot do, by construction:

* Choose the recipient.  It is a constant in this file, not an argument, not
  configuration, and not an environment variable.  There is no code path that
  sends anywhere else, so redirecting the mail means editing this file as the
  user who owns it.
* Forge headers.  Subject text is stripped of everything that could end a
  header line before it is set, and the body is set as content rather than
  assembled into the message text, so a newline in either cannot introduce a
  Bcc.
* Send without limit.  A compromised caller's most obvious move is to mailbomb
  the destination or to use it as an exfiltration channel one message at a
  time; both are bounded here, and the bound is enforced before the message is
  built.
* Attach anything, send HTML, or set any header the caller names.

What a caller CAN do is put text in a subject and a body.  That text reaches a
human, so it is untrusted content in a place a human reads: it is bounded,
stripped of control characters, and the destination is fixed so the worst case
is noise in one mailbox rather than mail sent as you to somebody else.

No credentials are involved.  Delivery is the local sendmail binary, so the
caller holds no secret that could be stolen from it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

from patch_watcher import childproc
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

# The one destination.  Deliberately a constant: see the module docstring.
RECIPIENT = "patrick@mulberrytree.us"
SENDER = "patch-watcher-bot@localhost"
SUBJECT_PREFIX = "[patch-watcher] "
SENDMAIL_PATH = "/usr/sbin/sendmail"

MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 8000
# A mailbomb and a slow exfiltration channel are the same bound from two
# directions: how often, and how many in a day.
MIN_SECONDS_BETWEEN = 60
MAX_PER_DAY = 50

DEFAULT_STATE = Path.home() / ".local" / "state" / "patch-watcher" / "bot-mail.json"

Runner = Callable[..., subprocess.CompletedProcess]


class BotMailRefused(RuntimeError):
    """The message was not sent, and why."""


@dataclass(frozen=True)
class MailOutcome:
    sent: bool
    detail: str


def _printable(text: object, limit: int) -> str:
    """Collapse to one line of printable characters, bounded.

    Header injection is the reason this is not a strip() of "\\r\\n": a
    subject containing a vertical tab or a form feed is not a header
    continuation everywhere, but it is somewhere, and the cost of removing
    every control character is nothing.
    """
    raw = str(text if text is not None else "")
    cleaned = "".join(" " if character < " " or character == "\x7f" else character
                      for character in raw)
    return " ".join(cleaned.split())[:limit]


def _body_text(text: object, limit: int) -> str:
    """The body keeps its line breaks; everything else control is removed."""
    raw = str(text if text is not None else "")
    kept = []
    for character in raw:
        if character in "\n\t":
            kept.append(character)
        elif character < " " or character == "\x7f":
            kept.append(" ")
        else:
            kept.append(character)
    return "".join(kept)[:limit]


class RateLimit:
    """A send budget that survives restarts, and fails closed.

    An unwritable state file means the budget cannot be enforced.  Sending
    anyway would make the limit advisory, which for a guard against an
    untrusted caller is the same as not having one, so it refuses instead.
    """

    def __init__(self, path: Path = DEFAULT_STATE, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        except OSError as exc:
            raise BotMailRefused(f"send budget is unreadable: {exc}") from exc
        return value if isinstance(value, dict) else {}

    def check_and_record(self) -> None:
        now = float(self.clock())
        state = self._read()
        last = float(state.get("last_sent_at") or 0.0)
        day = str(state.get("day") or "")
        count = int(state.get("count_today") or 0)
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != today:
            count = 0
        if now - last < MIN_SECONDS_BETWEEN:
            raise BotMailRefused(
                f"another message went out {int(now - last)}s ago; "
                f"the minimum gap is {MIN_SECONDS_BETWEEN}s"
            )
        if count >= MAX_PER_DAY:
            raise BotMailRefused(
                f"{count} messages already sent today; the daily limit is {MAX_PER_DAY}"
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_name(self.path.name + ".new")
            temporary.write_text(
                json.dumps({"last_sent_at": now, "day": today, "count_today": count + 1}),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise BotMailRefused(f"send budget is unwritable: {exc}") from exc


def send(
    subject: str,
    body: str,
    *,
    runner: Runner = childproc.run,
    limit: RateLimit | None = None,
    sendmail_path: str = SENDMAIL_PATH,
) -> MailOutcome:
    """Send one message to the one destination, or say why not.

    The budget is spent before the message is built.  Checking afterwards
    would let a caller that crashes the builder send without being counted.
    """
    clean_subject = _printable(subject, MAX_SUBJECT_CHARS)
    clean_body = _body_text(body, MAX_BODY_CHARS)
    if not clean_subject and not clean_body:
        return MailOutcome(False, "refused: nothing to say")
    try:
        (limit or RateLimit()).check_and_record()
    except BotMailRefused as exc:
        return MailOutcome(False, f"refused: {exc}")

    message = EmailMessage()
    message["To"] = RECIPIENT
    message["From"] = SENDER
    message["Subject"] = SUBJECT_PREFIX + (clean_subject or "(no subject)")
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(clean_body or "(no body)")
    try:
        # No shell, and `-t` is not used: the recipient is given on the
        # command line from the constant above, so even a message whose
        # headers were somehow malformed cannot redirect delivery.
        result = runner(
            [sendmail_path, "-i", "--", RECIPIENT],
            input=message.as_bytes(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return MailOutcome(False, f"could not invoke sendmail: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()[:300]
        return MailOutcome(False, f"sendmail exited {result.returncode}: {detail}".strip())
    return MailOutcome(True, f"sent to {RECIPIENT}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pw-notify",
        description=(
            "Send one short message to the operator. The destination is fixed "
            "and cannot be chosen; there are no other options."
        ),
    )
    parser.add_argument("subject", help="one line, truncated")
    parser.add_argument(
        "body", nargs="?", default="",
        help="message text; read from stdin when omitted",
    )
    arguments = parser.parse_args(argv)
    body = arguments.body
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()
    outcome = send(arguments.subject, body)
    print(outcome.detail)
    return 0 if outcome.sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
