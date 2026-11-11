"""Pick the open changes a target should be checked against."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from .graph.nodes import _LUSTRE_CHANGE_TRAILER_RE
from .graph.review import _is_bot_account, _parse_labels

_GENERIC_TAG_RE = re.compile(
    r"^(pw_.*|fortestonly|forbuildonly|.*_review_priority|.*_review_blocker)$"
)
_OPTIONS = ("LABELS", "DETAILED_LABELS", "CURRENT_REVISION",
            "CURRENT_COMMIT", "DETAILED_ACCOUNTS")
_PATHS_PER_QUERY = 20


def release_rank(hashtags: list[str]) -> int:
    if any(t.endswith("_review_blocker") for t in hashtags):
        return 2
    if any(t.endswith("_review_priority") for t in hashtags):
        return 1
    return 0


def shared_hashtags(a: list[str], b: list[str]) -> list[str]:
    return sorted(
        t for t in set(a) & set(b) if not _GENERIC_TAG_RE.match(t)
    )


def _is_owner(vote: dict[str, Any], owner: dict[str, Any]) -> bool:
    if vote.get("_account_id") is not None and \
            owner.get("_account_id") is not None:
        return vote["_account_id"] == owner["_account_id"]
    for key in ("name", "username"):
        if vote.get(key) and vote.get(key) == owner.get(key):
            return True
    return False


def _label_votes(change: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return [
        v for v in ((change.get("labels") or {}).get(label) or {})
        .get("all", []) if v.get("value", 0)
    ]


def _voter(vote: dict[str, Any]) -> str:
    return (vote.get("name") or vote.get("username") or "").lower()


def _blocking_reviewer(change: dict[str, Any]) -> tuple[str, int] | None:
    owner = change.get("owner") or {}
    for v in _label_votes(change, "Code-Review"):
        name = v.get("name") or v.get("username") or ""
        if v["value"] <= -1 and not _is_bot_account(name) \
                and not _is_owner(v, owner):
            return name, v["value"]
    return None


def _verified(change: dict[str, Any], who: str) -> int:
    for v in _label_votes(change, "Verified"):
        if who in (_voter(v), (v.get("username") or "").lower()):
            return v["value"]
    return 0


def is_backport(change: dict[str, Any]) -> bool:
    rev = (change.get("revisions") or {}).get(
        change.get("current_revision") or "") or {}
    message = (rev.get("commit") or {}).get("message") or ""
    return bool(_LUSTRE_CHANGE_TRAILER_RE.search(message))


def readiness(change: dict[str, Any]) -> str:
    if _blocking_reviewer(change):
        return "cr-1"
    jenkins = _verified(change, "jenkins")
    maloo = _verified(change, "maloo")
    if jenkins < 0:
        return "jenkins-1"
    if maloo < 0:
        return "maloo-1"
    owner = change.get("owner") or {}
    plus = sum(
        1 for v in _label_votes(change, "Code-Review")
        if v["value"] > 0 and not _is_owner(v, owner)
    )
    parsed = _parse_labels(change.get("labels") or {})
    need = 1 if is_backport(change) else 2
    if jenkins > 0 and maloo > 0 and plus >= need \
            and not parsed["verified_fail"]:
        return "ready"
    return "pending"


def _updated(change: dict[str, Any]) -> datetime | None:
    try:
        return datetime.strptime(
            (change.get("updated") or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def classify(
    change: dict[str, Any],
    target: dict[str, Any],
    queued_ids: set[str],
    max_age_days: int,
    now: datetime,
) -> tuple[str | None, str | None]:
    if change.get("change_id") in queued_ids:
        return "frozen", None
    blocker = _blocking_reviewer(change)
    if blocker:
        return None, f"Code-Review {blocker[1]} ({blocker[0]})"
    if _verified(change, "jenkins") < 0:
        return None, "Jenkins -1"
    if readiness(change) == "ready" \
            or release_rank(change.get("hashtags") or []) > 0:
        return "lands-first", None
    updated = _updated(change)
    if updated is not None and now - updated > timedelta(days=max_age_days):
        return None, f"stale (updated {updated:%Y-%m-%d})"
    same = shared_hashtags(change.get("hashtags") or [],
                           target.get("hashtags") or [])
    topic = change.get("topic")
    if same or (topic and topic == target.get("topic")):
        return "same-effort", None
    return "other", None


_FIX_SIDE = {
    "frozen": {"frozen": "coordinate", "lands-first": "candidate",
               "same-effort": "candidate", "other": "candidate"},
    "lands-first": {"frozen": "target", "lands-first": "coordinate",
                    "same-effort": "candidate", "other": "candidate"},
    "pending": {"frozen": "target", "lands-first": "target",
                "same-effort": "coordinate", "other": "candidate"},
}


def fix_side(target_tier: str, target_rank: int,
             cand_tier: str, cand_rank: int) -> str:
    row = _FIX_SIDE[target_tier if target_tier in ("frozen", "lands-first")
                    else "pending"]
    if target_tier == "lands-first" and cand_tier == "lands-first":
        if target_rank > cand_rank:
            return "candidate"
        if target_rank < cand_rank:
            return "target"
    return row[cand_tier]


def _query(project: str, branch: str, paths: list[str],
           age: int | None) -> str:
    files = " OR ".join(f'file:"^{re.escape(p)}$"' for p in paths)
    q = (f"project:{project} branch:{branch} status:open -is:wip "
         f"-is:private ({files})")
    return q + (f" -age:{age}d" if age is not None else "")


def _fetch(client: Any, q: str) -> list[dict[str, Any]]:
    opts = "".join(f"&o={o}" for o in _OPTIONS)
    out: list[dict[str, Any]] = []
    while True:
        start = f"&S={len(out)}" if out else ""
        page = client.rest.get(
            f"/changes/?q={quote(q, safe='')}{opts}&n=500{start}") or []
        out.extend(page)
        if not page or not page[-1].get("_more_changes"):
            return out


def query_candidates(client: Any, project: str, branch: str,
                     paths: list[str], max_age_days: int,
                     queued_ids: set[str] | frozenset[str] = frozenset()
                     ) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for i in range(0, len(paths), _PATHS_PER_QUERY):
        batch = paths[i:i + _PATHS_PER_QUERY]
        for age in (max_age_days, None):
            for ch in _fetch(client, _query(project, branch, batch, age)):
                if age is None and readiness(ch) != "ready" \
                        and ch.get("change_id") not in queued_ids \
                        and release_rank(ch.get("hashtags") or []) == 0:
                    continue
                seen.setdefault(ch["_number"], ch)
    return list(seen.values())
