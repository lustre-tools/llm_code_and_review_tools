"""The functions a diff edits, so two changes that merge cleanly but
edit the same function can be flagged: the merge says nothing about
whether the combination builds or behaves (a signature changed on one
side under a forward declaration on the other merged cleanly and did
not build).
"""

from __future__ import annotations

import re


def _lines(text: str) -> list[str]:
    r"""Lines split at \n only, as git numbers them."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


# A definition's or declaration's first line in column 0: `static int
# foo(`, `foo(` under a split return type, `int foo(int a);`, `test_12() {`.
_DEF_RE = re.compile(r"^((?:[A-Za-z_]\w*[ \t*]+)*)\**([A-Za-z_]\w*)[ \t]*\(")
_FILLER_RE = re.compile(
    r"^\s*($|/\*|\*(?:\s|/|$)|//|[{}];?\s*$|#(?!\s*(if|el|endif|define|undef|include)))")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@ ?(.*)$")


def defined_name(line: str, declarations: bool = True) -> str | None:
    """The function a line defines, or with `declarations` also declares
    (a prototype: a forward declaration of a function whose signature the
    other side changes is a clash). None for a statement, an initializer
    (`=`) or a macro call ending in `;` (`EXPORT_SYMBOL(foo);`)."""
    m = _DEF_RE.match(line)
    if not m or "=" in line.split("(", 1)[0]:
        return None
    if line.rstrip().endswith(";"):
        if not declarations or not m[1].strip() or m[2].isupper():
            return None
    return m[2]


def _hunk_names(context: str, lines: list[str]) -> set[str]:
    """The functions one side of a hunk (its removed or its added lines)
    edits: those it defines, and the one around it when it changes
    anything before its first definition. An insertion of a whole
    new function after f does not edit f."""
    names: set[str] = set()
    before_def = False
    for line in lines:
        name = defined_name(line)
        if name:
            names.add(name)
        elif not names and not _FILLER_RE.match(line):
            before_def = True
    if before_def and (outer := defined_name(context, declarations=False)):
        names.add(outer)
    return names


def edited_functions(diff: str) -> dict[str, set[str]]:
    """Functions per path that a `git diff -U0` edits, from each hunk's
    header context line and the definitions in its lines."""
    out: dict[str, set[str]] = {}
    lines = _lines(diff)
    path = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("+++ "):
            new = line[4:]
            path = new[2:] if new.startswith("b/") else path
            continue
        if line.startswith("--- "):
            old = line[4:]
            path = old[2:] if old.startswith("a/") else ""
            continue
        m = _HUNK_RE.match(line)
        if not m or not path:
            continue
        left = int(m[1] if m[1] is not None else 1) + int(m[2] if m[2] is not None else 1)
        removed: list[str] = []
        added: list[str] = []
        while left and i < len(lines):
            body = lines[i]
            i += 1
            if body.startswith("\\"):  # "\ No newline at end of file"
                continue
            (removed if body.startswith("-") else added).append(body[1:])
            left -= 1
        names = _hunk_names(m[3], removed) | _hunk_names(m[3], added)
        if names:
            out.setdefault(path, set()).update(names)
    return out


def shared_functions(ours: dict[str, set[str]], theirs: dict[str, set[str]]) -> list[dict]:
    """[{"file", "functions"}] both sides edit, by file."""
    return [{"file": path, "functions": sorted(both)}
            for path in sorted(set(ours) & set(theirs))
            if (both := ours[path] & theirs[path])]
