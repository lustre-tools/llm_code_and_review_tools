"""Tests for llm_tool_common.click_group."""

import json
import sys

import click
import pytest
import requests
from click.testing import CliRunner

from llm_tool_common.click_group import JsonErrorGroup, JsonUsageErrorGroup
from llm_tool_common.errors import ExitCode, ToolError


class _Group(JsonUsageErrorGroup):
    tool_name = "test-tool"


@click.group(cls=_Group)
@click.option("--envelope", is_flag=True)
def cli(envelope):
    pass


@cli.command()
@click.argument("name")
@click.option("--count", type=int, default=1)
def greet(name, count):
    click.echo(json.dumps({"name": name, "count": count}))


@cli.command()
def refuse():
    raise click.BadParameter("not like that")


@pytest.fixture
def runner():
    return CliRunner()


def _invalid(result):
    assert result.exit_code == 4, result.output
    out = json.loads(result.stdout)
    assert out["code"] == "INVALID_INPUT"
    return out


def test_unknown_command(runner):
    out = _invalid(runner.invoke(cli, ["nosuch"]))
    assert out["message"] == "No such command 'nosuch'."


def test_missing_argument_names_it_and_gives_usage(runner):
    out = _invalid(runner.invoke(cli, ["greet"]))
    assert out["message"] == "Missing argument 'NAME'."
    assert out["details"]["usage"] == "Usage: cli greet [OPTIONS] NAME"
    assert out["details"]["help"] == "cli greet --help"


def test_bad_option_value(runner):
    out = _invalid(runner.invoke(cli, ["greet", "bob", "--count", "x"]))
    assert "--count" in out["message"]


def test_unknown_group_option(runner):
    out = _invalid(runner.invoke(cli, ["--bogus", "greet", "bob"]))
    assert "--bogus" in out["message"]


def test_usage_error_raised_by_a_command(runner):
    out = _invalid(runner.invoke(cli, ["refuse"]))
    assert "not like that" in out["message"]


def test_no_command(runner):
    result = runner.invoke(cli, ["--envelope"])
    assert result.exit_code == 4
    env = json.loads(result.stdout)
    assert env["error"]["code"] == "INVALID_INPUT"
    assert env["error"]["message"] == "Missing command."


def test_bare_invocation_still_shows_help_on_stderr(runner):
    result = runner.invoke(cli, [])
    out = _invalid(result)
    assert out["message"] == "Missing command."
    assert "Commands:" in result.stderr


def test_envelope_in_subcommand_and_group_errors(runner):
    for args, command in (
        (["--envelope", "greet"], "greet"),
        (["--envelope", "--bogus", "greet"], "cli"),
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 4
        env = json.loads(result.stdout)
        assert env["ok"] is False
        assert env["error"]["code"] == "INVALID_INPUT"
        assert env["meta"]["tool"] == "test-tool"
        assert env["meta"]["command"] == command


def test_valid_invocation_and_help_unchanged(runner):
    result = runner.invoke(cli, ["greet", "bob", "--count", "2"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"name": "bob", "count": 2}

    result = runner.invoke(cli, ["greet", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout


class _ErrorGroup(JsonErrorGroup):
    tool_name = "test-tool"


@click.group(cls=_ErrorGroup)
@click.option("--envelope", is_flag=True)
def err_cli(envelope):
    pass


@err_cli.command()
@click.argument("what")
@click.pass_context
def fail(ctx, what):
    if what == "network":
        raise requests.ConnectionError("refused")
    if what == "tool":
        raise ToolError("AUTH_MISSING", "set it", exit_code=ExitCode.AUTH_ERROR)
    if what == "bug":
        raise KeyError("name")
    if what == "exit0":
        ctx.exit(0)
    if what == "abort":
        raise click.Abort()
    if what == "sysexit":
        sys.exit(3)
    click.echo("fine")


class TestJsonErrorGroup:
    def _error(self, result, code, exit_code):
        assert result.exit_code == exit_code, result.output
        out = json.loads(result.stdout)
        assert out["code"] == code
        return out

    def test_an_escaped_exception_is_mapped(self, runner):
        out = self._error(
            runner.invoke(err_cli, ["fail", "network"]), "CONNECTION_ERROR", 5
        )
        assert "refused" in out["message"]

    def test_a_tool_error_keeps_its_code_and_exit(self, runner):
        out = self._error(
            runner.invoke(err_cli, ["fail", "tool"]), "AUTH_MISSING", 2
        )
        assert out["message"] == "set it"

    def test_anything_else_is_an_api_error(self, runner):
        self._error(runner.invoke(err_cli, ["fail", "bug"]), "API_ERROR", 1)

    def test_envelope(self, runner):
        result = runner.invoke(err_cli, ["--envelope", "fail", "network"])
        env = json.loads(result.stdout)
        assert env["ok"] is False
        assert env["meta"]["tool"] == "test-tool"
        assert env["meta"]["command"] == "fail"

    def test_usage_errors_still_exit_4(self, runner):
        self._error(runner.invoke(err_cli, ["fail"]), "INVALID_INPUT", 4)

    def test_click_exits_and_sys_exit_pass_through(self, runner):
        assert runner.invoke(err_cli, ["fail", "exit0"]).exit_code == 0
        result = runner.invoke(err_cli, ["fail", "abort"])
        assert result.exit_code == 1
        assert "Aborted!" in result.stderr
        assert runner.invoke(err_cli, ["fail", "sysexit"]).exit_code == 3
        result = runner.invoke(err_cli, ["fail", "--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.stdout
        assert runner.invoke(err_cli, ["fail", "ok"]).stdout == "fine\n"
