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


def load_env_files(tool_name: str) -> None:
    """Load environment variables from .env files in standard locations.

    Checks the following paths in order, loading the FIRST one found:
      1. ~/.config/{tool_name}/.env
      2. /shared/support_files/.env
      3. ./.env

    Uses stdlib parsing only (no python-dotenv dependency).
    Does not override variables already set in the environment.

    Args:
        tool_name: Hyphenated tool name, e.g. "jenkins-tool", "maloo-tool".
    """
    env_locations = [
        Path.home() / ".config" / tool_name / ".env",
        Path("/shared/support_files/.env"),
        Path(".env"),
    ]
    for env_path in env_locations:
        if env_path.exists():
            _parse_env_file(env_path)
            return
