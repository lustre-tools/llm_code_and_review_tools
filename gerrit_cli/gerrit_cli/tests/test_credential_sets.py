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
