"""Read-only, exact-revision Jenkins evidence for build-failure runs.

Patch Watcher deliberately fetches a specific Jenkins build URL already
published on the current Gerrit patchset.  It never searches for a vaguely
related failure and never gives Jenkins credentials to a worker.  The
normalized snapshot is stable across repeated reads of the same completed
build and is safe to bind into a one-use run confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


SNAPSHOT_SCHEMA = "patch-watcher-jenkins-failure-snapshot/v1"
JENKINS_HOST = "build.whamcloud.com"
MAX_CONSOLE_BYTES = 256 * 1024
MAX_CONSOLE_LINES = 240
MAX_FAILED_RUNS = 8
TERMINAL_RESULTS = frozenset({
    "SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT",
})
GERRIT_IDENTITY_PARAMETERS = frozenset({
    "GERRIT_CHANGE_NUMBER",
    "GERRIT_PATCHSET_NUMBER",
    "GERRIT_PATCHSET_REVISION",
    "GERRIT_REFSPEC",
    "GERRIT_PROJECT",
    "GERRIT_BRANCH",
})
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|AUTHORIZATION))"
        r"\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"(?i)\b(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
)


class JenkinsSnapshotError(RuntimeError):
    """A Jenkins build could not be bound safely to the requested revision."""


Transport = Callable[[Request, float], bytes]


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read(MAX_CONSOLE_BYTES + 1)


def _url_segments(value: Any) -> tuple[list[str], list[str]]:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError as exc:
        raise JenkinsSnapshotError(
            "Jenkins build URL is not an approved Whamcloud URL"
        ) from exc
    if (
        parsed.scheme != "https" or parsed.hostname != JENKINS_HOST
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or port not in {None, 443}
    ):
        raise JenkinsSnapshotError("Jenkins build URL is not an approved Whamcloud URL")
    if "//" in parsed.path:
        raise JenkinsSnapshotError("Jenkins build URL has an invalid path")
    encoded = [piece for piece in parsed.path.split("/") if piece]
    decoded = []
    for piece in encoded:
        value = unquote(piece)
        if (
            value in {"", ".", ".."}
            or "/" in value or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise JenkinsSnapshotError("Jenkins build URL has an invalid path")
        decoded.append(value)
    return encoded, decoded


def _encode_segments(values: list[str]) -> str:
    return "/".join(
        quote(value, safe="!$&'()*+,-.:;=@_~") for value in values
    )


def _canonical_build_url(value: Any) -> tuple[str, str, int, tuple[str, ...]]:
    _encoded, pieces = _url_segments(value)
    if len(pieces) < 3 or not pieces[-1].isdigit():
        raise JenkinsSnapshotError("Jenkins build URL does not identify one build")
    job_pieces = pieces[:-1]
    # A parent job is /job/name/<number>, with folders represented by repeated
    # /job/name pairs. Reject matrix-run and arbitrary paths here.
    if len(job_pieces) % 2 or any(
        job_pieces[index] != "job" for index in range(0, len(job_pieces), 2)
    ):
        raise JenkinsSnapshotError("Jenkins build URL does not identify one parent build")
    number = int(pieces[-1])
    if number <= 0:
        raise JenkinsSnapshotError("Jenkins build number is invalid")
    names = job_pieces[1::2]
    path = _encode_segments(pieces)
    return (
        f"https://{JENKINS_HOST}/{path}/",
        "/".join(names),
        number,
        tuple(job_pieces),
    )


def _canonical_run_url(
    value: Any, *, parent_job_pieces: tuple[str, ...], expected_number: int
) -> tuple[str, str]:
    _encoded, pieces = _url_segments(value)
    if not pieces or not pieces[-1].isdigit() or int(pieces[-1]) != expected_number:
        raise JenkinsSnapshotError("Jenkins matrix run has a mismatched build number")
    run_parent = pieces[:-1]
    prefix = list(parent_job_pieces)
    if run_parent[:len(prefix)] != prefix or len(run_parent) <= len(prefix):
        raise JenkinsSnapshotError("Jenkins matrix run is not a child of the exact build job")
    axes = run_parent[len(prefix):]
    if any(value == "job" for value in axes):
        raise JenkinsSnapshotError("Jenkins matrix run has an invalid configuration path")
    return (
        f"https://{JENKINS_HOST}/{_encode_segments(pieces)}/",
        "/".join(axes),
    )


def _parameters(build: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for action in build.get("actions") or ():
        if not isinstance(action, Mapping):
            continue
        for item in action.get("parameters") or ():
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if name in GERRIT_IDENTITY_PARAMETERS:
                value = str(item.get("value") or "")
                if name in values:
                    raise JenkinsSnapshotError(
                        "Jenkins returned duplicate Gerrit identity parameters"
                    )
                values[name] = value
    missing = sorted(name for name in GERRIT_IDENTITY_PARAMETERS if not values.get(name))
    if missing:
        raise JenkinsSnapshotError("Jenkins Gerrit identity parameters are incomplete")
    return values


def _console_excerpt(raw: bytes) -> dict[str, Any]:
    """Return a bounded, redacted excerpt without retaining raw console data."""
    byte_truncated = len(raw) > MAX_CONSOLE_BYTES
    bounded = raw[:MAX_CONSOLE_BYTES]
    text = bounded.decode("utf-8", errors="replace")
    # Jenkins output can contain terminal control sequences.  Keep printable
    # text only; it remains untrusted evidence when presented to the worker.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = _SECRET_PATTERNS[0].sub(lambda match: match.group(1) + "=[REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub("[REDACTED AUTHORIZATION]", text)
    text = _SECRET_PATTERNS[2].sub(lambda match: match.group(1) + "[REDACTED]@", text)
    all_lines = [line[:2000] for line in text.splitlines()]
    lines = all_lines[-MAX_CONSOLE_LINES:]
    digest = hashlib.sha256(json.dumps(
        lines, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "lines": lines,
        "excerpt_sha256": digest,
        "bytes_read": min(len(raw), MAX_CONSOLE_BYTES),
        "truncated": byte_truncated or len(all_lines) > MAX_CONSOLE_LINES,
    }


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise JenkinsSnapshotError(f"Jenkins {label} is malformed")
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise JenkinsSnapshotError(f"Jenkins {label} is malformed") from exc
    if result < 0:
        raise JenkinsSnapshotError(f"Jenkins {label} is malformed")
    return result


def _normalize_build(
    build: Mapping[str, Any], *, canonical_url: str,
    parent_job_pieces: tuple[str, ...], expected_number: int,
) -> dict[str, Any]:
    """Normalize only immutable/allowlisted semantics used by the snapshot."""
    params = _parameters(build)
    try:
        number = int(build.get("number"))
    except (TypeError, ValueError) as exc:
        raise JenkinsSnapshotError("Jenkins build identity is malformed") from exc
    if number != expected_number:
        raise JenkinsSnapshotError("Jenkins build number changed while capturing evidence")
    returned_url, _, _, returned_job_pieces = _canonical_build_url(build.get("url"))
    if returned_url != canonical_url or returned_job_pieces != parent_job_pieces:
        raise JenkinsSnapshotError("Jenkins returned a different build URL")
    if build.get("building") is not False:
        raise JenkinsSnapshotError("The exact Jenkins build is not completed")
    result = str(build.get("result") or "").upper()
    if result not in TERMINAL_RESULTS:
        raise JenkinsSnapshotError("The exact Jenkins build has an unknown terminal result")

    raw_runs = build.get("runs") or ()
    if isinstance(raw_runs, (str, bytes, Mapping)):
        raise JenkinsSnapshotError("Jenkins matrix run collection is malformed")
    try:
        raw_runs = tuple(raw_runs)
    except TypeError as exc:
        raise JenkinsSnapshotError("Jenkins matrix run collection is malformed") from exc
    runs = []
    seen_urls = set()
    for raw_run in raw_runs:
        if not isinstance(raw_run, Mapping):
            raise JenkinsSnapshotError("Jenkins matrix run is malformed")
        try:
            run_number = int(raw_run.get("number"))
        except (TypeError, ValueError) as exc:
            raise JenkinsSnapshotError("Jenkins matrix run number is malformed") from exc
        # Jenkins matrix APIs can include entries from neighboring builds.
        if run_number != expected_number:
            continue
        if raw_run.get("building") is not False:
            raise JenkinsSnapshotError("Jenkins matrix build is still running")
        run_result = str(raw_run.get("result") or "").upper()
        if run_result not in TERMINAL_RESULTS:
            raise JenkinsSnapshotError("Jenkins matrix run has an unknown terminal result")
        run_url, configuration = _canonical_run_url(
            raw_run.get("url"), parent_job_pieces=parent_job_pieces,
            expected_number=expected_number,
        )
        if run_url in seen_urls:
            raise JenkinsSnapshotError("Jenkins returned a duplicate matrix run")
        seen_urls.add(run_url)
        runs.append({
            "configuration": configuration,
            "display_name": str(raw_run.get("fullDisplayName") or "")[:500],
            "node": str(raw_run.get("builtOn") or "")[:200],
            "url": run_url,
            "result": run_result,
            "duration_ms": _nonnegative_int(raw_run.get("duration"), "run duration"),
        })
    runs.sort(key=lambda item: (item["configuration"], item["url"]))
    return {
        "number": number,
        "url": returned_url,
        "result": result,
        "timestamp_ms": _nonnegative_int(build.get("timestamp"), "timestamp"),
        "duration_ms": _nonnegative_int(build.get("duration"), "duration"),
        "parameters": params,
        "runs": runs,
    }


def _snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    stable = {key: value for key, value in snapshot.items() if key not in {
        "captured_at", "snapshot_sha256",
    }}
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class JenkinsSnapshotClient:
    """Fetch one completed failure and bind it to an exact Gerrit revision."""

    def __init__(self, *, transport: Transport | None = None, timeout: float = 20.0) -> None:
        self.transport = transport or _default_transport
        self.timeout = timeout

    def _read(self, url: str) -> bytes:
        try:
            value = self.transport(
                Request(url, headers={"Accept": "application/json", "User-Agent": "patch-watcher/1"}),
                self.timeout,
            )
        except JenkinsSnapshotError:
            raise
        except Exception as exc:
            raise JenkinsSnapshotError("Jenkins evidence could not be read") from exc
        if not isinstance(value, bytes):
            raise JenkinsSnapshotError("Jenkins returned an invalid response")
        return value

    def _json(self, url: str) -> Mapping[str, Any]:
        raw = self._read(url)
        if len(raw) > MAX_CONSOLE_BYTES:
            raise JenkinsSnapshotError("Jenkins build metadata exceeds the evidence bound")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JenkinsSnapshotError("Jenkins returned malformed build metadata") from exc
        if not isinstance(value, Mapping):
            raise JenkinsSnapshotError("Jenkins returned an invalid build object")
        return value

    def fetch_failure_snapshot(
        self,
        build_url: str,
        *,
        change_number: int,
        patchset: int,
        revision_sha: str,
        revision_ref: str,
        project: str,
        branch: str = "",
    ) -> dict[str, Any]:
        canonical, job_name, expected_number, parent_job_pieces = _canonical_build_url(
            build_url
        )
        if isinstance(change_number, bool) or isinstance(patchset, bool):
            raise JenkinsSnapshotError("The Gerrit build identity is malformed")
        try:
            change_number = int(change_number)
            patchset = int(patchset)
        except (TypeError, ValueError) as exc:
            raise JenkinsSnapshotError("The Gerrit build identity is malformed") from exc
        if change_number <= 0 or patchset <= 0:
            raise JenkinsSnapshotError("The Gerrit build identity is malformed")
        revision_sha = str(revision_sha or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", revision_sha):
            raise JenkinsSnapshotError("A full exact Gerrit revision is required")
        revision_ref = str(revision_ref or "").strip()
        project = str(project or "").strip()
        branch = str(branch or "").strip()
        expected_ref = f"refs/changes/{change_number % 100:02d}/{change_number}/{patchset}"
        if revision_ref != expected_ref or not project:
            raise JenkinsSnapshotError("The Gerrit build identity is malformed")
        tree = (
            "number,url,result,timestamp,duration,building,"
            "actions[parameters[name,value],lastBuiltRevision[SHA1]],"
            "runs[number,url,result,building,duration,fullDisplayName,builtOn],"
            "changeSet[items[commitId,msg,author[fullName]]]"
        )
        api_url = canonical + "api/json?" + urlencode({"tree": tree})
        build = self._json(api_url)
        normalized = _normalize_build(
            build, canonical_url=canonical, parent_job_pieces=parent_job_pieces,
            expected_number=expected_number,
        )
        params = normalized["parameters"]
        try:
            observed_change = int(params.get("GERRIT_CHANGE_NUMBER") or 0)
            observed_patchset = int(params.get("GERRIT_PATCHSET_NUMBER") or 0)
        except (TypeError, ValueError) as exc:
            raise JenkinsSnapshotError("Jenkins build identity is malformed") from exc
        observed_revision = params.get("GERRIT_PATCHSET_REVISION", "").lower()
        observed_ref = params.get("GERRIT_REFSPEC", "")
        observed_project = params.get("GERRIT_PROJECT", "")
        observed_branch = params.get("GERRIT_BRANCH", "")
        if normalized["result"] != "FAILURE":
            raise JenkinsSnapshotError("The exact Jenkins build is not a completed failure")
        if (
            observed_change != change_number or observed_patchset != patchset
            or observed_revision != revision_sha or observed_ref != revision_ref
            or observed_project != project
            or (branch and observed_branch != branch)
        ):
            raise JenkinsSnapshotError("Jenkins failure is not for the exact current Gerrit revision")
        parent_console = _console_excerpt(self._read(canonical + "consoleText"))
        failed_runs = []
        for run in normalized["runs"]:
            if run["result"] != "FAILURE":
                continue
            if len(failed_runs) >= MAX_FAILED_RUNS:
                raise JenkinsSnapshotError("Jenkins failure has too many failed configurations")
            console = _console_excerpt(self._read(run["url"] + "consoleText"))
            failed_runs.append({
                **run,
                "console_tail": console["lines"],
                "console": console,
            })
        failed_runs.sort(key=lambda item: (item["configuration"], item["url"]))
        # Re-read the terminal build after collecting logs.  Relevant identity,
        # status, and matrix-run membership must remain stable throughout the
        # capture; a changing build is not immutable evidence.
        confirmed = _normalize_build(
            self._json(api_url), canonical_url=canonical,
            parent_job_pieces=parent_job_pieces, expected_number=expected_number,
        )
        if normalized != confirmed:
            raise JenkinsSnapshotError("Jenkins build changed while capturing evidence")
        timestamp = normalized["timestamp_ms"]
        duration = normalized["duration_ms"]
        started_at = ""
        completed_at = ""
        if timestamp > 0:
            started_at = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
            completed_at = datetime.fromtimestamp(
                (timestamp + duration) / 1000, tz=timezone.utc
            ).isoformat()
        snapshot: dict[str, Any] = {
            "schema": SNAPSHOT_SCHEMA,
            "complete": True,
            "change": {
                "change_number": change_number, "patchset": patchset,
                "revision_sha": revision_sha, "revision_ref": revision_ref,
                "project": project, "branch": branch or observed_branch,
            },
            "build": {
                "job_name": job_name, "build_number": expected_number,
                "url": canonical, "result": "FAILURE", "started_at": started_at,
                "completed_at": completed_at, "duration_ms": duration,
            },
            "matrix_runs": normalized["runs"],
            "parent_console_tail": parent_console["lines"],
            "parent_console": parent_console,
            "failed_runs": failed_runs,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        snapshot["snapshot_sha256"] = _snapshot_digest(snapshot)
        return snapshot


__all__ = [
    "JenkinsSnapshotClient", "JenkinsSnapshotError", "SNAPSHOT_SCHEMA",
]
