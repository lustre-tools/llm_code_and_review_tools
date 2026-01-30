"""Tests for the CLI module."""

import argparse
from unittest.mock import MagicMock, patch

import pytest

from gerrit_comments.cli import (
    cmd_series,
    filter_threads_by_fields,
    generate_review_prompt,
)
from gerrit_comments.models import Author, CodeContext, Comment, CommentThread


class TestFilterThreadsByFields:
    """Tests for filter_threads_by_fields function."""

    @pytest.fixture
    def sample_threads(self):
        """Create sample threads for testing."""
        author = Author(name="Test User", email="test@example.com")
        comment1 = Comment(
            id="c1",
            patch_set=1,
            file_path="src/main.py",
            line=10,
            message="Please fix this bug",
            author=author,
            unresolved=True,
            updated="2025-01-01",
            code_context=CodeContext(
                lines=["def foo():", "    pass"],
                start_line=9,
                end_line=11,
                target_line=10,
            ),
        )
        reply1 = Comment(
            id="c2",
            patch_set=1,
            file_path="src/main.py",
            line=10,
            message="Working on it",
            author=Author(name="Developer"),
            unresolved=True,
            updated="2025-01-02",
        )
        thread1 = CommentThread(root_comment=comment1, replies=[reply1])

        comment2 = Comment(
            id="c3",
            patch_set=2,
            file_path="tests/test_main.py",
            line=None,
            message="Add more tests",
            author=author,
            unresolved=False,
            updated="2025-01-03",
        )
        thread2 = CommentThread(root_comment=comment2, replies=[])

        return [thread1, thread2]

    def test_filter_basic_fields(self, sample_threads):
        """Test filtering to basic fields."""
        result = filter_threads_by_fields(sample_threads, "index,file,message")
        assert len(result) == 2
        assert result[0] == {
            "index": 0,
            "file": "src/main.py",
            "message": "Please fix this bug",
        }
        assert result[1] == {
            "index": 1,
            "file": "tests/test_main.py",
            "message": "Add more tests",
        }

    def test_filter_with_line_and_author(self, sample_threads):
        """Test filtering to line and author fields."""
        result = filter_threads_by_fields(sample_threads, "file,line,author")
        assert result[0] == {
            "file": "src/main.py",
            "line": 10,
            "author": "Test User",
        }
        assert result[1] == {
            "file": "tests/test_main.py",
            "line": None,
            "author": "Test User",
        }

    def test_filter_with_resolved(self, sample_threads):
        """Test filtering to resolved status."""
        result = filter_threads_by_fields(sample_threads, "index,resolved")
        assert result[0] == {"index": 0, "resolved": False}
        assert result[1] == {"index": 1, "resolved": True}

    def test_filter_with_code_context(self, sample_threads):
        """Test filtering to code_context."""
        result = filter_threads_by_fields(sample_threads, "file,code_context")
        assert result[0]["code_context"] == {
            "lines": ["def foo():", "    pass"],
            "start_line": 9,
            "end_line": 11,
            "target_line": 10,
        }
        assert result[1]["code_context"] is None

    def test_filter_with_replies(self, sample_threads):
        """Test filtering to replies."""
        result = filter_threads_by_fields(sample_threads, "index,replies")
        assert result[0]["replies"] == [
            {"author": "Developer", "message": "Working on it"}
        ]
        assert result[1]["replies"] == []

    def test_filter_with_patch_set(self, sample_threads):
        """Test filtering to patch_set."""
        result = filter_threads_by_fields(sample_threads, "file,patch_set")
        assert result[0] == {"file": "src/main.py", "patch_set": 1}
        assert result[1] == {"file": "tests/test_main.py", "patch_set": 2}


class TestGenerateReviewPrompt:
    """Tests for generate_review_prompt function."""

    def test_generates_prompt_with_url(self):
        """Test that the prompt includes the URL."""
        url = "https://review.whamcloud.com/c/fs/lustre-release/+/62757"
        prompt = generate_review_prompt(url)
        assert url in prompt
        assert "review-series" in prompt

    def test_prompt_includes_workflow_commands(self):
        """Test that the prompt includes key workflow commands."""
        url = "https://example.com/12345"
        prompt = generate_review_prompt(url)
        assert "stage" in prompt
        assert "finish-patch" in prompt
        assert "end-session" in prompt
        assert "abort-session" in prompt


class TestCmdSeries:
    """Tests for cmd_series function."""

    @pytest.fixture
    def mock_args(self):
        """Create mock args for cmd_series."""
        args = argparse.Namespace(
            url="https://review.whamcloud.com/c/fs/lustre-release/+/62757",
            pretty=False,
            urls_only=False,
            numbers_only=False,
            include_abandoned=False,
            no_prompt=False,
            no_checkout=False,
        )
        return args

    @pytest.fixture
    def mock_args_no_checkout(self):
        """Create mock args with no_checkout=True."""
        args = argparse.Namespace(
            url="https://review.whamcloud.com/c/fs/lustre-release/+/62757",
            pretty=False,
            urls_only=False,
            numbers_only=False,
            include_abandoned=False,
            no_prompt=False,
            no_checkout=True,
        )
        return args

    def test_checks_git_state_before_fetching(self, mock_args):
        """Test that git state is checked before fetching comments."""
        with patch('gerrit_comments.cli.RebaseManager') as MockRebaseManager, \
             patch('gerrit_comments.cli.SeriesFinder') as MockSeriesFinder, \
             pytest.raises(SystemExit) as exc_info:
            # Setup RebaseManager to report dirty state
            mock_manager = MagicMock()
            mock_manager.check_git_repo.return_value = (False, "Working tree is not clean.")
            MockRebaseManager.return_value = mock_manager

            cmd_series(mock_args)

            # Verify check_git_repo was called
            mock_manager.check_git_repo.assert_called_once()
            # Verify SeriesFinder was NOT called (should fail fast)
            MockSeriesFinder.assert_not_called()

        assert exc_info.value.code == 1

    def test_skips_git_check_when_no_checkout(self, mock_args_no_checkout):
        """Test that git state check is skipped when --no-checkout is set."""
        with patch('gerrit_comments.cli.RebaseManager') as MockRebaseManager, \
             patch('gerrit_comments.cli.SeriesFinder') as MockSeriesFinder:
            # Setup SeriesFinder to return a mock series
            mock_series = MagicMock()
            mock_series.patches = []
            mock_series.to_dict.return_value = {"patches": []}
            mock_finder = MagicMock()
            mock_finder.find_series.return_value = mock_series
            MockSeriesFinder.return_value = mock_finder

            # Set pretty=False to get JSON output path
            mock_args_no_checkout.pretty = False
            with pytest.raises(SystemExit) as exc_info:
                cmd_series(mock_args_no_checkout)
            assert exc_info.value.code == 0

            # Verify RebaseManager was NOT instantiated
            MockRebaseManager.assert_not_called()

    def test_skips_git_check_for_no_checkout_output(self):
        """Test that git state check is skipped when no_checkout is set."""
        args = argparse.Namespace(
            url="https://review.whamcloud.com/c/fs/lustre-release/+/62757",
            pretty=False,
            urls_only=False,
            numbers_only=False,
            include_abandoned=False,
            no_checkout=True,
        )
        with patch('gerrit_comments.cli.RebaseManager') as MockRebaseManager, \
             patch('gerrit_comments.cli.SeriesFinder') as MockSeriesFinder:
            mock_series = MagicMock()
            mock_series.patches = []
            mock_series.to_dict.return_value = {}
            mock_finder = MagicMock()
            mock_finder.find_series.return_value = mock_series
            MockSeriesFinder.return_value = mock_finder

            with pytest.raises(SystemExit) as exc_info:
                cmd_series(args)
            assert exc_info.value.code == 0

            # Verify RebaseManager was NOT instantiated
            MockRebaseManager.assert_not_called()


class TestCLIImports:
    """Test that CLI imports work correctly."""

    def test_cli_imports_without_error(self):
        """Test that the CLI module can be imported."""
        # This would have caught the SeriesRebaseManager typo
        from gerrit_comments import cli
        assert hasattr(cli, 'cmd_series')
        assert hasattr(cli, 'main')

    def test_rebase_manager_has_check_git_repo(self):
        """Test that RebaseManager has the check_git_repo method."""
        from gerrit_comments.rebase import RebaseManager
        manager = RebaseManager()
        assert hasattr(manager, 'check_git_repo')
        assert callable(manager.check_git_repo)

    def test_all_cmd_functions_exist(self):
        """Test that all command functions are defined."""
        from gerrit_comments import cli
        cmd_functions = [
            'cmd_extract', 'cmd_reply', 'cmd_batch_reply', 'cmd_review',
            'cmd_series', 'cmd_work_on_patch', 'cmd_finish_patch',
            'cmd_abort', 'cmd_status',
            'cmd_next_patch', 'cmd_stage', 'cmd_push', 'cmd_staged_list',
            'cmd_staged_show', 'cmd_staged_remove', 'cmd_staged_clear',
            'cmd_staged_refresh',
        ]
        for func_name in cmd_functions:
            assert hasattr(cli, func_name), f"Missing {func_name}"
            assert callable(getattr(cli, func_name)), f"{func_name} not callable"


class TestCmdExtract:
    """Tests for cmd_extract function."""

    def test_extract_with_json_output(self):
        """Test extract command with JSON output."""
        from gerrit_comments.cli import cmd_extract
        args = argparse.Namespace(
            url="https://review.whamcloud.com/c/fs/lustre-release/+/12345",
            all=False,
            no_context=False,
            context_lines=3,
            pretty=False,
        )
        with patch('gerrit_comments.cli.extract_comments') as mock_extract:
            mock_result = MagicMock()
            mock_result.to_dict.return_value = {"threads": []}
            mock_extract.return_value = mock_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_extract(args)
            assert exc_info.value.code == 0

            mock_extract.assert_called_once()

    def test_extract_error_handling(self):
        """Test that extract handles errors gracefully."""
        from gerrit_comments.cli import cmd_extract
        args = argparse.Namespace(
            url="https://review.whamcloud.com/c/fs/lustre-release/+/12345",
            all=False,
            no_context=False,
            context_lines=3,
            pretty=False,
        )
        with patch('gerrit_comments.cli.extract_comments') as mock_extract, \
             pytest.raises(SystemExit) as exc_info:
            mock_extract.side_effect = Exception("Network error")
            cmd_extract(args)

        assert exc_info.value.code == 1


class TestCmdWorkOnPatch:
    """Tests for cmd_work_on_patch function."""

    def test_work_on_patch_with_url(self):
        """Test work-on-patch with explicit URL."""
        from gerrit_comments.cli import cmd_work_on_patch
        args = argparse.Namespace(
            change_number=12345,
            url="https://review.whamcloud.com/c/fs/lustre-release/+/12345",
        )
        with patch('gerrit_comments.cli.work_on_patch') as mock_work:
            mock_work.return_value = (True, "Success")
            cmd_work_on_patch(args)
            mock_work.assert_called_once_with(args.url, args.change_number)

    def test_work_on_patch_uses_session_url(self):
        """Test work-on-patch uses session URL when not provided."""
        from gerrit_comments.cli import cmd_work_on_patch
        args = argparse.Namespace(
            change_number=12345,
            url=None,
        )
        with patch('gerrit_comments.cli.work_on_patch') as mock_work, \
             patch('gerrit_comments.cli.get_session_url') as mock_get_url:
            mock_get_url.return_value = "https://example.com/series/12345"
            mock_work.return_value = (True, "Success")
            cmd_work_on_patch(args)
            mock_get_url.assert_called_once()
            mock_work.assert_called_once()

    def test_work_on_patch_no_url_no_session(self):
        """Test work-on-patch fails without URL or session."""
        from gerrit_comments.cli import cmd_work_on_patch
        args = argparse.Namespace(
            change_number=12345,
            url=None,
        )
        with patch('gerrit_comments.cli.get_session_url') as mock_get_url, \
             pytest.raises(SystemExit) as exc_info:
            mock_get_url.return_value = None
            cmd_work_on_patch(args)

        assert exc_info.value.code == 1


class TestCmdFinishPatch:
    """Tests for cmd_finish_patch function."""

    def test_finish_patch_success(self):
        """Test finish-patch with successful completion."""
        from gerrit_comments.cli import cmd_finish_patch
        args = argparse.Namespace(no_advance=False)
        with patch('gerrit_comments.cli.finish_patch') as mock_finish:
            mock_finish.return_value = (True, "Finished successfully")
            cmd_finish_patch(args)
            mock_finish.assert_called_once_with(auto_next=True)

    def test_finish_patch_stay(self):
        """Test finish-patch with --stay flag."""
        from gerrit_comments.cli import cmd_finish_patch
        args = argparse.Namespace(stay=True)
        with patch('gerrit_comments.cli.finish_patch') as mock_finish:
            mock_finish.return_value = (True, "Finished")
            cmd_finish_patch(args)
            mock_finish.assert_called_once_with(auto_next=False)

    def test_finish_patch_failure(self):
        """Test finish-patch handles failure."""
        from gerrit_comments.cli import cmd_finish_patch
        args = argparse.Namespace(no_advance=False)
        with patch('gerrit_comments.cli.finish_patch') as mock_finish, \
             pytest.raises(SystemExit) as exc_info:
            mock_finish.return_value = (False, "Error: no session")
            cmd_finish_patch(args)

        assert exc_info.value.code == 1


class TestCmdAbort:
    """Tests for abort command (consolidates abort-patch and end-session)."""

    def test_abort_success(self):
        """Test abort without --keep-changes (restores original state)."""
        from gerrit_comments.cli import cmd_abort
        args = argparse.Namespace(keep_changes=False)
        with patch('gerrit_comments.cli.abort_patch') as mock_abort:
            mock_abort.return_value = (True, "Aborted")
            cmd_abort(args)
            mock_abort.assert_called_once()

    def test_abort_failure(self):
        """Test abort handles failure."""
        from gerrit_comments.cli import cmd_abort
        args = argparse.Namespace(keep_changes=False)
        with patch('gerrit_comments.cli.abort_patch') as mock_abort, \
             pytest.raises(SystemExit) as exc_info:
            mock_abort.return_value = (False, "No session")
            cmd_abort(args)

        assert exc_info.value.code == 1

    def test_abort_keep_changes_success(self):
        """Test abort with --keep-changes (keeps current git state)."""
        from gerrit_comments.cli import cmd_abort
        args = argparse.Namespace(keep_changes=True)
        with patch('gerrit_comments.cli.end_session') as mock_end:
            mock_end.return_value = (True, "Session ended")
            cmd_abort(args)
            mock_end.assert_called_once()

    def test_abort_keep_changes_failure(self):
        """Test abort --keep-changes handles failure."""
        from gerrit_comments.cli import cmd_abort
        args = argparse.Namespace(keep_changes=True)
        with patch('gerrit_comments.cli.end_session') as mock_end, \
             pytest.raises(SystemExit) as exc_info:
            mock_end.return_value = (False, "No session")
            cmd_abort(args)

        assert exc_info.value.code == 1


class TestCmdNextPatch:
    """Tests for cmd_next_patch function."""

    def test_next_patch_sequential(self):
        """Test next-patch without --with-comments."""
        from gerrit_comments.cli import cmd_next_patch
        args = argparse.Namespace(with_comments=False)
        with patch('gerrit_comments.cli.next_patch') as mock_next:
            mock_next.return_value = (True, "Moved to next patch")
            cmd_next_patch(args)
            mock_next.assert_called_once_with(with_comments=False)

    def test_next_patch_with_comments(self):
        """Test next-patch with --with-comments."""
        from gerrit_comments.cli import cmd_next_patch
        args = argparse.Namespace(with_comments=True)
        with patch('gerrit_comments.cli.next_patch') as mock_next:
            mock_next.return_value = (True, "Moved to next patch with comments")
            cmd_next_patch(args)
            mock_next.assert_called_once_with(with_comments=True)


class TestCmdStage:
    """Tests for cmd_stage function."""

    def test_stage_done(self):
        """Test staging a 'done' reply."""
        from gerrit_comments.cli import cmd_stage
        args = argparse.Namespace(
            thread_index=0,
            message=None,
            done=True,
            ack=False,
            resolve=False,
            url="https://review.whamcloud.com/c/fs/lustre-release/+/12345",
        )
        with patch('gerrit_comments.cli.extract_comments') as mock_extract, \
             patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.StagingManager') as MockStaging:
            # Setup mock URL parsing
            MockClient.parse_gerrit_url.return_value = ("https://review.whamcloud.com", 12345)

            # Setup mock thread
            mock_thread = MagicMock()
            mock_comment = MagicMock()
            mock_comment.file_path = "file.c"
            mock_comment.line = 10
            mock_thread.root_comment = mock_comment
            mock_thread.last_comment = mock_comment

            mock_result = MagicMock()
            mock_result.threads = [mock_thread]
            mock_result.change_info = MagicMock()
            mock_result.change_info.url = "https://example.com/12345"
            mock_extract.return_value = mock_result

            mock_client = MagicMock()
            mock_client.get_change_detail.return_value = {
                "current_revision": "abc123",
                "revisions": {"abc123": {"_number": 5}},
            }
            MockClient.return_value = mock_client

            cmd_stage(args)

            MockStaging.return_value.stage_operation.assert_called_once()


class TestCmdPush:
    """Tests for cmd_push function."""

    def test_push_success(self):
        """Test pushing staged operations."""
        from gerrit_comments.cli import cmd_push
        args = argparse.Namespace(change_number=12345, dry_run=False)
        with patch('gerrit_comments.cli.CommentReplier') as MockReplier:
            mock_replier = MagicMock()
            mock_replier.push_staged.return_value = (True, "Pushed 3 operations", 3)
            MockReplier.return_value = mock_replier

            cmd_push(args)

            mock_replier.push_staged.assert_called_once_with(
                change_number=12345, dry_run=False
            )

    def test_push_dry_run(self):
        """Test push with dry-run flag."""
        from gerrit_comments.cli import cmd_push
        args = argparse.Namespace(change_number=12345, dry_run=True)
        with patch('gerrit_comments.cli.CommentReplier') as MockReplier:
            mock_replier = MagicMock()
            mock_replier.push_staged.return_value = (True, "Would push 3 operations", 3)
            MockReplier.return_value = mock_replier

            cmd_push(args)

            mock_replier.push_staged.assert_called_once_with(
                change_number=12345, dry_run=True
            )


class TestCmdStagedOperations:
    """Tests for staged operation commands."""

    def test_staged_list_empty(self):
        """Test staged-list with no staged operations."""
        from gerrit_comments.cli import cmd_staged_list
        args = argparse.Namespace(pretty=False)
        with patch('gerrit_comments.cli.StagingManager') as MockStaging:
            MockStaging.return_value.list_all_staged.return_value = []
            with pytest.raises(SystemExit) as exc_info:
                cmd_staged_list(args)
            assert exc_info.value.code == 0

    def test_staged_list_with_items(self):
        """Test staged-list with staged operations."""
        from gerrit_comments.cli import cmd_staged_list
        args = argparse.Namespace(pretty=False)
        with patch('gerrit_comments.cli.StagingManager') as MockStaging:
            mock_staged = MagicMock()
            mock_staged.change_number = 12345
            mock_staged.patchset = 5
            mock_staged.to_dict.return_value = {"change_number": 12345, "patchset": 5, "operations": []}
            MockStaging.return_value.list_all_staged.return_value = [mock_staged]
            with pytest.raises(SystemExit) as exc_info:
                cmd_staged_list(args)
            assert exc_info.value.code == 0

    def test_staged_show(self):
        """Test staged show command."""
        from gerrit_comments.cli import cmd_staged_show
        args = argparse.Namespace(change_number=12345, pretty=False)
        with patch('gerrit_comments.cli.StagingManager') as MockStaging:
            mock_staged = MagicMock()
            mock_staged.change_number = 12345
            mock_staged.patchset = 5
            mock_staged.to_dict.return_value = {"change_number": 12345, "patchset": 5, "operations": []}
            MockStaging.return_value.load_staged.return_value = mock_staged
            with pytest.raises(SystemExit) as exc_info:
                cmd_staged_show(args)
            assert exc_info.value.code == 0

    def test_staged_remove(self):
        """Test staged remove command."""
        from gerrit_comments.cli import cmd_staged_remove
        args = argparse.Namespace(change_number=12345, operation_index=0)
        with patch('gerrit_comments.cli.StagingManager') as MockStaging:
            MockStaging.return_value.remove_operation.return_value = True
            cmd_staged_remove(args)
            MockStaging.return_value.remove_operation.assert_called_once_with(12345, 0)

    def test_staged_clear_one(self):
        """Test staged clear command for one change."""
        from gerrit_comments.cli import cmd_staged_clear
        args = argparse.Namespace(change_number=12345)
        with patch('gerrit_comments.cli.StagingManager') as MockStaging:
            cmd_staged_clear(args)
            MockStaging.return_value.clear_staged.assert_called_once_with(12345)

    def test_staged_clear_all(self):
        """Test staged clear command for all changes."""
        from gerrit_comments.cli import cmd_staged_clear
        args = argparse.Namespace(change_number=None)
        with patch('gerrit_comments.cli.StagingManager') as MockStaging:
            MockStaging.return_value.clear_all_staged.return_value = 3
            cmd_staged_clear(args)
            MockStaging.return_value.clear_all_staged.assert_called_once()


class TestCmdStatus:
    """Tests for cmd_status function."""

    def test_status_with_session(self):
        """Test status with active session."""
        from gerrit_comments.cli import cmd_status
        args = argparse.Namespace()
        with patch('gerrit_comments.cli.rebase_status') as mock_status:
            mock_status.return_value = (True, "Session status...")
            cmd_status(args)
            mock_status.assert_called_once()

    def test_status_no_session(self):
        """Test status without active session exits with error."""
        from gerrit_comments.cli import cmd_status
        args = argparse.Namespace()
        with patch('gerrit_comments.cli.rebase_status') as mock_status, \
             pytest.raises(SystemExit) as exc_info:
            mock_status.return_value = (False, "No active session")
            cmd_status(args)

        assert exc_info.value.code == 1

    def test_status_exception(self):
        """Test status handles exceptions."""
        from gerrit_comments.cli import cmd_status
        args = argparse.Namespace()
        with patch('gerrit_comments.cli.rebase_status') as mock_status, \
             pytest.raises(SystemExit) as exc_info:
            mock_status.side_effect = Exception("Something went wrong")
            cmd_status(args)

        assert exc_info.value.code == 1


class TestCmdAbortExceptions:
    """Additional tests for abort command exception handling."""

    def test_abort_exception(self):
        """Test abort handles exceptions."""
        from gerrit_comments.cli import cmd_abort
        args = argparse.Namespace(keep_changes=False)
        with patch('gerrit_comments.cli.abort_patch') as mock_abort, \
             pytest.raises(SystemExit) as exc_info:
            mock_abort.side_effect = Exception("Something went wrong")
            cmd_abort(args)

        assert exc_info.value.code == 1


class TestCmdStagedRefresh:
    """Tests for cmd_staged_refresh function."""

    def test_staged_refresh_success(self):
        """Test refreshing staged operations."""
        from gerrit_comments.cli import cmd_staged_refresh
        args = argparse.Namespace(change_number=12345)
        with patch('gerrit_comments.cli.StagingManager') as MockStaging, \
             patch('gerrit_comments.cli.GerritCommentsClient') as MockClient:
            mock_client = MagicMock()
            mock_client.get_change_detail.return_value = {
                "current_revision": "abc123",
                "revisions": {"abc123": {"_number": 6}},
            }
            MockClient.return_value = mock_client
            mock_staging = MagicMock()
            mock_staging.load_staged.return_value = MagicMock(
                patchset=5,
                operations=[MagicMock()]
            )
            MockStaging.return_value = mock_staging

            cmd_staged_refresh(args)

    def test_staged_refresh_no_patchset(self):
        """Test refresh failure when patchset unknown exits with error."""
        from gerrit_comments.cli import cmd_staged_refresh
        args = argparse.Namespace(change_number=12345)
        with patch('gerrit_comments.cli.StagingManager'), \
             patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             pytest.raises(SystemExit) as exc_info:
            mock_client = MagicMock()
            mock_client.get_change_detail.return_value = {
                "current_revision": "",
                "revisions": {},
            }
            MockClient.return_value = mock_client

            cmd_staged_refresh(args)

        assert exc_info.value.code == 1


class TestCmdReintegration:
    """Tests for reintegration commands."""

    def test_continue_reintegration_success(self):
        """Test continue-reintegration success."""
        from gerrit_comments.cli import cmd_continue_reintegration
        args = argparse.Namespace()
        with patch('gerrit_comments.rebase.RebaseManager') as MockManager:
            mock_mgr = MagicMock()
            mock_mgr.continue_reintegration.return_value = (True, "Reintegration continued")
            MockManager.return_value = mock_mgr
            cmd_continue_reintegration(args)
            mock_mgr.continue_reintegration.assert_called_once()

    def test_continue_reintegration_failure(self):
        """Test continue-reintegration failure."""
        from gerrit_comments.cli import cmd_continue_reintegration
        args = argparse.Namespace()
        with patch('gerrit_comments.rebase.RebaseManager') as MockManager, \
             pytest.raises(SystemExit) as exc_info:
            mock_mgr = MagicMock()
            mock_mgr.continue_reintegration.return_value = (False, "Error")
            MockManager.return_value = mock_mgr
            cmd_continue_reintegration(args)

        assert exc_info.value.code == 1

    def test_skip_reintegration_success(self):
        """Test skip-reintegration success."""
        from gerrit_comments.cli import cmd_skip_reintegration
        args = argparse.Namespace()
        with patch('gerrit_comments.rebase.RebaseManager') as MockManager:
            mock_mgr = MagicMock()
            mock_mgr.skip_reintegration.return_value = (True, "Skipped")
            MockManager.return_value = mock_mgr
            cmd_skip_reintegration(args)
            mock_mgr.skip_reintegration.assert_called_once()

    def test_skip_reintegration_failure(self):
        """Test skip-reintegration failure."""
        from gerrit_comments.cli import cmd_skip_reintegration
        args = argparse.Namespace()
        with patch('gerrit_comments.rebase.RebaseManager') as MockManager, \
             pytest.raises(SystemExit) as exc_info:
            mock_mgr = MagicMock()
            mock_mgr.skip_reintegration.return_value = (False, "Error")
            MockManager.return_value = mock_mgr
            cmd_skip_reintegration(args)

        assert exc_info.value.code == 1


class TestMainFunction:
    """Tests for the main() function."""

    def test_main_no_args_with_session(self):
        """Test main shows status when no args and session active."""
        from gerrit_comments.cli import main
        with patch('argparse.ArgumentParser') as MockParser, \
             patch('gerrit_comments.parsers.setup_parsers'), \
             patch('gerrit_comments.rebase.RebaseManager') as MockManager, \
             patch('gerrit_comments.cli.cmd_status') as mock_status:
            mock_args = MagicMock()
            mock_args.command = None
            MockParser.return_value.parse_args.return_value = mock_args
            MockManager.return_value.has_active_session.return_value = True

            main()

            mock_status.assert_called_once_with(mock_args)

    def test_main_no_args_no_session(self):
        """Test main shows help when no args and no session."""
        from gerrit_comments.cli import main
        with patch('argparse.ArgumentParser') as MockParser, \
             patch('gerrit_comments.parsers.setup_parsers'), \
             patch('gerrit_comments.rebase.RebaseManager') as MockManager, \
             pytest.raises(SystemExit) as exc_info:
            mock_args = MagicMock()
            mock_args.command = None
            MockParser.return_value.parse_args.return_value = mock_args
            MockManager.return_value.has_active_session.return_value = False

            main()

        assert exc_info.value.code == 1

    def test_main_with_command(self):
        """Test main calls command handler when command given."""
        from gerrit_comments.cli import main
        with patch('argparse.ArgumentParser') as MockParser, \
             patch('gerrit_comments.parsers.setup_parsers'):
            mock_args = MagicMock()
            mock_args.command = "status"
            mock_args.func = MagicMock()
            MockParser.return_value.parse_args.return_value = mock_args

            main()

            mock_args.func.assert_called_once_with(mock_args)


class TestCmdExtractFormatted:
    """Tests for cmd_extract with formatted output."""

    def test_extract_formatted_with_threads(self):
        """Test extract with pretty output and threads."""
        from gerrit_comments.cli import cmd_extract
        args = argparse.Namespace(
            url="https://example.com/12345",
            all=False,
            no_context=False,
            context_lines=3,
            pretty=True,
        )

        with patch('gerrit_comments.cli.extract_comments') as mock_extract:
            mock_result = MagicMock()
            mock_result.to_dict.return_value = {"threads": []}
            mock_extract.return_value = mock_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_extract(args)
            assert exc_info.value.code == 0

            mock_extract.assert_called_once()

    def test_extract_value_error(self):
        """Test extract handles ValueError."""
        from gerrit_comments.cli import cmd_extract
        args = argparse.Namespace(
            url="https://example.com/12345",
            all=False,
            no_context=False,
            context_lines=3,
            pretty=False,
        )

        with patch('gerrit_comments.cli.extract_comments') as mock_extract:
            mock_extract.side_effect = ValueError("Invalid URL")
            with pytest.raises(SystemExit) as exc_info:
                cmd_extract(args)
            assert exc_info.value.code == 1


class TestCmdReply:
    """Tests for cmd_reply function."""

    def test_reply_done(self):
        """Test reply with --done flag."""
        from gerrit_comments.cli import cmd_reply
        args = argparse.Namespace(
            url="https://example.com/12345",
            thread_index=0,
            done=True,
            ack=False,
            message=None,
            resolve=False,
            pretty=False,
        )

        with patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract, \
             patch('gerrit_comments.cli.CommentReplier') as MockReplier:

            MockClient.parse_gerrit_url.return_value = ("https://example.com", 12345)

            mock_thread = MagicMock()
            mock_thread.root_comment.file_path = "foo.py"
            mock_thread.root_comment.line = 42

            mock_result = MagicMock()
            mock_result.threads = [mock_thread]
            mock_extract.return_value = mock_result

            mock_reply_result = MagicMock()
            mock_reply_result.success = True
            mock_reply_result.to_dict.return_value = {"success": True}
            MockReplier.return_value.reply_to_thread.return_value = mock_reply_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_reply(args)
            assert exc_info.value.code == 0

            MockReplier.return_value.reply_to_thread.assert_called_once()
            call_kwargs = MockReplier.return_value.reply_to_thread.call_args
            assert call_kwargs[1]['message'] == "Done"
            assert call_kwargs[1]['mark_resolved'] is True

    def test_reply_thread_index_out_of_range(self):
        """Test reply with invalid thread index."""
        from gerrit_comments.cli import cmd_reply
        args = argparse.Namespace(
            url="https://example.com/12345",
            thread_index=99,
            done=False,
            ack=False,
            message="Test",
            resolve=False,
            pretty=False,
        )

        with patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract:

            MockClient.parse_gerrit_url.return_value = ("https://example.com", 12345)

            mock_result = MagicMock()
            mock_result.threads = []
            mock_extract.return_value = mock_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_reply(args)
            assert exc_info.value.code == 1

    def test_reply_missing_message(self):
        """Test reply without message fails."""
        from gerrit_comments.cli import cmd_reply
        args = argparse.Namespace(
            url="https://example.com/12345",
            thread_index=0,
            done=False,
            ack=False,
            message=None,
            resolve=False,
            pretty=False,
        )

        with patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract:

            MockClient.parse_gerrit_url.return_value = ("https://example.com", 12345)

            mock_thread = MagicMock()
            mock_result = MagicMock()
            mock_result.threads = [mock_thread]
            mock_extract.return_value = mock_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_reply(args)
            assert exc_info.value.code == 1


class TestCmdReview:
    """Tests for cmd_review function."""

    def test_review_json_output(self):
        """Test review with JSON output."""
        from gerrit_comments.cli import cmd_review
        args = argparse.Namespace(
            url="https://example.com/12345",
            full_content=False,
            post_comments=None,
            pretty=False,
            changes_only=False,
        )

        with patch('gerrit_comments.cli.CodeReviewer') as MockReviewer:
            mock_data = MagicMock()
            mock_data.to_dict.return_value = {"test": "data"}
            MockReviewer.return_value.get_review_data.return_value = mock_data

            with pytest.raises(SystemExit) as exc_info:
                cmd_review(args)
            assert exc_info.value.code == 0

            MockReviewer.return_value.get_review_data.assert_called_once()
            mock_data.to_dict.assert_called_once()

    def test_review_changes_only(self):
        """Test review with --changes-only."""
        from gerrit_comments.cli import cmd_review
        args = argparse.Namespace(
            url="https://example.com/12345",
            full_content=False,
            post_comments=None,
            pretty=False,
            changes_only=True,
        )

        with patch('gerrit_comments.cli.CodeReviewer') as MockReviewer:
            mock_data = MagicMock()
            mock_data.to_dict.return_value = {"files": []}
            MockReviewer.return_value.get_review_data.return_value = mock_data

            with pytest.raises(SystemExit) as exc_info:
                cmd_review(args)
            assert exc_info.value.code == 0

            MockReviewer.return_value.get_review_data.assert_called_once()

    def test_review_error_handling(self):
        """Test review error handling."""
        from gerrit_comments.cli import cmd_review
        args = argparse.Namespace(
            url="https://example.com/12345",
            full_content=False,
            post_comments=None,
            pretty=False,
            changes_only=False,
        )

        with patch('gerrit_comments.cli.CodeReviewer') as MockReviewer:
            MockReviewer.return_value.get_review_data.side_effect = ValueError("Bad URL")

            with pytest.raises(SystemExit) as exc_info:
                cmd_review(args)
            assert exc_info.value.code == 1


class TestCmdSeriesComments:
    """Tests for cmd_series_comments function."""

    def test_series_comments_json(self):
        """Test series-comments with JSON output."""
        from gerrit_comments.cli import cmd_series_comments
        args = argparse.Namespace(
            url="https://example.com/12345",
            all=False,
            no_context=False,
            context_lines=3,
            pretty=False,
        )

        with patch('gerrit_comments.cli.SeriesFinder') as MockFinder:
            mock_result = MagicMock()
            mock_result.to_dict.return_value = {"patches": []}
            MockFinder.return_value.get_series_comments.return_value = mock_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_series_comments(args)
            assert exc_info.value.code == 0

            MockFinder.return_value.get_series_comments.assert_called_once()
            mock_result.to_dict.assert_called_once()

    def test_series_comments_formatted(self):
        """Test series-comments with pretty output."""
        from gerrit_comments.cli import cmd_series_comments
        args = argparse.Namespace(
            url="https://example.com/12345",
            all=False,
            no_context=False,
            context_lines=3,
            pretty=True,
        )

        with patch('gerrit_comments.cli.SeriesFinder') as MockFinder:
            mock_result = MagicMock()
            mock_result.to_dict.return_value = {"patches": []}
            MockFinder.return_value.get_series_comments.return_value = mock_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_series_comments(args)
            assert exc_info.value.code == 0

    def test_series_comments_error(self):
        """Test series-comments error handling."""
        from gerrit_comments.cli import cmd_series_comments
        args = argparse.Namespace(
            url="https://example.com/12345",
            all=False,
            no_context=False,
            context_lines=3,
            pretty=False,
        )

        with patch('gerrit_comments.cli.SeriesFinder') as MockFinder:
            MockFinder.return_value.get_series_comments.side_effect = Exception("API error")

            with pytest.raises(SystemExit) as exc_info:
                cmd_series_comments(args)
            assert exc_info.value.code == 1


class TestCmdSeriesStatus:
    """Tests for cmd_series_status function."""

    def test_series_status_success(self):
        """Test series-status displays status (now always gets JSON from backend)."""
        from gerrit_comments.cli import cmd_series_status
        args = argparse.Namespace(
            url="https://example.com/12345",
            pretty=False,
        )

        with patch('gerrit_comments.cli.show_series_status') as mock_show:
            mock_show.return_value = '{"status": "ok"}'

            with pytest.raises(SystemExit) as exc_info:
                cmd_series_status(args)
            assert exc_info.value.code == 0

            mock_show.assert_called_once_with("https://example.com/12345", output_json=True)

    def test_series_status_json(self):
        """Test series-status with JSON output."""
        from gerrit_comments.cli import cmd_series_status
        args = argparse.Namespace(
            url="https://example.com/12345",
            pretty=False,
        )

        with patch('gerrit_comments.cli.show_series_status') as mock_show:
            mock_show.return_value = '{"status": "ok"}'

            with pytest.raises(SystemExit) as exc_info:
                cmd_series_status(args)
            assert exc_info.value.code == 0

            mock_show.assert_called_once_with("https://example.com/12345", output_json=True)

    def test_series_status_error(self):
        """Test series-status error handling."""
        from gerrit_comments.cli import cmd_series_status
        args = argparse.Namespace(
            url="https://example.com/12345",
            pretty=False,
        )

        with patch('gerrit_comments.cli.show_series_status') as mock_show:
            mock_show.side_effect = Exception("API error")

            with pytest.raises(SystemExit) as exc_info:
                cmd_series_status(args)
            assert exc_info.value.code == 1


class TestCmdInteractive:
    """Tests for cmd_interactive function."""

    def test_interactive_vim_mode(self):
        """Test interactive with --vim flag."""
        from gerrit_comments.cli import cmd_interactive
        args = argparse.Namespace(
            url="https://example.com/12345",
            vim=True,
        )

        with patch('gerrit_comments.cli.run_interactive_vim') as mock_vim:
            cmd_interactive(args)
            mock_vim.assert_called_once_with("https://example.com/12345")

    def test_interactive_default_mode(self):
        """Test interactive without --vim flag."""
        from gerrit_comments.cli import cmd_interactive
        args = argparse.Namespace(
            url="https://example.com/12345",
            vim=False,
        )

        with patch('gerrit_comments.cli.run_interactive') as mock_run:
            cmd_interactive(args)
            mock_run.assert_called_once_with("https://example.com/12345")

    def test_interactive_keyboard_interrupt(self):
        """Test interactive handles keyboard interrupt."""
        from gerrit_comments.cli import cmd_interactive
        args = argparse.Namespace(
            url="https://example.com/12345",
            vim=False,
        )

        with patch('gerrit_comments.cli.run_interactive') as mock_run:
            mock_run.side_effect = KeyboardInterrupt()

            with pytest.raises(SystemExit) as exc_info:
                cmd_interactive(args)
            assert exc_info.value.code == 0

    def test_interactive_error(self):
        """Test interactive error handling."""
        from gerrit_comments.cli import cmd_interactive
        args = argparse.Namespace(
            url="https://example.com/12345",
            vim=False,
        )

        with patch('gerrit_comments.cli.run_interactive') as mock_run:
            mock_run.side_effect = Exception("TUI error")

            with pytest.raises(SystemExit) as exc_info:
                cmd_interactive(args)
            assert exc_info.value.code == 1


class TestCmdBatchReply:
    """Tests for cmd_batch_reply function."""

    def test_batch_reply_success(self, tmp_path):
        """Test batch reply with valid file."""
        from gerrit_comments.cli import cmd_batch_reply

        # Create a temp file with replies
        replies_file = tmp_path / "replies.json"
        replies_file.write_text('[{"thread_index": 0, "message": "Done", "mark_resolved": true}]')

        args = argparse.Namespace(
            url="https://example.com/12345",
            file=str(replies_file),
            pretty=False,
        )

        with patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract, \
             patch('gerrit_comments.cli.CommentReplier') as MockReplier:

            MockClient.parse_gerrit_url.return_value = ("https://example.com", 12345)

            mock_comment = MagicMock()
            mock_thread = MagicMock()
            mock_thread.root_comment = mock_comment
            mock_thread.replies = []

            mock_result = MagicMock()
            mock_result.threads = [mock_thread]
            mock_extract.return_value = mock_result

            mock_reply_result = MagicMock()
            mock_reply_result.success = True
            mock_reply_result.to_dict.return_value = {"success": True}
            MockReplier.return_value.batch_reply.return_value = [mock_reply_result]

            with pytest.raises(SystemExit) as exc_info:
                cmd_batch_reply(args)
            assert exc_info.value.code == 0

            MockReplier.return_value.batch_reply.assert_called_once()

    def test_batch_reply_out_of_range(self, tmp_path):
        """Test batch reply with invalid thread index."""
        from gerrit_comments.cli import cmd_batch_reply

        # Create a temp file with out-of-range index
        replies_file = tmp_path / "replies.json"
        replies_file.write_text('[{"thread_index": 99, "message": "Test"}]')

        args = argparse.Namespace(
            url="https://example.com/12345",
            file=str(replies_file),
            pretty=False,
        )

        with patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract, \
             patch('gerrit_comments.cli.CommentReplier') as MockReplier:

            MockClient.parse_gerrit_url.return_value = ("https://example.com", 12345)

            mock_result = MagicMock()
            mock_result.threads = []
            mock_extract.return_value = mock_result

            MockReplier.return_value.batch_reply.return_value = []

            # Should not raise, just skip the invalid item - now outputs JSON with empty results
            with pytest.raises(SystemExit) as exc_info:
                cmd_batch_reply(args)
            assert exc_info.value.code == 0

            # batch_reply called with empty list
            MockReplier.return_value.batch_reply.assert_called_once()


class TestCmdSeriesOutputFormats:
    """Tests for cmd_series output formats."""

    def test_series_urls_only(self):
        """Test series with --urls-only."""
        args = argparse.Namespace(
            url="https://example.com/12345",
            pretty=False,
            urls_only=True,
            numbers_only=False,
            include_abandoned=False,
            no_prompt=True,
            no_checkout=True,
        )

        with patch('gerrit_comments.cli.SeriesFinder') as MockFinder:
            mock_patch = MagicMock()
            mock_patch.url = "https://example.com/12345"
            mock_patch.change_number = 12345

            mock_series = MagicMock()
            mock_series.patches = [mock_patch]
            mock_series.to_dict.return_value = {"patches": []}
            MockFinder.return_value.find_series.return_value = mock_series

            with pytest.raises(SystemExit) as exc_info:
                cmd_series(args)
            assert exc_info.value.code == 0

            MockFinder.return_value.find_series.assert_called_once()

    def test_series_numbers_only(self):
        """Test series with --numbers-only."""
        args = argparse.Namespace(
            url="https://example.com/12345",
            pretty=False,
            urls_only=False,
            numbers_only=True,
            include_abandoned=False,
            no_prompt=True,
            no_checkout=True,
        )

        with patch('gerrit_comments.cli.SeriesFinder') as MockFinder:
            mock_patch = MagicMock()
            mock_patch.change_number = 12345

            mock_series = MagicMock()
            mock_series.patches = [mock_patch]
            mock_series.to_dict.return_value = {"patches": []}
            MockFinder.return_value.find_series.return_value = mock_series

            with pytest.raises(SystemExit) as exc_info:
                cmd_series(args)
            assert exc_info.value.code == 0

            MockFinder.return_value.find_series.assert_called_once()

    def test_series_full_output(self):
        """Test series with full output (fetches comments)."""
        args = argparse.Namespace(
            url="https://example.com/12345",
            pretty=False,
            urls_only=False,
            numbers_only=False,
            include_abandoned=False,
            no_prompt=True,
            no_checkout=True,  # Skip checkout for this test
        )

        with patch('gerrit_comments.cli.SeriesFinder') as MockFinder, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract:

            mock_patch = MagicMock()
            mock_patch.change_number = 12345
            mock_patch.url = "https://example.com/12345"
            mock_patch.subject = "Test patch"

            mock_series = MagicMock()
            mock_series.patches = [mock_patch]
            mock_series.target_change = 12345
            mock_series.to_dict.return_value = {"patches": []}
            MockFinder.return_value.find_series.return_value = mock_series

            # No unresolved threads
            mock_result = MagicMock()
            mock_result.threads = []
            mock_extract.return_value = mock_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_series(args)
            assert exc_info.value.code == 0

            mock_extract.assert_called_once()


class TestCmdReplyVariants:
    """Additional tests for cmd_reply variants."""

    def test_reply_with_ack(self):
        """Test reply with --ack flag."""
        from gerrit_comments.cli import cmd_reply
        args = argparse.Namespace(
            url="https://example.com/12345",
            thread_index=0,
            done=False,
            ack=True,
            message=None,
            resolve=False,
            pretty=False,
        )

        with patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract, \
             patch('gerrit_comments.cli.CommentReplier') as MockReplier:

            MockClient.parse_gerrit_url.return_value = ("https://example.com", 12345)

            mock_thread = MagicMock()
            mock_thread.root_comment.file_path = "foo.py"
            mock_thread.root_comment.line = 42

            mock_result = MagicMock()
            mock_result.threads = [mock_thread]
            mock_extract.return_value = mock_result

            mock_reply_result = MagicMock()
            mock_reply_result.success = True
            mock_reply_result.to_dict.return_value = {"success": True}
            MockReplier.return_value.reply_to_thread.return_value = mock_reply_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_reply(args)
            assert exc_info.value.code == 0

            call_kwargs = MockReplier.return_value.reply_to_thread.call_args
            assert call_kwargs[1]['message'] == "Acknowledged"
            assert call_kwargs[1]['mark_resolved'] is True

    def test_reply_failure(self):
        """Test reply when post fails."""
        from gerrit_comments.cli import cmd_reply
        args = argparse.Namespace(
            url="https://example.com/12345",
            thread_index=0,
            done=True,
            ack=False,
            message=None,
            resolve=False,
            pretty=False,
        )

        with patch('gerrit_comments.cli.GerritCommentsClient') as MockClient, \
             patch('gerrit_comments.cli.extract_comments') as mock_extract, \
             patch('gerrit_comments.cli.CommentReplier') as MockReplier:

            MockClient.parse_gerrit_url.return_value = ("https://example.com", 12345)

            mock_thread = MagicMock()
            mock_thread.root_comment.file_path = "foo.py"
            mock_thread.root_comment.line = 42

            mock_result = MagicMock()
            mock_result.threads = [mock_thread]
            mock_extract.return_value = mock_result

            mock_reply_result = MagicMock()
            mock_reply_result.success = False
            mock_reply_result.error = "API error"
            mock_reply_result.to_dict.return_value = {"success": False, "error": "API error"}
            MockReplier.return_value.reply_to_thread.return_value = mock_reply_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_reply(args)
            assert exc_info.value.code == 1


class TestCmdReviewPostComments:
    """Tests for cmd_review with post-comments."""

    def test_review_post_comments_success(self, tmp_path):
        """Test review with --post-comments."""
        from gerrit_comments.cli import cmd_review

        # Create temp file with review spec
        review_file = tmp_path / "review.json"
        review_file.write_text('{"comments": [], "message": "LGTM", "vote": 1}')

        args = argparse.Namespace(
            url="https://example.com/12345",
            full_content=False,
            post_comments=str(review_file),
            pretty=False,
            changes_only=False,
        )

        with patch('gerrit_comments.cli.CodeReviewer') as MockReviewer:
            mock_data = MagicMock()
            mock_data.change_info.change_number = 12345
            MockReviewer.return_value.get_review_data.return_value = mock_data

            mock_post_result = MagicMock()
            mock_post_result.success = True
            mock_post_result.comments_posted = 0
            mock_post_result.vote = 1
            mock_post_result.to_dict.return_value = {"success": True}
            MockReviewer.return_value.post_review.return_value = mock_post_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_review(args)
            assert exc_info.value.code == 0

            MockReviewer.return_value.post_review.assert_called_once()

    def test_review_post_comments_failure(self, tmp_path):
        """Test review with --post-comments that fails."""
        from gerrit_comments.cli import cmd_review

        # Create temp file with review spec
        review_file = tmp_path / "review.json"
        review_file.write_text('{"comments": []}')

        args = argparse.Namespace(
            url="https://example.com/12345",
            full_content=False,
            post_comments=str(review_file),
            pretty=False,
            changes_only=False,
        )

        with patch('gerrit_comments.cli.CodeReviewer') as MockReviewer:
            mock_data = MagicMock()
            mock_data.change_info.change_number = 12345
            MockReviewer.return_value.get_review_data.return_value = mock_data

            mock_post_result = MagicMock()
            mock_post_result.success = False
            mock_post_result.error = "API error"
            mock_post_result.to_dict.return_value = {"success": False, "error": "API error"}
            MockReviewer.return_value.post_review.return_value = mock_post_result

            with pytest.raises(SystemExit) as exc_info:
                cmd_review(args)
            assert exc_info.value.code == 1

    def test_review_formatted_output(self):
        """Test review with pretty output (pretty JSON)."""
        from gerrit_comments.cli import cmd_review
        args = argparse.Namespace(
            url="https://example.com/12345",
            full_content=False,
            post_comments=None,
            pretty=True,
            changes_only=False,
        )

        with patch('gerrit_comments.cli.CodeReviewer') as MockReviewer:
            mock_data = MagicMock()
            mock_data.to_dict.return_value = {"test": "data"}
            MockReviewer.return_value.get_review_data.return_value = mock_data

            with pytest.raises(SystemExit) as exc_info:
                cmd_review(args)
            assert exc_info.value.code == 0
