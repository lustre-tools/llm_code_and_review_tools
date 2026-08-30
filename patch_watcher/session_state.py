"""Durable, side-effect-free state for managed Patch Watcher sessions.

This module deliberately stops at recording controller intent.  In particular,
confirming a cancellation or kill request never sends a signal and never marks
the session terminal; a separate runner/controller must perform and reconcile
that work.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


TRIAGE_PROFILE = "triage"
ENGINEERING_PROFILE = "engineering"

ACTIVE_INACTIVITY_STATES = frozenset({"preparing", "running"})
WAITING_STATES = frozenset(
    {"queued", "waiting_human", "waiting_external", "paused", "blocked"}
)
TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "stale", "resource_exhausted"}
)
SESSION_STATES = ACTIVE_INACTIVITY_STATES | WAITING_STATES | TERMINAL_STATES
WORKER_ADMISSION_STATES = frozenset(
    {"not_checked", "checking", "ready", "degraded", "blocked"}
)

TRIAGE_WALL_LIMIT = timedelta(minutes=20)
ENGINEERING_INACTIVITY_LIMIT = timedelta(minutes=30)
ENGINEERING_REMINDER_INTERVAL = timedelta(hours=2)
ABSOLUTE_RUNTIME_CAP = timedelta(hours=48)

AGENT_RUNTIME_TIMEOUT = "agent_runtime_timeout"
AGENT_INACTIVITY_TIMEOUT = "agent_inactivity_timeout"
AGENT_ABSOLUTE_RUNTIME_CAP = "agent_absolute_runtime_cap"


class SessionStateError(RuntimeError):
    """Base error for invalid or unavailable managed-session state."""


class SessionNotFound(SessionStateError):
    """Raised when an operation names an unknown session."""


class SessionAlreadyExists(SessionStateError):
    """Raised when a session or run is registered more than once."""


class InvalidSessionOperation(SessionStateError):
    """Raised when an operation would violate a session invariant."""


@dataclass(frozen=True)
class ManagedSession:
    session_id: str
    patch_id: str
    run_id: str
    profile: str
    state: str
    pid: int | None
    started_at: datetime
    last_qualifying_activity_at: datetime
    active_interval_started_at: datetime | None
    state_changed_at: datetime


@dataclass(frozen=True)
class SessionMessage:
    message_id: int
    session_id: str
    author: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class TimeoutDecision:
    code: str
    deadline_at: datetime


@dataclass(frozen=True)
class ReminderDue:
    session_id: str
    interval_index: int
    due_at: datetime
    idempotency_key: str


@dataclass(frozen=True)
class PolicyEvaluation:
    timeout: TimeoutDecision | None = None
    reminder: ReminderDue | None = None


@dataclass(frozen=True)
class ControlIntent:
    request_id: str
    session_id: str
    action: str
    requested_by: str
    requested_at: datetime
    confirmed_by: str | None
    confirmed_at: datetime | None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


@dataclass(frozen=True)
class WorkerAdmission:
    session_id: str
    profile_id: str
    profile_hash: str
    environment_instance_id: str
    status: str
    isolation_profile: str
    network_profile: str
    attestation: dict
    instruction_hash: str
    broker_session_id: str | None
    failure_code: str | None
    failure_summary: str | None
    checked_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_epoch(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.timestamp()


def _as_datetime(value: float | int) -> datetime:
    return datetime.fromtimestamp(float(value), timezone.utc)


def _required_text(name: str, value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _validate_pid(pid: int | None) -> int | None:
    if pid is not None and (isinstance(pid, bool) or pid <= 0):
        raise ValueError("pid must be a positive integer or None")
    return pid


class SessionStateStore:
    """SQLite-backed state and policy evaluation for managed sessions."""

    SCHEMA_VERSION = 3

    _MIGRATIONS = {
        1: (
            """
            CREATE TABLE pw_managed_session (
                session_id TEXT PRIMARY KEY,
                patch_id TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                profile TEXT NOT NULL CHECK (profile IN ('triage', 'engineering')),
                state TEXT NOT NULL,
                pid INTEGER,
                started_at REAL NOT NULL,
                last_qualifying_activity_at REAL NOT NULL,
                active_interval_started_at REAL,
                state_changed_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                CHECK (pid IS NULL OR pid > 0)
            )
            """,
            """
            CREATE INDEX pw_managed_session_state_idx
            ON pw_managed_session(state, started_at)
            """,
            """
            CREATE TABLE pw_session_message (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE INDEX pw_session_message_recent_idx
            ON pw_session_message(session_id, created_at DESC, message_id DESC)
            """,
            """
            CREATE TABLE pw_session_reminder_delivery (
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                interval_index INTEGER NOT NULL CHECK (interval_index > 0),
                idempotency_key TEXT NOT NULL UNIQUE,
                delivered_at REAL NOT NULL,
                PRIMARY KEY (session_id, interval_index)
            )
            """,
        ),
        2: (
            """
            CREATE TABLE pw_session_control_intent (
                request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                action TEXT NOT NULL CHECK (action IN ('cancel', 'kill')),
                requested_by TEXT NOT NULL,
                requested_at REAL NOT NULL,
                confirmed_by TEXT,
                confirmed_at REAL,
                CHECK (
                    (confirmed_by IS NULL AND confirmed_at IS NULL) OR
                    (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE INDEX pw_session_control_intent_session_idx
            ON pw_session_control_intent(session_id, requested_at DESC)
            """,
        ),
        3: (
            """
            CREATE TABLE pw_worker_admission (
                session_id TEXT PRIMARY KEY REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                profile_id TEXT NOT NULL,
                profile_hash TEXT NOT NULL,
                environment_instance_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('not_checked', 'checking', 'ready', 'degraded', 'blocked')
                ),
                isolation_profile TEXT NOT NULL,
                network_profile TEXT NOT NULL,
                attestation_json TEXT NOT NULL,
                instruction_hash TEXT NOT NULL,
                broker_session_id TEXT,
                failure_code TEXT,
                failure_summary TEXT,
                checked_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE INDEX pw_worker_admission_status_idx
            ON pw_worker_admission(status, checked_at DESC)
            """,
        ),
    }

    def __init__(
        self,
        database: str | Path,
        *,
        max_recent_messages: int = 50,
        max_message_chars: int = 4_000,
    ) -> None:
        if max_recent_messages <= 0:
            raise ValueError("max_recent_messages must be positive")
        if max_message_chars <= 0:
            raise ValueError("max_message_chars must be positive")
        self.database = str(database)
        self.max_recent_messages = max_recent_messages
        self.max_message_chars = max_message_chars
        if self.database != ":memory:":
            database_path = Path(self.database).expanduser()
            database_path.parent.mkdir(
                mode=0o700, parents=True, exist_ok=True
            )
        self._migrate()
        if self.database != ":memory:":
            os.chmod(Path(self.database).expanduser(), 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.database != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pw_session_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT version FROM pw_session_schema WHERE singleton = 1"
                ).fetchone()
                current = int(row["version"]) if row is not None else 0
                if current > self.SCHEMA_VERSION:
                    raise SessionStateError(
                        f"session schema version {current} is newer than supported "
                        f"version {self.SCHEMA_VERSION}"
                    )
                for version in range(current + 1, self.SCHEMA_VERSION + 1):
                    for statement in self._MIGRATIONS[version]:
                        connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO pw_session_schema(singleton, version)
                        VALUES (1, ?)
                        ON CONFLICT(singleton) DO UPDATE SET version = excluded.version
                        """,
                        (version,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> ManagedSession:
        active_started = row["active_interval_started_at"]
        return ManagedSession(
            session_id=row["session_id"],
            patch_id=row["patch_id"],
            run_id=row["run_id"],
            profile=row["profile"],
            state=row["state"],
            pid=row["pid"],
            started_at=_as_datetime(row["started_at"]),
            last_qualifying_activity_at=_as_datetime(
                row["last_qualifying_activity_at"]
            ),
            active_interval_started_at=(
                _as_datetime(active_started) if active_started is not None else None
            ),
            state_changed_at=_as_datetime(row["state_changed_at"]),
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> SessionMessage:
        return SessionMessage(
            message_id=row["message_id"],
            session_id=row["session_id"],
            author=row["author"],
            body=row["body"],
            created_at=_as_datetime(row["created_at"]),
        )

    @staticmethod
    def _control_from_row(row: sqlite3.Row) -> ControlIntent:
        confirmed_at = row["confirmed_at"]
        return ControlIntent(
            request_id=row["request_id"],
            session_id=row["session_id"],
            action=row["action"],
            requested_by=row["requested_by"],
            requested_at=_as_datetime(row["requested_at"]),
            confirmed_by=row["confirmed_by"],
            confirmed_at=(
                _as_datetime(confirmed_at) if confirmed_at is not None else None
            ),
        )

    @staticmethod
    def _admission_from_row(row: sqlite3.Row) -> WorkerAdmission:
        return WorkerAdmission(
            session_id=row["session_id"],
            profile_id=row["profile_id"],
            profile_hash=row["profile_hash"],
            environment_instance_id=row["environment_instance_id"],
            status=row["status"],
            isolation_profile=row["isolation_profile"],
            network_profile=row["network_profile"],
            attestation=json.loads(row["attestation_json"]),
            instruction_hash=row["instruction_hash"],
            broker_session_id=row["broker_session_id"],
            failure_code=row["failure_code"],
            failure_summary=row["failure_summary"],
            checked_at=_as_datetime(row["checked_at"]),
        )

    @staticmethod
    def _require_session(
        connection: sqlite3.Connection, session_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM pw_managed_session WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFound(f"unknown managed session: {session_id}")
        return row

    def register_session(
        self,
        session_id: str,
        *,
        patch_id: str,
        run_id: str,
        profile: str,
        state: str = "preparing",
        pid: int | None = None,
        started_at: datetime | None = None,
    ) -> ManagedSession:
        session_id = _required_text("session_id", session_id)
        patch_id = _required_text("patch_id", patch_id)
        run_id = _required_text("run_id", run_id)
        if profile not in {TRIAGE_PROFILE, ENGINEERING_PROFILE}:
            raise ValueError("profile must be 'triage' or 'engineering'")
        if state not in SESSION_STATES:
            raise ValueError(f"unknown session state: {state}")
        pid = _validate_pid(pid)
        started = started_at or _utc_now()
        started_epoch = _as_epoch(started)
        active_started = (
            started_epoch if state in ACTIVE_INACTIVITY_STATES else None
        )
        try:
            with self._connection() as connection, connection:
                connection.execute(
                    """
                    INSERT INTO pw_managed_session(
                        session_id, patch_id, run_id, profile, state, pid,
                        started_at, last_qualifying_activity_at,
                        active_interval_started_at, state_changed_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        patch_id,
                        run_id,
                        profile,
                        state,
                        pid,
                        started_epoch,
                        started_epoch,
                        active_started,
                        started_epoch,
                        started_epoch,
                        started_epoch,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise SessionAlreadyExists(
                f"session {session_id!r} or run {run_id!r} is already registered"
            ) from exc
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> ManagedSession:
        with self._connection() as connection:
            return self._session_from_row(
                self._require_session(connection, session_id)
            )

    def list_sessions(self, *, include_terminal: bool = True) -> list[ManagedSession]:
        query = "SELECT * FROM pw_managed_session"
        parameters: tuple[str, ...] = ()
        if not include_terminal:
            placeholders = ", ".join("?" for _ in TERMINAL_STATES)
            query += f" WHERE state NOT IN ({placeholders})"
            parameters = tuple(sorted(TERMINAL_STATES))
        query += " ORDER BY started_at DESC, session_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._session_from_row(row) for row in rows]

    def record_worker_admission(
        self,
        session_id: str,
        *,
        profile_id: str,
        profile_hash: str,
        environment_instance_id: str,
        status: str,
        isolation_profile: str,
        network_profile: str,
        attestation: dict | None = None,
        instruction_hash: str,
        broker_session_id: str | None = None,
        failure_code: str | None = None,
        failure_summary: str | None = None,
        checked_at: datetime | None = None,
    ) -> WorkerAdmission:
        """Persist one redacted environment-admission result for a run."""
        session_id = _required_text("session_id", session_id)
        profile_id = _required_text("profile_id", profile_id)
        profile_hash = _required_text("profile_hash", profile_hash)
        environment_instance_id = _required_text(
            "environment_instance_id", environment_instance_id
        )
        isolation_profile = _required_text(
            "isolation_profile", isolation_profile
        )
        network_profile = _required_text("network_profile", network_profile)
        instruction_hash = _required_text("instruction_hash", instruction_hash)
        if status not in WORKER_ADMISSION_STATES:
            raise ValueError(f"unknown worker admission state: {status}")
        if status == "blocked" and not failure_code:
            raise ValueError("blocked worker admission requires failure_code")
        broker_session_id = (
            _required_text("broker_session_id", broker_session_id)
            if broker_session_id is not None
            else None
        )
        failure_code = (
            _required_text("failure_code", failure_code)
            if failure_code is not None
            else None
        )
        failure_summary = (
            str(failure_summary)[:2_000] if failure_summary is not None else None
        )
        try:
            attestation_json = json.dumps(
                attestation or {}, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("attestation must be JSON serializable") from exc
        if len(attestation_json.encode("utf-8")) > 256_000:
            raise ValueError("attestation exceeds 256 KiB")
        checked_epoch = _as_epoch(checked_at or _utc_now())
        with self._connection() as connection, connection:
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT INTO pw_worker_admission(
                    session_id, profile_id, profile_hash,
                    environment_instance_id, status, isolation_profile,
                    network_profile, attestation_json, instruction_hash,
                    broker_session_id, failure_code, failure_summary,
                    checked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    profile_id = excluded.profile_id,
                    profile_hash = excluded.profile_hash,
                    environment_instance_id = excluded.environment_instance_id,
                    status = excluded.status,
                    isolation_profile = excluded.isolation_profile,
                    network_profile = excluded.network_profile,
                    attestation_json = excluded.attestation_json,
                    instruction_hash = excluded.instruction_hash,
                    broker_session_id = excluded.broker_session_id,
                    failure_code = excluded.failure_code,
                    failure_summary = excluded.failure_summary,
                    checked_at = excluded.checked_at,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    profile_id,
                    profile_hash,
                    environment_instance_id,
                    status,
                    isolation_profile,
                    network_profile,
                    attestation_json,
                    instruction_hash,
                    broker_session_id,
                    failure_code,
                    failure_summary,
                    checked_epoch,
                    checked_epoch,
                ),
            )
        admission = self.get_worker_admission(session_id)
        assert admission is not None
        return admission

    def get_worker_admission(self, session_id: str) -> WorkerAdmission | None:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT * FROM pw_worker_admission WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._admission_from_row(row) if row is not None else None

    def list_worker_admissions(self) -> list[WorkerAdmission]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pw_worker_admission
                ORDER BY checked_at DESC, session_id
                """
            ).fetchall()
        return [self._admission_from_row(row) for row in rows]

    def set_pid(self, session_id: str, pid: int | None) -> ManagedSession:
        pid = _validate_pid(pid)
        now_epoch = _as_epoch(_utc_now())
        with self._connection() as connection, connection:
            self._require_session(connection, session_id)
            connection.execute(
                """
                UPDATE pw_managed_session
                SET pid = ?, updated_at = MAX(updated_at, ?)
                WHERE session_id = ?
                """,
                (pid, now_epoch, session_id),
            )
        return self.get_session(session_id)

    def set_state(
        self,
        session_id: str,
        state: str,
        *,
        changed_at: datetime | None = None,
    ) -> ManagedSession:
        if state not in SESSION_STATES:
            raise ValueError(f"unknown session state: {state}")
        changed_epoch = _as_epoch(changed_at or _utc_now())
        with self._connection() as connection, connection:
            row = self._require_session(connection, session_id)
            old_state = row["state"]
            if old_state in TERMINAL_STATES and state != old_state:
                raise InvalidSessionOperation(
                    f"terminal session {session_id} cannot transition from "
                    f"{old_state} to {state}"
                )
            if state == old_state:
                return self._session_from_row(row)
            if state in ACTIVE_INACTIVITY_STATES:
                if old_state in ACTIVE_INACTIVITY_STATES:
                    active_started = row["active_interval_started_at"]
                else:
                    # A resumed engineering session gets a fresh inactivity
                    # interval; waiting time never consumes that interval.
                    active_started = changed_epoch
            else:
                active_started = None
            connection.execute(
                """
                UPDATE pw_managed_session
                SET state = ?, active_interval_started_at = ?,
                    state_changed_at = ?, updated_at = MAX(updated_at, ?)
                WHERE session_id = ?
                """,
                (
                    state,
                    active_started,
                    changed_epoch,
                    changed_epoch,
                    session_id,
                ),
            )
        return self.get_session(session_id)

    def record_activity(
        self, session_id: str, *, at: datetime | None = None
    ) -> ManagedSession:
        """Record owned, qualifying progress without allowing time regression."""
        activity_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            row = self._require_session(connection, session_id)
            if row["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation(
                    f"cannot record activity for terminal session {session_id}"
                )
            connection.execute(
                """
                UPDATE pw_managed_session
                SET last_qualifying_activity_at =
                        MAX(last_qualifying_activity_at, ?),
                    updated_at = MAX(updated_at, ?)
                WHERE session_id = ?
                """,
                (activity_epoch, activity_epoch, session_id),
            )
        return self.get_session(session_id)

    def record_message(
        self,
        session_id: str,
        author: str,
        body: str,
        *,
        at: datetime | None = None,
    ) -> SessionMessage:
        author = _required_text("author", author)
        body = str(body)[: self.max_message_chars]
        created_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            row = self._require_session(connection, session_id)
            if row["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation(
                    f"cannot add a message to terminal session {session_id}"
                )
            cursor = connection.execute(
                """
                INSERT INTO pw_session_message(session_id, author, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, author, body, created_epoch),
            )
            message_id = int(cursor.lastrowid)
            connection.execute(
                """
                DELETE FROM pw_session_message
                WHERE message_id IN (
                    SELECT message_id
                    FROM pw_session_message
                    WHERE session_id = ?
                    ORDER BY created_at DESC, message_id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (session_id, self.max_recent_messages),
            )
            stored = connection.execute(
                "SELECT * FROM pw_session_message WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        assert stored is not None
        return self._message_from_row(stored)

    def recent_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[SessionMessage]:
        requested_limit = self.max_recent_messages if limit is None else limit
        if requested_limit < 0:
            raise ValueError("limit must not be negative")
        effective_limit = min(requested_limit, self.max_recent_messages)
        with self._connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_session_message
                WHERE session_id = ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
                """,
                (session_id, effective_limit),
            ).fetchall()
        # Conversation display is chronological even though the bounded tail is
        # selected from newest to oldest.
        return [self._message_from_row(row) for row in reversed(rows)]

    def evaluate_policy(
        self, session_id: str, *, now: datetime | None = None
    ) -> PolicyEvaluation:
        evaluated_at = now or _utc_now()
        now_epoch = _as_epoch(evaluated_at)
        with self._connection() as connection:
            row = self._require_session(connection, session_id)
            session = self._session_from_row(row)

            if session.state in TERMINAL_STATES:
                return PolicyEvaluation()

            absolute_deadline = session.started_at + ABSOLUTE_RUNTIME_CAP
            if now_epoch >= _as_epoch(absolute_deadline):
                return PolicyEvaluation(
                    timeout=TimeoutDecision(
                        AGENT_ABSOLUTE_RUNTIME_CAP, absolute_deadline
                    )
                )

            if session.profile == TRIAGE_PROFILE:
                triage_deadline = session.started_at + TRIAGE_WALL_LIMIT
                if now_epoch >= _as_epoch(triage_deadline):
                    return PolicyEvaluation(
                        timeout=TimeoutDecision(
                            AGENT_RUNTIME_TIMEOUT, triage_deadline
                        )
                    )
                return PolicyEvaluation()

            if session.state in ACTIVE_INACTIVITY_STATES:
                activity_anchor = session.last_qualifying_activity_at
                if (
                    session.active_interval_started_at is not None
                    and session.active_interval_started_at > activity_anchor
                ):
                    activity_anchor = session.active_interval_started_at
                inactivity_deadline = (
                    activity_anchor + ENGINEERING_INACTIVITY_LIMIT
                )
                if now_epoch >= _as_epoch(inactivity_deadline):
                    return PolicyEvaluation(
                        timeout=TimeoutDecision(
                            AGENT_INACTIVITY_TIMEOUT, inactivity_deadline
                        )
                    )

            age_seconds = max(0.0, now_epoch - _as_epoch(session.started_at))
            interval_seconds = ENGINEERING_REMINDER_INTERVAL.total_seconds()
            interval_index = int(age_seconds // interval_seconds)
            if interval_index <= 0:
                return PolicyEvaluation()
            idempotency_key = self.reminder_idempotency_key(
                session_id, interval_index
            )
            delivered = connection.execute(
                """
                SELECT 1 FROM pw_session_reminder_delivery
                WHERE session_id = ? AND interval_index = ?
                """,
                (session_id, interval_index),
            ).fetchone()
            if delivered is not None:
                return PolicyEvaluation()
            return PolicyEvaluation(
                reminder=ReminderDue(
                    session_id=session_id,
                    interval_index=interval_index,
                    due_at=(
                        session.started_at
                        + ENGINEERING_REMINDER_INTERVAL * interval_index
                    ),
                    idempotency_key=idempotency_key,
                )
            )

    @staticmethod
    def reminder_idempotency_key(session_id: str, interval_index: int) -> str:
        if interval_index <= 0:
            raise ValueError("interval_index must be positive")
        return f"engineering-session-reminder:{session_id}:{interval_index}"

    def mark_reminder_delivered(
        self,
        session_id: str,
        interval_index: int,
        *,
        delivered_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Record one successful reminder delivery, idempotently."""
        expected_key = self.reminder_idempotency_key(
            session_id, interval_index
        )
        if idempotency_key is not None and idempotency_key != expected_key:
            raise ValueError("idempotency key does not match reminder interval")
        delivered = delivered_at or _utc_now()
        delivered_epoch = _as_epoch(delivered)
        with self._connection() as connection, connection:
            row = self._require_session(connection, session_id)
            if row["profile"] != ENGINEERING_PROFILE:
                raise InvalidSessionOperation(
                    "triage sessions do not have engineering reminders"
                )
            due_epoch = row["started_at"] + (
                ENGINEERING_REMINDER_INTERVAL.total_seconds() * interval_index
            )
            if delivered_epoch < due_epoch:
                raise InvalidSessionOperation(
                    "cannot mark a reminder delivered before its interval is due"
                )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO pw_session_reminder_delivery(
                    session_id, interval_index, idempotency_key, delivered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (session_id, interval_index, expected_key, delivered_epoch),
            )
            return cursor.rowcount == 1

    def request_cancellation(
        self,
        session_id: str,
        requested_by: str,
        *,
        requested_at: datetime | None = None,
        request_id: str | None = None,
    ) -> ControlIntent:
        return self._request_control(
            session_id,
            "cancel",
            requested_by,
            requested_at=requested_at,
            request_id=request_id,
        )

    def request_kill(
        self,
        session_id: str,
        requested_by: str,
        *,
        requested_at: datetime | None = None,
        request_id: str | None = None,
    ) -> ControlIntent:
        return self._request_control(
            session_id,
            "kill",
            requested_by,
            requested_at=requested_at,
            request_id=request_id,
        )

    def _request_control(
        self,
        session_id: str,
        action: str,
        requested_by: str,
        *,
        requested_at: datetime | None,
        request_id: str | None,
    ) -> ControlIntent:
        requested_by = _required_text("requested_by", requested_by)
        request_id = request_id or str(uuid.uuid4())
        request_id = _required_text("request_id", request_id)
        requested_epoch = _as_epoch(requested_at or _utc_now())
        with self._connection() as connection, connection:
            session = self._require_session(connection, session_id)
            if session["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation(
                    f"cannot request {action} for terminal session {session_id}"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO pw_session_control_intent(
                        request_id, session_id, action, requested_by, requested_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        action,
                        requested_by,
                        requested_epoch,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT * FROM pw_session_control_intent
                    WHERE request_id = ?
                    """,
                    (request_id,),
                ).fetchone()
                if (
                    existing is None
                    or existing["session_id"] != session_id
                    or existing["action"] != action
                    or existing["requested_by"] != requested_by
                ):
                    raise InvalidSessionOperation(
                        f"control request id {request_id!r} was already used"
                    )
                return self._control_from_row(existing)
            row = connection.execute(
                "SELECT * FROM pw_session_control_intent WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        assert row is not None
        return self._control_from_row(row)

    def confirm_cancellation(
        self,
        session_id: str,
        request_id: str,
        confirmed_by: str,
        *,
        confirmed_at: datetime | None = None,
    ) -> ControlIntent:
        return self._confirm_control(
            session_id,
            request_id,
            "cancel",
            confirmed_by,
            confirmed_at=confirmed_at,
        )

    def confirm_kill(
        self,
        session_id: str,
        request_id: str,
        confirmed_by: str,
        *,
        confirmed_at: datetime | None = None,
    ) -> ControlIntent:
        return self._confirm_control(
            session_id,
            request_id,
            "kill",
            confirmed_by,
            confirmed_at=confirmed_at,
        )

    def _confirm_control(
        self,
        session_id: str,
        request_id: str,
        action: str,
        confirmed_by: str,
        *,
        confirmed_at: datetime | None,
    ) -> ControlIntent:
        confirmed_by = _required_text("confirmed_by", confirmed_by)
        confirmed_epoch = _as_epoch(confirmed_at or _utc_now())
        with self._connection() as connection, connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                """
                SELECT * FROM pw_session_control_intent
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if (
                row is None
                or row["session_id"] != session_id
                or row["action"] != action
            ):
                raise InvalidSessionOperation(
                    f"no matching {action} request to confirm"
                )
            if row["confirmed_at"] is None:
                connection.execute(
                    """
                    UPDATE pw_session_control_intent
                    SET confirmed_by = ?, confirmed_at = ?
                    WHERE request_id = ? AND confirmed_at IS NULL
                    """,
                    (confirmed_by, confirmed_epoch, request_id),
                )
                row = connection.execute(
                    """
                    SELECT * FROM pw_session_control_intent
                    WHERE request_id = ?
                    """,
                    (request_id,),
                ).fetchone()
        assert row is not None
        return self._control_from_row(row)

    def list_control_intents(self, session_id: str) -> list[ControlIntent]:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_session_control_intent
                WHERE session_id = ?
                ORDER BY requested_at, request_id
                """,
                (session_id,),
            ).fetchall()
        return [self._control_from_row(row) for row in rows]
