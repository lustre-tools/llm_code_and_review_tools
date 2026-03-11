#!/usr/bin/env python3
"""orchestrator.py — Pure-code patch shepherd orchestrator.

Replaces the previous architecture where Claude Haiku ran the entire
loop.  Now the code handles all mechanical work:

  Phase 1: Check each patch via watcher_tool.sh check-patch
           (gerrit status, reviews, CI, linked-bug retests)
  Phase 2: For unknown failures, call Claude with jira access
           to do real multi-round JIRA research + relatedness
  Phase 3: Execute the LLM's decisions via watcher_tool.sh
           (link-bug, raise-bug, retest, stop)
  Phase 4: Build report JSON + write via watcher_tool.sh

Most runs have zero unknown failures → zero LLM cost.
When the LLM is invoked, it does genuine research instead of
being wasted on for-loops and JSON formatting.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    from patch_shepherd.config import load_config
except ImportError:
    # When run as a script (python3 patch_shepherd/orchestrator.py),
    # the parent directory may not be on sys.path.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from patch_shepherd.config import load_config

# Module-level config — initialized lazily in main() so import
# doesn't trigger validation (allows testing / partial imports).
_config = None


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def log(msg):
    """Print to stderr (stdout is for structured output only)."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


def run(args, stdin_data=None, timeout=60):
    """Run a command, return parsed JSON or {"raw": ..., "rc": ...}.

    stderr passes through to our stderr (for debug logging).
    """
    try:
        r = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=sys.stderr,
            text=True, timeout=timeout, input=stdin_data)
        out = r.stdout.strip()
        if not out:
            return {"error": f"empty output (rc={r.returncode})",
                    "stderr": r.stderr.strip()}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out, "rc": r.returncode}
    except subprocess.TimeoutExpired:
        return {"error": f"timeout ({timeout}s)"}
    except Exception as e:
        return {"error": str(e)}


def watcher(action, *args, stdin_data=None, timeout=120):
    """Call watcher_tool.sh <action> [args...]."""
    return run(
        [_config.watcher_tool, action] + list(args),
        stdin_data=stdin_data, timeout=timeout)


# -------------------------------------------------------------------
# Phase 1: Check all patches
# -------------------------------------------------------------------

def _check_one(i, patch):
    """Check a single patch (called from thread pool)."""
    import time
    url = patch["gerrit_url"]
    ws = patch.get("watch_status", "active")
    lp = str(patch.get("last_patchset", 0))
    lr = str(patch.get("last_review_count", 0))

    t0 = time.monotonic()
    result = watcher("check-patch", url, str(i), ws, lp, lr)
    elapsed = time.monotonic() - t0

    if "error" in result and "raw" not in result:
        result = {
            "gerrit_url": url, "patch_index": i,
            "skipped": False, "actions_taken": [],
            "needs_llm_decision": [],
            "errors": [f"check-patch: {result['error']}"],
        }

    n_act = len(result.get("actions_taken", []))
    n_llm = len(result.get("needs_llm_decision", []))
    n_err = len(result.get("errors", []))
    skipped = result.get("skipped", False)
    status = "skipped" if skipped else \
        f"{n_act}act/{n_llm}llm/{n_err}err"
    desc = patch.get("description", "?")[:50]
    log(f"  [{i}] {desc} → {elapsed:.1f}s [{status}]")

    return (i, patch, result)


def check_all_patches(patches):
    """Run check-patch for all patches in parallel."""
    results = [None] * len(patches)
    with ThreadPoolExecutor(max_workers=min(5, len(patches))) as pool:
        futures = {
            pool.submit(_check_one, i, p): i
            for i, p in enumerate(patches)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as e:
                log(f"  [{i}] exception: {e}")
                results[i] = (i, patches[i], {
                    "gerrit_url": patches[i]["gerrit_url"],
                    "patch_index": i,
                    "skipped": False, "actions_taken": [],
                    "needs_llm_decision": [],
                    "errors": [f"check-patch exception: {e}"],
                })
    return results


# -------------------------------------------------------------------
# Series-aware blocking
# -------------------------------------------------------------------

def _change_number_from_url(url):
    """Extract the numeric change ID from a Gerrit URL."""
    import re
    m = re.search(r"/\+/(\d+)", url)
    return int(m.group(1)) if m else None


def propagate_series_blocks(patches, results):
    """Stop dependents when a patch in the series has a negative CR.

    For each patch with negative_cr=True, call 'gerrit related' to get
    the full relation chain, then stop any watched patches that appear
    downstream (later in the chain) from the blocked patch.

    Returns list of (patch_index, patch, reason) tuples for newly blocked patches.
    """
    newly_blocked = []

    # Find patches with negative human CR
    blocked_indices = set()
    for i, patch, result in results:
        if result.get("negative_cr") and not result.get("skipped"):
            blocked_indices.add(i)

    if not blocked_indices:
        return newly_blocked

    # For each blocked patch, get its relation chain
    for blocked_i in blocked_indices:
        blocked_patch = patches[blocked_i]
        blocked_url = blocked_patch["gerrit_url"]
        blocked_change = _change_number_from_url(blocked_url)
        if not blocked_change:
            continue

        log(f"  Patch #{blocked_i} has negative CR — checking series...")
        related = run(["gerrit", "related", blocked_url], timeout=30)
        series = related.get("data", {}).get("series", [])
        if not series:
            continue

        # Find position of blocked patch in series
        blocked_pos = None
        change_positions = {}
        for pos, entry in enumerate(series):
            cn = entry.get("change_number")
            if cn is not None:
                change_positions[cn] = pos
            if cn == blocked_change:
                blocked_pos = pos

        if blocked_pos is None:
            continue

        # Find watched patches that are downstream (higher position = later in series)
        for j, patch, result in results:
            if j == blocked_i:
                continue
            if result.get("skipped") or result.get("watch_status") in (
                    "stopped", "merged", "abandoned"):
                continue
            dep_change = _change_number_from_url(patch["gerrit_url"])
            if dep_change is None:
                continue
            dep_pos = change_positions.get(dep_change)
            if dep_pos is not None and dep_pos > blocked_pos:
                reason = (
                    f"Blocked: patch #{blocked_i} "
                    f"({blocked_patch.get('description', blocked_url)}) "
                    f"has a negative Code-Review"
                )
                log(f"  → Stopping patch #{j} ({patch.get('description','?')[:50]}): {reason}")
                newly_blocked.append((j, patch, reason))

    return newly_blocked


# -------------------------------------------------------------------
# Phase 2: LLM research for unknown failures
# -------------------------------------------------------------------

def build_research_prompt(unknown_failures):
    """Build a focused prompt for JIRA research + relatedness."""
    lines = [
        "You are triaging Lustre CI test failures that have no",
        "linked bug in Maloo.  For EACH failure below:",
        "",
        "1. Search JIRA for an existing open bug that matches.",
        "   Try multiple strategies:",
        "   - Search by subtest name (e.g. test_42a)",
        "   - Search by error message keywords",
        "   - Search by suite name + error pattern",
        "   Use: jira search '<JQL>'",
        "   Read promising matches: jira get <KEY> --comments",
        "",
        "2. If you find a matching bug, record it.",
        "",
        "3. If NO matching bug exists, assess whether the",
        "   failure is RELATED to the patch (the patch likely",
        "   caused it) or UNRELATED (pre-existing/flaky).",
        "   Consider: does the patch component overlap with the",
        "   test?  Could the changes plausibly cause this error?",
        "   When in doubt, mark RELATED (conservative).",
        "",
        "After investigating ALL failures, output ONLY a JSON",
        "array (no other text).  Each element:",
        '{  "index": <N>,',
        '   "found_bug": "<KEY>" or null,',
        '   "related": true/false,  (only matters if no bug)',
        '   "reason": "<1-2 sentence explanation>",',
        '   "action": "link_and_retest" | "raise_and_retest"'
        ' | "stop"',
        "}",
        "",
        "Actions:",
        '  "link_and_retest" — you found a matching bug',
        '  "raise_and_retest" — no bug, failure is unrelated',
        '  "stop" — no bug, failure appears related to patch',
        "",
        "--- Failures to investigate ---",
        "",
    ]

    for i, (patch_idx, patch, failure) in enumerate(
            unknown_failures):
        desc = patch.get("description", "?")
        notes = patch.get("notes", "")
        jira = patch.get("jira", "")
        test = failure.get("test", "?")
        error = failure.get("error", "")

        lines.append(f"Failure {i}:")
        lines.append(f"  Patch #{patch_idx}: \"{desc}\"")
        if jira:
            lines.append(f"  JIRA: {jira}")
        if notes:
            lines.append(f"  Notes: {notes}")
        lines.append(f"  Test: {test}")
        if error:
            lines.append(f"  Error: {error}")
        lines.append("")

    return "\n".join(lines)


def research_failures(unknown_failures):
    """Invoke Claude to research unknown failures in JIRA.

    Returns list of decision dicts, one per failure.
    """
    prompt = build_research_prompt(unknown_failures)
    n = len(unknown_failures)
    log(f"Researching {n} unknown failure(s) via LLM...")

    # Unset CLAUDECODE so we can nest claude -p
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)

    try:
        r = subprocess.run(
            ["claude", "-p",
             "--model", "haiku",
             "--permission-mode", "bypassPermissions",
             "--allowedTools", "Bash(jira *)",
             "--max-budget-usd", "1.00",
             "--output-format", "json",
             "--no-session-persistence",
             prompt],
            capture_output=True, text=True,
            timeout=300, env=env)
    except subprocess.TimeoutExpired:
        log("WARNING: LLM research timed out (300s)")
        return _fallback_decisions(n)
    except Exception as e:
        log(f"WARNING: LLM invocation failed: {e}")
        return _fallback_decisions(n)

    return _parse_llm_response(r.stdout, n)


def _parse_llm_response(raw_output, n):
    """Extract the JSON decision array from Claude JSONL output."""
    # Claude --output-format json emits JSONL; find the result line
    response_text = None
    for line in raw_output.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                response_text = obj.get("result", "")
                break
        except json.JSONDecodeError:
            continue

    if not response_text:
        log("WARNING: No result line in LLM output")
        return _fallback_decisions(n)

    # Strip markdown fences if present
    text = response_text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if "```" in text:
            text = text[:text.rindex("```")]
        text = text.strip()

    try:
        decisions = json.loads(text)
    except json.JSONDecodeError as e:
        log(f"WARNING: Failed to parse LLM JSON: {e}")
        log(f"  Response: {text[:200]}")
        return _fallback_decisions(n)

    # Index decisions and fill gaps
    by_index = {d.get("index"): d for d in decisions}
    result = []
    for i in range(n):
        if i in by_index:
            result.append(by_index[i])
        else:
            result.append({
                "index": i, "found_bug": None,
                "related": True,
                "reason": "No LLM verdict — conservative stop",
                "action": "stop",
            })
    return result


def _fallback_decisions(n):
    """Conservative fallback: stop everything."""
    return [
        {"index": i, "found_bug": None, "related": True,
         "reason": "LLM unavailable — conservative stop",
         "action": "stop"}
        for i in range(n)
    ]


# -------------------------------------------------------------------
# Phase 3: Execute decisions
# -------------------------------------------------------------------

def execute_decisions(unknown_failures, decisions):
    """Execute LLM decisions, return report actions."""
    actions = []
    tool_calls = 0

    for (patch_idx, patch, failure), decision in zip(
            unknown_failures, decisions):
        action_type = decision.get("action", "stop")
        reason = decision.get("reason", "")
        found_bug = decision.get("found_bug")
        test = failure.get("test", "?")
        suite_id = failure.get("suite_id", "")
        session_id = failure.get("session_id", "")

        if action_type == "link_and_retest" and found_bug:
            log(f"  #{patch_idx}: link {found_bug} + retest "
                f"({test})")
            watcher("link-bug", suite_id, found_bug)
            watcher("retest", session_id, found_bug)
            tool_calls += 2
            actions.append({
                "type": "link_bug",
                "patch_index": patch_idx,
                "gerrit_url": patch["gerrit_url"],
                "jira": found_bug,
                "description":
                    f"{test}: linked {found_bug}, retest "
                    f"requested ({reason})",
            })

        elif action_type == "raise_and_retest":
            log(f"  #{patch_idx}: raise bug + retest ({test})")
            summary = test
            error = failure.get("error", "")
            if error:
                summary = f"{test}: {error[:80]}"

            bug_result = watcher(
                "raise-bug", suite_id,
                "--project", "LU", "--summary", summary)
            tool_calls += 1

            bug_key = _extract_bug_key(bug_result)
            if bug_key and session_id:
                watcher("retest", session_id, bug_key)
                tool_calls += 1

            actions.append({
                "type": "raise_bug",
                "patch_index": patch_idx,
                "gerrit_url": patch["gerrit_url"],
                "jira": bug_key or "",
                "description":
                    f"{test}: raised {bug_key or 'bug'}, "
                    f"retest requested ({reason})",
            })

        else:  # "stop" or unknown
            log(f"  #{patch_idx}: STOP — {reason}")
            actions.append({
                "type": "stopped",
                "patch_index": patch_idx,
                "gerrit_url": patch["gerrit_url"],
                "jira": patch.get("jira", ""),
                "description": f"{test}: {reason}",
            })

    return actions, tool_calls


def _extract_bug_key(result):
    """Try to extract a JIRA key from a maloo raise-bug result."""
    import re
    if result.get("ok"):
        key = result.get("data", {}).get("key", "")
        if key:
            return key
    # Try raw output
    raw = str(result.get("raw", ""))
    m = re.search(r"(LU-\d+|EX-\d+)", raw)
    return m.group(1) if m else None


# -------------------------------------------------------------------
# Phase 4: Report
# -------------------------------------------------------------------

def build_report(all_actions, all_errors, all_skipped,
                 patches_checked, tool_calls, llm_calls):
    """Assemble the report JSON."""
    summary = {
        "active": 0, "needs_review": 0, "stopped": 0,
        "merged": 0, "abandoned": 0,
        "retests_requested": 0, "bugs_raised": 0,
    }
    for a in all_actions:
        t = a.get("type", "")
        if t in ("merged", "abandoned", "stopped", "needs_review"):
            summary[t] += 1
        if t in ("retest", "link_bug"):
            summary["retests_requested"] += 1
        if t == "raise_bug":
            summary["bugs_raised"] += 1
            summary["retests_requested"] += 1

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "patches_checked": patches_checked,
        "actions": all_actions,
        "summary": summary,
        "debug": {
            "last_checked": None,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "errors": all_errors,
            "skipped": all_skipped,
        },
    }


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def main():
    global _config
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args()
    if args.dry_run:
        log("DRY RUN mode — no writes will be made")
        os.environ["PATCH_SHEPHERD_DRY_RUN"] = "1"
    _config = load_config()

    with open(_config.patches_file) as f:
        patches_data = json.load(f)
    patches = patches_data.get("patches", [])
    log(f"Checking {len(patches)} patches")

    tool_calls = 0
    llm_calls = 0

    # --- Phase 1: Check all patches ---
    results = check_all_patches(patches)
    tool_calls += len(results)

    # Collect actions, errors, skips, unknowns
    all_actions = []
    all_errors = []
    all_skipped = []
    unknown_failures = []

    for i, patch, result in results:
        for err in result.get("errors", []):
            all_errors.append(f"patch #{i}: {err}")

        if result.get("skipped"):
            reason = result.get("skip_reason", "?")
            all_skipped.append(f"#{i}: {reason}")
            log(f"    skipped: {reason}")
            continue

        for action in result.get("actions_taken", []):
            all_actions.append({
                "type": action["type"],
                "patch_index": i,
                "gerrit_url": patch["gerrit_url"],
                "jira": patch.get("jira", ""),
                "description": action.get("description", ""),
            })
            log(f"    {action['type']}: "
                f"{action.get('description', '')[:70]}")

        for item in result.get("needs_llm_decision", []):
            unknown_failures.append((i, patch, item))
            log(f"    unknown: {item.get('test', '?')[:70]}")

    # Series-aware blocking: stop dependents of patches with negative CR
    series_blocks = propagate_series_blocks(patches, results)
    for j, patch, reason in series_blocks:
        all_actions.append({
            "type": "stopped",
            "patch_index": j,
            "gerrit_url": patch["gerrit_url"],
            "jira": patch.get("jira", ""),
            "description": reason,
        })

    # --- Phase 2: Research unknown failures ---
    if unknown_failures:
        log(f"\n{len(unknown_failures)} unknown failure(s) "
            f"— invoking LLM for JIRA research")
        decisions = research_failures(unknown_failures)
        llm_calls = 1

        # --- Phase 3: Execute decisions ---
        decision_actions, decision_tool_calls = \
            execute_decisions(unknown_failures, decisions)
        all_actions.extend(decision_actions)
        tool_calls += decision_tool_calls
    else:
        log("\nNo unknown failures — no LLM call needed")

    # --- Phase 4: Report ---
    report = build_report(
        all_actions, all_errors, all_skipped,
        len(patches), tool_calls, llm_calls)

    report_json = json.dumps(report, indent=4)
    write_result = watcher(
        "write-report", _config.report_file, stdin_data=report_json)
    tool_calls += 1

    log(f"\nDone: {len(all_actions)} actions, "
        f"{len(all_errors)} errors, "
        f"{llm_calls} LLM call(s)")
    if write_result.get("patches_updated"):
        log("patches_to_watch.json updated")

    # Structured summary on stdout for run_watcher.sh
    print(json.dumps({
        "actions": len(all_actions),
        "errors": len(all_errors),
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
    }))

    return 0 if not all_errors else 0  # errors are non-fatal


if __name__ == "__main__":
    sys.exit(main())
