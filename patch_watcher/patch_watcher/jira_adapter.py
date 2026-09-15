"""Read-only JIRA issue observation.

Patch Watcher watches an issue the way it watches a change: it reads, and it
notices when something moved.  It does not write.  The agent's own policy
already forbids `jira comment`, `jira create` and `jira link`, and a watcher
that could write would make that promise unenforceable from the one process
best placed to keep it.

What counts as "moved" is deliberately narrow.  An issue's own `updated`
stamp shifts for edits nobody needs to act on, so the fingerprint is built
from the fields a reader would actually respond to: status, resolution,
priority, assignee, and the comments.  A new comment is the signal that most
often means "someone is asking you something".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_JIRA_CONFIG = Path.home() / ".config" / "patch-watcher" / "jira.json"
TICKET_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,19}-[1-9][0-9]{0,9}")
MAX_COMMENTS = 100
MAX_TEXT = 4000

Transport = Callable[[Request, float], bytes]


class JiraConfigError(RuntimeError):
    """The private JIRA configuration is unusable."""


class JiraRequestError(RuntimeError):
    """A read-only JIRA request failed."""


@dataclass(frozen=True, repr=False)
class JiraConfig:
    """Connection settings for one JIRA instance.

    ``repr=False`` is intentional, exactly as for Gerrit: accidental logging
    must not reveal the bearer token.
    """

    server: str
    token: str

    @classmethod
    def load(cls, path: Path | None = None) -> JiraConfig:
        config_path = path or DEFAULT_JIRA_CONFIG
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise JiraConfigError(
                f"JIRA is not configured. Expected {config_path}."
            ) from exc
        except (OSError, ValueError) as exc:
            raise JiraConfigError(f"JIRA configuration is unreadable: {exc}") from exc
        instances = raw.get("instances") if isinstance(raw, Mapping) else None
        if not isinstance(instances, Mapping) or not instances:
            raise JiraConfigError("JIRA configuration declares no instances")
        name = str(raw.get("default") or next(iter(instances)))
        instance = instances.get(name)
        if not isinstance(instance, Mapping):
            raise JiraConfigError(f"JIRA instance {name!r} is not configured")
        server = str(instance.get("server") or "").rstrip("/")
        auth = instance.get("auth")
        token = str(auth.get("token") or "") if isinstance(auth, Mapping) else ""
        if not server or not token:
            raise JiraConfigError(f"JIRA instance {name!r} has no server or token")
        return cls(server=server, token=token)


@dataclass(frozen=True)
class JiraIssue:
    """One observation of an issue, and what would make a reader act."""

    key: str
    summary: str = ""
    status: str = ""
    resolution: str = ""
    priority: str = ""
    assignee: str = ""
    updated: str = ""
    comments: tuple[dict[str, str], ...] = field(default_factory=tuple)

    @property
    def latest_comment(self) -> dict[str, str] | None:
        return self.comments[-1] if self.comments else None

    def fingerprint(self) -> str:
        """A digest of what a reader would respond to, not of every edit.

        A summary reword moves the issue's own `updated` stamp and means
        nothing to a watcher; a new comment, a status change or a reassignment
        are the things worth waking for.  Keying on `updated` alone would fire
        on the first and, worse, would keep firing for as long as anyone kept
        touching the issue.
        """
        material = {
            "key": self.key,
            "status": self.status,
            "resolution": self.resolution,
            "priority": self.priority,
            "assignee": self.assignee,
            "comments": [
                {"id": item.get("id", ""), "updated": item.get("updated", "")}
                for item in self.comments
            ],
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "patch-watcher-jira-issue/v1",
            "key": self.key,
            "summary": self.summary,
            "status": self.status,
            "resolution": self.resolution,
            "priority": self.priority,
            "assignee": self.assignee,
            "updated": self.updated,
            "comments": [dict(item) for item in self.comments],
            "fingerprint": self.fingerprint(),
        }


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _text(value: Any, limit: int = 500) -> str:
    return str(value if value is not None else "")[:limit]


def _named(value: Any, *keys: str) -> str:
    """JIRA returns objects for status, priority and person fields."""
    if isinstance(value, Mapping):
        for key in keys:
            if value.get(key):
                return _text(value[key])
    return _text(value) if isinstance(value, str) else ""


class JiraClient:
    """Minimal read-only JIRA REST client with injectable I/O for tests."""

    def __init__(
        self,
        config: JiraConfig,
        *,
        transport: Transport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.config = config
        self.transport = transport or _default_transport
        self.timeout = float(timeout)

    @classmethod
    def configured(cls, path: Path | None = None) -> JiraClient:
        return cls(JiraConfig.load(path))

    def _fetch_json(self, endpoint: str) -> Any:
        request = Request(
            self.config.server + endpoint,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self.config.token,
                "User-Agent": "patch-watcher/1",
            },
        )
        try:
            body = self.transport(request, self.timeout)
        except HTTPError as exc:
            raise JiraRequestError(f"JIRA returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise JiraRequestError(f"JIRA request failed: {exc}") from exc
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise JiraRequestError("JIRA returned a body that is not JSON") from exc

    def assigned_issues(self, *, limit: int = 50) -> list[dict[str, str]]:
        """The issues assigned to whoever these credentials are.

        This is how a ticket reaches Patch Watcher without anyone declaring
        it: somebody assigns it to the bot.  The query is `currentUser()`
        rather than a hardcoded name, so it cannot drift from the account the
        token actually belongs to -- and it keeps the account name out of the
        configuration, where it would be a second thing to keep in step.

        Resolved issues are excluded.  An assignee is not a to-do list once
        the work is finished, and an agent handed a closed ticket would look
        for work that no longer exists.
        """
        jql = "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
        bounded = max(1, min(int(limit), 200))
        endpoint = (
            f"/rest/api/2/search?jql={quote(jql, safe='')}"
            f"&maxResults={bounded}&fields=summary,status,updated"
        )
        body = self._fetch_json(endpoint)
        if not isinstance(body, Mapping):
            raise JiraRequestError("JIRA returned an unexpected response")
        issues = []
        for item in body.get("issues") or ():
            if not isinstance(item, Mapping):
                continue
            key = _text(item.get("key"), 64).upper()
            if not TICKET_KEY_RE.fullmatch(key):
                continue
            values = item.get("fields")
            values = values if isinstance(values, Mapping) else {}
            issues.append({
                "key": key,
                "summary": _text(values.get("summary"), 1000),
                "status": _named(values.get("status"), "name"),
                "updated": _text(values.get("updated"), 64),
            })
        return issues

    def whoami(self) -> dict[str, str]:
        """The account these credentials belong to.

        Worth being able to state plainly: a run that posts as the wrong
        identity is the kind of thing nobody notices until a reviewer asks
        why the patch owner is arguing with themselves.
        """
        body = self._fetch_json("/rest/api/2/myself")
        if not isinstance(body, Mapping):
            raise JiraRequestError("JIRA returned an unexpected response")
        return {
            "name": _text(body.get("name"), 200),
            "display_name": _text(body.get("displayName"), 200),
            "email": _text(body.get("emailAddress"), 200),
        }

    def fetch_issue(self, key: str) -> JiraIssue:
        """Observe one issue, or raise.

        The response is proven to describe the issue that was asked for before
        any field is read from it.  A misrouted or substituted body is
        internally consistent, so nothing downstream could notice.
        """
        wanted = str(key or "").strip().upper()
        if not TICKET_KEY_RE.fullmatch(wanted):
            raise JiraRequestError(f"not a JIRA issue key: {key!r}")
        fields = "summary,status,resolution,priority,assignee,updated,comment"
        endpoint = f"/rest/api/2/issue/{quote(wanted, safe='')}?fields={fields}"
        body = self._fetch_json(endpoint)
        if not isinstance(body, Mapping):
            raise JiraRequestError("JIRA returned an unexpected response")
        returned = _text(body.get("key")).upper()
        if returned != wanted:
            raise JiraRequestError(
                f"JIRA returned issue {returned or '(none)'} when asked for {wanted}"
            )
        values = body.get("fields")
        values = values if isinstance(values, Mapping) else {}
        raw_comments = values.get("comment")
        entries = (
            raw_comments.get("comments") if isinstance(raw_comments, Mapping) else None
        )
        comments: list[dict[str, str]] = []
        for item in entries or ():
            if not isinstance(item, Mapping) or not item.get("id"):
                continue
            comments.append({
                "id": _text(item.get("id"), 64),
                "author": _named(item.get("author"), "displayName", "name"),
                "created": _text(item.get("created"), 64),
                "updated": _text(item.get("updated"), 64),
                "body": _text(item.get("body"), MAX_TEXT),
            })
        return JiraIssue(
            key=wanted,
            summary=_text(values.get("summary"), 1000),
            status=_named(values.get("status"), "name"),
            resolution=_named(values.get("resolution"), "name"),
            priority=_named(values.get("priority"), "name"),
            assignee=_named(values.get("assignee"), "displayName", "name"),
            updated=_text(values.get("updated"), 64),
            comments=tuple(comments[-MAX_COMMENTS:]),
        )
