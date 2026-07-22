"""Unit tests for the Confluence client."""

import pytest
import responses

from confluence_tool.client import ConfluenceClient, html_to_text
from confluence_tool.config import AUTH_TYPE_BASIC, AUTH_TYPE_NONE, ConfluenceConfig
from confluence_tool.errors import AuthError, NotFoundError

pytestmark = pytest.mark.unit


@pytest.fixture
def cloud_client():
    cfg = ConfluenceConfig(
        server="https://x.atlassian.net/wiki", token="tok",
        auth_type=AUTH_TYPE_BASIC, email="u@e.com",
    )
    return ConfluenceClient(cfg, max_retries=0)


@pytest.fixture
def anon_client():
    cfg = ConfluenceConfig(server="https://wiki.whamcloud.com", auth_type=AUTH_TYPE_NONE)
    return ConfluenceClient(cfg, max_retries=0)


class TestHtmlToText:
    def test_headings_and_paragraphs(self):
        out = html_to_text("<h1>Title</h1><p>Hello <b>world</b></p>")
        assert "# Title" in out
        assert "Hello world" in out

    def test_list_items(self):
        out = html_to_text("<ul><li>one</li><li>two</li></ul>")
        assert "- one" in out and "- two" in out

    def test_empty(self):
        assert html_to_text(None) == ""


class TestAuthHeader:
    def test_anonymous_sends_no_auth(self, anon_client):
        assert "Authorization" not in anon_client._session.headers

    def test_cloud_sends_basic(self, cloud_client):
        assert cloud_client._session.headers["Authorization"].startswith("Basic ")


class TestUrls:
    def test_base_url_uses_rest_api(self, cloud_client):
        assert cloud_client._build_url("content/search") == "https://x.atlassian.net/wiki/rest/api/content/search"

    def test_whamcloud_base_url(self, anon_client):
        assert anon_client._build_url("space") == "https://wiki.whamcloud.com/rest/api/space"


class TestRequests:
    @responses.activate
    def test_search(self, cloud_client):
        responses.add(
            responses.GET, "https://x.atlassian.net/wiki/rest/api/content/search",
            json={"results": [{"id": "1", "type": "page", "title": "T"}], "size": 1, "totalSize": 1},
            status=200,
        )
        out = cloud_client.search('text ~ "x"', limit=5)
        assert out["results"][0]["id"] == "1"

    @responses.activate
    def test_get_page(self, anon_client):
        responses.add(
            responses.GET, "https://wiki.whamcloud.com/rest/api/content/98533935",
            json={"id": "98533935", "title": "Patch Status", "type": "page"},
            status=200,
        )
        out = anon_client.get_page("98533935")
        assert out["title"] == "Patch Status"

    @responses.activate
    def test_404(self, cloud_client):
        responses.add(
            responses.GET, "https://x.atlassian.net/wiki/rest/api/content/999",
            json={"message": "No content found"}, status=404,
        )
        with pytest.raises(NotFoundError):
            cloud_client.get_page("999")

    @responses.activate
    def test_401(self, cloud_client):
        responses.add(
            responses.GET, "https://x.atlassian.net/wiki/rest/api/space",
            json={"message": "nope"}, status=401,
        )
        with pytest.raises(AuthError):
            cloud_client.list_spaces()
