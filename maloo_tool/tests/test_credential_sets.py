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


def _run_cli(env_file, *args):
    """Run maloo in a fresh interpreter: the env file is read at import."""
    import subprocess
    import sys

    env = {
        key: value for key, value in os.environ.items()
        if key not in ("MALOO_URL", "MALOO_USER", "MALOO_PASS")
    }
    env["MALOO_TOOL_ENV_FILE"] = str(env_file)
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    return subprocess.run(
        [sys.executable, "-c", "from maloo_tool.cli import main; main()", *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


class TestMissingEnvFile:
    """MALOO_TOOL_ENV_FILE naming no file is a JSON CONFIG_ERROR from the
    command, not a traceback before any command runs."""

    def _config_error(self, proc):
        assert "Traceback" not in proc.stderr, proc.stderr
        assert proc.returncode == 1, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    def test_a_command(self, tmp_path):
        missing = tmp_path / "missing.env"
        out = self._config_error(
            _run_cli(missing, "session", "11111111-1111-1111-1111-111111111111")
        )
        assert out["code"] == "CONFIG_ERROR"
        assert str(missing) in out["message"]

    def test_envelope_and_user(self, tmp_path):
        env = self._config_error(
            _run_cli(tmp_path / "missing.env", "--envelope", "--user", "bot", "queue")
        )
        assert env["ok"] is False
        assert env["error"]["code"] == "CONFIG_ERROR"

    def test_help_still_works(self, tmp_path):
        proc = _run_cli(tmp_path / "missing.env", "--help")
        assert proc.returncode == 0
        assert "Usage:" in proc.stdout
