"""Backport detection for lreview.

A port to another branch carries Lustre-change: and Lustre-commit:
trailers that name the upstream change. A review of such a commit asks
a different question: is the commit an honest translation of the
upstream change? lreview adds backport-prompt.md to the review prompt
of these commits only, with the upstream commits already fetched.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import worktree as wt
from .gerrit import resolve_change
from .since import range_diff_args

BACKPORT_PROMPT_PATH = Path(__file__).resolve().parent / "backport-prompt.md"

_TRAILER_RE = re.compile(r"^Lustre-(change|commit):[ \t]*(.*?)[ \t]*$",
                         re.MULTILINE)
_HASH_RE = re.compile(r"\b[0-9a-f]{40}\b")


@dataclass
class Trailer:
    """One upstream change that the commit names."""
    url: Optional[str] = None
    commit: Optional[str] = None
    landed: bool = False  # False for "Lustre-commit: TBD ..."


@dataclass(frozen=True)
class Upstream:
    """An upstream change, and the commit to compare the port with."""
    name: str
    sha: Optional[str]  # None: lreview could not get the commit
    landed: bool
    ported_from: Optional[str] = None  # set when the port names another commit


def parse_trailers(body: str) -> list[Trailer]:
    """The upstream changes of a commit message, in order.

    A squashed port has one Lustre-change:/Lustre-commit: pair for each
    change. Older commits have some malformed values: these keep what
    they can, and the commit is still a port.
    """
    trailers: list[Trailer] = []
    for key, value in _TRAILER_RE.findall(body):
        found = _HASH_RE.search(value)
        commit = found.group() if found else None
        if key == "change":
            trailers.append(Trailer(
                url=value if "://" in value else None, commit=commit,
                landed=commit is not None))
        elif trailers and trailers[-1].commit is None:
            trailers[-1].commit = commit
            trailers[-1].landed = not value.upper().startswith("TBD")
        else:
            trailers.append(Trailer(
                commit=commit, landed=not value.upper().startswith("TBD")))
    return trailers


def _resolve(repo: Path, trailer: Trailer) -> Upstream:
    """Make the upstream commit of one trailer available in repo."""
    name = trailer.url or trailer.commit or "an unnamed upstream change"
    local = (trailer.commit is not None
             and wt.commit_exists(repo, trailer.commit))
    if trailer.landed and local:
        return Upstream(name, trailer.commit, True)
    try:
        change = resolve_change(trailer.url) if trailer.url else None
        if change is not None and not wt.commit_exists(repo, change.sha):
            wt.fetch_change(
                repo,
                wt.gerrit_remote_urls(repo, change.base_url, change.project)
                + [change.fetch_url()],
                change.ref)
    except Exception:  # noqa: BLE001 - offline, or no access: use the hash
        change = None
    if change is None:
        return Upstream(name, trailer.commit if local else None,
                        trailer.landed)
    moved = (not trailer.landed and trailer.commit is not None
             and trailer.commit != change.sha)
    return Upstream(name, change.sha, change.status == "MERGED",
                    trailer.commit if moved else None)


def detect(repo: Path, sha: str) -> Optional[list[Upstream]]:
    """The upstream changes of a commit, or None if it is not a port."""
    body = wt.run_git(repo, "log", "-1", "--format=%B", sha).stdout
    trailers = parse_trailers(body)
    if not trailers:
        return None
    return [_resolve(repo, trailer) for trailer in trailers]


def _describe(upstream: Upstream) -> str:
    if upstream.sha is None:
        return (f"- {upstream.name}: lreview could not get this commit. "
                "Try to find it yourself.")
    state = "landed" if upstream.landed else "not landed"
    line = f"- {upstream.name}: commit {upstream.sha}, {state}."
    if upstream.ported_from:
        line += (f" The port names {upstream.ported_from}, a different "
                 "commit, so check what changed between the two.")
    return line


def backport_prompt(upstreams: list[Upstream]) -> str:
    """The prompt section for a review of a port."""
    lines = ["This commit is a backport of:"]
    lines += [_describe(upstream) for upstream in upstreams]
    compare = ["  git " + " ".join(range_diff_args(u.sha, "HEAD"))
               for u in upstreams if u.sha]
    if compare:
        lines += ["", "Compare the port with upstream:", *compare]
    if len(upstreams) > 1:
        lines += ["", "The commit squashes these changes: together, they "
                      "are the upstream side of the comparison."]
    return "\n".join(lines) + "\n\n" + BACKPORT_PROMPT_PATH.read_text()


def compaction_note(upstreams: list[Upstream]) -> str:
    """What a review needs to hear again after a compaction."""
    shas = ", ".join(u.sha or u.name for u in upstreams)
    return (f"This commit is a backport of {shas}. Re-read the backport "
            f"rules {BACKPORT_PROMPT_PATH} and follow them.")
