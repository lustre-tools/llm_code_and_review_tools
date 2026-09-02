"""ci_parse against verbatim message shapes from review.whamcloud.com."""

from conftest import AUTOTEST, CHECKPATCH, JANITOR, JENKINS, MALOO, ME, mk_change, msg, vote

from gerrit_dashboard import ci_parse

BUILD_OK = "Patch Set 2: Verified+1\n\nBuild Successful \n\nhttps://build.whamcloud.com/job/lustre-reviews/127545/ : SUCCESS"
BUILD_FAIL = "Patch Set 2: Verified-1\n\nBuild Failed \n\nhttps://build.whamcloud.com/job/lustre-reviews/126400/ : FAILURE"
BUILD_ABORT = "Patch Set 2: Verified-1\n\nBuild Failed \n\nhttps://build.whamcloud.com/job/lustre-reviews/127542/ : ABORTED"
BUILD_START = "Patch Set 2:\n\nBuild Started https://build.whamcloud.com/job/lustre-reviews/127545/"
MALOO_FAIL = ("Patch Set 2:\n\nFailed enforced test review-dne-part-5 on RHEL 9.7 / x86_64 uploaded by "
              "Trevis Autotest 2 from trevis-154vm123: "
              "https://testing.whamcloud.com/test_sessions/1d22a934-ff89-4500-94e9-e7e607cd2706 "
              "ran 5 tests.  1 tests failed: sanityn.")
MALOO_PASS = ("Patch Set 2: Verified+1\n\nPassed enforced test review-dne-part-3 on CentOS 8.10 / x86_64 uploaded by "
              "Trevis Autotest from trevis-42vm1: "
              "https://testing.whamcloud.com/test_sessions/5cd6adea-0000-0000-0000-000000000000 ran 7 tests.")
MALOO_OPTIONAL_FAIL = ("Patch Set 2:\n\nFailed optional test review-ldiskfs-dne-arm on RHEL 9.7 / aarch64 uploaded by "
                       "Onyx Autotest from onyx-1: "
                       "https://testing.whamcloud.com/test_sessions/aaaabbbb-0000-0000-0000-000000000000 ran 3 tests.")
MALOO_ENUM = ("Patch Set 2:\n\n#### The following sessions will be run for Build 127545 (patch #2):\n\n"
              "#### Enforced:\n\n- review-zfs on el8.10-x86_64/...\n\n"
              "Maloo Test Queue: https://testing.whamcloud.com/test_queue?jobs=lustre-reviews&builds=127545&commit=Apply+Filter\n\n"
              "Maloo Results: https://testing.whamcloud.com/test_sessions/related?jobs=lustre-reviews&builds=127545#redirect")
RETEST = ("Patch Set 2:\n\nTrevis Autotest 2 retesting enforced review-dne-part-5 session on "
          "el9.7-x86_64/ldiskfs servers and clients. Requested by Marc Vef because of LU-9827")
# Real-world shape: a group fails, the requested retest passes and Maloo
# posts "Passed enforced" for the same group+platform, while other groups
# keep its vote at -1.
FAIL_DNE1 = ("Patch Set 2:\n\nFailed enforced test review-dne-part-1 on Rocky 10.0 / x86_64 uploaded by "
             "Onyx Autotest 2 from onyx-144vm15: "
             "https://testing.whamcloud.com/test_sessions/580670e6-8b8b-480c-89b2-6d9729cceb97 "
             "ran 3 tests.  1 tests failed: sanity.")
RETEST_DNE1 = ("Patch Set 2:\n\nTrevis Autotest retesting enforced review-dne-part-1 session on "
               "rocky10.0-x86_64/ldiskfs servers and clients for build 29651 patch 2. "
               "Requested by Marc Vef because of LU-20172.")
PASS_DNE1 = ("Patch Set 2:\n\nPassed enforced test review-dne-part-1 on Rocky 10.0 / x86_64 uploaded by "
             "Trevis Autotest from trevis-155vm6: "
             "https://testing.whamcloud.com/test_sessions/33dd729f-42f8-4f9c-a408-6fb89f742255 ran 3 tests.")
FAIL_ALT = ("Patch Set 2:\n\nFailed enforced test review-part-1 on Rocky 10.0 / x86_64, Rocky 9.7 / x86_64 "
             "uploaded by Onyx Autotest 2 from onyx-158vm21: "
             "https://testing.whamcloud.com/test_sessions/f993b7d4-92ad-45f9-b61f-be540875ef81 "
             "ran 6 tests.  1 tests failed: sanity-pcc.")
CHECKPATCH_REBASE = ("Patch Set 24: Code-Review-1\n\nThis change cannot be cherry-picked to master. "
                     "Please rebase the change locally and upload")
JANITOR_UNIQUE = ("Patch Set 1: Code-Review-1\n\nTesting has completed with errors!\n\n"
                  "Newly added or changed test failed in initial testing: IMPORTANT: these tests appear to be "
                  "new failures unique to this patch - sanity2@ldiskfs+DNE:test_84(NEW unique failure)")
# Current janitor format: unique-failure block with one line per test,
# then '>' failing-config lines with multi-line crash details.
JANITOR_UNIQUE_BLOCK = (
    "Patch Set 2:\n\nTesting has completed with errors!\n"
    "IMPORTANT: these tests appear to be new failures unique to this patch\n"
    "- sanity-hsm@ldiskfs+DNE:test_254b(NEW unique failure for this branch in the last 30 days, "
    "and was seen 0 times across 0 other branches 0 reviews)\n"
    "- sanity-hsm@zfs:test_254b(NEW unique failure for this branch in the last 30 days, "
    "and was seen 0 times across 0 other branches 0 reviews)\n"
    "\n\n"
    "> replay-dual@ldiskfs+DNE Timeout(332s)\n"
    "- (Crash processing failed with code 1 stdout: Usage: ./scripts/extract_crash_data.sh builddir corefile distro arch\n"
    " stderr: )(Usage: ./scripts/extract_crash_data.sh builddir corefile distro arch\n)\n"
    "> replay-dual@zfs Timeout(3234s)\n"
    "> sanity-hsm@ldiskfs+DNE Failure(7023s)\n"
    "- 251(request on 0x200000bd3:0x2e:0x0 is not STARTED on mds1) 254b(Expected 32 (!= 0) active archive requests) \n"
    "> sanity-hsm@zfs Failure(6296s)\n"
    "- 251(request on 0x200000bd3:0x34:0x0 is not STARTED on mds1) 254b(Expected 50 (!= 0) active archive requests) \n"
    "\nSucceeded:\n- runtests@ldiskfs+DNE runtests@zfs \n\n"
    "(rocky8.10)All results and logs: https://testing.whamcloud.com/gerrit-janitor/67449/results.html")
# A unique failure that is really a known flake elsewhere (65 reviews).
JANITOR_UNIQUE_SEEN = (
    "Patch Set 1:\n\nInitial testing failed:\n"
    "IMPORTANT: these tests appear to be new failures unique to this patch\n"
    "- sanity-quota@ldiskfs+DNE:test_80(Seen in reviews: 67728 67389 66587 66421 66252 65342 65094 "
    "65064 64567 64318 64297 63809 63707 63132 62853 62689 62591 62450 61943 61826 61767 61258 61001 "
    "60766 60715 60586 59172 58508 52231 52229 52228 52227 52226 52208 52207 52206 52205 52204 52203 "
    "52189 52188 52187 52182 52167 52166 52165 52164 52162 52161 52160 52159 52141 52140 52139 52136 "
    "52113 52111 52110 52109 52009 51236 50841 50525 50168 32038)\n"
    "\n\n"
    "> sanity-quota@ldiskfs+DNE Failure(10809s)\n- 80(write failed) \n\n"
    "Succeeded:\n- runtests@ldiskfs+DNE runtests@zfs \n\n"
    "(rocky8.10)All results and logs: https://testing.whamcloud.com/gerrit-janitor/67449/results.html")
# Run failed, but nothing unique to the patch.
JANITOR_ERRORS = (
    "Patch Set 1:\n\nTesting has completed with errors!\n\n"
    "> racer@ldiskfs+DNE Client crashed(265s)\n- (Untriaged #4262, seen 4 times before)\n"
    "> sanity-ec@zfs Failure(466s)\n- 5b(mirror resync failed) \n\n"
    "Succeeded:\n- conf-sanity1@ldiskfs+DNE conf-sanity1@zfs \n\n"
    "(rocky8.10)All results and logs: https://testing.whamcloud.com/gerrit-janitor/66757/results.html")
JANITOR_FINAL_OK = (
    "Patch Set 3:\n\nTesting has completed Successfully\n\n"
    "Succeeded:\n- conf-sanity1@ldiskfs+DNE conf-sanity1@zfs \n\n"
    "(rocky8.10)All results and logs: https://testing.whamcloud.com/gerrit-janitor/66700/results.html")
JANITOR_COMPILE_FAIL = (
    "Patch Set 1:\n\n(2 comments)\n\nrocky8.10: Compile failed\n\nrocky9.6: Compile failed\n\n\n"
    " Job output URL: https://testing.whamcloud.com/gerrit-janitor/62555/results.html")
JANITOR_CRASH_NOTE = (
    "Patch Set 1:\n\n(1 comment)\n\nCrash (id 4262 seen 4) in racer@ldiskfs+DNE\n"
    "- Failed run: https://testing.whamcloud.com/gerrit-janitor/66757/testresults/racer-ldiskfs-DNE-rocky8.10_x86_64")


class TestParseBuild:
    def test_success(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_OK)])
        b = ci_parse.parse_build(c)
        assert b["state"] == "SUCCESS"
        assert b["url"] == "https://build.whamcloud.com/job/lustre-reviews/127545"
        assert b["number"] == "127545"

    def test_failure(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_FAIL)])
        assert ci_parse.parse_build(c)["state"] == "FAILURE"

    def test_aborted(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_ABORT)])
        assert ci_parse.parse_build(c)["state"] == "ABORTED"

    def test_building(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_START)])
        assert ci_parse.parse_build(c)["state"] == "BUILDING"

    def test_latest_wins(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_FAIL, date="2026-07-14 09:00:00.0"),
                                msg(JENKINS, BUILD_OK, date="2026-07-15 09:00:00.0")])
        assert ci_parse.parse_build(c)["state"] == "SUCCESS"

    def test_old_patchset_ignored(self):
        c = mk_change(ps=3, messages=[msg(JENKINS, BUILD_FAIL, ps=2)])
        assert ci_parse.parse_build(c)["state"] == "NONE"


class TestParseTests:
    def test_enforced_fail(self):
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL)],
                      verified=[vote(MALOO, -1)])
        t = ci_parse.parse_tests(c)
        assert t["state"] == "FAIL"
        assert t["enforced_fail"] == 1
        ft = t["failed_tests"][0]
        assert ft["name"] == "review-dne-part-5"
        assert ft["failures"][0]["platform"].startswith("RHEL 9.7")
        assert "1d22a934" in ft["failures"][0]["url"]
        assert "1 tests failed: sanityn" in ft["failures"][0]["detail"]
        assert not ft["retest_pending"]

    def test_pass_vote(self):
        c = mk_change(messages=[msg(MALOO, MALOO_PASS)], verified=[vote(MALOO, 1)])
        t = ci_parse.parse_tests(c)
        assert t["state"] == "PASS"
        assert t["enforced_pass"] == 1

    def test_running_after_enumeration(self):
        c = mk_change(messages=[msg(MALOO, MALOO_ENUM)])
        assert ci_parse.parse_tests(c)["state"] == "RUNNING"

    def test_enumeration_links(self):
        c = mk_change(messages=[msg(MALOO, MALOO_ENUM)])
        t = ci_parse.parse_tests(c)
        assert t["results_url"] == ("https://testing.whamcloud.com/test_sessions/related"
                                    "?jobs=lustre-reviews&builds=127545#redirect")
        assert t["queue_url"] == ("https://testing.whamcloud.com/test_queue"
                                  "?jobs=lustre-reviews&builds=127545&commit=Apply+Filter")

    def test_enumeration_links_newest_build_wins(self):
        rerun = MALOO_ENUM.replace("127545", "127600")
        c = mk_change(messages=[msg(MALOO, MALOO_ENUM, date="2026-08-01 10:00:00.0"),
                                msg(MALOO, rerun, date="2026-08-02 10:00:00.0")])
        assert "127600" in ci_parse.parse_tests(c)["results_url"]

    def test_enumeration_without_links(self):
        bare = MALOO_ENUM.split("\n\nMaloo Test Queue")[0]
        c = mk_change(messages=[msg(MALOO, bare)])
        t = ci_parse.parse_tests(c)
        assert t["results_url"] == ""
        assert t["queue_url"] == ""

    def test_retest_pending(self):
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL, date="2026-07-15 10:00:00.0"),
                                msg(AUTOTEST, RETEST, date="2026-07-15 12:00:00.0")])
        t = ci_parse.parse_tests(c)
        assert t["failed_tests"][0]["retest_pending"]
        assert t["all_failures_retesting"]

    def test_retest_expired_by_second_failure(self):
        # fail -> retest -> fail again: the retest ran and failed, so it
        # is no longer "pending" and the failure must count again.
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL, date="2026-07-14 10:00:00.0"),
                                msg(AUTOTEST, RETEST, date="2026-07-14 12:00:00.0"),
                                msg(MALOO, MALOO_FAIL, date="2026-07-15 09:00:00.0")],
                      verified=[vote(MALOO, -1)])
        t = ci_parse.parse_tests(c)
        assert not t["failed_tests"][0]["retest_pending"]
        assert not t["all_failures_retesting"]
        assert t["state"] == "FAIL"

    def test_pass_after_retest_heals_failure(self):
        # Real-world case: review-dne-part-1 failed, the
        # requested retest passed and Maloo posted "Passed enforced" for
        # the same group+platform — but the vote stays -1 because other
        # groups still fail.  The healed group must drop out of
        # failed_tests and the failure count.
        c = mk_change(messages=[
                msg(MALOO, FAIL_ALT, date="2026-08-01 02:19:52.0"),
                msg(MALOO, FAIL_DNE1, date="2026-08-01 04:38:28.0"),
                msg(AUTOTEST, RETEST_DNE1, date="2026-08-01 08:45:16.0"),
                msg(MALOO, PASS_DNE1, date="2026-08-01 13:43:35.0")],
            verified=[vote(MALOO, -1)])
        t = ci_parse.parse_tests(c)
        assert t["state"] == "FAIL"
        assert [ft["name"] for ft in t["failed_tests"]] == ["review-part-1"]
        assert t["enforced_fail"] == 1
        assert t["enforced_pass"] == 1

    def test_pass_on_other_platform_does_not_heal(self):
        other = PASS_DNE1.replace("Rocky 10.0 / x86_64", "Rocky 9.7 / aarch64")
        c = mk_change(messages=[
                msg(MALOO, FAIL_DNE1, date="2026-08-01 04:38:28.0"),
                msg(MALOO, other, date="2026-08-01 13:43:35.0")],
            verified=[vote(MALOO, -1)])
        t = ci_parse.parse_tests(c)
        assert t["enforced_fail"] == 1
        assert t["failed_tests"][0]["name"] == "review-dne-part-1"

    def test_pass_before_fail_does_not_heal(self):
        # Newest verdict wins in both directions: a pass followed by a
        # newer failure on the same platform is still a failure.
        c = mk_change(messages=[
                msg(MALOO, PASS_DNE1, date="2026-08-01 04:38:28.0"),
                msg(MALOO, FAIL_DNE1, date="2026-08-01 13:43:35.0")],
            verified=[vote(MALOO, -1)])
        t = ci_parse.parse_tests(c)
        assert t["enforced_fail"] == 1
        assert t["failed_tests"][0]["name"] == "review-dne-part-1"

    def test_optional_failure_not_blocking(self):
        c = mk_change(messages=[msg(MALOO, MALOO_OPTIONAL_FAIL)], verified=[vote(MALOO, 1)])
        t = ci_parse.parse_tests(c)
        assert t["state"] == "PASS"
        assert t["optional_fail"] == 1

    def test_fail_beats_missing_vote(self):
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL)])
        assert ci_parse.parse_tests(c)["state"] == "FAIL"


class TestSignals:
    def test_needs_rebase(self):
        c = mk_change(ps=24, messages=[msg(CHECKPATCH, CHECKPATCH_REBASE, ps=24)])
        assert ci_parse.parse_signals(c)["needs_rebase"]

    def test_unique_failure(self):
        c = mk_change(ps=1, messages=[msg(JANITOR, JANITOR_UNIQUE, ps=1)])
        assert ci_parse.parse_signals(c)["unique_failure"]

    def test_clean(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_OK)])
        s = ci_parse.parse_signals(c)
        assert not s["needs_rebase"] and not s["unique_failure"]


class TestVerifiedGate:
    def test_submit_record_ok_alone_is_not_green(self):
        # Gerrit's DefaultSubmitRule says OK on ANY single +1 (e.g. jenkins
        # while tests still run) — the workflow gate must not trust it.
        c = mk_change(verified=[vote(JENKINS, 1)],
                      submit_records=[{"labels": [{"label": "Verified", "status": "OK"},
                                                  {"label": "Code-Review", "status": "NEED"}]}])
        assert ci_parse.verified_gate(c) == "PENDING"

    def test_submit_record_ok_with_both_bots(self):
        c = mk_change(verified=[vote(JENKINS, 1), vote(MALOO, 1)],
                      submit_records=[{"labels": [{"label": "Verified", "status": "OK"}]}])
        assert ci_parse.verified_gate(c) == "OK"

    def test_submit_record_reject(self):
        c = mk_change(submit_records=[{"labels": [{"label": "Verified", "status": "REJECT"}]}])
        assert ci_parse.verified_gate(c) == "FAIL"

    def test_submit_record_need(self):
        c = mk_change(submit_records=[{"labels": [{"label": "Verified", "status": "NEED"}]}])
        assert ci_parse.verified_gate(c) == "PENDING"

    def test_fallback_both_bots(self):
        c = mk_change(verified=[vote(JENKINS, 1), vote(MALOO, 1)])
        assert ci_parse.verified_gate(c) == "OK"

    def test_trusted_human_substitutes_for_missing_test_vote(self):
        # Only for the configured emails; nobody by default.
        c = mk_change(verified=[vote(JENKINS, 1), vote(ME, 1)])
        assert ci_parse.verified_gate(c) == "PENDING"
        assert ci_parse.verified_gate(c, [ME["email"]]) == "OK"
        assert ci_parse.verified_gate(c, ["someone-else@example.com"]) == "PENDING"

    def test_fallback_negative(self):
        c = mk_change(verified=[vote(JENKINS, 1), vote(MALOO, -1)])
        assert ci_parse.verified_gate(c) == "FAIL"


class TestVotesAndHistory:
    def test_my_prior_vote_found(self):
        c = mk_change(ps=14, messages=[
            msg(ME, "Patch Set 8: Code-Review-1\n\n(4 comments)", ps=8, date="2026-06-01 10:00:00.0"),
        ])
        prior = ci_parse.my_prior_vote(c, 1055)
        assert prior == {"ps": 8, "value": -1, "date": "2026-06-01 10:00:00.0"}

    def test_my_prior_vote_ignores_current_ps(self):
        c = mk_change(ps=8, messages=[msg(ME, "Patch Set 8: Code-Review+1", ps=8)])
        assert ci_parse.my_prior_vote(c, 1055) is None

    def test_human_votes_exclude_bots(self):
        from conftest import ADILGER
        c = mk_change(code_review=[vote(ADILGER, 1), vote(CHECKPATCH, -1)])
        votes = ci_parse.human_review_votes(c)
        assert [v["name"] for v in votes] == ["Andreas Dilger"]

    def test_is_bot_service_user_tag(self):
        assert ci_parse.is_bot(MALOO)
        assert not ci_parse.is_bot(ME)


class TestCalibration:
    """Regressions found during first live run."""

    def test_maloo_plus_one_wins_over_selfhealed_failures(self):
        # Enforced fail messages linger after auto-retest passes; Maloo's
        # final +1 is the aggregate verdict.
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL), msg(MALOO, MALOO_PASS)],
                      verified=[vote(MALOO, 1)])
        t = ci_parse.parse_tests(c)
        assert t["state"] == "PASS"
        assert t["enforced_fail"] == 1  # history retained for display

    def test_janitor_cleared_by_later_success(self):
        c = mk_change(ps=1, messages=[
            msg(JANITOR, JANITOR_UNIQUE, ps=1, date="2026-07-10 10:00:00.0"),
            msg(JANITOR, "Patch Set 1:\n\nInitial testing succeeded", ps=1,
                date="2026-07-11 10:00:00.0"),
        ])
        assert not ci_parse.parse_signals(c)["unique_failure"]

    def test_is_bot_by_name_username_tag(self):
        assert ci_parse.is_bot({"name": "Gerrit AI review for Lustre"})
        assert ci_parse.is_bot({"username": "aireview", "name": "Whatever"})
        assert ci_parse.is_bot({"name": "X", "tags": ["SERVICE_USER"]})
        assert not ci_parse.is_bot({"name": "Andreas Dilger", "username": "adilger"})


class TestReviewFixes:
    """Regressions from the adversarial review round."""

    def test_checkpatch_allclear_clears_needs_rebase(self):
        c = mk_change(ps=1, messages=[
            msg(CHECKPATCH, "Patch Set 1: Code-Review-1\n\nThis change cannot be cherry-picked to master.",
                ps=1, date="2026-07-10 10:00:00.0"),
            msg(CHECKPATCH, "Patch Set 1: -Code-Review\n\nLooks good to me.",
                ps=1, date="2026-07-10 11:00:00.0"),
        ])
        assert not ci_parse.parse_signals(c)["needs_rebase"]

    def test_build_falls_back_to_copied_vote(self):
        # NO_CODE_CHANGE patchset: vote copied, no jenkins message on new PS.
        c = mk_change(ps=3, messages=[msg(JENKINS, BUILD_OK, ps=2)],
                      verified=[vote(JENKINS, 1)])
        assert ci_parse.parse_build(c)["state"] == "SUCCESS"
        c = mk_change(ps=3, messages=[], verified=[vote(JENKINS, -1)])
        assert ci_parse.parse_build(c)["state"] == "FAILURE"

    def test_prior_vote_removal_clears_it(self):
        c = mk_change(ps=10, messages=[
            msg(ME, "Patch Set 8: Code-Review-1", ps=8, date="2026-06-01 10:00:00.0"),
            msg(ME, "Patch Set 8: -Code-Review", ps=8, date="2026-06-02 10:00:00.0"),
        ])
        assert ci_parse.my_prior_vote(c, 1055) is None

    def test_verified_negatives_lists_human_veto(self):
        from conftest import ADILGER
        c = mk_change(verified=[vote(JENKINS, 1), vote(ADILGER, -1)])
        negs = ci_parse.verified_negatives(c)
        assert [n["name"] for n in negs] == ["Andreas Dilger"]
        assert ci_parse.verified_gate(c) == "FAIL"

    def test_maloo_plus_one_clears_retest_pending(self):
        # Real-world case: enforced fail -> retest requested -> NO
        # "Passed enforced" message for that group ever appears, Maloo just
        # votes +1 (the aggregate verdict). +1 supersedes retest bookkeeping.
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL, date="2026-07-14 16:27:58.0"),
                                msg(AUTOTEST, RETEST, date="2026-07-14 17:35:32.0"),
                                msg(MALOO, MALOO_PASS, date="2026-07-14 23:14:17.0")],
                      verified=[vote(MALOO, 1)])
        t = ci_parse.parse_tests(c)
        assert t["state"] == "PASS"
        assert not t["all_failures_retesting"]
        assert all(not ft["retest_pending"] for ft in t["failed_tests"])


class TestCiRelevantPs:
    """Commit-message-only patchsets copy votes and do NOT
    re-run CI — build/test state must come from the last tested PS."""

    def test_msg_only_ps_uses_previous_ci(self):
        c = mk_change(ps=9, kind="NO_CODE_CHANGE",
                      messages=[msg(JENKINS, BUILD_OK, ps=8),
                                msg(MALOO, MALOO_FAIL.replace("Patch Set 2", "Patch Set 8"), ps=8)],
                      verified=[vote(JENKINS, 1), vote(MALOO, -1)])
        assert ci_parse.ci_relevant_ps(c) == 8
        b = ci_parse.parse_build(c)
        assert b["state"] == "SUCCESS" and b["number"] == "127545"
        t = ci_parse.parse_tests(c)
        assert t["state"] == "FAIL"
        assert t["failed_tests"][0]["name"] == "review-dne-part-5"

    def test_rework_ignores_old_ps(self):
        c = mk_change(ps=9, kind="REWORK", messages=[msg(JENKINS, BUILD_OK, ps=8)])
        assert ci_parse.ci_relevant_ps(c) == 9
        assert ci_parse.parse_build(c)["state"] == "NONE"

    def test_checkpatch_current_ps_wins_on_msg_only(self):
        # checkpatch re-runs on message-only uploads: its fresh verdict on
        # the new PS overrides the old one.
        c = mk_change(ps=9, kind="NO_CODE_CHANGE", messages=[
            msg(CHECKPATCH, "Patch Set 8: Code-Review-1\n\nThis change cannot be cherry-picked to master.", ps=8),
            msg(JENKINS, BUILD_OK, ps=8),
            msg(CHECKPATCH, "Patch Set 9: -Code-Review\n\nLooks good to me.", ps=9),
        ])
        assert not ci_parse.parse_signals(c)["needs_rebase"]

    def test_no_bot_messages_falls_back_to_current(self):
        c = mk_change(ps=3, kind="NO_CODE_CHANGE", messages=[])
        assert ci_parse.ci_relevant_ps(c) == 3


class TestRepeatedSessionFailures:
    def test_same_session_collapsed_to_newest(self):
        fail1 = MALOO_FAIL  # RHEL 9.7 / x86_64, session 1d22a934
        fail2 = MALOO_FAIL.replace("1d22a934-ff89-4500-94e9-e7e607cd2706",
                                   "99999999-0000-0000-0000-000000000000")
        c = mk_change(messages=[msg(MALOO, fail1, date="2026-07-15 14:53:00.0"),
                                msg(MALOO, fail2, date="2026-07-17 14:37:00.0")],
                      verified=[vote(MALOO, -1)])
        t = ci_parse.parse_tests(c)
        assert t["enforced_fail"] == 1  # one failing session, not two
        ft = t["failed_tests"][0]
        assert len(ft["failures"]) == 1
        f = ft["failures"][0]
        assert f["attempts"] == 2
        assert "99999999" in f["url"]  # the newest attempt wins

    def test_different_platforms_stay_separate(self):
        fail2 = MALOO_FAIL.replace("RHEL 9.7 / x86_64", "Rocky 10.1 / x86_64")
        c = mk_change(messages=[msg(MALOO, MALOO_FAIL, date="2026-07-15 14:53:00.0"),
                                msg(MALOO, fail2, date="2026-07-15 16:00:00.0")],
                      verified=[vote(MALOO, -1)])
        t = ci_parse.parse_tests(c)
        assert t["enforced_fail"] == 2
        assert len(t["failed_tests"][0]["failures"]) == 2
        assert all(f["attempts"] == 1 for f in t["failed_tests"][0]["failures"])


class TestJanitorState:
    def test_ok_after_success(self):
        c = mk_change(ps=1, messages=[msg(JANITOR, "Patch Set 1:\n\nInitial testing succeeded", ps=1)])
        assert ci_parse.parse_signals(c)["janitor"] == "OK"

    def test_fail_on_unique(self):
        c = mk_change(ps=1, messages=[msg(JANITOR, JANITOR_UNIQUE, ps=1)])
        assert ci_parse.parse_signals(c)["janitor"] == "FAIL"

    def test_none_without_verdict(self):
        c = mk_change(messages=[msg(JENKINS, BUILD_OK)])
        assert ci_parse.parse_signals(c)["janitor"] == "NONE"

    def test_unique_block_details(self):
        c = mk_change(ps=2, messages=[msg(JANITOR, JANITOR_UNIQUE_BLOCK, ps=2)])
        s = ci_parse.parse_signals(c)
        assert s["unique_failure"] and s["janitor"] == "FAIL"
        assert [u["test"] for u in s["unique_tests"]] == [
            "sanity-hsm@ldiskfs+DNE:test_254b", "sanity-hsm@zfs:test_254b"]
        assert all(u["new"] for u in s["unique_tests"])
        assert s["janitor_url"] == "https://testing.whamcloud.com/gerrit-janitor/67449/results.html"
        assert s["janitor_fails"] == 4

    def test_unique_seen_in_reviews_summarized(self):
        c = mk_change(ps=1, messages=[msg(JANITOR, JANITOR_UNIQUE_SEEN, ps=1)])
        s = ci_parse.parse_signals(c)
        assert s["unique_tests"] == [{"test": "sanity-quota@ldiskfs+DNE:test_80",
                                      "new": False, "note": "seen in 65 other reviews"}]

    def test_unique_inline_legacy_format(self):
        c = mk_change(ps=1, messages=[msg(JANITOR, JANITOR_UNIQUE, ps=1)])
        s = ci_parse.parse_signals(c)
        assert s["unique_tests"] == [{"test": "sanity2@ldiskfs+DNE:test_84",
                                      "new": True, "note": "NEW unique failure"}]

    def test_unique_multiple_subtests_one_line(self):
        # 64355-style: several subtests of one suite share a block line;
        # only the first carries the '<suite>@<fstype>:' prefix.
        text = ("Patch Set 1:\n\nInitial testing failed:\n"
                "IMPORTANT: these tests appear to be new failures unique to this patch\n"
                "- sanity-lfsck@ldiskfs+DNE:test_18g(Seen in reviews: 67603 66834), "
                "test_18h(Seen in reviews: 67603), "
                "test_18i(NEW unique failure for this branch in the last 30 days, "
                "and was seen 0 times across 0 other branches 0 reviews)\n"
                "\n\n> sanity-lfsck@ldiskfs+DNE Failure(100s)\n\n"
                "(rocky8.10)All results and logs: "
                "https://testing.whamcloud.com/gerrit-janitor/67168/results.html")
        c = mk_change(ps=1, messages=[msg(JANITOR, text, ps=1)])
        s = ci_parse.parse_signals(c)
        assert [u["test"] for u in s["unique_tests"]] == [
            "sanity-lfsck@ldiskfs+DNE:test_18g",
            "sanity-lfsck@ldiskfs+DNE:test_18h",
            "sanity-lfsck@ldiskfs+DNE:test_18i"]
        assert s["unique_tests"][0]["note"] == "seen in 2 other reviews"
        assert s["unique_tests"][1]["note"] == "seen in 1 other review"
        assert s["unique_tests"][2]["new"]

    def test_errors_without_unique(self):
        c = mk_change(ps=1, messages=[msg(JANITOR, JANITOR_ERRORS, ps=1)])
        s = ci_parse.parse_signals(c)
        assert s["janitor"] == "ERRORS"
        assert not s["unique_failure"]
        assert s["janitor_fails"] == 2
        assert s["janitor_url"].endswith("66757/results.html")

    def test_later_errors_round_clears_unique(self):
        c = mk_change(ps=1, messages=[
            msg(JANITOR, JANITOR_UNIQUE_BLOCK.replace("Patch Set 2", "Patch Set 1"),
                ps=1, date="2026-08-01 10:00:00.0"),
            msg(JANITOR, JANITOR_ERRORS, ps=1, date="2026-08-02 10:00:00.0")])
        s = ci_parse.parse_signals(c)
        assert not s["unique_failure"]
        assert s["unique_tests"] == []
        assert s["janitor"] == "ERRORS"

    def test_final_success_clears_unique(self):
        # The final-round wording differs from the initial one — both clear.
        c = mk_change(ps=1, messages=[
            msg(JANITOR, JANITOR_UNIQUE, ps=1, date="2026-08-01 10:00:00.0"),
            msg(JANITOR, JANITOR_FINAL_OK, ps=1, date="2026-08-02 10:00:00.0")])
        s = ci_parse.parse_signals(c)
        assert not s["unique_failure"]
        assert s["janitor"] == "OK"

    def test_compile_fail_is_errors(self):
        c = mk_change(ps=1, messages=[msg(JANITOR, JANITOR_COMPILE_FAIL, ps=1)])
        s = ci_parse.parse_signals(c)
        assert s["janitor"] == "ERRORS"
        assert s["janitor_compile_fail"]
        assert s["janitor_fails"] == 0

    def test_compile_fail_keeps_standing_unique_verdict(self):
        # A compile failure runs no tests, so it says nothing about a
        # unique-failure verdict from an earlier round — the flag, the
        # test list and the results link of that round must survive.
        c = mk_change(ps=1, messages=[
            msg(JANITOR, JANITOR_UNIQUE_BLOCK.replace("Patch Set 2", "Patch Set 1"),
                ps=1, date="2026-08-01 10:00:00.0"),
            msg(JANITOR, JANITOR_COMPILE_FAIL, ps=1, date="2026-08-02 10:00:00.0")])
        s = ci_parse.parse_signals(c)
        assert s["unique_failure"] and s["janitor"] == "FAIL"
        assert len(s["unique_tests"]) == 2
        assert s["janitor_url"].endswith("67449/results.html")
        assert not s["janitor_compile_fail"]

    def test_test_round_supersedes_compile_fail(self):
        c = mk_change(ps=1, messages=[
            msg(JANITOR, JANITOR_COMPILE_FAIL, ps=1, date="2026-08-01 10:00:00.0"),
            msg(JANITOR, JANITOR_FINAL_OK, ps=1, date="2026-08-02 10:00:00.0")])
        s = ci_parse.parse_signals(c)
        assert s["janitor"] == "OK"
        assert not s["janitor_compile_fail"]

    def test_crash_note_is_not_a_verdict(self):
        c = mk_change(ps=1, messages=[
            msg(JANITOR, JANITOR_UNIQUE_BLOCK.replace("Patch Set 2", "Patch Set 1"),
                ps=1, date="2026-08-01 10:00:00.0"),
            msg(JANITOR, JANITOR_CRASH_NOTE, ps=1, date="2026-08-02 10:00:00.0")])
        s = ci_parse.parse_signals(c)
        assert s["unique_failure"] and s["janitor"] == "FAIL"
        assert not s["janitor_url"].startswith(
            "https://testing.whamcloud.com/gerrit-janitor/66757/testresults")


class TestPatchsetHistoryAndPending:
    def test_history_from_upload_messages(self):
        c = mk_change(ps=3, messages=[
            msg(ME, "Uploaded patch set 1.", ps=1, date="2026-06-01 10:00:00.0",
                tag="autogenerated:gerrit:newPatchSet"),
            msg({"_account_id": 803, "name": "Wang Shilong"}, "Uploaded patch set 2.", ps=2,
                date="2026-06-10 10:00:00.0", tag="autogenerated:gerrit:newPatchSet"),
            msg(JENKINS, BUILD_OK, ps=2),
            msg(ME, "Uploaded patch set 3: Commit message was updated.", ps=3,
                date="2026-06-12 10:00:00.0", tag="autogenerated:gerrit:newPatchSet"),
        ])
        h = ci_parse.patchset_history(c)
        assert [(p["ps"], p["uploader"]) for p in h] == [
            (1, "Marc Vef"), (2, "Wang Shilong"), (3, "Marc Vef")]

    def test_pending_reviewers_excludes_bots_owner_self(self):
        from conftest import ADILGER
        BZZZ = {"_account_id": 119, "name": "Alex Zhuravlev", "email": "b@w.com"}
        c = mk_change(code_review=[vote(ADILGER, 1), vote(BZZZ, 0), vote(ME, 0),
                                   vote(CHECKPATCH, 0), vote(MALOO, 0)])
        assert ci_parse.pending_reviewers(c, 1055) == ["Alex Zhuravlev"]
