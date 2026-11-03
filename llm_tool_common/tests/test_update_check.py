"""Tests for llm_tool_common.update_check.

Nothing here touches the network or the real ~/.config: the config and
cache paths point into tmp_path, and the git-backed comparisons are
replaced unless a test builds a throwaway repository of its own.
"""

import json
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from llm_tool_common import update_check as uc

REPO = Path(__file__).resolve().parents[2]

LOCAL = "a" * 40
REMOTE = "b" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tool launched as `jira`, with private state and a fake GitHub."""
    monkeypatch.setattr(uc, "_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(uc, "_CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(uc, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(uc, "_STATE_FILE", tmp_path / "state" / "update_cache.json")
    monkeypatch.delenv(uc.DISABLE_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["/home/u/.local/bin/jira", "get", "LU-1"])
    monkeypatch.setattr(uc, "_under_test", lambda: False)

    class Fake:
        local = LOCAL
        remote = REMOTE
        newer = True
        interactive = False
        remote_calls = 0
        applied = []
        apply_ok = True
        answer = "n"

    def remote_head():
        Fake.remote_calls += 1
        return Fake.remote

    def apply_update():
        Fake.applied.append(True)
        return Fake.apply_ok

    def prompt(local, remote):
        return Fake.answer

    Fake.applied = []
    monkeypatch.setattr(uc, "_local_head", lambda: Fake.local)
    monkeypatch.setattr(uc, "_remote_head", remote_head)
    monkeypatch.setattr(uc, "_is_newer", lambda local, remote: Fake.newer)
    monkeypatch.setattr(uc, "_is_interactive", lambda: Fake.interactive)
    monkeypatch.setattr(uc, "_apply_update", apply_update)
    monkeypatch.setattr(uc, "_prompt_choice", prompt)
    return Fake


def state():
    return json.loads(uc._STATE_FILE.read_text())


def age(key, by):
    """Move a cached timestamp *by* into the past."""
    s = state()
    s[key] = (datetime.fromisoformat(s[key]) - by).isoformat()
    uc._STATE_FILE.write_text(json.dumps(s))


# --- who gets checked ------------------------------------------------------


# The tools that offer updates.  lustre-crash and gerrit-dashboard update
# with the checkout but do not ask.
CORE_TOOLS = (
    "jira_tool", "gerrit_cli", "maloo_tool", "jenkins_tool", "janitor_tool",
    "lreview", "pw_cli", "release_priority",
)


def _tool_projects():
    """(dir, [project]) of every core tool."""
    for name in CORE_TOOLS:
        pyproject = REPO / name / "pyproject.toml"
        yield pyproject.parent, tomllib.loads(pyproject.read_text())["project"]


def test_commands_match_every_console_script():
    scripts = set()
    for _, project in _tool_projects():
        scripts |= set(project.get("scripts", {}))
    assert scripts == uc.COMMANDS


def test_every_tool_main_calls_the_check():
    """A tool added later should not silently skip the check."""
    for tool_dir, project in _tool_projects():
        for target in set(project.get("scripts", {}).values()):
            module = target.split(":")[0]
            source = tool_dir / (module.replace(".", "/") + ".py")
            assert "maybe_check_for_updates()" in source.read_text(), source


@pytest.mark.parametrize("argv0", [
    "/usr/bin/pytest", "python", "", "/x/jira_tool/cli.py", "lustre-crash",
])
def test_not_launched_as_a_tool_does_nothing(env, monkeypatch, argv0):
    monkeypatch.setattr(sys, "argv", [argv0])
    uc.maybe_check_for_updates()
    assert env.remote_calls == 0
    assert not uc._STATE_FILE.exists()


@pytest.mark.parametrize("argv0", [
    "/venv/bin/gc", "lreview", "/home/u/.local/bin/pw-cli",
])
def test_launched_as_a_tool(monkeypatch, argv0):
    monkeypatch.setattr(sys, "argv", [argv0])
    assert uc._launched_as_tool()


def test_quiet_under_pytest_even_with_a_tool_argv(env, monkeypatch):
    """gerrit_cli's tests set sys.argv to ["gerrit", ...] and call main()."""
    monkeypatch.undo()
    monkeypatch.setattr(sys, "argv", ["gerrit", "info", "1"])
    monkeypatch.setattr(uc, "_remote_head", lambda: pytest.fail("network"))
    monkeypatch.setattr(uc, "_load_config", lambda: pytest.fail("config read"))
    uc.maybe_check_for_updates()


def test_env_var_disables(env, monkeypatch):
    monkeypatch.setenv(uc.DISABLE_ENV, "1")
    uc.maybe_check_for_updates()
    assert env.remote_calls == 0


def test_env_var_zero_does_not_disable(env, monkeypatch, capsys):
    monkeypatch.setenv(uc.DISABLE_ENV, "0")
    uc.maybe_check_for_updates()
    assert env.remote_calls == 1


def test_never_mode_does_nothing(env):
    uc._save_config({"update_check": {"mode": "never"}})
    uc.maybe_check_for_updates()
    assert env.remote_calls == 0


# --- non-interactive callers: one stderr line a day ------------------------


def test_notice_goes_to_stderr_only(env, capsys):
    uc.maybe_check_for_updates()
    out, err = capsys.readouterr()
    assert out == ""
    assert "update available (aaaaaaa -> bbbbbbb)" in err
    assert "install.sh --update" in err
    assert state()["pending_update"] == {"local": "aaaaaaa", "remote": "bbbbbbb"}


def test_notice_and_check_at_most_daily(env, capsys):
    uc.maybe_check_for_updates()
    capsys.readouterr()
    uc.maybe_check_for_updates()
    assert capsys.readouterr().err == ""
    assert env.remote_calls == 1

    age("last_notice_iso", timedelta(days=1, minutes=1))
    uc.maybe_check_for_updates()
    assert "update available" in capsys.readouterr().err
    assert env.remote_calls == 1  # the verdict was still fresh


def test_nothing_said_when_up_to_date(env, capsys):
    env.newer = False
    uc.maybe_check_for_updates()
    assert capsys.readouterr().err == ""
    assert state()["pending_update"] is None


def test_auto_mode_without_a_tty_only_notices(env, capsys):
    uc._save_config({"update_check": {"mode": "auto"}})
    uc.maybe_check_for_updates()
    assert env.applied == []
    assert "update available" in capsys.readouterr().err


def test_moved_checkout_clears_the_pending_update(env, capsys):
    uc.maybe_check_for_updates()
    capsys.readouterr()
    age("last_notice_iso", timedelta(days=2))
    env.local = REMOTE  # pulled by hand
    uc.maybe_check_for_updates()
    assert capsys.readouterr().err == ""
    assert state()["pending_update"] is None


def test_off_main_says_nothing(env, capsys):
    env.local = None
    uc.maybe_check_for_updates()
    assert capsys.readouterr().err == ""
    assert env.remote_calls == 0


def test_failed_check_keeps_the_known_update(env, capsys):
    uc.maybe_check_for_updates()
    capsys.readouterr()
    age("last_check_iso", timedelta(days=2))
    age("last_notice_iso", timedelta(days=2))
    env.remote = None  # offline today
    uc.maybe_check_for_updates()
    assert "update available" in capsys.readouterr().err
    assert env.remote_calls == 2
    # ... and does not retry on the next command.
    uc.maybe_check_for_updates()
    assert env.remote_calls == 2


def test_unreadable_files_are_defaults(env, capsys):
    uc._CONFIG_DIR.mkdir()
    uc._CONFIG_FILE.write_text("{not json")
    uc._STATE_DIR.mkdir()
    uc._STATE_FILE.write_text("[]")
    uc.maybe_check_for_updates()
    assert "update available" in capsys.readouterr().err


def test_errors_never_reach_the_caller(env, monkeypatch):
    def boom():
        raise RuntimeError("bug in the check")
    monkeypatch.setattr(uc, "_remote_head", boom)
    uc.maybe_check_for_updates()  # does not raise


# --- interactive callers: the prompt ---------------------------------------


def test_no_means_tomorrow(env):
    env.interactive = True
    env.answer = "n"
    uc.maybe_check_for_updates()
    assert env.applied == []
    prompted = state()["last_notice_iso"]
    uc.maybe_check_for_updates()
    assert state()["last_notice_iso"] == prompted


class _Execed(BaseException):
    """Stands in for an exec, which never returns; BaseException gets it
    past maybe_check_for_updates(), which swallows Exception."""


@pytest.fixture
def execve(monkeypatch):
    """Record os.execve instead of replacing the test process."""
    calls = []

    def fake(path, args, environ):
        calls.append((path, args, environ))
        raise _Execed
    monkeypatch.setattr(uc.os, "execve", fake)
    return calls


def test_yes_updates_and_runs_the_command_again(env, execve):
    """After "y" the command the user typed runs, on the new code."""
    env.interactive = True
    env.answer = "y"
    with pytest.raises(_Execed):
        uc.maybe_check_for_updates()
    assert env.applied == [True]
    assert state()["pending_update"] is None
    [(path, args, environ)] = execve
    assert path == sys.executable
    assert args == [sys.executable, "/home/u/.local/bin/jira", "get", "LU-1"]
    assert environ[uc._RERUN_ENV] == "1"


def test_the_rerun_does_not_check_but_its_children_do(env, monkeypatch):
    monkeypatch.setenv(uc._RERUN_ENV, "1")
    env.interactive = True
    env.answer = "y"
    uc.maybe_check_for_updates()
    assert env.remote_calls == 0 and env.applied == []
    assert uc._RERUN_ENV not in uc.os.environ


def test_a_rerun_that_cannot_start_says_so(env, monkeypatch, capsys):
    def fail(*a):
        raise OSError("exec format error")
    monkeypatch.setattr(uc.os, "execve", fail)
    env.interactive = True
    env.answer = "y"
    with pytest.raises(SystemExit) as e:
        uc.maybe_check_for_updates()
    assert e.value.code == 1
    assert "re-run your command" in capsys.readouterr().err


def test_failed_update_runs_the_command_anyway(env):
    env.interactive = True
    env.answer = "y"
    env.apply_ok = False
    uc.maybe_check_for_updates()  # no SystemExit
    assert state()["pending_update"] is not None


def test_auto_remembers_and_updates(env, execve):
    env.interactive = True
    env.answer = "a"
    with pytest.raises(_Execed):
        uc.maybe_check_for_updates()
    assert uc._load_config()["update_check"]["mode"] == "auto"


def test_auto_mode_updates_without_asking(env, monkeypatch, execve):
    env.interactive = True
    uc._save_config({"update_check": {"mode": "auto"}})
    monkeypatch.setattr(uc, "_prompt_choice", lambda *a: pytest.fail("asked"))
    with pytest.raises(_Execed):
        uc.maybe_check_for_updates()
    assert env.applied == [True]


def test_x_stops_checking(env, capsys):
    env.interactive = True
    env.answer = "x"
    uc.maybe_check_for_updates()
    assert uc._load_config()["update_check"]["mode"] == "never"
    assert "will not check" in capsys.readouterr().err


def test_prompt_reads_stdin_and_writes_stderr(monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("Yes\n"))
    assert uc._prompt_choice("aaaaaaa", "bbbbbbb") == "y"
    out, err = capsys.readouterr()
    assert out == ""
    assert "[y/a/N/x]" in err
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert uc._prompt_choice("aaaaaaa", "bbbbbbb") == "n"


# --- the installer call ----------------------------------------------------


def test_apply_update_names_the_running_venv(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "prefix", "/opt/tools-venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert uc._apply_update() is True
    assert calls[0][-4:] == ["--update", "--no-configure", "--venv", "/opt/tools-venv"]


def test_apply_update_without_a_venv(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1),
    )
    assert uc._apply_update() is False
    assert calls[0][-2:] == ["--update", "--no-configure"]


# --- git, for real ---------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "checkout"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "install.sh").write_text("#!/bin/bash\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "one")
    monkeypatch.setattr(uc, "_REPO", r)
    return r


def test_local_head_on_main(repo):
    assert uc._local_head() == _git(repo, "rev-parse", "HEAD")


def test_local_head_off_main(repo):
    _git(repo, "switch", "-qc", "topic")
    assert uc._local_head() is None
    _git(repo, "switch", "-q", "--detach", "main")
    assert uc._local_head() is None


def test_local_head_not_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "_REPO", tmp_path)
    assert uc._local_head() is None


def test_is_newer(repo):
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "two")
    second = _git(repo, "rev-parse", "HEAD")
    assert uc._is_newer(first, second)       # behind
    assert not uc._is_newer(second, first)   # ahead: nothing to pull
    assert not uc._is_newer(second, second)
    assert uc._is_newer(second, "c" * 40)    # never fetched
