"""Controller-owned, revision-pinned Gerrit review replies.

Workers can propose reply text in an immutable review-resolution artifact, but
they never receive Gerrit credentials and cannot dispatch the write.  This
module validates that artifact against the exact comment snapshot, records a
durable one-use claim, and reconciles an uncertain POST without blindly
retrying it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from gerrit_status import GerritConfig, GerritStatusClient


REPLY_STATES = frozenset({
    "prepared", "write_claimed", "succeeded", "stale", "ambiguous",
    "failed", "cancelled",
})
TERMINAL_REPLY_STATES = frozenset({
    "succeeded", "stale", "ambiguous", "failed", "cancelled",
})
MAX_RESOLUTION_BYTES = 128 * 1024
MAX_REPLIES = 64
MAX_MESSAGE_BYTES = 4_000
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_REVIEW_PATH_RE = re.compile(
    r"/a/changes/[1-9][0-9]*/revisions/[0-9a-f]{40}/review"
)
_COMMENTS_PATH_RE = re.compile(
    r"/a/changes/[1-9][0-9]*/revisions/[0-9a-f]{40}/comments"
)


class GerritReplyError(RuntimeError):
    """A review-reply operation could not safely proceed."""


class GerritReplyConflict(GerritReplyError):
    """An immutable binding, revision, or state is conflicting."""


class GerritReplyDefiniteFailure(GerritReplyError):
    """Gerrit definitively rejected a write."""


class GerritReplyAmbiguousError(GerritReplyError):
    """A write may have reached Gerrit and must only be reconciled."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def review_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Recompute the digest used by ``normalize_review_snapshot``."""

    base = {
        key: value for key, value in snapshot.items()
        if key not in {"snapshot_sha256", "captured_at"}
    }
    return hashlib.sha256(_canonical_json(base)).hexdigest()


def _safe_base_url(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or parsed.hostname != "review.whamcloud.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params or parsed.query or parsed.fragment
    ):
        raise GerritReplyConflict(
            "review writes are restricted to https://review.whamcloud.com"
        )
    return "https://review.whamcloud.com"


def _validate_review_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "review.whamcloud.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params or parsed.query or parsed.fragment
        or not _REVIEW_PATH_RE.fullmatch(parsed.path)
    ):
        raise GerritReplyConflict("Gerrit review API URL is not allowlisted")
    return value


def _validate_comments_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "review.whamcloud.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params or parsed.query or parsed.fragment
        or not _COMMENTS_PATH_RE.fullmatch(parsed.path)
    ):
        raise GerritReplyConflict("Gerrit comments API URL is not allowlisted")
    return value


def _snapshot_targets(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if (
        snapshot.get("schema") != "patch-watcher-review-snapshot/v1"
        or snapshot.get("complete") is not True
        or not isinstance(snapshot.get("change"), Mapping)
        or not isinstance(snapshot.get("threads"), list)
        or not snapshot.get("threads")
    ):
        raise GerritReplyConflict("a complete review-comment snapshot is required")
    digest = str(snapshot.get("snapshot_sha256") or "").lower()
    if not _SHA256_RE.fullmatch(digest) or digest != review_snapshot_digest(snapshot):
        raise GerritReplyConflict("review-comment snapshot digest does not match")
    targets: dict[str, Mapping[str, Any]] = {}
    for thread in snapshot["threads"]:
        comments = thread.get("comments") if isinstance(thread, Mapping) else None
        if not isinstance(comments, list) or not comments:
            raise GerritReplyConflict("review-comment snapshot contains an invalid thread")
        target = comments[-1]
        comment_id = str(target.get("comment_id") or "") if isinstance(target, Mapping) else ""
        if (
            not comment_id or len(comment_id.encode("utf-8")) > 256
            or comment_id in targets or target.get("unresolved") is not True
        ):
            raise GerritReplyConflict("review-comment snapshot target is invalid")
        targets[comment_id] = target
    return targets


def _load_resolution(
    path: str | Path, *, expected_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    resolution_path = Path(path)
    if not resolution_path.is_file() or resolution_path.is_symlink():
        raise GerritReplyConflict("immutable review resolution is unavailable")
    content = resolution_path.read_bytes()
    if not content or len(content) > MAX_RESOLUTION_BYTES:
        raise GerritReplyConflict("review resolution exceeds the controller bound")
    if (
        not _SHA256_RE.fullmatch(expected_sha256)
        or hashlib.sha256(content).hexdigest() != expected_sha256
    ):
        raise GerritReplyConflict("review resolution digest does not match")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GerritReplyConflict("review resolution is not valid JSON") from exc
    if not isinstance(value, dict):
        raise GerritReplyConflict("review resolution must be an object")
    return value, content


def _reply_payload(
    snapshot: Mapping[str, Any], resolution: Mapping[str, Any], *,
    run_id: str, resolution_sha256: str, tag: str,
) -> dict[str, Any]:
    targets = _snapshot_targets(snapshot)
    change = snapshot["change"]
    if (
        resolution.get("schema") != "patch-watcher-review-resolution/v1"
        or resolution.get("run_id") != run_id
        or str(resolution.get("revision_sha") or "").lower()
            != str(change.get("revision_sha") or "").lower()
        or resolution.get("review_snapshot_sha256") != snapshot.get("snapshot_sha256")
        or not isinstance(resolution.get("comment_results"), list)
    ):
        raise GerritReplyConflict("review resolution does not match its immutable run snapshot")

    results = resolution["comment_results"]
    if len(results) != len(targets):
        raise GerritReplyConflict("review resolution does not cover every snapshot target")
    seen: set[str] = set()
    comments: dict[str, list[dict[str, Any]]] = {}
    reply_count = 0
    for result in results:
        if not isinstance(result, Mapping):
            raise GerritReplyConflict("review resolution contains an invalid result")
        comment_id = str(result.get("comment_id") or "")
        if comment_id in seen or comment_id not in targets:
            raise GerritReplyConflict("review resolution names an unknown comment")
        seen.add(comment_id)
        disposition = result.get("disposition")
        if disposition in {"needs_human", "not_attempted"}:
            raise GerritReplyConflict("deferred review comments cannot be posted")
        if disposition not in {"addressed", "reply_draft"}:
            raise GerritReplyConflict("review resolution disposition is invalid")
        message = result.get("reply_draft")
        if message in {None, ""}:
            continue
        if (
            not isinstance(message, str) or "\x00" in message
            or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES
        ):
            raise GerritReplyConflict("review reply draft is invalid or too large")
        target = targets[comment_id]
        location = target.get("current_location") or target.get("location") or {}
        if not isinstance(location, Mapping):
            raise GerritReplyConflict("review reply target has no valid location")
        path = str(location.get("path") or "")
        if not path or len(path.encode("utf-8")) > 1000 or "\x00" in path:
            raise GerritReplyConflict("review reply target path is invalid")
        item: dict[str, Any] = {
            "in_reply_to": comment_id,
            "message": message,
            "unresolved": False,
        }
        if isinstance(location.get("line"), int):
            item["line"] = int(location["line"])
        if isinstance(location.get("range"), Mapping):
            item["range"] = dict(location["range"])
        if str(location.get("side") or "") in {"PARENT", "REVISION"}:
            item["side"] = str(location["side"])
        comments.setdefault(path, []).append(item)
        reply_count += 1
    if seen != set(targets):
        raise GerritReplyConflict("review resolution does not cover every snapshot target")
    if not reply_count or reply_count > MAX_REPLIES:
        raise GerritReplyConflict("review resolution has no bounded reply drafts to post")
    payload = {
        "tag": tag,
        "comments": comments,
    }
    if len(_canonical_json(payload)) > MAX_PAYLOAD_BYTES:
        raise GerritReplyConflict("Gerrit review payload exceeds the controller bound")
    # Keep the resolution digest in the derivation even though Gerrit must not
    # receive controller-internal artifact identifiers.
    if not _SHA256_RE.fullmatch(resolution_sha256):
        raise GerritReplyConflict("review resolution digest is invalid")
    return payload


@dataclass(frozen=True)
class ReviewReplyPlan:
    reply_id: str
    idempotency_key: str
    run_id: str
    session_id: str
    change_number: int
    project: str
    patchset: int
    revision_sha: str
    snapshot_sha256: str
    resolution_artifact_id: str
    resolution_sha256: str
    draft_sha256: str
    api_url: str
    tag: str
    payload_json: str
    state: str
    requested_by: str
    prepared_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    summary: str | None = None

    @property
    def binding_digest(self) -> str:
        values = [
            self.reply_id, self.idempotency_key, self.run_id, self.session_id,
            self.change_number, self.project, self.patchset, self.revision_sha,
            self.snapshot_sha256, self.resolution_artifact_id,
            self.resolution_sha256, self.draft_sha256, self.api_url, self.tag,
            self.payload_json, self.requested_by,
        ]
        return hashlib.sha256(_canonical_json(values)).hexdigest()

    @property
    def classification(self) -> str:
        return {
            "succeeded": "success", "stale": "stale",
            "ambiguous": "ambiguous", "failed": "failed",
            "cancelled": "failed",
        }.get(self.state, "pending")

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):  # Defensive check for a damaged ledger.
            raise GerritReplyConflict("stored review payload is invalid")
        return value


class ReviewReplyStateStore:
    """Private append-audited ledger for one-use Gerrit reply writes."""

    _BINDING_COLUMNS = (
        "idempotency_key", "run_id", "session_id", "change_number", "project",
        "patchset", "revision_sha", "snapshot_sha256", "resolution_artifact_id",
        "resolution_sha256", "draft_sha256", "api_url", "tag", "payload_json",
        "requested_by",
    )

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
                CREATE TABLE IF NOT EXISTS pw_gerrit_reply (
                    reply_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    change_number INTEGER NOT NULL CHECK(change_number > 0),
                    project TEXT NOT NULL,
                    patchset INTEGER NOT NULL CHECK(patchset > 0),
                    revision_sha TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    resolution_artifact_id TEXT NOT NULL,
                    resolution_sha256 TEXT NOT NULL,
                    draft_sha256 TEXT NOT NULL,
                    api_url TEXT NOT NULL,
                    tag TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared','write_claimed','succeeded','stale',
                        'ambiguous','failed','cancelled'
                    )),
                    requested_by TEXT NOT NULL,
                    prepared_at REAL NOT NULL,
                    claimed_at REAL,
                    completed_at REAL,
                    summary TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_reply_binding_immutable
                BEFORE UPDATE OF idempotency_key,run_id,session_id,change_number,
                    project,patchset,revision_sha,snapshot_sha256,
                    resolution_artifact_id,resolution_sha256,draft_sha256,api_url,
                    tag,payload_json,requested_by,prepared_at
                ON pw_gerrit_reply
                BEGIN SELECT RAISE(ABORT, 'reply binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_reply_no_delete
                BEFORE DELETE ON pw_gerrit_reply
                BEGIN SELECT RAISE(ABORT, 'reply records are durable'); END;
                CREATE TABLE IF NOT EXISTS pw_gerrit_reply_event (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reply_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_reply_event_no_update
                BEFORE UPDATE ON pw_gerrit_reply_event
                BEGIN SELECT RAISE(ABORT, 'reply events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS pw_gerrit_reply_event_no_delete
                BEFORE DELETE ON pw_gerrit_reply_event
                BEGIN SELECT RAISE(ABORT, 'reply events are append-only'); END;
            """)
            connection.commit()

    @staticmethod
    def _plan(row: sqlite3.Row) -> ReviewReplyPlan:
        def stamp(value):
            return datetime.fromtimestamp(value, timezone.utc) if value is not None else None
        return ReviewReplyPlan(
            reply_id=row["reply_id"], idempotency_key=row["idempotency_key"],
            run_id=row["run_id"], session_id=row["session_id"],
            change_number=int(row["change_number"]), project=row["project"],
            patchset=int(row["patchset"]), revision_sha=row["revision_sha"],
            snapshot_sha256=row["snapshot_sha256"],
            resolution_artifact_id=row["resolution_artifact_id"],
            resolution_sha256=row["resolution_sha256"],
            draft_sha256=row["draft_sha256"], api_url=row["api_url"],
            tag=row["tag"], payload_json=row["payload_json"],
            state=row["state"], requested_by=row["requested_by"],
            prepared_at=stamp(row["prepared_at"]), claimed_at=stamp(row["claimed_at"]),
            completed_at=stamp(row["completed_at"]), summary=row["summary"],
        )

    def get(self, reply_id: str) -> ReviewReplyPlan:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_gerrit_reply WHERE reply_id = ?", (reply_id,),
            ).fetchone()
        if row is None:
            raise GerritReplyError("unknown Gerrit review reply")
        return self._plan(row)

    def get_by_run(self, run_id: str) -> ReviewReplyPlan | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pw_gerrit_reply WHERE run_id = ?", (run_id,),
            ).fetchone()
        return self._plan(row) if row is not None else None

    def prepare(self, *, now: datetime | None = None, **values: Any) -> ReviewReplyPlan:
        timestamp = now or datetime.now(timezone.utc)
        reply_id = values.pop("reply_id", "reply-" + uuid.uuid4().hex)
        bound = [values[name] for name in self._BINDING_COLUMNS]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pw_gerrit_reply WHERE run_id = ? OR idempotency_key = ?",
                (values["run_id"], values["idempotency_key"]),
            ).fetchone()
            if row is not None:
                plan = self._plan(row)
                if any(str(getattr(plan, name)) != str(values[name])
                       for name in self._BINDING_COLUMNS):
                    connection.rollback()
                    raise GerritReplyConflict("review reply request identity was reused")
                connection.rollback()
                return plan
            placeholders = ",".join("?" for _ in range(len(bound) + 4))
            connection.execute(
                "INSERT INTO pw_gerrit_reply(reply_id," +
                ",".join(self._BINDING_COLUMNS) +
                ",state,prepared_at,updated_at) VALUES (" + placeholders + ")",
                (reply_id, *bound, "prepared", timestamp.timestamp(), timestamp.timestamp()),
            )
            self._event(connection, reply_id, "prepared", {}, timestamp)
            connection.commit()
        return self.get(reply_id)

    @staticmethod
    def _event(connection, reply_id, event_type, detail, at):
        connection.execute(
            "INSERT INTO pw_gerrit_reply_event(reply_id,event_type,detail_json,created_at) "
            "VALUES (?,?,?,?)",
            (reply_id, event_type, json.dumps(detail, sort_keys=True), at.timestamp()),
        )

    def transition(
        self, reply_id: str, *, expected: set[str], state: str,
        at: datetime | None = None, **updates: Any,
    ) -> ReviewReplyPlan:
        if state not in REPLY_STATES:
            raise ValueError("invalid review reply state")
        if set(updates) - {"claimed_at", "completed_at", "summary"}:
            raise ValueError("invalid review reply update")
        timestamp = at or datetime.now(timezone.utc)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM pw_gerrit_reply WHERE reply_id = ?", (reply_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise GerritReplyError("unknown Gerrit review reply")
            if row["state"] not in expected:
                connection.rollback()
                raise GerritReplyConflict(
                    f"review reply is {row['state']}, expected {', '.join(sorted(expected))}"
                )
            assignments = ["state = ?", "updated_at = ?"]
            params: list[Any] = [state, timestamp.timestamp()]
            for key, value in updates.items():
                assignments.append(key + " = ?")
                params.append(value.timestamp() if isinstance(value, datetime) else value)
            params.append(reply_id)
            connection.execute(
                "UPDATE pw_gerrit_reply SET " + ",".join(assignments) +
                " WHERE reply_id = ?", params,
            )
            self._event(connection, reply_id, state, {
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in updates.items() if key != "summary"
            }, timestamp)
            connection.commit()
        return self.get(reply_id)


Transport = Callable[[Request, float], bytes]


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


class GerritReviewWriter:
    """Credential-owning HTTP transport restricted to one Gerrit API shape."""

    def __init__(
        self, config: GerritConfig, *, transport: Transport | None = None,
        timeout: float = 30,
    ) -> None:
        self._config = config
        self._base_url = _safe_base_url(config.url)
        self._transport = transport or _default_transport
        self._timeout = timeout

    def review_url(self, change_number: int, revision_sha: str) -> str:
        if change_number <= 0 or not _REVISION_RE.fullmatch(revision_sha):
            raise GerritReplyConflict("Gerrit review identity is invalid")
        return _validate_review_url(
            f"{self._base_url}/a/changes/{quote(str(change_number), safe='')}"
            f"/revisions/{quote(revision_sha, safe='')}/review"
        )

    def post_review(self, api_url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        _validate_review_url(api_url)
        body = _canonical_json(payload)
        if not body or len(body) > MAX_PAYLOAD_BYTES:
            raise GerritReplyConflict("Gerrit review payload exceeds the controller bound")
        token = base64.b64encode(
            f"{self._config.username}:{self._config.password}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            api_url, data=body, method="POST", headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json", "Content-Type": "application/json",
                "User-Agent": "patch-watcher/0.1",
            },
        )
        try:
            raw = self._transport(request, self._timeout)
        except HTTPError as exc:
            raise GerritReplyDefiniteFailure(
                f"Gerrit rejected the review write with HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GerritReplyAmbiguousError(
                "Gerrit review write outcome is uncertain"
            ) from exc
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise GerritReplyAmbiguousError("Gerrit review response exceeded the bound")
        try:
            text = raw.decode("utf-8")
            if text.startswith(")]}'"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            value = json.loads(text or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GerritReplyAmbiguousError("Gerrit returned an invalid review response") from exc
        if not isinstance(value, Mapping):
            raise GerritReplyAmbiguousError("Gerrit returned an unexpected review response")
        return value

    def fetch_comments(self, change_number: int, revision_sha: str) -> Mapping[str, Any]:
        """Read exact-revision comments for post-claim reconciliation."""

        review_url = self.review_url(change_number, revision_sha)
        comments_url = _validate_comments_url(review_url.removesuffix("/review") + "/comments")
        token = base64.b64encode(
            f"{self._config.username}:{self._config.password}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            comments_url, method="GET", headers={
                "Authorization": f"Basic {token}", "Accept": "application/json",
                "User-Agent": "patch-watcher/0.1",
            },
        )
        try:
            raw = self._transport(request, self._timeout)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise GerritReplyError("could not fetch exact Gerrit comments") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GerritReplyError("Gerrit comments response exceeded the bound")
        try:
            text = raw.decode("utf-8")
            if text.startswith(")]}'"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            value = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GerritReplyError("Gerrit returned invalid comments JSON") from exc
        if not isinstance(value, Mapping):
            raise GerritReplyError("Gerrit returned invalid comments")
        return value


IdentityFetcher = Callable[[str], Mapping[str, Any]]
CommentsFetcher = Callable[[int, str], Mapping[str, Any]]
SnapshotFetcher = Callable[[str, str], Mapping[str, Any]]


class GerritReplyController:
    """Validate, dispatch once, and reconcile exact immutable reply drafts."""

    def __init__(
        self, store: ReviewReplyStateStore, writer: GerritReviewWriter, *,
        identity_fetcher: IdentityFetcher, snapshot_fetcher: SnapshotFetcher,
        comments_fetcher: CommentsFetcher, enabled: bool = False,
    ) -> None:
        self.store = store
        self.writer = writer
        self.identity_fetcher = identity_fetcher
        self.snapshot_fetcher = snapshot_fetcher
        self.comments_fetcher = comments_fetcher
        self.enabled = bool(enabled)

    def prepare(
        self, *, run_id: str, session_id: str,
        review_snapshot: Mapping[str, Any], resolution_path: str | Path,
        resolution_artifact_id: str, resolution_sha256: str,
        requested_by: str, idempotency_key: str,
    ) -> ReviewReplyPlan:
        if not self.enabled:
            raise GerritReplyConflict("Gerrit review replies are disabled")
        for name, value, limit in (
            ("run_id", run_id, 256), ("session_id", session_id, 256),
            ("resolution_artifact_id", resolution_artifact_id, 512),
            ("requested_by", requested_by, 1000),
            ("idempotency_key", idempotency_key, 512),
        ):
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
                raise GerritReplyConflict(f"{name} is invalid")
        targets = _snapshot_targets(review_snapshot)
        del targets  # Validation and digest verification are the purpose here.
        change = review_snapshot["change"]
        try:
            change_number = int(change["change_number"])
            patchset = int(change["patchset"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GerritReplyConflict("review snapshot change identity is invalid") from exc
        project = str(change.get("project") or "")
        revision_sha = str(change.get("revision_sha") or "").lower()
        if (
            change_number <= 0 or patchset <= 0 or not project
            or len(project.encode("utf-8")) > 1000
            or not _REVISION_RE.fullmatch(revision_sha)
            or str(change.get("status") or "").upper() != "NEW"
            or change.get("server") != "https://review.whamcloud.com"
        ):
            raise GerritReplyConflict("review snapshot change identity is invalid")
        resolution, _content = _load_resolution(
            resolution_path, expected_sha256=resolution_sha256,
        )
        tag_seed = hashlib.sha256(_canonical_json([
            idempotency_key, run_id, revision_sha,
            review_snapshot["snapshot_sha256"], resolution_sha256,
        ])).hexdigest()
        tag = "autogenerated:patch-watcher:" + tag_seed[:32]
        payload = _reply_payload(
            review_snapshot, resolution, run_id=run_id,
            resolution_sha256=resolution_sha256, tag=tag,
        )
        payload_json = _canonical_json(payload).decode("utf-8")
        draft_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        api_url = self.writer.review_url(change_number, revision_sha)
        plan = self.store.prepare(
            idempotency_key=idempotency_key, run_id=run_id, session_id=session_id,
            change_number=change_number, project=project, patchset=patchset,
            revision_sha=revision_sha,
            snapshot_sha256=str(review_snapshot["snapshot_sha256"]),
            resolution_artifact_id=resolution_artifact_id,
            resolution_sha256=resolution_sha256, draft_sha256=draft_sha256,
            api_url=api_url, tag=tag, payload_json=payload_json,
            requested_by=requested_by,
        )
        if plan.state != "prepared":
            return plan
        try:
            identity = self.identity_fetcher(self._change_url(plan))
        except Exception:
            return self.store.transition(
                plan.reply_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                summary="Could not verify Gerrit before preparing the review reply",
            )
        if not self._matches(plan, identity):
            return self.store.transition(
                plan.reply_id, expected={"prepared"}, state="stale",
                completed_at=datetime.now(timezone.utc),
                summary="Gerrit changed before the review reply was prepared",
            )
        try:
            current_snapshot = self.snapshot_fetcher(
                self._change_url(plan), plan.revision_sha,
            )
        except Exception:
            return self.store.transition(
                plan.reply_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                summary="Could not recapture review comments while preparing the reply",
            )
        if not self._same_snapshot(plan, current_snapshot):
            return self.store.transition(
                plan.reply_id, expected={"prepared"}, state="stale",
                completed_at=datetime.now(timezone.utc),
                summary="Review comments changed before the reply was prepared",
            )
        return plan

    def execute(self, reply_id: str, *, expected_binding_digest: str) -> ReviewReplyPlan:
        plan = self.store.get(reply_id)
        if plan.binding_digest != expected_binding_digest:
            raise GerritReplyConflict("review reply confirmation does not match its plan")
        if plan.state == "succeeded":
            return plan
        if plan.state in {"write_claimed", "ambiguous"}:
            return self.reconcile(reply_id)
        if plan.state != "prepared":
            raise GerritReplyConflict("review reply is not awaiting dispatch")
        try:
            identity = self.identity_fetcher(self._change_url(plan))
        except Exception:
            return self.store.transition(
                reply_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                summary="Could not recheck Gerrit before the review write",
            )
        if not self._matches(plan, identity):
            return self.store.transition(
                reply_id, expected={"prepared"}, state="stale",
                completed_at=datetime.now(timezone.utc),
                summary="Gerrit changed before the review write",
            )
        try:
            current_snapshot = self.snapshot_fetcher(
                self._change_url(plan), plan.revision_sha,
            )
        except Exception:
            return self.store.transition(
                reply_id, expected={"prepared"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                summary="Could not recapture review comments before the write",
            )
        if not self._same_snapshot(plan, current_snapshot):
            return self.store.transition(
                reply_id, expected={"prepared"}, state="stale",
                completed_at=datetime.now(timezone.utc),
                summary="Review comments changed before the write",
            )
        plan = self.store.transition(
            reply_id, expected={"prepared"}, state="write_claimed",
            claimed_at=datetime.now(timezone.utc),
        )
        try:
            self.writer.post_review(plan.api_url, plan.payload)
        except GerritReplyDefiniteFailure:
            return self.store.transition(
                reply_id, expected={"write_claimed"}, state="failed",
                completed_at=datetime.now(timezone.utc),
                summary="Gerrit definitively rejected the review write",
            )
        except Exception:
            # The durable claim precedes transport.  Any post-claim exception is
            # uncertain unless the transport explicitly proved rejection above.
            return self.reconcile(reply_id)
        return self.store.transition(
            reply_id, expected={"write_claimed"}, state="succeeded",
            completed_at=datetime.now(timezone.utc),
            summary="Gerrit accepted the exact immutable review replies",
        )

    def reconcile(self, reply_id: str) -> ReviewReplyPlan:
        plan = self.store.get(reply_id)
        if plan.state == "succeeded":
            return plan
        if plan.state not in {"write_claimed", "ambiguous"}:
            raise GerritReplyConflict("only a dispatched review reply can be reconciled")
        try:
            comments = self.comments_fetcher(plan.change_number, plan.revision_sha)
            observed = self._matching_replies(plan, comments)
        except Exception:
            return self._ambiguous(plan, "Could not reconcile the Gerrit review write")
        expected = sum(len(items) for items in plan.payload["comments"].values())
        if observed == expected:
            return self.store.transition(
                reply_id, expected={"write_claimed", "ambiguous"}, state="succeeded",
                completed_at=datetime.now(timezone.utc),
                summary="Gerrit contains every exact tagged review reply",
            )
        if observed:
            return self._ambiguous(
                plan, "Gerrit contains only part of the claimed review write",
            )
        try:
            identity = self.identity_fetcher(self._change_url(plan))
        except Exception:
            return self._ambiguous(plan, "Gerrit review write outcome remains uncertain")
        if not self._matches(plan, identity):
            return self.store.transition(
                reply_id, expected={"write_claimed", "ambiguous"}, state="stale",
                completed_at=datetime.now(timezone.utc),
                summary="Gerrit advanced without the exact tagged review replies",
            )
        return self._ambiguous(
            plan, "The write was claimed but Gerrit does not show the tagged replies",
        )

    def _ambiguous(self, plan: ReviewReplyPlan, summary: str) -> ReviewReplyPlan:
        if plan.state == "ambiguous":
            return plan
        return self.store.transition(
            plan.reply_id, expected={"write_claimed"}, state="ambiguous",
            completed_at=datetime.now(timezone.utc), summary=summary,
        )

    @staticmethod
    def _change_url(plan: ReviewReplyPlan) -> str:
        return f"https://review.whamcloud.com/c/{plan.project}/+/{plan.change_number}"

    @staticmethod
    def _matches(plan: ReviewReplyPlan, identity: Mapping[str, Any]) -> bool:
        base = (
            int(identity.get("change_number") or 0) == plan.change_number
            and str(identity.get("project") or "") == plan.project
            and str(identity.get("status") or "").upper() == "NEW"
        )
        revisions = identity.get("revision_numbers")
        if isinstance(revisions, Mapping):
            return base and int(revisions.get(plan.revision_sha) or 0) == plan.patchset
        # Backward-compatible fail-closed behavior for injected/test fetchers
        # that only know how to report the current revision.
        return base and (
            int(identity.get("patchset") or 0) == plan.patchset
            and str(identity.get("revision_sha") or "").lower() == plan.revision_sha
        )

    @staticmethod
    def _same_snapshot(plan: ReviewReplyPlan, snapshot: Mapping[str, Any]) -> bool:
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("complete") is not True
            or str((snapshot.get("change") or {}).get("revision_sha") or "").lower()
                != plan.revision_sha
        ):
            return False
        try:
            targets = _snapshot_targets(snapshot)
        except GerritReplyError:
            return False
        if str(snapshot.get("snapshot_sha256") or "") != plan.snapshot_sha256:
            return False
        expected = {}
        for path, items in plan.payload["comments"].items():
            for item in items:
                expected[str(item["in_reply_to"])] = {
                    "path": path, "side": item.get("side"),
                    "line": item.get("line"), "range": item.get("range"),
                }
        if not set(expected).issubset(targets):
            return False
        for comment_id, expected_location in expected.items():
            target = targets[comment_id]
            location = target.get("current_location") or target.get("location") or {}
            observed = {
                "path": location.get("path"), "side": location.get("side"),
                "line": location.get("line"), "range": location.get("range"),
            }
            if observed != expected_location:
                return False
        return True

    @staticmethod
    def _matching_replies(plan: ReviewReplyPlan, comments: Mapping[str, Any]) -> int:
        if not isinstance(comments, Mapping):
            raise GerritReplyError("Gerrit returned invalid comments during reconciliation")
        expected: set[tuple[str, str, str]] = set()
        for path, items in plan.payload["comments"].items():
            for item in items:
                expected.add((path, item["in_reply_to"], item["message"]))
        observed: set[tuple[str, str, str]] = set()
        for path, items in comments.items():
            if not isinstance(path, str) or not isinstance(items, list):
                raise GerritReplyError("Gerrit returned invalid comments during reconciliation")
            for item in items:
                if not isinstance(item, Mapping) or item.get("tag") != plan.tag:
                    continue
                key = (path, str(item.get("in_reply_to") or ""), str(item.get("message") or ""))
                if key in expected:
                    observed.add(key)
        return len(observed)


__all__ = [
    "GerritReplyAmbiguousError", "GerritReplyConflict",
    "GerritReplyController", "GerritReplyDefiniteFailure", "GerritReplyError",
    "GerritReviewWriter", "ReviewReplyPlan", "ReviewReplyStateStore",
    "TERMINAL_REPLY_STATES", "configured_reply_controller",
    "review_snapshot_digest",
]


def configured_reply_controller(state_path: str | Path) -> GerritReplyController:
    """Create the production controller; credentials stay inside its transports."""

    config = GerritConfig.load()
    writer = GerritReviewWriter(config)
    status_client = GerritStatusClient(config)
    return GerritReplyController(
        ReviewReplyStateStore(state_path), writer,
        identity_fetcher=status_client.fetch_identity,
        snapshot_fetcher=lambda url, revision: status_client.fetch_review_snapshot(
            url, expected_revision=revision, require_current=False,
        ),
        comments_fetcher=writer.fetch_comments,
        enabled=config.reply_enabled,
    )
