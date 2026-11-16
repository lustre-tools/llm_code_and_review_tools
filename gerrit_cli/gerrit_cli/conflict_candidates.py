"""Pick the open changes a target should be checked against."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any, Mapping
from urllib.parse import quote

from .graph.nodes import _LUSTRE_CHANGE_TRAILER_RE
from .graph.review import _is_bot_account, _parse_labels

# release tags of the current scheme: never a shared effort, whatever
# the configured release globs
_RELEASE_DEFAULTS = ("*_review_blocker", "*_review_priority")
BLOCKER_ENV = "GERRIT_CONFLICTS_BLOCKER_TAGS"
PRIORITY_ENV = "GERRIT_CONFLICTS_PRIORITY_TAGS"
IGNORE_ENV = "GERRIT_CONFLICTS_IGNORE_TAGS"
_OPTIONS = ("LABELS", "DETAILED_LABELS", "CURRENT_REVISION",
            "CURRENT_COMMIT", "DETAILED_ACCOUNTS")
_PATHS_PER_QUERY = 20


@dataclass(frozen=True)
class Ranks:
    """Which hashtags (fnmatch globs, case-sensitive) make a change a
    release blocker or release priority, and which never make two
    changes the same effort (neither do the release ones, configured or
    default)."""
    blocker: tuple[str, ...] = (_RELEASE_DEFAULTS[0],)
    priority: tuple[str, ...] = (_RELEASE_DEFAULTS[1],)
    ignore: tuple[str, ...] = ("pw_*", "fortestonly", "forbuildonly")

    @classmethod
    def from_env(cls, env: Mapping[str, str], blocker: list[str] | None = None,
                 priority: list[str] | None = None) -> "Ranks":
        """The defaults, replaced by each of the three environment
        variables (comma-separated globs) that is set and not blank,
        and then by the --blocker-tag / --priority-tag values given
        (each comma-separated too; blank ones are dropped)."""
        def split(values: list[str]) -> tuple[str, ...]:
            return tuple(g.strip() for v in values for g in v.split(",") if g.strip())

        def globs(name: str, given: list[str] | None, default: tuple[str, ...]
                  ) -> tuple[str, ...]:
            return split(given or []) or split([env.get(name) or ""]) or default

        d = cls()
        return cls(globs(BLOCKER_ENV, blocker, d.blocker),
                   globs(PRIORITY_ENV, priority, d.priority),
                   globs(IGNORE_ENV, None, d.ignore))

    def rank(self, hashtags: list[str]) -> int:
        """2 for a release blocker, 1 for release priority, else 0."""
        if any(fnmatchcase(t, g) for t in hashtags for g in self.blocker):
            return 2
        if any(fnmatchcase(t, g) for t in hashtags for g in self.priority):
            return 1
        return 0

    def generic(self, tag: str) -> bool:
        return self.rank([tag]) > 0 or any(
            fnmatchcase(tag, g) for g in self.ignore + _RELEASE_DEFAULTS)

    def as_dict(self) -> dict[str, list[str]]:
        return {"blocker": list(self.blocker), "priority": list(self.priority),
                "ignore": list(self.ignore)}


DEFAULT_RANKS = Ranks()


@dataclass
class Selection:
    """Which open changes a target is checked against, and why the
    others are not."""
    # open changes the queries found plus the --with ones, minus the
    # target's own stack and children
    found: dict[int, dict[str, Any]] = field(default_factory=dict)
    forced: set[int] = field(default_factory=set)  # --with
    tiers: dict[int, str] = field(default_factory=dict)
    # the tiered changes and the open changes they stand on
    every: dict[int, dict[str, Any]] = field(default_factory=dict)
    kept: list[int] = field(default_factory=list)  # tried against the target
    not_tried: list[int] = field(default_factory=list)  # "other" tier without --all
    skipped: list[dict[str, Any]] = field(default_factory=list)
    needed: set[int] = field(default_factory=set)  # kept and what they stand on
    ranks: Ranks = DEFAULT_RANKS


def release_rank(hashtags: list[str], ranks: Ranks = DEFAULT_RANKS) -> int:
    return ranks.rank(hashtags)


def shared_hashtags(a: list[str], b: list[str],
                    ranks: Ranks = DEFAULT_RANKS) -> list[str]:
    return sorted(t for t in set(a) & set(b) if not ranks.generic(t))


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
    ranks: Ranks = DEFAULT_RANKS,
) -> tuple[str | None, str | None]:
    if change.get("change_id") in queued_ids:
        return "frozen", None
    blocker = _blocking_reviewer(change)
    if blocker:
        return None, f"Code-Review {blocker[1]} ({blocker[0]})"
    if _verified(change, "jenkins") < 0:
        return None, "Jenkins -1"
    if readiness(change) == "ready" \
            or release_rank(change.get("hashtags") or [], ranks) > 0:
        return "lands-first", None
    updated = _updated(change)
    if updated is not None and now - updated > timedelta(days=max_age_days):
        return None, f"stale (updated {updated:%Y-%m-%d})"
    same = shared_hashtags(change.get("hashtags") or [],
                           target.get("hashtags") or [], ranks)
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


def phrase(value: str) -> str:
    """`value` as one quoted Gerrit query argument. The query parser
    drops the backslash before any character inside quotes, so a
    backslash or quote meant literally needs one of its own."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _path_regex(path: str) -> str:
    """A file: regex matching exactly `path`. The regex (dk.brics, with
    `"`, `<`, `@`, `#`, `~`, `&` special besides the usual) takes a
    backslash before any character as that character, but the server
    rejects one before some letters: only ASCII punctuation (bar `/`
    and `_`) and space get one."""
    return "^" + re.sub(r"([ -.:-@\[-^`{-~])", r"\\\1", path) + "$"


def _query(project: str, branch: str, paths: list[str],
           age: int | None) -> str:
    files = " OR ".join(f"file:{phrase(_path_regex(p))}" for p in paths)
    q = (f"project:{phrase(project)} branch:{phrase(branch)} status:open -is:wip "
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
                     queued_ids: set[str] | frozenset[str] = frozenset(),
                     ranks: Ranks = DEFAULT_RANKS) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for i in range(0, len(paths), _PATHS_PER_QUERY):
        batch = paths[i:i + _PATHS_PER_QUERY]
        for age in (max_age_days, None):
            for ch in _fetch(client, _query(project, branch, batch, age)):
                if age is None and readiness(ch) != "ready" \
                        and ch.get("change_id") not in queued_ids \
                        and release_rank(ch.get("hashtags") or [], ranks) == 0:
                    continue
                seen.setdefault(ch["_number"], ch)
    return list(seen.values())
