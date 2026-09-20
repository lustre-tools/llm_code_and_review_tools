"""Tests for Maloo CLI commands.

All tests mock the MalooClient to avoid hitting the real API.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from maloo_tool.cli import (
    main,
    _extract_session_id,
    _parse_review_arg,
    _resolve_branch_to_job,
)

# Valid UUIDs for test data (required by _extract_session_id regex)
SID_1 = "11111111-1111-1111-1111-111111111111"
SID_2 = "22222222-2222-2222-2222-222222222222"
SID_3 = "33333333-3333-3333-3333-333333333333"
TSID_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TSID_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_client():
    """Create a mock MalooClient and patch _make_client to return it."""
    client = MagicMock()
    with patch("maloo_tool.cli._make_client", return_value=client):
        yield client


def _parse_output(result):
    """Parse CLI JSON output, return the envelope dict."""
    assert result.exit_code == 0, f"CLI failed: {result.output}"
    return json.loads(result.output)


# -- session command --


class TestSession:
    def test_session_basic(self, runner, mock_client):
        mock_client.get_session.return_value = {
            "id": SID_1,
            "test_group": "full",
            "test_name": "lustre-master-el8--full--1.10",
            "test_host": "host1",
            "submission": "2026-01-15T10:00:00.000Z",
            "duration": 3600,
            "enforcing": True,
            "test_sets_passed_count": 5,
            "test_sets_failed_count": 1,
            "test_sets_aborted_count": 0,
            "test_sets_count": 6,
        }
        mock_client.get_test_sets.return_value = [
            {
                "id": TSID_1,
                "test_set_script_id": "script-1",
                "status": "PASS",
                "duration": 600,
                "sub_tests_passed_count": 10,
                "sub_tests_failed_count": 0,
                "sub_tests_skipped_count": 2,
                "sub_tests_count": 12,
            },
        ]
        mock_client.resolve_test_set_names.return_value = {
            "script-1": "sanity",
        }

        result = runner.invoke(main, ["--envelope", "session", SID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["session_id"] == SID_1
        assert env["data"]["suites"][0]["name"] == "sanity"

    def test_session_not_found(self, runner, mock_client):
        mock_client.get_session.return_value = None
        result = runner.invoke(main, ["--envelope", "session", SID_1])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert result.exit_code != 0


# -- failures command --


class TestFailures:
    def test_failures_with_data(self, runner, mock_client):
        mock_client.get_session.return_value = {
            "id": SID_1,
            "test_group": "full",
            "test_name": "lustre-master--full--1.10",
        }
        mock_client.get_test_sets.return_value = [
            {
                "id": TSID_1,
                "test_set_script_id": "script-san",
                "status": "FAIL",
                "sub_tests_failed_count": 1,
                "sub_tests_count": 50,
            },
        ]
        mock_client.resolve_test_set_names.return_value = {
            "script-san": "sanity",
        }
        failed = {
            "sub_test_script_id": "sub-39b",
            "status": "FAIL",
            "error": "assertion failed",
            "duration": 30,
            "return_code": 1,
            "order": 5,
        }
        passed = {"sub_test_script_id": "sub-0a", "status": "PASS", "order": 1}
        mock_client.get_subtests.return_value = [passed, failed]
        mock_client.resolve_subtest_names.return_value = {
            "sub-39b": "test_39b",
        }

        result = runner.invoke(main, ["--envelope", "failures", SID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert len(env["data"]["failed_suites"]) == 1
        assert env["data"]["failed_suites"][0]["failed_subtests"][0]["name"] == "test_39b"
        assert len(env["data"]["failed_suites"][0]["failed_subtests"]) == 1
        # Names are one request each; only the failed ones are wanted.
        mock_client.resolve_subtest_names.assert_called_once_with([failed])

    def test_failures_no_failures(self, runner, mock_client):
        mock_client.get_session.return_value = {
            "id": SID_1,
            "test_group": "full",
            "test_name": "lustre-master--full--1.10",
        }
        mock_client.get_test_sets.return_value = [
            {"id": TSID_1, "test_set_script_id": "sc-1", "status": "PASS"},
        ]
        mock_client.resolve_test_set_names.return_value = {"sc-1": "sanity"}

        result = runner.invoke(main, ["--envelope", "failures", SID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["failed_suites"] == []


# -- subtests command --


class TestSubtests:
    def _setup_subtests(self, mock_client):
        """Common setup for subtests tests."""
        mock_client.get_test_set.return_value = {
            "id": TSID_1,
            "test_set_script_id": "sc-1",
            "status": "FAIL",
        }
        mock_client.get_test_set_script.return_value = {
            "id": "sc-1",
            "name": "sanity",
        }
        mock_client.get_subtests.return_value = [
            {
                "id": "sub-id-1",
                "sub_test_script_id": "sub-1",
                "status": "PASS",
                "error": "",
                "duration": 10,
                "return_code": 0,
                "order": 0,
            },
            {
                "id": "sub-id-2",
                "sub_test_script_id": "sub-2",
                "status": "FAIL",
                "error": "oops",
                "duration": 5,
                "return_code": 1,
                "order": 1,
            },
        ]
        mock_client.resolve_subtest_names.return_value = {
            "sub-1": "test_1a",
            "sub-2": "test_1b",
        }

    def test_subtests_defaults_to_fail(self, runner, mock_client):
        """Default (no flags) should show only FAIL subtests."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["--envelope", "subtests", TSID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["suite"] == "sanity"
        assert env["data"]["total"] == 2
        assert env["data"]["shown"] == 1
        assert env["data"]["filter"] == "FAIL"
        assert env["data"]["subtests"][0]["name"] == "test_1b"

    def test_subtests_carry_their_ids(self, runner, mock_client):
        """link-bug --type SubTest needs one, and subtests is where it is."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["--envelope", "subtests", TSID_1, "--all"])
        env = _parse_output(result)
        assert [s["id"] for s in env["data"]["subtests"]] == ["sub-id-1", "sub-id-2"]

    def test_subtests_all_flag(self, runner, mock_client):
        """--all should show all subtests regardless of status."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["--envelope", "subtests", TSID_1, "--all"])
        env = _parse_output(result)
        assert env["data"]["shown"] == 2
        assert env["data"]["filter"] is None

    def test_subtests_status_filter(self, runner, mock_client):
        """Explicit --status filter should work."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["--envelope", "subtests", TSID_1, "--status", "PASS"])
        env = _parse_output(result)
        assert env["data"]["shown"] == 1
        assert env["data"]["subtests"][0]["name"] == "test_1a"
        assert env["data"]["filter"] == "PASS"

    def test_subtests_all_overrides_status(self, runner, mock_client):
        """--all should override --status."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["--envelope", "subtests", TSID_1, "--all", "--status", "PASS"])
        env = _parse_output(result)
        assert env["data"]["shown"] == 2
        assert env["data"]["filter"] is None


# -- review command --


class TestReview:
    def test_review_found(self, runner, mock_client):
        mock_client.find_sessions_by_commit.return_value = [
            {
                "id": SID_1,
                "test_group": "full",
                "test_name": "lustre-master--full--1.10",
                "test_host": "host1",
                "submission": "2026-01-15T10:00:00.000Z",
                "enforcing": True,
                "test_sets_passed_count": 5,
                "test_sets_failed_count": 0,
                "test_sets_count": 5,
                "duration": 3600,
            },
        ]

        result = runner.invoke(
            main, ["--envelope", "review", "54321", "--commit", "a" * 40]
        )
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["review_id"] == 54321
        assert env["data"]["commit"] == "a" * 40
        assert env["data"]["session_count"] == 1
        mock_client.find_sessions_by_commit.assert_called_once_with("a" * 40)

    def test_review_not_found(self, runner, mock_client):
        mock_client.find_sessions_by_commit.return_value = []
        result = runner.invoke(
            main, ["--envelope", "review", "99999", "--commit", "b" * 40]
        )
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["sessions"] == []

    def test_review_without_a_commit_says_so_instead_of_guessing(
        self, runner, mock_client
    ):
        """Maloo stores no change number to query, so there is nothing to
        fall back to: a review id on its own selected the whole table."""
        result = runner.invoke(main, ["--envelope", "review", "54321"])
        assert result.exit_code == 1
        env = json.loads(result.output)
        assert env["ok"] is False
        assert env["error"]["code"] == "MISSING_FILTER"
        assert "gerrit info 54321" in env["error"]["message"]
        mock_client.find_sessions_by_commit.assert_not_called()


# -- bugs command --


SUBTEST_1 = "ac9e298f-e265-48b9-9921-4246e6c1eddb"


def _maloo_link(attached_to, jira, valid):
    """A bug link as Maloo's API returns it: ``id`` is what it is attached to."""
    return {
        "id": attached_to, "jira": jira, "summary": f"{jira} summary",
        "status": "Open", "valid": valid,
    }


class TestBugs:
    def _links(self, mock_client, direct, with_children):
        """Answer the plain query with ``direct``, related=true with the rest."""
        def get_bug_links(buggable_id, related=False):
            return list(with_children if related else direct)

        mock_client.get_bug_links.side_effect = get_bug_links
        mock_client.get_subtest.return_value = {
            "id": SUBTEST_1, "sub_test_script_id": "script-np",
        }
        mock_client.get_sub_test_script.return_value = {"name": "node-provisioning"}
        mock_client.get_session.return_value = None

    def test_links_on_the_set_itself(self, runner, mock_client):
        link = _maloo_link(TSID_1, "LU-16301", True)
        self._links(mock_client, [link], [link])
        env = _parse_output(runner.invoke(main, ["--envelope", "bugs", TSID_1]))
        assert env["data"]["count"] == 1
        [item] = env["data"]["bug_links"]
        assert (item["ticket"], item["state"], item["buggable_id"], item["subtest"]) == (
            "LU-16301", "accepted", TSID_1, None,
        )

    def test_links_on_a_child_subtest_are_found(self, runner, mock_client):
        """Maloo auto-links DCO tickets to the failed subtest, not the set:
        a bare query on the set answered count 0 for exactly those."""
        self._links(mock_client, [], [
            _maloo_link(SUBTEST_1, "DCO-11631", True),
            _maloo_link(SUBTEST_1, "DCO-11677", None),
        ])
        env = _parse_output(runner.invoke(main, ["--envelope", "bugs", TSID_1]))
        assert env["data"]["count"] == 2
        assert [
            (i["ticket"], i["state"], i["buggable_id"], i["subtest"])
            for i in env["data"]["bug_links"]
        ] == [
            ("DCO-11631", "accepted", SUBTEST_1, "node-provisioning"),
            ("DCO-11677", "pending", SUBTEST_1, "node-provisioning"),
        ]
        # One lookup per subtest, however many links it carries.
        mock_client.get_subtest.assert_called_once_with(SUBTEST_1)

    def test_a_link_seen_both_ways_is_listed_once(self, runner, mock_client):
        link = _maloo_link(TSID_1, "LU-16301", True)
        child = _maloo_link(SUBTEST_1, "DCO-11631", True)
        self._links(mock_client, [link], [link, child])
        env = _parse_output(runner.invoke(main, ["--envelope", "bugs", TSID_1]))
        assert [i["ticket"] for i in env["data"]["bug_links"]] == ["LU-16301", "DCO-11631"]

    def test_a_link_maloo_repeats_is_listed_once(self, runner, mock_client):
        child = _maloo_link(SUBTEST_1, "LU-18361", True)
        self._links(mock_client, [], [child, dict(child)])
        env = _parse_output(runner.invoke(main, ["--envelope", "bugs", TSID_1]))
        assert env["data"]["count"] == 1

    def test_one_ticket_in_two_states_is_two_links(self, runner, mock_client):
        self._links(mock_client, [], [
            _maloo_link(SUBTEST_1, "LU-18361", True),
            _maloo_link(SUBTEST_1, "LU-18361", None),
        ])
        env = _parse_output(runner.invoke(main, ["--envelope", "bugs", TSID_1]))
        assert [i["state"] for i in env["data"]["bug_links"]] == ["accepted", "pending"]

    def test_direct_only(self, runner, mock_client):
        self._links(mock_client, [], [_maloo_link(SUBTEST_1, "DCO-11631", True)])
        env = _parse_output(
            runner.invoke(main, ["--envelope", "bugs", TSID_1, "--direct-only"])
        )
        assert env["data"]["count"] == 0

    def test_related_is_still_accepted(self, runner, mock_client):
        self._links(mock_client, [], [_maloo_link(SUBTEST_1, "DCO-11631", True)])
        env = _parse_output(
            runner.invoke(main, ["--envelope", "bugs", TSID_1, "--related"])
        )
        assert env["data"]["count"] == 1

    def test_a_rejected_link_says_so(self, runner, mock_client):
        self._links(mock_client, [_maloo_link(TSID_1, "LU-1", False)], [])
        env = _parse_output(runner.invoke(main, ["--envelope", "bugs", TSID_1]))
        assert env["data"]["bug_links"][0]["state"] == "rejected"

    def test_bugs_empty(self, runner, mock_client):
        self._links(mock_client, [], [])
        env = _parse_output(runner.invoke(main, ["--envelope", "bugs", TSID_1]))
        assert env["data"]["count"] == 0

    def test_a_session_id_is_refused_rather_than_answered_empty(self, runner, mock_client):
        """A link made on a test set read back through its session's id came
        back count 0, which looked like the link had silently failed."""
        self._links(mock_client, [], [])
        mock_client.get_session.return_value = {"id": SID_1}
        result = runner.invoke(main, ["--envelope", "bugs", SID_1])
        assert result.exit_code != 0
        env = json.loads(result.output)
        assert env["ok"] is False
        assert "is a test session" in env["error"]["message"]
        assert f"maloo failures {SID_1}" in env["error"]["message"]


# -- link-bug command --


class TestLinkBug:
    def test_link_bug_success(self, runner, mock_client):
        mock_client.create_bug_link.return_value = "OK"
        result = runner.invoke(main, ["--envelope", "link-bug", TSID_1, "LU-12345"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["bug"] == "LU-12345"

    def test_link_bug_error(self, runner, mock_client):
        mock_client.create_bug_link.return_value = "ERROR: bug not found"
        result = runner.invoke(main, ["--envelope", "link-bug", TSID_1, "LU-99999"])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert result.exit_code != 0


# -- sessions command --


class TestSessions:
    def test_sessions_by_branch(self, runner, mock_client):
        mock_client.get_sessions.return_value = [
            {
                "id": SID_1,
                "test_group": "full",
                "test_name": "lustre-master--full--1.10",
                "test_host": "host1",
                "submission": "2026-02-15T10:00:00.000Z",
                "enforcing": True,
                "test_sets_passed_count": 5,
                "test_sets_failed_count": 1,
                "test_sets_aborted_count": 0,
                "test_sets_count": 6,
                "duration": 3600,
                "trigger_job": "lustre-master",
            },
            {
                "id": SID_2,
                "test_group": "full",
                "test_name": "lustre-master--full--1.11",
                "test_host": "host2",
                "submission": "2026-02-14T10:00:00.000Z",
                "enforcing": True,
                "test_sets_passed_count": 6,
                "test_sets_failed_count": 0,
                "test_sets_aborted_count": 0,
                "test_sets_count": 6,
                "duration": 3400,
                "trigger_job": "lustre-master",
            },
        ]

        result = runner.invoke(main, ["--envelope", "sessions", "--branch", "lustre-master"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 2
        assert env["data"]["filters"]["branch"] == "lustre-master"
        assert env["data"]["sessions"][0]["trigger_job"] == "lustre-master"

    def test_sessions_failed_filter(self, runner, mock_client):
        mock_client.get_sessions.return_value = []
        result = runner.invoke(main, ["--envelope", "sessions", "--branch", "lustre-master", "--failed"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["filters"]["failed_only"] is True

    def test_sessions_passes_params(self, runner, mock_client):
        """Verify that filter params are passed correctly to the client."""
        mock_client.get_sessions.return_value = []
        runner.invoke(main, [
            "sessions", "--branch", "lustre-master",
            "--host", "onyx-1", "--failed", "--limit", "5",
        ])
        call_args = mock_client.get_sessions.call_args
        params = call_args[0][0]
        assert params["trigger_job"] == "lustre-master"
        assert params["test_host"] == "onyx-1"
        assert params["test_sets_failed"] == "true"
        # max_records passed as keyword arg
        assert call_args[1]["max_records"] == 5


# -- test-history command --

STATS_OK = {"sessions_scanned": 30, "sessions_with_suite": 30}
STATS_NONE = {"sessions_scanned": 40, "sessions_with_suite": 0}



class TestTestHistory:
    HISTORY_DATA = [
        {
            "session_id": SID_1,
            "submission": "2026-02-10T10:00:00.000Z",
            "test_host": "host1",
            "test_name": "lustre-master--full--1.10",
            "suite": "sanity",
            "status": "PASS",
            "error": "",
            "duration": 30,
            "test_set_id": TSID_1,
        },
        {
            "session_id": SID_2,
            "submission": "2026-02-12T10:00:00.000Z",
            "test_host": "host2",
            "test_name": "lustre-master--full--1.11",
            "suite": "sanity",
            "status": "FAIL",
            "error": "assertion failed",
            "duration": 25,
            "test_set_id": TSID_2,
        },
        {
            "session_id": SID_3,
            "submission": "2026-02-14T10:00:00.000Z",
            "test_host": "host1",
            "test_name": "lustre-master--full--1.12",
            "suite": "sanity",
            "status": "PASS",
            "error": "",
            "duration": 28,
            "test_set_id": TSID_1,
        },
    ]

    def test_history_defaults_to_failures_only(self, runner, mock_client):
        """Default should show summary for all, but history only for failures."""
        mock_client.get_test_history.return_value = (
            self.HISTORY_DATA, "sanity", STATS_OK)
        result = runner.invoke(main, ["--envelope", "test-history", "test_39b"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["test_name"] == "test_39b"
        assert env["data"]["occurrences"] == 3
        assert env["data"]["summary"]["pass"] == 2
        assert env["data"]["summary"]["fail"] == 1
        assert env["data"]["summary"]["fail_rate_pct"] == pytest.approx(33.3, abs=0.1)
        # History should only contain the failure entry
        assert len(env["data"]["history"]) == 1
        assert env["data"]["history"][0]["status"] == "FAIL"

    def test_history_all_flag(self, runner, mock_client):
        """--all should show all history entries."""
        mock_client.get_test_history.return_value = (
            self.HISTORY_DATA, "sanity", STATS_OK)
        result = runner.invoke(main, ["--envelope", "test-history", "test_39b", "--all"])
        env = _parse_output(result)
        assert len(env["data"]["history"]) == 3

    def test_history_limit(self, runner, mock_client):
        """--limit should cap history entries."""
        many = self.HISTORY_DATA * 5  # 15 entries (5 failures)
        mock_client.get_test_history.return_value = (many, "sanity", STATS_OK)
        result = runner.invoke(main, ["--envelope", "test-history", "test_39b", "--all", "--limit", "3"])
        env = _parse_output(result)
        assert len(env["data"]["history"]) == 3

    def test_history_with_suite_filter(self, runner, mock_client):
        mock_client.get_test_history.return_value = ([], None, STATS_NONE)
        runner.invoke(main, [
            "test-history", "test_1b",
            "--suite", "replay-vbr",
            "--branch", "lustre-reviews",
        ])
        call_args = mock_client.get_test_history.call_args
        assert call_args[1]["test_name"] == "test_1b"
        assert call_args[1]["suite"] == "replay-vbr"
        assert call_args[1]["trigger_job"] == "lustre-reviews"

    def test_history_empty(self, runner, mock_client):
        mock_client.get_test_history.return_value = ([], None, STATS_NONE)
        result = runner.invoke(main, ["--envelope", "test-history", "test_nonexistent"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["occurrences"] == 0
        # no data is not a clean record: a 0.0% rate would read as
        # "this test never fails" when the suite never ran
        assert env["data"]["summary"]["fail_rate_pct"] is None
        assert "no data" in env["data"]["warning"]

    def test_history_suite_absent_warns(self, runner, mock_client):
        """Suite missing from every scanned session must say so."""
        mock_client.get_test_history.return_value = ([], None, STATS_NONE)
        result = runner.invoke(main, [
            "--envelope", "test-history", "test_18e",
            "--suite", "sanity-lfsck",
        ])
        env = _parse_output(result)
        assert env["data"]["sessions_with_suite"] == 0
        assert env["data"]["sessions_scanned"] == 40
        assert "sanity-lfsck" in env["data"]["warning"]
        assert any("lustre-reviews" in a for a in env["next_actions"])

    def test_history_test_absent_but_suite_ran(self, runner, mock_client):
        """Suite ran but test never appeared -- a different message."""
        mock_client.get_test_history.return_value = (
            [], None, {"sessions_scanned": 30, "sessions_with_suite": 12})
        result = runner.invoke(main, [
            "--envelope", "test-history", "test_zzz", "--suite", "sanity",
        ])
        env = _parse_output(result)
        assert env["data"]["sessions_with_suite"] == 12
        assert "did not appear" in env["data"]["warning"]


# -- queue command --


class TestQueue:
    def test_queue_by_review(self, runner, mock_client):
        mock_client.get_test_queues.return_value = [
            {
                "id": "q-1",
                "job": "lustre-reviews",
                "buildno": 12345,
                "test_group": "full",
                "status": "Running",
                "instance": "Onyx Autotest",
                "review_id": 54321,
                "review_patch": 3,
            },
        ]

        with patch("maloo_tool.cli._resolve_review_to_revision",
                    return_value=("abc123def456", "")):
            result = runner.invoke(main, ["--envelope", "queue", "--review", "54321"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 1
        assert env["data"]["queue_entries"][0]["status"] == "Running"
        assert env["data"]["filters"]["review_id"] == "54321"

    def test_queue_by_status(self, runner, mock_client):
        mock_client.get_test_queues.return_value = []
        result = runner.invoke(main, ["--envelope", "queue", "--status", "Queued"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 0

    def test_queue_requires_filter(self, runner, mock_client):
        result = runner.invoke(main, ["--envelope", "queue"])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert "filter" in env["error"]["message"].lower()


# -- top-failures command --


class TestTopFailures:
    def test_top_failures_basic(self, runner, mock_client):
        mock_client.get_top_failures.return_value = (
            [
                {
                    "test_name": "test_39b",
                    "suite": "sanity",
                    "count": 5,
                    "session_count": 3,
                    "statuses": {"CRASH": 5},
                    "error_sample": "crash during test",
                    "example_session_id": SID_1,
                    "example_test_set_id": TSID_1,
                },
                {
                    "test_name": "test_1b",
                    "suite": "replay-vbr",
                    "count": 3,
                    "session_count": 3,
                    "statuses": {"FAIL": 3},
                    "error_sample": "not evicted",
                    "example_session_id": SID_2,
                    "example_test_set_id": TSID_2,
                },
            ],
            10,
            10,
        )

        result = runner.invoke(main, ["--envelope", "top-failures", "lustre-master"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["branch"] == "lustre-master"
        assert env["data"]["sessions_examined"] == 10
        assert len(env["data"]["top_failures"]) == 2
        assert env["data"]["top_failures"][0]["rank"] == 1
        assert env["data"]["top_failures"][0]["test_name"] == "test_39b"

    def test_top_failures_empty(self, runner, mock_client):
        mock_client.get_top_failures.return_value = ([], 0, 0)
        result = runner.invoke(main, ["--envelope", "top-failures"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["top_failures"] == []


# -- retest command --


class TestRetest:
    def test_retest_success(self, runner, mock_client):
        mock_client.retest.return_value = "HTTP 200"
        result = runner.invoke(main, ["--envelope", "retest", SID_1, "LU-19487"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["session_id"] == SID_1
        assert env["data"]["bug_id"] == "LU-19487"


# -- logs command --


class TestLogs:
    def test_logs_zip_archive(self, runner, mock_client):
        """Logs command should download and extract a zip archive."""
        import io
        import zipfile

        # Create a small zip in memory
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("console.log", "test output line 1\ntest output line 2\n")
        mock_client.download_logs.return_value = buf.getvalue()

        result = runner.invoke(main, ["--envelope", "logs", TSID_1, "--output-dir", "/tmp/test_maloo_logs_unit"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["test_set_id"] == TSID_1
        assert len(env["data"]["files"]) >= 1

    def test_logs_download_error(self, runner, mock_client):
        """Logs command should handle download failures."""
        mock_client.download_logs.side_effect = Exception("connection timeout")
        result = runner.invoke(main, ["--envelope", "logs", TSID_1])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert result.exit_code != 0


# -- Client unit tests --


class TestClientPagination:
    """Test the pagination logic in MalooClient."""

    def test_get_sessions_respects_max_records(self):
        """get_sessions should stop fetching when max_records reached."""
        from maloo_tool.client import MalooClient
        from maloo_tool.config import MalooConfig

        config = MalooConfig(
            base_url="https://example.com",
            username="test",
            password="test",
        )
        client = MalooClient(config)

        page_data = [{"id": f"s-{i}"} for i in range(200)]
        client._get = MagicMock(return_value=page_data)

        results = client.get_sessions({}, max_records=50)
        assert len(results) == 50
        assert client._get.call_count == 1

    def test_get_paginated_fetches_multiple_pages(self):
        """_get_paginated should fetch multiple pages."""
        from maloo_tool.client import MalooClient
        from maloo_tool.config import MalooConfig

        config = MalooConfig(
            base_url="https://example.com",
            username="test",
            password="test",
        )
        client = MalooClient(config)

        page1 = [{"id": f"s-{i}"} for i in range(200)]
        page2 = [{"id": f"s-{i}"} for i in range(200, 350)]

        client._get = MagicMock(side_effect=[page1, page2])

        results = client._get_paginated("test_sessions", {})
        assert len(results) == 350
        assert client._get.call_count == 2


# -- UUID extraction --


class TestUUIDExtraction:
    def test_extract_from_url(self, runner, mock_client):
        """Session command should extract UUID from full URL."""
        mock_client.get_session.return_value = {
            "id": SID_1,
            "test_group": "full",
            "test_name": "test",
            "test_host": "host1",
            "submission": "2026-01-01T00:00:00.000Z",
            "duration": 100,
            "enforcing": True,
            "test_sets_passed_count": 1,
            "test_sets_failed_count": 0,
            "test_sets_aborted_count": 0,
            "test_sets_count": 1,
        }
        mock_client.get_test_sets.return_value = []
        mock_client.resolve_test_set_names.return_value = {}

        url = f"https://testing.whamcloud.com/test_sessions/{SID_1}"
        result = runner.invoke(main, ["--envelope", "session", url])
        env = _parse_output(result)
        assert env["ok"] is True
        mock_client.get_session.assert_called_with(SID_1)

    def test_invalid_id(self, runner, mock_client):
        result = runner.invoke(main, ["session", "not-a-uuid"])
        assert result.exit_code != 0


# -- _extract_session_id helper --


class TestExtractSessionId:
    def test_bare_uuid(self):
        sid = _extract_session_id("11111111-1111-1111-1111-111111111111")
        assert sid == "11111111-1111-1111-1111-111111111111"

    def test_url_with_uuid(self):
        url = "https://testing.whamcloud.com/test_sessions/aabbccdd-1234-5678-9abc-def012345678"
        sid = _extract_session_id(url)
        assert sid == "aabbccdd-1234-5678-9abc-def012345678"

    def test_uppercase_uuid(self):
        sid = _extract_session_id("AABBCCDD-1234-5678-9ABC-DEF012345678")
        assert sid == "AABBCCDD-1234-5678-9ABC-DEF012345678"

    def test_invalid_raises(self):
        import click
        with pytest.raises(click.BadParameter, match="Cannot extract session ID"):
            _extract_session_id("not-a-uuid")


# -- _parse_review_arg helper --


class TestParseReviewArg:
    def test_gerrit_url_with_plus(self):
        url = "https://review.whamcloud.com/c/ex/lustre-release/+/64266"
        assert _parse_review_arg(url) == "64266"

    def test_simple_gerrit_url(self):
        url = "https://review.whamcloud.com/64266"
        assert _parse_review_arg(url) == "64266"

    def test_plain_number(self):
        assert _parse_review_arg("64266") == "64266"

    def test_commit_hash(self):
        h = "7b77eeb0190d6d93880951533c2e1d1145780375"
        assert _parse_review_arg(h) == h


# -- _resolve_branch_to_job helper --


class TestResolveBranchToJob:
    def test_already_jenkins_job(self):
        assert _resolve_branch_to_job("lustre-reviews") == "lustre-reviews"

    def test_master(self):
        assert _resolve_branch_to_job("master") == "lustre-reviews"

    def test_b_es6_0(self):
        assert _resolve_branch_to_job("b_es6_0") == "lustre-b_es-reviews"

    def test_b_ieel3_0(self):
        assert _resolve_branch_to_job("b_ieel3_0") == "lustre-b_ieel-reviews"

    def test_unknown_b_es_branch(self):
        """Unknown b_es branch should use heuristic."""
        assert _resolve_branch_to_job("b_es9_0") == "lustre-b_es-reviews"

    def test_unknown_b_ieel_branch(self):
        assert _resolve_branch_to_job("b_ieel9_0") == "lustre-b_ieel-reviews"

    def test_generic_branch(self):
        assert _resolve_branch_to_job("some-branch") == "lustre-some-branch"


# -- raise-bug command --


class TestRaiseBug:
    def test_raise_bug_success(self, runner, mock_client):
        mock_client.raise_bug.return_value = {
            "ticket": "LU-99999",
            "url": "https://jira.whamcloud.com/browse/LU-99999",
            "flash": "Created LU-99999",
        }
        result = runner.invoke(main, ["--envelope", "raise-bug", TSID_1, "--project", "LU", "--summary", "test bug"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["buggable_id"] == TSID_1

    def test_raise_bug_runtime_error(self, runner, mock_client):
        mock_client.raise_bug.side_effect = RuntimeError("JIRA connection failed")
        result = runner.invoke(main, ["--envelope", "raise-bug", TSID_1])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert "JIRA connection" in env["error"]["message"]
        assert result.exit_code != 0

    def test_raise_bug_generic_error(self, runner, mock_client):
        mock_client.raise_bug.side_effect = Exception("unexpected")
        result = runner.invoke(main, ["--envelope", "raise-bug", TSID_1])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert result.exit_code != 0

    def test_raise_bug_with_subtest_type(self, runner, mock_client):
        mock_client.raise_bug.return_value = {
            "ticket": "LU-100", "url": "", "flash": "ok"
        }
        result = runner.invoke(main, [
            "--envelope", "raise-bug", TSID_1, "--type", "SubTest",
            "--summary", "subtest bug",
        ])
        env = _parse_output(result)
        assert env["data"]["buggable_type"] == "SubTest"


# -- queue command: branch resolution --


class TestQueueBranchResolution:
    def test_queue_by_branch(self, runner, mock_client):
        """--branch should resolve branch name to job name."""
        mock_client.get_test_queues.return_value = []
        result = runner.invoke(main, ["--envelope", "queue", "--branch", "master"])
        env = _parse_output(result)
        assert env["ok"] is True
        # Should resolve 'master' to 'lustre-reviews'
        params = mock_client.get_test_queues.call_args[0][0]
        assert params["job"] == "lustre-reviews"

    def test_queue_by_build(self, runner, mock_client):
        mock_client.get_test_queues.return_value = [
            {
                "id": "q1", "job": "lustre-master", "buildno": 27341,
                "test_group": "full", "status": "Queued",
                "review_id": None, "review_patch": None,
            }
        ]
        result = runner.invoke(main, ["--envelope", "queue", "--build", "27341"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["queue_entries"][0]["buildno"] == 27341

    def test_queue_review_resolve_failure(self, runner, mock_client):
        """When gerrit CLI fails to resolve, should error."""
        with patch("maloo_tool.cli._resolve_review_to_revision",
                    return_value=(None, "`gerrit info 64266` exited 1")):
            result = runner.invoke(main, ["--envelope", "queue", "--review", "64266"])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert "resolve" in env["error"]["message"].lower()
        # The reason the lookup failed is what the user has to act on.
        assert "exited 1" in env["error"]["message"]

    def test_queue_review_with_commit_hash(self, runner, mock_client):
        """Commit hash should be passed through without resolution."""
        mock_client.get_test_queues.return_value = []
        result = runner.invoke(main, [
            "--envelope", "queue", "--review", "7b77eeb0190d6d93880951533c2e1d1145780375"
        ])
        env = _parse_output(result)
        params = mock_client.get_test_queues.call_args[0][0]
        assert params["review_id"] == "7b77eeb0190d6d93880951533c2e1d1145780375"

    def test_queue_api_error(self, runner, mock_client):
        mock_client.get_test_queues.side_effect = Exception("API down")
        result = runner.invoke(main, ["--envelope", "queue", "--status", "Running"])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert result.exit_code != 0


class TestResolveReviewToRevision:
    """The gerrit CLI prints the bare payload; --envelope wraps it."""

    def _run(self, stdout="", stderr="", returncode=0):
        from maloo_tool.cli import _resolve_review_to_revision
        proc = SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=returncode
        )
        with patch("subprocess.run", return_value=proc):
            return _resolve_review_to_revision(54749)

    def test_bare_payload(self):
        revision, why = self._run(
            stdout=json.dumps({"change_number": 54749, "current_revision": "68414988"})
        )
        assert revision == "68414988"
        assert why == ""

    def test_envelope_payload(self):
        revision, why = self._run(
            stdout=json.dumps(
                {"ok": True, "data": {"current_revision": "68414988"}}
            )
        )
        assert revision == "68414988"
        assert why == ""

    def test_nonzero_exit_says_why(self):
        revision, why = self._run(stderr="404 Not Found", returncode=1)
        assert revision is None
        assert "exited 1" in why
        assert "404 Not Found" in why

    def test_missing_revision_says_why(self):
        revision, why = self._run(stdout=json.dumps({"change_number": 54749}))
        assert revision is None
        assert "current_revision" in why

    def test_not_installed_says_why(self):
        from maloo_tool.cli import _resolve_review_to_revision
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            revision, why = _resolve_review_to_revision(54749)
        assert revision is None
        assert "not installed" in why


# -- retest command: additional tests --


class TestRetestExtended:
    def test_retest_all_option(self, runner, mock_client):
        mock_client.retest.return_value = "OK"
        result = runner.invoke(main, ["--envelope", "retest", SID_1, "LU-19487", "--option", "all"])
        env = _parse_output(result)
        assert env["data"]["retest_option"] == "all"

    def test_retest_livedebug_option(self, runner, mock_client):
        mock_client.retest.return_value = "OK"
        result = runner.invoke(main, ["--envelope", "retest", SID_1, "LU-19487", "--option", "livedebug"])
        env = _parse_output(result)
        assert env["data"]["retest_option"] == "livedebug"

    def test_retest_extracts_uuid_from_url(self, runner, mock_client):
        mock_client.retest.return_value = "OK"
        url = f"https://testing.whamcloud.com/test_sessions/{SID_1}"
        result = runner.invoke(main, ["--envelope", "retest", url, "LU-100"])
        env = _parse_output(result)
        assert env["data"]["session_id"] == SID_1


# -- logs command: grep tests --


class TestLogsGrep:
    def test_logs_with_grep(self, runner, mock_client):
        """Logs with --grep should search extracted files."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("console.log", "test_81a FAIL\ntest_81b PASS\n")
        mock_client.download_logs.return_value = buf.getvalue()

        result = runner.invoke(main, [
            "--envelope", "logs", TSID_1,
            "--output-dir", "/tmp/test_maloo_grep",
            "--grep", "test_81a",
        ])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["grep_pattern"] == "test_81a"
        assert len(env["data"]["grep_results"]) >= 1
        assert env["data"]["grep_results"][0]["match_count"] >= 1

    def test_logs_grep_no_matches(self, runner, mock_client):
        """Grep with no matches should return empty results."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("console.log", "nothing interesting\n")
        mock_client.download_logs.return_value = buf.getvalue()

        result = runner.invoke(main, [
            "--envelope", "logs", TSID_1,
            "--output-dir", "/tmp/test_maloo_grep2",
            "--grep", "nonexistent_pattern",
        ])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["grep_results"] == []


# -- sessions command: additional coverage --


class TestSessionsExtended:
    def test_sessions_api_error(self, runner, mock_client):
        mock_client.get_sessions.side_effect = Exception("timeout")
        result = runner.invoke(main, ["--envelope", "sessions", "--branch", "lustre-master"])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert result.exit_code != 0

    def test_sessions_no_filters(self, runner, mock_client):
        """Sessions without filters should still work (uses default days)."""
        mock_client.get_sessions.return_value = []
        result = runner.invoke(main, ["--envelope", "sessions"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["filters"] == {}

    def test_sessions_next_actions(self, runner, mock_client):
        """Should suggest next actions when there are results."""
        mock_client.get_sessions.return_value = [
            {
                "id": SID_1, "test_group": "full", "test_name": "test",
                "test_host": "h1", "submission": "2026-01-01", "enforcing": True,
                "test_sets_passed_count": 1, "test_sets_failed_count": 2,
                "test_sets_aborted_count": 0, "test_sets_count": 3,
                "duration": 100, "trigger_job": "lustre-master",
            },
        ]
        result = runner.invoke(main, ["--envelope", "sessions"])
        env = _parse_output(result)
        # Should have next_actions for failed session
        assert env.get("next_actions") is not None
        assert any("failures" in a for a in env["next_actions"])


# -- top-failures command: additional coverage --


class TestTopFailuresExtended:
    def test_top_failures_api_error(self, runner, mock_client):
        mock_client.get_top_failures.side_effect = Exception("connection refused")
        result = runner.invoke(main, ["--envelope", "top-failures"])
        env = json.loads(result.output)
        assert env["ok"] is False
        assert result.exit_code != 0

    def test_top_failures_with_options(self, runner, mock_client):
        mock_client.get_top_failures.return_value = ([], 0, 0)
        result = runner.invoke(main, [
            "--envelope", "top-failures", "lustre-b2_15",
            "--days", "30", "--limit", "5", "--sessions", "100",
        ])
        env = _parse_output(result)
        assert env["data"]["branch"] == "lustre-b2_15"
        assert env["data"]["days"] == 30

    def test_top_failures_next_actions(self, runner, mock_client):
        mock_client.get_top_failures.return_value = (
            [{
                "test_name": "test_1", "suite": "sanity", "count": 3,
                "session_count": 2, "statuses": {"FAIL": 3},
                "error_sample": "err", "example_session_id": SID_1,
                "example_test_set_id": TSID_1,
            }],
            5, 5,
        )
        result = runner.invoke(main, ["--envelope", "top-failures"])
        env = _parse_output(result)
        assert env.get("next_actions") is not None
        assert any("failures" in a for a in env["next_actions"])
        assert any("bugs" in a for a in env["next_actions"])

# -- No-envelope default behavior --


class TestNoEnvelopeDefault:
    """Verify that without --envelope, output is stripped to just data/error."""

    def test_success_outputs_data_only(self, runner, mock_client):
        """Without --envelope, success output should be the data dict directly."""
        mock_client.find_sessions_by_commit.return_value = [
            {
                "id": SID_1,
                "test_group": "full",
                "test_name": "lustre-master--full--1.10",
                "test_host": "host1",
                "submission": "2026-01-15T10:00:00.000Z",
                "enforcing": True,
                "test_sets_passed_count": 5,
                "test_sets_failed_count": 0,
                "test_sets_count": 5,
                "duration": 3600,
            },
        ]

        result = runner.invoke(
            main, ["review", "54321", "--commit", "a" * 40]
        )
        out = json.loads(result.output)
        # Should NOT have envelope keys
        assert "ok" not in out
        assert "meta" not in out
        # Should have the data payload directly
        assert out["review_id"] == 54321
        assert out["session_count"] == 1

    def test_error_outputs_error_only(self, runner, mock_client):
        """Without --envelope, error output should be the error dict directly."""
        result = runner.invoke(main, ["--envelope", "queue"])
        env = json.loads(result.output)
        # With envelope, error has wrapper
        assert env["ok"] is False
        assert "error" in env

        # Without envelope, error output is just the error dict
        result = runner.invoke(main, ["queue"])
        out = json.loads(result.output)
        assert "ok" not in out
        assert "meta" not in out
        assert out["code"] == "MISSING_FILTER"

    def test_envelope_flag_preserves_wrapper(self, runner, mock_client):
        """With --envelope, output should have the full ok/data/meta wrapper."""
        mock_client.find_sessions_by_commit.return_value = []
        result = runner.invoke(
            main, ["--envelope", "review", "99999", "--commit", "a" * 40]
        )
        out = json.loads(result.output)
        assert out["ok"] is True
        assert "data" in out
        assert "meta" in out
