"""Unit tests for CLI module."""

import json

import pytest
import responses
from click.testing import CliRunner

from jira_tool.cli import _normalize_comment, _normalize_comments, _normalize_issue, extract_issue_key, extract_field, main


@pytest.fixture
def runner():
    """Create CLI runner."""
    return CliRunner()


@pytest.fixture
def mock_env(monkeypatch):
    """Set up mock environment variables."""
    monkeypatch.setenv("JIRA_SERVER", "https://jira.example.com")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")


class TestExtractField:
    """Tests for extract_field function."""

    def test_simple_field(self):
        """Should extract top-level field."""
        data = {"key": "PROJ-123", "status": "Open"}
        assert extract_field(data, "key") == "PROJ-123"
        assert extract_field(data, "status") == "Open"

    def test_nested_field(self):
        """Should extract nested field with dot notation."""
        data = {"assignee": {"name": "jdoe", "displayName": "John Doe"}}
        assert extract_field(data, "assignee.name") == "jdoe"
        assert extract_field(data, "assignee.displayName") == "John Doe"

    def test_missing_field(self):
        """Should return None for missing field."""
        data = {"key": "PROJ-123"}
        assert extract_field(data, "status") is None
        assert extract_field(data, "assignee.name") is None


class TestExtractIssueKey:
    """Tests for extract_issue_key function."""

    def test_bare_key(self):
        """Should return bare key unchanged."""
        assert extract_issue_key("PROJ-123") == "PROJ-123"
        assert extract_issue_key("LU-19839") == "LU-19839"
        assert extract_issue_key("ABC_DEF-1") == "ABC_DEF-1"

    def test_browse_url(self):
        """Should extract key from browse URL."""
        assert extract_issue_key("https://jira.example.com/browse/PROJ-123") == "PROJ-123"
        assert extract_issue_key("https://jira.whamcloud.com/browse/LU-19839") == "LU-19839"
        assert extract_issue_key("http://jira.example.com/browse/TEST-1") == "TEST-1"

    def test_browse_url_with_query(self):
        """Should extract key from browse URL with query params."""
        assert extract_issue_key("https://jira.example.com/browse/PROJ-123?focusedCommentId=123") == "PROJ-123"

    def test_rest_api_url(self):
        """Should extract key from REST API URL."""
        assert extract_issue_key("https://jira.example.com/rest/api/2/issue/PROJ-123") == "PROJ-123"

    def test_fallback_pattern_match(self):
        """Should find key pattern anywhere in string."""
        assert extract_issue_key("some text PROJ-456 more text") == "PROJ-456"

    def test_invalid_returns_original(self):
        """Should return original if no key found."""
        assert extract_issue_key("not-a-key") == "not-a-key"
        assert extract_issue_key("lowercase-123") == "lowercase-123"


class TestNormalizeIssue:
    """Tests for issue normalization."""

    def test_normalize_full_issue(self):
        """Should normalize all fields."""
        raw = {
            "key": "PROJ-123",
            "id": "10001",
            "self": "https://jira.example.com/rest/api/2/issue/10001",
            "fields": {
                "summary": "Test issue",
                "description": "Issue description",
                "status": {"name": "Open"},
                "priority": {"name": "High"},
                "issuetype": {"name": "Bug"},
                "project": {"key": "PROJ"},
                "assignee": {"displayName": "John Doe"},
                "reporter": {"displayName": "Jane Doe"},
                "resolution": {"name": "Fixed"},
                "created": "2024-01-15T10:00:00.000+0000",
                "updated": "2024-01-15T12:00:00.000+0000",
                "labels": ["bug", "urgent"],
            },
        }

        result = _normalize_issue(raw)

        assert result["key"] == "PROJ-123"
        assert result["id"] == "10001"
        assert result["summary"] == "Test issue"
        assert result["description"] == "Issue description"
        assert result["status"] == "Open"
        assert result["priority"] == "High"
        assert result["issue_type"] == "Bug"
        assert result["project"] == "PROJ"
        assert result["assignee"] == "John Doe"
        assert result["reporter"] == "Jane Doe"
        assert result["resolution"] == "Fixed"
        assert result["labels"] == ["bug", "urgent"]

    def test_normalize_issue_with_null_fields(self):
        """Should handle null/missing fields."""
        raw = {
            "key": "PROJ-123",
            "fields": {
                "summary": "Test",
                "assignee": None,
                "resolution": None,
            },
        }

        result = _normalize_issue(raw)

        assert result["assignee"] is None
        assert result["resolution"] is None


class TestNormalizeComment:
    """Tests for comment normalization."""

    def test_normalize_comment(self):
        """Should normalize comment fields."""
        raw = {
            "id": "12345",
            "body": "Comment text",
            "author": {
                "displayName": "John Doe",
                "emailAddress": "john@example.com",
            },
            "updateAuthor": {"displayName": "Jane Doe"},
            "created": "2024-01-15T10:00:00.000+0000",
            "updated": "2024-01-15T12:00:00.000+0000",
        }

        result = _normalize_comment(raw)

        assert result["id"] == "12345"
        assert result["body"] == "Comment text"
        assert result["author"] == "John Doe"
        assert result["author_email"] == "john@example.com"
        assert result["update_author"] == "Jane Doe"


class TestNormalizeComments:
    """Tests for comments batch normalization."""

    def test_normalize_comments_with_content(self):
        """Should include full comment content."""
        raw = {
            "comments": [
                {
                    "id": "1",
                    "body": "First",
                    "author": {"displayName": "User"},
                    "created": "2024-01-15T10:00:00.000+0000",
                },
                {
                    "id": "2",
                    "body": "Second",
                    "author": {"displayName": "User"},
                    "created": "2024-01-15T11:00:00.000+0000",
                },
            ],
            "total": 2,
        }

        result = _normalize_comments(raw, summary_only=False)

        assert result["total_comments"] == 2
        assert len(result["comments"]) == 2
        assert result["comments"][0]["body"] == "First"

    def test_normalize_comments_summary_only(self):
        """Should only include summaries."""
        raw = {
            "comments": [
                {
                    "id": "1",
                    "body": "A" * 200,
                    "author": {"displayName": "User"},
                    "created": "2024-01-15T10:00:00.000+0000",
                },
            ],
            "total": 1,
        }

        result = _normalize_comments(raw, summary_only=True)

        assert "comments" not in result
        assert "comments_summary" in result
        assert result["comments_summary"][0]["body_preview"].endswith("...")
        assert len(result["comments_summary"][0]["body_preview"]) <= 103  # 100 + "..."


class TestCLIIssueGet:
    """Tests for 'jira issue get' command."""

    @responses.activate
    def test_issue_get_success(self, runner, mock_env):
        """Should return issue in envelope."""
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

        result = runner.invoke(main, ["issue", "get", "PROJ-123"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["key"] == "PROJ-123"
        assert data["meta"]["command"] == "issue.get"

    @responses.activate
    def test_issue_get_not_found(self, runner, mock_env):
        """Should return error envelope for 404."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-999",
            json={"errorMessages": ["Issue not found"]},
            status=404,
        )

        result = runner.invoke(main, ["issue", "get", "PROJ-999"])

        assert result.exit_code == 3  # NOT_FOUND
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "ISSUE_NOT_FOUND" in data["error"]["code"]

    @responses.activate
    def test_issue_get_pretty(self, runner, mock_env):
        """Should format with indentation when --pretty."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {}},
            status=200,
        )

        result = runner.invoke(main, ["--pretty", "issue", "get", "PROJ-123"])

        assert result.exit_code == 0
        assert "\n" in result.output
        assert "  " in result.output

    @responses.activate
    def test_issue_get_output_field(self, runner, mock_env):
        """Should output only requested field as plain text."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {"status": {"name": "Open"}, "summary": "Test issue"},
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "get", "PROJ-123", "--output", "key"])
        assert result.exit_code == 0
        assert result.output.strip() == "PROJ-123"
        # Should not be JSON
        assert "{" not in result.output

    @responses.activate
    def test_issue_get_output_status(self, runner, mock_env):
        """Should output status field."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {"status": {"name": "In Progress"}},
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "get", "PROJ-123", "--output", "status"])
        assert result.exit_code == 0
        assert result.output.strip() == "In Progress"


class TestCLIIssueComments:
    """Tests for 'jira issue comments' command."""

    @responses.activate
    def test_comments_default_limit(self, runner, mock_env):
        """Should use default limit of 5."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={"comments": [], "total": 0},
            status=200,
        )

        result = runner.invoke(main, ["issue", "comments", "PROJ-123"])

        assert result.exit_code == 0
        # Check that limit was 5 (default)
        url = responses.calls[0].request.url
        assert "maxResults=5" in url

    @responses.activate
    def test_comments_custom_limit(self, runner, mock_env):
        """Should respect --limit option."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={"comments": [], "total": 0},
            status=200,
        )

        result = runner.invoke(main, ["issue", "comments", "PROJ-123", "--limit", "10"])

        assert result.exit_code == 0
        url = responses.calls[0].request.url
        assert "maxResults=10" in url

    @responses.activate
    def test_comments_with_pagination_info(self, runner, mock_env):
        """Should include pagination metadata."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "comments": [
                    {
                        "id": "1",
                        "body": "Test",
                        "author": {"displayName": "User"},
                        "created": "2024-01-15T10:00:00.000+0000",
                    }
                ],
                "total": 10,
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "comments", "PROJ-123"])

        data = json.loads(result.output)
        assert data["data"]["pagination"]["total"] == 10
        assert data["data"]["pagination"]["returned"] == 1


class TestCLIIssueSearch:
    """Tests for 'jira issue search' command."""

    @responses.activate
    def test_search_basic(self, runner, mock_env):
        """Should search with JQL."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": [], "total": 0},
            status=200,
        )

        result = runner.invoke(main, ["issue", "search", "project = PROJ"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["jql"] == "project = PROJ"


class TestCLIIssueComment:
    """Tests for 'jira issue comment' command."""

    @responses.activate
    def test_add_comment(self, runner, mock_env):
        """Should add comment to issue."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "id": "12345",
                "body": "Test comment",
                "author": {"displayName": "User"},
                "created": "2024-01-15T10:00:00.000+0000",
            },
            status=201,
        )

        result = runner.invoke(main, ["issue", "comment", "PROJ-123", "Test comment"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["issue_key"] == "PROJ-123"
        assert data["data"]["comment"]["body"] == "Test comment"


class TestCLIIssueTransitions:
    """Tests for 'jira issue transitions' command."""

    @responses.activate
    def test_list_transitions(self, runner, mock_env):
        """Should list available transitions."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/transitions",
            json={
                "transitions": [
                    {"id": "11", "name": "Start", "to": {"name": "In Progress"}},
                ],
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "transitions", "PROJ-123"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["transitions"]) == 1
        assert data["data"]["transitions"][0]["name"] == "Start"


class TestCLIConfigTest:
    """Tests for 'jira config test' command."""

    @responses.activate
    def test_config_test_success(self, runner, mock_env):
        """Should test connectivity."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/serverInfo",
            json={
                "serverTitle": "Test JIRA",
                "version": "8.0.0",
                "baseUrl": "https://jira.example.com",
            },
            status=200,
        )

        result = runner.invoke(main, ["config", "test"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["connected"] is True
        assert data["data"]["version"] == "8.0.0"

    def test_config_missing(self, runner, monkeypatch):
        """Should error when config missing."""
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)

        result = runner.invoke(main, ["--config", "/nonexistent/path.json", "config", "test"])

        assert result.exit_code == 1  # GENERAL_ERROR
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "CONFIG_ERROR" in data["error"]["code"]


class TestCLIConfigShow:
    """Tests for 'jira config show' command."""

    def test_config_show_redacts_token(self, runner, mock_env):
        """Should redact token in output."""
        result = runner.invoke(main, ["config", "show"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        # Token should be redacted - full token never shown
        assert "test-token" not in data["data"]["token"]
        # Short tokens show as ***, longer tokens show partial with ...
        assert "..." in data["data"]["token"] or "***" in data["data"]["token"]


class TestCLIIssueAttachments:
    """Tests for 'jira issue attachments' command."""

    @responses.activate
    def test_attachments_list(self, runner, mock_env):
        """Should list attachments."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {
                    "attachment": [
                        {
                            "id": "12345",
                            "filename": "test.txt",
                            "size": 1024,
                            "mimeType": "text/plain",
                            "author": {"displayName": "User"},
                            "created": "2024-01-15T10:00:00.000+0000",
                            "content": "https://jira.example.com/secure/attachment/12345/test.txt",
                        }
                    ]
                },
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "attachments", "PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["total"] == 1
        assert data["data"]["attachments"][0]["filename"] == "test.txt"


class TestCLIIssueLinks:
    """Tests for 'jira issue links' command."""

    @responses.activate
    def test_links_list(self, runner, mock_env):
        """Should list issue links."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {
                    "issuelinks": [
                        {
                            "type": {
                                "name": "Blocks",
                                "inward": "is blocked by",
                                "outward": "blocks",
                            },
                            "outwardIssue": {
                                "key": "PROJ-456",
                                "fields": {
                                    "summary": "Blocked issue",
                                    "status": {"name": "Open"},
                                },
                            },
                        },
                        {
                            "type": {
                                "name": "Relates",
                                "inward": "relates to",
                                "outward": "relates to",
                            },
                            "inwardIssue": {
                                "key": "PROJ-789",
                                "fields": {
                                    "summary": "Related issue",
                                    "status": {"name": "Closed"},
                                },
                            },
                        },
                    ]
                },
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "links", "PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["total"] == 2
        assert data["data"]["links"][0]["relationship"] == "blocks"
        assert data["data"]["links"][0]["issue_key"] == "PROJ-456"
        assert data["data"]["links"][1]["relationship"] == "relates to"
        assert data["data"]["links"][1]["issue_key"] == "PROJ-789"


class TestCLIAttachmentGet:
    """Tests for 'jira attachment get' command."""

    @responses.activate
    def test_attachment_get(self, runner, mock_env):
        """Should get attachment metadata."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/12345",
            json={
                "id": "12345",
                "filename": "test.txt",
                "size": 1024,
                "mimeType": "text/plain",
                "author": {"displayName": "User"},
                "created": "2024-01-15T10:00:00.000+0000",
                "content": "https://jira.example.com/secure/attachment/12345/test.txt",
            },
            status=200,
        )

        result = runner.invoke(main, ["attachment", "get", "12345"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["filename"] == "test.txt"
        assert data["data"]["size_human"] == "1.0 KB"


class TestCLIAttachmentContent:
    """Tests for 'jira attachment content' command."""

    @responses.activate
    def test_attachment_content_text(self, runner, mock_env):
        """Should get text attachment content."""
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
        responses.add(
            responses.GET,
            "https://jira.example.com/secure/attachment/12345/test.txt",
            body=b"Hello, World!",
            status=200,
        )

        result = runner.invoke(main, ["attachment", "content", "12345"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["content"] == "Hello, World!"
        assert data["data"]["is_text"] is True

    @responses.activate
    def test_attachment_content_too_large(self, runner, mock_env):
        """Should error for large attachments."""
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

        result = runner.invoke(main, ["attachment", "content", "12345"])
        assert result.exit_code == 4  # INVALID_INPUT
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "too large" in data["error"]["message"]


class TestNormalizeAttachment:
    """Tests for attachment normalization."""

    def test_normalize_attachment(self):
        """Should normalize attachment fields."""
        from jira_tool.cli import _normalize_attachment

        raw = {
            "id": "12345",
            "filename": "test.txt",
            "size": 1536,  # 1.5 KB
            "mimeType": "text/plain",
            "author": {"displayName": "Test User"},
            "created": "2024-01-15T10:00:00.000+0000",
            "content": "https://jira.example.com/secure/attachment/12345/test.txt",
        }

        result = _normalize_attachment(raw)

        assert result["id"] == "12345"
        assert result["filename"] == "test.txt"
        assert result["size"] == 1536
        assert result["size_human"] == "1.5 KB"
        assert result["mime_type"] == "text/plain"
        assert result["author"] == "Test User"

    def test_normalize_attachment_large_size(self):
        """Should format large sizes in MB."""
        from jira_tool.cli import _normalize_attachment

        raw = {
            "id": "1",
            "filename": "large.bin",
            "size": 5 * 1024 * 1024,  # 5MB
            "mimeType": "application/octet-stream",
            "content": "https://example.com/file",
        }

        result = _normalize_attachment(raw)
        assert result["size_human"] == "5.0 MB"

    def test_normalize_attachment_bytes_size(self):
        """Should format small sizes in bytes."""
        from jira_tool.cli import _normalize_attachment

        raw = {
            "id": "1",
            "filename": "tiny.txt",
            "size": 500,
            "mimeType": "text/plain",
            "content": "https://example.com/file",
        }

        result = _normalize_attachment(raw)
        assert result["size_human"] == "500 B"


class TestCLIIssueTransition:
    """Tests for 'jira issue transition' command."""

    @responses.activate
    def test_transition_success(self, runner, mock_env):
        """Should transition issue."""
        # Mock get issue before
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"status": {"name": "Open"}}},
            status=200,
        )
        # Mock transition
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/transitions",
            status=204,
        )
        # Mock get issue after
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"status": {"name": "In Progress"}}},
            status=200,
        )

        result = runner.invoke(main, ["issue", "transition", "PROJ-123", "11"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["status_before"] == "Open"
        assert data["data"]["status_after"] == "In Progress"

    @responses.activate
    def test_transition_with_comment(self, runner, mock_env):
        """Should transition with comment."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"status": {"name": "Open"}}},
            status=200,
        )
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/transitions",
            status=204,
        )
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"status": {"name": "Done"}}},
            status=200,
        )

        result = runner.invoke(main, ["issue", "transition", "PROJ-123", "21", "--comment", "Closing"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["comment_added"] is True


class TestCLIIssueCreate:
    """Tests for 'jira issue create' command."""

    @responses.activate
    def test_create_issue(self, runner, mock_env):
        """Should create issue."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue",
            json={"id": "10001", "key": "PROJ-124", "self": "https://jira.example.com/rest/api/2/issue/10001"},
            status=201,
        )

        result = runner.invoke(
            main, ["issue", "create", "--project", "PROJ", "--type", "Bug", "--summary", "Test bug"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["key"] == "PROJ-124"

    @responses.activate
    def test_create_issue_with_description(self, runner, mock_env):
        """Should create issue with description."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue",
            json={"id": "10001", "key": "PROJ-125", "self": "https://jira.example.com/rest/api/2/issue/10001"},
            status=201,
        )

        result = runner.invoke(
            main,
            [
                "issue",
                "create",
                "--project",
                "PROJ",
                "--type",
                "Task",
                "--summary",
                "New task",
                "--description",
                "Task details",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["key"] == "PROJ-125"


class TestCLIIssueUpdate:
    """Tests for 'jira issue update' command."""

    @responses.activate
    def test_update_issue_summary(self, runner, mock_env):
        """Should update issue summary."""
        # Mock the GET before update
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {"summary": "Old summary", "status": {"name": "Open"}},
            },
            status=200,
        )
        # Mock the PUT update
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=204,
        )
        # Mock the GET after update
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {"summary": "New summary", "status": {"name": "Open"}},
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "update", "PROJ-123", "--summary", "New summary"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["issue_key"] == "PROJ-123"
        assert "summary" in data["data"]["updated_fields"]

    @responses.activate
    def test_update_issue_assignee(self, runner, mock_env):
        """Should update issue assignee."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"assignee": None}},
            status=200,
        )
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=204,
        )
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"assignee": {"displayName": "John Doe"}}},
            status=200,
        )

        result = runner.invoke(main, ["issue", "update", "PROJ-123", "--assignee", "jdoe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert "assignee" in data["data"]["updated_fields"]
        assert data["data"]["assignee"] == "John Doe"

    def test_update_no_fields_error(self, runner, mock_env):
        """Should error when no fields specified."""
        result = runner.invoke(main, ["issue", "update", "PROJ-123"])
        assert result.exit_code == 4  # INVALID_INPUT
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "No fields specified" in data["error"]["message"]

    @responses.activate
    def test_update_issue_labels(self, runner, mock_env):
        """Should update issue labels."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"labels": ["old"]}},
            status=200,
        )
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            status=204,
        )
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"labels": ["new", "labels"]}},
            status=200,
        )

        result = runner.invoke(main, ["issue", "update", "PROJ-123", "--labels", "new,labels"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert "labels" in data["data"]["updated_fields"]
        assert data["data"]["labels"] == ["new", "labels"]


class TestCLIConfigSample:
    """Tests for 'jira config sample' command."""

    def test_config_sample(self, runner):
        """Should output sample config."""
        result = runner.invoke(main, ["config", "sample"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert "sample_config" in data["data"]
        assert "default_path" in data["data"]


class TestCLICommentsAllFlag:
    """Tests for 'jira issue comments --all' flag."""

    @responses.activate
    def test_comments_all_flag(self, runner, mock_env):
        """Should fetch all comments with --all flag."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "comments": [{"id": "1", "body": "Comment", "author": {"displayName": "User"}}],
                "total": 1,
            },
            status=200,
        )

        result = runner.invoke(main, ["issue", "comments", "PROJ-123", "--all"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        # With --all, limit should be set to 1000
        assert data["data"]["pagination"]["limit"] == 1000


class TestCLIConfigError:
    """Tests for ConfigError handling in CLI commands."""

    def test_missing_config_error(self, runner):
        """Should return proper error for missing config."""
        # Don't set environment variables
        result = runner.invoke(main, ["issue", "get", "PROJ-123"])
        assert result.exit_code == 1  # GENERAL_ERROR (ConfigError)
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "JIRA_SERVER" in data["error"]["message"] or "config" in data["error"]["message"].lower()


class TestCLIAttachmentContentBinary:
    """Tests for binary attachment content handling."""

    @responses.activate
    def test_attachment_content_binary(self, runner, mock_env):
        """Should handle binary content gracefully."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/attachment/12345",
            json={
                "id": "12345",
                "filename": "image.png",
                "size": 10,
                "mimeType": "image/png",
                "content": "https://jira.example.com/secure/attachment/12345/image.png",
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://jira.example.com/secure/attachment/12345/image.png",
            body=b"\x89PNG\r\n\x1a\n\x00\x00",  # Invalid UTF-8
            status=200,
        )

        result = runner.invoke(main, ["attachment", "content", "12345"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["is_text"] is False
        assert data["data"]["content"] is None
        assert "Binary content" in data["data"]["note"]
