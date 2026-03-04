"""Unit tests for Maloo configuration loading."""

import os
from unittest.mock import patch

import pytest

from maloo_tool.config import MalooConfig, load_config


class TestMalooConfig:
    def test_creation(self):
        cfg = MalooConfig(
            base_url="https://testing.whamcloud.com",
            username="user",
            password="pass",
        )
        assert cfg.base_url == "https://testing.whamcloud.com"
        assert cfg.username == "user"
        assert cfg.password == "pass"

    def test_trailing_slash_stripped(self):
        cfg = MalooConfig(
            base_url="https://testing.whamcloud.com/",
            username="u", password="p",
        )
        assert cfg.base_url == "https://testing.whamcloud.com"

    def test_missing_username_raises(self):
        with pytest.raises(ValueError, match="credentials"):
            MalooConfig(base_url="https://x.com", username="", password="pass")

    def test_missing_password_raises(self):
        with pytest.raises(ValueError, match="credentials"):
            MalooConfig(base_url="https://x.com", username="user", password="")

    def test_both_missing_raises(self):
        with pytest.raises(ValueError, match="credentials"):
            MalooConfig(base_url="https://x.com", username="", password="")


class TestLoadConfig:
    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("MALOO_URL", "https://custom.example.com")
        monkeypatch.setenv("MALOO_USER", "testuser")
        monkeypatch.setenv("MALOO_PASS", "testpass")
        cfg = load_config()
        assert cfg.base_url == "https://custom.example.com"
        assert cfg.username == "testuser"
        assert cfg.password == "testpass"

    def test_default_url(self, monkeypatch):
        monkeypatch.delenv("MALOO_URL", raising=False)
        monkeypatch.setenv("MALOO_USER", "u")
        monkeypatch.setenv("MALOO_PASS", "p")
        cfg = load_config()
        assert cfg.base_url == "https://testing.whamcloud.com"

    def test_override_user(self, monkeypatch):
        monkeypatch.setenv("MALOO_USER", "env_user")
        monkeypatch.setenv("MALOO_PASS", "env_pass")
        cfg = load_config(user_override="override_user")
        assert cfg.username == "override_user"
        assert cfg.password == "env_pass"

    def test_override_password(self, monkeypatch):
        monkeypatch.setenv("MALOO_USER", "u")
        monkeypatch.setenv("MALOO_PASS", "env_pass")
        cfg = load_config(password_override="override_pass")
        assert cfg.password == "override_pass"

    def test_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("MALOO_USER", raising=False)
        monkeypatch.delenv("MALOO_PASS", raising=False)
        with pytest.raises(ValueError, match="credentials"):
            load_config()
