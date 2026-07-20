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
    apply_review_prefix,
    get_review_data,
    normalize_review_comments,
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
    def test_post_review_nested_dict_format(self, mock_client_class):
        """Test posting a review with Gerrit REST format comments."""
        mock_client = MagicMock()
        mock_client.rest.post.return_value = {}
        mock_client_class.return_value = mock_client

        reviewer = CodeReviewer()
        result = reviewer.post_review(
            change_number=123,
            comments={
                "test.py": [
                    {"line": 10, "message": "Fix this", "unresolved": True},
                    {"line": 20, "message": "And this"},
                ],
                "/COMMIT_MSG": [
                    {"line": 7, "message": "Typo"},
                ],
            },
        )

        assert result.success is True
        assert result.comments_posted == 3

        json_data = mock_client.rest.post.call_args[1]["json"]
        assert json_data["comments"]["test.py"] == [
            {"message": "Fix this", "unresolved": True, "line": 10},
            {"message": "And this", "unresolved": True, "line": 20},
        ]
        assert json_data["comments"]["/COMMIT_MSG"][0]["line"] == 7

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_post_review_prefix_and_tag(self, mock_client_class):
        """Test that prefix is applied and tag is passed through."""
        mock_client = MagicMock()
        mock_client.rest.post.return_value = {}
        mock_client_class.return_value = mock_client

        reviewer = CodeReviewer()
        result = reviewer.post_review(
            change_number=123,
            comments=[
                {"path": "test.py", "line": 10, "message": "Fix this"},
            ],
            message="Overall message",
            prefix="[Marc Bot]",
            tag="autogenerated:ai-review",
        )

        assert result.success is True

        json_data = mock_client.rest.post.call_args[1]["json"]
        assert json_data["message"] == "**[Marc Bot]**\n\nOverall message"
        assert json_data["tag"] == "autogenerated:ai-review"
        comment = json_data["comments"]["test.py"][0]
        assert comment["message"] == "**[Marc Bot]**\n\nFix this"

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_post_review_range_comment(self, mock_client_class):
        """Test that a range comment passes the range through."""
        mock_client = MagicMock()
        mock_client.rest.post.return_value = {}
        mock_client_class.return_value = mock_client

        comment_range = {
            "start_line": 10, "start_character": 0,
            "end_line": 12, "end_character": 5,
        }
        reviewer = CodeReviewer()
        result = reviewer.post_review(
            change_number=123,
            comments=[
                {"path": "test.py", "range": comment_range, "message": "Span"},
            ],
        )

        assert result.success is True
        comment = mock_client.rest.post.call_args[1]["json"]["comments"]["test.py"][0]
        assert comment["range"] == comment_range
        assert "line" not in comment

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_post_review_side_passthrough(self, mock_client_class):
        """Test that a PARENT-side comment keeps its side field."""
        mock_client = MagicMock()
        mock_client.rest.post.return_value = {}
        mock_client_class.return_value = mock_client

        reviewer = CodeReviewer()
        result = reviewer.post_review(
            change_number=123,
            comments=[
                {"path": "test.py", "line": 5, "message": "Old line",
                 "side": "PARENT"},
                {"path": "test.py", "line": 6, "message": "New line"},
            ],
        )

        assert result.success is True
        comments = mock_client.rest.post.call_args[1]["json"]["comments"]["test.py"]
        assert comments[0]["side"] == "PARENT"
        assert "side" not in comments[1]

    @patch("gerrit_cli.reviewer.GerritCommentsClient")
    def test_post_review_file_level_comment(self, mock_client_class):
        """Test that a comment without a line posts as file-level."""
        mock_client = MagicMock()
        mock_client.rest.post.return_value = {}
        mock_client_class.return_value = mock_client

        reviewer = CodeReviewer()
        result = reviewer.post_review(
            change_number=123,
            comments=[
                {"path": "test.py", "message": "File-level note"},
            ],
        )

        assert result.success is True
        comment = mock_client.rest.post.call_args[1]["json"]["comments"]["test.py"][0]
        assert "line" not in comment
        assert comment["message"] == "File-level note"

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


class TestNormalizeReviewComments:
    """Tests for normalize_review_comments."""

    def test_flat_list_passthrough(self):
        comments = [{"path": "a.c", "line": 1, "message": "x"}]
        assert normalize_review_comments(comments) == comments

    def test_nested_dict_flattened(self):
        result = normalize_review_comments({
            "a.c": [{"line": 1, "message": "x"}, {"line": 2, "message": "y"}],
            "b.c": [{"line": 3, "message": "z", "unresolved": False}],
        })
        assert result == [
            {"path": "a.c", "line": 1, "message": "x"},
            {"path": "a.c", "line": 2, "message": "y"},
            {"path": "b.c", "line": 3, "message": "z", "unresolved": False},
        ]

    def test_empty_inputs(self):
        assert normalize_review_comments(None) == []
        assert normalize_review_comments([]) == []
        assert normalize_review_comments({}) == []

    def test_outer_path_key_authoritative(self):
        """A stray 'path' inside a dict-format entry must not win."""
        result = normalize_review_comments({
            "a.c": [{"path": "b.c", "line": 5, "message": "x"}],
        })
        assert result == [{"path": "a.c", "line": 5, "message": "x"}]


class TestApplyReviewPrefix:
    """Tests for apply_review_prefix."""

    def test_bold_own_paragraph(self):
        assert apply_review_prefix("[Marc Bot]", "Fix this") == (
            "**[Marc Bot]**\n\nFix this")

    def test_no_prefix_unchanged(self):
        assert apply_review_prefix(None, "Fix this") == "Fix this"
        assert apply_review_prefix("", "Fix this") == "Fix this"
        assert apply_review_prefix("   ", "Fix this") == "Fix this"

    def test_block_markdown_survives(self):
        fenced = "```c\nfoo(x);\n```\nThis can deadlock."
        assert apply_review_prefix("[Bot]", fenced) == f"**[Bot]**\n\n{fenced}"


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
