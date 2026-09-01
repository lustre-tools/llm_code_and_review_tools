"""Durable state and safe manifests for isolated engineering runs.

This module records controller intent only.  It never creates, deletes, or
executes a checkout.  Filesystem effects belong to a controller which must
return here with the exact run, owner, and revision before advancing state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit


CHECKOUT_STATES = frozenset(
    {"planned", "allocated", "active", "cleanup_pending", "released", "quarantined"}
)
OPEN_CHECKOUT_STATES = frozenset(
    {"planned", "allocated", "active", "cleanup_pending"}
)
SAFE_ENVIRONMENT_KEYS = frozenset(
    {"CI", "LANG", "LC_ALL", "NO_COLOR", "PYTHONUNBUFFERED", "TERM", "TZ"}
)
MAX_COMMANDS = 64
MAX_ARGV_ITEMS = 128
MAX_ARG_BYTES = 4_096
MAX_ARGV_BYTES = 32_768
MAX_RELATIVE_PATH_BYTES = 512
MAX_ARTIFACT_BYTES = 2_147_483_648

_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CHECKOUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+/-]{0,126}$")
_SHELL_EXECUTABLES = frozenset(
    {"ash", "bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"}
)


class EngineeringStateError(RuntimeError):
    """Base error for engineering-run state."""


class EngineeringNotFound(EngineeringStateError):
    """An allocation or manifest does not exist."""


class EngineeringConflict(EngineeringStateError):
    """An operation violates an ownership, binding, or lifecycle invariant."""


@dataclass(frozen=True)
class CheckoutAllocation:
    allocation_id: str
    run_id: str
    session_id: str
    patch_id: str
    patchset: int
    revision_sha: str
    repository_url: str
    base_branch: str
    checkout_path: Path
    checkout_kind: str
    owner_id: str
    state: str
    initial_dirty: bool | None
    created_at: datetime
    allocated_at: datetime | None
    activated_at: datetime | None
    cleanup_requested_at: datetime | None
    released_at: datetime | None
    quarantined_at: datetime | None
    state_reason: str | None


@dataclass(frozen=True)
class RestartDecision:
    allocation_id: str
    run_id: str
    previous_state: str
    state: str
    action: str
    reason: str


def _required_text(name: str, value: object, *, maximum: int = 2_000) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in normalized or len(normalized.encode("utf-8")) > maximum:
        raise ValueError(f"{name} is invalid or too long")
    return normalized


def _identifier(name: str, value: object) -> str:
    normalized = _required_text(name, value, maximum=200)
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{name} is not a safe identifier")
    return normalized


def _revision(value: object) -> str:
    normalized = str(value).strip()
    if not _REVISION_RE.fullmatch(normalized):
        raise ValueError("revision_sha must be a 40-64 digit lowercase hex digest")
    return normalized


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.timestamp()


def _datetime(value: float | int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON serializable") from exc


def _relative_path(value: object, *, allow_dot: bool) -> str:
    text = str(value)
    if not text or "\x00" in text or "\\" in text:
        raise ValueError("path must be a portable relative path")
    if len(text.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise ValueError("path is too long")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise ValueError("path must be a confined relative path")
    if not allow_dot and path == Path("."):
        raise ValueError("artifact path must name a file")
    normalized = os.path.normpath(text)
    if normalized == ".." or normalized.startswith(f"..{os.sep}"):
        raise ValueError("path traversal is not allowed")
    return "." if allow_dot and normalized == "." else Path(normalized).as_posix()


def resolve_confined_path(root: str | Path, relative_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a validated relative path without allowing symlink escape."""

    base = Path(root).expanduser().resolve(strict=True)
    relative = _relative_path(relative_path, allow_dot=True)
    candidate = (base / relative).resolve(strict=must_exist)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("resolved path escapes its configured root") from exc
    return candidate


@dataclass(frozen=True)
class SafeCommand:
    """One immutable exec-style command; shell command text is not supported."""

    step_id: str
    argv: tuple[str, ...]
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int = 3_600
    expected_exit_codes: tuple[int, ...] = (0,)
    label: str = ""
    execution_target: str = "checkout"

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _identifier("step_id", self.step_id))
        if isinstance(self.argv, (str, bytes)) or not isinstance(self.argv, Sequence):
            raise ValueError("argv must be an array of arguments, not shell text")
        if any(not isinstance(item, str) for item in self.argv):
            raise ValueError("argv arguments must be strings")
        argv = tuple(self.argv)
        if not argv or len(argv) > MAX_ARGV_ITEMS:
            raise ValueError("argv must contain between 1 and 128 arguments")
        total = 0
        for item in argv:
            size = len(item.encode("utf-8"))
            if not item or "\x00" in item or size > MAX_ARG_BYTES:
                raise ValueError("argv contains an empty, invalid, or oversized argument")
            total += size
        if total > MAX_ARGV_BYTES:
            raise ValueError("argv is too large")
        executable = Path(argv[0]).name.lower()
        env_wrapped = executable == "env" and len(argv) > 1 and Path(argv[1]).name.lower() in _SHELL_EXECUTABLES
        if executable in _SHELL_EXECUTABLES or env_wrapped:
            raise ValueError("shell interpreters are not permitted in safe commands")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", _relative_path(self.cwd, allow_dot=True))

        raw_env: Iterable[tuple[object, object]]
        if isinstance(self.env, Mapping):
            raw_env = self.env.items()
        elif isinstance(self.env, Sequence) and not isinstance(self.env, (str, bytes)):
            raw_env = self.env
        else:
            raise ValueError("env must be a mapping or array of key/value pairs")
        normalized_env: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in raw_env:
            if not isinstance(entry, Sequence) or isinstance(entry, (str, bytes)) or len(entry) != 2:
                raise ValueError("env entries must be key/value pairs")
            if not isinstance(entry[0], str) or not isinstance(entry[1], str):
                raise ValueError("environment keys and values must be strings")
            key, value = entry
            if key not in SAFE_ENVIRONMENT_KEYS:
                raise ValueError(f"environment key {key!r} is not allowlisted")
            if key in seen:
                raise ValueError(f"environment key {key!r} is duplicated")
            if "\x00" in value or len(value.encode("utf-8")) > MAX_ARG_BYTES:
                raise ValueError("environment value is invalid or too long")
            seen.add(key)
            normalized_env.append((key, value))
        object.__setattr__(self, "env", tuple(sorted(normalized_env)))

        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 86_400:
            raise ValueError("timeout_seconds must be between 1 and 86400")
        if isinstance(self.expected_exit_codes, (str, bytes)) or not isinstance(
            self.expected_exit_codes, Sequence
        ):
            raise ValueError("expected_exit_codes must be an array")
        exit_codes = tuple(self.expected_exit_codes)
        if not exit_codes or len(exit_codes) > 16 or any(
            isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255
            for code in exit_codes
        ):
            raise ValueError("expected_exit_codes contains invalid values")
        object.__setattr__(self, "expected_exit_codes", tuple(sorted(set(exit_codes))))
        label = str(self.label).strip()
        if label and (
            any(character in label for character in "\x00\r\n")
            or len(label.encode("utf-8")) > 200
        ):
            raise ValueError("command label is invalid or too long")
        object.__setattr__(self, "label", label)
        execution_target = _required_text(
            "execution_target", self.execution_target, maximum=200
        )
        if any(character in execution_target for character in "\r\n"):
            raise ValueError("execution_target contains control characters")
        object.__setattr__(
            self,
            "execution_target",
            execution_target,
        )

    def resolve_cwd(self, checkout_path: str | Path, *, must_exist: bool = False) -> Path:
        return resolve_confined_path(checkout_path, self.cwd, must_exist=must_exist)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "env": {key: value for key, value in self.env},
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_codes": list(self.expected_exit_codes),
            "label": self.label,
            "execution_target": self.execution_target,
        }


@dataclass(frozen=True)
class ExecutionManifest:
    manifest_id: str
    run_id: str
    revision_sha: str
    commands: tuple[SafeCommand, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_id", _identifier("manifest_id", self.manifest_id))
        object.__setattr__(self, "run_id", _identifier("run_id", self.run_id))
        object.__setattr__(self, "revision_sha", _revision(self.revision_sha))
        if self.schema_version != 1:
            raise ValueError("unsupported execution manifest schema")
        if isinstance(self.commands, (str, bytes)) or not isinstance(self.commands, Sequence):
            raise ValueError("commands must be an array")
        commands = tuple(self.commands)
        if not commands or len(commands) > MAX_COMMANDS or not all(
            isinstance(command, SafeCommand) for command in commands
        ):
            raise ValueError("commands must contain 1-64 SafeCommand objects")
        if len({command.step_id for command in commands}) != len(commands):
            raise ValueError("command step IDs must be unique")
        object.__setattr__(self, "commands", commands)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "run_id": self.run_id,
            "revision_sha": self.revision_sha,
            "commands": [command.to_dict() for command in self.commands],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArtifactMetadata:
    artifact_id: str
    run_id: str
    revision_sha: str
    kind: str
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier("artifact_id", self.artifact_id))
        object.__setattr__(self, "run_id", _identifier("run_id", self.run_id))
        object.__setattr__(self, "revision_sha", _revision(self.revision_sha))
        object.__setattr__(self, "kind", _identifier("kind", self.kind))
        object.__setattr__(
            self, "relative_path", _relative_path(self.relative_path, allow_dot=False)
        )
        digest = str(self.sha256)
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("sha256 must be exactly 64 lowercase hexadecimal digits")
        if isinstance(self.size_bytes, bool) or not 0 <= self.size_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("artifact size is outside the collection bound")
        media_type = str(self.media_type)
        if not _MEDIA_TYPE_RE.fullmatch(media_type):
            raise ValueError("media_type is invalid")

    def resolve_path(self, artifact_root: str | Path, *, must_exist: bool = False) -> Path:
        return resolve_confined_path(artifact_root, self.relative_path, must_exist=must_exist)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "revision_sha": self.revision_sha,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }


class EngineeringStateStore:
    """SQLite checkout ownership ledger and immutable evidence registry."""

    SCHEMA_VERSION = 1

    def __init__(self, database: str | Path, *, checkout_root: str | Path) -> None:
        configured_root = Path(checkout_root).expanduser()
        if not configured_root.is_absolute():
            raise ValueError("checkout_root must be absolute")
        if configured_root.is_symlink():
            raise ValueError("checkout_root must not be a symlink")
        try:
            self.checkout_root = configured_root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("checkout_root must be a pre-created directory") from exc
        if not self.checkout_root.is_dir() or self.checkout_root in {Path("/"), Path.home().resolve()}:
            raise ValueError("checkout_root must be a dedicated directory")

        requested_database = str(database)
        self._database_is_memory = requested_database == ":memory:"
        self._database_uri = self._database_is_memory
        self._memory_keeper: sqlite3.Connection | None = None
        if self._database_is_memory:
            self.database = f"file:pw-engineering-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._memory_keeper = sqlite3.connect(self.database, uri=True)
        else:
            self.database = requested_database
            database_path = Path(self.database).expanduser()
            database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._migrate()
        if not self._database_is_memory:
            os.chmod(Path(self.database).expanduser(), 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database, timeout=30, uri=self._database_uri
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if not self._database_is_memory:
            connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        statements = (
            """
            CREATE TABLE pw_engineering_schema (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE pw_checkout_allocation (
                allocation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                patch_id TEXT NOT NULL,
                patchset INTEGER NOT NULL CHECK (patchset > 0),
                revision_sha TEXT NOT NULL,
                repository_url TEXT NOT NULL,
                base_branch TEXT NOT NULL,
                checkout_path TEXT NOT NULL UNIQUE,
                checkout_kind TEXT NOT NULL CHECK (checkout_kind = 'full_clone'),
                owner_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (state IN (
                    'planned', 'allocated', 'active', 'cleanup_pending',
                    'released', 'quarantined'
                )),
                initial_dirty INTEGER CHECK (initial_dirty IN (0, 1)),
                created_at REAL NOT NULL,
                allocated_at REAL,
                activated_at REAL,
                cleanup_requested_at REAL,
                released_at REAL,
                quarantined_at REAL,
                state_reason TEXT,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE INDEX pw_checkout_allocation_state_idx
            ON pw_checkout_allocation(state, updated_at)
            """,
            """
            CREATE TABLE pw_checkout_event (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                allocation_id TEXT NOT NULL REFERENCES pw_checkout_allocation(allocation_id),
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE TRIGGER pw_checkout_event_no_update BEFORE UPDATE ON pw_checkout_event
            BEGIN SELECT RAISE(ABORT, 'checkout events are append-only'); END
            """,
            """
            CREATE TRIGGER pw_checkout_event_no_delete BEFORE DELETE ON pw_checkout_event
            BEGIN SELECT RAISE(ABORT, 'checkout events are append-only'); END
            """,
            """
            CREATE TABLE pw_execution_manifest (
                manifest_id TEXT PRIMARY KEY,
                allocation_id TEXT NOT NULL REFERENCES pw_checkout_allocation(allocation_id),
                run_id TEXT NOT NULL UNIQUE,
                revision_sha TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE TRIGGER pw_execution_manifest_no_update BEFORE UPDATE ON pw_execution_manifest
            BEGIN SELECT RAISE(ABORT, 'execution manifests are immutable'); END
            """,
            """
            CREATE TRIGGER pw_execution_manifest_no_delete BEFORE DELETE ON pw_execution_manifest
            BEGIN SELECT RAISE(ABORT, 'execution manifests are immutable'); END
            """,
            """
            CREATE TABLE pw_engineering_artifact (
                artifact_id TEXT PRIMARY KEY,
                allocation_id TEXT NOT NULL REFERENCES pw_checkout_allocation(allocation_id),
                run_id TEXT NOT NULL,
                revision_sha TEXT NOT NULL,
                kind TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(allocation_id, relative_path)
            )
            """,
            """
            CREATE TRIGGER pw_engineering_artifact_no_update BEFORE UPDATE ON pw_engineering_artifact
            BEGIN SELECT RAISE(ABORT, 'artifact metadata is immutable'); END
            """,
            """
            CREATE TRIGGER pw_engineering_artifact_no_delete BEFORE DELETE ON pw_engineering_artifact
            BEGIN SELECT RAISE(ABORT, 'artifact metadata is immutable'); END
            """,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pw_engineering_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT version FROM pw_engineering_schema WHERE singleton = 1"
                ).fetchone()
                current = int(row["version"]) if row else 0
                if current > self.SCHEMA_VERSION:
                    raise EngineeringStateError("engineering schema is newer than supported")
                if current == 0:
                    for statement in statements[1:]:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO pw_engineering_schema(singleton, version) VALUES (1, 1)"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def validate_checkout_path(self, value: str | Path) -> Path:
        raw = Path(value).expanduser()
        if any(part == ".." for part in raw.parts):
            raise ValueError("checkout path traversal is not allowed")
        candidate = raw if raw.is_absolute() else self.checkout_root / raw
        if candidate.is_symlink():
            raise ValueError("checkout path must not be a symlink")
        resolved = candidate.resolve(strict=False)
        if resolved.parent != self.checkout_root or not _CHECKOUT_NAME_RE.fullmatch(resolved.name):
            raise ValueError("checkout path must be one safe direct child of checkout_root")
        return resolved

    @staticmethod
    def _repository_url(value: object) -> str:
        normalized = _required_text("repository_url", value, maximum=2_000)
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("repository_url must be a credential-free HTTPS URL")
        return normalized

    @staticmethod
    def _row(row: sqlite3.Row) -> CheckoutAllocation:
        return CheckoutAllocation(
            allocation_id=row["allocation_id"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            patch_id=row["patch_id"],
            patchset=int(row["patchset"]),
            revision_sha=row["revision_sha"],
            repository_url=row["repository_url"],
            base_branch=row["base_branch"],
            checkout_path=Path(row["checkout_path"]),
            checkout_kind=row["checkout_kind"],
            owner_id=row["owner_id"],
            state=row["state"],
            initial_dirty=(None if row["initial_dirty"] is None else bool(row["initial_dirty"])),
            created_at=_datetime(row["created_at"]),
            allocated_at=_datetime(row["allocated_at"]),
            activated_at=_datetime(row["activated_at"]),
            cleanup_requested_at=_datetime(row["cleanup_requested_at"]),
            released_at=_datetime(row["released_at"]),
            quarantined_at=_datetime(row["quarantined_at"]),
            state_reason=row["state_reason"],
        )

    @staticmethod
    def _event(connection: sqlite3.Connection, allocation_id: str, event_type: str, payload: dict, now: datetime) -> None:
        connection.execute(
            "INSERT INTO pw_checkout_event(allocation_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (allocation_id, event_type, _canonical_json(payload), _epoch(now)),
        )

    def plan_checkout(
        self,
        *,
        run_id: str,
        session_id: str,
        patch_id: str,
        patchset: int,
        revision_sha: str,
        repository_url: str,
        base_branch: str,
        checkout_path: str | Path,
        owner_id: str | None = None,
        allocation_id: str | None = None,
        now: datetime | None = None,
    ) -> CheckoutAllocation:
        run_id = _identifier("run_id", run_id)
        session_id = _identifier("session_id", session_id)
        patch_id = _identifier("patch_id", patch_id)
        if isinstance(patchset, bool) or not isinstance(patchset, int) or patchset <= 0:
            raise ValueError("patchset must be a positive integer")
        revision_sha = _revision(revision_sha)
        repository_url = self._repository_url(repository_url)
        base_branch = _required_text("base_branch", base_branch, maximum=256)
        if not _REF_RE.fullmatch(base_branch) or ".." in base_branch.split("/"):
            raise ValueError("base_branch is not a safe ref name")
        path = self.validate_checkout_path(checkout_path)
        allocation_id = _identifier("allocation_id", allocation_id or f"checkout-{uuid.uuid4().hex}")
        owner_id = _identifier("owner_id", owner_id or f"patch-watcher:{run_id}")
        timestamp = now or _now()
        values = (
            allocation_id, run_id, session_id, patch_id, patchset, revision_sha,
            repository_url, base_branch, str(path), "full_clone", owner_id,
            "planned", _epoch(timestamp), _epoch(timestamp),
        )
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO pw_checkout_allocation(
                        allocation_id, run_id, session_id, patch_id, patchset,
                        revision_sha, repository_url, base_branch, checkout_path,
                        checkout_kind, owner_id, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                self._event(connection, allocation_id, "checkout_planned", {
                    "run_id": run_id, "owner_id": owner_id,
                    "revision_sha": revision_sha, "checkout_kind": "full_clone",
                }, timestamp)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise EngineeringConflict("run, owner, or checkout path is already allocated") from exc
        return self.get_checkout(allocation_id)

    def get_checkout(self, allocation_id: str) -> CheckoutAllocation:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_checkout_allocation WHERE allocation_id = ?",
                (str(allocation_id),),
            ).fetchone()
        if row is None:
            raise EngineeringNotFound(f"unknown checkout allocation {allocation_id!r}")
        return self._row(row)

    def get_allocation_by_run(self, run_id: str) -> CheckoutAllocation | None:
        """Return a run's exclusive checkout allocation, if it has one."""

        run_id = _identifier("run_id", run_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_checkout_allocation WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._row(row) if row is not None else None

    def list_allocations(
        self, *, states: Iterable[str] | None = None, limit: int = 100
    ) -> tuple[CheckoutAllocation, ...]:
        """Return a bounded newest-first dashboard projection."""

        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        requested = tuple(dict.fromkeys(states or ()))
        if any(state not in CHECKOUT_STATES for state in requested):
            raise ValueError("states contains an unknown checkout lifecycle state")
        parameters: list[Any] = []
        where = ""
        if requested:
            where = f"WHERE state IN ({','.join('?' for _ in requested)})"
            parameters.extend(requested)
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM pw_checkout_allocation {where}
                ORDER BY created_at DESC, allocation_id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def _owned_row(
        self,
        connection: sqlite3.Connection,
        allocation_id: str,
        *,
        run_id: str,
        owner_id: str,
        revision_sha: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM pw_checkout_allocation WHERE allocation_id = ?",
            (str(allocation_id),),
        ).fetchone()
        if row is None:
            raise EngineeringNotFound(f"unknown checkout allocation {allocation_id!r}")
        if (
            row["run_id"] != run_id
            or row["owner_id"] != owner_id
            or row["revision_sha"] != revision_sha
        ):
            raise EngineeringConflict("checkout ownership or exact revision binding does not match")
        return row

    def mark_allocated(
        self, allocation_id: str, *, run_id: str, owner_id: str,
        revision_sha: str, now: datetime | None = None,
    ) -> CheckoutAllocation:
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_row(connection, allocation_id, run_id=run_id, owner_id=owner_id, revision_sha=revision_sha)
            if row["state"] != "planned":
                raise EngineeringConflict("only a planned checkout can be allocated")
            path = self.validate_checkout_path(row["checkout_path"])
            if not path.is_dir() or path.is_symlink():
                raise EngineeringConflict("allocated checkout directory must exist and not be a symlink")
            connection.execute(
                "UPDATE pw_checkout_allocation SET state = 'allocated', allocated_at = ?, updated_at = ? WHERE allocation_id = ?",
                (_epoch(timestamp), _epoch(timestamp), allocation_id),
            )
            self._event(connection, allocation_id, "checkout_allocated", {"path": str(path)}, timestamp)
            connection.commit()
        return self.get_checkout(allocation_id)

    def activate_checkout(
        self, allocation_id: str, *, run_id: str, owner_id: str,
        revision_sha: str, observed_revision: str, initial_dirty: bool,
        now: datetime | None = None,
    ) -> CheckoutAllocation:
        observed_revision = _revision(observed_revision)
        if isinstance(initial_dirty, bool) is False:
            raise ValueError("initial_dirty must be boolean")
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_row(connection, allocation_id, run_id=run_id, owner_id=owner_id, revision_sha=revision_sha)
            if row["state"] != "allocated":
                raise EngineeringConflict("only an allocated checkout can become active")
            if observed_revision != row["revision_sha"]:
                raise EngineeringConflict("observed checkout revision is stale")
            path = self.validate_checkout_path(row["checkout_path"])
            if not path.is_dir() or not (path / ".git").is_dir() or (path / ".git").is_symlink():
                raise EngineeringConflict("checkout is not an independent full clone")
            if initial_dirty:
                raise EngineeringConflict("an initially dirty checkout cannot become active")
            connection.execute(
                """
                UPDATE pw_checkout_allocation
                SET state = 'active', initial_dirty = 0, activated_at = ?, updated_at = ?
                WHERE allocation_id = ?
                """,
                (_epoch(timestamp), _epoch(timestamp), allocation_id),
            )
            self._event(connection, allocation_id, "checkout_active", {"revision_sha": observed_revision, "initial_dirty": False}, timestamp)
            connection.commit()
        return self.get_checkout(allocation_id)

    def request_cleanup(
        self, allocation_id: str, *, run_id: str, owner_id: str,
        revision_sha: str, reason: str, now: datetime | None = None,
    ) -> CheckoutAllocation:
        reason = _required_text("reason", reason, maximum=2_000)
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_row(connection, allocation_id, run_id=run_id, owner_id=owner_id, revision_sha=revision_sha)
            if row["state"] == "cleanup_pending":
                connection.commit()
                return self._row(row)
            if row["state"] not in {"planned", "allocated", "active"}:
                raise EngineeringConflict("checkout cannot enter cleanup from its current state")
            connection.execute(
                """
                UPDATE pw_checkout_allocation
                SET state = 'cleanup_pending', cleanup_requested_at = ?, state_reason = ?, updated_at = ?
                WHERE allocation_id = ?
                """,
                (_epoch(timestamp), reason, _epoch(timestamp), allocation_id),
            )
            self._event(connection, allocation_id, "checkout_cleanup_requested", {"reason": reason}, timestamp)
            connection.commit()
        return self.get_checkout(allocation_id)

    def release_checkout(
        self, allocation_id: str, *, run_id: str, owner_id: str,
        revision_sha: str, now: datetime | None = None,
    ) -> CheckoutAllocation:
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_row(connection, allocation_id, run_id=run_id, owner_id=owner_id, revision_sha=revision_sha)
            if row["state"] != "cleanup_pending":
                raise EngineeringConflict("only cleanup-pending checkout state can be released")
            path = self.validate_checkout_path(row["checkout_path"])
            if path.exists() or path.is_symlink():
                raise EngineeringConflict("checkout path still exists; release is not confirmed")
            connection.execute(
                "UPDATE pw_checkout_allocation SET state = 'released', released_at = ?, updated_at = ? WHERE allocation_id = ?",
                (_epoch(timestamp), _epoch(timestamp), allocation_id),
            )
            self._event(connection, allocation_id, "checkout_released", {"path": str(path)}, timestamp)
            connection.commit()
        return self.get_checkout(allocation_id)

    def quarantine_checkout(
        self, allocation_id: str, *, run_id: str, owner_id: str,
        revision_sha: str, reason: str, now: datetime | None = None,
    ) -> CheckoutAllocation:
        reason = _required_text("reason", reason, maximum=2_000)
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_row(connection, allocation_id, run_id=run_id, owner_id=owner_id, revision_sha=revision_sha)
            if row["state"] == "released":
                raise EngineeringConflict("a released checkout cannot be quarantined")
            if row["state"] == "quarantined":
                connection.commit()
                return self._row(row)
            connection.execute(
                """
                UPDATE pw_checkout_allocation
                SET state = 'quarantined', quarantined_at = ?, state_reason = ?, updated_at = ?
                WHERE allocation_id = ?
                """,
                (_epoch(timestamp), reason, _epoch(timestamp), allocation_id),
            )
            self._event(connection, allocation_id, "checkout_quarantined", {"reason": reason}, timestamp)
            connection.commit()
        return self.get_checkout(allocation_id)

    def reconcile_after_restart(self, active_runs: Mapping[str, str], *, now: datetime | None = None) -> tuple[RestartDecision, ...]:
        """Keep exactly-bound active runs and queue every other open checkout."""

        normalized = {_identifier("run_id", run): _revision(revision) for run, revision in active_runs.items()}
        timestamp = now or _now()
        decisions: list[RestartDecision] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM pw_checkout_allocation WHERE state IN ('planned', 'allocated', 'active', 'cleanup_pending') ORDER BY created_at"
            ).fetchall()
            for row in rows:
                previous = row["state"]
                if previous == "cleanup_pending":
                    decisions.append(RestartDecision(row["allocation_id"], row["run_id"], previous, previous, "resume_cleanup", row["state_reason"] or "cleanup_pending"))
                    continue
                admitted_revision = normalized.get(row["run_id"])
                if admitted_revision == row["revision_sha"]:
                    decisions.append(RestartDecision(row["allocation_id"], row["run_id"], previous, previous, "retain", "exact_run_revision_match"))
                    continue
                reason = "restart_orphaned" if admitted_revision is None else "restart_stale_revision"
                connection.execute(
                    """
                    UPDATE pw_checkout_allocation
                    SET state = 'cleanup_pending', cleanup_requested_at = COALESCE(cleanup_requested_at, ?),
                        state_reason = ?, updated_at = ? WHERE allocation_id = ?
                    """,
                    (_epoch(timestamp), reason, _epoch(timestamp), row["allocation_id"]),
                )
                self._event(connection, row["allocation_id"], "checkout_restart_reconciled", {"action": "request_cleanup", "reason": reason}, timestamp)
                decisions.append(RestartDecision(row["allocation_id"], row["run_id"], previous, "cleanup_pending", "request_cleanup", reason))
            connection.commit()
        return tuple(decisions)

    def save_manifest(self, allocation_id: str, manifest: ExecutionManifest, *, now: datetime | None = None) -> str:
        timestamp = now or _now()
        payload = _canonical_json(manifest.to_dict())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM pw_checkout_allocation WHERE allocation_id = ?", (allocation_id,)).fetchone()
            if row is None:
                raise EngineeringNotFound(f"unknown checkout allocation {allocation_id!r}")
            if row["run_id"] != manifest.run_id or row["revision_sha"] != manifest.revision_sha:
                raise EngineeringConflict("manifest does not match checkout run and revision")
            existing = connection.execute("SELECT manifest_sha256 FROM pw_execution_manifest WHERE manifest_id = ?", (manifest.manifest_id,)).fetchone()
            if existing:
                if existing["manifest_sha256"] != manifest.digest:
                    raise EngineeringConflict("manifest ID already has different immutable content")
                connection.commit()
                return manifest.digest
            try:
                connection.execute(
                    """
                    INSERT INTO pw_execution_manifest(manifest_id, allocation_id, run_id, revision_sha, manifest_json, manifest_sha256, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (manifest.manifest_id, allocation_id, manifest.run_id, manifest.revision_sha, payload, manifest.digest, _epoch(timestamp)),
                )
            except sqlite3.IntegrityError as exc:
                raise EngineeringConflict("run already has an immutable execution manifest") from exc
            connection.commit()
        return manifest.digest

    def get_manifest(self, run_id: str) -> ExecutionManifest | None:
        """Read and verify the immutable execution manifest for one run."""

        run_id = _identifier("run_id", run_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_execution_manifest WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["manifest_json"])
            commands = tuple(
                SafeCommand(
                    step_id=command["step_id"],
                    argv=command["argv"],
                    cwd=command["cwd"],
                    env=command["env"],
                    timeout_seconds=command["timeout_seconds"],
                    expected_exit_codes=command["expected_exit_codes"],
                    label=command.get("label", ""),
                    execution_target=command.get("execution_target", "checkout"),
                )
                for command in payload["commands"]
            )
            manifest = ExecutionManifest(
                manifest_id=payload["manifest_id"],
                run_id=payload["run_id"],
                revision_sha=payload["revision_sha"],
                commands=commands,
                schema_version=payload["schema_version"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EngineeringStateError("stored execution manifest is invalid") from exc
        if manifest.digest != row["manifest_sha256"]:
            raise EngineeringStateError("stored execution manifest digest does not match")
        return manifest

    def register_artifact(self, allocation_id: str, artifact: ArtifactMetadata, *, now: datetime | None = None) -> None:
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM pw_checkout_allocation WHERE allocation_id = ?", (allocation_id,)).fetchone()
            if row is None:
                raise EngineeringNotFound(f"unknown checkout allocation {allocation_id!r}")
            if row["run_id"] != artifact.run_id or row["revision_sha"] != artifact.revision_sha:
                raise EngineeringConflict("artifact does not match checkout run and revision")
            if row["state"] not in {"active", "cleanup_pending", "quarantined"}:
                raise EngineeringConflict("artifacts can only be registered after checkout activation")
            try:
                connection.execute(
                    """
                    INSERT INTO pw_engineering_artifact(
                        artifact_id, allocation_id, run_id, revision_sha, kind,
                        relative_path, sha256, size_bytes, media_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id, allocation_id, artifact.run_id,
                        artifact.revision_sha, artifact.kind, artifact.relative_path,
                        artifact.sha256, artifact.size_bytes, artifact.media_type,
                        _epoch(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EngineeringConflict("artifact ID or path is already registered") from exc
            connection.commit()

    def list_artifacts(
        self, run_id: str, *, limit: int = 100
    ) -> tuple[ArtifactMetadata, ...]:
        """Return bounded immutable artifact metadata for one run."""

        run_id = _identifier("run_id", run_id)
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pw_engineering_artifact WHERE run_id = ?
                ORDER BY created_at DESC, artifact_id DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return tuple(
            ArtifactMetadata(
                artifact_id=row["artifact_id"],
                run_id=row["run_id"],
                revision_sha=row["revision_sha"],
                kind=row["kind"],
                relative_path=row["relative_path"],
                sha256=row["sha256"],
                size_bytes=int(row["size_bytes"]),
                media_type=row["media_type"],
            )
            for row in rows
        )


__all__ = [
    "ArtifactMetadata", "CheckoutAllocation", "CHECKOUT_STATES",
    "EngineeringConflict", "EngineeringNotFound", "EngineeringStateError",
    "EngineeringStateStore", "ExecutionManifest", "RestartDecision",
    "SAFE_ENVIRONMENT_KEYS", "SafeCommand", "resolve_confined_path",
]
