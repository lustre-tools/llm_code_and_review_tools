"""Unit tests for shared error classes."""

from llm_tool_common.errors import (
    AuthError,
    ConfigError,
    ErrorCode,
    ExitCode,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    ToolError,
)


class TestExitCode:
    """Tests for exit codes."""

    def test_exit_codes_have_expected_values(self):
        """Exit codes should match standard values."""
        assert ExitCode.SUCCESS == 0
        assert ExitCode.GENERAL_ERROR == 1
        assert ExitCode.AUTH_ERROR == 2
        assert ExitCode.NOT_FOUND == 3
        assert ExitCode.INVALID_INPUT == 4
        assert ExitCode.NETWORK_ERROR == 5


class TestErrorCode:
    """Tests for error codes."""

    def test_error_codes_are_strings(self):
        """All error codes should be strings."""
        assert isinstance(ErrorCode.AUTH_FAILED, str)
        assert isinstance(ErrorCode.NOT_FOUND, str)
        assert isinstance(ErrorCode.INVALID_INPUT, str)
        assert isinstance(ErrorCode.CONNECTION_ERROR, str)
        assert isinstance(ErrorCode.SERVER_ERROR, str)
        assert isinstance(ErrorCode.CONFIG_ERROR, str)


class TestToolError:
    """Tests for base ToolError."""

    def test_basic_error_creation(self):
        """Should create error with code and message."""
        error = ToolError(code="TEST", message="Test error")
        assert error.code == "TEST"
        assert error.message == "Test error"
        assert str(error) == "Test error"

    def test_error_with_all_fields(self):
        """Should store all optional fields."""
        error = ToolError(
            code="TEST",
            message="Test",
            http_status=500,
            details={"key": "value"},
            exit_code=ExitCode.AUTH_ERROR,
        )
        assert error.http_status == 500
        assert error.details == {"key": "value"}
        assert error.exit_code == ExitCode.AUTH_ERROR

    def test_to_dict_minimal(self):
        """to_dict should include only code and message when minimal."""
        error = ToolError(code="ERR", message="Error message")
        d = error.to_dict()
        assert d == {"code": "ERR", "message": "Error message"}

    def test_to_dict_with_http_status(self):
        """to_dict should include http_status when present."""
        error = ToolError(code="ERR", message="Error", http_status=404)
        d = error.to_dict()
        assert d["http_status"] == 404

    def test_to_dict_with_details(self):
        """to_dict should include details when present."""
        error = ToolError(code="ERR", message="Error", details={"field": "info"})
        d = error.to_dict()
        assert d["details"] == {"field": "info"}

    def test_error_is_exception(self):
        """ToolError should be an exception."""
        error = ToolError(code="ERR", message="Test")
        assert isinstance(error, Exception)


class TestAuthError:
    """Tests for AuthError."""

    def test_auth_error_defaults(self):
        """AuthError should have correct defaults."""
        error = AuthError(message="Auth failed")
        assert error.code == ErrorCode.AUTH_FAILED
        assert error.exit_code == ExitCode.AUTH_ERROR
        assert error.message == "Auth failed"

    def test_auth_error_with_http_status(self):
        """AuthError should accept http_status."""
        error = AuthError(message="Auth failed", http_status=401)
        assert error.http_status == 401


class TestNotFoundError:
    """Tests for NotFoundError."""

    def test_not_found_error(self):
        """NotFoundError should have correct defaults."""
        error = NotFoundError(code="RESOURCE_NOT_FOUND", message="Not found")
        assert error.code == "RESOURCE_NOT_FOUND"
        assert error.exit_code == ExitCode.NOT_FOUND
        assert error.http_status == 404  # default


class TestInvalidInputError:
    """Tests for InvalidInputError."""

    def test_invalid_input_error(self):
        """InvalidInputError should have correct defaults."""
        error = InvalidInputError(code="BAD_INPUT", message="Invalid")
        assert error.code == "BAD_INPUT"
        assert error.exit_code == ExitCode.INVALID_INPUT


class TestNetworkError:
    """Tests for NetworkError."""

    def test_network_error(self):
        """NetworkError should have correct defaults."""
        error = NetworkError(code="TIMEOUT", message="Connection timeout")
        assert error.code == "TIMEOUT"
        assert error.exit_code == ExitCode.NETWORK_ERROR
        assert error.http_status is None


class TestConfigError:
    """Tests for ConfigError."""

    def test_config_error(self):
        """ConfigError should have correct defaults."""
        error = ConfigError(message="Config missing")
        assert error.code == ErrorCode.CONFIG_ERROR
        assert error.exit_code == ExitCode.GENERAL_ERROR
        assert error.message == "Config missing"

    def test_config_error_with_details(self):
        """ConfigError should accept details."""
        error = ConfigError(message="Config error", details={"file": "config.json"})
        assert error.details == {"file": "config.json"}



class TestExitCodeFor:
    """The contract's exit status for an error code."""

    def test_base_codes(self):
        from llm_tool_common.errors import exit_code_for

        assert exit_code_for(ErrorCode.AUTH_FAILED) == ExitCode.AUTH_ERROR
        assert exit_code_for(ErrorCode.AUTH_MISSING) == ExitCode.AUTH_ERROR
        assert exit_code_for(ErrorCode.NOT_FOUND) == ExitCode.NOT_FOUND
        assert exit_code_for(ErrorCode.INVALID_INPUT) == ExitCode.INVALID_INPUT
        assert exit_code_for(ErrorCode.MISSING_REQUIRED_FIELD) == ExitCode.INVALID_INPUT
        assert exit_code_for(ErrorCode.CONNECTION_ERROR) == ExitCode.NETWORK_ERROR
        assert exit_code_for(ErrorCode.TIMEOUT) == ExitCode.NETWORK_ERROR

    def test_everything_else_is_a_general_error(self):
        from llm_tool_common.errors import exit_code_for

        for code in (ErrorCode.API_ERROR, ErrorCode.SERVER_ERROR,
                     ErrorCode.CONFIG_ERROR, ErrorCode.CONFIG_NOT_FOUND,
                     "RESOLVE_FAILED"):
            assert exit_code_for(code) == ExitCode.GENERAL_ERROR

    def test_a_tool_adds_its_own_codes(self):
        from llm_tool_common.errors import exit_code_for

        tool_codes = {"BUILD_NOT_FOUND": ExitCode.NOT_FOUND}
        assert exit_code_for("BUILD_NOT_FOUND", tool_codes) == ExitCode.NOT_FOUND
        assert exit_code_for("BUILD_NOT_FOUND") == ExitCode.GENERAL_ERROR
        assert exit_code_for(ErrorCode.TIMEOUT, tool_codes) == ExitCode.NETWORK_ERROR
