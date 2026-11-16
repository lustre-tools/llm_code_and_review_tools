"""The JSON `gc conflicts` prints: the engine's result for the focus
stack and the selected candidates, sorted into what the reader acts on.
`report` builds the full output (--full); `compact` cuts it down to
what deciding needs, the default.

Nothing here talks to Gerrit or runs git; the command gathers every
input first (commands/conflicts.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .conflict_candidates import Selection, fix_side, readiness, release_rank
from .conflicts import Patch, own_stack

TIER_ORDER = {"frozen": 0, "lands-first": 1, "same-effort": 2, "other": 3}


def owner_name(change: dict[str, Any]) -> str:
    owner = change.get("owner") or {}
    return owner.get("name") or owner.get("username") or ""


def target_tier(change: dict[str, Any], queued_ids: set[str]) -> str:
    """A target's own tier (the fix_side row)."""
    if change.get("change_id") in queued_ids:
        return "frozen"
    if readiness(change) == "ready" or release_rank(change.get("hashtags") or []) > 0:
        return "lands-first"
    return "pending"


@dataclass
class Checked:
    """The target as tried, and everything the engine said about it."""
    target: dict[str, Any]
    cn: int
    ps: int
    uploaded: str  # the target's current patchset commit
    stack: list[Patch]  # bottom first, the target (or its local commit) last
    children: list[int]
    queued_ids: set[str]
    result: dict[str, Any]  # check_conflicts in focus mode
    on_branch: dict[str, Any]  # the target's own result on the branch
    outdated: dict[str, Any] | None  # its parent's newer patchset
    old: dict[int, dict[str, int]]  # the other stack members on an older parent
    current_ps: dict[int, int]  # change -> its current patchset
    base: dict[str, Any]  # base_info
    hunks: bool | set[int]

    @property
    def landed(self) -> bool:
        return self.on_branch["status"] == "clean"


def on_next(result: dict[str, Any], cn: int, next_branch: str) -> dict[str, Any]:
    nxt = result.get("next")
    if nxt is None:
        return {"status": "no-next", "branch": next_branch}
    if "skipped" in nxt:
        return {"status": "stale", "branch": next_branch, "reason": nxt["skipped"]}
    return nxt["results"].get(str(cn)) or {"status": "not-tried"}


def pair_entry(pair: dict[str, Any], sel: Selection, landed: bool, t_tier: str,
               t_rank: int) -> dict[str, Any]:
    """One tried candidate, as listed under conflicts / as_uploaded_only."""
    n = pair["other"]
    c = sel.every[n]
    e: dict[str, Any] = {
        "cn": n, "subject": c.get("subject", ""), "owner": owner_name(c),
        "tier": sel.tiers[n], "readiness": readiness(c),
        "hashtags": sorted(c.get("hashtags") or []),
        "fix_side": fix_side(t_tier, t_rank, sel.tiers[n], release_rank(c.get("hashtags") or [])),
        "files": sorted(set(pair["landing"]["files"]) | set(pair["as_uploaded"]["files"])),
    }
    if landed:
        e["landing"] = {k: pair["landing"][k] for k in ("target_first", "candidate_first")}
    e["as_uploaded"] = {k: pair["as_uploaded"][k]
                        for k in ("target_on_theirs", "theirs_on_target")}
    e["via"] = pair["via"]
    e["silent"] = pair["silent"]
    if pair.get("base_drift"):
        e["base_drift"] = pair["base_drift"]
    if pair.get("same_function"):
        e["same_function"] = pair["same_function"]
    if "hunks" in pair:
        e["hunks"] = pair["hunks"] if landed else \
            {k: v for k, v in pair["hunks"].items() if k != "landing"}
    if n in sel.forced:
        e["forced"] = True
    return e


def _sorted_by_tier(entries: list[dict[str, Any]], sel: Selection) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda e: (
        TIER_ORDER[e["tier"]], -release_rank(sel.every[e["cn"]].get("hashtags") or []), e["cn"]))


@dataclass
class _Buckets:
    conflicts: list[dict[str, Any]]
    uploaded_only: list[dict[str, Any]]
    silent_only: list[dict[str, Any]]
    watch: list[dict[str, Any]]  # clean merge, a function edited on both sides
    clean: list[int]
    errors: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


def _buckets(chk: Checked, sel: Selection, t_tier: str, t_rank: int) -> _Buckets:
    """Each tried candidate under the one heading that says what to do
    about it; the ones the engine could not try under errors/skipped."""
    res = chk.result["results"]
    b = _Buckets([], [], [], [], [], [], list(sel.skipped))
    b.errors = [{"cn": n, "reason": res.get(str(n), {}).get("reason", "")}
                for n in sel.kept if res.get(str(n), {}).get("status") == "error"]
    b.skipped += [{"cn": n, "reason": res[str(n)].get("reason", "")}
                  for n in sel.kept if res.get(str(n), {}).get("status") == "skipped"]
    b.skipped.sort(key=lambda s: s["cn"])
    for pair in chk.result["focus_pairs"]:
        e = pair_entry(pair, sel, chk.landed, t_tier, t_rank)
        statuses = [*e.get("landing", {}).values(), *e["as_uploaded"].values()]
        if "conflict" in statuses:
            (b.conflicts if chk.landed else b.uploaded_only).append(e)
        elif pair["silent"]:
            b.silent_only.append({k: e[k] for k in ("cn", "tier", "fix_side", "silent",
                                                    "same_function") if k in e})
        elif "error" in statuses:
            b.errors.append({"cn": e["cn"], "reason": "trial merge failed"})
        elif "same_function" in e:
            b.watch.append(e)
        else:
            b.clean.append(e["cn"])
    b.conflicts = _sorted_by_tier(b.conflicts, sel)
    b.uploaded_only = _sorted_by_tier(b.uploaded_only, sel)
    b.silent_only = _sorted_by_tier(b.silent_only, sel)
    b.watch = _sorted_by_tier(b.watch, sel)
    return b


def _rebase_info(chk: Checked) -> dict[str, Any]:
    """Why a target that does not apply needs a rebase, and onto what:
    a blocked target's `by` is the stack member that does not apply."""
    res = chk.result["results"]
    stuck, seen = chk.cn, set()
    while res.get(str(stuck), {}).get("status") == "blocked" and stuck not in seen:
        seen.add(stuck)
        stuck = res[str(stuck)].get("by", stuck)
    parent = res.get(str(stuck), {}).get("parent")
    out: dict[str, Any] = {"rebase_cause": "parent" if parent else "branch"}
    if parent:
        onto = {"cn": parent["cn"]}
        if (cur := chk.current_ps.get(parent["cn"])) is not None:
            onto["ps"] = cur
        if stuck != chk.cn:
            onto["for"] = stuck
        out["rebase_onto"] = onto
    return out


def report(chk: Checked, sel: Selection, next_branch: str, me: str | None) -> dict[str, Any]:
    target, cn, landed = chk.target, chk.cn, chk.landed
    hashtags = target.get("hashtags") or []
    t_tier, t_rank = target_tier(target, chk.queued_ids), release_rank(hashtags)
    b = _buckets(chk, sel, t_tier, t_rank)
    tried = {pair["other"] for pair in chk.result["focus_pairs"]}
    notes = [f"hunks-for {n}: not a tried candidate"
             for n in sorted(chk.hunks - tried)] if isinstance(chk.hunks, set) else []
    focus_stack = [p.cn for p in chk.stack]

    out: dict[str, Any] = {
        "change": cn,
        "patchset": chk.ps,
        "commit": chk.uploaded,
        "tested_commit": chk.stack[-1].commit,
        "subject": target.get("subject", ""),
        "owner": owner_name(target),
        "me": me,
        "readiness": readiness(target),
        "tier_of_target": t_tier,
        "release_hashtags": sorted(h for h in hashtags if release_rank([h]) > 0),
        "project": target["project"],
        "branch": target["branch"],
        "tip": chk.result["tip"],
        "status": "ok" if landed else "needs-rebase",
    }
    if not landed:
        out.update(_rebase_info(chk))
    if chk.outdated is not None:
        out["outdated_parent"] = chk.outdated
    out.update({
        "base": chk.base,
        "on_branch": chk.on_branch,
        "on_next": on_next(chk.result, cn, next_branch),
        "own_stack": own_stack(chk.result, focus_stack, chk.children, chk.old),
        "conflicts": b.conflicts if landed else None,
    })
    if not landed:
        out["as_uploaded_only"] = b.uploaded_only
    out.update({
        "silent_only": b.silent_only,
        "watch": b.watch,
        "already_conflicting": sorted(chk.result["already_conflicting"]),
        "not_tried": {"other": sel.not_tried},
        "clean": b.clean,
        "skipped": b.skipped,
        "errors": b.errors,
        "counts": {
            "candidates": len(sel.found),
            "after_filters": len(sel.kept) + len(sel.not_tried),
            "tried": len(chk.result["focus_pairs"]),
            "conflicting": len(b.conflicts) + len(b.silent_only) + len(b.uploaded_only),
            "watch": len(b.watch),
        },
    })
    if notes:
        out["notes"] = notes
    return out


SUBJECT_MAX = 70
ORDERS = {"landing": ("target_first", "candidate_first"),
          "as_uploaded": ("target_on_theirs", "theirs_on_target")}
# full-only top-level keys: hashes, and what branch already says
_FULL_ONLY = ("commit", "tested_commit", "project", "tip")
_COUNTED = ("already_conflicting", "clean")
_ENTRY_KEYS = ("cn", "subject", "owner", "tier", "readiness", "fix_side", "files")


def _short(subject: str) -> str:
    return subject if len(subject) <= SUBJECT_MAX else subject[:SUBJECT_MAX - 3] + "..."


def compact_entry(e: dict[str, Any], same_function: bool = True) -> dict[str, Any]:
    """One pair of the full output, its per-order results joined in
    ORDERS order and only the fields that say something. A pair that
    conflicts anyway drops same_function (`same_function` False)."""
    out = {k: e[k] for k in _ENTRY_KEYS if k in e and e[k] != []}
    if "subject" in out:
        out["subject"] = _short(out["subject"])
    release = [h for h in e.get("hashtags") or [] if release_rank([h]) > 0]
    if release:
        out["release_hashtags"] = release
    for model, orders in ORDERS.items():
        if model in e:
            out[model] = "/".join(e[model][o] for o in orders)
    if e.get("via") is not None:
        out["via"] = e["via"]
    for key in ("silent", "same_function", "forced", "hunks"):
        if e.get(key) and (key != "same_function" or same_function):
            out[key] = e[key]
    if e.get("base_drift"):
        out["base_drift"] = len(e["base_drift"])
    return out


def compact(full: dict[str, Any]) -> dict[str, Any]:
    """The default output: `report`'s with the change lists that are
    only counted (clean, already_conflicting, not_tried) as numbers,
    each pair through compact_entry, and no commit hashes."""
    out: dict[str, Any] = {}
    for key, value in full.items():
        if key in _FULL_ONLY:
            continue
        if key in _COUNTED:
            out[key] = len(value)
        elif key == "not_tried":
            out[key] = sum(len(v) for v in value.values())
        elif key in ("conflicts", "as_uploaded_only", "silent_only", "watch") \
                and value is not None:
            out[key] = [compact_entry(e, key in ("silent_only", "watch")) for e in value]
        else:
            out[key] = value
    if full.get("tested_commit") != full.get("commit"):
        out["tested_commit"] = full["tested_commit"]
    out["orders"] = {m: "/".join(o) for m, o in ORDERS.items()}
    out["full"] = ("--full adds commit hashes, every hashtag, base_drift files "
                   "and the changes behind clean, already_conflicting and not_tried")
    return out
