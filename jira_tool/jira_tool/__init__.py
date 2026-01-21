"""JIRA tool - LLM-agent-focused CLI for JIRA REST API."""

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
)

__version__ = "0.1.0"
__all__ = [
    "JiraClient",
    "JiraConfig",
    "load_config",
    "success_response",
    "error_response",
    "format_json",
    "JiraToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
    "ExitCode",
    "ErrorCode",
]
