"""Tests for the graph/review.py extractors.

These exercise the pure-data helpers that turn raw Gerrit message
and label payloads into the compact review-info shape consumed by
the graph nodes. No network — everything is fed synthetic payloads
that mirror the actual Gerrit response shapes.
"""

from gerrit_cli.graph.review import (
    _empty_review,
    _extract_ci_links,
    _extract_cr_history,
    _parse_labels,
)


# ─── _extract_ci_links ────────────────────────────────────────────


def _jenkins_msg(ps, job, build):
    return {
        "_revision_number": ps,
        "message": (
            f"Patch Set {ps}: Verified+1\n\nBuild Successful\n\n"
            f"https://build.whamcloud.com/job/{job}/{build}/ : SUCCESS"
        ),
    }


def _maloo_msg(ps, job, build):
    """Maloo Results bot message — includes the fully-formed URL."""
    return {
        "_revision_number": ps,
        "message": (
            f"Patch Set {ps}:\n\n"
            f"#### The following sessions will be run for Build {build} (patch #{ps}):\n\n"
            f"Maloo Results: https://testing.whamcloud.com/test_sessions/related"
            f"?jobs={job}&builds={build}#redirect"
        ),
    }


def _maloo_buildnum_only_msg(ps, build):
    """Older Maloo bot format with just the build number, no full URL."""
    return {
        "_revision_number": ps,
        "message": (
            f"Patch Set {ps}:\n\n"
            f"sessions will be run for Build {build} (patch #{ps})"
        ),
    }


class TestExtractCiLinksSingleRun:
    """A typical patch with one Jenkins + one Maloo run on the
    current patchset — the common case."""

    def test_extracts_jenkins_and_maloo_urls(self):
        msgs = [
            _jenkins_msg(5, "lustre-reviews", 100),
            _maloo_msg(5, "lustre-reviews", 100),
        ]
        out = _extract_ci_links(msgs, 5)
        assert out["jenkins_url"] == (
            "https://build.whamcloud.com/job/lustre-reviews/100/"
        )
        assert out["maloo_url"] == (
            "https://testing.whamcloud.com/test_sessions/related"
            "?jobs=lustre-reviews&builds=100#redirect"
        )

    def test_jenkins_url_only_no_maloo(self):
        """Jenkins ran but Maloo never posted — maloo_url empty."""
        out = _extract_ci_links(
            [_jenkins_msg(5, "lustre-reviews", 100)], 5
        )
        assert "lustre-reviews/100" in out["jenkins_url"]
        assert out["maloo_url"] == ""

    def test_no_messages_returns_empty(self):
        out = _extract_ci_links([], 5)
        assert out == {"jenkins_url": "", "maloo_url": ""}


class TestExtractCiLinksRetest:
    """A reviewer requested a retest after the first CI run finished
    — there are now TWO Jenkins + TWO Maloo messages on the same
    patchset and the panel must point at the latest run."""

    def test_latest_run_wins_for_both_links(self):
        """Direct regression for 62135: retest happened on the same
        patchset, latest build number should be returned."""
        msgs = [
            _jenkins_msg(7, "lustre-reviews", 125904),
            _maloo_msg(7, "lustre-reviews", 125904),
            _jenkins_msg(7, "lustre-reviews", 126123),   # retest
            _maloo_msg(7, "lustre-reviews", 126123),
        ]
        out = _extract_ci_links(msgs, 7)
        assert "/126123/" in out["jenkins_url"]
        assert "builds=126123" in out["maloo_url"]
        assert "125904" not in out["jenkins_url"]
        assert "125904" not in out["maloo_url"]

    def test_three_retests_picks_latest(self):
        msgs = [
            _jenkins_msg(3, "lustre-reviews", 100),
            _maloo_msg(3, "lustre-reviews", 100),
            _jenkins_msg(3, "lustre-reviews", 200),
            _maloo_msg(3, "lustre-reviews", 200),
            _jenkins_msg(3, "lustre-reviews", 300),
            _maloo_msg(3, "lustre-reviews", 300),
        ]
        out = _extract_ci_links(msgs, 3)
        assert "/300/" in out["jenkins_url"]
        assert "builds=300" in out["maloo_url"]


class TestExtractCiLinksBuildNumFallback:
    """Older bot messages didn't include the fully-formed Maloo URL,
    only the "sessions will be run for Build NNNNN" line. The job
    name has to be reconstructed from the Jenkins URL on the same
    patchset."""

    def test_reconstructs_url_from_buildnum_and_jenkins_job(self):
        msgs = [
            _jenkins_msg(2, "lustre-reviews", 500),
            _maloo_buildnum_only_msg(2, 500),
        ]
        out = _extract_ci_links(msgs, 2)
        assert out["maloo_url"] == (
            "https://testing.whamcloud.com/test_sessions/related"
            "?jobs=lustre-reviews&builds=500#redirect"
        )

    def test_falls_back_to_default_job_when_no_jenkins_url(self):
        """If only the Maloo build-number line appears (no Jenkins
        URL ever seen), default the job to lustre-reviews — preserves
        the historical default for fs/lustre-release patches."""
        out = _extract_ci_links([_maloo_buildnum_only_msg(2, 700)], 2)
        assert "jobs=lustre-reviews" in out["maloo_url"]
        assert "builds=700" in out["maloo_url"]


class TestExtractCiLinksPerProject:
    """Maloo's `jobs=` parameter must match whichever Jenkins job
    actually ran the build — `lustre-b_es-reviews` for ex/-tree
    patches on b_es branches, `lustre-reviews` for fs/. The bot's
    own URL already carries this; we just preserve it."""

    def test_ex_lustre_release_keeps_b_es_job_in_maloo_url(self):
        msgs = [
            _jenkins_msg(1, "lustre-b_es-reviews", 28583),
            _maloo_msg(1, "lustre-b_es-reviews", 28583),
        ]
        out = _extract_ci_links(msgs, 1)
        assert "jobs=lustre-b_es-reviews" in out["maloo_url"]
        assert "/lustre-b_es-reviews/28583" in out["jenkins_url"]

    def test_buildnum_fallback_inherits_jenkins_job(self):
        """Build-num-only line + a Jenkins URL on the b_es job →
        reconstructed Maloo URL also points at b_es."""
        msgs = [
            _jenkins_msg(1, "lustre-b_es-reviews", 28583),
            _maloo_buildnum_only_msg(1, 28583),
        ]
        out = _extract_ci_links(msgs, 1)
        assert "jobs=lustre-b_es-reviews" in out["maloo_url"]


class TestExtractCiLinksPatchsetFilter:
    """Messages on other patchsets must not bleed into the result —
    even if older patchsets had Jenkins/Maloo runs, we only want
    the target patchset's links."""

    def test_ignores_older_patchset_messages(self):
        msgs = [
            _jenkins_msg(3, "lustre-reviews", 100),
            _maloo_msg(3, "lustre-reviews", 100),
            _jenkins_msg(5, "lustre-reviews", 500),
            _maloo_msg(5, "lustre-reviews", 500),
        ]
        out = _extract_ci_links(msgs, 5)
        assert "/500/" in out["jenkins_url"]
        assert "builds=500" in out["maloo_url"]
        assert "100" not in out["jenkins_url"]
        assert "builds=100" not in out["maloo_url"]

    def test_returns_empty_when_target_ps_has_no_messages(self):
        msgs = [
            _jenkins_msg(3, "lustre-reviews", 100),
            _maloo_msg(3, "lustre-reviews", 100),
        ]
        out = _extract_ci_links(msgs, 7)
        assert out["jenkins_url"] == ""
        assert out["maloo_url"] == ""


# ─── _extract_cr_history ──────────────────────────────────────────


def _vote_msg(ps, author, text):
    return {
        "_revision_number": ps,
        "author": {"name": author},
        "tag": "",
        "message": text,
    }


def _upload_msg(ps, author):
    """Autogenerated 'Uploaded patch set N.' message — should be
    skipped even though it carries the same ps and an author."""
    return {
        "_revision_number": ps,
        "author": {"name": author},
        "tag": "autogenerated:gerrit:newPatchSet",
        "message": f"Uploaded patch set {ps}.",
    }


class TestExtractCrHistoryBasic:
    def test_extracts_vote_actions_before_current_ps(self):
        msgs = [
            _vote_msg(3, "Sebastien Buisson",
                      "Patch Set 3: Code-Review+1\n\n(2 comments)"),
            _vote_msg(4, "Andreas Dilger",
                      "Patch Set 4: Code-Review-1"),
        ]
        out = _extract_cr_history(msgs, current_ps=5)
        assert out == [
            {"name": "Sebastien Buisson", "ps": 3, "value": 1},
            {"name": "Andreas Dilger", "ps": 4, "value": -1},
        ]

    def test_skips_current_patchset_votes(self):
        msgs = [
            _vote_msg(3, "Sebastien Buisson",
                      "Patch Set 3: Code-Review+1"),
            _vote_msg(5, "Sebastien Buisson",
                      "Patch Set 5: Code-Review-1"),
        ]
        out = _extract_cr_history(msgs, current_ps=5)
        assert out == [
            {"name": "Sebastien Buisson", "ps": 3, "value": 1},
        ]

    def test_returns_empty_when_no_prior_votes(self):
        out = _extract_cr_history([], current_ps=5)
        assert out == []


class TestExtractCrHistoryFilters:
    def test_excludes_bot_voters(self):
        """wc-checkpatch and similar automated voters are filtered
        — the panel labels surviving entries as human reviewers."""
        msgs = [
            _vote_msg(2, "wc-checkpatch",
                      "Patch Set 2: Code-Review-1"),
            _vote_msg(2, "Lustre Gerrit Janitor",
                      "Patch Set 2: Code-Review-1"),
            _vote_msg(3, "Sebastien Buisson",
                      "Patch Set 3: Code-Review+1"),
        ]
        out = _extract_cr_history(msgs, current_ps=5)
        assert out == [
            {"name": "Sebastien Buisson", "ps": 3, "value": 1},
        ]

    def test_skips_autogenerated_upload_messages(self):
        """The 'Uploaded patch set N' message is tagged autogenerated
        and carries no vote info — it must not produce a history
        entry even when an author name is present."""
        msgs = [
            _upload_msg(2, "wangdi"),
            _vote_msg(3, "Sebastien Buisson",
                      "Patch Set 3: Code-Review+1"),
        ]
        out = _extract_cr_history(msgs, current_ps=5)
        assert out == [
            {"name": "Sebastien Buisson", "ps": 3, "value": 1},
        ]

    def test_ignores_messages_without_vote_header(self):
        """A reply without 'Patch Set N:' at the start of the body
        is just a comment, not a vote — must not be recorded."""
        msgs = [
            _vote_msg(3, "Sebastien Buisson",
                      "Thanks for the patch, looks good.\n"
                      "Code-Review+1 incoming once tests pass"),
        ]
        out = _extract_cr_history(msgs, current_ps=5)
        assert out == []


class TestExtractCrHistoryResetSemantics:
    def test_reset_emits_zero_value(self):
        """'-Code-Review' (no score) is a retraction — we emit
        value 0 so the JS layer can drop the voter."""
        msgs = [
            _vote_msg(3, "Andreas Dilger",
                      "Patch Set 3: Code-Review+1"),
            _vote_msg(4, "Andreas Dilger",
                      "Patch Set 4: -Code-Review"),
        ]
        out = _extract_cr_history(msgs, current_ps=5)
        assert out == [
            {"name": "Andreas Dilger", "ps": 3, "value": 1},
            {"name": "Andreas Dilger", "ps": 4, "value": 0},
        ]

    def test_negative_score_is_a_vote_not_a_reset(self):
        """'-Code-Review-1' must be parsed as a -1 vote, not as a
        reset followed by '-1'. The reset regex's negative
        lookahead protects this."""
        msgs = [
            _vote_msg(3, "Andreas Dilger",
                      "Patch Set 3: -Code-Review-1"),
        ]
        out = _extract_cr_history(msgs, current_ps=5)
        assert out == [
            {"name": "Andreas Dilger", "ps": 3, "value": -1},
        ]


# ─── _parse_labels ────────────────────────────────────────────────


class TestParseLabelsVerified:
    def test_verified_pass_with_only_positive_votes(self):
        labels = {"Verified": {"all": [
            {"name": "jenkins", "value": 1},
            {"name": "Maloo", "value": 1},
        ]}}
        out = _parse_labels(labels)
        assert out["verified_pass"] is True
        assert out["verified_fail"] is False
        assert len(out["verified_votes"]) == 2

    def test_verified_fail_when_any_minus(self):
        labels = {"Verified": {"all": [
            {"name": "jenkins", "value": 1},
            {"name": "Maloo", "value": -1},
        ]}}
        out = _parse_labels(labels)
        assert out["verified_pass"] is False
        assert out["verified_fail"] is True

    def test_zero_value_votes_ignored(self):
        """Voters who set value 0 (no opinion) must not appear in
        verified_votes — _parse_labels filters them out."""
        labels = {"Verified": {"all": [
            {"name": "jenkins", "value": 0},
            {"name": "Maloo", "value": 1},
        ]}}
        out = _parse_labels(labels)
        assert len(out["verified_votes"]) == 1
        assert out["verified_votes"][0]["name"] == "Maloo"


class TestParseLabelsCodeReview:
    def test_cr_approved_when_flag_set(self):
        labels = {"Code-Review": {
            "all": [{"name": "Oleg Drokin", "value": 2}],
            "approved": {"name": "Oleg Drokin"},
        }}
        out = _parse_labels(labels)
        assert out["cr_approved"] is True
        assert out["cr_rejected"] is False

    def test_cr_rejected_records_rejecter_name(self):
        labels = {"Code-Review": {
            "all": [{"name": "Reviewer", "value": -2}],
            "rejected": {"name": "Reviewer"},
        }}
        out = _parse_labels(labels)
        assert out["cr_rejected"] is True
        assert out["cr_rejected_by"] == "Reviewer"

    def test_cr_veto_on_any_negative(self):
        """A -1 (not just -2) sets cr_veto — used by reviewHealth
        to short-circuit the green verdict."""
        labels = {"Code-Review": {"all": [
            {"name": "Reviewer", "value": -1},
        ]}}
        out = _parse_labels(labels)
        assert out["cr_veto"] is True

    def test_votes_sorted_negative_first(self):
        """The panel iterates cr_votes in order; negative votes must
        appear first so the most concerning verdict reads at the top."""
        labels = {"Code-Review": {"all": [
            {"name": "Pos1", "value": 1},
            {"name": "Neg1", "value": -1},
            {"name": "Pos2", "value": 2},
        ]}}
        out = _parse_labels(labels)
        values = [v["value"] for v in out["cr_votes"]]
        # negative before positive; among negatives, the sort key is
        # (value > 0, abs(value)) so -1 < -2 is the order seen.
        assert values[0] < 0
        assert all(v > 0 for v in values[1:])


# ─── _empty_review ────────────────────────────────────────────────


class TestEmptyReview:
    def test_includes_all_panel_keys(self):
        """Smoke-test for the contract between Python and the JS
        panel — the panel reads every one of these keys, so the
        empty shape must keep them present even when unset."""
        ev = _empty_review()
        for key in [
            "verified_votes", "verified_pass", "verified_fail",
            "cr_votes", "cr_approved", "cr_rejected", "cr_rejected_by",
            "cr_veto", "jenkins_url", "maloo_url",
            "cr_history", "unresolved_count", "unresolved_comments",
        ]:
            assert key in ev, f"missing key {key!r} in _empty_review"
