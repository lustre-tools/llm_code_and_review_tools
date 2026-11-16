"""Focus mode of check_conflicts: one target stack against candidates."""

import pytest

from gerrit_cli.conflicts import ConflictCheckError, Patch
from gerrit_cli.tests.test_graph_conflicts import _check, _text, gerrit, local  # noqa: F401


def _pair(r, other):
    return next(p for p in r["focus_pairs"] if p["other"] == other)


def test_focus_only_pairs_with_the_target(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    d = gerrit.change(3, gerrit.master, {"a.c": _text({5: "D"})})
    r = _check(gerrit, local, [t, c, d], focus={1})
    assert {(p["target"], p["other"]) for p in r["focus_pairs"]} == {(1, 2), (1, 3)}
    assert "pairs" not in r


def test_both_orders_are_reported(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    r = _check(gerrit, local, [t, c], focus={1})
    assert _pair(r, 2)["landing"] == {"target_first": "conflict",
                                      "candidate_first": "conflict", "files": ["a.c"]}
    assert _pair(r, 2)["via"] is None


def test_neither_hunks_nor_silent_checks_load_their_module(gerrit, local, monkeypatch):
    import sys

    # conflicting both ways and no --hunks: nothing needs conflict_hunks
    monkeypatch.setitem(sys.modules, "gerrit_cli.conflict_hunks", None)
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    r = _check(gerrit, local, [t, c], focus={1})
    assert _pair(r, 2)["landing"]["target_first"] == "conflict"


def test_clean_pairs_are_listed_clean(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({2: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({15: "C"})})
    r = _check(gerrit, local, [t, c], focus={1})
    assert _pair(r, 2)["landing"] == {"target_first": "clean",
                                      "candidate_first": "clean", "files": []}


def test_conflict_at_a_candidates_ancestor_names_via(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c1 = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    c2 = gerrit.change(3, c1.commit, {"a.c": _text({5: "C", 15: "C2"})})
    r = _check(gerrit, local, [t, c1, c2], focus={1})
    assert _pair(r, 3)["via"] == 2
    assert _pair(r, 3)["landing"]["target_first"] == "conflict"
    assert _pair(r, 2)["via"] is None


def test_a_candidate_that_does_not_apply_on_the_tip_is_already_conflicting(gerrit, local):
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(1, base, {"a.c": _text({15: "T"})})
    c = gerrit.change(2, base, {"a.c": _text({5: "C"})})
    r = _check(gerrit, local, [t, c], focus={1})
    assert r["already_conflicting"] == [2]
    assert r["focus_pairs"] == []


def test_an_unfetchable_candidate_is_an_error_not_a_crash(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    bad = Patch(3, 1, "1" * 40)
    r = _check(gerrit, local, [t, c, bad], focus={1})
    assert r["results"]["3"]["status"] == "error"
    assert [p["other"] for p in r["focus_pairs"]] == [2]


def test_focus_with_two_independent_tops_is_an_error(gerrit, local):
    a = gerrit.change(1, gerrit.master, {"a.c": _text({2: "A"})})
    b = gerrit.change(5, gerrit.master, {"a.c": _text({15: "B"})})
    with pytest.raises(ConflictCheckError, match="tops 1, 5"):
        _check(gerrit, local, [a, b], focus={1, 5})


def test_a_focus_stack_pairs_candidates_against_its_top(gerrit, local):
    t1 = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    t2 = gerrit.change(2, t1.commit, {"a.c": _text({5: "T", 15: "T2"})})
    c = gerrit.change(3, gerrit.master, {"a.c": _text({5: "C"})})
    r = _check(gerrit, local, [t1, t2, c], focus={1, 2})
    assert [(p["target"], p["other"]) for p in r["focus_pairs"]] == [(2, 3)]
    assert _pair(r, 3)["landing"] == {"target_first": "conflict",
                                      "candidate_first": "conflict", "files": ["a.c"]}


def test_stale_base_candidate_conflicts_only_as_uploaded(gerrit, local):
    old = gerrit.master
    gerrit.advance({"a.c": _text({6: "M"})})
    c = gerrit.change(2, old, {"a.c": _text({4: "C"})})
    t = gerrit.change(1, gerrit.master, {"a.c": _text({6: "M", 7: "T"})})
    p = _pair(_check(gerrit, local, [t, c], focus={1}), 2)
    assert p["landing"]["target_first"] == "clean"
    assert p["as_uploaded"]["target_on_theirs"] == "conflict"
    assert p["as_uploaded"]["files"] == ["a.c"]


def test_as_uploaded_is_clean_when_both_models_are_clean(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({2: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({15: "C"})})
    p = _pair(_check(gerrit, local, [t, c], focus={1}), 2)
    assert p["as_uploaded"] == {"target_on_theirs": "clean",
                                "theirs_on_target": "clean", "files": []}


def test_target_off_the_tip_is_still_tried_as_uploaded(gerrit, local):
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(1, base, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "master", 9: "C"})})
    d = gerrit.change(3, base, {"a.c": _text({5: "D"})})
    r = _check(gerrit, local, [t, c, d], focus={1})
    assert r["results"]["1"]["status"] == "conflict"
    assert _pair(r, 2)["landing"] == {"target_first": "skipped",
                                      "candidate_first": "skipped", "files": []}
    assert _pair(r, 2)["as_uploaded"]["target_on_theirs"] == "conflict"
    assert _pair(r, 3)["as_uploaded"] == {"target_on_theirs": "conflict",
                                          "theirs_on_target": "conflict", "files": ["a.c"]}
    assert _pair(r, 3)["silent"] == []
    assert r["already_conflicting"] == [3]


def test_a_patch_base_overrides_its_parent(gerrit, local):
    """A local target made of two commits: its own diff spans both."""
    first = gerrit.commit(gerrit.master, {"a.c": _text({5: "T"})}, "no change")
    top = gerrit.commit(first, {"a.c": _text({5: "T", 9: "T2"})}, "target")
    from gerrit_cli.tests.test_graph_conflicts import _git
    _git(gerrit.path, "update-ref", "refs/changes/01/1/1", top)
    t = Patch(1, 1, top, base=gerrit.master)
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    r = _check(gerrit, local, [t, c], focus={1})
    assert _pair(r, 2)["landing"]["target_first"] == "conflict"


def test_pair_with_limits_the_candidates(gerrit, local):
    from gerrit_cli.conflicts import Repo, check_conflicts
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    d = gerrit.change(3, gerrit.master, {"a.c": _text({5: "D"})})
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c, d], gerrit.owner,
                        workers=2, focus={1}, pair_with={3})
    assert [p["other"] for p in r["focus_pairs"]] == [3]


def test_hunks_for_a_set_of_candidates(gerrit, local):
    from gerrit_cli.conflicts import Repo, check_conflicts
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    d = gerrit.change(3, gerrit.master, {"a.c": _text({5: "D"})})
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c, d], gerrit.owner,
                        workers=2, focus={1}, hunks={3})
    assert "hunks" not in _pair(r, 2)
    assert _pair(r, 3)["hunks"]["landing"]["a.c"]


def test_as_uploaded_conflict_on_a_file_only_one_side_changes_is_base_drift(gerrit, local):
    old = gerrit.master
    gerrit.advance({"a.c": _text({6: "M"})})
    c = gerrit.change(2, old, {"b.c": _text({4: "C"})})
    t = gerrit.change(1, gerrit.master, {"a.c": _text({6: "M", 7: "T"})})
    p = _pair(_check(gerrit, local, [t, c], focus={1}), 2)
    assert p["as_uploaded"] == {"target_on_theirs": "clean",
                                "theirs_on_target": "clean", "files": []}
    assert p["base_drift"] == ["a.c"]
    assert p["landing"]["target_first"] == "clean"


def test_base_drift_is_left_out_of_as_uploaded_hunks(gerrit, local):
    from gerrit_cli.conflicts import Repo, check_conflicts
    old = gerrit.master
    gerrit.advance({"a.c": _text({6: "M"})})
    c = gerrit.change(2, old, {"b.c": _text({4: "C"})})
    t = gerrit.change(1, gerrit.master, {"a.c": _text({6: "M", 7: "T"})})
    r = check_conflicts(Repo(local), str(gerrit.path), "master", [t, c], gerrit.owner,
                        workers=2, focus={1}, hunks=True)
    assert _pair(r, 2)["hunks"]["as_uploaded"] == {}


def test_a_pair_without_drift_has_no_base_drift_key(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    c = gerrit.change(2, gerrit.master, {"a.c": _text({5: "C"})})
    assert "base_drift" not in _pair(_check(gerrit, local, [t, c], focus={1}), 2)


def test_conflict_in_a_target_parent_names_own_via(gerrit, local):
    """69268: the colliding line is in its parent 69267, not in its own diff."""
    t1 = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    t2 = gerrit.change(2, t1.commit, {"a.c": _text({5: "T"}), "b.c": _text({9: "T2"})})
    c = gerrit.change(3, gerrit.master, {"a.c": _text({5: "C"})})
    p = _pair(_check(gerrit, local, [t1, t2, c], focus={1, 2}), 3)
    assert p["landing"]["candidate_first"] == "conflict"
    assert p["own_via"] == 1
    assert p["via"] is None


def test_conflict_in_the_targets_own_diff_has_no_own_via(gerrit, local):
    t1 = gerrit.change(1, gerrit.master, {"b.c": _text({9: "T1"})})
    t2 = gerrit.change(2, t1.commit, {"a.c": _text({5: "T"}), "b.c": _text({9: "T1"})})
    c = gerrit.change(3, gerrit.master, {"a.c": _text({5: "C"})})
    p = _pair(_check(gerrit, local, [t1, t2, c], focus={1, 2}), 3)
    assert p["landing"]["candidate_first"] == "conflict"
    assert p["own_via"] is None
