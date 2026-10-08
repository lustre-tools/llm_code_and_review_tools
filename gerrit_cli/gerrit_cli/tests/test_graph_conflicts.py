"""Tests for the --conflicts trial merges (graph/conflicts.py).

A bare repository stands in for Gerrit: master plus refs/changes/...
for every patch set. The checked repository starts empty and gets
everything by fetching, as it would from Gerrit.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gerrit_cli.graph.build import _check_conflicts, build_graph
from gerrit_cli.graph.conflicts import (
    ConflictCheckError,
    Patch,
    Repo,
    change_ref,
    check_conflicts,
)

_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
LINES = [f"line {i}" for i in range(1, 21)]


def _git(cwd: Path, *args: str, input: str | None = None, env=None) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          capture_output=True, input=input,
                          env=env or _ENV).stdout.strip()


def _text(edits: dict[int, str]) -> str:
    """The 20-line file with the given 1-based lines replaced."""
    return "\n".join(edits.get(i + 1, line) for i, line in enumerate(LINES)) + "\n"


class Gerrit:
    """Commits and change refs in a bare repository."""

    def __init__(self, root: Path):
        self.path = root / "fs" / "lustre-release.git"
        self.path.mkdir(parents=True)
        _git(self.path, "init", "-q", "--bare")
        self.index = root / "index"
        self.owner: dict[str, tuple[int, str]] = {}
        self.master = self.commit(None, {"a.c": _text({}), "b.c": _text({})}, "base")
        _git(self.path, "update-ref", "refs/heads/master", self.master)

    def commit(self, parent: str | None, files: dict[str, str], msg: str) -> str:
        env = dict(_ENV, GIT_INDEX_FILE=str(self.index))
        _git(self.path, "read-tree", *([parent] if parent else ["--empty"]), env=env)
        for path, content in files.items():
            blob = _git(self.path, "hash-object", "-w", "--stdin", input=content)
            _git(self.path, "update-index", "--add", "--cacheinfo",
                 f"100644,{blob},{path}", env=env)
        tree = _git(self.path, "write-tree", env=env)
        return _git(self.path, "commit-tree", tree,
                    *(["-p", parent] if parent else []), "-m", msg)

    def advance(self, files: dict[str, str]) -> str:
        self.master = self.commit(self.master, files, "master moves on")
        _git(self.path, "update-ref", "refs/heads/master", self.master)
        return self.master

    def change(self, cn: int, parent: str, files: dict[str, str], ps: int = 1,
               status: str = "NEW") -> Patch:
        sha = self.commit(parent, files, f"change {cn} ps{ps}\n\nChange-Id: {change_id(cn)}")
        _git(self.path, "update-ref", change_ref(cn, ps), sha)
        self.owner[sha] = (cn, status)
        return Patch(cn, ps, sha)


def change_id(cn: int) -> str:
    return f"I{cn:040x}"


def _queue(gerrit: "Gerrit", picks: list[tuple[int | None, dict[str, str]]]) -> list[str]:
    """master-next: master plus one commit per (change or None, files)."""
    head, out = gerrit.master, []
    for cn, files in picks:
        msg = f"queued {cn}" + (f"\n\nChange-Id: {change_id(cn)}" if cn else "")
        head = gerrit.commit(head, files, msg)
        out.append(head)
    _git(gerrit.path, "update-ref", "refs/heads/master-next", head)
    return out


@pytest.fixture
def gerrit(tmp_path):
    return Gerrit(tmp_path)


@pytest.fixture
def local(tmp_path):
    path = tmp_path / "local"
    path.mkdir()
    _git(path, "init", "-q")
    return path


def _check(gerrit: Gerrit, local: Path, patches: list[Patch], owners=None,
           lookup=None) -> dict:
    return check_conflicts(Repo(local), str(gerrit.path), "master", patches,
                           gerrit.owner if owners is None else owners,
                           lookup=lookup, workers=2)


def _status(result: dict) -> dict[int, str]:
    return {int(cn): r["status"] for cn, r in result["results"].items()}


def _pairs(result: dict) -> list[tuple[int, int, list[str]]]:
    return [(p["a"], p["b"], p["files"]) for p in result["pairs"]]


class TestAgainstTheBranch:
    def test_changes_on_different_lines_apply_and_do_not_conflict(self, gerrit, local):
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        z = gerrit.change(20, gerrit.master, {"a.c": _text({15: "z"})})
        result = _check(gerrit, local, [x, z])
        assert result["tip"] == gerrit.master
        assert _status(result) == {10: "clean", 20: "clean"}
        assert result["pairs"] == [] and result["pairs_tried"] == 1

    def test_a_conflict_blocks_the_changes_above_it(self, gerrit, local):
        base = gerrit.master
        gerrit.advance({"a.c": _text({5: "master"})})
        m1 = gerrit.change(10, base, {"a.c": _text({5: "m1"})})
        m2 = gerrit.change(11, m1.commit, {"a.c": _text({5: "m1", 9: "m2"})})
        m3 = gerrit.change(12, m2.commit, {"a.c": _text({5: "m1", 9: "m2", 12: "m3"})})
        q = gerrit.change(20, base, {"a.c": _text({9: "q"})})
        result = _check(gerrit, local, [m1, m2, m3, q])
        res = result["results"]
        assert res["10"] == {"status": "conflict", "files": ["a.c"]}
        assert res["11"] == {"status": "blocked", "by": 10, "under": 10}
        assert res["12"] == {"status": "blocked", "by": 10, "under": 11}
        assert res["20"]["status"] == "clean"
        # m2 and q collide on line 9, but m2 never lands: not tried
        assert result["pairs"] == [] and result["pairs_tried"] == 0

    def test_a_stack_lands_change_by_change_on_the_moved_branch(self, gerrit, local):
        base = gerrit.master
        gerrit.advance({"b.c": _text({1: "master"})})
        s1 = gerrit.change(10, base, {"a.c": _text({2: "s1"})})
        s2 = gerrit.change(11, s1.commit, {"a.c": _text({2: "s1", 3: "s2"})})
        result = _check(gerrit, local, [s1, s2])
        assert result["results"]["11"] == {"status": "clean", "under": 10}

    def test_a_change_on_an_old_patch_set_lands_after_the_current_one(self, gerrit, local):
        y1 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "y"})})
        y2 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "y", 3: "y2"})}, ps=2)
        x = gerrit.change(20, y1.commit, {"a.c": _text({2: "y", 12: "x"})})
        result = _check(gerrit, local, [y2, x])
        assert result["results"]["20"] == {"status": "clean", "under": 10}

    def test_a_change_already_on_the_branch_is_empty(self, gerrit, local):
        base = gerrit.master
        gerrit.advance({"a.c": _text({4: "landed"})})
        e = gerrit.change(10, base, {"a.c": _text({4: "landed"})})
        result = _check(gerrit, local, [e])
        assert result["results"]["10"] == {"status": "clean", "empty": True}

    def test_a_ref_gerrit_does_not_have_is_an_error(self, gerrit, local):
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        gone = Patch(11, 1, "1" * 40)
        result = _check(gerrit, local, [x, gone])
        assert _status(result) == {10: "clean", 11: "error"}

    def test_changes_on_each_others_old_patch_sets_are_circular(self, gerrit, local):
        x1 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        y1 = gerrit.change(20, x1.commit, {"a.c": _text({2: "x", 9: "y"})})
        x2 = gerrit.change(10, y1.commit, {"a.c": _text({2: "x", 9: "y", 15: "x2"})}, ps=2)
        result = _check(gerrit, local, [x2, y1])
        assert result["results"]["10"]["reason"] == "circular dependency"
        assert _status(result) == {10: "error", 20: "error"}


class TestBaseNotOnTheBranch:
    """A change at the bottom whose parent is not on the branch is tried
    with its own diff alone; the result says what it sits on."""

    def _on(self, gerrit, status):
        a = gerrit.change(30, gerrit.master, {"a.c": _text({2: "a"})}, status=status)
        x = gerrit.change(10, a.commit, {"a.c": _text({2: "x"})})
        return a, x

    @pytest.mark.parametrize("status", ["ABANDONED", "NEW"])
    def test_a_change_on_a_change_that_does_not_land(self, gerrit, local, status):
        """x rewrites the line its base added: alone it does not apply,
        and that is the base's doing, not master's."""
        _a, x = self._on(gerrit, status)
        res = _check(gerrit, local, [x])["results"]["10"]
        assert res == {"status": "conflict", "files": ["a.c"],
                       "base": {"cn": 30, "status": status}}

    def test_a_change_on_an_old_patch_set_of_a_merged_change(self, gerrit, local):
        m1 = gerrit.change(30, gerrit.master, {"a.c": _text({2: "m"})}, status="MERGED")
        gerrit.advance({"a.c": _text({2: "m"})})
        x = gerrit.change(10, m1.commit, {"a.c": _text({2: "m", 9: "x"})})
        assert _check(gerrit, local, [x])["results"]["10"] == {"status": "clean"}

    def test_a_change_on_an_older_branch_commit(self, gerrit, local):
        old = gerrit.master
        gerrit.advance({"b.c": _text({1: "moved"})})
        x = gerrit.change(10, old, {"a.c": _text({9: "x"})})
        calls = []
        result = _check(gerrit, local, [x], lookup=lambda shas: calls.append(shas) or {})
        assert result["results"]["10"] == {"status": "clean"} and calls == []

    def test_a_commit_no_known_change_owns_is_looked_up(self, gerrit, local):
        stray = gerrit.commit(gerrit.master, {"a.c": _text({2: "s"})}, "stray")
        x = gerrit.change(10, stray, {"a.c": _text({2: "x"})})
        owners = {x.commit: (10, "NEW")}
        res = _check(gerrit, local, [x], owners, lookup=lambda shas: {})["results"]["10"]
        assert res["base"] == {"status": "unknown"}
        res = _check(gerrit, local, [x], owners,
                     lookup=lambda shas: {stray: (40, "ABANDONED")})["results"]["10"]
        assert res["base"] == {"cn": 40, "status": "ABANDONED"}
        res = _check(gerrit, local, [x], owners,
                     lookup=lambda shas: {stray: (40, "MERGED")})["results"]["10"]
        assert "base" not in res

    def test_the_changes_above_the_bottom_say_it_too(self, gerrit, local):
        """x applies alone; y on it does not: the base may be why."""
        a = gerrit.change(30, gerrit.master, {"a.c": _text({2: "a"})}, status="ABANDONED")
        x = gerrit.change(10, a.commit, {"a.c": _text({2: "a", 9: "x"})})
        y = gerrit.change(11, x.commit, {"a.c": _text({2: "a", 9: "x", 14: "y"})})
        gerrit.advance({"a.c": _text({14: "master"})})
        result = _check(gerrit, local, [x, y])["results"]
        assert result["10"] == {"status": "clean", "base": {"cn": 30, "status": "ABANDONED"}}
        assert result["11"] == {"status": "conflict", "files": ["a.c"], "under": 10,
                                "base": {"cn": 30, "status": "ABANDONED"}}

    def test_a_change_on_its_own_older_patch_set(self, gerrit, local):
        x1 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x1"})})
        x2 = gerrit.change(10, x1.commit, {"a.c": _text({2: "x2"})}, ps=2)
        owners = {x2.commit: (10, "NEW")}
        res = _check(gerrit, local, [x2], owners,
                     lookup=lambda shas: {x1.commit: (10, "NEW")})["results"]["10"]
        assert res["base"] == {"status": "own"}


class TestParentMovedOn:
    """x sits on ps1 of y, y is at ps2 now."""

    def _stack(self, gerrit, x_edits):
        y1 = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y1"})})
        y2 = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y2"})}, ps=2)
        x = gerrit.change(30, y1.commit, {"a.c": _text({2: "y1", **x_edits})})
        return y2, x

    def test_a_conflict_with_the_new_patch_set_is_the_parents(self, gerrit, local):
        y2, x = self._stack(gerrit, {3: "x"})
        res = _check(gerrit, local, [y2, x])["results"]["30"]
        assert res == {"status": "conflict", "files": ["a.c"], "under": 20,
                       "parent": {"cn": 20, "files": ["a.c"]}}

    def test_a_conflict_the_parent_does_not_cause_is_the_branchs(self, gerrit, local):
        y2, x = self._stack(gerrit, {9: "x"})
        gerrit.advance({"a.c": _text({9: "master"})})
        res = _check(gerrit, local, [y2, x])["results"]["30"]
        assert res == {"status": "conflict", "files": ["a.c"], "under": 20}

    def test_on_the_current_patch_set_no_extra_pick(self, gerrit, local):
        y = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y"})})
        x = gerrit.change(30, y.commit, {"a.c": _text({2: "y", 9: "x"})})
        gerrit.advance({"a.c": _text({9: "master"})})
        res = _check(gerrit, local, [y, x])["results"]["30"]
        assert "parent" not in res and res["status"] == "conflict"


class TestMasterNext:
    """master-next = master + the queued patches. Changes that are not
    queued are tried on it, and a conflict names the queued patch."""

    def _check(self, gerrit, local, patches, lookup=None):
        return check_conflicts(Repo(local), str(gerrit.path), "master", patches,
                               gerrit.owner, next_branch="master-next",
                               change_lookup=lookup, workers=2)

    def test_a_change_colliding_with_a_queued_patch(self, gerrit, local):
        y = gerrit.change(10, gerrit.master, {"a.c": _text({2: "y"})})
        y2 = gerrit.change(11, y.commit, {"a.c": _text({2: "y", 12: "y2"})})
        z = gerrit.change(20, gerrit.master, {"b.c": _text({5: "z"})})
        queue = _queue(gerrit, [(None, {"b.c": _text({1: "synthetic"})}),
                                (500, {"b.c": _text({1: "synthetic"}), "a.c": _text({2: "q"})}),
                                (501, {"b.c": _text({1: "synthetic"}), "a.c": _text({2: "q", 18: "q2"})})])
        result = self._check(gerrit, local, [y, y2, z],
                             lambda ids: {change_id(500): 500, change_id(501): 501})
        assert result["results"]["10"]["status"] == "clean"
        nxt = result["next"]
        assert (nxt["branch"], nxt["tip"], nxt["ahead"]) == ("master-next", queue[-1], 3)
        assert nxt["results"]["10"] == {
            "status": "conflict", "files": ["a.c"],
            "with": {"cn": 500, "subject": "queued 500", "commit": queue[1], "position": 2}}
        assert nxt["results"]["11"] == {"status": "blocked", "by": 10}
        assert nxt["results"]["20"] == {"status": "clean"}

    def test_queued_changes_and_changes_on_them(self, gerrit, local):
        """x is queued; x2 on it is tried on top of the queue. Cut after
        q0, x is not in the queue yet and x2 (next to x's line) would
        fail there too: x is added, so the blame lands on q2."""
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        x2 = gerrit.change(11, x.commit, {"a.c": _text({2: "x", 3: "x2", 9: "x2"})})
        queue = _queue(gerrit, [(600, {"a.c": _text({15: "q0"})}),
                                (10, {"a.c": _text({2: "x", 15: "q0"})}),
                                (602, {"a.c": _text({2: "x", 9: "q2", 15: "q0"})})])
        result = self._check(gerrit, local, [x, x2],
                             lambda ids: {change_id(600): 600, change_id(602): 602})
        nxt = result["next"]["results"]
        assert nxt["10"] == {"status": "queued"}
        assert nxt["11"]["status"] == "conflict"
        assert nxt["11"]["with"]["cn"] == 602 and nxt["11"]["with"]["commit"] == queue[2]

    def test_a_queued_ancestor_that_fails_early_blames_nothing(self, gerrit, local):
        """x is queued after q1, which it needs. Cut after q0, x itself
        does not apply, which says nothing about x2: the blame is q3."""
        x = gerrit.change(10, gerrit.master, {"a.c": _text({5: "x"})})
        x2 = gerrit.change(11, x.commit, {"a.c": _text({5: "x", 9: "x2"})})
        queue = _queue(gerrit, [(600, {"a.c": _text({15: "q0"})}),
                                (601, {"a.c": _text({15: "q0", 5: "q1"})}),
                                (10, {"a.c": _text({15: "q0", 5: "x"})}),
                                (603, {"a.c": _text({15: "q0", 5: "x", 9: "q3"})})])
        result = self._check(gerrit, local, [x, x2],
                             lambda ids: {change_id(c): c for c in (600, 601, 603)})
        assert result["next"]["results"]["11"]["with"]["commit"] == queue[3]

    def test_a_change_that_does_not_land_on_the_branch_is_not_tried(self, gerrit, local):
        base = gerrit.master
        gerrit.advance({"a.c": _text({5: "master"})})
        m = gerrit.change(10, base, {"a.c": _text({5: "m"})})
        _queue(gerrit, [(500, {"b.c": _text({1: "q"})})])
        result = self._check(gerrit, local, [m])
        assert result["results"]["10"]["status"] == "conflict"
        assert "10" not in result["next"]["results"]

    def test_a_queue_behind_the_branch_is_not_used(self, gerrit, local):
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        _queue(gerrit, [(500, {"b.c": _text({1: "q"})})])
        gerrit.advance({"b.c": _text({16: "master moved"})})
        nxt = self._check(gerrit, local, [x])["next"]
        assert nxt["skipped"] == "master-next is not on top of the branch tip"

    def test_without_a_queue_there_is_no_next(self, gerrit, local):
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        assert "next" not in self._check(gerrit, local, [x])


class TestBetweenChanges:
    def test_two_changes_on_the_same_line_conflict(self, gerrit, local):
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        y = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y"}), "b.c": _text({7: "y"})})
        result = _check(gerrit, local, [x, y])
        assert _status(result) == {10: "clean", 20: "clean"}
        assert _pairs(result) == [(10, 20, ["a.c"])]

    @pytest.mark.parametrize("other", [5, 50])
    def test_only_the_change_that_collides_is_paired(self, gerrit, local, other):
        """a2 sits on a1, which edits the line y edits; a2 edits the file
        elsewhere. Whichever side is applied first, the pair is a1-y."""
        a1 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "a1"})})
        a2 = gerrit.change(11, a1.commit, {"a.c": _text({2: "a1", 18: "a2"})})
        y = gerrit.change(other, gerrit.master, {"a.c": _text({2: "y"})})
        result = _check(gerrit, local, [a1, a2, y])
        assert [sorted(p[:2]) for p in _pairs(result)] == [sorted([10, other])]

    def test_a_change_editing_the_collision_again_is_not_paired_again(self, gerrit, local):
        """The stack's top has the lower number, so the pair of its base
        and y is tried with y first: still only the base is paired."""
        a1 = gerrit.change(30, gerrit.master, {"a.c": _text({2: "a1"})})
        a2 = gerrit.change(10, a1.commit, {"a.c": _text({2: "a2"})})
        y = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y"})})
        result = _check(gerrit, local, [a1, a2, y])
        assert _pairs(result) == [(20, 30, ["a.c"])]

    def test_changes_above_the_collision_on_another_file_are_not_paired(self, gerrit, local):
        a1 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "a1"})})
        a2 = gerrit.change(11, a1.commit, {"a.c": _text({2: "a1"}), "b.c": _text({4: "a2"})})
        y = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y"}), "b.c": _text({16: "y"})})
        result = _check(gerrit, local, [a1, a2, y])
        assert _pairs(result) == [(10, 20, ["a.c"])]

    def test_both_changes_of_a_stack_collide_on_their_own_files(self, gerrit, local):
        a1 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "a1"})})
        a2 = gerrit.change(11, a1.commit, {"a.c": _text({2: "a1"}), "b.c": _text({4: "a2"})})
        y = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y"}), "b.c": _text({4: "y"})})
        result = _check(gerrit, local, [a1, a2, y])
        assert _pairs(result) == [(10, 20, ["a.c"]), (11, 20, ["b.c"])]

    def test_siblings_on_one_change_are_tried_against_each_other(self, gerrit, local):
        p = gerrit.change(10, gerrit.master, {"b.c": _text({1: "p"})})
        s1 = gerrit.change(11, p.commit, {"b.c": _text({1: "p"}), "a.c": _text({2: "s1"})})
        s2 = gerrit.change(12, p.commit, {"b.c": _text({1: "p"}), "a.c": _text({2: "s2"})})
        result = _check(gerrit, local, [p, s1, s2])
        assert _pairs(result) == [(11, 12, ["a.c"])]
        assert result["pairs_tried"] == 1

    def test_a_stack_and_a_change_on_it_are_never_paired(self, gerrit, local):
        a1 = gerrit.change(10, gerrit.master, {"a.c": _text({2: "a1"})})
        a2 = gerrit.change(11, a1.commit, {"a.c": _text({2: "a2"})})
        result = _check(gerrit, local, [a1, a2])
        assert result["pairs_tried"] == 0


class TestRepository:
    def test_merge_results_stay_out_of_the_repository(self, gerrit, local, tmp_path,
                                                      monkeypatch):
        scratch = tmp_path / "tmp"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        y = gerrit.change(20, gerrit.master, {"a.c": _text({15: "y"})})
        _git(local, "fetch", "-q", str(gerrit.path), "refs/heads/master",
             change_ref(10, 1), change_ref(20, 1))
        before = _git(local, "count-objects", "-v")
        result = _check(gerrit, local, [x, y])
        assert _status(result) == {10: "clean", 20: "clean"}
        assert _git(local, "count-objects", "-v") == before
        assert list(scratch.iterdir()) == []

    def test_only_objects_are_fetched(self, gerrit, local):
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        _check(gerrit, local, [x])
        assert _git(local, "for-each-ref") == ""
        assert not (local / ".git" / "FETCH_HEAD").exists()

    def test_an_unknown_branch_is_an_error(self, gerrit, local):
        with pytest.raises(ConflictCheckError, match="has no branch b2_15"):
            check_conflicts(Repo(local), str(gerrit.path), "b2_15", [], {})

    def test_a_named_remote_keeps_its_refs(self, gerrit, local):
        """Fetching a branch from a remote with a fetch refspec would
        move its remote-tracking branch."""
        _git(local, "remote", "add", "origin", str(gerrit.path))
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        _queue(gerrit, [(500, {"b.c": _text({1: "q"})})])
        result = check_conflicts(Repo(local), "origin", "master", [x], gerrit.owner,
                                 next_branch="master-next", workers=2)
        assert result["next"]["results"]["10"] == {"status": "clean"}
        assert _git(local, "for-each-ref") == ""
        assert not (local / ".git" / "FETCH_HEAD").exists()

    def test_not_a_directory(self, tmp_path):
        with pytest.raises(ConflictCheckError, match="no such directory"):
            Repo(tmp_path / "nope")

    def test_not_a_repository(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        with pytest.raises(ConflictCheckError, match="not a git repository"):
            Repo(tmp_path)

    def test_the_remote_that_fetches_the_project(self, gerrit, local):
        _git(local, "remote", "add", "lit", "/elsewhere/lustre-dev.git")
        _git(local, "remote", "add", "gerrit", str(gerrit.path))
        assert Repo(local).resolve_remote("fs/lustre-release") == "gerrit"
        assert Repo(local).resolve_remote("ex/lustre-release") is None


class TestBuildStep:
    def test_in_flight_changes_of_the_branch_are_checked(self, gerrit, local):
        _git(local, "remote", "add", "origin", str(gerrit.path))
        x = gerrit.change(10, gerrit.master, {"a.c": _text({2: "x"})})
        y = gerrit.change(20, gerrit.master, {"a.c": _text({2: "y"})})
        w = gerrit.change(30, x.commit, {"a.c": _text({2: "x", 8: "w"})})

        def node(cn, status="NEW", commit="", branch="master", ps=1):
            return {"id": cn, "status": status, "current_commit": commit,
                    "current_patchset": ps, "project": "fs/lustre-release",
                    "branch": branch}

        ctx = SimpleNamespace(
            nodes={10: node(10, commit=x.commit), 20: node(20, commit=y.commit),
                   30: node(30, commit=w.commit), 40: node(40, "MERGED", gerrit.master),
                   50: node(50, commit="f" * 40, branch="b2_15"), 60: node(60)},
            commit_to_change_ps={x.commit: (10, 1), y.commit: (20, 1), w.commit: (30, 1),
                                 gerrit.master: (40, 1)},
            project="fs/lustre-release", branch="master",
            base_url="https://gerrit.invalid", log=lambda *a, **k: None,
        )
        ctx.external_merged_submitted = {}
        result = _check_conflicts(ctx, Repo(local))
        res = result["results"]
        assert res["30"] == {"status": "clean", "under": 10}
        assert res["50"] == {"status": "skipped", "reason": "not on fs/lustre-release master"}
        assert res["60"] == {"status": "skipped", "reason": "no current commit"}
        assert "40" not in res
        assert _pairs(result) == [(10, 20, ["a.c"])]

    def test_a_bad_repository_fails_before_any_gerrit_query(self, tmp_path):
        client = MagicMock()
        with pytest.raises(ConflictCheckError):
            build_graph(client, 1, "https://gerrit.invalid", progress=False,
                        conflicts_repo=str(tmp_path / "nope"))
        assert client.mock_calls == []
