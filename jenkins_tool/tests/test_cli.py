"""Tests for jenkins_tool.cli."""

import json
import pytest
from unittest.mock import MagicMock, patch
from click.testing import CliRunner

from jenkins_tool.cli import (
    main,
    _ts_to_iso,
    _ms_to_human,
    _color_to_status,
    _extract_build_params,
    _extract_build_causes,
    _normalize_build,
)


@pytest.fixture
def runner():
    return CliRunner()


def _parse(result):
    """Parse JSON output from CLI command."""
    return json.loads(result.output)


def _make_env(**overrides):
    """Create env vars dict for CLI invocation."""
    env = {
        "JENKINS_URL": "https://build.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_TOKEN": "testtoken",
    }
    env.update(overrides)
    return env


# -- Helper function tests --


class TestTsToIso:
    def test_converts_ms_timestamp(self):
        # 2023-11-14T22:13:20Z
        assert _ts_to_iso(1700000000000) == "2023-11-14T22:13:20Z"

    def test_none_returns_none(self):
        assert _ts_to_iso(None) is None

    def test_zero_returns_none(self):
        assert _ts_to_iso(0) is None


class TestMsToHuman:
    def test_seconds(self):
        assert _ms_to_human(45000) == "45s"

    def test_minutes(self):
        assert _ms_to_human(125000) == "2m5s"

    def test_hours(self):
        assert _ms_to_human(3661000) == "1h1m"

    def test_none_returns_none(self):
        assert _ms_to_human(None) is None

    def test_zero_returns_none(self):
        assert _ms_to_human(0) is None


class TestColorToStatus:
    def test_blue_is_success(self):
        assert _color_to_status("blue") == "success"

    def test_red_is_failed(self):
        assert _color_to_status("red") == "failed"

    def test_anime_variants(self):
        assert "building" in _color_to_status("blue_anime")
        assert "building" in _color_to_status("red_anime")

    def test_none_is_unknown(self):
        assert _color_to_status(None) == "unknown"

    def test_unknown_color_passthrough(self):
        assert _color_to_status("purple") == "purple"


class TestExtractBuildParams:
    def test_extracts_params(self):
        build = {
            "actions": [
                {
                    "parameters": [
                        {"name": "GERRIT_BRANCH", "value": "master"},
                        {"name": "GERRIT_CHANGE_NUMBER", "value": "54225"},
                    ]
                }
            ]
        }
        params = _extract_build_params(build)
        assert params["GERRIT_BRANCH"] == "master"
        assert params["GERRIT_CHANGE_NUMBER"] == "54225"

    def test_no_actions(self):
        assert _extract_build_params({}) == {}

    def test_empty_parameters(self):
        build = {"actions": [{"parameters": []}]}
        assert _extract_build_params(build) == {}

    def test_none_value_becomes_empty(self):
        build = {
            "actions": [
                {"parameters": [{"name": "FOO", "value": None}]}
            ]
        }
        assert _extract_build_params(build)["FOO"] == ""


class TestExtractBuildCauses:
    def test_extracts_causes(self):
        build = {
            "actions": [
                {
                    "causes": [
                        {"shortDescription": "Triggered by Gerrit"}
                    ]
                }
            ]
        }
        causes = _extract_build_causes(build)
        assert causes == ["Triggered by Gerrit"]

    def test_no_causes(self):
        assert _extract_build_causes({}) == []


class TestNormalizeBuild:
    def test_basic_fields(self):
        build = {
            "number": 100,
            "result": "SUCCESS",
            "building": False,
            "timestamp": 1700000000000,
            "duration": 60000,
            "url": "https://build.example.com/job/foo/100/",
            "actions": [],
            "changeSet": {"items": []},
        }
        result = _normalize_build(build, job_name="foo")
        assert result["number"] == 100
        assert result["result"] == "SUCCESS"
        assert result["building"] is False
        assert result["job"] == "foo"
        assert result["duration"] == "1m0s"

    def test_gerrit_info_extracted(self):
        build = {
            "number": 100,
            "result": "FAILURE",
            "building": False,
            "timestamp": None,
            "duration": None,
            "url": "",
            "actions": [
                {
                    "parameters": [
                        {"name": "GERRIT_CHANGE_NUMBER", "value": "54225"},
                        {"name": "GERRIT_BRANCH", "value": "master"},
                        {"name": "GERRIT_PATCHSET_NUMBER", "value": "3"},
                    ]
                }
            ],
            "changeSet": {"items": []},
        }
        result = _normalize_build(build)
        assert result["gerrit"]["change"] == "54225"
        assert result["gerrit"]["branch"] == "master"
        assert result["gerrit"]["patchset"] == "3"

    def test_matrix_runs_sorted(self):
        build = {
            "number": 100,
            "result": None,
            "building": True,
            "timestamp": None,
            "duration": None,
            "url": "",
            "actions": [],
            "changeSet": {"items": []},
            "runs": [
                {
                    "number": 100,
                    "result": "SUCCESS",
                    "building": False,
                    "duration": 1000,
                    "url": "https://build.example.com/job/foo/config_a/100/",
                    "builtOn": "node1",
                },
                {
                    "number": 100,
                    "result": "FAILURE",
                    "building": False,
                    "duration": 2000,
                    "url": "https://build.example.com/job/foo/config_b/100/",
                    "builtOn": "node2",
                },
            ],
        }
        result = _normalize_build(build)
        assert result["runs_total"] == 2
        assert result["runs_failed"] == 1
        assert result["runs_success"] == 1
        # failures sorted first
        assert result["runs"][0]["result"] == "FAILURE"

    def test_runs_from_other_build_numbers_excluded(self):
        build = {
            "number": 100,
            "result": "SUCCESS",
            "building": False,
            "timestamp": None,
            "duration": None,
            "url": "",
            "actions": [],
            "changeSet": {"items": []},
            "runs": [
                {"number": 100, "result": "SUCCESS", "building": False,
                 "duration": 1000, "url": "https://x/job/foo/cfg/100/"},
                {"number": 99, "result": "FAILURE", "building": False,
                 "duration": 1000, "url": "https://x/job/foo/cfg/99/"},
            ],
        }
        result = _normalize_build(build)
        assert result["runs_total"] == 1

    def test_commits_extracted(self):
        build = {
            "number": 100,
            "result": "SUCCESS",
            "building": False,
            "timestamp": None,
            "duration": None,
            "url": "",
            "actions": [],
            "changeSet": {
                "items": [
                    {
                        "commitId": "abc123def456",
                        "msg": "Fix the thing",
                        "author": {"fullName": "Test User"},
                    }
                ]
            },
        }
        result = _normalize_build(build)
        assert len(result["commits"]) == 1
        assert result["commits"][0]["id"] == "abc123def456"[:12]
        assert result["commits"][0]["message"] == "Fix the thing"


# -- CLI command tests --


class TestJobsCommand:
    @patch("jenkins_tool.cli._make_client")
    def test_jobs_success(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_jobs.return_value = [
            {
                "name": "lustre-master",
                "color": "blue",
                "url": "https://build.example.com/job/lustre-master/",
                "healthReport": [{"score": 80, "description": "Build stability: 80%"}],
            }
        ]
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["jobs"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 1
        assert env["data"]["jobs"][0]["name"] == "lustre-master"
        assert env["data"]["jobs"][0]["status"] == "success"

    @patch("jenkins_tool.cli._make_client")
    def test_jobs_with_view(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_view.return_value = {
            "jobs": [{"name": "foo", "color": "blue", "url": "", "healthReport": []}]
        }
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["jobs", "--view", "myview"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is True
        mock_client.get_view.assert_called_with("myview")

    @patch("jenkins_tool.cli._make_client")
    def test_jobs_api_error(self, mock_make, runner):
        import requests
        mock_make.side_effect = requests.HTTPError(
            response=MagicMock(status_code=500)
        )

        result = runner.invoke(main, ["jobs"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is False
        assert env["error"]["code"] == "API_ERROR"


class TestBuildsCommand:
    @patch("jenkins_tool.cli._make_client")
    def test_builds_success(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_builds.return_value = [
            {
                "number": 100,
                "result": "SUCCESS",
                "building": False,
                "timestamp": 1700000000000,
                "duration": 60000,
                "url": "https://build.example.com/job/foo/100/",
            }
        ]
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["builds", "foo"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["job"] == "foo"
        assert env["data"]["count"] == 1

    @patch("jenkins_tool.cli._make_client")
    def test_builds_not_found(self, mock_make, runner):
        import requests
        resp = MagicMock()
        resp.status_code = 404
        mock_client = MagicMock()
        mock_client.get_builds.side_effect = requests.HTTPError(response=resp)
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["builds", "nonexistent"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is False
        assert env["error"]["code"] == "NOT_FOUND"


class TestBuildCommand:
    @patch("jenkins_tool.cli._make_client")
    def test_build_detail(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_build.return_value = {
            "number": 100,
            "result": "SUCCESS",
            "building": False,
            "timestamp": 1700000000000,
            "duration": 60000,
            "url": "https://build.example.com/job/foo/100/",
            "actions": [],
            "runs": [],
            "changeSet": {"items": []},
        }
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["build", "foo", "100"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["number"] == 100
        assert env["data"]["job"] == "foo"


class TestConsoleCommand:
    @patch("jenkins_tool.cli._make_client")
    def test_console_tail(self, mock_make, runner):
        mock_client = MagicMock()
        lines = [f"line {i}" for i in range(300)]
        mock_client.get_console_text.return_value = "\n".join(lines)
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["console", "foo", "100"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["total_lines"] == 300
        assert len(env["data"]["lines"]) == 200  # default tail

    @patch("jenkins_tool.cli._make_client")
    def test_console_head(self, mock_make, runner):
        mock_client = MagicMock()
        lines = [f"line {i}" for i in range(300)]
        mock_client.get_console_text.return_value = "\n".join(lines)
        mock_make.return_value = mock_client

        result = runner.invoke(
            main, ["console", "foo", "100", "--head", "10"], env=_make_env()
        )
        env = _parse(result)
        assert env["ok"] is True
        assert len(env["data"]["lines"]) == 10

    @patch("jenkins_tool.cli._make_client")
    def test_console_grep(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_console_text.return_value = (
            "INFO: starting\nERROR: something broke\nINFO: done"
        )
        mock_make.return_value = mock_client

        result = runner.invoke(
            main, ["console", "foo", "100", "--grep", "error"], env=_make_env()
        )
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["match_count"] == 1
        assert "ERROR" in env["data"]["matches"][0]["text"]

    @patch("jenkins_tool.cli._make_client")
    def test_console_invalid_regex(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_console_text.return_value = "line"
        mock_make.return_value = mock_client

        result = runner.invoke(
            main, ["console", "foo", "100", "--grep", "[invalid"],
            env=_make_env(),
        )
        env = _parse(result)
        assert env["ok"] is False
        assert env["error"]["code"] == "INVALID_INPUT"


class TestReviewCommand:
    @patch("jenkins_tool.cli._make_client")
    def test_review_finds_builds(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.find_review_builds.return_value = [
            {
                "_job_name": "lustre-reviews",
                "number": 100,
                "result": "SUCCESS",
                "building": False,
                "timestamp": 1700000000000,
                "duration": 60000,
                "url": "https://build.example.com/job/lustre-reviews/100/",
                "actions": [],
                "runs": [],
                "changeSet": {"items": []},
            }
        ]
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["review", "54225"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["change_number"] == 54225
        assert env["data"]["count"] == 1

    @patch("jenkins_tool.cli._make_client")
    def test_review_no_builds(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.find_review_builds.return_value = []
        mock_make.return_value = mock_client

        result = runner.invoke(main, ["review", "99999"], env=_make_env())
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["count"] == 0


class TestAbortCommand:
    @patch("jenkins_tool.cli._make_client")
    def test_abort_running_build(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_build.return_value = {
            "building": True,
            "number": 100,
            "runs": [],
        }
        mock_client.abort_build.return_value = 302
        mock_make.return_value = mock_client

        result = runner.invoke(
            main, ["abort", "foo", "100"], env=_make_env()
        )
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["aborted"] is True

    @patch("jenkins_tool.cli._make_client")
    def test_abort_not_running(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.get_build.return_value = {
            "building": False,
            "result": "SUCCESS",
            "runs": [],
        }
        mock_make.return_value = mock_client

        result = runner.invoke(
            main, ["abort", "foo", "100"], env=_make_env()
        )
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["aborted"] is False


class TestRetriggerCommand:
    @patch("jenkins_tool.cli._make_client")
    def test_retrigger_success(self, mock_make, runner):
        mock_client = MagicMock()
        mock_client.retrigger_build.return_value = (
            "https://build.example.com/job/foo/101/"
        )
        mock_make.return_value = mock_client

        result = runner.invoke(
            main, ["retrigger", "foo", "100"], env=_make_env()
        )
        env = _parse(result)
        assert env["ok"] is True
        assert env["data"]["success"] is True

    @patch("jenkins_tool.cli._make_client")
    def test_retrigger_not_found(self, mock_make, runner):
        import requests
        resp = MagicMock()
        resp.status_code = 404
        mock_client = MagicMock()
        mock_client.retrigger_build.side_effect = requests.HTTPError(
            response=resp
        )
        mock_make.return_value = mock_client

        result = runner.invoke(
            main, ["retrigger", "foo", "100"], env=_make_env()
        )
        env = _parse(result)
        assert env["ok"] is False
        assert env["error"]["code"] == "NOT_FOUND"


class TestConfigValidation:
    def test_missing_credentials_raises(self):
        from jenkins_tool.config import JenkinsConfig
        with pytest.raises(ValueError, match="credentials required"):
            JenkinsConfig(
                base_url="https://build.example.com",
                user="",
                token="",
            )

    def test_url_trailing_slash_stripped(self):
        from jenkins_tool.config import JenkinsConfig
        config = JenkinsConfig(
            base_url="https://build.example.com/",
            user="test",
            token="test",
        )
        assert not config.base_url.endswith("/")
