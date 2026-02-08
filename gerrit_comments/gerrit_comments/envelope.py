"""Response envelope helpers for standardized JSON output.

This module provides thin wrappers around the shared llm_tool_common
envelope functions, pre-configured for the gerrit-comments tool.
"""

from typing import Any

from llm_tool_common import (
    error_response as _error_response,
    error_response_from_dict as _error_response_from_dict,
    format_json,
    success_response as _success_response,
)

# The tool name for metadata
TOOL_NAME = "gerrit-comments"

# Re-export format_json directly
__all__ = ["success_response", "error_response", "error_response_from_dict", "format_json"]


def success_response(
    data: Any,
    command: str,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a success response envelope.

    Args:
        data: The response data payload
        command: The command that was executed (e.g., "extract", "reply")
        next_actions: Optional list of suggested follow-up commands

    Returns:
        Standard success envelope dictionary
    """
    return _success_response(data, TOOL_NAME, command, next_actions=next_actions)


def error_response(error: Any, command: str) -> dict[str, Any]:
    """
    Create an error response envelope from a GerritToolError.

    Args:
        error: The error that occurred (must have to_dict() method)
        command: The command that was executed

    Returns:
        Standard error envelope dictionary
    """
    return _error_response(error, TOOL_NAME, command)


def error_response_from_dict(
    code: str,
    message: str,
    command: str,
    http_status: int | None = None,
    details: dict | None = None,
) -> dict[str, Any]:
    """
    Create an error response envelope from individual fields.

    Args:
        code: Error code string
        message: Human-readable error message
        command: The command that was executed
        http_status: Optional HTTP status code
        details: Optional additional error details

    Returns:
        Standard error envelope dictionary
    """
    return _error_response_from_dict(
        code, message, TOOL_NAME, command, http_status, details
    )

