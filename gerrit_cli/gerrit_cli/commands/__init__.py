"""Command modules for the Gerrit CLI.

Each module groups related command handlers. All cmd_* functions are
re-exported here so that ``from gerrit_cli.commands import cmd_extract``
(and similar) works, and the main cli.py can build its handler dict
from a single import.
"""

from .comments import (
    cmd_extract,
    cmd_reply,
    cmd_batch_reply,
    cmd_done,
    cmd_ack,
)
from .workflow import (
    cmd_work_on_patch,
    cmd_next_patch,
    cmd_finish_patch,
    cmd_abort,
    cmd_status,
    cmd_checkout,
)
from .staging import (
    cmd_stage,
    cmd_push,
    cmd_staged_list,
    cmd_staged_show,
    cmd_staged_remove,
    cmd_staged_clear,
    cmd_staged_refresh,
)
from .review import (
    cmd_review,
    cmd_series,
    cmd_series_comments,
    cmd_series_status,
    cmd_interactive,
)
from .ci import (
    cmd_maloo,
    cmd_info,
    cmd_watch,
    cmd_diff,
)
from .change import (
    cmd_abandon,
    cmd_restore,
    cmd_rebase,
    cmd_vote,
    cmd_set_topic,
    cmd_message,
)
from .reviewers import (
    cmd_reviewers,
    cmd_add_reviewer,
    cmd_remove_reviewer,
    cmd_find_user,
)
from .meta import (
    cmd_search,
    cmd_explain,
    cmd_examples,
    cmd_describe,
)
from .reintegration import (
    cmd_continue_reintegration,
    cmd_skip_reintegration,
)

__all__ = [
    "cmd_extract",
    "cmd_reply",
    "cmd_batch_reply",
    "cmd_done",
    "cmd_ack",
    "cmd_work_on_patch",
    "cmd_next_patch",
    "cmd_finish_patch",
    "cmd_abort",
    "cmd_status",
    "cmd_checkout",
    "cmd_stage",
    "cmd_push",
    "cmd_staged_list",
    "cmd_staged_show",
    "cmd_staged_remove",
    "cmd_staged_clear",
    "cmd_staged_refresh",
    "cmd_review",
    "cmd_series",
    "cmd_series_comments",
    "cmd_series_status",
    "cmd_interactive",
    "cmd_maloo",
    "cmd_info",
    "cmd_watch",
    "cmd_diff",
    "cmd_abandon",
    "cmd_restore",
    "cmd_rebase",
    "cmd_vote",
    "cmd_set_topic",
    "cmd_message",
    "cmd_reviewers",
    "cmd_add_reviewer",
    "cmd_remove_reviewer",
    "cmd_find_user",
    "cmd_search",
    "cmd_explain",
    "cmd_examples",
    "cmd_describe",
    "cmd_continue_reintegration",
    "cmd_skip_reintegration",
]
