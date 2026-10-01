"""Tests for llm_tool_common.click_group."""

import json

import click
import pytest
from click.testing import CliRunner

from llm_tool_common.click_group import JsonUsageErrorGroup


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
