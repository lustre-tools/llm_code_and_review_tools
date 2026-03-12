"""CLI entry point for Jenkins build server tool."""

import re
import sys
from datetime import datetime, timezone
from typing import Any

import click
import requests

from llm_tool_common.envelope import (
    error_response_from_dict,
    format_json,
    success_response,
)
from llm_tool_common.decorators import handle_errors

from .client import JenkinsClient
from .config import load_config

TOOL_NAME = "jenkins"

# Module-level flag for --envelope; toggled by the group callback.
_FULL_ENVELOPE = False


def _make_client(
    url: str | None = None,
    user: str | None = None,
    token: str | None = None,
) -> JenkinsClient:
    config = load_config(
        url_override=url, user_override=user, token_override=token
    )
    return JenkinsClient(config)


def _output(envelope: dict[str, Any], pretty: bool) -> None:
    click.echo(format_json(envelope, pretty=pretty, full_envelope=_FULL_ENVELOPE))


def _error(
    code: str, message: str, command: str, pretty: bool
) -> None:
    env = error_response_from_dict(code, message, TOOL_NAME, command)
    _output(env, pretty)
    sys.exit(1)


def _ts_to_iso(timestamp_ms: int | None) -> str | None:
    """Convert Jenkins millisecond timestamp to ISO-8601 string."""
    if not timestamp_ms:
        return None
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_to_human(ms: int | None) -> str | None:
    """Convert milliseconds to human-readable duration."""
    if not ms:
        return None
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins}m"


def _color_to_status(color: str | None) -> str:
    """Map Jenkins color to a human-readable status."""
    if not color:
        return "unknown"
    mapping = {
        "blue": "success",
        "blue_anime": "building (last: success)",
        "red": "failed",
        "red_anime": "building (last: failed)",
        "yellow": "unstable",
        "yellow_anime": "building (last: unstable)",
        "grey": "pending",
        "grey_anime": "building (last: pending)",
        "disabled": "disabled",
        "aborted": "aborted",
        "aborted_anime": "building (last: aborted)",
        "notbuilt": "not built",
        "notbuilt_anime": "building (never built)",
    }
    return mapping.get(color, color)


def _extract_build_params(build: dict[str, Any]) -> dict[str, str]:
    """Extract parameters from build actions."""
    params: dict[str, str] = {}
    for action in build.get("actions", []):
        for p in action.get("parameters", []):
            name = p.get("name", "")
            value = p.get("value", "")
            if name:
                params[name] = str(value) if value is not None else ""
    return params


def _extract_build_causes(build: dict[str, Any]) -> list[str]:
    """Extract cause descriptions from build actions."""
    causes: list[str] = []
    for action in build.get("actions", []):
        for c in action.get("causes", []):
            desc = c.get("shortDescription", "")
            if desc:
                causes.append(desc)
    return causes


def _normalize_build(
    build: dict[str, Any], job_name: str | None = None
) -> dict[str, Any]:
    """Normalize a build response to agent-friendly format."""
    params = _extract_build_params(build)
    causes = _extract_build_causes(build)

    result: dict[str, Any] = {
        "number": build.get("number"),
        "result": build.get("result"),
        "building": build.get("building", False),
        "timestamp": _ts_to_iso(build.get("timestamp")),
        "duration": _ms_to_human(build.get("duration")),
        "duration_ms": build.get("duration"),
        "url": build.get("url"),
    }

    if job_name:
        result["job"] = job_name
    if build.get("_job_name"):
        result["job"] = build["_job_name"]
    if causes:
        result["causes"] = causes
    if params:
        result["parameters"] = params

    # Extract Gerrit info from parameters if present
    gerrit_change = params.get("GERRIT_CHANGE_NUMBER")
    if gerrit_change:
        result["gerrit"] = {
            "change": gerrit_change,
            "patchset": params.get("GERRIT_PATCHSET_NUMBER", ""),
            "branch": params.get("GERRIT_BRANCH", ""),
            "project": params.get("GERRIT_PROJECT", ""),
            "subject": params.get("GERRIT_CHANGE_SUBJECT", ""),
            "owner": params.get("GERRIT_CHANGE_OWNER_NAME", ""),
            "refspec": params.get("GERRIT_REFSPEC", ""),
        }

    # Extract changeset/SCM info
    changeset = build.get("changeSet", {})
    items = changeset.get("items", [])
    if items:
        result["commits"] = [
            {
                "id": c.get("commitId", "")[:12],
                "message": c.get("msg", ""),
                "author": c.get("author", {}).get("fullName", ""),
            }
            for c in items[:10]
        ]

    # Matrix build runs (sub-builds per configuration)
    runs = build.get("runs", [])
    if runs:
        run_items = []
        for r in runs:
            # Only include runs matching this build number
            if r.get("number") != build.get("number"):
                continue
            run_url = r.get("url", "")
            config_str = ""
            if "/job/" in run_url:
                parts = run_url.split("/job/")
                if len(parts) > 1:
                    remainder = parts[1]
                    segments = remainder.strip("/").split("/")
                    if len(segments) >= 2:
                        config_str = segments[1]
            run_items.append({
                "config": config_str,
                "result": r.get("result"),
                "building": r.get("building", False),
                "duration": _ms_to_human(r.get("duration")),
                "node": r.get("builtOn"),
                "url": run_url,
            })
        if run_items:
            # Sort: failures first, then building, then success
            def _run_sort_key(r: dict[str, Any]) -> tuple[int, str]:
                if r["result"] in ("FAILURE", "ABORTED"):
                    return (0, r.get("config", ""))
                if r["building"]:
                    return (1, r.get("config", ""))
                return (2, r.get("config", ""))
            run_items.sort(key=_run_sort_key)
            result["runs_total"] = len(run_items)
            result["runs_failed"] = sum(
                1 for r in run_items if r["result"] == "FAILURE"
            )
            result["runs_building"] = sum(
                1 for r in run_items if r["building"]
            )
            result["runs_success"] = sum(
                1 for r in run_items if r["result"] == "SUCCESS"
            )
            result["runs"] = run_items

    return result


# ---- Commands ----

@click.group()
@click.version_option(package_name="jenkins-tool", prog_name="jenkins")
@click.option("--envelope", is_flag=True, help="Include full response envelope (ok/data/meta wrapper)")
@click.pass_context
def main(ctx: click.Context, envelope: bool) -> None:
    """Jenkins build server CLI - query Lustre CI builds."""
    global _FULL_ENVELOPE
    _FULL_ENVELOPE = envelope


@main.command()
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--view", "view_name", default=None, help="Filter by view name")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "jobs")
def jobs(
    url: str | None,
    user: str | None,
    token: str | None,
    view_name: str | None,
    pretty: bool,
) -> None:
    """List all jobs with status.

    \b
    Examples:
      jenkins jobs
      jenkins jobs --view lustre
      jenkins jobs --pretty
    """
    client = _make_client(url, user, token)
    if view_name:
        view_data = client.get_view(view_name)
        raw_jobs = view_data.get("jobs", [])
    else:
        raw_jobs = client.get_jobs()

    items = []
    for j in raw_jobs:
        health = j.get("healthReport", [])
        items.append({
            "name": j.get("name"),
            "status": _color_to_status(j.get("color")),
            "url": j.get("url"),
            "health_score": health[0].get("score") if health else None,
            "health": health[0].get("description") if health else None,
        })

    result = {"count": len(items), "view": view_name, "jobs": items}
    next_actions = (
        [f"jenkins builds {items[0]['name']} -- recent builds for first job"]
        if items else []
    )
    env = success_response(result, TOOL_NAME, "jobs", next_actions or None)
    _output(env, pretty)


@main.command()
@click.argument("job_name")
@click.option("--limit", type=int, default=10, help="Number of builds to show (default: 10)")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "builds")
def builds(
    job_name: str,
    limit: int,
    url: str | None,
    user: str | None,
    token: str | None,
    pretty: bool,
) -> None:
    """List recent builds for a job.

    \b
    Examples:
      jenkins builds lustre-master
      jenkins builds lustre-reviews --limit 20
    """
    client = _make_client(url, user, token)
    raw_builds = client.get_builds(job_name, limit=limit)

    items = []
    for b in raw_builds:
        items.append({
            "number": b.get("number"),
            "result": b.get("result"),
            "building": b.get("building", False),
            "timestamp": _ts_to_iso(b.get("timestamp")),
            "duration": _ms_to_human(b.get("duration")),
            "url": b.get("url"),
        })

    result = {"job": job_name, "count": len(items), "limit": limit, "builds": items}
    next_actions = []
    if items:
        next_actions.append(
            f"jenkins build {job_name} {items[0]['number']} -- details of most recent build"
        )
        failed = [b for b in items if b["result"] == "FAILURE"]
        if failed:
            next_actions.append(
                f"jenkins build {job_name} {failed[0]['number']} -- details of most recent failure"
            )
    env = success_response(result, TOOL_NAME, "builds", next_actions or None)
    _output(env, pretty)


@main.command()
@click.argument("job_name")
@click.argument("build_number", default="lastBuild")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "build")
def build(
    job_name: str,
    build_number: str,
    url: str | None,
    user: str | None,
    token: str | None,
    pretty: bool,
) -> None:
    """Show details for a specific build.

    BUILD_NUMBER defaults to 'lastBuild'. Can also use 'lastSuccessfulBuild'
    or 'lastFailedBuild'.

    \b
    Examples:
      jenkins build lustre-master 4704
      jenkins build lustre-reviews lastBuild
      jenkins build lustre-master lastFailedBuild
    """
    client = _make_client(url, user, token)
    data = client.get_build(job_name, build_number)

    result = _normalize_build(data, job_name=job_name)
    bnum = result.get("number", build_number)

    # Auto-fetch error tail for failed sub-builds (limit 2, short timeout)
    failed_runs = [r for r in result.get("runs", []) if r.get("result") == "FAILURE"]
    for fr in failed_runs[:2]:
        run_url = fr.get("url", "")
        if run_url:
            try:
                url = run_url.rstrip("/") + "/consoleText"
                resp = client.session.get(url, timeout=10)
                resp.raise_for_status()
                lines = resp.text.splitlines()
                fr["error_tail"] = lines[-10:] if lines else []
            except Exception:
                fr["error_tail"] = ["(could not fetch console)"]

    next_actions = [
        f"jenkins console {job_name} {bnum} -- console output",
        f"jenkins builds {job_name} -- build history",
    ]
    for fr in failed_runs[:2]:
        cfg = fr.get("config", "")
        if cfg:
            next_actions.append(
                f'jenkins run-console {job_name} {bnum} "{cfg}" -- console for failed {cfg}'
            )
    gerrit = result.get("gerrit")
    if gerrit and gerrit.get("change"):
        next_actions.append(
            f"jenkins review {gerrit['change']} -- all builds for this Gerrit change"
        )
    if result.get("result") == "FAILURE":
        next_actions.append(f"jenkins retrigger {job_name} {bnum} -- retrigger this failed build")
    env = success_response(result, TOOL_NAME, "build", next_actions)
    _output(env, pretty)


@main.command()
@click.argument("job_name")
@click.argument("build_number", default="lastBuild")
@click.option("--tail", type=int, default=200, help="Number of lines from end (default: 200)")
@click.option("--head", type=int, default=None, help="Number of lines from start")
@click.option("--grep", "grep_pattern", default=None, help="Filter lines matching pattern")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "console")
def console(
    job_name: str,
    build_number: str,
    tail: int,
    head: int | None,
    grep_pattern: str | None,
    url: str | None,
    user: str | None,
    token: str | None,
    pretty: bool,
) -> None:
    """Get console output for a build.

    By default shows the last 200 lines. Use --tail or --head to control
    how much output to show. Use --grep to filter for specific patterns.

    \b
    Examples:
      jenkins console lustre-master 4704
      jenkins console lustre-reviews lastBuild --tail 50
      jenkins console lustre-master lastFailedBuild --grep "error"
      jenkins console lustre-master 4704 --head 100
    """
    client = _make_client(url, user, token)
    text = client.get_console_text(job_name, build_number)

    lines = text.splitlines()
    total_lines = len(lines)
    next_action = [f"jenkins build {job_name} {build_number} -- build details"]

    if grep_pattern:
        try:
            pattern = re.compile(grep_pattern, re.IGNORECASE)
        except re.error:
            _error("INVALID_INPUT", f"Invalid regex: {grep_pattern}", "console", pretty)
            return
        matched = [
            {"line_number": i + 1, "text": line}
            for i, line in enumerate(lines)
            if pattern.search(line)
        ]
        result: dict[str, Any] = {
            "job": job_name, "build": build_number,
            "total_lines": total_lines, "grep_pattern": grep_pattern,
            "match_count": len(matched), "matches": matched[:200],
        }
        _output(success_response(result, TOOL_NAME, "console", next_action), pretty)
        return

    if head is not None:
        selected = lines[:head]
        showing = f"first {len(selected)} lines"
    else:
        selected = lines[-tail:] if tail < total_lines else lines
        showing = (
            f"last {len(selected)} lines" if len(selected) < total_lines
            else f"all {total_lines} lines"
        )

    result = {
        "job": job_name, "build": build_number,
        "total_lines": total_lines, "showing": showing, "lines": selected,
    }
    _output(success_response(result, TOOL_NAME, "console", next_action), pretty)


@main.command()
@click.argument("change_number", type=int)
@click.option("--job", default=None, help="Specific job to search (default: all *-reviews jobs)")
@click.option("--limit", type=int, default=20, help="Max builds to search per job (default: 20)")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "review")
def review(
    change_number: int,
    job: str | None,
    limit: int,
    url: str | None,
    user: str | None,
    token: str | None,
    pretty: bool,
) -> None:
    """Find builds for a Gerrit review change number.

    Searches recent builds of review jobs for ones triggered
    by the specified Gerrit change.

    \b
    Examples:
      jenkins review 54225
      jenkins review 54225 --job lustre-reviews
    """
    client = _make_client(url, user, token)
    if job:
        matches = client.find_builds_by_gerrit_change(
            job, change_number, max_builds=limit
        )
        for m in matches:
            m["_job_name"] = job
    else:
        matches = client.find_review_builds(change_number, max_builds=limit)

    items = [_normalize_build(m) for m in matches]

    result = {
        "change_number": change_number,
        "job_filter": job,
        "count": len(items),
        "builds": items,
    }
    next_actions = []
    if items:
        b = items[0]
        j = b.get("job", "lustre-reviews")
        next_actions.append(
            f"jenkins console {j} {b['number']} -- console output of latest build"
        )
        failed = [b for b in items if b.get("result") == "FAILURE"]
        if failed:
            fj = failed[0].get("job", "lustre-reviews")
            next_actions.append(
                f"jenkins console {fj} {failed[0]['number']} -- console output of failed build"
            )
    _output(success_response(result, TOOL_NAME, "review", next_actions or None), pretty)


@main.command(name="run-console")
@click.argument("job_name")
@click.argument("build_number", type=int)
@click.argument("config")
@click.option("--tail", type=int, default=200, help="Number of lines from end (default: 200)")
@click.option("--head", type=int, default=None, help="Number of lines from start")
@click.option("--grep", "grep_pattern", default=None, help="Filter lines matching pattern")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "run-console")
def run_console(
    job_name: str,
    build_number: int,
    config: str,
    tail: int,
    head: int | None,
    grep_pattern: str | None,
    url: str | None,
    user: str | None,
    token: str | None,
    pretty: bool,
) -> None:
    """Get console output for a specific matrix sub-build (run).

    CONFIG is the matrix configuration string, e.g.:
    "arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel"

    You can get the config strings from 'jenkins build' output (runs field).

    \b
    Examples:
      jenkins run-console lustre-reviews 121880 "arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel"
      jenkins run-console lustre-reviews 121880 "arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel" --tail 50
      jenkins run-console lustre-reviews 121880 "arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel" --grep "error"
    """
    client = _make_client(url, user, token)
    run_url = f"{client.config.base_url}/job/{job_name}/{config}/{build_number}"
    text = client.get_run_console_text(run_url)

    lines = text.splitlines()
    total_lines = len(lines)
    next_action = [f"jenkins build {job_name} {build_number} -- full build details with all runs"]

    if grep_pattern:
        try:
            pattern = re.compile(grep_pattern, re.IGNORECASE)
        except re.error:
            _error("INVALID_INPUT", f"Invalid regex: {grep_pattern}", "run-console", pretty)
            return
        matched = [
            {"line_number": i + 1, "text": line}
            for i, line in enumerate(lines)
            if pattern.search(line)
        ]
        result: dict[str, Any] = {
            "job": job_name, "build": build_number, "config": config,
            "total_lines": total_lines, "grep_pattern": grep_pattern,
            "match_count": len(matched), "matches": matched[:200],
        }
        _output(success_response(result, TOOL_NAME, "run-console", next_action), pretty)
        return

    if head is not None:
        selected = lines[:head]
        showing = f"first {len(selected)} lines"
    else:
        selected = lines[-tail:] if tail < total_lines else lines
        showing = (
            f"last {len(selected)} lines" if len(selected) < total_lines
            else f"all {total_lines} lines"
        )

    result = {
        "job": job_name, "build": build_number, "config": config,
        "total_lines": total_lines, "showing": showing, "lines": selected,
    }
    _output(success_response(result, TOOL_NAME, "run-console", next_action), pretty)


@main.command()
@click.argument("job_name")
@click.argument("build_number", type=int)
@click.option("--kill", "force_kill", is_flag=True, default=False,
              help="Hard-kill instead of graceful stop")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "abort")
def abort(
    job_name: str,
    build_number: int,
    force_kill: bool,
    url: str | None,
    user: str | None,
    token: str | None,
    pretty: bool,
) -> None:
    """Abort a running build and all its sub-builds.

    For matrix builds (like lustre-reviews), aborting the parent
    build stops all running sub-builds (configurations).

    Use --kill for a hard stop if the graceful abort doesn't work.

    \b
    Examples:
      jenkins abort lustre-reviews 121884
      jenkins abort lustre-reviews 121884 --kill
    """
    client = _make_client(url, user, token)

    # First check if the build is actually running
    data = client.get_build(job_name, build_number)
    if not data.get("building", False):
        msg = f"Build {build_number} is not running (result: {data.get('result')})"
        result: dict[str, Any] = {
            "job": job_name, "build": build_number,
            "message": msg, "aborted": False,
        }
        _output(success_response(result, TOOL_NAME, "abort"), pretty)
        return

    if force_kill:
        client.kill_build(job_name, build_number)
        action = "killed"
    else:
        client.abort_build(job_name, build_number)
        action = "aborted"

    # Count how many sub-builds were running
    runs = data.get("runs", [])
    running_runs = [
        r for r in runs
        if r.get("number") == data.get("number") and r.get("building", False)
    ]

    result = {
        "job": job_name, "build": build_number,
        "action": action, "aborted": True,
        "message": f"Build {build_number} {action} successfully",
    }
    if running_runs:
        result["sub_builds_stopped"] = len(running_runs)
    env = success_response(result, TOOL_NAME, "abort", [
        f"jenkins build {job_name} {build_number} -- verify build status",
        f"jenkins builds {job_name} -- build history",
    ])
    _output(env, pretty)


@main.command()
@click.argument("job_name")
@click.argument("build_number", type=int)
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@handle_errors(TOOL_NAME, "retrigger")
def retrigger(
    job_name: str,
    build_number: int,
    url: str | None,
    user: str | None,
    token: str | None,
    pretty: bool,
) -> None:
    """Retrigger a Gerrit-triggered build.

    Uses the Gerrit Trigger plugin's retrigger action to re-run
    a build with the same Gerrit event parameters. Useful when a
    build failed for infrastructure reasons rather than code issues.

    \b
    Examples:
      jenkins retrigger lustre-reviews 121880
      jenkins retrigger lustre-master 4699
    """
    client = _make_client(url, user, token)
    location = client.retrigger_build(job_name, build_number)

    result = {
        "success": True,
        "job": job_name,
        "original_build": build_number,
        "message": f"Build {build_number} retriggered successfully",
        "redirect": location,
    }
    env = success_response(result, TOOL_NAME, "retrigger",
                           [f"jenkins builds {job_name} -- check for new build"])
    _output(env, pretty)


@main.command()
@click.option("--command", "command_name", default=None, help="Show specific command")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
def describe(command_name: str | None, pretty: bool) -> None:
    """Show machine-readable tool description (always JSON).

    \b
    Examples:
      jenkins describe
      jenkins describe --command jobs
      jenkins describe --pretty
    """
    from .describe import get_tool_description

    tool_desc = get_tool_description()

    if command_name:
        matching = [c for c in tool_desc.commands if c.name == command_name]
        if not matching:
            env = error_response_from_dict(
                "NOT_FOUND", f"Command '{command_name}' not found", TOOL_NAME, "describe"
            )
            click.echo(format_json(env, pretty=pretty, full_envelope=_FULL_ENVELOPE))
            sys.exit(1)
            return
        data = matching[0].to_dict()
    else:
        data = tool_desc.to_dict()

    env = success_response(data, TOOL_NAME, "describe")
    click.echo(format_json(env, pretty=pretty, full_envelope=_FULL_ENVELOPE))


if __name__ == "__main__":
    main()
