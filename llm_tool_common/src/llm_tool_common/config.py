"""Shared configuration loading for LLM CLI tools.

Provides common env-file loading and a base config mixin used by
jenkins_tool, maloo_tool, patch_shepherd, etc.
"""

import os
from pathlib import Path


def _parse_env_file(path: Path) -> None:
    """Parse a simple KEY=VALUE .env file into os.environ.

    Supports:
      - Lines with KEY=VALUE (optional quoting with ' or ")
      - Comments (#) and blank lines are skipped
      - Does NOT override existing environment variables
    """
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip matching quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def env_file_variable(tool_name: str) -> str:
    """Name the variable that points a tool at an explicit .env file.

    ``"maloo-tool"`` -> ``"MALOO_TOOL_ENV_FILE"``, ``"gerrit-cli"`` ->
    ``"GERRIT_CLI_ENV_FILE"``.
    """
    return tool_name.replace("-", "_").upper() + "_ENV_FILE"


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

    The /etc location is included because gerrit-cli and jira-tool both had it
    before they shared this loader; dropping it while unifying them would have
    silently unconfigured any host that used it.

    Only the FIRST file found is loaded. Uses stdlib parsing only.

    Args:
        tool_name: Hyphenated tool name, e.g. "jenkins-tool", "maloo-tool".

    Raises:
        FileNotFoundError: if ``<TOOL>_ENV_FILE`` names a file that does not
            exist. Falling back would silently run with whatever credentials
            happened to be lying around, which is the exact failure this
            pointer exists to prevent, so it fails loudly instead.
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
        _parse_env_file(override_path)
        return

    env_locations = [
        Path.home() / ".config" / tool_name / ".env",
        Path(f"/etc/{tool_name}/.env"),
        Path("/shared/support_files/.env"),
        Path(".env"),
    ]
    for env_path in env_locations:
        if env_path.exists():
            _parse_env_file(env_path)
            return
