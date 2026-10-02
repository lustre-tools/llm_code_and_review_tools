"""Retried GETs, the split timeout and the script-name cache."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from llm_tool_common.errors import ConfigError
from maloo_tool.cli import main
from maloo_tool.client import GET_ATTEMPTS, MalooClient
from maloo_tool.config import MalooConfig, load_config

SID = "11111111-1111-1111-1111-111111111111"
TSID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BASE = "https://testing.example.com"


def _resp(data, status=200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = data
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status} Server Error", response=resp
        )
    return resp


def _client():
    c = MalooClient(MalooConfig(BASE, "u", "p"))
    c.session = MagicMock(spec=requests.Session)
    return c


class TestRetry:
    def test_dropped_connection_is_retried(self, _no_sleep):
        c = _client()
        c.session.get.side_effect = [
            requests.ConnectionError("reset by peer"),
            _resp([{"id": "a"}]),
        ]
        assert c._get("test_sessions") == [{"id": "a"}]
        assert c.session.get.call_count == 2
        _no_sleep.assert_called_once()

    @pytest.mark.parametrize("exc", [
        requests.ConnectTimeout("connect"),
        requests.ReadTimeout("read"),
        requests.exceptions.ChunkedEncodingError("short body"),
    ])
    def test_timeouts_and_short_bodies_are_retried(self, exc):
        c = _client()
        c.session.get.side_effect = [exc, _resp([])]
        assert c._get("test_sessions") == []

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_gateway_errors_are_retried(self, status):
        c = _client()
        c.session.get.side_effect = [_resp(None, status), _resp([{"id": 1}])]
        assert c._get("test_sets") == [{"id": 1}]

    def test_other_errors_are_not_retried(self):
        c = _client()
        c.session.get.return_value = _resp(None, 500)
        with pytest.raises(requests.HTTPError):
            c._get("test_sets")
        assert c.session.get.call_count == 1

    def test_gives_up_after_the_last_attempt(self, _no_sleep):
        c = _client()
        c.session.get.side_effect = requests.ConnectionError("reset")
        with pytest.raises(requests.ConnectionError, match="after 4 attempts"):
            c._get("test_sets")
        assert c.session.get.call_count == GET_ATTEMPTS
        assert _no_sleep.call_count == GET_ATTEMPTS - 1

    def test_short_bodies_to_the_end_are_a_connection_error(self):
        c = _client()
        c.session.get.side_effect = (
            requests.exceptions.ChunkedEncodingError("short")
        )
        with pytest.raises(requests.ConnectionError):
            c._get("test_sets")

    def test_gateway_error_to_the_end_is_an_http_error(self):
        c = _client()
        c.session.get.return_value = _resp(None, 503)
        with pytest.raises(requests.HTTPError):
            c._get("test_sets")
        assert c.session.get.call_count == GET_ATTEMPTS

    def test_split_timeout_is_passed(self):
        c = MalooClient(MalooConfig(BASE, "u", "p", timeout=(3.0, 90.0)))
        c.session = MagicMock(spec=requests.Session)
        c.session.get.return_value = _resp([])
        c._get("test_sets")
        assert c.session.get.call_args.kwargs["timeout"] == (3.0, 90.0)


class TestTimeoutConfig:
    @pytest.fixture(autouse=True)
    def _creds(self, monkeypatch):
        monkeypatch.setenv("MALOO_USER", "u")
        monkeypatch.setenv("MALOO_PASS", "p")

    def test_default(self):
        assert load_config().timeout == (10.0, 60.0)

    def test_read_only(self, monkeypatch):
        monkeypatch.setenv("MALOO_TIMEOUT", "120")
        assert load_config().timeout == (10.0, 120.0)

    def test_connect_and_read(self, monkeypatch):
        monkeypatch.setenv("MALOO_TIMEOUT", "5, 90")
        assert load_config().timeout == (5.0, 90.0)

    @pytest.mark.parametrize("bad", ["x", "0", "1,2,3", "-4", "5,"])
    def test_bad_value(self, monkeypatch, bad):
        monkeypatch.setenv("MALOO_TIMEOUT", bad)
        with pytest.raises(ConfigError, match="MALOO_TIMEOUT"):
            load_config()


class TestScriptNameCache:
    def test_lookup_is_made_once_per_client(self):
        c = _client()
        c.session.get.return_value = _resp([{"id": "st1", "name": "test_1"}])
        for _ in range(3):
            assert c.get_sub_test_script("st1")["name"] == "test_1"
        assert c.session.get.call_count == 1

    def test_unknown_script_is_not_cached(self):
        c = _client()
        c.session.get.return_value = _resp([])
        assert c.get_sub_test_script("nope") is None
        assert c.get_sub_test_script("nope") is None
        assert c.session.get.call_count == 2

    def test_names_survive_on_disk(self, tmp_path):
        c = _client()
        c.session.get.side_effect = [
            _resp([{"id": "st1", "name": "test_1"}]),
            _resp([{"id": "sc1", "name": "sanity"}]),
        ]
        c.get_sub_test_script("st1")
        c.get_test_set_script("sc1")
        c.save_script_names()

        path = tmp_path / "cache" / "maloo-tool" / "script-names.json"
        assert json.loads(path.read_text()) == {BASE: {
            "test_set_scripts": {"sc1": "sanity"},
            "sub_test_scripts": {"st1": "test_1"},
        }}

        d = _client()
        assert d.get_sub_test_script("st1") == {"id": "st1", "name": "test_1"}
        assert d.get_test_set_script("sc1") == {"id": "sc1", "name": "sanity"}
        d.session.get.assert_not_called()

    def test_saving_merges_with_another_run(self):
        a, b = _client(), _client()
        a.session.get.return_value = _resp([{"id": "st1", "name": "test_1"}])
        b.session.get.return_value = _resp([{"id": "st2", "name": "test_2"}])
        a.get_sub_test_script("st1")
        b.get_sub_test_script("st2")
        a.save_script_names()
        b.save_script_names()
        c = _client()
        assert c.get_sub_test_script("st1")["name"] == "test_1"
        assert c.get_sub_test_script("st2")["name"] == "test_2"
        c.session.get.assert_not_called()

    def test_servers_are_kept_apart(self):
        a = _client()
        a.session.get.return_value = _resp([{"id": "st1", "name": "test_1"}])
        a.get_sub_test_script("st1")
        a.save_script_names()
        other = MalooClient(MalooConfig("https://other.example.com", "u", "p"))
        other.session = MagicMock(spec=requests.Session)
        other.session.get.return_value = _resp([])
        assert other.get_sub_test_script("st1") is None

    def test_corrupt_cache_is_ignored_and_replaced(self, tmp_path):
        path = tmp_path / "cache" / "maloo-tool" / "script-names.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        c = _client()
        c.session.get.return_value = _resp([{"id": "st1", "name": "test_1"}])
        assert c.get_sub_test_script("st1")["name"] == "test_1"
        c.save_script_names()
        assert json.loads(path.read_text())[BASE]["sub_test_scripts"] == {
            "st1": "test_1"
        }

    def test_unwritable_cache_is_skipped(self, tmp_path):
        (tmp_path / "cache").write_text("a file, not a directory")
        c = _client()
        c.session.get.return_value = _resp([{"id": "st1", "name": "test_1"}])
        c.get_sub_test_script("st1")
        c.save_script_names()


def _top_failures_server(drops):
    """A fake Maloo for top-failures; drops the first `drops` requests."""
    calls = {"n": 0}

    def get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] <= drops:
            raise requests.ConnectionError("Connection reset by peer")
        endpoint = url.rsplit("/", 1)[1]
        offset = (params or {}).get("offset", 0)
        if endpoint == "test_sessions":
            rows = [{"id": SID}]
        elif endpoint == "test_sets":
            rows = [{"id": TSID, "status": "FAIL",
                     "test_set_script_id": "sc1"}]
        elif endpoint == "sub_tests":
            rows = [{"id": "x", "status": "FAIL", "sub_test_script_id": "st1",
                     "error": "boom"}]
        elif endpoint == "test_set_scripts":
            return _resp([{"id": "sc1", "name": "sanity"}])
        elif endpoint == "sub_test_scripts":
            return _resp([{"id": "st1", "name": "test_1"}])
        else:
            rows = []
        return _resp(rows if not offset else [])

    return get, calls


class TestTopFailuresOverAFlakyServer:
    def _run(self, drops, monkeypatch):
        monkeypatch.setenv("MALOO_URL", BASE)
        monkeypatch.setenv("MALOO_USER", "u")
        monkeypatch.setenv("MALOO_PASS", "p")
        get, calls = _top_failures_server(drops)
        with patch.object(requests.Session, "get", side_effect=get):
            result = CliRunner().invoke(main, ["top-failures", "--sessions", "1"])
        return result, calls

    def test_one_dropped_connection_does_not_abort_the_run(self, monkeypatch):
        result, calls = self._run(1, monkeypatch)
        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["top_failures"][0]["suite"] == "sanity"
        assert out["top_failures"][0]["test_name"] == "test_1"

    def test_every_attempt_dropped_is_a_connection_error(self, monkeypatch):
        result, calls = self._run(1000, monkeypatch)
        assert result.exit_code == 5, result.output
        out = json.loads(result.stdout)
        assert out["code"] == "CONNECTION_ERROR"
        assert "after 4 attempts" in out["message"]
        assert calls["n"] == GET_ATTEMPTS

    def test_names_are_cached_for_the_next_run(self, monkeypatch, tmp_path):
        self._run(0, monkeypatch)
        path = tmp_path / "cache" / "maloo-tool" / "script-names.json"
        assert json.loads(path.read_text())[BASE] == {
            "test_set_scripts": {"sc1": "sanity"},
            "sub_test_scripts": {"st1": "test_1"},
        }
