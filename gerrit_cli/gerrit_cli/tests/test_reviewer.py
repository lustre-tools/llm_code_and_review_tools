"""Tests for the reviewer module."""

from unittest.mock import MagicMock, patch

from gerrit_cli.models import Author, ChangeInfo
from gerrit_cli.reviewer import (
    CodeReviewer,
    DiffHunk,
    DiffLine,
    FileChange,
    ReviewData,
    ReviewResult,
    get_review_data,
    post_review,
)


class TestDiffLine:
    """Tests for DiffLine dataclass."""

    def test_format_added_line(self):
        """Test formatting an added line."""
        line = DiffLine(
            line_number_old=None,
            line_number_new=42,
            content="new code here",
            type="added",
        )
        assert line.format() == "+new code here"

    def test_format_deleted_line(self):
        """Test formatting a deleted line."""
        line = DiffLine(
            line_number_old=10,
            line_number_new=None,
            content="old code here",
            type="deleted",
        )
        assert line.format() == "-old code here"

    def test_format_context_line(self):
        """Test formatting a context line."""
        line = DiffLine(
            line_number_old=5,
            line_number_new=5,
            content="unchanged code",
            type="context",
        )
        assert line.format() == " unchanged code"


class TestDiffHunk:
    """Tests for DiffHunk dataclass."""

    def test_format_hunk(self):
        """Test formatting a diff hunk."""
        hunk = DiffHunk(
            old_start=10,
            old_count=3,
            new_start=10,
            new_count=4,
            lines=[
                DiffLine(10, 10, "context", "context"),
                DiffLine(11, None, "deleted", "deleted"),
                DiffLine(None, 11, "added1", "added"),
                DiffLine(None, 12, "added2", "added"),
                DiffLine(12, 13, "context2", "context"),
            ],
        )
        formatted = hunk.format()
        assert "@@ -10,3 +10,4 @@" in formatted
        assert "context" in formatted
        assert "deleted" in formatted
        assert "added1" in formatted


class TestFileChange:
    """Tests for FileChange dataclass."""

    def test_format_diff(self):
        """Test formatting a file diff."""
        file_change = FileChange(
            path="test.py",
            status="M",
            old_path=None,
            lines_added=2,
            lines_deleted=1,
            size_delta=10,
            hunks=[
                DiffHunk(
                    old_start=1,
                    old_count=2,
                    new_start=1,
                    new_count=3,
                    lines=[
                        DiffLine(1, 1, "line1", "context"),
                        DiffLine(2, None, "old", "deleted"),
                        DiffLine(None, 2, "new1", "added"),
                        DiffLine(None, 3, "new2", "added"),
                    ],
                )
            ],
        )
        formatted = file_change.format_diff()
        assert "--- a/test.py" in formatted
        assert "+++ b/test.py" in formatted

    def test_to_dict(self):
        """Test converting FileChange to dict."""
        file_change = FileChange(
            path="test.py",
            status="A",
            old_path=None,
            lines_added=10,
            lines_deleted=0,
            size_delta=100,
        )
        result = file_change.to_dict()
        assert result["path"] == "test.py"
        assert result["status"] == "A"
        assert result["lines_added"] == 10


class TestReviewData:
    """Tests for ReviewData dataclass."""

    def test_format_for_review(self):
        """Test formatting review data."""
        change_info = ChangeInfo(
            change_id="test~123",
            change_number=123,
            project="test/project",
            branch="master",
            subject="Test change",
            status="NEW",
            current_revision="abc123",
            owner=Author(name="Owner"),
            url="https://example.com/123",
        )
        files = [
            FileChange(
                path="file.py",
                status="M",
                old_path=None,
                lines_added=5,
                lines_deleted=2,
                size_delta=30,
            )
        ]
        review_data = ReviewData(
            change_info=change_info,
            files=files,
            commit_message="Test commit\n\nAdd feature X",
            parent_commit="parent123",
        )

        formatted = review_data.format_for_review()
        assert "Test change" in formatted
        assert "test/project" in formatted
        assert "Owner" in formatted
        assert "file.py" in formatted
        assert "modified" in formatted

    def test_to_dict(self):
        """Test converting ReviewData to dict."""
        change_info = ChangeInfo(
            change_id="test~123",
            change_number=123,
            project="test",
            branch="master",
            subject="Test",
            status="NEW",
            current_revision="abc",
            owner=Author(name="Owner"),
            url="https://example.com/123",
        )
        review_data = ReviewData(
            change_info=change_info,
            files=[],
            commit_message="Test",
            parent_commit="parent",
        )
        result = review_data.to_dict()
        assert result["change_info"]["change_number"] == 123
        assert result["commit_message"] == "Test"


class TestReviewResult:
    """Tests for ReviewResult dataclass."""

    def test_success_result(self):
        """Test successful review result."""
        result = ReviewResult(
            success=True,
            change_number=123,
            comments_posted=3,
            message="LGTM",
            vote=1,
        )
        assert result.success is True
        assert result.error is None

    def test_failure_result(self):
        """Test failed review result."""
        result = ReviewResult(
            success=False,
            change_number=123,
            comments_posted=0,
            message=None,
            vote=None,
            error="Permission denied",
        )
        assert result.success is False
        assert "Permission denied" in result.error


class TestCodeReviewer:
    """Tests for CodeReviewer."""

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_get_review_data(self, mock_client_class):
        """Test getting review data."""
        mock_client = MagicMock()
        mock_client.url = "https://example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"

        # Mock change details
        mock_client.rest.get.side_effect = [
            # First call: get change details
            {
                "id": "test~123",
                "project": "test",
                "branch": "master",
                "subject": "Test change",
                "status": "NEW",
                "current_revision": "abc123",
                "owner": {"name": "Owner"},
                "revisions": {
                    "abc123": {
                        "_number": 1,
                        "commit": {
                            "message": "Test commit",
                            "parents": [{"commit": "parent"}],
                        },
                    }
                },
            },
            # Second call: get files
            {
                "test.py": {
                    "status": "M",
                    "lines_inserted": 5,
                    "lines_deleted": 2,
                },
            },
            # Third call: get diff
            {
                "content": [
                    {"ab": ["line1", "line2"]},
                    {"a": ["old"], "b": ["new"]},
                ],
            },
        ]
        mock_client_class.return_value = mock_client
        mock_client_class.parse_gerrit_url.return_value = ("https://example.com", 123)

        reviewer = CodeReviewer()
        result = reviewer.get_review_data("https://example.com/123")

        assert result.change_info.change_number == 123
        assert len(result.files) == 1
        assert result.files[0].path == "test.py"

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_post_review_success(self, mock_client_class):
        """Test posting a review."""
        mock_client = MagicMock()
        mock_client.rest.post.return_value = {}
        mock_client_class.return_value = mock_client

        reviewer = CodeReviewer()
        result = reviewer.post_review(
            change_number=123,
            comments=[
                {"path": "test.py", "line": 10, "message": "Fix this"},
            ],
            message="Please address",
            vote=-1,
        )

        assert result.success is True
        assert result.comments_posted == 1
        assert result.vote == -1

        # Verify API call
        mock_client.rest.post.assert_called_once()
        call_args = mock_client.rest.post.call_args
        assert "/changes/123/revisions/current/review" in call_args[0][0]
        json_data = call_args[1]["json"]
        assert json_data["message"] == "Please address"
        assert json_data["labels"]["Code-Review"] == -1
        assert "test.py" in json_data["comments"]

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_post_review_failure(self, mock_client_class):
        """Test posting review with failure."""
        mock_client = MagicMock()
        mock_client.rest.post.side_effect = Exception("API error")
        mock_client_class.return_value = mock_client

        reviewer = CodeReviewer()
        result = reviewer.post_review(
            change_number=123,
            message="Test",
        )

        assert result.success is False
        assert "API error" in result.error

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_post_comment(self, mock_client_class):
        """Test posting a single comment."""
        mock_client = MagicMock()
        mock_client.rest.post.return_value = {}
        mock_client_class.return_value = mock_client

        reviewer = CodeReviewer()
        result = reviewer.post_comment(
            change_number=123,
            path="test.py",
            line=42,
            message="Consider using const",
        )

        assert result.success is True
        assert result.comments_posted == 1


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    @patch("gerrit_cli.reviewer.CodeReviewer")
    def test_get_review_data_function(self, mock_reviewer_class):
        """Test get_review_data convenience function."""
        mock_reviewer = MagicMock()
        mock_reviewer_class.return_value = mock_reviewer

        get_review_data("https://example.com/123", include_file_content=True)

        mock_reviewer.get_review_data.assert_called_once_with(
            "https://example.com/123", True
        )

    @patch("gerrit_cli.reviewer.CodeReviewer")
    def test_post_review_function(self, mock_reviewer_class):
        """Test post_review convenience function."""
        mock_reviewer = MagicMock()
        mock_reviewer_class.return_value = mock_reviewer

        post_review(
            change_number=123,
            comments=[{"path": "test.py", "line": 1, "message": "Fix"}],
            message="Review",
            vote=1,
        )

        mock_reviewer.post_review.assert_called_once()


class TestDiffParsing:
    """Tests for diff parsing logic."""

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_parse_diff_with_context(self, mock_client_class):
        """Test parsing diff with context lines."""
        mock_client = MagicMock()
        mock_client.url = "https://example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.rest.get.side_effect = [
            # Change details
            {
                "id": "test~123",
                "project": "test",
                "branch": "master",
                "subject": "Test",
                "status": "NEW",
                "current_revision": "abc",
                "owner": {"name": "Owner"},
                "revisions": {"abc": {"commit": {"message": "Test", "parents": []}}},
            },
            # Files list
            {"test.py": {"status": "M", "lines_inserted": 1, "lines_deleted": 1}},
            # Diff
            {
                "content": [
                    {"ab": ["context1", "context2"]},
                    {"a": ["deleted_line"], "b": ["added_line"]},
                    {"ab": ["context3"]},
                ]
            },
        ]
        mock_client_class.return_value = mock_client
        mock_client_class.parse_gerrit_url.return_value = ("https://example.com", 123)

        reviewer = CodeReviewer()
        result = reviewer.get_review_data("https://example.com/123")

        assert len(result.files) == 1
        file_change = result.files[0]
        assert len(file_change.hunks) == 1

        hunk = file_change.hunks[0]
        # Should have: 2 context + 1 deleted + 1 added + 1 context = 5 lines
        assert len(hunk.lines) == 5

        # Check line types
        types = [line.type for line in hunk.lines]
        assert types == ["context", "context", "deleted", "added", "context"]
