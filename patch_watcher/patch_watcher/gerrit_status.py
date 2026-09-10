"""Read-only Gerrit status support for Patch Watcher.

The status model mirrors the criteria used by Marc Vef's Gerrit graph:
Gerrit lifecycle/current patchset/WIP, Code-Review and Verified votes,
unresolved comments, and current-patchset Jenkins/Maloo signals.

This module deliberately uses only the Python standard library so the small
web application does not inherit gerrit-cli's runtime dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "patch-watcher" / "config"

_LUSTRE_CHANGE_RE = re.compile(r"^Lustre-change:\s*\S+\s*$", re.MULTILINE)
_JENKINS_URL_RE = re.compile(
    r"https?://build\.whamcloud\.com/job/([^/\s]+)/([0-9]+)/?"
)
_MALOO_DIRECT_RE = re.compile(
    r"https?://testing\.whamcloud\.com/test_sessions/related"
    r"\?jobs=[^&\s]+&builds=[0-9]+#redirect"
)
_MALOO_BUILD_RE = re.compile(r"sessions will be run for Build ([0-9]+)")
# Gerrit change detail is small; a response larger than this is a fault, not
# data.  The transport reads one byte past the bound so an oversized body is
# detected without materializing it.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# A change message longer than this is not a request for help.
MAX_MESSAGE_BYTES = 4000
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GerritConfigError(RuntimeError):
    """The private Patch Watcher Gerrit configuration is unavailable."""


class GerritRequestError(RuntimeError):
    """A read-only Gerrit request failed."""


@dataclass(frozen=True, repr=False)
class GerritConfig:
    """Private Gerrit connection settings.

    ``repr=False`` is intentional: accidental logging must not reveal the
    Gerrit HTTP password.
    """

    url: str
    username: str
    password: str
    refresh_interval: int = 300
    email_enabled: bool = False
    email_to: str = "paf@mulberrytree.us"
    sendmail_path: str = "/usr/sbin/sendmail"

    @classmethod
    def load(cls, path: Path | None = None) -> GerritConfig:
        config_path = path or DEFAULT_CONFIG_PATH
        try:
            mode = config_path.stat().st_mode
        except FileNotFoundError as exc:
            raise GerritConfigError(
                f"Gerrit is not configured. Create {config_path} with mode 0600."
            ) from exc
        except OSError as exc:
            raise GerritConfigError(f"Cannot inspect Gerrit config {config_path}.") from exc

        if not stat.S_ISREG(mode) or config_path.is_symlink():
            raise GerritConfigError(f"Gerrit config {config_path} must be a regular file.")
        if stat.S_IMODE(mode) & 0o077:
            raise GerritConfigError(
                f"Gerrit config {config_path} has unsafe permissions; run chmod 600."
            )

        values: dict[str, str] = {}
        try:
            lines = config_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise GerritConfigError(f"Cannot read Gerrit config {config_path}.") from exc

        for line_number, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise GerritConfigError(
                    f"Invalid Gerrit config line {line_number}: expected KEY=VALUE."
                )
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key in {
                "GERRIT_URL", "GERRIT_USER", "GERRIT_PASS",
                "REFRESH_INTERVAL_SECONDS", "EMAIL_ENABLED", "EMAIL_TO",
                "SENDMAIL_PATH",
            }:
                values[key] = value

        missing = [
            key for key in ("GERRIT_URL", "GERRIT_USER", "GERRIT_PASS")
            if not values.get(key)
        ]
        if missing:
            raise GerritConfigError(
                f"Gerrit config {config_path} is missing: {', '.join(missing)}."
            )

        parsed = urlparse(values["GERRIT_URL"])
        if parsed.scheme != "https" or parsed.hostname != "review.whamcloud.com":
            raise GerritConfigError(
                "GERRIT_URL must be https://review.whamcloud.com."
            )
        interval_text = values.get("REFRESH_INTERVAL_SECONDS", "300")
        try:
            refresh_interval = int(interval_text)
        except ValueError as exc:
            raise GerritConfigError(
                "REFRESH_INTERVAL_SECONDS must be an integer."
            ) from exc
        if not 15 <= refresh_interval <= 86400:
            raise GerritConfigError(
                "REFRESH_INTERVAL_SECONDS must be between 15 and 86400."
            )

        email_enabled = values.get("EMAIL_ENABLED", "false").casefold()
        if email_enabled not in {"true", "false", "yes", "no", "1", "0"}:
            raise GerritConfigError(
                "EMAIL_ENABLED must be true/false, yes/no, or 1/0."
            )
        email_to = values.get("EMAIL_TO", "paf@mulberrytree.us")
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", email_to):
            raise GerritConfigError("EMAIL_TO must be a valid email address.")
        sendmail_path = values.get("SENDMAIL_PATH", "/usr/sbin/sendmail")
        if not Path(sendmail_path).is_absolute():
            raise GerritConfigError("SENDMAIL_PATH must be an absolute path.")

        return cls(
            values["GERRIT_URL"].rstrip("/"),
            values["GERRIT_USER"],
            values["GERRIT_PASS"],
            refresh_interval,
            email_enabled in {"true", "yes", "1"},
            email_to,
            sendmail_path,
        )


def _shape_error(detail: str) -> GerritRequestError:
    return GerritRequestError(f"Gerrit returned an unexpected {detail}.")


def _as_mapping(value: Any, detail: str) -> dict[str, Any]:
    """Accept an absent field, reject a wrong-typed one.

    Gerrit's own contract makes these objects.  Anything else is a broken
    response, and treating it as one keeps the failure inside the caller's
    error bookkeeping instead of letting an AttributeError past it.
    """

    if value is None or value == {}:
        return {}
    if not isinstance(value, Mapping):
        raise _shape_error(detail)
    return dict(value)


def _as_list(value: Any, detail: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _shape_error(detail)
    return value


def _require_str(value: Any, detail: str) -> str:
    """Require a field Gerrit's own contract defines as a string.

    Coercing these would fabricate an answer -- a non-string ``status`` would
    become a lifecycle nobody can act on, and a non-integer unresolved count
    would silently read as zero -- so a broken response is reported as one.
    """

    if value is None:
        return ""
    if not isinstance(value, str):
        raise _shape_error(detail)
    return value


def _require_count(value: Any, detail: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _shape_error(detail)
    return value


def _as_int(value: Any, default: int | None = 0) -> int | None:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*-?[0-9]+\s*", value):
        return int(value)
    return default


def _safe_text(value: Any, *, limit: int = 1000) -> str:
    """Return bounded text that always survives ``str.encode()``.

    Gerrit JSON may legally contain a lone surrogate such as ``"\\ud800"``.
    Python decodes it happily and then refuses to encode it again, so every
    later consumer -- the HTML response, the daily email, any JSON dump --
    raises UnicodeEncodeError on text this module handed it.  Because the
    value is persisted in the watch list the failure repeats forever, so the
    sanitisation belongs here, at the boundary where the text enters.
    """

    result = _sanitize(value)
    if len(result) > limit:
        result = result[: limit - 1] + "\u2026"
    return result


def _sanitize(value: Any) -> str:
    """Return ``value`` as text with no lone surrogate and no control byte."""

    result = "" if value is None else str(value)
    result = result.encode("utf-8", "backslashreplace").decode("utf-8")
    return _CONTROL_RE.sub("\ufffd", result)


def _safe_line(value: Any, *, limit: int = 500) -> str:
    """Return ``_safe_text`` collapsed onto a single line."""

    return _safe_text(" ".join(_safe_text(value, limit=limit + 200).split()), limit=limit)


Transport = Callable[[Request, float], bytes]


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        # Bounded exactly like jenkins_adapter._default_transport: read one
        # byte past the limit so the caller can tell "at the limit" from
        # "over it" without ever holding an unbounded response.
        return response.read(MAX_RESPONSE_BYTES + 1)


class GerritStatusClient:
    """Minimal read-only Gerrit REST client with injectable I/O for tests."""

    def __init__(
        self,
        config: GerritConfig,
        *,
        transport: Transport | None = None,
        timeout: float = 20,
    ) -> None:
        self._config = config
        self._transport = transport or _default_transport
        self._timeout = timeout

    @classmethod
    def configured(cls) -> GerritStatusClient:
        return cls(GerritConfig.load())

    def fetch(self, change_url: str) -> dict[str, Any]:
        change_number = parse_change_number(change_url)
        options = (
            "o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_LABELS"
            "&o=DETAILED_ACCOUNTS&o=MESSAGES"
        )
        change = self._fetch_detail(change_number, options)
        return summarize_change(change, expected_change_number=change_number)

    def fetch_identity(self, change_url: str) -> dict[str, Any]:
        """Return exact write-precondition identity including every revision SHA."""
        change_number = parse_change_number(change_url)
        change = self._fetch_detail(
            change_number,
            "o=ALL_REVISIONS&o=CURRENT_REVISION&o=CURRENT_COMMIT",
        )
        try:
            current = _safe_line(
                _require_str(change.get("current_revision"), "current revision"),
                limit=64,
            ).lower()
            revisions = _as_mapping(change.get("revisions"), "revision map")
            current_record = _as_mapping(revisions.get(current), "revision record")
            return {
                # ``_fetch_detail`` already proved the body is about
                # ``change_number``; the identity is pinned to the requested
                # value so a later comparison cannot be satisfied by the
                # response describing itself.
                "change_number": change_number,
                "project": _safe_line(change.get("project") or "", limit=200),
                "branch": _safe_line(change.get("branch") or "", limit=200),
                "change_id": _safe_line(change.get("change_id") or "", limit=200),
                "status": _safe_line(
                    _require_str(change.get("status"), "change status"), limit=40
                ).upper(),
                "revision_sha": current,
                "patchset": _as_int(current_record.get("_number")) or 0,
                "revision_shas": tuple(sorted(
                    _safe_line(item, limit=64).lower() for item in revisions
                )),
                "revision_numbers": {
                    _safe_line(item, limit=64).lower():
                        _as_int(_as_mapping(record, "revision record").get("_number")) or 0
                    for item, record in revisions.items()
                },
                "updated": _safe_line(change.get("updated") or "", limit=64),
                "unresolved_comment_count": _require_count(
                    change.get("unresolved_comment_count"), "unresolved comment count"
                ),
            }
        except GerritRequestError:
            raise
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise _shape_error("change identity") from exc

    def fetch_review_snapshot(
        self, change_url: str, *, expected_revision: str | None = None,
        require_current: bool = True,
    ) -> dict[str, Any]:
        """Capture one fail-closed unresolved-comment snapshot.

        Both comment calls name the immutable revision SHA rather than the
        moving ``current`` alias.  Identity calls bracket the capture so a
        patchset advance cannot silently mix two Gerrit states.
        """

        change_number = parse_change_number(change_url)
        before = self.fetch_identity(change_url)
        current_revision = str(before.get("revision_sha") or "").lower()
        revision = str(expected_revision or current_revision).lower()
        if expected_revision and current_revision != revision and require_current:
            raise GerritRequestError("Gerrit revision changed before comment capture.")
        revision_numbers = before.get("revision_numbers") or {}
        if revision != current_revision and revision not in revision_numbers:
            raise GerritRequestError("The requested Gerrit revision is no longer in the change.")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise GerritRequestError("Gerrit returned an invalid current revision.")
        encoded_change = quote(str(change_number), safe="")
        encoded_revision = quote(revision, safe="")
        direct = self._fetch_json(
            f"/a/changes/{encoded_change}/revisions/{encoded_revision}/comments"
        )
        ported = self._fetch_json(
            f"/a/changes/{encoded_change}/revisions/{encoded_revision}/ported_comments"
        )
        after = self.fetch_identity(change_url)
        identity_keys = (
            "change_number", "project", "branch", "change_id", "status",
            "revision_sha", "patchset", "revision_shas", "revision_numbers",
            "updated", "unresolved_comment_count",
        )
        if any(before.get(key) != after.get(key) for key in identity_keys):
            raise GerritRequestError("Gerrit changed during comment capture.")
        target_identity = dict(before)
        target_identity["revision_sha"] = revision
        target_identity["patchset"] = int(
            revision_numbers.get(revision) or before.get("patchset") or 0
        )
        return normalize_review_snapshot(target_identity, direct, ported)

    def _fetch_detail(self, change_number: int, options: str) -> dict[str, Any]:
        endpoint = f"/a/changes/{quote(str(change_number), safe='')}/detail?{options}"
        change = self._fetch_json(endpoint)
        if not isinstance(change, dict):
            raise GerritRequestError("Gerrit returned an unexpected response.")
        # Every field below is taken from the body, so the body must first be
        # proven to describe the change that was asked for.  A misrouted or
        # substituted response is internally self-consistent, so nothing later
        # can notice it: the watch row, the revision SHA a write is bound to,
        # and the ref a worker would fetch would all come from another change.
        # The Maloo adapter checks the same way (get_bug_links, normalize_queue,
        # request_retest, link_bug).
        if _as_int(change.get("_number"), None) != change_number:
            raise GerritRequestError(
                "Gerrit answered about a different change: asked "
                f"{change_number}, got {_safe_line(change.get('_number'), limit=40)!r}."
            )
        return change

    def post_message(self, change_number: int, message: str) -> None:
        """Post one change message (a plain review comment) to the current revision.

        The only write this otherwise read-only client makes.  It exists so a
        run that bails to a human can say so on the review itself, where the
        patch owner will see it even if they never open the console.
        """
        text = _safe_text(message, limit=MAX_MESSAGE_BYTES)
        if not text.strip():
            raise GerritRequestError("Refusing to post an empty change message.")
        endpoint = (
            f"/a/changes/{quote(str(int(change_number)), safe='')}"
            "/revisions/current/review"
        )
        body = json.dumps({"message": text}).encode("utf-8")
        self._request_json(endpoint, method="POST", data=body)

    def _fetch_json(self, endpoint: str) -> Any:
        return self._request_json(endpoint, method="GET")

    def _request_json(self, endpoint: str, *, method: str, data: bytes | None = None) -> Any:
        token = base64.b64encode(
            f"{self._config.username}:{self._config.password}".encode()
        ).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "patch-watcher/0.1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            self._config.url + endpoint, headers=headers, method=method, data=data,
        )
        try:
            payload = self._transport(request, self._timeout)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise GerritRequestError("Gerrit rejected the configured credentials.") from exc
            if exc.code == 404:
                raise GerritRequestError(f"Gerrit returned 404 for {endpoint}.") from exc
            raise GerritRequestError(f"Gerrit returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GerritRequestError("Could not reach Gerrit.") from exc

        if not isinstance(payload, (bytes, bytearray)):
            raise GerritRequestError("Gerrit returned an invalid response.")
        if len(payload) > MAX_RESPONSE_BYTES:
            raise GerritRequestError(
                f"Gerrit returned more than {MAX_RESPONSE_BYTES} bytes for {endpoint}."
            )
        try:
            text = payload.decode("utf-8")
            if text.startswith(")]}'"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            if method != "GET" and not text.strip():
                return None
            value = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GerritRequestError("Gerrit returned an invalid JSON response.") from exc
        return value


def _bounded_comment_text(value: Any, *, limit: int) -> str:
    # ``_safe_text`` first: a lone surrogate in a review comment made the
    # ``encode`` below -- the length measurement itself -- raise
    # UnicodeEncodeError out of the snapshot capture.
    text = _sanitize(value).replace("\x00", "\ufffd")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "\u2026"


def _flatten_comments(value: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if not isinstance(value, Mapping):
        raise GerritRequestError("Gerrit returned invalid review comments.")
    result: list[tuple[str, Mapping[str, Any]]] = []
    for path, comments in value.items():
        if not isinstance(path, str) or not isinstance(comments, list):
            raise GerritRequestError("Gerrit returned invalid review comments.")
        for comment in comments:
            if not isinstance(comment, Mapping):
                raise GerritRequestError("Gerrit returned invalid review comments.")
            result.append((path, comment))
    return result


def normalize_review_snapshot(
    identity: Mapping[str, Any], direct: Any, ported: Any,
) -> dict[str, Any]:
    """Normalize exact-revision Gerrit comments into an immutable contract."""

    revision = str(identity.get("revision_sha") or "").lower()
    patchset = int(identity.get("patchset") or 0)
    revision_numbers = {
        str(key).lower(): int(value)
        for key, value in (identity.get("revision_numbers") or {}).items()
    }
    by_patchset = {number: sha for sha, number in revision_numbers.items()}
    reasons: list[str] = []
    records: dict[str, dict[str, Any]] = {}

    def add(path: str, raw: Mapping[str, Any], binding: str) -> None:
        comment_id = str(raw.get("id") or "").strip()
        if not comment_id or len(comment_id) > 256:
            reasons.append("comment missing stable id")
            return
        if comment_id in records and binding == "ported":
            return
        origin_patchset = int(raw.get("patch_set") or patchset)
        origin_revision = str(raw.get("commit_id") or by_patchset.get(origin_patchset) or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", origin_revision):
            reasons.append(f"comment {comment_id} has unknown origin revision")
        author = raw.get("author") or {}
        if not isinstance(author, Mapping):
            author = {}
        account = author.get("_account_id")
        author_key = (
            f"account:{int(account)}" if isinstance(account, int)
            else "username:" + _bounded_comment_text(author.get("username"), limit=256)
        )
        if author_key == "username:":
            author_key = "unknown"
        range_value = raw.get("range")
        normalized_range = None
        if isinstance(range_value, Mapping):
            keys = ("start_line", "start_character", "end_line", "end_character")
            if all(isinstance(range_value.get(key), int) for key in keys):
                normalized_range = {key: int(range_value[key]) for key in keys}
            else:
                reasons.append(f"comment {comment_id} has invalid range")
        location = {
            "path": _bounded_comment_text(path, limit=1000),
            "side": str(raw.get("side") or "REVISION"),
            "line": int(raw["line"]) if isinstance(raw.get("line"), int) else None,
            "range": normalized_range,
        }
        record = {
            "comment_id": comment_id,
            "thread_id": comment_id,
            "in_reply_to": str(raw.get("in_reply_to") or ""),
            "binding": binding,
            "origin_patchset": origin_patchset,
            "origin_revision_sha": origin_revision,
            "location": location,
            "current_location": location if binding == "ported" else None,
            "author_key": author_key,
            "author_name": _bounded_comment_text(author.get("name"), limit=500),
            "message": _bounded_comment_text(raw.get("message"), limit=16 * 1024),
            "updated": str(raw.get("updated") or ""),
            "unresolved": bool(raw.get("unresolved", False)),
            "tag": _bounded_comment_text(raw.get("tag"), limit=500),
            "change_message_id": str(raw.get("change_message_id") or ""),
        }
        records[comment_id] = record

    for path, raw in _flatten_comments(ported):
        add(path, raw, "ported")
    for path, raw in _flatten_comments(direct):
        add(path, raw, "direct")

    for record in records.values():
        parent = record["in_reply_to"]
        seen = {record["comment_id"]}
        root = record["comment_id"]
        while parent:
            if parent in seen:
                reasons.append(f"comment {record['comment_id']} has a reply cycle")
                break
            seen.add(parent)
            parent_record = records.get(parent)
            if parent_record is None:
                reasons.append(f"comment {record['comment_id']} has a missing parent")
                break
            root = parent
            parent = parent_record["in_reply_to"]
        record["thread_id"] = root

    threads: dict[str, list[dict[str, Any]]] = {}
    for record in records.values():
        threads.setdefault(record["thread_id"], []).append(record)
    unresolved_threads = []
    for thread_id, comments in threads.items():
        ordered = sorted(comments, key=lambda item: (item["updated"], item["comment_id"]))
        latest = ordered[-1]
        if latest["unresolved"]:
            unresolved_threads.append({"thread_id": thread_id, "comments": ordered})
    unresolved_threads.sort(key=lambda item: item["thread_id"])
    reported = int(identity.get("unresolved_comment_count") or 0)
    if len(unresolved_threads) != reported:
        reasons.append(
            f"unresolved thread count {len(unresolved_threads)} does not match Gerrit {reported}"
        )
    base = {
        "schema": "patch-watcher-review-snapshot/v1",
        "change": {
            "server": "https://review.whamcloud.com",
            "change_number": int(identity.get("change_number") or 0),
            "project": str(identity.get("project") or ""),
            "branch": str(identity.get("branch") or ""),
            "change_id": str(identity.get("change_id") or ""),
            "status": str(identity.get("status") or ""),
            "patchset": patchset,
            "revision_sha": revision,
            "gerrit_updated_at": str(identity.get("updated") or ""),
        },
        "reported_unresolved_count": reported,
        "complete": not reasons,
        "incompleteness_reasons": sorted(set(reasons)),
        "threads": unresolved_threads,
    }
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    base["snapshot_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    base["captured_at"] = datetime.now(UTC).isoformat()
    return base


def parse_change_number(value: str) -> int:
    """Extract a change number from a supported Whamcloud Gerrit URL."""
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "review.whamcloud.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use an HTTPS Whamcloud Gerrit change URL.")

    path = parsed.path.rstrip("/")
    patterns = (
        r"^/c/(?:[^/]+/)+\+/([0-9]+)(?:/[0-9]+)?$",
        r"^/c/([0-9]+)(?:/[0-9]+)?$",
        r"^/([0-9]+)(?:/[0-9]+)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, path)
        if match:
            return int(match.group(1))
    raise ValueError("Use a Whamcloud Gerrit change URL containing a change number.")


def _parse_labels(labels: Any) -> dict[str, Any]:
    """Compact DETAILED_LABELS using the same rules as gerrit-cli graph."""
    labels = _as_mapping(labels, "label map")
    code_review = _as_mapping(labels.get("Code-Review"), "label")
    verified_votes = _nonzero_votes(labels.get("Verified"))
    cr_votes = _nonzero_votes(code_review)
    verified_fail = any(vote["value"] < 0 for vote in verified_votes)
    review_blockers = [
        vote for vote in cr_votes
        if vote["value"] <= -1 and vote["name"].casefold() != "maloo"
    ]
    return {
        "verified_votes": verified_votes,
        "verified_pass": (
            any(vote["value"] > 0 for vote in verified_votes)
            and not verified_fail
        ),
        "verified_fail": verified_fail,
        "cr_votes": sorted(
            cr_votes,
            key=lambda vote: (vote["value"] > 0, abs(vote["value"])),
        ),
        "cr_approved": bool(code_review.get("approved")),
        "cr_rejected": bool(code_review.get("rejected")),
        "cr_veto": bool(review_blockers),
        "review_blockers": review_blockers,
    }


def _nonzero_votes(label: Any) -> list[dict[str, Any]]:
    votes = []
    for raw_voter in _as_list(_as_mapping(label, "label").get("all"), "label vote list"):
        voter = _as_mapping(raw_voter, "label vote")
        value = voter.get("value", 0)
        if not isinstance(value, int) or isinstance(value, bool) or value == 0:
            continue
        votes.append({
            "name": _safe_line(
                voter.get("name")
                or f"account:{_safe_line(voter.get('_account_id', '?'), limit=40)}",
                limit=200,
            ),
            "value": value,
        })
    return votes


def _extract_ci_links(messages: list[dict[str, Any]], patchset: int) -> dict[str, str]:
    """Select the newest current-patchset Jenkins and Maloo run links."""
    jenkins_url = ""
    jenkins_job = ""
    maloo_url = ""
    pending_maloo_build = ""
    for message in messages:
        if _as_int(message.get("_revision_number")) != patchset:
            continue
        text = _safe_text(message.get("message", ""), limit=100000)
        match = _JENKINS_URL_RE.search(text)
        if match:
            jenkins_url = match.group(0)
            jenkins_job = match.group(1)
        direct = _MALOO_DIRECT_RE.search(text)
        if direct:
            maloo_url = direct.group(0)
            pending_maloo_build = ""
        else:
            build = _MALOO_BUILD_RE.search(text)
            if build:
                pending_maloo_build = build.group(1)
                maloo_url = ""
    if not maloo_url and pending_maloo_build:
        job = jenkins_job or "lustre-reviews"
        maloo_url = (
            "https://testing.whamcloud.com/test_sessions/related"
            f"?jobs={job}&builds={pending_maloo_build}#redirect"
        )
    return {"jenkins_url": jenkins_url, "maloo_url": maloo_url}


def _voter_status(voter: str, votes: list[dict[str, Any]]) -> str:
    matching = [
        vote["value"] for vote in votes
        if str(vote["name"]).casefold() == voter.casefold()
    ]
    if any(value < 0 for value in matching):
        return "FAIL"
    if any(value > 0 for value in matching):
        return "PASS"
    return "—"


def _message_status(
    service: str, messages: list[dict[str, Any]], patchset: int
) -> str:
    """Fallback status when a CI service has not left a current label vote."""
    status = "—"
    for message in messages:
        if _as_int(message.get("_revision_number")) != patchset:
            continue
        author = _safe_line(
            _as_mapping(message.get("author"), "message author").get("name", ""),
            limit=200,
        ).casefold()
        if service.casefold() not in author:
            continue
        text = _safe_text(message.get("message", ""), limit=100000).casefold()
        if "build started" in text or "sessions will be run" in text:
            status = "RUNNING"
        if "build successful" in text or "verified+1" in text:
            status = "PASS"
        if "build failed" in text or "verified-1" in text:
            status = "FAIL"
        if service.casefold() == "maloo":
            if "failed enforced test" in text:
                status = "FAIL"
            elif "passed enforced test" in text and status != "FAIL":
                status = "PASS"
    return status


def _review_health(
    raw_status: str,
    owner: str,
    author: str,
    is_backport: bool,
    review: dict[str, Any],
) -> str:
    """Mirror the graph's Ready/Pending/CI-failure classification."""
    if raw_status != "NEW":
        return "—"
    if review["cr_veto"]:
        return "Veto"
    if review["verified_fail"]:
        failed = {
            str(vote["name"]).casefold()
            for vote in review["verified_votes"]
            if vote["value"] < 0
        }
        if "maloo" in failed:
            return "Maloo failed"
        if "jenkins" in failed:
            return "Jenkins failed"
        return "Verified failed"

    if review["verified_pass"]:
        passing = {
            str(vote["name"]).casefold()
            for vote in review["verified_votes"]
            if vote["value"] > 0
        }
        if {"jenkins", "maloo"}.issubset(passing):
            change_owner = owner or author
            non_owner_plus = sum(
                1 for vote in review["cr_votes"]
                if vote["value"] > 0 and vote["name"] != change_owner
            )
            if non_owner_plus >= (1 if is_backport else 2):
                return "Ready"
    return "Pending"


def _describe_latest_update(
    current: dict[str, Any],
    messages: list[dict[str, Any]],
    patchset: int,
    fallback_time: str,
) -> tuple[str, str]:
    """Return (event time, description) for the newest visible update."""
    uploader = _safe_line(
        _as_mapping(current.get("uploader"), "revision uploader").get("name")
        or "Unknown user",
        limit=200,
    )
    candidates = []
    # Every candidate key is normalized to text before it reaches ``max``:
    # Gerrit timestamps are strings, and one wrong-typed date used to make the
    # comparison itself raise TypeError.
    created = _safe_line(current.get("created", ""), limit=64)
    if created:
        candidates.append((
            created,
            f"{uploader} uploaded patchset {patchset}",
        ))
    for message in messages:
        date = _safe_line(message.get("date", ""), limit=64)
        if not date:
            continue
        author = _safe_line(
            _as_mapping(message.get("author"), "message author").get("name")
            or "Unknown user",
            limit=200,
        )
        message_patchset = _as_int(message.get("_revision_number")) or patchset
        lines = [
            line.strip()
            for line in _safe_text(message.get("message", ""), limit=100000).splitlines()
            if line.strip()
        ]
        first_line = lines[0] if lines else ""
        if re.fullmatch(r"Patch Set [0-9]+:", first_line) and len(lines) > 1:
            first_line = lines[1]
        if len(first_line) > 120:
            first_line = first_line[:117] + "..."
        description = f"{author} posted on patchset {message_patchset}"
        if first_line:
            description += f": {first_line}"
        candidates.append((date, description))
    if candidates:
        return max(candidates, key=lambda item: item[0])
    return fallback_time, "Gerrit change metadata updated"


def _review_blocker_details(
    blockers: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    patchset: int,
) -> list[dict[str, Any]]:
    """Attach the newest current-patchset message from each -1 reviewer."""
    details = []
    for blocker in blockers:
        name = blocker["name"]
        excerpt = "Code-Review -1"
        for message in reversed(messages):
            if _as_int(message.get("_revision_number")) != patchset:
                continue
            author = _safe_line(
                _as_mapping(message.get("author"), "message author").get("name", ""),
                limit=200,
            )
            if author != name:
                continue
            lines = [
                line.strip()
                for line in _safe_text(
                    message.get("message", ""), limit=100000
                ).splitlines()
                if line.strip()
            ]
            if lines:
                excerpt = lines[0]
                if re.fullmatch(r"Patch Set [0-9]+:", excerpt) and len(lines) > 1:
                    excerpt = lines[1]
            break
        details.append({
            "name": name,
            "value": blocker["value"],
            "patchset": patchset,
            "message": excerpt[:200],
        })
    return details


def _watch_classification(
    lifecycle: str,
    wip: bool,
    review_health: str,
    unresolved: int,
    jenkins: str,
    maloo: str,
) -> tuple[str, str]:
    """Classify read-only state and recommend a future human action.

    Patch Watcher never executes the recommendation.  Keeping this model
    separate makes a future guarded action workflow possible without mixing
    mutations into the polling layer.
    """
    if lifecycle == "Merged":
        return "merged", "Patch is merged; consider stopping the watch"
    if lifecycle == "Abandoned":
        return "abandoned", "Patch is abandoned; consider stopping the watch"
    if wip:
        return "work-in-progress", "Wait for the author to remove WIP"
    if review_health == "Veto":
        return "needs-attention", "Resolve outstanding review feedback"
    if "failed" in review_health.casefold() or "FAIL" in {jenkins, maloo}:
        return "ci-failed", "Inspect the CI failure; retest only after it is addressed"
    if unresolved:
        return "needs-attention", "Resolve outstanding review feedback"
    if review_health == "Ready":
        return "ready", "Ready for maintainer action"
    if "—" in {jenkins, maloo} or "RUNNING" in {jenkins, maloo}:
        return "awaiting-ci", "Wait for Jenkins and Maloo to finish"
    return "needs-review", "Request the missing Code-Review votes"


def summarize_change(
    change: dict[str, Any], *, expected_change_number: int | None = None
) -> dict[str, Any]:
    """Transform Gerrit ChangeInfo into Patch Watcher's flat status model.

    Every value below comes from the response body, so a caller that knows
    which change it asked for should pass ``expected_change_number``: a body
    describing a different change is internally self-consistent and nothing
    downstream can notice the substitution.
    """

    try:
        return _summarize_change(change, expected_change_number)
    except GerritRequestError:
        raise
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        # A wrong-typed Gerrit field used to escape as AttributeError or
        # TypeError, straight past refresh_patch's (GerritConfigError,
        # GerritRequestError, ValueError) handler: no status_error, no
        # last_checked stamp, a frozen row, and a health banner claiming the
        # refresh succeeded.  Every malformed shape now lands as the typed
        # error that handler already understands.
        raise _shape_error("response shape") from exc


def _summarize_change(
    change: Any, expected_change_number: int | None
) -> dict[str, Any]:
    change = _as_mapping(change, "change object")
    change_number = _as_int(change.get("_number"), None)
    if expected_change_number is not None and change_number != expected_change_number:
        raise GerritRequestError(
            "Gerrit answered about a different change: asked "
            f"{expected_change_number}, got {_safe_line(change.get('_number'), limit=40)!r}."
        )
    current_revision = _safe_line(
        _require_str(change.get("current_revision"), "current revision"), limit=64
    )
    revisions = _as_mapping(change.get("revisions"), "revision map")
    current = _as_mapping(revisions.get(current_revision), "revision record")
    patchset = (
        _as_int(current.get("_number"))
        or _as_int(change.get("_current_revision_number"))
        or 0
    )
    messages = [
        _as_mapping(message, "change message")
        for message in _as_list(change.get("messages"), "message list")
    ]
    labels = _parse_labels(change.get("labels"))
    links = _extract_ci_links(messages, patchset)

    commit = _as_mapping(current.get("commit"), "commit object")
    commit_message = _safe_text(
        _require_str(commit.get("message"), "commit message"), limit=100000
    )
    author = _safe_line(
        _as_mapping(commit.get("author"), "commit author").get("name", ""), limit=200
    )
    owner = _safe_line(
        _as_mapping(change.get("owner"), "change owner").get("name", ""), limit=200
    )
    is_backport = bool(_LUSTRE_CHANGE_RE.search(commit_message))
    raw_status = _safe_line(
        _require_str(change.get("status"), "change status") or "UNKNOWN", limit=40
    )
    lifecycle = {
        "NEW": "Open",
        "MERGED": "Merged",
        "ABANDONED": "Abandoned",
    }.get(raw_status, raw_status.title())

    jenkins = _voter_status("jenkins", labels["verified_votes"])
    if jenkins == "—":
        jenkins = _message_status("jenkins", messages, patchset)
    maloo = _voter_status("Maloo", labels["verified_votes"])
    if maloo == "—":
        maloo = _message_status("Maloo", messages, patchset)

    review_health = _review_health(raw_status, owner, author, is_backport, labels)
    unresolved = _require_count(
        change.get("unresolved_comment_count"), "unresolved comment count"
    )
    watch_state, recommendation = _watch_classification(
        lifecycle,
        bool(change.get("work_in_progress", False)),
        review_health,
        unresolved,
        jenkins,
        maloo,
    )
    change_time, change_summary = _describe_latest_update(
        current, messages, patchset, _safe_line(change.get("updated", ""), limit=64)
    )
    review_blockers = _review_blocker_details(
        labels["review_blockers"], messages, patchset
    )
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")

    return {
        "change_number": change_number or 0,
        "project": _safe_line(change.get("project", ""), limit=200),
        "branch": _safe_line(change.get("branch", ""), limit=200),
        "change_id": _safe_line(change.get("change_id", ""), limit=200),
        "revision_sha": current_revision,
        "revision_ref": _safe_line(current.get("ref", ""), limit=200),
        # Bounded and single-line: the subject is persisted in the watch list
        # and interpolated into HTML and the daily email.
        "title": _safe_line(change.get("subject", ""), limit=500),
        "status": raw_status,
        "lifecycle": lifecycle,
        "patchset": patchset,
        "wip": bool(change.get("work_in_progress", False)),
        "review": review_health,
        "review_votes": labels["cr_votes"],
        "review_blockers": review_blockers,
        "test_flow_blocked": bool(review_blockers),
        "verified_votes": labels["verified_votes"],
        "unresolved": unresolved,
        "jenkins": jenkins,
        "jenkins_url": links["jenkins_url"],
        "maloo": maloo,
        "maloo_url": links["maloo_url"],
        "is_backport": is_backport,
        "last_updated": _safe_line(change.get("updated", ""), limit=64),
        "last_changed": _safe_line(change.get("updated", ""), limit=64),
        "last_checked": checked_at,
        "change_event_at": change_time,
        "change_summary": change_summary,
        "watch_state": watch_state,
        "recommendation": recommendation,
        "refreshed_at": checked_at,
        "status_error": "",
    }


def refresh_patch(
    patch: dict[str, Any], client: GerritStatusClient | None = None
) -> str | None:
    """Refresh one mutable patch record, preserving it on failure.

    Returns an operator-safe error message instead of raising so a temporary
    Gerrit failure does not remove the last known status from the watch list.
    """
    previous_state = patch.get("watch_state", "")
    previous_blockers = patch.get("review_blockers") or []
    history = list(patch.get("history") or [])
    patch["check_count"] = int(patch.get("check_count", 0)) + 1
    try:
        status = (client or GerritStatusClient.configured()).fetch(patch["url"])
    except (GerritConfigError, GerritRequestError, ValueError) as exc:
        patch["status_error"] = str(exc)
        errors = list(patch.get("errors") or [])
        error_event = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "message": str(exc),
        }
        patch["last_checked"] = error_event["checked_at"]
        errors.append(error_event)
        patch["errors"] = errors[-50:]
        try:
            from patch_watcher.reporting import log_structured_error

            log_structured_error("gerrit_refresh", str(exc), patch.get("url", ""))
        except OSError:
            pass
        return str(exc)
    new_state = status.get("watch_state", "")
    patch.update(status)
    if previous_state and previous_state != new_state:
        patch["state_transition"] = f"{previous_state} → {new_state}"
        patch["state_changed_at"] = status["last_checked"]
    else:
        patch["state_transition"] = ""
        patch.setdefault("state_changed_at", status["last_checked"])
    if status.get("review_blockers") and status["review_blockers"] != previous_blockers:
        blocker_text = "; ".join(
            f"{item['name']} on patchset {item['patchset']}: {item['message']}"
            for item in status["review_blockers"]
        )
        try:
            from patch_watcher.reporting import log_structured_error

            log_structured_error("review_gate", blocker_text, patch.get("url", ""))
        except OSError:
            pass
    event = {
        "checked_at": status["last_checked"],
        "changed_at": status["last_changed"],
        "summary": status["change_summary"],
        "watch_state": new_state,
        "review": status["review"],
        "jenkins": status["jenkins"],
        "maloo": status["maloo"],
    }
    signature_keys = (
        "changed_at", "summary", "watch_state", "review", "jenkins", "maloo"
    )
    if not history or any(
        history[-1].get(key) != event[key] for key in signature_keys
    ):
        history.append(event)
    patch["history"] = history[-50:]
    return None
