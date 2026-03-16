"""Unit tests for configuration handling."""

import base64
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from jira_tool.config import (
    AUTH_TYPE_BASIC,
    AUTH_TYPE_BEARER,
    JiraConfig,
    _load_env_file,
    _resolve_instance,
    create_sample_config,
    load_config,
)
from jira_tool.errors import ConfigError


class TestJiraConfig:
    """Tests for JiraConfig dataclass."""

    def test_basic_creation(self):
        """Should create config with required fields."""
        config = JiraConfig(
            server="https://jira.example.com",
            token="test-token",
        )
        assert config.server == "https://jira.example.com"
        assert config.token == "test-token"
        assert config.auth_type == AUTH_TYPE_BEARER

    def test_server_url_normalization(self):
        """Should strip trailing slash from server URL."""
        config = JiraConfig(
            server="https://jira.example.com/",
            token="test-token",
        )
        assert config.server == "https://jira.example.com"

    def test_multiple_trailing_slashes(self):
        """Should strip multiple trailing slashes."""
        config = JiraConfig(
            server="https://jira.example.com///",
            token="test-token",
        )
        assert config.server == "https://jira.example.com"

    def test_empty_server_raises_error(self):
        """Should raise ConfigError for empty server."""
        with pytest.raises(ConfigError) as exc_info:
            JiraConfig(server="", token="test-token")
        assert "Server URL is required" in str(exc_info.value)

    def test_empty_token_raises_error(self):
        """Should raise ConfigError for empty token."""
        with pytest.raises(ConfigError) as exc_info:
            JiraConfig(server="https://jira.example.com", token="")
        assert "API token is required" in str(exc_info.value)

    def test_invalid_auth_type_raises_error(self):
        """Should raise ConfigError for invalid auth type."""
        with pytest.raises(ConfigError) as exc_info:
            JiraConfig(
                server="https://jira.example.com",
                token="test-token",
                auth_type="oauth",
            )
        assert "Invalid auth type" in str(exc_info.value)

    def test_basic_auth_requires_email(self):
        """Should raise ConfigError when basic auth has no email."""
        with pytest.raises(ConfigError) as exc_info:
            JiraConfig(
                server="https://myorg.atlassian.net",
                token="api-token",
                auth_type="basic",
            )
        assert "Email is required" in str(exc_info.value)

    def test_basic_auth_with_email(self):
        """Should create config with basic auth."""
        config = JiraConfig(
            server="https://myorg.atlassian.net",
            token="api-token",
            auth_type="basic",
            email="user@example.com",
        )
        assert config.auth_type == AUTH_TYPE_BASIC
        assert config.email == "user@example.com"

    def test_get_auth_header_bearer(self):
        """Should return Bearer auth header."""
        config = JiraConfig(
            server="https://jira.example.com",
            token="test-token",
        )
        assert config.get_auth_header() == "Bearer test-token"

    def test_get_auth_header_basic(self):
        """Should return Basic auth header with base64 email:token."""
        config = JiraConfig(
            server="https://myorg.atlassian.net",
            token="api-token",
            auth_type="basic",
            email="user@example.com",
        )
        expected = base64.b64encode(b"user@example.com:api-token").decode()
        assert config.get_auth_header() == f"Basic {expected}"

    def test_from_dict_flat_format(self):
        """Should parse flat config format."""
        data = {
            "server": "https://jira.example.com",
            "token": "my-token",
        }
        config = JiraConfig.from_dict(data)
        assert config.server == "https://jira.example.com"
        assert config.token == "my-token"
        assert config.auth_type == AUTH_TYPE_BEARER

    def test_from_dict_nested_auth_format(self):
        """Should parse nested auth config format."""
        data = {
            "server": "https://jira.example.com",
            "auth": {
                "type": "token",
                "token": "my-token",
            },
        }
        config = JiraConfig.from_dict(data)
        assert config.token == "my-token"
        # "token" type normalizes to "bearer"
        assert config.auth_type == AUTH_TYPE_BEARER

    def test_from_dict_bearer_auth(self):
        """Should parse bearer auth config."""
        data = {
            "server": "https://jira.example.com",
            "auth": {
                "type": "bearer",
                "token": "my-bearer-token",
            },
        }
        config = JiraConfig.from_dict(data)
        assert config.token == "my-bearer-token"
        assert config.auth_type == AUTH_TYPE_BEARER

    def test_from_dict_basic_auth(self):
        """Should parse basic auth config for JIRA Cloud."""
        data = {
            "server": "https://myorg.atlassian.net",
            "auth": {
                "type": "basic",
                "email": "user@example.com",
                "token": "api-token",
            },
        }
        config = JiraConfig.from_dict(data)
        assert config.token == "api-token"
        assert config.auth_type == AUTH_TYPE_BASIC
        assert config.email == "user@example.com"


class TestResolveInstance:
    """Tests for _resolve_instance function."""

    def test_no_instances_returns_data_unchanged(self):
        """Non-multi-instance config should pass through."""
        data = {"server": "https://jira.example.com", "token": "tok"}
        assert _resolve_instance(data, None) is data

    def test_named_instance_resolved(self):
        """Should resolve named instance."""
        data = {
            "instances": {
                "lu": {"server": "https://jira.whamcloud.com", "auth": {"type": "bearer", "token": "tok1"}},
                "cloud": {"server": "https://myorg.atlassian.net", "auth": {"type": "basic", "email": "a@b.com", "token": "tok2"}},
            },
            "default": "lu",
        }
        result = _resolve_instance(data, "cloud")
        assert result["server"] == "https://myorg.atlassian.net"
        assert result["auth"]["type"] == "basic"

    def test_default_instance_used_when_none_specified(self):
        """Should use default when no instance specified."""
        data = {
            "instances": {
                "lu": {"server": "https://jira.whamcloud.com", "auth": {"type": "bearer", "token": "tok1"}},
                "cloud": {"server": "https://myorg.atlassian.net", "auth": {"type": "basic", "email": "a@b.com", "token": "tok2"}},
            },
            "default": "lu",
        }
        result = _resolve_instance(data, None)
        assert result["server"] == "https://jira.whamcloud.com"

    def test_single_instance_auto_selected(self):
        """Should auto-select when only one instance exists."""
        data = {
            "instances": {
                "only": {"server": "https://jira.example.com", "auth": {"type": "bearer", "token": "tok"}},
            },
        }
        result = _resolve_instance(data, None)
        assert result["server"] == "https://jira.example.com"

    def test_missing_instance_raises_error(self):
        """Should raise ConfigError for unknown instance name."""
        data = {
            "instances": {
                "lu": {"server": "https://jira.whamcloud.com", "auth": {"type": "bearer", "token": "tok"}},
            },
            "default": "lu",
        }
        with pytest.raises(ConfigError) as exc_info:
            _resolve_instance(data, "nonexistent")
        assert "nonexistent" in str(exc_info.value)
        assert "lu" in str(exc_info.value)

    def test_no_default_multiple_instances_raises_error(self):
        """Should raise ConfigError when multiple instances and no default."""
        data = {
            "instances": {
                "a": {"server": "https://a.com", "auth": {"type": "bearer", "token": "t"}},
                "b": {"server": "https://b.com", "auth": {"type": "bearer", "token": "t"}},
            },
        }
        with pytest.raises(ConfigError) as exc_info:
            _resolve_instance(data, None)
        assert "no --instance specified" in str(exc_info.value)


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_from_explicit_overrides(self, tmp_path):
        """Explicit overrides should take precedence."""
        # Create a config file with different values
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "server": "https://file-server.com",
                    "token": "file-token",
                }
            )
        )

        # Override should win
        config = load_config(
            config_path=config_file,
            server_override="https://override-server.com",
            token_override="override-token",
        )
        assert config.server == "https://override-server.com"
        assert config.token == "override-token"

    def test_load_from_env_vars(self, tmp_path, monkeypatch):
        """Environment variables should override config file."""
        # Create config file
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "server": "https://file-server.com",
                    "token": "file-token",
                }
            )
        )

        # Set env vars
        monkeypatch.setenv("JIRA_SERVER", "https://env-server.com")
        monkeypatch.setenv("JIRA_TOKEN", "env-token")

        config = load_config(config_path=config_file)
        assert config.server == "https://env-server.com"
        assert config.token == "env-token"

    def test_load_from_config_file(self, tmp_path, monkeypatch):
        """Should load from config file when no overrides."""
        # Clear any env vars
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "server": "https://file-server.com",
                    "token": "file-token",
                }
            )
        )

        config = load_config(config_path=config_file)
        assert config.server == "https://file-server.com"
        assert config.token == "file-token"

    def test_missing_config_and_env_raises_error(self, tmp_path, monkeypatch):
        """Should raise ConfigError when no config source available."""
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        nonexistent = tmp_path / "nonexistent.json"

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=nonexistent)
        assert "No configuration found" in str(exc_info.value)

    def test_missing_server_raises_error(self, tmp_path, monkeypatch):
        """Should raise ConfigError when server is missing."""
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"token": "my-token"}))

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=config_file)
        assert "Server URL not configured" in str(exc_info.value)

    def test_missing_token_raises_error(self, tmp_path, monkeypatch):
        """Should raise ConfigError when token is missing."""
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"server": "https://jira.example.com"}))

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=config_file)
        assert "API token not configured" in str(exc_info.value)

    def test_invalid_json_raises_error(self, tmp_path, monkeypatch):
        """Should raise ConfigError for invalid JSON."""
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        config_file = tmp_path / "config.json"
        config_file.write_text("not valid json {")

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=config_file)
        assert "Invalid JSON" in str(exc_info.value)

    def test_priority_order(self, tmp_path, monkeypatch):
        """Test full priority chain: explicit > env > file."""
        # Config file
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "server": "https://file.com",
                    "token": "file-token",
                }
            )
        )

        # Env vars (higher priority)
        monkeypatch.setenv("JIRA_SERVER", "https://env.com")
        monkeypatch.setenv("JIRA_TOKEN", "env-token")

        # Test env wins over file
        config = load_config(config_path=config_file)
        assert config.server == "https://env.com"

        # Test explicit wins over env
        config = load_config(
            config_path=config_file,
            server_override="https://explicit.com",
        )
        assert config.server == "https://explicit.com"
        assert config.token == "env-token"  # Not overridden

    def test_load_multi_instance_default(self, tmp_path, monkeypatch):
        """Should load default instance from multi-instance config."""
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "instances": {
                "lu": {
                    "server": "https://jira.whamcloud.com",
                    "auth": {"type": "bearer", "token": "bearer-tok"},
                },
                "cloud": {
                    "server": "https://myorg.atlassian.net",
                    "auth": {"type": "basic", "email": "user@example.com", "token": "cloud-tok"},
                },
            },
            "default": "lu",
        }))

        config = load_config(config_path=config_file)
        assert config.server == "https://jira.whamcloud.com"
        assert config.auth_type == AUTH_TYPE_BEARER
        assert config.token == "bearer-tok"

    def test_load_multi_instance_named(self, tmp_path, monkeypatch):
        """Should load named instance from multi-instance config."""
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "instances": {
                "lu": {
                    "server": "https://jira.whamcloud.com",
                    "auth": {"type": "bearer", "token": "bearer-tok"},
                },
                "cloud": {
                    "server": "https://myorg.atlassian.net",
                    "auth": {"type": "basic", "email": "user@example.com", "token": "cloud-tok"},
                },
            },
            "default": "lu",
        }))

        config = load_config(config_path=config_file, instance="cloud")
        assert config.server == "https://myorg.atlassian.net"
        assert config.auth_type == AUTH_TYPE_BASIC
        assert config.email == "user@example.com"
        assert config.token == "cloud-tok"

    def test_env_overrides_instance_server(self, tmp_path, monkeypatch):
        """Env vars should override even within a named instance."""
        monkeypatch.setenv("JIRA_SERVER", "https://env-override.com")
        monkeypatch.setenv("JIRA_TOKEN", "env-tok")

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "instances": {
                "lu": {
                    "server": "https://jira.whamcloud.com",
                    "auth": {"type": "bearer", "token": "bearer-tok"},
                },
            },
            "default": "lu",
        }))

        config = load_config(config_path=config_file)
        assert config.server == "https://env-override.com"
        assert config.token == "env-tok"


class TestCreateSampleConfig:
    """Tests for create_sample_config function."""

    def test_returns_valid_json(self):
        """Sample config should be valid JSON."""
        sample = create_sample_config()
        data = json.loads(sample)
        assert "instances" in data
        assert "default" in data

    def test_has_placeholder_values(self):
        """Sample config should have placeholder values."""
        sample = create_sample_config()
        data = json.loads(sample)
        assert "onprem" in data["instances"]
        assert "cloud" in data["instances"]
        assert data["instances"]["cloud"]["auth"]["type"] == "basic"
        assert data["instances"]["onprem"]["auth"]["type"] == "bearer"

    def test_has_both_auth_types(self):
        """Sample config should demonstrate both auth types."""
        sample = create_sample_config()
        data = json.loads(sample)
        assert data["instances"]["onprem"]["auth"]["type"] == "bearer"
        assert data["instances"]["cloud"]["auth"]["type"] == "basic"
        assert "email" in data["instances"]["cloud"]["auth"]


class TestLoadEnvFile:
    """Tests for _load_env_file function."""

    def test_loads_from_user_config_dir(self, tmp_path, monkeypatch):
        """Should load .env from ~/.config/jira-tool/.env."""
        # Clear any existing env vars
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        # Create fake user config directory
        user_config = tmp_path / ".config" / "jira-tool"
        user_config.mkdir(parents=True)
        env_file = user_config / ".env"
        env_file.write_text("JIRA_SERVER=https://user-config.example.com\nJIRA_TOKEN=user-token\n")

        # Patch Path.home() to return our temp directory
        with patch.object(Path, "home", return_value=tmp_path):
            _load_env_file()

        assert os.environ.get("JIRA_SERVER") == "https://user-config.example.com"
        assert os.environ.get("JIRA_TOKEN") == "user-token"

        # Cleanup
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

    def test_loads_from_cwd_env(self, tmp_path, monkeypatch):
        """Should load .env from current directory."""
        # Clear any existing env vars
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        # Create .env in "current directory"
        env_file = tmp_path / ".env"
        env_file.write_text("JIRA_SERVER=https://cwd.example.com\nJIRA_TOKEN=cwd-token\n")

        # Change to temp directory and patch home to avoid user config
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        # Patch home to a non-existent path so user config isn't found
        fake_home = tmp_path / "fake_home"
        try:
            with patch.object(Path, "home", return_value=fake_home):
                _load_env_file()

            assert os.environ.get("JIRA_SERVER") == "https://cwd.example.com"
            assert os.environ.get("JIRA_TOKEN") == "cwd-token"
        finally:
            os.chdir(original_cwd)
            monkeypatch.delenv("JIRA_SERVER", raising=False)
            monkeypatch.delenv("JIRA_TOKEN", raising=False)

    def test_user_config_takes_priority_over_cwd(self, tmp_path, monkeypatch):
        """User config .env should take priority over cwd .env."""
        # Clear any existing env vars
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        # Create user config .env
        user_config = tmp_path / ".config" / "jira-tool"
        user_config.mkdir(parents=True)
        user_env = user_config / ".env"
        user_env.write_text("JIRA_SERVER=https://user.example.com\n")

        # Create cwd .env with different value
        cwd_env = tmp_path / ".env"
        cwd_env.write_text("JIRA_SERVER=https://cwd.example.com\n")

        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            with patch.object(Path, "home", return_value=tmp_path):
                _load_env_file()

            # User config should win
            assert os.environ.get("JIRA_SERVER") == "https://user.example.com"
        finally:
            os.chdir(original_cwd)
            monkeypatch.delenv("JIRA_SERVER", raising=False)

    def test_no_env_file_does_not_error(self, tmp_path, monkeypatch):
        """Should not error when no .env file exists."""
        # Clear any existing env vars
        monkeypatch.delenv("JIRA_SERVER", raising=False)

        # Point to empty directories
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        original_cwd = os.getcwd()
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        os.chdir(empty_dir)

        try:
            with patch.object(Path, "home", return_value=fake_home):
                # Should not raise
                _load_env_file()

            # Env var should not be set
            assert os.environ.get("JIRA_SERVER") is None
        finally:
            os.chdir(original_cwd)

    def test_env_file_integrates_with_load_config(self, tmp_path, monkeypatch):
        """Variables from .env should be available to load_config."""
        # Clear any existing env vars
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        # Create .env file
        env_file = tmp_path / ".env"
        env_file.write_text("JIRA_SERVER=https://dotenv.example.com\nJIRA_TOKEN=dotenv-token\n")

        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        # Patch home to avoid user config
        fake_home = tmp_path / "fake_home"
        try:
            with patch.object(Path, "home", return_value=fake_home):
                _load_env_file()

            # Now load_config should pick up the env vars
            config = load_config(config_path=tmp_path / "nonexistent.json")
            assert config.server == "https://dotenv.example.com"
            assert config.token == "dotenv-token"
        finally:
            os.chdir(original_cwd)
            monkeypatch.delenv("JIRA_SERVER", raising=False)
            monkeypatch.delenv("JIRA_TOKEN", raising=False)
