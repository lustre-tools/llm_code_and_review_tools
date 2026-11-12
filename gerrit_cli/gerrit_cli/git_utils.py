"""Git utility functions for gerrit-cli.

This module provides a clean interface for common git operations,
abstracting away subprocess calls and error handling.
"""

import subprocess
from typing import Optional


class GitRunner:
    """Runs git commands with consistent error handling."""

    def __init__(self, cwd: Optional[str] = None):
        """Initialize the git runner.

        Args:
            cwd: Working directory for git commands. Defaults to current dir.
        """
        self.cwd = cwd

    def run(
        self,
        args: list[str],
        capture_output: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a git command.

        Args:
            args: Git command arguments (without 'git' prefix)
            capture_output: Whether to capture stdout/stderr
            check: Whether to raise on non-zero exit

        Returns:
            CompletedProcess instance

        Raises:
            subprocess.CalledProcessError: If check=True and command fails
        """
        cmd = ["git"] + args
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            check=check,
            cwd=self.cwd,
        )


def check_git_repo() -> tuple[bool, str]:
    """Check if we're in a valid git repository.

    Returns:
        Tuple of (is_valid, message)
    """
    runner = GitRunner()
    try:
        result = runner.run(["rev-parse", "--is-inside-work-tree"])
        if result.stdout.strip() == "true":
            return True, "Valid git repository"
        return False, "Not inside a git work tree"
    except subprocess.CalledProcessError:
        return False, "Not a git repository"
    except FileNotFoundError:
        return False, "Git is not installed"


def get_current_branch() -> Optional[str]:
    """Get the current git branch name.

    Returns:
        Branch name or None if in detached HEAD state
    """
    runner = GitRunner()
    try:
        result = runner.run(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = result.stdout.strip()
        return None if branch == "HEAD" else branch
    except subprocess.CalledProcessError:
        return None


def get_current_commit() -> Optional[str]:
    """Get the current HEAD commit hash.

    Returns:
        Full commit hash or None on error
    """
    runner = GitRunner()
    try:
        result = runner.run(["rev-parse", "HEAD"])
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def is_cherry_pick_in_progress() -> bool:
    """Check if a cherry-pick is in progress.

    Returns:
        True if cherry-pick is in progress
    """
    runner = GitRunner()
    try:
        # Check for CHERRY_PICK_HEAD
        result = runner.run(
            ["rev-parse", "--verify", "CHERRY_PICK_HEAD"],
            check=False,
        )
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False


def has_unmerged_files() -> bool:
    """Check if there are unmerged files (conflicts).

    Returns:
        True if there are unmerged files
    """
    runner = GitRunner()
    try:
        result = runner.run(["diff", "--name-only", "--diff-filter=U"])
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def is_working_tree_clean() -> tuple[bool, str]:
    """Check if the working tree is clean (no uncommitted changes).

    Returns:
        Tuple of (is_clean, message)
    """
    runner = GitRunner()
    try:
        result = runner.run(["status", "--porcelain"])
        # Filter out untracked files (lines starting with ??)
        tracked_changes = [
            line for line in result.stdout.strip().split('\n')
            if line and not line.startswith('??')
        ]
        if tracked_changes:
            return False, "Working tree is not clean. Please commit or stash your changes first."
        return True, "Working tree is clean"
    except subprocess.CalledProcessError as e:
        return False, f"Error checking git status: {e}"


def fetch_ref(ref: str) -> tuple[bool, str]:
    """Fetch a ref from origin.

    Args:
        ref: The ref to fetch (e.g., 'refs/changes/45/12345/1')

    Returns:
        Tuple of (success, commit_hash_or_error)
    """
    runner = GitRunner()
    try:
        runner.run(["fetch", "origin", ref])
        result = runner.run(["rev-parse", "FETCH_HEAD"])
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip() if e.stderr else str(e)


def checkout(ref: str) -> tuple[bool, str]:
    """Checkout a ref or commit.

    Args:
        ref: The ref or commit to checkout

    Returns:
        Tuple of (success, message)
    """
    runner = GitRunner()
    try:
        runner.run(["checkout", ref])
        return True, f"Checked out {ref}"
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip() if e.stderr else str(e)


def cherry_pick(commit: str) -> tuple[bool, str]:
    """Cherry-pick a commit.

    Args:
        commit: The commit hash to cherry-pick

    Returns:
        Tuple of (success, message)
    """
    runner = GitRunner()
    try:
        runner.run(["cherry-pick", commit])
        return True, f"Cherry-picked {commit}"
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip() if e.stderr else str(e)


def is_ancestor(ancestor: str, descendant: str) -> bool:
    """Check if one commit is an ancestor of another.

    Args:
        ancestor: The potential ancestor commit
        descendant: The potential descendant commit

    Returns:
        True if ancestor is an ancestor of descendant
    """
    runner = GitRunner()
    try:
        result = runner.run(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            check=False
        )
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False
