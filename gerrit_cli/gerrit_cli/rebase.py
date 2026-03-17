"""Integrated git rebase mode for working on patches with comments.

This module is a backward-compatibility facade. The canonical code now
lives in:
  - rebase_manager.py    — RebaseManager class
  - patch_workflow.py     — module-level orchestration functions
  - session.py            — RebaseSession, SessionManager, LastURLManager

All public names are re-exported here so that existing ``from
gerrit_cli.rebase import ...`` statements continue to work.
"""

# Re-export the RebaseManager class (canonical: rebase_manager.py)
from .rebase_manager import RebaseManager  # noqa: F401

# Re-export session types (canonical: session.py) — some callers import
# RebaseSession from rebase rather than from session directly.
from .session import RebaseSession  # noqa: F401

# Re-export module-level workflow functions (canonical: patch_workflow.py)
from .patch_workflow import (  # noqa: F401
    abort_patch,
    end_session,
    finish_patch,
    get_session_info,
    get_session_url,
    next_patch,
    rebase_status,
    work_on_patch,
)

__all__ = [
    "RebaseManager",
    "RebaseSession",
    "abort_patch",
    "end_session",
    "finish_patch",
    "get_session_info",
    "get_session_url",
    "next_patch",
    "rebase_status",
    "work_on_patch",
]
