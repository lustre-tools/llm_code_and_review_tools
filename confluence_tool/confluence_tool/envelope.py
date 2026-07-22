"""Response envelope helpers for standardized JSON output.

Thin wrappers around the shared llm_tool_common envelope functions,
pre-configured for the Confluence tool.
"""

from typing import Any

from llm_tool_common import (
    error_response as _error_response,
    error_response_from_dict as _error_response_from_dict,
    format_json,
    success_response as _success_response,
)

# The tool name for metadata
TOOL_NAME = "confluence"

__all__ = ["success_response", "error_response", "error_response_from_dict", "format_json"]


def success_response(
    data: Any,
    command: str,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a success response envelope."""
    return _success_response(data, TOOL_NAME, command, next_actions=next_actions)


def error_response(error: Any, command: str) -> dict[str, Any]:
    """Create an error response envelope from a ConfluenceToolError."""
    return _error_response(error, TOOL_NAME, command)


def error_response_from_dict(
    code: str,
    message: str,
    command: str,
    http_status: int | None = None,
    details: dict | None = None,
) -> dict[str, Any]:
    """Create an error response envelope from individual fields."""
    return _error_response_from_dict(
        code, message, TOOL_NAME, command, http_status, details
    )
