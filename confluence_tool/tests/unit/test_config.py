"""Unit tests for Confluence configuration handling."""

import json

import pytest

from confluence_tool.config import (
    AUTH_TYPE_BASIC,
    AUTH_TYPE_BEARER,
    AUTH_TYPE_NONE,
    ConfluenceConfig,
    _resolve_instance,
    create_sample_config,
    load_config,
)
from confluence_tool.errors import ConfigError

pytestmark = pytest.mark.unit


class TestConfluenceConfig:
    def test_basic_creation(self):
        c = ConfluenceConfig(server="https://x.atlassian.net/wiki", token="t", auth_type=AUTH_TYPE_BASIC, email="a@b.c")
        assert c.is_cloud is True
        assert c.auth_type == AUTH_TYPE_BASIC

    def test_trailing_slash_stripped(self):
        c = ConfluenceConfig(server="https://wiki.whamcloud.com/", auth_type=AUTH_TYPE_NONE)
        assert c.server == "https://wiki.whamcloud.com"

    def test_anonymous_needs_no_token(self):
        c = ConfluenceConfig(server="https://wiki.whamcloud.com", auth_type=AUTH_TYPE_NONE)
        assert c.is_anonymous is True
        assert c.get_auth_header() is None

    def test_basic_requires_email(self):
        with pytest.raises(ConfigError):
            ConfluenceConfig(server="https://x.atlassian.net/wiki", token="t", auth_type=AUTH_TYPE_BASIC)

    def test_bearer_requires_token(self):
        with pytest.raises(ConfigError):
            ConfluenceConfig(server="https://wiki.example.com", auth_type=AUTH_TYPE_BEARER)

    def test_basic_auth_header(self):
        c = ConfluenceConfig(server="https://x.atlassian.net/wiki", token="tok", auth_type=AUTH_TYPE_BASIC, email="u@e.com")
        assert c.get_auth_header().startswith("Basic ")

    def test_invalid_auth_type(self):
        with pytest.raises(ConfigError):
            ConfluenceConfig(server="https://x", token="t", auth_type="oops")

    def test_from_dict_nested_none(self):
        c = ConfluenceConfig.from_dict({"server": "https://wiki.whamcloud.com", "auth": {"type": "none"}})
        assert c.is_anonymous is True


class TestResolveInstance:
    def test_default_instance(self):
        data = {"instances": {"cloud": {"server": "c"}, "whamcloud": {"server": "w"}}, "default": "cloud"}
        assert _resolve_instance(data, None)["server"] == "c"

    def test_named_instance(self):
        data = {"instances": {"cloud": {"server": "c"}, "whamcloud": {"server": "w"}}, "default": "cloud"}
        assert _resolve_instance(data, "whamcloud")["server"] == "w"

    def test_unknown_instance(self):
        data = {"instances": {"cloud": {"server": "c"}}, "default": "cloud"}
        with pytest.raises(ConfigError):
            _resolve_instance(data, "nope")


class TestLoadConfig:
    def test_load_whamcloud_anonymous(self, tmp_path):
        cfg = tmp_path / "conf.json"
        cfg.write_text(create_sample_config())
        c = load_config(config_path=cfg, instance="whamcloud")
        assert c.server == "https://wiki.whamcloud.com"
        assert c.is_anonymous is True

    def test_missing_server_errors(self, tmp_path):
        cfg = tmp_path / "conf.json"
        cfg.write_text(json.dumps({"instances": {"x": {"token": "t"}}, "default": "x"}))
        with pytest.raises(ConfigError):
            load_config(config_path=cfg)


def test_sample_config_is_valid_json():
    data = json.loads(create_sample_config())
    assert data["default"] == "cloud"
    assert data["instances"]["whamcloud"]["auth"]["type"] == "none"
