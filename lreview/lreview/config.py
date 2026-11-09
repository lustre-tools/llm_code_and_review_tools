"""User configuration for lreview.

The file is TOML. Lookup order: $LREVIEW_CONFIG, then
~/.config/lreview/lreview-config.toml. A missing file means no settings.
See lreview-config.example.toml for the keys.
"""

import argparse
import os
import tomllib
from pathlib import Path


def config_path() -> Path:
    env = os.environ.get("LREVIEW_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "lreview" / "lreview-config.toml"


def load_config() -> dict:
    path = config_path()
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"error: bad config file {path}: {e}")


# Options that make no sense as a standing preference: they name this
# one invocation's targets, or are one-shot actions.
_NOT_CONFIGURABLE = frozenset({
    "changes", "change", "paths", "github", "local", "last", "since",
    "output", "dry_run", "clear_memory", "force", "help", "version",
    "func", "command", "worktrees_dir",
})

# Environment variable that outranks the config file for an option.
_ENV_VARS = {
    "agent": "LREVIEW_AGENT",
    "model": "LREVIEW_MODEL",
    "effort": "LREVIEW_EFFORT",
    "prefix": "LREVIEW_PREFIX",
    "prompts_dir": "REVIEW_PROMPTS_DIR",
    "lean": "LREVIEW_LEAN",
    "preload": "LREVIEW_PRELOAD",
    "backport": "LREVIEW_BACKPORT",
    "stall_timeout": "LREVIEW_STALL_TIMEOUT",
    "results_dir": "LREVIEW_RESULTS_DIR",
    "db": "LREVIEW_DB",
}


def _fail(path: Path, message: str):
    raise SystemExit(f"error: {path}: {message}")


def _convert(path: Path, key: str, action, value):
    """Turn a TOML value into what the option's argparse type yields."""
    is_list = isinstance(action, argparse._AppendAction)
    if isinstance(value, list) != is_list:
        _fail(path, f"bad value for '{key}': "
                    f"{'needs' if is_list else 'must not be'} a list")
    if is_list:
        return [_convert_one(path, key, action, v) for v in value]
    return _convert_one(path, key, action, value)


def _convert_one(path: Path, key: str, action, value):
    is_flag = (action.nargs == 0 or
               isinstance(action, argparse.BooleanOptionalAction))
    if isinstance(value, bool) != is_flag:
        _fail(path, f"bad value for '{key}': "
                    f"{'needs true or false' if is_flag else 'must not be true or false'}")
    if is_flag or action.type is None:
        converted = value
        if not is_flag and not isinstance(value, str):
            _fail(path, f"bad value for '{key}': needs a string")
    else:
        try:
            converted = action.type(str(value))
        except (ValueError, TypeError, argparse.ArgumentTypeError) as e:
            _fail(path, f"bad value for '{key}': {value!r} ({e})")
    if action.choices is not None and converted not in action.choices:
        _fail(path, f"bad value for '{key}': {value!r} "
                    f"(choose from {', '.join(map(str, action.choices))})")
    if isinstance(converted, str) and (action.dest.endswith("_dir") or action.dest == "db"):
        converted = os.path.expanduser(converted)
    return converted


def apply_config(subparsers: dict) -> None:
    """Make the config file the default of every option it names.

    Top-level keys apply to every command that has the option; a
    [command] table applies to that command only and wins. A command
    line option or the option's environment variable wins over both.
    """
    path = config_path()
    cfg = load_config()
    if not cfg:
        return
    general = {k: v for k, v in cfg.items() if not isinstance(v, dict)}
    per_command = {k: v for k, v in cfg.items() if isinstance(v, dict)}
    for name in per_command:
        if name not in subparsers:
            _fail(path, f"unknown section [{name}] "
                        f"(commands: {', '.join(sorted(subparsers))})")
    configurable = {
        cmd: {a.dest: a for a in parser._actions
              if a.option_strings and a.dest not in _NOT_CONFIGURABLE}
        for cmd, parser in subparsers.items()}
    for key in general:
        dest = key.replace("-", "_")
        if dest != "worktrees_dir" and not any(
                dest in acts for acts in configurable.values()):
            _fail(path, f"unknown option '{key}'")
    for cmd, table in per_command.items():
        for key in table:
            if key.replace("-", "_") not in configurable[cmd]:
                _fail(path, f"unknown option '{key}' in [{cmd}]")
    for cmd, actions in configurable.items():
        values = dict(general)
        values.update(per_command.get(cmd, {}))
        for key, value in values.items():
            dest = key.replace("-", "_")
            action = actions.get(dest)
            if action is None:
                continue
            env = _ENV_VARS.get(dest)
            if env and os.environ.get(env):
                continue
            action.default = _convert(path, key, action, value)
