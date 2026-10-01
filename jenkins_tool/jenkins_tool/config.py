"""Configuration loading for Jenkins tool."""

import os
from dataclasses import dataclass

from llm_tool_common.config import load_env_files
from llm_tool_common.errors import ConfigError

# This runs at import, before a command can report anything, so a
# JENKINS_TOOL_ENV_FILE naming no file is raised by load_config() instead.
try:
    load_env_files("jenkins-tool")
    _ENV_FILE_ERROR: str | None = None
except FileNotFoundError as e:
    _ENV_FILE_ERROR = str(e)


CREDENTIAL_HINT = (
    "Set JENKINS_USER and JENKINS_TOKEN, or run "
    "`install.sh --configure --only jenkins`. The token comes from "
    "Jenkins: your name (top right) > Configure > API Token."
)


@dataclass
class JenkinsConfig:
    """Jenkins tool configuration.

    Credentials are optional.  A Jenkins that allows anonymous read --
    build.whamcloud.com does -- serves jobs, builds, console output and
    the CSRF crumb without them, which is everything this tool reads.
    They are needed only to change something: abort, kill, retrigger.
    """

    base_url: str
    user: str = ""
    token: str = ""

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    @property
    def authenticated(self) -> bool:
        """True when both halves of a credential are present.

        Half a credential is worse than none: Jenkins rejects a request
        carrying a username with no token outright, where the same
        request with no Authorization header at all would have been
        served anonymously.
        """
        return bool(self.user and self.token)


def load_config(
    url_override: str | None = None,
    user_override: str | None = None,
    token_override: str | None = None,
) -> JenkinsConfig:
    """Load Jenkins configuration from environment."""
    if _ENV_FILE_ERROR:
        raise ConfigError(_ENV_FILE_ERROR)
    base_url = url_override or os.environ.get(
        "JENKINS_URL", "https://build.whamcloud.com"
    )
    user = user_override or os.environ.get("JENKINS_USER", "")
    token = token_override or os.environ.get("JENKINS_TOKEN", "")

    return JenkinsConfig(base_url=base_url, user=user, token=token)
