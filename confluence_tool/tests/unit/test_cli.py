"""Unit tests for the Confluence CLI."""

import json

import pytest
import responses
from click.testing import CliRunner

from confluence_tool.cli import main
from confluence_tool.commands._helpers import extract_page_id

pytestmark = pytest.mark.unit


@pytest.fixture
def config_file(tmp_path):
    cfg = tmp_path / "conf.json"
    cfg.write_text(json.dumps({
        "instances": {
            "cloud": {"server": "https://x.atlassian.net/wiki",
                      "auth": {"type": "basic", "email": "u@e.com", "token": "tok"}},
            "whamcloud": {"server": "https://wiki.whamcloud.com", "auth": {"type": "none"}},
        },
        "default": "cloud",
    }))
    return str(cfg)


class TestExtractPageId:
    def test_bare_id(self):
        assert extract_page_id("3727963991") == "3727963991"

    def test_cloud_url(self):
        url = "https://x.atlassian.net/wiki/spaces/EXA/pages/3727963991/Lustre+Maintainers"
        assert extract_page_id(url) == "3727963991"

    def test_server_url(self):
        assert extract_page_id("https://wiki.whamcloud.com/spaces/PUB/pages/98533935/Patch+Status") == "98533935"


class TestCli:
    def test_config_sample(self):
        result = CliRunner().invoke(main, ["config-sample"])
        assert result.exit_code == 0
        assert json.loads(result.output)["default"] == "cloud"

    def test_describe(self):
        result = CliRunner().invoke(main, ["describe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "confluence"
        assert {c["name"] for c in data["commands"]} >= {"search", "get", "spaces"}

    @responses.activate
    def test_search_cloud(self, config_file):
        responses.add(
            responses.GET, "https://x.atlassian.net/wiki/rest/api/content/search",
            json={"results": [{"id": "1", "type": "page", "title": "Nodemap",
                               "space": {"key": "EXA"}, "_links": {"webui": "/x/1"}}],
                  "totalSize": 1},
            status=200,
        )
        result = CliRunner().invoke(main, ["--config", config_file, "search", "nodemap"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["results"][0]["title"] == "Nodemap"
        assert data["results"][0]["url"] == "https://x.atlassian.net/wiki/x/1"

    @responses.activate
    def test_get_via_whamcloud_sends_no_auth(self, config_file):
        def _assert_no_auth(request):
            assert "Authorization" not in request.headers
            return (200, {}, json.dumps({
                "id": "98533935", "title": "Patch Status", "type": "page",
                "space": {"key": "PUB"}, "version": {"number": 3},
                "body": {"view": {"value": "<p>hello</p>"}}, "_links": {"webui": "/x/2"},
            }))
        responses.add_callback(
            responses.GET, "https://wiki.whamcloud.com/rest/api/content/98533935",
            callback=_assert_no_auth, content_type="application/json",
        )
        result = CliRunner().invoke(
            main, ["--config", config_file, "-I", "whamcloud", "get", "98533935"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["text"] == "hello"
        assert data["version"] == 3
