"""Tests for multiple credential sets in one .env file."""

import os

import pytest

from llm_tool_common.config import (
    CredentialSetError,
    apply_credential_set,
    credential_sets,
    load_env_files,
    parse_env_file,
    resolve_credential_set,
)


GERRIT_ENV = """\
# the set everything uses when nothing asks otherwise
GERRIT_URL=https://review.whamcloud.com
GERRIT_USER=pfarrell
GERRIT_PASS=wc-secret

[exa]
GERRIT_URL=https://review.exa.com
GERRIT_USER=pfarrell-exa
GERRIT_PASS=exa-secret

[bot]
GERRIT_USER=lustre-bot
GERRIT_PASS=bot-secret
"""


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point gerrit-cli at a throwaway .env with three sets."""
    path = tmp_path / ".env"
    path.write_text(GERRIT_ENV)
    monkeypatch.setenv("GERRIT_CLI_ENV_FILE", str(path))
    for key in ("GERRIT_URL", "GERRIT_USER", "GERRIT_PASS"):
        monkeypatch.delenv(key, raising=False)
    return path


def test_parse_splits_default_from_named(env_file):
    default, sets = parse_env_file(env_file)
    assert default["GERRIT_USER"] == "pfarrell"
    assert sorted(sets) == ["bot", "exa"]
    assert sets["exa"]["GERRIT_URL"] == "https://review.exa.com"
    # The default set stops at the first header; a named set's keys are
    # never mixed into it.
    assert default["GERRIT_PASS"] == "wc-secret"


def test_named_set_inherits_the_default(env_file):
    sets = credential_sets("gerrit-cli")
    # [bot] names no URL, so it is the default server with another account.
    assert sets["bot"]["GERRIT_URL"] == "https://review.whamcloud.com"
    assert sets["bot"]["GERRIT_USER"] == "lustre-bot"
    # [exa] overrides the URL it does name.
    assert sets["exa"]["GERRIT_URL"] == "https://review.exa.com"


def test_import_time_load_ignores_named_sets(env_file, monkeypatch):
    """A tool that never asks for --user sees only the default set.

    The bug this guards: a plain KEY=VALUE parser reads every line in
    the file, so a key that appears only under [bot] would leak into the
    default identity of every command.
    """
    monkeypatch.delenv("GERRIT_USER", raising=False)
    load_env_files("gerrit-cli")
    assert os.environ["GERRIT_USER"] == "pfarrell"
    assert os.environ["GERRIT_PASS"] == "wc-secret"


def test_select_by_alias(env_file):
    name, values = resolve_credential_set("gerrit-cli", "exa")
    assert name == "exa"
    assert values["GERRIT_PASS"] == "exa-secret"


def test_select_by_username(env_file):
    name, values = resolve_credential_set("gerrit-cli", "lustre-bot")
    assert name == "bot"
    assert values["GERRIT_PASS"] == "bot-secret"


def test_select_is_case_insensitive(env_file):
    assert resolve_credential_set("gerrit-cli", "EXA")[0] == "exa"
    assert resolve_credential_set("gerrit-cli", "Lustre-Bot")[0] == "bot"


def test_default_is_selectable_by_name_and_username(env_file):
    assert resolve_credential_set("gerrit-cli", "default")[0] == "default"
    assert resolve_credential_set("gerrit-cli", "pfarrell")[0] == "default"


def test_unknown_user_lists_what_there_is(env_file):
    with pytest.raises(CredentialSetError) as excinfo:
        resolve_credential_set("gerrit-cli", "nobody")
    message = str(excinfo.value)
    assert "nobody" in message
    # The error is the only place someone learns what they could have
    # typed, so it carries both halves of every set.
    assert "exa (pfarrell-exa)" in message
    assert "bot (lustre-bot)" in message
    assert excinfo.value.available == ["bot", "default", "exa"]


def test_ambiguous_username_names_the_candidates(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "GERRIT_USER=pfarrell\nGERRIT_PASS=a\n"
        "\n[one]\nGERRIT_USER=shared\nGERRIT_PASS=b\n"
        "\n[two]\nGERRIT_USER=shared\nGERRIT_PASS=c\n"
    )
    monkeypatch.setenv("GERRIT_CLI_ENV_FILE", str(path))
    with pytest.raises(CredentialSetError) as excinfo:
        resolve_credential_set("gerrit-cli", "shared")
    assert "one" in str(excinfo.value) and "two" in str(excinfo.value)


def test_apply_overrides_the_ambient_environment(env_file, monkeypatch):
    """--user must beat a value exported in the calling shell.

    Import-time loading deliberately yields to the environment; an
    explicit --user is the opposite case, a choice made on the spot.
    """
    monkeypatch.setenv("GERRIT_USER", "someone-else")
    name = apply_credential_set("gerrit-cli", "exa")
    assert name == "exa"
    assert os.environ["GERRIT_USER"] == "pfarrell-exa"
    assert os.environ["GERRIT_URL"] == "https://review.exa.com"


def test_no_credential_file_says_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.delenv("GERRIT_CLI_ENV_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CredentialSetError) as excinfo:
        resolve_credential_set("gerrit-cli", "exa")
    assert "gerrit-cli" in str(excinfo.value)


def test_literal_default_section_is_the_default_set(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "[default]\nMALOO_USER=alice\nMALOO_PASS=a\n"
        "\n[bob]\nMALOO_USER=bob\nMALOO_PASS=b\n"
    )
    monkeypatch.setenv("MALOO_TOOL_ENV_FILE", str(path))
    sets = credential_sets("maloo-tool")
    assert sets["default"]["MALOO_USER"] == "alice"
    assert resolve_credential_set("maloo-tool", "alice")[0] == "default"


def test_jira_matches_on_cloud_email(tmp_path, monkeypatch):
    """Jira Server has no username, so a set is reached by alias.

    Cloud does have one -- the email is half the basic-auth credential
    -- so that is matchable too.
    """
    path = tmp_path / ".env"
    path.write_text(
        "JIRA_SERVER=https://jira.whamcloud.com\nJIRA_TOKEN=t\n"
        "\n[acme]\nJIRA_CLOUD_SERVER=https://acme.atlassian.net\n"
        "JIRA_CLOUD_EMAIL=me@acme.com\nJIRA_CLOUD_TOKEN=c\n"
    )
    monkeypatch.setenv("JIRA_TOOL_ENV_FILE", str(path))
    assert resolve_credential_set("jira-tool", "acme")[0] == "acme"
    assert resolve_credential_set("jira-tool", "me@acme.com")[0] == "acme"
