"""Response envelope helpers for standardized JSON output.

This module provides functions to create consistent JSON response envelopes
for LLM-focused CLI tools. All tools using this module will have the same
output structure.
"""

import json
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def _get_timestamp() -> str:
    """Get current timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_meta(tool: str, command: str) -> dict[str, str]:
    """Build metadata block for response envelope.
    
    Args:
        tool: The tool name (e.g., "jira", "gerrit-comments")
        command: The command that was executed (e.g., "issue.get", "extract")
    """
    return {
        "tool": tool,
        "command": command,
        "timestamp": _get_timestamp(),
    }


def success_response(
    data: Any,
    tool: str,
    command: str,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a success response envelope.

    Args:
        data: The response data payload
        tool: The tool name (e.g., "jira", "gerrit-comments")
        command: The command that was executed (e.g., "issue.get", "extract")
        next_actions: Optional list of suggested follow-up commands.
            Helps LLMs discover what to do next without consulting docs.

    Returns:
        Standard success envelope dictionary
    """
    envelope: dict[str, Any] = {
        "ok": True,
        "data": data,
        "meta": _build_meta(tool, command),
    }
    if next_actions:
        envelope["next_actions"] = next_actions
    return envelope


@runtime_checkable
class HasToDict(Protocol):
    """Protocol for objects with a to_dict method."""
    def to_dict(self) -> dict[str, Any]: ...


def error_response(error: HasToDict, tool: str, command: str) -> dict[str, Any]:
    """
    Create an error response envelope from an error object.

    Args:
        error: An error object with a to_dict() method
        tool: The tool name (e.g., "jira", "gerrit-comments")
        command: The command that was executed

    Returns:
        Standard error envelope dictionary
    """
    return {
        "ok": False,
        "error": error.to_dict(),
        "meta": _build_meta(tool, command),
    }


def error_response_from_dict(
    code: str,
    message: str,
    tool: str,
    command: str,
    http_status: int | None = None,
    details: dict | None = None,
) -> dict[str, Any]:
    """
    Create an error response envelope from individual fields.

    Args:
        code: Error code string
        message: Human-readable error message
        tool: The tool name (e.g., "jira", "gerrit-comments")
        command: The command that was executed
        http_status: Optional HTTP status code
        details: Optional additional error details

    Returns:
        Standard error envelope dictionary
    """
    error_dict: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if http_status is not None:
        error_dict["http_status"] = http_status
    if details:
        error_dict["details"] = details

    return {
        "ok": False,
        "error": error_dict,
        "meta": _build_meta(tool, command),
    }


def format_json(envelope: dict[str, Any], pretty: bool = False) -> str:
    """
    Format envelope as JSON string.

    Args:
        envelope: The response envelope dictionary
        pretty: If True, format with indentation for human readability

    Returns:
        JSON string
    """
    if pretty:
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    return json.dumps(envelope, ensure_ascii=False)

