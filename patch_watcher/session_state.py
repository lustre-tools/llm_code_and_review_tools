"""Durable, side-effect-free state for managed Patch Watcher sessions.

This module deliberately stops at recording controller intent.  In particular,
confirming a cancellation or kill request never sends a signal and never marks
the session terminal; a separate runner/controller must perform and reconcile
that work.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


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
    revision: str | None = None
    patchset: int | None = None


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
    status: str = "recorded"
    detail: dict | None = None
    executed_at: datetime | None = None
    failure_code: str | None = None
    failure_summary: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


@dataclass(frozen=True)
class SessionEvent:
    event_id: int
    session_id: str
    event_type: str
    payload: dict
    idempotency_key: str | None
    created_at: datetime


@dataclass(frozen=True)
class OutboundGuidance:
    guidance_id: str
    session_id: str
    body: str
    status: str
    idempotency_key: str
    created_at: datetime
    claimed_by: str | None
    claimed_at: datetime | None
    delivered_at: datetime | None
    failed_at: datetime | None
    failure_summary: str | None


@dataclass(frozen=True)
class RunnerTransport:
    session_id: str
    transport: str
    transport_session_id: str
    pid: int
    process_started_at: datetime
    process_fingerprint: str
    adoption_state: str
    attached_at: datetime
    adopted_at: datetime | None


@dataclass(frozen=True)
class HumanQuestion:
    question_id: str
    session_id: str
    question: str
    status: str
    asked_at: datetime
    answered_by: str | None
    answer: str | None
    answered_at: datetime | None


@dataclass(frozen=True)
class TerminalResult:
    session_id: str
    state: str
    result: dict
    failure_code: str | None
    failure_summary: str | None
    finished_at: datetime


@dataclass(frozen=True)
class OwnedResource:
    resource_id: str
    session_id: str
    owner_id: str
    resource_type: str
    external_id: str
    state: str
    metadata: dict
    created_at: datetime
    cleanup_requested_at: datetime | None
    cleanup_completed_at: datetime | None
    cleanup_failure: str | None


@dataclass(frozen=True)
class DeliveryRecord:
    idempotency_key: str
    session_id: str
    kind: str
    status: str
    payload: dict
    created_at: datetime
    delivered_at: datetime | None
    failed_at: datetime | None
    failure_summary: str | None


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


def _json_text(name: str, value: Any, *, maximum_bytes: int = 256_000) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} exceeds {maximum_bytes // 1_000} KiB")
    return encoded


class SessionStateStore:
    """SQLite-backed state and policy evaluation for managed sessions."""

    SCHEMA_VERSION = 4

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
        4: (
            "ALTER TABLE pw_managed_session ADD COLUMN revision TEXT",
            "ALTER TABLE pw_managed_session ADD COLUMN patchset INTEGER",
            """
            CREATE TRIGGER pw_one_active_session_per_patch_insert
            BEFORE INSERT ON pw_managed_session
            WHEN NEW.state NOT IN (
                'succeeded', 'failed', 'cancelled', 'stale', 'resource_exhausted'
            ) AND EXISTS (
                SELECT 1 FROM pw_managed_session
                WHERE patch_id = NEW.patch_id
                  AND state NOT IN (
                    'succeeded', 'failed', 'cancelled', 'stale',
                    'resource_exhausted'
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'active session already exists for patch');
            END
            """,
            """
            CREATE TRIGGER pw_one_active_session_per_patch_update
            BEFORE UPDATE OF patch_id, state ON pw_managed_session
            WHEN NEW.state NOT IN (
                'succeeded', 'failed', 'cancelled', 'stale', 'resource_exhausted'
            ) AND EXISTS (
                SELECT 1 FROM pw_managed_session
                WHERE patch_id = NEW.patch_id
                  AND session_id <> NEW.session_id
                  AND state NOT IN (
                    'succeeded', 'failed', 'cancelled', 'stale',
                    'resource_exhausted'
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'active session already exists for patch');
            END
            """,
            """
            CREATE INDEX pw_managed_session_patch_state_idx
            ON pw_managed_session(patch_id, state)
            """,
            "ALTER TABLE pw_session_control_intent RENAME TO pw_session_control_intent_v2",
            """
            CREATE TABLE pw_session_control_intent (
                request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                action TEXT NOT NULL CHECK (
                    action IN ('pause', 'interrupt', 'cancel', 'kill', 'follow_up')
                ),
                requested_by TEXT NOT NULL,
                requested_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'recorded' CHECK (
                    status IN ('recorded', 'confirmed', 'executed', 'failed')
                ),
                detail_json TEXT NOT NULL DEFAULT '{}',
                confirmed_by TEXT,
                confirmed_at REAL,
                executed_at REAL,
                failure_code TEXT,
                failure_summary TEXT,
                confirmation_token_hash TEXT,
                confirmation_expires_at REAL,
                confirmation_used_at REAL,
                CHECK (
                    (confirmed_by IS NULL AND confirmed_at IS NULL) OR
                    (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
                )
            )
            """,
            """
            INSERT INTO pw_session_control_intent(
                request_id, session_id, action, requested_by, requested_at,
                status, confirmed_by, confirmed_at
            )
            SELECT request_id, session_id, action, requested_by, requested_at,
                   CASE WHEN confirmed_at IS NULL THEN 'recorded' ELSE 'confirmed' END,
                   confirmed_by, confirmed_at
            FROM pw_session_control_intent_v2
            """,
            "DROP TABLE pw_session_control_intent_v2",
            """
            CREATE INDEX pw_session_control_intent_session_idx_v4
            ON pw_session_control_intent(session_id, requested_at DESC)
            """,
            """
            CREATE TABLE pw_session_event (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE INDEX pw_session_event_stream_idx
            ON pw_session_event(session_id, event_id)
            """,
            """
            CREATE TRIGGER pw_session_event_no_update
            BEFORE UPDATE ON pw_session_event
            BEGIN
                SELECT RAISE(ABORT, 'session events are append-only');
            END
            """,
            """
            CREATE TRIGGER pw_session_event_no_delete
            BEFORE DELETE ON pw_session_event
            BEGIN
                SELECT RAISE(ABORT, 'session events are append-only');
            END
            """,
            """
            CREATE TABLE pw_outbound_guidance (
                guidance_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                body TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'delivered', 'failed')
                ),
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                delivered_at REAL,
                failed_at REAL,
                failure_summary TEXT,
                CHECK (
                    (claimed_by IS NULL AND claimed_at IS NULL) OR
                    (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE INDEX pw_outbound_guidance_pending_idx
            ON pw_outbound_guidance(session_id, status, created_at)
            """,
            """
            CREATE TABLE pw_runner_transport (
                session_id TEXT PRIMARY KEY REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                transport TEXT NOT NULL,
                transport_session_id TEXT NOT NULL,
                pid INTEGER NOT NULL CHECK (pid > 0),
                process_started_at REAL NOT NULL,
                process_fingerprint TEXT NOT NULL,
                adoption_state TEXT NOT NULL CHECK (
                    adoption_state IN ('attached', 'adopted', 'lost')
                ),
                attached_at REAL NOT NULL,
                adopted_at REAL,
                updated_at REAL NOT NULL,
                UNIQUE(transport, transport_session_id)
            )
            """,
            """
            CREATE TABLE pw_human_question (
                question_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                question TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('open', 'answered', 'dismissed')
                ),
                asked_at REAL NOT NULL,
                answered_by TEXT,
                answer TEXT,
                answered_at REAL
            )
            """,
            """
            CREATE UNIQUE INDEX pw_one_open_question_per_session
            ON pw_human_question(session_id) WHERE status = 'open'
            """,
            """
            CREATE TABLE pw_terminal_result (
                session_id TEXT PRIMARY KEY REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                state TEXT NOT NULL,
                result_json TEXT NOT NULL,
                failure_code TEXT,
                failure_summary TEXT,
                finished_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE pw_owned_resource (
                resource_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                owner_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('active', 'cleanup_pending', 'cleaned', 'cleanup_failed')
                ),
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                cleanup_requested_at REAL,
                cleanup_completed_at REAL,
                cleanup_failure TEXT,
                UNIQUE(owner_id, resource_type, external_id)
            )
            """,
            """
            CREATE INDEX pw_owned_resource_cleanup_idx
            ON pw_owned_resource(owner_id, state)
            """,
            """
            CREATE TABLE pw_delivery_ledger (
                idempotency_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES pw_managed_session(session_id)
                    ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'delivered', 'failed')
                ),
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                delivered_at REAL,
                failed_at REAL,
                failure_summary TEXT
            )
            """,
            """
            CREATE INDEX pw_delivery_ledger_session_idx
            ON pw_delivery_ledger(session_id, kind, status)
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
            revision=row["revision"] if "revision" in row.keys() else None,
            patchset=row["patchset"] if "patchset" in row.keys() else None,
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
        executed_at = row["executed_at"] if "executed_at" in row.keys() else None
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
            status=row["status"] if "status" in row.keys() else (
                "confirmed" if confirmed_at is not None else "recorded"
            ),
            detail=(
                json.loads(row["detail_json"])
                if "detail_json" in row.keys()
                else {}
            ),
            executed_at=(
                _as_datetime(executed_at) if executed_at is not None else None
            ),
            failure_code=(
                row["failure_code"] if "failure_code" in row.keys() else None
            ),
            failure_summary=(
                row["failure_summary"]
                if "failure_summary" in row.keys()
                else None
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
    def _event_from_row(row: sqlite3.Row) -> SessionEvent:
        return SessionEvent(
            event_id=row["event_id"],
            session_id=row["session_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            idempotency_key=row["idempotency_key"],
            created_at=_as_datetime(row["created_at"]),
        )

    @staticmethod
    def _guidance_from_row(row: sqlite3.Row) -> OutboundGuidance:
        return OutboundGuidance(
            guidance_id=row["guidance_id"],
            session_id=row["session_id"],
            body=row["body"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            created_at=_as_datetime(row["created_at"]),
            claimed_by=row["claimed_by"],
            claimed_at=(
                _as_datetime(row["claimed_at"])
                if row["claimed_at"] is not None
                else None
            ),
            delivered_at=(
                _as_datetime(row["delivered_at"])
                if row["delivered_at"] is not None
                else None
            ),
            failed_at=(
                _as_datetime(row["failed_at"])
                if row["failed_at"] is not None
                else None
            ),
            failure_summary=row["failure_summary"],
        )

    @staticmethod
    def _transport_from_row(row: sqlite3.Row) -> RunnerTransport:
        return RunnerTransport(
            session_id=row["session_id"],
            transport=row["transport"],
            transport_session_id=row["transport_session_id"],
            pid=row["pid"],
            process_started_at=_as_datetime(row["process_started_at"]),
            process_fingerprint=row["process_fingerprint"],
            adoption_state=row["adoption_state"],
            attached_at=_as_datetime(row["attached_at"]),
            adopted_at=(
                _as_datetime(row["adopted_at"])
                if row["adopted_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _question_from_row(row: sqlite3.Row) -> HumanQuestion:
        return HumanQuestion(
            question_id=row["question_id"],
            session_id=row["session_id"],
            question=row["question"],
            status=row["status"],
            asked_at=_as_datetime(row["asked_at"]),
            answered_by=row["answered_by"],
            answer=row["answer"],
            answered_at=(
                _as_datetime(row["answered_at"])
                if row["answered_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _terminal_from_row(row: sqlite3.Row) -> TerminalResult:
        return TerminalResult(
            session_id=row["session_id"],
            state=row["state"],
            result=json.loads(row["result_json"]),
            failure_code=row["failure_code"],
            failure_summary=row["failure_summary"],
            finished_at=_as_datetime(row["finished_at"]),
        )

    @staticmethod
    def _resource_from_row(row: sqlite3.Row) -> OwnedResource:
        return OwnedResource(
            resource_id=row["resource_id"],
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            resource_type=row["resource_type"],
            external_id=row["external_id"],
            state=row["state"],
            metadata=json.loads(row["metadata_json"]),
            created_at=_as_datetime(row["created_at"]),
            cleanup_requested_at=(
                _as_datetime(row["cleanup_requested_at"])
                if row["cleanup_requested_at"] is not None
                else None
            ),
            cleanup_completed_at=(
                _as_datetime(row["cleanup_completed_at"])
                if row["cleanup_completed_at"] is not None
                else None
            ),
            cleanup_failure=row["cleanup_failure"],
        )

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
        return DeliveryRecord(
            idempotency_key=row["idempotency_key"],
            session_id=row["session_id"],
            kind=row["kind"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            created_at=_as_datetime(row["created_at"]),
            delivered_at=(
                _as_datetime(row["delivered_at"])
                if row["delivered_at"] is not None
                else None
            ),
            failed_at=(
                _as_datetime(row["failed_at"])
                if row["failed_at"] is not None
                else None
            ),
            failure_summary=row["failure_summary"],
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
        revision: str | None = None,
        patchset: int | None = None,
    ) -> ManagedSession:
        session_id = _required_text("session_id", session_id)
        patch_id = _required_text("patch_id", patch_id)
        run_id = _required_text("run_id", run_id)
        if profile not in {TRIAGE_PROFILE, ENGINEERING_PROFILE}:
            raise ValueError("profile must be 'triage' or 'engineering'")
        if state not in SESSION_STATES:
            raise ValueError(f"unknown session state: {state}")
        pid = _validate_pid(pid)
        revision = (
            _required_text("revision", revision) if revision is not None else None
        )
        if patchset is not None and (
            isinstance(patchset, bool) or not isinstance(patchset, int) or patchset <= 0
        ):
            raise ValueError("patchset must be a positive integer or None")
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
                        created_at, updated_at, revision, patchset
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        revision,
                        patchset,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "active session already exists for patch" in str(exc):
                raise SessionAlreadyExists(
                    f"patch {patch_id!r} already has an active session"
                ) from exc
            raise SessionAlreadyExists(
                f"session {session_id!r} or run {run_id!r} is already registered"
            ) from exc
        return self.get_session(session_id)

    def register_pinned_session(
        self,
        session_id: str,
        *,
        patch_id: str,
        run_id: str,
        revision: str,
        patchset: int,
        profile: str,
        state: str = "preparing",
        pid: int | None = None,
        started_at: datetime | None = None,
    ) -> ManagedSession:
        """Atomically reserve a patch and pin the exact Gerrit revision."""
        return self.register_session(
            session_id,
            patch_id=patch_id,
            run_id=run_id,
            revision=revision,
            patchset=patchset,
            profile=profile,
            state=state,
            pid=pid,
            started_at=started_at,
        )

    def assert_pinned_revision(
        self, session_id: str, *, revision: str, patchset: int
    ) -> ManagedSession:
        session = self.get_session(session_id)
        if session.revision != revision or session.patchset != patchset:
            raise InvalidSessionOperation(
                f"session {session_id} is pinned to revision "
                f"{session.revision!r} patchset {session.patchset!r}"
            )
        return session

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

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        idempotency_key: str | None = None,
        at: datetime | None = None,
    ) -> SessionEvent:
        """Append an immutable event, or return the matching idempotent event."""
        event_type = _required_text("event_type", event_type)
        payload_json = _json_text("event payload", payload or {})
        idempotency_key = (
            _required_text("idempotency_key", idempotency_key)
            if idempotency_key is not None
            else None
        )
        created_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            self._require_session(connection, session_id)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO pw_session_event(
                        session_id, event_type, payload_json,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        event_type,
                        payload_json,
                        idempotency_key,
                        created_epoch,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM pw_session_event WHERE event_id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT * FROM pw_session_event WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if (
                    row is None
                    or row["session_id"] != session_id
                    or row["event_type"] != event_type
                    or row["payload_json"] != payload_json
                ):
                    raise InvalidSessionOperation(
                        f"event idempotency key {idempotency_key!r} was already used"
                    )
        assert row is not None
        return self._event_from_row(row)

    def list_events(
        self, session_id: str, *, after_event_id: int = 0
    ) -> list[SessionEvent]:
        if after_event_id < 0:
            raise ValueError("after_event_id must not be negative")
        with self._connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_session_event
                WHERE session_id = ? AND event_id > ?
                ORDER BY event_id
                """,
                (session_id, after_event_id),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def enqueue_guidance(
        self,
        session_id: str,
        body: str,
        *,
        idempotency_key: str,
        guidance_id: str | None = None,
        at: datetime | None = None,
    ) -> OutboundGuidance:
        body = _required_text("body", body)[: self.max_message_chars]
        idempotency_key = _required_text("idempotency_key", idempotency_key)
        guidance_id = _required_text(
            "guidance_id", guidance_id or str(uuid.uuid4())
        )
        created_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            session = self._require_session(connection, session_id)
            if session["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation(
                    f"cannot guide terminal session {session_id}"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO pw_outbound_guidance(
                        guidance_id, session_id, body, status,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        guidance_id,
                        session_id,
                        body,
                        idempotency_key,
                        created_epoch,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM pw_outbound_guidance WHERE guidance_id = ?",
                    (guidance_id,),
                ).fetchone()
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT * FROM pw_outbound_guidance
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if (
                    row is None
                    or row["session_id"] != session_id
                    or row["body"] != body
                ):
                    raise InvalidSessionOperation(
                        f"guidance idempotency key {idempotency_key!r} was already used"
                    )
        assert row is not None
        return self._guidance_from_row(row)

    def claim_next_guidance(
        self,
        session_id: str,
        consumer_id: str,
        *,
        at: datetime | None = None,
    ) -> OutboundGuidance | None:
        consumer_id = _required_text("consumer_id", consumer_id)
        claimed_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_session(connection, session_id)
                # A controller restart keeps the same durable consumer identity.
                # Return its in-flight item rather than duplicating delivery or
                # allowing another consumer to claim it.
                row = connection.execute(
                    """
                    SELECT * FROM pw_outbound_guidance
                    WHERE session_id = ? AND status = 'pending'
                      AND claimed_by = ?
                    ORDER BY created_at, guidance_id LIMIT 1
                    """,
                    (session_id, consumer_id),
                ).fetchone()
                if row is not None:
                    connection.commit()
                    return self._guidance_from_row(row)
                row = connection.execute(
                    """
                    SELECT * FROM pw_outbound_guidance
                    WHERE session_id = ? AND status = 'pending'
                      AND claimed_by IS NULL
                    ORDER BY created_at, guidance_id LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                cursor = connection.execute(
                    """
                    UPDATE pw_outbound_guidance
                    SET claimed_by = ?, claimed_at = ?
                    WHERE guidance_id = ? AND status = 'pending'
                      AND claimed_by IS NULL
                    """,
                    (consumer_id, claimed_epoch, row["guidance_id"]),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                row = connection.execute(
                    "SELECT * FROM pw_outbound_guidance WHERE guidance_id = ?",
                    (row["guidance_id"],),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._guidance_from_row(row)

    def finish_guidance_delivery(
        self,
        guidance_id: str,
        consumer_id: str,
        *,
        delivered: bool,
        at: datetime | None = None,
        failure_summary: str | None = None,
    ) -> OutboundGuidance:
        consumer_id = _required_text("consumer_id", consumer_id)
        finished_epoch = _as_epoch(at or _utc_now())
        status = "delivered" if delivered else "failed"
        with self._connection() as connection, connection:
            row = connection.execute(
                "SELECT * FROM pw_outbound_guidance WHERE guidance_id = ?",
                (guidance_id,),
            ).fetchone()
            if row is None:
                raise InvalidSessionOperation("unknown outbound guidance")
            if row["status"] == status:
                return self._guidance_from_row(row)
            if row["status"] != "pending" or row["claimed_by"] != consumer_id:
                raise InvalidSessionOperation(
                    "guidance is not pending and claimed by this consumer"
                )
            connection.execute(
                """
                UPDATE pw_outbound_guidance
                SET status = ?, delivered_at = ?, failed_at = ?,
                    failure_summary = ?
                WHERE guidance_id = ?
                """,
                (
                    status,
                    finished_epoch if delivered else None,
                    None if delivered else finished_epoch,
                    None if delivered else str(failure_summary or "delivery failed")[:2000],
                    guidance_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_outbound_guidance WHERE guidance_id = ?",
                (guidance_id,),
            ).fetchone()
        assert row is not None
        return self._guidance_from_row(row)

    def list_guidance(self, session_id: str) -> list[OutboundGuidance]:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_outbound_guidance
                WHERE session_id = ? ORDER BY created_at, guidance_id
                """,
                (session_id,),
            ).fetchall()
        return [self._guidance_from_row(row) for row in rows]

    def attach_runner_transport(
        self,
        session_id: str,
        *,
        transport: str,
        transport_session_id: str,
        pid: int,
        process_started_at: datetime,
        process_fingerprint: str,
        attached_at: datetime | None = None,
    ) -> RunnerTransport:
        transport = _required_text("transport", transport)
        transport_session_id = _required_text(
            "transport_session_id", transport_session_id
        )
        pid = _validate_pid(pid)
        assert pid is not None
        process_fingerprint = _required_text(
            "process_fingerprint", process_fingerprint
        )
        process_started_epoch = _as_epoch(process_started_at)
        attached_epoch = _as_epoch(attached_at or _utc_now())
        with self._connection() as connection, connection:
            session = self._require_session(connection, session_id)
            if session["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation(
                    f"cannot attach transport to terminal session {session_id}"
                )
            existing = connection.execute(
                "SELECT * FROM pw_runner_transport WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            identity = (
                transport,
                transport_session_id,
                pid,
                process_started_epoch,
                process_fingerprint,
            )
            if existing is not None:
                existing_identity = (
                    existing["transport"],
                    existing["transport_session_id"],
                    existing["pid"],
                    existing["process_started_at"],
                    existing["process_fingerprint"],
                )
                if existing_identity != identity:
                    raise InvalidSessionOperation(
                        "runner transport identity cannot be replaced"
                    )
                return self._transport_from_row(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO pw_runner_transport(
                        session_id, transport, transport_session_id, pid,
                        process_started_at, process_fingerprint,
                        adoption_state, attached_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'attached', ?, ?)
                    """,
                    (
                        session_id,
                        *identity,
                        attached_epoch,
                        attached_epoch,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidSessionOperation(
                    "transport session is already bound to another run"
                ) from exc
            connection.execute(
                "UPDATE pw_managed_session SET pid = ? WHERE session_id = ?",
                (pid, session_id),
            )
            row = connection.execute(
                "SELECT * FROM pw_runner_transport WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return self._transport_from_row(row)

    def adopt_runner_transport(
        self,
        session_id: str,
        *,
        process_fingerprint: str,
        at: datetime | None = None,
    ) -> RunnerTransport:
        """Adopt only an exact previously-recorded process identity after restart."""
        process_fingerprint = _required_text(
            "process_fingerprint", process_fingerprint
        )
        adopted_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            session = self._require_session(connection, session_id)
            if session["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation("cannot adopt a terminal session")
            row = connection.execute(
                "SELECT * FROM pw_runner_transport WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(
                row["process_fingerprint"], process_fingerprint
            ):
                raise InvalidSessionOperation("runner process fingerprint mismatch")
            connection.execute(
                """
                UPDATE pw_runner_transport
                SET adoption_state = 'adopted', adopted_at = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (adopted_epoch, adopted_epoch, session_id),
            )
            row = connection.execute(
                "SELECT * FROM pw_runner_transport WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return self._transport_from_row(row)

    def get_runner_transport(self, session_id: str) -> RunnerTransport | None:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT * FROM pw_runner_transport WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._transport_from_row(row) if row is not None else None

    def ask_human(
        self,
        session_id: str,
        question: str,
        *,
        question_id: str | None = None,
        at: datetime | None = None,
    ) -> HumanQuestion:
        question = _required_text("question", question)[: self.max_message_chars]
        question_id = _required_text(
            "question_id", question_id or str(uuid.uuid4())
        )
        asked_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            session = self._require_session(connection, session_id)
            if session["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation("terminal session cannot ask a question")
            try:
                connection.execute(
                    """
                    INSERT INTO pw_human_question(
                        question_id, session_id, question, status, asked_at
                    ) VALUES (?, ?, ?, 'open', ?)
                    """,
                    (question_id, session_id, question, asked_epoch),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidSessionOperation(
                    "session already has an open human question"
                ) from exc
            connection.execute(
                """
                UPDATE pw_managed_session
                SET state = 'waiting_human', active_interval_started_at = NULL,
                    state_changed_at = ?, updated_at = MAX(updated_at, ?)
                WHERE session_id = ?
                """,
                (asked_epoch, asked_epoch, session_id),
            )
            row = connection.execute(
                "SELECT * FROM pw_human_question WHERE question_id = ?",
                (question_id,),
            ).fetchone()
        assert row is not None
        return self._question_from_row(row)

    def answer_human_question(
        self,
        session_id: str,
        question_id: str,
        *,
        answered_by: str,
        answer: str,
        at: datetime | None = None,
    ) -> tuple[HumanQuestion, OutboundGuidance]:
        answered_by = _required_text("answered_by", answered_by)
        answer = _required_text("answer", answer)[: self.max_message_chars]
        answered_epoch = _as_epoch(at or _utc_now())
        guidance_key = f"human-question-answer:{question_id}"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = self._require_session(connection, session_id)
                if session["state"] != "waiting_human":
                    raise InvalidSessionOperation(
                        "session is not waiting for human input"
                    )
                question_row = connection.execute(
                    """
                    SELECT * FROM pw_human_question
                    WHERE question_id = ? AND session_id = ?
                    """,
                    (question_id, session_id),
                ).fetchone()
                if question_row is None or question_row["status"] != "open":
                    raise InvalidSessionOperation("human question is not open")
                connection.execute(
                    """
                    UPDATE pw_human_question
                    SET status = 'answered', answered_by = ?, answer = ?,
                        answered_at = ?
                    WHERE question_id = ? AND status = 'open'
                    """,
                    (answered_by, answer, answered_epoch, question_id),
                )
                guidance_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO pw_outbound_guidance(
                        guidance_id, session_id, body, status,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        guidance_id,
                        session_id,
                        answer,
                        guidance_key,
                        answered_epoch,
                    ),
                )
                connection.execute(
                    """
                    UPDATE pw_managed_session
                    SET state = 'running', active_interval_started_at = ?,
                        state_changed_at = ?, updated_at = MAX(updated_at, ?)
                    WHERE session_id = ?
                    """,
                    (answered_epoch, answered_epoch, answered_epoch, session_id),
                )
                question_row = connection.execute(
                    "SELECT * FROM pw_human_question WHERE question_id = ?",
                    (question_id,),
                ).fetchone()
                guidance_row = connection.execute(
                    "SELECT * FROM pw_outbound_guidance WHERE guidance_id = ?",
                    (guidance_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert question_row is not None and guidance_row is not None
        return (
            self._question_from_row(question_row),
            self._guidance_from_row(guidance_row),
        )

    def list_human_questions(self, session_id: str) -> list[HumanQuestion]:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_human_question
                WHERE session_id = ? ORDER BY asked_at, question_id
                """,
                (session_id,),
            ).fetchall()
        return [self._question_from_row(row) for row in rows]

    def finish_session(
        self,
        session_id: str,
        state: str,
        *,
        result: dict | None = None,
        failure_code: str | None = None,
        failure_summary: str | None = None,
        finished_at: datetime | None = None,
        request_resource_cleanup: bool = True,
    ) -> TerminalResult:
        if state not in TERMINAL_STATES:
            raise ValueError("finish state must be terminal")
        if state in {"failed", "resource_exhausted", "stale"} and not failure_code:
            raise ValueError(f"{state} result requires failure_code")
        result_json = _json_text("terminal result", result or {})
        failure_code = (
            _required_text("failure_code", failure_code)
            if failure_code is not None
            else None
        )
        failure_summary = (
            str(failure_summary)[:2000] if failure_summary is not None else None
        )
        finished_epoch = _as_epoch(finished_at or _utc_now())
        with self._connection() as connection, connection:
            session = self._require_session(connection, session_id)
            existing = connection.execute(
                "SELECT * FROM pw_terminal_result WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["state"] != state
                    or existing["result_json"] != result_json
                    or existing["failure_code"] != failure_code
                    or existing["failure_summary"] != failure_summary
                ):
                    raise InvalidSessionOperation(
                        "terminal result is immutable once recorded"
                    )
                return self._terminal_from_row(existing)
            if session["state"] in TERMINAL_STATES and session["state"] != state:
                raise InvalidSessionOperation(
                    f"terminal session is already {session['state']}"
                )
            connection.execute(
                """
                INSERT INTO pw_terminal_result(
                    session_id, state, result_json, failure_code,
                    failure_summary, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    state,
                    result_json,
                    failure_code,
                    failure_summary,
                    finished_epoch,
                ),
            )
            connection.execute(
                """
                UPDATE pw_managed_session
                SET state = ?, active_interval_started_at = NULL,
                    state_changed_at = ?, updated_at = MAX(updated_at, ?)
                WHERE session_id = ?
                """,
                (state, finished_epoch, finished_epoch, session_id),
            )
            if request_resource_cleanup:
                connection.execute(
                    """
                    UPDATE pw_owned_resource
                    SET state = 'cleanup_pending', cleanup_requested_at = ?
                    WHERE session_id = ? AND state = 'active'
                    """,
                    (finished_epoch, session_id),
                )
            row = connection.execute(
                "SELECT * FROM pw_terminal_result WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return self._terminal_from_row(row)

    def get_terminal_result(self, session_id: str) -> TerminalResult | None:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT * FROM pw_terminal_result WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._terminal_from_row(row) if row is not None else None

    def mark_stale_for_revision(
        self,
        session_id: str,
        *,
        observed_revision: str,
        observed_patchset: int,
        at: datetime | None = None,
    ) -> TerminalResult | None:
        session = self.get_session(session_id)
        if (
            session.revision == observed_revision
            and session.patchset == observed_patchset
        ):
            return None
        return self.finish_session(
            session_id,
            "stale",
            result={
                "pinned_revision": session.revision,
                "pinned_patchset": session.patchset,
                "observed_revision": observed_revision,
                "observed_patchset": observed_patchset,
            },
            failure_code="patch_revision_changed",
            failure_summary="Gerrit revision changed while the run was active",
            finished_at=at,
        )

    def register_owned_resource(
        self,
        session_id: str,
        *,
        owner_id: str,
        resource_type: str,
        external_id: str,
        metadata: dict | None = None,
        resource_id: str | None = None,
        at: datetime | None = None,
    ) -> OwnedResource:
        owner_id = _required_text("owner_id", owner_id)
        resource_type = _required_text("resource_type", resource_type)
        external_id = _required_text("external_id", external_id)
        resource_id = _required_text(
            "resource_id", resource_id or str(uuid.uuid4())
        )
        metadata_json = _json_text("resource metadata", metadata or {})
        created_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            session = self._require_session(connection, session_id)
            if session["state"] in TERMINAL_STATES:
                raise InvalidSessionOperation(
                    "cannot register a resource after session termination"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO pw_owned_resource(
                        resource_id, session_id, owner_id, resource_type,
                        external_id, state, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        resource_id,
                        session_id,
                        owner_id,
                        resource_type,
                        external_id,
                        metadata_json,
                        created_epoch,
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT * FROM pw_owned_resource
                    WHERE owner_id = ? AND resource_type = ? AND external_id = ?
                    """,
                    (owner_id, resource_type, external_id),
                ).fetchone()
                if (
                    row is None
                    or row["session_id"] != session_id
                    or row["metadata_json"] != metadata_json
                ):
                    raise InvalidSessionOperation(
                        "resource ownership key is already registered"
                    )
                return self._resource_from_row(row)
            row = connection.execute(
                "SELECT * FROM pw_owned_resource WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
        assert row is not None
        return self._resource_from_row(row)

    def mark_resource_cleanup(
        self,
        resource_id: str,
        *,
        succeeded: bool,
        at: datetime | None = None,
        failure_summary: str | None = None,
    ) -> OwnedResource:
        completed_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            row = connection.execute(
                "SELECT * FROM pw_owned_resource WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if row is None:
                raise InvalidSessionOperation("unknown owned resource")
            target = "cleaned" if succeeded else "cleanup_failed"
            if row["state"] == target:
                return self._resource_from_row(row)
            if row["state"] not in {"active", "cleanup_pending", "cleanup_failed"}:
                raise InvalidSessionOperation("resource cleanup is already complete")
            connection.execute(
                """
                UPDATE pw_owned_resource
                SET state = ?,
                    cleanup_requested_at = COALESCE(cleanup_requested_at, ?),
                    cleanup_completed_at = ?, cleanup_failure = ?
                WHERE resource_id = ?
                """,
                (
                    target,
                    completed_epoch,
                    completed_epoch if succeeded else None,
                    None if succeeded else str(failure_summary or "cleanup failed")[:2000],
                    resource_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_owned_resource WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
        assert row is not None
        return self._resource_from_row(row)

    def list_owned_resources(
        self, *, session_id: str | None = None, owner_id: str | None = None
    ) -> list[OwnedResource]:
        clauses: list[str] = []
        parameters: list[str] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if owner_id is not None:
            clauses.append("owner_id = ?")
            parameters.append(owner_id)
        query = "SELECT * FROM pw_owned_resource"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, resource_id"
        with self._connection() as connection:
            if session_id is not None:
                self._require_session(connection, session_id)
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._resource_from_row(row) for row in rows]

    def ensure_delivery(
        self,
        session_id: str,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict | None = None,
        at: datetime | None = None,
    ) -> DeliveryRecord:
        kind = _required_text("kind", kind)
        idempotency_key = _required_text("idempotency_key", idempotency_key)
        payload_json = _json_text("delivery payload", payload or {})
        created_epoch = _as_epoch(at or _utc_now())
        with self._connection() as connection, connection:
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO pw_delivery_ledger(
                    idempotency_key, session_id, kind, status,
                    payload_json, created_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (idempotency_key, session_id, kind, payload_json, created_epoch),
            )
            row = connection.execute(
                "SELECT * FROM pw_delivery_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if (
                row is None
                or row["session_id"] != session_id
                or row["kind"] != kind
                or row["payload_json"] != payload_json
            ):
                raise InvalidSessionOperation(
                    f"delivery key {idempotency_key!r} was already used"
                )
        return self._delivery_from_row(row)

    def finish_delivery(
        self,
        idempotency_key: str,
        *,
        delivered: bool,
        at: datetime | None = None,
        failure_summary: str | None = None,
    ) -> DeliveryRecord:
        finished_epoch = _as_epoch(at or _utc_now())
        target = "delivered" if delivered else "failed"
        with self._connection() as connection, connection:
            row = connection.execute(
                "SELECT * FROM pw_delivery_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise InvalidSessionOperation("unknown delivery key")
            if row["status"] == target:
                return self._delivery_from_row(row)
            if row["status"] != "pending":
                raise InvalidSessionOperation("delivery already has a terminal state")
            connection.execute(
                """
                UPDATE pw_delivery_ledger
                SET status = ?, delivered_at = ?, failed_at = ?,
                    failure_summary = ?
                WHERE idempotency_key = ? AND status = 'pending'
                """,
                (
                    target,
                    finished_epoch if delivered else None,
                    None if delivered else finished_epoch,
                    None if delivered else str(failure_summary or "delivery failed")[:2000],
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_delivery_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        assert row is not None
        return self._delivery_from_row(row)

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

    def request_pause(
        self,
        session_id: str,
        requested_by: str,
        *,
        requested_at: datetime | None = None,
        request_id: str | None = None,
    ) -> ControlIntent:
        return self._request_control(
            session_id,
            "pause",
            requested_by,
            requested_at=requested_at,
            request_id=request_id,
        )

    def request_interrupt(
        self,
        session_id: str,
        requested_by: str,
        *,
        requested_at: datetime | None = None,
        request_id: str | None = None,
    ) -> ControlIntent:
        return self._request_control(
            session_id,
            "interrupt",
            requested_by,
            requested_at=requested_at,
            request_id=request_id,
        )

    def request_follow_up(
        self,
        session_id: str,
        requested_by: str,
        message: str,
        *,
        requested_at: datetime | None = None,
        request_id: str | None = None,
    ) -> ControlIntent:
        message = _required_text("message", message)[: self.max_message_chars]
        return self._request_control(
            session_id,
            "follow_up",
            requested_by,
            requested_at=requested_at,
            request_id=request_id,
            detail={"message": message},
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

    def request_destructive_control(
        self,
        session_id: str,
        action: str,
        requested_by: str,
        *,
        requested_at: datetime | None = None,
        expires_in: timedelta = timedelta(minutes=30),
        request_id: str | None = None,
    ) -> tuple[ControlIntent, str]:
        """Record cancel/kill and return a one-time token; only its hash is stored."""
        if action not in {"cancel", "kill"}:
            raise ValueError("destructive action must be cancel or kill")
        if expires_in <= timedelta(0):
            raise ValueError("expires_in must be positive")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        requested = requested_at or _utc_now()
        intent = self._request_control(
            session_id,
            action,
            requested_by,
            requested_at=requested,
            request_id=request_id,
            confirmation_token_hash=token_hash,
            confirmation_expires_at=requested + expires_in,
        )
        return intent, token

    def _request_control(
        self,
        session_id: str,
        action: str,
        requested_by: str,
        *,
        requested_at: datetime | None,
        request_id: str | None,
        detail: dict | None = None,
        confirmation_token_hash: str | None = None,
        confirmation_expires_at: datetime | None = None,
    ) -> ControlIntent:
        requested_by = _required_text("requested_by", requested_by)
        request_id = request_id or str(uuid.uuid4())
        request_id = _required_text("request_id", request_id)
        requested_epoch = _as_epoch(requested_at or _utc_now())
        detail_json = _json_text("control detail", detail or {})
        confirmation_expires_epoch = (
            _as_epoch(confirmation_expires_at)
            if confirmation_expires_at is not None
            else None
        )
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
                        request_id, session_id, action, requested_by, requested_at,
                        detail_json, confirmation_token_hash,
                        confirmation_expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        action,
                        requested_by,
                        requested_epoch,
                        detail_json,
                        confirmation_token_hash,
                        confirmation_expires_epoch,
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
                    or existing["detail_json"] != detail_json
                    or existing["confirmation_token_hash"]
                    != confirmation_token_hash
                    or existing["confirmation_expires_at"]
                    != confirmation_expires_epoch
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
                    SET confirmed_by = ?, confirmed_at = ?, status = 'confirmed'
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

    def confirm_control_with_token(
        self,
        session_id: str,
        request_id: str,
        token: str,
        confirmed_by: str,
        *,
        confirmed_at: datetime | None = None,
    ) -> ControlIntent:
        """Validate a POSTed one-time token and confirm without performing I/O."""
        token = _required_text("token", token)
        confirmed_by = _required_text("confirmed_by", confirmed_by)
        confirmed = confirmed_at or _utc_now()
        confirmed_epoch = _as_epoch(confirmed)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_session(connection, session_id)
                row = connection.execute(
                    """
                    SELECT * FROM pw_session_control_intent
                    WHERE request_id = ? AND session_id = ?
                    """,
                    (request_id, session_id),
                ).fetchone()
                if row is None or row["action"] not in {"cancel", "kill"}:
                    raise InvalidSessionOperation(
                        "no matching destructive control request"
                    )
                if row["confirmation_used_at"] is not None:
                    raise InvalidSessionOperation("confirmation token was already used")
                if (
                    row["confirmation_expires_at"] is None
                    or confirmed_epoch >= row["confirmation_expires_at"]
                ):
                    raise InvalidSessionOperation("confirmation token has expired")
                stored_hash = row["confirmation_token_hash"] or ""
                if not hmac.compare_digest(stored_hash, token_hash):
                    raise InvalidSessionOperation("invalid confirmation token")
                connection.execute(
                    """
                    UPDATE pw_session_control_intent
                    SET confirmed_by = ?, confirmed_at = ?, status = 'confirmed',
                        confirmation_used_at = ?
                    WHERE request_id = ? AND confirmation_used_at IS NULL
                    """,
                    (confirmed_by, confirmed_epoch, confirmed_epoch, request_id),
                )
                row = connection.execute(
                    "SELECT * FROM pw_session_control_intent WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._control_from_row(row)

    def finish_control_intent(
        self,
        session_id: str,
        request_id: str,
        *,
        succeeded: bool,
        executed_at: datetime | None = None,
        failure_code: str | None = None,
        failure_summary: str | None = None,
    ) -> ControlIntent:
        """Record runner reconciliation; this method never signals a process."""
        executed_epoch = _as_epoch(executed_at or _utc_now())
        target = "executed" if succeeded else "failed"
        if not succeeded and not failure_code:
            raise ValueError("failed control execution requires failure_code")
        with self._connection() as connection, connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                """
                SELECT * FROM pw_session_control_intent
                WHERE request_id = ? AND session_id = ?
                """,
                (request_id, session_id),
            ).fetchone()
            if row is None:
                raise InvalidSessionOperation("unknown control request")
            if row["status"] == target:
                return self._control_from_row(row)
            if row["status"] in {"executed", "failed"}:
                raise InvalidSessionOperation("control request is already terminal")
            if row["action"] in {"cancel", "kill"} and row["status"] != "confirmed":
                raise InvalidSessionOperation(
                    "destructive control must be confirmed before execution"
                )
            connection.execute(
                """
                UPDATE pw_session_control_intent
                SET status = ?, executed_at = ?, failure_code = ?,
                    failure_summary = ?
                WHERE request_id = ?
                """,
                (
                    target,
                    executed_epoch,
                    None if succeeded else _required_text("failure_code", failure_code),
                    None if succeeded else str(failure_summary or "control failed")[:2000],
                    request_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_session_control_intent WHERE request_id = ?",
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
