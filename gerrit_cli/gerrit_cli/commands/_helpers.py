"""Shared helper functions used by multiple command modules.

These were originally defined in cli.py and are kept here to avoid
circular imports while sharing code across command modules.
"""

import sys
from typing import Any

from ..envelope import error_response_from_dict, format_json, success_response
from ..errors import ErrorCode, ExitCode


def _cli():
    """Return the ``gerrit_cli.cli`` module at call time.

    Command modules use this to resolve dependencies that tests may
    patch via ``patch('gerrit_cli.cli.RebaseManager')`` etc.  By
    going through ``sys.modules`` at call time rather than importing
    at module level, the command functions always see the (possibly
    mocked) attribute on the cli module.

    Usage inside a command function::

        cli = _cli()
        manager = cli.RebaseManager()
    """
    return sys.modules["gerrit_cli.cli"]


# Bot account names to filter from reviewer lists
BOT_REVIEWER_NAMES: set[str] = {
    "Maloo", "jenkins", "Jenkins", "Autotest",
    "wc-checkpatch", "Lustre Gerrit Janitor",
    "Misc Code Checks Robot (Gatekeeper helper)",
    "CI Bot", "Build Bot", "Janitor Bot",
}


def _patchset_age(timestamp_str: str) -> str:
    """Convert a Gerrit timestamp to a human-readable age string."""
    from datetime import datetime, timezone
    try:
        # Gerrit format: "2026-02-18 17:35:19.000000000"
        ts = timestamp_str.split(".")[0]
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        remaining_m = minutes % 60
        if hours < 24:
            return f"{hours}h {remaining_m}m"
        days = hours // 24
        remaining_h = hours % 24
        return f"{days}d {remaining_h}h"
    except Exception:
        return ""


def filter_threads_by_fields(
    threads: list,
    fields: str,
) -> list[dict]:
    """Filter threads to only include specified fields.

    This produces a flat list of thread summaries for reduced token usage.

    Args:
        threads: List of CommentThread objects
        fields: Comma-separated field names

    Available fields:
        index - Thread index (0-based)
        file - File path
        line - Line number
        message - Root comment message
        author - Author name
        resolved - Whether thread is resolved
        patch_set - Patchset number
        code_context - Code context around comment
        replies - Reply messages

    Returns:
        List of dicts with only the requested fields per thread
    """
    field_list = [f.strip() for f in fields.split(",")]
    result = []

    for idx, thread in enumerate(threads):
        thread_data = {}
        root = thread.root_comment

        for field in field_list:
            if field == "index":
                thread_data["index"] = idx
            elif field == "file":
                thread_data["file"] = root.file_path
            elif field == "line":
                thread_data["line"] = root.line
            elif field == "message":
                thread_data["message"] = root.message
            elif field == "author":
                thread_data["author"] = root.author.name
            elif field == "resolved":
                thread_data["resolved"] = thread.is_resolved
            elif field == "patch_set":
                thread_data["patch_set"] = root.patch_set
            elif field == "code_context":
                if root.code_context:
                    thread_data["code_context"] = root.code_context.to_dict()
                else:
                    thread_data["code_context"] = None
            elif field == "replies":
                thread_data["replies"] = [
                    {"author": r.author.name, "message": r.message}
                    for r in thread.replies
                ]

        result.append(thread_data)

    return result


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout."""
    print(format_json(envelope, pretty=pretty))


def output_success(
    data: Any,
    command: str,
    pretty: bool,
    next_actions: list[str] | None = None,
) -> None:
    """Output success envelope to stdout."""
    envelope = success_response(data, command, next_actions=next_actions)
    output_result(envelope, pretty)


def output_error(code: str, message: str, command: str, pretty: bool) -> int:
    """Output error envelope to stdout and return exit code."""
    envelope = error_response_from_dict(code, message, command)
    output_result(envelope, pretty)
    return ExitCode.GENERAL_ERROR


def generate_review_prompt(url: str) -> str:
    """Generate a prompt for AI-assisted patch series review.

    Args:
        url: URL to any patch in the series

    Returns:
        Formatted prompt string
    """
    return f"""Address comments on this patch series.

Start: gerrit review-series {url}
  (shows series, checks out first patch with comments)

For each patch:
  1. Review comments shown, make fixes
  2. Stage replies:  gerrit stage --done <index>
                     gerrit stage <index> "message"
  3. Commit:         git add <files> && git commit --amend --no-edit
  4. Next patch:     gerrit finish-patch
     (rebases descendants, advances to next patch with comments)

For substantive issues, ask me before making changes.

When done: gerrit end-session
To abort: gerrit abort-session (discards all changes)"""
