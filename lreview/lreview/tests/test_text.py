"""Tests for the plain-text batch dump."""

import json
from pathlib import Path

from lreview.gerrit import LocalChange, ResolvedChange
from lreview.runner import (
    ReviewResult,
    STATUS_CLEAN,
    STATUS_FINDINGS,
    STATUS_TIMEOUT,
)
from lreview.text import batch_text, result_text, write_batch_text


SPEC = {
    "message": "Two problems in the error path.",
    "comments": {
        "lustre/osc/osc_request.c": [
            {"line": 42, "message": "(bug) rc is leaked here",
             "unresolved": True},
            {"range": {"start_line": 90, "end_line": 95},
             "message": "(suggestion) fold this into the caller",
             "unresolved": False},
        ],
    },
}


def _local_result(status=STATUS_FINDINGS, **kwargs):
    change = LocalChange(ref_name="HEAD~1", sha="a" * 40,
                         subject="LU-1 osc: fix a leak")
    defaults = dict(findings=2, severity="medium", model="haiku",
                    tokens=1234, cost_usd=0.12, duration=61.0)
    defaults.update(kwargs)
    return ReviewResult(change, status, **defaults)


class TestResultText:

    def test_findings_render(self):
        text = result_text(_local_result(), SPEC, index=1, total=3)
        assert "[1/3] HEAD~1  aaaaaaaaaaaa  LU-1 osc: fix a leak" in text
        assert "status: findings (2 finding(s)), severity medium" in text
        assert "run:    haiku, 1k tokens, $0.12, 1m01s" in text
        assert "Two problems in the error path." in text
        assert "Findings (2)" in text
        assert "(1) lustre/osc/osc_request.c (line 42)" in text
        assert "(2) lustre/osc/osc_request.c (lines 90-95)" in text
        assert "[informational]" in text
        assert "    (bug) rc is leaked here" in text

    def test_clean_review_says_so(self):
        text = result_text(_local_result(status=STATUS_CLEAN, findings=0,
                                         severity=None))
        assert "No findings reported for this commit." in text

    def test_failed_review_reports_the_error(self):
        text = result_text(_local_result(status=STATUS_TIMEOUT, findings=0,
                                         severity=None,
                                         error="timed out after 60s"))
        assert "error:  timed out after 60s" in text
        assert "No review output was produced" in text

    def test_zero_findings_still_states_the_count(self):
        """A findings JSON with no comments must not read as a bare
        'status: findings'."""
        text = result_text(_local_result(findings=0, severity="none"),
                           {"message": "No regressions found.", "comments": {}})
        assert "status: findings (0 finding(s)), severity none" in text
        assert "Findings (0)" in text

    def test_gerrit_change_headline(self):
        change = ResolvedChange(
            number=64086, project="fs/lustre-release", subject="LU-2 lov: x",
            sha="b" * 40, patchset=7, ref="refs/changes/86/64086/7",
            base_url="https://review.example.com")
        text = result_text(ReviewResult(change, STATUS_CLEAN))
        assert "change 64086 ps7  bbbbbbbbbbbb  LU-2 lov: x" in text


class TestBatchText:

    def test_header_index_and_totals(self, tmp_path):
        json_path = tmp_path / "gerrit-review-x.json"
        json_path.write_text(json.dumps(SPEC))
        results = [
            _local_result(json_path=json_path),
            _local_result(status=STATUS_CLEAN, findings=0, severity=None,
                          tokens=1000, cost_usd=0.05),
        ]
        text = batch_text(results, repo=Path("/repo"), title="my batch")
        assert "my batch" in text
        assert "repository: /repo" in text
        assert "reviews: 2" in text
        assert "totals: 2k tokens, $0.17" in text
        assert "  1. HEAD~1  aaaaaaaaaaaa  LU-1 osc: fix a leak -- " \
               "findings (2)" in text
        assert "(bug) rc is leaked here" in text
        assert "No findings reported for this commit." in text

    def test_unreadable_json_is_not_fatal(self, tmp_path):
        json_path = tmp_path / "broken.json"
        json_path.write_text("{not json")
        text = batch_text([_local_result(json_path=json_path)])
        assert "No review output was produced" in text

    def test_write_creates_parent_dirs(self, tmp_path):
        dest = tmp_path / "deep" / "dir" / "dump.txt"
        written = write_batch_text(dest, [_local_result(findings=0)])
        assert written == dest
        assert dest.read_text().endswith("\n")
