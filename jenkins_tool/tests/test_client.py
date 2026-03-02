"""Tests for jenkins_tool.client."""

import pytest
import requests
from unittest.mock import MagicMock, patch

from jenkins_tool.client import JenkinsClient
from jenkins_tool.config import JenkinsConfig


@pytest.fixture
def config():
    return JenkinsConfig(
        base_url="https://build.example.com",
        user="testuser",
        token="testtoken",
    )


@pytest.fixture
def client(config):
    c = JenkinsClient(config)
    c.session = MagicMock()
    return c


class TestClientInit:
    def test_creates_session_with_auth(self, config):
        c = JenkinsClient(config)
        assert c.session.auth == ("testuser", "testtoken")
        assert c.timeout == 30

    def test_custom_timeout(self, config):
        c = JenkinsClient(config, timeout=60)
        assert c.timeout == 60


class TestGetJson:
    def test_appends_api_json(self, client):
        resp = MagicMock()
        resp.json.return_value = {"jobs": []}
        client.session.get.return_value = resp

        client._get_json("/job/foo")
        url = client.session.get.call_args[0][0]
        assert url.endswith("/api/json")

    def test_does_not_double_api_json(self, client):
        resp = MagicMock()
        resp.json.return_value = {}
        client.session.get.return_value = resp

        client._get_json("/job/foo/api/json")
        url = client.session.get.call_args[0][0]
        assert url.count("/api/json") == 1

    def test_passes_params(self, client):
        resp = MagicMock()
        resp.json.return_value = {}
        client.session.get.return_value = resp

        client._get_json("/api/json", params={"tree": "jobs[name]"})
        _, kwargs = client.session.get.call_args
        assert kwargs["params"] == {"tree": "jobs[name]"}

    def test_raises_on_http_error(self, client):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=500)
        )
        client.session.get.return_value = resp

        with pytest.raises(requests.HTTPError):
            client._get_json("/api/json")


class TestGetJobs:
    def test_returns_jobs_list(self, client):
        resp = MagicMock()
        resp.json.return_value = {
            "jobs": [
                {"name": "lustre-master", "color": "blue"},
                {"name": "lustre-reviews", "color": "red"},
            ]
        }
        client.session.get.return_value = resp

        jobs = client.get_jobs()
        assert len(jobs) == 2
        assert jobs[0]["name"] == "lustre-master"

    def test_empty_jobs(self, client):
        resp = MagicMock()
        resp.json.return_value = {"jobs": []}
        client.session.get.return_value = resp

        assert client.get_jobs() == []


class TestGetBuilds:
    def test_returns_builds(self, client):
        resp = MagicMock()
        resp.json.return_value = {
            "builds": [
                {"number": 100, "result": "SUCCESS"},
                {"number": 99, "result": "FAILURE"},
            ]
        }
        client.session.get.return_value = resp

        builds = client.get_builds("lustre-master", limit=10)
        assert len(builds) == 2
        assert builds[0]["number"] == 100

    def test_limit_in_tree_query(self, client):
        resp = MagicMock()
        resp.json.return_value = {"builds": []}
        client.session.get.return_value = resp

        client.get_builds("foo", limit=5)
        _, kwargs = client.session.get.call_args
        assert "{0,5}" in kwargs["params"]["tree"]


class TestGetBuild:
    def test_returns_build_detail(self, client):
        build_data = {
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
        resp = MagicMock()
        resp.json.return_value = build_data
        client.session.get.return_value = resp

        result = client.get_build("foo", 100)
        assert result["number"] == 100
        assert result["result"] == "SUCCESS"

    def test_build_number_aliases(self, client):
        resp = MagicMock()
        resp.json.return_value = {"number": 100}
        client.session.get.return_value = resp

        client.get_build("foo", "lastBuild")
        url = client.session.get.call_args[0][0]
        assert "/lastBuild/" in url


class TestGetConsoleText:
    def test_returns_text(self, client):
        resp = MagicMock()
        resp.text = "line1\nline2\nline3"
        client.session.get.return_value = resp

        text = client.get_console_text("foo", 100)
        assert text == "line1\nline2\nline3"

    def test_builds_correct_url(self, client):
        resp = MagicMock()
        resp.text = ""
        client.session.get.return_value = resp

        client.get_console_text("foo", "lastBuild")
        url = client.session.get.call_args[0][0]
        assert "/job/foo/lastBuild/consoleText" in url


class TestGetRunConsoleText:
    def test_appends_console_text(self, client):
        resp = MagicMock()
        resp.text = "run output"
        client.session.get.return_value = resp

        text = client.get_run_console_text(
            "https://build.example.com/job/foo/config/100"
        )
        assert text == "run output"
        url = client.session.get.call_args[0][0]
        assert url.endswith("/consoleText")


class TestAbortBuild:
    def test_posts_to_stop(self, client):
        # get_crumb
        crumb_resp = MagicMock()
        crumb_resp.json.return_value = {
            "crumbRequestField": "Jenkins-Crumb",
            "crumb": "abc123",
        }
        # stop POST
        stop_resp = MagicMock()
        stop_resp.status_code = 302

        client.session.get.return_value = crumb_resp
        client.session.post.return_value = stop_resp

        status = client.abort_build("foo", 100)
        assert status == 302
        url = client.session.post.call_args[0][0]
        assert "/job/foo/100/stop" in url

    def test_kill_build(self, client):
        crumb_resp = MagicMock()
        crumb_resp.json.return_value = {
            "crumbRequestField": "Jenkins-Crumb",
            "crumb": "abc123",
        }
        kill_resp = MagicMock()
        kill_resp.status_code = 200

        client.session.get.return_value = crumb_resp
        client.session.post.return_value = kill_resp

        status = client.kill_build("foo", 100)
        assert status == 200
        url = client.session.post.call_args[0][0]
        assert "/job/foo/100/kill" in url


class TestRetrigger:
    def test_retrigger_returns_location(self, client):
        crumb_resp = MagicMock()
        crumb_resp.json.return_value = {
            "crumbRequestField": "Jenkins-Crumb",
            "crumb": "abc123",
        }
        retrigger_resp = MagicMock()
        retrigger_resp.status_code = 302
        retrigger_resp.headers = {
            "Location": "https://build.example.com/job/foo/101/"
        }

        client.session.get.return_value = crumb_resp
        client.session.post.return_value = retrigger_resp

        loc = client.retrigger_build("foo", 100)
        assert "101" in loc

    def test_retrigger_no_location(self, client):
        crumb_resp = MagicMock()
        crumb_resp.json.return_value = {
            "crumbRequestField": "Jenkins-Crumb",
            "crumb": "abc123",
        }
        retrigger_resp = MagicMock()
        retrigger_resp.status_code = 200
        retrigger_resp.headers = {}

        client.session.get.return_value = crumb_resp
        client.session.post.return_value = retrigger_resp

        loc = client.retrigger_build("foo", 100)
        assert loc == "HTTP 200"


class TestFindBuildsByGerritChange:
    def test_finds_matching_builds(self, client):
        builds_resp = MagicMock()
        builds_resp.json.return_value = {
            "builds": [
                {"number": 100},
                {"number": 99},
            ]
        }

        detail_100 = {
            "number": 100,
            "actions": [
                {"parameters": [
                    {"name": "GERRIT_CHANGE_NUMBER", "value": "54225"}
                ]}
            ],
        }
        detail_99 = {
            "number": 99,
            "actions": [
                {"parameters": [
                    {"name": "GERRIT_CHANGE_NUMBER", "value": "54000"}
                ]}
            ],
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "/99/" in url:
                resp.json.return_value = detail_99
            elif "/100/" in url:
                resp.json.return_value = detail_100
            else:
                resp.json.return_value = builds_resp.json.return_value
            return resp

        client.session.get.side_effect = mock_get

        matches = client.find_builds_by_gerrit_change("foo", 54225)
        assert len(matches) == 1
        assert matches[0]["number"] == 100

    def test_no_matches(self, client):
        builds_resp = MagicMock()
        builds_resp.json.return_value = {"builds": [{"number": 100}]}

        detail_100 = {
            "number": 100,
            "actions": [
                {"parameters": [
                    {"name": "GERRIT_CHANGE_NUMBER", "value": "99999"}
                ]}
            ],
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "/100/" in url:
                resp.json.return_value = detail_100
            else:
                resp.json.return_value = builds_resp.json.return_value
            return resp

        client.session.get.side_effect = mock_get

        matches = client.find_builds_by_gerrit_change("foo", 54225)
        assert len(matches) == 0
