"""Unit tests for gerrit-comments response envelope wrappers."""

import json

from gerrit_comments.envelope import (
    TOOL_NAME,
    error_response,
    error_response_from_dict,
    format_json,
    success_response,
)
from gerrit_comments.errors import GerritToolError


class TestToolName:
    """Tests for tool name configuration."""

    def test_tool_name_is_gerrit_comments(self):
        """Tool name should be 'gerrit-comments'."""
        assert TOOL_NAME == "gerrit-comments"


class TestSuccessResponse:
    """Tests for success response wrapper."""

    def test_success_response_includes_tool_name(self):
        """Success response should include gerrit-comments tool name."""
        result = success_response({"key": "value"}, "extract")
        assert result["ok"] is True
        assert result["data"] == {"key": "value"}
        assert result["meta"]["tool"] == "gerrit-comments"
        assert result["meta"]["command"] == "extract"

    def test_success_response_with_list_data(self):
        """Success response should work with list data."""
        data = [{"id": 1}, {"id": 2}]
        result = success_response(data, "series-comments")
        assert result["data"] == data
        assert result["meta"]["tool"] == "gerrit-comments"


class TestErrorResponse:
    """Tests for error response wrapper."""

    def test_error_response_with_tool_error(self):
        """Error response should work with GerritToolError."""
        error = GerritToolError(
            code="CHANGE_NOT_FOUND",
            message="Change 12345 not found",
            http_status=404,
        )
        result = error_response(error, "extract")

        assert result["ok"] is False
        assert result["error"]["code"] == "CHANGE_NOT_FOUND"
        assert result["error"]["message"] == "Change 12345 not found"
        assert result["error"]["http_status"] == 404
        assert result["meta"]["tool"] == "gerrit-comments"
        assert result["meta"]["command"] == "extract"

    def test_error_response_with_details(self):
        """Error response should include details."""
        error = GerritToolError(
            code="INVALID_URL",
            message="Invalid URL",
            details={"url": "not-a-url"},
        )
        result = error_response(error, "review")
        assert result["error"]["details"] == {"url": "not-a-url"}


class TestErrorResponseFromDict:
    """Tests for error_response_from_dict wrapper."""

    def test_minimal_error(self):
        """Should work with just code and message."""
        result = error_response_from_dict(
            code="ERROR",
            message="An error",
            command="test",
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "ERROR"
        assert result["meta"]["tool"] == "gerrit-comments"

    def test_full_error(self):
        """Should include all optional fields."""
        result = error_response_from_dict(
            code="ERROR",
            message="An error",
            command="test",
            http_status=500,
            details={"extra": "info"},
        )
        assert result["error"]["http_status"] == 500
        assert result["error"]["details"] == {"extra": "info"}


class TestFormatJson:
    """Tests for format_json re-export."""

    def test_format_json_compact(self):
        """format_json should produce compact output by default."""
        envelope = {"ok": True, "data": {"key": "value"}}
        result = format_json(envelope, pretty=False)
        assert "\n" not in result
        assert json.loads(result) == envelope

    def test_format_json_pretty(self):
        """format_json with pretty=True should indent."""
        envelope = {"ok": True, "data": {"key": "value"}}
        result = format_json(envelope, pretty=True)
        assert "\n" in result
        assert json.loads(result) == envelope

