"""Usage telemetry for the tools in this checkout -- an anonymous weekly
check-in, modelled on ltvm's.

Five tools -- jira, gerrit, maloo, jenkins and lreview -- count their
commands into one shared file,
and once a week one check-in carrying all of them goes to
https://llm-tools.mulberrytree.cc, so the people maintaining these tools
can tell which ones are used, which commands, by whom (a person, an AI
agent or a script) and how often they fail.  Opt-out, with a notice on
the first run.

What is sent is a closed list, built in _payload() and printed verbatim
by ``llm-tools-telemetry show``:

  * a random install ID, minted once and not derived from the host;
  * host facts: OS and version, architecture, WSL or not, Python;
  * for each tool used since the last check-in: its version, how many
    times each of its commands ran with an ok/fail split, and how many
    runs came from a terminal (human), an AI agent (agent) or neither
    (script).

Never sent: arguments, change numbers, issue keys, URLs, server names,
usernames, hostnames, paths, environment values, error messages.  A
command is counted under its registered subcommand name, so a typo
arrives as nothing more than a failed "invalid".  The server stores a
keyed hash of the source address, never the address.

Notice and send are gated separately, as in ltvm: the notice prints
once on the first run; the send needs no human, so it is gated on
``enabled`` and the seven-day interval alone -- most runs of these
tools are an agent's, and gating on a TTY would count none of them.

Off switches, in precedence order:

  * ``LLM_TOOLS_TELEMETRY=0`` in the environment (one command, one CI job);
  * ``/etc/llm-tools.conf`` ``[telemetry] enabled = false`` (every user
    on the host; a user cannot undo it);
  * ``llm-tools-telemetry off`` (you).

A host whose runs each start from an empty home (an agent service that
gives every run a fresh container) can set ``install_id`` and
``state_dir`` in ``[telemetry]`` of ``/etc/llm-tools.conf`` so its runs
are one install with one schedule, exactly as ``/etc/ltvm.conf`` does
for ltvm.

Nothing here may ever fail or slow a user's command: every entry point
swallows its own exceptions, and the send runs in a detached child.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import timing
from .config import user_state_dir

log = logging.getLogger(__name__)

ENDPOINT_DEFAULT = "https://llm-tools.mulberrytree.cc/v1/checkin"
SCHEMA_VERSION = 1
DISABLE_ENV = "LLM_TOOLS_TELEMETRY"

# Tool label -> the distribution its version is read from.  Closed: a
# tool not listed here is not counted, and the server takes no other.
# The other tools share llm_tool_common's click group, and so call
# session() too; for them it does nothing at all.
TOOLS: dict[str, str] = {
    "gerrit": "gerrit-cli",
    "jira": "jira-tool",
    "maloo": "maloo-tool",
    "jenkins": "jenkins-tool",
    "lreview": "lreview",
}
# Console scripts that count.  As in update_check: a test driving main()
# through CliRunner, or a program importing a tool as a library, is not a
# use of the tool.
COMMANDS = frozenset({
    "jira",
    "gerrit", "gerrit-cli", "gc",
    "maloo",
    "jenkins",
    "lreview",
})
# Set by AI coding agents in the environment of the commands they run:
# AI_AGENT is a cross-vendor convention, the rest are Claude Code, Codex
# and Gemini CLI.  Only their presence is read.
AGENT_ENV_VARS = ("AI_AGENT", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_CI", "GEMINI_CLI")
CALLERS = ("human", "agent", "script")
# Every key is a registered command name already; this is a backstop
# against a bug, not against a user.
MAX_COMMANDS_PER_TOOL = 96
MAX_STR_LEN = 64
SEND_INTERVAL = timedelta(days=7)
SEND_TIMEOUT = 5

NOTICE = """\
jira, gerrit, maloo, jenkins and lreview send anonymous usage counts
once a week: which commands ran and whether they succeeded, no arguments.
  llm-tools-telemetry show   what it sends    llm-tools-telemetry off   stop it
"""


# ---------------------------------------------------------------------------
# Paths -- resolved per call, so tests can move them with the environment
# ---------------------------------------------------------------------------


def _endpoint() -> str:
    return os.environ.get("LLM_TOOLS_TELEMETRY_URL") or ENDPOINT_DEFAULT


def _config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "llm-tools" / "config.json"


def _site_config() -> Path:
    return Path(os.environ.get("LLM_TOOLS_SITE_CONFIG", "/etc/llm-tools.conf"))


def _state_dir() -> Path:
    shared = _site_state_dir()
    if shared is not None:
        return shared
    return user_state_dir()


def _state_file() -> Path:
    return _state_dir() / "telemetry.json"


def _counters_file() -> Path:
    return _state_dir() / "telemetry-counters.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Config, site config and state
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, Any]:
    """The telemetry block of ~/.config/llm-tools/config.json."""
    out: dict[str, Any] = {"enabled": True, "install_id": None, "notice_shown": False}
    try:
        data = json.loads(_config_file().read_text())
    except (OSError, json.JSONDecodeError):
        return out
    block = data.get("telemetry") if isinstance(data, dict) else None
    if isinstance(block, dict):
        out.update(block)
    return out


def _save_config(block: dict[str, Any]) -> None:
    """Merge *block* in, leaving update_check's settings alone."""
    path = _config_file()
    data: dict[str, Any] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            data = loaded
    data["telemetry"] = block
    _write_json(path, data, mode=None)


def _write_json(path: Path, data: dict[str, Any], mode: int | None) -> None:
    """Atomic write that never raises: several tools may run at once."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            if mode is not None:
                os.fchmod(fd, mode)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as e:
        log.debug("cannot write %s: %s", path, e)


def _site_settings() -> dict[str, str]:
    """The [telemetry] section of /etc/llm-tools.conf, or nothing."""
    site = _site_config()
    if not site.is_file():
        return {}
    parser = configparser.ConfigParser()
    try:
        parser.read(site)
        return dict(parser["telemetry"]) if parser.has_section("telemetry") else {}
    except (configparser.Error, OSError) as e:
        log.debug("ignoring unreadable %s: %s", site, e)
        return {}


def _site_disabled() -> bool:
    """The site file can only ever disable: an opt-out a user could undo
    would not be one."""
    raw = _site_settings().get("enabled", "").strip().lower()
    return raw in ("0", "no", "false", "off")


def _site_install_id() -> str | None:
    """A host-wide ID set by an admin.  Only a uuid4: a uuid1 carries the MAC."""
    raw = _site_settings().get("install_id", "").strip()
    if not raw:
        return None
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return str(parsed) if parsed.version == 4 else None


def _site_state_dir() -> Path | None:
    """Shared schedule and counters -- only alongside a site install ID,
    or one user's counts would be sent under another's ID."""
    raw = _site_settings().get("state_dir", "").strip()
    if not raw or not os.path.isabs(raw) or _site_install_id() is None:
        return None
    return Path(raw)


def _shared_mode() -> int | None:
    return 0o644 if _site_state_dir() is not None else None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_state() -> dict[str, Any]:
    return {"last_send_iso": None, **_load_json(_state_file())}


def _save_state(state: dict[str, Any]) -> None:
    _write_json(_state_file(), state, _shared_mode())


def _env_disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("0", "no", "false", "off")


def is_enabled() -> bool:
    if _env_disabled() or _site_disabled():
        return False
    return bool(_load_config().get("enabled", True))


def install_id() -> str:
    """This install's random ID, minted on first use (a site ID wins)."""
    site = _site_install_id()
    if site is not None:
        return site
    cfg = _load_config()
    existing = cfg.get("install_id")
    if isinstance(existing, str) and existing:
        return existing
    cfg["install_id"] = str(uuid.uuid4())
    _save_config(cfg)
    return cfg["install_id"]


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def _empty_counters() -> dict[str, Any]:
    return {"since": _now_iso(), "tools": {}}


def _load_counters() -> dict[str, Any]:
    data = _load_json(_counters_file())
    if not isinstance(data.get("tools"), dict) or not isinstance(data.get("since"), str):
        return _empty_counters()
    return data


@contextlib.contextmanager
def _counters_lock() -> Iterator[None]:
    """Serialise read-modify-write of the counters across processes.

    An agent runs these tools several at a time; without the lock two
    runs finishing together would each write back the count without
    the other's.  Best effort: where flock is unavailable, we count
    without it rather than not at all.
    """
    lock = _state_dir() / ".telemetry.lock"
    fh = None
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock, "a")  # noqa: SIM115
        import fcntl

        fcntl.flock(fh, fcntl.LOCK_EX)
    except (OSError, ImportError) as e:
        log.debug("telemetry lock unavailable: %s", e)
    try:
        yield
    finally:
        if fh is not None:
            fh.close()


def _caller(env: Mapping[str, str] | None = None) -> str:
    """"agent", "human" or "script".  Agents first: Codex and Gemini CLI
    run commands in a pty, so a terminal alone does not mean a person."""
    env = os.environ if env is None else env
    if any(env.get(name) for name in AGENT_ENV_VARS):
        return "agent"
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.isatty():
                return "human"
        except (OSError, ValueError):
            continue
    return "script"


def _label(command: str | None) -> str:
    if not isinstance(command, str) or not command:
        return "invalid"
    return command[:MAX_STR_LEN]


def record(tool: str, command: str | None, ok: bool) -> None:
    """Count one finished run of *tool*'s *command*."""
    if tool not in TOOLS:
        return
    with _counters_lock():
        counters = _load_counters()
        entry = counters["tools"].get(tool)
        if not isinstance(entry, dict):
            entry = {}
            counters["tools"][tool] = entry
        commands = entry.setdefault("commands", {})
        callers = entry.setdefault("callers", {})
        label = _label(command)
        if label in commands or len(commands) < MAX_COMMANDS_PER_TOOL:
            slot = commands.get(label)
            if not isinstance(slot, dict):
                slot = {"ok": 0, "fail": 0}
                commands[label] = slot
            key = "ok" if ok else "fail"
            slot[key] = int(slot.get(key, 0)) + 1
        who = _caller()
        callers[who] = int(callers.get(who, 0)) + 1
        _write_json(_counters_file(), counters, _shared_mode())


# ---------------------------------------------------------------------------
# Payload and send
# ---------------------------------------------------------------------------


def _host_os() -> tuple[str | None, str | None]:
    import platform

    if platform.system() == "Darwin":
        return "macos", (platform.mac_ver()[0] or None)
    fields: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v.strip().strip('"')
    except OSError:
        return None, None
    return fields.get("ID"), fields.get("VERSION_ID")


def _is_wsl() -> bool:
    with contextlib.suppress(OSError):
        return "microsoft" in Path("/proc/version").read_text().lower()
    return False


def _host_info() -> dict[str, Any]:
    import platform

    os_id, os_version = _host_os()
    machine = platform.machine()
    return {
        "os": os_id,
        "os_version": os_version,
        "arch": "aarch64" if machine in ("aarch64", "arm64") else machine,
        "wsl": _is_wsl(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def _tool_version(tool: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(TOOLS[tool])[:MAX_STR_LEN]
    except (PackageNotFoundError, KeyError):
        return None


def _payload() -> dict[str, Any]:
    """Everything that leaves the machine.  A new field goes on the list
    in this module's docstring first."""
    counters = _load_counters()
    tools: dict[str, Any] = {}
    for tool, entry in sorted(counters["tools"].items()):
        if tool not in TOOLS or not isinstance(entry, dict):
            continue
        tools[tool] = {
            "version": _tool_version(tool),
            "commands": entry.get("commands", {}),
            "callers": entry.get("callers", {}),
        }
    return {
        "schema": SCHEMA_VERSION,
        "install_id": install_id(),
        "sent_at": _now_iso(),
        "since": counters["since"],
        "host": _host_info(),
        "tools": tools,
    }


def _post(payload: dict[str, Any]) -> bool:
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "llm-tools-telemetry/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.debug("telemetry send failed: %s", e)
        return False


def send_now() -> bool:
    """Send one check-in.  The stamp moves on attempt, not success: a
    missed week is a missed week, not a retry loop against an outage."""
    with _counters_lock():
        payload = _payload()
    state = _load_state()
    ok = _post(payload)
    state["last_send_iso"] = _now_iso()
    _save_state(state)
    if ok:
        # Only what was sent is cleared: runs that finished while the
        # POST was in flight stay for next week.
        with _counters_lock():
            current = _load_counters()
            for tool, sent in payload["tools"].items():
                _subtract(current["tools"].get(tool), sent)
            current["tools"] = {t: e for t, e in current["tools"].items() if _has_counts(e)}
            current["since"] = payload["sent_at"]
            _write_json(_counters_file(), current, _shared_mode())
    return ok


def _subtract(entry: Any, sent: dict[str, Any]) -> None:
    if not isinstance(entry, dict):
        return
    for name, slot in sent.get("commands", {}).items():
        mine = entry.get("commands", {}).get(name)
        if isinstance(mine, dict) and isinstance(slot, dict):
            for key in ("ok", "fail"):
                mine[key] = max(0, int(mine.get(key, 0)) - int(slot.get(key, 0)))
            if not mine["ok"] and not mine["fail"]:
                del entry["commands"][name]
    for who, n in sent.get("callers", {}).items():
        if who in entry.get("callers", {}):
            entry["callers"][who] = max(0, int(entry["callers"][who]) - int(n))
            if not entry["callers"][who]:
                del entry["callers"][who]


def _has_counts(entry: Any) -> bool:
    return isinstance(entry, dict) and bool(entry.get("commands") or entry.get("callers"))


def _jitter(uid: str) -> timedelta:
    """A stable +/-12h per install, so installs set up on one day do not
    all check in within the same hour a week later."""
    digest = hashlib.sha256(uid.encode()).digest()
    return timedelta(seconds=int.from_bytes(digest[:4], "big") % 86400 - 43200)


def _due_for_send(state: dict[str, Any], uid: str) -> bool:
    last = state.get("last_send_iso")
    if not isinstance(last, str) or not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(UTC) >= last_dt + SEND_INTERVAL + _jitter(uid)


def _spawn_detached_send() -> None:
    """POST from a detached child: no tool run waits on the network."""
    with contextlib.suppress(OSError, ValueError):
        subprocess.Popen(
            [sys.executable, "-m", "llm_tool_common.telemetry", "send", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _maybe_show_notice() -> None:
    cfg = _load_config()
    if cfg.get("notice_shown"):
        return
    print(NOTICE, file=sys.stderr)
    cfg["notice_shown"] = True
    _save_config(cfg)


def maybe_send() -> None:
    if _site_install_id() is None:
        _maybe_show_notice()
    state = _load_state()
    if not _due_for_send(state, install_id()):
        return
    # Stamp before spawning, so a loop of runs does not spawn a sender
    # each in the moment before the first child records its attempt.
    state["last_send_iso"] = _now_iso()
    _save_state(state)
    _spawn_detached_send()


# ---------------------------------------------------------------------------
# The hook the tools call
# ---------------------------------------------------------------------------


def _launched_as_tool() -> bool:
    return bool(sys.argv) and Path(sys.argv[0]).name in COMMANDS


def _under_test() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def _active() -> bool:
    try:
        return _launched_as_tool() and not _under_test() and is_enabled()
    except Exception:  # noqa: BLE001
        return False


class Run:
    """One tool invocation.  The tool sets ``command`` once it knows it."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.command: str | None = None


def _exit_ok(code: object) -> bool:
    return code is None or code == 0


@contextlib.contextmanager
def session(tool: str) -> Iterator[Run]:
    """Wrap a tool's main(): counts the run, and sends if a week is up.

    It also times the run when local timing is on (timing.py), which
    is independent of whether telemetry is.

    ``SystemExit`` carries the outcome (click's standalone mode and the
    argparse tools both end in sys.exit); any other exception is a
    failure.  Both propagate untouched.
    """
    run = Run(tool)
    with timing.recording(tool, run):
        if tool not in TOOLS or not _active():
            yield run
            return
        ok = False
        try:
            yield run
            ok = True
        except SystemExit as e:
            ok = _exit_ok(e.code)
            raise
        finally:
            try:
                record(tool, run.command, ok)
                maybe_send()
            except Exception as e:  # noqa: BLE001
                log.debug("telemetry: %s", e)


# ---------------------------------------------------------------------------
# llm-tools-telemetry: status / show / on / off / send
# ---------------------------------------------------------------------------


def status() -> dict[str, Any]:
    cfg = _load_config()
    reason = None
    if _env_disabled():
        reason = f"{DISABLE_ENV} in the environment"
    elif _site_disabled():
        reason = f"{_site_config()} (site-wide)"
    elif not cfg.get("enabled", True):
        reason = str(_config_file())
    site_id = _site_install_id()
    return {
        "enabled": is_enabled(),
        "disabled_by": reason,
        "install_id": site_id or cfg.get("install_id"),
        "install_id_from": str(_site_config()) if site_id else None,
        "state_dir": str(_state_dir()),
        "last_send": _load_state().get("last_send_iso"),
        "endpoint": _endpoint(),
        "interval_days": SEND_INTERVAL.days,
    }


def set_enabled(value: bool) -> None:
    cfg = _load_config()
    cfg["enabled"] = value
    cfg["notice_shown"] = True  # whoever chose has seen what it is
    _save_config(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-tools-telemetry",
        description="Anonymous weekly usage counts for the llm tools.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="on/off, install ID, last send")
    sub.add_parser("show", help="the exact JSON a check-in would send")
    sub.add_parser("on", help="turn telemetry on")
    sub.add_parser("off", help="turn telemetry off")
    send = sub.add_parser("send", help="send a check-in now")
    send.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.action == "status":
        print(json.dumps(status(), indent=2))
    elif args.action == "show":
        print(json.dumps(_payload(), indent=2))
    elif args.action in ("on", "off"):
        set_enabled(args.action == "on")
        print(json.dumps(status(), indent=2))
    elif args.action == "send":
        if not is_enabled():
            if not args.quiet:
                print(json.dumps({"sent": False, "reason": "disabled"}))
            return 0
        ok = send_now()
        if not args.quiet:
            print(json.dumps({"sent": ok, "endpoint": _endpoint()}))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
