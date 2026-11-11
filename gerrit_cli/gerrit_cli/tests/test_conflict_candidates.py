"""Candidate selection for gc conflicts (canned REST dicts)."""

from datetime import datetime
from urllib.parse import unquote

import pytest

from gerrit_cli.conflict_candidates import (
    classify, fix_side, query_candidates, readiness, release_rank,
    shared_hashtags,
)

NOW = datetime(2026, 11, 11, 12, 0, 0)
OWNER = {"_account_id": 1, "name": "Olive Owner", "username": "olive"}
BACKPORT_MSG = "x\n\nLustre-change: https://review/1\n"


def vote(value, name, aid=None, username=None):
    return {"value": value, "name": name, "_account_id": aid or hash(name) % 9999 + 10,
            "username": username or name.lower()}


def ch(cr=(), ver=(), tags=(), topic=None, updated="2026-11-10 10:00:00.000000000",
       cid="I1", number=1, msg="x\n"):
    return {
        "_number": number, "change_id": cid, "owner": OWNER, "hashtags": list(tags),
        "topic": topic, "updated": updated,
        "labels": {"Code-Review": {"all": list(cr)}, "Verified": {"all": list(ver)}},
        "current_revision": "abc",
        "revisions": {"abc": {"commit": {"message": msg}}},
    }


JEN_OK = vote(1, "jenkins")
MAL_OK = vote(1, "Maloo")
TARGET = {"hashtags": ["pt_ecro", "pw_own"], "topic": None}


def run(c, queued=()):
    return classify(c, TARGET, set(queued), 30, NOW)


def test_bot_minus_one_is_ignored():
    c = ch(cr=[vote(-1, "wc-checkpatch")])
    assert run(c) == ("other", None)


def test_owner_hold_minus_one_is_ignored():
    c = ch(cr=[{"value": -1, **OWNER}])
    assert run(c) == ("other", None)


@pytest.mark.parametrize("val", [-1, -2])
def test_reviewer_minus_one_skips_with_name(val):
    c = ch(cr=[vote(val, "Ronnie Sahlberg")])
    assert readiness(c) == "cr-1"
    assert run(c) == (None, f"Code-Review {val} (Ronnie Sahlberg)")


def test_jenkins_minus_one_skips():
    c = ch(ver=[vote(-1, "jenkins")])
    assert readiness(c) == "jenkins-1"
    assert run(c) == (None, "Jenkins -1")


def test_maloo_minus_one_is_kept_as_maloo_1():
    c = ch(ver=[JEN_OK, vote(-1, "Maloo")])
    assert readiness(c) == "maloo-1"
    assert run(c) == ("other", None)


def test_queued_change_is_frozen_even_with_minus_one():
    c = ch(cr=[vote(-1, "Ronnie Sahlberg")], ver=[vote(-1, "jenkins")],
           updated="2020-01-01 00:00:00.000000000")
    assert run(c, queued={"I1"}) == ("frozen", None)


def test_ready_needs_both_ci_and_two_non_owner_plus_ones():
    two = [vote(1, "A"), vote(1, "B")]
    assert readiness(ch(cr=two, ver=[JEN_OK, MAL_OK])) == "ready"
    assert readiness(ch(cr=two, ver=[JEN_OK])) == "pending"
    assert readiness(ch(cr=[vote(1, "A")], ver=[JEN_OK, MAL_OK])) == "pending"
    assert readiness(ch(cr=[vote(1, "A"), {"value": 1, **OWNER}],
                        ver=[JEN_OK, MAL_OK])) == "pending"
    assert readiness(ch(cr=[vote(1, "A")], ver=[JEN_OK, MAL_OK],
                        msg=BACKPORT_MSG)) == "ready"


def test_old_unready_change_is_stale():
    c = ch(updated="2026-09-02 10:00:00.000000000")
    assert run(c) == (None, "stale (updated 2026-09-02)")


def test_old_ready_change_is_lands_first():
    c = ch(cr=[vote(1, "A"), vote(1, "B")], ver=[JEN_OK, MAL_OK],
           updated="2026-01-01 00:00:00.000000000")
    assert run(c) == ("lands-first", None)
    c = ch(tags=["218_review_priority"], updated="2026-01-01 00:00:00.000000000")
    assert run(c) == ("lands-first", None)


def test_generic_hashtag_does_not_make_same_effort():
    assert run(ch(tags=["pw_own"])) == ("other", None)
    assert shared_hashtags(["pw_own", "fortestonly", "x_review_blocker"],
                           ["pw_own", "fortestonly", "x_review_blocker"]) == []


def test_shared_pt_tag_makes_same_effort():
    assert run(ch(tags=["pt_ecro"])) == ("same-effort", None)
    assert run(ch(topic="t1")) == ("other", None)
    assert classify(ch(topic="t1"), {"hashtags": [], "topic": "t1"},
                    set(), 30, NOW) == ("same-effort", None)


def test_release_rank():
    assert release_rank(["a_review_blocker", "b_review_priority"]) == 2
    assert release_rank(["b_review_priority"]) == 1
    assert release_rank(["x"]) == 0


TABLE = [
    ("frozen", "frozen", "coordinate"), ("frozen", "lands-first", "candidate"),
    ("frozen", "same-effort", "candidate"), ("frozen", "other", "candidate"),
    ("lands-first", "frozen", "target"), ("lands-first", "lands-first", "coordinate"),
    ("lands-first", "same-effort", "candidate"), ("lands-first", "other", "candidate"),
    ("pending", "frozen", "target"), ("pending", "lands-first", "target"),
    ("pending", "same-effort", "coordinate"), ("pending", "other", "candidate"),
]


@pytest.mark.parametrize("t,c,want", TABLE)
def test_fix_side_table(t, c, want):
    assert fix_side(t, 0, c, 0) == want


def test_fix_side_rank_and_unknown_target_tier():
    assert fix_side("lands-first", 1, "lands-first", 2) == "target"
    assert fix_side("lands-first", 2, "lands-first", 1) == "candidate"
    assert fix_side("lands-first", 1, "lands-first", 1) == "coordinate"
    assert fix_side("same-effort", 0, "frozen", 0) == "target"


class FakeRest:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url):
        self.urls.append(url)
        return self.pages(unquote(url))


class FakeClient:
    def __init__(self, pages):
        self.rest = FakeRest(pages)


def test_query_batches_paths_and_dedupes():
    ready = ch(cr=[vote(1, "A"), vote(1, "B")], ver=[JEN_OK, MAL_OK], number=5)
    pending = ch(number=6)
    prio = ch(number=7, tags=["x_review_priority"])
    fresh = ch(number=8)

    def pages(url):
        if "-age:30d" in url:
            if "&S=1" in url:
                return [fresh]
            return [dict(fresh, _more_changes=True)]
        return [ready, pending, prio]

    client = FakeClient(pages)
    paths = [f"lustre/a{i}.c" for i in range(25)] + ["lustre/b+c.h"]
    out = query_candidates(client, "fs/lustre-release", "master", paths, 30)
    urls = [unquote(u) for u in client.rest.urls]
    assert sorted(c["_number"] for c in out) == [5, 7, 8]
    assert out.count(ready) == 1
    assert any("file:\"^lustre/a0\\.c$\"" in u for u in urls)
    assert any("lustre/b\\+c\\.h" in u for u in urls)
    first = urls[0]
    assert first.count("file:") == 20 and " OR " in first
    assert all("project:fs/lustre-release branch:master status:open -is:wip -is:private" in u
               for u in urls)
    assert any("&S=1" in u for u in urls)
    for o in ("LABELS", "DETAILED_LABELS", "CURRENT_REVISION",
              "CURRENT_COMMIT", "DETAILED_ACCOUNTS"):
        assert f"o={o}" in first
    assert "&n=500" in first


def test_empty_paths_no_request():
    client = FakeClient(lambda u: [])
    assert query_candidates(client, "p", "b", [], 30) == []
    assert client.rest.urls == []
