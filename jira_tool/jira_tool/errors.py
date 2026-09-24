"""Error codes and exceptions for JIRA tool.

This module re-exports base classes from llm_tool_common and adds
JIRA-specific error codes and exception types.
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

# JiraToolError is an alias for ToolError for backwards compatibility
JiraToolError = ToolError


class ErrorCode(BaseErrorCode):
    """JIRA-specific error codes extending the base codes."""

    # JIRA-specific resource errors
    ISSUE_NOT_FOUND = "ISSUE_NOT_FOUND"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    TRANSITION_NOT_FOUND = "TRANSITION_NOT_FOUND"
    USER_NOT_FOUND = "USER_NOT_FOUND"

    # JIRA-specific input errors
    INVALID_JQL = "INVALID_JQL"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    USER_INACTIVE = "USER_INACTIVE"

    # JIRA-specific server errors
    RATE_LIMITED = "RATE_LIMITED"


# Re-export all base classes
__all__ = [
    "ExitCode",
    "ErrorCode",
    "JiraToolError",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
]
