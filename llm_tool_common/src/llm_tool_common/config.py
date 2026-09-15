"""Shared configuration loading for LLM CLI tools.

Provides common env-file loading and a base config mixin used by
jenkins_tool, maloo_tool, patch_shepherd, etc.

A .env file may hold more than one credential set.  Keys before the
first ``[alias]`` header are the default set, which is what every tool
uses when nothing asks otherwise; each header starts a named set that
``--user`` can select.
"""

import os
import re
from pathlib import Path

# The set used when --user says nothing.  Also accepted as a literal
# [default] header, so a file can name every section if the author
# prefers that to a leading unnamed block.
DEFAULT_SET = "default"

_SECTION_RE = re.compile(r"^\[\s*([A-Za-z0-9._@+-]+)\s*\]\s*$")

# The key in each tool's .env that holds the account name, so --user can
# take one instead of an alias.  Jira Server authenticates with a token
# alone and has no username; JIRA_USER is read only here, as a label for
# saying whose set this is.
USERNAME_KEYS: dict[str, tuple[str, ...]] = {
    "gerrit-cli": ("GERRIT_USER",),
    "jira-tool": ("JIRA_USER", "JIRA_CLOUD_EMAIL"),
    "maloo-tool": ("MALOO_USER",),
    "jenkins-tool": ("JENKINS_USER",),
}


class CredentialSetError(Exception):
    """Raised when --user names no credential set, or an ambiguous one."""

    def __init__(self, message: str, available: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.available = available or []


def env_file_variable(tool_name: str) -> str:
    """Name the variable that points a tool at an explicit .env file.

    ``"maloo-tool"`` -> ``"MALOO_TOOL_ENV_FILE"``, ``"gerrit-cli"`` ->
    ``"GERRIT_CLI_ENV_FILE"``.
    """
    return tool_name.replace("-", "_").upper() + "_ENV_FILE"


def env_file_locations(tool_name: str) -> list[Path]:
    """The standard .env locations for a tool, highest priority first.

    The /etc location is included because gerrit-cli and jira-tool both
    had it before they shared this loader; dropping it while unifying
    them would have silently unconfigured any host that used it.
    """
    return [
        Path.home() / ".config" / tool_name / ".env",
        Path(f"/etc/{tool_name}/.env"),
        Path("/shared/support_files/.env"),
        Path(".env"),
    ]


def resolve_env_file(tool_name: str) -> Path | None:
    """The one .env file this tool reads, or None if there is none.

    Raises:
        FileNotFoundError: if ``<TOOL>_ENV_FILE`` names a file that does
            not exist.  Falling back would silently run with whatever
            credentials happened to be lying around, which is the exact
            failure this pointer exists to prevent.
    """
    override = os.environ.get(env_file_variable(tool_name))
    if override:
        override_path = Path(override)
        if not override_path.is_file():
            raise FileNotFoundError(
                f"{env_file_variable(tool_name)} points at {override}, "
                "which is not a readable file. Refusing to fall back to the "
                "default configuration."
            )
        return override_path

    for env_path in env_file_locations(tool_name):
        if env_path.exists():
            return env_path
    return None


def parse_env_file(path: Path) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Split a KEY=VALUE file into its default set and its named sets.

    Supports:
      - Lines with KEY=VALUE (optional quoting with ' or ")
      - ``[alias]`` headers, each starting a named credential set
      - Comments (#) and blank lines are skipped

    Within a set the first value of a key wins, which is what the plain
    loader did before sections existed.
    """
    default: dict[str, str] = {}
    sets: dict[str, dict[str, str]] = {}
    current = default

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            section = _SECTION_RE.match(line)
            if section:
                name = section.group(1)
                if name.lower() == DEFAULT_SET:
                    current = default
                else:
                    current = sets.setdefault(name, {})
                continue

            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip matching quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key:
                current.setdefault(key, value)

    return default, sets


def _parse_env_file(path: Path) -> None:
    """Load a file's default credential set into os.environ.

    Does NOT override existing environment variables, and does not look
    at named sections: those are reached through --user, which is a
    deliberate choice rather than something a file should make for a
    caller that did not ask.
    """
    default, _ = parse_env_file(path)
    for key, value in default.items():
        if key not in os.environ:
            os.environ[key] = value


def load_env_files(tool_name: str) -> None:
    """Load environment variables from .env files in standard locations.

    Precedence, highest first:
      1. Variables already set in the real environment. Never overridden, so a
         caller can always decide what a tool it spawns will use.
      2. The file named by ``<TOOL>_ENV_FILE`` (see :func:`env_file_variable`),
         if that variable is set. This lets one process give a child tool a
         different identity than the one the developer uses interactively,
         without touching HOME or the developer's own config.
      3. ~/.config/{tool_name}/.env
      4. /etc/{tool_name}/.env
      5. /shared/support_files/.env
      6. ./.env

    Only the FIRST file found is loaded. Uses stdlib parsing only.

    Args:
        tool_name: Hyphenated tool name, e.g. "jenkins-tool", "maloo-tool".

    Raises:
        FileNotFoundError: if ``<TOOL>_ENV_FILE`` names a file that does not
            exist.
    """
    env_path = resolve_env_file(tool_name)
    if env_path is not None:
        _parse_env_file(env_path)


def credential_sets(tool_name: str) -> dict[str, dict[str, str]]:
    """Every credential set in this tool's .env, keyed by name.

    A named set is returned merged over the default one, so a second
    account on the same server needs only the credential itself and
    inherits the URL and anything else it does not repeat.
    """
    env_path = resolve_env_file(tool_name)
    if env_path is None:
        return {}

    default, sets = parse_env_file(env_path)
    merged: dict[str, dict[str, str]] = {DEFAULT_SET: dict(default)}
    for name, values in sets.items():
        combined = dict(default)
        combined.update(values)
        merged[name] = combined
    return merged


def _describe_sets(
    sets: dict[str, dict[str, str]], username_keys: tuple[str, ...]
) -> str:
    """Render the available sets as "alias (username)" for an error."""
    parts = []
    for name in sorted(sets):
        username = ""
        for key in username_keys:
            if sets[name].get(key):
                username = sets[name][key]
                break
        parts.append(f"{name} ({username})" if username else name)
    return ", ".join(parts)


def resolve_credential_set(
    tool_name: str,
    user: str,
    username_keys: tuple[str, ...] | None = None,
) -> tuple[str, dict[str, str]]:
    """Find the credential set that --user names.

    Matches the section alias first, then the account name inside each
    set, both case-insensitively.  Returns the set's name and values.
    """
    if username_keys is None:
        username_keys = USERNAME_KEYS.get(tool_name, ())

    sets = credential_sets(tool_name)
    if not sets:
        locations = ", ".join(str(p) for p in env_file_locations(tool_name))
        raise CredentialSetError(
            f"--user {user} needs a credential file, and there is none. "
            f"Looked in: {locations}"
        )

    wanted = user.strip().lower()
    for name in sets:
        if name.lower() == wanted:
            return name, sets[name]

    matches = [
        name
        for name, values in sets.items()
        if any(
            values.get(key, "").strip().lower() == wanted
            for key in username_keys
            if values.get(key)
        )
    ]
    if len(matches) == 1:
        return matches[0], sets[matches[0]]
    if len(matches) > 1:
        raise CredentialSetError(
            f"--user {user} matches more than one credential set "
            f"({', '.join(sorted(matches))}). Give the alias instead.",
            available=sorted(sets),
        )

    raise CredentialSetError(
        f"--user {user} matches no credential set. "
        f"Available: {_describe_sets(sets, username_keys)}",
        available=sorted(sets),
    )


def argv_option_value(argv: list[str], names: tuple[str, ...]) -> str | None:
    """Read an option's value straight out of a raw argument list.

    For a choice that has to be made before the argument parser runs.
    gerrit-cli's client reads GERRIT_URL into a module constant as it is
    imported, and the command modules are imported before main() gets
    its parsed arguments, so --user has to be found the hard way first.
    """
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name:
                return argv[i + 1] if i + 1 < len(argv) else None
            if arg.startswith(f"{name}="):
                return arg[len(name) + 1:]
    return None


def hoist_args(
    argv: list[str],
    flags: tuple[str, ...] = (),
    options: tuple[str, ...] = (),
) -> list[str]:
    """Move global flags and options to the front of an argument list.

    Click parses a group's own options only before the subcommand name,
    so without this ``tool sub --user x`` is a usage error while
    ``tool --user x sub`` works -- a distinction neither people nor
    agents reliably remember.
    """
    hoisted: list[str] = []
    remaining: list[str] = []
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in flags:
            hoisted.append(arg)
        elif arg in options:
            hoisted.append(arg)
            if i + 1 < len(argv):
                hoisted.append(argv[i + 1])
                skip_next = True
        elif any(arg.startswith(f"{opt}=") for opt in options):
            hoisted.append(arg)
        else:
            remaining.append(arg)
    return hoisted + remaining


def apply_credential_set(
    tool_name: str,
    user: str,
    username_keys: tuple[str, ...] | None = None,
) -> str:
    """Put the set that --user names into the environment, and name it.

    Overriding, unlike the import-time load: --user is a deliberate
    choice and has to beat whatever the surrounding shell exported.
    """
    name, values = resolve_credential_set(tool_name, user, username_keys)
    for key, value in values.items():
        os.environ[key] = value
    return name
