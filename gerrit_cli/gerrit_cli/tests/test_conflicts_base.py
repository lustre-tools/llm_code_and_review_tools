"""base_info and own_stack: rebase feasibility and the target's own stack."""

from gerrit_cli.conflicts import Repo, base_info, own_stack
from gerrit_cli.tests.test_graph_conflicts import _check, _text, gerrit, local  # noqa: F401


def _info(gerrit, local, patches, stack, tiers):
    r = _check(gerrit, local, patches, focus=set(stack))
    return base_info(Repo(local), r, {p.cn: p for p in patches}, stack, r["tip"], tiers)


def test_target_on_branch_is_rebasable(gerrit, local):
    t = gerrit.change(1, gerrit.master, {"a.c": _text({5: "T"})})
    info = _info(gerrit, local, [t], [1], {})
    assert info["on_branch"] is True
    assert info["rebase"] == "possible"
    assert info["parents"] == []


def test_ready_parent_blocks_rebase(gerrit, local):
    p = gerrit.change(1, gerrit.master, {"a.c": _text({2: "P"})})
    t = gerrit.change(2, p.commit, {"a.c": _text({2: "P", 15: "T"})})
    info = _info(gerrit, local, [p, t], [1, 2], {1: "frozen"})
    assert info["parents"] == [{"cn": 1, "tier": "frozen"}]
    assert info["rebase"] == "would move ready/queued parents"
    info = _info(gerrit, local, [p, t], [1, 2], {1: "same-effort"})
    assert info["rebase"] == "possible"


def test_behind_counts_branch_commits(gerrit, local):
    base = gerrit.master
    gerrit.advance({"b.c": _text({1: "m1"})})
    gerrit.advance({"b.c": _text({1: "m2"})})
    t = gerrit.change(1, base, {"a.c": _text({5: "T"})})
    info = _info(gerrit, local, [t], [1], {})
    assert info["behind"] == 2
    assert info["on_branch"] is True


def test_child_that_no_longer_applies_is_reported(gerrit, local):
    base = gerrit.master
    gerrit.advance({"a.c": _text({5: "master"})})
    t = gerrit.change(1, base, {"b.c": _text({2: "T"})})
    ch = gerrit.change(2, t.commit, {"a.c": _text({5: "C"})})
    r = _check(gerrit, local, [t, ch], focus={1})
    r["results"]["2"]["status"] = "conflict"
    own = own_stack(r, [1], [2])
    assert own["parents"] == []
    assert own["children"] == [{"cn": 2, "status": "conflict"}]
    assert own_stack(r, [3, 1], [])["parents"][0]["cn"] == 3
