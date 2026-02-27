"""Unit tests for CLI module."""

import json

import pytest
import responses
from click.testing import CliRunner

from jira_tool.cli import (
    _normalize_comment,
    _normalize_comments,
    _normalize_issue,
    _parse_visibility,
    extract_issue_key,
    extract_field,
    main,
)


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

    def test_normalize_comment_with_visibility(self):
        """Should include visibility when present."""
        raw = {
            "id": "12345",
            "body": "Restricted",
            "author": {"displayName": "John Doe"},
            "visibility": {"type": "role", "value": "Developers"},
            "created": "2024-01-15T10:00:00.000+0000",
        }

        result = _normalize_comment(raw)

        assert result["visibility"]["type"] == "role"
        assert result["visibility"]["value"] == "Developers"

    def test_normalize_comment_without_visibility(self):
        """Should not include visibility when absent."""
        raw = {
            "id": "12345",
            "body": "Public",
            "author": {"displayName": "John Doe"},
            "created": "2024-01-15T10:00:00.000+0000",
        }

        result = _normalize_comment(raw)

        assert "visibility" not in result


class TestParseVisibility:
    """Tests for _parse_visibility helper."""

    def test_parse_role_visibility(self):
        """Should parse role:Name format."""
        result = _parse_visibility("role:Developers")
        assert result == {"type": "role", "value": "Developers"}

    def test_parse_group_visibility(self):
        """Should parse group:Name format."""
        result = _parse_visibility("group:jira-users")
        assert result == {"type": "group", "value": "jira-users"}

    def test_parse_visibility_with_spaces(self):
        """Should handle spaces around colon."""
        result = _parse_visibility("role : Project Admins")
        assert result == {"type": "role", "value": "Project Admins"}

    def test_parse_visibility_case_insensitive_type(self):
        """Should accept uppercase type."""
        result = _parse_visibility("Role:Developers")
        assert result == {"type": "role", "value": "Developers"}

    def test_parse_visibility_no_colon(self):
        """Should reject missing colon."""
        from click import BadParameter

        with pytest.raises(BadParameter, match="Invalid visibility format"):
            _parse_visibility("Developers")

    def test_parse_visibility_invalid_type(self):
        """Should reject invalid type."""
        from click import BadParameter

        with pytest.raises(BadParameter, match="Invalid visibility type"):
            _parse_visibility("user:john")

    def test_parse_visibility_empty_value(self):
        """Should reject empty value."""
        from click import BadParameter

        with pytest.raises(BadParameter, match="cannot be empty"):
            _parse_visibility("role:")


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
    """Tests for 'jira get' command."""

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

        result = runner.invoke(main, ["get","PROJ-123"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["key"] == "PROJ-123"
        assert data["meta"]["command"] == "get"

    @responses.activate
    def test_issue_get_not_found(self, runner, mock_env):
        """Should return error envelope for 404."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-999",
            json={"errorMessages": ["Issue not found"]},
            status=404,
        )

        result = runner.invoke(main, ["get","PROJ-999"])

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

        result = runner.invoke(main, ["--pretty", "get", "PROJ-123"])

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

        result = runner.invoke(main, ["get","PROJ-123", "--output", "key"])
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

        result = runner.invoke(main, ["get","PROJ-123", "--output", "status"])
        assert result.exit_code == 0
        assert result.output.strip() == "In Progress"


class TestCLIIssueGetWithComments:
    """Tests for 'jira get --comments' flag."""

    @responses.activate
    def test_get_with_comments_flag(self, runner, mock_env):
        """Should include comments when --comments is used."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={
                "key": "PROJ-123",
                "fields": {"summary": "Test issue", "status": {"name": "Open"}},
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "comments": [
                    {"id": "1", "body": "First comment", "author": {"displayName": "User"}, "created": "2024-01-15T10:00:00.000+0000"},
                ],
                "total": 1,
            },
            status=200,
        )

        result = runner.invoke(main, ["get", "PROJ-123", "--comments"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["key"] == "PROJ-123"
        assert data["data"]["total_comments"] == 1
        assert data["data"]["comments"][0]["body"] == "First comment"

    @responses.activate
    def test_get_without_comments(self, runner, mock_env):
        """Should not include comments by default."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {"summary": "Test"}},
            status=200,
        )

        result = runner.invoke(main, ["get", "PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "comments" not in data["data"]


class TestCLIPrettyTrailing:
    """Tests for --pretty in trailing position."""

    @responses.activate
    def test_pretty_after_command(self, runner, mock_env):
        """Should work when --pretty comes after the command."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123",
            json={"key": "PROJ-123", "fields": {}},
            status=200,
        )

        result = runner.invoke(main, ["get", "PROJ-123", "--pretty"])
        assert result.exit_code == 0
        assert "\n" in result.output
        assert "  " in result.output


class TestCLIIssueComments:
    """Tests for 'jira comments' command."""

    @responses.activate
    def test_comments_default_limit(self, runner, mock_env):
        """Should use default limit of 5."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={"comments": [], "total": 0},
            status=200,
        )

        result = runner.invoke(main, ["comments","PROJ-123"])

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

        result = runner.invoke(main, ["comments","PROJ-123", "--limit", "10"])

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

        result = runner.invoke(main, ["comments","PROJ-123"])

        data = json.loads(result.output)
        assert data["data"]["pagination"]["total"] == 10
        assert data["data"]["pagination"]["returned"] == 1


class TestCLIIssueSearch:
    """Tests for 'jira search' command."""

    @responses.activate
    def test_search_basic(self, runner, mock_env):
        """Should search with JQL."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": [], "total": 0},
            status=200,
        )

        result = runner.invoke(main, ["search","project = PROJ"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["jql"] == "project = PROJ"


class TestCLIIssueComment:
    """Tests for 'jira comment' command."""

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

        result = runner.invoke(main, ["comment","PROJ-123", "Test comment"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["issue_key"] == "PROJ-123"
        assert data["data"]["comment"]["body"] == "Test comment"

    @responses.activate
    def test_add_comment_with_visibility(self, runner, mock_env):
        """Should add comment with visibility restriction."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "id": "12345",
                "body": "Internal note",
                "author": {"displayName": "User"},
                "created": "2024-01-15T10:00:00.000+0000",
                "visibility": {"type": "role", "value": "Developers"},
            },
            status=201,
        )

        result = runner.invoke(
            main, ["comment", "PROJ-123", "Internal note", "--visibility", "role:Developers"]
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["comment"]["visibility"]["type"] == "role"
        assert data["data"]["comment"]["visibility"]["value"] == "Developers"

        # Verify the request body included visibility
        request_body = json.loads(responses.calls[0].request.body)
        assert request_body["visibility"] == {"type": "role", "value": "Developers"}

    def test_add_comment_with_invalid_visibility(self, runner, mock_env):
        """Should fail with invalid visibility format."""
        result = runner.invoke(
            main, ["comment", "PROJ-123", "text", "--visibility", "invalid"]
        )

        assert result.exit_code != 0

    @responses.activate
    def test_add_comment_without_visibility(self, runner, mock_env):
        """Should not send visibility when option not provided."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment",
            json={
                "id": "12345",
                "body": "Public comment",
                "author": {"displayName": "User"},
                "created": "2024-01-15T10:00:00.000+0000",
            },
            status=201,
        )

        result = runner.invoke(main, ["comment", "PROJ-123", "Public comment"])

        assert result.exit_code == 0
        request_body = json.loads(responses.calls[0].request.body)
        assert "visibility" not in request_body


class TestCLIProjectRoles:
    """Tests for 'jira roles' command."""

    @responses.activate
    def test_list_roles(self, runner, mock_env):
        """Should list project roles."""
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

        result = runner.invoke(main, ["roles", "PROJ"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["project_key"] == "PROJ"
        assert data["data"]["total"] == 3
        assert "Administrators" in data["data"]["roles"]
        assert "Developers" in data["data"]["roles"]
        assert "Users" in data["data"]["roles"]
        # Should be sorted
        assert data["data"]["roles"] == sorted(data["data"]["roles"])


class TestCLIIssueTransitions:
    """Tests for 'jira transitions' command."""

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

        result = runner.invoke(main, ["transitions","PROJ-123"])

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
    """Tests for 'jira attachments' command."""

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

        result = runner.invoke(main, ["attachments","PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["total"] == 1
        assert data["data"]["attachments"][0]["filename"] == "test.txt"


class TestCLIIssueLinks:
    """Tests for 'jira links' command."""

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

        result = runner.invoke(main, ["links","PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["total"] == 2
        assert data["data"]["links"][0]["relationship"] == "blocks"
        assert data["data"]["links"][0]["issue_key"] == "PROJ-456"
        assert data["data"]["links"][1]["relationship"] == "relates to"
        assert data["data"]["links"][1]["issue_key"] == "PROJ-789"


class TestCLIIssueWorklogs:
    """Tests for 'jira worklogs' command."""

    @responses.activate
    def test_worklogs_list(self, runner, mock_env):
        """Should list worklogs."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/worklog",
            json={
                "worklogs": [
                    {
                        "id": "10001",
                        "author": {"displayName": "John Doe"},
                        "timeSpent": "2h",
                        "timeSpentSeconds": 7200,
                        "comment": "Working on feature",
                        "started": "2024-01-15T10:00:00.000+0000",
                        "created": "2024-01-15T12:00:00.000+0000",
                        "updated": "2024-01-15T12:00:00.000+0000",
                    }
                ],
                "total": 1,
            },
            status=200,
        )

        result = runner.invoke(main, ["worklogs","PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["total"] == 1
        assert data["data"]["worklogs"][0]["time_spent"] == "2h"
        assert data["data"]["worklogs"][0]["author"] == "John Doe"


class TestCLIIssueWorklogAdd:
    """Tests for 'jira worklog' command."""

    @responses.activate
    def test_worklog_add(self, runner, mock_env):
        """Should add worklog."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/worklog",
            json={
                "id": "10002",
                "timeSpent": "1h 30m",
                "timeSpentSeconds": 5400,
                "comment": "Code review",
                "started": "2024-01-15T14:00:00.000+0000",
            },
            status=201,
        )

        result = runner.invoke(main, ["worklog","PROJ-123", "1h 30m", "--comment", "Code review"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["worklog"]["time_spent"] == "1h 30m"


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
    """Tests for 'jira transition' command."""

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

        result = runner.invoke(main, ["transition","PROJ-123", "11"])
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

        result = runner.invoke(main, ["transition","PROJ-123", "21", "--comment", "Closing"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["comment_added"] is True


class TestCLIIssueCreate:
    """Tests for 'jira create' command."""

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
            main, ["create","--project", "PROJ", "--type", "Bug", "--summary", "Test bug"]
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
    """Tests for 'jira update' command."""

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

        result = runner.invoke(main, ["update","PROJ-123", "--summary", "New summary"])
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

        result = runner.invoke(main, ["update","PROJ-123", "--assignee", "jdoe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert "assignee" in data["data"]["updated_fields"]
        assert data["data"]["assignee"] == "John Doe"

    def test_update_no_fields_error(self, runner, mock_env):
        """Should error when no fields specified."""
        result = runner.invoke(main, ["update","PROJ-123"])
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

        result = runner.invoke(main, ["update","PROJ-123", "--labels", "new,labels"])
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

        result = runner.invoke(main, ["comments","PROJ-123", "--all"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        # With --all, limit should be set to 1000
        assert data["data"]["pagination"]["limit"] == 1000


class TestCLIConfigError:
    """Tests for ConfigError handling in CLI commands."""

    def test_missing_config_error(self, runner, monkeypatch):
        """Should return proper error for missing config."""
        # Ensure no environment variables or config file
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)
        result = runner.invoke(main, ["--config", "/nonexistent/no-such-file.json", "get", "PROJ-123"])
        assert result.exit_code == 1  # GENERAL_ERROR (ConfigError)
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "CONFIG_ERROR" in data["error"]["code"]


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


class TestCLIIssueWatchers:
    """Tests for 'jira watchers' command."""

    @responses.activate
    def test_watchers_list(self, runner, mock_env):
        """Should list watchers."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            json={
                "watchCount": 2,
                "isWatching": True,
                "watchers": [
                    {
                        "name": "jdoe",
                        "displayName": "John Doe",
                        "emailAddress": "john@example.com",
                        "active": True,
                    },
                    {
                        "name": "jsmith",
                        "displayName": "Jane Smith",
                        "emailAddress": "jane@example.com",
                        "active": True,
                    },
                ],
            },
            status=200,
        )

        result = runner.invoke(main, ["watchers","PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["count"] == 2
        assert data["data"]["is_watching"] is True
        assert len(data["data"]["watchers"]) == 2
        assert data["data"]["watchers"][0]["name"] == "jdoe"
        assert data["data"]["watchers"][0]["display_name"] == "John Doe"


class TestCLIIssueWatch:
    """Tests for 'jira watch' command."""

    @responses.activate
    def test_watch_with_user(self, runner, mock_env):
        """Should add specified user as watcher."""
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        result = runner.invoke(main, ["watch","PROJ-123", "--user", "jdoe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["issue_key"] == "PROJ-123"
        assert data["data"]["user"] == "jdoe"
        assert data["data"]["action"] == "added"

    @responses.activate
    def test_watch_current_user(self, runner, mock_env):
        """Should add current user as watcher when no --user specified."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/myself",
            json={"name": "currentuser", "displayName": "Current User"},
            status=200,
        )
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        result = runner.invoke(main, ["watch","PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["user"] == "currentuser"


class TestCLIIssueUnwatch:
    """Tests for 'jira unwatch' command."""

    @responses.activate
    def test_unwatch_with_user(self, runner, mock_env):
        """Should remove specified user as watcher."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        result = runner.invoke(main, ["unwatch","PROJ-123", "--user", "jdoe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["issue_key"] == "PROJ-123"
        assert data["data"]["user"] == "jdoe"
        assert data["data"]["action"] == "removed"

    @responses.activate
    def test_unwatch_current_user(self, runner, mock_env):
        """Should remove current user as watcher when no --user specified."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/myself",
            json={"name": "currentuser", "displayName": "Current User"},
            status=200,
        )
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/watchers",
            status=204,
        )

        result = runner.invoke(main, ["unwatch","PROJ-123"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["user"] == "currentuser"


class TestCLIEditCommentVisibility:
    """Tests for 'jira edit-comment' with --visibility."""

    @responses.activate
    def test_edit_comment_with_visibility(self, runner, mock_env):
        """Should edit comment with visibility restriction."""
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment/456",
            json={
                "id": "456",
                "body": "Updated text",
                "author": {"displayName": "User"},
                "updated": "2024-01-15T12:00:00.000+0000",
                "visibility": {"type": "role", "value": "Developers"},
            },
            status=200,
        )

        result = runner.invoke(
            main, ["edit-comment", "PROJ-123", "456", "Updated text", "--visibility", "role:Developers"]
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["comment"]["visibility"]["type"] == "role"
        assert data["data"]["comment"]["visibility"]["value"] == "Developers"

        request_body = json.loads(responses.calls[0].request.body)
        assert request_body["visibility"] == {"type": "role", "value": "Developers"}

    @responses.activate
    def test_edit_comment_without_visibility(self, runner, mock_env):
        """Should not send visibility when option not provided."""
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/PROJ-123/comment/456",
            json={
                "id": "456",
                "body": "Updated text",
                "author": {"displayName": "User"},
                "updated": "2024-01-15T12:00:00.000+0000",
            },
            status=200,
        )

        result = runner.invoke(main, ["edit-comment", "PROJ-123", "456", "Updated text"])

        assert result.exit_code == 0
        request_body = json.loads(responses.calls[0].request.body)
        assert "visibility" not in request_body


class TestCLIAttachmentDelete:
    """Tests for 'jira attachment delete' command."""

    @responses.activate
    def test_delete_attachment(self, runner, mock_env):
        """Should delete attachment."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/attachment/12345",
            status=204,
        )

        result = runner.invoke(main, ["attachment", "delete", "12345"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["attachment_id"] == "12345"
        assert data["data"]["deleted"] is True

    @responses.activate
    def test_delete_attachment_not_found(self, runner, mock_env):
        """Should return error for non-existent attachment."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/attachment/99999",
            json={"errorMessages": ["Attachment not found"]},
            status=404,
        )

        result = runner.invoke(main, ["attachment", "delete", "99999"])

        assert result.exit_code != 0


class TestCLIUserSearch:
    """Tests for 'jira users' command."""

    @responses.activate
    def test_search_users(self, runner, mock_env):
        """Should search and return users."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/user/search",
            json=[
                {"name": "jdoe", "displayName": "John Doe", "emailAddress": "jdoe@example.com", "active": True},
                {"name": "jsmith", "displayName": "Jane Smith", "emailAddress": "jsmith@example.com", "active": True},
            ],
            status=200,
        )

        result = runner.invoke(main, ["users", "j"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["query"] == "j"
        assert data["data"]["total"] == 2
        assert data["data"]["users"][0]["name"] == "jdoe"
        assert data["data"]["users"][0]["display_name"] == "John Doe"
        assert data["data"]["users"][0]["email"] == "jdoe@example.com"

    @responses.activate
    def test_search_users_with_limit(self, runner, mock_env):
        """Should respect --limit option."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/user/search",
            json=[],
            status=200,
        )

        result = runner.invoke(main, ["users", "test", "--limit", "5"])

        assert result.exit_code == 0
        url = responses.calls[0].request.url
        assert "maxResults=5" in url


class TestCLIIssueTypes:
    """Tests for 'jira issue-types' command."""

    @responses.activate
    def test_list_all_issue_types(self, runner, mock_env):
        """Should list all server issue types."""
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/issuetype",
            json=[
                {"id": "1", "name": "Bug", "subtask": False, "description": "A bug"},
                {"id": "2", "name": "Task", "subtask": False},
                {"id": "3", "name": "Sub-task", "subtask": True},
            ],
            status=200,
        )

        result = runner.invoke(main, ["issue-types"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["total"] == 3
        assert data["data"]["issue_types"][0]["name"] == "Bug"
        assert data["data"]["issue_types"][0]["description"] == "A bug"
        assert data["data"]["issue_types"][2]["subtask"] is True
        assert "project_key" not in data["data"]

    @responses.activate
    def test_list_project_issue_types(self, runner, mock_env):
        """Should list project-specific issue types."""
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

        result = runner.invoke(main, ["issue-types", "PROJ"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["project_key"] == "PROJ"
        assert data["data"]["total"] == 2


class TestCLIDeleteSubtask:
    """Tests for 'jira delete-subtask' command."""

    @responses.activate
    def test_delete_subtask(self, runner, mock_env):
        """Should delete a subtask."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-124",
            status=204,
        )

        result = runner.invoke(main, ["delete-subtask", "PROJ-124"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["issue_key"] == "PROJ-124"
        assert data["data"]["deleted"] is True

    @responses.activate
    def test_delete_subtask_not_found(self, runner, mock_env):
        """Should return error for non-existent subtask."""
        responses.add(
            responses.DELETE,
            "https://jira.example.com/rest/api/2/issue/PROJ-999",
            json={"errorMessages": ["Issue Does Not Exist"]},
            status=404,
        )

        result = runner.invoke(main, ["delete-subtask", "PROJ-999"])

        assert result.exit_code != 0
