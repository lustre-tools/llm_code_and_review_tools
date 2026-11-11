"""Conflict regions and their kind (conflict_hunks.py)."""

from gerrit_cli.conflict_hunks import classify, conflict_regions
from gerrit_cli.conflicts import Repo, check_conflicts
from gerrit_cli.tests.test_graph_conflicts import _check, _git, _text, gerrit, local  # noqa: F401


def test_both_append_after_the_same_line_is_insert_insert():
    assert classify([], ["x:1,", "a:1;"], ["x:1,", "b:1;"], {"a:1;"}) == "insert-insert"


def test_both_rewrite_the_last_bitfield_line_is_edit_edit():
    assert classify(["c:1;"], ["c:1,", "a:1;"], ["c:1,", "b:1;"], {"c:1,", "a:1;"}) == "edit-edit"


def test_neighbouring_lines_are_adjacent():
    assert classify(["l5", "l6"], ["L5", "l6"], ["l5", "L6"], {"L5"}) == "adjacent"


def test_branch_moved_under_a_stale_side_is_context():
    assert classify(["l5", "l6"], ["M5", "l6"], ["l5", "L6"], set()) == "context"


def _fetch(gerrit, local, *shas):
    _git(local, "fetch", "-q", str(gerrit.path), "+refs/heads/*:refs/remotes/o/*",
         "+refs/changes/*:refs/remotes/c/*")


def test_regions_come_from_the_merge_with_line_numbers(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    _fetch(gerrit, local)
    with Repo(local) as repo:
        regs = conflict_regions(repo, t.commit, c.commit, gerrit.master, ["a.c"],
                                {"a.c": {"T"}})
    [r] = regs["a.c"]
    assert (r["line"], r["kind"], r["truncated"]) == (5, "edit-edit", False)
    assert "|||||||" in r["text"] and "line 5" in r["text"]
    assert "line 2" in r["text"]


def test_a_deleted_file_conflict_has_no_regions(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    listing = _git(gerrit.path, "ls-tree", gerrit.master).splitlines()
    tree = _git(gerrit.path, "mktree",
                input="\n".join(x for x in listing if not x.endswith("\ta.c")) + "\n")
    d = _git(gerrit.path, "commit-tree", tree, "-p", gerrit.master, "-m", "del")
    _git(gerrit.path, "update-ref", "refs/changes/02/2/1", d)
    _fetch(gerrit, local)
    with Repo(local) as repo:
        regs = conflict_regions(repo, t.commit, d, gerrit.master, ["a.c"], {})
    assert regs == {"a.c": []}


def test_text_is_capped(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    _fetch(gerrit, local)
    with Repo(local) as repo:
        [r] = conflict_regions(repo, t.commit, c.commit, gerrit.master, ["a.c"], {},
                               max_lines=4)["a.c"]
    assert r["truncated"] and len(r["text"].splitlines()) == 4


def _hunks(r, other):
    return next(p for p in r["focus_pairs"] if p["other"] == other)


def test_focus_hunks_are_attached_to_conflicting_pairs_only(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    d = gerrit.change(3, gerrit.master, {"a.c": _text({15: "D"})})
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c, d],
                        gerrit.owner, workers=2, focus={1}, hunks=True)
    h = _hunks(r, 2)["hunks"]
    assert [x["kind"] for x in h["landing"]["a.c"]] == ["edit-edit", "edit-edit"]
    assert {x["order"] for x in h["landing"]["a.c"]} == {"target_first", "candidate_first"}
    assert [x["line"] for x in h["as_uploaded"]["a.c"]] == [5, 5]
    assert _hunks(r, 3)["hunks"] == {"landing": {}, "as_uploaded": {}}
    r = _check(gerrit, local, [t, c, d], focus={1})
    assert all("hunks" not in p for p in r["focus_pairs"])


def test_branch_moved_under_the_candidate_is_context(gerrit, local):
    old = gerrit.master
    gerrit.advance({"a.c": _text({6: "M"})})
    c = gerrit.change(2, old, {"a.c": _text({4: "C"})})
    t = gerrit.change(1, gerrit.master, {"a.c": _text({6: "M", 7: "T"})})
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c],
                        gerrit.owner, workers=2, focus={1}, hunks=True)
    kinds = {x["order"]: x["kind"] for x in _hunks(r, 2)["hunks"]["as_uploaded"]["a.c"]}
    assert kinds == {"target_on_theirs": "context"}
