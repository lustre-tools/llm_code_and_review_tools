"""Controller-owned, fail-closed Jenkins retrigger writes.

Workers never receive Jenkins credentials.  A caller supplies a dedicated
capability check and kill switch; this adapter binds one retrigger to one
completed failed Jenkins build for one exact, still-current Gerrit revision.
The durable dispatch claim is written before the POST.  Once claimed, the
operation is reconciliation-only and is never blindly submitted again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import quote, unquote, urlparse

from jenkins_adapter import SNAPSHOT_SCHEMA


JENKINS_RETRIGGER_CAPABILITY = "jenkins_retrigger"
JENKINS_HOST = "build.whamcloud.com"
RETRIGGER_STATES = frozenset({
    "prepared", "dispatch_claimed", "succeeded", "ambiguous", "failed",
    "stale", "cancelled",
})
HUMAN_STATES = frozenset({"ambiguous", "failed", "stale"})
_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_JOB_RE = re.compile(r"[A-Za-z0-9_.-]+")


class JenkinsRetriggerError(RuntimeError):
    """A retrigger could not safely proceed."""


class JenkinsRetriggerConflict(JenkinsRetriggerError):
    """The retrigger capability, binding, or state is stale/conflicting."""


class JenkinsRetriggerWriter(Protocol):
    """Narrow controller transport; implementations hold Jenkins credentials."""

    def retrigger(self, *, job_name: str, build_number: int) -> str: ...

    def observe_matching_retrigger(
        self, *, job_name: str, original_build_number: int,
        change_number: int, patchset: int, revision_sha: str,
        revision_ref: str, project: str, branch: str,
    ) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class JenkinsRetriggerPlan:
    action_id: str
    idempotency_key: str
    run_id: str
    session_id: str
    change_number: int
    patchset: int
    revision_sha: str
    revision_ref: str
    project: str
    branch: str
    job_name: str
    build_number: int
    build_url: str
    snapshot_sha256: str
    capability: str
    action_budget: int
    action_ordinal: int
    requested_by: str
    state: str
    prepared_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    retrigger_build_number: int | None = None
    retrigger_build_url: str | None = None
    reason_code: str | None = None
    summary: str | None = None

    @property
    def requires_human(self) -> bool:
        return self.state in HUMAN_STATES

    @property
    def binding_digest(self) -> str:
        values = [
            self.action_id, self.idempotency_key, self.run_id, self.session_id,
            self.change_number, self.patchset, self.revision_sha,
            self.revision_ref, self.project, self.branch, self.job_name,
            self.build_number, self.build_url, self.snapshot_sha256,
            self.capability, self.action_budget, self.action_ordinal,
            self.requested_by,
        ]
        return hashlib.sha256(json.dumps(
            values, ensure_ascii=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()


def _stamp(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def _canonical_build_url(value: Any) -> tuple[str, str, int]:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError as exc:
        raise JenkinsRetriggerConflict("Jenkins URL is not allowlisted") from exc
    if (
        parsed.scheme != "https" or parsed.hostname != JENKINS_HOST
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or port not in {None, 443}
    ):
        raise JenkinsRetriggerConflict("Jenkins URL is not allowlisted")
    pieces = [unquote(piece) for piece in parsed.path.split("/") if piece]
    if (
        len(pieces) != 3 or pieces[0] != "job" or not _JOB_RE.fullmatch(pieces[1])
        or not pieces[2].isdigit() or int(pieces[2]) <= 0
    ):
        # jenkins_tool's retrigger method interpolates a single job name.  Until
        # that transport supports folder jobs safely, reject them rather than
        # constructing a subtly different write endpoint.
        raise JenkinsRetriggerConflict("Jenkins URL is not a supported parent build")
    job_name, build_number = pieces[1], int(pieces[2])
    canonical = (
        f"https://{JENKINS_HOST}/job/{quote(job_name, safe='-._~')}/"
        f"{build_number}/"
    )
    if text != canonical:
        raise JenkinsRetriggerConflict("Jenkins URL is not canonical")
    return canonical, job_name, build_number


def _snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    stable = {
        key: value for key, value in snapshot.items()
        if key not in {"captured_at", "snapshot_sha256"}
    }
    return hashlib.sha256(json.dumps(
        stable, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _snapshot_binding(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise JenkinsRetriggerConflict("Jenkins failure snapshot is malformed")
    if snapshot.get("schema") != SNAPSHOT_SCHEMA or snapshot.get("complete") is not True:
        raise JenkinsRetriggerConflict("a complete Jenkins failure snapshot is required")
    change = snapshot.get("change")
    build = snapshot.get("build")
    if not isinstance(change, Mapping) or not isinstance(build, Mapping):
        raise JenkinsRetriggerConflict("Jenkins failure snapshot is malformed")
    try:
        change_number = int(change.get("change_number"))
        patchset = int(change.get("patchset"))
        build_number = int(build.get("build_number"))
    except (TypeError, ValueError) as exc:
        raise JenkinsRetriggerConflict("Jenkins failure snapshot is malformed") from exc
    if change_number <= 0 or patchset <= 0 or build_number <= 0:
        raise JenkinsRetriggerConflict("Jenkins failure snapshot is malformed")
    revision_sha = str(change.get("revision_sha") or "").lower()
    snapshot_sha256 = str(snapshot.get("snapshot_sha256") or "").lower()
    if not _SHA1_RE.fullmatch(revision_sha) or not _SHA256_RE.fullmatch(snapshot_sha256):
        raise JenkinsRetriggerConflict("Jenkins failure snapshot digest is malformed")
    if not hmac.compare_digest(_snapshot_digest(snapshot), snapshot_sha256):
        raise JenkinsRetriggerConflict("Jenkins failure snapshot digest changed")
    build_url, job_name, url_build_number = _canonical_build_url(build.get("url"))
    if (
        build.get("result") != "FAILURE" or build_number != url_build_number
        or str(build.get("job_name") or "") != job_name
    ):
        raise JenkinsRetriggerConflict("snapshot is not the exact failed Jenkins build")
    revision_ref = str(change.get("revision_ref") or "")
    expected_ref = f"refs/changes/{change_number % 100:02d}/{change_number}/{patchset}"
    project = str(change.get("project") or "")
    branch = str(change.get("branch") or "")
    if revision_ref != expected_ref or not project or not branch:
        raise JenkinsRetriggerConflict("Jenkins failure snapshot revision is malformed")
    return {
        "change_number": change_number, "patchset": patchset,
        "revision_sha": revision_sha, "revision_ref": revision_ref,
        "project": project, "branch": branch, "job_name": job_name,
        "build_number": build_number, "build_url": build_url,
        "snapshot_sha256": snapshot_sha256,
    }


class JenkinsRetriggerStore:
    """Private durable ledger with immutable action bindings and events."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(Path(database).expanduser())
        path = Path(self.database)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._migrate()
        os.chmod(path, 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pw_jenkins_retrigger (
                    action_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    change_number INTEGER NOT NULL CHECK(change_number > 0),
                    patchset INTEGER NOT NULL CHECK(patchset > 0),
                    revision_sha TEXT NOT NULL,
                    revision_ref TEXT NOT NULL,
                    project TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    build_number INTEGER NOT NULL CHECK(build_number > 0),
                    build_url TEXT NOT NULL UNIQUE,
                    snapshot_sha256 TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    action_budget INTEGER NOT NULL CHECK(action_budget > 0),
                    action_ordinal INTEGER NOT NULL CHECK(action_ordinal > 0),
                    requested_by TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared','dispatch_claimed','succeeded','ambiguous',
                        'failed','stale','cancelled'
                    )),
                    prepared_at REAL NOT NULL,
                    claimed_at REAL,
                    completed_at REAL,
                    retrigger_build_number INTEGER,
                    retrigger_build_url TEXT,
                    reason_code TEXT,
                    summary TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS pw_jenkins_retrigger_binding_immutable
                BEFORE UPDATE OF idempotency_key,run_id,session_id,change_number,
                    patchset,revision_sha,revision_ref,project,branch,job_name,
                    build_number,build_url,snapshot_sha256,capability,action_budget,
                    action_ordinal,requested_by,prepared_at
                ON pw_jenkins_retrigger
                BEGIN SELECT RAISE(ABORT, 'retrigger binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS pw_jenkins_retrigger_no_delete
                BEFORE DELETE ON pw_jenkins_retrigger
                BEGIN SELECT RAISE(ABORT, 'retrigger records are durable'); END;
                CREATE TABLE IF NOT EXISTS pw_jenkins_retrigger_event (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS pw_jenkins_retrigger_event_no_update
                BEFORE UPDATE ON pw_jenkins_retrigger_event
                BEGIN SELECT RAISE(ABORT, 'retrigger events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS pw_jenkins_retrigger_event_no_delete
                BEFORE DELETE ON pw_jenkins_retrigger_event
                BEGIN SELECT RAISE(ABORT, 'retrigger events are append-only'); END;
            """)
            connection.commit()

    @staticmethod
    def _plan(row: sqlite3.Row) -> JenkinsRetriggerPlan:
        return JenkinsRetriggerPlan(
            action_id=row["action_id"], idempotency_key=row["idempotency_key"],
            run_id=row["run_id"], session_id=row["session_id"],
            change_number=int(row["change_number"]), patchset=int(row["patchset"]),
            revision_sha=row["revision_sha"], revision_ref=row["revision_ref"],
            project=row["project"], branch=row["branch"], job_name=row["job_name"],
            build_number=int(row["build_number"]), build_url=row["build_url"],
            snapshot_sha256=row["snapshot_sha256"], capability=row["capability"],
            action_budget=int(row["action_budget"]),
            action_ordinal=int(row["action_ordinal"]), requested_by=row["requested_by"],
            state=row["state"], prepared_at=_stamp(row["prepared_at"]),
            claimed_at=_stamp(row["claimed_at"]), completed_at=_stamp(row["completed_at"]),
            retrigger_build_number=row["retrigger_build_number"],
            retrigger_build_url=row["retrigger_build_url"],
            reason_code=row["reason_code"], summary=row["summary"],
        )

    def get(self, action_id: str) -> JenkinsRetriggerPlan:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_jenkins_retrigger WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise JenkinsRetriggerError("unknown Jenkins retrigger")
        return self._plan(row)

    def get_by_run(self, run_id: str) -> JenkinsRetriggerPlan | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_jenkins_retrigger WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._plan(row) if row is not None else None

    def get_by_build_url(self, build_url: str) -> JenkinsRetriggerPlan | None:
        """Return the terminal one-use action bound to an exact parent build."""

        canonical, _, _ = _canonical_build_url(build_url)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_jenkins_retrigger WHERE build_url = ?",
                (canonical,),
            ).fetchone()
        return self._plan(row) if row is not None else None

    @staticmethod
    def _event(connection, action_id, event_type, detail, at):
        connection.execute(
            "INSERT INTO pw_jenkins_retrigger_event"
            "(action_id,event_type,detail_json,created_at) VALUES (?,?,?,?)",
            (action_id, event_type, json.dumps(detail, sort_keys=True), at.timestamp()),
        )

    def prepare(self, *, now: datetime | None = None, **values) -> JenkinsRetriggerPlan:
        timestamp = now or datetime.now(timezone.utc)
        action_id = values.pop("action_id", "jenkins-retrigger-" + uuid.uuid4().hex)
        columns = (
            "idempotency_key", "run_id", "session_id", "change_number", "patchset",
            "revision_sha", "revision_ref", "project", "branch", "job_name",
            "build_number", "build_url", "snapshot_sha256", "capability",
            "action_budget", "action_ordinal", "requested_by",
        )
        bound = [values[name] for name in columns]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pw_jenkins_retrigger WHERE run_id = ? "
                "OR idempotency_key = ? OR build_url = ?",
                (values["run_id"], values["idempotency_key"], values["build_url"]),
            ).fetchone()
            if row is not None:
                plan = self._plan(row)
                if any(str(getattr(plan, name)) != str(values[name]) for name in columns):
                    connection.rollback()
                    raise JenkinsRetriggerConflict("Jenkins retrigger identity was reused")
                connection.rollback()
                return plan
            placeholders = ",".join("?" for _ in range(len(columns) + 4))
            connection.execute(
                "INSERT INTO pw_jenkins_retrigger(action_id," + ",".join(columns) +
                ",state,prepared_at,updated_at) VALUES (" + placeholders + ")",
                (action_id, *bound, "prepared", timestamp.timestamp(), timestamp.timestamp()),
            )
            self._event(connection, action_id, "prepared", {}, timestamp)
            connection.commit()
        return self.get(action_id)

    def transition(
        self, action_id: str, *, expected: set[str], state: str,
        at: datetime | None = None, **updates,
    ) -> JenkinsRetriggerPlan:
        if state not in RETRIGGER_STATES:
            raise ValueError("invalid Jenkins retrigger state")
        allowed = {
            "claimed_at", "completed_at", "retrigger_build_number",
            "retrigger_build_url", "reason_code", "summary",
        }
        if set(updates) - allowed:
            raise ValueError("invalid Jenkins retrigger update")
        timestamp = at or datetime.now(timezone.utc)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM pw_jenkins_retrigger WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise JenkinsRetriggerError("unknown Jenkins retrigger")
            if row["state"] not in expected:
                connection.rollback()
                raise JenkinsRetriggerConflict(
                    f"Jenkins retrigger is {row['state']}, expected "
                    + ", ".join(sorted(expected))
                )
            assignments = ["state = ?", "updated_at = ?"]
            params: list[Any] = [state, timestamp.timestamp()]
            for key, value in updates.items():
                assignments.append(key + " = ?")
                params.append(value.timestamp() if isinstance(value, datetime) else value)
            params.append(action_id)
            connection.execute(
                "UPDATE pw_jenkins_retrigger SET " + ",".join(assignments)
                + " WHERE action_id = ?", params,
            )
            self._event(connection, action_id, state, {
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in updates.items() if key != "summary"
            }, timestamp)
            connection.commit()
        return self.get(action_id)


class JenkinsToolRetriggerWriter:
    """Credential-holding adapter around jenkins_tool's narrow client calls."""

    def __init__(self, client: Any, *, max_builds: int = 50) -> None:
        self.client = client
        self.max_builds = max(1, min(int(max_builds), 100))

    @staticmethod
    def _parameters(build: Mapping[str, Any]) -> dict[str, str]:
        result = {}
        for action in build.get("actions") or ():
            if not isinstance(action, Mapping):
                continue
            for item in action.get("parameters") or ():
                if isinstance(item, Mapping):
                    name = str(item.get("name") or "")
                    if name.startswith("GERRIT_"):
                        if name in result:
                            raise JenkinsRetriggerError(
                                "Jenkins returned duplicate Gerrit parameters"
                            )
                        result[name] = str(item.get("value") or "")
        return result

    def retrigger(self, *, job_name: str, build_number: int) -> str:
        _canonical_build_url(
            f"https://{JENKINS_HOST}/job/{quote(str(job_name), safe='-._~')}/"
            f"{build_number}/"
        )
        return str(self.client.retrigger_build(job_name, build_number))[:1000]

    def observe_matching_retrigger(
        self, *, job_name: str, original_build_number: int,
        change_number: int, patchset: int, revision_sha: str,
        revision_ref: str, project: str, branch: str,
    ) -> Mapping[str, Any] | None:
        _canonical_build_url(
            f"https://{JENKINS_HOST}/job/{quote(str(job_name), safe='-._~')}/"
            f"{original_build_number}/"
        )
        matches = []
        for summary in self.client.get_builds(job_name, limit=self.max_builds):
            try:
                number = int(summary.get("number"))
            except (TypeError, ValueError):
                continue
            if number <= original_build_number:
                continue
            detail = self.client.get_build(job_name, number)
            if not isinstance(detail, Mapping):
                continue
            params = self._parameters(detail)
            if not (
                params.get("GERRIT_CHANGE_NUMBER") == str(change_number)
                and params.get("GERRIT_PATCHSET_NUMBER") == str(patchset)
                and params.get("GERRIT_PATCHSET_REVISION", "").lower()
                    == revision_sha.lower()
                and params.get("GERRIT_REFSPEC") == revision_ref
                and params.get("GERRIT_PROJECT") == project
                and params.get("GERRIT_BRANCH") == branch
            ):
                continue
            url, returned_job, returned_number = _canonical_build_url(detail.get("url"))
            if returned_job != job_name or returned_number != number:
                continue
            matches.append((number, url))
        if not matches:
            return None
        number, url = min(matches)
        return {"build_number": number, "url": url}


class JenkinsRetriggerController:
    """Prepare, claim, dispatch once, and reconcile a Jenkins retrigger."""

    def __init__(
        self, store: JenkinsRetriggerStore, writer: JenkinsRetriggerWriter,
        *, status_fetcher: Callable[[int], Mapping[str, Any]],
        failure_fetcher: Callable[..., Mapping[str, Any]],
        capability_check: Callable[[Mapping[str, Any]], bool],
        enabled: bool = False,
    ) -> None:
        self.store = store
        self.writer = writer
        self.status_fetcher = status_fetcher
        self.failure_fetcher = failure_fetcher
        self.capability_check = capability_check
        self.enabled = bool(enabled)

    def _authorized(self, binding: Mapping[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            return bool(self.capability_check({
                **binding, "capability": JENKINS_RETRIGGER_CAPABILITY,
            }))
        except Exception:
            return False

    @staticmethod
    def _current_matches(binding: Mapping[str, Any], status: Mapping[str, Any]) -> bool:
        if not isinstance(status, Mapping):
            return False
        try:
            return (
                int(status.get("change_number") or 0) == binding["change_number"]
                and int(status.get("patchset") or 0) == binding["patchset"]
                and str(status.get("revision_sha") or "").lower()
                    == binding["revision_sha"]
                and str(status.get("project") or "") == binding["project"]
                and str(status.get("branch") or "") == binding["branch"]
                and str(status.get("status") or "").upper() == "NEW"
            )
        except (TypeError, ValueError):
            return False

    def _recapture(self, binding: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.failure_fetcher(
            binding["build_url"], change_number=binding["change_number"],
            patchset=binding["patchset"], revision_sha=binding["revision_sha"],
            revision_ref=binding["revision_ref"], project=binding["project"],
            branch=binding["branch"],
        )

    def _observe_existing(self, binding: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return self.writer.observe_matching_retrigger(
            job_name=binding["job_name"],
            original_build_number=binding["build_number"],
            change_number=binding["change_number"], patchset=binding["patchset"],
            revision_sha=binding["revision_sha"],
            revision_ref=binding["revision_ref"], project=binding["project"],
            branch=binding["branch"],
        )

    @staticmethod
    def _plan_binding(plan: JenkinsRetriggerPlan) -> dict[str, Any]:
        return {
            "run_id": plan.run_id, "session_id": plan.session_id,
            "change_number": plan.change_number, "patchset": plan.patchset,
            "revision_sha": plan.revision_sha, "revision_ref": plan.revision_ref,
            "project": plan.project, "branch": plan.branch,
            "job_name": plan.job_name, "build_number": plan.build_number,
            "build_url": plan.build_url, "snapshot_sha256": plan.snapshot_sha256,
        }

    def prepare(
        self, *, snapshot: Mapping[str, Any], idempotency_key: str,
        run_id: str, session_id: str, requested_by: str,
        action_budget: int, actions_used: int = 0,
    ) -> JenkinsRetriggerPlan:
        binding = _snapshot_binding(snapshot)
        text_fields = {
            "idempotency key": idempotency_key, "run ID": run_id,
            "session ID": session_id, "requesting principal": requested_by,
        }
        for label, value in text_fields.items():
            text = str(value or "").strip()
            if not text or len(text) > 500:
                raise JenkinsRetriggerConflict(f"Jenkins retrigger {label} is malformed")
        try:
            if isinstance(action_budget, bool) or isinstance(actions_used, bool):
                raise ValueError
            action_budget = int(action_budget)
            actions_used = int(actions_used)
        except (TypeError, ValueError) as exc:
            raise JenkinsRetriggerConflict("Jenkins action budget is malformed") from exc
        if action_budget <= 0 or actions_used < 0 or actions_used >= action_budget:
            raise JenkinsRetriggerConflict("Jenkins retrigger action budget is exhausted")
        request_binding = {
            **binding, "run_id": str(run_id), "session_id": str(session_id),
        }
        if not self._authorized(request_binding):
            raise JenkinsRetriggerConflict(
                "Jenkins retrigger is disabled or its dedicated capability is unavailable"
            )
        plan = self.store.prepare(
            idempotency_key=str(idempotency_key), run_id=str(run_id),
            session_id=str(session_id), requested_by=str(requested_by),
            capability=JENKINS_RETRIGGER_CAPABILITY,
            action_budget=action_budget, action_ordinal=actions_used + 1,
            **binding,
        )
        if plan.state != "prepared":
            return plan
        try:
            status = self.status_fetcher(plan.change_number)
            recaptured = _snapshot_binding(self._recapture(binding))
        except Exception:
            return self.store.transition(
                plan.action_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                reason_code="preflight_unavailable",
                summary="Exact Gerrit/Jenkins preflight failed; no retrigger was sent",
            )
        if (
            not self._current_matches(binding, status)
            or recaptured != binding
        ):
            return self.store.transition(
                plan.action_id, expected={"prepared"}, state="stale",
                completed_at=datetime.now(timezone.utc), reason_code="binding_changed",
                summary="Gerrit or the failed Jenkins snapshot changed; no retrigger was sent",
            )
        try:
            existing = self._observe_existing(binding)
        except Exception:
            return self.store.transition(
                plan.action_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                reason_code="preflight_unavailable",
                summary="Existing Jenkins builds could not be checked; no retrigger was sent",
            )
        if existing is not None:
            return self.store.transition(
                plan.action_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                reason_code="already_retriggered",
                summary="A newer exact Jenkins build already exists; no duplicate was sent",
            )
        return plan

    def execute(
        self, action_id: str, *, expected_binding_digest: str,
    ) -> JenkinsRetriggerPlan:
        plan = self.store.get(action_id)
        if not hmac.compare_digest(plan.binding_digest, str(expected_binding_digest)):
            raise JenkinsRetriggerConflict("Jenkins retrigger confirmation does not match")
        if plan.state == "succeeded":
            return plan
        if plan.state in {"dispatch_claimed", "ambiguous"}:
            return self.reconcile(action_id)
        if plan.state != "prepared":
            raise JenkinsRetriggerConflict("Jenkins retrigger is not ready for dispatch")
        binding = self._plan_binding(plan)
        if not self._authorized(binding):
            return self.store.transition(
                action_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc), reason_code="capability_denied",
                summary="Jenkins retrigger capability was disabled before dispatch",
            )
        try:
            status = self.status_fetcher(plan.change_number)
            recaptured = _snapshot_binding(self._recapture(binding))
        except Exception:
            return self.store.transition(
                action_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc), reason_code="preflight_unavailable",
                summary="Final Gerrit/Jenkins preflight failed; no retrigger was sent",
            )
        if not self._current_matches(binding, status) or recaptured != {
            key: binding[key] for key in recaptured
        }:
            return self.store.transition(
                action_id, expected={"prepared"}, state="stale",
                completed_at=datetime.now(timezone.utc), reason_code="binding_changed",
                summary="Gerrit or Jenkins changed immediately before dispatch",
            )
        try:
            existing = self._observe_existing(binding)
        except Exception:
            return self.store.transition(
                action_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                reason_code="preflight_unavailable",
                summary="Final Jenkins duplicate check failed; no retrigger was sent",
            )
        if existing is not None:
            return self.store.transition(
                action_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                reason_code="already_retriggered",
                summary="A newer exact Jenkins build appeared; no duplicate was sent",
            )
        plan = self.store.transition(
            action_id, expected={"prepared"}, state="dispatch_claimed",
            claimed_at=datetime.now(timezone.utc),
            summary="Jenkins retrigger dispatch durably claimed",
        )
        try:
            self.writer.retrigger(
                job_name=plan.job_name, build_number=plan.build_number,
            )
        except Exception:
            # The POST may have reached Jenkins.  Reconciliation is the only
            # safe next action; never issue another retrigger for this claim.
            return self.reconcile(action_id)
        return self.reconcile(action_id)

    def reconcile(self, action_id: str) -> JenkinsRetriggerPlan:
        plan = self.store.get(action_id)
        if plan.state == "succeeded":
            return plan
        if plan.state not in {"dispatch_claimed", "ambiguous"}:
            raise JenkinsRetriggerConflict("only a dispatched retrigger can be reconciled")
        binding = self._plan_binding(plan)
        try:
            status = self.status_fetcher(plan.change_number)
        except Exception:
            status = None
        if status is None:
            if plan.state == "ambiguous":
                return plan
            return self.store.transition(
                action_id, expected={"dispatch_claimed"}, state="ambiguous",
                completed_at=datetime.now(timezone.utc), reason_code="reconcile_unavailable",
                summary="Retrigger outcome is unknown; human review is required",
            )
        if not self._current_matches(binding, status):
            return self.store.transition(
                action_id, expected={"dispatch_claimed", "ambiguous"}, state="stale",
                completed_at=datetime.now(timezone.utc), reason_code="revision_advanced",
                summary="Gerrit advanced after dispatch; human review is required",
            )
        try:
            observed = self.writer.observe_matching_retrigger(
                job_name=plan.job_name, original_build_number=plan.build_number,
                change_number=plan.change_number, patchset=plan.patchset,
                revision_sha=plan.revision_sha, revision_ref=plan.revision_ref,
                project=plan.project, branch=plan.branch,
            )
        except Exception:
            observed = None
        if observed is None:
            if plan.state == "ambiguous":
                return plan
            return self.store.transition(
                action_id, expected={"dispatch_claimed"}, state="ambiguous",
                completed_at=datetime.now(timezone.utc), reason_code="not_observed",
                summary="Retrigger was claimed but no exact new build was observed; do not retry",
            )
        try:
            observed_url, observed_job, observed_number = _canonical_build_url(
                observed.get("url")
            )
            if observed_job != plan.job_name or observed_number <= plan.build_number:
                raise JenkinsRetriggerConflict("observed Jenkins build does not match")
        except Exception:
            if plan.state == "ambiguous":
                return plan
            return self.store.transition(
                action_id, expected={"dispatch_claimed"}, state="ambiguous",
                completed_at=datetime.now(timezone.utc), reason_code="invalid_observation",
                summary="Jenkins returned an unsafe retrigger observation; do not retry",
            )
        return self.store.transition(
            action_id, expected={"dispatch_claimed", "ambiguous"}, state="succeeded",
            completed_at=datetime.now(timezone.utc),
            retrigger_build_number=observed_number,
            retrigger_build_url=observed_url, reason_code="retrigger_observed",
            summary="Jenkins accepted the retrigger and an exact new build was observed",
        )


def configured_jenkins_writer() -> JenkinsToolRetriggerWriter:
    """Load controller-only credentials using jenkins_tool's configuration."""
    try:
        from jenkins_tool.client import JenkinsClient
        from jenkins_tool.config import load_config
    except ModuleNotFoundError:
        # Support a source checkout of the llm_code_and_review_tools monorepo
        # without making Patch Watcher's worker environment carry credentials.
        repository = Path(__file__).resolve().parent.parent
        for source_root in (
            repository / "jenkins_tool",
            repository / "llm_tool_common" / "src",
        ):
            if source_root.is_dir() and str(source_root) not in sys.path:
                sys.path.insert(0, str(source_root))
        from jenkins_tool.client import JenkinsClient
        from jenkins_tool.config import load_config

    return JenkinsToolRetriggerWriter(JenkinsClient(load_config()))


__all__ = [
    "JENKINS_RETRIGGER_CAPABILITY", "JenkinsRetriggerConflict",
    "JenkinsRetriggerController", "JenkinsRetriggerError",
    "JenkinsRetriggerPlan", "JenkinsRetriggerStore",
    "JenkinsToolRetriggerWriter", "configured_jenkins_writer",
]
