#!/usr/bin/env python3
"""Import an lreview results directory into the benchmark dataset.

One "run" is one (agent, model, effort) reviewing every case in
cases/manifest.json. The importer copies the small artifacts verbatim
(manifest entry, findings JSON, Markdown report), gzips the agent event
log, and derives the per-review metrics that make runs comparable --
including tool-call counts, which is the only direct measure of how
much work the agent actually did.

    ./import_run.py <results-dir> <run-id> [--note TEXT]

Run ids are conventionally <date>-<model>-<effort>, e.g.
2026-09-09-gpt-5.6-sol-medium.
"""

import argparse
import gzip
import json
import shutil
from pathlib import Path

DATASET = Path(__file__).resolve().parent


def count_tool_calls(log_path):
    """Agent actions in the event log, per backend log format.

    claude stream-json: tool_use blocks in assistant messages.
    codex --json: command_execution items. Returns (calls, shell_cmds).
    """
    calls = shell = 0
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind == "assistant":  # claude
                    for block in (obj.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            calls += 1
                            if block.get("name") in ("Bash", "BashOutput"):
                                shell += 1
                elif kind == "item.completed":  # codex
                    item = obj.get("item") or {}
                    calls += 1
                    if item.get("type") == "command_execution":
                        shell += 1
    except OSError:
        return None, None
    return calls, shell


def load_findings(results_dir, entry):
    """The findings themselves, flattened to path/line/kind/message."""
    name = entry.get("json")
    if not name:
        return []
    spec = json.loads((results_dir / name).read_text())
    out = []
    comments = spec.get("comments")
    items = []
    if isinstance(comments, dict):
        for path, lst in comments.items():
            for it in lst:
                items.append((path, it))
    else:
        for it in spec.get("findings") or []:
            items.append((it.get("path"), it))
    for path, it in items:
        msg = (it.get("message") or "").strip()
        kind = None
        if msg.startswith("("):
            kind = msg[1:msg.find(")")] if ")" in msg[:20] else None
        out.append({"path": path, "line": it.get("line"),
                    "kind": kind, "message": msg})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("run_id")
    ap.add_argument("--note", default="")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing run of the same id")
    args = ap.parse_args()

    results = args.results_dir.expanduser().resolve()
    summary = json.loads((results / "summary.json").read_text())
    cases = json.loads((DATASET / "cases" / "manifest.json").read_text())
    by_sha = {c["sha"]: c for c in cases["cases"]}

    dest = DATASET / "runs" / args.run_id
    if dest.exists() and not args.force:
        raise SystemExit(f"{dest} already exists (use --force)")
    for sub in ("findings", "reports", "logs"):
        (dest / sub).mkdir(parents=True, exist_ok=True)

    reviews, agents, models, efforts = [], set(), set(), set()
    for key, entry in sorted(summary.items()):
        sha = entry.get("sha")
        case = by_sha.get(sha)
        if case is None:
            print(f"  skipping {key}: sha {sha[:12] if sha else '?'} is not a dataset case")
            continue
        case_id = f"{case['change']}_ps{case['patchset']}"
        agents.add(entry.get("agent"))
        models.add(entry.get("model"))
        efforts.add(entry.get("effort"))

        calls = shell = None
        if entry.get("log"):
            src = results / entry["log"]
            if src.is_file():
                calls, shell = count_tool_calls(src)
                with open(src, "rb") as fin, \
                        gzip.open(dest / "logs" / f"{case_id}.log.gz", "wb") as fout:
                    shutil.copyfileobj(fin, fout)
        if entry.get("json"):
            shutil.copy(results / entry["json"],
                        dest / "findings" / f"{case_id}.json")
        if entry.get("markdown"):
            src = results / entry["markdown"]
            if src.is_file():
                shutil.copy(src, dest / "reports" / f"{case_id}.md")

        reviews.append({
            "case": case_id,
            "sha": sha,
            "subject": entry.get("subject"),
            "status": entry.get("status"),
            "finding_count": entry.get("findings"),
            "severity": entry.get("severity"),
            "duration_s": entry.get("duration_s"),
            "tokens": entry.get("tokens"),
            "cost_usd": entry.get("cost_usd"),
            "tool_calls": calls,
            "shell_commands": shell,
            "findings": load_findings(results, entry),
        })

    def one(values, name):
        values = {v for v in values if v is not None}
        if len(values) > 1:
            raise SystemExit(f"results dir mixes {name}: {values}")
        return values.pop() if values else None

    run = {
        "run_id": args.run_id,
        "agent": one(agents, "agents"),
        "model": one(models, "models"),
        "effort": one(efforts, "efforts"),
        "note": args.note,
        "review_count": len(reviews),
        "totals": {
            "findings": sum(r["finding_count"] or 0 for r in reviews),
            "duration_s": sum(r["duration_s"] or 0 for r in reviews),
            "tokens": sum(r["tokens"] or 0 for r in reviews),
            "cost_usd": (round(sum(r["cost_usd"] or 0 for r in reviews), 2)
                         if any(r["cost_usd"] for r in reviews) else None),
        },
        "reviews": reviews,
    }
    with open(dest / "run.json", "w") as f:
        json.dump(run, f, indent=2)
        f.write("\n")
    t = run["totals"]
    print(f"{args.run_id}: {run['review_count']} reviews, {t['findings']} findings, "
          f"{t['duration_s']}s, {t['tokens']:,} tok, "
          f"cost {t['cost_usd'] if t['cost_usd'] is not None else 'n/a'}")
    print(f"  -> {dest}")


if __name__ == "__main__":
    main()
