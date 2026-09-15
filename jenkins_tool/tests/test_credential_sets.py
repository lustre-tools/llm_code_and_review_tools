"""Tests for jenkins --user, which now names a credential set."""

import json
import os

import pytest
from click.testing import CliRunner

from jenkins_tool.cli import main
from jenkins_tool.config import load_config


OLD_STYLE_ENV = """\
JENKINS_URL=https://build.whamcloud.com
JENKINS_USER=pfarrell
JENKINS_TOKEN=wc-token
"""

TWO_SETS_ENV = OLD_STYLE_ENV + """
[bot]
JENKINS_USER=ci-bot
JENKINS_TOKEN=bot-token
"""


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path, monkeypatch, text):
    path = tmp_path / ".env"
    path.write_text(text)
    monkeypatch.setenv("JENKINS_TOOL_ENV_FILE", str(path))
    return path


def test_a_file_without_sections_is_unchanged(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, OLD_STYLE_ENV)
    from llm_tool_common.config import load_env_files

    load_env_files("jenkins-tool")
    config = load_config()
    assert config.user == "pfarrell"
    assert config.token == "wc-token"
    assert config.authenticated


def test_user_selects_a_set(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    runner.invoke(main, ["--user", "bot", "jobs"])
    assert os.environ["JENKINS_USER"] == "ci-bot"
    assert os.environ["JENKINS_TOKEN"] == "bot-token"


def test_user_no_longer_means_a_bare_username(tmp_path, monkeypatch, runner):
    """The old --user passed a username straight through, with no token.

    That was half a credential, which Jenkins rejects outright.  It now
    names a set, so both halves arrive together -- and a name that is no
    set at all is an error rather than a request that quietly fails at
    the server.
    """
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    result = runner.invoke(main, ["--user", "someone-unknown", "jobs"])
    assert result.exit_code == 1
    assert "someone-unknown" in result.output


def test_explicit_token_still_overrides_the_set(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    captured = {}

    import jenkins_tool.cli as cli

    def fake_client(url=None, token=None):
        config = load_config(url_override=url, token_override=token)
        captured["user"] = config.user
        captured["token"] = config.token
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_make_client", fake_client)
    runner.invoke(main, ["--user", "bot", "jobs", "--token", "typed-by-hand"])
    assert captured["user"] == "ci-bot"
    assert captured["token"] == "typed-by-hand"


def test_user_works_after_the_subcommand(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    runner.invoke(main, ["jobs", "--user", "bot"])
    assert os.environ["JENKINS_USER"] == "ci-bot"


def test_unknown_user_lists_the_sets(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    result = runner.invoke(main, ["--user", "nobody", "jobs"])
    assert result.exit_code == 1
    assert "bot" in json.dumps(json.loads(result.output))
