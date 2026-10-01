"""Exit codes follow the shared contract: 0 ok, 1 general, 2 auth,
3 not found, 4 invalid input, 5 network."""

import argparse
import json
from unittest.mock import patch

import pytest
import requests

from gerrit_cli.client import GerritAuthRequired, GerritConfigError
from gerrit_cli.cli import error_code_for, output_error
from gerrit_cli.errors import ErrorCode, ExitCode


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=response)


class TestOutputErrorExitCode:

    @pytest.mark.parametrize("code, expected", [
        (ErrorCode.API_ERROR, ExitCode.GENERAL_ERROR),
        (ErrorCode.GIT_ERROR, ExitCode.GENERAL_ERROR),
        (ErrorCode.AUTH_MISSING, ExitCode.AUTH_ERROR),
        (ErrorCode.AUTH_FAILED, ExitCode.AUTH_ERROR),
        (ErrorCode.NOT_FOUND, ExitCode.NOT_FOUND),
        (ErrorCode.CHANGE_NOT_FOUND, ExitCode.NOT_FOUND),
        (ErrorCode.INVALID_INPUT, ExitCode.INVALID_INPUT),
        (ErrorCode.MISSING_REQUIRED_FIELD, ExitCode.INVALID_INPUT),
        (ErrorCode.THREAD_INDEX_OUT_OF_RANGE, ExitCode.INVALID_INPUT),
        (ErrorCode.CONNECTION_ERROR, ExitCode.NETWORK_ERROR),
        (ErrorCode.TIMEOUT, ExitCode.NETWORK_ERROR),
    ])
    def test_exit_code_follows_error_code(self, code, expected, capsys):
        assert output_error(code, "msg", "test", False) == expected


class TestErrorCodeFor:

    @pytest.mark.parametrize("exc, expected", [
        (GerritAuthRequired("no credentials"), ErrorCode.AUTH_MISSING),
        (GerritConfigError("no GERRIT_URL"), ErrorCode.AUTH_MISSING),
        (_http_error(401), ErrorCode.AUTH_FAILED),
        (_http_error(403), ErrorCode.AUTH_FAILED),
        (_http_error(404), ErrorCode.NOT_FOUND),
        (_http_error(409), ErrorCode.API_ERROR),
        (_http_error(500), ErrorCode.API_ERROR),
        (requests.ConnectionError("refused"), ErrorCode.CONNECTION_ERROR),
        (requests.ConnectTimeout("slow"), ErrorCode.TIMEOUT),
        (requests.ReadTimeout("slow"), ErrorCode.TIMEOUT),
        (RuntimeError("boom"), ErrorCode.API_ERROR),
    ])
    def test_classifies(self, exc, expected):
        assert error_code_for(exc) == expected


class TestHandlersExitByKind:
    """A handler's catch-all reports what kind of failure it caught."""

    @staticmethod
    def _vote(capsys, failure):
        from gerrit_cli.cli import cmd_vote
        args = argparse.Namespace(url="12345", label="Code-Review",
                                  score=1, message=None, pretty=False)
        with patch('gerrit_cli.cli.GerritCommentsClient') as MockClient, \
             pytest.raises(SystemExit) as exc_info:
            MockClient.parse_gerrit_url.return_value = (
                "https://example.com", 12345)
            MockClient.return_value.post_review.side_effect = failure
            cmd_vote(args)
        return exc_info.value.code, json.loads(capsys.readouterr().out)

    def test_write_without_credentials_is_auth(self, capsys):
        code, out = self._vote(capsys, GerritAuthRequired("need creds"))
        assert code == ExitCode.AUTH_ERROR
        assert out["code"] == ErrorCode.AUTH_MISSING

    def test_rejected_credentials_are_auth(self, capsys):
        code, out = self._vote(capsys, _http_error(401))
        assert code == ExitCode.AUTH_ERROR
        assert out["code"] == ErrorCode.AUTH_FAILED

    def test_missing_change_is_not_found(self, capsys):
        code, out = self._vote(capsys, _http_error(404))
        assert code == ExitCode.NOT_FOUND
        assert out["code"] == ErrorCode.NOT_FOUND

    def test_unreachable_server_is_network(self, capsys):
        code, out = self._vote(capsys, requests.ConnectionError("refused"))
        assert code == ExitCode.NETWORK_ERROR
        assert out["code"] == ErrorCode.CONNECTION_ERROR

    def test_bad_change_is_invalid_input(self, capsys):
        from gerrit_cli.cli import cmd_info
        args = argparse.Namespace(url="nonsense", pretty=False)
        with patch('gerrit_cli.cli.GerritCommentsClient') as MockClient, \
             pytest.raises(SystemExit) as exc_info:
            MockClient.parse_gerrit_url.side_effect = ValueError("bad")
            cmd_info(args)
        assert exc_info.value.code == ExitCode.INVALID_INPUT

    def test_other_failures_stay_general(self, capsys):
        code, out = self._vote(capsys, _http_error(409))
        assert code == ExitCode.GENERAL_ERROR
        assert out["code"] == ErrorCode.API_ERROR

    def test_text_handler_exits_by_kind(self, capsys):
        from gerrit_cli.cli import cmd_work_on_patch
        args = argparse.Namespace(target="https://example.com/12345")
        with patch('gerrit_cli.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_cli.cli.work_on_patch',
                   side_effect=GerritConfigError("no GERRIT_URL")), \
             pytest.raises(SystemExit) as exc_info:
            MockClient.parse_gerrit_url.return_value = (
                "https://example.com", 12345)
            cmd_work_on_patch(args)
        assert exc_info.value.code == ExitCode.AUTH_ERROR
