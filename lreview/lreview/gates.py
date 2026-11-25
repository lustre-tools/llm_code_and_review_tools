"""Facts about the version gates a commit adds to the tests.

A reviewer left to compare version numbers argues that a gate at the
current tag lets in builds without the fix and asks for the next one,
which would skip the test on the patch's own build.  So lreview compares
them: with the version of the tree, with the current version of the
branch the patch goes to, and with the branch another number is for.

Only remotes of the change's own project are looked at, and upstream's
for a port: the review of a public change must not know other branches.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import worktree as wt
from .backport import (MIN_SUBJECT_MATCH, UPSTREAM_BRANCH,
                       UPSTREAM_PROJECT, subject_tail)

VERSION_GATES_PATH = Path(__file__).resolve().parent / "version-gates.md"
TEST_DIR = "lustre/tests"
# Landing moves a patch to the next tag anyway.
NOTE_TAGS = 1
MAX_GATES = 20

_GATE_RE = re.compile(r"version_code\s+(v?\d[\w.\-]*)")
_HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)")
# 2.17.62, v2_17_62, 2.16.0-xyz62; no -RC1 and the like
_TAG_RE = re.compile(r"^v?\d+([._]\d+){2,3}$|^\d+\.\d+\.\d+-[a-z]{2,}\d+$")
_BRANCH_RE = re.compile(r"^(master|b\w+)$")


@dataclass(frozen=True)
class Gate:
    path: str
    line: int
    version: str
    text: str


def version_code(text: str) -> Optional[tuple]:
    """The four numbers test-framework.sh's version_code() keeps."""
    numbers = [int(n) for n in re.findall(r"\d+", text)][:4]
    return tuple(numbers + [0] * (4 - len(numbers))) if numbers else None


def added_gates(repo: Path, sha: str) -> list[Gate]:
    """The version_code comparisons on lines the commit adds to tests."""
    diff = _git(repo, "show", "--format=", "-U0", "--no-color", sha, "--",
                TEST_DIR)
    gates, path, line = [], "", 0
    # moved or re-indented lines add nothing
    old = {text[1:].strip() for text in diff.splitlines()
           if text.startswith("-") and not text.startswith("--- ")}
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text[6:]
        elif text.startswith("@@"):
            match = _HUNK_RE.match(text)
            line = int(match.group(1)) if match else 0
        elif text.startswith("+"):
            found = [] if text[1:].strip() in old else _GATE_RE.findall(text)
            for version in dict.fromkeys(found):
                gates.append(Gate(path, line, version, text[1:].strip()))
            line += 1
    return gates


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args],
                                capture_output=True, text=True,
                                errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def branch_tags(repo: Path, ref: str) -> dict:
    """{version code: tag} of the version tags reachable from ref."""
    tags: dict = {}
    for tag in _git(repo, "tag", "--merged", ref).split():
        if _TAG_RE.match(tag):
            code = version_code(tag)
            # 2.17.62 rather than v2_17_62
            if code not in tags or "." in tag:
                tags[code] = tag
    return tags


def tree_version(repo: Path, ref: str) -> Optional[tuple[tuple, str]]:
    """The version a build of ref reports, and the tag it is from.

    The commits since a three-number tag are its fourth number
    (2.17.62-35-g0123abc is 2.17.62.35); one that has four keeps them.
    """
    text = _git(repo, "describe", "--tags", "--match", "[0-9]*",
                "--exclude", "*-RC*", "--exclude", "*-rc*", ref).strip()
    if not text:
        return None
    name = re.sub(r"-(\d+)-g[0-9a-f]+$", "", text)
    commits = re.search(r"-(\d+)-g[0-9a-f]+$", text)
    if not _TAG_RE.match(name):
        return None
    code = list(version_code(name))
    if commits and len(re.findall(r"\d+", name)) == 3:
        code[3] = int(commits.group(1))
    return tuple(code), name


def same_line(a: tuple, b: tuple, tag: str) -> bool:
    """Whether version a is of the release line of b, whose tag is tag.

    2.16.0-x61 and 2.16.0-x62; the releases 2.15.1 and 2.15.8; any two
    development versions (x.y.50 and up), which are all on master.
    """
    if "-" in tag:
        return a[:3] == b[:3]
    if a[2] >= 50 and b[2] >= 50:
        return True
    return a[:2] == b[:2] and (a[2] >= 50) == (b[2] >= 50)


def remote_names(repo: Path, base_url: str, project: str) -> list[str]:
    """The repository's remotes for a Gerrit project."""
    urls = set(wt.gerrit_remote_urls(repo, base_url, project))
    names = []
    for line in _git(repo, "config", "--get-regexp",
                     r"^remote\..*\.url$").splitlines():
        key, _, url = line.partition(" ")
        if url.strip() in urls:
            names.append(key[len("remote."):-len(".url")])
    return names


def refresh(repo: Path, remote: str, branch: str) -> None:
    """Fetch a branch and its tags; what is there already will do."""
    with wt._GIT_LOCK:
        try:
            subprocess.run(
                ["git", "-C", str(repo), "fetch", "-q", remote, branch],
                capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                     "GIT_SSH_COMMAND": os.environ.get(
                         "GIT_SSH_COMMAND", "ssh -o BatchMode=yes")})
        except (OSError, subprocess.TimeoutExpired):
            pass


def _refs(repo: Path, change) -> list[tuple[str, str, str]]:
    """(project, remote, branch) the review may look at."""
    project = getattr(change, "project", None)
    base_url = getattr(change, "base_url", None)
    if not project or not base_url:
        return []
    upstream = []
    if project != UPSTREAM_PROJECT:
        upstream = [(UPSTREAM_PROJECT, remote, UPSTREAM_BRANCH) for remote in
                    remote_names(repo, base_url, UPSTREAM_PROJECT)]
    refs = []
    for remote in remote_names(repo, base_url, project):
        for name in _git(repo, "for-each-ref", "--format=%(refname:lstrip=3)",
                         f"refs/remotes/{remote}").split():
            # another project's master is a copy, often an old one
            if _BRANCH_RE.match(name) and not (
                    upstream and name == UPSTREAM_BRANCH):
                refs.append((project, remote, name))
    return refs + upstream


def _find_patch(repo: Path, ref: str, change_ids: set,
                subjects: set) -> Optional[str]:
    """The patch's commit on ref, by Change-Id or subject."""
    found = None
    for change_id in sorted(change_ids):
        found = _git(repo, "log", "-n1", "--format=%H", "-E",
                     f"--grep=^Change-Id: {change_id}", ref).strip()
        if found:
            break
    if not found:
        for subject in sorted(subjects):
            for line in _git(repo, "log", "-n20", "--format=%H %s", "-F",
                             f"--grep={subject}", ref).splitlines():
                sha, _, text = line.partition(" ")
                if subject_tail(text) == subject:
                    found = sha
                    break
            if found:
                break
    if found and not _git(repo, "log", "-n1", "--format=%h", "-F",
                          f"--grep=This reverts commit {found}",
                          f"{found}..{ref}").strip():
        return found
    return None


def _find_open(change, project: str, branch: str, change_ids: set,
               subjects: set) -> Optional[int]:
    """The open change of the patch on a branch."""
    from gerrit_cli.client import GerritCommentsClient

    client = GerritCommentsClient(url=change.base_url)
    scope = f"project:{project} branch:{branch} status:open"
    for change_id in sorted(change_ids):
        for hit in client.search_changes(f"{scope} change:{change_id}",
                                         limit=1, options=[]):
            return hit["_number"]
    for subject in sorted(subjects):
        if '"' in subject:
            continue
        for hit in client.search_changes(f'{scope} message:"{subject}"',
                                         limit=10, options=[]):
            if subject_tail(hit.get("subject", "")) == subject:
                return hit["_number"]
    return None


def _own_fact(code: tuple, tree: tuple, tip: Optional[tuple]) -> str:
    """A gate for the branch the patch is on.

    tree: (version, tag); tip: (branch, version, tag, tags).
    """
    tree_code, tree_tag = tree
    shown = _show(*tree)
    if code > tree_code:
        return f"for this branch, ABOVE the tree's version {shown}"
    where = "at" if code == tree_code else "below"
    text = f"for this branch, {where} the tree's version {shown}: fine"
    tags = tip[3] if tip else {}
    limit = tip[1] if tip else tree_code
    behind = [c for c in tags if code < c <= limit]
    if len(behind) <= NOTE_TAGS:
        return text
    branch, _tip_code, tip_tag, _ = tip
    old_base = [c for c in behind if c > tree_code]
    why = []
    if old_base:
        why.append(f"the patch has an old base ({tree_tag})")
    if len(behind) > len(old_base):
        why.append(f"the gate is older than its base ({tree_tag})")
    return (f"{text}.  NOTE: {branch} is at {tip_tag}, {len(behind)} tags "
            f"past the gate: " + " and ".join(why))


def _show(code: tuple, tag: str) -> str:
    """A version as a gate would write it."""
    if "-" in tag or not code[3]:
        return tag
    return ".".join(str(n) for n in code)


def _other_fact(repo: Path, change, code: tuple, other: tuple,
                change_ids: set, subjects: set) -> str:
    """A gate for another branch: (project, branch, ref, version, tag)."""
    project, branch, ref, tip_code, tip_tag = other
    text = f"for {branch}, now at {_show(tip_code, tip_tag)}"
    sha = _find_patch(repo, ref, change_ids, subjects)
    if sha is None:
        if code > tip_code:
            return f"{text}: not there yet, and the gate keeps it out"
        try:
            number = _find_open(change, project, branch, change_ids, subjects)
        except Exception:  # noqa: BLE001 - a hint, not worth the review
            return f"{text}: not there, and whether it is open is unknown"
        if number:
            return (f"{text}: not there yet, but open as change {number}.  "
                    f"NOTE: until that lands, {branch} passes the gate "
                    f"without the patch")
        return (f"{text}: MISSING there (no commit and no open change with "
                f"its Change-Id or subject), and {branch} passes the gate")
    landed = tree_version(repo, sha)
    text += f": the patch is there ({sha[:11]}"
    text += f", landed at {_show(*landed)})" if landed else ")"
    if code > tip_code:
        return (f"{text}.  NOTE: the gate is above {branch}, which does not "
                f"run the test yet")
    if landed is None:
        return text
    low, high = sorted((code, landed[0]))
    drift = [c for c in branch_tags(repo, ref) if low < c <= high]
    if len(drift) > NOTE_TAGS:
        return (f"{text}.  NOTE: the gate is {len(drift)} tags "
                + ("above" if code > landed[0] else "below")
                + " where it landed")
    return f"{text}: fine"


def gate_facts(repo: Path, change, upstreams=None) -> str:
    """The review prompt's section on version gates, "" without any."""
    gates = added_gates(repo, change.sha)
    if not gates:
        return ""
    tree = tree_version(repo, change.sha)
    if tree is None:
        return ""
    refs = _refs(repo, change)
    target = getattr(change, "branch", None)

    tip = None
    for project, remote, branch in refs:
        if branch == target and project == change.project:
            refresh(repo, remote, branch)
            ref = f"refs/remotes/{remote}/{branch}"
            found = tree_version(repo, ref)
            if found and same_line(tree[0], *found):
                tip = (branch, *found, branch_tags(repo, ref))
            break

    change_ids = {i for i in [getattr(change, "change_id", None)] if i}
    subjects = {subject_tail(wt.commit_subject(repo, change.sha))}
    for upstream in upstreams or []:
        if upstream.change_id:
            change_ids.add(upstream.change_id)
        if upstream.subject:
            subjects.add(subject_tail(upstream.subject))
    subjects = {s for s in subjects if len(s) >= MIN_SUBJECT_MATCH}

    def other_branch(code: tuple) -> Optional[tuple]:
        """The branch whose release line a version is of: bN before
        bN_hotfix and other branches off it."""
        for project, remote, branch in sorted(refs, key=lambda r: len(r[2])):
            if (project, branch) == (change.project, target):
                continue
            ref = f"refs/remotes/{remote}/{branch}"
            if ref not in versions:
                versions[ref] = tree_version(repo, ref)
            if versions[ref] and same_line(code, *versions[ref]):
                refresh(repo, remote, branch)
                return (project, branch, ref,
                        *(tree_version(repo, ref) or versions[ref]))
        return None

    versions: dict = {}
    known: dict = {}
    lines = ["Version gates this commit adds (facts computed by lreview):"]
    for gate in gates[:MAX_GATES]:
        code = version_code(gate.version)
        if code not in known:
            other = None
            if same_line(code, *tree):
                known[code] = _own_fact(code, tree, tip)
            else:
                other = other_branch(code)
                known[code] = (
                    _other_fact(repo, change, code, other, change_ids,
                                subjects) if other
                    else "for another release, not checked: nothing to report")
        lines.append(f"- {gate.path}:{gate.line} {gate.version}: "
                     f"{known[code]}")
    if len(gates) > MAX_GATES:
        lines.append(f"- and {len(gates) - MAX_GATES} more")
    return "\n".join(lines) + "\n\n" + VERSION_GATES_PATH.read_text()
