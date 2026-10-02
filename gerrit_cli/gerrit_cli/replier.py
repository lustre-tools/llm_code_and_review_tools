"""Reply to Gerrit comments."""

from typing import Any, Optional

from .client import GerritCommentsClient
from .models import Comment, CommentThread, ExtractedComments, ReplyResult
from .staging import StagingManager

# Paths every revision has; an inline comment on any other names a file the
# current revision may no longer have.
_MAGIC_PATHS = {"/PATCHSET_LEVEL", "/COMMIT_MSG", "/MERGE_LIST"}


def _error_text(exc: Exception) -> str:
    """The exception, with Gerrit's own reason when the server gave one."""
    response = getattr(exc, "response", None)
    body = (getattr(response, "text", "") or "").strip()
    if body and not body.lstrip().startswith("<"):
        return f"{exc}: {' '.join(body.split())[:500]}"
    return str(exc)


def _reply_revisions(client, change_number, change, comments) -> list[str]:
    """The revision to post each reply on, in order.

    Gerrit refuses an inline reply on a revision that no longer has the
    commented file, so a reply on a file the current revision deleted or
    renamed goes on the patchset the comment was made on.
    """
    current = change.get("current_revision") or "current"
    by_number = {
        info.get("_number"): sha
        for sha, info in (change.get("revisions") or {}).items()
        if isinstance(info, dict)
    }
    files = None
    revisions = []
    for comment in comments:
        revision = current
        if comment.file_path not in _MAGIC_PATHS:
            if files is None:
                try:
                    files = set(client.get_revision_files(change_number, current))
                except Exception:
                    files = set()
                    by_number = {}
            if comment.file_path not in files and comment.patch_set in by_number:
                revision = by_number[comment.patch_set]
        revisions.append(revision)
    return revisions


class CommentReplier:
    """Reply to comments on Gerrit changes."""

    def __init__(
        self,
        client: Optional[GerritCommentsClient] = None,
        staging_manager: Optional[StagingManager] = None,
    ):
        """Initialize the replier.

        Args:
            client: Optional GerritCommentsClient. Creates default if not provided.
            staging_manager: Optional StagingManager. Creates default if not provided.
        """
        self.client = client or GerritCommentsClient()
        self.staging_manager = staging_manager or StagingManager()

    def reply_to_comment(
        self,
        change_number: int,
        comment: Comment,
        message: str,
        mark_resolved: bool = False,
    ) -> ReplyResult:
        """Reply to a specific comment.

        Args:
            change_number: The change number
            comment: The Comment object to reply to
            message: Reply message
            mark_resolved: Whether to mark the comment thread as resolved

        Returns:
            ReplyResult with success status
        """
        try:
            change = self.client.get_change_detail(change_number)
            [revision] = _reply_revisions(self.client, change_number, change, [comment])

            self.client.reply_to_comment(
                change_number=change_number,
                revision_id=revision,
                file_path=comment.file_path,
                comment_id=comment.id,
                message=message,
                line=comment.line,
                mark_resolved=mark_resolved,
            )

            return ReplyResult(
                success=True,
                comment_id=comment.id,
                message=message,
                marked_resolved=mark_resolved,
            )

        except Exception as e:
            return ReplyResult(
                success=False,
                comment_id=comment.id,
                message=message,
                marked_resolved=False,
                error=_error_text(e),
            )

    def mark_done(
        self,
        change_number: int,
        comment: Comment,
        message: str = "Done",
    ) -> ReplyResult:
        """Mark a comment as done.

        Args:
            change_number: The change number
            comment: The Comment object to mark done
            message: Optional message (default: "Done")

        Returns:
            ReplyResult with success status
        """
        return self.reply_to_comment(
            change_number=change_number,
            comment=comment,
            message=message,
            mark_resolved=True,
        )

    def acknowledge(
        self,
        change_number: int,
        comment: Comment,
        message: str = "Acknowledged",
    ) -> ReplyResult:
        """Acknowledge a comment and mark it as resolved.

        Args:
            change_number: The change number
            comment: The Comment object to acknowledge
            message: Optional message (default: "Acknowledged")

        Returns:
            ReplyResult with success status
        """
        return self.reply_to_comment(
            change_number=change_number,
            comment=comment,
            message=message,
            mark_resolved=True,
        )

    def reply_to_thread(
        self,
        change_number: int,
        thread: CommentThread,
        message: str,
        mark_resolved: bool = False,
    ) -> ReplyResult:
        """Reply to a comment thread.

        Replies to the last comment in the thread.

        Args:
            change_number: The change number
            thread: The CommentThread to reply to
            message: Reply message
            mark_resolved: Whether to mark the thread as resolved

        Returns:
            ReplyResult with success status
        """
        # Reply to the last comment in the thread
        last_comment = thread.replies[-1] if thread.replies else thread.root_comment
        return self.reply_to_comment(
            change_number=change_number,
            comment=last_comment,
            message=message,
            mark_resolved=mark_resolved,
        )

    def mark_thread_done(
        self,
        change_number: int,
        thread: CommentThread,
        message: str = "Done",
    ) -> ReplyResult:
        """Mark a thread as done.

        Args:
            change_number: The change number
            thread: The CommentThread to mark done
            message: Optional message (default: "Done")

        Returns:
            ReplyResult with success status
        """
        return self.reply_to_thread(
            change_number=change_number,
            thread=thread,
            message=message,
            mark_resolved=True,
        )

    def batch_reply(
        self,
        change_number: int,
        replies: list[dict[str, Any]],
    ) -> list[ReplyResult]:
        """Post multiple replies, in one review per revision they go on.

        That is one review unless a reply is on a file the current revision
        no longer has (see _reply_revisions).

        Args:
            change_number: The change number
            replies: List of dicts with keys:
                - comment: Comment object to reply to
                - message: Reply message
                - mark_resolved: Whether to mark resolved (default False)

        Returns:
            List of ReplyResult for each reply, in the order given
        """
        try:
            change = self.client.get_change_detail(change_number)
            revisions = _reply_revisions(
                self.client, change_number, change,
                [reply_spec["comment"] for reply_spec in replies],
            )
        except Exception as e:
            error = _error_text(e)
            return [
                ReplyResult(
                    success=False, comment_id=reply_spec["comment"].id,
                    message=reply_spec["message"], marked_resolved=False, error=error,
                )
                for reply_spec in replies
            ]

        groups: dict[str, list[int]] = {}
        for index, revision in enumerate(revisions):
            groups.setdefault(revision, []).append(index)

        results: list[Optional[ReplyResult]] = [None] * len(replies)
        for revision, indexes in groups.items():
            comments_dict: dict[str, list[dict[str, Any]]] = {}
            for index in indexes:
                reply_spec = replies[index]
                comment = reply_spec["comment"]
                comment_input = {
                    "in_reply_to": comment.id,
                    "message": reply_spec["message"],
                    "unresolved": not reply_spec.get("mark_resolved", False),
                }
                if comment.line is not None:
                    comment_input["line"] = comment.line
                comments_dict.setdefault(comment.file_path, []).append(comment_input)

            error = None
            try:
                self.client.post_review(
                    change_number=change_number,
                    revision_id=revision,
                    comments=comments_dict,
                )
            except Exception as e:
                error = _error_text(e)
            for index in indexes:
                reply_spec = replies[index]
                results[index] = ReplyResult(
                    success=error is None,
                    comment_id=reply_spec["comment"].id,
                    message=reply_spec["message"],
                    marked_resolved=error is None and reply_spec.get("mark_resolved", False),
                    error=error,
                )

        return [result for result in results if result is not None]

    def reply_from_extracted(
        self,
        extracted: ExtractedComments,
        thread_index: int,
        message: str,
        mark_resolved: bool = False,
    ) -> ReplyResult:
        """Reply to a thread from extracted comments by index.

        Args:
            extracted: ExtractedComments object
            thread_index: Index of the thread to reply to
            message: Reply message
            mark_resolved: Whether to mark as resolved

        Returns:
            ReplyResult with success status
        """
        if thread_index < 0 or thread_index >= len(extracted.threads):
            return ReplyResult(
                success=False,
                comment_id="",
                message=message,
                marked_resolved=False,
                error=f"Invalid thread index: {thread_index}",
            )

        thread = extracted.threads[thread_index]
        return self.reply_to_thread(
            change_number=extracted.change_info.change_number,
            thread=thread,
            message=message,
            mark_resolved=mark_resolved,
        )

    def push_staged(
        self,
        change_number: int,
        dry_run: bool = False,
    ) -> tuple[bool, str, int]:
        """Push all staged operations for a change.

        Note: Comments are identified by comment_id, so replies will be correctly
        threaded even if the change has moved to a newer patchset since staging.

        Args:
            change_number: The change number
            dry_run: If True, only show what would be pushed without actually pushing

        Returns:
            Tuple of (success, message, operations_count)
        """
        # Load staged operations
        staged = self.staging_manager.load_staged(change_number)

        if staged is None or not staged.operations:
            return False, f"No staged operations for change {change_number}", 0

        # Get current revision for posting
        try:
            change = self.client.get_change_detail(change_number)
            current_revision = change.get("current_revision", "")
            current_patchset = change.get("revisions", {}).get(current_revision, {}).get("_number", 0)

            if current_patchset == 0:
                return False, f"Error: Could not determine current patchset for change {change_number}", len(staged.operations)

            # Note: No patchset validation needed. Comments are identified by comment_id,
            # so replies work correctly even if patchset has changed.

        except ConnectionError as e:
            return False, f"Network error: Could not connect to Gerrit server.\nDetails: {e}", len(staged.operations)
        except TimeoutError as e:
            return False, f"Timeout error: Gerrit server did not respond in time.\nDetails: {e}", len(staged.operations)
        except Exception as e:
            return False, f"Error getting change details: {e}\nPlease check your network connection and try again.", len(staged.operations)

        # In dry-run mode, just show what would be done
        if dry_run:
            msg = f"Would push {len(staged.operations)} operations to Change {change_number}:\n"
            for op in staged.operations:
                action = "RESOLVE" if op.resolve else "COMMENT"
                location = f"{op.file_path}:{op.line}" if op.line else f"{op.file_path}:patchset"
                msg += f"  [{op.thread_index}] {location} - {action}: \"{op.message[:50]}...\"\n"
            return True, msg, len(staged.operations)

        # Build comments dict for batch posting
        comments_dict: dict[str, list[dict[str, Any]]] = {}

        for op in staged.operations:
            if op.file_path not in comments_dict:
                comments_dict[op.file_path] = []

            comment_input = {
                "in_reply_to": op.comment_id,
                "message": op.message,
                "unresolved": not op.resolve,
            }

            if op.line is not None:
                comment_input["line"] = op.line

            comments_dict[op.file_path].append(comment_input)

        # Push all operations in a single API call
        try:
            self.client.post_review(
                change_number=change_number,
                revision_id=current_revision,
                comments=comments_dict,
            )

            # Success: clear staged file
            self.staging_manager.clear_staged(change_number)

            success_msg = f"✓ Pushed {len(staged.operations)} operations to Change {change_number}"
            return True, success_msg, len(staged.operations)

        except ConnectionError as e:
            return False, f"Network error: Could not connect to Gerrit server.\nDetails: {e}\nPlease check your connection and try again.", len(staged.operations)
        except TimeoutError as e:
            return False, f"Timeout error: Gerrit server did not respond.\nDetails: {e}\nPlease try again later.", len(staged.operations)
        except Exception as e:
            error_msg = f"Error pushing operations: {e}"
            # Check if it's a common error and provide helpful message
            if "401" in str(e) or "Unauthorized" in str(e):
                error_msg += "\nPossible cause: Invalid credentials. Check your .env file or environment variables."
            elif "403" in str(e) or "Forbidden" in str(e):
                error_msg += "\nPossible cause: Insufficient permissions for this operation."
            elif "404" in str(e):
                error_msg += f"\nPossible cause: Change {change_number} not found or has been deleted."
            return False, error_msg, len(staged.operations)


def reply_to_comment(
    change_number: int,
    comment: Comment,
    message: str,
    mark_resolved: bool = False,
) -> ReplyResult:
    """Convenience function to reply to a comment.

    Args:
        change_number: The change number
        comment: The Comment object to reply to
        message: Reply message
        mark_resolved: Whether to mark the comment thread as resolved

    Returns:
        ReplyResult with success status
    """
    replier = CommentReplier()
    return replier.reply_to_comment(
        change_number=change_number,
        comment=comment,
        message=message,
        mark_resolved=mark_resolved,
    )


def mark_done(
    change_number: int,
    comment: Comment,
    message: str = "Done",
) -> ReplyResult:
    """Convenience function to mark a comment as done.

    Args:
        change_number: The change number
        comment: The Comment object to mark done
        message: Optional message (default: "Done")

    Returns:
        ReplyResult with success status
    """
    replier = CommentReplier()
    return replier.mark_done(
        change_number=change_number,
        comment=comment,
        message=message,
    )
