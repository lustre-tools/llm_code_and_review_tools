"""CLI entry point for Maloo test results tool."""

import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import click

from llm_tool_common.envelope import (
    error_response_from_dict,
    format_json,
    success_response,
)

from .client import MalooClient
from .config import load_config

TOOL_NAME = "maloo"

# Match test session URLs or bare UUIDs
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _extract_session_id(url_or_id: str) -> str:
    """Extract UUID from a Maloo URL or bare ID."""
    m = UUID_RE.search(url_or_id)
    if m:
        return m.group(0)
    raise click.BadParameter(
        f"Cannot extract session ID from: {url_or_id}"
    )


def _make_client() -> MalooClient:
    config = load_config()
    return MalooClient(config)


def _output(envelope: dict[str, Any], pretty: bool) -> None:
    click.echo(format_json(envelope, pretty=pretty))


def _error(
    code: str, message: str, command: str, pretty: bool
) -> None:
    env = error_response_from_dict(code, message, TOOL_NAME, command)
    _output(env, pretty)
    sys.exit(1)


@click.group()
def main() -> None:
    """Maloo test results CLI - query Lustre CI test results."""
    pass


@main.command()
@click.argument("session_url")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def session(session_url: str, pretty: bool) -> None:
    """Show test session overview.

    SESSION_URL can be a full Maloo URL or a bare UUID.
    """
    sid = _extract_session_id(session_url)
    client = _make_client()
    data = client.get_session(sid)
    if not data:
        _error("NOT_FOUND", f"Session {sid} not found", "session", pretty)

    # Get test sets for summary
    test_sets = client.get_test_sets(sid)
    set_names = client.resolve_test_set_names(test_sets)

    suites = []
    for ts in test_sets:
        name = set_names.get(ts.get("test_set_script_id", ""), "unknown")
        suites.append({
            "id": ts["id"],
            "name": name,
            "status": ts["status"],
            "duration": ts.get("duration"),
            "passed": ts.get("sub_tests_passed_count", 0),
            "failed": ts.get("sub_tests_failed_count", 0),
            "skipped": ts.get("sub_tests_skipped_count", 0),
            "total": ts.get("sub_tests_count", 0),
        })

    result = {
        "session_id": sid,
        "test_group": data.get("test_group"),
        "test_name": data.get("test_name"),
        "test_host": data.get("test_host"),
        "submission": data.get("submission"),
        "duration": data.get("duration"),
        "enforcing": data.get("enforcing"),
        "passed": data.get("test_sets_passed_count", 0),
        "failed": data.get("test_sets_failed_count", 0),
        "aborted": data.get("test_sets_aborted_count", 0),
        "total": data.get("test_sets_count", 0),
        "suites": suites,
    }

    next_actions = []
    failed = [s for s in suites if s["status"] == "FAIL"]
    if failed:
        next_actions.append(
            f"maloo failures {sid} -- show failed subtests"
        )
        for f in failed[:3]:
            next_actions.append(
                f"maloo subtests {f['id']} -- details for {f['name']}"
            )

    env = success_response(result, TOOL_NAME, "session", next_actions)
    _output(env, pretty)


@main.command()
@click.argument("session_url")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def failures(session_url: str, pretty: bool) -> None:
    """Show failed subtests for a test session.

    Drills into each failed test set and shows the individual
    subtest failures with error messages.
    """
    sid = _extract_session_id(session_url)
    client = _make_client()

    data = client.get_session(sid)
    if not data:
        _error("NOT_FOUND", f"Session {sid} not found", "failures", pretty)

    test_sets = client.get_test_sets(sid)
    set_names = client.resolve_test_set_names(test_sets)

    failed_sets = [ts for ts in test_sets if ts["status"] in ("FAIL", "CRASH", "ABORT", "TIMEOUT")]

    if not failed_sets:
        env = success_response(
            {"session_id": sid, "message": "No failures found", "failed_suites": []},
            TOOL_NAME, "failures",
        )
        _output(env, pretty)
        return

    failed_suites = []
    for ts in failed_sets:
        suite_name = set_names.get(ts.get("test_set_script_id", ""), "unknown")
        subtests = client.get_subtests(test_set_id=ts["id"])
        subtest_names = client.resolve_subtest_names(subtests)

        failed_subtests = []
        for st in subtests:
            if st["status"] in ("FAIL", "CRASH", "ABORT", "TIMEOUT"):
                st_name = subtest_names.get(
                    st.get("sub_test_script_id", ""), f"order_{st.get('order', '?')}"
                )
                failed_subtests.append({
                    "name": st_name,
                    "status": st["status"],
                    "error": st.get("error", ""),
                    "duration": st.get("duration"),
                    "return_code": st.get("return_code"),
                })

        failed_suites.append({
            "suite": suite_name,
            "suite_id": ts["id"],
            "status": ts["status"],
            "failed_count": ts.get("sub_tests_failed_count", 0),
            "total_count": ts.get("sub_tests_count", 0),
            "failed_subtests": failed_subtests,
            "logs_cmd": f"maloo logs {ts['id']}",
        })

    result = {
        "session_id": sid,
        "test_group": data.get("test_group"),
        "test_name": data.get("test_name"),
        "failed_suites": failed_suites,
    }

    suite_ids = [s["suite_id"] for s in failed_suites[:1]]
    next_actions = [
        f"maloo subtests <suite_id> -- get all subtests for a suite",
    ]
    for suite_id in suite_ids:
        next_actions.append(
            f"maloo logs {suite_id} -- download test logs"
        )

    env = success_response(result, TOOL_NAME, "failures", next_actions)
    _output(env, pretty)


@main.command()
@click.argument("test_set_id")
@click.option("--status", type=str, default="FAIL",
              help="Filter by status (PASS/FAIL/SKIP/CRASH). Default: FAIL")
@click.option("--all", "show_all", is_flag=True,
              help="Show all subtests (override --status filter)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def subtests(test_set_id: str, status: str | None, show_all: bool, pretty: bool) -> None:
    """Show subtests for a test set (suite).

    By default shows only FAIL subtests. Use --all to see everything,
    or --status PASS/SKIP/CRASH to filter differently.

    TEST_SET_ID is the UUID of the test set.
    """
    client = _make_client()

    ts = client.get_test_set(test_set_id)
    if not ts:
        _error("NOT_FOUND", f"Test set {test_set_id} not found", "subtests", pretty)

    all_subtests = client.get_subtests(test_set_id=test_set_id)
    subtest_names = client.resolve_subtest_names(all_subtests)

    # Resolve suite name
    suite_name = "unknown"
    if ts.get("test_set_script_id"):
        script = client.get_test_set_script(ts["test_set_script_id"])
        if script:
            suite_name = script["name"]

    active_filter = None if show_all else status
    items = []
    for st in all_subtests:
        if active_filter and st["status"] != active_filter.upper():
            continue
        st_name = subtest_names.get(
            st.get("sub_test_script_id", ""), f"order_{st.get('order', '?')}"
        )
        items.append({
            "name": st_name,
            "status": st["status"],
            "error": st.get("error", ""),
            "duration": st.get("duration"),
            "return_code": st.get("return_code"),
            "order": st.get("order"),
        })

    result = {
        "test_set_id": test_set_id,
        "suite": suite_name,
        "suite_status": ts["status"],
        "total": len(all_subtests),
        "shown": len(items),
        "filter": active_filter,
        "subtests": items,
    }

    env = success_response(result, TOOL_NAME, "subtests")
    _output(env, pretty)


@main.command()
@click.argument("review_id", type=int)
@click.option("--patch", type=int, default=None, help="Patchset number")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def review(review_id: int, patch: int | None, pretty: bool) -> None:
    """Find test sessions for a Gerrit review.

    REVIEW_ID is the Gerrit change number.
    """
    client = _make_client()
    sessions = client.find_sessions_by_review(review_id, patch)

    if not sessions:
        env = success_response(
            {"review_id": review_id, "patch": patch,
             "message": "No test sessions found", "sessions": []},
            TOOL_NAME, "review",
        )
        _output(env, pretty)
        return

    items = []
    for s in sessions:
        items.append({
            "session_id": s.get("id"),
            "test_group": s.get("test_group"),
            "test_name": s.get("test_name"),
            "test_host": s.get("test_host"),
            "submission": s.get("submission"),
            "enforcing": s.get("enforcing"),
            "passed": s.get("test_sets_passed_count", 0),
            "failed": s.get("test_sets_failed_count", 0),
            "total": s.get("test_sets_count", 0),
            "duration": s.get("duration"),
            "url": f"https://testing.whamcloud.com/test_sessions/{s.get('id')}",
        })

    result = {
        "review_id": review_id,
        "patch": patch,
        "session_count": len(items),
        "sessions": items,
    }

    next_actions = []
    failed = [s for s in items if s["failed"] > 0]
    for f in failed[:3]:
        next_actions.append(
            f"maloo failures {f['session_id']} -- failures for {f['test_group']}"
        )

    env = success_response(result, TOOL_NAME, "review", next_actions or None)
    _output(env, pretty)


@main.command()
@click.argument("buggable_id")
@click.option("--related", is_flag=True, help="Include bug links from child subtests")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def bugs(buggable_id: str, related: bool, pretty: bool) -> None:
    """Show bug links for a test set or subtest.

    BUGGABLE_ID is the UUID of a test set or subtest.
    """
    client = _make_client()
    links = client.get_bug_links(buggable_id, related=related)

    result = {
        "buggable_id": buggable_id,
        "count": len(links),
        "bug_links": links,
    }

    next_actions = [
        "maloo link-bug <test_set_id> <JIRA_TICKET> -- associate a bug with a test failure",
    ]

    env = success_response(result, TOOL_NAME, "bugs", next_actions)
    _output(env, pretty)


@main.command(name="link-bug")
@click.argument("buggable_id")
@click.argument("jira_ticket")
@click.option(
    "--type", "buggable_class", type=click.Choice(["TestSet", "SubTest"]),
    default="TestSet", help="Type of entity to link (default: TestSet)")
@click.option(
    "--state", type=click.Choice(["accepted", "pending"]),
    default="accepted", help="Bug link state (default: accepted)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def link_bug(
    buggable_id: str,
    jira_ticket: str,
    buggable_class: str,
    state: str,
    pretty: bool,
) -> None:
    """Associate a JIRA bug with a test set or subtest.

    This marks a test failure as a known bug so it doesn't
    block patch landing.

    \b
    Examples:
      maloo link-bug <test_set_id> LU-12345
      maloo link-bug <subtest_id> LU-12345 --type SubTest
    """
    client = _make_client()
    resp = client.create_bug_link(
        buggable_class=buggable_class,
        buggable_id=buggable_id,
        bug_upstream_id=jira_ticket,
        bug_state=state,
    )

    if resp.startswith("OK"):
        result = {
            "success": True,
            "buggable_class": buggable_class,
            "buggable_id": buggable_id,
            "bug": jira_ticket,
            "state": state,
            "response": resp,
        }
        env = success_response(result, TOOL_NAME, "link-bug")
        _output(env, pretty)
    else:
        _error("LINK_FAILED", resp, "link-bug", pretty)


@main.command(name="raise-bug")
@click.argument("buggable_id")
@click.option(
    "--project", default="LU",
    help="JIRA project (default: LU)")
@click.option(
    "--summary", default="",
    help="Bug summary (default: auto-generated from test set name)")
@click.option(
    "--description", default=None,
    help="Bug description (default: Maloo's auto-generated template)")
@click.option(
    "--type", "buggable_type",
    type=click.Choice(["TestSet", "SubTest"]),
    default="TestSet",
    help="Type of entity (default: TestSet)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def raise_bug(
    buggable_id: str,
    project: str,
    summary: str,
    description: str | None,
    buggable_type: str,
    pretty: bool,
) -> None:
    """Raise a new JIRA bug via Maloo and auto-link to test failure.

    Uses Maloo's "Raise bug" web feature to create a JIRA issue
    and automatically associate it with the test set/subtest.

    \b
    Examples:
      maloo raise-bug <test_set_id> --project LU --summary "sanity test_81a FAIL"
      maloo raise-bug <test_set_id>  # uses Maloo's default summary/description
    """
    client = _make_client()
    try:
        result = client.raise_bug(
            buggable_id=buggable_id,
            buggable_type=buggable_type,
            project=project,
            summary=summary,
            description=description,
        )
        result["buggable_id"] = buggable_id
        result["buggable_type"] = buggable_type
        result["project"] = project
        env = success_response(result, TOOL_NAME, "raise-bug")
        _output(env, pretty)
    except RuntimeError as exc:
        _error("RAISE_BUG_FAILED", str(exc), "raise-bug", pretty)
    except Exception as exc:
        _error("API_ERROR", str(exc), "raise-bug", pretty)


@main.command()
@click.option("--branch", type=str, default=None,
              help="Filter by branch (trigger_job), e.g. lustre-master")
@click.option("--days", type=int, default=7,
              help="Number of days to look back (default: 7)")
@click.option("--host", type=str, default=None,
              help="Filter by test host name")
@click.option("--failed", is_flag=True, default=False,
              help="Only show sessions with failures")
@click.option("--limit", type=int, default=20,
              help="Max sessions to return (default: 20)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def sessions(
    branch: str | None,
    days: int,
    host: str | None,
    failed: bool,
    limit: int,
    pretty: bool,
) -> None:
    """List and search test sessions.

    Shows recent test sessions, optionally filtered by branch,
    host, or failure status.

    \b
    Examples:
      maloo sessions --branch lustre-master
      maloo sessions --branch lustre-master --failed --days 14
      maloo sessions --host onyx-53vm1 --days 3
      maloo sessions --limit 50
    """
    client = _make_client()

    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    params: dict[str, Any] = {
        "from": from_date,
        "to": to_date,
    }
    if branch:
        params["trigger_job"] = branch
    if host:
        params["test_host"] = host
    if failed:
        params["test_sets_failed"] = "true"

    try:
        raw = client.get_sessions(params, max_records=limit)
    except Exception as exc:
        _error("API_ERROR", str(exc), "sessions", pretty)
        return

    items = []
    for s in raw:
        items.append({
            "session_id": s.get("id"),
            "test_group": s.get("test_group"),
            "test_name": s.get("test_name"),
            "test_host": s.get("test_host"),
            "submission": s.get("submission"),
            "enforcing": s.get("enforcing"),
            "passed": s.get("test_sets_passed_count", 0),
            "failed": s.get("test_sets_failed_count", 0),
            "aborted": s.get("test_sets_aborted_count", 0),
            "total": s.get("test_sets_count", 0),
            "duration": s.get("duration"),
            "trigger_job": s.get("trigger_job"),
            "url": f"https://testing.whamcloud.com/test_sessions/{s.get('id')}",
        })

    filters = {}
    if branch:
        filters["branch"] = branch
    if host:
        filters["host"] = host
    if failed:
        filters["failed_only"] = True

    result = {
        "period": f"{from_date} to {to_date}",
        "days": days,
        "filters": filters,
        "count": len(items),
        "sessions": items,
    }

    next_actions = []
    if items:
        next_actions.append(
            f"maloo session {items[0]['session_id']}"
            f" -- details for most recent session"
        )
        has_failed = [s for s in items if s["failed"] > 0]
        if has_failed:
            next_actions.append(
                f"maloo failures {has_failed[0]['session_id']}"
                f" -- see failures"
            )

    env = success_response(result, TOOL_NAME, "sessions", next_actions or None)
    _output(env, pretty)


@main.command(name="test-history")
@click.argument("test_name")
@click.option("--branch", type=str, default="lustre-master",
              help="Branch to search (default: lustre-master)")
@click.option("--suite", type=str, default=None,
              help="Suite name to filter (e.g. sanity, replay-single)")
@click.option("--days", type=int, default=14,
              help="Number of days to look back (default: 14)")
@click.option("--sessions", "max_sessions", type=int, default=30,
              help="Max sessions to examine (default: 30)")
@click.option("--all", "show_all", is_flag=True,
              help="Show all history entries (default: failures only)")
@click.option("--limit", type=int, default=10,
              help="Max history entries to return (default: 10)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def test_history(
    test_name: str,
    branch: str,
    suite: str | None,
    days: int,
    max_sessions: int,
    show_all: bool,
    limit: int,
    pretty: bool,
) -> None:
    """Show pass/fail history for a specific test.

    By default shows only failures in the history detail.
    Use --all to include PASS entries too.

    \b
    TEST_NAME is the subtest name (e.g. test_39b).

    \b
    Examples:
      maloo test-history test_39b
      maloo test-history test_39b --suite sanity --days 30
      maloo test-history test_1b --branch lustre-reviews --suite replay-vbr
      maloo test-history test_39b --sessions 50 --all
    """
    client = _make_client()

    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    try:
        history, resolved_suite = client.get_test_history(
            test_name=test_name,
            trigger_job=branch,
            from_date=from_date,
            to_date=to_date,
            suite=suite,
            max_sessions=max_sessions,
        )
    except Exception as exc:
        _error("API_ERROR", str(exc), "test-history", pretty)
        return

    # Compute summary stats
    total = len(history)
    pass_count = sum(1 for h in history if h["status"] == "PASS")
    fail_count = sum(1 for h in history if h["status"] in ("FAIL", "CRASH", "TIMEOUT"))
    skip_count = sum(1 for h in history if h["status"] == "SKIP")
    fail_rate = (fail_count / total * 100) if total > 0 else 0.0

    # Filter history entries: default to failures only
    if show_all:
        filtered = history
    else:
        filtered = [h for h in history if h["status"] in ("FAIL", "CRASH", "TIMEOUT")]

    # Apply limit
    filtered = filtered[:limit]

    result = {
        "test_name": test_name,
        "branch": branch,
        "suite": resolved_suite or suite,
        "period": f"{from_date} to {to_date}",
        "days": days,
        "sessions_examined": max_sessions,
        "occurrences": total,
        "summary": {
            "pass": pass_count,
            "fail": fail_count,
            "skip": skip_count,
            "fail_rate_pct": round(fail_rate, 1),
        },
        "history": [
            {
                "date": h["submission"][:10] if h["submission"] else "",
                "status": h["status"],
                "error": h["error"][:200] if h["error"] else "",
                "duration": h["duration"],
                "test_host": h["test_host"],
                "session_id": h["session_id"],
                "test_set_id": h["test_set_id"],
            }
            for h in filtered
        ],
    }

    next_actions = []
    failed_entries = [h for h in history if h["status"] in ("FAIL", "CRASH", "TIMEOUT")]
    if failed_entries:
        ex = failed_entries[-1]
        next_actions.append(
            f"maloo failures {ex['session_id']}"
            f" -- see all failures in that session"
        )
        next_actions.append(
            f"maloo bugs {ex['test_set_id']}"
            f" -- check bug links"
        )

    env = success_response(result, TOOL_NAME, "test-history", next_actions or None)
    _output(env, pretty)


def _resolve_review_to_revision(review_id: int) -> str | None:
    """Resolve a Gerrit change number to its current patchset revision hash.

    Uses the gerrit CLI tool to look up the change.  Returns the commit
    hash string, or None if the lookup fails.
    """
    import json
    import subprocess

    try:
        proc = subprocess.run(
            ["gerrit", "info", str(review_id)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return data.get("data", {}).get("current_revision")
    except Exception:
        return None


def _parse_review_arg(value: str) -> str:
    """Parse a --review value into a change number or commit hash.

    Accepts:
      - A Gerrit URL: https://review.whamcloud.com/c/ex/lustre-release/+/64266
      - A plain change number: 64266
      - A commit hash: 7b77eeb0190d6d93880951533c2e1d1145780375

    Returns the change number (digits only) or commit hash string.
    """
    # Full Gerrit URL — extract the change number
    m = re.match(r"https?://[^/]+/c/[^/]+(?:/[^/]+)*/\+/(\d+)", value)
    if m:
        return m.group(1)
    m = re.match(r"https?://[^/]+/(\d+)", value)
    if m:
        return m.group(1)
    return value


# Map common git branch names to their Jenkins review job names.
# Integration branch jobs (lustre-b_es6_0, lustre-master) use the
# branch name directly as lustre-{branch}, but review jobs have
# different naming conventions.
_BRANCH_TO_REVIEW_JOB = {
    "master": "lustre-reviews",
    "b_es6_0": "lustre-b_es-reviews",
    "b_es7_0": "lustre-b_es-reviews",
    "b_es5_2": "lustre-b_es-reviews",
    "b_es5_1": "lustre-b_es-reviews",
    "b_es5_0": "lustre-b_es-reviews",
    "b_ieel3_0": "lustre-b_ieel-reviews",
    "b_ieel2_3": "lustre-b_ieel-reviews",
}


def _resolve_branch_to_job(branch: str) -> str:
    """Resolve a git branch name to a Jenkins job name.

    If the value already looks like a Jenkins job name (starts with
    'lustre-'), return it as-is.  Otherwise try the known mapping,
    then fall back to 'lustre-{branch}'.
    """
    if branch.startswith("lustre-"):
        return branch
    if branch in _BRANCH_TO_REVIEW_JOB:
        return _BRANCH_TO_REVIEW_JOB[branch]
    # Heuristic: b_es* branches use lustre-b_es-reviews
    if branch.startswith("b_es"):
        return "lustre-b_es-reviews"
    if branch.startswith("b_ieel"):
        return "lustre-b_ieel-reviews"
    return f"lustre-{branch}"


@main.command()
@click.option("--review", "review_id", type=str, default=None,
              help="Gerrit change number, URL, or commit hash")
@click.option("--build", "buildno", type=int, default=None,
              help="Jenkins build number")
@click.option("--branch", "job", type=str, default=None,
              help="Git branch or Jenkins job name (e.g. b_es6_0, lustre-reviews)")
@click.option("--status", type=str, default=None,
              help="Filter by queue status (e.g. Queued, Running)")
@click.option("--limit", type=int, default=20,
              help="Max entries to return (default: 20)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def queue(
    review_id: str | None,
    buildno: int | None,
    job: str | None,
    status: str | None,
    limit: int,
    pretty: bool,
) -> None:
    """Show test queue status.

    Lists queued and running tests, optionally filtered by
    Gerrit review, build number, branch/job, or status.

    --review accepts a Gerrit change number (e.g. 64266), a full
    Gerrit URL, or a commit hash.  Change numbers are automatically
    resolved to commit hashes via the gerrit CLI.

    --branch accepts a git branch name (e.g. b_es6_0, master) or a
    Jenkins job name (e.g. lustre-b_es-reviews, lustre-reviews).
    Branch names are mapped to their review job names automatically.

    \b
    Examples:
      maloo queue --review 64266
      maloo queue --review https://review.whamcloud.com/c/ex/lustre-release/+/64266
      maloo queue --build 27341
      maloo queue --branch b_es6_0
      maloo queue --branch lustre-reviews
      maloo queue --status Running
    """
    client = _make_client()

    params: dict[str, Any] = {}
    resolved_review = review_id  # track what we actually queried with
    if review_id is not None:
        review_id = _parse_review_arg(review_id)
        # If it looks like a pure integer (Gerrit change number),
        # resolve to the current patchset commit hash via gerrit CLI.
        if review_id.isdigit():
            revision = _resolve_review_to_revision(int(review_id))
            if revision:
                params["review_id"] = revision
                resolved_review = revision
            else:
                _error(
                    "RESOLVE_FAILED",
                    f"Could not resolve Gerrit change {review_id} to a "
                    f"commit hash (is the gerrit CLI available?). "
                    f"Try --build <buildno> or pass the commit hash directly.",
                    "queue",
                    pretty,
                )
                return
        else:
            # Assume it's already a commit hash
            params["review_id"] = review_id
    if buildno is not None:
        params["buildno"] = buildno
    if job:
        job = _resolve_branch_to_job(job)
        params["job"] = job
    if status:
        params["status"] = status

    if not params:
        _error(
            "MISSING_FILTER",
            "At least one filter required: --review, --build, --branch, or --status",
            "queue",
            pretty,
        )
        return

    try:
        raw = client.get_test_queues(params, max_records=limit)
    except Exception as exc:
        _error("API_ERROR", str(exc), "queue", pretty)
        return

    items = []
    for q in raw:
        entry: dict[str, Any] = {
            "id": q.get("id"),
            "job": q.get("job"),
            "buildno": q.get("buildno"),
            "test_group": q.get("test_group"),
            "distros": q.get("distros"),
            "status": q.get("status"),
            "info": q.get("info"),
            "instance": q.get("instance"),
            "remain": q.get("remain"),
            "review_id": q.get("review_id"),
            "review_patch": q.get("review_patch"),
        }
        # Include current suite/test if running
        if q.get("suite"):
            entry["suite"] = q["suite"]
        if q.get("test"):
            entry["test"] = q["test"]
        items.append(entry)

    filters = {}
    if review_id is not None:
        filters["review_id"] = review_id
        if resolved_review != review_id:
            filters["resolved_revision"] = resolved_review
    if buildno is not None:
        filters["buildno"] = buildno
    if job:
        filters["job"] = job
    if status:
        filters["status"] = status

    result = {
        "filters": filters,
        "count": len(items),
        "queue_entries": items,
    }

    next_actions = []
    if items and items[0].get("review_id"):
        next_actions.append(
            f"maloo review {items[0]['review_id']}"
            f" -- see test sessions for this review"
        )

    env = success_response(result, TOOL_NAME, "queue", next_actions or None)
    _output(env, pretty)


@main.command(name="top-failures")
@click.argument("branch", default="lustre-master")
@click.option("--days", type=int, default=7, help="Number of days to look back (default: 7)")
@click.option("--limit", type=int, default=20, help="Max failures to show (default: 20)")
@click.option("--sessions", "max_sessions", type=int, default=50,
              help="Max test sessions to examine (default: 50)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def top_failures(
    branch: str,
    days: int,
    limit: int,
    max_sessions: int,
    pretty: bool,
) -> None:
    """Show most common test failures for a branch.

    Queries recent test sessions that have failures, drills into
    each one to find failing subtests, and aggregates by test name.

    \b
    BRANCH is the trigger_job name (default: lustre-master).
    Common branches: lustre-master, lustre-b2_15, lustre-b2_12

    \b
    Examples:
      maloo top-failures
      maloo top-failures lustre-master --days 14
      maloo top-failures lustre-b2_15 --days 30 --limit 10
      maloo top-failures --sessions 100
    """
    client = _make_client()

    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    try:
        failures, sessions_examined, _ = client.get_top_failures(
            trigger_job=branch,
            from_date=from_date,
            to_date=to_date,
            max_sessions=max_sessions,
        )
    except Exception as exc:
        _error("API_ERROR", str(exc), "top-failures", pretty)
        return  # unreachable, _error calls sys.exit

    top = failures[:limit]

    result = {
        "branch": branch,
        "period": f"{from_date} to {to_date}",
        "days": days,
        "sessions_examined": sessions_examined,
        "unique_failures": len(failures),
        "showing": len(top),
        "top_failures": [
            {
                "rank": i + 1,
                "suite": f["suite"],
                "test_name": f["test_name"],
                "count": f["count"],
                "session_count": f["session_count"],
                "statuses": f["statuses"],
                "error_sample": f["error_sample"],
                "example_session_id": f["example_session_id"],
                "example_test_set_id": f["example_test_set_id"],
            }
            for i, f in enumerate(top)
        ],
    }

    next_actions = []
    if top:
        ex = top[0]
        next_actions.append(
            f"maloo failures {ex['example_session_id']}"
            f" -- see failures in example session"
        )
        next_actions.append(
            f"maloo bugs {ex['example_test_set_id']}"
            f" -- check bug links for top failure"
        )

    env = success_response(result, TOOL_NAME, "top-failures", next_actions or None)
    _output(env, pretty)


@main.command()
@click.argument("session_url")
@click.argument("jira_ticket")
@click.option(
    "--option", type=click.Choice(["single", "all", "livedebug"]),
    default="single",
    help="Retest scope: single session, all sessions, or livedebug (default: single)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def retest(session_url: str, jira_ticket: str, option: str, pretty: bool) -> None:
    """Request a retest of a test session.

    Requires a JIRA ticket to justify the retest.

    \b
    Examples:
      maloo retest <session_url> LU-19487
      maloo retest <session_url> LU-19487 --option all
      maloo retest <session_url> LU-19487 --option livedebug
    """
    sid = _extract_session_id(session_url)
    client = _make_client()

    resp = client.retest(session_id=sid, option=option, bug_id=jira_ticket)

    result = {
        "success": True,
        "session_id": sid,
        "retest_option": option,
        "bug_id": jira_ticket,
        "response": resp,
    }
    env = success_response(result, TOOL_NAME, "retest")
    _output(env, pretty)


@main.command()
@click.argument("test_set_id")
@click.option("--output-dir", type=str, default="/tmp/maloo_logs",
              help="Directory to extract logs into (default: /tmp/maloo_logs)")
@click.option("--grep", "grep_pattern", type=str, default=None,
              help="Search extracted logs for a pattern (grep -i)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def logs(
    test_set_id: str,
    output_dir: str,
    grep_pattern: str | None,
    pretty: bool,
) -> None:
    """Download and extract test logs for a test set (suite).

    TEST_SET_ID is the UUID of the test set. You can find it
    from 'maloo failures' or 'maloo session' output.

    \b
    Examples:
      maloo logs <test_set_id>
      maloo logs <test_set_id> --grep "test_81a"
      maloo logs <test_set_id> --output-dir /tmp/my_logs
    """
    import os
    import zipfile
    import tempfile
    from io import BytesIO

    client = _make_client()

    try:
        data = client.download_logs(test_set_id)
    except Exception as exc:
        _error("DOWNLOAD_FAILED", str(exc), "logs", pretty)
        return

    # Extract the archive
    os.makedirs(output_dir, exist_ok=True)
    extracted_files: list[str] = []

    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            for name in zf.namelist():
                zf.extract(name, output_dir)
                extracted_files.append(
                    os.path.join(output_dir, name)
                )
    except zipfile.BadZipFile:
        # Try as gzip/tar
        import tarfile
        try:
            with tarfile.open(
                fileobj=BytesIO(data), mode="r:gz"
            ) as tf:
                tf.extractall(output_dir)
                extracted_files = [
                    os.path.join(output_dir, m.name)
                    for m in tf.getmembers()
                    if m.isfile()
                ]
        except Exception:
            # Save raw file for manual inspection
            raw_path = os.path.join(
                output_dir, f"{test_set_id}.bin"
            )
            with open(raw_path, "wb") as f:
                f.write(data)
            extracted_files = [raw_path]

    # Optional grep
    grep_results: list[dict[str, Any]] = []
    if grep_pattern and extracted_files:
        import subprocess

        for fpath in extracted_files:
            if not os.path.isfile(fpath):
                continue
            try:
                proc = subprocess.run(
                    ["grep", "-in", grep_pattern, fpath],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                )
                if proc.returncode == 0:
                    lines = proc.stdout.decode(
                        "utf-8", errors="replace"
                    ).strip().split("\n")
                    grep_results.append({
                        "file": os.path.basename(fpath),
                        "path": fpath,
                        "match_count": len(lines),
                        "matches": lines[:50],
                    })
            except Exception:
                pass

    result: dict[str, Any] = {
        "test_set_id": test_set_id,
        "output_dir": output_dir,
        "archive_size": len(data),
        "files": [
            {
                "name": os.path.basename(f),
                "path": f,
                "size": (
                    os.path.getsize(f)
                    if os.path.isfile(f)
                    else 0
                ),
            }
            for f in extracted_files
        ],
    }

    if grep_pattern:
        result["grep_pattern"] = grep_pattern
        result["grep_results"] = grep_results

    env = success_response(result, TOOL_NAME, "logs")
    _output(env, pretty)


if __name__ == "__main__":
    main()
