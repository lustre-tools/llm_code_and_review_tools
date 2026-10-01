"""Base error codes and exceptions for LLM-focused CLI tools.

This module provides standardized error handling that can be shared
between different tools. Tools can extend these base classes with
their own specific error codes.
"""

from collections.abc import Mapping
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Standard exit codes for LLM CLI tools.
    
    These follow common Unix conventions:
    - 0: Success
    - 1: General error
    - 2: Authentication error
    - 3: Resource not found
    - 4: Invalid input
    - 5: Network error
    """

    SUCCESS = 0
    GENERAL_ERROR = 1
    AUTH_ERROR = 2
    NOT_FOUND = 3
    INVALID_INPUT = 4
    NETWORK_ERROR = 5


class ErrorCode:
    """Base error codes for structured error responses.
    
    Tools can extend this class with their own specific codes.
    """

    # Authentication errors
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_MISSING = "AUTH_MISSING"

    # Generic resource errors
    NOT_FOUND = "NOT_FOUND"

    # Input errors
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"

    # Network errors
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"

    # Server errors
    SERVER_ERROR = "SERVER_ERROR"
    API_ERROR = "API_ERROR"

    # Config errors
    CONFIG_ERROR = "CONFIG_ERROR"
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"


#: The exit status the contract gives each base error code.  Every
#: other code -- API_ERROR, CONFIG_ERROR, a tool's own -- exits
#: GENERAL_ERROR unless the tool maps it.
EXIT_CODES: dict[str, ExitCode] = {
    ErrorCode.AUTH_FAILED: ExitCode.AUTH_ERROR,
    ErrorCode.AUTH_MISSING: ExitCode.AUTH_ERROR,
    ErrorCode.NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.INVALID_INPUT: ExitCode.INVALID_INPUT,
    ErrorCode.MISSING_REQUIRED_FIELD: ExitCode.INVALID_INPUT,
    ErrorCode.CONNECTION_ERROR: ExitCode.NETWORK_ERROR,
    ErrorCode.TIMEOUT: ExitCode.NETWORK_ERROR,
}


def exit_code_for(
    code: str, tool_codes: Mapping[str, ExitCode] | None = None
) -> ExitCode:
    """The exit status for an error code.

    ``tool_codes`` maps a tool's own codes, such as BUILD_NOT_FOUND, to
    the status they share with a base code.
    """
    if tool_codes and code in tool_codes:
        return tool_codes[code]
    return EXIT_CODES.get(code, ExitCode.GENERAL_ERROR)


class ToolError(Exception):
    """Base exception for LLM CLI tool errors.
    
    All tool-specific errors should inherit from this class.
    """

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
        exit_code: ExitCode = ExitCode.GENERAL_ERROR,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        """Convert error to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.details:
            result["details"] = self.details
        return result


class AuthError(ToolError):
    """Authentication-related errors."""

    def __init__(
        self,
        message: str,
        http_status: int | None = None,
        details: dict | None = None,
    ):
        super().__init__(
            code=ErrorCode.AUTH_FAILED,
            message=message,
            http_status=http_status,
            details=details,
            exit_code=ExitCode.AUTH_ERROR,
        )


class NotFoundError(ToolError):
    """Resource not found errors."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 404,
        details: dict | None = None,
    ):
        super().__init__(
            code=code,
            message=message,
            http_status=http_status,
            details=details,
            exit_code=ExitCode.NOT_FOUND,
        )


class InvalidInputError(ToolError):
    """Invalid input errors."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        details: dict | None = None,
    ):
        super().__init__(
            code=code,
            message=message,
            http_status=http_status,
            details=details,
            exit_code=ExitCode.INVALID_INPUT,
        )


class NetworkError(ToolError):
    """Network-related errors."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(
            code=code,
            message=message,
            http_status=None,
            details=details,
            exit_code=ExitCode.NETWORK_ERROR,
        )


class ConfigError(ToolError):
    """Configuration-related errors."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            code=ErrorCode.CONFIG_ERROR,
            message=message,
            http_status=None,
            details=details,
            exit_code=ExitCode.GENERAL_ERROR,
        )

