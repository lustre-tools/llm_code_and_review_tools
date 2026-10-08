"""Trial merges of a graph's in-flight changes (`gc graph --conflicts REPO`).

A change lands the way Gerrit's cherry-pick submit lands it: its own diff
(parent commit -> commit) picked onto the branch after the in-flight
changes it stands on. Every pick is `git merge-tree --merge-base=<parent>
<onto> <commit>`, the ort merge `git cherry-pick` runs, done in memory.

1. Against the branch tip: each change is picked onto the result of the
   changes below it, so one pass says for every change whether its stack
   applies, where it stops applying (conflict) and what sits above that
   (blocked). A change at the bottom whose parent is not on the branch
   (an abandoned change, a change outside the graph, a stray commit) is
   tried with its own diff alone, so its result says that ("base"). A
   change on an older patchset of the change below it that does not
   apply is also tried on that change's current patchset alone; when it
   conflicts there too, the cause is the parent's new patchset
   ("parent"), not the branch.
2. Against <branch>-next (master-next: the patches queued to land, on
   top of the branch tip), when it exists: each change that applies on
   the branch and is not queued itself lands the same way on the
   -next tip, its queued ancestors being there already. When it does
   not apply, the -next commits touching the conflicting files are
   tried in queue order, each as the tip; the first one it fails on is
   the queued patch it collides with ("with").
3. Against each other: for two changes neither of which stands on the
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
_CHANGE_ID_RE = re.compile(r"^Change-Id:\s*(I[0-9a-f]{40})\s*$", re.M)
# merge-tree --write-tree with --merge-base
_MIN_GIT = (2, 40)
_TIMED_OUT = -1
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


class ConflictRepoError(ConflictCheckError):
    """The --conflicts argument is not a usable repository."""


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
            raise ConflictRepoError(f"{self.path}: no such directory")
        version = self._run("version").stdout.split()
        nums = tuple(int(x) for x in re.findall(r"\d+", version[2] if len(version) > 2 else "")[:2])
        if nums < _MIN_GIT:
            raise ConflictCheckError(
                f"git {'.'.join(map(str, _MIN_GIT))} or newer is needed "
                f"for merge-tree --merge-base (found {' '.join(version[2:3])})"
            )
        r = self._run("rev-parse", "--path-format=absolute", "--git-path", "objects")
        if r.returncode != 0:
            raise ConflictRepoError(f"{self.path}: not a git repository")
        self.objects = r.stdout.strip()
        self._scratch: str | None = None
        self._trees: dict[str, str] = {}

    def _run(self, *args: str, scratch: bool = False, input: str | None = None,
             timeout: int = 600) -> subprocess.CompletedProcess[str]:
        env = None
        if scratch:
            env = dict(os.environ, GIT_OBJECT_DIRECTORY=self._scratch or "",
                       GIT_ALTERNATE_OBJECT_DIRECTORIES=self.objects, **_COMMIT_ENV)
        try:
            return subprocess.run(
                ["git", "-C", str(self.path), *args], capture_output=True,
                text=True, input=input, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, _TIMED_OUT, "", f"timed out after {timeout} s")

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

    def branch_tips(self, remote: str, branches: list[str]) -> dict[str, str]:
        """Tip of each branch on the remote; a missing branch is left out."""
        r = self._run("ls-remote", remote, *(f"refs/heads/{b}" for b in branches))
        if r.returncode != 0:
            raise ConflictCheckError(f"git ls-remote {remote} failed: {r.stderr.strip()}")
        tips = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                tips[parts[1][len("refs/heads/"):]] = parts[0]
        return tips

    def first_parent_range(self, base: str, head: str) -> list[str]:
        r = self._run("rev-list", "--reverse", "--first-parent", f"{base}..{head}")
        return r.stdout.split()

    def messages(self, commits: list[str]) -> dict[str, tuple[str, str]]:
        """(subject, last Change-Id or "") of each commit."""
        if not commits:
            return {}
        r = self._run("log", "--no-walk=unsorted", "--format=%H%x00%s%x00%B%x01", *commits)
        out = {}
        for rec in r.stdout.split("\x01"):
            parts = rec.strip("\n").split("\x00", 2)
            if len(parts) == 3:
                ids = _CHANGE_ID_RE.findall(parts[2])
                out[parts[0]] = (parts[1], ids[-1] if ids else "")
        return out

    def is_ancestor(self, commit: str, of: str) -> bool:
        return self._run("merge-base", "--is-ancestor", commit, of).returncode == 0

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
        not lose the rest; after a timeout nothing more is fetched and
        what is missing is reported missing."""
        # an empty --refmap: a named remote's fetch refspec would
        # otherwise move its remote-tracking branches. No auto-maintenance:
        # the fetched commits are referenced by nothing, so a `gc --auto`
        # started here may prune the older ones while another build on the
        # same clone is about to merge them. Packing the clone is left to
        # its owner (README: "Sharing a clone").
        args = ("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                "--no-auto-maintenance", "--refmap=", remote)
        for i in range(0, len(refs), _FETCH_BATCH):
            batch = refs[i:i + _FETCH_BATCH]
            rc = self._run(*args, *batch).returncode
            if rc == _TIMED_OUT:
                return
            if rc == 0:
                continue
            for ref in batch:
                if self._run(*args, ref).returncode == _TIMED_OUT:
                    return

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
    off_branch: dict[int, dict[str, Any]] = field(default_factory=dict)
    under: dict[int, int] = field(default_factory=dict)      # in-flight change below
    results: dict[int, dict[str, Any]] = field(default_factory=dict)
    applied: dict[int, str] = field(default_factory=dict)    # stack on the tip
    stack: dict[int, list[int]] = field(default_factory=dict)  # bottom-up, incl. self


def check_conflicts(
    repo: Repo,
    remote: str,
    branch: str,
    patches: list[Patch],
    owners: dict[str, tuple[int, str]],
    *,
    lookup: Callable[[list[str]], dict[str, tuple[int, str]]] | None = None,
    next_branch: str | None = None,
    change_lookup: Callable[[list[str]], dict[str, int]] | None = None,
    log: Callable[[str], None] | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Trial-merge `patches` against the fetched tip of `branch` and
    against each other.

    `owners` maps known patch set commits to (change, status): a parent
    owned by one of `patches` is the change a patch stands on. `lookup`
    finds the owners of other parent commits that are not on the branch.
    `next_branch` names the queue to check against when the remote has
    it; `change_lookup` turns the Change-Ids of the queued patches the
    changes collide with into change numbers.
    """
    say = log or (lambda _msg: None)
    workers = workers or min(8, os.cpu_count() or 2)
    st = _State({p.cn: p for p in patches})

    with repo:
        heads = repo.branch_tips(remote, [branch] + ([next_branch] if next_branch else []))
        if branch not in heads:
            raise ConflictCheckError(f"{remote} has no branch {branch}")
        tip = heads[branch]
        missing = repo.missing(list(heads.values()) + [p.commit for p in patches])
        refs = [f"refs/heads/{b}" for b, sha in heads.items() if sha in missing]
        refs += [change_ref(p.cn, p.ps) for p in patches if p.commit in missing]
        if refs:
            say(f"fetching {len(refs)} ref(s)")
            repo.fetch_refs(remote, refs)
            missing = repo.missing(sorted(missing))
        if tip in missing:
            raise ConflictCheckError(f"could not fetch {branch} from {remote}")
        next_tip = heads.get(next_branch or "")
        if next_tip in missing:
            say(f"could not fetch {next_branch}")
            next_tip = None
        _link(repo, st, missing, owners)
        _off_branch(repo, st, tip, owners, lookup)
        _check_tip(repo, st, tip, workers)
        own = repo.changed_files({st.patches[cn].commit: st.base[cn] for cn in st.applied})
        own_by_cn = {cn: own.get(st.patches[cn].commit, set()) for cn in st.applied}
        pairs, tried, errors = _check_pairs(repo, st, own_by_cn, workers)
        queue = (_check_next(repo, st, branch, tip, next_branch, next_tip, change_lookup, workers)
                 if next_branch and next_tip else None)

    for cn, res in st.results.items():
        if cn in st.under:
            res["under"] = st.under[cn]
        bottom = st.stack[cn][0] if st.stack.get(cn) else cn
        if bottom in st.off_branch:
            res["base"] = st.off_branch[bottom]
    summary = defaultdict(int)
    for res in st.results.values():
        summary[res["status"]] += 1
    say(", ".join(f"{n} {s}" for s, n in sorted(summary.items()))
        + f"; {tried} pair(s) tried, {len(pairs)} conflicting"
        + (f", {errors} failed" if errors else ""))
    out = {
        "branch": branch,
        "tip": tip,
        "results": {str(cn): st.results[cn] for cn in sorted(st.results)},
        "pairs": pairs,
        "pairs_tried": tried,
        "pair_errors": errors,
    }
    if queue is not None:
        counts = defaultdict(int)
        for res in queue.get("results", {}).values():
            counts[res["status"]] += 1
        say(f"{next_branch}: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
                                   or queue.get("skipped", "nothing to try")))
        out["next"] = queue
    return out


def _link(repo: Repo, st: _State, missing: set[str],
          owners: dict[str, tuple[int, str]]) -> None:
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
        below = owners.get(ps[0], (None, ""))[0]
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


def _off_branch(repo: Repo, st: _State, tip: str, owners: dict[str, tuple[int, str]],
                lookup: Callable[[list[str]], dict[str, tuple[int, str]]] | None) -> None:
    """The bottom changes whose parent is not on the branch: what they
    sit on instead. A merged change's old patchset counts as on the
    branch; cherry-pick submit landed it under another commit."""
    unknown: dict[str, list[int]] = defaultdict(list)
    for cn, parent in st.base.items():
        if cn in st.under or cn in st.results:
            continue
        owner = owners.get(parent)
        if owner is None or owner[0] == cn:
            unknown[parent].append(cn)
        elif owner[1] != "MERGED":
            st.off_branch[cn] = {"cn": owner[0], "status": owner[1]}
    for parent in [p for p in unknown if repo.is_ancestor(p, tip)]:
        del unknown[parent]
    found: dict[str, tuple[int, str]] = {}
    if unknown and lookup is not None:
        try:
            found = lookup(sorted(unknown))
        except Exception:  # the base stays "unknown"
            found = {}
    for parent, cns in unknown.items():
        owner = found.get(parent)
        if owner is not None and owner[1] == "MERGED":
            continue
        for cn in cns:
            if owner is None:
                st.off_branch[cn] = {"status": "unknown"}
            elif owner[0] == cn:
                st.off_branch[cn] = {"status": "own"}
            else:
                st.off_branch[cn] = {"cn": owner[0], "status": owner[1]}


def _check_tip(repo: Repo, st: _State, tip: str, workers: int) -> None:
    levels: dict[int, list[int]] = defaultdict(list)
    for cn in st.patches:
        if cn not in st.results:
            levels[len(st.stack[cn])].append(cn)

    def one(cn: int) -> tuple[int, _Pick | None, _Pick | None]:
        below = st.under.get(cn)
        if below is not None and below not in st.applied:
            return cn, None, None
        onto = st.applied[below] if below is not None else tip
        commit, base = st.patches[cn].commit, st.base[cn]
        pick = repo.pick(onto, commit, base)
        on_parent = None
        if (pick.status == "conflict" and below is not None
                and base != st.patches[below].commit):
            on_parent = repo.pick(st.patches[below].commit, commit, base)
        return cn, pick, on_parent

    with ThreadPoolExecutor(workers) as pool:
        for depth in sorted(levels):
            for cn, pick, on_parent in pool.map(one, sorted(levels[depth])):
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
                    if on_parent is not None and on_parent.status == "conflict":
                        st.results[cn]["parent"] = {"cn": st.under[cn],
                                                    "files": list(on_parent.files)}
                else:
                    st.results[cn] = {"status": "error", "reason": pick.detail[:300]}


def _check_next(repo: Repo, st: _State, branch: str, tip: str, name: str, next_tip: str,
                change_lookup: Callable[[list[str]], dict[str, int]] | None,
                workers: int) -> dict[str, Any]:
    # Landing puts new commits on the branch (not -next's), so until
    # -next is rebuilt on the new tip it holds what already landed.
    if not repo.is_ancestor(tip, next_tip):
        return {"branch": name, "tip": next_tip,
                "skipped": f"{name} is stale, {branch} has moved on since it was built"}
    queue = repo.first_parent_range(tip, next_tip)
    pos = {c: i for i, c in enumerate(queue)}
    info = repo.messages(queue + [st.patches[cn].commit for cn in st.base])
    queued_ids = {info[c][1]: c for c in queue if info.get(c, ("", ""))[1]}
    # change -> its commit in the queue
    queued = {cn: queued_ids[cid] for cn in st.base
              if (cid := info.get(st.patches[cn].commit, ("", ""))[1]) in queued_ids}
    results: dict[int, dict[str, Any]] = {cn: {"status": "queued"} for cn in queued}
    todo = [cn for cn in st.applied if cn not in queued]
    own = {cn: [z for z in st.stack[cn] if z not in queued] for cn in todo}
    touched = repo.changed_files({c: (queue[i - 1] if i else tip) for i, c in enumerate(queue)})
    applied: dict[int, str] = {}

    def one(cn: int) -> tuple[int, _Pick | None]:
        below = own[cn][-2] if len(own[cn]) > 1 else None
        if below is not None and below not in applied:
            return cn, None
        onto = applied[below] if below is not None else next_tip
        return cn, repo.pick(onto, st.patches[cn].commit, st.base[cn])

    levels: dict[int, list[int]] = defaultdict(list)
    for cn in todo:
        levels[len(own[cn])].append(cn)
    with ThreadPoolExecutor(workers) as pool:
        for depth in sorted(levels):
            for cn, pick in pool.map(one, sorted(levels[depth])):
                if pick is None:
                    below = own[cn][-2]
                    res = results[below]
                    by = res.get("by", below) if res["status"] == "blocked" else below
                    results[cn] = {"status": "blocked", "by": by}
                elif pick.ok:
                    applied[cn] = pick.commit
                    results[cn] = {"status": "clean"}
                elif pick.status == "conflict":
                    results[cn] = {"status": "conflict", "files": list(pick.files)}
                else:
                    results[cn] = {"status": "error", "reason": pick.detail[:300]}

    def lands_on(i: int, cn: int) -> bool | None:
        """Does cn apply with the queue cut after commit i? The queued
        changes its stack stands on that come later are added first;
        None when one of those or a change below cn fails there, which
        says nothing about cn itself."""
        cur = queue[i]
        for z in st.stack[cn]:
            if z in queued:
                c = queued[z]
                if pos[c] <= i:
                    continue
                pick = repo.pick(cur, c, queue[pos[c] - 1] if pos[c] else tip)
            else:
                pick = repo.pick(cur, st.patches[z].commit, st.base[z])
            if not pick.ok:
                return False if z == cn else None
            cur = pick.commit
        return True

    def culprit(cn: int) -> tuple[int, int | None]:
        files = set(results[cn]["files"])
        for i, c in enumerate(queue):
            if touched.get(c, set()) & files and lands_on(i, cn) is False:
                return cn, i
        return cn, None

    failing = [cn for cn, r in results.items() if r["status"] == "conflict"]
    found: dict[int, int] = {}
    with ThreadPoolExecutor(workers) as pool:
        for cn, i in pool.map(culprit, failing):
            if i is not None:
                found[cn] = i
    numbers: dict[str, int] = {}
    ids = sorted({cid for i in found.values() if (cid := info.get(queue[i], ("", ""))[1])})
    if ids and change_lookup is not None:
        try:
            numbers = change_lookup(ids)
        except Exception:  # the patch is still named by subject
            numbers = {}
    for cn, i in found.items():
        subject, cid = info.get(queue[i], ("", ""))
        results[cn]["with"] = {"cn": numbers.get(cid), "subject": subject,
                               "commit": queue[i], "position": i + 1}
    return {"branch": name, "tip": next_tip, "ahead": len(queue),
            "results": {str(cn): results[cn] for cn in sorted(results)}}


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
