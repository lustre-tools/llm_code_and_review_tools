"""CLI entry point for Maloo test results tool."""

import json
import re
import sys
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
        for f in failed[:3]:
            next_actions.append(
                f"maloo failures {sid} -- show failed subtests"
            )
            break
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
            "logs": ts.get("logs"),
        })

    result = {
        "session_id": sid,
        "test_group": data.get("test_group"),
        "test_name": data.get("test_name"),
        "failed_suites": failed_suites,
    }

    next_actions = [
        f"maloo subtests <suite_id> -- get all subtests for a suite",
        f"maloo logs {sid} -- download test logs",
    ]

    env = success_response(result, TOOL_NAME, "failures", next_actions)
    _output(env, pretty)


@main.command()
@click.argument("test_set_id")
@click.option("--status", type=str, default=None, help="Filter by status (PASS/FAIL/SKIP/CRASH)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def subtests(test_set_id: str, status: str | None, pretty: bool) -> None:
    """Show subtests for a test set (suite).

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

    items = []
    for st in all_subtests:
        if status and st["status"] != status.upper():
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
        "filter": status,
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


if __name__ == "__main__":
    main()
