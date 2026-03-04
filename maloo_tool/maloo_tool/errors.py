"""Error codes and exceptions for Maloo tool.

This module re-exports base classes from llm_tool_common and adds
Maloo-specific error codes.
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


class ErrorCode(BaseErrorCode):
    """Maloo-specific error codes extending the base codes."""

    # Maloo-specific resource errors
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    TEST_SET_NOT_FOUND = "TEST_SET_NOT_FOUND"

    # Maloo-specific operation errors
    LINK_FAILED = "LINK_FAILED"
    RAISE_BUG_FAILED = "RAISE_BUG_FAILED"
    RETEST_FAILED = "RETEST_FAILED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    RESOLVE_FAILED = "RESOLVE_FAILED"
    MISSING_FILTER = "MISSING_FILTER"


# Re-export all base classes
__all__ = [
    "ExitCode",
    "ErrorCode",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
]
