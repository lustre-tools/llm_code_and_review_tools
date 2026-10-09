"""lreview stats: where reviews spent their time and money.

Reads review logs (or the .telemetry.json saved beside them) and prints
one line per review, then the totals: dollars by token class, wall time
split between the model and the tools, and what each kind of tool
output cost to write into the context and read back on every later call.
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

from .telemetry import parse_log


def _load(path: Path):
    """(summary, detail) for a log or a saved telemetry file; detail
    holds the per-call and per-tool rows."""
    if path.name.endswith(".telemetry.json"):
        data = json.loads(path.read_text())
        return data, data
    tel = parse_log(path)
    return tel.summary(), tel.to_json()


def find_logs(paths, results_dir: Path, last: int):
    """Logs named on the command line (files or directories), else the
    newest `last` in the results directory."""
    found = []
    for raw in paths or [results_dir]:
        path = Path(raw).expanduser()
        if path.is_dir():
            found += sorted(path.glob("kreview-*.log"),
                            key=lambda p: p.stat().st_mtime)[-last:]
        elif path.is_file():
            found.append(path)
    return found


def _money(value) -> str:
    return f"${value:,.2f}" if value is not None else "-"


def _minutes(seconds) -> str:
    return f"{(seconds or 0) / 60:5.1f}m"


def _k(tokens) -> str:
    return f"{(tokens or 0) / 1000:,.0f}K"


def render(rows, details=None, calls=False, top=10) -> str:
    lines = []
    head = (f"{'review':44s} {'cost':>7s} {'wall':>6s} {'model':>6s} "
            f"{'calls':>5s} {'start':>6s} {'peak':>6s}  "
            "write/read/output")
    lines.append(head)
    for row in rows:
        split = row.get("split_usd") or {}
        total = sum(split.values()) or 1
        lines.append(
            f"{row['log'][:44]:44s} {_money(row.get('cost_usd')):>7s} "
            f"{_minutes(row.get('wall_s')):>6s} "
            f"{_minutes(row.get('model_s')):>6s} "
            f"{row.get('main_calls') or 0:5d} "
            f"{_k(row.get('starting_context')):>6s} "
            f"{_k(row.get('peak_context')):>6s}  "
            f"{split.get('cache_write', 0) / total:4.0%}/"
            f"{split.get('cache_read', 0) / total:.0%}/"
            f"{split.get('output', 0) / total:.0%}")
    if not rows:
        return "no review logs found"

    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    walls = [r["wall_s"] for r in rows if r.get("wall_s")]
    lines.append("")
    lines.append(f"{len(rows)} review(s): cost total {_money(sum(costs))}, "
                 f"median {_money(statistics.median(costs))}; wall median "
                 f"{_minutes(statistics.median(walls)).strip()}")

    split = defaultdict(float)
    for row in rows:
        for key, value in (row.get("split_usd") or {}).items():
            split[key] += value
    total = sum(split.values()) or 1
    lines.append("dollars by token class: " + ", ".join(
        f"{k} {v / total:.0%}" for k, v in
        sorted(split.items(), key=lambda kv: -kv[1])))

    model = sum(r.get("model_s") or 0 for r in rows)
    tools = sum(r.get("tool_s") or 0 for r in rows)
    wall = sum(walls) or 1
    lines.append(f"wall time: model {model / wall:.0%}, tools "
                 f"{tools / wall:.0%}")
    starting = [r.get("starting_context") or 0 for r in rows]
    lines.append(f"context before the patch is read: median "
                 f"{_k(statistics.median(starting))} tokens")

    cats = defaultdict(lambda: defaultdict(float))
    for row in rows:
        for name, cat in (row.get("categories") or {}).items():
            for key, value in cat.items():
                cats[name][key] += value
    if cats:
        n = len(rows)
        lines.append("")
        lines.append("tool output, per review: what it cost to write "
                     "into the context and re-read")
        lines.append(f"  {'kind':11s} {'calls':>6s} {'tokens':>8s} "
                     f"{'cost':>7s} {'tool time':>9s}")
        for name, cat in sorted(cats.items(), key=lambda kv: -kv[1]["cost"]):
            lines.append(
                f"  {name:11s} {cat['count'] / n:6.1f} "
                f"{_k(cat['tokens'] / n):>8s} "
                f"{_money(cat['cost'] / n):>7s} "
                f"{cat['seconds'] / n:8.0f}s")

    if details:
        outputs = []
        for detail in details:
            for tool in detail.get("tools_detail") or []:
                outputs.append((tool.get("cost", 0), detail["log"], tool))
        outputs.sort(key=lambda item: -item[0])
        if outputs:
            lines.append("")
            lines.append(f"costliest tool outputs (top {top}):")
            for cost, log, tool in outputs[:top]:
                lines.append(
                    f"  {_money(cost):>6s} {_k(tool['tokens']):>5s} x "
                    f"{tool['later_calls']:3d} later calls  "
                    f"{tool['what'][:90]}")

    if calls and details:
        for detail in details:
            lines.append("")
            lines.append(f"calls of {detail['log']}:")
            tools = {t["id"]: t for t in detail.get("tools_detail") or []}
            for call in detail.get("calls_detail") or []:
                if call["agent"] != "main":
                    continue
                what = " | ".join(tools[i]["what"][:60]
                                  for i in call["tools"] if i in tools)
                lines.append(
                    f"  {call['index']:3d} {call['seconds']:6.1f}s "
                    f"out {call['output']:6d} ctx {_k(call['context']):>5s} "
                    f"{_money(call['cost']):>6s}  {what[:110]}")
    return "\n".join(lines)


def cmd_stats(args) -> int:
    logs = find_logs(args.paths, Path(args.results_dir), args.last)
    rows, details = [], []
    for log in logs:
        try:
            summary, detail = _load(log)
        except (OSError, ValueError) as exc:
            print(f"skipping {log}: {exc}")
            continue
        if args.complete and not summary.get("complete"):
            continue
        rows.append(summary)
        details.append(detail)
    if args.json:
        print(json.dumps(details if args.calls else rows, indent=1))
        return 0
    print(render(rows, details, calls=args.calls, top=args.top))
    return 0
