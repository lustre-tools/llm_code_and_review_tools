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
VALIDATION_ADMISSION_STATES = frozenset(
    {"disabled", "awaiting_approval", "approved"}
)
VALIDATION_EXECUTION_STATES = frozenset(
    {
        "planned", "claimed", "running", "succeeded", "failed",
        "cancelled", "resource_exhausted", "stale", "ambiguous",
    }
)
VALIDATION_ATTEMPT_STATES = frozenset(
    {
        "claimed", "running", "succeeded", "failed", "cancelled",
        "resource_exhausted", "stale", "ambiguous",
    }
)
VALIDATION_STEP_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "skipped", "resource_exhausted"}
)
VALIDATION_TERMINAL_ATTEMPT_STATES = VALIDATION_ATTEMPT_STATES - {
    "claimed", "running"
}

_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CHECKOUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+/-]{0,126}$")
_ENVIRONMENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
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


@dataclass(frozen=True)
class ValidationExecution:
    """One exact-revision guest-execution capability grant and its outcome."""

    execution_id: str
    idempotency_key: str
    allocation_id: str
    run_id: str
    session_id: str
    patch_id: str
    revision_sha: str
    owner_id: str
    manifest_id: str | None
    manifest_sha256: str | None
    initial_admission_state: str
    admission_state: str
    state: str
    requested_by: str
    requested_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    disabled_by: str | None
    disabled_at: datetime | None
    disabled_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ValidationAttempt:
    attempt_id: str
    execution_id: str
    attempt_number: int
    idempotency_key: str
    run_id: str
    session_id: str
    revision_sha: str
    owner_id: str
    state: str
    worker_id: str
    claimed_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failure_code: str | None
    summary: str | None
    retry_grant_id: str | None


@dataclass(frozen=True)
class ValidationStepResult:
    attempt_id: str
    step_id: str
    command: ValidationCommandAudit
    command_sha256: str
    state: str
    exit_code: int | None
    summary: str
    artifact_ids: tuple[str, ...]
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True)
class CapacityCooldown:
    patch_id: str
    not_before: datetime
    consecutive_exhaustions: int
    total_exhaustions: int
    last_execution_id: str
    last_attempt_id: str
    updated_at: datetime

    def active_at(self, now: datetime) -> bool:
        return now < self.not_before


@dataclass(frozen=True)
class ValidationRetryGrant:
    grant_id: str
    execution_id: str
    idempotency_key: str
    revision_sha: str
    approved_by: str
    approved_at: datetime
    consumed_by_attempt_id: str | None
    consumed_at: datetime | None


@dataclass(frozen=True)
class ValidationRestartDecision:
    attempt_id: str
    execution_id: str
    previous_state: str
    state: str
    action: str
    reason: str


@dataclass(frozen=True)
class ValidationCommandAudit:
    """Bounded record of an open-ended command interpreted only in the guest.

    This is deliberately separate from :class:`SafeCommand`: manifests are
    safe plans, while an approved guest capability may execute arbitrary argv
    or guest-side command text.  This object records what was requested; it
    does not authorize or execute it.
    """

    command_id: str
    argv: tuple[str, ...] | None = None
    text: str | None = None
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int = 3_600
    expected_exit_codes: tuple[int, ...] = (0,)
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", _identifier("command_id", self.command_id))
        if (self.argv is None) == (self.text is None):
            raise ValueError("exactly one of argv and guest command text is required")
        if self.argv is not None:
            if isinstance(self.argv, (str, bytes)) or not isinstance(self.argv, Sequence):
                raise ValueError("argv must be an array")
            argv = tuple(self.argv)
            if not argv or len(argv) > 256 or any(
                not isinstance(item, str)
                or not item
                or "\x00" in item
                or len(item.encode("utf-8")) > 16_384
                for item in argv
            ):
                raise ValueError("argv is empty, invalid, or oversized")
            if sum(len(item.encode("utf-8")) for item in argv) > 131_072:
                raise ValueError("argv is oversized")
            object.__setattr__(self, "argv", argv)
        elif (
            not isinstance(self.text, str)
            or not self.text
            or "\x00" in self.text
            or len(self.text.encode("utf-8")) > 131_072
        ):
            raise ValueError("guest command text is empty, invalid, or oversized")
        if (
            not isinstance(self.cwd, str)
            or not self.cwd
            or "\x00" in self.cwd
            or len(self.cwd.encode("utf-8")) > 4_096
        ):
            raise ValueError("guest cwd is empty, invalid, or oversized")
        raw_env: Any = self.env.items() if isinstance(self.env, Mapping) else self.env
        if isinstance(raw_env, (str, bytes)) or not isinstance(raw_env, Iterable):
            raise ValueError("env must be key/value pairs")
        normalized_env: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in raw_env:
            if (
                isinstance(item, (str, bytes))
                or not isinstance(item, Sequence)
                or len(item) != 2
            ):
                raise ValueError("env must contain key/value pairs")
            key, value = item
            if (
                not isinstance(key, str)
                or not _ENVIRONMENT_KEY_RE.fullmatch(key)
                or key in seen
            ):
                raise ValueError("environment key is invalid or duplicated")
            if (
                not isinstance(value, str)
                or "\x00" in value
                or len(value.encode("utf-8")) > 16_384
            ):
                raise ValueError("environment value is invalid or oversized")
            seen.add(key)
            normalized_env.append((key, value))
        object.__setattr__(self, "env", tuple(sorted(normalized_env)))
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 86_400:
            raise ValueError("timeout_seconds must be between 1 and 86400")
        if isinstance(self.expected_exit_codes, (str, bytes)) or not isinstance(
            self.expected_exit_codes, Sequence
        ):
            raise ValueError("expected_exit_codes must be an array")
        codes = tuple(self.expected_exit_codes)
        if not codes or len(codes) > 256 or any(
            isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255
            for code in codes
        ):
            raise ValueError("expected_exit_codes contains invalid values")
        object.__setattr__(self, "expected_exit_codes", tuple(sorted(set(codes))))
        if (
            not isinstance(self.label, str)
            or "\x00" in self.label
            or len(self.label.encode("utf-8")) > 500
        ):
            raise ValueError("label is invalid or oversized")

    @property
    def mode(self) -> str:
        return "argv" if self.argv is not None else "text"

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "command_id": self.command_id,
            "mode": self.mode,
            "cwd": self.cwd,
            "env": dict(self.env),
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_codes": list(self.expected_exit_codes),
            "label": self.label,
        }
        value["argv" if self.argv is not None else "text"] = (
            list(self.argv) if self.argv is not None else self.text
        )
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_command(cls, value: object) -> ValidationCommandAudit:
        """Copy a broker GuestCommand (or its mapping) without importing it."""

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            payload = value
        else:
            to_dict = getattr(value, "to_dict", None)
            if not callable(to_dict):
                raise ValueError("command must expose a command audit mapping")
            payload = to_dict()
        if not isinstance(payload, Mapping):
            raise ValueError("command audit payload must be a mapping")
        return cls(
            command_id=payload["command_id"],
            argv=payload.get("argv"),
            text=payload.get("text"),
            cwd=payload.get("cwd", "."),
            env=payload.get("env", {}),
            timeout_seconds=payload.get("timeout_seconds", 3_600),
            expected_exit_codes=payload.get("expected_exit_codes", (0,)),
            label=payload.get("label", ""),
        )


@dataclass(frozen=True)
class ValidationCommandClaim:
    """Durable pre-dispatch decision for one command identity.

    Only ``dispatch`` authorizes a caller to execute the command.  A prior
    reservation with no result has an unknown outcome after a crash and is
    deliberately not replayable.
    """

    attempt_id: str
    command_id: str
    command: ValidationCommandAudit
    command_sha256: str
    reserved_at: datetime
    disposition: str

    @property
    def should_dispatch(self) -> bool:
        return self.disposition == "dispatch"


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

    SCHEMA_VERSION = 3

    def __init__(
        self,
        database: str | Path,
        *,
        checkout_root: str | Path,
        capacity_cooldown_base_seconds: int = 900,
        capacity_cooldown_max_seconds: int = 86_400,
    ) -> None:
        if (
            isinstance(capacity_cooldown_base_seconds, bool)
            or capacity_cooldown_base_seconds <= 0
            or isinstance(capacity_cooldown_max_seconds, bool)
            or capacity_cooldown_max_seconds < capacity_cooldown_base_seconds
        ):
            raise ValueError("capacity cooldown bounds are invalid")
        self.capacity_cooldown_base_seconds = capacity_cooldown_base_seconds
        self.capacity_cooldown_max_seconds = capacity_cooldown_max_seconds
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
        validation_statements = (
            """
            CREATE TABLE pw_validation_execution (
                execution_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                allocation_id TEXT NOT NULL REFERENCES pw_checkout_allocation(allocation_id),
                run_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                patch_id TEXT NOT NULL,
                revision_sha TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                manifest_id TEXT REFERENCES pw_execution_manifest(manifest_id),
                manifest_sha256 TEXT,
                initial_admission_state TEXT NOT NULL CHECK (
                    initial_admission_state IN ('disabled', 'awaiting_approval')
                ),
                admission_state TEXT NOT NULL CHECK (admission_state IN (
                    'disabled', 'awaiting_approval', 'approved'
                )),
                state TEXT NOT NULL CHECK (state IN (
                    'planned', 'claimed', 'running', 'succeeded', 'failed',
                    'cancelled', 'resource_exhausted', 'stale', 'ambiguous'
                )),
                requested_by TEXT NOT NULL,
                requested_at REAL NOT NULL,
                approved_by TEXT,
                approved_at REAL,
                disabled_by TEXT,
                disabled_at REAL,
                disabled_reason TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                CHECK ((manifest_id IS NULL) = (manifest_sha256 IS NULL)),
                CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
                CHECK ((disabled_by IS NULL) = (disabled_at IS NULL)),
                CHECK (
                    admission_state != 'approved' OR
                    (approved_by IS NOT NULL AND approved_at IS NOT NULL)
                ),
                CHECK (
                    admission_state != 'disabled' OR
                    (disabled_by IS NOT NULL AND disabled_at IS NOT NULL
                     AND disabled_reason IS NOT NULL)
                )
            )
            """,
            """
            CREATE TRIGGER pw_validation_execution_binding_immutable
            BEFORE UPDATE OF idempotency_key, allocation_id, run_id, session_id,
                patch_id, revision_sha, owner_id, manifest_id, manifest_sha256,
                initial_admission_state, requested_by, requested_at, created_at
            ON pw_validation_execution
            BEGIN SELECT RAISE(ABORT, 'validation execution binding is immutable'); END
            """,
            """
            CREATE TRIGGER pw_validation_execution_transition_guard
            BEFORE UPDATE OF admission_state ON pw_validation_execution
            WHEN NOT (
                OLD.admission_state = NEW.admission_state OR
                (OLD.admission_state = 'awaiting_approval'
                 AND NEW.admission_state IN ('approved', 'disabled')) OR
                (OLD.admission_state = 'approved' AND NEW.admission_state = 'disabled')
            )
            BEGIN SELECT RAISE(ABORT, 'validation admission transition is invalid'); END
            """,
            """
            CREATE TRIGGER pw_validation_execution_decision_immutable
            BEFORE UPDATE OF approved_by, approved_at, disabled_by, disabled_at,
                disabled_reason ON pw_validation_execution
            WHEN
                (OLD.approved_at IS NOT NULL AND (
                    NEW.approved_at IS NOT OLD.approved_at OR
                    NEW.approved_by IS NOT OLD.approved_by
                )) OR
                (OLD.disabled_at IS NOT NULL AND (
                    NEW.disabled_at IS NOT OLD.disabled_at OR
                    NEW.disabled_by IS NOT OLD.disabled_by OR
                    NEW.disabled_reason IS NOT OLD.disabled_reason
                ))
            BEGIN SELECT RAISE(ABORT, 'validation decision evidence is immutable'); END
            """,
            """
            CREATE TRIGGER pw_validation_execution_no_delete
            BEFORE DELETE ON pw_validation_execution
            BEGIN SELECT RAISE(ABORT, 'validation executions are durable'); END
            """,
            """
            CREATE TABLE pw_validation_attempt (
                attempt_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES pw_validation_execution(execution_id),
                attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
                idempotency_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                revision_sha TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'claimed', 'running', 'succeeded', 'failed', 'cancelled',
                    'resource_exhausted', 'stale', 'ambiguous'
                )),
                worker_id TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                failure_code TEXT,
                summary TEXT,
                retry_grant_id TEXT,
                updated_at REAL NOT NULL,
                UNIQUE(execution_id, attempt_number)
            )
            """,
            """
            CREATE UNIQUE INDEX pw_one_active_validation_attempt
            ON pw_validation_attempt(execution_id)
            WHERE state IN ('claimed', 'running')
            """,
            """
            CREATE TRIGGER pw_validation_attempt_binding_immutable
            BEFORE UPDATE OF execution_id, attempt_number, idempotency_key,
                run_id, session_id, revision_sha, owner_id, worker_id,
                claimed_at, retry_grant_id
            ON pw_validation_attempt
            BEGIN SELECT RAISE(ABORT, 'validation attempt binding is immutable'); END
            """,
            """
            CREATE TRIGGER pw_validation_attempt_terminal_immutable
            BEFORE UPDATE OF state, started_at, finished_at, failure_code, summary
            ON pw_validation_attempt
            WHEN OLD.state IN (
                'succeeded', 'failed', 'cancelled', 'resource_exhausted',
                'stale', 'ambiguous'
            )
            BEGIN SELECT RAISE(ABORT, 'validation attempt result is immutable'); END
            """,
            """
            CREATE TRIGGER pw_validation_attempt_no_delete
            BEFORE DELETE ON pw_validation_attempt
            BEGIN SELECT RAISE(ABORT, 'validation attempts are durable'); END
            """,
            """
            CREATE TABLE pw_validation_step_result (
                attempt_id TEXT NOT NULL REFERENCES pw_validation_attempt(attempt_id),
                step_id TEXT NOT NULL,
                command_json TEXT NOT NULL,
                command_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'succeeded', 'failed', 'cancelled', 'skipped',
                    'resource_exhausted'
                )),
                exit_code INTEGER CHECK (
                    exit_code IS NULL OR (exit_code >= 0 AND exit_code <= 255)
                ),
                summary TEXT NOT NULL,
                artifact_ids_json TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (attempt_id, step_id)
            )
            """,
            """
            CREATE TRIGGER pw_validation_step_no_update
            BEFORE UPDATE ON pw_validation_step_result
            BEGIN SELECT RAISE(ABORT, 'validation step results are immutable'); END
            """,
            """
            CREATE TRIGGER pw_validation_step_no_delete
            BEFORE DELETE ON pw_validation_step_result
            BEGIN SELECT RAISE(ABORT, 'validation step results are immutable'); END
            """,
            """
            CREATE TABLE pw_validation_capacity_cooldown (
                patch_id TEXT PRIMARY KEY,
                not_before REAL NOT NULL,
                consecutive_exhaustions INTEGER NOT NULL CHECK (
                    consecutive_exhaustions >= 0
                ),
                total_exhaustions INTEGER NOT NULL CHECK (total_exhaustions >= 0),
                last_execution_id TEXT NOT NULL,
                last_attempt_id TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE pw_validation_retry_grant (
                grant_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES pw_validation_execution(execution_id),
                idempotency_key TEXT NOT NULL UNIQUE,
                revision_sha TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                approved_at REAL NOT NULL,
                consumed_by_attempt_id TEXT,
                consumed_at REAL,
                CHECK (
                    (consumed_by_attempt_id IS NULL AND consumed_at IS NULL) OR
                    (consumed_by_attempt_id IS NOT NULL AND consumed_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE TRIGGER pw_validation_retry_binding_immutable
            BEFORE UPDATE OF execution_id, idempotency_key, revision_sha,
                approved_by, approved_at
            ON pw_validation_retry_grant
            BEGIN SELECT RAISE(ABORT, 'validation retry grant binding is immutable'); END
            """,
            """
            CREATE TRIGGER pw_validation_retry_consumption_immutable
            BEFORE UPDATE OF consumed_by_attempt_id, consumed_at
            ON pw_validation_retry_grant
            WHEN OLD.consumed_at IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'validation retry consumption is immutable'); END
            """,
        )
        command_claim_statements = (
            """
            CREATE TABLE pw_validation_command_claim (
                attempt_id TEXT NOT NULL REFERENCES pw_validation_attempt(attempt_id),
                command_id TEXT NOT NULL,
                command_json TEXT NOT NULL,
                command_sha256 TEXT NOT NULL,
                reserved_at REAL NOT NULL,
                PRIMARY KEY (attempt_id, command_id)
            )
            """,
            """
            INSERT INTO pw_validation_command_claim(
                attempt_id, command_id, command_json, command_sha256, reserved_at
            )
            SELECT attempt_id, step_id, command_json, command_sha256, created_at
            FROM pw_validation_step_result
            """,
            """
            CREATE TRIGGER pw_validation_command_claim_no_update
            BEFORE UPDATE ON pw_validation_command_claim
            BEGIN SELECT RAISE(ABORT, 'validation command claims are immutable'); END
            """,
            """
            CREATE TRIGGER pw_validation_command_claim_no_delete
            BEFORE DELETE ON pw_validation_command_claim
            BEGIN SELECT RAISE(ABORT, 'validation command claims are durable'); END
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
                    current = 1
                if current < 2:
                    for statement in validation_statements:
                        connection.execute(statement)
                    connection.execute(
                        "UPDATE pw_engineering_schema SET version = 2 WHERE singleton = 1"
                    )
                    current = 2
                if current < 3:
                    for statement in command_claim_statements:
                        connection.execute(statement)
                    connection.execute(
                        "UPDATE pw_engineering_schema SET version = 3 WHERE singleton = 1"
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

    @staticmethod
    def _validation_execution(row: sqlite3.Row) -> ValidationExecution:
        return ValidationExecution(
            execution_id=row["execution_id"],
            idempotency_key=row["idempotency_key"],
            allocation_id=row["allocation_id"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            patch_id=row["patch_id"],
            revision_sha=row["revision_sha"],
            owner_id=row["owner_id"],
            manifest_id=row["manifest_id"],
            manifest_sha256=row["manifest_sha256"],
            initial_admission_state=row["initial_admission_state"],
            admission_state=row["admission_state"],
            state=row["state"],
            requested_by=row["requested_by"],
            requested_at=_datetime(row["requested_at"]),
            approved_by=row["approved_by"],
            approved_at=_datetime(row["approved_at"]),
            disabled_by=row["disabled_by"],
            disabled_at=_datetime(row["disabled_at"]),
            disabled_reason=row["disabled_reason"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )

    @staticmethod
    def _validation_attempt(row: sqlite3.Row) -> ValidationAttempt:
        return ValidationAttempt(
            attempt_id=row["attempt_id"],
            execution_id=row["execution_id"],
            attempt_number=int(row["attempt_number"]),
            idempotency_key=row["idempotency_key"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            revision_sha=row["revision_sha"],
            owner_id=row["owner_id"],
            state=row["state"],
            worker_id=row["worker_id"],
            claimed_at=_datetime(row["claimed_at"]),
            started_at=_datetime(row["started_at"]),
            finished_at=_datetime(row["finished_at"]),
            failure_code=row["failure_code"],
            summary=row["summary"],
            retry_grant_id=row["retry_grant_id"],
        )

    @staticmethod
    def _validation_command_from_dict(
        value: Mapping[str, Any]
    ) -> ValidationCommandAudit:
        return ValidationCommandAudit.from_command(value)

    @classmethod
    def _validation_step(cls, row: sqlite3.Row) -> ValidationStepResult:
        try:
            command_value = json.loads(row["command_json"])
            artifact_ids = tuple(json.loads(row["artifact_ids_json"]))
            command = cls._validation_command_from_dict(command_value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EngineeringStateError("stored validation step is invalid") from exc
        digest = hashlib.sha256(
            _canonical_json(command.to_dict()).encode("utf-8")
        ).hexdigest()
        if digest != row["command_sha256"]:
            raise EngineeringStateError("stored validation command digest does not match")
        return ValidationStepResult(
            attempt_id=row["attempt_id"],
            step_id=row["step_id"],
            command=command,
            command_sha256=digest,
            state=row["state"],
            exit_code=row["exit_code"],
            summary=row["summary"],
            artifact_ids=artifact_ids,
            started_at=_datetime(row["started_at"]),
            finished_at=_datetime(row["finished_at"]),
        )

    @staticmethod
    def _cooldown(row: sqlite3.Row) -> CapacityCooldown:
        return CapacityCooldown(
            patch_id=row["patch_id"],
            not_before=_datetime(row["not_before"]),
            consecutive_exhaustions=int(row["consecutive_exhaustions"]),
            total_exhaustions=int(row["total_exhaustions"]),
            last_execution_id=row["last_execution_id"],
            last_attempt_id=row["last_attempt_id"],
            updated_at=_datetime(row["updated_at"]),
        )

    @staticmethod
    def _retry_grant(row: sqlite3.Row) -> ValidationRetryGrant:
        return ValidationRetryGrant(
            grant_id=row["grant_id"],
            execution_id=row["execution_id"],
            idempotency_key=row["idempotency_key"],
            revision_sha=row["revision_sha"],
            approved_by=row["approved_by"],
            approved_at=_datetime(row["approved_at"]),
            consumed_by_attempt_id=row["consumed_by_attempt_id"],
            consumed_at=_datetime(row["consumed_at"]),
        )

    def create_validation_execution(
        self,
        allocation_id: str,
        *,
        idempotency_key: str,
        requested_by: str,
        admission_state: str = "awaiting_approval",
        manifest_id: str | None = None,
        disabled_reason: str | None = None,
        execution_id: str | None = None,
        now: datetime | None = None,
    ) -> ValidationExecution:
        """Create one immutable run/session grant for guest validation work."""

        if admission_state not in {"disabled", "awaiting_approval"}:
            raise ValueError("new validation must be disabled or awaiting approval")
        idempotency_key = _required_text(
            "idempotency_key", idempotency_key, maximum=500
        )
        requested_by = _required_text("requested_by", requested_by, maximum=500)
        reason = (
            _required_text("disabled_reason", disabled_reason, maximum=2_000)
            if disabled_reason is not None else None
        )
        if admission_state == "disabled" and reason is None:
            raise ValueError("disabled validation requires a reason")
        if admission_state != "disabled" and reason is not None:
            raise ValueError("disabled_reason is only valid for disabled validation")
        execution_id = _identifier(
            "execution_id", execution_id or f"validation-{uuid.uuid4().hex}"
        )
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            allocation = connection.execute(
                "SELECT * FROM pw_checkout_allocation WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            if allocation is None:
                raise EngineeringNotFound(f"unknown checkout allocation {allocation_id!r}")
            manifest_sha256 = None
            if manifest_id is not None:
                manifest_id = _identifier("manifest_id", manifest_id)
                manifest = connection.execute(
                    "SELECT * FROM pw_execution_manifest WHERE manifest_id = ?",
                    (manifest_id,),
                ).fetchone()
                if (
                    manifest is None
                    or manifest["allocation_id"] != allocation_id
                    or manifest["run_id"] != allocation["run_id"]
                    or manifest["revision_sha"] != allocation["revision_sha"]
                ):
                    raise EngineeringConflict(
                        "validation manifest is not bound to this exact checkout revision"
                    )
                manifest_sha256 = manifest["manifest_sha256"]
            if existing is not None:
                if (
                    existing["allocation_id"] != allocation_id
                    or existing["manifest_id"] != manifest_id
                    or existing["requested_by"] != requested_by
                    or existing["initial_admission_state"] != admission_state
                    or (
                        admission_state == "disabled"
                        and existing["disabled_reason"] != reason
                    )
                ):
                    raise EngineeringConflict(
                        "validation idempotency key has different immutable content"
                    )
                connection.commit()
                return self._validation_execution(existing)
            if allocation["state"] != "active":
                raise EngineeringConflict("validation requires an active exact checkout")
            try:
                connection.execute(
                    """
                    INSERT INTO pw_validation_execution(
                        execution_id, idempotency_key, allocation_id, run_id,
                        session_id, patch_id, revision_sha, owner_id, manifest_id,
                        manifest_sha256, initial_admission_state, admission_state,
                        state, requested_by,
                        requested_at, disabled_by, disabled_at, disabled_reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_id, idempotency_key, allocation_id,
                        allocation["run_id"], allocation["session_id"],
                        allocation["patch_id"], allocation["revision_sha"],
                        allocation["owner_id"], manifest_id, manifest_sha256,
                        admission_state, admission_state, requested_by, _epoch(timestamp),
                        requested_by if admission_state == "disabled" else None,
                        _epoch(timestamp) if admission_state == "disabled" else None,
                        reason,
                        _epoch(timestamp), _epoch(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EngineeringConflict(
                    "run already has a validation execution grant"
                ) from exc
            row = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            connection.commit()
        return self._validation_execution(row)

    def get_validation_execution(self, execution_id: str) -> ValidationExecution:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (str(execution_id),),
            ).fetchone()
        if row is None:
            raise EngineeringNotFound(f"unknown validation execution {execution_id!r}")
        return self._validation_execution(row)

    def get_validation_execution_by_run(
        self, run_id: str
    ) -> ValidationExecution | None:
        run_id = _identifier("run_id", run_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._validation_execution(row) if row is not None else None

    def list_validation_executions(
        self,
        *,
        states: Iterable[str] | None = None,
        admission_states: Iterable[str] | None = None,
        limit: int = 100,
    ) -> tuple[ValidationExecution, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        normalized_states = set(states or ())
        normalized_admission = set(admission_states or ())
        if not normalized_states.issubset(VALIDATION_EXECUTION_STATES):
            raise ValueError("unknown validation execution state")
        if not normalized_admission.issubset(VALIDATION_ADMISSION_STATES):
            raise ValueError("unknown validation admission state")
        clauses: list[str] = []
        parameters: list[object] = []
        if normalized_states:
            placeholders = ",".join("?" for _ in normalized_states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(sorted(normalized_states))
        if normalized_admission:
            placeholders = ",".join("?" for _ in normalized_admission)
            clauses.append(f"admission_state IN ({placeholders})")
            parameters.extend(sorted(normalized_admission))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM pw_validation_execution {where}
                ORDER BY updated_at DESC, execution_id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._validation_execution(row) for row in rows)

    def get_active_validation_capability(
        self,
        *,
        session_id: str,
        run_id: str,
        revision_sha: str,
    ) -> ValidationExecution | None:
        """Return the exact capability only while its guest attempt is running."""

        session_id = _identifier("session_id", session_id)
        run_id = _identifier("run_id", run_id)
        revision_sha = _revision(revision_sha)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT e.*
                FROM pw_validation_execution e
                JOIN pw_checkout_allocation c
                  ON c.allocation_id = e.allocation_id
                JOIN pw_validation_attempt a
                  ON a.execution_id = e.execution_id
                WHERE e.session_id = ? AND e.run_id = ? AND e.revision_sha = ?
                  AND e.admission_state = 'approved' AND e.state = 'running'
                  AND a.state = 'running'
                  AND a.run_id = e.run_id AND a.session_id = e.session_id
                  AND a.revision_sha = e.revision_sha AND a.owner_id = e.owner_id
                  AND c.state = 'active'
                  AND c.run_id = e.run_id AND c.session_id = e.session_id
                  AND c.revision_sha = e.revision_sha AND c.owner_id = e.owner_id
                """,
                (session_id, run_id, revision_sha),
            ).fetchone()
        return self._validation_execution(row) if row is not None else None

    def approve_validation_execution(
        self,
        execution_id: str,
        *,
        expected_revision: str,
        expected_owner_id: str,
        approved_by: str,
        now: datetime | None = None,
    ) -> ValidationExecution:
        expected_revision = _revision(expected_revision)
        approved_by = _required_text("approved_by", approved_by, maximum=500)
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise EngineeringNotFound(f"unknown validation execution {execution_id!r}")
            if (
                row["revision_sha"] != expected_revision
                or row["owner_id"] != expected_owner_id
            ):
                raise EngineeringConflict(
                    "validation approval does not match exact revision and owner"
                )
            if row["admission_state"] == "approved":
                if row["approved_by"] != approved_by:
                    raise EngineeringConflict("validation was approved by a different actor")
                connection.commit()
                return self._validation_execution(row)
            if row["admission_state"] != "awaiting_approval" or row["state"] != "planned":
                raise EngineeringConflict("validation execution cannot be approved")
            connection.execute(
                """
                UPDATE pw_validation_execution
                SET admission_state = 'approved', approved_by = ?, approved_at = ?,
                    updated_at = ? WHERE execution_id = ?
                """,
                (approved_by, _epoch(timestamp), _epoch(timestamp), execution_id),
            )
            connection.commit()
        return self.get_validation_execution(execution_id)

    def disable_validation_execution(
        self,
        execution_id: str,
        *,
        expected_revision: str,
        expected_owner_id: str,
        disabled_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> ValidationExecution:
        expected_revision = _revision(expected_revision)
        disabled_by = _required_text("disabled_by", disabled_by, maximum=500)
        reason = _required_text("reason", reason, maximum=2_000)
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise EngineeringNotFound(f"unknown validation execution {execution_id!r}")
            if (
                row["revision_sha"] != expected_revision
                or row["owner_id"] != expected_owner_id
            ):
                raise EngineeringConflict(
                    "validation disable does not match exact revision and owner"
                )
            if row["admission_state"] == "disabled":
                if row["disabled_by"] != disabled_by or row["disabled_reason"] != reason:
                    raise EngineeringConflict(
                        "validation was disabled with different immutable evidence"
                    )
                connection.commit()
                return self._validation_execution(row)
            connection.execute(
                """
                UPDATE pw_validation_execution
                SET admission_state = 'disabled', disabled_by = ?, disabled_at = ?,
                    disabled_reason = ?, updated_at = ? WHERE execution_id = ?
                """,
                (
                    disabled_by, _epoch(timestamp), reason, _epoch(timestamp),
                    execution_id,
                ),
            )
            connection.commit()
        return self.get_validation_execution(execution_id)

    def get_capacity_cooldown(self, patch_id: str) -> CapacityCooldown | None:
        patch_id = _identifier("patch_id", patch_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_validation_capacity_cooldown WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        return self._cooldown(row) if row is not None else None

    def authorize_capacity_retry(
        self,
        execution_id: str,
        *,
        expected_revision: str,
        approved_by: str,
        idempotency_key: str,
        grant_id: str | None = None,
        now: datetime | None = None,
    ) -> ValidationRetryGrant:
        expected_revision = _revision(expected_revision)
        approved_by = _required_text("approved_by", approved_by, maximum=500)
        idempotency_key = _required_text(
            "idempotency_key", idempotency_key, maximum=500
        )
        grant_id = _identifier(
            "grant_id", grant_id or f"validation-retry-{uuid.uuid4().hex}"
        )
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pw_validation_retry_grant WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            execution = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None:
                raise EngineeringNotFound(f"unknown validation execution {execution_id!r}")
            if existing is not None:
                if (
                    existing["execution_id"] != execution_id
                    or existing["revision_sha"] != expected_revision
                    or existing["approved_by"] != approved_by
                ):
                    raise EngineeringConflict(
                        "retry idempotency key has different immutable content"
                    )
                connection.commit()
                return self._retry_grant(existing)
            if (
                execution["revision_sha"] != expected_revision
                or execution["state"] != "resource_exhausted"
                or execution["admission_state"] != "approved"
            ):
                raise EngineeringConflict(
                    "capacity retry requires the exact exhausted revision"
                )
            connection.execute(
                """
                INSERT INTO pw_validation_retry_grant(
                    grant_id, execution_id, idempotency_key, revision_sha,
                    approved_by, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id, execution_id, idempotency_key, expected_revision,
                    approved_by, _epoch(timestamp),
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_validation_retry_grant WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            connection.commit()
        return self._retry_grant(row)

    def get_validation_retry_grant(self, grant_id: str) -> ValidationRetryGrant:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_validation_retry_grant WHERE grant_id = ?",
                (str(grant_id),),
            ).fetchone()
        if row is None:
            raise EngineeringNotFound(f"unknown validation retry grant {grant_id!r}")
        return self._retry_grant(row)

    def claim_validation_attempt(
        self,
        execution_id: str,
        *,
        worker_id: str,
        idempotency_key: str,
        expected_revision: str,
        expected_owner_id: str,
        retry_grant_id: str | None = None,
        attempt_id: str | None = None,
        now: datetime | None = None,
    ) -> ValidationAttempt:
        worker_id = _required_text("worker_id", worker_id, maximum=500)
        idempotency_key = _required_text(
            "idempotency_key", idempotency_key, maximum=500
        )
        expected_revision = _revision(expected_revision)
        attempt_id = _identifier(
            "attempt_id", attempt_id or f"validation-attempt-{uuid.uuid4().hex}"
        )
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pw_validation_attempt WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            execution = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None:
                raise EngineeringNotFound(f"unknown validation execution {execution_id!r}")
            if (
                execution["revision_sha"] != expected_revision
                or execution["owner_id"] != expected_owner_id
            ):
                raise EngineeringConflict(
                    "validation claim does not match exact revision and owner"
                )
            if existing is not None:
                if (
                    existing["execution_id"] != execution_id
                    or existing["worker_id"] != worker_id
                    or existing["revision_sha"] != expected_revision
                    or existing["owner_id"] != expected_owner_id
                ):
                    raise EngineeringConflict(
                        "attempt idempotency key has different immutable content"
                    )
                connection.commit()
                return self._validation_attempt(existing)
            if execution["admission_state"] != "approved":
                raise EngineeringConflict("validation capability is not approved")
            if execution["state"] not in {"planned", "resource_exhausted"}:
                raise EngineeringConflict("validation execution cannot start another attempt")
            allocation = connection.execute(
                "SELECT * FROM pw_checkout_allocation WHERE allocation_id = ?",
                (execution["allocation_id"],),
            ).fetchone()
            if (
                allocation is None
                or allocation["state"] != "active"
                or allocation["run_id"] != execution["run_id"]
                or allocation["session_id"] != execution["session_id"]
                or allocation["revision_sha"] != execution["revision_sha"]
                or allocation["owner_id"] != execution["owner_id"]
            ):
                raise EngineeringConflict(
                    "validation capability no longer has its exact active owner session"
                )
            cooldown = connection.execute(
                "SELECT * FROM pw_validation_capacity_cooldown WHERE patch_id = ?",
                (execution["patch_id"],),
            ).fetchone()
            retry = None
            if retry_grant_id is not None:
                retry = connection.execute(
                    "SELECT * FROM pw_validation_retry_grant WHERE grant_id = ?",
                    (retry_grant_id,),
                ).fetchone()
                if (
                    retry is None
                    or retry["execution_id"] != execution_id
                    or retry["revision_sha"] != expected_revision
                    or retry["consumed_at"] is not None
                ):
                    raise EngineeringConflict("capacity retry grant is invalid or consumed")
            if (
                cooldown is not None
                and float(cooldown["not_before"]) > _epoch(timestamp)
                and retry is None
            ):
                raise EngineeringConflict("validation capacity cooldown is active")
            attempt_number = int(connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) FROM pw_validation_attempt WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()[0]) + 1
            try:
                connection.execute(
                    """
                    INSERT INTO pw_validation_attempt(
                        attempt_id, execution_id, attempt_number, idempotency_key,
                        run_id, session_id, revision_sha, owner_id, state,
                        worker_id, claimed_at, retry_grant_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?)
                    """,
                    (
                        attempt_id, execution_id, attempt_number, idempotency_key,
                        execution["run_id"], execution["session_id"],
                        execution["revision_sha"], execution["owner_id"], worker_id,
                        _epoch(timestamp), retry_grant_id, _epoch(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EngineeringConflict("validation execution already has an active attempt") from exc
            if retry is not None:
                connection.execute(
                    """
                    UPDATE pw_validation_retry_grant
                    SET consumed_by_attempt_id = ?, consumed_at = ? WHERE grant_id = ?
                    """,
                    (attempt_id, _epoch(timestamp), retry_grant_id),
                )
            connection.execute(
                "UPDATE pw_validation_execution SET state = 'claimed', updated_at = ? WHERE execution_id = ?",
                (_epoch(timestamp), execution_id),
            )
            connection.commit()
        return self.get_validation_attempt(attempt_id)

    def get_validation_attempt(self, attempt_id: str) -> ValidationAttempt:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_validation_attempt WHERE attempt_id = ?",
                (str(attempt_id),),
            ).fetchone()
        if row is None:
            raise EngineeringNotFound(f"unknown validation attempt {attempt_id!r}")
        return self._validation_attempt(row)

    def list_validation_attempts(
        self, execution_id: str, *, limit: int = 100
    ) -> tuple[ValidationAttempt, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pw_validation_attempt WHERE execution_id = ?
                ORDER BY attempt_number DESC LIMIT ?
                """,
                (execution_id, limit),
            ).fetchall()
        return tuple(self._validation_attempt(row) for row in rows)

    def mark_validation_attempt_running(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> ValidationAttempt:
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pw_validation_attempt WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise EngineeringNotFound(f"unknown validation attempt {attempt_id!r}")
            if row["worker_id"] != worker_id:
                raise EngineeringConflict("validation attempt belongs to another worker")
            if row["state"] == "running":
                connection.commit()
                return self._validation_attempt(row)
            if row["state"] != "claimed":
                raise EngineeringConflict("only a claimed validation attempt can run")
            execution = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (row["execution_id"],),
            ).fetchone()
            if execution["admission_state"] != "approved":
                raise EngineeringConflict("validation capability was disabled before start")
            connection.execute(
                """
                UPDATE pw_validation_attempt SET state = 'running', started_at = ?,
                    updated_at = ? WHERE attempt_id = ?
                """,
                (_epoch(timestamp), _epoch(timestamp), attempt_id),
            )
            connection.execute(
                "UPDATE pw_validation_execution SET state = 'running', updated_at = ? WHERE execution_id = ?",
                (_epoch(timestamp), row["execution_id"]),
            )
            connection.commit()
        return self.get_validation_attempt(attempt_id)

    @classmethod
    def _validation_command_claim(
        cls, row: sqlite3.Row, disposition: str
    ) -> ValidationCommandClaim:
        try:
            command = cls._validation_command_from_dict(
                json.loads(row["command_json"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EngineeringStateError("stored validation command claim is invalid") from exc
        if command.digest != row["command_sha256"]:
            raise EngineeringStateError(
                "stored validation command claim digest does not match"
            )
        return ValidationCommandClaim(
            attempt_id=row["attempt_id"],
            command_id=row["command_id"],
            command=command,
            command_sha256=row["command_sha256"],
            reserved_at=_datetime(row["reserved_at"]),
            disposition=disposition,
        )

    def claim_validation_command(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        command: ValidationCommandAudit | object,
        now: datetime | None = None,
    ) -> ValidationCommandClaim:
        """Reserve a command before dispatch; only a new claim may execute.

        Repeating an exact completed command returns ``completed``. Repeating
        a reservation without a result returns ``already_reserved`` because a
        crash may have occurred after dispatch. Neither disposition authorizes
        another execution.
        """

        command = ValidationCommandAudit.from_command(command)
        worker_id = _required_text("worker_id", worker_id, maximum=500)
        command_json = _canonical_json(command.to_dict())
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM pw_validation_attempt WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise EngineeringNotFound(f"unknown validation attempt {attempt_id!r}")
            if attempt["worker_id"] != worker_id:
                raise EngineeringConflict("validation attempt belongs to another worker")
            existing = connection.execute(
                """
                SELECT * FROM pw_validation_command_claim
                WHERE attempt_id = ? AND command_id = ?
                """,
                (attempt_id, command.command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_json"] != command_json
                    or existing["command_sha256"] != command.digest
                ):
                    raise EngineeringConflict(
                        "validation command identity has different immutable content"
                    )
                result = connection.execute(
                    """
                    SELECT 1 FROM pw_validation_step_result
                    WHERE attempt_id = ? AND step_id = ?
                    """,
                    (attempt_id, command.command_id),
                ).fetchone()
                connection.commit()
                return self._validation_command_claim(
                    existing, "completed" if result is not None else "already_reserved"
                )
            if attempt["state"] != "running":
                raise EngineeringConflict(
                    "validation command requires a running attempt"
                )
            execution = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (attempt["execution_id"],),
            ).fetchone()
            if (
                execution is None
                or execution["admission_state"] != "approved"
                or execution["state"] != "running"
            ):
                raise EngineeringConflict("validation capability is not active")
            allocation = connection.execute(
                "SELECT * FROM pw_checkout_allocation WHERE allocation_id = ?",
                (execution["allocation_id"],),
            ).fetchone()
            if (
                allocation is None
                or allocation["state"] != "active"
                or allocation["run_id"] != attempt["run_id"]
                or allocation["session_id"] != attempt["session_id"]
                or allocation["revision_sha"] != attempt["revision_sha"]
                or allocation["owner_id"] != attempt["owner_id"]
            ):
                raise EngineeringConflict(
                    "validation command lost its exact active owner session"
                )
            connection.execute(
                """
                INSERT INTO pw_validation_command_claim(
                    attempt_id, command_id, command_json, command_sha256,
                    reserved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, command.command_id, command_json,
                    command.digest, _epoch(timestamp),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM pw_validation_command_claim
                WHERE attempt_id = ? AND command_id = ?
                """,
                (attempt_id, command.command_id),
            ).fetchone()
            connection.commit()
        return self._validation_command_claim(row, "dispatch")

    def list_validation_command_claims(
        self, attempt_id: str, *, limit: int = 200
    ) -> tuple[ValidationCommandClaim, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.*,
                    EXISTS(
                        SELECT 1 FROM pw_validation_step_result s
                        WHERE s.attempt_id = c.attempt_id
                          AND s.step_id = c.command_id
                    ) AS completed
                FROM pw_validation_command_claim c
                WHERE c.attempt_id = ?
                ORDER BY c.reserved_at, c.command_id LIMIT ?
                """,
                (attempt_id, limit),
            ).fetchall()
        return tuple(
            self._validation_command_claim(
                row, "completed" if row["completed"] else "already_reserved"
            )
            for row in rows
        )

    def record_validation_step_result(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        command: ValidationCommandAudit | object,
        state: str,
        summary: str,
        artifact_ids: Sequence[str] = (),
        exit_code: int | None = None,
        started_at: datetime,
        finished_at: datetime,
    ) -> ValidationStepResult:
        command = ValidationCommandAudit.from_command(command)
        if state not in VALIDATION_STEP_STATES:
            raise ValueError("invalid validation step state")
        summary = _required_text("summary", summary, maximum=4_000)
        if exit_code is not None and (
            isinstance(exit_code, bool) or not 0 <= exit_code <= 255
        ):
            raise ValueError("exit_code must be between 0 and 255")
        if state == "succeeded" and exit_code not in command.expected_exit_codes:
            raise ValueError("successful step exit code was not expected")
        started_epoch = _epoch(started_at)
        finished_epoch = _epoch(finished_at)
        if finished_epoch < started_epoch:
            raise ValueError("validation step finished before it started")
        if isinstance(artifact_ids, (str, bytes)) or not isinstance(
            artifact_ids, Sequence
        ):
            raise ValueError("artifact_ids must be an array")
        artifacts = tuple(dict.fromkeys(
            _identifier("artifact_id", artifact_id) for artifact_id in artifact_ids
        ))
        if len(artifacts) > 50:
            raise ValueError("validation step has too many artifact references")
        command_json = _canonical_json(command.to_dict())
        command_digest = hashlib.sha256(command_json.encode("utf-8")).hexdigest()
        artifacts_json = _canonical_json(list(artifacts))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM pw_validation_attempt WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise EngineeringNotFound(f"unknown validation attempt {attempt_id!r}")
            if attempt["worker_id"] != worker_id:
                raise EngineeringConflict("validation attempt belongs to another worker")
            claim = connection.execute(
                """
                SELECT * FROM pw_validation_command_claim
                WHERE attempt_id = ? AND command_id = ?
                """,
                (attempt_id, command.command_id),
            ).fetchone()
            if (
                claim is None
                or claim["command_json"] != command_json
                or claim["command_sha256"] != command_digest
            ):
                raise EngineeringConflict(
                    "validation command was not exactly reserved before dispatch"
                )
            existing = connection.execute(
                "SELECT * FROM pw_validation_step_result WHERE attempt_id = ? AND step_id = ?",
                (attempt_id, command.command_id),
            ).fetchone()
            if existing is not None:
                expected = (
                    command_json, command_digest, state, exit_code, summary,
                    artifacts_json, started_epoch, finished_epoch,
                )
                actual = tuple(existing[key] for key in (
                    "command_json", "command_sha256", "state", "exit_code",
                    "summary", "artifact_ids_json", "started_at", "finished_at",
                ))
                if actual != expected:
                    raise EngineeringConflict(
                        "validation step already has different immutable evidence"
                    )
                connection.commit()
                return self._validation_step(existing)
            if attempt["state"] != "running":
                raise EngineeringConflict("validation step requires a running attempt")
            for artifact_id in artifacts:
                artifact = connection.execute(
                    "SELECT * FROM pw_engineering_artifact WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if (
                    artifact is None
                    or artifact["run_id"] != attempt["run_id"]
                    or artifact["revision_sha"] != attempt["revision_sha"]
                ):
                    raise EngineeringConflict(
                        "validation artifact is not bound to the attempt revision"
                    )
            connection.execute(
                """
                INSERT INTO pw_validation_step_result(
                    attempt_id, step_id, command_json, command_sha256, state,
                    exit_code, summary, artifact_ids_json, started_at,
                    finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, command.command_id, command_json, command_digest,
                    state, exit_code, summary, artifacts_json, started_epoch,
                    finished_epoch, finished_epoch,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pw_validation_step_result WHERE attempt_id = ? AND step_id = ?",
                (attempt_id, command.command_id),
            ).fetchone()
            connection.commit()
        return self._validation_step(row)

    def list_validation_step_results(
        self, attempt_id: str, *, limit: int = 200
    ) -> tuple[ValidationStepResult, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pw_validation_step_result WHERE attempt_id = ?
                ORDER BY started_at, step_id LIMIT ?
                """,
                (attempt_id, limit),
            ).fetchall()
        return tuple(self._validation_step(row) for row in rows)

    def finish_validation_attempt(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        state: str,
        summary: str,
        failure_code: str | None = None,
        now: datetime | None = None,
    ) -> ValidationAttempt:
        if state not in VALIDATION_TERMINAL_ATTEMPT_STATES:
            raise ValueError("validation attempt finish state must be terminal")
        summary = _required_text("summary", summary, maximum=4_000)
        failure_code = (
            _identifier("failure_code", failure_code)
            if failure_code is not None else None
        )
        if state != "succeeded" and failure_code is None:
            raise ValueError("non-success validation requires a failure code")
        timestamp = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM pw_validation_attempt WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise EngineeringNotFound(f"unknown validation attempt {attempt_id!r}")
            if attempt["worker_id"] != worker_id:
                raise EngineeringConflict("validation attempt belongs to another worker")
            if attempt["state"] in VALIDATION_TERMINAL_ATTEMPT_STATES:
                if (
                    attempt["state"] != state
                    or attempt["summary"] != summary
                    or attempt["failure_code"] != failure_code
                ):
                    raise EngineeringConflict("validation attempt result is immutable")
                connection.commit()
                return self._validation_attempt(attempt)
            if attempt["state"] not in {"claimed", "running"}:
                raise EngineeringConflict("validation attempt cannot finish")
            steps = connection.execute(
                "SELECT state FROM pw_validation_step_result WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
            if state == "succeeded" and (
                not steps or any(step["state"] not in {"succeeded", "skipped"} for step in steps)
            ):
                raise EngineeringConflict(
                    "successful validation requires successful immutable step evidence"
                )
            if state == "resource_exhausted" and not any(
                step["state"] == "resource_exhausted" for step in steps
            ):
                raise EngineeringConflict(
                    "resource exhaustion requires a matching step result"
                )
            connection.execute(
                """
                UPDATE pw_validation_attempt SET state = ?, finished_at = ?,
                    failure_code = ?, summary = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    state, _epoch(timestamp), failure_code, summary,
                    _epoch(timestamp), attempt_id,
                ),
            )
            connection.execute(
                "UPDATE pw_validation_execution SET state = ?, updated_at = ? WHERE execution_id = ?",
                (state, _epoch(timestamp), attempt["execution_id"]),
            )
            execution = connection.execute(
                "SELECT * FROM pw_validation_execution WHERE execution_id = ?",
                (attempt["execution_id"],),
            ).fetchone()
            cooldown = connection.execute(
                "SELECT * FROM pw_validation_capacity_cooldown WHERE patch_id = ?",
                (execution["patch_id"],),
            ).fetchone()
            if state == "resource_exhausted":
                consecutive = int(cooldown["consecutive_exhaustions"]) + 1 if cooldown else 1
                total = int(cooldown["total_exhaustions"]) + 1 if cooldown else 1
                delay = min(
                    self.capacity_cooldown_base_seconds * (2 ** min(consecutive - 1, 20)),
                    self.capacity_cooldown_max_seconds,
                )
                not_before = _epoch(timestamp) + delay
                connection.execute(
                    """
                    INSERT INTO pw_validation_capacity_cooldown(
                        patch_id, not_before, consecutive_exhaustions,
                        total_exhaustions, last_execution_id, last_attempt_id,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(patch_id) DO UPDATE SET
                        not_before = excluded.not_before,
                        consecutive_exhaustions = excluded.consecutive_exhaustions,
                        total_exhaustions = excluded.total_exhaustions,
                        last_execution_id = excluded.last_execution_id,
                        last_attempt_id = excluded.last_attempt_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        execution["patch_id"], not_before, consecutive, total,
                        execution["execution_id"], attempt_id, _epoch(timestamp),
                    ),
                )
            elif state == "succeeded" and cooldown is not None:
                connection.execute(
                    """
                    UPDATE pw_validation_capacity_cooldown
                    SET not_before = ?, consecutive_exhaustions = 0,
                        last_execution_id = ?, last_attempt_id = ?, updated_at = ?
                    WHERE patch_id = ?
                    """,
                    (
                        _epoch(timestamp), execution["execution_id"], attempt_id,
                        _epoch(timestamp), execution["patch_id"],
                    ),
                )
            connection.commit()
        return self.get_validation_attempt(attempt_id)

    def reconcile_validation_after_restart(
        self,
        active_attempts: Mapping[str, str],
        *,
        now: datetime | None = None,
    ) -> tuple[ValidationRestartDecision, ...]:
        """Reconcile claims without replaying possibly-started guest commands."""

        normalized = {
            _identifier("attempt_id", attempt_id): _required_text(
                "worker_id", worker_id, maximum=500
            )
            for attempt_id, worker_id in active_attempts.items()
        }
        timestamp = now or _now()
        decisions: list[ValidationRestartDecision] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT a.*, e.admission_state
                FROM pw_validation_attempt a
                JOIN pw_validation_execution e ON e.execution_id = a.execution_id
                WHERE a.state IN ('claimed', 'running')
                ORDER BY a.claimed_at, a.attempt_id
                """
            ).fetchall()
            for row in rows:
                active_worker = normalized.get(row["attempt_id"])
                if active_worker == row["worker_id"]:
                    action = (
                        "stop_required"
                        if row["admission_state"] == "disabled" else "retain"
                    )
                    reason = (
                        "capability_disabled"
                        if action == "stop_required" else "exact_worker_claim_match"
                    )
                    decisions.append(ValidationRestartDecision(
                        row["attempt_id"], row["execution_id"], row["state"],
                        row["state"], action, reason,
                    ))
                    continue
                previous = row["state"]
                state = "cancelled" if previous == "claimed" else "ambiguous"
                reason = (
                    "restart_unclaimed" if previous == "claimed"
                    else "restart_running_outcome_unknown"
                )
                connection.execute(
                    """
                    UPDATE pw_validation_attempt SET state = ?, finished_at = ?,
                        failure_code = ?, summary = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        state, _epoch(timestamp), reason, reason,
                        _epoch(timestamp), row["attempt_id"],
                    ),
                )
                execution_state = (
                    "planned"
                    if previous == "claimed" and row["admission_state"] == "approved"
                    else "cancelled" if previous == "claimed" else "ambiguous"
                )
                connection.execute(
                    "UPDATE pw_validation_execution SET state = ?, updated_at = ? WHERE execution_id = ?",
                    (execution_state, _epoch(timestamp), row["execution_id"]),
                )
                decisions.append(ValidationRestartDecision(
                    row["attempt_id"], row["execution_id"], previous, state,
                    "retry_safe" if previous == "claimed" else "manual_reconciliation",
                    reason,
                ))
            connection.commit()
        return tuple(decisions)


__all__ = [
    "ArtifactMetadata", "CapacityCooldown", "CheckoutAllocation", "CHECKOUT_STATES",
    "EngineeringConflict", "EngineeringNotFound", "EngineeringStateError",
    "EngineeringStateStore", "ExecutionManifest", "RestartDecision",
    "SAFE_ENVIRONMENT_KEYS", "SafeCommand", "ValidationAttempt",
    "ValidationCommandAudit", "ValidationCommandClaim", "ValidationExecution",
    "ValidationRestartDecision",
    "ValidationRetryGrant", "ValidationStepResult", "resolve_confined_path",
]
