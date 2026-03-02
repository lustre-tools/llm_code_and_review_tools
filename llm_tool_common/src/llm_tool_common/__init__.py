"""Shared utilities for LLM-focused CLI tools.

This package provides common functionality shared between tools like
jira_tool and gerrit_cli, including:
- Response envelope helpers for standardized JSON output
- Base error classes and exit codes
"""

from .envelope import (
    success_response,
    error_response,
    error_response_from_dict,
    format_json,
)
from .errors import (
    ExitCode,
    ErrorCode,
    ToolError,
    AuthError,
    NotFoundError,
    InvalidInputError,
    NetworkError,
    ConfigError,
)
from .describe import (
    Argument,
    Command,
    ToolDescription,
)
from .decorators import handle_errors

__all__ = [
    # Envelope functions
    "success_response",
    "error_response",
    "error_response_from_dict",
    "format_json",
    # Error classes
    "ExitCode",
    "ErrorCode",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
    # Describe helpers
    "Argument",
    "Command",
    "ToolDescription",
    # Decorators
    "handle_errors",
]

