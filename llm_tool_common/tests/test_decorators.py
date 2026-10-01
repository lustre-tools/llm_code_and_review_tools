"""Tests for llm_tool_common.decorators."""

import json
import pytest
from unittest.mock import MagicMock

import click
import requests
from click.testing import CliRunner

from llm_tool_common.decorators import handle_errors


@pytest.fixture
def runner():
    return CliRunner()


def _make_cli(func):
    """Wrap a function in a click group for testing."""
    @click.group()
    @click.option("--envelope", is_flag=True, default=False)
    def cli(envelope):
        pass

    cli.add_command(func)
    return cli


def _parse(result):
    return json.loads(result.output)


class TestHandleHTTPErrors:
    def test_404_becomes_not_found(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("test-tool", "test-cmd")
        def cmd(pretty):
            resp = MagicMock()
            resp.status_code = 404
            raise requests.HTTPError(response=resp)

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        env = _parse(result)
        assert env["ok"] is False
        assert env["error"]["code"] == "NOT_FOUND"
        assert env["meta"]["tool"] == "test-tool"
        assert env["meta"]["command"] == "test-cmd"

    def test_custom_not_found_message(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c", not_found_msg="Job 'foo' not found")
        def cmd(pretty):
            resp = MagicMock()
            resp.status_code = 404
            raise requests.HTTPError(response=resp)

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        env = _parse(result)
        assert env["error"]["message"] == "Job 'foo' not found"

    def test_401_becomes_auth_failed(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            resp = MagicMock()
            resp.status_code = 401
            raise requests.HTTPError(response=resp)

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        env = _parse(result)
        assert env["error"]["code"] == "AUTH_FAILED"

    def test_500_becomes_api_error(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            resp = MagicMock()
            resp.status_code = 500
            raise requests.HTTPError("server error", response=resp)

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        env = _parse(result)
        assert env["error"]["code"] == "API_ERROR"
        assert "500" in env["error"]["message"]


class TestHandleConnectionErrors:
    def test_connection_error(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            raise requests.ConnectionError("refused")

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        env = _parse(result)
        assert env["error"]["code"] == "CONNECTION_ERROR"

    def test_timeout_error(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            raise requests.Timeout("timed out")

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        env = _parse(result)
        assert env["error"]["code"] == "TIMEOUT"


class TestHandleGenericExceptions:
    def test_generic_exception(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            raise RuntimeError("something broke")

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        env = _parse(result)
        assert env["error"]["code"] == "API_ERROR"
        assert "something broke" in env["error"]["message"]


class TestSuccessPassthrough:
    def test_no_exception_passes_through(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            click.echo('{"ok": true}')

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        assert result.exit_code == 0
        assert '"ok": true' in result.output


class TestPrettyFlag:
    def test_pretty_formatting(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            raise RuntimeError("oops")

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd", "--pretty"])
        # Pretty output has newlines and indentation
        assert "\n" in result.output
        env = _parse(result)
        assert env["ok"] is False


class TestNoEnvelopeDefault:
    """Default output should strip the envelope wrapper."""

    def test_error_without_envelope(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            resp = MagicMock()
            resp.status_code = 404
            raise requests.HTTPError(response=resp)

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["cmd"])
        out = _parse(result)
        # Without --envelope, output is just the error dict
        assert "ok" not in out
        assert "meta" not in out
        assert out["code"] == "NOT_FOUND"

    def test_error_with_envelope(self, runner):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            resp = MagicMock()
            resp.status_code = 404
            raise requests.HTTPError(response=resp)

        cli = _make_cli(cmd)
        result = runner.invoke(cli, ["--envelope", "cmd"])
        out = _parse(result)
        # With --envelope, output has full wrapper
        assert out["ok"] is False
        assert "meta" in out
        assert out["error"]["code"] == "NOT_FOUND"


def _http_error(status):
    resp = MagicMock()
    resp.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=resp)


class TestExitCodes:
    """Each failure exits with the contract's code for it, not always 1."""

    @pytest.mark.parametrize("exc, code, exit_code", [
        (_http_error(404), "NOT_FOUND", 3),
        (_http_error(401), "AUTH_FAILED", 2),
        (_http_error(403), "AUTH_FAILED", 2),
        (_http_error(500), "API_ERROR", 1),
        (requests.ConnectionError("refused"), "CONNECTION_ERROR", 5),
        (requests.Timeout("slow"), "TIMEOUT", 5),
        (RuntimeError("broke"), "API_ERROR", 1),
    ])
    def test_exit_code(self, runner, exc, code, exit_code):
        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            raise exc

        result = runner.invoke(_make_cli(cmd), ["cmd"])
        assert _parse(result)["code"] == code
        assert result.exit_code == exit_code

    def test_a_tool_error_keeps_its_code_message_and_exit(self, runner):
        from llm_tool_common.errors import ExitCode, ToolError

        @click.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("t", "c")
        def cmd(pretty):
            raise ToolError("AUTH_MISSING", "set the token",
                            exit_code=ExitCode.AUTH_ERROR)

        result = runner.invoke(_make_cli(cmd), ["cmd"])
        out = _parse(result)
        assert out["code"] == "AUTH_MISSING"
        assert out["message"] == "set the token"
        assert result.exit_code == 2
