"""Configuration loading for the Confluence tool.

Mirrors jira_tool's multi-instance config (``~/.confluence-tool.json``),
with one addition: an ``auth_type`` of ``"none"`` for anonymous access.
The public whamcloud community wiki is reached anonymously so that only
public (open-source) content is ever visible.
"""

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path.home() / ".confluence-tool.json"

# Valid auth types
AUTH_TYPE_BEARER = "bearer"
AUTH_TYPE_BASIC = "basic"
AUTH_TYPE_NONE = "none"
VALID_AUTH_TYPES = {AUTH_TYPE_BEARER, AUTH_TYPE_BASIC, AUTH_TYPE_NONE}


def _load_env_file() -> None:
    """Load environment variables from a .env file in standard locations."""
    env_locations = [
        Path.home() / ".config" / "confluence-tool" / ".env",
        Path("/etc/confluence-tool/.env"),
        Path("/shared/support_files/.env"),
        Path(".env"),
    ]
    for env_path in env_locations:
        try:
            if env_path.exists():
                load_dotenv(env_path)
                return
        except OSError:
            continue  # host down, NFS stale, etc.


_load_env_file()


@dataclass
class ConfluenceConfig:
    """Confluence tool configuration for a single instance."""

    server: str
    token: str | None = None
    auth_type: str = AUTH_TYPE_BEARER
    email: str | None = None
    extras: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.server:
            raise ConfigError("Server URL is required")
        if self.auth_type not in VALID_AUTH_TYPES:
            raise ConfigError(
                f"Invalid auth type '{self.auth_type}'. Must be one of: "
                f"{', '.join(sorted(VALID_AUTH_TYPES))}"
            )
        if self.auth_type == AUTH_TYPE_BASIC and not self.email:
            raise ConfigError("Email is required for basic auth (Confluence Cloud)")
        if self.auth_type in (AUTH_TYPE_BASIC, AUTH_TYPE_BEARER) and not self.token:
            raise ConfigError(
                f"API token is required for '{self.auth_type}' auth"
            )
        # Normalize server URL (remove trailing slash)
        self.server = self.server.rstrip("/")

    @property
    def is_cloud(self) -> bool:
        """Detect a Confluence Cloud instance (atlassian.net)."""
        return ".atlassian.net" in self.server

    @property
    def is_anonymous(self) -> bool:
        """True when this instance sends no credentials (public, read-only)."""
        return self.auth_type == AUTH_TYPE_NONE

    def get_extra(self, key: str, default: Any = None) -> Any:
        """Get an extra config value (anything beyond server/token)."""
        if self.extras:
            return self.extras.get(key, default)
        return default

    def get_auth_header(self) -> str | None:
        """Return the Authorization header value, or None for anonymous."""
        if self.auth_type == AUTH_TYPE_NONE:
            return None
        if self.auth_type == AUTH_TYPE_BASIC:
            credentials = base64.b64encode(
                f"{self.email}:{self.token}".encode()
            ).decode()
            return f"Basic {credentials}"
        return f"Bearer {self.token}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConfluenceConfig":
        """Create config from a dictionary (flat token or nested auth)."""
        server = data.get("server", "")
        auth_type = AUTH_TYPE_BEARER
        email = None

        if "auth" in data and isinstance(data["auth"], dict):
            auth = data["auth"]
            token = auth.get("token")
            auth_type = auth.get("type", AUTH_TYPE_BEARER)
            if auth_type == "token":  # normalize legacy alias
                auth_type = AUTH_TYPE_BEARER
            email = auth.get("email")
        else:
            token = data.get("token")

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
    """Resolve a named instance from a multi-instance config."""
    instances = config_data.get("instances")
    if not instances or not isinstance(instances, dict):
        return config_data

    if instance is None:
        instance = config_data.get("default")
        if instance is None:
            if len(instances) == 1:
                instance = next(iter(instances))
            else:
                raise ConfigError(
                    "Multiple instances configured but no --instance specified and "
                    f"no 'default' set. Available instances: {', '.join(sorted(instances.keys()))}"
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
    instance: str | None = None,
) -> ConfluenceConfig:
    """Load configuration from the config file, resolving a named instance.

    Priority (highest to lowest):
    1. Explicit --server override
    2. Environment variables (CONFLUENCE_SERVER, CONFLUENCE_TOKEN) when no
       named instance is selected
    3. Config file (with optional named instance)
    """
    config_data: dict[str, Any] = {}

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

    config_data = _resolve_instance(config_data, instance)

    # Env overrides only when no explicit instance was selected.
    if not instance:
        env_server = os.environ.get("CONFLUENCE_SERVER")
        env_token = os.environ.get("CONFLUENCE_TOKEN")
        if env_server:
            config_data["server"] = env_server
        if env_token:
            config_data["token"] = env_token
            if "auth" in config_data and isinstance(config_data["auth"], dict):
                config_data["auth"]["token"] = env_token

    if server_override:
        config_data["server"] = server_override

    server = config_data.get("server", "")
    if not server:
        raise ConfigError(
            "No Confluence server configured. Set CONFLUENCE_SERVER or create a "
            f"config file at {DEFAULT_CONFIG_PATH} (run 'confluence config-sample').",
            details={"config_path": str(config_path)},
        )

    return ConfluenceConfig.from_dict(config_data)


def create_sample_config(path: Path | str | None = None) -> str:
    """Generate sample configuration file content.

    'cloud' is the default authenticated instance. 'whamcloud' is the public
    Lustre community wiki, reached anonymously (read-only) so only public,
    open-source articles are ever accessible.
    """
    sample = {
        "instances": {
            "cloud": {
                "server": "https://your-org.atlassian.net/wiki",
                "auth": {
                    "type": "basic",
                    "email": "user@example.com",
                    "token": "your-atlassian-api-token-here",
                },
            },
            "whamcloud": {
                "server": "https://wiki.whamcloud.com",
                "auth": {"type": "none"},
                "_note": "public Lustre community wiki — anonymous, read-only",
            },
        },
        "default": "cloud",
    }
    return json.dumps(sample, indent=2)
