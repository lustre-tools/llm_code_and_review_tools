"""Unit tests for JIRA client."""

import json
from unittest.mock import patch

import pytest
import responses

from jira_tool.client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_RETRY_MAX_DELAY,
    JiraClient,
)
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

    def test_client_basic_auth_header(self):
        """Should set Basic auth header for JIRA Cloud."""
        import base64
        cloud_config = JiraConfig(
            server="https://myorg.atlassian.net",
            token="api-token",
            auth_type="basic",
            email="user@example.com",
        )
        client = JiraClient(cloud_config)
        expected = base64.b64encode(b"user@example.com:api-token").decode()
        assert client._session.headers["Authorization"] == f"Basic {expected}"


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


class TestAddCommentVisibility:
    """Tests for add_comment with visibility."""

    @responses.activate
    def test_add_comment_with_role_visibility(self, client):
        """Should include visibility in request body."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "id": "12345",
                "body": "Restricted comment",
                "visibility": {"type": "role", "value": "Developers"},
            },
            status=201,
        )

        result = client.add_comment(
            "PROJ-123",
            "Restricted comment",
            visibility={"type": "role", "value": "Developers"},
        )
        assert result["id"] == "12345"
        assert result["visibility"]["type"] == "role"
        assert result["visibility"]["value"] == "Developers"

        request_body = json.loads(responses.calls[0].request.body)
        assert request_body["visibility"] == {"type": "role", "value": "Developers"}

    @responses.activate
    def test_add_comment_with_group_visibility(self, client):
        """Should include group visibility in request body."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={"id": "12345", "body": "Group restricted"},
            status=201,
        )

        client.add_comment(
            "PROJ-123",
            "Group restricted",
            visibility={"type": "group", "value": "jira-users"},
        )

        request_body = json.loads(responses.calls[0].request.body)
        assert request_body["visibility"] == {"type": "group", "value": "jira-users"}

    @responses.activate
    def test_add_comment_without_visibility(self, client):
        """Should not include visibility key when not provided."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={"id": "1"},
            status=201,
        )

        client.add_comment("PROJ-123", "Public comment")

        request_body = json.loads(responses.calls[0].request.body)
        assert "visibility" not in request_body


class TestGetProjectRoles:
    """Tests for get_project_roles method."""

    @responses.activate
    def test_get_project_roles_success(self, client):
        """Should return role name to URL mapping."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/project/PROJ/role",
            json={
                "Administrators": "https://jira.example.com/rest/api/2/project/PROJ/role/10001",
                "Developers": "https://jira.example.com/rest/api/2/project/PROJ/role/10002",
                "Users": "https://jira.example.com/rest/api/2/project/PROJ/role/10003",
            },
            status=200,
        )

        result = client.get_project_roles("PROJ")
        assert "Administrators" in result
        assert "Developers" in result
        assert "Users" in result


class TestEditCommentVisibility:
    """Tests for edit_comment with visibility."""

    @responses.activate
    def test_edit_comment_with_visibility(self, client):
        """Should include visibility in request body."""
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment/456",
            json={
                "id": "456",
                "body": "Updated",
                "visibility": {"type": "role", "value": "Developers"},
            },
            status=200,
        )

        result = client.edit_comment(
            "PROJ-123", "456", "Updated",
            visibility={"type": "role", "value": "Developers"},
        )
        assert result["visibility"]["type"] == "role"

        request_body = json.loads(responses.calls[0].request.body)
        assert request_body["visibility"] == {"type": "role", "value": "Developers"}

    @responses.activate
    def test_edit_comment_without_visibility(self, client):
        """Should not include visibility when not provided."""
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment/456",
            json={"id": "456", "body": "Updated"},
            status=200,
        )

        client.edit_comment("PROJ-123", "456", "Updated")

        request_body = json.loads(responses.calls[0].request.body)
        assert "visibility" not in request_body


class TestDeleteAttachment:
    """Tests for delete_attachment method."""

    @responses.activate
    def test_delete_attachment_success(self, client):
        """Should delete attachment."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/attachment/12345",
            status=204,
        )

        client.delete_attachment("12345")
        assert len(responses.calls) == 1


class TestSearchUsers:
    """Tests for search_users method."""

    @responses.activate
    def test_search_users_success(self, client):
        """Should return matching users."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/user/search",
            json=[
                {"name": "jdoe", "displayName": "John Doe", "emailAddress": "jdoe@example.com", "active": True},
                {"name": "jsmith", "displayName": "Jane Smith", "emailAddress": "jsmith@example.com", "active": True},
            ],
            status=200,
        )

        result = client.search_users("j")
        assert len(result) == 2
        assert result[0]["name"] == "jdoe"

        url = responses.calls[0].request.url
        assert "username=j" in url
        assert "maxResults=10" in url

    @responses.activate
    def test_search_users_with_limit(self, client):
        """Should pass custom limit."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/user/search",
            json=[],
            status=200,
        )

        client.search_users("test", max_results=5)

        url = responses.calls[0].request.url
        assert "maxResults=5" in url


class TestGetIssueTypes:
    """Tests for get_issue_types method."""

    @responses.activate
    def test_get_all_issue_types(self, client):
        """Should return all server issue types."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issuetype",
            json=[
                {"id": "1", "name": "Bug", "subtask": False},
                {"id": "2", "name": "Task", "subtask": False},
                {"id": "3", "name": "Sub-task", "subtask": True},
            ],
            status=200,
        )

        result = client.get_issue_types()
        assert len(result) == 3
        assert result[0]["name"] == "Bug"

    @responses.activate
    def test_get_project_issue_types(self, client):
        """Should return project-specific issue types."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/project/PROJ",
            json={
                "key": "PROJ",
                "issueTypes": [
                    {"id": "1", "name": "Bug", "subtask": False},
                    {"id": "2", "name": "Task", "subtask": False},
                ],
            },
            status=200,
        )

        result = client.get_issue_types("PROJ")
        assert len(result) == 2

        url = responses.calls[0].request.url
        assert "expand=issueTypes" in url


class TestDeleteIssue:
    """Tests for delete_issue method."""

    @responses.activate
    def test_delete_issue_success(self, client):
        """Should delete issue."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=204,
        )

        client.delete_issue("PROJ-123")
        assert len(responses.calls) == 1

    @responses.activate
    def test_delete_issue_with_subtasks(self, client):
        """Should pass deleteSubtasks param."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=204,
        )

        client.delete_issue("PROJ-123", delete_subtasks=True)

        url = responses.calls[0].request.url
        assert "deleteSubtasks=true" in url


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


class TestResponseParsing:
    """Tests for response parsing edge cases."""

    @responses.activate
    def test_invalid_json_response(self, client):
        """Should handle invalid JSON in error response."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            body="Not valid JSON",
            status=400,
        )

        with pytest.raises(InvalidInputError):
            client.get_issue("PROJ-123")

    @responses.activate
    def test_field_level_jira_errors(self, client):
        """Should extract field-level errors from JIRA response."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue",
            json={
                "errors": {
                    "summary": "Summary is required",
                    "project": "Project does not exist",
                },
                "errorMessages": [],
            },
            status=400,
        )

        with pytest.raises(InvalidInputError) as exc_info:
            client.create_issue("BADPROJ", "Bug", "")
        assert "summary: Summary is required" in str(exc_info.value)

    @responses.activate
    def test_unexpected_http_status(self, client):
        """Should handle unexpected HTTP status codes."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"message": "I'm a teapot"},
            status=418,  # Unusual status code
        )

        with pytest.raises(JiraToolError) as exc_info:
            client.get_issue("PROJ-123")
        assert exc_info.value.code == ErrorCode.SERVER_ERROR
        assert exc_info.value.http_status == 418

    @responses.activate
    def test_empty_response_body(self, client):
        """Should handle empty error response body."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            body="",
            status=401,
        )

        with pytest.raises(AuthError):
            client.get_issue("PROJ-123")


class TestGetWatchers:
    """Tests for get_watchers method."""

    @responses.activate
    def test_get_watchers_success(self, client):
        """Should return watchers."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            json={
                "watchCount": 2,
                "isWatching": True,
                "watchers": [
                    {"name": "jdoe", "displayName": "John Doe", "active": True},
                    {"name": "jsmith", "displayName": "Jane Smith", "active": True},
                ],
            },
            status=200,
        )

        result = client.get_watchers("PROJ-123")
        assert result["watchCount"] == 2
        assert result["isWatching"] is True
        assert len(result["watchers"]) == 2


class TestAddWatcher:
    """Tests for add_watcher method."""

    @responses.activate
    def test_add_watcher_success(self, client):
        """Should add watcher."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        result = client.add_watcher("PROJ-123", "jdoe")
        assert result == {}

    @responses.activate
    def test_add_watcher_request_body(self, client):
        """Should send username as JSON string."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        client.add_watcher("PROJ-123", "jdoe")

        request_body = responses.calls[0].request.body
        assert request_body == '"jdoe"'


class TestRemoveWatcher:
    """Tests for remove_watcher method."""

    @responses.activate
    def test_remove_watcher_success(self, client):
        """Should remove watcher."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        result = client.remove_watcher("PROJ-123", "jdoe")
        assert result == {}

    @responses.activate
    def test_remove_watcher_params(self, client):
        """Should pass username as query parameter."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        client.remove_watcher("PROJ-123", "jdoe")

        url = responses.calls[0].request.url
        assert "username=jdoe" in url


class TestRetryConfiguration:
    """Tests for retry configuration."""

    def test_default_retry_settings(self, config):
        """Should have default retry settings."""
        client = JiraClient(config)
        assert client.max_retries == DEFAULT_MAX_RETRIES
        assert client.retry_backoff == DEFAULT_RETRY_BACKOFF
        assert client.retry_max_delay == DEFAULT_RETRY_MAX_DELAY

    def test_custom_retry_settings(self, config):
        """Should accept custom retry settings."""
        client = JiraClient(
            config,
            max_retries=5,
            retry_backoff=2.0,
            retry_max_delay=60.0,
        )
        assert client.max_retries == 5
        assert client.retry_backoff == 2.0
        assert client.retry_max_delay == 60.0

    def test_disable_retries(self, config):
        """Should allow disabling retries."""
        client = JiraClient(config, max_retries=0)
        assert client.max_retries == 0


class TestRetryOnTransientErrors:
    """Tests for retry behavior on transient errors."""

    @responses.activate
    @patch("time.sleep")
    def test_retry_on_500_then_success(self, mock_sleep, config):
        """Should retry on 500 and succeed."""
        client = JiraClient(config, max_retries=2, retry_backoff=0.1)

        # First call fails with 500
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"errorMessages": ["Internal error"]},
            status=500,
        )
        # Second call succeeds
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123"},
            status=200,
        )

        result = client.get_issue("PROJ-123")
        assert result["key"] == "PROJ-123"
        assert len(responses.calls) == 2
        assert mock_sleep.called

    @responses.activate
    @patch("time.sleep")
    def test_retry_on_429_rate_limit(self, mock_sleep, config):
        """Should retry on rate limiting."""
        client = JiraClient(config, max_retries=2, retry_backoff=0.1)

        # First call fails with rate limit
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=429,
        )
        # Second call succeeds
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123"},
            status=200,
        )

        result = client.get_issue("PROJ-123")
        assert result["key"] == "PROJ-123"
        assert len(responses.calls) == 2

    @responses.activate
    @patch("time.sleep")
    def test_retry_on_timeout(self, mock_sleep, config):
        """Should retry on timeout."""
        import requests.exceptions

        client = JiraClient(config, max_retries=2, retry_backoff=0.1)

        # First call times out
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            body=requests.exceptions.Timeout("Timeout"),
        )
        # Second call succeeds
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123"},
            status=200,
        )

        result = client.get_issue("PROJ-123")
        assert result["key"] == "PROJ-123"
        assert len(responses.calls) == 2

    @responses.activate
    @patch("time.sleep")
    def test_retry_on_connection_error(self, mock_sleep, config):
        """Should retry on connection error."""
        import requests.exceptions

        client = JiraClient(config, max_retries=2, retry_backoff=0.1)

        # First call fails with connection error
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )
        # Second call succeeds
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123"},
            status=200,
        )

        result = client.get_issue("PROJ-123")
        assert result["key"] == "PROJ-123"
        assert len(responses.calls) == 2

    @responses.activate
    @patch("time.sleep")
    def test_exhausted_retries_raises(self, mock_sleep, config):
        """Should raise after exhausting retries."""
        client = JiraClient(config, max_retries=2, retry_backoff=0.1)

        # All calls fail with 500
        for _ in range(3):  # Initial + 2 retries
            responses.add(
                responses.GET,
                "https://jira.example.com/rest/api/2/issue/PROJ-123",
                json={"errorMessages": ["Internal error"]},
                status=500,
            )

        with pytest.raises(JiraToolError) as exc_info:
            client.get_issue("PROJ-123")
        assert exc_info.value.code == ErrorCode.SERVER_ERROR
        assert len(responses.calls) == 3  # Initial + 2 retries


class TestNoRetryOnPermanentErrors:
    """Tests that permanent errors are not retried."""

    @responses.activate
    def test_no_retry_on_401(self, config):
        """Should not retry on auth failure."""
        client = JiraClient(config, max_retries=3)

        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=401,
        )

        with pytest.raises(AuthError):
            client.get_issue("PROJ-123")
        assert len(responses.calls) == 1  # No retries

    @responses.activate
    def test_no_retry_on_403(self, config):
        """Should not retry on forbidden."""
        client = JiraClient(config, max_retries=3)

        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=403,
        )

        with pytest.raises(AuthError):
            client.get_issue("PROJ-123")
        assert len(responses.calls) == 1

    @responses.activate
    def test_no_retry_on_404(self, config):
        """Should not retry on not found."""
        client = JiraClient(config, max_retries=3)

        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=404,
        )

        with pytest.raises(NotFoundError):
            client.get_issue("PROJ-123")
        assert len(responses.calls) == 1

    @responses.activate
    def test_no_retry_on_400(self, config):
        """Should not retry on bad request."""
        client = JiraClient(config, max_retries=3)

        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/search",
            json={"errorMessages": ["Bad JQL"]},
            status=400,
        )

        with pytest.raises(InvalidInputError):
            client.search_issues("bad jql")
        assert len(responses.calls) == 1


class TestRetryBackoff:
    """Tests for retry backoff calculation."""

    def test_exponential_backoff(self, config):
        """Should use exponential backoff."""
        client = JiraClient(config, retry_backoff=1.0, retry_max_delay=100.0)

        # With jitter disabled (mocked), delays should be approximately:
        # attempt 0: 1.0 * 2^0 = 1.0
        # attempt 1: 1.0 * 2^1 = 2.0
        # attempt 2: 1.0 * 2^2 = 4.0
        with patch("random.random", return_value=0.5):  # No jitter (centered)
            delay0 = client._calculate_retry_delay(0)
            delay1 = client._calculate_retry_delay(1)
            delay2 = client._calculate_retry_delay(2)

        assert 0.75 <= delay0 <= 1.25  # ~1.0 with some jitter tolerance
        assert 1.5 <= delay1 <= 2.5  # ~2.0
        assert 3.0 <= delay2 <= 5.0  # ~4.0

    def test_max_delay_cap(self, config):
        """Should cap delay at max_delay."""
        client = JiraClient(config, retry_backoff=10.0, retry_max_delay=5.0)

        # Even with high backoff, should be capped
        delay = client._calculate_retry_delay(10)  # Would be 10 * 2^10 = 10240
        assert delay <= 5.0


class TestRetryWithAttachments:
    """Tests for retry with attachment operations."""

    @responses.activate
    @patch("time.sleep")
    def test_attachment_download_retry(self, mock_sleep, config):
        """Should retry attachment download on transient errors."""
        client = JiraClient(config, max_retries=2, retry_backoff=0.1)

        # Metadata call succeeds
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/123",
            json={
                "id": "123",
                "filename": "test.txt",
                "size": 10,
                "content": "https://jira.example.com/secure/attachment/123/test.txt",
            },
            status=200,
        )
        # First download fails with 500
        responses.add(
            responses.GET,
            "https://jira.example.com/secure/attachment/123/test.txt",
            status=500,
        )
        # Second download succeeds
        responses.add(
            responses.GET,
            "https://jira.example.com/secure/attachment/123/test.txt",
            body=b"test content",
            status=200,
        )

        content, metadata = client.get_attachment_content("123")
        assert content == b"test content"
        assert len(responses.calls) == 3  # metadata + 2 download attempts

    @responses.activate
    @patch("time.sleep")
    def test_add_watcher_retry(self, mock_sleep, config):
        """Should retry add_watcher on transient errors."""
        client = JiraClient(config, max_retries=2, retry_backoff=0.1)

        # First call fails with 500
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=500,
        )
        # Second call succeeds
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        result = client.add_watcher("PROJ-123", "jdoe")
        assert result == {}
        assert len(responses.calls) == 2
