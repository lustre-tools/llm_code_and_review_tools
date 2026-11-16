"""Conflict regions of a trial merge and what kind each one is.

Regions come from the blob `git merge-tree` wrote for a conflicted
path, so the merge must have been run with merge.conflictStyle=diff3
inside the scratch object directory (`Repo.merge_tree_diff3`).
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from fnmatch import fnmatch
from typing import Any, Callable

from gerrit_cli.conflicts import Repo

_CONTEXT = 3


def _replaced(base: list[str], other: list[str]) -> set[int]:
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    out: set[int] = set()
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            out.update(range(i1, i2))
    return out


def classify(base: list[str], ours: list[str], theirs: list[str],
             ours_own_added: set[str]) -> str:
    """insert-insert, edit-edit, adjacent or context.

    `ours` is the side already applied; `ours_own_added` the `+` lines
    of that side's own change. Edits of `ours` that are not those lines
    came from the branch moving underneath the change."""
    if not base:
        return "insert-insert"
    if _replaced(base, ours) & _replaced(base, theirs):
        return "edit-edit"
    in_base = set(base)
    added = [line for line in ours if line not in in_base]
    # a pure deletion says nothing about whose it is
    if added and all(line not in ours_own_added for line in added):
        return "context"
    return "adjacent"


def _lines(blob: str) -> list[str]:
    r"""Lines as git numbers them, split at \n only; str.splitlines()
    also splits at \f, \x1c-\x1e, \x85 and U+2028."""
    lines = blob.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _regions(blob: str) -> list[dict[str, Any]]:
    lines = _lines(blob)
    out = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("<<<<<<< "):
            i += 1
            continue
        start = i
        ours: list[str] = []
        base: list[str] = []
        theirs: list[str] = []
        cur = ours
        i += 1
        while i < len(lines) and not lines[i].startswith(">>>>>>> "):
            if lines[i].startswith("||||||| "):
                cur = base
            elif lines[i] == "=======":
                cur = theirs
            else:
                cur.append(lines[i])
            i += 1
        out.append({"start": start, "end": min(i, len(lines) - 1),
                    "ours": ours, "base": base, "theirs": theirs})
        i += 1
    return out


def conflict_regions(repo: Repo, onto: str, commit: str, parent: str,
                     paths: list[str], own_added: dict[str, set[str]],
                     max_lines: int = 80) -> dict[str, list[dict]]:
    """The conflict regions of picking `commit` (on `parent`) onto
    `onto`, per path. A path whose blob is missing or has no markers
    (deleted, binary) maps to []."""
    tree = repo.merge_tree_diff3(onto, commit, parent)
    result: dict[str, list[dict]] = {}
    for path in paths:
        blob = repo.blob(tree, path) if tree else None
        result[path] = []
        if not blob:
            continue
        lines = _lines(blob)
        for reg in _regions(blob):
            lo = max(0, reg["start"] - _CONTEXT)
            hi = min(len(lines), reg["end"] + 1 + _CONTEXT)
            text = lines[lo:hi]
            truncated = len(text) > max_lines
            result[path].append({
                "line": reg["start"] + 1,
                "kind": classify(reg["base"], reg["ours"], reg["theirs"],
                                 own_added.get(path, set())),
                "text": "\n".join(text[:max_lines]),
                "truncated": truncated,
            })
    return result


def _test_keys(text: str) -> list[str]:
    return ([f"test_{m}" for m in re.findall(r"^test_(\w+)[ \t]*\(\)", text, re.M)]
            + [f"run_test {m}" for m in re.findall(r"^\s*run_test\s+(\w+)", text, re.M)])


def _fail_values(text: str) -> list[str]:
    """Each OBD_FAIL value, as a number (0x0143a is 0x143a). Anchored
    at the line start, so a define quoted in a comment is not one."""
    return [f"{int(m, 16):#x}" for m in re.findall(
        r"^[ \t]*#[ \t]*define[ \t]+OBD_FAIL_\w+[ \t]+(0x[0-9a-fA-F]+)", text, re.M)]


# (path glob, text -> keys that must stay unique). fnmatch's `*` crosses `/`.
SILENT_CHECKS: list[tuple[str, Callable[[str], list[str]]]] = [
    ("lustre/tests/*.sh", _test_keys),
    ("*/obd_support.h", _fail_values),
]


def silent_collisions(texts: dict[str, tuple[str, str, str]]) -> list[dict]:
    """Keys the merged text holds more often than either side alone:
    both sides added the same one, though the merge had no conflict.
    `texts` maps path to (target-only, candidate-only, merged)."""
    out: list[dict] = []
    for path, (target, cand, merged) in sorted(texts.items()):
        for glob, keys in SILENT_CHECKS:
            if not fnmatch(path, glob):
                continue
            t, c, m = Counter(keys(target)), Counter(keys(cand)), Counter(keys(merged))
            for key in sorted(m):
                if m[key] > t[key] and m[key] > c[key]:
                    out.append({"check": "duplicate-value" if key.startswith("0x")
                                else "duplicate-test", "file": path, "what": key})
    return out
