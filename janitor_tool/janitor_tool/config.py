"""Configuration for Janitor tool."""

import os
from dataclasses import dataclass


@dataclass
class JanitorConfig:
    """Janitor tool configuration."""

    base_url: str
    gerrit_url: str = "https://review.whamcloud.com"

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.gerrit_url = self.gerrit_url.rstrip("/")


def load_config() -> JanitorConfig:
    """Load Janitor configuration from environment."""
    base_url = os.environ.get(
        "JANITOR_URL", "https://testing.whamcloud.com/gerrit-janitor"
    )
    gerrit_url = (
        os.environ.get("GERRIT_URL") or "https://review.whamcloud.com"
    )
    return JanitorConfig(base_url=base_url, gerrit_url=gerrit_url)
