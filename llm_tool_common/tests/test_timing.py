"""Tests for the opt-in local timing log (timing.py)."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter

from llm_tool_common import timing


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(timing.ENV, raising=False)
    return tmp_path


@pytest.fixture
def live(home, monkeypatch):
    """Timing active as in a real run, with HTTP answered locally."""
    monkeypatch.setattr(timing, "_under_test", lambda: False)
    monkeypatch.setattr(timing, "_installed", False)
    monkeypatch.setattr(subprocess, "run", subprocess.run)

    def fake_send(self, request, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"x" * 10
        resp.request = request
        resp.url = request.url
        return resp

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    path = home / "runs.jsonl"
    monkeypatch.setenv(timing.ENV, str(path))
    return path


def test_off_by_default(home):
    assert timing.target() is None


@pytest.mark.parametrize("value,expected", [
    ("0", None), ("off", None), ("1", "default"), ("on", "default"),
])
def test_env_switch(home, monkeypatch, value, expected):
    monkeypatch.setenv(timing.ENV, value)
    got = timing.target()
    assert (got is None) if expected is None else got == timing._default_file()


def test_env_path_and_config(home, monkeypatch):
    monkeypatch.setenv(timing.ENV, str(home / "x.jsonl"))
    assert timing.target() == home / "x.jsonl"
    monkeypatch.delenv(timing.ENV)
    timing._set(True, str(home / "y.jsonl"))
    assert timing.target() == (home / "y.jsonl").resolve()
    timing._set(False, None)
    assert timing.target() is None


def test_secrets_are_not_kept():
    argv = ["get", "X-1", "--token", "s3cret", "--password=hunter2",
            "a" * 500]
    out = timing._argv(argv)
    assert "s3cret" not in out and "--password=<redacted>" in out
    assert out[:2] == ["get", "X-1"]
    assert len(out[-1]) < 200


def test_concurrent_waits_count_once():
    assert timing._busy_ms([(0, 1), (0.5, 1.5), (3, 4)]) == pytest.approx(2.5)


def test_a_run_is_recorded(live):
    run = SimpleNamespace(command=None)
    with pytest.raises(SystemExit), timing.recording("gerrit", run):
        run.command = "comments"
        requests.Session().get("https://review.example.com/changes/1?o=X")
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        sys.exit(0)
    (line,) = live.read_text().splitlines()
    rec = json.loads(line)
    assert rec["tool"] == "gerrit" and rec["command"] == "comments"
    assert rec["exit"] == 0
    assert rec["http_n"] == 1 and rec["http_bytes"] == 10
    http, proc = rec["calls"]
    assert (http["host"], http["path"], http["q"]) == (
        "review.example.com", "/changes/1", "o=X")
    assert proc["kind"] == "proc" and proc["rc"] == 0
    assert rec["wall_ms"] >= rec["wait_ms"]


def test_nothing_recorded_when_off(home, monkeypatch):
    monkeypatch.setattr(timing, "_under_test", lambda: False)
    with timing.recording("jira", SimpleNamespace(command="get")):
        pass
    assert not (home / "state").exists()


def test_summary_and_endpoints(live):
    for _ in range(2):
        with timing.recording("gerrit", SimpleNamespace(command="info")):
            requests.Session().get("https://r.example.com/a/changes/70792/reviewers")
    records = timing._load(live, None, None)
    (row,) = timing.summary(records)
    assert (row["tool"], row["command"], row["runs"]) == ("gerrit", "info", 2)
    (ep,) = timing.endpoints(records)
    assert ep["endpoint"] == "GET r.example.com/a/changes/{id}/reviewers"
    assert ep["n"] == 2
