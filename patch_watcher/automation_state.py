"""Durable, side-effect-free state for Patch Watcher automation.

The store records observations, decisions, claims, and action outcomes.  It
never contacts Gerrit, CI, mail, a process, or a worker.  Controllers perform
those effects and reconcile them here using idempotency keys.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


POLICY_MODES = frozenset({"disabled", "advise", "approval", "automatic"})
RESEARCH_MODES = frozenset({"disabled", "manual", "automatic"})
RESEARCH_ADMISSION_STATES = frozenset({"reserved", "registered", "released"})
TRIGGER_STATES = frozenset({"pending", "claimed", "consumed", "stale"})
RUN_ACTIVE_STATES = frozenset({"planned", "executing", "waiting_external"})
RUN_TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "ambiguous", "cancelled", "stale"}
)
RUN_STATES = RUN_ACTIVE_STATES | RUN_TERMINAL_STATES
ACTION_STATES = frozenset(
    {
        "planned",
        "executing",
        "succeeded",
        "failed",
        "ambiguous",
        "cancelled",
        "waiting_external",
    }
)
ACTION_ACTIVE_STATES = frozenset({"planned", "executing", "waiting_external"})
ACTION_TERMINAL_STATES = ACTION_STATES - ACTION_ACTIVE_STATES
BUDGET_BUCKETS = frozenset({"action", "delivery"})


class AutomationStateError(RuntimeError):
    """Base error for automation-state failures."""


class AutomationNotFound(AutomationStateError):
    """Raised when an identifier is unknown."""


class AutomationConflict(AutomationStateError):
    """Raised when an operation violates a durable invariant."""


class BudgetExhausted(AutomationConflict):
    """Raised before an action can exceed its policy snapshot budget."""


class GlobalAutomationDisabled(AutomationConflict):
    """Raised when automatic execution is attempted while globally disabled."""


@dataclass(frozen=True)
class PatchRecord:
    patch_id: str
    gerrit_url: str
    change_number: int
    current_revision: str
    current_patchset: int
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AutomationPolicy:
    patch_id: str
    mode: str
    action_budget: int
    delivery_budget: int
    updated_by: str
    updated_at: datetime


@dataclass(frozen=True)
class ResearchPolicy:
    patch_id: str
    mode: str
    run_budget: int
    updated_by: str
    updated_at: datetime

    @property
    def version(self) -> str:
        return self.updated_at.isoformat()


@dataclass(frozen=True)
class ResearchAdmission:
    """One durable per-revision budget slot reserved for a research attempt."""

    admission_id: str
    patch_id: str
    revision: str
    patchset: int
    policy_version: str
    mode: str
    slot: int
    attempt_id: str
    evidence_fingerprint: str
    state: str
    session_id: str | None
    created_at: datetime
    updated_at: datetime
    failure_summary: str | None


@dataclass(frozen=True)
class Observation:
    observation_id: str
    patch_id: str
    revision: str
    source: str
    kind: str
    fingerprint: str
    payload: dict
    observed_at: datetime


@dataclass(frozen=True)
class AutomationTrigger:
    trigger_id: str
    patch_id: str
    revision: str
    kind: str
    fingerprint: str
    payload: dict
    state: str
    created_at: datetime
    claimed_by: str | None
    claimed_at: datetime | None
    consumed_at: datetime | None


@dataclass(frozen=True)
class AutomationRun:
    run_id: str
    patch_id: str
    revision: str
    patchset: int
    trigger_id: str
    deterministic_key: str
    policy_snapshot: dict
    status: str
    action_budget: int
    action_count: int
    delivery_budget: int
    delivery_count: int
    created_at: datetime
    claimed_by: str | None
    claimed_at: datetime | None
    finished_at: datetime | None
    failure_code: str | None
    failure_summary: str | None


@dataclass(frozen=True)
class TimelineEvent:
    event_id: int
    run_id: str
    event_type: str
    payload: dict
    idempotency_key: str | None
    created_at: datetime


@dataclass(frozen=True)
class ActionAttempt:
    action_id: str
    run_id: str
    action_type: str
    budget_bucket: str
    idempotency_key: str
    status: str
    request: dict
    result: dict | None
    created_at: datetime
    claimed_by: str | None
    claimed_at: datetime | None
    finished_at: datetime | None
    failure_code: str | None
    failure_summary: str | None


@dataclass(frozen=True)
class ActionApproval:
    action_id: str
    approved_by: str
    approved_at: datetime
    expected_revision: str
    policy_snapshot: dict


@dataclass(frozen=True)
class GlobalAutomationSetting:
    enabled: bool
    changed_by: str
    reason: str
    changed_at: datetime


@dataclass(frozen=True)
class SettingAuditEvent:
    audit_id: int
    enabled: bool
    changed_by: str
    reason: str
    changed_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.timestamp()


def _datetime(value: float | int) -> datetime:
    return datetime.fromtimestamp(float(value), timezone.utc)


def _text(name: str, value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _budget(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _json(name: str, value: Any, *, maximum_bytes: int = 256_000) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} is too large")
    return encoded


class AutomationStateStore:
    """SQLite automation ledger with transactional claims and no external I/O."""

    SCHEMA_VERSION = 4

    _MIGRATIONS = {
        1: (
            """
            CREATE TABLE pw_automation_patch (
                patch_id TEXT PRIMARY KEY,
                gerrit_url TEXT NOT NULL,
                change_number INTEGER NOT NULL CHECK (change_number > 0),
                current_revision TEXT NOT NULL,
                current_patchset INTEGER NOT NULL CHECK (current_patchset > 0),
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE pw_automation_policy (
                patch_id TEXT PRIMARY KEY REFERENCES pw_automation_patch(patch_id)
                    ON DELETE CASCADE,
                mode TEXT NOT NULL CHECK (
                    mode IN ('disabled', 'advise', 'approval', 'automatic')
                ),
                action_budget INTEGER NOT NULL CHECK (action_budget >= 0),
                updated_by TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE pw_automation_observation (
                observation_id TEXT PRIMARY KEY,
                patch_id TEXT NOT NULL REFERENCES pw_automation_patch(patch_id)
                    ON DELETE CASCADE,
                revision TEXT NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                observed_at REAL NOT NULL,
                UNIQUE(patch_id, revision, fingerprint)
            )
            """,
            """
            CREATE TABLE pw_automation_trigger (
                trigger_id TEXT PRIMARY KEY,
                patch_id TEXT NOT NULL REFERENCES pw_automation_patch(patch_id)
                    ON DELETE CASCADE,
                revision TEXT NOT NULL,
                kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'claimed', 'consumed', 'stale')
                ),
                created_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                consumed_at REAL,
                UNIQUE(patch_id, revision, fingerprint),
                CHECK (
                    (claimed_by IS NULL AND claimed_at IS NULL) OR
                    (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE INDEX pw_automation_trigger_claim_idx
            ON pw_automation_trigger(state, created_at, trigger_id)
            """,
            """
            CREATE TABLE pw_automation_run (
                run_id TEXT PRIMARY KEY,
                patch_id TEXT NOT NULL REFERENCES pw_automation_patch(patch_id)
                    ON DELETE CASCADE,
                revision TEXT NOT NULL,
                patchset INTEGER NOT NULL CHECK (patchset > 0),
                trigger_id TEXT NOT NULL REFERENCES pw_automation_trigger(trigger_id),
                deterministic_key TEXT NOT NULL UNIQUE,
                policy_snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'planned', 'executing', 'waiting_external', 'succeeded',
                        'failed', 'ambiguous', 'cancelled', 'stale'
                    )
                ),
                action_budget INTEGER NOT NULL CHECK (action_budget >= 0),
                action_count INTEGER NOT NULL DEFAULT 0 CHECK (action_count >= 0),
                created_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                finished_at REAL,
                failure_code TEXT,
                failure_summary TEXT,
                CHECK (
                    (claimed_by IS NULL AND claimed_at IS NULL) OR
                    (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX pw_one_active_automation_run_per_patch
            ON pw_automation_run(patch_id)
            WHERE status IN ('planned', 'executing', 'waiting_external')
            """,
            """
            CREATE TABLE pw_automation_timeline (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES pw_automation_run(run_id)
                    ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE INDEX pw_automation_timeline_stream_idx
            ON pw_automation_timeline(run_id, event_id)
            """,
            """
            CREATE TRIGGER pw_automation_timeline_no_update
            BEFORE UPDATE ON pw_automation_timeline
            BEGIN
                SELECT RAISE(ABORT, 'automation timeline is append-only');
            END
            """,
            """
            CREATE TRIGGER pw_automation_timeline_no_delete
            BEFORE DELETE ON pw_automation_timeline
            BEGIN
                SELECT RAISE(ABORT, 'automation timeline is append-only');
            END
            """,
            """
            CREATE TABLE pw_automation_action (
                action_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES pw_automation_run(run_id)
                    ON DELETE CASCADE,
                action_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'planned', 'executing', 'succeeded', 'failed',
                        'ambiguous', 'cancelled', 'waiting_external'
                    )
                ),
                request_json TEXT NOT NULL,
                result_json TEXT,
                created_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                finished_at REAL,
                failure_code TEXT,
                failure_summary TEXT,
                CHECK (
                    (claimed_by IS NULL AND claimed_at IS NULL) OR
                    (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE INDEX pw_automation_action_claim_idx
            ON pw_automation_action(run_id, status, created_at, action_id)
            """,
            """
            CREATE TABLE pw_automation_setting (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                changed_by TEXT NOT NULL,
                reason TEXT NOT NULL,
                changed_at REAL NOT NULL
            )
            """,
            """
            INSERT INTO pw_automation_setting(
                singleton, enabled, changed_by, reason, changed_at
            ) VALUES (1, 0, 'system', 'safe default', 0)
            """,
            """
            CREATE TABLE pw_automation_setting_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                changed_by TEXT NOT NULL,
                reason TEXT NOT NULL,
                changed_at REAL NOT NULL
            )
            """,
        ),
        2: (
            """
            ALTER TABLE pw_automation_policy
            ADD COLUMN delivery_budget INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_budget >= 0)
            """,
            """
            ALTER TABLE pw_automation_run
            ADD COLUMN delivery_budget INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_budget >= 0)
            """,
            """
            ALTER TABLE pw_automation_run
            ADD COLUMN delivery_count INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_count >= 0)
            """,
            """
            ALTER TABLE pw_automation_action
            ADD COLUMN budget_bucket TEXT NOT NULL DEFAULT 'action'
                CHECK (budget_bucket IN ('action', 'delivery'))
            """,
            """
            CREATE TABLE pw_automation_action_approval (
                action_id TEXT PRIMARY KEY REFERENCES pw_automation_action(action_id)
                    ON DELETE CASCADE,
                approved_by TEXT NOT NULL,
                approved_at REAL NOT NULL,
                expected_revision TEXT NOT NULL,
                policy_snapshot_json TEXT NOT NULL
            )
            """,
        ),
        3: (
            """
            CREATE TABLE pw_research_policy (
                patch_id TEXT PRIMARY KEY REFERENCES pw_automation_patch(patch_id)
                    ON DELETE CASCADE,
                mode TEXT NOT NULL DEFAULT 'disabled' CHECK (
                    mode IN ('disabled', 'manual', 'automatic')
                ),
                run_budget INTEGER NOT NULL DEFAULT 0 CHECK (run_budget >= 0),
                updated_by TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            INSERT INTO pw_research_policy(
                patch_id, mode, run_budget, updated_by, updated_at
            )
            SELECT patch_id, 'disabled', 0, 'system', updated_at
            FROM pw_automation_patch
            """,
        ),
        4: (
            """
            CREATE TABLE pw_research_admission (
                admission_id TEXT PRIMARY KEY,
                patch_id TEXT NOT NULL REFERENCES pw_automation_patch(patch_id)
                    ON DELETE CASCADE,
                revision TEXT NOT NULL,
                patchset INTEGER NOT NULL CHECK (patchset > 0),
                policy_version TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('manual', 'automatic')),
                slot INTEGER NOT NULL CHECK (slot > 0),
                attempt_id TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('reserved', 'registered', 'released')
                ),
                session_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                failure_summary TEXT,
                UNIQUE(patch_id, revision, attempt_id),
                CHECK (
                    (state = 'registered' AND session_id IS NOT NULL) OR
                    (state <> 'registered' AND session_id IS NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX pw_research_active_slot
            ON pw_research_admission(patch_id, revision, slot)
            WHERE state IN ('reserved', 'registered')
            """,
            """
            CREATE INDEX pw_research_admission_state
            ON pw_research_admission(state, updated_at, admission_id)
            """,
        ),
    }

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        if self.database != ":memory:":
            path = Path(self.database).expanduser()
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
                    CREATE TABLE IF NOT EXISTS pw_automation_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT version FROM pw_automation_schema WHERE singleton = 1"
                ).fetchone()
                current = int(row["version"]) if row is not None else 0
                if current > self.SCHEMA_VERSION:
                    raise AutomationStateError(
                        f"automation schema {current} is newer than supported "
                        f"version {self.SCHEMA_VERSION}"
                    )
                for version in range(current + 1, self.SCHEMA_VERSION + 1):
                    for statement in self._MIGRATIONS[version]:
                        connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO pw_automation_schema(singleton, version)
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
    def _patch(row: sqlite3.Row) -> PatchRecord:
        return PatchRecord(
            patch_id=row["patch_id"],
            gerrit_url=row["gerrit_url"],
            change_number=row["change_number"],
            current_revision=row["current_revision"],
            current_patchset=row["current_patchset"],
            status=row["status"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )

    @staticmethod
    def _policy(row: sqlite3.Row) -> AutomationPolicy:
        return AutomationPolicy(
            patch_id=row["patch_id"],
            mode=row["mode"],
            action_budget=row["action_budget"],
            delivery_budget=row["delivery_budget"],
            updated_by=row["updated_by"],
            updated_at=_datetime(row["updated_at"]),
        )

    @staticmethod
    def _research_policy(row: sqlite3.Row) -> ResearchPolicy:
        return ResearchPolicy(
            patch_id=row["patch_id"],
            mode=row["mode"],
            run_budget=row["run_budget"],
            updated_by=row["updated_by"],
            updated_at=_datetime(row["updated_at"]),
        )

    @staticmethod
    def _research_admission(row: sqlite3.Row) -> ResearchAdmission:
        return ResearchAdmission(
            admission_id=row["admission_id"],
            patch_id=row["patch_id"],
            revision=row["revision"],
            patchset=row["patchset"],
            policy_version=row["policy_version"],
            mode=row["mode"],
            slot=row["slot"],
            attempt_id=row["attempt_id"],
            evidence_fingerprint=row["evidence_fingerprint"],
            state=row["state"],
            session_id=row["session_id"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
            failure_summary=row["failure_summary"],
        )

    @staticmethod
    def _observation(row: sqlite3.Row) -> Observation:
        return Observation(
            observation_id=row["observation_id"],
            patch_id=row["patch_id"],
            revision=row["revision"],
            source=row["source"],
            kind=row["kind"],
            fingerprint=row["fingerprint"],
            payload=json.loads(row["payload_json"]),
            observed_at=_datetime(row["observed_at"]),
        )

    @staticmethod
    def _trigger(row: sqlite3.Row) -> AutomationTrigger:
        return AutomationTrigger(
            trigger_id=row["trigger_id"],
            patch_id=row["patch_id"],
            revision=row["revision"],
            kind=row["kind"],
            fingerprint=row["fingerprint"],
            payload=json.loads(row["payload_json"]),
            state=row["state"],
            created_at=_datetime(row["created_at"]),
            claimed_by=row["claimed_by"],
            claimed_at=(
                _datetime(row["claimed_at"]) if row["claimed_at"] is not None else None
            ),
            consumed_at=(
                _datetime(row["consumed_at"])
                if row["consumed_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> AutomationRun:
        return AutomationRun(
            run_id=row["run_id"],
            patch_id=row["patch_id"],
            revision=row["revision"],
            patchset=row["patchset"],
            trigger_id=row["trigger_id"],
            deterministic_key=row["deterministic_key"],
            policy_snapshot=json.loads(row["policy_snapshot_json"]),
            status=row["status"],
            action_budget=row["action_budget"],
            action_count=row["action_count"],
            delivery_budget=row["delivery_budget"],
            delivery_count=row["delivery_count"],
            created_at=_datetime(row["created_at"]),
            claimed_by=row["claimed_by"],
            claimed_at=(
                _datetime(row["claimed_at"]) if row["claimed_at"] is not None else None
            ),
            finished_at=(
                _datetime(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
            failure_code=row["failure_code"],
            failure_summary=row["failure_summary"],
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> TimelineEvent:
        return TimelineEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            idempotency_key=row["idempotency_key"],
            created_at=_datetime(row["created_at"]),
        )

    @staticmethod
    def _action(row: sqlite3.Row) -> ActionAttempt:
        return ActionAttempt(
            action_id=row["action_id"],
            run_id=row["run_id"],
            action_type=row["action_type"],
            budget_bucket=row["budget_bucket"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            request=json.loads(row["request_json"]),
            result=(
                json.loads(row["result_json"])
                if row["result_json"] is not None
                else None
            ),
            created_at=_datetime(row["created_at"]),
            claimed_by=row["claimed_by"],
            claimed_at=(
                _datetime(row["claimed_at"]) if row["claimed_at"] is not None else None
            ),
            finished_at=(
                _datetime(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
            failure_code=row["failure_code"],
            failure_summary=row["failure_summary"],
        )

    @staticmethod
    def _approval(row: sqlite3.Row) -> ActionApproval:
        return ActionApproval(
            action_id=row["action_id"],
            approved_by=row["approved_by"],
            approved_at=_datetime(row["approved_at"]),
            expected_revision=row["expected_revision"],
            policy_snapshot=json.loads(row["policy_snapshot_json"]),
        )

    @staticmethod
    def _setting(row: sqlite3.Row) -> GlobalAutomationSetting:
        return GlobalAutomationSetting(
            enabled=bool(row["enabled"]),
            changed_by=row["changed_by"],
            reason=row["reason"],
            changed_at=_datetime(row["changed_at"]),
        )

    @staticmethod
    def _require_patch(connection: sqlite3.Connection, patch_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM pw_automation_patch WHERE patch_id = ?", (patch_id,)
        ).fetchone()
        if row is None:
            raise AutomationNotFound(f"unknown automation patch: {patch_id}")
        return row

    @staticmethod
    def _require_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM pw_automation_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise AutomationNotFound(f"unknown automation run: {run_id}")
        return row

    def upsert_patch(
        self,
        patch_id: str,
        *,
        gerrit_url: str,
        change_number: int,
        revision: str,
        patchset: int,
        status: str = "open",
        at: datetime | None = None,
    ) -> PatchRecord:
        patch_id = _text("patch_id", patch_id)
        gerrit_url = _text("gerrit_url", gerrit_url)
        change_number = _positive_int("change_number", change_number)
        revision = _text("revision", revision)
        patchset = _positive_int("patchset", patchset)
        status = _text("status", status)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM pw_automation_patch WHERE patch_id = ?",
                    (patch_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO pw_automation_patch(
                            patch_id, gerrit_url, change_number, current_revision,
                            current_patchset, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            patch_id,
                            gerrit_url,
                            change_number,
                            revision,
                            patchset,
                            status,
                            at_epoch,
                            at_epoch,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO pw_automation_policy(
                            patch_id, mode, action_budget, delivery_budget,
                            updated_by, updated_at
                        ) VALUES (?, 'disabled', 0, 0, 'system', ?)
                        """,
                        (patch_id, at_epoch),
                    )
                    connection.execute(
                        """
                        INSERT INTO pw_research_policy(
                            patch_id, mode, run_budget, updated_by, updated_at
                        ) VALUES (?, 'disabled', 0, 'system', ?)
                        """,
                        (patch_id, at_epoch),
                    )
                else:
                    if patchset < existing["current_patchset"]:
                        raise AutomationConflict("patchset cannot move backwards")
                    if (
                        patchset == existing["current_patchset"]
                        and revision != existing["current_revision"]
                    ):
                        raise AutomationConflict(
                            "one patchset cannot identify two revisions"
                        )
                    revision_changed = (
                        existing["current_revision"] != revision
                        or existing["current_patchset"] != patchset
                    )
                    connection.execute(
                        """
                        UPDATE pw_automation_patch
                        SET gerrit_url = ?, change_number = ?, current_revision = ?,
                            current_patchset = ?, status = ?, updated_at = ?
                        WHERE patch_id = ?
                        """,
                        (
                            gerrit_url,
                            change_number,
                            revision,
                            patchset,
                            status,
                            at_epoch,
                            patch_id,
                        ),
                    )
                    if revision_changed:
                        connection.execute(
                            """
                            UPDATE pw_automation_trigger
                            SET state = 'stale', claimed_by = NULL, claimed_at = NULL
                            WHERE patch_id = ? AND revision <> ?
                              AND state IN ('pending', 'claimed')
                            """,
                            (patch_id, revision),
                        )
                        active_runs = connection.execute(
                            """
                            SELECT run_id FROM pw_automation_run
                            WHERE patch_id = ? AND revision <> ?
                              AND status IN ('planned', 'executing', 'waiting_external')
                            """,
                            (patch_id, revision),
                        ).fetchall()
                        for run in active_runs:
                            connection.execute(
                                """
                                UPDATE pw_automation_action
                                SET status = CASE
                                      WHEN status = 'executing' THEN 'ambiguous'
                                      ELSE 'cancelled'
                                    END,
                                    finished_at = ?,
                                    failure_code = 'patch_revision_changed',
                                    failure_summary = 'Pinned revision became stale'
                                WHERE run_id = ?
                                  AND status IN (
                                    'planned', 'executing', 'waiting_external'
                                  )
                                """,
                                (at_epoch, run["run_id"]),
                            )
                        connection.execute(
                            """
                            UPDATE pw_automation_run
                            SET status = 'stale', finished_at = ?,
                                failure_code = 'patch_revision_changed',
                                failure_summary = 'Pinned revision became stale'
                            WHERE patch_id = ? AND revision <> ?
                              AND status IN ('planned', 'executing', 'waiting_external')
                            """,
                            (at_epoch, patch_id, revision),
                        )
                row = connection.execute(
                    "SELECT * FROM pw_automation_patch WHERE patch_id = ?",
                    (patch_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._patch(row)

    def get_patch(self, patch_id: str) -> PatchRecord:
        with self._connection() as connection:
            return self._patch(self._require_patch(connection, patch_id))

    def list_patches(self) -> list[PatchRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pw_automation_patch ORDER BY change_number, patch_id"
            ).fetchall()
        return [self._patch(row) for row in rows]

    def set_policy(
        self,
        patch_id: str,
        *,
        mode: str,
        action_budget: int,
        delivery_budget: int,
        updated_by: str,
        at: datetime | None = None,
    ) -> AutomationPolicy:
        if mode not in POLICY_MODES:
            raise ValueError(f"unknown policy mode: {mode}")
        action_budget = _budget("action_budget", action_budget)
        delivery_budget = _budget("delivery_budget", delivery_budget)
        updated_by = _text("updated_by", updated_by)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection, connection:
            self._require_patch(connection, patch_id)
            connection.execute(
                """
                UPDATE pw_automation_policy
                SET mode = ?, action_budget = ?, delivery_budget = ?,
                    updated_by = ?, updated_at = ?
                WHERE patch_id = ?
                """,
                (
                    mode,
                    action_budget,
                    delivery_budget,
                    updated_by,
                    at_epoch,
                    patch_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_automation_policy WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        assert row is not None
        return self._policy(row)

    def get_policy(self, patch_id: str) -> AutomationPolicy:
        with self._connection() as connection:
            self._require_patch(connection, patch_id)
            row = connection.execute(
                "SELECT * FROM pw_automation_policy WHERE patch_id = ?", (patch_id,)
            ).fetchone()
        assert row is not None
        return self._policy(row)

    def set_research_policy(
        self,
        patch_id: str,
        *,
        mode: str,
        run_budget: int,
        updated_by: str,
        expected_version: str | None = None,
        at: datetime | None = None,
    ) -> ResearchPolicy:
        """Set investigation authority independently from retest authority."""

        if mode not in RESEARCH_MODES:
            raise ValueError(f"unknown research policy mode: {mode}")
        run_budget = _budget("run_budget", run_budget)
        if run_budget > 20:
            raise ValueError("run_budget must not exceed 20")
        if mode != "disabled" and run_budget < 1:
            raise ValueError("enabled research modes require at least one run")
        updated_by = _text("updated_by", updated_by)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_patch(connection, patch_id)
                current = connection.execute(
                    "SELECT * FROM pw_research_policy WHERE patch_id = ?",
                    (patch_id,),
                ).fetchone()
                assert current is not None
                if (
                    expected_version is not None
                    and self._research_policy(current).version != expected_version
                ):
                    raise AutomationConflict(
                        "research policy changed after the proposal was prepared"
                    )
                connection.execute(
                    """
                    UPDATE pw_research_policy
                    SET mode = ?, run_budget = ?, updated_by = ?, updated_at = ?
                    WHERE patch_id = ?
                    """,
                    (mode, run_budget, updated_by, at_epoch, patch_id),
                )
                row = connection.execute(
                    "SELECT * FROM pw_research_policy WHERE patch_id = ?",
                    (patch_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._research_policy(row)

    def get_research_policy(self, patch_id: str) -> ResearchPolicy:
        with self._connection() as connection:
            self._require_patch(connection, patch_id)
            row = connection.execute(
                "SELECT * FROM pw_research_policy WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        assert row is not None
        return self._research_policy(row)

    def claim_research_admission(
        self,
        patch_id: str,
        *,
        revision: str,
        patchset: int,
        expected_policy_version: str,
        mode: str,
        attempt_id: str,
        evidence_fingerprint: str,
        admission_id: str | None = None,
        at: datetime | None = None,
    ) -> tuple[ResearchAdmission, bool]:
        """Atomically reserve one policy-versioned per-revision run slot."""

        patch_id = _text("patch_id", patch_id)
        revision = _text("revision", revision).lower()
        patchset = _positive_int("patchset", patchset)
        expected_policy_version = _text(
            "expected_policy_version", expected_policy_version
        )
        if mode not in RESEARCH_MODES - {"disabled"}:
            raise ValueError("research admission mode must be manual or automatic")
        attempt_id = _text("attempt_id", attempt_id)
        if len(attempt_id.encode("utf-8")) > 256:
            raise ValueError("attempt_id is too large")
        evidence_fingerprint = _text(
            "evidence_fingerprint", evidence_fingerprint
        )
        admission_id = _text(
            "admission_id", admission_id or str(uuid.uuid4())
        )
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                patch = self._require_patch(connection, patch_id)
                existing = connection.execute(
                    """
                    SELECT * FROM pw_research_admission
                    WHERE patch_id = ? AND revision = ? AND attempt_id = ?
                    """,
                    (patch_id, revision, attempt_id),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["patchset"] != patchset
                        or existing["policy_version"] != expected_policy_version
                        or existing["mode"] != mode
                        or existing["evidence_fingerprint"] != evidence_fingerprint
                    ):
                        raise AutomationConflict(
                            "research attempt identity was reused with different admission data"
                        )
                    if existing["state"] == "reserved":
                        if (
                            patch["current_revision"].lower() != revision
                            or patch["current_patchset"] != patchset
                        ):
                            raise AutomationConflict(
                                "research admission revision is no longer current"
                            )
                        current = connection.execute(
                            "SELECT * FROM pw_research_policy WHERE patch_id = ?",
                            (patch_id,),
                        ).fetchone()
                        assert current is not None
                        current_policy = self._research_policy(current)
                        if current_policy.version != expected_policy_version:
                            raise AutomationConflict(
                                "research policy changed before admission"
                            )
                        if current_policy.mode != mode:
                            raise AutomationConflict(
                                f"unknown-failure research policy is "
                                f"{current_policy.mode}, not {mode}"
                            )
                        if mode == "automatic":
                            enabled = connection.execute(
                                "SELECT enabled FROM pw_automation_setting "
                                "WHERE singleton = 1"
                            ).fetchone()["enabled"]
                            if not enabled:
                                raise GlobalAutomationDisabled(
                                    "global automation execution is disabled"
                                )
                    connection.commit()
                    return self._research_admission(existing), False
                if (
                    patch["current_revision"].lower() != revision
                    or patch["current_patchset"] != patchset
                ):
                    raise AutomationConflict(
                        "research admission revision is no longer current"
                    )
                policy = connection.execute(
                    "SELECT * FROM pw_research_policy WHERE patch_id = ?",
                    (patch_id,),
                ).fetchone()
                assert policy is not None
                current_policy = self._research_policy(policy)
                if current_policy.version != expected_policy_version:
                    raise AutomationConflict(
                        "research policy changed before admission"
                    )
                if current_policy.mode != mode:
                    raise AutomationConflict(
                        f"unknown-failure research policy is {current_policy.mode}, not {mode}"
                    )
                if mode == "automatic":
                    enabled = connection.execute(
                        "SELECT enabled FROM pw_automation_setting WHERE singleton = 1"
                    ).fetchone()["enabled"]
                    if not enabled:
                        raise GlobalAutomationDisabled(
                            "global automation execution is disabled"
                        )
                used = {
                    int(row["slot"])
                    for row in connection.execute(
                        """
                        SELECT slot FROM pw_research_admission
                        WHERE patch_id = ? AND revision = ?
                          AND state IN ('reserved', 'registered')
                        """,
                        (patch_id, revision),
                    ).fetchall()
                }
                slot = next(
                    (
                        candidate
                        for candidate in range(1, current_policy.run_budget + 1)
                        if candidate not in used
                    ),
                    None,
                )
                if slot is None:
                    raise BudgetExhausted(
                        "the per-revision research run budget is exhausted"
                    )
                connection.execute(
                    """
                    INSERT INTO pw_research_admission(
                        admission_id, patch_id, revision, patchset,
                        policy_version, mode, slot, attempt_id,
                        evidence_fingerprint, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        admission_id, patch_id, revision, patchset,
                        expected_policy_version, mode, slot, attempt_id,
                        evidence_fingerprint, at_epoch, at_epoch,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM pw_research_admission WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._research_admission(row), True

    def register_research_admission(
        self,
        admission_id: str,
        session_id: str,
        *,
        at: datetime | None = None,
    ) -> ResearchAdmission:
        """Bind a reserved slot to the durable session created for it."""

        session_id = _text("session_id", session_id)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM pw_research_admission WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                if row is None:
                    raise AutomationNotFound(
                        f"unknown research admission: {admission_id}"
                    )
                if row["state"] == "registered":
                    if row["session_id"] != session_id:
                        raise AutomationConflict(
                            "research admission is registered to another session"
                        )
                    connection.commit()
                    return self._research_admission(row)
                if row["state"] != "reserved":
                    raise AutomationConflict("research admission is no longer reserved")
                connection.execute(
                    """
                    UPDATE pw_research_admission
                    SET state = 'registered', session_id = ?, updated_at = ?,
                        failure_summary = NULL
                    WHERE admission_id = ? AND state = 'reserved'
                    """,
                    (session_id, at_epoch, admission_id),
                )
                row = connection.execute(
                    "SELECT * FROM pw_research_admission WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._research_admission(row)

    def release_research_admission(
        self,
        admission_id: str,
        *,
        reason: str,
        at: datetime | None = None,
    ) -> ResearchAdmission:
        """Release an unregistered slot after session creation failed."""

        reason = _text("reason", reason)[:2000]
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM pw_research_admission WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                if row is None:
                    raise AutomationNotFound(
                        f"unknown research admission: {admission_id}"
                    )
                if row["state"] == "released":
                    connection.commit()
                    return self._research_admission(row)
                if row["state"] == "registered":
                    raise AutomationConflict(
                        "registered research admission cannot be released"
                    )
                connection.execute(
                    """
                    UPDATE pw_research_admission
                    SET state = 'released', updated_at = ?, failure_summary = ?
                    WHERE admission_id = ? AND state = 'reserved'
                    """,
                    (at_epoch, reason, admission_id),
                )
                row = connection.execute(
                    "SELECT * FROM pw_research_admission WHERE admission_id = ?",
                    (admission_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._research_admission(row)

    def list_research_admissions(
        self,
        *,
        patch_id: str | None = None,
        revision: str | None = None,
    ) -> list[ResearchAdmission]:
        clauses = []
        parameters = []
        if patch_id is not None:
            clauses.append("patch_id = ?")
            parameters.append(str(patch_id))
        if revision is not None:
            clauses.append("revision = ?")
            parameters.append(str(revision).lower())
        query = "SELECT * FROM pw_research_admission"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY patch_id, revision, slot, created_at, admission_id"
        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._research_admission(row) for row in rows]

    def get_global_automation(self) -> GlobalAutomationSetting:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_automation_setting WHERE singleton = 1"
            ).fetchone()
        assert row is not None
        return self._setting(row)

    def set_global_automation(
        self,
        enabled: bool,
        *,
        changed_by: str,
        reason: str,
        at: datetime | None = None,
    ) -> GlobalAutomationSetting:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        changed_by = _text("changed_by", changed_by)
        reason = _text("reason", reason)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection, connection:
            connection.execute(
                """
                UPDATE pw_automation_setting
                SET enabled = ?, changed_by = ?, reason = ?, changed_at = ?
                WHERE singleton = 1
                """,
                (int(enabled), changed_by, reason, at_epoch),
            )
            connection.execute(
                """
                INSERT INTO pw_automation_setting_audit(
                    enabled, changed_by, reason, changed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (int(enabled), changed_by, reason, at_epoch),
            )
        return self.get_global_automation()

    def list_global_automation_audit(self) -> list[SettingAuditEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pw_automation_setting_audit ORDER BY audit_id"
            ).fetchall()
        return [
            SettingAuditEvent(
                audit_id=row["audit_id"],
                enabled=bool(row["enabled"]),
                changed_by=row["changed_by"],
                reason=row["reason"],
                changed_at=_datetime(row["changed_at"]),
            )
            for row in rows
        ]

    def record_observation(
        self,
        patch_id: str,
        *,
        revision: str,
        source: str,
        kind: str,
        fingerprint: str,
        payload: dict,
        observation_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[Observation, bool]:
        revision = _text("revision", revision)
        source = _text("source", source)
        kind = _text("kind", kind)
        fingerprint = _text("fingerprint", fingerprint)
        payload_json = _json("observation payload", payload)
        observation_id = _text(
            "observation_id", observation_id or str(uuid.uuid4())
        )
        at_epoch = _epoch(observed_at or _utc_now())
        with self._connection() as connection, connection:
            self._require_patch(connection, patch_id)
            try:
                connection.execute(
                    """
                    INSERT INTO pw_automation_observation(
                        observation_id, patch_id, revision, source, kind,
                        fingerprint, payload_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        patch_id,
                        revision,
                        source,
                        kind,
                        fingerprint,
                        payload_json,
                        at_epoch,
                    ),
                )
                created = True
                row = connection.execute(
                    """
                    SELECT * FROM pw_automation_observation
                    WHERE observation_id = ?
                    """,
                    (observation_id,),
                ).fetchone()
            except sqlite3.IntegrityError:
                created = False
                row = connection.execute(
                    """
                    SELECT * FROM pw_automation_observation
                    WHERE patch_id = ? AND revision = ? AND fingerprint = ?
                    """,
                    (patch_id, revision, fingerprint),
                ).fetchone()
                if (
                    row is None
                    or row["source"] != source
                    or row["kind"] != kind
                    or row["payload_json"] != payload_json
                ):
                    raise AutomationConflict(
                        f"observation fingerprint {fingerprint!r} was reused"
                    )
        assert row is not None
        return self._observation(row), created

    def list_observations(self, patch_id: str) -> list[Observation]:
        with self._connection() as connection:
            self._require_patch(connection, patch_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_automation_observation
                WHERE patch_id = ? ORDER BY observed_at, observation_id
                """,
                (patch_id,),
            ).fetchall()
        return [self._observation(row) for row in rows]

    def create_trigger(
        self,
        patch_id: str,
        *,
        revision: str,
        kind: str,
        fingerprint: str,
        payload: dict,
        trigger_id: str | None = None,
        created_at: datetime | None = None,
    ) -> AutomationTrigger:
        revision = _text("revision", revision)
        kind = _text("kind", kind)
        fingerprint = _text("fingerprint", fingerprint)
        payload_json = _json("trigger payload", payload)
        trigger_id = _text("trigger_id", trigger_id or str(uuid.uuid4()))
        at_epoch = _epoch(created_at or _utc_now())
        with self._connection() as connection, connection:
            patch = self._require_patch(connection, patch_id)
            initial_state = (
                "pending" if patch["current_revision"] == revision else "stale"
            )
            try:
                connection.execute(
                    """
                    INSERT INTO pw_automation_trigger(
                        trigger_id, patch_id, revision, kind, fingerprint,
                        payload_json, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trigger_id,
                        patch_id,
                        revision,
                        kind,
                        fingerprint,
                        payload_json,
                        initial_state,
                        at_epoch,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM pw_automation_trigger WHERE trigger_id = ?",
                    (trigger_id,),
                ).fetchone()
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT * FROM pw_automation_trigger
                    WHERE patch_id = ? AND revision = ? AND fingerprint = ?
                    """,
                    (patch_id, revision, fingerprint),
                ).fetchone()
                if (
                    row is None
                    or row["kind"] != kind
                    or row["payload_json"] != payload_json
                ):
                    raise AutomationConflict(
                        f"trigger fingerprint {fingerprint!r} was reused"
                    )
        assert row is not None
        return self._trigger(row)

    def claim_next_trigger(
        self, worker_id: str, *, at: datetime | None = None
    ) -> AutomationTrigger | None:
        worker_id = _text("worker_id", worker_id)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM pw_automation_trigger
                    WHERE state = 'claimed' AND claimed_by = ?
                    ORDER BY created_at, trigger_id LIMIT 1
                    """,
                    (worker_id,),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT * FROM pw_automation_trigger
                        WHERE state = 'pending'
                        ORDER BY created_at, trigger_id LIMIT 1
                        """
                    ).fetchone()
                    if row is not None:
                        connection.execute(
                            """
                            UPDATE pw_automation_trigger
                            SET state = 'claimed', claimed_by = ?, claimed_at = ?
                            WHERE trigger_id = ? AND state = 'pending'
                            """,
                            (worker_id, at_epoch, row["trigger_id"]),
                        )
                        row = connection.execute(
                            """
                            SELECT * FROM pw_automation_trigger WHERE trigger_id = ?
                            """,
                            (row["trigger_id"],),
                        ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._trigger(row) if row is not None else None

    def get_trigger(self, trigger_id: str) -> AutomationTrigger:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_automation_trigger WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
        if row is None:
            raise AutomationNotFound(f"unknown trigger: {trigger_id}")
        return self._trigger(row)

    def list_triggers(
        self, *, patch_id: str | None = None, state: str | None = None
    ) -> list[AutomationTrigger]:
        if state is not None and state not in TRIGGER_STATES:
            raise ValueError("unknown trigger state")
        clauses = []
        parameters = []
        if patch_id is not None:
            clauses.append("patch_id = ?")
            parameters.append(patch_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        query = "SELECT * FROM pw_automation_trigger"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, trigger_id"
        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._trigger(row) for row in rows]

    def create_run(
        self,
        trigger_id: str,
        *,
        deterministic_key: str,
        run_id: str | None = None,
        at: datetime | None = None,
    ) -> AutomationRun:
        deterministic_key = _text("deterministic_key", deterministic_key)
        run_id = _text("run_id", run_id or str(uuid.uuid4()))
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                trigger = connection.execute(
                    "SELECT * FROM pw_automation_trigger WHERE trigger_id = ?",
                    (trigger_id,),
                ).fetchone()
                if trigger is None:
                    raise AutomationNotFound(f"unknown trigger: {trigger_id}")
                patch = self._require_patch(connection, trigger["patch_id"])
                policy = connection.execute(
                    "SELECT * FROM pw_automation_policy WHERE patch_id = ?",
                    (trigger["patch_id"],),
                ).fetchone()
                assert policy is not None
                snapshot = {
                    "mode": policy["mode"],
                    "action_budget": policy["action_budget"],
                    "delivery_budget": policy["delivery_budget"],
                    "updated_by": policy["updated_by"],
                    "updated_at": _datetime(policy["updated_at"]).isoformat(),
                }
                snapshot_json = _json("policy snapshot", snapshot)
                existing = connection.execute(
                    """
                    SELECT * FROM pw_automation_run WHERE deterministic_key = ?
                    """,
                    (deterministic_key,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["trigger_id"] != trigger_id
                        or existing["policy_snapshot_json"] != snapshot_json
                    ):
                        raise AutomationConflict(
                            f"deterministic key {deterministic_key!r} was reused"
                        )
                    connection.commit()
                    return self._run(existing)
                if policy["mode"] == "disabled":
                    raise AutomationConflict("patch automation policy is disabled")
                if (
                    trigger["state"] not in {"pending", "claimed"}
                    or trigger["revision"] != patch["current_revision"]
                ):
                    raise AutomationConflict("trigger is consumed or stale")
                try:
                    connection.execute(
                        """
                        INSERT INTO pw_automation_run(
                            run_id, patch_id, revision, patchset, trigger_id,
                            deterministic_key, policy_snapshot_json, status,
                            action_budget, delivery_budget, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                        """,
                        (
                            run_id,
                            patch["patch_id"],
                            patch["current_revision"],
                            patch["current_patchset"],
                            trigger_id,
                            deterministic_key,
                            snapshot_json,
                            policy["action_budget"],
                            policy["delivery_budget"],
                            at_epoch,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AutomationConflict(
                        f"patch {patch['patch_id']} already has an active run"
                    ) from exc
                connection.execute(
                    """
                    UPDATE pw_automation_trigger
                    SET state = 'consumed', consumed_at = ?
                    WHERE trigger_id = ?
                    """,
                    (at_epoch, trigger_id),
                )
                row = connection.execute(
                    "SELECT * FROM pw_automation_run WHERE run_id = ?", (run_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._run(row)

    def get_run(self, run_id: str) -> AutomationRun:
        with self._connection() as connection:
            return self._run(self._require_run(connection, run_id))

    def list_runs(
        self, *, patch_id: str | None = None, include_terminal: bool = True
    ) -> list[AutomationRun]:
        clauses = []
        parameters = []
        if patch_id is not None:
            clauses.append("patch_id = ?")
            parameters.append(patch_id)
        if not include_terminal:
            placeholders = ",".join("?" for _ in RUN_TERMINAL_STATES)
            clauses.append(f"status NOT IN ({placeholders})")
            parameters.extend(sorted(RUN_TERMINAL_STATES))
        query = "SELECT * FROM pw_automation_run"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, run_id"
        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._run(row) for row in rows]

    @staticmethod
    def _automatic_allowed(connection: sqlite3.Connection, run: sqlite3.Row) -> None:
        snapshot = json.loads(run["policy_snapshot_json"])
        if snapshot.get("mode") != "automatic":
            return
        enabled = connection.execute(
            "SELECT enabled FROM pw_automation_setting WHERE singleton = 1"
        ).fetchone()["enabled"]
        if not enabled:
            raise GlobalAutomationDisabled(
                "global automation execution is disabled"
            )

    def claim_run(
        self, run_id: str, worker_id: str, *, at: datetime | None = None
    ) -> AutomationRun:
        worker_id = _text("worker_id", worker_id)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_run(connection, run_id)
                self._automatic_allowed(connection, row)
                if row["status"] == "executing" and row["claimed_by"] == worker_id:
                    connection.commit()
                    return self._run(row)
                if row["status"] != "planned":
                    raise AutomationConflict("run is not available to claim")
                connection.execute(
                    """
                    UPDATE pw_automation_run
                    SET status = 'executing', claimed_by = ?, claimed_at = ?
                    WHERE run_id = ? AND status = 'planned'
                    """,
                    (worker_id, at_epoch, run_id),
                )
                row = self._require_run(connection, run_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run(row)

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        at: datetime | None = None,
        failure_code: str | None = None,
        failure_summary: str | None = None,
    ) -> AutomationRun:
        if status not in RUN_STATES - {"planned", "executing"}:
            raise ValueError("invalid run completion status")
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection, connection:
            row = self._require_run(connection, run_id)
            if row["status"] == status:
                return self._run(row)
            if row["status"] in RUN_TERMINAL_STATES:
                raise AutomationConflict("run is already terminal")
            terminal = status in RUN_TERMINAL_STATES
            connection.execute(
                """
                UPDATE pw_automation_run
                SET status = ?, finished_at = ?, failure_code = ?,
                    failure_summary = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    at_epoch if terminal else None,
                    failure_code,
                    str(failure_summary)[:2000]
                    if failure_summary is not None
                    else None,
                    run_id,
                ),
            )
            row = self._require_run(connection, run_id)
        return self._run(row)

    def append_timeline(
        self,
        run_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        idempotency_key: str | None = None,
        at: datetime | None = None,
    ) -> TimelineEvent:
        event_type = _text("event_type", event_type)
        payload_json = _json("timeline payload", payload or {})
        idempotency_key = (
            _text("idempotency_key", idempotency_key)
            if idempotency_key is not None
            else None
        )
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection, connection:
            self._require_run(connection, run_id)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO pw_automation_timeline(
                        run_id, event_type, payload_json, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, event_type, payload_json, idempotency_key, at_epoch),
                )
                row = connection.execute(
                    "SELECT * FROM pw_automation_timeline WHERE event_id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT * FROM pw_automation_timeline WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if (
                    row is None
                    or row["run_id"] != run_id
                    or row["event_type"] != event_type
                    or row["payload_json"] != payload_json
                ):
                    raise AutomationConflict("timeline idempotency key was reused")
        assert row is not None
        return self._event(row)

    def list_timeline(self, run_id: str) -> list[TimelineEvent]:
        with self._connection() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_automation_timeline
                WHERE run_id = ? ORDER BY event_id
                """,
                (run_id,),
            ).fetchall()
        return [self._event(row) for row in rows]

    def plan_action(
        self,
        run_id: str,
        *,
        action_type: str,
        request: dict,
        idempotency_key: str,
        budget_bucket: str = "action",
        action_id: str | None = None,
        at: datetime | None = None,
    ) -> ActionAttempt:
        action_type = _text("action_type", action_type)
        idempotency_key = _text("idempotency_key", idempotency_key)
        if budget_bucket not in BUDGET_BUCKETS:
            raise ValueError("budget_bucket must be action or delivery")
        request_json = _json("action request", request)
        action_id = _text("action_id", action_id or str(uuid.uuid4()))
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM pw_automation_action WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["run_id"] != run_id
                        or existing["action_type"] != action_type
                        or existing["budget_bucket"] != budget_bucket
                        or existing["request_json"] != request_json
                    ):
                        raise AutomationConflict("action idempotency key was reused")
                    connection.commit()
                    return self._action(existing)
                run = self._require_run(connection, run_id)
                if run["status"] not in RUN_ACTIVE_STATES:
                    raise AutomationConflict("cannot plan action for terminal run")
                patch = self._require_patch(connection, run["patch_id"])
                if patch["current_revision"] != run["revision"]:
                    raise AutomationConflict("run revision is stale")
                count_column = (
                    "action_count" if budget_bucket == "action" else "delivery_count"
                )
                budget_column = (
                    "action_budget" if budget_bucket == "action" else "delivery_budget"
                )
                if run[count_column] >= run[budget_column]:
                    raise BudgetExhausted(
                        f"{budget_bucket} budget exhausted for run {run_id}"
                    )
                connection.execute(
                    f"""
                    UPDATE pw_automation_run
                    SET {count_column} = {count_column} + 1
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
                connection.execute(
                    """
                    INSERT INTO pw_automation_action(
                        action_id, run_id, action_type, budget_bucket,
                        idempotency_key, status, request_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'planned', ?, ?)
                    """,
                    (
                        action_id,
                        run_id,
                        action_type,
                        budget_bucket,
                        idempotency_key,
                        request_json,
                        at_epoch,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM pw_automation_action WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return self._action(row)

    def claim_next_action(
        self,
        run_id: str,
        worker_id: str,
        *,
        at: datetime | None = None,
    ) -> ActionAttempt | None:
        worker_id = _text("worker_id", worker_id)
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run(connection, run_id)
                policy_mode = json.loads(run["policy_snapshot_json"])["mode"]
                self._automatic_allowed(connection, run)
                if policy_mode == "advise":
                    raise AutomationConflict(
                        "advise-mode actions cannot be executed"
                    )
                if run["status"] not in RUN_ACTIVE_STATES:
                    raise AutomationConflict("run is terminal")
                row = connection.execute(
                    """
                    SELECT * FROM pw_automation_action
                    WHERE run_id = ? AND status = 'executing' AND claimed_by = ?
                    ORDER BY created_at, action_id LIMIT 1
                    """,
                    (run_id, worker_id),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT * FROM pw_automation_action
                        WHERE run_id = ? AND status = 'planned'
                        ORDER BY created_at, action_id LIMIT 1
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is not None:
                        if policy_mode == "approval":
                            approval = connection.execute(
                                """
                                SELECT 1 FROM pw_automation_action_approval
                                WHERE action_id = ?
                                """,
                                (row["action_id"],),
                            ).fetchone()
                            if approval is None:
                                raise AutomationConflict(
                                    "approval-mode action requires explicit approval"
                                )
                        connection.execute(
                            """
                            UPDATE pw_automation_action
                            SET status = 'executing', claimed_by = ?, claimed_at = ?
                            WHERE action_id = ? AND status = 'planned'
                            """,
                            (worker_id, at_epoch, row["action_id"]),
                        )
                        row = connection.execute(
                            "SELECT * FROM pw_automation_action WHERE action_id = ?",
                            (row["action_id"],),
                        ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._action(row) if row is not None else None

    def approve_action(
        self,
        action_id: str,
        *,
        approved_by: str,
        expected_revision: str,
        expected_policy_mode: str,
        expected_policy_snapshot: dict | None = None,
        at: datetime | None = None,
    ) -> ActionApproval:
        """Durably approve one exact action/revision/policy snapshot."""
        approved_by = _text("approved_by", approved_by)
        expected_revision = _text("expected_revision", expected_revision)
        if expected_policy_mode not in POLICY_MODES:
            raise ValueError("unknown expected_policy_mode")
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                action = connection.execute(
                    "SELECT * FROM pw_automation_action WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if action is None:
                    raise AutomationNotFound(f"unknown action: {action_id}")
                if action["status"] != "planned":
                    raise AutomationConflict("only a planned action can be approved")
                run = self._require_run(connection, action["run_id"])
                patch = self._require_patch(connection, run["patch_id"])
                snapshot = json.loads(run["policy_snapshot_json"])
                current_policy = connection.execute(
                    "SELECT * FROM pw_automation_policy WHERE patch_id = ?",
                    (run["patch_id"],),
                ).fetchone()
                assert current_policy is not None
                current_snapshot = {
                    "mode": current_policy["mode"],
                    "action_budget": current_policy["action_budget"],
                    "delivery_budget": current_policy["delivery_budget"],
                    "updated_by": current_policy["updated_by"],
                    "updated_at": _datetime(current_policy["updated_at"]).isoformat(),
                }
                if (
                    run["revision"] != expected_revision
                    or patch["current_revision"] != expected_revision
                ):
                    raise AutomationConflict("approval revision is stale")
                if (
                    snapshot.get("mode") != "approval"
                    or expected_policy_mode != "approval"
                    or current_snapshot != snapshot
                    or (
                        expected_policy_snapshot is not None
                        and expected_policy_snapshot != snapshot
                    )
                ):
                    raise AutomationConflict(
                        "approval policy no longer matches the run snapshot"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM pw_automation_action_approval
                    WHERE action_id = ?
                    """,
                    (action_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO pw_automation_action_approval(
                            action_id, approved_by, approved_at,
                            expected_revision, policy_snapshot_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            action_id,
                            approved_by,
                            at_epoch,
                            expected_revision,
                            run["policy_snapshot_json"],
                        ),
                    )
                    existing = connection.execute(
                        """
                        SELECT * FROM pw_automation_action_approval
                        WHERE action_id = ?
                        """,
                        (action_id,),
                    ).fetchone()
                elif (
                    existing["approved_by"] != approved_by
                    or existing["expected_revision"] != expected_revision
                    or existing["policy_snapshot_json"]
                    != run["policy_snapshot_json"]
                ):
                    raise AutomationConflict("action already has a different approval")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert existing is not None
        return self._approval(existing)

    def get_action_approval(self, action_id: str) -> ActionApproval | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM pw_automation_action_approval WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
        return self._approval(row) if row is not None else None

    def finish_action(
        self,
        action_id: str,
        status: str,
        *,
        result: dict | None = None,
        at: datetime | None = None,
        failure_code: str | None = None,
        failure_summary: str | None = None,
    ) -> ActionAttempt:
        if status not in ACTION_STATES - {"planned", "executing"}:
            raise ValueError("invalid action completion status")
        result_json = _json("action result", result) if result is not None else None
        at_epoch = _epoch(at or _utc_now())
        with self._connection() as connection, connection:
            row = connection.execute(
                "SELECT * FROM pw_automation_action WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise AutomationNotFound(f"unknown action: {action_id}")
            if row["status"] == status:
                return self._action(row)
            if row["status"] in ACTION_TERMINAL_STATES:
                raise AutomationConflict("action is already terminal")
            terminal = status in ACTION_TERMINAL_STATES
            connection.execute(
                """
                UPDATE pw_automation_action
                SET status = ?, result_json = ?, finished_at = ?,
                    failure_code = ?, failure_summary = ?
                WHERE action_id = ?
                """,
                (
                    status,
                    result_json,
                    at_epoch if terminal else None,
                    failure_code,
                    str(failure_summary)[:2000]
                    if failure_summary is not None
                    else None,
                    action_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_automation_action WHERE action_id = ?", (action_id,)
            ).fetchone()
        assert row is not None
        return self._action(row)

    def list_actions(self, run_id: str) -> list[ActionAttempt]:
        with self._connection() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM pw_automation_action
                WHERE run_id = ? ORDER BY created_at, action_id
                """,
                (run_id,),
            ).fetchall()
        return [self._action(row) for row in rows]

    def get_action(self, action_id: str) -> ActionAttempt:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_automation_action WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if row is None:
            raise AutomationNotFound(f"unknown action: {action_id}")
        return self._action(row)

    def recover_executing_as_ambiguous(
        self, *, before: datetime | None = None, at: datetime | None = None
    ) -> tuple[list[str], list[str]]:
        """Conservatively reconcile uncertain in-flight work after a crash."""
        at_epoch = _epoch(at or _utc_now())
        before_epoch = _epoch(before) if before is not None else at_epoch
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                action_rows = connection.execute(
                    """
                    SELECT action_id FROM pw_automation_action
                    WHERE status = 'executing' AND claimed_at <= ?
                    ORDER BY action_id
                    """,
                    (before_epoch,),
                ).fetchall()
                action_ids = [row["action_id"] for row in action_rows]
                connection.execute(
                    """
                    UPDATE pw_automation_action
                    SET status = 'ambiguous', finished_at = ?,
                        failure_code = 'controller_restart',
                        failure_summary = 'Execution outcome is unknown after restart'
                    WHERE status = 'executing' AND claimed_at <= ?
                    """,
                    (at_epoch, before_epoch),
                )
                run_rows = connection.execute(
                    """
                    SELECT run_id FROM pw_automation_run
                    WHERE status = 'executing' AND claimed_at <= ?
                    ORDER BY run_id
                    """,
                    (before_epoch,),
                ).fetchall()
                run_ids = [row["run_id"] for row in run_rows]
                connection.execute(
                    """
                    UPDATE pw_automation_run
                    SET status = 'ambiguous', finished_at = ?,
                        failure_code = 'controller_restart',
                        failure_summary = 'Run outcome is unknown after restart'
                    WHERE status = 'executing' AND claimed_at <= ?
                    """,
                    (at_epoch, before_epoch),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return run_ids, action_ids
