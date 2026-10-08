"""Shared helper functions used by multiple command modules.

These were originally defined in cli.py and are kept here to avoid
circular imports while sharing code across command modules.
"""

import sys
from typing import Any

import requests

from ..client import GerritAuthRequired, GerritConfigError
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
        updated - When the root comment was posted
        last_updated - When anyone last said anything in the thread
        code_context - Code context around comment
        replies - Reply messages, with their authors and times

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
            elif field == "updated":
                thread_data["updated"] = root.updated
            elif field == "last_updated":
                thread_data["last_updated"] = max(
                    [root.updated, *(reply.updated for reply in thread.replies)])
            elif field == "code_context":
                if root.code_context:
                    thread_data["code_context"] = root.code_context.to_dict()
                else:
                    thread_data["code_context"] = None
            elif field == "replies":
                thread_data["replies"] = [
                    {"author": r.author.name, "message": r.message, "updated": r.updated}
                    for r in thread.replies
                ]

        result.append(thread_data)

    return result


def threads_since(threads: list, since: str) -> list:
    """The threads anyone has said something in at or after ``since``."""
    return [
        thread for thread in threads
        if max([thread.root_comment.updated, *(r.updated for r in thread.replies)]) >= since
    ]


def comment_timeline(threads: list, since: str = "") -> list[dict]:
    """Every comment of ``threads``, root or reply, oldest first: who said
    what, when, where, and in which thread."""
    entries = []
    for index, thread in enumerate(threads):
        root = thread.root_comment
        for comment in [root, *thread.replies]:
            if since and comment.updated < since:
                continue
            entries.append({
                "updated": comment.updated,
                "patch_set": comment.patch_set,
                "author": comment.author.name,
                "file": root.file_path,
                "line": root.line,
                "thread": index,
                "reply": comment is not root,
                "thread_resolved": thread.is_resolved,
                "message": comment.message,
            })
    return sorted(entries, key=lambda entry: entry["updated"])


def thread_index_error(result: Any, index: int) -> str | None:
    """Why index names no thread reply/done/ack/stage can answer.

    Those commands index the default listing.  `comments --all` lists
    the same threads first and the resolved ones after them, so an index
    past the default listing but inside --all's is one of those.
    """
    shown = len(result.threads)
    if shown <= index < shown + result.hidden_resolved_count:
        return (
            f"Thread {index} is a resolved thread that only 'gc comments "
            f"--all' lists; reply, done, ack and stage take the {shown} "
            "thread(s) the default listing shows. To answer it, use "
            "'gc batch' with its comment_id."
        )
    return None


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout.

    Checks for --envelope flag via the ``_full_envelope`` module-level
    flag (set by the CLI entry point).
    """
    full_env = _cli().FULL_ENVELOPE if hasattr(_cli(), 'FULL_ENVELOPE') else False
    print(format_json(envelope, pretty=pretty, full_envelope=full_env))


def output_success(
    data: Any,
    command: str,
    pretty: bool,
    next_actions: list[str] | None = None,
) -> None:
    """Output success envelope to stdout."""
    envelope = success_response(data, command, next_actions=next_actions)
    output_result(envelope, pretty)


_EXIT_CODES: dict[str, int] = {
    ErrorCode.AUTH_FAILED: ExitCode.AUTH_ERROR,
    ErrorCode.AUTH_MISSING: ExitCode.AUTH_ERROR,
    ErrorCode.CONFIG_ERROR: ExitCode.AUTH_ERROR,
    ErrorCode.NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.CHANGE_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.THREAD_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.COMMENT_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.PATCH_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.SERIES_NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.INVALID_INPUT: ExitCode.INVALID_INPUT,
    ErrorCode.MISSING_REQUIRED_FIELD: ExitCode.INVALID_INPUT,
    ErrorCode.INVALID_URL: ExitCode.INVALID_INPUT,
    ErrorCode.THREAD_INDEX_OUT_OF_RANGE: ExitCode.INVALID_INPUT,
    ErrorCode.CONNECTION_ERROR: ExitCode.NETWORK_ERROR,
    ErrorCode.TIMEOUT: ExitCode.NETWORK_ERROR,
}


def exit_code_for(code: str) -> int:
    """The process exit code for an error code."""
    return _EXIT_CODES.get(code, ExitCode.GENERAL_ERROR)


def error_code_for(exc: BaseException, default: str = ErrorCode.API_ERROR) -> str:
    """The error code for an exception a command handler caught.

    Handlers catch Exception so that a failure prints JSON rather than a
    traceback; this keeps a missing credential, a missing change and an
    unreachable server apart from any other failure.  An exception raised
    `from` another is classified by that one when it says nothing itself.
    """
    seen = set()
    code = _own_error_code(exc)
    while code is None and exc.__cause__ is not None and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.__cause__
        code = _own_error_code(exc)
    return code or default


def _own_error_code(exc: BaseException) -> str | None:
    from ..graph.conflicts import ConflictRepoError

    if isinstance(exc, ConflictRepoError):
        return ErrorCode.INVALID_INPUT
    if isinstance(exc, GerritAuthRequired):
        return ErrorCode.AUTH_MISSING
    if isinstance(exc, GerritConfigError):
        return ErrorCode.CONFIG_ERROR
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403):
            return ErrorCode.AUTH_FAILED
        if status == 404:
            return ErrorCode.NOT_FOUND
    # ConnectTimeout is both, and a timeout says more
    if isinstance(exc, requests.Timeout):
        return ErrorCode.TIMEOUT
    if isinstance(exc, requests.ConnectionError):
        return ErrorCode.CONNECTION_ERROR
    return None


def output_error(code: str, message: str, command: str, pretty: bool) -> int:
    """Output error envelope to stdout and return the exit code for code."""
    envelope = error_response_from_dict(code, message, command)
    output_result(envelope, pretty)
    return exit_code_for(code)


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

When done: gerrit abort --keep-changes
To abort: gerrit abort (discards all changes)"""
