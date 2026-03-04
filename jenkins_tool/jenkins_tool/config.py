"""Configuration loading for Jenkins tool."""

import os
from dataclasses import dataclass

from llm_tool_common.config import load_env_files

load_env_files("jenkins-tool")


@dataclass
class JenkinsConfig:
    """Jenkins tool configuration."""

    base_url: str
    user: str
    token: str

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.user or not self.token:
            raise ValueError(
                "Jenkins credentials required. Set JENKINS_USER and JENKINS_TOKEN "
                "environment variables, or create "
                "~/.config/jenkins-tool/.env with:\n"
                "  JENKINS_URL=https://build.whamcloud.com\n"
                "  JENKINS_USER=youruser\n"
                "  JENKINS_TOKEN=your-api-token"
            )


def load_config(
    url_override: str | None = None,
    user_override: str | None = None,
    token_override: str | None = None,
) -> JenkinsConfig:
    """Load Jenkins configuration from environment."""
    base_url = url_override or os.environ.get(
        "JENKINS_URL", "https://build.whamcloud.com"
    )
    user = user_override or os.environ.get("JENKINS_USER", "")
    token = token_override or os.environ.get("JENKINS_TOKEN", "")

    return JenkinsConfig(base_url=base_url, user=user, token=token)
