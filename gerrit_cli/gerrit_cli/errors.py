"""Error codes and exceptions for gerrit-cli tool.

This module re-exports base classes from llm_tool_common and adds
Gerrit-specific error codes and exception types.
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

# GerritToolError is an alias for ToolError for type consistency
GerritToolError = ToolError


class ErrorCode(BaseErrorCode):
    """Gerrit-specific error codes extending the base codes."""

    # Gerrit-specific resource errors
    CHANGE_NOT_FOUND = "CHANGE_NOT_FOUND"
    THREAD_NOT_FOUND = "THREAD_NOT_FOUND"
    COMMENT_NOT_FOUND = "COMMENT_NOT_FOUND"
    PATCH_NOT_FOUND = "PATCH_NOT_FOUND"
    SERIES_NOT_FOUND = "SERIES_NOT_FOUND"

    # Gerrit-specific input errors
    INVALID_URL = "INVALID_URL"
    THREAD_INDEX_OUT_OF_RANGE = "THREAD_INDEX_OUT_OF_RANGE"

    # Session errors
    NO_ACTIVE_SESSION = "NO_ACTIVE_SESSION"
    SESSION_ERROR = "SESSION_ERROR"

    # Git errors
    GIT_ERROR = "GIT_ERROR"
    REBASE_ERROR = "REBASE_ERROR"

    # Upload refusals and failures
    CHANGE_ID_MISMATCH = "CHANGE_ID_MISMATCH"
    NO_CHANGE_ID = "NO_CHANGE_ID"
    MULTIPLE_CHANGE_IDS = "MULTIPLE_CHANGE_IDS"
    INVALID_CHANGE_ID = "INVALID_CHANGE_ID"
    AMBIGUOUS_CHANGE = "AMBIGUOUS_CHANGE"
    CHANGE_CLOSED = "CHANGE_CLOSED"
    NO_NEW_CHANGES = "NO_NEW_CHANGES"
    TOO_MANY_COMMITS = "TOO_MANY_COMMITS"
    MULTIPLE_COMMITS = "MULTIPLE_COMMITS"
    COMMITTER_NOT_REGISTERED = "COMMITTER_NOT_REGISTERED"
    PUSH_REJECTED = "PUSH_REJECTED"
    STALE_PATCHSET = "STALE_PATCHSET"


# Re-export all base classes
__all__ = [
    "ExitCode",
    "ErrorCode",
    "GerritToolError",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
]

