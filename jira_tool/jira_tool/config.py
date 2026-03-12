"""Configuration loading for JIRA tool."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path.home() / ".jira-tool.json"


# Load .env file from standard locations (in priority order)
def _load_env_file() -> None:
    """Load environment variables from .env file in standard locations.

    Priority order:
    1. User config directory (~/.config/jira-tool/.env)
    2. System config directory (/etc/jira-tool/.env)
    3. Current directory (.env) - for development
    """
    env_locations = [
        Path.home() / ".config" / "jira-tool" / ".env",
        Path("/etc/jira-tool/.env"),
        Path("/shared/support_files/.env"),
        Path(".env"),
    ]

    for env_path in env_locations:
        if env_path.exists():
            load_dotenv(env_path)
            return

    # No .env file found, will use environment variables or JSON config


# Load .env file when module is imported
_load_env_file()


@dataclass
class JiraConfig:
    """JIRA tool configuration."""

    server: str
    token: str
    extras: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.server:
            raise ConfigError("Server URL is required")
        if not self.token:
            raise ConfigError("API token is required")

        # Normalize server URL (remove trailing slash)
        self.server = self.server.rstrip("/")

    def get_extra(self, key: str, default: Any = None) -> Any:
        """Get an extra config value (anything beyond server/token)."""
        if self.extras:
            return self.extras.get(key, default)
        return default

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JiraConfig":
        """
        Create config from dictionary.

        Expected format:
        {
            "server": "https://jira.example.com",
            "auth": {
                "type": "token",
                "token": "your-api-token"
            }
        }

        Or simplified:
        {
            "server": "https://jira.example.com",
            "token": "your-api-token"
        }
        """
        server = data.get("server", "")

        # Handle both nested auth and flat token
        if "auth" in data and isinstance(data["auth"], dict):
            token = data["auth"].get("token", "")
        else:
            token = data.get("token", "")

        return cls(server=server, token=token, extras=data)


def load_config(
    config_path: Path | str | None = None,
    server_override: str | None = None,
    token_override: str | None = None,
) -> JiraConfig:
    """
    Load configuration from file and environment variables.

    Priority (highest to lowest):
    1. Explicit overrides passed to this function
    2. Environment variables (JIRA_SERVER, JIRA_TOKEN)
    3. Config file

    Args:
        config_path: Optional path to config file. Defaults to ~/.jira-tool.json
        server_override: Optional server URL override
        token_override: Optional token override

    Returns:
        JiraConfig instance

    Raises:
        ConfigError: If configuration is invalid or missing required fields
    """
    # Start with empty config
    config_data: dict[str, Any] = {}

    # Load from config file if it exists
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    config_path = Path(config_path)

    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(
                f"Invalid JSON in config file: {e}",
                details={"path": str(config_path)},
            ) from e
        except PermissionError as e:
            raise ConfigError(
                f"Permission denied reading config file: {config_path}",
                details={"path": str(config_path)},
            ) from e

    # Apply environment variable overrides
    env_server = os.environ.get("JIRA_SERVER")
    env_token = os.environ.get("JIRA_TOKEN")

    if env_server:
        config_data["server"] = env_server
    if env_token:
        config_data["token"] = env_token

    # Apply explicit overrides
    if server_override:
        config_data["server"] = server_override
    if token_override:
        config_data["token"] = token_override

    # Validate we have required fields
    server = config_data.get("server", "")
    token = config_data.get("token", "")

    # Also check nested auth structure
    if not token and "auth" in config_data:
        token = config_data.get("auth", {}).get("token", "")

    if not server and not token:
        raise ConfigError(
            "No configuration found. Set JIRA_SERVER and JIRA_TOKEN environment variables "
            f"or create config file at {DEFAULT_CONFIG_PATH}",
            details={
                "config_path": str(config_path),
                "env_vars": ["JIRA_SERVER", "JIRA_TOKEN"],
            },
        )

    if not server:
        raise ConfigError(
            "Server URL not configured. Set JIRA_SERVER environment variable or add 'server' to config file."
        )

    if not token:
        raise ConfigError(
            "API token not configured. Set JIRA_TOKEN environment variable or add 'token' to config file."
        )

    return JiraConfig.from_dict(config_data)


def create_sample_config(path: Path | str | None = None) -> str:
    """
    Generate a sample configuration file content.

    Args:
        path: Optional path where the config would be saved

    Returns:
        Sample config file content as string
    """
    sample = {"server": "https://jira.example.com", "auth": {"type": "token", "token": "your-api-token-here"}}
    return json.dumps(sample, indent=2)
