"""Unit tests for JIRA client."""

import pytest
import responses

from jira_tool.client import JiraClient
from jira_tool.config import JiraConfig
from jira_tool.errors import (
    AuthError,
    ErrorCode,
    InvalidInputError,
    JiraToolError,
    NetworkError,
    NotFoundError,
)


@pytest.fixture
def config():
    """Create test config."""
    return JiraConfig(
        server="https://jira.example.com",
        token="test-token",
    )


@pytest.fixture
def client(config):
    """Create test client."""
    return JiraClient(config)


class TestClientInit:
    """Tests for JiraClient initialization."""

    def test_client_creation(self, config):
        """Should create client with config."""
        client = JiraClient(config)
        assert client.config == config

    def test_client_default_timeout(self, config):
        """Should have default timeout."""
        client = JiraClient(config)
        assert client.timeout == 30

    def test_client_custom_timeout(self, config):
        """Should accept custom timeout."""
        client = JiraClient(config, timeout=60)
        assert client.timeout == 60

    def test_client_auth_header(self, config):
        """Should set Bearer auth header."""
        client = JiraClient(config)
        assert "Authorization" in client._session.headers
        assert client._session.headers["Authorization"] == "Bearer test-token"


class TestBuildUrl:
    """Tests for URL building."""

    def test_build_url(self, client):
        """Should build correct URL."""
        url = client._build_url("issue/PROJ-123")
        assert url == "https://jira.example.com/rest/api/2/issue/PROJ-123"

    def test_build_url_strips_leading_slash(self, client):
        """Should handle leading slash."""
        url = client._build_url("/issue/PROJ-123")
        assert url == "https://jira.example.com/rest/api/2/issue/PROJ-123"


class TestGetIssue:
    """Tests for get_issue method."""

    @responses.activate
    def test_get_issue_success(self, client):
        """Should return issue data on success."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {
                    "summary": "Test issue",
                    "status": {"name": "Open"},
                },
            },
            status=200,
        )

        result = client.get_issue("PROJ-123")
        assert result["key"] == "PROJ-123"
        assert result["fields"]["summary"] == "Test issue"

    @responses.activate
    def test_get_issue_with_fields(self, client):
        """Should pass fields parameter."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {}},
            status=200,
        )

        client.get_issue("PROJ-123", fields=["summary", "status"])

        assert "fields=summary%2Cstatus" in responses.calls[0].request.url

    @responses.activate
    def test_get_issue_not_found(self, client):
        """Should raise NotFoundError for 404."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-999",
            json={"errorMessages": ["Issue does not exist"]},
            status=404,
        )

        with pytest.raises(NotFoundError) as exc_info:
            client.get_issue("PROJ-999")
        assert "Issue does not exist" in str(exc_info.value)

    @responses.activate
    def test_get_issue_auth_failure(self, client):
        """Should raise AuthError for 401."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"errorMessages": ["Authentication failed"]},
            status=401,
        )

        with pytest.raises(AuthError):
            client.get_issue("PROJ-123")

    @responses.activate
    def test_get_issue_forbidden(self, client):
        """Should raise AuthError for 403."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"errorMessages": ["Permission denied"]},
            status=403,
        )

        with pytest.raises(AuthError):
            client.get_issue("PROJ-123")


class TestSearchIssues:
    """Tests for search_issues method."""

    @responses.activate
    def test_search_success(self, client):
        """Should return search results."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/search",
            json={
                "issues": [
                    {"key": "PROJ-1"},
                    {"key": "PROJ-2"},
                ],
                "total": 2,
                "startAt": 0,
                "maxResults": 50,
            },
            status=200,
        )

        result = client.search_issues("project = PROJ")
        assert len(result["issues"]) == 2
        assert result["total"] == 2

    @responses.activate
    def test_search_with_pagination(self, client):
        """Should pass pagination parameters."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": [], "total": 100, "startAt": 20, "maxResults": 10},
            status=200,
        )

        client.search_issues("project = PROJ", start_at=20, max_results=10)

        request_body = responses.calls[0].request.body
        assert b'"startAt": 20' in request_body
        assert b'"maxResults": 10' in request_body

    @responses.activate
    def test_search_max_results_capped(self, client):
        """Should cap max_results at 1000."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": [], "total": 0},
            status=200,
        )

        client.search_issues("project = PROJ", max_results=5000)

        request_body = responses.calls[0].request.body
        assert b'"maxResults": 1000' in request_body

    @responses.activate
    def test_search_invalid_jql(self, client):
        """Should raise InvalidInputError for bad JQL."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/search",
            json={"errorMessages": ["Error in JQL syntax"]},
            status=400,
        )

        with pytest.raises(InvalidInputError):
            client.search_issues("invalid jql ((")


class TestGetComments:
    """Tests for get_comments method."""

    @responses.activate
    def test_get_comments_success(self, client):
        """Should return comments."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "comments": [
                    {"id": "1", "body": "First comment"},
                    {"id": "2", "body": "Second comment"},
                ],
                "total": 2,
            },
            status=200,
        )

        result = client.get_comments("PROJ-123")
        assert len(result["comments"]) == 2
        assert result["total"] == 2

    @responses.activate
    def test_get_comments_with_pagination(self, client):
        """Should pass pagination params."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={"comments": [], "total": 10},
            status=200,
        )

        client.get_comments("PROJ-123", start_at=5, max_results=3)

        url = responses.calls[0].request.url
        assert "startAt=5" in url
        assert "maxResults=3" in url


class TestAddComment:
    """Tests for add_comment method."""

    @responses.activate
    def test_add_comment_success(self, client):
        """Should create and return comment."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "id": "12345",
                "body": "New comment",
                "author": {"displayName": "Test User"},
            },
            status=201,
        )

        result = client.add_comment("PROJ-123", "New comment")
        assert result["id"] == "12345"
        assert result["body"] == "New comment"

    @responses.activate
    def test_add_comment_request_body(self, client):
        """Should send correct request body."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={"id": "1"},
            status=201,
        )

        client.add_comment("PROJ-123", "Test body")

        request_body = responses.calls[0].request.body
        assert b'"body": "Test body"' in request_body


class TestGetTransitions:
    """Tests for get_transitions method."""

    @responses.activate
    def test_get_transitions_success(self, client):
        """Should return available transitions."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/transitions",
            json={
                "transitions": [
                    {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
                    {"id": "21", "name": "Resolve", "to": {"name": "Resolved"}},
                ],
            },
            status=200,
        )

        result = client.get_transitions("PROJ-123")
        assert len(result["transitions"]) == 2


class TestDoTransition:
    """Tests for do_transition method."""

    @responses.activate
    def test_transition_success(self, client):
        """Should perform transition."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/transitions",
            status=204,
        )

        result = client.do_transition("PROJ-123", "11")
        assert result == {}

    @responses.activate
    def test_transition_with_comment(self, client):
        """Should include comment in transition."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/transitions",
            status=204,
        )

        client.do_transition("PROJ-123", "11", comment="Transitioning")

        request_body = responses.calls[0].request.body
        assert b'"comment"' in request_body
        assert b'"Transitioning"' in request_body


class TestCreateIssue:
    """Tests for create_issue method."""

    @responses.activate
    def test_create_issue_success(self, client):
        """Should create and return issue."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue",
            json={
                "id": "10001",
                "key": "PROJ-124",
                "self": "https://jira.example.com/rest/api/2/issue/10001",
            },
            status=201,
        )

        result = client.create_issue(
            project_key="PROJ",
            issue_type="Bug",
            summary="Test bug",
            description="Bug description",
        )

        assert result["key"] == "PROJ-124"

    @responses.activate
    def test_create_issue_request_body(self, client):
        """Should send correct request body."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue",
            json={"id": "1", "key": "PROJ-1"},
            status=201,
        )

        client.create_issue(
            project_key="PROJ",
            issue_type="Task",
            summary="New task",
        )

        request_body = responses.calls[0].request.body
        assert b'"project": {"key": "PROJ"}' in request_body
        assert b'"issuetype": {"name": "Task"}' in request_body
        assert b'"summary": "New task"' in request_body


class TestNetworkErrors:
    """Tests for network error handling."""

    @responses.activate
    def test_timeout_error(self, client):
        """Should raise NetworkError on timeout."""
        import requests.exceptions

        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            body=requests.exceptions.Timeout("Connection timed out"),
        )

        with pytest.raises(NetworkError) as exc_info:
            client.get_issue("PROJ-123")
        assert exc_info.value.code == ErrorCode.TIMEOUT

    @responses.activate
    def test_connection_error(self, client):
        """Should raise NetworkError on connection failure."""
        import requests.exceptions

        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )

        with pytest.raises(NetworkError) as exc_info:
            client.get_issue("PROJ-123")
        assert exc_info.value.code == ErrorCode.CONNECTION_ERROR


class TestServerErrors:
    """Tests for server error handling."""

    @responses.activate
    def test_500_error(self, client):
        """Should raise JiraToolError for 500."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"errorMessages": ["Internal server error"]},
            status=500,
        )

        with pytest.raises(JiraToolError) as exc_info:
            client.get_issue("PROJ-123")
        assert exc_info.value.code == ErrorCode.SERVER_ERROR
        assert exc_info.value.http_status == 500

    @responses.activate
    def test_rate_limited(self, client):
        """Should raise specific error for 429."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=429,
        )

        with pytest.raises(JiraToolError) as exc_info:
            client.get_issue("PROJ-123")
        assert exc_info.value.code == ErrorCode.RATE_LIMITED


class TestGetServerInfo:
    """Tests for get_server_info method."""

    @responses.activate
    def test_server_info_success(self, client):
        """Should return server info."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/serverInfo",
            json={
                "baseUrl": "https://jira.example.com",
                "version": "8.0.0",
                "serverTitle": "Example JIRA",
            },
            status=200,
        )

        result = client.get_server_info()
        assert result["version"] == "8.0.0"
        assert result["serverTitle"] == "Example JIRA"


class TestGetAttachment:
    """Tests for get_attachment method."""

    @responses.activate
    def test_get_attachment_success(self, client):
        """Should return attachment metadata."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/12345",
            json={
                "id": "12345",
                "filename": "test.txt",
                "size": 1024,
                "mimeType": "text/plain",
                "content": "https://jira.example.com/secure/attachment/12345/test.txt",
                "author": {"displayName": "Test User"},
                "created": "2024-01-15T10:00:00.000+0000",
            },
            status=200,
        )

        result = client.get_attachment("12345")
        assert result["id"] == "12345"
        assert result["filename"] == "test.txt"
        assert result["size"] == 1024

    @responses.activate
    def test_get_attachment_not_found(self, client):
        """Should raise NotFoundError for missing attachment."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/99999",
            json={"errorMessages": ["Attachment not found"]},
            status=404,
        )

        with pytest.raises(NotFoundError):
            client.get_attachment("99999")


class TestGetAttachmentContent:
    """Tests for get_attachment_content method."""

    @responses.activate
    def test_get_content_success(self, client):
        """Should download attachment content."""
        # Mock metadata endpoint
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/12345",
            json={
                "id": "12345",
                "filename": "test.txt",
                "size": 13,
                "mimeType": "text/plain",
                "content": "https://jira.example.com/secure/attachment/12345/test.txt",
            },
            status=200,
        )
        # Mock content download
        responses.add(
            responses.GET,
            "https://jira.example.com/secure/attachment/12345/test.txt",
            body=b"Hello, World!",
            status=200,
        )

        content, metadata = client.get_attachment_content("12345")
        assert content == b"Hello, World!"
        assert metadata["filename"] == "test.txt"

    @responses.activate
    def test_get_content_too_large(self, client):
        """Should raise error when attachment exceeds max size."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/12345",
            json={
                "id": "12345",
                "filename": "large.bin",
                "size": 10 * 1024 * 1024,  # 10MB
                "content": "https://jira.example.com/secure/attachment/12345/large.bin",
            },
            status=200,
        )

        with pytest.raises(InvalidInputError) as exc_info:
            client.get_attachment_content("12345", max_size=1024 * 1024)  # 1MB limit
        assert "too large" in str(exc_info.value)

    @responses.activate
    def test_get_content_no_limit(self, client):
        """Should allow downloading large files with max_size=0."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/12345",
            json={
                "id": "12345",
                "filename": "large.bin",
                "size": 10 * 1024 * 1024,
                "content": "https://jira.example.com/secure/attachment/12345/large.bin",
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://jira.example.com/secure/attachment/12345/large.bin",
            body=b"x" * 100,  # Just return some content
            status=200,
        )

        content, metadata = client.get_attachment_content("12345", max_size=0)
        assert len(content) == 100
