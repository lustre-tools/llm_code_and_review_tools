"""Unit tests for configuration handling."""

import json

import pytest

from jira_tool.config import (
    JiraConfig,
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

    def test_from_dict_flat_format(self):
        """Should parse flat config format."""
        data = {
            "server": "https://jira.example.com",
            "token": "my-token",
        }
        config = JiraConfig.from_dict(data)
        assert config.server == "https://jira.example.com"
        assert config.token == "my-token"

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


class TestCreateSampleConfig:
    """Tests for create_sample_config function."""

    def test_returns_valid_json(self):
        """Sample config should be valid JSON."""
        sample = create_sample_config()
        data = json.loads(sample)
        assert "server" in data
        assert "auth" in data

    def test_has_placeholder_values(self):
        """Sample config should have placeholder values."""
        sample = create_sample_config()
        data = json.loads(sample)
        assert "example.com" in data["server"]
        assert "token" in data["auth"]["type"]
