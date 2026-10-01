"""Tests for gerrit --user, resolved before the command modules load."""

import os
import subprocess
import sys

import pytest


OLD_STYLE_ENV = """\
GERRIT_URL=https://review.whamcloud.com
GERRIT_USER=pfarrell
GERRIT_PASS=wc-secret
"""

TWO_SETS_ENV = OLD_STYLE_ENV + """
[exa]
GERRIT_URL=https://review.exa.com
GERRIT_USER=pfarrell-exa
GERRIT_PASS=exa-secret
"""


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def run_probe(env_file, argv, extra_env=None):
    """Import gerrit_cli.cli under a given argv and report what it saw.

    A subprocess because the selection happens at import time, which a
    test in this process could only do once.
    """
    code = (
        "import sys, json\n"
        f"sys.argv = {argv!r}\n"
        "import gerrit_cli.cli as cli\n"
        "import os\n"
        "print(json.dumps({\n"
        "  'url': os.environ.get('GERRIT_URL'),\n"
        "  'user': os.environ.get('GERRIT_USER'),\n"
        "  'pass': os.environ.get('GERRIT_PASS'),\n"
        "  'default_url': cli.GerritCommentsClient.__module__ and "
        "__import__('gerrit_cli.client', fromlist=['x']).DEFAULT_GERRIT_URL,\n"
        "  'argv': sys.argv,\n"
        "}))\n"
    )
    env = dict(os.environ)
    for key in ("GERRIT_URL", "GERRIT_USER", "GERRIT_PASS"):
        env.pop(key, None)
    env["GERRIT_CLI_ENV_FILE"] = str(env_file)
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env,
    )
    return result


def test_a_file_without_sections_is_unchanged(tmp_path):
    """The shape every existing host has still loads exactly as it did."""
    env_file = write_env(tmp_path, OLD_STYLE_ENV)
    result = run_probe(env_file, ["gc", "comments", "123"])
    assert result.returncode == 0, result.stderr
    import json

    got = json.loads(result.stdout)
    assert got["user"] == "pfarrell"
    assert got["pass"] == "wc-secret"
    assert got["url"] == "https://review.whamcloud.com"


def test_user_is_applied_before_the_client_freezes_the_url(tmp_path):
    """client.py reads GERRIT_URL into a constant as it is imported.

    The bug this guards: selecting the set in main(), after the command
    modules are imported, leaves DEFAULT_GERRIT_URL pointing at the
    previous server, so `gc --user exa info 12345` resolves the bare
    change number against the wrong Gerrit.
    """
    env_file = write_env(tmp_path, TWO_SETS_ENV)
    result = run_probe(env_file, ["gc", "--user", "exa", "info", "12345"])
    assert result.returncode == 0, result.stderr
    import json

    got = json.loads(result.stdout)
    assert got["user"] == "pfarrell-exa"
    assert got["url"] == "https://review.exa.com"
    assert got["default_url"] == "https://review.exa.com"


def test_user_is_hoisted_from_after_the_subcommand(tmp_path):
    env_file = write_env(tmp_path, TWO_SETS_ENV)
    result = run_probe(env_file, ["gc", "info", "12345", "--user", "exa"])
    assert result.returncode == 0, result.stderr
    import json

    got = json.loads(result.stdout)
    assert got["user"] == "pfarrell-exa"
    # argparse owns --user at the top level, so it is moved back there.
    assert got["argv"][1:3] == ["--user", "exa"]


def test_user_matches_a_username_too(tmp_path):
    env_file = write_env(tmp_path, TWO_SETS_ENV)
    result = run_probe(env_file, ["gc", "--user", "pfarrell-exa", "info", "1"])
    import json

    assert json.loads(result.stdout)["url"] == "https://review.exa.com"


def test_user_beats_an_exported_value(tmp_path):
    env_file = write_env(tmp_path, TWO_SETS_ENV)
    result = run_probe(
        env_file,
        ["gc", "--user", "exa", "info", "1"],
        extra_env={"GERRIT_USER": "someone-else"},
    )
    import json

    assert json.loads(result.stdout)["user"] == "pfarrell-exa"


def test_unknown_user_exits_with_a_json_error(tmp_path):
    env_file = write_env(tmp_path, TWO_SETS_ENV)
    result = run_probe(env_file, ["gc", "--user", "nobody", "info", "1"])
    assert result.returncode != 0
    assert "nobody" in result.stdout
    assert "exa" in result.stdout


SSH_ENV = """\
GERRIT_URL=https://review.example.com
GERRIT_USER=dev
GERRIT_PASS=dev-secret
GERRIT_SSH_USER=dev-ssh

[bot]
GERRIT_USER=bot
GERRIT_PASS=bot-secret
"""


def ssh_fallback_user(env_file, argv, extra_env=None):
    """Who the abandon SSH fallback would log in as, under argv.

    subprocess.run is stubbed in the probe: git remote -v names the
    developer's account and ssh itself is never run.
    """
    code = (
        "import sys, json\n"
        f"sys.argv = {argv!r}\n"
        "from unittest.mock import MagicMock, patch\n"
        "import gerrit_cli.cli\n"
        "from gerrit_cli import client\n"
        "ssh = []\n"
        "def run(cmd, **kwargs):\n"
        "    if cmd[0] == 'ssh':\n"
        "        ssh.append(cmd[3])\n"
        "    out = b''\n"
        "    if cmd[:2] == ['git', 'remote']:\n"
        "        out = b'origin\\tssh://dev@review.example.com:29418/p (push)\\n'\n"
        "    return MagicMock(returncode=0, stdout=out, stderr=b'')\n"
        "with patch('subprocess.run', side_effect=run):\n"
        "    client.GerritCommentsClient()._abandon_via_ssh(123)\n"
        "print(json.dumps(ssh))\n"
    )
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("GERRIT_")]:
        env.pop(key)
    env["GERRIT_CLI_ENV_FILE"] = str(env_file)
    # discovery's last resort reads ~/.config/gerrit-cli/.env
    env["HOME"] = str(env_file.parent)
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    import json

    return json.loads(result.stdout)


def test_ssh_fallback_acts_as_the_selected_set(tmp_path):
    """--user bot must not fall back to the developer's SSH identity,
    whether it would come from GERRIT_SSH_USER (which the bot set
    inherits from the default one) or from the git remote."""
    env_file = write_env(tmp_path, SSH_ENV)
    assert ssh_fallback_user(
        env_file, ["gc", "--user", "bot", "abandon", "123"]
    ) == ["bot@review.example.com"]

    no_ssh_user = write_env(
        tmp_path, SSH_ENV.replace("GERRIT_SSH_USER=dev-ssh\n", ""))
    assert ssh_fallback_user(
        no_ssh_user, ["gc", "--user", "bot", "abandon", "123"]
    ) == ["bot@review.example.com"]


def test_ssh_fallback_without_user_is_unchanged(tmp_path):
    env_file = write_env(tmp_path, SSH_ENV)
    assert ssh_fallback_user(
        env_file, ["gc", "abandon", "123"]
    ) == ["dev-ssh@review.example.com"]

    no_ssh_user = write_env(
        tmp_path, SSH_ENV.replace("GERRIT_SSH_USER=dev-ssh\n", ""))
    assert ssh_fallback_user(
        no_ssh_user, ["gc", "abandon", "123"]
    ) == ["dev@review.example.com"]


def run_cli(env_file, argv, cwd):
    """The real CLI in a fresh interpreter: the env file is read at import."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GERRIT_")}
    env["GERRIT_CLI_ENV_FILE"] = str(env_file)
    env["HOME"] = str(cwd)
    code = (
        "import sys\n"
        f"sys.argv = {['gerrit', *argv]!r}\n"
        "import gerrit_cli.cli as cli\n"
        "cli.main()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, cwd=cwd,
    )


class TestMissingEnvFile:
    """GERRIT_CLI_ENV_FILE naming no file is reported by the command that
    needs the configuration, as JSON -- not a traceback at import."""

    def _config_error(self, result):
        import json

        assert "Traceback" not in result.stderr, result.stderr
        assert result.returncode == 2, result.stdout + result.stderr
        return json.loads(result.stdout)

    def test_help_still_works(self, tmp_path):
        result = run_cli(tmp_path / "missing.env", ["--help"], tmp_path)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout

    def test_a_command_reports_it(self, tmp_path):
        out = self._config_error(
            run_cli(tmp_path / "missing.env", ["info", "123"], tmp_path))
        assert out["code"] == "CONFIG_ERROR"
        assert "GERRIT_CLI_ENV_FILE" in out["message"]

    def test_with_envelope(self, tmp_path):
        out = self._config_error(run_cli(
            tmp_path / "missing.env", ["--envelope", "info", "123"], tmp_path))
        assert out["ok"] is False
        assert out["error"]["code"] == "CONFIG_ERROR"

    def test_with_user(self, tmp_path):
        out = self._config_error(run_cli(
            tmp_path / "missing.env", ["--user", "bot", "info", "123"],
            tmp_path))
        assert out["code"] == "CONFIG_ERROR"
        assert "GERRIT_CLI_ENV_FILE" in out["message"]

    def test_a_command_that_needs_no_configuration_runs(self, tmp_path):
        result = run_cli(tmp_path / "missing.env", ["status"], tmp_path)
        assert "Traceback" not in result.stderr, result.stderr
        assert "No active rebase session" in result.stdout
