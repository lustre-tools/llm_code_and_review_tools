"""conflicts: one change against the open changes likely to land around it.

The target (a change, or a local commit standing in for it with
--commit) and its in-flight parents form the focus stack. The open
changes on the same branch that touch its files are the candidates:
those that need a new patchset anyway are skipped, the rest tiered
(conflict_candidates.py). Each candidate in a checked tier is
trial-merged against the target in both orders and both models
(conflicts.py, focus mode), and each collision says which side should
move (`fix_side`).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ..client import GerritCommentsClient
from ..conflict_candidates import classify, fix_side, query_candidates, readiness, release_rank
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
    own_stack,
)
from ..errors import ErrorCode
from ._helpers import _cli, error_code_for, output_error, output_success

_DETAIL = ("CURRENT_REVISION", "CURRENT_COMMIT", "LABELS", "DETAILED_LABELS",
           "DETAILED_ACCOUNTS")
_OPTIONS = "".join(f"&o={o}" for o in _DETAIL)
# with ALL_REVISIONS, which changes_by_commit adds
_WALK_OPTIONS = "".join(f"&o={o}" for o in _DETAIL[1:])
_TIER_ORDER = {"frozen": 0, "lands-first": 1, "same-effort": 2, "other": 3}
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


def _query(client: Any, q: str) -> list[dict[str, Any]]:
    return client.rest.get(f"/changes/?q={quote(q, safe='')}{_OPTIONS}&n=500") or []


def _owner_name(change: dict[str, Any]) -> str:
    owner = change.get("owner") or {}
    return owner.get("name") or owner.get("username") or ""


def _tier(change: dict[str, Any], queued_ids: set[str]) -> str:
    """A target's own tier (the fix_side row)."""
    if change.get("change_id") in queued_ids:
        return "frozen"
    if readiness(change) == "ready" or release_rank(change.get("hashtags") or []) > 0:
        return "lands-first"
    return "pending"


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


def _target_stack(client: Any, repo: Repo, remote: str, change: dict[str, Any],
                  related: list[dict[str, Any]], owners: dict[str, tuple[int, str]],
                  ) -> tuple[list[Patch], dict[int, dict[str, Any]]]:
    """The uploaded target and its in-flight parents, bottom first."""
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
    _fetch(repo, remote, {p.commit: change_ref(p.cn, p.ps) for p in patches})
    return patches, details


def _local_stack(client: Any, repo: Repo, change: dict[str, Any], commit: str, tip: str,
                 owners: dict[str, tuple[int, str]],
                 ) -> tuple[list[Patch], dict[int, dict[str, Any]]]:
    """The local commit and the changes below it, bottom first. A commit
    without an open change of its own belongs to the change above it,
    the ones above the top change to the target."""
    cn, project, branch = change["_number"], change["project"], change["branch"]
    sha = repo.rev_parse(commit)
    if sha is None:
        raise ConflictRepoError(f"{commit}: not in {repo.path}")
    local = repo.first_parent_range(tip, sha)
    msgs = repo.messages(local)
    ids = sorted({cid for _subject, cid in msgs.values() if cid})
    by_id: dict[str, dict[str, Any]] = {}
    if ids:
        q = f"({' OR '.join(f'change:{i}' for i in ids)}) project:{project} branch:{branch}"
        by_id = {c["change_id"]: c for c in _query(client, q) if c.get("status") == "NEW"}
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
    for found, top, base in groups:
        n = found["_number"]
        owners[top] = (n, "NEW")
        patches.append(Patch(n, 0, top, "" if parents.get(top) == [base] else base))
    return patches, {g[0]["_number"]: g[0] for g in groups[:-1]}


def run_conflicts(client: Any, repo_path: str, change: str, commit: str | None, max_age: int,
                  with_: list[int], all_: bool, hunks: bool, now: datetime) -> dict[str, Any]:
    """Check one change, or a local commit standing in for it, against
    the open changes that touch its files."""
    _base, cn = GerritCommentsClient.parse_gerrit_url(change)
    target = client.rest.get(f"/changes/{cn}?{_OPTIONS[1:]}")
    project, branch = target["project"], target["branch"]
    repo = Repo(repo_path)
    remote = repo.resolve_remote(project)
    if remote is None:
        raise ConflictRepoError(f"{repo.path} is not a clone of {project}; pass --repo")
    get = client.rest.get
    next_branch = f"{branch}-next"
    heads = repo.branch_tips(remote, [branch, next_branch])
    if branch not in heads:
        raise ConflictCheckError(f"{remote} has no branch {branch}")
    tip, next_tip = heads[branch], heads.get(next_branch)
    _fetch(repo, remote, {sha: f"refs/heads/{b}" for b, sha in heads.items()})
    queued_ids: set[str] = set()
    # a stale -next holds what already landed; nothing in it counts
    if next_tip and not repo.missing([tip, next_tip]) and repo.is_ancestor(tip, next_tip):
        queue = repo.first_parent_range(tip, next_tip)
        queued_ids = {cid for _subject, cid in repo.messages(queue).values() if cid}

    related = (get(f"/changes/{cn}/revisions/current/related") or {}).get("changes") or []
    owners: dict[str, tuple[int, str]] = {}
    for e in related:
        owners[e["commit"]["commit"]] = (e["_change_number"], e.get("status", ""))
    uploaded, ps, _ = _current(target)
    owners[uploaded] = (cn, target.get("status", ""))
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

    if commit:
        stack, parents_detail = _local_stack(client, repo, target, commit, tip, owners)
    else:
        stack, parents_detail = _target_stack(client, repo, remote, target, related, owners)
    focus_stack = [p.cn for p in stack]
    bases = repo.parents([p.commit for p in stack if not p.base])
    diff_of = {p.commit: p.base or (bases.get(p.commit) or [""])[0] for p in stack}
    paths = sorted(set().union(*repo.changed_files(
        {c: b for c, b in diff_of.items() if b}).values()))

    own = set(focus_stack) | {p.cn for p in children}
    found = {c["_number"]: c for c in query_candidates(
        client, project, branch, paths, max_age, frozenset(queued_ids)) if c["_number"] not in own}
    forced = set(with_ or []) - own
    skipped: list[dict[str, Any]] = [{"cn": n, "reason": "own stack"}
                                     for n in sorted(set(with_ or []) & own)]
    if forced - set(found):
        extra = _query(client, " OR ".join(f"change:{n}" for n in sorted(forced - set(found))))
        for c in extra:
            if c.get("status") == "NEW" and c.get("project") == project \
                    and c.get("branch") == branch and c["_number"] in forced:
                found[c["_number"]] = c
        skipped += [{"cn": n, "reason": f"not open on {project} {branch}"}
                    for n in sorted(forced - set(found))]

    tiers: dict[int, str] = {}
    for n in sorted(found):
        tier, reason = classify(found[n], target, queued_ids, max_age, now)
        if tier is None and n in forced:
            tier = "other"
        if tier is None:
            skipped.append({"cn": n, "reason": reason})
        else:
            tiers[n] = tier

    tiered = {n: found[n] for n in tiers}
    for n, c in tiered.items():
        owners[_current(c)[0]] = (n, "NEW")
    ancestors = _ancestors(client, project, branch, tiered, own, owners)
    every = {**ancestors, **tiered}

    def below(n: int) -> list[int]:
        out, cur = [], n
        while True:
            o = owners.get(_current(every[cur])[2])
            if o is None or o[0] not in every or o[0] in out or o[0] in (n, cur):
                return out
            cur = o[0]
            out.append(cur)

    kept, not_tried = [], []
    for n in sorted(tiers):
        chain = below(n)
        if n not in forced and tiers[n] != "frozen":
            reason = next(((a, r) for a in chain
                           if (r := classify(every[a], target, queued_ids, _NEVER_STALE, now)[1])),
                          None)
            if reason:
                skipped.append({"cn": n, "reason": f"stacked on {reason[0]} ({reason[1]})"})
                continue
        if tiers[n] == "other" and not all_ and n not in forced:
            not_tried.append(n)
        else:
            kept.append(n)
    skipped.sort(key=lambda s: s["cn"])
    needed = {a for n in kept for a in below(n)} | set(kept)

    patches = stack + children + [Patch(n, _current(every[n])[1], _current(every[n])[0])
                                  for n in sorted(needed)]
    result = check_conflicts(
        repo, remote, branch, patches, owners, lookup=commit_lookup(get),
        next_branch=next_branch, change_lookup=change_id_lookup(get, project, branch),
        focus=set(focus_stack), hunks=hunks, pair_with=set(kept))

    res = result["results"]
    on_branch = dict(res.get(str(cn)) or {"status": "error"})
    if on_branch["status"] in ("error", "skipped"):
        raise ConflictCheckError(
            f"change {cn} could not be tried: {on_branch.get('reason', 'unknown')}")
    landed = on_branch["status"] == "clean"
    nxt = result.get("next")
    if nxt is None:
        on_next = {"status": "no-next", "branch": next_branch}
    elif "skipped" in nxt:
        on_next = {"status": "stale", "branch": next_branch, "reason": nxt["skipped"]}
    else:
        on_next = nxt["results"].get(str(cn)) or {"status": "not-tried"}

    hashtags = target.get("hashtags") or []
    t_tier, t_rank = _tier(target, queued_ids), release_rank(hashtags)

    def entry(pair: dict[str, Any]) -> dict[str, Any]:
        n = pair["other"]
        c = every[n]
        e: dict[str, Any] = {
            "cn": n, "subject": c.get("subject", ""), "owner": _owner_name(c),
            "tier": tiers[n], "readiness": readiness(c),
            "hashtags": sorted(c.get("hashtags") or []),
            "fix_side": fix_side(t_tier, t_rank, tiers[n], release_rank(c.get("hashtags") or [])),
            "files": sorted(set(pair["landing"]["files"]) | set(pair["as_uploaded"]["files"])),
        }
        if landed:
            e["landing"] = {k: pair["landing"][k] for k in ("target_first", "candidate_first")}
        e["as_uploaded"] = {k: pair["as_uploaded"][k]
                            for k in ("target_on_theirs", "theirs_on_target")}
        e["via"] = pair["via"]
        e["silent"] = pair["silent"]
        if "hunks" in pair:
            e["hunks"] = pair["hunks"] if landed else \
                {k: v for k, v in pair["hunks"].items() if k != "landing"}
        if n in forced:
            e["forced"] = True
        return e

    def order(e: dict[str, Any]) -> tuple[int, int, int]:
        return (_TIER_ORDER[e["tier"]],
                -release_rank(every[e["cn"]].get("hashtags") or []), e["cn"])

    conflicts: list[dict[str, Any]] = []
    silent_only: list[dict[str, Any]] = []
    clean: list[int] = []
    errors = [{"cn": n, "reason": res.get(str(n), {}).get("reason", "")}
              for n in kept if res.get(str(n), {}).get("status") == "error"]
    skipped += [{"cn": n, "reason": res[str(n)].get("reason", "")}
                for n in kept if res.get(str(n), {}).get("status") == "skipped"]
    skipped.sort(key=lambda s: s["cn"])
    entries = [(pair, entry(pair)) for pair in result["focus_pairs"]]
    for pair, e in entries if landed else []:
        statuses = [*e["landing"].values(), *e["as_uploaded"].values()]
        if "conflict" in statuses:
            conflicts.append(e)
        elif pair["silent"]:
            silent_only.append({k: e[k] for k in ("cn", "tier", "fix_side", "silent")})
        elif "error" in statuses:
            errors.append({"cn": e["cn"], "reason": "trial merge failed"})
        else:
            clean.append(e["cn"])
    conflicts.sort(key=order)
    silent_only.sort(key=order)
    tiers_of_parents = {n: _tier(d, queued_ids) for n, d in parents_detail.items()}

    out: dict[str, Any] = {
        "change": cn,
        "patchset": ps,
        "commit": uploaded,
        "tested_commit": stack[-1].commit,
        "subject": target.get("subject", ""),
        "owner": _owner_name(target),
        "me": os.environ.get("GERRIT_USER"),
        "readiness": readiness(target),
        "tier_of_target": t_tier,
        "release_hashtags": sorted(h for h in hashtags if release_rank([h]) > 0),
        "project": project,
        "branch": branch,
        "tip": result["tip"],
        "status": "ok" if landed else "needs-rebase",
        "base": base_info(repo, result, {p.cn: p for p in stack}, focus_stack,
                          result["tip"], tiers_of_parents),
        "on_branch": on_branch,
        "on_next": on_next,
        "own_stack": own_stack(result, focus_stack, [p.cn for p in children]),
        "conflicts": conflicts if landed else None,
    }
    if not landed:
        out["as_uploaded_only"] = sorted((e for _pair, e in entries), key=order)
    out.update({
        "silent_only": silent_only,
        "already_conflicting": sorted(result["already_conflicting"]),
        "not_tried": {"other": not_tried},
        "clean": clean,
        "skipped": skipped,
        "errors": errors,
        "counts": {
            "candidates": len(found),
            "after_filters": len(kept) + len(not_tried),
            "tried": len(result["focus_pairs"]),
            "conflicting": len(conflicts) + len(silent_only) if landed else
            sum(1 for _pair, e in entries if "conflict" in e["as_uploaded"].values()),
        },
    })
    return out


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
            args.with_ or [], args.all, args.hunks,
            datetime.now(timezone.utc).replace(tzinfo=None))
        output_success(result, command, pretty)
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))
