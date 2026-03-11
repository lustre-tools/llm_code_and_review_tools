"""Configuration for Janitor tool."""

import os
from dataclasses import dataclass


@dataclass
class JanitorConfig:
    """Janitor tool configuration."""

    base_url: str

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")


def load_config() -> JanitorConfig:
    """Load Janitor configuration from environment."""
    base_url = os.environ.get(
        "JANITOR_URL", "https://testing.whamcloud.com/gerrit-janitor"
    )
    return JanitorConfig(base_url=base_url)
