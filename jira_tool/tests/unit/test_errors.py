"""Unit tests for error handling."""

import pytest

from jira_tool.errors import (
    AuthError,
    ConfigError,
    ErrorCode,
    ExitCode,
    InvalidInputError,
    JiraToolError,
    NetworkError,
    NotFoundError,
)


class TestExitCode:
    """Tests for ExitCode enum."""

    def test_exit_codes_have_expected_values(self):
        assert ExitCode.SUCCESS == 0
        assert ExitCode.GENERAL_ERROR == 1
        assert ExitCode.AUTH_ERROR == 2
        assert ExitCode.NOT_FOUND == 3
        assert ExitCode.INVALID_INPUT == 4
        assert ExitCode.NETWORK_ERROR == 5


class TestErrorCode:
    """Tests for ErrorCode constants."""

    def test_error_codes_are_strings(self):
        assert isinstance(ErrorCode.AUTH_FAILED, str)
        assert isinstance(ErrorCode.ISSUE_NOT_FOUND, str)
        assert isinstance(ErrorCode.INVALID_INPUT, str)
        assert isinstance(ErrorCode.CONNECTION_ERROR, str)


class TestJiraToolError:
    """Tests for JiraToolError base class."""

    def test_basic_error_creation(self):
        error = JiraToolError(
            code="TEST_ERROR",
            message="Test error message",
        )
        assert error.code == "TEST_ERROR"
        assert error.message == "Test error message"
        assert error.http_status is None
        assert error.details == {}
        assert error.exit_code == ExitCode.GENERAL_ERROR

    def test_error_with_all_fields(self):
        error = JiraToolError(
            code="TEST_ERROR",
            message="Test error message",
            http_status=400,
            details={"field": "value"},
            exit_code=ExitCode.INVALID_INPUT,
        )
        assert error.code == "TEST_ERROR"
        assert error.http_status == 400
        assert error.details == {"field": "value"}
        assert error.exit_code == ExitCode.INVALID_INPUT

    def test_to_dict_minimal(self):
        error = JiraToolError(code="TEST", message="Test")
        result = error.to_dict()
        assert result == {"code": "TEST", "message": "Test"}

    def test_to_dict_with_http_status(self):
        error = JiraToolError(code="TEST", message="Test", http_status=404)
        result = error.to_dict()
        assert result["http_status"] == 404

    def test_to_dict_with_details(self):
        error = JiraToolError(code="TEST", message="Test", details={"key": "value"})
        result = error.to_dict()
        assert result["details"] == {"key": "value"}

    def test_error_is_exception(self):
        error = JiraToolError(code="TEST", message="Test message")
        assert str(error) == "Test message"
        with pytest.raises(JiraToolError):
            raise error


class TestAuthError:
    """Tests for AuthError."""

    def test_auth_error_defaults(self):
        error = AuthError(message="Authentication failed")
        assert error.code == ErrorCode.AUTH_FAILED
        assert error.exit_code == ExitCode.AUTH_ERROR

    def test_auth_error_with_http_status(self):
        error = AuthError(message="Auth failed", http_status=401)
        assert error.http_status == 401


class TestNotFoundError:
    """Tests for NotFoundError."""

    def test_not_found_error(self):
        error = NotFoundError(
            code=ErrorCode.ISSUE_NOT_FOUND,
            message="Issue not found",
        )
        assert error.code == ErrorCode.ISSUE_NOT_FOUND
        assert error.http_status == 404
        assert error.exit_code == ExitCode.NOT_FOUND


class TestInvalidInputError:
    """Tests for InvalidInputError."""

    def test_invalid_input_error(self):
        error = InvalidInputError(
            code=ErrorCode.INVALID_JQL,
            message="Invalid JQL query",
            http_status=400,
        )
        assert error.code == ErrorCode.INVALID_JQL
        assert error.exit_code == ExitCode.INVALID_INPUT


class TestNetworkError:
    """Tests for NetworkError."""

    def test_network_error(self):
        error = NetworkError(
            code=ErrorCode.CONNECTION_ERROR,
            message="Connection failed",
        )
        assert error.code == ErrorCode.CONNECTION_ERROR
        assert error.http_status is None
        assert error.exit_code == ExitCode.NETWORK_ERROR


class TestConfigError:
    """Tests for ConfigError."""

    def test_config_error(self):
        error = ConfigError(message="Config not found")
        assert error.code == ErrorCode.CONFIG_ERROR
        assert error.exit_code == ExitCode.GENERAL_ERROR
