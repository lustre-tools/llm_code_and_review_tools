"""lreview: run the kreview AI review skill on Gerrit changes in parallel.

Orchestrates headless Claude Code runs of the /kreview slash command
(from the review-prompts repository) over a batch of Gerrit changes,
each in its own git worktree, collects the generated gerrit-review.json
files, and posts them to Gerrit via gerrit-cli.
"""

from importlib import metadata as _metadata

# pyproject.toml is the only version that gets bumped
try:
    __version__ = _metadata.version("lreview")
except _metadata.PackageNotFoundError:  # a source tree never installed
    __version__ = "unknown"
