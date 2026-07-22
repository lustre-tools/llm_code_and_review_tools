"""Error codes and exceptions for the Confluence tool.

This module re-exports base classes from llm_tool_common and adds
Confluence-specific error codes and exception types.
"""

from llm_tool_common import (
    AuthError,
    ConfigError,
    ErrorCode as BaseErrorCode,
    ExitCode,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    ToolError,
)

# ConfluenceToolError is an alias for ToolError for consistency with jira_tool
ConfluenceToolError = ToolError


class ErrorCode(BaseErrorCode):
    """Confluence-specific error codes extending the base codes."""

    # Confluence-specific resource errors
    PAGE_NOT_FOUND = "PAGE_NOT_FOUND"
    SPACE_NOT_FOUND = "SPACE_NOT_FOUND"

    # Confluence-specific input errors
    INVALID_CQL = "INVALID_CQL"

    # Confluence-specific server errors
    RATE_LIMITED = "RATE_LIMITED"


__all__ = [
    "ExitCode",
    "ErrorCode",
    "ConfluenceToolError",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
]
