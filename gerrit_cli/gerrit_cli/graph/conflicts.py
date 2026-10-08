"""Trial merges of a graph's in-flight changes (`gc graph --conflicts REPO`).

A change lands the way Gerrit's cherry-pick submit lands it: its own diff
(parent commit -> commit) picked onto the branch after the in-flight
changes it stands on. Every pick is `git merge-tree --merge-base=<parent>
<onto> <commit>`, the ort merge `git cherry-pick` runs, done in memory.

1. Against the branch tip: each change is picked onto the result of the
   changes below it, so one pass says for every change whether its stack
   applies, where it stops applying (conflict) and what sits above that
   (blocked).
2. Against each other: for two changes neither of which stands on the
   other, one stack is applied and the other's own part picked on top.
   Only pairs whose own diffs share a file are tried: a conflict on a
   file needs both sides to change it, and the change lower in a stack
   that changes it is the one that collides. A pair is reported only
   for the files no change below it on its side already collides on, so
   a conflict shows once, between the two changes that cause it.

Merge results are written to a private object directory that is deleted
afterwards; the repository only gains the fetched branch and changes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
# merge-tree --write-tree with --merge-base
_MIN_GIT = (2, 40)
_FETCH_BATCH = 50
# The throwaway commits get fixed ids and need no user.name/email.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "gc graph", "GIT_AUTHOR_EMAIL": "gc-graph@invalid",
    "GIT_AUTHOR_DATE": "@0 +0000",
    "GIT_COMMITTER_NAME": "gc graph", "GIT_COMMITTER_EMAIL": "gc-graph@invalid",
    "GIT_COMMITTER_DATE": "@0 +0000",
}


class ConflictCheckError(Exception):
    """The repository can't be used or the branch can't be fetched."""


@dataclass(frozen=True)
class Patch:
    cn: int
    ps: int
    commit: str


@dataclass(frozen=True)
class _Pick:
    status: str  # "clean" | "empty" | "conflict" | "error"
    commit: str = ""
    files: tuple[str, ...] = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("clean", "empty")


def change_ref(cn: int, ps: int) -> str:
    return f"refs/changes/{cn % 100:02d}/{cn}/{ps}"


class Repo:
    """A local clone used for fetching and trial merges."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_dir():
            raise ConflictCheckError(f"{self.path}: no such directory")
        version = self._run("version").stdout.split()
        nums = tuple(int(x) for x in re.findall(r"\d+", version[2] if len(version) > 2 else "")[:2])
        if nums < _MIN_GIT:
            raise ConflictCheckError(
                f"git {'.'.join(map(str, _MIN_GIT))} or newer is needed "
                f"for merge-tree --merge-base (found {' '.join(version[2:3])})"
            )
        r = self._run("rev-parse", "--path-format=absolute", "--git-path", "objects")
        if r.returncode != 0:
            raise ConflictCheckError(f"{self.path}: not a git repository")
        self.objects = r.stdout.strip()
        self._scratch: str | None = None
        self._trees: dict[str, str] = {}

    def _run(self, *args: str, scratch: bool = False, input: str | None = None,
             timeout: int = 600) -> subprocess.CompletedProcess[str]:
        env = None
        if scratch:
            env = dict(os.environ, GIT_OBJECT_DIRECTORY=self._scratch or "",
                       GIT_ALTERNATE_OBJECT_DIRECTORIES=self.objects, **_COMMIT_ENV)
        return subprocess.run(
            ["git", "-C", str(self.path), *args], capture_output=True,
            text=True, input=input, timeout=timeout, env=env,
        )

    def resolve_remote(self, project: str) -> str | None:
        """The remote that fetches Gerrit `project`, "origin" first."""
        r = self._run("remote", "-v")
        pattern = re.compile(r"[/:]" + re.escape(project) + r"(?:\.git)?/?$")
        names = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2] == "(fetch)" and pattern.search(parts[1]):
                names.append(parts[0])
        if "origin" in names:
            return "origin"
        return names[0] if names else None

    def branch_tip(self, remote: str, branch: str) -> str:
        ref = f"refs/heads/{branch}"
        r = self._run("ls-remote", remote, ref)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
        raise ConflictCheckError(
            f"git ls-remote {remote} {ref} failed: {r.stderr.strip() or 'no such branch'}")

    def missing(self, commits: list[str]) -> set[str]:
        r = self._run("cat-file", "--batch-check=%(objectname) %(objecttype)",
                      input="".join(c + "\n" for c in commits))
        out = set()
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "missing":
                out.add(parts[0])
        return out

    def fetch_refs(self, remote: str, refs: list[str]) -> None:
        """Fetch refs into the object store (no refs, no FETCH_HEAD).

        A batch that fails is retried ref by ref so one deleted ref does
        not lose the rest."""
        for i in range(0, len(refs), _FETCH_BATCH):
            batch = refs[i:i + _FETCH_BATCH]
            args = ("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", remote)
            if self._run(*args, *batch).returncode == 0:
                continue
            for ref in batch:
                self._run(*args, ref)

    def parents(self, commits: list[str]) -> dict[str, list[str]]:
        if not commits:
            return {}
        r = self._run("rev-list", "--no-walk", "--parents", *commits)
        out = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if parts:
                out[parts[0]] = parts[1:]
        return out

    def changed_files(self, pairs: dict[str, str]) -> dict[str, set[str]]:
        """Paths each commit changes against the given parent."""
        if not pairs:
            return {}
        r = self._run("diff-tree", "--stdin", "-r", "--name-only", "--no-renames",
                      input="".join(f"{c} {p}\n" for c, p in pairs.items()))
        out: dict[str, set[str]] = {c: set() for c in pairs}
        cur = None
        for line in r.stdout.splitlines():
            if line in out:
                cur = line
            elif line and cur is not None:
                out[cur].add(line)
        return out

    def tree_of(self, commit: str) -> str:
        if commit not in self._trees:
            r = self._run("rev-parse", f"{commit}^{{tree}}", scratch=True)
            self._trees[commit] = r.stdout.strip()
        return self._trees[commit]

    def pick(self, onto: str, commit: str, parent: str) -> _Pick:
        r = self._run("merge-tree", "--write-tree", "--name-only", "--no-messages",
                      f"--merge-base={parent}", onto, commit, scratch=True)
        lines = r.stdout.splitlines()
        # exit 1 is also what an unknown object gets; only a real merge
        # prints the result tree first
        tree = lines[0] if lines and _OID_RE.match(lines[0]) else ""
        if r.returncode == 1 and tree:
            return _Pick("conflict", files=tuple(dict.fromkeys(x for x in lines[1:] if x)))
        if r.returncode != 0 or not tree:
            return _Pick("error", detail=r.stderr.strip() or f"exit {r.returncode}")
        if tree == self.tree_of(onto):
            return _Pick("empty", commit=onto)
        c = self._run("commit-tree", tree, "-p", onto, "-m", commit, scratch=True)
        if c.returncode != 0:
            return _Pick("error", detail=c.stderr.strip())
        new = c.stdout.strip()
        self._trees[new] = tree
        return _Pick("clean", commit=new)

    def __enter__(self) -> "Repo":
        self._scratch = tempfile.mkdtemp(prefix="gc-graph-conflicts-")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._scratch:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None
        self._trees.clear()


@dataclass
class _State:
    patches: dict[int, Patch]
    base: dict[int, str] = field(default_factory=dict)       # parent commit
    under: dict[int, int] = field(default_factory=dict)      # in-flight change below
    results: dict[int, dict[str, Any]] = field(default_factory=dict)
    applied: dict[int, str] = field(default_factory=dict)    # stack on the tip
    stack: dict[int, list[int]] = field(default_factory=dict)  # bottom-up, incl. self


def check_conflicts(
    repo: Repo,
    remote: str,
    branch: str,
    patches: list[Patch],
    owner_of: dict[str, int],
    *,
    log: Callable[[str], None] | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Trial-merge `patches` against the fetched tip of `branch` and
    against each other.

    `owner_of` maps every known patch set commit of the in-flight changes
    to its change number; it decides which change a patch stands on.
    """
    say = log or (lambda _msg: None)
    workers = workers or min(8, os.cpu_count() or 2)
    st = _State({p.cn: p for p in patches})

    with repo:
        tip = repo.branch_tip(remote, branch)
        missing = repo.missing([tip] + [p.commit for p in patches])
        refs = [f"refs/heads/{branch}"] if tip in missing else []
        refs += [change_ref(p.cn, p.ps) for p in patches if p.commit in missing]
        if refs:
            say(f"fetching {len(refs)} ref(s)")
            repo.fetch_refs(remote, refs)
            missing = repo.missing(sorted(missing))
        if tip in missing:
            raise ConflictCheckError(f"could not fetch {branch} from {remote}")
        _link(repo, st, missing, owner_of)
        _check_tip(repo, st, tip, workers)
        own = repo.changed_files({st.patches[cn].commit: st.base[cn] for cn in st.applied})
        own_by_cn = {cn: own.get(st.patches[cn].commit, set()) for cn in st.applied}
        pairs, tried, errors = _check_pairs(repo, st, own_by_cn, workers)

    for cn, res in st.results.items():
        if cn in st.under:
            res["under"] = st.under[cn]
    summary = defaultdict(int)
    for res in st.results.values():
        summary[res["status"]] += 1
    say(", ".join(f"{n} {s}" for s, n in sorted(summary.items()))
        + f"; {tried} pair(s) tried, {len(pairs)} conflicting"
        + (f", {errors} failed" if errors else ""))
    return {
        "branch": branch,
        "tip": tip,
        "results": {str(cn): st.results[cn] for cn in sorted(st.results)},
        "pairs": pairs,
        "pairs_tried": tried,
        "pair_errors": errors,
    }


def _link(repo: Repo, st: _State, missing: set[str], owner_of: dict[str, int]) -> None:
    """Parent commit and in-flight change below each patch."""
    present = [p.commit for p in st.patches.values() if p.commit not in missing]
    parents = repo.parents(present)
    for cn, p in st.patches.items():
        if p.commit in missing:
            st.results[cn] = {"status": "error", "reason": "commit could not be fetched"}
            continue
        ps = parents.get(p.commit, [])
        if len(ps) != 1:
            st.results[cn] = {"status": "skipped",
                              "reason": "merge commit" if ps else "root commit"}
            continue
        st.base[cn] = ps[0]
        below = owner_of.get(ps[0])
        if below is not None and below != cn and below in st.patches:
            st.under[cn] = below
    # Two changes each on an old patch set of the other: no order lands both.
    for cn in list(st.under):
        seen, cur = [], cn
        while cur in st.under and cur not in seen:
            seen.append(cur)
            cur = st.under[cur]
        if cur in seen:
            for c in seen[seen.index(cur):]:
                st.under.pop(c, None)
                st.results[c] = {"status": "error", "reason": "circular dependency"}
    for cn in st.patches:
        chain, cur = [], cn
        while cur is not None:
            chain.append(cur)
            cur = st.under.get(cur)
        st.stack[cn] = chain[::-1]


def _check_tip(repo: Repo, st: _State, tip: str, workers: int) -> None:
    levels: dict[int, list[int]] = defaultdict(list)
    for cn in st.patches:
        if cn not in st.results:
            levels[len(st.stack[cn])].append(cn)

    def one(cn: int) -> tuple[int, _Pick | None]:
        below = st.under.get(cn)
        if below is not None and below not in st.applied:
            return cn, None
        onto = st.applied[below] if below is not None else tip
        return cn, repo.pick(onto, st.patches[cn].commit, st.base[cn])

    with ThreadPoolExecutor(workers) as pool:
        for depth in sorted(levels):
            for cn, pick in pool.map(one, sorted(levels[depth])):
                if pick is None:
                    res = st.results[st.under[cn]]
                    by = res.get("by", st.under[cn]) if res["status"] == "blocked" else st.under[cn]
                    st.results[cn] = {"status": "blocked", "by": by}
                elif pick.ok:
                    st.applied[cn] = pick.commit
                    st.results[cn] = {"status": "clean"}
                    if pick.status == "empty":
                        st.results[cn]["empty"] = True
                elif pick.status == "conflict":
                    st.results[cn] = {"status": "conflict", "files": list(pick.files)}
                else:
                    st.results[cn] = {"status": "error", "reason": pick.detail[:300]}


def _check_pairs(repo: Repo, st: _State, own: dict[int, set[str]],
                 workers: int) -> tuple[list[dict[str, Any]], int, int]:
    by_file: dict[str, list[int]] = defaultdict(list)
    for cn in sorted(own):
        for f in own[cn]:
            by_file[f].append(cn)
    stack_set = {cn: set(st.stack[cn]) for cn in own}

    def side_start(x: int, other: int) -> int:
        return next(n for n in st.stack[x] if n not in stack_set[other])

    # Oriented by the first change of each side above their common part,
    # which is the same for every change further up that side: the pair
    # (a, b) and (a's ancestor, b) are tried the same way round.
    todo: dict[int, set[int]] = defaultdict(set)
    for cns in by_file.values():
        for i, x in enumerate(cns):
            for y in cns[i + 1:]:
                if x in stack_set[y] or y in stack_set[x]:
                    continue
                a, b = (x, y) if side_start(x, y) < side_start(y, x) else (y, x)
                todo[a].add(b)

    def run(a: int) -> list[tuple[int, int, _Pick, int]]:
        # memo[z] = stack(a) and then stack(z) above the common part
        memo: dict[int, _Pick] = {}
        out = []
        for b in sorted(todo[a]):
            cur, fail, at = st.applied[a], None, b
            for z in st.stack[b]:
                if z in stack_set[a]:
                    continue
                if z not in memo:
                    memo[z] = repo.pick(cur, st.patches[z].commit, st.base[z])
                if not memo[z].ok:
                    fail, at = memo[z], z
                    break
                cur = memo[z].commit
            out.append((a, b, fail or _Pick("clean"), at))
        return out

    tried: dict[tuple[int, int], tuple[_Pick, int]] = {}
    with ThreadPoolExecutor(workers) as pool:
        for chunk in pool.map(run, sorted(todo)):
            for a, b, pick, at in chunk:
                tried[(a, b)] = (pick, at)

    errors = sum(1 for pick, _ in tried.values() if pick.status == "error")
    pairs = []
    for (a, b), (pick, at) in sorted(tried.items()):
        if pick.status != "conflict" or at != b:
            continue
        files = set(pick.files) & own[a]
        for lower in st.stack[a][:-1]:
            if lower in stack_set[b]:
                continue
            prev = tried.get((lower, b))
            if prev and prev[0].status == "conflict" and prev[1] == b:
                files -= set(prev[0].files) & own[lower]
        if files:
            pairs.append({"a": a, "b": b, "files": sorted(files)})
    return pairs, len(tried), errors
