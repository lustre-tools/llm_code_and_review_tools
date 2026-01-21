"""Error codes and exceptions for JIRA tool."""

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Standard exit codes for the JIRA tool."""

    SUCCESS = 0
    GENERAL_ERROR = 1
    AUTH_ERROR = 2
    NOT_FOUND = 3
    INVALID_INPUT = 4
    NETWORK_ERROR = 5


class ErrorCode:
    """Standard error codes for structured error responses."""

    # Authentication errors
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_MISSING = "AUTH_MISSING"

    # Resource errors
    ISSUE_NOT_FOUND = "ISSUE_NOT_FOUND"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    TRANSITION_NOT_FOUND = "TRANSITION_NOT_FOUND"

    # Input errors
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_JQL = "INVALID_JQL"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_TRANSITION = "INVALID_TRANSITION"

    # Network errors
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"

    # Server errors
    SERVER_ERROR = "SERVER_ERROR"
    RATE_LIMITED = "RATE_LIMITED"

    # Config errors
    CONFIG_ERROR = "CONFIG_ERROR"
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"


class JiraToolError(Exception):
    """Base exception for JIRA tool errors."""

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
        result = {
            "code": self.code,
            "message": self.message,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.details:
            result["details"] = self.details
        return result


class AuthError(JiraToolError):
    """Authentication-related errors."""

    def __init__(self, message: str, http_status: int | None = None, details: dict | None = None):
        super().__init__(
            code=ErrorCode.AUTH_FAILED,
            message=message,
            http_status=http_status,
            details=details,
            exit_code=ExitCode.AUTH_ERROR,
        )


class NotFoundError(JiraToolError):
    """Resource not found errors."""

    def __init__(self, code: str, message: str, http_status: int = 404, details: dict | None = None):
        super().__init__(
            code=code,
            message=message,
            http_status=http_status,
            details=details,
            exit_code=ExitCode.NOT_FOUND,
        )


class InvalidInputError(JiraToolError):
    """Invalid input errors."""

    def __init__(self, code: str, message: str, http_status: int | None = None, details: dict | None = None):
        super().__init__(
            code=code,
            message=message,
            http_status=http_status,
            details=details,
            exit_code=ExitCode.INVALID_INPUT,
        )


class NetworkError(JiraToolError):
    """Network-related errors."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(
            code=code,
            message=message,
            http_status=None,
            details=details,
            exit_code=ExitCode.NETWORK_ERROR,
        )


class ConfigError(JiraToolError):
    """Configuration-related errors."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            code=ErrorCode.CONFIG_ERROR,
            message=message,
            http_status=None,
            details=details,
            exit_code=ExitCode.GENERAL_ERROR,
        )
