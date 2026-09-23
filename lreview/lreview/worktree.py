"""Git fetch and worktree management for lreview.

Worktree add/remove mutate the shared .git of the source repository, so
they are serialized with a lock; the actual reviews then run fully in
parallel, each in its own worktree.
"""

import os
import shutil
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


def commit_change_id(repo: Path, sha: str):
    """The commit's Gerrit Change-Id trailer, or None."""
    import re
    body = run_git(repo, "log", "-1", "--format=%B", sha).stdout
    matches = re.findall(r"^Change-Id:\s*(I[0-9a-f]{8,40})\s*$",
                         body, re.MULTILINE)
    return matches[-1] if matches else None


def recent_commits(repo: Path, count: int, start: str = "HEAD") -> list[str]:
    """The newest `count` commit SHAs reachable from start, newest first.

    Returns fewer than `count` only when the history is shorter; the
    caller decides whether that is an error.
    """
    result = run_git(repo, "rev-list", "--max-count", str(count), start)
    return result.stdout.split()


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


def _owning_repo(wtree: Path):
    """The repository a worktree directory belongs to, or None.

    A worktree's .git is a file holding
    'gitdir: <repo>/.git/worktrees/<name>'.
    """
    try:
        line = (wtree / ".git").read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line.split(":", 1)[1].strip())
    # .../<repo>/.git/worktrees/<name>
    if gitdir.parent.name != "worktrees":
        return None
    return gitdir.parent.parent.parent


def reap_orphan_worktrees(worktrees_dir: Path) -> int:
    """Remove worktrees left behind by lreview runs that were killed.

    A run cleans up in a finally, so only a SIGKILL or a lost machine
    strands one -- but nothing reaped those afterwards, and each is a
    full checkout.  The directory name ends in the creating process's
    pid, so a directory whose pid is gone belongs to no live run.
    Returns the number removed.
    """
    reaped = 0
    for wtree in sorted(worktrees_dir.glob("kreview_*")):
        if not wtree.is_dir():
            continue
        pid = wtree.name.rsplit(".", 1)[-1]
        if not pid.isdigit():
            continue
        try:
            os.kill(int(pid), 0)
            continue        # a live run owns it
        except PermissionError:
            continue        # alive, someone else's
        except (OSError, ValueError):
            pass            # no such process: stranded

        owner = _owning_repo(wtree)
        if owner is not None:
            remove_worktree(owner, wtree)
        if wtree.exists():
            shutil.rmtree(wtree, ignore_errors=True)
        if owner is not None:
            prune_worktrees(owner)
        if not wtree.exists():
            reaped += 1
    return reaped
