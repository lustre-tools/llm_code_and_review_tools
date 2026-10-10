"""lreview bench: a fixed set of reviews to measure lreview against.

Each case in benchmark/cases.json is a merged Lustre change, most with bugs
that a later commit fixed (its Fixes: trailer names the change), so
whether a review finds them can be checked.  `bench run` reviews every
case with the options given, `--reps` times, and `bench report` says
what each arm cost, how long it took, and which known bugs it found.

A case is reviewed in a repository holding only its own history: the
source checkout has the later fixes, and the review protocol has the
reviewer look forward in git for them.  See prepare_repos().
"""

import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .gerrit import LocalChange, change_ref
from .telemetry import parse_log

CASES_PATH = Path(__file__).resolve().parent / "benchmark" / "cases.json"
GERRIT_URL = "https://review.whamcloud.com/fs/lustre-release"
RUN_FILE = "bench-run.json"
JUDGE_MODEL = "sonnet"


def load_cases(path: Path = CASES_PATH) -> dict:
    data = json.loads(Path(path).read_text())
    for case in data["cases"]:
        for bug in case.get("bugs") or []:
            bug.setdefault("match", [])
    return data


def select(cases: list, wanted: Optional[str],
           case_set: Optional[str] = None) -> list:
    """Cases named in a comma-separated list of ids, else the cases in
    `case_set` (every case when it is None or "all")."""
    if not wanted:
        if case_set in (None, "all"):
            return cases
        chosen = [c for c in cases if case_set in c.get("sets", [])]
        if not chosen:
            raise ValueError(f"no cases in set {case_set!r}")
        return chosen
    names = {w.strip() for w in wanted.split(",") if w.strip()}
    chosen = [c for c in cases if c["id"] in names]
    missing = names - {c["id"] for c in chosen}
    if missing:
        raise ValueError(f"no such case(s): {', '.join(sorted(missing))}")
    return chosen


def _git(repo: Path, *args, timeout=600, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=timeout, check=check)


def _has(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}",
                check=False).returncode == 0


def prepare_repos(bench_dir: Path, cases: list,
                  source: Optional[Path] = None) -> dict:
    """One repository per case, holding that case's commit and its
    history and nothing later; returns {case id: repository}.

    The commits are fetched by SHA, from the source checkout when it
    has them, else from the change's Gerrit ref, into a bare store
    with no refs, which every case repository borrows objects from.
    A case repository has no refs either: its review worktree's HEAD
    is the only commit git log --all can reach, so no reviewer sees
    another case's newer history (where an older case's fix can be).
    """
    store = bench_dir / "store.git"
    if not (store / "objects").is_dir():
        store.mkdir(parents=True, exist_ok=True)
        _git(store, "init", "-q", "--bare")
        # Keep the fetched commits: they are reachable from no ref.
        _git(store, "config", "gc.auto", "0")
        _git(store, "config", "gc.pruneExpire", "never")
    for case in cases:
        sha = case["sha"]
        if _has(store, sha):
            continue
        if source is not None and _git(source, "cat-file", "-e",
                                       f"{sha}^{{commit}}",
                                       check=False).returncode == 0:
            _git(store, "fetch", "-q", "--no-tags", str(source), sha)
        else:
            _git(store, "fetch", "-q", "--no-tags", GERRIT_URL,
                 change_ref(case["change"], case["patchset"]))
        if not _has(store, sha):
            raise RuntimeError(f"case {case['id']}: {sha} not fetched")
    (store / "FETCH_HEAD").unlink(missing_ok=True)
    repos = {}
    for case in cases:
        repo = bench_dir / "repos" / case["id"]
        if not (repo / ".git").is_dir():
            repo.mkdir(parents=True, exist_ok=True)
            _git(repo, "init", "-q")
            (repo / ".git" / "objects" / "info" / "alternates").write_text(
                str((store / "objects").resolve()) + "\n")
            _git(repo, "config", "gc.auto", "0")
            _git(repo, "config", "user.email", "lreview-bench@invalid")
            _git(repo, "config", "user.name", "lreview bench")
        repos[case["id"]] = repo
    return repos


def bench_change(case: dict) -> LocalChange:
    return LocalChange(ref_name=f"bench-{case['id']}", sha=case["sha"],
                       subject=case["subject"],
                       change_id=case.get("change_id"))


def environment(config, cases_path: Path = CASES_PATH) -> dict:
    """What a bench run ran with, for comparing runs made apart."""
    from . import __version__
    from .prompts import _git_out

    def version(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=30).stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return {
        "lreview": __version__,
        "agent": config.agent,
        "agent_version": version([config.agent, "--version"]),
        "model": config.model, "effort": config.effort, "mode": config.mode,
        "lean": config.lean, "preload": config.preload,
        "memory": config.memory_db is not None,
        "resume": config.memory_db is not None and config.resume,
        "agent_args": config.agent_args,
        "prompts_rev": _git_out(config.prompts_dir, "rev-parse", "HEAD"),
        "cases_file": str(cases_path),
        "cases_sha": (hashlib.sha256(Path(cases_path).read_bytes())
                      .hexdigest()[:12] if Path(cases_path).exists()
                      else None),
    }


def run_bench(config_for, cases: list, label_dir: Path, reps: int,
              repos: dict, jobs: int = 4, log=print,
              cases_path: Path = CASES_PATH) -> None:
    """Review every case `reps` times; rep k's results go to
    label_dir/rep<k>.  config_for(results_dir, repo) gives the
    BatchConfig for one case; each case runs as its own batch, in its
    own repository, `jobs` at a time.

    With review memory, the reps are rounds: every rep's config names
    the same database, label_dir/db, so round k reads the notes round
    k-1 wrote."""
    from concurrent.futures import ThreadPoolExecutor
    from .runner import kill_running_reviews, run_batch
    label_dir.mkdir(parents=True, exist_ok=True)
    meta_path = label_dir / RUN_FILE
    meta = (json.loads(meta_path.read_text()) if meta_path.exists()
            else {"started": datetime.now(timezone.utc).isoformat(
                timespec="seconds"), "reps": []})
    for _ in range(reps):
        rep = len(meta["reps"]) + 1
        results_dir = label_dir / f"rep{rep}"
        config = config_for(results_dir, repos[cases[0]["id"]])
        meta.setdefault("environment", environment(config, cases_path))
        meta["memory"] = config.memory_db is not None
        meta["cases"] = sorted(set(meta.get("cases", []))
                               | {c["id"] for c in cases})
        log(f"bench rep {rep}: {len(cases)} case(s) -> {results_dir}")
        started = time.time()
        pool = ThreadPoolExecutor(max_workers=jobs)
        try:
            futures = [pool.submit(run_batch,
                                   config_for(results_dir, repos[c["id"]]),
                                   [bench_change(c)]) for c in cases]
            for future in futures:
                future.result()
        except KeyboardInterrupt:
            kill_running_reviews()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        meta["reps"].append({"rep": rep, "dir": results_dir.name,
                             "cases": [c["id"] for c in cases],
                             "wall_s": round(time.time() - started)})
        meta_path.write_text(json.dumps(meta, indent=1))


# -- scoring ---------------------------------------------------------

def _findings(results_dir: Path, case_id: str):
    """The review's findings as a list of strings, or None when the
    case was not reviewed there; [] for a clean review."""
    spec = None
    for path in results_dir.glob(f"gerrit-review-bench-{case_id}_*.json"):
        spec = json.loads(path.read_text())
    if spec is None:
        metas = list(results_dir.glob(
            f"review-metadata-bench-{case_id}_*.json"))
        return [] if metas else None
    out = []
    if spec.get("message"):
        out.append(f"[overall] {spec['message']}")
    comments = spec.get("comments") or {}
    for path, items in (comments.items() if isinstance(comments, dict)
                        else []):
        for item in items:
            out.append(f"[{path}:{item.get('line')}] {item.get('message', '')}")
    return out


def leaked(log: Path, case: dict) -> list:
    """The case's later fixes that appear in what the review's tools
    returned: the reviewer saw the answer.  The bench repository is
    built so that this stays empty."""
    marks = set()
    for bug in case.get("bugs") or []:
        if bug.get("fix_sha"):
            marks.add(bug["fix_sha"][:10])
        if bug.get("fix_subject"):
            marks.add(bug["fix_subject"])
    seen = set()
    if not marks:
        return []
    with open(log, errors="replace") as handle:
        for line in handle:
            if '"tool_result"' not in line:
                continue
            for mark in marks:
                if mark in line:
                    seen.add(mark)
    return sorted(seen)


def _log_for(results_dir: Path, case_id: str) -> Optional[Path]:
    logs = sorted(results_dir.glob(f"kreview-bench-{case_id}_*.log"))
    return logs[-1] if logs else None


def regex_found(bug: dict, findings: list) -> Optional[int]:
    """Index of the first finding matching every pattern of any one of
    the bug's match sets, or None."""
    for idx, text in enumerate(findings):
        for patterns in bug.get("match") or []:
            if all(re.search(p, text, re.I | re.S) for p in patterns):
                return idx
    return None


def _judge_prompt(case: dict, bug: dict, findings: list) -> str:
    listing = "\n\n".join(f"#{i}: {f}" for i, f in enumerate(findings))
    return (
        "You are grading an automated code review against a known bug.\n\n"
        f"Change under review: {case['subject']}\n\n"
        f"Known bug (fixed later by {bug.get('fix_subject', 'a later commit')}):"
        f"\n{bug['summary']}\nLocations: {', '.join(bug.get('locations', []))}"
        f"\n\nThe review's findings:\n\n{listing}\n\n"
        "Did any single finding identify this bug: the same defect at the "
        "same place, with the right mechanism or consequence? A finding "
        "that only touches the same code for another reason does not "
        "count. Reply with JSON only: {\"found\": true|false, "
        "\"finding\": <index or null>, \"why\": \"<one sentence>\"}")


def llm_found(case: dict, bug: dict, findings: list,
              model: str = JUDGE_MODEL) -> dict:
    from .agents import LEAN_CLAUDE_ENV
    if not findings:
        return {"found": False, "finding": None, "why": "no findings"}
    env = dict(os.environ, **LEAN_CLAUDE_ENV)
    result = subprocess.run(
        ["claude", "-p", _judge_prompt(case, bug, findings), "--model", model,
         "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
         "--output-format", "json"],
        capture_output=True, text=True, timeout=600, env=env,
        stdin=subprocess.DEVNULL)
    try:
        text = json.loads(result.stdout).get("result", "")
        verdict = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return {"found": bool(verdict.get("found")),
                "finding": verdict.get("finding"),
                "why": verdict.get("why", "")}
    except (ValueError, AttributeError):
        return {"found": None, "finding": None,
                "why": f"judge failed: {result.stderr[-200:]}"}


def score(label_dir: Path, cases: list, judge: bool = False,
          judge_jobs: int = 6) -> dict:
    """Per rep and case: telemetry and which known bugs were found.
    LLM verdicts are asked `judge_jobs` at a time and cached in each
    rep directory."""
    from concurrent.futures import ThreadPoolExecutor
    by_id = {c["id"]: c for c in cases}
    reps = []
    for rep_dir in sorted(label_dir.glob("rep*"),
                          key=lambda p: int(p.name[3:] or 0)):
        cache_path = rep_dir / "judge.json"
        cache = (json.loads(cache_path.read_text())
                 if cache_path.exists() else {})
        rows, asks = [], []
        for case_id, case in by_id.items():
            findings = _findings(rep_dir, case_id)
            log = _log_for(rep_dir, case_id)
            if findings is None and log is None:
                continue
            row = {"case": case_id, "complete": findings is not None,
                   "findings": len(findings or []) - (
                       1 if findings and findings[0].startswith("[overall]")
                       else 0), "bugs": {}}
            if log is not None:
                row["leaked"] = leaked(log, case)
                summary = parse_log(log).summary()
                wall = summary["wall_s"]
                saved = log.with_suffix(".telemetry.json")
                if not wall and saved.exists():
                    # codex logs carry no timestamps; lreview timed it
                    wall = (json.loads(saved.read_text()).get("run") or {}
                            ).get("duration_s") or 0
                tokens = summary["tokens"]
                row.update(cost=summary["cost_usd"], wall=wall,
                           calls=summary["main_calls"],
                           peak=summary["peak_context"],
                           output=tokens["output"],
                           tokens=sum(v or 0 for k, v in tokens.items()
                                      if k != "thinking"),
                           plan_pct=summary.get("plan_pct_est"))
            for bug in case.get("bugs") or []:
                hit = regex_found(bug, findings or [])
                verdict = {"regex": hit is not None}
                if judge and findings is not None:
                    key = hashlib.sha256(json.dumps(
                        [bug["summary"], findings]).encode()).hexdigest()[:16]
                    if key not in cache:
                        asks.append((key, case, bug, findings))
                    verdict["key"] = key
                row["bugs"][bug["id"]] = verdict
            rows.append(row)
        if asks:
            with ThreadPoolExecutor(judge_jobs) as pool:
                answers = pool.map(lambda a: llm_found(a[1], a[2], a[3]), asks)
                for (key, *_), answer in zip(asks, answers):
                    cache[key] = answer
            cache_path.write_text(json.dumps(cache, indent=1))
        for row in rows:
            for verdict in row["bugs"].values():
                key = verdict.pop("key", None)
                if key is not None:
                    verdict["judge"] = cache[key]["found"]
                    verdict["why"] = cache[key]["why"]
        reps.append({"rep": rep_dir.name, "rows": rows})
    return {"label": label_dir.name, "reps": reps}


def _found(verdict: dict, judge: bool) -> bool:
    return bool(verdict.get("judge") if judge else verdict.get("regex"))


def summarize(scored: dict, judge: bool = False) -> dict:
    rows = [r for rep in scored["reps"] for r in rep["rows"]]
    # Means over finished reviews: a running or failed one has spent
    # only part of what a review costs.
    done = [r for r in rows if "wall" in r and r["complete"]]
    # A review that saw a later fix says nothing about finding the bug
    bugs = [(r["case"], b, v) for r in rows
            if r["complete"] and not r.get("leaked")
            for b, v in r["bugs"].items()]
    union = defaultdict(bool)
    for case, bug, verdict in bugs:
        union[(case, bug)] |= _found(verdict, judge)

    def mean(key):
        vals = [r[key] for r in done if r.get(key) is not None]
        return round(statistics.mean(vals), 3) if vals else None
    return {
        "label": scored["label"],
        "reviews": len(rows),
        "incomplete": sum(1 for r in rows if not r["complete"]),
        "cost_mean": mean("cost"), "wall_mean": mean("wall"),
        "calls_mean": mean("calls"), "peak_mean": mean("peak"),
        "output_mean": mean("output"),
        "tokens_mean": mean("tokens"),
        "plan_pct_mean": (round(statistics.mean(r["plan_pct"] for r in done), 4)
                          if done and all(r.get("plan_pct") is not None
                                          for r in done) else None),
        "findings_mean": (round(statistics.mean(r["findings"] for r in rows
                                                if r["complete"]), 2)
                          if any(r["complete"] for r in rows) else None),
        "bugs_found": sum(_found(v, judge) for _, _, v in bugs),
        "bug_chances": len(bugs),
        "bugs_found_any_rep": sum(union.values()),
        "bugs_known": len(union),
        "leaked": sum(1 for r in rows if r.get("leaked")),
    }


def _cost_cell(s: dict) -> str:
    if s["cost_mean"] is not None:
        return "$%.2f" % s["cost_mean"]
    if s.get("plan_pct_mean") is not None:
        return "%.3f%%p" % s["plan_pct_mean"]
    return "-"


def render(scoreds: list, judge: bool = False) -> str:
    lines = []
    head = (f"{'arm':28s} {'reviews':>7s} {'$/review':>9s} {'Mtok':>5s} {'min':>5s} "
            f"{'calls':>5s} {'peak':>6s} {'finds':>5s} "
            f"{'bugs/run':>9s} {'any run':>8s}")
    lines.append(head)
    for scored in scoreds:
        s = summarize(scored, judge)
        recall = (f"{s['bugs_found']}/{s['bug_chances']}"
                  if s["bug_chances"] else "-")
        anyrep = (f"{s['bugs_found_any_rep']}/{s['bugs_known']}"
                  if s["bugs_known"] else "-")
        lines.append(
            f"{s['label'][:28]:28s} {s['reviews']:7d} "
            f"{_cost_cell(s):>9s} "
            f"{(s['tokens_mean'] or 0) / 1e6:5.2f} "
            f"{(s['wall_mean'] or 0) / 60:5.1f} "
            f"{s['calls_mean'] or 0:5.0f} "
            f"{(s['peak_mean'] or 0) / 1000:5.0f}K "
            f"{s['findings_mean'] if s['findings_mean'] is not None else '-':>5} "
            f"{recall:>9s} {anyrep:>8s}")
    lines.append("")
    leaky = [summarize(sc, judge)["label"] for sc in scoreds
             if summarize(sc, judge)["leaked"]]
    if leaky:
        lines.append("Reviews in " + ", ".join(leaky) + " saw a case's "
                     "later fix (marked LEAKED below); their bugs are left "
                     "out of the counts.")
    lines.append("$/review is Claude's list-price figure; for codex, N%p is an "
                 "upper bound on the share of the weekly plan a review used.  "
                 "Mtok: tokens processed per review.")
    lines.append("bugs/run: known bugs found, over every review of a case "
                 "with known bugs; any run: found in at least one rep.  "
                 + ("Verdicts from the LLM judge." if judge else
                    "Verdicts from the cases' regex patterns; --judge "
                    "asks an LLM instead."))
    for scored in scoreds:
        lines.append("")
        lines.append(f"{scored['label']}:")
        for rep in scored["reps"]:
            got = [_found(v, judge) for r in rep["rows"]
                   if r["complete"] and not r.get("leaked")
                   for v in r["bugs"].values()]
            costs = [r["cost"] for r in rep["rows"] if r.get("cost") is not None]
            lines.append(f"  {rep['rep']}: known bugs {sum(got)}/{len(got)}, "
                         f"${sum(costs):.2f} for {len(rep['rows'])} review(s)")
        for rep in scored["reps"]:
            for r in rep["rows"]:
                bugs = " ".join(
                    f"{b}{'+' if _found(v, judge) else '-'}"
                    for b, v in r["bugs"].items())
                cost = f"${r['cost']:.2f}" if r.get("cost") is not None else "-"
                wall = f"{(r.get('wall') or 0) / 60:.1f}m"
                status = "" if r["complete"] else " (no result)"
                if r.get("leaked"):
                    status += " LEAKED: saw " + ", ".join(r["leaked"])
                lines.append(f"  {rep['rep']:5s} {r['case']:24s} {cost:>6s} "
                             f"{wall:>6s} {r['findings']:3d} finds  "
                             f"{bugs}{status}")
    return "\n".join(lines)


def default_label(args) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parts = [args.model or "default"]
    if args.effort:
        parts.append(args.effort)
    if args.mode != "full":
        parts.append(args.mode)
    if not args.lean:
        parts.append("nolean")
    if not args.preload:
        parts.append("nopreload")
    if args.memory:
        parts.append("memory-resume" if args.resume else "memory")
    return f"{stamp}-{'-'.join(parts)}"


