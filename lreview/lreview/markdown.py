"""Render collected review JSONs as human-readable Markdown reports.

Every review with findings gets a report at
<results-dir>/markdown/<change>_<subject>_ps<N>.md so the results can
be read comfortably without posting them to Gerrit.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .gerrit import ResolvedChange, change_ref
from .ui import elapsed, format_tokens

MARKDOWN_SUBDIR = "markdown"

_REVIEW_JSON_RE = re.compile(r"gerrit-review-(\d+)_ps(\d+)\.json$")


def _sanitize(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len].rstrip("_")


def markdown_filename(change) -> str:
    if change.number is None:  # local review: keyed by ref + sha
        return f"{change.slug}_{_sanitize(change.subject)}.md"
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

    if change.number is None:  # local review
        ref_name = getattr(change, "ref_name", "local")
        lines = [
            f"# {ref_name} — {change.subject}",
            "",
            f"- **Commit:** `{change.sha[:12]}` ({ref_name}, local "
            "review — not tied to a Gerrit change)",
            f"- **Review:** {', '.join(review_bits)}",
        ]
    else:
        url = (f"{change.base_url.rstrip('/')}/c/{change.project}"
               f"/+/{change.number}")
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


def _reconstruct(json_path: Path, number: int, patchset: int):
    """Best-effort (change, stats) for an existing review JSON, from
    summary.json when its entry matches this patchset, else from the
    review-metadata sidecar, else minimal."""
    results_dir = json_path.parent
    entry: dict = {}
    summary_path = results_dir / "summary.json"
    if summary_path.is_file():
        try:
            candidate = json.loads(summary_path.read_text()).get(
                str(number)) or {}
            if candidate.get("patchset") == patchset:
                entry = candidate
        except json.JSONDecodeError:
            pass

    subject = entry.get("subject")
    sha = entry.get("sha")
    if not subject or not sha:
        metadata_path = (results_dir /
                         f"review-metadata-{number}_ps{patchset}.json")
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text())
                subject = subject or metadata.get("subject")
                sha = sha or metadata.get("sha")
            except json.JSONDecodeError:
                pass

    base_url = (entry.get("base_url")
                or os.environ.get("GERRIT_URL")
                or "https://review.whamcloud.com")
    change = ResolvedChange(
        number=number, project="fs/lustre-release",
        subject=subject or f"change {number}",
        sha=sha or "0" * 40, patchset=patchset,
        ref=change_ref(number, patchset), base_url=base_url)
    stats = {
        "severity": entry.get("severity"),
        "model": entry.get("model"),
        "tokens": entry.get("tokens"),
        "cost_usd": entry.get("cost_usd"),
        "duration": entry.get("duration_s"),
    }
    return change, stats


def render_existing(
    files: Optional[list[Path]] = None,
    results_dir: Optional[Path] = None,
):
    """Render existing gerrit-review-*.json files to Markdown.

    Returns (written_paths, skipped) where skipped is a list of
    (path, reason) for files that could not be rendered.
    """
    if files:
        targets = [Path(f) for f in files]
    else:
        targets = sorted((results_dir or Path(".")).glob(
            "gerrit-review-*.json"))

    written: list[Path] = []
    skipped: list[tuple] = []
    for json_path in targets:
        match = _REVIEW_JSON_RE.search(json_path.name)
        if not match:
            skipped.append((json_path,
                            "name is not gerrit-review-<N>_ps<M>.json"))
            continue
        try:
            spec = json.loads(json_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append((json_path, str(exc)))
            continue
        if not isinstance(spec, dict):
            skipped.append((json_path, "not a JSON object"))
            continue
        change, stats = _reconstruct(
            json_path, int(match.group(1)), int(match.group(2)))
        written.append(write_review_markdown(
            json_path.parent, change, spec, **stats))
    return written, skipped
