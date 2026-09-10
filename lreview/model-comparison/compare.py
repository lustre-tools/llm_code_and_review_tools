#!/usr/bin/env python3
"""Compare imported benchmark runs against each other and the reference set.

    ./compare.py                      # every run in runs/
    ./compare.py <run-id> <run-id>    # just these, in this order

Prints cost/speed per run, findings per case, and the per-case overlap
with the aireview reference set. Overlap is reported as same-file and
same-file-within-N-lines matches -- deliberately crude, because two
reviewers phrasing the same defect differently is not something string
matching can settle. Treat it as a pointer to what to read, not a score.
"""

import argparse
import json
from pathlib import Path

DATASET = Path(__file__).resolve().parent
NEAR_LINES = 25


def load_runs(ids=None):
    runs = []
    for path in sorted((DATASET / "runs").glob("*/run.json")):
        run = json.loads(path.read_text())
        if ids and run["run_id"] not in ids:
            continue
        runs.append(run)
    if ids:
        runs.sort(key=lambda r: ids.index(r["run_id"]))
    return runs


def load_reference():
    path = DATASET / "ground-truth" / "aireview.json"
    return json.loads(path.read_text())["cases"] if path.is_file() else {}


def fmt(n, width=0, na="-"):
    return f"{n:>{width},}" if isinstance(n, (int, float)) and n else f"{na:>{width}}"


def hhmm(seconds):
    if not seconds:
        return "-"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def overlap(findings, ref_findings):
    """(same file, same file within NEAR_LINES) counts."""
    same_file = near = 0
    for f in findings:
        for r in ref_findings:
            if f.get("path") != r.get("path"):
                continue
            same_file += 1
            fl, rl = f.get("line"), r.get("line")
            if fl and rl and abs(fl - rl) <= NEAR_LINES:
                near += 1
            break
    return same_file, near


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_ids", nargs="*")
    ap.add_argument("--findings", action="store_true",
                    help="also print every finding, grouped by case")
    args = ap.parse_args()

    runs = load_runs(args.run_ids or None)
    if not runs:
        raise SystemExit("no runs found -- import one with ./import_run.py")
    ref = load_reference()

    print("=" * 78)
    print("RUNS")
    print("=" * 78)
    head = (f"{'run':<32} {'find':>5} {'wall':>8} {'mean':>7} "
            f"{'tokens':>12} {'cost':>7} {'calls':>6}")
    print(head)
    for run in runs:
        t = run["totals"]
        n = run["review_count"] or 1
        calls = sum(r.get("tool_calls") or 0 for r in run["reviews"])
        print(f"{run['run_id']:<32} {t['findings']:>5} "
              f"{hhmm(t['duration_s']):>8} {hhmm(t['duration_s'] / n):>7} "
              f"{fmt(t['tokens'], 12)} "
              f"{('$' + format(t['cost_usd'], '.2f')) if t['cost_usd'] else '-':>7} "
              f"{calls:>6}")

    print()
    print("=" * 78)
    print(f"FINDINGS PER CASE  (ref = aireview; ~ = same file within {NEAR_LINES} lines)")
    print("=" * 78)
    cases = sorted({r["case"] for run in runs for r in run["reviews"]})
    label = f"{'case':<14} {'ref':>4}"
    for run in runs:
        label += f" | {run['model'] or run['agent']}/{run['effort'] or 'dflt'}"[:26]
    print(label)
    for case in cases:
        refs = ref.get(case, {}).get("findings", [])
        line = f"{case:<14} {len(refs):>4}"
        for run in runs:
            entry = next((r for r in run["reviews"] if r["case"] == case), None)
            if entry is None:
                line += " | " + f"{'-':>10}"
                continue
            same, near = overlap(entry["findings"], refs)
            line += f" | {entry['finding_count']:>3} ({same} file, {near}~)"
        print(line)

    print()
    print("=" * 78)
    print("PER-RUN CASE DETAIL")
    print("=" * 78)
    for run in runs:
        print(f"\n--- {run['run_id']}  ({run['agent']} {run['model']} "
              f"effort={run['effort']})")
        for r in run["reviews"]:
            print(f"  {r['case']:<14} {r['status']:<12} "
                  f"{r['finding_count']:>2} finding(s)  "
                  f"{hhmm(r['duration_s']):>7}  {fmt(r['tokens'], 11)} tok  "
                  f"{('$' + format(r['cost_usd'], '.2f')) if r['cost_usd'] else '-':>6}  "
                  f"{r.get('tool_calls') or '-':>3} calls")
            if args.findings:
                for f in r["findings"]:
                    head = f"({f['kind']}) " if f.get("kind") else ""
                    text = " ".join(f["message"].split())[:100]
                    print(f"      {f['path']}:{f['line']}  {head}{text}...")


if __name__ == "__main__":
    main()
