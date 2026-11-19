"""Human-readable report of `gc conflicts` (--human).

Rendered only from the compact result dict that the JSON output prints,
so the two cannot disagree.  Pairs that collide only through an
ancestor (`via`) are grouped under that ancestor, the change whose lines
actually conflict.
"""
from __future__ import annotations

import os
from typing import Any, TextIO

_CODES = {"red": "31", "green": "32", "yellow": "33", "magenta": "35",
          "bold": "1", "dim": "2"}

_READINESS = {"ready": "ready", "pending": "not ready", "cr-1": "has a Code-Review -1"}


def use_color(stream: TextIO) -> bool:
    """Colour only on a terminal, and never with NO_COLOR set."""
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


class _Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        codes = ";".join(_CODES[s] for s in styles)
        return f"\x1b[{codes}m{text}\x1b[0m"


def group_by_root(entries: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Each change whose own lines collide, with the entries that collide
    only because they carry it (`via`).  A `via` change that is not itself
    an entry is built from `via_change`."""
    roots: dict[int, dict[str, Any]] = {}
    children: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for e in entries:
        key = e.get("via") or e["cn"]
        if key not in children:
            children[key] = []
            order.append(key)
        if e.get("via"):
            children[key].append(e)
            if key not in roots:
                roots[key] = {**(e.get("via_change") or {"cn": key}), "synthesized": True}
        else:
            roots[key] = e
    return [(roots[k], children[k]) for k in order]


def _tier(tier: str | None, branch: str) -> str:
    return {"frozen": f"in {branch}-next", "lands-first": "ready to land",
            "same-effort": "same effort (shared hashtag or topic)",
            "other": "not close to landing"}.get(tier or "", tier or "?")


def _tags(e: dict[str, Any]) -> str:
    tags = e.get("release_hashtags") or []
    return f" [{', '.join(tags)}]" if tags else ""


def _landing(landing: str | None, cn: int) -> str:
    return {"conflict/conflict": "conflicts in both landing orders",
            "conflict/clean": "conflicts only if this change lands first",
            "clean/conflict": f"conflicts only if {cn} lands first",
            "clean/clean": "merges cleanly in both orders"}.get(landing or "", landing or "?")


def _who_moves(e: dict[str, Any], cn: int, target: int) -> str:
    side = e.get("fix_side")
    if e.get("own_via"):
        parent = e["own_via"]
        if side == "target":
            return f"this change's parent {parent}"
    if side == "target":
        return f"this change ({target})"
    if side == "candidate":
        return f"{cn} ({e.get('owner', '?')})"
    if side == "coordinate":
        return "either side: agree on an order"
    return side or "?"


def _join_by_tier(entries: list[dict[str, Any]], branch: str) -> str:
    parts: list[str] = []
    cns: list[str] = []
    tier = None
    for e in entries:
        if cns and e.get("tier") != tier:
            parts.append(f"{', '.join(cns)} ({_tier(tier, branch)})")
            cns = []
        tier = e.get("tier")
        cns.append(str(e["cn"]))
    if cns:
        parts.append(f"{', '.join(cns)} ({_tier(tier, branch)})")
    return ", ".join(parts)


def _hunk_lines(e: dict[str, Any], cn: int, target: int, s: _Style) -> list[str]:
    """The conflict regions, markers labelled with the change numbers
    instead of commit hashes (the change landed first is "ours")."""
    out: list[str] = []
    for path, hunks in ((e.get("hunks") or {}).get("landing") or {}).items():
        for h in hunks:
            target_first = h.get("order") == "target_first"
            first = "this change" if target_first else str(cn)
            ours, theirs = ((f"{target} (this change)", str(cn)) if target_first
                            else (str(cn), f"{target} (this change)"))
            labels = {"<<<<<<<": ours, "|||||||": "base", ">>>>>>>": theirs}
            head = f"    {path}:{h.get('line', '?')}  {h.get('kind', '?')}, {first} lands first"
            if h.get("truncated"):
                head += " (truncated)"
            out.append(s(head, "bold"))
            for line in (h.get("text") or "").splitlines():
                mark = line[:7]
                if mark in labels and (len(line) == 7 or line[7] == " "):
                    line = f"{mark} {labels[mark]}"
                if mark in labels or line == "=======":
                    line = s(line, "magenta")
                out.append("      " + line)
    return out


def _header(r: dict[str, Any], s: _Style) -> list[str]:
    cn, branch = r.get("change"), r.get("branch", "master")
    out = [s(f"{cn} PS{r.get('patchset', '?')}", "bold") + f"  {r.get('subject', '')}"]
    who = [r.get("owner") or "?", _READINESS.get(r.get("readiness"), r.get("readiness") or "?")]
    out.append(" · ".join(who) + _tags(r))
    base = r.get("base") or {}
    if base:
        behind = base.get("behind")
        line = f"base: {behind} behind {branch}" if behind else f"base: on the {branch} tip"
        if base.get("rebase"):
            line += f", rebase {base['rebase']}"
        out.append(s(line, "dim"))
    ob = r.get("on_branch") or {}
    if ob.get("status") == "clean":
        out.append(f"{branch}: " + s("lands cleanly", "green"))
    elif ob:
        detail = f" (blocked by {ob['by']})" if ob.get("by") else ""
        out.append(f"{branch}: " + s(f"{ob.get('status', '?').upper()}{detail}", "red"))
    nx = r.get("on_next") or {}
    if nx.get("status") == "clean":
        out.append(f"{branch}-next: " + s("lands cleanly", "green"))
    elif nx.get("status") == "conflict":
        w = nx.get("with") or {}
        out.append(f"{branch}-next: " + s(f"CONFLICTS with {w.get('cn', '?')}", "red")
                   + (f"  {w['subject']}" if w.get("subject") else ""))
    elif nx:
        out.append(s(f"{branch}-next: not checked ({nx.get('status')})", "dim"))
    return out


def _own_stack(r: dict[str, Any], s: _Style) -> list[str]:
    out: list[str] = []
    if r.get("status") == "needs-rebase":
        onto = r.get("rebase_onto")
        if r.get("rebase_cause") == "parent" and onto:
            line = f"needs a rebase onto {onto['cn']}"
            if onto.get("ps") is not None:
                line += f" PS{onto['ps']}"
            if onto.get("for"):
                line += f" (for {onto['for']})"
            out.append(s(line + ": it does not apply on its parent's current patchset", "red"))
        else:
            out.append(s(f"needs a rebase onto {r.get('branch', 'the branch')}: "
                         "it does not apply on the tip", "red"))
    op = r.get("outdated_parent")
    if op:
        if op.get("status") == "clean":
            out.append(s(f"on an older patchset of parent {op.get('cn')}, "
                         "but lands with the current one: leave it", "dim"))
        else:
            out.append(s(f"on an older patchset of parent {op.get('cn')}, "
                         "and conflicts with the current one", "red"))
    stack = r.get("own_stack") or {}
    for kind in ("parents", "children"):
        for m in stack.get(kind) or []:
            if m.get("status") == "clean":
                continue
            line = f"{kind[:-1] if kind == 'parents' else 'child'} {m.get('cn')}: {m.get('status')}"
            old = m.get("on_old_patchset")
            if old:
                line += f" (on PS{old.get('ps')} of {old.get('of')}, current {old.get('current')})"
            out.append(s(line, "yellow"))
    return ["", s("OWN STACK", "bold"), *("  " + x for x in out)] if out else []


def _collisions(r: dict[str, Any], s: _Style) -> list[str]:
    entries = r.get("conflicts")
    if not entries:
        return []
    target, branch = r.get("change"), r.get("branch", "master")
    groups = group_by_root(entries)
    out = ["", s(f"COLLISIONS  {len(groups)} root patch{'es' if len(groups) != 1 else ''}"
                 f" ({len(entries)} pair{'s' if len(entries) != 1 else ''})", "bold")]
    for root, kids in groups:
        cn = root["cn"]
        ref = kids[0] if root.get("synthesized") else root
        subject = root.get("subject") or "(collides only through the changes stacked on it)"
        out.append(s("✗ ", "red") + s(str(cn), "bold", "red") + f"  {subject}"
                   + s(f"  {root.get('owner', '?')}", "dim"))
        out.append(f"    {_tier(root.get('tier'), branch)}{_tags(root)} · "
                   f"{_landing(ref.get('landing'), cn)}")
        files = ref.get("files") if not root.get("synthesized") else sorted(
            {f for k in kids for f in k.get("files") or []})
        if files:
            out.append(f"    files: {', '.join(files)}")
        if root.get("own_via"):
            oc = root.get("own_via_change") or {}
            out.append(f"    in this change's parent {root['own_via']} ({oc.get('owner', '?')})")
        out.append(f"    who moves: {_who_moves(ref, cn, target)}")
        if kids:
            out.append(s(f"    stacked on it, same conflict: {_join_by_tier(kids, branch)}", "dim"))
        out.extend(_hunk_lines(root, cn, target, s))
    return out


def _silent(r: dict[str, Any], s: _Style) -> list[str]:
    entries = r.get("silent_only") or []
    if not entries:
        return []
    target, branch = r.get("change"), r.get("branch", "master")
    out = ["", s("SILENT  merge cleanly but define the same thing twice", "bold")]
    for e in entries:
        out.append(s("! ", "yellow") + s(str(e["cn"]), "bold") + f"  {e.get('subject', '')}"
                   + s(f"  {e.get('owner', '?')}", "dim"))
        out.append(f"    {_tier(e.get('tier'), branch)}{_tags(e)}")
        for x in e.get("silent") or []:
            out.append(f"    {x.get('check')} in {x.get('file')}: {x.get('what')}")
        out.append(f"    who moves: {_who_moves(e, e['cn'], target)}")
    return out


def _watch(r: dict[str, Any], s: _Style) -> list[str]:
    entries = r.get("watch") or []
    if not entries:
        return []
    branch = r.get("branch", "master")
    out = ["", s("WATCH  merge cleanly but edit the same function", "bold")]
    for e in entries:
        out.append(s("~ ", "yellow") + s(str(e["cn"]), "bold") + f"  {e.get('subject', '')}"
                   + s(f"  {e.get('owner', '?')} · {_tier(e.get('tier'), branch)}{_tags(e)}", "dim"))
        for sf in e.get("same_function") or []:
            out.append(f"    {', '.join(sf.get('functions') or [])} in {sf.get('file')}")
    return out


def _counts(r: dict[str, Any], s: _Style) -> list[str]:
    out: list[str] = []
    errors = r.get("errors") or []
    if errors:
        out += ["", s("NOT TRIED (not clean either)", "bold")]
        out += [f"  {e.get('cn')}: {e.get('reason', '')}" for e in errors]

    def n(key: str) -> int:
        v = r.get(key)
        return v if isinstance(v, int) else len(v or [])

    out += ["", s(f"{n('clean')} clean · {n('already_conflicting')} already conflicting · "
                  f"{n('not_tried')} not tried · {n('skipped')} skipped", "dim")]
    out += [s(f"note: {x}", "dim") for x in r.get("notes") or []]
    return out


def render(result: dict[str, Any], color: bool) -> str:
    """The --human report for one `gc conflicts` result (compact form)."""
    s = _Style(color)
    lines = _header(result, s) + _own_stack(result, s)
    body = _collisions(result, s) + _silent(result, s) + _watch(result, s)
    if result.get("status") == "ok" and not result.get("conflicts") \
            and not result.get("silent_only"):
        body = ["", s("no collisions", "green")] + body
    lines += body + _counts(result, s)
    roots = group_by_root(result.get("conflicts") or [])
    if roots and not any(e.get("hunks") for e in result.get("conflicts") or []):
        lines.append(s(f"next: gerrit conflicts {result.get('change')} "
                       f"--hunks-for {roots[0][0]['cn']}", "dim"))
    return "\n".join(lines)
