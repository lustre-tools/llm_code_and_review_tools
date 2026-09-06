"""CLI entry point for Gerrit Janitor test results tool."""

from __future__ import annotations

import re
import sys
from typing import Any

import click

from llm_tool_common.envelope import (
    error_response_from_dict,
    format_json,
    success_response,
)

from .client import JanitorClient
from .config import load_config
from .errors import ErrorCode

TOOL_NAME = "janitor"

_FULL_ENVELOPE = False

# Crash-related patterns to grep for in logs
CRASH_PATTERNS = [
    r"LBUG",
    r"LASSERT",
    r"ASSERTION",
    r"kernel BUG",
    r"Kernel panic",
    r"Oops:",
    r"general protection fault",
    r"RIP:",
    r"Call Trace:",
]
CRASH_RE = re.compile("|".join(CRASH_PATTERNS), re.IGNORECASE)


def _make_client() -> JanitorClient:
    config = load_config()
    return JanitorClient(config)


#: Set by _resolve_build() when a bare number had to be interpreted
#: without being able to rule out the other interpretation.
_RESOLVE_WARNING: str | None = None


def _output(envelope: dict[str, Any], pretty: bool) -> None:
    if _RESOLVE_WARNING and isinstance(envelope.get("data"), dict):
        envelope["data"]["warning"] = _RESOLVE_WARNING
    click.echo(
        format_json(envelope, pretty=pretty, full_envelope=_FULL_ENVELOPE)
    )


def _error(
    code: str, message: str, command: str, pretty: bool
) -> None:
    env = error_response_from_dict(code, message, TOOL_NAME, command)
    _output(env, pretty)
    sys.exit(1)


def _resolve_build(
    client: JanitorClient,
    build_or_change: str,
    command: str,
    pretty: bool,
    as_build: bool = False,
    as_change: bool = False,
) -> int:
    """Resolve a build number or Gerrit change to a Janitor build.

    Accepts:
      - A Janitor build number (e.g. 61009)
      - A Gerrit change number (e.g. 64440) -- resolved via REF files
      - A Gerrit URL (e.g. https://review.whamcloud.com/c/.../+/64440)

    Build numbers and Gerrit change numbers share an ID space (both
    are 5-digit and currently overlap), so a bare number can be a
    valid build AND a valid change -- pointing at two unrelated
    patches.  Guessing there silently returns another patch's test
    results, so an ambiguous number is refused; --build/--change say
    which was meant.
    """
    # Extract change number from Gerrit URL
    m = re.search(r"/\+/(\d+)", build_or_change)
    if m:
        # A Gerrit URL is unambiguous: it names a change.
        build_or_change = m.group(1)
        as_change = True

    val = int(build_or_change)

    if as_build:
        if client.get_ref(val):
            return val
        _error(
            ErrorCode.BUILD_NOT_FOUND,
            f"No Janitor build {val}.",
            command,
            pretty,
        )
        return 0  # unreachable

    if as_change:
        build = client.resolve_change(val)
        if build:
            return build
        if client.change_lookup_error:
            _error(
                ErrorCode.BUILD_NOT_FOUND,
                f"Could not resolve Gerrit change {val} to a build: "
                f"{client.change_lookup_error}. The Janitor posts its "
                f"build URL on the change (gerrit-janitor/<build>/"
                f"results.html), so pass that build with --build.",
                command,
                pretty,
            )
        _error(
            ErrorCode.BUILD_NOT_FOUND,
            f"No Janitor build found for Gerrit change {val}.",
            command,
            pretty,
        )
        return 0  # unreachable

    global _RESOLVE_WARNING
    ref = client.get_ref(val)
    change_build = client.resolve_change(val)
    lookup_error = client.change_lookup_error

    if ref and change_build:
        _error(
            ErrorCode.BUILD_NOT_FOUND,
            f"'{val}' is both Janitor build {val} (which tested Gerrit "
            f"change {ref.get('change')}) and Gerrit change {val} (tested "
            f"by build {change_build}). These are different patches. "
            f"Pass --build {val} or --change {val} to say which.",
            command,
            pretty,
        )
        return 0  # unreachable

    if ref:
        # Build and change numbers share an ID space, so when the
        # change lookup could not run we cannot say this is not also
        # a change number.  Answer, but say the check did not happen.
        if lookup_error:
            _RESOLVE_WARNING = (
                f"Read {val} as a Janitor build number; it tested Gerrit "
                f"change {ref.get('change')}. Could not check whether "
                f"{val} is also a Gerrit change number, because "
                f"{lookup_error}. If you meant Gerrit change {val}, this "
                f"is a different patch -- take the build number from the "
                f"Janitor comment on the change."
            )
        return val

    if change_build:
        return change_build

    if lookup_error:
        _error(
            ErrorCode.BUILD_NOT_FOUND,
            f"'{val}' is not a Janitor build, and it could not be "
            f"checked as a Gerrit change because {lookup_error}.",
            command,
            pretty,
        )
    _error(
        ErrorCode.BUILD_NOT_FOUND,
        f"No Janitor build found for '{build_or_change}'. "
        f"Try a build number or Gerrit change number.",
        command,
        pretty,
    )
    return 0  # unreachable


@click.group()
@click.version_option(package_name="janitor-tool", prog_name="janitor")
@click.option(
    "--envelope", is_flag=True,
    help="Include full response envelope",
)
@click.pass_context
def main(ctx: click.Context, envelope: bool) -> None:
    """Gerrit Janitor test results CLI.

    Query Lustre Gerrit Janitor initial and comprehensive test results.
    Unlike Maloo (which handles enforced CI), the Janitor runs its own
    test infrastructure with direct access to console logs, crash data,
    and per-test YAML results.
    """
    global _FULL_ENVELOPE
    _FULL_ENVELOPE = envelope


@main.command()
@click.argument("build_or_change")
@click.option("--failures-only", is_flag=True,
              help="Only show failed/crashed tests")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
@click.option("--build", "as_build", is_flag=True,
              help="Read the number as a Janitor build number")
@click.option("--change", "as_change", is_flag=True,
              help="Read the number as a Gerrit change number")
def results(build_or_change: str, failures_only: bool, pretty: bool,
            as_build: bool = False, as_change: bool = False) -> None:
    """Show test results for a Janitor build.

    BUILD_OR_CHANGE can be a Janitor build number, a Gerrit change
    number, or a Gerrit URL.  Build and change numbers overlap, so
    a bare number that is both is refused -- use --build/--change.

    \b
    Examples:
      janitor results 61009
      janitor results 64440
      janitor results --build 61009
      janitor results --change 64440
      janitor results --failures-only 61009
    """
    client = _make_client()
    build = _resolve_build(client, build_or_change, "results", pretty,
                           as_build=as_build, as_change=as_change)
    data = client.get_results(build)
    if not data:
        _error(
            ErrorCode.BUILD_NOT_FOUND,
            f"Could not fetch results for build {build}",
            "results", pretty,
        )

    # Compute summary
    all_tests = []
    for section in data["sections"]:
        for t in section["tests"]:
            t["phase"] = section["phase"]
            all_tests.append(t)

    passed = sum(1 for t in all_tests if t["status"] == "PASS")
    failed = sum(
        1 for t in all_tests
        if t["status"] in ("FAIL", "CRASH", "TIMEOUT")
    )
    not_run = sum(1 for t in all_tests if t["status"] == "NOT_RUN")

    if failures_only:
        for section in data["sections"]:
            section["tests"] = [
                t for t in section["tests"]
                if t["status"] in ("FAIL", "CRASH", "TIMEOUT")
            ]

    result = {
        "build": data["build"],
        "change": data["change"],
        "patchset": data["patchset"],
        "subject": data["subject"],
        "build_status": data["build_status"],
        "distros": data["distros"],
        "summary": {
            "passed": passed,
            "failed": failed,
            "not_run": not_run,
            "total": len(all_tests),
        },
        "sections": data["sections"],
        "url": data["url"],
    }

    next_actions = []
    failures = [
        t for t in all_tests
        if t["status"] in ("FAIL", "CRASH", "TIMEOUT")
    ]
    if failures:
        for f in failures[:3]:
            test = f["test"]
            if f["status"] == "CRASH":
                next_actions.append(
                    f"janitor crash {build} \"{test}\""
                    f" -- get crash logs"
                )
            else:
                next_actions.append(
                    f"janitor logs {build} \"{test}\""
                    f" -- get test logs"
                )
        next_actions.append(
            f"janitor detail {build} \"{failures[0]['test']}\""
            f" -- get per-subtest results"
        )

    env = success_response(result, TOOL_NAME, "results", next_actions)
    _output(env, pretty)


@main.command()
@click.argument("build_or_change")
@click.argument("test_name")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
@click.option("--build", "as_build", is_flag=True,
              help="Read the number as a Janitor build number")
@click.option("--change", "as_change", is_flag=True,
              help="Read the number as a Gerrit change number")
def detail(build_or_change: str, test_name: str, pretty: bool,
           as_build: bool = False, as_change: bool = False) -> None:
    """Show per-subtest results from results.yml.

    Parses the YAML results file for a specific test to show
    individual subtest pass/fail status.

    \b
    Examples:
      janitor detail 61009 "sanity2@ldiskfs+DNE"
      janitor detail 64440 "sanity3@zfs"
    """
    client = _make_client()
    build = _resolve_build(client, build_or_change, "detail", pretty,
                           as_build=as_build, as_change=as_change)

    test_dir = client.find_test_dir(build, test_name)
    if not test_dir:
        _error(
            ErrorCode.TEST_NOT_FOUND,
            f"No test directory found for '{test_name}' in build {build}",
            "detail", pretty,
        )

    data = client.get_test_yaml(build, test_dir)
    if not data:
        _error(
            ErrorCode.TEST_NOT_FOUND,
            f"No results.yml for '{test_name}' in build {build}",
            "detail", pretty,
        )

    # Parse subtests
    tests = data.get("Tests", [])
    all_subtests = []
    for suite in tests:
        suite_name = suite.get("name", "unknown")
        for st in suite.get("SubTests", []):
            entry = {
                "suite": suite_name,
                "name": st.get("name", ""),
                "status": st.get("status", ""),
                "duration": st.get("duration"),
                "return_code": st.get("return_code"),
                "error": st.get("error", ""),
            }
            all_subtests.append(entry)

    failed = [
        s for s in all_subtests
        if s["status"] in ("FAIL", "CRASH", "TIMEOUT", "ABORT")
    ]

    result = {
        "build": build,
        "test": test_name,
        "test_dir": test_dir,
        "test_host": data.get("TestGroup", {}).get("testhost", ""),
        "total_subtests": len(all_subtests),
        "failed_subtests": failed,
        "passed": sum(1 for s in all_subtests if s["status"] == "PASS"),
        "failed_count": len(failed),
    }

    next_actions = []
    if failed:
        next_actions.append(
            f"janitor logs {build} \"{test_name}\""
            f" -- get log files"
        )
        next_actions.append(
            f"janitor crash {build} \"{test_name}\""
            f" -- search for crash signatures"
        )

    env = success_response(result, TOOL_NAME, "detail", next_actions)
    _output(env, pretty)


@main.command()
@click.argument("build_or_change")
@click.argument("test_name")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
@click.option("--build", "as_build", is_flag=True,
              help="Read the number as a Janitor build number")
@click.option("--change", "as_change", is_flag=True,
              help="Read the number as a Gerrit change number")
def logs(build_or_change: str, test_name: str, pretty: bool,
         as_build: bool = False, as_change: bool = False) -> None:
    """List available log files for a test.

    Shows all files in the test result directory (console logs,
    syslog, suite logs, crash logs, YAML results).

    \b
    Examples:
      janitor logs 61009 "sanity2@ldiskfs+DNE"
    """
    client = _make_client()
    build = _resolve_build(client, build_or_change, "logs", pretty,
                           as_build=as_build, as_change=as_change)

    test_dir = client.find_test_dir(build, test_name)
    if not test_dir:
        _error(
            ErrorCode.TEST_NOT_FOUND,
            f"No test directory found for '{test_name}' in build {build}",
            "logs", pretty,
        )

    files = client.list_test_files(build, test_dir)

    base_url = client._build_url(build, f"testresults/{test_dir}/")
    result = {
        "build": build,
        "test": test_name,
        "test_dir": test_dir,
        "base_url": base_url,
        "files": files,
    }

    next_actions = []
    crash_files = [
        f for f in files
        if any(
            p in f["name"]
            for p in ("console", "crash", "syslog", "kernel")
        )
    ]
    if crash_files:
        next_actions.append(
            f"janitor crash {build} \"{test_name}\""
            f" -- search crash logs for LBUG/LASSERT/panic"
        )
    suite_logs = [
        f for f in files if "suite_log" in f["name"]
    ]
    if suite_logs:
        next_actions.append(
            f"janitor fetch {build} \"{test_name}\" "
            f"\"{suite_logs[0]['name']}\""
            f" -- fetch suite log"
        )

    env = success_response(result, TOOL_NAME, "logs", next_actions)
    _output(env, pretty)


@main.command()
@click.argument("build_or_change")
@click.argument("test_name")
@click.argument("filename")
@click.option("--grep", "grep_pattern", type=str, default=None,
              help="Search for a pattern in the log")
@click.option("--tail", type=int, default=None,
              help="Show only last N lines")
@click.option("--max-bytes", type=int, default=500000,
              help="Max bytes to fetch (default: 500KB)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
@click.option("--build", "as_build", is_flag=True,
              help="Read the number as a Janitor build number")
@click.option("--change", "as_change", is_flag=True,
              help="Read the number as a Gerrit change number")
def fetch(
    build_or_change: str,
    test_name: str,
    filename: str,
    grep_pattern: str | None,
    tail: int | None,
    max_bytes: int,
    pretty: bool,
    as_build: bool = False,
    as_change: bool = False,
) -> None:
    """Fetch a specific log file from a test result.

    \b
    Examples:
      janitor fetch 61009 "sanity3@zfs" "oleg243-client-console.txt"
      janitor fetch 61009 "sanity3@zfs" "results.yml"
      janitor fetch 61009 "sanity2@ldiskfs+DNE" "console.txt" --grep "LBUG"
    """
    client = _make_client()
    build = _resolve_build(client, build_or_change, "fetch", pretty,
                           as_build=as_build, as_change=as_change)

    test_dir = client.find_test_dir(build, test_name)
    if not test_dir:
        _error(
            ErrorCode.TEST_NOT_FOUND,
            f"No test directory for '{test_name}' in build {build}",
            "fetch", pretty,
        )

    content = client.fetch_log(build, test_dir, filename, max_bytes)
    if content is None:
        _error(
            ErrorCode.LOG_NOT_FOUND,
            f"Could not fetch '{filename}' for '{test_name}'",
            "fetch", pretty,
        )

    lines = content.splitlines()
    if grep_pattern:
        pat = re.compile(grep_pattern, re.IGNORECASE)
        lines = [l for l in lines if pat.search(l)]
    if tail:
        lines = lines[-tail:]

    result: dict[str, Any] = {
        "build": build,
        "test": test_name,
        "file": filename,
        "line_count": len(lines),
        "truncated": len(content) >= max_bytes,
    }
    if grep_pattern:
        result["grep"] = grep_pattern
    result["content"] = "\n".join(lines)

    env = success_response(result, TOOL_NAME, "fetch")
    _output(env, pretty)


@main.command()
@click.argument("build_or_change")
@click.argument("test_name")
@click.option("--context", "-C", type=int, default=3,
              help="Lines of context around crash matches (default: 3)")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON")
@click.option("--build", "as_build", is_flag=True,
              help="Read the number as a Janitor build number")
@click.option("--change", "as_change", is_flag=True,
              help="Read the number as a Gerrit change number")
def crash(
    build_or_change: str,
    test_name: str,
    context: int,
    pretty: bool,
    as_build: bool = False,
    as_change: bool = False,
) -> None:
    """Search test logs for crash signatures.

    Fetches console logs, syslog, and kernel-crash logs, then
    searches for LBUG, LASSERT, kernel panic, and oops patterns.
    Shows matching lines with context.

    \b
    Examples:
      janitor crash 61009 "sanity3@zfs"
      janitor crash 64440 "sanity2@ldiskfs+DNE" -C 5
    """
    client = _make_client()
    build = _resolve_build(client, build_or_change, "crash", pretty,
                           as_build=as_build, as_change=as_change)

    test_dir = client.find_test_dir(build, test_name)
    if not test_dir:
        _error(
            ErrorCode.TEST_NOT_FOUND,
            f"No test directory for '{test_name}' in build {build}",
            "crash", pretty,
        )

    files = client.list_test_files(build, test_dir)

    # Prioritize crash-relevant files
    crash_files = [
        f for f in files
        if any(
            p in f["name"].lower()
            for p in ("console", "crash", "syslog", "kernel")
        )
    ]
    if not crash_files:
        crash_files = [
            f for f in files
            if f["name"].endswith((".txt", ".log"))
        ]

    all_matches: list[dict[str, Any]] = []
    files_searched = 0

    for cf in crash_files:
        content = client.fetch_log(
            build, test_dir, cf["name"], max_bytes=2_000_000
        )
        if content is None:
            continue
        files_searched += 1

        lines = content.splitlines()
        for i, line in enumerate(lines):
            if CRASH_RE.search(line):
                start = max(0, i - context)
                end = min(len(lines), i + context + 1)
                all_matches.append({
                    "file": cf["name"],
                    "line_number": i + 1,
                    "match": line.strip(),
                    "context": [
                        l.strip() for l in lines[start:end]
                    ],
                })

    # Deduplicate: if same match text appears multiple times,
    # keep first occurrence per file
    seen: set[tuple[str, str]] = set()
    unique_matches = []
    for m in all_matches:
        key = (m["file"], m["match"][:200])
        if key not in seen:
            seen.add(key)
            unique_matches.append(m)

    result: dict[str, Any] = {
        "build": build,
        "test": test_name,
        "test_dir": test_dir,
        "files_searched": files_searched,
        "crash_signatures_found": len(unique_matches),
        "matches": unique_matches[:50],  # cap at 50
    }

    if not unique_matches:
        result["assessment"] = (
            "No crash signatures found in logs. "
            "The failure may be a timeout, VM hang, or "
            "network issue rather than a Lustre crash."
        )

    next_actions = []
    for m in unique_matches[:2]:
        next_actions.append(
            f"janitor fetch {build} \"{test_name}\" "
            f"\"{m['file']}\" --tail 100"
            f" -- see full log context"
        )

    env = success_response(
        result, TOOL_NAME, "crash", next_actions or None
    )
    _output(env, pretty)


if __name__ == "__main__":
    main()
