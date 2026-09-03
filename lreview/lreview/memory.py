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
