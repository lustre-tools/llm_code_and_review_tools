"""Daily "update available" check for the tools in this checkout.

Modelled on ltvm's update_check.  Every tool is an editable install
from one git checkout, so "the installed version" is that checkout's
HEAD, and an update is ``install.sh --update``: a fast-forward pull of
main followed by a reinstall of whatever changed.

The work is split three ways because each part has different rules:

  * **refresh** -- ``git ls-remote`` against GitHub, cache the verdict.
    At most daily.
  * **notify** -- tell somebody, at most daily while an update is
    pending.  A human at a terminal gets a prompt; anyone else (an
    agent, a script) gets one line on stderr.
  * **act** -- run ``install.sh --update``.  Only ever from that
    interactive prompt, or by hand.

stdout is never touched: it carries the tools' JSON.  The prompt and
the notice both go to stderr.

**Acting without a TTY is forbidden, not merely skipped.**  A
successful update replaces the process with a fresh run of the same
command (the old modules are already imported), and a script or agent
would see installer output and a restart nobody asked for.  So
``auto`` mode with no TTY degrades to the notice.

Nothing here may fail a command: every error is swallowed and costs at
most one more network check.

Preferences live in ``~/.config/llm-tools/config.json``::

    {"update_check": {"mode": "prompt" | "auto" | "never"}}

The cached verdict lives apart from them, in
``~/.local/state/llm-tools/update_cache.json``::

    {
      "last_check_iso": "2026-10-20T16:00:00+00:00",
      "last_notice_iso": "2026-10-20T16:00:00+00:00",
      "pending_update": {"local": "abc1234", "remote": "def5678"}
    }

Two files, because the config records what a human chose and is
written only when one answers the prompt, while the cache is machine
state written by any invocation -- including the many an agent makes,
which must not use up the check a human's next run would prompt from.

``LLM_TOOLS_NO_UPDATE_CHECK=1`` turns the whole thing off for a
process (CI, containers, fleets of agents updated some other way).
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import user_state_dir

log = logging.getLogger(__name__)


REPO_URL = "https://github.com/lustre-tools/llm_code_and_review_tools.git"
BRANCH = "main"

# How often we ask GitHub.  The tools change often enough that a weekly
# check would leave most fixes unseen for days.
CHECK_INTERVAL = timedelta(days=1)
# How often a pending update is mentioned -- prompt or notice alike.
# Answering "not now" is good for a day, not for one command.
NOTICE_INTERVAL = timedelta(days=1)

# The core tools' console scripts.  The check runs only when the
# process was started as one of them: a test driving main() through
# CliRunner, or a program importing a tool as a library, must not hit
# the network or prompt.  test_update_check keeps this in step with
# those tools' pyproject.toml.
COMMANDS = frozenset({
    "jira",
    "gerrit", "gerrit-cli", "gc",
    "maloo",
    "jenkins",
    "janitor",
    "lreview",
    "pw-cli",
    "release-priority",
})

DISABLE_ENV = "LLM_TOOLS_NO_UPDATE_CHECK"
# Set on the command re-run after an update, for that process only:
# _check() removes it, so the tools it starts check as usual.
_RERUN_ENV = "LLM_TOOLS_RERUN_AFTER_UPDATE"

_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "llm-tools"
)
_CONFIG_FILE = _CONFIG_DIR / "config.json"

_STATE_DIR = user_state_dir()
_STATE_FILE = _STATE_DIR / "update_cache.json"

# llm_tool_common/src/llm_tool_common/update_check.py -> the checkout.
_REPO = Path(__file__).resolve().parents[3]

_DEFAULT_CONFIG: dict[str, Any] = {"update_check": {"mode": "prompt"}}
_DEFAULT_STATE: dict[str, Any] = {
    "last_check_iso": None,
    "last_notice_iso": None,
    "pending_update": None,
}


# ---------------------------------------------------------------------------
# Config and state IO
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, Any]:
    out = copy.deepcopy(_DEFAULT_CONFIG)
    try:
        data = json.loads(_CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return out
    uc = data.get("update_check") if isinstance(data, dict) else None
    if isinstance(uc, dict):
        out["update_check"].update(uc)
    return out


def _save_config(cfg: dict[str, Any]) -> None:
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    except OSError as e:
        log.debug("cannot write %s: %s", _CONFIG_FILE, e)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_state() -> dict[str, Any]:
    out = copy.deepcopy(_DEFAULT_STATE)
    try:
        data = json.loads(_STATE_FILE.read_text())
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        out.update(data)
    return out


def _save_state(state: dict[str, Any]) -> None:
    """Persist the cached verdict atomically.

    An agent can run a dozen tools at once, so a plain write could be
    read back half-written; tempfile + replace makes every reader see a
    whole file.
    """
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_STATE_DIR), prefix=".update_cache.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(state, indent=2) + "\n")
            os.replace(tmp, _STATE_FILE)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as e:
        log.debug("cannot write %s: %s", _STATE_FILE, e)


def _mark_noticed(state: dict[str, Any]) -> None:
    state["last_notice_iso"] = _now_iso()
    _save_state(state)


def _clear_pending(state: dict[str, Any]) -> None:
    state["pending_update"] = None
    _save_state(state)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _launched_as_tool() -> bool:
    """True if this process was started as one of the tools' commands."""
    return bool(sys.argv) and Path(sys.argv[0]).name in COMMANDS


def _under_test() -> bool:
    """True inside a pytest run, or a process a test started.

    argv alone is not enough: tests set sys.argv to ["gerrit", ...] and
    call main(), and that must not reach GitHub or the real cache.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def _is_interactive() -> bool:
    """True if a human is at the terminal: stdin, stdout and stderr TTYs.

    stdout too, although the prompt goes to stderr: ``jira get X | jq``
    is a pipeline, and a pipeline is a script whether or not a human
    started it.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _elapsed_since(stamp: Any, interval: timedelta) -> bool:
    """True if *stamp* is missing, unparseable, or older than *interval*."""
    if not isinstance(stamp, str) or not stamp:
        return True
    try:
        stamp_dt = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return datetime.now(UTC) - stamp_dt >= interval


# ---------------------------------------------------------------------------
# Local and remote comparison
# ---------------------------------------------------------------------------


def _git(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    # GIT_TERMINAL_PROMPT=0: a credential prompt here would hang a tool
    # that was only asked to read a Jira issue.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _local_head() -> str | None:
    """Full sha of the checkout's HEAD, or None if it is not on main.

    Someone on a branch, or detached, is developing the tools and
    manages the checkout themselves -- and ``install.sh --update``
    would refuse there anyway.  None also covers "not a git checkout"
    (a non-editable install) and git being unavailable.
    """
    if not (_REPO / ".git").exists() or not (_REPO / "install.sh").is_file():
        return None
    try:
        r = _git(
            "-C", str(_REPO), "rev-parse", "HEAD",
            "--symbolic-full-name", "HEAD",
            timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    lines = r.stdout.split()
    if r.returncode != 0 or len(lines) != 2:
        return None
    sha, ref = lines
    if ref != f"refs/heads/{BRANCH}":
        return None
    return sha


def _remote_head() -> str | None:
    """Full sha of main on GitHub, or None on any failure.

    The public https URL rather than the checkout's origin: origin may
    be an ssh remote whose key wants a passphrase.
    """
    try:
        r = _git("ls-remote", REPO_URL, f"refs/heads/{BRANCH}", timeout=5)
    except (subprocess.SubprocessError, OSError):
        return None
    out = r.stdout.split()
    if r.returncode != 0 or not out:
        return None
    return out[0]


def _is_newer(local: str, remote: str) -> bool:
    """True unless the local HEAD already contains the remote one.

    A checkout ahead of GitHub (unpushed commits on main) has nothing
    to pull.  A remote sha this checkout has never fetched makes
    merge-base fail, which correctly reads as "newer".
    """
    if local == remote:
        return False
    try:
        r = _git(
            "-C", str(_REPO), "merge-base", "--is-ancestor", remote, local,
            timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return True
    return r.returncode != 0


def _refresh_cache(state: dict[str, Any]) -> None:
    """Ask GitHub and cache the verdict in *state*.

    A failed check leaves any earlier verdict alone -- an offline day
    should not make a known update disappear -- but still bumps
    last_check_iso, so a host that cannot reach GitHub does not try on
    every command.
    """
    state["last_check_iso"] = _now_iso()
    local = _local_head()
    remote = _remote_head() if local is not None else None
    if local is not None and remote is not None:
        state["pending_update"] = (
            {"local": local[:7], "remote": remote[:7]}
            if _is_newer(local, remote)
            else None
        )
    _save_state(state)


def _pending_if_current(state: dict[str, Any]) -> dict[str, Any] | None:
    """The cached verdict, or None if the checkout has moved since.

    Someone who just ran ``install.sh --update`` (or pulled by hand)
    should not hear about the update they already have.
    """
    pending = state.get("pending_update")
    if not isinstance(pending, dict):
        return None
    head = _local_head()
    if head is None or head[:7] != pending.get("local"):
        _clear_pending(state)
        return None
    return pending


# ---------------------------------------------------------------------------
# Notify and act
# ---------------------------------------------------------------------------


def update_command() -> str:
    """The command that updates this checkout, for messages."""
    return f"{_REPO / 'install.sh'} --update"


_NOTICE = "llm-tools: update available ({local} -> {remote}).  Run: {cmd}"

_PROMPT = """
A newer version of the LLM tools is available (local={local}, remote={remote}).

  [y] yes, update now
  [a] auto-update from now on (still checks daily)
  [n] not right now (asks again tomorrow)
  [x] don't ask again (stops checking for updates)

Your choice [y/a/N/x]: """


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _prompt_choice(local: str, remote: str) -> str:
    """Ask y/a/n/x on stderr; anything else, EOF or ^C is "n"."""
    sys.stderr.write(_PROMPT.format(local=local, remote=remote))
    sys.stderr.flush()
    try:
        ans = sys.stdin.readline()
    except (OSError, KeyboardInterrupt):
        _say("")
        return "n"
    ans = ans.strip().lower()
    return ans[0] if ans else "n"


def _apply_update() -> bool:
    """Run ``install.sh --update`` with the interpreter running this tool.

    The installer picks its own Python, which on a host with several is
    not necessarily the one these tools were installed into.  When that
    is a venv, --venv names it.  --no-configure because someone who
    asked for a Jira issue and said "yes, update" did not ask for the
    credential walkthrough; the installer still prints what is unset.
    """
    cmd = ["bash", str(_REPO / "install.sh"), "--update", "--no-configure"]
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        cmd += ["--venv", sys.prefix]
    _say(f"  Running {' '.join(cmd)}...")
    try:
        # stdout to our stderr: the installer's progress is not the
        # tool's output, even on a terminal.
        r = subprocess.run(cmd, stdout=sys.stderr, check=False)
    except OSError as e:
        _say(f"  Could not run the installer: {e}")
        return False
    if r.returncode != 0:
        _say("  Update failed (see above); continuing with this version.")
        return False
    return True


def _rerun_after_update() -> None:
    """Run the command again in a fresh process, on the updated code.

    The pull replaced the source under a process that has already
    imported it; the rest of this run would mix old and new code.
    """
    _say("  LLM tools updated.  Running your command again...")
    env = dict(os.environ, **{_RERUN_ENV: "1"})
    try:
        os.execve(sys.executable, [sys.executable, *sys.argv], env)
    except OSError as e:
        _say(f"  Could not run it again ({e}); re-run your command.")
        raise SystemExit(1) from e


def _handle_interactive(
    cfg: dict[str, Any], state: dict[str, Any], mode: str,
    pending: dict[str, Any],
) -> None:
    local = pending.get("local", "?")
    remote = pending.get("remote", "?")
    _mark_noticed(state)  # we told them, whatever they answer

    if mode == "auto":
        _say(f"llm-tools: auto-updating ({local} -> {remote})...")
        choice = "y"
    else:
        choice = _prompt_choice(local, remote)

    if choice == "a":
        cfg["update_check"]["mode"] = "auto"
        _save_config(cfg)
    if choice in ("y", "a"):
        if _apply_update():
            _clear_pending(state)
            _rerun_after_update()
    elif choice == "x":
        cfg["update_check"]["mode"] = "never"
        _save_config(cfg)
        _say(
            "  The LLM tools will not check for updates again.  "
            f"Re-enable with: rm {_CONFIG_FILE}"
        )


def _emit_notice(state: dict[str, Any], pending: dict[str, Any]) -> None:
    _say(_NOTICE.format(
        local=pending.get("local", "?"),
        remote=pending.get("remote", "?"),
        cmd=update_command(),
    ))
    _mark_noticed(state)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def maybe_check_for_updates() -> None:
    """Called once per invocation, at the top of each tool's main().

    Cheap when there is nothing to do: two small file reads, and a git
    call only when the daily check is due or an update is pending.
    Never raises, except SystemExit when the command cannot be run
    again after a successful update.
    """
    try:
        _check()
    except SystemExit:
        raise
    except Exception as e:  # advisory: never fail the user's command
        log.debug("update check failed: %s", e)


def _check() -> None:
    if os.environ.pop(_RERUN_ENV, None):
        return
    if os.environ.get(DISABLE_ENV, "").strip() not in ("", "0"):
        return
    if not _launched_as_tool() or _under_test():
        return
    cfg = _load_config()
    mode = cfg["update_check"].get("mode", "prompt")
    if mode == "never":
        return

    state = _load_state()
    if _elapsed_since(state.get("last_check_iso"), CHECK_INTERVAL):
        _refresh_cache(state)

    pending = _pending_if_current(state)
    if pending is None:
        return
    if not _elapsed_since(state.get("last_notice_iso"), NOTICE_INTERVAL):
        return

    if _is_interactive():
        _handle_interactive(cfg, state, mode, pending)
    else:
        _emit_notice(state, pending)
