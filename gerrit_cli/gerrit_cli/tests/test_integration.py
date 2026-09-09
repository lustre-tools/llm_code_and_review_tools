"""Integration tests that test against the real Gerrit server.

These tests require network access and use the real credentials.
They are marked with pytest.mark.integration and can be skipped with:
    pytest -m "not integration"
"""

import pytest

from gerrit_cli import (
    CommentExtractor,
    GerritCommentsClient,
    extract_comments,
)

pytestmark = pytest.mark.integration


class TestRealGerritExtraction:
    """Integration tests for comment extraction against real Gerrit."""

    def test_extract_from_change_61965(self):
        """Test extracting comments from change 61965 (many comments)."""
        extractor = CommentExtractor()
        result = extractor.extract_from_change(
            61965,
            include_resolved=True,
            include_code_context=False,
        )

        # Verify we got the change info
        assert result.change_info.change_number == 61965
        assert result.change_info.project == "fs/lustre-release"
        assert "LU-12187" in result.change_info.subject

        # This change should have many comments
        assert result.total_count > 0
        print(f"Total comments: {result.total_count}")
        print(f"Total threads: {len(result.threads)}")
        print(f"Unresolved threads: {result.unresolved_count}")

    def test_extract_from_change_61965_with_context(self):
        """Test extracting with code context from change 61965."""
        extractor = CommentExtractor()
        result = extractor.extract_from_change(
            61965,
            include_resolved=True,
            include_code_context=True,
            context_lines=2,
        )

        # Find a thread with code context
        threads_with_context = [
            t for t in result.threads
            if t.root_comment.code_context is not None
        ]

        print(f"Threads with code context: {len(threads_with_context)}")

        # We should have some threads with context
        # (not all will have it - virtual files like /COMMIT_MSG won't)
        if threads_with_context:
            thread = threads_with_context[0]
            ctx = thread.root_comment.code_context
            print(f"Sample context from {thread.root_comment.file_path}:{thread.root_comment.line}")
            print(ctx.format())

    def test_extract_from_url(self):
        """Test extracting from URL."""
        result = extract_comments(
            "https://review.whamcloud.com/c/fs/lustre-release/+/61965",
            include_resolved=False,
            include_code_context=False,
        )

        assert result.change_info.change_number == 61965

    def test_to_dict_serialization(self):
        """Test that extraction result can be serialized."""
        import json

        result = extract_comments(
            "https://review.whamcloud.com/c/fs/lustre-release/+/61965",
            include_resolved=False,
            include_code_context=False,
        )

        # Should be able to convert to dict and serialize to JSON
        data = result.to_dict()
        json_str = json.dumps(data, indent=2)

        # Should be valid JSON
        parsed = json.loads(json_str)
        assert parsed["change_info"]["change_number"] == 61965


class TestRealGerritClient:
    """Integration tests for the Gerrit client."""

    def test_parse_various_urls(self):
        """Test URL parsing with various formats."""
        urls = [
            ("https://review.whamcloud.com/c/fs/lustre-release/+/61965", 61965),
            ("https://review.whamcloud.com/c/fs/lustre-release/+/61965/5", 61965),
            ("https://review.whamcloud.com/61965", 61965),
            ("https://review.whamcloud.com/62796", 62796),
        ]

        for url, expected_number in urls:
            base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)
            assert change_number == expected_number
            assert base_url == "https://review.whamcloud.com"

    def test_get_change_detail(self):
        """Test getting change details."""
        client = GerritCommentsClient()
        change = client.get_change_detail(61965)

        assert change["project"] == "fs/lustre-release"
        assert "revisions" in change

    def test_get_comments(self):
        """Test getting comments."""
        client = GerritCommentsClient()
        comments = client.get_comments(61965)

        # Should be a dict with file paths as keys
        assert isinstance(comments, dict)
        assert len(comments) > 0

        # Check structure of comments
        for _file_path, file_comments in comments.items():
            assert isinstance(file_comments, list)
            if file_comments:
                comment = file_comments[0]
                assert "id" in comment
                assert "message" in comment
                assert "author" in comment


class TestPrintFormatting:
    """Tests that print formatted output for manual verification."""

    def test_print_summary(self):
        """Print a formatted summary of extracted comments."""
        result = extract_comments(
            "https://review.whamcloud.com/c/fs/lustre-release/+/61965",
            include_resolved=False,
            include_code_context=True,
            context_lines=2,
        )

        print("\n" + "=" * 60)
        print(result.format_summary())
        print("=" * 60)
