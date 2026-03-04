"""Configuration loading for Maloo tool."""

import os
from dataclasses import dataclass

from llm_tool_common.config import load_env_files

load_env_files("maloo-tool")


@dataclass
class MalooConfig:
    """Maloo tool configuration."""

    base_url: str
    username: str
    password: str

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


def load_config(
    user_override: str | None = None,
    password_override: str | None = None,
) -> MalooConfig:
    """Load Maloo configuration from environment."""
    base_url = os.environ.get(
        "MALOO_URL", "https://testing.whamcloud.com"
    )
    username = user_override or os.environ.get("MALOO_USER", "")
    password = password_override or os.environ.get("MALOO_PASS", "")

    return MalooConfig(
        base_url=base_url, username=username, password=password
    )
