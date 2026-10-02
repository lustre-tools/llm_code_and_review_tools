"""Configuration loading for Maloo tool."""

import os
from dataclasses import dataclass, field

from llm_tool_common.config import load_env_files
from llm_tool_common.errors import ConfigError

# This runs at import, before a command can report anything, so a
# MALOO_TOOL_ENV_FILE naming no file is raised by load_config() instead.
try:
    load_env_files("maloo-tool")
    _ENV_FILE_ERROR: str | None = None
except FileNotFoundError as e:
    _ENV_FILE_ERROR = str(e)


@dataclass
class MalooConfig:
    """Maloo tool configuration."""

    base_url: str
    username: str
    password: str
    timeout: tuple[float, float] = field(default=(10.0, 60.0))

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.username or not self.password:
            raise ValueError(
                "Maloo credentials required. Set MALOO_USER and MALOO_PASS "
                "environment variables, or create "
                "~/.config/maloo-tool/.env with:\n"
                "  MALOO_USER=you@whamcloud.com\n"
                "  MALOO_PASS=yourpassword"
            )


def _parse_timeout(value: str) -> tuple[float, float]:
    """MALOO_TIMEOUT: "READ" or "CONNECT,READ", in seconds."""
    parts = [p.strip() for p in value.split(",")]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        nums = []
    if len(nums) not in (1, 2) or any(n <= 0 for n in nums):
        raise ConfigError(
            f"MALOO_TIMEOUT={value!r}: expected seconds as READ or "
            "CONNECT,READ, e.g. 120 or 10,120"
        )
    if len(nums) == 1:
        return (10.0, nums[0])
    return (nums[0], nums[1])


def load_config(
    user_override: str | None = None,
    password_override: str | None = None,
) -> MalooConfig:
    """Load Maloo configuration from environment."""
    if _ENV_FILE_ERROR:
        raise ConfigError(_ENV_FILE_ERROR)
    base_url = os.environ.get(
        "MALOO_URL", "https://testing.whamcloud.com"
    )
    username = user_override or os.environ.get("MALOO_USER", "")
    password = password_override or os.environ.get("MALOO_PASS", "")

    timeout = os.environ.get("MALOO_TIMEOUT", "").strip()

    return MalooConfig(
        base_url=base_url, username=username, password=password,
        timeout=_parse_timeout(timeout) if timeout else (10.0, 60.0),
    )
