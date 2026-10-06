"""`lreview run --since REV`: review only what changed from an earlier
version of the commit.

The commit under review is taken to be a revision of REV -- typically
the Gerrit patchset an agent started from before amending its own
change in. The review prompt gains a focus section that hands the
reviewer the interdiff and limits findings to it.

The interdiff is `git range-diff REV^! SHA^!`: it compares the two
patches rather than the two trees, so it stays exact when the commit
was rebased between the versions, and it covers the commit message.
When both versions have the same parent, `git diff REV SHA` is the same
interdiff as a plain diff and is offered alongside it; after a rebase
that diff would also carry everything the new base brought in.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .worktree import GitError, run_git

# Force the two single-commit ranges to pair: at the default factor a
# heavily reworked commit is shown as one patch removed and another
# added, which is no interdiff at all.
CREATION_FACTOR = 999

_IDENTICAL_RE = re.compile(r"^\s*1:\s+[0-9a-f]+\s+=\s+1:\s+[0-9a-f]+")


@dataclass
class SinceFocus:
    ref: str            # as given on the command line
    sha: str
    same_base: bool
    unchanged: bool = False


def range_diff_args(old: str, new: str) -> list[str]:
    return ["range-diff", f"--creation-factor={CREATION_FACTOR}",
            f"{old}^!", f"{new}^!"]


def _parent(repo, sha: str) -> Optional[str]:
    result = run_git(repo, "rev-parse", "--verify", "-q", f"{sha}^",
                     check=False)
    return result.stdout.strip() or None


def _is_ancestor(repo, a: str, b: str) -> bool:
    return run_git(repo, "merge-base", "--is-ancestor", a, b,
                   check=False).returncode == 0


def resolve_since(repo, ref: str, sha: str) -> SinceFocus:
    """Check that `ref` names an earlier version of commit `sha`.

    Raises ValueError with a message for the user when it does not.
    """
    result = run_git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
                     check=False)
    since = result.stdout.strip()
    if result.returncode != 0 or not since:
        raise ValueError(
            f"--since {ref} does not name a commit in {repo}; fetch the "
            "earlier version first (for a Gerrit patchset: git fetch "
            "<remote> refs/changes/NN/NNNNN/PS)")
    if since == sha:
        return SinceFocus(ref, since, same_base=True, unchanged=True)
    if _is_ancestor(repo, since, sha):
        raise ValueError(
            f"--since {ref} ({since[:12]}) is an ancestor of the reviewed "
            f"commit {sha[:12]}, not an earlier version of it; pass the "
            "commit as it was before you amended it")
    if _is_ancestor(repo, sha, since):
        raise ValueError(
            f"--since {ref} ({since[:12]}) is a descendant of the reviewed "
            f"commit {sha[:12]}, not an earlier version of it")
    old_parent, new_parent = _parent(repo, since), _parent(repo, sha)
    if old_parent is None or new_parent is None:
        raise ValueError("--since needs both versions of the commit to "
                         "have a parent")
    try:
        out = run_git(repo, "-c", "color.ui=never",
                      *range_diff_args(since, sha)).stdout
    except GitError as exc:
        raise ValueError(f"cannot compare {since[:12]} with {sha[:12]}: "
                         f"{exc}") from exc
    return SinceFocus(ref, since, same_base=old_parent == new_parent,
                      unchanged=bool(_IDENTICAL_RE.match(out)))


def focus_label(focus: SinceFocus) -> str:
    if focus.ref == focus.sha:
        return f"changes since {focus.sha[:12]}"
    return f"changes since {focus.ref} ({focus.sha[:12]})"


def focus_prompt(focus: SinceFocus, sha: str) -> str:
    """The prompt section limiting the review to what changed."""
    old = focus.sha
    rdiff = "git " + " ".join(range_diff_args(old, sha))
    lines = [
        f"Focus: the commit under review ({sha}) is a revision of an "
        f"earlier version of the same commit, {old}. Review what changed "
        "between the two versions. Read the rest of the commit as context "
        "for the change, not as code to review again.",
        "",
        "See what changed with:",
        f"  {rdiff}",
        "    the change to the patch itself, commit message included",
    ]
    if focus.same_base:
        lines += [
            f"  git diff {old} {sha}",
            "    the same code changes as a plain diff; both versions "
            "have the same parent, so this is exactly the interdiff",
        ]
    else:
        lines += [
            "",
            f"The two versions have different parents (the commit was "
            f"rebased), so `git diff {old} {sha}` also shows unrelated "
            "changes from the new base. Use the range-diff.",
        ]
    lines += [
        "",
        "Findings must be about the lines and hunks that changed, "
        "including changes to the commit message. Report anything outside "
        "them only if it is a real bug -- incorrect behaviour, a crash or "
        "hang, data loss or corruption, a security hole -- never style, "
        "naming, wording, comments or optional cleanups. If what changed "
        "is clean, the review is clean, whatever else the commit contains.",
    ]
    return "\n".join(lines)
