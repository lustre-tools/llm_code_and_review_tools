"""Reply to Gerrit comments."""

from typing import Any, Optional

from .client import GerritCommentsClient
from .mode import MODE_DRAFTS, MODE_ENV_VAR, is_drafts_mode
from .models import Author, Comment, CommentThread, ExtractedComments, ReplyResult
from .staging import StagingManager


def _error_text(exc: Exception) -> str:
    """The exception, with Gerrit's own reason when the server gave one."""
    response = getattr(exc, "response", None)
    body = (getattr(response, "text", "") or "").strip()
    if body and not body.lstrip().startswith("<"):
        return f"{exc}: {' '.join(body.split())[:500]}"
    return str(exc)


def _reply_revisions(change, patch_sets) -> list[str]:
    """The revision to post each reply on, in order.

    A reply goes on the patchset its parent comment was made on, as
    Gerrit's own UI files it: on any later one the thread shows split
    across patchsets, with its line numbers read against other code.
    """
    current = change.get("current_revision") or "current"
    by_number = {
        info.get("_number"): sha
        for sha, info in (change.get("revisions") or {}).items()
        if isinstance(info, dict)
    }
    return [by_number.get(patch_set, current) for patch_set in patch_sets]


def _draft_labels(drafts: Optional[dict]) -> list[tuple[Any, str]]:
    """(draft id, "path:line") for a get_drafts() answer."""
    return [
        (draft.get("id"), f"{path}:{draft.get('line', 'patchset')}")
        for path, path_drafts in (drafts or {}).items()
        for draft in path_drafts
    ]


def _reply_input(parent: Comment, message: str, mark_resolved: bool) -> dict[str, Any]:
    """A reply in the parent's thread, at the parent's own location."""
    comment_input: dict[str, Any] = {
        "in_reply_to": parent.id,
        "message": message,
        "unresolved": not mark_resolved,
    }
    if parent.line is not None:
        comment_input["line"] = parent.line
    if parent.range:
        comment_input["range"] = parent.range
    if parent.side:
        comment_input["side"] = parent.side
    return comment_input


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
            [revision] = _reply_revisions(change, [comment.patch_set])
            self.client.post_review(
                change_number=change_number,
                revision_id=revision,
                comments={comment.file_path: [_reply_input(comment, message, mark_resolved)]},
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
        """Post multiple replies as one change message.

        Each reply goes on its parent comment's patchset (see
        _reply_revisions); see _post_replies for how replies on several
        patchsets are posted, and when that refuses.  All replies are
        posted or none is.

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
                change, [reply_spec["comment"].patch_set for reply_spec in replies],
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

        failure = self._post_replies(change_number, [
            (revision, {
                "path": reply_spec["comment"].file_path,
                **_reply_input(reply_spec["comment"], reply_spec["message"],
                               reply_spec.get("mark_resolved", False)),
            })
            for revision, reply_spec in zip(revisions, replies)
        ])
        error = None
        if failure is not None:
            reason, uncertain = failure
            error = reason + (
                "\nCheck the change in the web UI: if the replies are "
                "there, they were posted; if not, publish or delete their "
                "drafts." if uncertain else "")
        return [
            ReplyResult(
                success=error is None,
                comment_id=reply_spec["comment"].id,
                message=reply_spec["message"],
                marked_resolved=error is None and reply_spec.get("mark_resolved", False),
                error=error,
            )
            for reply_spec in replies
        ]

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

        Each reply goes on its parent comment's patchset, found by the
        comment_id, so a reply staged before a new patchset still lands in
        its thread. All are posted as one message (see _post_replies) or
        none is, and only a push that posted clears the staged replies.

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

        try:
            change = self.client.get_change_detail(change_number)
            current_revision = change.get("current_revision", "")
            current_patchset = change.get("revisions", {}).get(current_revision, {}).get("_number", 0)

            if current_patchset == 0:
                return False, f"Error: Could not determine current patchset for change {change_number}", len(staged.operations)

            published = {} if dry_run else self.client.get_comments(change_number)

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

        parents = {
            raw.get("id"): Comment.from_gerrit(path, raw)
            for path, raws in (published or {}).items()
            for raw in raws
        }
        replies = []
        for op in staged.operations:
            parent = parents.get(op.comment_id) or Comment(
                id=op.comment_id, patch_set=current_patchset,
                file_path=op.file_path, line=op.line, message="",
                author=Author(name=""), unresolved=True, updated="",
            )
            [revision] = _reply_revisions(change, [parent.patch_set])
            replies.append((revision, {
                "path": parent.file_path,
                **_reply_input(parent, op.message, op.resolve),
            }))

        total = len(replies)
        failure = self._post_replies(change_number, replies)
        if failure is not None:
            reason, uncertain = failure
            if uncertain:
                return False, (
                    f"{reason}\nThe {total} replies are still staged and "
                    "their drafts may still be on the change: check it in "
                    "the web UI. If the replies are there, run 'gerrit "
                    f"staged clear {change_number}'; if not, publish or "
                    "delete those drafts. Push refuses while they exist, "
                    "so it cannot post them twice."
                ), total
            return False, (f"{reason}\nNothing was posted; all {total} are "
                           "still staged."), total

        self.staging_manager.clear_staged(change_number)
        if is_drafts_mode():
            return True, (
                f"✓ Created {total} draft replies on Change {change_number}; "
                f"not published ({MODE_ENV_VAR}={MODE_DRAFTS}). Publish them "
                "in the Gerrit web UI."
            ), total
        return True, f"✓ Pushed {total} operations to Change {change_number}", total

    def _post_replies(
        self,
        change_number: int,
        replies: list[tuple[str, dict[str, Any]]],
    ) -> Optional[tuple[str, bool]]:
        """Post replies, each a (revision, CommentInput with "path"), as
        one change message.

        A review POST carries comments for its own revision only, and
        one post per patchset makes as many messages, which Gerrit's
        one-second timestamps mix up: every comment is filed under one of
        them and the others show empty.  So replies on several patchsets
        are first drafts on their patchsets and then published at once,
        as the web UI does.  Publishing takes every draft of the account
        on the change, so drafts it already had make this refuse, and so
        does one saved meanwhile; on any failure the drafts made are
        deleted.  When the publish fails, the drafts tell what happened:
        gone means Gerrit published them and only its answer was lost.
        In drafts mode the replies stay drafts.

        Return: None when posted, else (why not, uncertain); uncertain
                means it could not be told whether the publish went
                through, and the drafts were left in place.
        """
        revisions = {revision for revision, _ in replies}
        if len(revisions) == 1:
            comments: dict[str, list[dict[str, Any]]] = {}
            for _, comment in replies:
                comment = dict(comment)
                comments.setdefault(comment.pop("path"), []).append(comment)
            try:
                self.client.post_review(
                    change_number=change_number,
                    revision_id=revisions.pop(),
                    comments=comments,
                )
            except Exception as e:
                return self._failure(e, change_number), False
            return None

        drafts_mode = is_drafts_mode()
        if not drafts_mode:
            try:
                existing = self.client.get_drafts(change_number) or {}
            except Exception as e:
                return self._failure(e, change_number), False
            mine = [label for _, label in _draft_labels(existing)]
            if mine:
                return (
                    f"Change {change_number} already has {len(mine)} draft "
                    f"comment(s) of yours ({', '.join(mine)}); publishing the "
                    "replies would publish them too. Publish or delete them "
                    "in the web UI, then try again."
                ), False

        created: list[tuple[str, Optional[str]]] = []
        try:
            for revision, comment in replies:
                draft = self.client.create_draft(change_number, revision, comment)
                created.append((revision, (draft or {}).get("id")))
        except Exception as e:
            self._delete_drafts(change_number, created)
            return self._failure(e, change_number), False
        if drafts_mode:
            return None

        ours = {draft_id for _, draft_id in created}
        try:
            # a draft saved in the web UI since the first check
            others = [label for draft_id, label
                      in _draft_labels(self.client.get_drafts(change_number))
                      if draft_id not in ours]
        except Exception as e:
            self._delete_drafts(change_number, created)
            return self._failure(e, change_number), False
        if others:
            self._delete_drafts(change_number, created)
            return (
                f"You saved {len(others)} draft comment(s) on change "
                f"{change_number} meanwhile ({', '.join(others)}); "
                "publishing would have taken them along."
            ), False
        try:
            self.client.publish_drafts(change_number)
        except Exception as e:
            published = self._published(change_number, ours)
            if published is None:
                return (
                    f"Publishing failed or its answer was lost ({_error_text(e)}), "
                    "and gerrit-cli could not tell whether it went through."
                ), True
            if not published:
                self._delete_drafts(change_number, created)
                return self._failure(e, change_number), False
        return None

    def _delete_drafts(self, change_number: int,
                       created: list[tuple[str, Optional[str]]]) -> None:
        """Remove the drafts _post_replies made, best effort."""
        for revision, draft_id in created:
            if draft_id:
                try:
                    self.client.delete_draft(change_number, revision, draft_id)
                except Exception:
                    pass

    def _published(self, change_number: int, ours: set) -> Optional[bool]:
        """After a failed publish: True when this push's drafts are gone
        (Gerrit published them and only its answer was lost), False when
        they are still there, None when that cannot be checked."""
        try:
            left = {draft_id for draft_id, _
                    in _draft_labels(self.client.get_drafts(change_number))}
        except Exception:
            return None
        return not (ours & left)

    @staticmethod
    def _failure(e: Exception, change_number: int) -> str:
        if isinstance(e, ConnectionError):
            msg = f"Network error: Could not connect to Gerrit server.\nDetails: {e}\nPlease check your connection and try again."
        elif isinstance(e, TimeoutError):
            msg = f"Timeout error: Gerrit server did not respond.\nDetails: {e}\nPlease try again later."
        else:
            msg = f"Error posting replies: {_error_text(e)}"
            if "401" in str(e) or "Unauthorized" in str(e):
                msg += "\nPossible cause: Invalid credentials. Check your .env file or environment variables."
            elif "403" in str(e) or "Forbidden" in str(e):
                msg += "\nPossible cause: Insufficient permissions for this operation."
            elif "404" in str(e):
                msg += f"\nPossible cause: Change {change_number} not found or has been deleted."
        return msg


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
