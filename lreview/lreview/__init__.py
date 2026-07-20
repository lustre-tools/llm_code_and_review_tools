"""lreview: run the kreview AI review skill on Gerrit changes in parallel.

Orchestrates headless Claude Code runs of the /kreview slash command
(from the review-prompts repository) over a batch of Gerrit changes,
each in its own git worktree, collects the generated gerrit-review.json
files, and posts them to Gerrit via gerrit-cli.
"""

__version__ = "0.1.0"
