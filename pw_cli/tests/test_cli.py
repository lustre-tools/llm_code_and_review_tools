"""pw-cli against a fake Patch Watcher: no network."""

import json
from unittest.mock import patch

import pytest
import requests
from click.testing import CliRunner

from pw_cli import cli


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GERRIT_USER", "alice")
    monkeypatch.setenv("GERRIT_PASS", "secret")
    monkeypatch.setenv("GERRIT_CLI_ENV_FILE", str(tmp_path / "gerrit.env"))
    (tmp_path / "gerrit.env").write_text("")
    monkeypatch.delenv(cli.URL_ENV, raising=False)


def run(*args, answers=None):
    """(exit code, decoded output, the requests made)."""
    sent = []
    answers = list(answers or [])

    def fake(method, url, **kwargs):
        sent.append((method, url, kwargs.get("json"), kwargs.get("auth")))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(*answer)

    with patch.object(cli.requests, "request", side_effect=fake):
        result = CliRunner().invoke(cli.main, list(args))
    return result.exit_code, json.loads(result.output), sent


@pytest.mark.parametrize("value", [
    "69157", " 69157 ",
    "https://review.whamcloud.com/c/fs/lustre-release/+/69157",
    "https://review.whamcloud.com/c/fs/lustre-release/+/69157/3",
    "https://review.whamcloud.com/c/fs/lustre-release/+/69157?usp=email",
    "https://review.whamcloud.com/c/69157",
    "https://review.whamcloud.com/69157",
    "https://review.whamcloud.com/#/c/69157/",
])
def test_change_forms(value):
    assert cli.parse_change(value) == 69157


@pytest.mark.parametrize("value", ["", "0", "LU-123", "abc", "https://example.org/x"])
def test_not_a_change(value):
    with pytest.raises(cli.ToolError) as exc:
        cli.parse_change(value)
    assert exc.value.code == "INVALID_INPUT"


def test_retest_sends_the_gerrit_login_and_the_level():
    code, out, sent = run("retest", "69157", answers=[(200, {"change": 69157, "level": "retest"})])
    assert code == 0
    assert out == [{"change": 69157, "level": "retest"}]
    assert sent == [(
        "POST", "https://patchwatcher.mulberrytree.us/tagger/v1/changes/69157/watch",
        {"level": "retest"}, ("alice", "secret"),
    )]


def test_halt_joins_the_reason_and_clear_halt_takes_a_note():
    _, _, sent = run("halt", "69157", "owner", "away", answers=[(200, {"halted": True})])
    assert sent[0][1].endswith("/changes/69157/halt") and sent[0][2] == {"reason": "owner away"}
    _, _, sent = run("clear-halt", "69157", "--note", "back", answers=[(200, {})])
    assert sent[0][1].endswith("/changes/69157/clear-halt") and sent[0][2] == {"note": "back"}


def test_a_refusal_keeps_the_servers_reason():
    refused = {"error": {"code": "not_authorized", "message": "Not authorized: mine only."}}
    code, out, _ = run("stop", "69157", answers=[(403, refused)])
    assert code == 1
    assert out["code"] == "NOT_AUTHORIZED"
    assert out["message"] == "Not authorized: mine only."
    assert out["details"]["server_code"] == "not_authorized"


def test_a_bad_login_is_an_auth_error():
    code, out, _ = run("whoami", answers=[(401, {"error": {"code": "bad_login", "message": "no"}})])
    assert (code, out["code"]) == (2, "AUTH_FAILED")


def test_a_redirect_to_a_login_page_is_named():
    code, out, _ = run("whoami", answers=[(302, None)])
    assert code == 1 and "not open to Gerrit logins" in out["message"]


def test_several_changes_carry_on_past_one_refusal():
    answers = [
        (200, {"change": 1, "level": "shepherd"}),
        (409, {"error": {"code": "out_of_reach", "message": "watched at bigfix"}}),
        (200, {"change": 3, "level": "shepherd"}),
    ]
    code, out, sent = run("shepherd", "1", "2", "3", answers=answers)
    assert code == 1 and len(sent) == 3
    assert out[0]["level"] == "shepherd" and out[2]["level"] == "shepherd"
    assert out[1]["change"] == 2 and out[1]["error"]["code"] == "CONFLICT"


def test_a_login_failure_stops_a_list():
    answers = [(401, {"error": {"code": "bad_login", "message": "no"}})]
    code, out, sent = run("retest", "1", "2", answers=answers)
    assert (code, out["code"], len(sent)) == (2, "AUTH_FAILED", 1)


def test_no_login_is_named_before_anything_is_sent(monkeypatch):
    monkeypatch.delenv("GERRIT_PASS")
    code, out, sent = run("whoami")
    assert (code, out["code"], sent) == (2, "AUTH_MISSING", [])


def test_the_password_goes_only_over_https(monkeypatch):
    monkeypatch.setenv(cli.URL_ENV, "http://192.168.50.112:8080")
    code, out, sent = run("whoami")
    assert (code, out["code"], sent) == (1, "CONFIG_ERROR", [])
    monkeypatch.setenv(cli.URL_ENV, "http://127.0.0.1:8080/")
    _, _, sent = run("whoami", answers=[(200, {})])
    assert sent[0][1] == "http://127.0.0.1:8080/tagger/v1/whoami"


def test_network_trouble_is_a_network_error():
    code, out, _ = run("whoami", answers=[requests.ConnectionError("down")])
    assert (code, out["code"]) == (5, "CONNECTION_ERROR")


def test_envelope_anywhere():
    code, out, _ = run("whoami", "--envelope", answers=[(200, {"username": "alice"})])
    assert code == 0 and out["ok"] is True and out["data"]["username"] == "alice"


def test_watch_takes_any_level():
    _, _, sent = run("watch", "1", "2", "--level", "bigfix", answers=[(200, {}), (200, {})])
    assert [(url.rsplit("/", 2)[-2], body) for _, url, body, _ in sent] == [
        ("1", {"level": "bigfix"}), ("2", {"level": "bigfix"}),
    ]


def test_runs_go_to_their_routes():
    _, _, sent = run("reviewers", "1", answers=[(200, {"run_id": "r"})])
    assert sent[0][1].endswith("/changes/1/reviewers") and sent[0][2] == {}
    _, _, sent = run("lreview", "1", "--light", answers=[(200, {})])
    assert sent[0][1].endswith("/changes/1/lreview") and sent[0][2] == {"mode": "light"}
    _, _, sent = run("rebase", "1", answers=[(200, {"run_id": "r"})])
    assert sent[0][1].endswith("/changes/1/rebase") and sent[0][2] == {}
    _, _, sent = run("request", "1", "run", "sanity", "56", answers=[(200, {})])
    assert sent[0][1].endswith("/changes/1/request") and sent[0][2] == {"text": "run sanity 56"}
    _, _, sent = run("feedback", "1", "too", "chatty", answers=[(200, {})])
    assert sent[0][1].endswith("/changes/1/feedback") and sent[0][2] == {"text": "too chatty"}


def test_a_request_may_come_from_stdin():
    sent = []

    def fake(method, url, **kwargs):
        sent.append(kwargs.get("json"))
        return FakeResponse(200, {})

    with patch.object(cli.requests, "request", side_effect=fake):
        result = CliRunner().invoke(cli.main, ["request", "1", "-"], input="line one\n\nline two\n")
    assert result.exit_code == 0
    assert sent == [{"text": "line one\n\nline two\n"}]
