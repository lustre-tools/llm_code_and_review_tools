"""Summary/truncation utilities for reducing output verbosity.

This module provides functions to truncate long content (code context, diffs)
when --summary mode is used, reducing token usage while preserving key information.
"""

from typing import Any


def truncate_lines(lines: list[str], max_lines: int) -> tuple[list[str], int]:
    """Truncate a list of lines to max_lines.

    Args:
        lines: List of lines to truncate
        max_lines: Maximum number of lines to keep

    Returns:
        Tuple of (truncated_lines, remaining_count)
    """
    if len(lines) <= max_lines:
        return lines, 0
    return lines[:max_lines], len(lines) - max_lines


def truncate_code_context(context: dict[str, Any], max_lines: int) -> dict[str, Any]:
    """Truncate code context lines.

    Args:
        context: CodeContext dict with 'lines' key
        max_lines: Maximum lines to keep

    Returns:
        Modified context dict with truncation indicator
    """
    if context is None:
        return None

    lines = context.get("lines", [])
    truncated, remaining = truncate_lines(lines, max_lines)

    result = dict(context)
    result["lines"] = truncated
    if remaining > 0:
        result["truncated"] = remaining
    return result


def truncate_comment(comment: dict[str, Any], max_lines: int) -> dict[str, Any]:
    """Truncate code context in a comment.

    Args:
        comment: Comment dict
        max_lines: Maximum context lines

    Returns:
        Modified comment with truncated context
    """
    result = dict(comment)
    if result.get("code_context"):
        result["code_context"] = truncate_code_context(result["code_context"], max_lines)
    return result


def truncate_thread(thread: dict[str, Any], max_lines: int) -> dict[str, Any]:
    """Truncate code context in a thread.

    Args:
        thread: Thread dict with root_comment and replies
        max_lines: Maximum context lines

    Returns:
        Modified thread with truncated context
    """
    result = dict(thread)
    if result.get("root_comment"):
        result["root_comment"] = truncate_comment(result["root_comment"], max_lines)
    if result.get("replies"):
        result["replies"] = [truncate_comment(r, max_lines) for r in result["replies"]]
    return result


def truncate_extracted_comments(data: dict[str, Any], max_lines: int) -> dict[str, Any]:
    """Truncate all code contexts in extracted comments.

    Args:
        data: ExtractedComments dict
        max_lines: Maximum context lines per comment

    Returns:
        Modified data with truncated contexts and summary hint
    """
    result = dict(data)

    if result.get("threads"):
        result["threads"] = [truncate_thread(t, max_lines) for t in result["threads"]]

    # Add summary mode indicator and hint
    result["_summary_mode"] = True
    result["_summary_hint"] = "Use without --summary to see full code context"

    return result


def truncate_diff_hunks(hunks: list[dict[str, Any]], max_lines: int) -> list[dict[str, Any]]:
    """Truncate lines in diff hunks.

    Args:
        hunks: List of hunk dicts with 'lines' key
        max_lines: Maximum lines per hunk

    Returns:
        Modified hunks with truncated lines
    """
    result = []
    for hunk in hunks:
        hunk_copy = dict(hunk)
        lines = hunk_copy.get("lines", [])
        truncated, remaining = truncate_lines(lines, max_lines)
        hunk_copy["lines"] = truncated
        if remaining > 0:
            hunk_copy["truncated"] = remaining
        result.append(hunk_copy)
    return result


def truncate_file_change(file_change: dict[str, Any], max_lines: int) -> dict[str, Any]:
    """Truncate diff hunks in a file change.

    Args:
        file_change: FileChange dict
        max_lines: Maximum lines per hunk

    Returns:
        Modified file change with truncated hunks
    """
    result = dict(file_change)
    if result.get("hunks"):
        result["hunks"] = truncate_diff_hunks(result["hunks"], max_lines)
    # Also truncate full file content if present
    if result.get("new_content"):
        lines = result["new_content"].split("\n")
        if len(lines) > max_lines:
            result["new_content"] = "\n".join(lines[:max_lines])
            result["new_content_truncated"] = len(lines) - max_lines
    if result.get("old_content"):
        lines = result["old_content"].split("\n")
        if len(lines) > max_lines:
            result["old_content"] = "\n".join(lines[:max_lines])
            result["old_content_truncated"] = len(lines) - max_lines
    return result


def truncate_review_data(data: dict[str, Any], max_lines: int) -> dict[str, Any]:
    """Truncate all diffs in review data.

    Args:
        data: ReviewData dict
        max_lines: Maximum lines per hunk

    Returns:
        Modified data with truncated diffs and summary hint
    """
    result = dict(data)

    if result.get("files"):
        result["files"] = [truncate_file_change(f, max_lines) for f in result["files"]]

    # Add summary mode indicator and hint
    result["_summary_mode"] = True
    result["_summary_hint"] = "Use without --summary to see full diffs"

    return result


def truncate_series_comments(data: dict[str, Any], max_lines: int) -> dict[str, Any]:
    """Truncate all code contexts in series comments.

    Args:
        data: SeriesComments dict with patches containing threads
        max_lines: Maximum context lines per comment

    Returns:
        Modified data with truncated contexts and summary hint
    """
    result = dict(data)

    if result.get("patches"):
        patches = []
        for patch in result["patches"]:
            patch_copy = dict(patch)
            if patch_copy.get("threads"):
                patch_copy["threads"] = [
                    truncate_thread(t, max_lines) for t in patch_copy["threads"]
                ]
            patches.append(patch_copy)
        result["patches"] = patches

    # Add summary mode indicator and hint
    result["_summary_mode"] = True
    result["_summary_hint"] = "Use without --summary to see full code context"

    return result
