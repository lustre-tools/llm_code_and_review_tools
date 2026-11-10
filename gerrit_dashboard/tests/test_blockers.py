"""Anything that stops a change landing is shown, whoever owns it and
whoever cast it -- a bot's Code-Review -1 included."""

from conftest import ADILGER, CHECKPATCH, JANITOR, JENKINS, MALOO, ME, mk_bundle, mk_change, msg, vote

from gerrit_dashboard.classify import build_snapshot
from gerrit_dashboard.config import Config

OTHER = {"_account_id": 1289, "name": "Xiyang Wang", "email": "xw@x.com", "username": "xw"}
LINT_BOT = {"_account_id": 990, "name": "Some Lint Bot", "username": "lintbot", "tags": ["SERVICE_USER"]}
GREEN = dict(verified=[vote(JENKINS, 1), vote(MALOO, 1)])
REBASE = ("Patch Set 2: Code-Review-1\n\nThis change cannot be cherry-picked to master.\n"
          "Please rebase the change locally and upload the rebased commit for review.")
STYLE = ("Patch Set 2: Code-Review-1\n\n(3 comments)\n\n"
         "ERROR: trailing whitespace in lustre/llite/file.c")


def record(change, roles):
    b = mk_bundle([(change, roles)], watchlist=[{"number": change["_number"], "note": ""}])
    s = build_snapshot(b, Config())
    pools = [s["action"], s["action_old"], s["watch"], s["cc"]]
    for sect in ("mine", "carry", "reviews"):
        pools += list(s[sect].values())
    return next(r for g in pools for r in g if r["number"] == change["_number"])


def rebase_change(**kw):
    return mk_change(owner=OTHER, messages=[msg(CHECKPATCH, REBASE)],
                     code_review=[vote(CHECKPATCH, -1)], **GREEN, **kw)


class TestOthersPatches:
    """The 67825 case: green CI, checkpatch -1 for a rebase, someone else's
    patch -- previously indistinguishable from a landable one."""

    def test_reviewer_sees_it_before_reviewing(self):
        r = record(rebase_change(), {"review"})
        assert [b["label"] for b in r["blockers"]] == ["needs rebase"]
        assert r["top_reason"] == "CI green — awaiting your review (needs rebase first)"

    def test_cc_and_watch_rows_say_it_is_blocked(self):
        for roles in ({"cc"}, {"watch"}):
            r = record(rebase_change(), roles)
            assert r["top_reason"].startswith("blocked on the owner: needs rebase")
            assert r["top_prio"] == 3          # information, not your action

    def test_any_bot_vote_counts(self):
        c = mk_change(owner=OTHER, messages=[msg(LINT_BOT, STYLE)],
                      code_review=[vote(LINT_BOT, -1)], **GREEN)
        r = record(c, {"cc"})
        assert r["blockers"][0]["label"] == "Some Lint Bot -1"
        assert "trailing whitespace" in r["top_reason"]   # the bot's reason, not "(3 comments)"

    def test_human_minus_two_blocks(self):
        c = mk_change(owner=OTHER, code_review=[vote(ADILGER, -2)], **GREEN)
        assert record(c, {"cc"})["blockers"][0]["label"] == "CR -2"

    def test_human_minus_one_blocks_whatever_else_there_is(self):
        c = mk_change(owner=OTHER, code_review=[vote(ADILGER, -1), vote(ME, 2)], **GREEN)
        assert [b["label"] for b in record(c, {"cc"})["blockers"]] == ["CR -1"]


class TestOwnPatches:
    def test_checkpatch_style_minus_one_is_p0(self):
        c = mk_change(messages=[msg(CHECKPATCH, STYLE)], code_review=[vote(CHECKPATCH, -1)], **GREEN)
        r = record(c, {"mine"})
        assert r["group"] == "failed" and r["top_prio"] == 0
        assert "wc-checkpatch voted -1: ERROR: trailing whitespace" in r["top_reason"]

    def test_rebase_is_reported_once(self):
        c = mk_change(messages=[msg(CHECKPATCH, REBASE)], code_review=[vote(CHECKPATCH, -1)], **GREEN)
        r = record(c, {"mine"})
        rules = [i["rule"] for i in r["items"]]
        assert rules.count("needs-rebase") == 1 and "bot-veto" not in rules

    def test_janitor_vote_keeps_its_own_rule(self):
        c = mk_change(messages=[msg(JANITOR, "Patch Set 2: Code-Review-1\n\nTesting has completed with errors!")],
                      code_review=[vote(JANITOR, -1)], **GREEN)
        assert "bot-veto" not in [i["rule"] for i in record(c, {"mine"})["items"]]


def test_merged_changes_have_no_blockers():
    c = mk_change(status="MERGED", messages=[msg(CHECKPATCH, STYLE)], code_review=[vote(CHECKPATCH, -1)])
    assert record(c, {"watch"})["blockers"] == []
