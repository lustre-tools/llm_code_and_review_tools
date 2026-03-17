#!/usr/bin/env python3
"""Command-line interface for Gerrit comments tools.

This CLI provides commands for:
1. comments - Get unresolved comments from a Gerrit change
2. reply - Reply to comments or mark them as done
3. review - Get diff/changes for code review, optionally post review comments

All commands output JSON.

Examples:
    # Get unresolved comments
    gc comments https://review.whamcloud.com/c/fs/lustre-release/+/62796

    # Reply to a comment (by thread index from comments output)
    gc reply https://review.whamcloud.com/c/fs/lustre-release/+/62796 0 "Done"

    # Mark a comment as done
    gc reply --done https://review.whamcloud.com/c/fs/lustre-release/+/62796 0

    # Get changes for code review
    gc review https://review.whamcloud.com/c/fs/lustre-release/+/62796

    # Post a code review with comments from JSON file
    gc review --post-comments comments.json https://review.whamcloud.com/62796
"""

import argparse
import sys

from .envelope import error_response_from_dict, format_json
from .errors import ExitCode

# ---------------------------------------------------------------------------
# Import names that tests patch via patch('gerrit_cli.cli.X').
# These MUST be imported here (at module level) BEFORE the command modules
# are imported, so that they exist in this module's namespace and can be
# patched by unittest.mock.patch.
# ---------------------------------------------------------------------------
from .client import GerritCommentsClient  # noqa: F401
from .extractor import extract_comments  # noqa: F401
from .interactive import run_interactive  # noqa: F401
from .rebase import (  # noqa: F401
    RebaseManager,
    abort_patch,
    end_session,
    finish_patch,
    get_session_info,
    get_session_url,
    next_patch,
    rebase_status,
    work_on_patch,
)
from .replier import CommentReplier  # noqa: F401
from .reviewer import CodeReviewer  # noqa: F401
from .series import SeriesFinder  # noqa: F401
from .session import LastURLManager  # noqa: F401
from .series_status import show_series_status  # noqa: F401
from .staging import StagingManager  # noqa: F401
from .summary import (  # noqa: F401
    truncate_extracted_comments,
    truncate_review_data,
    truncate_series_comments,
)

# ---------------------------------------------------------------------------
# Re-export shared helpers so that ``from gerrit_cli.cli import output_success``
# (and similar) keeps working for any existing callers.
# ---------------------------------------------------------------------------
from .commands._helpers import (  # noqa: F401 -- re-exports
    BOT_REVIEWER_NAMES,
    _patchset_age,
    filter_threads_by_fields,
    generate_review_prompt,
    output_error,
    output_result,
    output_success,
)

# ---------------------------------------------------------------------------
# Import command handler modules. They are imported as modules so we can
# inject patchable names into their namespace (see below).
# ---------------------------------------------------------------------------
from .commands import comments as _cmd_comments
from .commands import workflow as _cmd_workflow
from .commands import staging as _cmd_staging
from .commands import review as _cmd_review
from .commands import ci as _cmd_ci
from .commands import change as _cmd_change
from .commands import reviewers as _cmd_reviewers
from .commands import meta as _cmd_meta
from .commands import reintegration as _cmd_reintegration

# ---------------------------------------------------------------------------
# Re-export every cmd_* handler from the commands modules.
# This is required for backward compatibility with existing tests that do
# ``from gerrit_cli.cli import cmd_extract``.
# ---------------------------------------------------------------------------
cmd_extract = _cmd_comments.cmd_extract
cmd_reply = _cmd_comments.cmd_reply
cmd_batch_reply = _cmd_comments.cmd_batch_reply
cmd_done = _cmd_comments.cmd_done
cmd_ack = _cmd_comments.cmd_ack

cmd_work_on_patch = _cmd_workflow.cmd_work_on_patch
cmd_next_patch = _cmd_workflow.cmd_next_patch
cmd_finish_patch = _cmd_workflow.cmd_finish_patch
cmd_abort = _cmd_workflow.cmd_abort
cmd_status = _cmd_workflow.cmd_status
cmd_checkout = _cmd_workflow.cmd_checkout

cmd_stage = _cmd_staging.cmd_stage
cmd_push = _cmd_staging.cmd_push
cmd_staged_list = _cmd_staging.cmd_staged_list
cmd_staged_show = _cmd_staging.cmd_staged_show
cmd_staged_remove = _cmd_staging.cmd_staged_remove
cmd_staged_clear = _cmd_staging.cmd_staged_clear
cmd_staged_refresh = _cmd_staging.cmd_staged_refresh

cmd_review = _cmd_review.cmd_review
cmd_series = _cmd_review.cmd_series
cmd_series_comments = _cmd_review.cmd_series_comments
cmd_series_status = _cmd_review.cmd_series_status
cmd_interactive = _cmd_review.cmd_interactive

cmd_maloo = _cmd_ci.cmd_maloo
cmd_info = _cmd_ci.cmd_info
cmd_series_info = _cmd_ci.cmd_series_info
cmd_watch = _cmd_ci.cmd_watch
cmd_diff = _cmd_ci.cmd_diff

cmd_abandon = _cmd_change.cmd_abandon
cmd_restore = _cmd_change.cmd_restore
cmd_rebase = _cmd_change.cmd_rebase
cmd_vote = _cmd_change.cmd_vote
cmd_set_topic = _cmd_change.cmd_set_topic
cmd_hashtag = _cmd_change.cmd_hashtag
cmd_related = _cmd_change.cmd_related
cmd_message = _cmd_change.cmd_message

cmd_reviewers = _cmd_reviewers.cmd_reviewers
cmd_add_reviewer = _cmd_reviewers.cmd_add_reviewer
cmd_remove_reviewer = _cmd_reviewers.cmd_remove_reviewer
cmd_find_user = _cmd_reviewers.cmd_find_user

cmd_search = _cmd_meta.cmd_search
cmd_explain = _cmd_meta.cmd_explain
cmd_examples = _cmd_meta.cmd_examples
cmd_describe = _cmd_meta.cmd_describe

cmd_continue_reintegration = _cmd_reintegration.cmd_continue_reintegration
cmd_skip_reintegration = _cmd_reintegration.cmd_skip_reintegration


# Module-level flag for --envelope; read by _helpers.output_result().
FULL_ENVELOPE = False


class _JsonErrorParser(argparse.ArgumentParser):
    """ArgumentParser that outputs errors as JSON instead of stderr.

    Used as parser_class for subparsers so that argument errors from
    any subcommand produce structured JSON output.
    """

    def error(self, message: str) -> None:
        envelope = error_response_from_dict(
            "invalid_input",
            message,
            "cli",
        )
        print(format_json(envelope, full_envelope=FULL_ENVELOPE))
        sys.exit(ExitCode.INVALID_INPUT)


def main():
    """Main entry point."""
    from .parsers import setup_parsers

    from importlib.metadata import version as _pkg_version
    try:
        _ver = _pkg_version("gerrit-cli")
    except Exception:
        _ver = "unknown"

    parser = argparse.ArgumentParser(
        description="Extract and reply to Gerrit review comments. "
                    "Run 'gc describe' for machine-readable API documentation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {_ver}",
    )
    parser.add_argument(
        "--envelope",
        action="store_true",
        help="Include full response envelope (ok/data/meta wrapper)",
    )
    # Use _JsonErrorParser for subparsers so argument errors from
    # subcommands also produce JSON output. The top-level parser is
    # left as standard ArgumentParser so tests that mock it still work.
    subparsers = parser.add_subparsers(
        dest="command", help="Command to run",
        parser_class=_JsonErrorParser,
    )

    # Map command names to handler functions
    handlers = {
        'comments': cmd_extract,
        'reply': cmd_reply,
        'batch': cmd_batch_reply,
        'review': cmd_review,
        'series_comments': cmd_series_comments,
        'series': cmd_series,
        'series_status': cmd_series_status,
        'interactive': cmd_interactive,
        'work_on_patch': cmd_work_on_patch,
        'next_patch': cmd_next_patch,
        'finish_patch': cmd_finish_patch,
        'abort': cmd_abort,
        'status': cmd_status,
        'stage': cmd_stage,
        'push': cmd_push,
        'staged_list': cmd_staged_list,
        'staged_show': cmd_staged_show,
        'staged_remove': cmd_staged_remove,
        'staged_clear': cmd_staged_clear,
        'staged_refresh': cmd_staged_refresh,
        'continue_reintegration': cmd_continue_reintegration,
        'skip_reintegration': cmd_skip_reintegration,
        'reviewers': cmd_reviewers,
        'add_reviewer': cmd_add_reviewer,
        'remove_reviewer': cmd_remove_reviewer,
        'find_user': cmd_find_user,
        'abandon': cmd_abandon,
        'checkout': cmd_checkout,
        'maloo': cmd_maloo,
        'info': cmd_info,
        'series_info': cmd_series_info,
        'search': cmd_search,
        'watch': cmd_watch,
        'set_topic': cmd_set_topic,
        'hashtag': cmd_hashtag,
        'related': cmd_related,
        'restore': cmd_restore,
        'rebase': cmd_rebase,
        'vote': cmd_vote,
        'diff': cmd_diff,
        'message': cmd_message,
        'explain': cmd_explain,
        'examples': cmd_examples,
        'done': cmd_done,
        'ack': cmd_ack,
        'describe': cmd_describe,
    }

    setup_parsers(subparsers, handlers)

    args = parser.parse_args()

    global FULL_ENVELOPE
    FULL_ENVELOPE = getattr(args, "envelope", False)

    if not args.command:
        # If there's an active session, show status by default
        from .rebase import RebaseManager as _RM
        manager = _RM()
        if manager.has_active_session():
            cmd_status(args)
        else:
            parser.print_help()
            sys.exit(1)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
