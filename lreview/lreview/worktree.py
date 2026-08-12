"""Git fetch and worktree management for lreview.

Worktree add/remove mutate the shared .git of the source repository, so
they are serialized with a lock; the actual reviews then run fully in
parallel, each in its own worktree.
"""

import subprocess
import threading
from pathlib import Path

_GIT_LOCK = threading.Lock()


class GitError(RuntimeError):
    pass


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the given repository."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result


def is_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def commit_exists(repo: Path, sha: str) -> bool:
    result = run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    return result.returncode == 0


def rev_parse(repo: Path, ref: str) -> str:
    """Resolve a local ref (branch, tag, SHA, HEAD) to a commit SHA."""
    result = run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return result.stdout.strip()


def commit_subject(repo: Path, sha: str) -> str:
    result = run_git(repo, "log", "-1", "--format=%s", sha)
    return result.stdout.strip()


def fetch_change(repo: Path, remote_url: str, ref: str) -> None:
    """Fetch a Gerrit change ref into the repository object store."""
    with _GIT_LOCK:
        run_git(repo, "fetch", remote_url, ref)


def add_worktree(repo: Path, dest: Path, sha: str) -> None:
    """Create a detached worktree of sha at dest.

    dest is resolved to an absolute path: git -C <repo> would resolve
    a relative path against the repo, not our cwd.
    """
    dest = dest.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _GIT_LOCK:
        run_git(repo, "worktree", "add", "--detach", str(dest), sha)


def remove_worktree(repo: Path, dest: Path) -> bool:
    """Remove a worktree; returns False (instead of raising) on failure."""
    dest = dest.expanduser().resolve()
    with _GIT_LOCK:
        result = run_git(
            repo, "worktree", "remove", "--force", str(dest), check=False)
    return result.returncode == 0


def prune_worktrees(repo: Path) -> None:
    """Drop registrations of worktrees whose directories are gone."""
    with _GIT_LOCK:
        run_git(repo, "worktree", "prune", check=False)
