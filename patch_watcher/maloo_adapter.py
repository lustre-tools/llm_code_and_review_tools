"""Typed Patch Watcher adapter for the JSON Maloo CLI.

The adapter deliberately uses shell-free argv and the ``--envelope`` contract.
Read operations are normalized into stable records.  ``request_retest`` is an
at-most-once mutation: this module never retries it, and uncertain transport or
response failures are reported as an ambiguous outcome for remote
reconciliation.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple


UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
JIRA_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
RETEST_OPTIONS = {"single", "all", "livedebug"}
PENDING_STATES = {"pending", "queued", "running", "requested", "in_progress"}
REQUESTED_STATES = PENDING_STATES | {"accepted", "submitted", "complete", "completed"}


class MalooErrorCode:
    INVALID_INPUT = "invalid_input"
    AUTHENTICATION = "authentication"
    NOT_FOUND = "not_found"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    CLI_UNAVAILABLE = "cli_unavailable"
    CLI_FAILED = "cli_failed"
    INVALID_RESPONSE = "invalid_response"
    AMBIGUOUS_MUTATION = "ambiguous_mutation"


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(MALOO_(?:PASS|USER)|password|token|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
    re.compile(r"(?i)\b(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]+"),
)


def redact_text(value: Any) -> str:
    """Return bounded diagnostic text with common credentials removed."""
    text = str(value or "")[:2000]
    text = _SECRET_PATTERNS[0].sub(lambda m: m.group(1) + "=[REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub(lambda m: m.group(1) + "[REDACTED]@", text)
    text = _SECRET_PATTERNS[2].sub("[REDACTED AUTHORIZATION]", text)
    return text


class MalooAdapterError(Exception):
    """A typed, redacted adapter failure suitable for durable events."""

    def __init__(
        self,
        code: str,
        operation: str,
        message: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
        exit_code: Optional[int] = None,
    ) -> None:
        self.code = code
        self.operation = operation
        self.message = redact_text(message)
        self.retryable = bool(retryable)
        self.ambiguous = bool(ambiguous)
        self.exit_code = exit_code
        super().__init__(self.message)

    def to_dict(self) -> dict:
        result = {
            "code": self.code,
            "operation": self.operation,
            "message": self.message,
            "retryable": self.retryable,
            "ambiguous": self.ambiguous,
        }
        if self.exit_code is not None:
            result["exit_code"] = self.exit_code
        return result


@dataclass(frozen=True)
class MalooSuite:
    suite_id: str
    name: str
    status: str
    passed: Optional[int] = None
    failed: Optional[int] = None
    skipped: Optional[int] = None
    total: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MalooSession:
    session_id: str
    test_group: str
    test_name: str
    test_host: str
    submission: str
    enforcing: Optional[bool]
    passed: Optional[int]
    failed: Optional[int]
    aborted: Optional[int]
    total: Optional[int]
    suites: Tuple[MalooSuite, ...]
    retest_pending: Optional[bool] = None
    retest_status: str = "unknown"
    retest_ticket: str = ""

    def to_dict(self) -> dict:
        result = asdict(self)
        result["suites"] = [suite.to_dict() for suite in self.suites]
        return result


@dataclass(frozen=True)
class MalooReviewSessions:
    change_number: int
    patchset: int
    sessions: Tuple[MalooSession, ...]

    @property
    def enforced_failed(self) -> Tuple[MalooSession, ...]:
        return tuple(
            session for session in self.sessions
            if session.enforcing is True and (session.failed or 0) > 0
        )

    def to_dict(self) -> dict:
        return {
            "change_number": self.change_number,
            "patchset": self.patchset,
            "sessions": [session.to_dict() for session in self.sessions],
            "enforced_failed": [session.to_dict() for session in self.enforced_failed],
        }


@dataclass(frozen=True)
class MalooSubtestFailure:
    name: str
    status: str
    error: str
    duration: Any = None
    return_code: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MalooFailedSuite:
    suite_id: str
    suite: str
    status: str
    failed_count: Optional[int]
    total_count: Optional[int]
    failed_subtests: Tuple[MalooSubtestFailure, ...]

    def to_dict(self) -> dict:
        result = asdict(self)
        result["failed_subtests"] = [item.to_dict() for item in self.failed_subtests]
        return result


@dataclass(frozen=True)
class MalooFailures:
    session_id: str
    test_group: str
    test_name: str
    failed_suites: Tuple[MalooFailedSuite, ...]

    @property
    def suite_ids(self) -> Tuple[str, ...]:
        return tuple(suite.suite_id for suite in self.failed_suites)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "test_group": self.test_group,
            "test_name": self.test_name,
            "suite_ids": list(self.suite_ids),
            "failed_suites": [suite.to_dict() for suite in self.failed_suites],
        }


@dataclass(frozen=True)
class MalooBugLink:
    ticket: str
    state: str
    buggable_id: str = ""

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"

    @property
    def pending(self) -> bool:
        return self.state == "pending"

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "state": self.state,
            "buggable_id": self.buggable_id,
            "accepted": self.accepted,
            "pending": self.pending,
        }


@dataclass(frozen=True)
class MalooBugLinks:
    buggable_id: str
    links: Tuple[MalooBugLink, ...]

    @property
    def accepted(self) -> Tuple[MalooBugLink, ...]:
        return tuple(link for link in self.links if link.accepted)

    @property
    def pending(self) -> Tuple[MalooBugLink, ...]:
        return tuple(link for link in self.links if link.pending)

    def to_dict(self) -> dict:
        return {
            "buggable_id": self.buggable_id,
            "links": [link.to_dict() for link in self.links],
            "accepted": [link.to_dict() for link in self.accepted],
            "pending": [link.to_dict() for link in self.pending],
        }


@dataclass(frozen=True)
class MalooSuiteBugEvidence:
    suite_id: str
    suite: str
    bugs: MalooBugLinks

    def to_dict(self) -> dict:
        return {
            "suite_id": self.suite_id,
            "suite": self.suite,
            "bugs": self.bugs.to_dict(),
        }


@dataclass(frozen=True)
class MalooEnforcedSessionFailure:
    """One retest decision unit: exactly one session/test-group pair."""

    session: MalooSession
    failures: MalooFailures
    suite_bugs: Tuple[MalooSuiteBugEvidence, ...]

    @property
    def decision_key(self) -> Tuple[str, str]:
        return (self.session.session_id, self.session.test_group)

    def to_dict(self) -> dict:
        return {
            "decision_key": list(self.decision_key),
            "session": self.session.to_dict(),
            "failures": self.failures.to_dict(),
            "suite_bugs": [item.to_dict() for item in self.suite_bugs],
        }


@dataclass(frozen=True)
class MalooQueueEntry:
    queue_id: str
    revision_sha: str
    test_group: str
    status: str
    job: str = ""
    build_number: Optional[int] = None
    patchset: Optional[int] = None

    @property
    def pending(self) -> bool:
        return self.status.casefold().replace(" ", "_") in PENDING_STATES

    def to_dict(self) -> dict:
        result = asdict(self)
        result["pending"] = self.pending
        return result


@dataclass(frozen=True)
class MalooQueueEvidence:
    requested_revision: str
    entries: Tuple[MalooQueueEntry, ...]

    @property
    def pending(self) -> Tuple[MalooQueueEntry, ...]:
        return tuple(entry for entry in self.entries if entry.pending)

    def to_dict(self) -> dict:
        return {
            "requested_revision": self.requested_revision,
            "entries": [entry.to_dict() for entry in self.entries],
            "pending": [entry.to_dict() for entry in self.pending],
        }


@dataclass(frozen=True)
class MalooRetestResult:
    session_id: str
    jira_ticket: str
    option: str
    requested: bool
    response: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MalooLinkBugResult:
    buggable_id: str
    jira_ticket: str
    buggable_class: str
    state: str
    linked: bool
    response: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MalooRetestReconciliation:
    session_id: str
    already_requested: bool
    pending: Optional[bool]
    ticket: str
    sources: Tuple[str, ...]

    @property
    def outcome(self) -> str:
        if self.pending is True:
            return "pending"
        if self.already_requested:
            return "already_requested"
        return "not_observed"

    @property
    def automatic_retry_allowed(self) -> bool:
        """Remote absence never proves that an ambiguous mutation was absent."""
        return False

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "already_requested": self.already_requested,
            "pending": self.pending,
            "ticket": self.ticket,
            "sources": list(self.sources),
            "outcome": self.outcome,
            "automatic_retry_allowed": self.automatic_retry_allowed,
        }


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


Runner = Callable[[Sequence[str]], Any]


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        shell=False,
    )


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "enforcing", "enforced"}:
            return True
        if normalized in {"false", "no", "0", "optional"}:
            return False
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _mapping(value: Any, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MalooAdapterError(
            MalooErrorCode.INVALID_RESPONSE,
            operation,
            "Maloo returned an unexpected data shape",
        )
    return value


def _sequence(value: Any, operation: str) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        raise MalooAdapterError(
            MalooErrorCode.INVALID_RESPONSE,
            operation,
            "Maloo returned an unexpected list shape",
        )
    try:
        return tuple(value)
    except TypeError as exc:
        raise MalooAdapterError(
            MalooErrorCode.INVALID_RESPONSE,
            operation,
            "Maloo returned an unexpected list shape",
        ) from exc


def _session_id(value: str) -> str:
    match = UUID_RE.search(_text(value))
    if not match:
        raise MalooAdapterError(
            MalooErrorCode.INVALID_INPUT,
            "input",
            "A valid Maloo session UUID is required",
        )
    return match.group(0).lower()


def _identifier(value: str, label: str) -> str:
    result = _text(value).strip()
    if not result or result.startswith("-") or len(result) > 200:
        raise MalooAdapterError(
            MalooErrorCode.INVALID_INPUT, "input", f"A valid {label} is required"
        )
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        parsed = 0
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
    if parsed <= 0:
        raise MalooAdapterError(
            MalooErrorCode.INVALID_INPUT, "input", f"A valid {label} is required"
        )
    return parsed


def _revision_sha(value: str) -> str:
    revision = _text(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise MalooAdapterError(
            MalooErrorCode.INVALID_INPUT,
            "input",
            "A full 40-character Gerrit revision SHA is required",
        )
    return revision


def _normalize_retest_fields(data: Mapping[str, Any]) -> Tuple[Optional[bool], str, str]:
    raw_pending = data.get("retest_pending", data.get("pending_retest"))
    pending = _optional_bool(raw_pending)
    status = _text(data.get("retest_status", data.get("retest_state"))).strip().casefold()
    requested = _optional_bool(data.get("retest_requested"))
    if status in PENDING_STATES:
        pending = True
    elif pending is None and requested is False:
        pending = False
    ticket = _text(data.get("retest_ticket", data.get("bug_id", ""))).upper()
    return pending, status or "unknown", ticket


def normalize_session(data: Mapping[str, Any]) -> MalooSession:
    operation = "session"
    data = _mapping(data, operation)
    sid = _session_id(_text(data.get("session_id", data.get("id", ""))))
    suites = []
    for raw_suite in _sequence(data.get("suites", ()), operation):
        suite = _mapping(raw_suite, operation)
        suites.append(MalooSuite(
            suite_id=_identifier(_text(suite.get("id", suite.get("suite_id", ""))), "suite ID"),
            name=_text(suite.get("name", suite.get("suite", "unknown"))) or "unknown",
            status=_text(suite.get("status", "unknown")).upper(),
            passed=_optional_int(suite.get("passed")),
            failed=_optional_int(suite.get("failed")),
            skipped=_optional_int(suite.get("skipped")),
            total=_optional_int(suite.get("total")),
        ))
    pending, retest_status, ticket = _normalize_retest_fields(data)
    return MalooSession(
        session_id=sid,
        test_group=_text(data.get("test_group")),
        test_name=_text(data.get("test_name")),
        test_host=_text(data.get("test_host")),
        submission=_text(data.get("submission")),
        enforcing=_optional_bool(data.get("enforcing")),
        passed=_optional_int(data.get("passed")),
        failed=_optional_int(data.get("failed")),
        aborted=_optional_int(data.get("aborted")),
        total=_optional_int(data.get("total")),
        suites=tuple(suites),
        retest_pending=pending,
        retest_status=retest_status,
        retest_ticket=ticket,
    )


def normalize_review_sessions(data: Mapping[str, Any]) -> MalooReviewSessions:
    operation = "review"
    data = _mapping(data, operation)
    change = _positive_int(data.get("review_id"), "Gerrit change number")
    patchset = _positive_int(data.get("patch"), "patchset number")
    sessions = []
    for raw_session in _sequence(data.get("sessions", ()), operation):
        session = dict(_mapping(raw_session, operation))
        # The review command already emits the same stable summary fields as
        # session, except it has no suite list.
        session.setdefault("suites", [])
        sessions.append(normalize_session(session))
    return MalooReviewSessions(change, patchset, tuple(sessions))


def normalize_failures(data: Mapping[str, Any]) -> MalooFailures:
    operation = "failures"
    data = _mapping(data, operation)
    sid = _session_id(_text(data.get("session_id", "")))
    failed_suites = []
    for raw_suite in _sequence(data.get("failed_suites", ()), operation):
        suite = _mapping(raw_suite, operation)
        subtests = []
        for raw_subtest in _sequence(suite.get("failed_subtests", ()), operation):
            subtest = _mapping(raw_subtest, operation)
            subtests.append(MalooSubtestFailure(
                name=_text(subtest.get("name")) or "unknown",
                status=_text(subtest.get("status", "unknown")).upper(),
                error=redact_text(subtest.get("error", "")),
                duration=subtest.get("duration"),
                return_code=subtest.get("return_code"),
            ))
        failed_suites.append(MalooFailedSuite(
            suite_id=_identifier(_text(suite.get("suite_id", "")), "suite ID"),
            suite=_text(suite.get("suite")) or "unknown",
            status=_text(suite.get("status", "unknown")).upper(),
            failed_count=_optional_int(suite.get("failed_count")),
            total_count=_optional_int(suite.get("total_count")),
            failed_subtests=tuple(subtests),
        ))
    return MalooFailures(
        session_id=sid,
        test_group=_text(data.get("test_group")),
        test_name=_text(data.get("test_name")),
        failed_suites=tuple(failed_suites),
    )


def normalize_bug_links(data: Mapping[str, Any]) -> MalooBugLinks:
    operation = "bugs"
    data = _mapping(data, operation)
    buggable_id = _identifier(_text(data.get("buggable_id", "")), "buggable ID")
    links = []
    for raw_link in _sequence(data.get("bug_links", ()), operation):
        link = _mapping(raw_link, operation)
        ticket = _text(
            link.get("ticket", link.get("bug_id", link.get("bug_upstream_id", "")))
        ).upper()
        if not ticket:
            continue
        # Current Maloo omits state for the default accepted link.  Preserve
        # explicit pending, but treat an absent state as accepted.
        raw_state = link.get("state", link.get("status"))
        state = "accepted" if raw_state in (None, "") else _text(raw_state).casefold()
        if state not in {"accepted", "pending"}:
            state = "unknown"
        links.append(MalooBugLink(
            ticket=ticket,
            state=state,
            buggable_id=_text(link.get("buggable_id", buggable_id)),
        ))
    return MalooBugLinks(buggable_id=buggable_id, links=tuple(links))


def normalize_queue(data: Mapping[str, Any], revision_sha: str) -> MalooQueueEvidence:
    operation = "queue"
    data = _mapping(data, operation)
    revision = _revision_sha(revision_sha)
    filters = data.get("filters")
    if not isinstance(filters, Mapping):
        raise MalooAdapterError(
            MalooErrorCode.INVALID_RESPONSE, operation,
            "Maloo queue response did not identify its revision filter",
        )
    observed_revision = _text(
        filters.get("resolved_revision", filters.get("review_id", ""))
    ).lower()
    if observed_revision != revision:
        raise MalooAdapterError(
            MalooErrorCode.INVALID_RESPONSE, operation,
            "Maloo queue response is for a different revision",
        )
    entries = []
    for raw_entry in _sequence(data.get("queue_entries", ()), operation):
        entry = _mapping(raw_entry, operation)
        entry_revision = _text(entry.get("review_id", entry.get("revision_sha", ""))).lower()
        entries.append(MalooQueueEntry(
            queue_id=_text(entry.get("id")),
            revision_sha=entry_revision,
            test_group=_text(entry.get("test_group")),
            status=_text(entry.get("status", "unknown")),
            job=_text(entry.get("job")),
            build_number=_optional_int(entry.get("buildno")),
            patchset=_optional_int(entry.get("review_patch")),
        ))
    return MalooQueueEvidence(revision, tuple(entries))


def _walk_retest_evidence(value: Any, session_id: str, path: str = "evidence") -> list:
    """Find normalized retest evidence without treating arbitrary text as proof."""
    matches = []
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if isinstance(value, Mapping):
        own_session = _text(value.get("session_id", value.get("id", "")))
        url = _text(value.get("url", value.get("session_url", "")))
        own_match = UUID_RE.search(own_session + " " + url)
        applies = own_match is None or own_match.group(0).lower() == session_id
        pending, status, ticket = _normalize_retest_fields(value)
        requested = _optional_bool(value.get("retest_requested"))
        result_requested = _optional_bool(value.get("requested"))
        if applies and (
            pending is not None or requested is True or result_requested is True
            or status in REQUESTED_STATES
        ):
            matches.append({
                "pending": pending,
                "requested": requested is True or result_requested is True or status in REQUESTED_STATES,
                "ticket": ticket or _text(value.get("jira_ticket", "")).upper(),
                "source": path,
            })
        for key, child in value.items():
            if isinstance(child, (Mapping, list, tuple)):
                matches.extend(_walk_retest_evidence(child, session_id, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            matches.extend(_walk_retest_evidence(child, session_id, f"{path}[{index}]"))
    return matches


def reconcile_retest_evidence(
    session_ref: str, evidence: Any, *, jira_ticket: str = ""
) -> MalooRetestReconciliation:
    """Determine existing/pending retest state from already-normalized evidence."""
    sid = _session_id(session_ref)
    wanted_ticket = _text(jira_ticket).upper()
    matches = _walk_retest_evidence(evidence, sid)
    if wanted_ticket:
        ticket_matches = [item for item in matches if not item["ticket"] or item["ticket"] == wanted_ticket]
        matches = ticket_matches
    pending_values = [item["pending"] for item in matches if item["pending"] is not None]
    already = any(item["requested"] or item["pending"] is True for item in matches)
    pending = True if True in pending_values else (False if pending_values else None)
    ticket = next((item["ticket"] for item in matches if item["ticket"]), wanted_ticket)
    sources = tuple(dict.fromkeys(item["source"] for item in matches))
    return MalooRetestReconciliation(sid, already, pending, ticket, sources)


class MalooAdapter:
    """Shell-free Maloo CLI adapter with injectable command execution."""

    def __init__(
        self,
        *,
        binary: str = "maloo",
        runner: Optional[Runner] = None,
    ) -> None:
        if not binary or "\x00" in binary:
            raise ValueError("binary must be a non-empty executable path")
        self.binary = binary
        self.runner = runner or _default_runner

    def _invoke(self, operation: str, args: Sequence[str], *, mutation: bool = False) -> Mapping[str, Any]:
        argv = [self.binary, "--envelope", operation] + [str(arg) for arg in args]
        try:
            completed = self.runner(tuple(argv))
        except subprocess.TimeoutExpired as exc:
            if mutation:
                raise MalooAdapterError(
                    MalooErrorCode.AMBIGUOUS_MUTATION, operation,
                    f"Maloo {operation} timed out; the remote outcome is unknown and must be reconciled",
                    ambiguous=True,
                ) from exc
            raise MalooAdapterError(
                MalooErrorCode.TIMEOUT, operation, "Maloo read timed out", retryable=True
            ) from exc
        except FileNotFoundError as exc:
            raise MalooAdapterError(
                MalooErrorCode.CLI_UNAVAILABLE, operation, "The Maloo CLI is unavailable"
            ) from exc
        except OSError as exc:
            if mutation:
                raise MalooAdapterError(
                    MalooErrorCode.AMBIGUOUS_MUTATION, operation,
                    f"Maloo {operation} transport failed; the remote outcome is unknown and must be reconciled",
                    ambiguous=True,
                ) from exc
            raise MalooAdapterError(
                MalooErrorCode.CONNECTION, operation,
                "Could not execute the Maloo read command", retryable=True,
            ) from exc

        return_code = getattr(completed, "returncode", None)
        stdout = getattr(completed, "stdout", "")
        stderr = getattr(completed, "stderr", "")
        if not isinstance(return_code, int):
            raise MalooAdapterError(
                MalooErrorCode.AMBIGUOUS_MUTATION if mutation else MalooErrorCode.INVALID_RESPONSE,
                operation,
                "The Maloo command runner returned an invalid result",
                ambiguous=mutation,
            )
        stderr_text = redact_text(stderr)
        credential_error = stderr_text.casefold()
        if return_code != 0 and (
            "maloo credentials required" in credential_error
            or (
                "maloo_user" in credential_error
                and "maloo_pass" in credential_error
            )
        ):
            # The CLI failed before contacting Maloo, so this is definitive
            # even for a mutation and must not be recorded as ambiguous.
            raise MalooAdapterError(
                MalooErrorCode.AUTHENTICATION,
                operation,
                "Maloo credentials are not configured for Patch Watcher",
                exit_code=return_code,
            )
        try:
            envelope = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            if mutation:
                raise MalooAdapterError(
                    MalooErrorCode.AMBIGUOUS_MUTATION, operation,
                    f"Maloo {operation} returned an unreadable response; the remote outcome is unknown and must be reconciled",
                    ambiguous=True, exit_code=return_code,
                ) from exc
            raise MalooAdapterError(
                MalooErrorCode.INVALID_RESPONSE, operation,
                "Maloo returned invalid JSON", exit_code=return_code,
            ) from exc
        if not isinstance(envelope, Mapping):
            raise MalooAdapterError(
                MalooErrorCode.AMBIGUOUS_MUTATION if mutation else MalooErrorCode.INVALID_RESPONSE,
                operation,
                "Maloo returned an invalid envelope",
                ambiguous=mutation,
                exit_code=return_code,
            )

        if return_code != 0 or envelope.get("ok") is not True:
            error = envelope.get("error", {})
            error = error if isinstance(error, Mapping) else {}
            remote_code = _text(error.get("code")).upper()
            remote_message = redact_text(error.get("message", stderr or "Maloo command failed"))
            classifications = {
                "AUTH_FAILED": (MalooErrorCode.AUTHENTICATION, False),
                "AUTH_MISSING": (MalooErrorCode.AUTHENTICATION, False),
                "NOT_FOUND": (MalooErrorCode.NOT_FOUND, False),
                "INVALID_INPUT": (MalooErrorCode.INVALID_INPUT, False),
                "MISSING_REQUIRED_FIELD": (MalooErrorCode.INVALID_INPUT, False),
                "CONNECTION_ERROR": (MalooErrorCode.CONNECTION, True),
                "TIMEOUT": (MalooErrorCode.TIMEOUT, True),
            }
            classified = classifications.get(remote_code)
            if mutation and (
                classified is None
                or remote_code in {"CONNECTION_ERROR", "TIMEOUT"}
            ):
                raise MalooAdapterError(
                    MalooErrorCode.AMBIGUOUS_MUTATION, operation,
                    f"Maloo {operation} failed without a definitive rejection; reconcile remote state before another request",
                    ambiguous=True, exit_code=return_code,
                )
            code, retryable = classified or (MalooErrorCode.CLI_FAILED, False)
            raise MalooAdapterError(
                code, operation, remote_message, retryable=retryable,
                exit_code=return_code,
            )

        meta = envelope.get("meta")
        if isinstance(meta, Mapping):
            if meta.get("tool") not in (None, "maloo") or meta.get("command") not in (None, operation):
                raise MalooAdapterError(
                    MalooErrorCode.AMBIGUOUS_MUTATION if mutation else MalooErrorCode.INVALID_RESPONSE,
                    operation,
                    "Maloo response metadata does not match the requested operation",
                    ambiguous=mutation,
                )
        data = envelope.get("data")
        if not isinstance(data, Mapping):
            raise MalooAdapterError(
                MalooErrorCode.AMBIGUOUS_MUTATION if mutation else MalooErrorCode.INVALID_RESPONSE,
                operation,
                "Maloo returned an unexpected data shape",
                ambiguous=mutation,
                exit_code=return_code,
            )
        return data

    def get_session(self, session_ref: str) -> MalooSession:
        sid = _session_id(session_ref)
        return normalize_session(self._invoke("session", [sid]))

    def get_review_sessions(
        self, change_number: int, patchset: int
    ) -> MalooReviewSessions:
        change = _positive_int(change_number, "Gerrit change number")
        patch = _positive_int(patchset, "patchset number")
        return normalize_review_sessions(
            self._invoke("review", [str(change), "--patch", str(patch)])
        )

    def get_failures(self, session_ref: str) -> MalooFailures:
        sid = _session_id(session_ref)
        return normalize_failures(self._invoke("failures", [sid]))

    def get_bug_links(self, buggable_id: str, *, related: bool = True) -> MalooBugLinks:
        target = _identifier(buggable_id, "buggable ID")
        args = [target] + (["--related"] if related else [])
        return normalize_bug_links(self._invoke("bugs", args))

    def get_queue(self, revision_sha: str) -> MalooQueueEvidence:
        revision = _revision_sha(revision_sha)
        return normalize_queue(
            self._invoke("queue", ["--review", revision]), revision
        )

    def get_enforced_failures(
        self, change_number: int, patchset: int
    ) -> Tuple[MalooEnforcedSessionFailure, ...]:
        """Read all evidence, grouped once per enforced session/test group.

        This method never mutates Maloo.  It calls ``failures`` once per
        unique enforcing failed session and ``bugs --related`` once per failed
        suite, producing the unit on which a later retest decision is made.
        """
        review = self.get_review_sessions(change_number, patchset)
        grouped = []
        seen = set()
        for session in review.enforced_failed:
            key = (session.session_id, session.test_group)
            if key in seen:
                continue
            seen.add(key)
            failures = self.get_failures(session.session_id)
            suite_bugs = tuple(
                MalooSuiteBugEvidence(
                    suite_id=suite.suite_id,
                    suite=suite.suite,
                    bugs=self.get_bug_links(suite.suite_id, related=True),
                )
                for suite in failures.failed_suites
            )
            grouped.append(MalooEnforcedSessionFailure(
                session=session,
                failures=failures,
                suite_bugs=suite_bugs,
            ))
        return tuple(grouped)

    def request_retest(
        self, session_ref: str, jira_ticket: str, *, option: str = "single"
    ) -> MalooRetestResult:
        """Request one retest without any automatic retry."""
        sid = _session_id(session_ref)
        ticket = _text(jira_ticket).upper()
        if not JIRA_RE.fullmatch(ticket):
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT, "retest", "A valid JIRA ticket is required"
            )
        if option not in RETEST_OPTIONS:
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT, "retest", "Invalid retest option"
            )
        data = self._invoke("retest", [sid, ticket, "--option", option], mutation=True)
        returned_sid = _session_id(_text(data.get("session_id", sid)))
        returned_ticket = _text(data.get("bug_id", ticket)).upper()
        returned_option = _text(data.get("retest_option", option))
        if returned_sid != sid or returned_ticket != ticket or returned_option != option:
            raise MalooAdapterError(
                MalooErrorCode.AMBIGUOUS_MUTATION, "retest",
                "Maloo acknowledged a different retest request; reconcile remote state",
                ambiguous=True,
            )
        return MalooRetestResult(
            session_id=sid,
            jira_ticket=ticket,
            option=option,
            requested=bool(data.get("success", True)),
            response=redact_text(data.get("response", "")),
        )

    def link_bug(
        self,
        buggable_id: str,
        jira_ticket: str,
        *,
        buggable_class: str = "TestSet",
        state: str = "accepted",
    ) -> MalooLinkBugResult:
        """Associate one existing Jira key without an automatic retry."""
        target = _identifier(buggable_id, "buggable ID")
        ticket = _text(jira_ticket).upper()
        if not JIRA_RE.fullmatch(ticket):
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT,
                "link-bug",
                "A valid existing JIRA ticket is required",
            )
        if buggable_class not in {"TestSet", "SubTest"}:
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT, "link-bug", "Invalid buggable type"
            )
        if state not in {"accepted", "pending"}:
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT, "link-bug", "Invalid bug-link state"
            )
        data = self._invoke(
            "link-bug",
            [target, ticket, "--type", buggable_class, "--state", state],
            mutation=True,
        )
        returned_target = _identifier(
            _text(data.get("buggable_id", target)), "buggable ID"
        )
        returned_ticket = _text(data.get("bug", data.get("ticket", ticket))).upper()
        returned_class = _text(data.get("buggable_class", buggable_class))
        returned_state = _text(data.get("state", state)).casefold()
        if (
            returned_target != target
            or returned_ticket != ticket
            or returned_class != buggable_class
            or returned_state != state
        ):
            raise MalooAdapterError(
                MalooErrorCode.AMBIGUOUS_MUTATION,
                "link-bug",
                "Maloo acknowledged a different bug association; reconcile remote state",
                ambiguous=True,
            )
        return MalooLinkBugResult(
            buggable_id=target,
            jira_ticket=ticket,
            buggable_class=buggable_class,
            state=state,
            linked=bool(data.get("success", True)),
            response=redact_text(data.get("response", "")),
        )

    def reconcile_retest(
        self,
        session_ref: str,
        *,
        evidence: Any = None,
        jira_ticket: str = "",
    ) -> MalooRetestReconciliation:
        """Reconcile from evidence, fetching only read-only session data if absent."""
        sid = _session_id(session_ref)
        observed = self.get_session(sid) if evidence is None else evidence
        return reconcile_retest_evidence(sid, observed, jira_ticket=jira_ticket)

    def reconcile_remote_retest(
        self,
        *,
        change_number: int,
        patchset: int,
        revision_sha: str,
        session_ref: str,
        test_group: str,
        original_submission: str = "",
        jira_ticket: str = "",
        queue_evidence: Optional[MalooQueueEvidence] = None,
        review_evidence: Optional[MalooReviewSessions] = None,
    ) -> MalooRetestReconciliation:
        """Reconcile an uncertain request using exact remote read evidence.

        The queue lookup is pinned to the full revision SHA and the review
        lookup to the exact change/patchset.  A matching queued/running group
        proves a pending retest.  A different, newer session in the same
        test-group proves the request was already fulfilled.  Absence of both
        remains ``not_observed`` and does not authorize an automatic retry.
        """
        sid = _session_id(session_ref)
        revision = _revision_sha(revision_sha)
        group = _identifier(test_group, "test group")
        queue = queue_evidence or self.get_queue(revision)
        review = review_evidence or self.get_review_sessions(change_number, patchset)
        if queue.requested_revision != revision:
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT, "reconcile_retest",
                "Queue evidence is for a different revision",
            )
        if review.change_number != _positive_int(change_number, "Gerrit change number"):
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT, "reconcile_retest",
                "Review evidence is for a different change",
            )
        if review.patchset != _positive_int(patchset, "patchset number"):
            raise MalooAdapterError(
                MalooErrorCode.INVALID_INPUT, "reconcile_retest",
                "Review evidence is for a different patchset",
            )

        pending_entries = [
            entry for entry in queue.entries
            if entry.revision_sha == revision
            and entry.test_group == group
            and entry.pending
        ]
        if pending_entries:
            return MalooRetestReconciliation(
                sid, True, True, _text(jira_ticket).upper(),
                tuple("queue:" + (entry.queue_id or entry.status) for entry in pending_entries),
            )

        original = next((item for item in review.sessions if item.session_id == sid), None)
        baseline = original_submission or (original.submission if original else "")
        newer = [
            item for item in review.sessions
            if item.session_id != sid
            and item.test_group == group
            and baseline
            and item.submission
            and item.submission > baseline
        ]
        if newer:
            return MalooRetestReconciliation(
                sid, True, False, _text(jira_ticket).upper(),
                tuple("newer_session:" + item.session_id for item in newer),
            )

        # Forward-compatible direct flags on the exact original session are
        # still useful when the CLI gains them, but never substitute evidence
        # from a different revision or test group.
        if original is not None:
            direct = reconcile_retest_evidence(sid, original, jira_ticket=jira_ticket)
            if direct.already_requested or direct.pending is not None:
                return direct
        return MalooRetestReconciliation(
            sid, False, None, _text(jira_ticket).upper(), ()
        )
