"""Tests for the rebase module."""

import subprocess
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from gerrit_cli.rebase import (
    RebaseManager,
    RebaseSession,
    abort_patch,
    end_session,
    finish_patch,
    rebase_status,
    work_on_patch,
)
from gerrit_cli.series import PatchInfo


class TestRebaseSession:
    """Tests for RebaseSession dataclass."""

    def test_to_dict(self):
        """Test converting RebaseSession to dict."""
        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62641,
            target_commit="abc123",
            original_head="def456",
            original_branch="main",
            series_patches=[{"change_number": 62640, "subject": "Test"}],
            started_at="2026-01-14T15:00:00",
        )

        result = session.to_dict()

        assert result["series_url"] == "https://review.whamcloud.com/62640"
        assert result["target_change"] == 62641
        assert result["target_commit"] == "abc123"
        assert result["original_head"] == "def456"
        assert result["original_branch"] == "main"
        assert len(result["series_patches"]) == 1

    def test_from_dict(self):
        """Test creating RebaseSession from dict."""
        data = {
            "series_url": "https://review.whamcloud.com/62640",
            "target_change": 62641,
            "target_commit": "abc123",
            "original_head": "def456",
            "original_branch": "main",
            "series_patches": [{"change_number": 62640, "subject": "Test"}],
            "started_at": "2026-01-14T15:00:00",
        }

        session = RebaseSession.from_dict(data)

        assert session.series_url == "https://review.whamcloud.com/62640"
        assert session.target_change == 62641
        assert session.target_commit == "abc123"


class TestRebaseManager:
    """Tests for RebaseManager class."""

    @patch("gerrit_cli.rebase_manager.Path")
    def test_init(self, mock_path):
        """Test RebaseManager initialization."""
        manager = RebaseManager()

        assert manager._session_mgr is not None
        assert manager.state_file is not None

    def test_run_git_success(self):
        """Test running git command successfully."""
        manager = RebaseManager()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="clean",
                stderr="",
            )

            result = manager._run_git(["status"])

            assert result.returncode == 0
            assert result.stdout == "clean"
            mock_run.assert_called_once()

    def test_run_git_failure(self):
        """Test running git command that fails."""
        manager = RebaseManager()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git")

            with pytest.raises(subprocess.CalledProcessError):
                manager._run_git(["status"])

    def test_check_git_repo_not_in_repo(self):
        """Test check when not in a git repository."""
        manager = RebaseManager()

        with patch("gerrit_cli.rebase_manager.git_utils.check_git_repo") as mock_check:
            mock_check.return_value = (False, "Not a git repository")

            is_valid, msg = manager.check_git_repo()

            assert not is_valid
            assert "Not in a git repository" in msg

    def test_check_git_repo_dirty_working_tree(self):
        """Test check when working tree is dirty."""
        manager = RebaseManager()

        with patch("gerrit_cli.rebase_manager.git_utils.check_git_repo") as mock_check:
            with patch("gerrit_cli.rebase_manager.git_utils.is_working_tree_clean") as mock_clean:
                mock_check.return_value = (True, "Valid git repository")
                mock_clean.return_value = (False, "Working tree is not clean. Please commit or stash your changes first.")

                is_valid, msg = manager.check_git_repo()

                assert not is_valid
                assert "Working tree is not clean" in msg

    def test_check_git_repo_clean(self):
        """Test check when git repo is clean."""
        manager = RebaseManager()

        with patch("gerrit_cli.rebase_manager.git_utils.check_git_repo") as mock_check:
            with patch("gerrit_cli.rebase_manager.git_utils.is_working_tree_clean") as mock_clean:
                mock_check.return_value = (True, "Valid git repository")
                mock_clean.return_value = (True, "Working tree is clean")

                is_valid, msg = manager.check_git_repo()

                assert is_valid
                assert "clean" in msg

    def test_get_current_branch(self):
        """Test getting current branch name."""
        manager = RebaseManager()

        with patch("gerrit_cli.rebase_manager.git_utils.get_current_branch") as mock_branch:
            mock_branch.return_value = "main"

            branch = manager.get_current_branch()

            assert branch == "main"

    def test_get_current_branch_detached_head(self):
        """Test getting branch when in detached HEAD."""
        manager = RebaseManager()

        with patch("gerrit_cli.rebase_manager.git_utils.get_current_branch") as mock_branch:
            mock_branch.return_value = None

            branch = manager.get_current_branch()

            assert branch is None

    def test_get_current_commit(self):
        """Test getting current commit hash."""
        manager = RebaseManager()

        with patch("gerrit_cli.rebase_manager.git_utils.get_current_commit") as mock_commit:
            mock_commit.return_value = "abc123"

            commit = manager.get_current_commit()

            assert commit == "abc123"

    def test_has_active_session_true(self):
        """Test checking for active session when it exists."""
        manager = RebaseManager()

        with patch("pathlib.Path.exists") as mock_exists:
            mock_exists.return_value = True

            assert manager.has_active_session()

    def test_has_active_session_false(self):
        """Test checking for active session when it doesn't exist."""
        manager = RebaseManager()

        with patch("pathlib.Path.exists") as mock_exists:
            mock_exists.return_value = False

            assert not manager.has_active_session()

    def test_save_and_load_session(self):
        """Test saving and loading session state."""
        manager = RebaseManager()
        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62641,
            target_commit="abc123",
            original_head="def456",
            original_branch="main",
            series_patches=[],
            started_at="2026-01-14T15:00:00",
        )

        import json
        with patch("builtins.open", create=True) as mock_open:
            mock_file = MagicMock()
            mock_open.return_value.__enter__.return_value = mock_file

            # Test save
            manager.save_session(session)
            mock_open.assert_called()

            # Test load with existing file
            with patch("pathlib.Path.exists") as mock_exists:
                mock_exists.return_value = True
                mock_file_read = MagicMock()
                mock_file_read.read.return_value = json.dumps(session.to_dict())
                mock_open.return_value.__enter__.return_value = mock_file_read

                loaded = manager.load_session()
                assert loaded.target_change == 62641

    def test_clear_session(self):
        """Test clearing session state."""
        manager = RebaseManager()

        with patch("pathlib.Path.exists") as mock_exists, \
             patch("pathlib.Path.unlink"):
            mock_exists.return_value = True

            manager.clear_session()

            # Check that unlink was called (on the actual path instance)
            # Note: This is a simplified test

    @patch("gerrit_cli.rebase_manager.extract_comments")
    @patch("gerrit_cli.rebase_manager.SeriesFinder")
    def test_start_rebase_to_patch_not_in_repo(self, mock_finder_cls, mock_extract):
        """Test starting rebase when not in a git repo."""
        manager = RebaseManager()

        with patch.object(manager, "check_git_repo") as mock_check:
            mock_check.return_value = (False, "Not in a git repository")

            success, msg = manager.start_rebase_to_patch(
                "https://review.whamcloud.com/62640", 62641
            )

            assert not success
            assert "Not in a git repository" in msg

    @patch("gerrit_cli.rebase_manager.extract_comments")
    @patch("gerrit_cli.rebase_manager.SeriesFinder")
    def test_start_rebase_to_patch_continues_existing_session(
        self, mock_finder_cls, mock_extract
    ):
        """Test starting rebase when already in a session continues with that session."""
        manager = RebaseManager()

        # Create an existing session with patches
        existing_session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="abc123",
            original_head="original123",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "abc123"},
                {"change_number": 62641, "subject": "Second", "commit": "def456"},
            ],
            started_at="2025-01-01T00:00:00",
        )

        with patch.object(manager, "check_git_repo") as mock_check, \
             patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "_run_git") as mock_git, \
             patch.object(manager, "save_session"):
            mock_check.return_value = (True, "ready")
            mock_load.return_value = existing_session
            mock_extract.return_value = Mock(threads=[])

            success, msg = manager.start_rebase_to_patch(
                "https://review.whamcloud.com/62640", 62641
            )

            # Should succeed and checkout the patch from existing session
            assert success
            # Should have tried to checkout the commit from existing session
            mock_git.assert_called_with(["checkout", "def456"])

    @patch("gerrit_cli.rebase_manager.extract_comments")
    @patch("gerrit_cli.rebase_manager.SeriesFinder")
    def test_start_rebase_to_patch_patch_not_found(self, mock_finder_cls, mock_extract):
        """Test starting rebase when patch not in series."""
        # Set up the mock BEFORE creating the manager
        mock_series = Mock()
        mock_series.patches = [
            PatchInfo(62640, "First", "abc", "parent", "NEW", "url1"),
            PatchInfo(62642, "Third", "def", "parent", "NEW", "url2"),
        ]
        mock_series.error = None
        mock_finder_cls.return_value.find_series.return_value = mock_series

        manager = RebaseManager()

        with patch.object(manager, "check_git_repo") as mock_check, \
             patch.object(manager, "has_active_session") as mock_active:
            mock_check.return_value = (True, "ready")
            mock_active.return_value = False

            success, msg = manager.start_rebase_to_patch(
                "https://review.whamcloud.com/62640", 62641
            )

            assert not success
            assert "not found in series" in msg

    def test_finish_rebase_no_session(self):
        """Test finishing rebase when no session exists."""
        manager = RebaseManager()

        with patch.object(manager, "load_session") as mock_load:
            mock_load.return_value = None

            success, msg = manager.finish_rebase()

            assert not success
            assert "No active rebase session" in msg

    def test_abort_rebase_no_session(self):
        """Test aborting rebase when no session exists."""
        manager = RebaseManager()

        with patch.object(manager, "load_session") as mock_load:
            mock_load.return_value = None

            success, msg = manager.abort_rebase()

            assert not success
            assert "No active rebase session" in msg

    def test_get_status_no_session(self):
        """Test getting status when no session exists."""
        manager = RebaseManager()

        with patch.object(manager, "load_session") as mock_load:
            mock_load.return_value = None

            has_session, msg = manager.get_status()

            assert not has_session
            assert "No active rebase session" in msg

    def test_get_status_with_session(self):
        """Test getting status with an active session."""
        manager = RebaseManager()
        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62641,
            target_commit="abc123",
            original_head="def456",
            original_branch="main",
            series_patches=[],
            started_at="2026-01-14T15:00:00",
        )

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "get_current_branch") as mock_branch:
            mock_load.return_value = session
            mock_commit.return_value = "xyz789"
            mock_branch.return_value = "main"

            has_session, msg = manager.get_status()

            assert has_session
            assert "62641" in msg
            assert "xyz789" in msg


class TestModuleFunctions:
    """Tests for module-level functions."""

    @patch("gerrit_cli.patch_workflow.RebaseManager")
    def test_work_on_patch(self, mock_manager_cls):
        """Test work_on_patch function."""
        mock_manager = Mock()
        mock_manager_cls.return_value = mock_manager
        mock_manager.start_rebase_to_patch.return_value = (True, "Success")

        success, msg = work_on_patch("https://review.whamcloud.com/62640", 62641)

        assert success
        assert msg == "Success"
        mock_manager.start_rebase_to_patch.assert_called_once_with(
            "https://review.whamcloud.com/62640", 62641
        )

    @patch("gerrit_cli.patch_workflow.RebaseManager")
    def test_finish_patch(self, mock_manager_cls):
        """Test finish_patch function."""
        mock_manager = Mock()
        mock_manager_cls.return_value = mock_manager
        mock_manager.finish_rebase.return_value = (True, "Done")
        # Need to return None for load_session to avoid auto_next logic
        mock_manager.load_session.return_value = None

        success, msg = finish_patch()

        assert success
        assert msg == "Done"
        mock_manager.finish_rebase.assert_called_once()

    @patch("gerrit_cli.patch_workflow.RebaseManager")
    def test_abort_patch(self, mock_manager_cls):
        """Test abort_patch function."""
        mock_manager = Mock()
        mock_manager_cls.return_value = mock_manager
        mock_manager.abort_rebase.return_value = (True, "Aborted")

        success, msg = abort_patch()

        assert success
        assert msg == "Aborted"
        mock_manager.abort_rebase.assert_called_once()

    @patch("gerrit_cli.patch_workflow.RebaseManager")
    def test_rebase_status(self, mock_manager_cls):
        """Test rebase_status function."""
        mock_manager = Mock()
        mock_manager_cls.return_value = mock_manager
        mock_manager.get_status.return_value = (True, "Status info")

        has_session, msg = rebase_status()

        assert has_session
        assert msg == "Status info"
        mock_manager.get_status.assert_called_once()

    @patch("gerrit_cli.patch_workflow.RebaseManager")
    def test_end_session_no_session(self, mock_manager_cls):
        """Test end_session with no active session."""
        mock_manager = Mock()
        mock_manager_cls.return_value = mock_manager
        mock_manager.load_session.return_value = None

        success, msg = end_session()

        assert not success
        assert "No active rebase session" in msg

    @patch("gerrit_cli.patch_workflow.RebaseManager")
    def test_end_session_success(self, mock_manager_cls):
        """Test end_session with active session."""
        mock_manager = Mock()
        mock_manager_cls.return_value = mock_manager
        mock_manager.load_session.return_value = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62641,
            target_commit="abc123",
            original_head="def456",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "aaa111"},
                {"change_number": 62641, "subject": "Second", "commit": "bbb222"},
            ],
            started_at="2025-01-01T00:00:00",
        )
        mock_manager.get_current_commit.return_value = "bbb222"

        success, msg = end_session()

        assert success
        assert "Session ended" in msg
        # Output format shows tip commit, not change numbers
        assert "bbb222" in msg
        mock_manager.clear_session.assert_called_once()


class TestFinishRebaseWithDescendants:
    """Tests for finish_rebase with descendant patches."""

    def test_finish_rebase_cherry_picks_descendants(self):
        """Test that finish_rebase cherry-picks descendant patches."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Second", "commit": "original_b"},
                {"change_number": 62642, "subject": "Third", "commit": "original_c"},
            ],
            started_at="2025-01-01T00:00:00",
        )

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "_run_git") as mock_git, \
             patch.object(manager, "save_session"):
            mock_load.return_value = session
            # Return amended commit first, then new commits after cherry-picks
            # Called: once for amended, once after each cherry-pick, once at end
            mock_commit.side_effect = [
                "amended_a", "new_b", "new_c", "new_c"
            ]

            success, msg = manager.finish_rebase()

            assert success
            # Should have cherry-picked both descendants
            cherry_pick_calls = [
                c for c in mock_git.call_args_list
                if c[0][0][0] == "cherry-pick"
            ]
            assert len(cherry_pick_calls) == 2
            assert cherry_pick_calls[0] == call(["cherry-pick", "original_b"])
            assert cherry_pick_calls[1] == call(["cherry-pick", "original_c"])

    def test_finish_rebase_handles_cherry_pick_conflict(self):
        """Test that finish_rebase handles cherry-pick conflicts gracefully."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Second", "commit": "original_b"},
            ],
            started_at="2025-01-01T00:00:00",
        )

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "_run_git") as mock_git, \
             patch.object(manager, "_is_cherry_pick_in_progress") as mock_cherry, \
             patch.object(manager, "_has_unmerged_files") as mock_unmerged, \
             patch.object(manager, "save_session"):
            mock_load.return_value = session
            mock_commit.return_value = "amended_a"
            mock_cherry.return_value = False
            mock_unmerged.return_value = True  # Has conflicts
            # Cherry-pick fails with conflict
            mock_git.side_effect = subprocess.CalledProcessError(1, "git")

            success, msg = manager.finish_rebase()

            assert not success
            assert "CONFLICT" in msg
            assert "62641" in msg

    def test_finish_rebase_tracks_pending_cherry_pick(self):
        """Test that finish_rebase tracks pending cherry-pick for conflict recovery."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Second", "commit": "original_b"},
            ],
            started_at="2025-01-01T00:00:00",
        )

        saved_sessions = []

        def capture_save(s):
            saved_sessions.append(s.to_dict().copy())

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "_run_git") as mock_git, \
             patch.object(manager, "_is_cherry_pick_in_progress") as mock_cherry, \
             patch.object(manager, "_has_unmerged_files") as mock_unmerged, \
             patch.object(manager, "save_session", side_effect=capture_save):
            mock_load.return_value = session
            mock_commit.return_value = "amended_a"
            mock_cherry.return_value = False
            mock_unmerged.return_value = True  # Has conflicts
            # Cherry-pick fails with conflict
            mock_git.side_effect = subprocess.CalledProcessError(1, "git")

            success, msg = manager.finish_rebase()

            assert not success
            # Should have saved pending_cherry_pick before attempting
            assert any(s.get("pending_cherry_pick") == 62641 for s in saved_sessions)

    def test_finish_rebase_detects_manual_skip(self):
        """Test that finish_rebase detects when user ran git cherry-pick --skip."""
        manager = RebaseManager()

        # Session with a pending cherry-pick that was skipped
        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Second (skipped)", "commit": "original_b"},
                {"change_number": 62642, "subject": "Third", "commit": "original_c"},
            ],
            started_at="2025-01-01T00:00:00",
            pending_cherry_pick=62641,  # Was pending when conflict happened
        )

        saved_sessions = []

        def capture_save(s):
            saved_sessions.append(s.to_dict().copy())

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "_run_git") as mock_git, \
             patch.object(manager, "_is_cherry_pick_in_progress") as mock_cherry, \
             patch.object(manager, "save_session", side_effect=capture_save):
            mock_load.return_value = session
            # No cherry-pick in progress = user ran --skip
            mock_cherry.return_value = False
            mock_commit.side_effect = ["amended_a", "new_c", "new_c"]
            mock_git.return_value = subprocess.CompletedProcess([], 0, "", "")

            success, msg = manager.finish_rebase()

            assert success
            # Should have added 62641 to skipped_changes
            assert any(62641 in s.get("skipped_changes", []) for s in saved_sessions)
            # Should have only cherry-picked 62642 (not 62641)
            cherry_pick_calls = [
                c for c in mock_git.call_args_list
                if len(c[0]) > 0 and len(c[0][0]) > 0 and c[0][0][0] == "cherry-pick"
            ]
            assert len(cherry_pick_calls) == 1
            assert cherry_pick_calls[0] == call(["cherry-pick", "original_c"])

    def test_finish_rebase_skips_already_rebased(self):
        """Test that finish_rebase skips patches already in rebased_changes."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Second", "commit": "original_b"},
                {"change_number": 62642, "subject": "Third", "commit": "original_c"},
            ],
            started_at="2025-01-01T00:00:00",
            rebased_changes=[62641],  # Already rebased
        )

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "_run_git") as mock_git, \
             patch.object(manager, "_is_cherry_pick_in_progress") as mock_cherry, \
             patch.object(manager, "save_session"):
            mock_load.return_value = session
            mock_cherry.return_value = False
            mock_commit.side_effect = ["amended_a", "new_c", "new_c"]
            mock_git.return_value = subprocess.CompletedProcess([], 0, "", "")

            success, msg = manager.finish_rebase()

            assert success
            # Should only cherry-pick 62642
            cherry_pick_calls = [
                c for c in mock_git.call_args_list
                if len(c[0]) > 0 and len(c[0][0]) > 0 and c[0][0][0] == "cherry-pick"
            ]
            assert len(cherry_pick_calls) == 1
            assert cherry_pick_calls[0] == call(["cherry-pick", "original_c"])

    def test_finish_rebase_auto_skips_empty_commits(self):
        """Test that finish_rebase auto-skips empty commits (already in upstream)."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Empty", "commit": "original_b"},
                {"change_number": 62642, "subject": "Third", "commit": "original_c"},
            ],
            started_at="2025-01-01T00:00:00",
        )

        call_count = [0]

        def mock_git_behavior(args):
            call_count[0] += 1
            if args[0] == "cherry-pick":
                if args[1] == "original_b":
                    # First cherry-pick fails (empty commit)
                    raise subprocess.CalledProcessError(1, "git")
                # Second one succeeds
                return subprocess.CompletedProcess([], 0, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        saved_sessions = []

        def capture_save(s):
            saved_sessions.append(s.to_dict().copy())

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "_run_git", side_effect=mock_git_behavior), \
             patch.object(manager, "_is_cherry_pick_in_progress") as mock_cherry, \
             patch.object(manager, "_has_unmerged_files") as mock_unmerged, \
             patch.object(manager, "save_session", side_effect=capture_save):
            mock_load.return_value = session
            mock_cherry.return_value = False
            # No unmerged files = empty commit, not a conflict
            mock_unmerged.return_value = False
            mock_commit.side_effect = ["amended_a", "new_c", "new_c"]

            success, msg = manager.finish_rebase()

            assert success
            # Should show that 62641 was skipped as empty
            assert "empty" in msg.lower() or any(
                62641 in s.get("skipped_changes", []) for s in saved_sessions
            )

    def test_finish_rebase_continues_in_progress_cherry_pick(self):
        """Test that finish_rebase continues an in-progress cherry-pick."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Second", "commit": "original_b"},
            ],
            started_at="2025-01-01T00:00:00",
            pending_cherry_pick=62641,
        )

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "get_current_commit") as mock_commit, \
             patch.object(manager, "_run_git") as mock_git, \
             patch.object(manager, "_is_cherry_pick_in_progress") as mock_cherry, \
             patch.object(manager, "_has_unmerged_files") as mock_unmerged, \
             patch.object(manager, "save_session"):
            mock_load.return_value = session
            # Cherry-pick in progress, conflicts resolved
            mock_cherry.side_effect = [True, False]  # In progress first, then done
            mock_unmerged.return_value = False  # Conflicts resolved
            mock_commit.side_effect = ["new_b", "new_b"]
            mock_git.return_value = subprocess.CompletedProcess([], 0, "", "")

            success, msg = manager.finish_rebase()

            assert success
            # Should have run cherry-pick --continue
            continue_calls = [
                c for c in mock_git.call_args_list
                if c[0][0] == ["cherry-pick", "--continue"]
            ]
            assert len(continue_calls) == 1

    def test_finish_rebase_prompts_for_unresolved_conflicts(self):
        """Test that finish_rebase prompts user when conflicts not resolved."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62640,
            target_commit="original_a",
            original_head="original_tip",
            original_branch="main",
            series_patches=[
                {"change_number": 62640, "subject": "First", "commit": "original_a"},
                {"change_number": 62641, "subject": "Second", "commit": "original_b"},
            ],
            started_at="2025-01-01T00:00:00",
            pending_cherry_pick=62641,
        )

        with patch.object(manager, "load_session") as mock_load, \
             patch.object(manager, "_is_cherry_pick_in_progress") as mock_cherry, \
             patch.object(manager, "_has_unmerged_files") as mock_unmerged:
            mock_load.return_value = session
            mock_cherry.return_value = True  # Cherry-pick still in progress
            mock_unmerged.return_value = True  # Still have conflicts

            success, msg = manager.finish_rebase()

            assert not success
            assert "Unresolved conflicts" in msg
            assert "git add" in msg




def _make_reintegration_dict(
    active=False,
    stale_changes=None,
    current_stale_idx=0,
    pending_descendants=None
):
    """Helper to create a reintegration state dict."""
    return {
        'active': active,
        'stale_changes': stale_changes or [],
        'current_stale_idx': current_stale_idx,
        'pending_descendants': pending_descendants or [],
    }


class TestReintegration:
    """Tests for series reintegration functionality."""

    def test_session_reintegration_fields(self):
        """Test RebaseSession has reintegration fields."""
        reint = _make_reintegration_dict(
            active=True,
            stale_changes=[{"change_number": 100, "old_revision": 1,
                            "current_revision": 2, "subject": "Stale"}],
            current_stale_idx=0,
            pending_descendants=[101, 102],
        )
        session = RebaseSession(
            series_url="https://review.whamcloud.com/62640",
            target_change=62641,
            target_commit="abc123",
            original_head="def456",
            original_branch="main",
            series_patches=[{"change_number": 62640, "subject": "Test"}],
            started_at="2026-01-14T15:00:00",
            reintegration=reint,
        )

        assert session.reintegrating is True
        state = session.reintegration_state
        assert len(state.stale_changes) == 1
        assert state.current_stale_idx == 0
        assert state.pending_descendants == [101, 102]

    def test_session_from_dict_with_reintegration(self):
        """Test loading session with reintegration state."""
        data = {
            "series_url": "https://review.whamcloud.com/62640",
            "target_change": 62641,
            "target_commit": "abc123",
            "original_head": "def456",
            "original_branch": "main",
            "series_patches": [{"change_number": 62640, "subject": "Test"}],
            "started_at": "2026-01-14T15:00:00",
            "reintegration": _make_reintegration_dict(
                active=True,
                stale_changes=[{"change_number": 100}],
                current_stale_idx=1,
                pending_descendants=[103],
            ),
        }

        session = RebaseSession.from_dict(data)

        assert session.reintegrating is True
        state = session.reintegration_state
        assert len(state.stale_changes) == 1
        assert state.current_stale_idx == 1
        assert state.pending_descendants == [103]

    def test_session_from_dict_without_reintegration_fields(self):
        """Test loading session without reintegration fields."""
        data = {
            "series_url": "https://review.whamcloud.com/62640",
            "target_change": 62641,
            "target_commit": "abc123",
            "original_head": "def456",
            "original_branch": "main",
            "series_patches": [{"change_number": 62640, "subject": "Test"}],
            "started_at": "2026-01-14T15:00:00",
        }

        session = RebaseSession.from_dict(data)

        assert session.reintegrating is False
        state = session.reintegration_state
        assert state.stale_changes == []
        assert state.current_stale_idx == 0
        assert state.pending_descendants == []

    @patch.object(RebaseManager, '_run_git')
    @patch.object(RebaseManager, '_fetch_gerrit_commit')
    @patch.object(RebaseManager, 'get_current_commit')
    @patch.object(RebaseManager, 'get_current_branch')
    @patch.object(RebaseManager, 'save_session')
    @patch.object(RebaseManager, 'load_session')
    def test_start_reintegration_single_stale(
        self, mock_load, mock_save, mock_branch, mock_commit, mock_fetch, mock_git
    ):
        """Test starting reintegration with a single stale change."""
        from gerrit_cli.series import PatchInfo, PatchSeries, StaleChangeInfo

        mock_load.return_value = None
        mock_commit.return_value = "original123"
        mock_branch.return_value = "main"
        mock_fetch.return_value = (True, "newcommit456")

        manager = RebaseManager()

        # Create a series with one stale change
        # Series: A (tip) -> B -> C (stale) -> D (base)
        patches = [
            PatchInfo(change_number=100, subject="D - Base", commit="d1",
                      parent_commit="base", status="NEW", url="http://test/100"),
            PatchInfo(change_number=101, subject="C - Stale", commit="c1",
                      parent_commit="d1", status="NEW", url="http://test/101"),
            PatchInfo(change_number=102, subject="B", commit="b1",
                      parent_commit="c1", status="NEW", url="http://test/102"),
            PatchInfo(change_number=103, subject="A - Tip", commit="a1",
                      parent_commit="b1", status="NEW", url="http://test/103"),
        ]
        stale_info = [
            StaleChangeInfo(change_number=101, old_revision=1,
                            current_revision=2, still_in_series=True,
                            subject="C - Stale")
        ]
        series = PatchSeries(
            patches=patches,
            error="The following changes have newer patchsets: 101. "
                  "The series can be reintegrated automatically.",
            stale_changes=[101],
            stale_info=stale_info,
            needs_reintegration=True,
        )

        # Call _start_reintegration
        with patch.object(manager, '_continue_reintegration') as mock_continue:
            mock_continue.return_value = (True, "Continuing...")
            success, msg = manager._start_reintegration(
                "http://test/101", 103, series
            )

        assert success
        # Verify session was saved with reintegration state
        saved_session = mock_save.call_args[0][0]
        assert saved_session.reintegrating is True
        state = saved_session.reintegration_state
        assert len(state.stale_changes) == 1
        assert state.stale_changes[0]['change_number'] == 101
        # Changes to rebase are B and A (indices 2 and 3)
        assert state.pending_descendants == [102, 103]

    @patch.object(RebaseManager, '_run_git')
    @patch.object(RebaseManager, '_fetch_gerrit_commit')
    @patch.object(RebaseManager, 'get_current_commit')
    @patch.object(RebaseManager, 'save_session')
    @patch.object(RebaseManager, 'load_session')
    def test_continue_reintegration_success(
        self, mock_load, mock_save, mock_commit, mock_fetch, mock_git
    ):
        """Test continuing reintegration with successful cherry-pick."""
        mock_commit.return_value = "newcommit789"

        manager = RebaseManager()

        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[
                {"change_number": 100, "subject": "D", "commit": "d1"},
                {"change_number": 101, "subject": "C", "commit": "c2"},
                {"change_number": 102, "subject": "B", "commit": "b1"},
                {"change_number": 103, "subject": "A", "commit": "a1"},
            ],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(
                active=True,
                stale_changes=[{"change_number": 101, "old_revision": 1,
                                "current_revision": 2, "subject": "C"}],
                current_stale_idx=0,
                pending_descendants=[102, 103],
            ),
        )

        # Mock successful cherry-pick
        mock_git.return_value = Mock(returncode=0, stdout="b1")

        with patch.object(manager, '_is_cherry_pick_in_progress', return_value=False):
            with patch.object(manager, '_advance_reintegration') as mock_advance:
                mock_advance.return_value = (True, "Done!")
                success, msg = manager._continue_reintegration(session)

        # Should have cherry-picked both changes and advanced
        assert mock_advance.called

    @patch.object(RebaseManager, 'load_session')
    @patch.object(RebaseManager, 'save_session')
    def test_complete_reintegration(self, mock_save, mock_load):
        """Test completing reintegration clears state."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[
                {"change_number": 100, "subject": "D", "commit": "d1"},
                {"change_number": 101, "subject": "C", "commit": "c2"},
            ],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(
                active=True,
                stale_changes=[{"change_number": 101}],
                current_stale_idx=0,
                pending_descendants=[],
            ),
            rebased_changes=[102, 103],
        )

        success, msg = manager._complete_reintegration(session)

        assert success
        # The message is printed to stdout, msg is the follow-up message
        # Check that reintegration state was cleared
        saved_session = mock_save.call_args[0][0]
        assert saved_session.reintegrating is False
    @patch.object(RebaseManager, 'load_session')
    def test_continue_reintegration_no_session(self, mock_load):
        """Test continue_reintegration with no active session."""
        mock_load.return_value = None
        manager = RebaseManager()

        success, msg = manager.continue_reintegration()

        assert not success
        assert "No active session" in msg

    @patch.object(RebaseManager, 'load_session')
    def test_continue_reintegration_not_in_reintegration_mode(self, mock_load):
        """Test continue_reintegration when not in reintegration mode."""
        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(active=False),
        )
        mock_load.return_value = session
        manager = RebaseManager()

        success, msg = manager.continue_reintegration()

        assert not success
        assert "Not in reintegration mode" in msg

    @patch.object(RebaseManager, 'load_session')
    def test_skip_reintegration_no_session(self, mock_load):
        """Test skip_reintegration with no active session."""
        mock_load.return_value = None
        manager = RebaseManager()

        success, msg = manager.skip_reintegration()

        assert not success
        assert "No active session" in msg

    @patch.object(RebaseManager, 'load_session')
    def test_skip_reintegration_not_in_reintegration_mode(self, mock_load):
        """Test skip_reintegration when not in reintegration mode."""
        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(active=False),
        )
        mock_load.return_value = session
        manager = RebaseManager()

        success, msg = manager.skip_reintegration()

        assert not success
        assert "Not in reintegration mode" in msg

    @patch.object(RebaseManager, 'load_session')
    def test_skip_reintegration_no_pending(self, mock_load):
        """Test skip_reintegration with no pending cherry-pick."""
        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(active=True),
            pending_cherry_pick=None,  # No pending
        )
        mock_load.return_value = session
        manager = RebaseManager()

        success, msg = manager.skip_reintegration()

        assert not success
        assert "No pending cherry-pick" in msg

    @patch.object(RebaseManager, '_run_git')
    @patch.object(RebaseManager, 'load_session')
    @patch.object(RebaseManager, 'save_session')
    def test_skip_reintegration_success(self, mock_save, mock_load, mock_git):
        """Test successfully skipping a change during reintegration."""
        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[
                {"change_number": 102, "subject": "B", "commit": "b1"},
                {"change_number": 103, "subject": "A", "commit": "a1"},
            ],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(
                active=True,
                stale_changes=[{"change_number": 101}],
                current_stale_idx=0,
                pending_descendants=[102, 103],
            ),
            pending_cherry_pick=102,
        )
        mock_load.return_value = session

        manager = RebaseManager()

        with patch.object(manager, '_continue_reintegration') as mock_continue:
            mock_continue.return_value = (True, "Continuing...")
            success, msg = manager.skip_reintegration()

        assert success
        # Verify 102 was added to skipped
        saved_session = mock_save.call_args[0][0]
        assert 102 in saved_session.skipped_changes

    def test_format_reintegration_conflict_message(self):
        """Test formatting conflict message during reintegration."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[
                {"change_number": 102, "subject": "Fix the bug", "commit": "b1"},
            ],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(active=True),
        )

        msg = manager._format_reintegration_conflict_message(session, 102)

        assert "CONFLICT" in msg or "conflict" in msg.lower()
        assert "102" in msg
        assert "Fix the bug" in msg

    @patch.object(RebaseManager, '_run_git')
    @patch.object(RebaseManager, '_fetch_gerrit_commit')
    @patch.object(RebaseManager, 'get_current_commit')
    @patch.object(RebaseManager, 'save_session')
    def test_advance_reintegration_to_next_stale(
        self, mock_save, mock_commit, mock_fetch, mock_git
    ):
        """Test advancing to the next stale change."""
        mock_commit.return_value = "newcommit"
        mock_fetch.return_value = (True, "fetched123")

        manager = RebaseManager()

        # Session with two stale changes, first one done
        session = RebaseSession(
            series_url="http://test",
            target_change=105,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[
                {"change_number": 100, "subject": "E - Base", "commit": "e1"},
                {"change_number": 101, "subject": "D - Stale1", "commit": "d1"},
                {"change_number": 102, "subject": "C", "commit": "c1"},
                {"change_number": 103, "subject": "B - Stale2", "commit": "b1"},
                {"change_number": 104, "subject": "A", "commit": "a1"},
                {"change_number": 105, "subject": "Tip", "commit": "t1"},
            ],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(
                active=True,
                stale_changes=[
                    {"change_number": 101, "old_revision": 1, "current_revision": 2,
                     "subject": "D - Stale1"},
                    {"change_number": 103, "old_revision": 1, "current_revision": 2,
                     "subject": "B - Stale2"},
                ],
                current_stale_idx=0,  # Will be incremented to 1
                pending_descendants=[],  # First stale done
            ),
        )

        with patch.object(manager, '_continue_reintegration') as mock_continue:
            mock_continue.return_value = (True, "Continuing...")
            success, msg = manager._advance_reintegration(session)

        # Should have fetched the second stale change
        mock_fetch.assert_called_with(103)

    @patch.object(RebaseManager, 'save_session')
    def test_advance_reintegration_complete(self, mock_save):
        """Test advancing when all stale changes are done."""
        manager = RebaseManager()

        session = RebaseSession(
            series_url="http://test",
            target_change=103,
            target_commit="c2",
            original_head="original123",
            original_branch="main",
            series_patches=[],
            started_at="2026-01-14T15:00:00",
            reintegration=_make_reintegration_dict(
                active=True,
                stale_changes=[{"change_number": 101}],
                current_stale_idx=0,  # Will be incremented to 1, >= len(stale)
                pending_descendants=[],
            ),
            rebased_changes=[102, 103],
        )

        success, msg = manager._advance_reintegration(session)

        assert success
        # The completion message is printed to stdout, msg is the follow-up

    @patch.object(RebaseManager, '_run_git')
    @patch.object(RebaseManager, '_fetch_gerrit_commit')
    @patch.object(RebaseManager, 'get_current_commit')
    @patch.object(RebaseManager, 'get_current_branch')
    @patch.object(RebaseManager, 'save_session')
    @patch.object(RebaseManager, 'load_session')
    def test_start_reintegration_multiple_stale(
        self, mock_load, mock_save, mock_branch, mock_commit, mock_fetch, mock_git
    ):
        """Test starting reintegration with multiple stale changes."""
        from gerrit_cli.series import PatchInfo, PatchSeries, StaleChangeInfo

        mock_load.return_value = None
        mock_commit.return_value = "original123"
        mock_branch.return_value = "main"
        mock_fetch.return_value = (True, "newcommit456")

        manager = RebaseManager()

        # Series: A (tip) -> B (stale) -> C -> D (stale) -> E (base)
        patches = [
            PatchInfo(change_number=100, subject="E - Base", commit="e1",
                      parent_commit="base", status="NEW", url="http://test/100"),
            PatchInfo(change_number=101, subject="D - Stale", commit="d1",
                      parent_commit="e1", status="NEW", url="http://test/101"),
            PatchInfo(change_number=102, subject="C", commit="c1",
                      parent_commit="d1", status="NEW", url="http://test/102"),
            PatchInfo(change_number=103, subject="B - Stale", commit="b1",
                      parent_commit="c1", status="NEW", url="http://test/103"),
            PatchInfo(change_number=104, subject="A - Tip", commit="a1",
                      parent_commit="b1", status="NEW", url="http://test/104"),
        ]
        stale_info = [
            StaleChangeInfo(change_number=101, old_revision=1,
                            current_revision=2, still_in_series=True,
                            subject="D - Stale"),
            StaleChangeInfo(change_number=103, old_revision=1,
                            current_revision=2, still_in_series=True,
                            subject="B - Stale"),
        ]
        series = PatchSeries(
            patches=patches,
            error="The following changes have newer patchsets: 101, 103. "
                  "The series can be reintegrated automatically.",
            stale_changes=[101, 103],
            stale_info=stale_info,
            needs_reintegration=True,
        )

        with patch.object(manager, '_continue_reintegration') as mock_continue:
            mock_continue.return_value = (True, "Continuing...")
            success, msg = manager._start_reintegration(
                "http://test/104", 104, series
            )

        assert success
        # Verify session has reintegration state
        saved_session = mock_save.call_args[0][0]
        state = saved_session.reintegration_state
        assert len(state.stale_changes) == 2
        # First stale is 101 (D), so pending changes are C, B, A (102, 103, 104)
        assert state.pending_descendants == [102, 103, 104]
