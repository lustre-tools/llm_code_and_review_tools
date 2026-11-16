"""The functions a diff edits, so two changes that merge cleanly but
edit the same function can be flagged: the merge says nothing about
whether the combination builds or behaves (a signature changed on one
side under a forward declaration on the other merged cleanly and did
not build).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _lines(text: str) -> list[str]:
    r"""Lines split at \n only, as git numbers them."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


# A definition's or declaration's first line in column 0: `static int
# foo(`, `foo(` under a split return type, `int foo(int a);`, `test_12() {`.
_DEF_RE = re.compile(r"^((?:[A-Za-z_]\w*[ \t*]+)*)\**([A-Za-z_]\w*)[ \t]*\(")
# column-0 words that take a `(` without being a function
_KEYWORDS = frozenset(("if", "while", "for", "until", "switch", "case", "return", "sizeof",
                       "do", "elif", "else", "then", "fi", "done", "select", "function"))
_FILLER_RE = re.compile(
    r"^\s*($|/\*|\*(?:\s|/|$)|//|[{}];?\s*$|#(?!\s*(if|el|endif|define|undef|include)))")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@")
# the line a hunk sits under: git's funcname rule for C (a line starting
# a word in column 0, not a goto label), which also finds `test_N() {`
_OUTER_RE = re.compile(r"^[A-Za-z_$]")
_LABEL_RE = re.compile(r"^[A-Za-z_]\w*:\s*($|/[/*])")


def defined_name(line: str, declarations: bool = True) -> str | None:
    """The function a line defines, or with `declarations` also declares
    (a prototype: a forward declaration of a function whose signature the
    other side changes is a clash). None for a statement, an initializer
    (`=`), a keyword (`if (`, `for ((`) or an upper-case name, which is a
    macro (`EXPORT_SYMBOL(foo);`, `LUSTRE_RW_ATTR(x)`)."""
    m = _DEF_RE.match(line)
    if not m or "=" in line.split("(", 1)[0] or m[2] in _KEYWORDS or m[2].isupper():
        return None
    if line.rstrip().endswith(";") and (not declarations or not m[1].strip()):
        return None
    return m[2]


@dataclass
class Hunk:
    path: str
    old_start: int
    old_count: int
    removed: list[str]
    added: list[str]


def _unquote(path: str) -> str:
    """A path as git prints it, C-quoted when it holds a quote, a
    backslash or a control character."""
    if not path.startswith('"'):
        return path
    raw = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", "replace").decode("utf-8", "replace")


def parse_diff(diff: str) -> list[Hunk]:
    """The hunks of a `git diff -U0` printed with a/ and b/ prefixes. A
    context line (" ", or "" with diff.suppressBlankEmpty) counts
    against both sides and is dropped."""
    out: list[Hunk] = []
    lines = _lines(diff)
    path = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("--- "):
            old = _unquote(line[4:])
            path = old[2:] if old.startswith("a/") else ""
            continue
        if line.startswith("+++ "):
            new = _unquote(line[4:])
            path = new[2:] if new.startswith("b/") else path
            continue
        m = _HUNK_RE.match(line)
        if not m or not path:
            continue
        old_count = int(m[2]) if m[2] is not None else 1
        old_left, new_left = old_count, int(m[3]) if m[3] is not None else 1
        h = Hunk(path, int(m[1]), old_count, [], [])
        while (old_left or new_left) and i < len(lines):
            body = lines[i]
            i += 1
            if body.startswith("\\"):  # "\ No newline at end of file"
                continue
            if body.startswith("-"):
                h.removed.append(body[1:])
                old_left -= 1
            elif body.startswith("+"):
                h.added.append(body[1:])
                new_left -= 1
            else:
                old_left, new_left = old_left - 1, new_left - 1
        out.append(h)
    return out


def outer_name(old: list[str], h: Hunk) -> str | None:
    """The function around a hunk in the old text: the nearest line above
    it (at or above the line an insertion follows) that git's funcname
    rule would pick, when that line defines a function."""
    top = h.old_start - 1 if h.old_count else h.old_start
    for n in range(min(top, len(old)), 0, -1):
        line = old[n - 1]
        if _OUTER_RE.match(line) and not _LABEL_RE.match(line):
            return defined_name(line, declarations=False)
    return None


def _side_names(lines: list[str]) -> tuple[set[str], bool]:
    """(the functions one side of a hunk defines or declares, whether it
    changes anything before its first definition)."""
    names: set[str] = set()
    before_def = False
    for line in lines:
        name = defined_name(line)
        if name:
            names.add(name)
        elif not names and not _FILLER_RE.match(line):
            before_def = True
    return names, before_def


def edited_functions(hunks: list[Hunk], old_text: dict[str, str]) -> dict[str, set[str]]:
    """Functions per path the hunks edit: those a hunk defines or declares
    on either side, and the one around it when it changes anything before
    its first definition, so a whole new function inserted after f does
    not edit f. `old_text` is each path's text before the diff (no entry
    for a new file); the function around a hunk is looked up there rather
    than taken from git's hunk header, which the user's diff drivers and
    attributes decide."""
    out: dict[str, set[str]] = {}
    split: dict[str, list[str]] = {}
    for h in hunks:
        names, before = _side_names(h.removed)
        added, added_before = _side_names(h.added)
        names |= added
        if (before or added_before) and h.path in old_text:
            if h.path not in split:
                split[h.path] = _lines(old_text[h.path])
            if outer := outer_name(split[h.path], h):
                names.add(outer)
        if names:
            out.setdefault(h.path, set()).update(names)
    return out


def shared_functions(ours: dict[str, set[str]], theirs: dict[str, set[str]]) -> list[dict]:
    """[{"file", "functions"}] both sides edit, by file."""
    return [{"file": path, "functions": sorted(both)}
            for path in sorted(set(ours) & set(theirs))
            if (both := ours[path] & theirs[path])]
