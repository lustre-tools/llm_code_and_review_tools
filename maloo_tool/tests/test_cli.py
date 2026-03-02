"""Tests for Maloo CLI commands.

All tests mock the MalooClient to avoid hitting the real API.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from maloo_tool.cli import main

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

        result = runner.invoke(main, ["session", SID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["session_id"] == SID_1
        assert env["data"]["suites"][0]["name"] == "sanity"

    def test_session_not_found(self, runner, mock_client):
        mock_client.get_session.return_value = None
        result = runner.invoke(main, ["session", SID_1])
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
        mock_client.get_subtests.return_value = [
            {
                "sub_test_script_id": "sub-39b",
                "status": "FAIL",
                "error": "assertion failed",
                "duration": 30,
                "return_code": 1,
                "order": 5,
            },
        ]
        mock_client.resolve_subtest_names.return_value = {
            "sub-39b": "test_39b",
        }

        result = runner.invoke(main, ["failures", SID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert len(env["data"]["failed_suites"]) == 1
        assert env["data"]["failed_suites"][0]["failed_subtests"][0]["name"] == "test_39b"

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

        result = runner.invoke(main, ["failures", SID_1])
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
                "sub_test_script_id": "sub-1",
                "status": "PASS",
                "error": "",
                "duration": 10,
                "return_code": 0,
                "order": 0,
            },
            {
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
        result = runner.invoke(main, ["subtests", TSID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["suite"] == "sanity"
        assert env["data"]["total"] == 2
        assert env["data"]["shown"] == 1
        assert env["data"]["filter"] == "FAIL"
        assert env["data"]["subtests"][0]["name"] == "test_1b"

    def test_subtests_all_flag(self, runner, mock_client):
        """--all should show all subtests regardless of status."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["subtests", TSID_1, "--all"])
        env = _parse_output(result)
        assert env["data"]["shown"] == 2
        assert env["data"]["filter"] is None

    def test_subtests_status_filter(self, runner, mock_client):
        """Explicit --status filter should work."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["subtests", TSID_1, "--status", "PASS"])
        env = _parse_output(result)
        assert env["data"]["shown"] == 1
        assert env["data"]["subtests"][0]["name"] == "test_1a"
        assert env["data"]["filter"] == "PASS"

    def test_subtests_all_overrides_status(self, runner, mock_client):
        """--all should override --status."""
        self._setup_subtests(mock_client)
        result = runner.invoke(main, ["subtests", TSID_1, "--all", "--status", "PASS"])
        env = _parse_output(result)
        assert env["data"]["shown"] == 2
        assert env["data"]["filter"] is None


# -- review command --


class TestReview:
    def test_review_found(self, runner, mock_client):
        mock_client.find_sessions_by_review.return_value = [
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

        result = runner.invoke(main, ["review", "54321"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["review_id"] == 54321
        assert env["data"]["session_count"] == 1

    def test_review_not_found(self, runner, mock_client):
        mock_client.find_sessions_by_review.return_value = []
        result = runner.invoke(main, ["review", "99999"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["sessions"] == []


# -- bugs command --


class TestBugs:
    def test_bugs_found(self, runner, mock_client):
        mock_client.get_bug_links.return_value = [
            {"bug_upstream_id": "LU-12345", "buggable_id": TSID_1},
        ]
        result = runner.invoke(main, ["bugs", TSID_1])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 1

    def test_bugs_empty(self, runner, mock_client):
        mock_client.get_bug_links.return_value = []
        result = runner.invoke(main, ["bugs", TSID_1])
        env = _parse_output(result)
        assert env["data"]["count"] == 0


# -- link-bug command --


class TestLinkBug:
    def test_link_bug_success(self, runner, mock_client):
        mock_client.create_bug_link.return_value = "OK"
        result = runner.invoke(main, ["link-bug", TSID_1, "LU-12345"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["bug"] == "LU-12345"

    def test_link_bug_error(self, runner, mock_client):
        mock_client.create_bug_link.return_value = "ERROR: bug not found"
        result = runner.invoke(main, ["link-bug", TSID_1, "LU-99999"])
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

        result = runner.invoke(main, ["sessions", "--branch", "lustre-master"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 2
        assert env["data"]["filters"]["branch"] == "lustre-master"
        assert env["data"]["sessions"][0]["trigger_job"] == "lustre-master"

    def test_sessions_failed_filter(self, runner, mock_client):
        mock_client.get_sessions.return_value = []
        result = runner.invoke(main, ["sessions", "--branch", "lustre-master", "--failed"])
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
        mock_client.get_test_history.return_value = (self.HISTORY_DATA, "sanity")
        result = runner.invoke(main, ["test-history", "test_39b"])
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
        mock_client.get_test_history.return_value = (self.HISTORY_DATA, "sanity")
        result = runner.invoke(main, ["test-history", "test_39b", "--all"])
        env = _parse_output(result)
        assert len(env["data"]["history"]) == 3

    def test_history_limit(self, runner, mock_client):
        """--limit should cap history entries."""
        many = self.HISTORY_DATA * 5  # 15 entries (5 failures)
        mock_client.get_test_history.return_value = (many, "sanity")
        result = runner.invoke(main, ["test-history", "test_39b", "--all", "--limit", "3"])
        env = _parse_output(result)
        assert len(env["data"]["history"]) == 3

    def test_history_with_suite_filter(self, runner, mock_client):
        mock_client.get_test_history.return_value = ([], None)
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
        mock_client.get_test_history.return_value = ([], None)
        result = runner.invoke(main, ["test-history", "test_nonexistent"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["occurrences"] == 0
        assert env["data"]["summary"]["fail_rate_pct"] == 0.0


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
                    return_value="abc123def456"):
            result = runner.invoke(main, ["queue", "--review", "54321"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 1
        assert env["data"]["queue_entries"][0]["status"] == "Running"
        assert env["data"]["filters"]["review_id"] == "54321"

    def test_queue_by_status(self, runner, mock_client):
        mock_client.get_test_queues.return_value = []
        result = runner.invoke(main, ["queue", "--status", "Queued"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 0

    def test_queue_requires_filter(self, runner, mock_client):
        result = runner.invoke(main, ["queue"])
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

        result = runner.invoke(main, ["top-failures", "lustre-master"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["branch"] == "lustre-master"
        assert env["data"]["sessions_examined"] == 10
        assert len(env["data"]["top_failures"]) == 2
        assert env["data"]["top_failures"][0]["rank"] == 1
        assert env["data"]["top_failures"][0]["test_name"] == "test_39b"

    def test_top_failures_empty(self, runner, mock_client):
        mock_client.get_top_failures.return_value = ([], 0, 0)
        result = runner.invoke(main, ["top-failures"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["top_failures"] == []


# -- retest command --


class TestRetest:
    def test_retest_success(self, runner, mock_client):
        mock_client.retest.return_value = "HTTP 200"
        result = runner.invoke(main, ["retest", SID_1, "LU-19487"])
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

        result = runner.invoke(main, ["logs", TSID_1, "--output-dir", "/tmp/test_maloo_logs_unit"])
        env = _parse_output(result)
        assert env["ok"] is True
        assert env["data"]["test_set_id"] == TSID_1
        assert len(env["data"]["files"]) >= 1

    def test_logs_download_error(self, runner, mock_client):
        """Logs command should handle download failures."""
        mock_client.download_logs.side_effect = Exception("connection timeout")
        result = runner.invoke(main, ["logs", TSID_1])
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
        result = runner.invoke(main, ["session", url])
        env = _parse_output(result)
        assert env["ok"] is True
        mock_client.get_session.assert_called_with(SID_1)

    def test_invalid_id(self, runner, mock_client):
        result = runner.invoke(main, ["session", "not-a-uuid"])
        assert result.exit_code != 0
