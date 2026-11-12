"""Tests for the git_utils module."""

import subprocess
from unittest.mock import MagicMock, patch


class TestGitRunner:
    """Test the GitRunner class."""

    def test_run_success(self):
        """Test successful git command execution."""
        from gerrit_cli.git_utils import GitRunner

        runner = GitRunner()
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="output\n",
                stderr="",
                returncode=0
            )
            result = runner.run(["status"])

            mock_run.assert_called_once_with(
                ["git", "status"],
                capture_output=True,
                text=True,
                check=True,
                cwd=None,
            )
            assert result.stdout == "output\n"

    def test_run_with_cwd(self):
        """Test git command with working directory."""
        from gerrit_cli.git_utils import GitRunner

        runner = GitRunner(cwd="/some/path")
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(["status"])

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs['cwd'] == "/some/path"


class TestCheckGitRepo:
    """Test check_git_repo function."""

    def test_valid_repo(self):
        """Test detecting a valid git repository."""
        from gerrit_cli.git_utils import check_git_repo

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="true\n",
                returncode=0
            )
            is_valid, msg = check_git_repo()

            assert is_valid is True
            assert "Valid" in msg

    def test_not_a_repo(self):
        """Test detecting not a git repository."""
        from gerrit_cli.git_utils import check_git_repo

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                128, ["git"], stderr="fatal: not a git repository"
            )
            is_valid, msg = check_git_repo()

            assert is_valid is False
            assert "Not a git repository" in msg

    def test_git_not_installed(self):
        """Test when git is not installed."""
        from gerrit_cli.git_utils import check_git_repo

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = FileNotFoundError()
            is_valid, msg = check_git_repo()

            assert is_valid is False
            assert "not installed" in msg


class TestGetCurrentBranch:
    """Test get_current_branch function."""

    def test_on_branch(self):
        """Test getting branch name when on a branch."""
        from gerrit_cli.git_utils import get_current_branch

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout="main\n")
            branch = get_current_branch()
            assert branch == "main"

    def test_detached_head(self):
        """Test getting None when in detached HEAD state."""
        from gerrit_cli.git_utils import get_current_branch

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout="HEAD\n")
            branch = get_current_branch()
            assert branch is None

    def test_error(self):
        """Test getting None on error."""
        from gerrit_cli.git_utils import get_current_branch

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, ["git"])
            branch = get_current_branch()
            assert branch is None


class TestGetCurrentCommit:
    """Test get_current_commit function."""

    def test_success(self):
        """Test getting current commit hash."""
        from gerrit_cli.git_utils import get_current_commit

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="abc123def456\n"
            )
            commit = get_current_commit()
            assert commit == "abc123def456"

    def test_error(self):
        """Test getting None on error."""
        from gerrit_cli.git_utils import get_current_commit

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, ["git"])
            commit = get_current_commit()
            assert commit is None


class TestHasUnmergedFiles:
    """Test has_unmerged_files function."""

    def test_has_unmerged(self):
        """Test when there are unmerged files."""
        from gerrit_cli.git_utils import has_unmerged_files

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="file1.c\nfile2.c\n",
                returncode=0
            )
            assert has_unmerged_files() is True

    def test_no_unmerged(self):
        """Test when there are no unmerged files."""
        from gerrit_cli.git_utils import has_unmerged_files

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="",
                returncode=0
            )
            assert has_unmerged_files() is False

    def test_exception(self):
        """Test when exception occurs."""
        from gerrit_cli.git_utils import has_unmerged_files

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git")
            assert has_unmerged_files() is False


class TestIsWorkingTreeClean:
    """Test is_working_tree_clean function."""

    def test_clean(self):
        """Test when working tree is clean."""
        from gerrit_cli.git_utils import is_working_tree_clean

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="",
                returncode=0
            )
            is_clean, message = is_working_tree_clean()
            assert is_clean is True
            assert "clean" in message.lower()

    def test_dirty(self):
        """Test when working tree has changes."""
        from gerrit_cli.git_utils import is_working_tree_clean

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout=" M file.c\n",
                returncode=0
            )
            is_clean, message = is_working_tree_clean()
            assert is_clean is False
            assert "not clean" in message.lower()

    def test_untracked_only(self):
        """Test when only untracked files exist."""
        from gerrit_cli.git_utils import is_working_tree_clean

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="?? newfile.txt\n",
                returncode=0
            )
            is_clean, message = is_working_tree_clean()
            assert is_clean is True

    def test_exception(self):
        """Test when exception occurs."""
        from gerrit_cli.git_utils import is_working_tree_clean

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git")
            is_clean, message = is_working_tree_clean()
            assert is_clean is False
            assert "error" in message.lower()


class TestFetchRef:
    """Test fetch_ref function."""

    def test_success(self):
        """Test successful fetch."""
        from gerrit_cli.git_utils import fetch_ref

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout="abc123def456\n",
                returncode=0
            )
            success, result = fetch_ref("refs/changes/45/12345/1")
            assert success is True
            assert result == "abc123def456"

    def test_failure(self):
        """Test failed fetch."""
        from gerrit_cli.git_utils import fetch_ref

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git", stderr="fatal: couldn't find remote ref"
            )
            success, error = fetch_ref("refs/changes/99/99999/1")
            assert success is False
            assert "couldn't find" in error


class TestCheckout:
    """Test checkout function."""

    def test_success(self):
        """Test successful checkout."""
        from gerrit_cli.git_utils import checkout

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            success, message = checkout("main")
            assert success is True
            assert "main" in message

    def test_failure(self):
        """Test failed checkout."""
        from gerrit_cli.git_utils import checkout

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git", stderr="error: pathspec 'nonexistent' did not match"
            )
            success, error = checkout("nonexistent")
            assert success is False
            assert "pathspec" in error


class TestCherryPick:
    """Test cherry_pick function."""

    def test_success(self):
        """Test successful cherry-pick."""
        from gerrit_cli.git_utils import cherry_pick

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            success, message = cherry_pick("abc123")
            assert success is True

    def test_conflict(self):
        """Test cherry-pick with conflict."""
        from gerrit_cli.git_utils import cherry_pick

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git", stderr="error: could not apply abc123"
            )
            success, error = cherry_pick("abc123")
            assert success is False
            assert "could not apply" in error


class TestIsAncestor:
    """Test is_ancestor function."""

    def test_is_ancestor(self):
        """Test when commit is an ancestor."""
        from gerrit_cli.git_utils import is_ancestor

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert is_ancestor("abc123", "def456") is True

    def test_not_ancestor(self):
        """Test when commit is not an ancestor."""
        from gerrit_cli.git_utils import is_ancestor

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert is_ancestor("abc123", "def456") is False

    def test_exception(self):
        """Test when exception occurs."""
        from gerrit_cli.git_utils import is_ancestor

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git")
            assert is_ancestor("abc123", "def456") is False
