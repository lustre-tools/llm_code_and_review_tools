"""Tests for maloo --user, and for the files that predate it."""

import json
import os

import pytest
from click.testing import CliRunner

from maloo_tool.cli import main
from maloo_tool.config import load_config


OLD_STYLE_ENV = """\
MALOO_URL=https://testing.whamcloud.com
MALOO_USER=pfarrell
MALOO_PASS=wc-secret
"""

TWO_SETS_ENV = OLD_STYLE_ENV + """
[bot]
MALOO_USER=ci-bot
MALOO_PASS=bot-secret
"""


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("MALOO_URL", "MALOO_USER", "MALOO_PASS"):
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path, monkeypatch, text):
    path = tmp_path / ".env"
    path.write_text(text)
    monkeypatch.setenv("MALOO_TOOL_ENV_FILE", str(path))
    return path


def test_a_file_without_sections_is_unchanged(tmp_path, monkeypatch):
    """The shape every existing host has still loads exactly as it did."""
    write_env(tmp_path, monkeypatch, OLD_STYLE_ENV)
    from llm_tool_common.config import load_env_files

    load_env_files("maloo-tool")
    config = load_config()
    assert config.username == "pfarrell"
    assert config.password == "wc-secret"
    assert config.base_url == "https://testing.whamcloud.com"


def test_user_selects_an_alias(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    result = runner.invoke(main, ["--user", "bot", "queue"], catch_exceptions=False)
    # The command itself needs the network; all this asserts is that the
    # credential the client was built with came from [bot].
    assert os.environ["MALOO_USER"] == "ci-bot"
    assert os.environ["MALOO_PASS"] == "bot-secret"
    # and that the section inherits the URL it does not name
    assert os.environ["MALOO_URL"] == "https://testing.whamcloud.com"
    del result


def test_user_selects_by_username(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    runner.invoke(main, ["--user", "ci-bot", "queue"])
    assert os.environ["MALOO_USER"] == "ci-bot"


def test_user_works_after_the_subcommand(tmp_path, monkeypatch, runner):
    """Global options are hoisted, so position does not matter."""
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    runner.invoke(main, ["queue", "--user", "bot"])
    assert os.environ["MALOO_USER"] == "ci-bot"


def test_unknown_user_is_a_json_error(tmp_path, monkeypatch, runner):
    write_env(tmp_path, monkeypatch, TWO_SETS_ENV)
    result = runner.invoke(main, ["--user", "nobody", "queue"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "nobody" in json.dumps(payload)
    # The error names what could have been typed instead.
    assert "bot" in json.dumps(payload)
