"""Persistent per-change review memory (the lreview-db directory).

With `run --memory`, each change gets a Markdown document that the
review agent reads before analyzing (to skip re-deriving what the
patch does, which findings were already reported, and which false
positives were already eliminated) and rewrites afterwards. Modeled
on the ai_docs per-patch-notes workflow.

Documents are keyed by Gerrit Change-Id when available — so a local
pre-push review and later Gerrit reviews of the same patch share one
document — with the change number and subject slug as fallbacks.
Without --memory the database is neither read nor written.
"""

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

DB_DIRNAME = "lreview-db"

# Instructions handed to the agent alongside the review prompt
MEMORY_PROMPT_PATH = Path(__file__).resolve().parent / "memory-prompt.md"

_CHANGE_ID_LINE_RE = re.compile(
    r"^change-id:\s*(I[0-9a-f]{8,40})\s*$", re.MULTILINE)


def default_db_dir(repo_root: Path) -> Path:
    """$LREVIEW_DB, else lreview-db/ in the llm tools checkout
    (gitignored); a cwd-relative lreview-db for installs without a
    checkout (e.g. CI's plain pip install)."""
    env = os.environ.get("LREVIEW_DB")
    if env:
        return Path(env).expanduser()
    if (repo_root / ".git").exists():
        return repo_root / DB_DIRNAME
    return Path(DB_DIRNAME)


def _slug(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len].rstrip("_")


def _new_doc_name(change) -> str:
    if change.number:
        return f"{change.number}-{_slug(change.subject)}.md"
    change_id = getattr(change, "change_id", None)
    if change_id:
        return f"{change_id[:9]}-{_slug(change.subject)}.md"
    return f"local-{_slug(change.subject)}.md"


def find_doc(db_dir: Path, change) -> Optional[Path]:
    """Locate the memory document for a change.

    Change-Id match in the frontmatter wins (shared between local and
    Gerrit reviews of the same patch); then the change-number filename
    prefix; then the exact name a new document would get.
    """
    if not db_dir.is_dir():
        return None

    change_id = getattr(change, "change_id", None)
    if change_id:
        for path in sorted(db_dir.glob("*.md")):
            try:
                head = path.read_text(errors="replace")[:2000]
            except OSError:
                continue
            match = _CHANGE_ID_LINE_RE.search(head)
            if match and match.group(1) == change_id:
                return path

    if change.number:
        candidates = sorted(db_dir.glob(f"{change.number}-*.md"))
        if candidates:
            return candidates[0]

    candidate = db_dir / _new_doc_name(change)
    if candidate.is_file():
        return candidate
    return None


def ensure_doc(db_dir: Path, change) -> Path:
    """Return the change's memory document, creating a skeleton with
    identity frontmatter when none exists yet."""
    existing = find_doc(db_dir, change)
    if existing:
        return existing

    db_dir.mkdir(parents=True, exist_ok=True)
    path = db_dir / _new_doc_name(change)
    change_id = getattr(change, "change_id", None)
    frontmatter = ["---"]
    if change_id:
        frontmatter.append(f"change-id: {change_id}")
    if change.number:
        frontmatter.append(f"number: {change.number}")
    frontmatter += [
        f"subject: {change.subject}",
        f"created: {date.today().isoformat()}",
        "reviews: 0",
        "last-reviewed: never",
        "---",
        "",
        "No notes yet — the first --memory review run fills this in.",
        "",
    ]
    path.write_text("\n".join(frontmatter))
    return path


def clear_doc(db_dir: Path, change) -> Optional[Path]:
    """Delete the change's memory document (for --clear-memory).

    Returns the removed path, or None if there was nothing."""
    existing = find_doc(db_dir, change)
    if existing:
        existing.unlink()
        return existing
    return None


_REVIEWS_LINE_RE = re.compile(r"^reviews:\s*(\d+)\s*$", re.MULTILINE)
_HISTORY_BULLET_RE = re.compile(r"^- \d{4}-\d{2}-\d{2} ", re.MULTILINE)


def bump_review_count(path: Path,
                      doc_includes_this_run: bool = False) -> int:
    """Increment the document's completed-review counter; returns it.

    The `reviews:` frontmatter line is maintained by lreview, not by
    the review agent — the runner bumps it after every review that
    ran to completion, so the number is deterministic. A document
    from before the counter existed is seeded from its History
    section (one bullet per run); the bump runs after the agent's
    rewrite, so when the document was updated this run its History
    already includes the current run — doc_includes_this_run says
    so, and the seed then must not add 1 on top or the current run
    is counted twice.
    """
    text = path.read_text()
    has_frontmatter = text.startswith("---")
    end = text.find("---", 3) if has_frontmatter else -1
    head, tail = (text[:end], text[end:]) if end > 0 else ("", text)

    match = _REVIEWS_LINE_RE.search(head)
    if match:
        count = int(match.group(1)) + 1
        head = (head[:match.start()] + f"reviews: {count}"
                + head[match.end():])
    else:
        history = text.split("## History", 1)
        bullets = (len(_HISTORY_BULLET_RE.findall(history[1]))
                   if len(history) > 1 else 0)
        count = max(1, bullets + (0 if doc_includes_this_run else 1))
        line = f"reviews: {count}\n"
        anchor = head.find("last-reviewed:")
        if anchor >= 0:
            head = head[:anchor] + line + head[anchor:]
        elif head:
            head = head.rstrip("\n") + "\n" + line
        else:
            # the agent rewrote the doc without any frontmatter —
            # restore a minimal block rather than appending noise
            head, tail = f"---\n{line}---\n\n", text
    path.write_text(head + tail)
    return count


@dataclass(frozen=True)
class ClaudeSession:
    """The Claude session of the last completed review in one mode."""
    session_id: str
    # what that session reviewed: "ps<N> <sha12>" or "commit <sha12>"
    reviewed: str


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter up to its closing ---, the rest); ("", text) if none."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            return text[:end + 1], text[end + 1:]
    return "", text


def _session_line_re(mode: str) -> re.Pattern:
    return re.compile(rf"^claude-session-{re.escape(mode)}:[ \t]*(.*?)[ \t]*$",
                      re.MULTILINE)


def read_session(path: Path, mode: str) -> Optional[ClaudeSession]:
    """The session recorded for `mode`, from the frontmatter only."""
    try:
        head, _ = _split_frontmatter(path.read_text(errors="replace"))
    except OSError:
        return None
    match = _session_line_re(mode).search(head)
    if not match or not match.group(1):
        return None
    session_id, _, rest = match.group(1).partition(" ")
    words = rest.split()
    if words and _DATE_RE.fullmatch(words[-1]):
        words = words[:-1]
    return ClaudeSession(session_id, " ".join(words))


def record_session(path: Path, mode: str, session_id: str,
                   reviewed: str) -> None:
    """Record the review's Claude session for `mode`, replacing any
    earlier one. Like `reviews:`, the line is lreview's, not the
    agent's."""
    line = (f"claude-session-{mode}: {session_id} {reviewed} "
            f"{date.today().isoformat()}")
    text = path.read_text()
    head, tail = _split_frontmatter(text)
    pattern = _session_line_re(mode)
    if pattern.search(head):
        head = pattern.sub(line, head, count=1)
    elif head:
        head = head.rstrip("\n") + "\n" + line + "\n"
    else:
        # the agent rewrote the doc without any frontmatter
        head, tail = f"---\n{line}\n", "---\n\n" + text
    path.write_text(head + tail)
