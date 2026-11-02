"""Tests for the replier module."""

from unittest.mock import MagicMock, patch

from gerrit_cli.models import (
    Author,
    ChangeInfo,
    Comment,
    CommentThread,
    ExtractedComments,
)
from gerrit_cli.replier import CommentReplier, mark_done, reply_to_comment


class TestCommentReplier:
    """Tests for CommentReplier."""

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_reply_to_comment_success(self, mock_client_class):
        """Test successful reply to comment."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client_class.return_value = mock_client

        comment = Comment(
            id="comment1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix this",
            author=Author(name="Reviewer"),
            unresolved=True,
            updated="2025-01-01",
        )

        replier = CommentReplier()
        result = replier.reply_to_comment(
            change_number=123,
            comment=comment,
            message="Done",
            mark_resolved=True,
        )

        assert result.success is True
        assert result.comment_id == "comment1"
        assert result.message == "Done"
        assert result.marked_resolved is True
        assert result.error is None

        mock_client.post_review.assert_called_once_with(
            change_number=123,
            revision_id="abc123",
            comments={"test.py": [{
                "in_reply_to": "comment1",
                "message": "Done",
                "unresolved": False,
                "line": 10,
            }]},
        )

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_reply_to_comment_failure(self, mock_client_class):
        """Test failed reply to comment."""
        mock_client = MagicMock()
        mock_client.get_change_detail.side_effect = Exception("API Error")
        mock_client_class.return_value = mock_client

        comment = Comment(
            id="comment1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix this",
            author=Author(name="Reviewer"),
            unresolved=True,
            updated="2025-01-01",
        )

        replier = CommentReplier()
        result = replier.reply_to_comment(
            change_number=123,
            comment=comment,
            message="Done",
            mark_resolved=True,
        )

        assert result.success is False
        assert result.error == "API Error"

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_mark_done(self, mock_client_class):
        """Test marking comment as done."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client_class.return_value = mock_client

        comment = Comment(
            id="comment1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix this",
            author=Author(name="Reviewer"),
            unresolved=True,
            updated="2025-01-01",
        )

        replier = CommentReplier()
        result = replier.mark_done(change_number=123, comment=comment)

        assert result.success is True
        assert result.message == "Done"
        assert result.marked_resolved is True

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_mark_done_custom_message(self, mock_client_class):
        """Test marking comment as done with custom message."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client_class.return_value = mock_client

        comment = Comment(
            id="comment1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix this",
            author=Author(name="Reviewer"),
            unresolved=True,
            updated="2025-01-01",
        )

        replier = CommentReplier()
        result = replier.mark_done(
            change_number=123,
            comment=comment,
            message="Fixed in patchset 5",
        )

        assert result.message == "Fixed in patchset 5"

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_acknowledge(self, mock_client_class):
        """Test acknowledging a comment."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client_class.return_value = mock_client

        comment = Comment(
            id="comment1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Consider refactoring",
            author=Author(name="Reviewer"),
            unresolved=True,
            updated="2025-01-01",
        )

        replier = CommentReplier()
        result = replier.acknowledge(change_number=123, comment=comment)

        assert result.success is True
        assert result.message == "Acknowledged"
        assert result.marked_resolved is True

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_reply_to_thread_uses_last_comment(self, mock_client_class):
        """Test that reply_to_thread uses the last comment in the thread."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client_class.return_value = mock_client

        root = Comment(
            id="root",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix this",
            author=Author(name="Reviewer"),
            unresolved=True,
            updated="2025-01-01",
        )
        reply = Comment(
            id="reply1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Working on it",
            author=Author(name="Author"),
            unresolved=True,
            updated="2025-01-02",
            in_reply_to="root",
        )
        thread = CommentThread(root_comment=root, replies=[reply])

        replier = CommentReplier()
        replier.reply_to_thread(
            change_number=123,
            thread=thread,
            message="Done now",
            mark_resolved=True,
        )

        # Should reply to the last comment (reply1)
        mock_client.post_review.assert_called_once()
        [reply_input] = mock_client.post_review.call_args[1]["comments"]["test.py"]
        assert reply_input["in_reply_to"] == "reply1"

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_reply_to_thread_with_no_replies(self, mock_client_class):
        """Test reply_to_thread when thread has no replies."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client_class.return_value = mock_client

        root = Comment(
            id="root",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix this",
            author=Author(name="Reviewer"),
            unresolved=True,
            updated="2025-01-01",
        )
        thread = CommentThread(root_comment=root)

        replier = CommentReplier()
        replier.reply_to_thread(
            change_number=123,
            thread=thread,
            message="Done",
        )

        # Should reply to the root comment
        [reply_input] = mock_client.post_review.call_args[1]["comments"]["test.py"]
        assert reply_input["in_reply_to"] == "root"

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_batch_reply_success(self, mock_client_class):
        """Test batch reply with multiple comments."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client.post_review.return_value = {}
        mock_client_class.return_value = mock_client

        comment1 = Comment(
            id="c1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix 1",
            author=Author(name="R"),
            unresolved=True,
            updated="2025-01-01",
        )
        comment2 = Comment(
            id="c2",
            patch_set=1,
            file_path="other.py",
            line=20,
            message="Fix 2",
            author=Author(name="R"),
            unresolved=True,
            updated="2025-01-01",
        )

        replier = CommentReplier()
        results = replier.batch_reply(
            change_number=123,
            replies=[
                {"comment": comment1, "message": "Done 1", "mark_resolved": True},
                {"comment": comment2, "message": "Done 2", "mark_resolved": True},
            ],
        )

        assert len(results) == 2
        assert all(r.success for r in results)

        # Verify post_review was called with both comments
        call_args = mock_client.post_review.call_args
        comments = call_args[1]["comments"]
        assert "test.py" in comments
        assert "other.py" in comments

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_batch_reply_failure(self, mock_client_class):
        """Test batch reply failure."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client.post_review.side_effect = Exception("Batch failed")
        mock_client_class.return_value = mock_client

        comment = Comment(
            id="c1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix",
            author=Author(name="R"),
            unresolved=True,
            updated="2025-01-01",
        )

        replier = CommentReplier()
        results = replier.batch_reply(
            change_number=123,
            replies=[{"comment": comment, "message": "Done"}],
        )

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error == "Error posting replies: Batch failed"

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_batch_reply_on_a_file_the_new_patchset_deleted(self, mock_client_class):
        """51164: PS9 deleted the file PS8's comment was on, and Gerrit
        refused the whole review posted on PS9."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "ps9sha",
            "revisions": {"ps8sha": {"_number": 8}, "ps9sha": {"_number": 9}},
        }
        mock_client_class.return_value = mock_client

        def comment(cid, path, line):
            return Comment(
                id=cid, patch_set=8, file_path=path, line=line, message="x",
                author=Author(name="R"), unresolved=True, updated="2026-10-02",
            )

        replier = CommentReplier()
        results = replier.batch_reply(
            change_number=51164,
            replies=[
                {"comment": comment("gone", "Documentation/man4/pinger.4", 3),
                 "message": "Removed in PS9.", "mark_resolved": True},
                {"comment": comment("kept", "lustre/kept.c", 10),
                 "message": "Done."},
                {"comment": comment("ps", "/PATCHSET_LEVEL", None),
                 "message": "Thanks."},
            ],
        )

        assert [r.comment_id for r in results] == ["gone", "kept", "ps"]
        assert all(r.success for r in results)
        posts = {
            c.kwargs["revision_id"]: c.kwargs["comments"]
            for c in mock_client.post_review.call_args_list
        }
        assert list(posts) == ["ps8sha"]
        assert sorted(posts["ps8sha"]) == [
            "/PATCHSET_LEVEL", "Documentation/man4/pinger.4", "lustre/kept.c",
        ]
        assert posts["ps8sha"]["Documentation/man4/pinger.4"][0]["unresolved"] is False

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_batch_reply_goes_on_each_parents_patchset(self, mock_client_class):
        """68672: replies to PS1 comments went on PS2, at PS1's line
        numbers, and the threads showed split across patchsets."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "ps3sha",
            "revisions": {
                "ps1sha": {"_number": 1},
                "ps2sha": {"_number": 2},
                "ps3sha": {"_number": 3},
            },
        }
        mock_client.get_drafts.return_value = {}
        mock_client.create_draft.side_effect = (
            lambda change, revision, comment: {"id": f"d-{comment['in_reply_to']}"})
        mock_client_class.return_value = mock_client
        span = {"start_line": 40, "start_character": 0,
                "end_line": 42, "end_character": 7}
        on_ps1 = Comment(
            id="a", patch_set=1, file_path="lustre/mdt/mdt_reint.c", line=42,
            message="x", author=Author(name="R"), unresolved=True,
            updated="2026-10-21", range=span,
        )
        on_ps2 = Comment(
            id="b", patch_set=2, file_path="lustre/mdt/mdt_reint.c", line=9,
            message="x", author=Author(name="R"), unresolved=True,
            updated="2026-10-21", side="PARENT",
        )

        results = CommentReplier().batch_reply(
            change_number=68672,
            replies=[
                {"comment": on_ps1, "message": "Done.", "mark_resolved": True},
                {"comment": on_ps2, "message": "Why?"},
            ],
        )

        assert all(r.success for r in results)
        drafts = [(c.args[1], c.args[2])
                  for c in mock_client.create_draft.call_args_list]
        assert drafts == [
            ("ps1sha", {"path": "lustre/mdt/mdt_reint.c", "in_reply_to": "a",
                        "message": "Done.", "unresolved": False, "line": 42,
                        "range": span}),
            ("ps2sha", {"path": "lustre/mdt/mdt_reint.c", "in_reply_to": "b",
                        "message": "Why?", "unresolved": True, "line": 9,
                        "side": "PARENT"}),
        ]
        mock_client.publish_drafts.assert_called_once_with(68672)
        mock_client.post_review.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_batch_reply_on_several_patchsets_keeps_the_users_drafts(
            self, mock_client_class):
        """64441: replies on PS24 and PS32 went out as two reviews in the
        same second; Gerrit filed all comments under one message and the
        other showed empty.  Publishing would take the user's drafts too,
        so with drafts on the change nothing is posted."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "ps34sha",
            "revisions": {"ps24sha": {"_number": 24}, "ps32sha": {"_number": 32},
                          "ps34sha": {"_number": 34}},
        }
        mock_client.get_drafts.return_value = {"a.c": [{"id": "mine", "line": 3}]}
        mock_client_class.return_value = mock_client

        def comment(cid, patch_set):
            return Comment(id=cid, patch_set=patch_set, file_path="x.c",
                           line=1, message="x", author=Author(name="R"),
                           unresolved=True, updated="2026-11-02")

        results = CommentReplier().batch_reply(
            change_number=64441,
            replies=[{"comment": comment("p24", 24), "message": "Done."},
                     {"comment": comment("p32", 32), "message": "Done."}],
        )

        assert not any(r.success for r in results)
        assert "a.c:3" in results[0].error
        mock_client.create_draft.assert_not_called()
        mock_client.post_review.assert_not_called()
        mock_client.publish_drafts.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_batch_reply_failure_carries_gerrits_reason(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {"current_revision": "abc"}
        error = Exception("400 Client Error: Bad Request")
        error.response = MagicMock(text="file x not found in revision 51164,9\n")
        mock_client.post_review.side_effect = error
        mock_client_class.return_value = mock_client
        comment = Comment(
            id="c1", patch_set=1, file_path="test.py", line=1, message="x",
            author=Author(name="R"), unresolved=True, updated="2026-10-02",
        )

        [result] = CommentReplier().batch_reply(
            change_number=1, replies=[{"comment": comment, "message": "y"}],
        )

        assert result.success is False
        assert ("400 Client Error: Bad Request: file x not found in "
                "revision 51164,9") in result.error

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_reply_from_extracted(self, mock_client_class):
        """Test replying from extracted comments by index."""
        mock_client = MagicMock()
        mock_client.get_change_detail.return_value = {
            "current_revision": "abc123"
        }
        mock_client_class.return_value = mock_client

        change_info = ChangeInfo(
            change_id="test~123",
            change_number=123,
            project="test",
            branch="master",
            subject="Test",
            status="NEW",
            current_revision="abc123",
            owner=Author(name="Owner"),
            url="https://example.com/123",
        )
        thread = CommentThread(
            root_comment=Comment(
                id="c1",
                patch_set=1,
                file_path="test.py",
                line=10,
                message="Fix",
                author=Author(name="R"),
                unresolved=True,
                updated="2025-01-01",
            )
        )
        extracted = ExtractedComments(
            change_info=change_info,
            threads=[thread],
            unresolved_count=1,
            total_count=1,
        )

        replier = CommentReplier()
        result = replier.reply_from_extracted(
            extracted=extracted,
            thread_index=0,
            message="Done",
            mark_resolved=True,
        )

        assert result.success is True

    @patch("gerrit_cli.replier.GerritCommentsClient")
    def test_reply_from_extracted_invalid_index(self, mock_client_class):
        """Test replying with invalid thread index."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        change_info = ChangeInfo(
            change_id="test~123",
            change_number=123,
            project="test",
            branch="master",
            subject="Test",
            status="NEW",
            current_revision="abc123",
            owner=Author(name="Owner"),
            url="https://example.com/123",
        )
        extracted = ExtractedComments(
            change_info=change_info,
            threads=[],
            unresolved_count=0,
            total_count=0,
        )

        replier = CommentReplier()
        result = replier.reply_from_extracted(
            extracted=extracted,
            thread_index=5,
            message="Done",
        )

        assert result.success is False
        assert "Invalid thread index" in result.error


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    @patch("gerrit_cli.replier.CommentReplier")
    def test_reply_to_comment_function(self, mock_replier_class):
        """Test reply_to_comment convenience function."""
        mock_replier = MagicMock()
        mock_replier_class.return_value = mock_replier

        comment = Comment(
            id="c1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix",
            author=Author(name="R"),
            unresolved=True,
            updated="2025-01-01",
        )

        reply_to_comment(123, comment, "Thanks!", mark_resolved=False)

        mock_replier.reply_to_comment.assert_called_once_with(
            change_number=123,
            comment=comment,
            message="Thanks!",
            mark_resolved=False,
        )

    @patch("gerrit_cli.replier.CommentReplier")
    def test_mark_done_function(self, mock_replier_class):
        """Test mark_done convenience function."""
        mock_replier = MagicMock()
        mock_replier_class.return_value = mock_replier

        comment = Comment(
            id="c1",
            patch_set=1,
            file_path="test.py",
            line=10,
            message="Fix",
            author=Author(name="R"),
            unresolved=True,
            updated="2025-01-01",
        )

        mark_done(123, comment, message="Fixed!")

        mock_replier.mark_done.assert_called_once_with(
            change_number=123,
            comment=comment,
            message="Fixed!",
        )


class TestPushStaged:
    """push_staged: replies on one patchset are one review; on several,
    one draft per reply on its parent's patchset, then a single publish,
    so the replies make one Gerrit message."""

    @staticmethod
    def _setup(mock_staging_class, mock_client_class, operations,
               comments=None, drafts=None):
        from gerrit_cli.staging import StagedPatch

        client = MagicMock()
        client.get_change_detail.return_value = {
            "current_revision": "ps3sha",
            "revisions": {"ps1sha": {"_number": 1}, "ps2sha": {"_number": 2},
                          "ps3sha": {"_number": 3}},
        }
        client.get_comments.return_value = comments or {}
        client.get_drafts.return_value = drafts or {}
        client.create_draft.side_effect = (
            lambda change, revision, comment: {"id": f"d-{comment['message']}"})
        mock_client_class.return_value = client
        staged = StagedPatch(change_number=12345,
                             change_url="https://example.com/12345",
                             patchset=3, operations=list(operations))
        staging = MagicMock()
        staging.load_staged.return_value = staged
        mock_staging_class.return_value = staging
        return client, staging, staged

    #: id1 on PS1 and id2 on PS2: replies on two patchsets, which
    #: go out as drafts and one publish
    TWO_PATCHSETS = {"test.c": [{"id": "id1", "patch_set": 1, "line": 10},
                                {"id": "id2", "patch_set": 2, "line": 10}]}

    @staticmethod
    def _op(index, message, comment_id, resolve=True, line=10):
        from gerrit_cli.staging import StagedOperation
        return StagedOperation(thread_index=index, file_path="test.c",
                               line=line, message=message, resolve=resolve,
                               comment_id=comment_id)

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_push_staged_no_operations(self, mock_staging_class, mock_client_class):
        mock_staging = MagicMock()
        mock_staging.load_staged.return_value = None
        mock_staging_class.return_value = mock_staging

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False
        assert "No staged operations" in msg
        assert count == 0

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_push_staged_dry_run(self, mock_staging_class, mock_client_class):
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class, [self._op(0, "Done", "id1")])

        success, msg, count = CommentReplier().push_staged(12345, dry_run=True)

        assert success is True
        assert "Would push" in msg and "RESOLVE" in msg
        client.create_draft.assert_not_called()
        client.publish_drafts.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_replies_on_several_patchsets_publish_once(
            self, mock_staging_class, mock_client_class, monkeypatch):
        """Each reply is drafted on its parent's patchset, at the parent's
        line and range; a parent the change no longer has means the
        current patchset. Then one publish, and the staging is cleared."""
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        span = {"start_line": 8, "start_character": 0,
                "end_line": 10, "end_character": 3}
        comments = {"test.c": [
            {"id": "id1", "patch_set": 1, "line": 10, "range": span},
            {"id": "id2", "patch_set": 2, "line": 20},
        ]}
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"),
             self._op(1, "Why?", "id2", resolve=False, line=20),
             self._op(2, "Gone", "gone", line=30)],
            comments=comments)

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is True and count == 3
        drafts = [(c.args[1], c.args[2]) for c in client.create_draft.call_args_list]
        assert drafts == [
            ("ps1sha", {"path": "test.c", "in_reply_to": "id1",
                        "message": "Done", "unresolved": False,
                        "line": 10, "range": span}),
            ("ps2sha", {"path": "test.c", "in_reply_to": "id2",
                        "message": "Why?", "unresolved": True, "line": 20}),
            ("ps3sha", {"path": "test.c", "in_reply_to": "gone",
                        "message": "Gone", "unresolved": False, "line": 30}),
        ]
        client.publish_drafts.assert_called_once_with(12345)
        client.post_review.assert_not_called()
        staging.clear_staged.assert_called_once_with(12345)

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_replies_on_one_patchset_are_one_review(
            self, mock_staging_class, mock_client_class, monkeypatch):
        """No drafts are needed, so the user's own drafts neither stop
        the push nor get published (a review keeps drafts by default)."""
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2", line=12)],
            drafts={"other.c": [{"id": "theirs", "line": 7}]})

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is True and count == 2
        client.post_review.assert_called_once_with(
            change_number=12345, revision_id="ps3sha",
            comments={"test.c": [
                {"in_reply_to": "id1", "message": "Done",
                 "unresolved": False, "line": 10},
                {"in_reply_to": "id2", "message": "Ok",
                 "unresolved": False, "line": 12},
            ]})
        client.create_draft.assert_not_called()
        client.get_drafts.assert_not_called()
        client.publish_drafts.assert_not_called()
        staging.clear_staged.assert_called_once_with(12345)

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_a_failed_review_on_one_patchset_keeps_everything_staged(
            self, mock_staging_class, mock_client_class, monkeypatch):
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class, [self._op(0, "Done", "id1")])
        client.post_review.side_effect = Exception("API Error")

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False and "API Error" in msg
        assert "all 1 are still staged" in msg
        staging.clear_staged.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_existing_drafts_are_not_published_with_the_replies(
            self, mock_staging_class, mock_client_class, monkeypatch):
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS,
            drafts={"lustre/llite/file.c": [{"id": "x", "line": 42}]})

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False
        assert "lustre/llite/file.c:42" in msg
        client.create_draft.assert_not_called()
        client.publish_drafts.assert_not_called()
        client.delete_draft.assert_not_called()
        staging.clear_staged.assert_not_called()
        staging.save_staged.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_drafts_mode_creates_drafts_and_publishes_nothing(
            self, mock_staging_class, mock_client_class, monkeypatch):
        monkeypatch.setenv("GERRIT_CLI_MODE", "drafts")
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS,
            drafts={"other.c": [{"id": "x", "line": 1}]})

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is True
        assert client.create_draft.call_count == 2
        client.publish_drafts.assert_not_called()
        assert "GERRIT_CLI_MODE=drafts" in msg and "not published" in msg
        client.delete_draft.assert_not_called()
        staging.clear_staged.assert_called_once_with(12345)

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_a_failed_draft_removes_the_drafts_already_made(
            self, mock_staging_class, mock_client_class, monkeypatch):
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS)
        client.create_draft.side_effect = [{"id": "d1"}, Exception("API Error")]

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False and "API Error" in msg
        assert "all 2 are still staged" in msg
        client.delete_draft.assert_called_once_with(12345, "ps1sha", "d1")
        client.publish_drafts.assert_not_called()
        staging.clear_staged.assert_not_called()
        staging.save_staged.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_a_failed_publish_removes_the_drafts(
            self, mock_staging_class, mock_client_class, monkeypatch):
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS)
        client.publish_drafts.side_effect = Exception("API Error")
        ours = {"test.c": [{"id": "d-Done", "line": 10},
                           {"id": "d-Ok", "line": 10}]}
        client.get_drafts.side_effect = [{}, ours, ours]

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False
        assert sorted(c.args[2] for c in client.delete_draft.call_args_list) \
            == ["d-Done", "d-Ok"]
        staging.clear_staged.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_a_failing_draft_check_creates_nothing(
            self, mock_staging_class, mock_client_class, monkeypatch):
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS)
        client.get_drafts.side_effect = Exception("API Error")

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False and "still staged" in msg
        client.create_draft.assert_not_called()
        client.publish_drafts.assert_not_called()
        staging.clear_staged.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_a_draft_saved_during_the_push_is_not_published(
            self, mock_staging_class, mock_client_class, monkeypatch):
        """A draft of the user's that appears between the check and the
        publish: the push refuses, removes its own drafts, keeps theirs."""
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS)
        client.get_drafts.side_effect = [
            {},
            {"test.c": [{"id": "d-Done", "line": 10}, {"id": "d-Ok", "line": 10}],
             "other.c": [{"id": "theirs", "line": 7}]},
        ]

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False and "other.c:7" in msg
        client.publish_drafts.assert_not_called()
        assert sorted(c.args[1:] for c in client.delete_draft.call_args_list) \
            == [("ps1sha", "d-Done"), ("ps2sha", "d-Ok")]
        staging.clear_staged.assert_not_called()

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_a_publish_whose_answer_was_lost_counts_as_done(
            self, mock_staging_class, mock_client_class, monkeypatch):
        """The publish raised, but the push's drafts are gone: Gerrit
        published them. Clearing the staging keeps a re-push from
        posting the replies twice."""
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS)
        client.publish_drafts.side_effect = TimeoutError("read timed out")
        client.get_drafts.side_effect = [
            {}, {"test.c": [{"id": "d-Done"}, {"id": "d-Ok"}]}, {}]

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is True and count == 2
        client.delete_draft.assert_not_called()
        staging.clear_staged.assert_called_once_with(12345)

    @patch("gerrit_cli.replier.GerritCommentsClient")
    @patch("gerrit_cli.replier.StagingManager")
    def test_an_unverifiable_publish_leaves_everything_in_place(
            self, mock_staging_class, mock_client_class, monkeypatch):
        """Neither the publish nor the check answered: the drafts stay
        (a re-push then refuses on them, so nothing is posted twice) and
        so do the staged replies."""
        monkeypatch.delenv("GERRIT_CLI_MODE", raising=False)
        client, staging, _ = self._setup(
            mock_staging_class, mock_client_class,
            [self._op(0, "Done", "id1"), self._op(1, "Ok", "id2")],
            comments=self.TWO_PATCHSETS)
        client.publish_drafts.side_effect = TimeoutError("read timed out")
        client.get_drafts.side_effect = [
            {}, {"test.c": [{"id": "d-Done"}, {"id": "d-Ok"}]},
            TimeoutError("again")]

        success, msg, count = CommentReplier().push_staged(12345)

        assert success is False
        assert "could not tell" in msg
        client.delete_draft.assert_not_called()
        staging.clear_staged.assert_not_called()
