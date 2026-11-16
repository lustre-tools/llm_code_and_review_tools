"""conflicts: one change against the open changes likely to land around it.

The target (a change, or a local commit standing in for it with
--commit) and its in-flight parents form the focus stack. The open
changes on the same branch that touch its files are the candidates:
those that need a new patchset anyway are skipped, the rest tiered
(conflict_candidates.py). Each candidate in a checked tier is
trial-merged against the target in both orders and both models
(conflicts.py, focus mode), and each collision says which side should
move (`fix_side`).

run_conflicts runs the steps in order: open target -> related changes
and branch tips -> uploaded focus stack -> branch and -next (fetched with that stack)
-> local focus stack -> candidate selection -> engine -> older parent
patchsets; conflict_report.py turns the result into the printed JSON.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from ..conflict_candidates import (
    DEFAULT_RANKS,
    Ranks,
    Selection,
    classify,
    phrase,
    query_candidates,
)
from ..conflict_report import Checked, compact, report, target_tier
from ..conflicts import (
    ConflictCheckError,
    ConflictRepoError,
    Patch,
    Repo,
    base_info,
    change_id_lookup,
    change_ref,
    changes_by_commit,
    check_conflicts,
    commit_lookup,
)
from ..errors import ErrorCode
from ..parallel import parallel_map
from ._helpers import _cli, error_code_for, output_error, output_success

_DETAIL = ("CURRENT_REVISION", "CURRENT_COMMIT", "LABELS", "DETAILED_LABELS",
           "DETAILED_ACCOUNTS")
_OPTIONS = "".join(f"&o={o}" for o in _DETAIL)
# with ALL_REVISIONS, which changes_by_commit adds
_WALK_OPTIONS = "".join(f"&o={o}" for o in _DETAIL[1:])
_MAX_DEPTH = 100
# classify with this age only skips for a vote that needs a new patchset
_NEVER_STALE = 10 ** 6


def _client() -> Any:
    return _cli().GerritCommentsClient()


def _current(change: dict[str, Any]) -> tuple[str, int, str]:
    """(commit, patchset, parent commit) of the current revision."""
    sha = change.get("current_revision") or ""
    rev = (change.get("revisions") or {}).get(sha) or {}
    parents = (rev.get("commit") or {}).get("parents") or []
    return sha, rev.get("_number", 0), parents[0].get("commit", "") if parents else ""


def _query(client: Any, q: str, options: str = _OPTIONS) -> list[dict[str, Any]]:
    return client.rest.get(f"/changes/?q={quote(q, safe='')}{options}&n=500") or []


class _Patchsets:
    """Which patchset of which change each known commit is, and each
    change's current patchset."""

    def __init__(self) -> None:
        self.of: dict[str, tuple[int, int]] = {}
        self.current: dict[int, int] = {}

    def add_change(self, change: dict[str, Any]) -> None:
        cn = change.get("_number", 0)
        for sha, rev in (change.get("revisions") or {}).items():
            if rev.get("_number"):
                self.of[sha] = (cn, rev["_number"])
        if ps := _current(change)[1]:
            self.current[cn] = ps

    def add_related(self, entry: dict[str, Any]) -> None:
        cn = entry["_change_number"]
        self.of[entry["commit"]["commit"]] = (cn, entry.get("_revision_number", 0))
        if entry.get("_current_revision_number"):
            self.current[cn] = entry["_current_revision_number"]

    def older(self, commit: str, below: int | None = None) -> dict[str, int] | None:
        """{"of", "ps", "current"} when `commit` is an older patchset (of
        change `below`, when given)."""
        cn, ps = self.of.get(commit, (0, 0))
        cur = self.current.get(cn)
        if not ps or not cur or ps == cur or (below is not None and cn != below):
            return None
        return {"of": cn, "ps": ps, "current": cur}


def _fetch(repo: Repo, remote: str, refs: dict[str, str]) -> None:
    """Fetch the refs ({commit: ref}) whose commits are not in repo."""
    missing = repo.missing(list(refs))
    if missing:
        repo.fetch_refs(remote, [refs[c] for c in refs if c in missing])


def _ancestors(client: Any, project: str, branch: str, changes: dict[int, dict[str, Any]],
               known: set[int], owners: dict[str, tuple[int, str]]) -> dict[int, dict[str, Any]]:
    """The in-flight changes `changes` stand on, transitively, that are
    not in `known`. Every revision seen goes into `owners`."""
    found: dict[int, dict[str, Any]] = {}
    known = set(known) | set(changes)
    todo = {p for c in changes.values() if (p := _current(c)[2])}
    seen: set[str] = set()
    for _ in range(_MAX_DEPTH):
        shas = sorted(s for s in todo if s not in seen and s not in owners)
        if not shas:
            break
        seen.update(shas)
        todo = set()
        for c in changes_by_commit(client.rest.get, shas, options=_WALK_OPTIONS):
            cn = c.get("_number", 0)
            for h in c.get("revisions") or {}:
                owners[h] = (cn, c.get("status", ""))
            if (c.get("status") != "NEW" or c.get("project") != project
                    or c.get("branch") != branch or cn in known):
                continue
            known.add(cn)
            found[cn] = c
            if parent := _current(c)[2]:
                todo.add(parent)
    return found


def _target_stack(client: Any, change: dict[str, Any], related: list[dict[str, Any]],
                  owners: dict[str, tuple[int, str]], patchsets: _Patchsets,
                  ) -> tuple[list[Patch], dict[int, dict[str, Any]]]:
    """The uploaded target and its in-flight parents, bottom first (not
    fetched: _branch fetches them with the branch)."""
    cn = change["_number"]
    by_sha = {e["commit"]["commit"]: e for e in related}
    below: list[int] = []
    sha = _current(change)[2]
    while sha in by_sha and by_sha[sha].get("status") == "NEW" \
            and by_sha[sha]["_change_number"] != cn:
        e = by_sha[sha]
        below.append(e["_change_number"])
        parents = e["commit"].get("parents") or []
        sha = parents[0]["commit"] if parents else ""
    below.reverse()
    details = {c["_number"]: c for c in _query(client, " OR ".join(f"change:{n}" for n in below))} \
        if below else {}
    for d in details.values():
        patchsets.add_change(d)
    patches = []
    for n in below:
        if n in details:
            commit, ps, _ = _current(details[n])
        else:
            e = next(e for e in related if e["_change_number"] == n)
            commit, ps = e["commit"]["commit"], e["_revision_number"]
        owners[commit] = (n, "NEW")
        patches.append(Patch(n, ps, commit))
    commit, ps, _ = _current(change)
    patches.append(Patch(cn, ps, commit))
    return patches, details


def _local_stack(client: Any, repo: Repo, remote: str, change: dict[str, Any], commit: str,
                 tip: str, owners: dict[str, tuple[int, str]], patchsets: _Patchsets,
                 ) -> tuple[list[Patch], dict[int, dict[str, Any]]]:
    """The local commit and the changes below it, bottom first. A commit
    without an open change of its own belongs to the change above it,
    the ones above the top change to the target. A change below the
    target whose local commit is an older patchset of it is its current
    patchset instead, the commit above standing on the older one as on
    Gerrit; a local commit that is no patchset stays (the user edits
    that change too)."""
    cn, project, branch = change["_number"], change["project"], change["branch"]
    sha = repo.rev_parse(commit)
    if sha is None:
        raise ConflictRepoError(f"{commit}: not in {repo.path}")
    local = repo.first_parent_range(tip, sha)
    msgs = repo.messages(local)
    ids = sorted({cid for _subject, cid in msgs.values() if cid})
    by_id: dict[str, dict[str, Any]] = {}
    if ids:
        q = (f"({' OR '.join(f'change:{i}' for i in ids)}) "
             f"project:{phrase(project)} branch:{phrase(branch)}")
        by_id = {c["change_id"]: c for c in _query(client, q, _OPTIONS + "&o=ALL_REVISIONS")
                 if c.get("status") == "NEW"}
        for c in by_id.values():
            patchsets.add_change(c)
    start = (repo.parents(local[:1]).get(local[0]) or [""])[0] if local else ""
    groups: list[list[Any]] = []  # [change, commit, base], bottom first
    run_base = start
    for c in local:
        found = by_id.get(msgs.get(c, ("", ""))[1])
        if found is None:
            continue
        # the same change twice: its first commit belongs to its last
        earlier = next((g for g in groups if g[0]["_number"] == found["_number"]), None)
        if earlier is not None:
            groups.remove(earlier)
        groups.append([found, c, earlier[2] if earlier else run_base])
        run_base = c
    at = next((i for i, g in enumerate(groups) if g[0]["_number"] == cn), None)
    if at is None:
        raise ConflictRepoError(
            f"{commit}: no commit between {branch} and it belongs to change {cn}")
    if at != len(groups) - 1:
        above = ", ".join(str(g[0]["_number"]) for g in groups[at + 1:])
        raise ConflictRepoError(f"{commit}: change(s) {above} sit above {cn}; pass {cn}'s commit")
    groups[-1][1] = sha
    parents = repo.parents([g[1] for g in groups])
    patches = []
    newer: dict[str, str] = {}
    for found, top, base in groups:
        n = found["_number"]
        owners[top] = (n, "NEW")
        cur, cur_ps, _ = _current(found)
        if n != cn and patchsets.older(top, n):
            owners[cur] = (n, "NEW")
            newer[cur] = change_ref(n, cur_ps)
            patches.append(Patch(n, cur_ps, cur))
        else:
            patches.append(Patch(n, 0, top, "" if parents.get(top) == [base] else base))
    _fetch(repo, remote, newer)
    return patches, {g[0]["_number"]: g[0] for g in groups[:-1]}


@dataclass
class _Branch:
    """The checkout and the branch the target lands on."""
    repo: Repo
    remote: str
    project: str
    branch: str
    tip: str
    next_branch: str
    queued_ids: set[str]  # Change-Ids queued on a current -next
    heads: dict[str, str]  # branch and -next tips; a missing one left out


@dataclass
class _Around:
    """What the target's related list says: who owns each commit seen,
    and the open children above it."""
    related: list[dict[str, Any]]
    owners: dict[str, tuple[int, str]]
    patchsets: _Patchsets
    children: list[Patch]
    child_base: dict[int, str]  # child -> the commit it stands on


@dataclass
class _Focus:
    """The stack being checked: the target (or its local commit) on top
    of its in-flight parents."""
    stack: list[Patch]  # bottom first
    parents_detail: dict[int, dict[str, Any]]
    diff_of: dict[str, str]  # commit -> the commit its own diff starts from
    paths: list[str]  # every file the stack changes

    @property
    def cns(self) -> list[int]:
        return [p.cn for p in self.stack]


def _open_target(client: Any, change: str) -> tuple[int, dict[str, Any]]:
    _base, cn = _cli().GerritCommentsClient.parse_gerrit_url(change)
    target = client.rest.get(f"/changes/{cn}?{_OPTIONS[1:]}")
    if target.get("status") != "NEW":
        raise ConflictRepoError(f"change {cn} is {target.get('status')}; only an open "
                                "change can be checked")
    return cn, target


def _related_and_tips(client: Any, repo: Repo, remote: str, cn: int, target: dict[str, Any]
                      ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The target's related changes and the branch and -next tips, read
    at once."""
    branches = [target["branch"], f"{target['branch']}-next"]
    related, heads = parallel_map(lambda read: read(), [
        lambda: client.rest.get(f"/changes/{cn}/revisions/current/related"),
        lambda: _branch_tips(client, repo, remote, target["project"], branches)])
    return (related or {}).get("changes") or [], heads


def _branch_tips(client: Any, repo: Repo, remote: str, project: str,
                 branches: list[str]) -> dict[str, str]:
    """Tip of each branch that exists. By REST: `git ls-remote` over
    SSH costs a connection of seconds; it is the fallback."""
    try:
        revs = parallel_map(lambda b: client.get_branch_revision(project, b), branches)
    except (requests.RequestException, OSError, ValueError):
        return repo.branch_tips(remote, branches)
    return {b: r for b, r in zip(branches, revs) if r}


def _clone(repo_path: str, project: str) -> tuple[Repo, str]:
    """The checkout and its remote for `project`."""
    repo = Repo(repo_path)
    remote = repo.resolve_remote(project)
    if remote is None:
        raise ConflictRepoError(f"{repo.path} is not a clone of {project}; pass --repo")
    return repo, remote


def _branch(repo: Repo, remote: str, target: dict[str, Any], heads: dict[str, str],
            refs: dict[str, str]) -> _Branch:
    """Fetch the branch and its -next (`heads`, their tips as Gerrit has
    them), with `refs` ({commit: ref}) in the same fetch: each fetch is
    an SSH connection of seconds. A tip the remote does not deliver (a
    mirror behind Gerrit, a -next pushed again since) is read from the
    remote instead. A stale -next holds what already landed, so nothing
    in it counts as queued."""
    project, branch = target["project"], target["branch"]
    next_branch = f"{branch}-next"
    if branch not in heads:
        raise ConflictCheckError(f"{remote} has no branch {branch}")
    _fetch(repo, remote, {**refs, **{sha: f"refs/heads/{b}" for b, sha in heads.items()}})
    if repo.missing(list(heads.values())):
        heads = repo.branch_tips(remote, [branch, next_branch])
        if branch not in heads:
            raise ConflictCheckError(f"{remote} has no branch {branch}")
        _fetch(repo, remote, {sha: f"refs/heads/{b}" for b, sha in heads.items()})
        if repo.missing([heads[branch]]):
            raise ConflictCheckError(f"could not fetch {branch} from {remote}")
    tip, next_tip = heads[branch], heads.get(next_branch)
    queued_ids: set[str] = set()
    if next_tip and not repo.missing([tip, next_tip]) and repo.is_ancestor(tip, next_tip):
        queue = repo.first_parent_range(tip, next_tip)
        queued_ids = {cid for _subject, cid in repo.messages(queue).values() if cid}
    return _Branch(repo, remote, project, branch, tip, next_branch, queued_ids, heads)


def _around(get: Any, cn: int, target: dict[str, Any], related: list[dict[str, Any]]
            ) -> _Around:
    owners: dict[str, tuple[int, str]] = {}
    patchsets = _Patchsets()
    for e in related:
        owners[e["commit"]["commit"]] = (e["_change_number"], e.get("status", ""))
        patchsets.add_related(e)
    owners[_current(target)[0]] = (cn, target.get("status", ""))
    patchsets.add_change(target)
    at = next((i for i, e in enumerate(related) if e.get("_change_number") == cn), 0)
    above = [e for e in reversed(related[:at]) if e.get("status") == "NEW"]
    children = [Patch(e["_change_number"], e["_revision_number"], e["commit"]["commit"])
                for e in above]
    # a child on an older patchset of the change below it: the related
    # list only has the patchsets in the chain
    loose = sorted({p["commit"] for e in above for p in e["commit"].get("parents") or []}
                   - set(owners))
    for c in changes_by_commit(get, loose) if loose else []:
        for h in c.get("revisions") or {}:
            owners[h] = (c.get("_number", 0), c.get("status", ""))
        patchsets.add_change(c)
    child_base = {e["_change_number"]: (e["commit"].get("parents") or [{}])[0].get("commit", "")
                  for e in above}
    return _Around(related, owners, patchsets, children, child_base)


def _focus(client: Any, br: _Branch, target: dict[str, Any], commit: str | None,
           ar: _Around, uploaded: tuple[list[Patch], dict[int, dict[str, Any]]] | None
           ) -> _Focus:
    """`uploaded`: _target_stack's result, for the uploaded target."""
    if commit:
        stack, parents_detail = _local_stack(client, br.repo, br.remote, target, commit, br.tip,
                                             ar.owners, ar.patchsets)
    else:
        assert uploaded is not None
        stack, parents_detail = uploaded
    bases = br.repo.parents([p.commit for p in stack if not p.base])
    diff_of = {p.commit: p.base or (bases.get(p.commit) or [""])[0] for p in stack}
    paths = sorted(set().union(*br.repo.changed_files(
        {c: b for c, b in diff_of.items() if b}).values()))
    return _Focus(stack, parents_detail, diff_of, paths)


def _query_step(client: Any, br: _Branch, paths: list[str], own: set[int], with_: list[int],
                max_age: int, sel: Selection) -> None:
    """The open changes on the target's files, plus the --with ones."""
    sel.found = {c["_number"]: c for c in query_candidates(
        client, br.project, br.branch, paths, max_age, frozenset(br.queued_ids), sel.ranks)
        if c["_number"] not in own}
    sel.forced = set(with_ or []) - own
    sel.skipped = [{"cn": n, "reason": "own stack"} for n in sorted(set(with_ or []) & own)]
    if sel.forced - set(sel.found):
        extra = _query(client, " OR ".join(f"change:{n}"
                                           for n in sorted(sel.forced - set(sel.found))))
        for c in extra:
            if c.get("status") == "NEW" and c.get("project") == br.project \
                    and c.get("branch") == br.branch and c["_number"] in sel.forced:
                sel.found[c["_number"]] = c
        sel.skipped += [{"cn": n, "reason": f"not open on {br.project} {br.branch}"}
                        for n in sorted(sel.forced - set(sel.found))]


def _tier_step(target: dict[str, Any], queued_ids: set[str], max_age: int, now: datetime,
               sel: Selection) -> None:
    """A tier for each found change, or the reason it is dropped."""
    for n in sorted(sel.found):
        tier, reason = classify(sel.found[n], target, queued_ids, max_age, now, sel.ranks)
        if tier is None and n in sel.forced:
            tier = "other"
        if tier is None:
            sel.skipped.append({"cn": n, "reason": reason})
        else:
            sel.tiers[n] = tier


def _stands_on(n: int, every: dict[int, dict[str, Any]],
               owners: dict[str, tuple[int, str]]) -> list[int]:
    """The changes of `every` that change `n` stands on, nearest first."""
    out, cur = [], n
    while True:
        o = owners.get(_current(every[cur])[2])
        if o is None or o[0] not in every or o[0] in out or o[0] in (n, cur):
            return out
        cur = o[0]
        out.append(cur)


def _stacked_step(client: Any, br: _Branch, target: dict[str, Any], own: set[int],
                  owners: dict[str, tuple[int, str]], all_: bool, now: datetime,
                  sel: Selection) -> None:
    """Drop a change standing on one that needs a new patchset; keep an
    "other" one for --all only."""
    tiered = {n: sel.found[n] for n in sel.tiers}
    for n, c in tiered.items():
        owners[_current(c)[0]] = (n, "NEW")
    ancestors = _ancestors(client, br.project, br.branch, tiered, own, owners)
    sel.every = {**ancestors, **tiered}
    for n in sorted(sel.tiers):
        chain = _stands_on(n, sel.every, owners)
        if n not in sel.forced and sel.tiers[n] != "frozen":
            reason = next(((a, r) for a in chain
                           if (r := classify(sel.every[a], target, br.queued_ids,
                                             _NEVER_STALE, now, sel.ranks)[1])),
                          None)
            if reason:
                sel.skipped.append({"cn": n, "reason": f"stacked on {reason[0]} ({reason[1]})"})
                continue
        if sel.tiers[n] == "other" and not all_ and n not in sel.forced:
            sel.not_tried.append(n)
        else:
            sel.kept.append(n)
    sel.skipped.sort(key=lambda s: s["cn"])
    sel.needed = {a for n in sel.kept for a in _stands_on(n, sel.every, owners)} | set(sel.kept)


def _select(client: Any, br: _Branch, target: dict[str, Any], focus: _Focus, ar: _Around,
            max_age: int, with_: list[int], all_: bool, now: datetime,
            ranks: Ranks) -> Selection:
    """query -> drop the own stack -> tier -> drop what stands on a
    change that needs a new patchset -> park "other" without --all."""
    own = set(focus.cns) | {p.cn for p in ar.children}
    sel = Selection(ranks=ranks)
    _query_step(client, br, focus.paths, own, with_, max_age, sel)
    _tier_step(target, br.queued_ids, max_age, now, sel)
    _stacked_step(client, br, target, own, ar.owners, all_, now, sel)
    return sel


def _try(client: Any, br: _Branch, focus: _Focus, ar: _Around, sel: Selection,
         hunks: bool | set[int]) -> dict[str, Any]:
    get = client.rest.get
    patches = focus.stack + ar.children + [
        Patch(n, _current(sel.every[n])[1], _current(sel.every[n])[0]) for n in sorted(sel.needed)]
    return check_conflicts(
        br.repo, br.remote, br.branch, patches, ar.owners, lookup=commit_lookup(get),
        next_branch=br.next_branch, change_lookup=change_id_lookup(get, br.project, br.branch),
        focus=set(focus.cns), hunks=hunks, pair_with=set(sel.kept), tips=br.heads)


def _on_branch(result: dict[str, Any], cn: int) -> dict[str, Any]:
    on_branch = dict(result["results"].get(str(cn)) or {"status": "error"})
    if on_branch["status"] in ("error", "skipped"):
        raise ConflictCheckError(
            f"change {cn} could not be tried: {on_branch.get('reason', 'unknown')}")
    return on_branch


def _older_parents(br: _Branch, focus: _Focus, ar: _Around, cn: int,
                   on_branch: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[int, dict]]:
    """(the target's parent's newer patchset and whether the target
    applies on it, the other stack members on an older patchset)."""
    stack = focus.stack
    old = {p.cn: o for below_p, p in zip(stack, stack[1:])
           if (o := ar.patchsets.older(focus.diff_of[p.commit], below_p.cn))}
    old.update({n: o for n, sha in ar.child_base.items() if (o := ar.patchsets.older(sha))})
    outdated = old.pop(cn, None)
    if outdated is not None:
        target_p, parent_p = stack[-1], stack[-2]
        if on_branch.get("parent"):
            outdated["status"], files = "conflict", on_branch["parent"]["files"]
        else:
            with br.repo:
                pick = br.repo.pick(parent_p.commit, target_p.commit,
                                    focus.diff_of[target_p.commit])
            outdated["status"] = "clean" if pick.ok else pick.status
            files = list(pick.files)
        outdated = {"cn": outdated.pop("of"), **outdated}
        if files:
            outdated["files"] = files
    return outdated, old


def run_conflicts(client: Any, repo_path: str, change: str, commit: str | None, max_age: int,
                  with_: list[int], all_: bool, hunks: bool | set[int], now: datetime,
                  ranks: Ranks = DEFAULT_RANKS) -> dict[str, Any]:
    """Check one change, or a local commit standing in for it, against
    the open changes that touch its files. `hunks`: True for every
    pair's conflict regions, a set of change numbers for those pairs;
    `ranks`: the release hashtags (the command reads them from the
    environment and the options)."""
    cn, target = _open_target(client, change)
    repo, remote = _clone(repo_path, target["project"])
    related, heads = _related_and_tips(client, repo, remote, cn, target)
    ar = _around(client.rest.get, cn, target, related)
    uploaded = None if commit else _target_stack(client, target, ar.related, ar.owners,
                                                 ar.patchsets)
    br = _branch(repo, remote, target, heads,
                 {p.commit: change_ref(p.cn, p.ps) for p in uploaded[0]} if uploaded else {})
    focus = _focus(client, br, target, commit, ar, uploaded)
    sel = _select(client, br, target, focus, ar, max_age, with_, all_, now, ranks)
    result = _try(client, br, focus, ar, sel, hunks)

    on_branch = _on_branch(result, cn)
    outdated, old = _older_parents(br, focus, ar, cn, on_branch)
    tiers_of_parents = {n: target_tier(d, br.queued_ids, ranks)
                        for n, d in focus.parents_detail.items()}
    base = base_info(br.repo, result, {p.cn: p for p in focus.stack}, focus.cns, result["tip"],
                     tiers_of_parents)
    uploaded, ps, _ = _current(target)
    chk = Checked(target, cn, ps, uploaded, focus.stack, [p.cn for p in ar.children],
                  br.queued_ids, result, on_branch, outdated, old, ar.patchsets.current, base,
                  hunks)
    return report(chk, sel, br.next_branch, os.environ.get("GERRIT_USER"))


def cmd_conflicts(args: Any) -> None:
    """Which open changes the given change collides with, and who moves."""
    command = "conflicts"
    pretty = getattr(args, "pretty", False)
    if args.max_age <= 0:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, "--max-age must be positive",
                              command, pretty))
    try:
        result = run_conflicts(
            _client(), args.repo, args.change, args.commit, args.max_age,
            args.with_ or [], args.all,
            True if args.hunks else set(getattr(args, "hunks_for", None) or []),
            datetime.now(timezone.utc).replace(tzinfo=None),
            Ranks.from_env(os.environ, args.blocker_tag, args.priority_tag))
        output_success(result if args.full else compact(result), command, pretty)
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))
