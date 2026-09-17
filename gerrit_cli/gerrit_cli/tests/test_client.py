"""Tests for the client module."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from gerrit_cli.client import GerritCommentsClient


class TestGerritCommentsClient:
    """Tests for GerritCommentsClient."""

    def test_parse_gerrit_url_with_project(self):
        """Test parsing URL with project path."""
        url = "https://review.whamcloud.com/c/fs/lustre-release/+/61965"
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        assert base_url == "https://review.whamcloud.com"
        assert change_number == 61965

    def test_parse_gerrit_url_with_patchset(self):
        """Test parsing URL with patchset number."""
        url = "https://review.whamcloud.com/c/fs/lustre-release/+/61965/5"
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        assert base_url == "https://review.whamcloud.com"
        assert change_number == 61965

    def test_parse_gerrit_url_simple(self):
        """Test parsing simple URL format."""
        url = "https://review.whamcloud.com/61965"
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        assert base_url == "https://review.whamcloud.com"
        assert change_number == 61965

    def test_parse_gerrit_url_simple_with_patchset(self):
        """Test parsing simple URL with patchset."""
        url = "https://review.whamcloud.com/61965/3"
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        assert base_url == "https://review.whamcloud.com"
        assert change_number == 61965

    def test_parse_gerrit_url_nested_project(self):
        """Test parsing URL with deeply nested project."""
        url = "https://gerrit.example.com/c/some/deep/project/path/+/12345"
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        assert base_url == "https://gerrit.example.com"
        assert change_number == 12345

    def test_parse_gerrit_url_invalid(self):
        """Test parsing invalid URL."""
        url = "https://example.com/not-a-gerrit-url"
        with pytest.raises(ValueError, match="Could not parse Gerrit URL"):
            GerritCommentsClient.parse_gerrit_url(url)

    def test_parse_gerrit_url_change_number_only(self):
        """Test parsing just a change number with explicit default URL."""
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(
            "12345",
            default_base_url="https://review.example.com"
        )

        assert base_url == "https://review.example.com"
        assert change_number == 12345

    def test_parse_gerrit_url_change_number_uses_env(self):
        """Test parsing change number falls back to GERRIT_URL env var."""
        with patch.dict('os.environ', {'GERRIT_URL': 'https://env.gerrit.com'}):
            # Reload the module constant
            import gerrit_cli.client as client_module
            old_default = client_module.DEFAULT_GERRIT_URL
            client_module.DEFAULT_GERRIT_URL = 'https://env.gerrit.com'

            try:
                base_url, change_number = GerritCommentsClient.parse_gerrit_url("67890")
                assert base_url == "https://env.gerrit.com"
                assert change_number == 67890
            finally:
                client_module.DEFAULT_GERRIT_URL = old_default

    def test_parse_gerrit_url_change_number_no_default(self):
        """Test parsing change number with no default URL raises error."""
        import gerrit_cli.client as client_module
        old_default = client_module.DEFAULT_GERRIT_URL
        client_module.DEFAULT_GERRIT_URL = None

        try:
            with pytest.raises(ValueError, match="Could not parse Gerrit URL"):
                GerritCommentsClient.parse_gerrit_url("12345")
        finally:
            client_module.DEFAULT_GERRIT_URL = old_default

    def test_parse_gerrit_url_http(self):
        """Test parsing HTTP URL."""
        url = "http://gerrit.local/c/project/+/999"
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        assert base_url == "http://gerrit.local"
        assert change_number == 999

    def test_format_change_url(self):
        """Test formatting change URL."""
        client = GerritCommentsClient.__new__(GerritCommentsClient)
        client.url = "https://review.example.com"

        url = client.format_change_url("my/project", 12345)

        assert url == "https://review.example.com/c/my/project/+/12345"


class TestChangeIdResolution:
    """Tests for resolving a Change-Id to a change number."""

    CHANGE_ID = "If2706506135264f501c6cbc6243ed449f9792605"

    def setup_method(self):
        import gerrit_cli.client as client_module
        client_module._CHANGE_ID_CACHE.clear()

    @contextmanager
    def _server(self, matches):
        """Stand in for the Gerrit resolve_change_id() builds a client for."""
        calls = []

        def fake_init(self, url=None, username=None, password=None):
            self.url = url or "https://review.example.com"

        def fake_search(self, query, limit=25, start=0, options=None):
            calls.append(query)
            return matches

        with patch.object(GerritCommentsClient, "__init__", fake_init), \
                patch.object(GerritCommentsClient, "search_changes", fake_search):
            yield calls

    def test_extract_change_id_bare(self):
        assert (
            GerritCommentsClient.extract_change_id(self.CHANGE_ID)
            == self.CHANGE_ID
        )

    def test_extract_change_id_triplet(self):
        triplet = f"fs%2Flustre-release~master~{self.CHANGE_ID}"
        assert (
            GerritCommentsClient.extract_change_id(triplet) == self.CHANGE_ID
        )

    def test_extract_change_id_query_url(self):
        url = f"https://review.example.com/q/{self.CHANGE_ID}"
        assert GerritCommentsClient.extract_change_id(url) == self.CHANGE_ID

    def test_extract_change_id_rejects_other_words(self):
        assert GerritCommentsClient.extract_change_id("Invalid") is None
        assert GerritCommentsClient.extract_change_id("61965") is None

    def test_parse_gerrit_url_resolves_change_id(self):
        with self._server([{"_number": 61965, "status": "NEW"}]) as calls:
            base_url, change_number = GerritCommentsClient.parse_gerrit_url(
                self.CHANGE_ID,
                default_base_url="https://review.example.com",
            )

        assert base_url == "https://review.example.com"
        assert change_number == 61965
        assert calls == [f"change:{self.CHANGE_ID}"]

    def test_parse_gerrit_url_resolves_triplet(self):
        triplet = f"fs%2Flustre-release~master~{self.CHANGE_ID}"
        with self._server([{"_number": 61965, "status": "NEW"}]):
            _, change_number = GerritCommentsClient.parse_gerrit_url(
                triplet, default_base_url="https://review.example.com"
            )

        assert change_number == 61965

    def test_parse_gerrit_url_change_id_url_keeps_its_server(self):
        url = f"https://other.gerrit.com/q/{self.CHANGE_ID}"
        with self._server([{"_number": 61965, "status": "NEW"}]):
            base_url, change_number = GerritCommentsClient.parse_gerrit_url(
                url, default_base_url="https://review.example.com"
            )

        assert base_url == "https://other.gerrit.com"
        assert change_number == 61965

    def test_resolve_change_id_caches(self):
        with self._server([{"_number": 61965, "status": "NEW"}]) as calls:
            for _ in range(3):
                GerritCommentsClient.resolve_change_id(
                    self.CHANGE_ID, "https://review.example.com"
                )

        assert len(calls) == 1

    def test_resolve_change_id_no_match(self):
        with self._server([]):
            with pytest.raises(ValueError, match="No change on"):
                GerritCommentsClient.resolve_change_id(
                    self.CHANGE_ID, "https://review.example.com"
                )

    def test_resolve_change_id_picks_the_open_one(self):
        with self._server([
            {"_number": 61965, "status": "MERGED", "branch": "master"},
            {"_number": 62001, "status": "NEW", "branch": "b2_15"},
        ]):
            assert GerritCommentsClient.resolve_change_id(
                self.CHANGE_ID, "https://review.example.com"
            ) == 62001

    def test_resolve_change_id_ambiguous(self):
        with self._server([
            {"_number": 61965, "status": "NEW", "branch": "master"},
            {"_number": 62001, "status": "NEW", "branch": "b2_15"},
        ]):
            with pytest.raises(ValueError, match="matches 2 changes"):
                GerritCommentsClient.resolve_change_id(
                    self.CHANGE_ID, "https://review.example.com"
                )


class TestGerritCommentsClientWithMocks:
    """Tests for GerritCommentsClient with mocked API calls."""

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_init_with_defaults(self, mock_auth, mock_api):
        """Test initialization with default credentials."""
        client = GerritCommentsClient()

        mock_auth.assert_called_once()
        mock_api.assert_called_once()
        assert client.url == "https://review.whamcloud.com"

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_init_with_custom_credentials(self, mock_auth, mock_api):
        """Test initialization with custom credentials."""
        client = GerritCommentsClient(
            url="https://custom.gerrit.com",
            username="testuser",
            password="testpass",
        )

        mock_auth.assert_called_once_with("testuser", "testpass")
        assert client.url == "https://custom.gerrit.com"

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_get_change_detail(self, mock_auth, mock_api):
        """Test getting change details."""
        mock_rest = MagicMock()
        mock_rest.get.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
        }
        mock_api.return_value = mock_rest

        client = GerritCommentsClient()
        result = client.get_change_detail(123)

        mock_rest.get.assert_called()
        assert "o=ALL_REVISIONS" in mock_rest.get.call_args[0][0]
        assert result["project"] == "test"

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_get_comments(self, mock_auth, mock_api):
        """Test getting comments."""
        mock_rest = MagicMock()
        mock_rest.get.return_value = {
            "test.py": [
                {"id": "abc", "message": "Test comment"}
            ]
        }
        mock_api.return_value = mock_rest

        client = GerritCommentsClient()
        result = client.get_comments(123)

        mock_rest.get.assert_called_with("/changes/123/comments")
        assert "test.py" in result

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_get_revision_for_patchset(self, mock_auth, mock_api):
        """Test getting revision ID for patch set."""
        mock_rest = MagicMock()
        mock_rest.get.return_value = {
            "revisions": {
                "abc123": {"_number": 1},
                "def456": {"_number": 2},
                "ghi789": {"_number": 3},
            }
        }
        mock_api.return_value = mock_rest

        client = GerritCommentsClient()
        result = client.get_revision_for_patchset(123, 2)

        assert result == "def456"

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_get_revision_for_patchset_not_found(self, mock_auth, mock_api):
        """Test getting revision ID for nonexistent patch set."""
        mock_rest = MagicMock()
        mock_rest.get.return_value = {
            "revisions": {
                "abc123": {"_number": 1},
            }
        }
        mock_api.return_value = mock_rest

        client = GerritCommentsClient()
        result = client.get_revision_for_patchset(123, 99)

        assert result is None

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_post_review(self, mock_auth, mock_api):
        """Test posting a review."""
        mock_rest = MagicMock()
        mock_rest.post.return_value = {"labels": {}}
        mock_api.return_value = mock_rest

        client = GerritCommentsClient()
        client.post_review(
            change_number=123,
            revision_id="abc123",
            message="LGTM",
            labels={"Code-Review": 1},
        )

        mock_rest.post.assert_called_once()
        call_args = mock_rest.post.call_args
        assert "/changes/123/revisions/abc123/review" in call_args[0][0]
        assert call_args[1]["json"]["message"] == "LGTM"
        assert call_args[1]["json"]["labels"]["Code-Review"] == 1

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_reply_to_comment(self, mock_auth, mock_api):
        """Test replying to a comment."""
        mock_rest = MagicMock()
        mock_rest.post.return_value = {}
        mock_api.return_value = mock_rest

        client = GerritCommentsClient()
        client.reply_to_comment(
            change_number=123,
            revision_id="abc123",
            file_path="test.py",
            comment_id="comment456",
            message="Acknowledged",
            line=42,
            mark_resolved=True,
        )

        mock_rest.post.assert_called_once()
        call_args = mock_rest.post.call_args
        comments = call_args[1]["json"]["comments"]
        assert "test.py" in comments
        assert comments["test.py"][0]["in_reply_to"] == "comment456"
        assert comments["test.py"][0]["message"] == "Acknowledged"
        assert comments["test.py"][0]["unresolved"] is False

    @patch("gerrit_cli.client.GerritRestAPI")
    @patch("gerrit_cli.client.HTTPBasicAuth")
    def test_mark_comment_done(self, mock_auth, mock_api):
        """Test marking a comment as done."""
        mock_rest = MagicMock()
        mock_rest.post.return_value = {}
        mock_api.return_value = mock_rest

        client = GerritCommentsClient()
        client.mark_comment_done(
            change_number=123,
            revision_id="abc123",
            file_path="test.py",
            comment_id="comment456",
            line=42,
        )

        mock_rest.post.assert_called_once()
        call_args = mock_rest.post.call_args
        comments = call_args[1]["json"]["comments"]
        assert comments["test.py"][0]["message"] == "Done"
        assert comments["test.py"][0]["unresolved"] is False
