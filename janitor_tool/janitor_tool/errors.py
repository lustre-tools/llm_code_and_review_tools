"""Error codes for Janitor tool."""

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
    """Janitor-specific error codes."""

    BUILD_NOT_FOUND = "BUILD_NOT_FOUND"
    TEST_NOT_FOUND = "TEST_NOT_FOUND"
    LOG_NOT_FOUND = "LOG_NOT_FOUND"
    CHANGE_NOT_FOUND = "CHANGE_NOT_FOUND"


#: Exit status of the Janitor codes that mean what a base code does.
EXIT_CODES = {
    ErrorCode.BUILD_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.TEST_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.LOG_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.CHANGE_NOT_FOUND: ExitCode.NOT_FOUND,
}


__all__ = [
    "EXIT_CODES",
    "ExitCode",
    "ErrorCode",
    "ToolError",
    "AuthError",
    "NotFoundError",
    "InvalidInputError",
    "NetworkError",
    "ConfigError",
]
