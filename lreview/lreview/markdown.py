"""Render collected review JSONs as human-readable Markdown reports.

Every review with findings gets a report at
<results-dir>/markdown/<change>_<subject>_ps<N>.md so the results can
be read comfortably without posting them to Gerrit.
"""

import re
from pathlib import Path
from typing import Any, Optional

from .gerrit import ResolvedChange
from .ui import elapsed, format_tokens

MARKDOWN_SUBDIR = "markdown"


def _sanitize(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len].rstrip("_")


def markdown_filename(change: ResolvedChange) -> str:
    return (f"{change.number}_{_sanitize(change.subject)}"
            f"_ps{change.patchset}.md")


def _iter_findings(spec: dict):
    """Yield (path, comment) in file order for both JSON shapes."""
    comments = spec.get("comments") or {}
    if isinstance(comments, dict):
        for path, entries in comments.items():
            for entry in entries:
                yield path, entry
    else:
        for entry in comments:
            yield entry.get("path", "?"), entry


def _anchor(path: str, entry: dict) -> str:
    rng = entry.get("range")
    if rng:
        loc = f"lines {rng.get('start_line')}–{rng.get('end_line')}"
    elif entry.get("line") is not None:
        loc = f"line {entry['line']}"
    else:
        loc = "file-level"
    if entry.get("side") == "PARENT":
        loc += ", old side"
    return f"`{path}` ({loc})"


def review_markdown(
    change: ResolvedChange,
    spec: dict,
    severity: Optional[str] = None,
    model: Optional[str] = None,
    tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    duration: Optional[float] = None,
) -> str:
    url = (f"{change.base_url.rstrip('/')}/c/{change.project}"
           f"/+/{change.number}")
    findings = list(_iter_findings(spec))

    review_bits = [f"{len(findings)} finding(s)"]
    if severity:
        review_bits.append(f"severity **{severity}**")
    run_bits = []
    if model:
        run_bits.append(model)
    if tokens is not None:
        run_bits.append(f"{format_tokens(tokens)} tokens")
    if cost_usd is not None:
        run_bits.append(f"${cost_usd:.2f}")
    if duration is not None:
        run_bits.append(elapsed(duration))

    lines = [
        f"# {change.number} ps{change.patchset} — {change.subject}",
        "",
        f"- **Change:** {url} (patchset {change.patchset}, "
        f"`{change.sha[:12]}`)",
        f"- **Review:** {', '.join(review_bits)}",
    ]
    if run_bits:
        lines.append(f"- **Run:** {', '.join(run_bits)}")
    lines.append("")

    message = spec.get("message")
    if message:
        lines += ["## Overall assessment", "", message, ""]

    lines += ["## Findings", ""]
    for i, (path, entry) in enumerate(findings, 1):
        note = ("" if entry.get("unresolved", True)
                else " *(informational)*")
        lines.append(f"### {i}. {_anchor(path, entry)}{note}")
        lines.append("")
        lines.append(entry.get("message", "").strip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_review_markdown(
    results_dir: Path,
    change: ResolvedChange,
    spec: dict,
    **stats: Any,
) -> Path:
    md_dir = results_dir / MARKDOWN_SUBDIR
    md_dir.mkdir(parents=True, exist_ok=True)
    path = md_dir / markdown_filename(change)
    path.write_text(review_markdown(change, spec, **stats))
    return path
