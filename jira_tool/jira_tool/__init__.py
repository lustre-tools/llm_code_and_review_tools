"""JIRA tool - LLM-agent-focused CLI for JIRA REST API."""

import importlib.metadata

from .client import JiraClient
from .config import JiraConfig, load_config
from .envelope import error_response, format_json, success_response
from .errors import (
    AuthError,
    ConfigError,
    ErrorCode,
    ExitCode,
    InvalidInputError,
    JiraToolError,
    NetworkError,
    NotFoundError,
    ToolError,
)

try:
    __version__ = importlib.metadata.version("jira-tool")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "JiraClient",
    "JiraConfig",
    "load_config",
    "success_response",
    "error_response",
    "format_json",
    "JiraToolError",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
    "ExitCode",
    "ErrorCode",
]
