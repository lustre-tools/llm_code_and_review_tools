"""Durable, explicitly confirmed Phase 3C Gerrit patchset uploads.

The worker never receives Gerrit credentials.  It produces an immutable diff
and validation evidence; this controller independently reconstructs a commit,
records the push claim before dispatch, and reconciles Gerrit before any retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from gerrit_status import GerritConfig, GerritStatusClient


UPLOAD_STATES = frozenset({
    "prepared", "commit_ready", "push_claimed", "succeeded", "ambiguous",
    "failed", "stale", "cancelled",
})
TERMINAL_UPLOAD_STATES = frozenset(
    {"succeeded", "ambiguous", "failed", "stale", "cancelled"}
)


class GerritUploadError(RuntimeError):
    """An upload could not safely proceed."""


class GerritUploadConflict(GerritUploadError):
    """An upload binding or state transition is stale or conflicting."""


@dataclass(frozen=True)
class UploadPlan:
    upload_id: str
    idempotency_key: str
    run_id: str
    session_id: str
    change_number: int
    project: str
    branch: str
    change_id: str
    patchset: int
    revision_sha: str
    revision_ref: str
    diff_path: str
    diff_artifact_id: str
    diff_sha256: str
    evidence_sha256: str
    state: str
    requested_by: str
    prepared_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    local_commit_sha: str | None = None
    new_patchset: int | None = None
    new_revision_sha: str | None = None
    summary: str | None = None

    @property
    def binding_digest(self) -> str:
        payload = [
            self.upload_id, self.run_id, self.session_id, self.change_number,
            self.project, self.branch, self.change_id, self.patchset, self.revision_sha,
            self.revision_ref, self.diff_artifact_id, self.diff_sha256,
            self.evidence_sha256, self.local_commit_sha or "",
        ]
        return hashlib.sha256(json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class PreparedCommit:
    upload_id: str
    commit_sha: str
    workspace: Path


class UploadStateStore:
    """Small private SQLite ledger with immutable upload bindings."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._memory = self.database == ":memory:"
        if not self._memory:
            path = Path(self.database).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._migrate()
        if not self._memory:
            os.chmod(Path(self.database).expanduser(), 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        if not self._memory:
            connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pw_gerrit_upload (
                    upload_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    change_number INTEGER NOT NULL CHECK(change_number > 0),
                    project TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    change_id TEXT NOT NULL,
                    patchset INTEGER NOT NULL CHECK(patchset > 0),
                    revision_sha TEXT NOT NULL,
                    revision_ref TEXT NOT NULL,
                    diff_path TEXT NOT NULL,
                    diff_artifact_id TEXT NOT NULL,
                    diff_sha256 TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared','commit_ready','push_claimed','succeeded',
                        'ambiguous','failed','stale','cancelled'
                    )),
                    requested_by TEXT NOT NULL,
                    prepared_at REAL NOT NULL,
                    claimed_at REAL,
                    completed_at REAL,
                    local_commit_sha TEXT,
                    new_patchset INTEGER,
                    new_revision_sha TEXT,
                    summary TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_upload_binding_immutable
                BEFORE UPDATE OF idempotency_key,run_id,session_id,change_number,
                    project,branch,change_id,patchset,revision_sha,revision_ref,diff_path,
                    diff_artifact_id,diff_sha256,evidence_sha256,requested_by,
                    prepared_at
                ON pw_gerrit_upload
                BEGIN SELECT RAISE(ABORT, 'upload binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_upload_no_delete
                BEFORE DELETE ON pw_gerrit_upload
                BEGIN SELECT RAISE(ABORT, 'upload records are durable'); END;
                CREATE TABLE IF NOT EXISTS pw_gerrit_upload_event (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    upload_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_upload_event_no_update
                BEFORE UPDATE ON pw_gerrit_upload_event
                BEGIN SELECT RAISE(ABORT, 'upload events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_upload_event_no_delete
                BEFORE DELETE ON pw_gerrit_upload_event
                BEGIN SELECT RAISE(ABORT, 'upload events are append-only'); END;
            """)
            connection.commit()

    @staticmethod
    def _plan(row: sqlite3.Row) -> UploadPlan:
        def stamp(value):
            return datetime.fromtimestamp(value, timezone.utc) if value is not None else None
        return UploadPlan(
            upload_id=row["upload_id"], idempotency_key=row["idempotency_key"],
            run_id=row["run_id"], session_id=row["session_id"],
            change_number=int(row["change_number"]), project=row["project"],
            branch=row["branch"], change_id=row["change_id"],
            patchset=int(row["patchset"]),
            revision_sha=row["revision_sha"], revision_ref=row["revision_ref"],
            diff_path=row["diff_path"], diff_artifact_id=row["diff_artifact_id"],
            diff_sha256=row["diff_sha256"], evidence_sha256=row["evidence_sha256"],
            state=row["state"], requested_by=row["requested_by"],
            prepared_at=stamp(row["prepared_at"]), claimed_at=stamp(row["claimed_at"]),
            completed_at=stamp(row["completed_at"]),
            local_commit_sha=row["local_commit_sha"], new_patchset=row["new_patchset"],
            new_revision_sha=row["new_revision_sha"], summary=row["summary"],
        )

    def get(self, upload_id: str) -> UploadPlan:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_gerrit_upload WHERE upload_id = ?", (upload_id,)
            ).fetchone()
        if row is None:
            raise GerritUploadError("unknown Gerrit upload")
        return self._plan(row)

    def get_by_run(self, run_id: str) -> UploadPlan | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_gerrit_upload WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._plan(row) if row is not None else None

    def prepare(self, *, now: datetime | None = None, **values) -> UploadPlan:
        timestamp = now or datetime.now(timezone.utc)
        upload_id = values.pop("upload_id", "upload-" + uuid.uuid4().hex)
        columns = (
            "idempotency_key","run_id","session_id","change_number","project",
            "branch","change_id","patchset","revision_sha","revision_ref","diff_path",
            "diff_artifact_id","diff_sha256","evidence_sha256","requested_by",
        )
        bound = [values[name] for name in columns]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pw_gerrit_upload WHERE run_id = ? OR idempotency_key = ?",
                (values["run_id"], values["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                plan = self._plan(existing)
                if any(str(getattr(plan, name)) != str(values[name]) for name in columns):
                    connection.rollback()
                    raise GerritUploadConflict("upload request identity was reused")
                connection.rollback()
                return plan
            connection.execute(
                "INSERT INTO pw_gerrit_upload(upload_id," + ",".join(columns) +
                ",state,prepared_at,updated_at) VALUES (" + ",".join("?" for _ in range(19)) + ")",
                (upload_id, *bound, "prepared", timestamp.timestamp(), timestamp.timestamp()),
            )
            self._event(connection, upload_id, "prepared", {}, timestamp)
            connection.commit()
        return self.get(upload_id)

    @staticmethod
    def _event(connection, upload_id, event_type, detail, at):
        connection.execute(
            "INSERT INTO pw_gerrit_upload_event(upload_id,event_type,detail_json,created_at) "
            "VALUES (?,?,?,?)",
            (upload_id, event_type, json.dumps(detail, sort_keys=True), at.timestamp()),
        )

    def transition(
        self, upload_id: str, *, expected: set[str], state: str,
        at: datetime | None = None, **updates,
    ) -> UploadPlan:
        if state not in UPLOAD_STATES:
            raise ValueError("invalid upload state")
        timestamp = at or datetime.now(timezone.utc)
        allowed = {"claimed_at", "completed_at", "local_commit_sha", "new_patchset",
                   "new_revision_sha", "summary"}
        if set(updates) - allowed:
            raise ValueError("invalid upload update")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pw_gerrit_upload WHERE upload_id = ?", (upload_id,)
            ).fetchone()
            if row is None:
                connection.rollback(); raise GerritUploadError("unknown Gerrit upload")
            if row["state"] not in expected:
                connection.rollback(); raise GerritUploadConflict(
                    f"upload is {row['state']}, expected {', '.join(sorted(expected))}"
                )
            assignments = ["state = ?", "updated_at = ?"]
            params = [state, timestamp.timestamp()]
            for key, value in updates.items():
                assignments.append(key + " = ?")
                params.append(value.timestamp() if isinstance(value, datetime) else value)
            params.append(upload_id)
            connection.execute(
                "UPDATE pw_gerrit_upload SET " + ",".join(assignments) + " WHERE upload_id = ?",
                params,
            )
            self._event(connection, upload_id, state, {
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in updates.items()
                if key not in {"summary"}
            }, timestamp)
            connection.commit()
        return self.get(upload_id)


class GitGerritUploader:
    """Controller-owned git transport; credentials never enter the worker."""

    def __init__(self, config: GerritConfig, workspace_root: str | Path) -> None:
        self.config = config
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _git(self, workspace: Path, args: list[str], *, auth=False) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workspace),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        askpass = workspace / ".pw-askpass"
        if auth:
            askpass.write_text(
                "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' \"$PW_GERRIT_USER\" ;; "
                "*) printf '%s\\n' \"$PW_GERRIT_PASS\" ;; esac\n",
                encoding="utf-8",
            )
            os.chmod(askpass, 0o700)
            env.update({
                "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                "PW_GERRIT_USER": self.config.username,
                "PW_GERRIT_PASS": self.config.password,
            })
        try:
            return subprocess.run(
                [
                    "git", "-c", "credential.helper=", "-c",
                    "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never",
                    *args,
                ],
                cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=180, check=False,
            )
        finally:
            if askpass.exists():
                askpass.unlink()

    def prepare_commit(self, plan: UploadPlan) -> PreparedCommit:
        workspace = self.workspace_root / plan.upload_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(mode=0o700)
        try:
            repository = f"{self.config.url}/{plan.project}"
            commands = (
                ["init", "-q"],
                ["fetch", "--no-tags", repository, plan.revision_ref],
                ["checkout", "--detach", "-q", "FETCH_HEAD"],
            )
            for args in commands:
                result = self._git(workspace, args)
                if result.returncode:
                    raise GerritUploadError(
                        "could not reconstruct the exact Gerrit revision"
                    )
            head = self._git(workspace, ["rev-parse", "HEAD"])
            if (
                head.returncode
                or head.stdout.decode().strip().lower() != plan.revision_sha.lower()
            ):
                raise GerritUploadConflict(
                    "fetched revision does not match the upload plan"
                )
            message = self._git(workspace, ["show", "-s", "--format=%B", "HEAD"])
            change_ids = re.findall(
                rb"(?m)^Change-Id:\s*(I[0-9A-Fa-f]{40})\s*$", message.stdout
            ) if message.returncode == 0 else []
            if len(change_ids) != 1 or change_ids[0].decode("ascii") != plan.change_id:
                raise GerritUploadConflict(
                    "pinned commit Change-Id does not match Gerrit"
                )
            result = self._git(
                workspace, ["apply", "--index", "--binary", plan.diff_path]
            )
            if result.returncode:
                raise GerritUploadError(
                    "captured diff no longer applies to the exact revision"
                )
            staged = self._git(workspace, ["diff", "--cached", "--quiet"])
            if staged.returncode != 1:
                raise GerritUploadError(
                    "captured diff does not create a new patchset"
                )
            result = self._git(workspace, [
                "-c", f"user.name={self.config.git_name}",
                "-c", f"user.email={self.config.git_email}",
                "commit", "--amend", "--no-edit", "-q",
            ])
            if result.returncode:
                raise GerritUploadError(
                    "could not create the controller-owned amended commit"
                )
            commit = self._git(workspace, ["rev-parse", "HEAD"])
            if commit.returncode:
                raise GerritUploadError("could not identify the amended commit")
            return PreparedCommit(
                plan.upload_id, commit.stdout.decode().strip().lower(), workspace
            )
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    def push(self, plan: UploadPlan, prepared: PreparedCommit) -> None:
        target = f"{self.config.url}/a/{plan.project}"
        result = self._git(
            prepared.workspace, [
                "push", "--porcelain", "--no-progress", target,
                f"HEAD:refs/for/{plan.branch}",
            ], auth=True,
        )
        if result.returncode:
            raise GerritUploadError("Gerrit push did not report success")

    def prepared_commit(self, plan: UploadPlan) -> PreparedCommit:
        workspace = self.workspace_root / plan.upload_id
        if not plan.local_commit_sha or not workspace.is_dir() or workspace.is_symlink():
            raise GerritUploadConflict("prepared upload workspace is unavailable")
        result = self._git(workspace, ["rev-parse", "HEAD"])
        if (
            result.returncode
            or result.stdout.decode().strip().lower() != plan.local_commit_sha.lower()
        ):
            raise GerritUploadConflict("prepared upload commit no longer matches its plan")
        return PreparedCommit(plan.upload_id, plan.local_commit_sha, workspace)

    def cleanup(self, prepared: PreparedCommit | None) -> None:
        if prepared is not None and prepared.workspace.parent == self.workspace_root:
            shutil.rmtree(prepared.workspace, ignore_errors=True)


class GerritUploadController:
    """Eligibility, explicit dispatch, and post-dispatch reconciliation."""

    def __init__(
        self, store: UploadStateStore, uploader: GitGerritUploader,
        *, status_fetcher: Callable[[str], Mapping], enabled: bool = False,
    ) -> None:
        self.store = store
        self.uploader = uploader
        self.status_fetcher = status_fetcher
        self.enabled = bool(enabled)

    @staticmethod
    def evidence_digest(step_results) -> str:
        normalized = [{
            "step_id": item.step_id, "state": item.state,
            "exit_code": item.exit_code, "command_sha256": item.command_sha256,
        } for item in step_results]
        return hashlib.sha256(json.dumps(
            normalized, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def prepare(self, **values) -> UploadPlan:
        if not self.enabled:
            raise GerritUploadConflict("Gerrit upload is disabled by the controller kill switch")
        diff_path = Path(values["diff_path"])
        content = diff_path.read_bytes()
        if not content or hashlib.sha256(content).hexdigest() != values["diff_sha256"]:
            raise GerritUploadConflict("captured diff is missing or its digest changed")
        plan = self.store.prepare(**values)
        if plan.state != "prepared":
            return plan
        url = f"{self.uploader.config.url}/c/{plan.project}/+/{plan.change_number}"
        try:
            status = self.status_fetcher(url)
        except Exception as exc:
            raise GerritUploadError(
                "could not recheck Gerrit while preparing the upload"
            ) from exc
        if not self._matches(plan, status):
            return self.store.transition(
                plan.upload_id, expected={"prepared"}, state="stale",
                completed_at=datetime.now(timezone.utc),
                summary="Gerrit changed while the upload plan was prepared",
            )
        prepared = None
        try:
            prepared = self.uploader.prepare_commit(plan)
            return self.store.transition(
                plan.upload_id, expected={"prepared"}, state="commit_ready",
                local_commit_sha=prepared.commit_sha,
                summary="Controller prepared the exact amended commit for review",
            )
        except Exception:
            self.uploader.cleanup(prepared)
            return self.store.transition(
                plan.upload_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                summary="Upload preparation failed before any Gerrit write",
            )

    @staticmethod
    def _matches(plan: UploadPlan, status: Mapping) -> bool:
        return (
            int(status.get("change_number") or 0) == plan.change_number
            and int(status.get("patchset") or 0) == plan.patchset
            and str(status.get("revision_sha") or "").lower() == plan.revision_sha.lower()
            and str(status.get("project") or "") == plan.project
            and str(status.get("branch") or "") == plan.branch
            and str(status.get("change_id") or "") == plan.change_id
            and str(status.get("status") or "").upper() == "NEW"
        )

    def execute(self, upload_id: str, *, expected_binding_digest: str) -> UploadPlan:
        plan = self.store.get(upload_id)
        if plan.binding_digest != expected_binding_digest:
            raise GerritUploadConflict("upload confirmation does not match the prepared plan")
        if plan.state == "succeeded":
            return plan
        if plan.state in {"push_claimed", "ambiguous"}:
            return self.reconcile(upload_id)
        if plan.state != "commit_ready":
            raise GerritUploadConflict("upload is not awaiting confirmation")
        url = f"{self.uploader.config.url}/c/{plan.project}/+/{plan.change_number}"
        try:
            status = self.status_fetcher(url)
        except Exception as exc:
            raise GerritUploadError(
                "could not recheck Gerrit immediately before upload"
            ) from exc
        if not self._matches(plan, status):
            result = self.store.transition(
                upload_id, expected={"commit_ready"}, state="stale",
                completed_at=datetime.now(timezone.utc),
                summary="Gerrit changed before upload confirmation",
            )
            try:
                self.uploader.cleanup(self.uploader.prepared_commit(plan))
            except Exception:
                pass
            return result
        try:
            prepared = self.uploader.prepared_commit(plan)
        except GerritUploadConflict:
            return self.store.transition(
                upload_id, expected={"commit_ready"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                summary="Prepared upload workspace was lost before any Gerrit write",
            )
        try:
            plan = self.store.transition(
                upload_id, expected={"commit_ready"}, state="push_claimed",
                claimed_at=datetime.now(timezone.utc),
            )
            self.uploader.push(plan, prepared)
        except GerritUploadConflict:
            raise
        except Exception:
            if prepared is None:
                raise GerritUploadConflict("prepared upload workspace is unavailable")
            result = self.reconcile(upload_id)
        else:
            result = self.reconcile(upload_id)
        if result.state != "ambiguous":
            self.uploader.cleanup(prepared)
        return result

    def reconcile(self, upload_id: str) -> UploadPlan:
        plan = self.store.get(upload_id)
        if plan.state == "succeeded":
            return plan
        if plan.state not in {"push_claimed", "ambiguous"}:
            raise GerritUploadConflict("only a dispatched upload can be reconciled")
        url = f"{self.uploader.config.url}/c/{plan.project}/+/{plan.change_number}"
        try:
            status = self.status_fetcher(url)
        except Exception:
            if plan.state == "ambiguous":
                return plan
            return self.store.transition(
                upload_id, expected={"push_claimed"}, state="ambiguous",
                completed_at=datetime.now(timezone.utc),
                summary="Push outcome is ambiguous; Gerrit reconciliation failed",
            )
        current_patchset = int(status.get("patchset") or 0)
        current_revision = str(status.get("revision_sha") or "").lower()
        revisions = {
            str(item).lower() for item in (status.get("revision_shas") or ())
        }
        revision_numbers = status.get("revision_numbers") or {}
        if plan.local_commit_sha and plan.local_commit_sha.lower() in revisions:
            observed_patchset = int(
                revision_numbers.get(plan.local_commit_sha.lower()) or current_patchset
            )
            return self.store.transition(
                upload_id, expected={"push_claimed", "ambiguous"}, state="succeeded",
                completed_at=datetime.now(timezone.utc), new_patchset=observed_patchset,
                new_revision_sha=plan.local_commit_sha.lower(),
                summary="Gerrit accepted a new patchset",
            )
        if (
            current_patchset > plan.patchset
            and plan.local_commit_sha
            and current_revision == plan.local_commit_sha.lower()
        ):
            return self.store.transition(
                upload_id, expected={"push_claimed", "ambiguous"}, state="succeeded",
                completed_at=datetime.now(timezone.utc), new_patchset=current_patchset,
                new_revision_sha=current_revision,
                summary="Gerrit accepted a new patchset",
            )
        if current_patchset == plan.patchset and current_revision == plan.revision_sha.lower():
            if plan.state == "ambiguous":
                return plan
            return self.store.transition(
                upload_id, expected={"push_claimed"}, state="ambiguous",
                completed_at=datetime.now(timezone.utc),
                summary="Push was dispatched but Gerrit does not yet show the commit",
            )
        return self.store.transition(
            upload_id, expected={"push_claimed", "ambiguous"}, state="stale",
            completed_at=datetime.now(timezone.utc),
            summary="Gerrit advanced to a different patchset; no retry was attempted",
        )


def configured_upload_controller(
    state_path: str | Path, workspace_root: str | Path,
) -> GerritUploadController:
    config = GerritConfig.load()
    return GerritUploadController(
        UploadStateStore(state_path), GitGerritUploader(config, workspace_root),
        status_fetcher=GerritStatusClient(config).fetch_identity,
        enabled=config.upload_enabled,
    )
