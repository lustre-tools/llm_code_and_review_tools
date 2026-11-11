"""Focus mode of check_conflicts: one target stack against candidates."""

from gerrit_cli.conflicts import Patch
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
