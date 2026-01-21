"""
Integration tests for JIRA tool against a live server.

These tests require:
- JIRA_SERVER environment variable
- JIRA_TOKEN environment variable
- Optionally JIRA_TEST_PROJECT for the project to test against (default: LU)

Tests are read-only by default to avoid side effects.
Run with: pytest tests/integration/ -v -m integration

To run write tests (which may modify data):
    pytest tests/integration/ -v -m "integration and not readonly"
"""

import json
import os

import pytest
from click.testing import CliRunner

from jira_tool.cli import main
from jira_tool.client import JiraClient
from jira_tool.config import JiraConfig
from jira_tool.errors import NotFoundError


def get_test_config():
    """Get test configuration from environment."""
    server = os.environ.get("JIRA_SERVER")
    token = os.environ.get("JIRA_TOKEN")
    if not server or not token:
        return None
    return JiraConfig(server=server, token=token)


def get_test_project():
    """Get test project key from environment."""
    return os.environ.get("JIRA_TEST_PROJECT", "LU")


# Skip all tests if no config
requires_jira = pytest.mark.skipif(
    get_test_config() is None, reason="JIRA_SERVER and JIRA_TOKEN environment variables required"
)


@pytest.fixture
def config():
    """Get JIRA config, skip if not available."""
    cfg = get_test_config()
    if cfg is None:
        pytest.skip("JIRA configuration not available")
    return cfg


@pytest.fixture
def client(config):
    """Get JIRA client."""
    return JiraClient(config)


@pytest.fixture
def runner():
    """Get CLI runner."""
    return CliRunner()


@pytest.fixture
def project():
    """Get test project key."""
    return get_test_project()


@pytest.mark.integration
@requires_jira
class TestServerConnectivity:
    """Tests for basic server connectivity."""

    def test_server_info(self, client):
        """Should be able to get server info."""
        info = client.get_server_info()
        assert "version" in info
        assert "baseUrl" in info

    def test_cli_config_test(self, runner):
        """CLI config test should succeed."""
        result = runner.invoke(main, ["config", "test"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["connected"] is True


@pytest.mark.integration
@pytest.mark.readonly
@requires_jira
class TestIssueSearch:
    """Tests for issue search functionality."""

    def test_search_project(self, client, project):
        """Should be able to search within project."""
        result = client.search_issues(
            f"project = {project} ORDER BY created DESC",
            max_results=5,
        )
        assert "issues" in result
        assert "total" in result
        # Project should have at least some issues
        assert result["total"] > 0

    def test_search_returns_expected_fields(self, client, project):
        """Search results should include expected fields."""
        result = client.search_issues(
            f"project = {project} ORDER BY created DESC",
            max_results=1,
        )
        assert len(result["issues"]) >= 1
        issue = result["issues"][0]
        assert "key" in issue
        assert "fields" in issue
        assert "summary" in issue["fields"]

    def test_search_pagination(self, client, project):
        """Should support pagination."""
        # Get first page
        page1 = client.search_issues(
            f"project = {project} ORDER BY key ASC",
            max_results=2,
            start_at=0,
        )

        # Get second page
        page2 = client.search_issues(
            f"project = {project} ORDER BY key ASC",
            max_results=2,
            start_at=2,
        )

        if page1["total"] > 2:
            # Keys should be different between pages
            page1_keys = {i["key"] for i in page1["issues"]}
            page2_keys = {i["key"] for i in page2["issues"]}
            assert page1_keys.isdisjoint(page2_keys)

    def test_cli_search(self, runner, project):
        """CLI search should work."""
        result = runner.invoke(main, ["issue", "search", f"project = {project} ORDER BY created DESC", "--limit", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert "issues" in data["data"]
        assert "pagination" in data["data"]


@pytest.mark.integration
@pytest.mark.readonly
@requires_jira
class TestIssueGet:
    """Tests for getting individual issues."""

    def test_get_issue_from_search(self, client, project):
        """Should be able to get an issue found by search."""
        # First find an issue
        search = client.search_issues(
            f"project = {project} ORDER BY created DESC",
            max_results=1,
        )
        if not search["issues"]:
            pytest.skip("No issues found in project")

        issue_key = search["issues"][0]["key"]

        # Now get it directly
        issue = client.get_issue(issue_key)
        assert issue["key"] == issue_key
        assert "fields" in issue

    def test_get_issue_includes_core_fields(self, client, project):
        """Issue should include core fields."""
        search = client.search_issues(
            f"project = {project} ORDER BY created DESC",
            max_results=1,
        )
        if not search["issues"]:
            pytest.skip("No issues found in project")

        issue_key = search["issues"][0]["key"]
        issue = client.get_issue(issue_key)

        fields = issue["fields"]
        assert "summary" in fields
        assert "status" in fields
        assert "issuetype" in fields
        assert "created" in fields

    def test_get_nonexistent_issue(self, client, project):
        """Should raise error for nonexistent issue.

        Note: JIRA may return 404 (NotFoundError) or 401 (AuthError)
        depending on configuration - 401 prevents information disclosure
        about which issue keys exist.
        """
        from jira_tool.errors import AuthError

        with pytest.raises((NotFoundError, AuthError)):
            client.get_issue(f"{project}-99999999")

    def test_cli_get_issue(self, runner, project):
        """CLI issue get should work."""
        # Find an issue first
        search_result = runner.invoke(main, ["issue", "search", f"project = {project}", "--limit", "1"])
        search_data = json.loads(search_result.output)
        if not search_data["data"]["issues"]:
            pytest.skip("No issues found")

        issue_key = search_data["data"]["issues"][0]["key"]

        # Get it via CLI
        result = runner.invoke(main, ["issue", "get", issue_key])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["key"] == issue_key


@pytest.mark.integration
@pytest.mark.readonly
@requires_jira
class TestComments:
    """Tests for comment functionality."""

    def test_get_comments(self, client, project):
        """Should be able to get comments for an issue."""
        # Find an issue
        search = client.search_issues(
            f"project = {project} ORDER BY created DESC",
            max_results=1,
        )
        if not search["issues"]:
            pytest.skip("No issues found")

        issue_key = search["issues"][0]["key"]

        # Get comments (may be empty, that's OK)
        comments = client.get_comments(issue_key, max_results=5)
        assert "comments" in comments
        assert "total" in comments

    def test_comments_pagination(self, client, project):
        """Should support comment pagination."""
        search = client.search_issues(
            f"project = {project} ORDER BY created DESC",
            max_results=1,
        )
        if not search["issues"]:
            pytest.skip("No issues found")

        issue_key = search["issues"][0]["key"]

        # Get with pagination params
        comments = client.get_comments(issue_key, start_at=0, max_results=2)
        assert "comments" in comments

    def test_cli_comments(self, runner, project):
        """CLI comments should work."""
        search_result = runner.invoke(main, ["issue", "search", f"project = {project}", "--limit", "1"])
        search_data = json.loads(search_result.output)
        if not search_data["data"]["issues"]:
            pytest.skip("No issues found")

        issue_key = search_data["data"]["issues"][0]["key"]

        result = runner.invoke(main, ["issue", "comments", issue_key, "--limit", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert "comments" in data["data"] or "comments_summary" in data["data"]
        assert "pagination" in data["data"]


@pytest.mark.integration
@pytest.mark.readonly
@requires_jira
class TestTransitions:
    """Tests for transition functionality."""

    def test_get_transitions(self, client, project):
        """Should be able to get available transitions."""
        search = client.search_issues(
            f"project = {project} ORDER BY created DESC",
            max_results=1,
        )
        if not search["issues"]:
            pytest.skip("No issues found")

        issue_key = search["issues"][0]["key"]

        # Get transitions (may be empty depending on permissions)
        transitions = client.get_transitions(issue_key)
        assert "transitions" in transitions

    def test_cli_transitions(self, runner, project):
        """CLI transitions should work."""
        search_result = runner.invoke(main, ["issue", "search", f"project = {project}", "--limit", "1"])
        search_data = json.loads(search_result.output)
        if not search_data["data"]["issues"]:
            pytest.skip("No issues found")

        issue_key = search_data["data"]["issues"][0]["key"]

        result = runner.invoke(main, ["issue", "transitions", issue_key])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert "transitions" in data["data"]


@pytest.mark.integration
@requires_jira
class TestResponseEnvelope:
    """Tests for consistent response envelope format."""

    def test_success_envelope_structure(self, runner, project):
        """Success responses should have consistent structure."""
        result = runner.invoke(main, ["issue", "search", f"project = {project}", "--limit", "1"])
        data = json.loads(result.output)

        # Required envelope fields
        assert "ok" in data
        assert "data" in data
        assert "meta" in data

        # Meta fields
        assert data["meta"]["tool"] == "jira"
        assert "command" in data["meta"]
        assert "timestamp" in data["meta"]

    def test_error_envelope_structure(self, runner, project):
        """Error responses should have consistent structure."""
        result = runner.invoke(main, ["issue", "get", f"{project}-99999999"])
        data = json.loads(result.output)

        # Required envelope fields
        assert data["ok"] is False
        assert "error" in data
        assert "meta" in data

        # Error fields
        assert "code" in data["error"]
        assert "message" in data["error"]
