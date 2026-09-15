"""Tests for jira --user, which reaches both credential stores."""

import json
import os

import pytest
from click.testing import CliRunner

from jira_tool.cli import main


OLD_STYLE_ENV = """\
JIRA_SERVER=https://jira.whamcloud.com
JIRA_TOKEN=wc-token
"""

TWO_SETS_ENV = OLD_STYLE_ENV + """
[acme]
JIRA_SERVER=https://acme.atlassian.net
JIRA_USER=me@acme.com
JIRA_TOKEN=acme-token
"""


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("JIRA_SERVER", "JIRA_TOKEN", "JIRA_USER", "JIRA_TOOL_CONFIG"):
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path, monkeypatch, text):
    path = tmp_path / ".env"
    path.write_text(text)
    monkeypatch.setenv("JIRA_TOOL_ENV_FILE", str(path))
    return path


def test_a_file_without_sections_is_unchanged(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, OLD_STYLE_ENV)
    from llm_tool_common.config import load_env_files

    load_env_files("jira-tool")
    assert os.environ["JIRA_SERVER"] == "https://jira.whamcloud.com"
    assert os.environ["JIRA_TOKEN"] == "wc-token"


def test_user_selects_an_env_section(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    runner.invoke(main, ["--user", "acme", "get", "LU-1"])
    assert os.environ["JIRA_SERVER"] == "https://acme.atlassian.net"
    assert os.environ["JIRA_TOKEN"] == "acme-token"


def test_user_matches_the_jira_user_label(tmp_path, monkeypatch, runner):
    """Jira Server has no username, so a set may carry one as a label."""
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    runner.invoke(main, ["--user", "me@acme.com", "get", "LU-1"])
    assert os.environ["JIRA_TOKEN"] == "acme-token"


def test_user_falls_through_to_a_json_instance(tmp_path, monkeypatch, runner):
    """--user reaches the instances map too, so one flag covers both."""
    write_env(tmp_path, monkeypatch, OLD_STYLE_ENV)
    config = tmp_path / "jira-tool.json"
    config.write_text(
        json.dumps(
            {
                "instances": {
                    "cloud": {
                        "server": "https://acme.atlassian.net",
                        "auth": {
                            "type": "basic",
                            "email": "me@acme.com",
                            "token": "t",
                        },
                    }
                },
                "default": "cloud",
            }
        )
    )
    monkeypatch.setenv("JIRA_TOOL_CONFIG", str(config))

    import jira_tool.cli as cli

    seen = {}
    original = cli._instance_named

    def spy(user, config_path):
        seen["result"] = original(user, config_path)
        return seen["result"]

    monkeypatch.setattr(cli, "_instance_named", spy)
    runner.invoke(main, ["--user", "cloud", "get", "EX-1"])
    assert seen["result"] == "cloud"


def test_watch_keeps_its_own_user(tmp_path, monkeypatch, runner):
    """`jira watch LU-1 --user alice` still adds alice as a watcher.

    --user after the subcommand is jira's oldest meaning of the flag, so
    it is deliberately not hoisted to the group; -U is what selects a
    credential set from any position.
    """
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    from jira_tool.cli import _HOISTABLE_OPTIONS

    assert "--user" not in _HOISTABLE_OPTIONS
    assert "-U" in _HOISTABLE_OPTIONS
    # and the credential set is untouched by a watcher argument
    runner.invoke(main, ["watch", "LU-1", "--user", "acme"])
    assert os.environ.get("JIRA_TOKEN") != "acme-token"


def test_short_flag_hoists_from_anywhere(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    runner.invoke(main, ["get", "LU-1", "-U", "acme"])
    assert os.environ["JIRA_TOKEN"] == "acme-token"


def test_unknown_user_is_a_json_error(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    result = runner.invoke(main, ["--user", "nobody", "get", "LU-1"])
    assert result.exit_code == 1
    assert "nobody" in result.output
    assert "acme" in result.output
