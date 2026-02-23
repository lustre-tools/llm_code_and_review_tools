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

from .client import JenkinsClient
from .config import load_config

TOOL_NAME = "jenkins"


def _make_client(
    url: str | None = None,
    user: str | None = None,
    token: str | None = None,
) -> JenkinsClient:
    config = load_config(
        url_override=url, user_override=user, token_override=token
    )
    return JenkinsClient(config)


def _json_output(envelope: dict[str, Any]) -> None:
    click.echo(format_json(envelope, pretty=False))


def _error(
    code: str, message: str, command: str, json_out: bool
) -> None:
    if json_out:
        env = error_response_from_dict(code, message, TOOL_NAME, command)
        _json_output(env)
    else:
        click.echo(f"Error: {message}", err=True)
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


def _result_col(result: str | None, building: bool = False, width: int = 10) -> str:
    """Colorized result string padded to visual width (ANSI-safe)."""
    if building:
        label = "BUILDING"
        colored = click.style(label, fg="cyan")
    elif result == "SUCCESS":
        label = result
        colored = click.style(result, fg="green")
    elif result == "FAILURE":
        label = result
        colored = click.style(result, fg="red")
    elif result == "ABORTED":
        label = result
        colored = click.style(result, fg="yellow")
    else:
        label = result or "unknown"
        colored = label
    return colored + " " * max(0, width - len(label))


def _col(text: Any, width: int) -> str:
    """Left-align text in a fixed-width column, truncating with ellipsis."""
    s = str(text) if text is not None else ""
    if len(s) > width:
        return s[:width - 1] + "\u2026"
    return s.ljust(width)


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


# ---- Human output helpers ----

def _print_console_text(
    lines: list[str],
    total_lines: int,
    header: str,
    grep_pattern: str | None = None,
    matched: list[dict[str, Any]] | None = None,
) -> None:
    """Print console output with a header/footer."""
    click.echo(f"=== {header} ===")
    if grep_pattern is not None and matched is not None:
        for m in matched:
            click.echo(f"  L{m['line_number']}: {m['text']}")
        click.echo(f"--- {len(matched)} match(es) for '{grep_pattern}' in {total_lines} lines ---")
    else:
        for line in lines:
            click.echo(line)
        if len(lines) < total_lines:
            click.echo(f"--- {total_lines} lines total, showing {len(lines)} ---")


# ---- Commands ----

@click.group()
def main() -> None:
    """Jenkins build server CLI - query Lustre CI builds."""
    pass


@main.command()
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--view", "view_name", default=None, help="Filter by view name")
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
def jobs(
    url: str | None,
    user: str | None,
    token: str | None,
    view_name: str | None,
    json_out: bool,
) -> None:
    """List all jobs with status.

    \b
    Examples:
      jenkins jobs
      jenkins jobs --view lustre
      jenkins jobs --json
    """
    try:
        client = _make_client(url, user, token)
        if view_name:
            view_data = client.get_view(view_name)
            raw_jobs = view_data.get("jobs", [])
        else:
            raw_jobs = client.get_jobs()
    except requests.HTTPError as e:
        _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "jobs", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "jobs", json_out)
        return

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

    if json_out:
        result = {"count": len(items), "view": view_name, "jobs": items}
        next_actions = (
            [f"jenkins builds {items[0]['name']} -- recent builds for first job"]
            if items else []
        )
        env = success_response(result, TOOL_NAME, "jobs", next_actions or None)
        _json_output(env)
        return

    title = f"{len(items)} jobs"
    if view_name:
        title += f"  (view: {view_name})"
    click.echo(title)
    click.echo(f"  {'NAME':<40}  {'STATUS':<35}  HEALTH")
    click.echo(f"  {'-'*40}  {'-'*35}  ------")
    for j in items:
        score = j["health_score"]
        health_str = f"{score}%" if score is not None else ""
        click.echo(f"  {_col(j['name'], 40)}  {_col(j['status'], 35)}  {health_str}")


@main.command()
@click.argument("job_name")
@click.option("--limit", type=int, default=10, help="Number of builds to show (default: 10)")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
def builds(
    job_name: str,
    limit: int,
    url: str | None,
    user: str | None,
    token: str | None,
    json_out: bool,
) -> None:
    """List recent builds for a job.

    \b
    Examples:
      jenkins builds lustre-master
      jenkins builds lustre-reviews --limit 20
    """
    try:
        client = _make_client(url, user, token)
        raw_builds = client.get_builds(job_name, limit=limit)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _error("NOT_FOUND", f"Job '{job_name}' not found", "builds", json_out)
        else:
            _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "builds", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "builds", json_out)
        return

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

    if json_out:
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
        _json_output(env)
        return

    click.echo(f"{job_name} — {len(items)} builds")
    click.echo(f"  {'BUILD':<8}  {'RESULT':<10}  {'STARTED':<16}  DURATION")
    click.echo(f"  {'-'*8}  {'-'*10}  {'-'*16}  --------")
    for b in items:
        num = f"#{b['number']}"
        ts = (b.get("timestamp") or "")[:16].replace("T", " ")
        dur = b.get("duration") or ""
        click.echo(
            f"  {_col(num, 8)}  {_result_col(b.get('result'), b.get('building', False), 10)}"
            f"  {_col(ts, 16)}  {dur}"
        )


@main.command()
@click.argument("job_name")
@click.argument("build_number", default="lastBuild")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
def build(
    job_name: str,
    build_number: str,
    url: str | None,
    user: str | None,
    token: str | None,
    json_out: bool,
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
    try:
        client = _make_client(url, user, token)
        data = client.get_build(job_name, build_number)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _error("NOT_FOUND", f"Build {build_number} not found for '{job_name}'", "build", json_out)
        else:
            _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "build", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "build", json_out)
        return

    result = _normalize_build(data, job_name=job_name)
    bnum = result.get("number", build_number)

    if json_out:
        next_actions = [
            f"jenkins console {job_name} {bnum} -- console output",
            f"jenkins builds {job_name} -- build history",
        ]
        for fr in [r for r in result.get("runs", []) if r.get("result") == "FAILURE"][:2]:
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
        _json_output(env)
        return

    # Human output
    dur = result.get("duration") or ""
    ts = (result.get("timestamp") or "")[:19].replace("T", " ")
    result_label = _result_col(result.get("result"), result.get("building", False), 0)

    header = f"Build #{bnum} — {result_label}"
    if dur:
        header += f" — {dur}"
    click.echo(header)
    click.echo(f"  Job:     {job_name}")
    if ts:
        click.echo(f"  Started: {ts}")

    for i, cause in enumerate(result.get("causes", [])):
        label = "Cause:  " if i == 0 else "        "
        click.echo(f"  {label} {cause}")

    gerrit = result.get("gerrit")
    if gerrit:
        change = gerrit.get("change", "")
        ps = gerrit.get("patchset", "")
        branch = gerrit.get("branch", "")
        subj = gerrit.get("subject", "")
        owner = gerrit.get("owner", "")
        refspec = gerrit.get("refspec", "")
        click.echo(f'  Gerrit:  Change {change} PS{ps} | branch: {branch} | "{subj}"')
        parts = []
        if owner:
            parts.append(f"Owner: {owner}")
        if refspec:
            parts.append(refspec)
        if parts:
            click.echo(f"           {' | '.join(parts)}")

    runs = result.get("runs", [])
    if runs:
        n_total = result.get("runs_total", len(runs))
        n_failed = result.get("runs_failed", 0)
        n_building = result.get("runs_building", 0)
        n_success = result.get("runs_success", 0)
        n_aborted = sum(1 for r in runs if r.get("result") == "ABORTED")

        summary_parts = [f"{n_total} runs"]
        if n_failed:
            summary_parts.append(click.style(f"{n_failed} FAILED", fg="red"))
        if n_building:
            summary_parts.append(click.style(f"{n_building} BUILDING", fg="cyan"))
        if n_aborted:
            summary_parts.append(click.style(f"{n_aborted} ABORTED", fg="yellow"))
        if n_success:
            summary_parts.append(click.style(f"{n_success} SUCCESS", fg="green"))
        click.echo(f"\n  Matrix: {' — '.join(summary_parts)}")
        click.echo(f"  {'CONFIG':<58}  {'RESULT':<10}  {'DURATION':<10}  NODE")
        click.echo(f"  {'-'*58}  {'-'*10}  {'-'*10}  ----")
        for r in runs:
            cfg = r.get("config", "")
            rdur = r.get("duration") or ""
            node = r.get("node") or ""
            if len(node) > 28:
                node = node[:27] + "\u2026"
            click.echo(
                f"  {_col(cfg, 58)}  "
                f"{_result_col(r.get('result'), r.get('building', False), 10)}"
                f"  {_col(rdur, 10)}  {node}"
            )


@main.command()
@click.argument("job_name")
@click.argument("build_number", default="lastBuild")
@click.option("--tail", type=int, default=200, help="Number of lines from end (default: 200)")
@click.option("--head", type=int, default=None, help="Number of lines from start")
@click.option("--grep", "grep_pattern", default=None, help="Filter lines matching pattern")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
def console(
    job_name: str,
    build_number: str,
    tail: int,
    head: int | None,
    grep_pattern: str | None,
    url: str | None,
    user: str | None,
    token: str | None,
    json_out: bool,
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
    try:
        client = _make_client(url, user, token)
        text = client.get_console_text(job_name, build_number)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _error("NOT_FOUND", f"Build {build_number} not found for '{job_name}'", "console", json_out)
        else:
            _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "console", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "console", json_out)
        return

    lines = text.splitlines()
    total_lines = len(lines)
    next_action = [f"jenkins build {job_name} {build_number} -- build details"]

    if grep_pattern:
        try:
            pattern = re.compile(grep_pattern, re.IGNORECASE)
        except re.error:
            _error("INVALID_INPUT", f"Invalid regex: {grep_pattern}", "console", json_out)
            return
        matched = [
            {"line_number": i + 1, "text": line}
            for i, line in enumerate(lines)
            if pattern.search(line)
        ]
        if json_out:
            result: dict[str, Any] = {
                "job": job_name, "build": build_number,
                "total_lines": total_lines, "grep_pattern": grep_pattern,
                "match_count": len(matched), "matches": matched[:200],
            }
            _json_output(success_response(result, TOOL_NAME, "console", next_action))
        else:
            header = f"{job_name} #{build_number} | grep: \"{grep_pattern}\" | {len(matched)} match(es)"
            _print_console_text([], total_lines, header, grep_pattern, matched[:200])
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

    if json_out:
        result = {
            "job": job_name, "build": build_number,
            "total_lines": total_lines, "showing": showing, "lines": selected,
        }
        _json_output(success_response(result, TOOL_NAME, "console", next_action))
        return

    header = f"{job_name} #{build_number} | {total_lines} lines | {showing}"
    _print_console_text(selected, total_lines, header)


@main.command()
@click.argument("change_number", type=int)
@click.option("--job", default=None, help="Specific job to search (default: all *-reviews jobs)")
@click.option("--limit", type=int, default=20, help="Max builds to search per job (default: 20)")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
def review(
    change_number: int,
    job: str | None,
    limit: int,
    url: str | None,
    user: str | None,
    token: str | None,
    json_out: bool,
) -> None:
    """Find builds for a Gerrit review change number.

    Searches recent builds of review jobs for ones triggered
    by the specified Gerrit change.

    \b
    Examples:
      jenkins review 54225
      jenkins review 54225 --job lustre-reviews
    """
    try:
        client = _make_client(url, user, token)
        if job:
            matches = client.find_builds_by_gerrit_change(
                job, change_number, max_builds=limit
            )
            for m in matches:
                m["_job_name"] = job
        else:
            matches = client.find_review_builds(change_number, max_builds=limit)
    except requests.HTTPError as e:
        _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "review", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "review", json_out)
        return

    items = [_normalize_build(m) for m in matches]

    if json_out:
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
        _json_output(success_response(result, TOOL_NAME, "review", next_actions or None))
        return

    click.echo(f"{len(items)} build(s) for Gerrit change {change_number}")
    if items:
        job_col = max((len(b.get("job", "")) for b in items), default=14)
        job_col = max(job_col, 14)
        click.echo(
            f"  {'BUILD':<8}  {_col('JOB', job_col)}  {'RESULT':<10}  {'STARTED':<16}  DURATION"
        )
        click.echo(
            f"  {'-'*8}  {'-'*job_col}  {'-'*10}  {'-'*16}  --------"
        )
        for b in items:
            num = f"#{b.get('number', '?')}"
            ts = (b.get("timestamp") or "")[:16].replace("T", " ")
            dur = b.get("duration") or ""
            job_val = b.get("job", "")
            click.echo(
                f"  {_col(num, 8)}  {_col(job_val, job_col)}  "
                f"{_result_col(b.get('result'), b.get('building', False), 10)}"
                f"  {_col(ts, 16)}  {dur}"
            )


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
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
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
    json_out: bool,
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
    try:
        client = _make_client(url, user, token)
        run_url = f"{client.config.base_url}/job/{job_name}/{config}/{build_number}"
        text = client.get_run_console_text(run_url)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _error(
                "NOT_FOUND",
                f"Run not found: {job_name}/{config}/{build_number}",
                "run-console", json_out,
            )
        else:
            _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "run-console", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "run-console", json_out)
        return

    lines = text.splitlines()
    total_lines = len(lines)
    next_action = [f"jenkins build {job_name} {build_number} -- full build details with all runs"]

    if grep_pattern:
        try:
            pattern = re.compile(grep_pattern, re.IGNORECASE)
        except re.error:
            _error("INVALID_INPUT", f"Invalid regex: {grep_pattern}", "run-console", json_out)
            return
        matched = [
            {"line_number": i + 1, "text": line}
            for i, line in enumerate(lines)
            if pattern.search(line)
        ]
        if json_out:
            result: dict[str, Any] = {
                "job": job_name, "build": build_number, "config": config,
                "total_lines": total_lines, "grep_pattern": grep_pattern,
                "match_count": len(matched), "matches": matched[:200],
            }
            _json_output(success_response(result, TOOL_NAME, "run-console", next_action))
        else:
            header = (
                f"{job_name} #{build_number} [{config}]"
                f" | grep: \"{grep_pattern}\" | {len(matched)} match(es)"
            )
            _print_console_text([], total_lines, header, grep_pattern, matched[:200])
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

    if json_out:
        result = {
            "job": job_name, "build": build_number, "config": config,
            "total_lines": total_lines, "showing": showing, "lines": selected,
        }
        _json_output(success_response(result, TOOL_NAME, "run-console", next_action))
        return

    header = f"{job_name} #{build_number} [{config}] | {total_lines} lines | {showing}"
    _print_console_text(selected, total_lines, header)


@main.command()
@click.argument("job_name")
@click.argument("build_number", type=int)
@click.option("--kill", "force_kill", is_flag=True, default=False,
              help="Hard-kill instead of graceful stop")
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
def abort(
    job_name: str,
    build_number: int,
    force_kill: bool,
    url: str | None,
    user: str | None,
    token: str | None,
    json_out: bool,
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
    try:
        client = _make_client(url, user, token)

        # First check if the build is actually running
        data = client.get_build(job_name, build_number)
        if not data.get("building", False):
            msg = f"Build {build_number} is not running (result: {data.get('result')})"
            if json_out:
                result: dict[str, Any] = {
                    "job": job_name, "build": build_number,
                    "message": msg, "aborted": False,
                }
                _json_output(success_response(result, TOOL_NAME, "abort"))
            else:
                click.echo(msg)
            return

        if force_kill:
            client.kill_build(job_name, build_number)
            action = "killed"
        else:
            client.abort_build(job_name, build_number)
            action = "aborted"

    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _error("NOT_FOUND", f"Build {build_number} not found for '{job_name}'", "abort", json_out)
        else:
            _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "abort", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "abort", json_out)
        return

    # Count how many sub-builds were running
    runs = data.get("runs", [])
    running_runs = [
        r for r in runs
        if r.get("number") == data.get("number") and r.get("building", False)
    ]

    if json_out:
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
        _json_output(env)
        return

    msg = f"Build {build_number} {action} successfully"
    if running_runs:
        msg += f" ({len(running_runs)} sub-build(s) stopped)"
    click.echo(msg)


@main.command()
@click.argument("job_name")
@click.argument("build_number", type=int)
@click.option("--url", envvar="JENKINS_URL", default=None, help="Jenkins server URL")
@click.option("--user", envvar="JENKINS_USER", default=None, help="Jenkins username")
@click.option("--token", envvar="JENKINS_TOKEN", default=None, help="Jenkins API token")
@click.option("--json", "json_out", is_flag=True, help="Output raw JSON envelope")
def retrigger(
    job_name: str,
    build_number: int,
    url: str | None,
    user: str | None,
    token: str | None,
    json_out: bool,
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
    try:
        client = _make_client(url, user, token)
        location = client.retrigger_build(job_name, build_number)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _error(
                "NOT_FOUND",
                f"Build {build_number} not found for '{job_name}' "
                "(or gerrit-trigger retrigger not available)",
                "retrigger", json_out,
            )
        else:
            _error("API_ERROR", f"HTTP {e.response.status_code}: {e}", "retrigger", json_out)
        return
    except Exception as exc:
        _error("API_ERROR", str(exc), "retrigger", json_out)
        return

    if json_out:
        result = {
            "success": True,
            "job": job_name,
            "original_build": build_number,
            "message": f"Build {build_number} retriggered successfully",
            "redirect": location,
        }
        env = success_response(result, TOOL_NAME, "retrigger",
                               [f"jenkins builds {job_name} -- check for new build"])
        _json_output(env)
        return

    click.echo(f"Build {build_number} retriggered successfully")


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
            click.echo(format_json(env, pretty=pretty))
            sys.exit(1)
            return
        data = matching[0].to_dict()
    else:
        data = tool_desc.to_dict()

    env = success_response(data, TOOL_NAME, "describe")
    click.echo(format_json(env, pretty=pretty))


if __name__ == "__main__":
    main()
