"""Attention-rule engine tests: one scenario per rule, plus the traps."""

from conftest import ADILGER, AUTOTEST, JENKINS, MALOO, ME, mk_bundle, mk_change, msg, vote
from test_ci_parse import BUILD_FAIL, BUILD_OK, MALOO_ENUM, MALOO_FAIL, RETEST

from gerrit_dashboard.classify import build_snapshot
from gerrit_dashboard.config import Config

CFG = Config()

OTHER = {"_account_id": 1289, "name": "Xiyang Wang", "email": "xw@x.com", "username": "xw"}


def snap_for(*changes_roles, **kw):
    return build_snapshot(mk_bundle(list(changes_roles), **kw), CFG)


def only(snapshot, section, group=None):
    rows = snapshot[section] if group is None else snapshot[section][group]
    assert len(rows) == 1, f"expected 1 row in {section}/{group}, got {len(rows)}"
    return rows[0]


class TestMinePatches:
    def test_build_failed_is_p0_action(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_FAIL)], verified=[vote(JENKINS, -1)])
        s = snap_for((c, {"mine"}))
        r = only(s, "action")
        assert r["top_prio"] == 0
        assert "build failure" in r["top_reason"] or "build" in r["top_reason"]
        assert only(s, "mine", "failed")["number"] == c["_number"]
        assert s["kpis"]["action"] == 1

    def test_test_failed_is_p0(self):
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL)], verified=[vote(MALOO, -1)])
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "failed")
        assert r["top_prio"] == 0
        assert "review-dne-part-5" in r["top_reason"]

    def test_retest_pending_downgrades(self):
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL, date="2026-07-15 10:00:00.0"),
                                msg(AUTOTEST, RETEST, date="2026-07-15 12:00:00.0")],
                      verified=[vote(MALOO, -1)])
        s = snap_for((c, {"mine"}))
        assert s["action"] == []
        r = only(s, "mine", "in_ci")
        assert "retest" in r["top_reason"]

    def test_wip_parked(self):
        c = mk_change(wip=True, messages=[msg(JENKINS, BUILD_FAIL)])
        s = snap_for((c, {"mine"}))
        assert s["action"] == []
        assert only(s, "mine", "parked")

    def test_self_minus_one_parked_not_attention(self):
        c = mk_change(code_review=[vote(ME, -1)], messages=[msg(JENKINS, BUILD_OK)])
        s = snap_for((c, {"mine"}))
        assert s["action"] == []
        r = only(s, "mine", "parked")
        assert "parked by your own -1" in r["top_reason"]

    def test_reviewer_negative_is_p1(self):
        c = mk_change(code_review=[vote(ADILGER, -1)])
        s = snap_for((c, {"mine"}))
        r = only(s, "action")
        assert r["top_prio"] == 1
        assert "Andreas Dilger" in r["top_reason"]

    def test_feedback_threads_p1(self):
        c = mk_change()
        threads = {c["_number"]: {"my_turn": 2, "their_turn": 0, "sticky": 0, "bot": 0,
                                  "items": [{"kind": "my_turn", "author": "Andreas Dilger",
                                             "file": "a.c", "line": 5, "updated": "", "snippet": "hm"}]}}
        s = snap_for((c, {"mine"}), threads=threads)
        r = only(s, "action")
        assert r["top_prio"] == 1
        assert "await your reply" in r["top_reason"]

    def test_needs_reviewers_when_green_and_old(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_OK)],
                      verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                      submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "needs_reviewers")
        assert "add reviewers" in r["top_reason"]
        assert s["action"] == []  # P2, not in action list

    def test_in_ci_running(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_OK), msg(MALOO, MALOO_ENUM)])
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "in_ci")
        assert "testing in progress" in r["top_reason"]

    def test_landing_queue(self):
        c = mk_change(hashtags=["master-next"],
                      verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                      code_review=[vote(ADILGER, 2)],
                      submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])
        s = snap_for((c, {"mine"}))
        assert only(s, "mine", "landing")


class TestCarrying:
    def test_carried_failure_is_action(self):
        c = mk_change(owner=OTHER, uploader=ME,
                      messages=[msg(JENKINS, BUILD_FAIL)], verified=[vote(JENKINS, -1)])
        s = snap_for((c, {"carry"}))
        r = only(s, "action")
        assert r["top_prio"] == 0
        assert only(s, "carry", "failed")["owner"] == "Xiyang Wang"


class TestReviews:
    def test_re_review_lost_negative_is_p1(self):
        c = mk_change(owner=ADILGER, ps=14,
                      code_review=[dict(ME, value=0)],
                      messages=[msg(ME, "Patch Set 8: Code-Review-1\n\n(4 comments)", ps=8)])
        s = snap_for((c, {"review"}))
        r = only(s, "reviews", "re_review")
        assert r["top_prio"] == 1
        assert "PS8→PS14" in r["top_reason"]
        assert r in s["action"] or r["number"] in [x["number"] for x in s["action"]]

    def test_re_review_lost_positive_is_p2(self):
        c = mk_change(owner=ADILGER, ps=9,
                      messages=[msg(ME, "Patch Set 8: Code-Review+1", ps=8)])
        s = snap_for((c, {"review"}))
        r = only(s, "reviews", "re_review")
        assert r["top_prio"] == 2

    def test_requested_when_green(self):
        c = mk_change(owner=ADILGER,
                      verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                      submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])
        s = snap_for((c, {"review"}))
        assert only(s, "reviews", "requested")

    def test_later_when_not_green(self):
        c = mk_change(owner=ADILGER,
                      submit_records=[{"labels": [{"label": "Verified", "status": "NEED"}]}])
        s = snap_for((c, {"review"}))
        assert only(s, "reviews", "later")

    def test_done_when_voted_current(self):
        c = mk_change(owner=ADILGER, code_review=[vote(ME, 1)])
        s = snap_for((c, {"review"}))
        assert only(s, "reviews", "done")

    def test_carry_change_not_duplicated_in_reviews(self):
        c = mk_change(owner=OTHER, uploader=ME)
        s = snap_for((c, {"carry", "review"}))
        for g in s["reviews"].values():
            assert c["_number"] not in [r["number"] for r in g]


class TestWatchlist:
    def test_merged_watch_entry_flagged(self):
        c = mk_change(status="MERGED")
        s = snap_for((c, {"watch"}), watchlist=[{"number": c["_number"], "note": "after rebase"}])
        r = only(s, "watch")
        assert "removed from the watchlist" in r["top_reason"]
        assert r["watch_note"] == "after rebase"

    def test_watch_kpi(self):
        c = mk_change()
        s = snap_for((c, {"watch"}), watchlist=[{"number": c["_number"], "note": ""}])
        assert s["kpis"]["watch"] == 1


class TestSnapshotShape:
    def test_json_serializable_and_id_stable(self):
        import json
        c = mk_change(messages=[msg(JENKINS, BUILD_OK)])
        b1 = mk_bundle([(c, {"mine"})])
        s1 = build_snapshot(b1, CFG)
        json.dumps(s1)  # must not raise
        b2 = mk_bundle([(c, {"mine"})])
        b2["fetched_at"] += 1000
        s2 = build_snapshot(b2, CFG)
        assert s1["snapshot_id"] == s2["snapshot_id"]  # volatile fields excluded


class TestCalibrationRules:
    def test_unique_failure_reason_names_tests(self):
        from test_ci_parse import JANITOR_UNIQUE_BLOCK
        from conftest import JANITOR
        c = mk_change(ps=2, messages=[msg(JANITOR, JANITOR_UNIQUE_BLOCK, ps=2)])
        s = snap_for((c, {"mine"}))
        r = (s["action"] + s["action_old"])[0]
        item = next(i for i in r["items"] if i["rule"] == "unique-failure")
        assert "2 test failures unique to this patch" in item["reason"]
        assert "sanity-hsm@ldiskfs+DNE:test_254b" in item["reason"]
        assert r["janitor_alert"]

    def test_unique_failure_downgraded_when_retesting(self):
        from test_ci_parse import JANITOR_UNIQUE
        from conftest import JANITOR
        c = mk_change(messages=[msg(JANITOR, JANITOR_UNIQUE, date="2026-07-15 09:00:00.0"),
                                msg(MALOO, MALOO_FAIL, date="2026-07-15 10:00:00.0"),
                                msg(AUTOTEST, RETEST, date="2026-07-15 12:00:00.0")])
        s = snap_for((c, {"mine"}))
        assert s["action"] == [] and s["action_old"] == []
        r = only(s, "mine", "in_ci")
        assert r["top_prio"] == 2

    def test_old_failure_goes_to_longstanding(self):
        old = "2026-06-01 09:00:00.000000000"
        c = mk_change(messages=[msg(JENKINS, BUILD_FAIL, date=old)],
                      verified=[vote(JENKINS, -1)], updated=old)
        s = snap_for((c, {"mine"}))
        assert s["action"] == []
        assert len(s["action_old"]) == 1
        assert s["kpis"]["action"] == 0 and s["kpis"]["action_old"] == 1

    def test_fresh_failure_stays_in_action(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000000000")
        c = mk_change(messages=[msg(JENKINS, BUILD_FAIL, date=now)],
                      verified=[vote(JENKINS, -1)], updated=now)
        s = snap_for((c, {"mine"}))
        assert len(s["action"]) == 1


class TestReviewRoundFixes:
    def test_human_verified_veto_is_p0(self):
        c = mk_change(verified=[vote(JENKINS, 1), vote(ADILGER, -1)],
                      messages=[msg(JENKINS, BUILD_OK)])
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "failed")
        assert r["top_prio"] == 0
        assert "veto by Andreas Dilger" in r["top_reason"]

    def test_wip_reviewer_change_not_awaiting_review(self):
        c = mk_change(owner=ADILGER, wip=True)
        s = snap_for((c, {"review"}))
        assert s["reviews"]["later"] == [] and s["reviews"]["requested"] == []
        assert only(s, "reviews", "done")

    def test_snapshot_has_schema_and_no_volatile_in_hash(self):
        from gerrit_dashboard.classify import _strip_volatile
        from gerrit_dashboard.store import SNAPSHOT_SCHEMA
        c = mk_change(messages=[msg(JENKINS, BUILD_OK)])
        s = snap_for((c, {"mine"}))
        assert s["schema"] == SNAPSHOT_SCHEMA
        import json
        stripped = json.dumps(_strip_volatile(s))
        assert "ps_age" not in stripped and "updated_age" not in stripped


class TestByTagGrouping:
    def test_hashtag_clusters(self):
        c1 = mk_change(number=1, hashtags=["pt_ecro"])
        c2 = mk_change(number=2, hashtags=["pt_ecro", "zfs"])
        c3 = mk_change(number=3)
        s = snap_for((c1, {"mine"}), (c2, {"mine"}), (c3, {"mine"}))
        tags = s["mine_by_tag"]
        assert len(tags["pt_ecro"]) == 2
        assert [n["number"] for n in tags["zfs"]] == [2]
        assert [n["number"] for n in tags["(untagged)"]] == [3]
        assert list(tags)[-1] == "(untagged)"

    def test_master_next_not_a_tag(self):
        c = mk_change(hashtags=["master-next"])
        s = snap_for((c, {"mine"}))
        assert "master-next" not in s["mine_by_tag"]
        assert list(s["mine_by_tag"]) == ["(untagged)"]


class TestThreadBucketItems:
    def test_item_carries_ps_and_date(self):
        from gerrit_dashboard.fetcher import build_thread_buckets
        comments = {"lustre/a.c": [
            {"id": "x1", "patch_set": 3, "line": 12, "unresolved": True,
             "updated": "2026-07-15 10:00:00.000000000",
             "author": {"_account_id": 117, "name": "Andreas Dilger"},
             "message": "please fix this\nsecond line"},
        ]}
        b = build_thread_buckets(comments, 1055)
        assert b["my_turn"] == 1
        item = b["items"][0]
        assert item["ps"] == 3
        assert item["updated"].startswith("2026-07-15")
        assert item["snippet"] == "please fix this"
        assert item["conv"][0]["message"].startswith("please fix this")
        assert item["conv"][0]["author"] == "Andreas Dilger"


class TestMergedSection:
    def test_merged_rows_and_kpi(self):
        c1 = mk_change(number=1, status="MERGED", updated="2026-07-16 09:00:00.000000000")
        c2 = mk_change(number=2, status="MERGED", owner=OTHER, uploader=ME,
                       updated="2026-07-17 09:00:00.000000000")
        s = snap_for((c1, {"merged"}), (c2, {"merged"}))
        assert s["kpis"]["merged"] == 2
        assert [r["number"] for r in s["merged"]] == [2, 1]  # newest first
        # merged-only roles must not leak into the open sections
        for g in s["mine"].values():
            assert g == []

    def test_ci_ps_in_record(self):
        c = mk_change(ps=9, kind="NO_CODE_CHANGE",
                      messages=[msg(JENKINS, BUILD_OK, ps=8)])
        s = snap_for((c, {"mine"}))
        rows = [r for g in s["mine"].values() for r in g]
        assert rows[0]["ci_ps"] == 8 and rows[0]["ps"] == 9


class TestReadyToLand:
    GREEN = dict(verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                 submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])

    def test_all_green_is_ready_even_without_master_next(self):
        from conftest import ADILGER
        BZZZ = {"_account_id": 119, "name": "Alex Zhuravlev", "email": "bzzz@whamcloud.com"}
        c = mk_change(code_review=[vote(ADILGER, 1), vote(BZZZ, 1)], **self.GREEN)
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "landing")
        assert "ready to land" in r["top_reason"]
        assert s["action"] == []

    def test_green_backport_needs_one_plus_one(self):
        from conftest import ADILGER
        c = mk_change(code_review=[vote(ADILGER, 1)],
                      commit_message="LU-1 x\n\nLustre-change: https://x/1\nLustre-commit: abc\n",
                      **self.GREEN)
        s = snap_for((c, {"mine"}))
        assert only(s, "mine", "landing")

    def test_comments_not_blocking_when_review_green(self):
        from conftest import ADILGER
        BZZZ = {"_account_id": 119, "name": "Alex Zhuravlev", "email": "bzzz@whamcloud.com"}
        c = mk_change(code_review=[vote(ADILGER, 1), vote(BZZZ, 1)], **self.GREEN)
        threads = {c["_number"]: {"my_turn": 2, "their_turn": 0, "sticky": 0, "bot": 0,
                                  "items": [{"kind": "my_turn", "author": "Andreas Dilger",
                                             "file": "a.c", "line": 5, "updated": "", "snippet": "nit"}]}}
        s = snap_for((c, {"mine"}), threads=threads)
        assert s["action"] == [] and s["action_old"] == []
        r = only(s, "mine", "landing")
        assert any(i["rule"] == "comments-later" for i in r["items"])

    def test_comments_still_block_when_not_green(self):
        c = mk_change()  # no votes at all
        threads = {c["_number"]: {"my_turn": 1, "their_turn": 0, "sticky": 0, "bot": 0,
                                  "items": [{"kind": "my_turn", "author": "Andreas Dilger",
                                             "file": "a.c", "line": 5, "updated": "", "snippet": "hm"}]}}
        s = snap_for((c, {"mine"}), threads=threads)
        assert len(s["action"]) + len(s["action_old"]) == 1


class TestBranchLandingQueues:
    GREEN = dict(verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                 submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])

    def test_release_branch_queued_via_next_branch(self):
        c = mk_change(branch="release-1", project="private/example-release",
                      code_review=[vote(ADILGER, 1)],
                      commit_message="LU-1 x\n\nLustre-change: https://x/1\nLustre-commit: abc\n",
                      **self.GREEN)
        nq = {("private/example-release", "release-1"): {c["change_id"]}}
        s = build_snapshot(mk_bundle([(c, {"mine"})], next_queues=nq), CFG)
        r = only(s, "mine", "landing")
        assert "queued for landing (release-1-next)" in r["top_reason"]
        assert r["queued"] and r["queued_tag"] == "release-1-next"

    def test_release_branch_not_queued_mentions_right_tag(self):
        c = mk_change(branch="release-1", project="private/example-release",
                      code_review=[vote(ADILGER, 1)],
                      commit_message="LU-1 x\n\nLustre-change: https://x/1\nLustre-commit: abc\n",
                      **self.GREEN)
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "landing")
        assert "not in release-1-next yet" in r["top_reason"]

    def test_master_hashtag_still_works(self):
        BZZZ = {"_account_id": 119, "name": "Alex Zhuravlev", "email": "bzzz@whamcloud.com"}
        c = mk_change(hashtags=["master-next"],
                      code_review=[vote(ADILGER, 1), vote(BZZZ, 1)], **self.GREEN)
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "landing")
        assert r["queued"] and "master-next" in r["top_reason"]

    def test_landing_tag_not_a_category(self):
        c = mk_change(branch="release-1", project="private/example-release",
                      hashtags=["release-1-next", "csdc"])
        s = snap_for((c, {"mine"}))
        assert "release-1-next" not in s["mine_by_tag"]
        assert "csdc" in s["mine_by_tag"]


class TestReviewBotThreads:
    """AI review / Misc Code Checks comments are review content by
    policy, not CI noise — they bucket like human reviewer threads."""

    AIREVIEW = {"_account_id": 1345, "name": "Gerrit AI review for Lustre", "username": "aireview"}
    MISC = {"_account_id": 1400, "name": "Misc Code Checks Robot (Gatekeeper helper)",
            "tags": ["SERVICE_USER"]}
    JENKINS_ACC = {"_account_id": 683, "name": "jenkins", "username": "jenkins",
                   "tags": ["SERVICE_USER"]}

    def _bucket(self, author):
        from gerrit_dashboard.fetcher import build_thread_buckets
        comments = {"lustre/a.c": [
            {"id": "x1", "patch_set": 2, "line": 7, "unresolved": True,
             "updated": "2026-07-16 10:00:00.000000000", "author": author,
             "message": "this loop can overflow"},
        ]}
        return build_thread_buckets(comments, 1055)

    def test_aireview_thread_is_my_turn(self):
        b = self._bucket(self.AIREVIEW)
        assert b["my_turn"] == 1 and b["bot"] == 0
        assert b["items"][0]["author"] == "Gerrit AI review for Lustre"

    def test_misc_code_checks_thread_is_my_turn(self):
        b = self._bucket(self.MISC)
        assert b["my_turn"] == 1 and b["bot"] == 0

    def test_ci_bot_thread_still_bot(self):
        b = self._bucket(self.JENKINS_ACC)
        assert b["bot"] == 1 and b["my_turn"] == 0


class TestTonesAndStalled:
    GREEN = dict(verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                 submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])

    def test_ready_row_tone_ok_and_not_stalled(self):
        BZZZ = {"_account_id": 119, "name": "Alex Zhuravlev", "email": "bzzz@whamcloud.com"}
        from conftest import days_ago
        c = mk_change(code_review=[vote(ADILGER, 1), vote(BZZZ, 1)],
                      updated=days_ago(46), **self.GREEN)
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "landing")
        assert r["tone"] == "ok"
        assert not r["stalled"]  # 46d < 100d threshold

    def test_very_old_change_still_stalled(self):
        from conftest import days_ago
        c = mk_change(updated=days_ago(197))
        s = snap_for((c, {"mine"}))
        rows = [x for g in s["mine"].values() for x in g]
        assert rows[0]["stalled"]

    def test_failed_tone_p0_and_in_ci_tone_run(self):
        c1 = mk_change(number=1, messages=[msg(JENKINS, BUILD_FAIL)], verified=[vote(JENKINS, -1)])
        c2 = mk_change(number=2, messages=[msg(JENKINS, BUILD_OK), msg(MALOO, MALOO_ENUM)])
        s = snap_for((c1, {"mine"}), (c2, {"mine"}))
        assert only(s, "mine", "failed")["tone"] == "p0"
        assert only(s, "mine", "in_ci")["tone"] == "run"

    def test_merged_tone_ok(self):
        c = mk_change(status="MERGED")
        s = snap_for((c, {"merged"}))
        assert s["merged"][0]["tone"] == "ok"


class TestColorAndHeadlineFixes:
    GREEN = dict(verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                 submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])

    def test_selfhealed_unique_failure_hidden(self):
        from test_ci_parse import JANITOR_UNIQUE, MALOO_PASS
        from conftest import JANITOR
        c = mk_change(messages=[msg(JANITOR, JANITOR_UNIQUE),
                                msg(MALOO, MALOO_PASS)],
                      verified=[vote(MALOO, 1)])
        s = snap_for((c, {"mine"}))
        rows = [x for g in s["mine"].values() for x in g]
        assert all(i["rule"] != "unique-failure" for i in rows[0]["items"])
        # The template's badge/red-line styling keys on the same gate.
        assert not rows[0]["janitor_alert"]

    def test_retesting_unique_failure_hidden(self):
        from test_ci_parse import JANITOR_UNIQUE
        from conftest import JANITOR
        c = mk_change(messages=[msg(JANITOR, JANITOR_UNIQUE, date="2026-07-15 09:00:00.0"),
                                msg(MALOO, MALOO_FAIL, date="2026-07-15 10:00:00.0"),
                                msg(AUTOTEST, RETEST, date="2026-07-15 12:00:00.0")])
        s = snap_for((c, {"mine"}))
        rows = [x for g in s["mine"].values() for x in g]
        assert all(i["rule"] != "unique-failure" for i in rows[0]["items"])
        assert any(i["rule"] == "retest-running" for i in rows[0]["items"])

    def test_green_row_headlines_ready_not_comments(self):
        BZZZ = {"_account_id": 119, "name": "Alex Zhuravlev", "email": "bzzz@whamcloud.com"}
        c = mk_change(code_review=[vote(ADILGER, 1), vote(BZZZ, 1)], **self.GREEN)
        threads = {c["_number"]: {"my_turn": 2, "their_turn": 0, "sticky": 0, "bot": 0,
                                  "items": [{"kind": "my_turn", "author": "Sebastien Buisson",
                                             "file": "a.c", "line": 5, "updated": "", "snippet": "x"}]}}
        s = snap_for((c, {"mine"}), threads=threads)
        r = only(s, "mine", "landing")
        assert r["top_reason"].startswith("all green")
        assert r["tone"] == "ok"
        assert any(i["rule"] == "comments-later" for i in r["items"])

    def test_negative_vote_red_and_headline(self):
        TIM = {"_account_id": 931, "name": "Timothy Day", "email": "t@d.com"}
        c = mk_change(code_review=[vote(TIM, -1)])
        threads = {c["_number"]: {"my_turn": 1, "their_turn": 0, "sticky": 0, "bot": 0,
                                  "items": [{"kind": "my_turn", "author": "Timothy Day",
                                             "file": "a.c", "line": 5, "updated": "", "snippet": "x"}]}}
        s = snap_for((c, {"mine"}), threads=threads)
        r = only(s, "action")
        assert "Timothy Day voted -1" in r["top_reason"]
        assert r["tone"] == "p0"
        assert r["top_prio"] == 1  # ranking unchanged, only the color escalates


class TestBranchList:
    def test_branches_sorted_master_first(self):
        c1 = mk_change(number=1, branch="release-1", project="private/example-release")
        c2 = mk_change(number=2, branch="master")
        c3 = mk_change(number=3, branch="release-0", project="private/example-release")
        s = snap_for((c1, {"mine"}), (c2, {"mine"}), (c3, {"mine"}))
        assert [b["name"] for b in s["branches"]] == ["master", "release-0", "release-1"]
        assert all(b["count"] == 1 for b in s["branches"])


class TestNewestFirstSorting:
    def test_action_pure_updated_desc(self):
        # P1 updated yesterday must sort ABOVE P0 updated last week
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S.000000000"
        old_p0 = mk_change(number=1, messages=[msg(JENKINS, BUILD_FAIL,
                           date=(now - timedelta(days=5)).strftime(fmt))],
                           verified=[vote(JENKINS, -1)],
                           updated=(now - timedelta(days=5)).strftime(fmt))
        new_p1 = mk_change(number=2, code_review=[vote(ADILGER, -1)],
                           updated=(now - timedelta(days=1)).strftime(fmt))
        s = snap_for((old_p0, {"mine"}), (new_p1, {"mine"}))
        assert [r["number"] for r in s["action"]] == [2, 1]


class TestCcTab:
    def test_cc_only_row_listed(self):
        c = mk_change(owner=ADILGER)
        s = snap_for((c, {"cc"}))
        assert [r["number"] for r in s["cc"]] == [c["_number"]]
        # informational: no attention items, not in action
        assert s["action"] == [] and s["cc"][0]["top_prio"] == 5

    def test_cc_plus_carry_shows_in_carry_only(self):
        c = mk_change(owner=OTHER, uploader=ME)
        s = snap_for((c, {"cc", "carry"}))
        assert s["cc"] == []
        rows = [r for g in s["carry"].values() for r in g]
        assert rows


class TestCommitMessage:
    def test_commit_msg_in_record_for_free(self):
        c = mk_change(commit_message="LU-1 test: subject\n\nBody.\n\nChange-Id: Iabc\n")
        s = snap_for((c, {"mine"}))
        rows = [r for g in s["mine"].values() for r in g]
        assert "Change-Id: Iabc" in rows[0]["commit_msg"]


class TestPickedExtras:
    def test_size_bucket_and_fields(self):
        c = mk_change()
        c["insertions"], c["deletions"] = 200, 30
        c["topic"] = "ec-recovery"
        s = snap_for((c, {"mine"}))
        r = [x for g in s["mine"].values() for x in g][0]
        assert r["size_bucket"] == "M" and r["topic"] == "ec-recovery"

    def test_nudge_names_pending_reviewers(self):
        BZZZ = {"_account_id": 119, "name": "Alex Zhuravlev", "email": "b@w.com"}
        c = mk_change(verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                      code_review=[vote(BZZZ, 0)],
                      submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])
        s = snap_for((c, {"mine"}))
        r = only(s, "mine", "needs_reviewers")
        assert "no vote yet from Alex Zhuravlev" in r["top_reason"]


class TestRoleLabels:
    def test_mine(self):
        c = mk_change()
        s = snap_for((c, {"mine"}))
        assert [x for g in s["mine"].values() for x in g][0]["role"] == "mine"

    def test_carrying(self):
        c = mk_change(owner=OTHER, uploader=ME)
        s = snap_for((c, {"carry"}))
        assert [x for g in s["carry"].values() for x in g][0]["role"] == "carrying"

    def test_reviewing_and_cc(self):
        c1 = mk_change(number=1, owner=ADILGER)
        c2 = mk_change(number=2, owner=ADILGER)
        s = snap_for((c1, {"review"}), (c2, {"cc"}))
        assert [x for g in s["reviews"].values() for x in g][0]["role"] == "reviewing"
        assert s["cc"][0]["role"] == "cc"


class TestTabCountsAndCommunity:
    def test_mine_carry_kpis(self):
        c1 = mk_change(number=1)
        c2 = mk_change(number=2, owner=OTHER, uploader=ME)
        s = snap_for((c1, {"mine"}), (c2, {"carry"}))
        assert s["kpis"]["mine_open"] == 1 and s["kpis"]["carry_open"] == 1

    def test_community_config_parsing(self, monkeypatch):
        from gerrit_dashboard.config import Config
        monkeypatch.setenv("GD_COMMUNITY", "1")
        monkeypatch.delenv("GD_PROJECTS", raising=False)
        assert Config.from_env().projects == ["fs/lustre-release"]
        monkeypatch.setenv("GD_PROJECTS", "fs/lustre-release, fs/lustre-tests")
        assert Config.from_env().projects == ["fs/lustre-release", "fs/lustre-tests"]

    def test_project_clause_and_postfilter(self, tmp_path):
        from gerrit_dashboard.config import Config
        from gerrit_dashboard.fetcher import GerritFetcher
        from gerrit_dashboard.store import Store
        cfg = Config()
        cfg.projects = ["fs/lustre-release"]
        f = GerritFetcher(cfg, Store(tmp_path))
        assert f._project_clause() == " project:fs/lustre-release"
        cfg.projects = ["a", "b"]
        assert f._project_clause() == " (project:a OR project:b)"
        cfg.projects = []
        assert f._project_clause() == ""


class TestHiddenPatches:
    def test_hidden_out_of_action_and_kpis_but_in_group(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_FAIL)], verified=[vote(JENKINS, -1)])
        b = mk_bundle([(c, {"mine"})])
        b["hidden"] = {c["_number"]}
        s = build_snapshot(b, CFG)
        assert s["action"] == [] and s["action_old"] == []
        assert s["kpis"]["action"] == 0 and s["kpis"]["mine_open"] == 0
        assert s["kpis"]["hidden"] == 1
        r = only(s, "mine", "failed")  # still present in its group, marked
        assert r["hidden"] and r["top_prio"] == 0
