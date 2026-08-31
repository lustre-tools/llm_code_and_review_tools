"""Render a whole batch of reviews as one plain-text dump.

The Markdown reports under <results-dir>/markdown/ are per review and
meant for reading in a Markdown viewer; this module produces the
single concatenated text file `lreview run --output FILE` writes, so a
local run over the last N commits leaves one self-contained file with
every review in it -- including the ones that came back clean or
failed, which get no Markdown report at all.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .markdown import iter_findings
from .ui import elapsed, format_tokens

# Imported for the count-vs-status wording below; runner does not
# import this module, so the dependency stays one-way.
STATUS_FINDINGS = "findings"

RULE = "=" * 72
THIN = "-" * 72


def _location(path: str, entry: dict) -> str:
    rng = entry.get("range")
    if rng:
        loc = f"lines {rng.get('start_line')}-{rng.get('end_line')}"
    elif entry.get("line") is not None:
        loc = f"line {entry['line']}"
    else:
        loc = "file-level"
    if entry.get("side") == "PARENT":
        loc += ", old side"
    return f"{path} ({loc})"


def _indent(text: str, prefix: str = "    ") -> str:
    body = (text or "").strip()
    if not body:
        return ""
    return "\n".join(
        prefix + line if line.strip() else "" for line in body.splitlines())


def _headline(change) -> str:
    if change.number is None:
        ref_name = getattr(change, "ref_name", None) or "local"
        return f"{ref_name}  {change.sha[:12]}  {change.subject}"
    return (f"change {change.number} ps{change.patchset}  "
            f"{change.sha[:12]}  {change.subject}")


def result_text(result, spec: Optional[dict] = None,
                index: Optional[int] = None,
                total: Optional[int] = None) -> str:
    """One review as plain text: header, stats, assessment, findings."""
    change = result.change
    counter = f"[{index}/{total}] " if index and total else ""
    lines = [RULE, f"{counter}{_headline(change)}", RULE, ""]

    status = result.status
    # A review can report the findings status with an empty comment
    # list; spell the count out rather than leaving a bare "findings".
    if result.findings or result.status == STATUS_FINDINGS:
        status += f" ({result.findings} finding(s))"
    if result.severity:
        status += f", severity {result.severity}"
    lines.append(f"status: {status}")

    run_bits = []
    if result.model:
        run_bits.append(result.model)
    if result.tokens is not None:
        run_bits.append(f"{format_tokens(result.tokens)} tokens")
    if result.cost_usd is not None:
        run_bits.append(f"${result.cost_usd:.2f}")
    if result.duration:
        run_bits.append(elapsed(result.duration))
    if run_bits:
        lines.append(f"run:    {', '.join(run_bits)}")
    if result.error:
        lines.append(f"error:  {result.error}")
    lines.append("")

    if not spec:
        lines.append("No findings reported for this commit."
                     if result.status == "clean"
                     else "No review output was produced for this commit.")
        lines.append("")
        return "\n".join(lines)

    message = spec.get("message")
    if message:
        lines += ["Overall assessment", THIN, message.strip(), ""]

    findings = list(iter_findings(spec))
    lines += [f"Findings ({len(findings)})", THIN, ""]
    for i, (path, entry) in enumerate(findings, 1):
        note = ("" if entry.get("unresolved", True)
                else "  [informational]")
        lines.append(f"({i}) {_location(path, entry)}{note}")
        body = _indent(entry.get("message", ""))
        if body:
            lines.append(body)
        lines.append("")

    return "\n".join(lines)


def batch_text(results, repo: Optional[Path] = None,
               title: str = "lreview batch") -> str:
    """The full dump for a batch, newest-first in the given order."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = [RULE, title, RULE,
              f"generated: {stamp}"]
    if repo:
        header.append(f"repository: {repo}")
    header.append(f"reviews: {len(results)}")

    total_cost = sum(r.cost_usd for r in results if r.cost_usd)
    total_tokens = sum(r.tokens for r in results if r.tokens)
    if total_tokens:
        totals = f"totals: {format_tokens(total_tokens)} tokens"
        if total_cost:
            totals += f", ${total_cost:.2f}"
        header.append(totals)
    header.append("")

    for i, result in enumerate(results, 1):
        count = (f" ({result.findings})"
                 if result.findings or result.status == STATUS_FINDINGS
                 else "")
        header.append(f"  {i}. {_headline(result.change)} -- "
                      f"{result.status}{count}")
    header.append("")

    chunks = ["\n".join(header)]
    for i, result in enumerate(results, 1):
        spec = None
        if result.json_path and Path(result.json_path).is_file():
            try:
                loaded = json.loads(Path(result.json_path).read_text())
                if isinstance(loaded, dict):
                    spec = loaded
            except (OSError, ValueError):
                spec = None
        chunks.append(result_text(result, spec, index=i,
                                  total=len(results)))
    return "\n".join(chunks).rstrip() + "\n"


def write_batch_text(path: Path, results, repo: Optional[Path] = None,
                     title: str = "lreview batch") -> Path:
    path = Path(path).expanduser()
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(batch_text(results, repo=repo, title=title))
    return path
