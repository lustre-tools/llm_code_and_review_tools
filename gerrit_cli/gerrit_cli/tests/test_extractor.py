"""Tests for the extractor module."""

from unittest.mock import MagicMock, patch

from gerrit_cli.extractor import CommentExtractor, extract_comments
from gerrit_cli.models import Author, Comment


class TestCommentExtractor:
    """Tests for CommentExtractor."""

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_extract_from_change_basic(self, mock_client_class):
        """Test basic comment extraction."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://review.example.com/c/test/+/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test change",
            "status": "NEW",
            "current_revision": "abc123",
            "owner": {"name": "Owner", "_account_id": 1},
            "revisions": {"abc123": {"_number": 1}},
        }
        mock_client.get_comments.return_value = {
            "test.py": [
                {
                    "id": "comment1",
                    "patch_set": 1,
                    "line": 10,
                    "message": "Fix this",
                    "author": {"name": "Reviewer"},
                    "unresolved": True,
                    "updated": "2025-01-01",
                }
            ]
        }
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(123, include_code_context=False)

        assert result.change_info.change_number == 123
        assert result.change_info.project == "test"
        assert result.total_count == 1
        assert result.unresolved_count == 1
        assert len(result.threads) == 1
        assert result.threads[0].root_comment.message == "Fix this"

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_extract_filters_resolved_by_default(self, mock_client_class):
        """Test that resolved threads are filtered by default."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {},
        }
        mock_client.get_comments.return_value = {
            "test.py": [
                {
                    "id": "resolved",
                    "patch_set": 1,
                    "line": 10,
                    "message": "Good job",
                    "author": {"name": "R"},
                    "unresolved": False,
                    "updated": "2025-01-01",
                },
                {
                    "id": "unresolved",
                    "patch_set": 1,
                    "line": 20,
                    "message": "Fix this",
                    "author": {"name": "R"},
                    "unresolved": True,
                    "updated": "2025-01-01",
                },
            ]
        }
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(123, include_code_context=False)

        assert len(result.threads) == 1
        assert result.threads[0].root_comment.id == "unresolved"

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_extract_include_resolved(self, mock_client_class):
        """Test including resolved threads."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {},
        }
        mock_client.get_comments.return_value = {
            "test.py": [
                {
                    "id": "resolved",
                    "patch_set": 1,
                    "line": 10,
                    "message": "Good job",
                    "author": {"name": "R"},
                    "unresolved": False,
                    "updated": "2025-01-01",
                },
                {
                    "id": "unresolved",
                    "patch_set": 1,
                    "line": 20,
                    "message": "Fix this",
                    "author": {"name": "R"},
                    "unresolved": True,
                    "updated": "2025-01-01",
                },
            ]
        }
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(
            123, include_resolved=True, include_code_context=False
        )

        assert len(result.threads) == 2

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_extract_organizes_threads(self, mock_client_class):
        """Test that comments are organized into threads."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {},
        }
        mock_client.get_comments.return_value = {
            "test.py": [
                {
                    "id": "root",
                    "patch_set": 1,
                    "line": 10,
                    "message": "Fix this",
                    "author": {"name": "Reviewer"},
                    "unresolved": True,
                    "updated": "2025-01-01 10:00:00",
                },
                {
                    "id": "reply1",
                    "patch_set": 1,
                    "line": 10,
                    "message": "Working on it",
                    "author": {"name": "Author"},
                    "unresolved": True,
                    "updated": "2025-01-01 11:00:00",
                    "in_reply_to": "root",
                },
                {
                    "id": "reply2",
                    "patch_set": 1,
                    "line": 10,
                    "message": "Done",
                    "author": {"name": "Author"},
                    "unresolved": False,
                    "updated": "2025-01-01 12:00:00",
                    "in_reply_to": "reply1",
                },
            ]
        }
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(
            123, include_resolved=True, include_code_context=False
        )

        assert len(result.threads) == 1
        thread = result.threads[0]
        assert thread.root_comment.id == "root"
        assert len(thread.replies) == 2
        assert thread.replies[0].id == "reply1"
        assert thread.replies[1].id == "reply2"
        assert thread.is_resolved is True

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_extract_with_code_context(self, mock_client_class):
        """Test extracting with code context."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc123",
            "owner": {"name": "Owner"},
            "revisions": {"abc123": {"_number": 1}},
        }
        mock_client.get_comments.return_value = {
            "test.py": [
                {
                    "id": "comment1",
                    "patch_set": 1,
                    "line": 3,
                    "message": "Fix this",
                    "author": {"name": "Reviewer"},
                    "unresolved": True,
                    "updated": "2025-01-01",
                }
            ]
        }
        mock_client.get_revision_for_patchset.return_value = "abc123"
        mock_client.get_file_diff.return_value = {
            "content": [
                {"b": ["line1", "line2", "line3", "line4", "line5"]}
            ]
        }
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(
            123, include_code_context=True, context_lines=2
        )

        assert len(result.threads) == 1
        comment = result.threads[0].root_comment
        assert comment.code_context is not None
        assert comment.code_context.target_line == 3
        assert len(comment.code_context.lines) == 5  # lines 1-5

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_extract_skips_context_for_virtual_files(self, mock_client_class):
        """Test that virtual files like /COMMIT_MSG don't get context."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {},
        }
        mock_client.get_comments.return_value = {
            "/COMMIT_MSG": [
                {
                    "id": "comment1",
                    "patch_set": 1,
                    "line": 10,
                    "message": "Fix commit message",
                    "author": {"name": "Reviewer"},
                    "unresolved": True,
                    "updated": "2025-01-01",
                }
            ]
        }
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(123, include_code_context=True)

        comment = result.threads[0].root_comment
        assert comment.code_context is None
        # get_file_diff should not have been called
        mock_client.get_file_diff.assert_not_called()

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_extract_from_url(self, mock_client_class):
        """Test extracting from URL."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {},
        }
        mock_client.get_comments.return_value = {}
        mock_client_class.return_value = mock_client
        mock_client_class.parse_gerrit_url.return_value = ("https://review.example.com", 123)

        extractor = CommentExtractor()
        result = extractor.extract_from_url(
            "https://review.example.com/c/test/+/123",
            include_code_context=False,
        )

        assert result.change_info.change_number == 123


class TestExtractCommentsFunction:
    """Tests for the extract_comments convenience function."""

    @patch("gerrit_cli.extractor.CommentExtractor")
    def test_extract_comments_function(self, mock_extractor_class):
        """Test the convenience function."""
        mock_extractor = MagicMock()
        mock_extractor_class.return_value = mock_extractor

        extract_comments(
            "https://example.com/123",
            include_resolved=True,
            include_code_context=False,
            context_lines=5,
        )

        mock_extractor.extract_from_url.assert_called_once_with(
            url="https://example.com/123",
            include_resolved=True,
            include_code_context=False,
            context_lines=5,
            exclude_ci_bots=True,
            exclude_lint_bots=False,
            include_system=False,
        )


class TestMessageFiltering:
    """Tests for review message filtering logic."""

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_filters_empty_comment_only_messages(self, mock_client_class):
        """Test that messages with only '(N comments)' are filtered."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {"abc": {"_number": 8}},
        }
        mock_client.get_comments.return_value = {}
        mock_client.get_messages.return_value = [
            {
                "id": "msg1",
                "author": {"name": "Reviewer"},
                "message": "Patch Set 8:\n\n(1 comment)",
                "_revision_number": 8,
                "date": "2025-01-01",
            },
            {
                "id": "msg2",
                "author": {"name": "Reviewer"},
                "message": "Patch Set 8: Code-Review+1\n\n(1 comment)",
                "_revision_number": 8,
                "date": "2025-01-01",
            },
            {
                "id": "msg3",
                "author": {"name": "Reviewer"},
                "message": "Patch Set 8: Code-Review+1\n\nThis is a real comment!",
                "_revision_number": 8,
                "date": "2025-01-01",
            },
        ]
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(123, include_code_context=False)

        # Only the message with real content should be included
        assert len(result.review_messages) == 1
        assert "This is a real comment!" in result.review_messages[0].message

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_filters_multiple_comments_format(self, mock_client_class):
        """Test filtering messages with '(N comments)' plural."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {"abc": {"_number": 11}},
        }
        mock_client.get_comments.return_value = {}
        mock_client.get_messages.return_value = [
            {
                "id": "msg1",
                "author": {"name": "Reviewer"},
                "message": "Patch Set 11: Code-Review-1\n\n(5 comments)",
                "_revision_number": 11,
                "date": "2025-01-01",
            },
        ]
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(123, include_code_context=False)

        # Message with only "(N comments)" should be filtered
        assert len(result.review_messages) == 0

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_keeps_substantive_messages_with_comments_suffix(self, mock_client_class):
        """Test that substantive messages with '(N comments)' suffix are kept."""
        mock_client = MagicMock()
        mock_client.url = "https://review.example.com"
        mock_client.format_change_url.return_value = "https://example.com/123"
        mock_client.get_change_detail.return_value = {
            "id": "test~123",
            "project": "test",
            "branch": "master",
            "subject": "Test",
            "status": "NEW",
            "current_revision": "abc",
            "owner": {"name": "Owner"},
            "revisions": {"abc": {"_number": 11}},
        }
        mock_client.get_comments.return_value = {}
        mock_client.get_messages.return_value = [
            {
                "id": "msg1",
                "author": {"name": "Reviewer"},
                "message": "Patch Set 11: Code-Review+1\n\nLooks good overall, just a few minor nits.\n\n(3 comments)",
                "_revision_number": 11,
                "date": "2025-01-01",
            },
        ]
        mock_client_class.return_value = mock_client

        extractor = CommentExtractor()
        result = extractor.extract_from_change(123, include_code_context=False)

        # Message with real content should be kept
        assert len(result.review_messages) == 1
        assert "Looks good overall" in result.review_messages[0].message


class TestThreadOrganization:
    """Tests for comment thread organization logic."""

    def test_find_root_id_direct_reply(self):
        """Test finding root for a direct reply."""
        extractor = CommentExtractor.__new__(CommentExtractor)

        root = Comment(
            id="root",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Root",
            author=Author(name="A"),
            unresolved=True,
            updated="2025-01-01",
        )
        reply = Comment(
            id="reply",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Reply",
            author=Author(name="B"),
            unresolved=True,
            updated="2025-01-02",
            in_reply_to="root",
        )

        comments_by_id = {root.id: root, reply.id: reply}
        root_id = extractor._find_root_id(reply, comments_by_id)

        assert root_id == "root"

    def test_find_root_id_nested_reply(self):
        """Test finding root for a nested reply chain."""
        extractor = CommentExtractor.__new__(CommentExtractor)

        root = Comment(
            id="root",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Root",
            author=Author(name="A"),
            unresolved=True,
            updated="2025-01-01",
        )
        reply1 = Comment(
            id="reply1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Reply 1",
            author=Author(name="B"),
            unresolved=True,
            updated="2025-01-02",
            in_reply_to="root",
        )
        reply2 = Comment(
            id="reply2",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Reply 2",
            author=Author(name="A"),
            unresolved=True,
            updated="2025-01-03",
            in_reply_to="reply1",
        )

        comments_by_id = {c.id: c for c in [root, reply1, reply2]}
        root_id = extractor._find_root_id(reply2, comments_by_id)

        assert root_id == "root"


def _change_with_mixed_threads():
    """a.c:1 resolved, a.c:5 open, /PATCHSET_LEVEL resolved, b.c:3 open."""
    client = MagicMock()
    client.url = "https://review.example.com"
    client.format_change_url.return_value = "https://review.example.com/123"
    client.get_change_detail.return_value = {
        "id": "test~123", "project": "test", "branch": "master",
        "subject": "Test", "status": "NEW", "current_revision": "abc",
        "owner": {"name": "Owner"}, "revisions": {},
    }

    def comment(cid, line, unresolved):
        return {"id": cid, "patch_set": 1, "line": line, "message": cid,
                "author": {"name": "R"}, "unresolved": unresolved,
                "updated": "2025-01-01"}

    client.get_comments.return_value = {
        "a.c": [comment("a1-resolved", 1, False), comment("a5-open", 5, True)],
        "/PATCHSET_LEVEL": [comment("ps-resolved", None, False)],
        "b.c": [comment("b3-open", 3, True)],
    }
    client.get_messages.return_value = []
    return client


class TestThreadIndicesAcrossViews:
    """An index taken from `comments --all` names the same thread, or none,
    in the default listing that reply, done, ack and stage index into."""

    @patch("gerrit_cli.extractor.GerritCommentsClient")
    def test_all_lists_the_default_threads_first(self, mock_client_class):
        mock_client_class.return_value = _change_with_mixed_threads()
        extractor = CommentExtractor()

        default = extractor.extract_from_change(123, include_code_context=False)
        everything = extractor.extract_from_change(
            123, include_resolved=True, include_code_context=False)

        ids = [t.root_comment.id for t in everything.threads]
        default_ids = [t.root_comment.id for t in default.threads]
        assert ids[:len(default_ids)] == default_ids
        assert ids[len(default_ids):] == ["a1-resolved"]
