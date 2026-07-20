"""Gerrit CLI Tools - Extract, reply, and review Gerrit code changes.

This module provides tools for:
1. Extracting comments from Gerrit changes with code context
2. Replying to comments and marking them as done
3. Reviewing code changes and posting review comments

Example usage:

    # Extract comments from a Gerrit URL
    from gerrit_cli import extract_comments

    result = extract_comments("https://review.whamcloud.com/c/fs/lustre-release/+/61965")
    print(f"Found {result.unresolved_count} unresolved threads")

    for thread in result.get_unresolved_threads():
        print(f"File: {thread.root_comment.file_path}")
        print(f"Author: {thread.root_comment.author.name}")
        print(f"Message: {thread.root_comment.message}")

    # Reply to a comment
    from gerrit_cli import CommentReplier

    replier = CommentReplier()
    result = replier.mark_done(
        change_number=61965,
        comment=thread.root_comment,
        message="Done"
    )

    # Or use the convenience functions
    from gerrit_cli import mark_done, reply_to_comment

    mark_done(61965, comment)
    reply_to_comment(61965, comment, "Thanks for the feedback!")

    # Review a code change
    from gerrit_cli import CodeReviewer, get_review_data

    review_data = get_review_data("https://review.whamcloud.com/c/fs/lustre-release/+/62796")
    print(review_data.format_for_review())

    # Post review comments
    reviewer = CodeReviewer()
    reviewer.post_review(
        change_number=62796,
        comments=[{"path": "file.c", "line": 42, "message": "Consider const here"}],
        message="Some suggestions below.",
    )

    # Find all patches in a series
    from gerrit_cli import find_series, SeriesFinder

    series = find_series("https://review.whamcloud.com/c/fs/lustre-release/+/61965")
    print(f"Found {len(series)} patches in series")
    for patch in series.patches:
        print(f"  {patch.change_number}: {patch.subject}")
"""

from .client import GerritCommentsClient, GerritConfigError  # noqa: F401
from .envelope import (
    error_response,
    error_response_from_dict,
    format_json,
    success_response,
)
from .errors import (
    AuthError,
    ConfigError,
    ErrorCode,
    ExitCode,
    GerritToolError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    ToolError,
)
from .extractor import (
    CommentExtractor,
    extract_comments,
)
from .models import (
    Author,
    ChangeInfo,
    CodeContext,
    Comment,
    CommentThread,
    ExtractedComments,
    ReplyResult,
)
from .replier import (
    CommentReplier,
    mark_done,
    reply_to_comment,
)
from .reviewer import (
    CodeReviewer,
    FileChange,
    ReviewData,
    ReviewResult,
    apply_review_prefix,
    get_review_data,
    normalize_review_comments,
    post_review,
)
from .series import (
    PatchComments,
    PatchInfo,
    PatchSeries,
    SeriesComments,
    SeriesFinder,
    StaleChangeInfo,
    find_series,
    find_series_by_change,
    get_series_comments,
)
from .staging import (
    StagedOperation,
    StagedPatch,
    StagingManager,
)

__all__ = [
    # Envelope/Errors
    "error_response",
    "error_response_from_dict",
    "format_json",
    "success_response",
    "AuthError",
    "ConfigError",
    "ErrorCode",
    "ExitCode",
    "GerritToolError",
    "InvalidInputError",
    "NetworkError",
    "NotFoundError",
    "ToolError",
    # Models
    "Author",
    "CodeContext",
    "Comment",
    "CommentThread",
    "ChangeInfo",
    "ExtractedComments",
    "ReplyResult",
    # Client
    "GerritCommentsClient",
    "GerritConfigError",
    # Extractor
    "CommentExtractor",
    "extract_comments",
    # Replier
    "CommentReplier",
    "reply_to_comment",
    "mark_done",
    # Reviewer
    "CodeReviewer",
    "FileChange",
    "ReviewData",
    "ReviewResult",
    "apply_review_prefix",
    "get_review_data",
    "normalize_review_comments",
    "post_review",
    # Series
    "SeriesFinder",
    "PatchInfo",
    "PatchSeries",
    "PatchComments",
    "SeriesComments",
    "StaleChangeInfo",
    "find_series",
    "find_series_by_change",
    "get_series_comments",
    # Staging
    "StagingManager",
    "StagedOperation",
    "StagedPatch",
]

__version__ = "0.1.0"
