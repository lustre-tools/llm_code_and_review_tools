"""Read-only Gerrit status support for Patch Watcher.

The status model mirrors the criteria used by Marc Vef's Gerrit graph:
Gerrit lifecycle/current patchset/WIP, Code-Review and Verified votes,
unresolved comments, and current-patchset Jenkins/Maloo signals.

This module deliberately uses only the Python standard library so the small
web application does not inherit gerrit-cli's runtime dependencies.
"""

from __future__ import annotations

import base64
import json
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
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
    def load(cls, path: Path | None = None) -> "GerritConfig":
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


Transport = Callable[[Request, float], bytes]


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read()


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
    def configured(cls) -> "GerritStatusClient":
        return cls(GerritConfig.load())

    def fetch(self, change_url: str) -> dict[str, Any]:
        change_number = parse_change_number(change_url)
        options = (
            "o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_LABELS"
            "&o=DETAILED_ACCOUNTS&o=MESSAGES"
        )
        endpoint = f"/a/changes/{quote(str(change_number), safe='')}/detail?{options}"
        token = base64.b64encode(
            f"{self._config.username}:{self._config.password}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            self._config.url + endpoint,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
                "User-Agent": "patch-watcher/0.1",
            },
            method="GET",
        )
        try:
            payload = self._transport(request, self._timeout)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise GerritRequestError("Gerrit rejected the configured credentials.") from exc
            if exc.code == 404:
                raise GerritRequestError(f"Gerrit change {change_number} was not found.") from exc
            raise GerritRequestError(f"Gerrit returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GerritRequestError("Could not reach Gerrit.") from exc

        try:
            text = payload.decode("utf-8")
            if text.startswith(")]}'"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            change = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GerritRequestError("Gerrit returned an invalid JSON response.") from exc
        if not isinstance(change, dict):
            raise GerritRequestError("Gerrit returned an unexpected response.")
        return summarize_change(change)


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


def _parse_labels(labels: dict[str, Any]) -> dict[str, Any]:
    """Compact DETAILED_LABELS using the same rules as gerrit-cli graph."""
    verified_votes = _nonzero_votes(labels.get("Verified", {}))
    cr_votes = _nonzero_votes(labels.get("Code-Review", {}))
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
        "cr_approved": bool(labels.get("Code-Review", {}).get("approved")),
        "cr_rejected": bool(labels.get("Code-Review", {}).get("rejected")),
        "cr_veto": bool(review_blockers),
        "review_blockers": review_blockers,
    }


def _nonzero_votes(label: dict[str, Any]) -> list[dict[str, Any]]:
    votes = []
    for voter in label.get("all", []):
        value = voter.get("value", 0)
        if not isinstance(value, int) or value == 0:
            continue
        votes.append({
            "name": voter.get("name", f"account:{voter.get('_account_id', '?')}"),
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
        if message.get("_revision_number", 0) != patchset:
            continue
        text = message.get("message", "")
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
        if vote["name"].casefold() == voter.casefold()
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
        if message.get("_revision_number", 0) != patchset:
            continue
        author = message.get("author", {}).get("name", "").casefold()
        if service.casefold() not in author:
            continue
        text = message.get("message", "").casefold()
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
            vote["name"].casefold()
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
            vote["name"].casefold()
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
    uploader = (current.get("uploader") or {}).get("name", "Unknown user")
    candidates = []
    if current.get("created"):
        candidates.append((
            current["created"],
            f"{uploader} uploaded patchset {patchset}",
        ))
    for message in messages:
        date = message.get("date", "")
        if not date:
            continue
        author = (message.get("author") or {}).get("name", "Unknown user")
        message_patchset = message.get("_revision_number") or patchset
        lines = [
            line.strip() for line in message.get("message", "").splitlines()
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
            if message.get("_revision_number", 0) != patchset:
                continue
            author = (message.get("author") or {}).get("name", "")
            if author != name:
                continue
            lines = [
                line.strip() for line in message.get("message", "").splitlines()
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


def summarize_change(change: dict[str, Any]) -> dict[str, Any]:
    """Transform Gerrit ChangeInfo into Patch Watcher's flat status model."""
    current_revision = change.get("current_revision", "")
    current = (change.get("revisions") or {}).get(current_revision, {})
    patchset = current.get("_number") or change.get("_current_revision_number") or 0
    messages = change.get("messages") or []
    labels = _parse_labels(change.get("labels") or {})
    links = _extract_ci_links(messages, patchset)

    commit = current.get("commit") or {}
    commit_message = commit.get("message", "")
    author = (commit.get("author") or {}).get("name", "")
    owner = (change.get("owner") or {}).get("name", "")
    is_backport = bool(_LUSTRE_CHANGE_RE.search(commit_message))
    raw_status = change.get("status", "UNKNOWN")
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
    unresolved = int(change.get("unresolved_comment_count", 0) or 0)
    watch_state, recommendation = _watch_classification(
        lifecycle,
        bool(change.get("work_in_progress", False)),
        review_health,
        unresolved,
        jenkins,
        maloo,
    )
    change_time, change_summary = _describe_latest_update(
        current, messages, patchset, change.get("updated", "")
    )
    review_blockers = _review_blocker_details(
        labels["review_blockers"], messages, patchset
    )
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return {
        "change_number": int(change.get("_number", 0) or 0),
        "project": str(change.get("project", "") or ""),
        "revision_sha": str(current_revision or ""),
        "revision_ref": str(current.get("ref", "") or ""),
        "title": change.get("subject", ""),
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
        "last_updated": change.get("updated", ""),
        "last_changed": change.get("updated", ""),
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
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message": str(exc),
        }
        patch["last_checked"] = error_event["checked_at"]
        errors.append(error_event)
        patch["errors"] = errors[-50:]
        try:
            from reporting import log_structured_error

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
            from reporting import log_structured_error

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
