"""Configuration loading for JIRA tool."""

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path.home() / ".jira-tool.json"

# Valid auth types
AUTH_TYPE_BEARER = "bearer"
AUTH_TYPE_BASIC = "basic"
VALID_AUTH_TYPES = {AUTH_TYPE_BEARER, AUTH_TYPE_BASIC}


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
    auth_type: str = AUTH_TYPE_BEARER
    email: str | None = None
    extras: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.server:
            raise ConfigError("Server URL is required")
        if not self.token:
            raise ConfigError("API token is required")
        if self.auth_type not in VALID_AUTH_TYPES:
            raise ConfigError(
                f"Invalid auth type '{self.auth_type}'. Must be one of: {', '.join(sorted(VALID_AUTH_TYPES))}"
            )
        if self.auth_type == AUTH_TYPE_BASIC and not self.email:
            raise ConfigError("Email is required for basic auth (JIRA Cloud)")

        # Normalize server URL (remove trailing slash)
        self.server = self.server.rstrip("/")

    def get_extra(self, key: str, default: Any = None) -> Any:
        """Get an extra config value (anything beyond server/token)."""
        if self.extras:
            return self.extras.get(key, default)
        return default

    def get_auth_header(self) -> str:
        """Return the Authorization header value for this config."""
        if self.auth_type == AUTH_TYPE_BASIC:
            credentials = base64.b64encode(
                f"{self.email}:{self.token}".encode()
            ).decode()
            return f"Basic {credentials}"
        return f"Bearer {self.token}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JiraConfig":
        """
        Create config from dictionary.

        Supported formats:

        Flat (legacy):
        {
            "server": "https://jira.example.com",
            "token": "your-api-token"
        }

        Nested auth:
        {
            "server": "https://jira.example.com",
            "auth": {
                "type": "bearer",
                "token": "your-api-token"
            }
        }

        Basic auth (JIRA Cloud):
        {
            "server": "https://myorg.atlassian.net",
            "auth": {
                "type": "basic",
                "email": "user@example.com",
                "token": "api-token"
            }
        }
        """
        server = data.get("server", "")
        auth_type = AUTH_TYPE_BEARER
        email = None

        # Handle both nested auth and flat token
        if "auth" in data and isinstance(data["auth"], dict):
            auth = data["auth"]
            token = auth.get("token", "")
            auth_type = auth.get("type", AUTH_TYPE_BEARER)
            # Normalize legacy "token" type to "bearer"
            if auth_type == "token":
                auth_type = AUTH_TYPE_BEARER
            email = auth.get("email")
        else:
            token = data.get("token", "")

        return cls(
            server=server,
            token=token,
            auth_type=auth_type,
            email=email,
            extras=data,
        )


def _resolve_instance(
    config_data: dict[str, Any],
    instance: str | None,
) -> dict[str, Any]:
    """Resolve a named instance from multi-instance config.

    If the config has an "instances" key, look up the named instance
    (or the default). Otherwise return config_data unchanged.
    """
    instances = config_data.get("instances")
    if not instances or not isinstance(instances, dict):
        return config_data

    # Determine which instance to use
    if instance is None:
        instance = config_data.get("default")
        if instance is None:
            # If there's only one instance, use it
            if len(instances) == 1:
                instance = next(iter(instances))
            else:
                raise ConfigError(
                    f"Multiple instances configured but no --instance specified and no 'default' set. "
                    f"Available instances: {', '.join(sorted(instances.keys()))}"
                )

    if instance not in instances:
        raise ConfigError(
            f"Instance '{instance}' not found in config. "
            f"Available instances: {', '.join(sorted(instances.keys()))}"
        )

    return instances[instance]


def load_config(
    config_path: Path | str | None = None,
    server_override: str | None = None,
    token_override: str | None = None,
    instance: str | None = None,
) -> JiraConfig:
    """
    Load configuration from file and environment variables.

    Priority (highest to lowest):
    1. Explicit overrides passed to this function
    2. Environment variables (JIRA_SERVER, JIRA_TOKEN)
    3. Config file (with optional named instance)

    Args:
        config_path: Optional path to config file. Defaults to ~/.jira-tool.json
        server_override: Optional server URL override
        token_override: Optional token override
        instance: Optional named instance from multi-instance config

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

    # Resolve named instance if multi-instance config
    config_data = _resolve_instance(config_data, instance)

    # Apply environment variable overrides — but NOT when a named instance
    # was explicitly selected, since the instance config should take precedence.
    if not instance:
        env_server = os.environ.get("JIRA_SERVER")
        env_token = os.environ.get("JIRA_TOKEN")

        if env_server:
            config_data["server"] = env_server
        if env_token:
            config_data["token"] = env_token
            # Also update nested auth token so from_dict picks it up
            if "auth" in config_data and isinstance(config_data["auth"], dict):
                config_data["auth"]["token"] = env_token

    # Apply explicit overrides
    if server_override:
        config_data["server"] = server_override
    if token_override:
        config_data["token"] = token_override
        if "auth" in config_data and isinstance(config_data["auth"], dict):
            config_data["auth"]["token"] = token_override

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
    sample = {
        "instances": {
            "onprem": {
                "server": "https://jira.example.com",
                "auth": {"type": "bearer", "token": "your-bearer-token-here"},
            },
            "cloud": {
                "server": "https://myorg.atlassian.net",
                "auth": {
                    "type": "basic",
                    "email": "user@example.com",
                    "token": "your-api-token-here",
                },
            },
        },
        "default": "onprem",
    }
    return json.dumps(sample, indent=2)
