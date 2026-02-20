"""Extract comments from Gerrit changes with code context."""

import re
from typing import Any, Optional

from .client import GerritCommentsClient
from .models import (
    Author,
    ChangeInfo,
    CodeContext,
    Comment,
    CommentThread,
    ExtractedComments,
    ReviewMessage,
)

# Known CI/build systems that post review messages.
# These are filtered out by default when extracting review messages.
CI_BOT_AUTHORS: set[str] = {
    # Lustre CI/build systems
    "Maloo",
    "jenkins",
    "Jenkins",
    # Common CI system names
    "Autotest",
    "CI Bot",
    "Build Bot",
}

# Lint/style/static analysis checkers - kept by default but can be filtered separately.
LINT_BOT_AUTHORS: set[str] = {
    "wc-checkpatch",
    "Misc Code Checks Robot (Gatekeeper helper)",
    "Lustre Gerrit Janitor",  # Posts review comments about code problems
    "Janitor Bot",  # Static analysis and auto-fixes
}


class CommentExtractor:
    """Extracts and organizes comments from Gerrit changes."""

    def __init__(self, client: Optional[GerritCommentsClient] = None):
        """Initialize the extractor.

        Args:
            client: Optional GerritCommentsClient. Creates default if not provided.
        """
        self.client = client or GerritCommentsClient()
        self._file_content_cache: dict[str, list[str]] = {}

    def extract_from_url(
        self,
        url: str,
        include_resolved: bool = False,
        include_code_context: bool = True,
        context_lines: int = 3,
        exclude_ci_bots: bool = True,
        exclude_lint_bots: bool = False,
        include_system: bool = False,
    ) -> ExtractedComments:
        """Extract comments from a Gerrit URL.

        Args:
            url: Gerrit change URL
            include_resolved: Whether to include resolved comment threads
            include_code_context: Whether to fetch code context for comments
            context_lines: Number of lines of context to include around comments
            exclude_ci_bots: Whether to exclude messages from CI/build systems
                (Maloo, Jenkins, etc.). Default True.
            exclude_lint_bots: Whether to exclude messages from lint/style checkers
                (checkpatch, etc.). Default False.
            include_system: Whether to include system messages like
                "Uploaded patch set N", rebases, topic changes. Default False.

        Returns:
            ExtractedComments with all comment data
        """
        # Parse URL and potentially update client if different server
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        if base_url != self.client.url:
            # Create new client for different server
            self.client = GerritCommentsClient(url=base_url)

        return self.extract_from_change(
            change_number=change_number,
            include_resolved=include_resolved,
            include_code_context=include_code_context,
            context_lines=context_lines,
            exclude_ci_bots=exclude_ci_bots,
            exclude_lint_bots=exclude_lint_bots,
            include_system=include_system,
        )

    def extract_from_change(
        self,
        change_number: int,
        include_resolved: bool = False,
        include_code_context: bool = True,
        context_lines: int = 3,
        exclude_ci_bots: bool = True,
        exclude_lint_bots: bool = False,
        include_system: bool = False,
    ) -> ExtractedComments:
        """Extract comments from a Gerrit change number.

        Args:
            change_number: The change number
            include_resolved: Whether to include resolved comment threads
            include_code_context: Whether to fetch code context for comments
            context_lines: Number of lines of context to include around comments
            exclude_ci_bots: Whether to exclude messages from CI/build systems
                (Maloo, Jenkins, etc.). Default True.
            exclude_lint_bots: Whether to exclude messages from lint/style checkers
                (checkpatch, etc.). Default False.
            include_system: Whether to include system messages like
                "Uploaded patch set N", rebases, topic changes. Default False.

        Returns:
            ExtractedComments with all comment data
        """
        # Clear cache for new extraction
        self._file_content_cache = {}

        # Get change details
        change = self.client.get_change_detail(change_number)

        # Get current patchset number for filtering old messages
        current_revision = change.get("current_revision", "")
        current_patchset = change.get("revisions", {}).get(
            current_revision, {}
        ).get("_number", 0)

        change_info = self._build_change_info(change, change_number, current_patchset)

        # Get all inline comments
        raw_comments = self.client.get_comments(change_number)

        # Get all review messages (top-level comments)
        raw_messages = self.client.get_messages(change_number)
        review_messages = self._parse_messages(
            raw_messages,
            current_patchset=current_patchset,
            exclude_ci_bots=exclude_ci_bots,
            exclude_lint_bots=exclude_lint_bots,
            include_system=include_system,
        )

        # Build comment objects and organize into threads
        comments = self._parse_comments(raw_comments)
        threads = self._organize_into_threads(comments)

        # Optionally filter to unresolved only
        if not include_resolved:
            threads = [t for t in threads if not t.is_resolved]

        # Add code context if requested
        if include_code_context:
            for thread in threads:
                self._add_code_context(
                    thread.root_comment,
                    change_number,
                    context_lines,
                )

        # Count totals
        total_count = sum(len(file_comments) for file_comments in raw_comments.values())
        unresolved_count = len([t for t in threads if not t.is_resolved])

        return ExtractedComments(
            change_info=change_info,
            threads=threads,
            unresolved_count=unresolved_count,
            total_count=total_count,
            review_messages=review_messages,
        )

    def _build_change_info(
        self, change: dict[str, Any], change_number: int, current_patchset: int = 0
    ) -> ChangeInfo:
        """Build ChangeInfo from API response."""
        project = change.get("project", "")
        return ChangeInfo(
            change_id=change.get("id", ""),
            change_number=change_number,
            project=project,
            branch=change.get("branch", ""),
            subject=change.get("subject", ""),
            status=change.get("status", ""),
            current_revision=change.get("current_revision", ""),
            owner=Author.from_gerrit(change.get("owner", {})),
            url=self.client.format_change_url(project, change_number),
            current_patchset=current_patchset,
        )

    def _parse_comments(
        self, raw_comments: dict[str, list[dict[str, Any]]]
    ) -> list[Comment]:
        """Parse raw API comments into Comment objects."""
        comments = []
        for file_path, file_comments in raw_comments.items():
            for raw_comment in file_comments:
                comment = Comment.from_gerrit(file_path, raw_comment)
                comments.append(comment)
        return comments

    def _parse_messages(
        self,
        raw_messages: list[dict[str, Any]],
        current_patchset: int,
        exclude_ci_bots: bool = True,
        exclude_lint_bots: bool = False,
        include_system: bool = False,
    ) -> list[ReviewMessage]:
        """Parse raw API messages into ReviewMessage objects.

        Filters out auto-generated messages (like "Uploaded patch set X")
        and optionally filters by author type.

        Args:
            raw_messages: Raw message dicts from Gerrit API
            current_patchset: The current patchset number
            exclude_ci_bots: Whether to exclude CI/build system messages
            exclude_lint_bots: Whether to exclude lint/style checker messages
            include_system: Whether to include system messages
        """
        messages = []
        for raw_msg in raw_messages:
            # Skip messages without content or auto-generated ones
            msg_text = raw_msg.get("message", "")
            if not msg_text:
                continue

            if not include_system:
                # Skip auto-generated "Uploaded patch set" messages
                if msg_text.startswith("Uploaded patch set"):
                    continue

                # Skip other common auto-generated messages
                if msg_text.startswith("Patch Set ") and any(
                    phrase in msg_text for phrase in [
                        "was rebased",
                        "Cherry Picked from",
                        "Commit message was updated",
                    ]
                ):
                    continue

                # Skip topic set/removed messages (noise)
                if msg_text.startswith("Topic set to") or msg_text.startswith("Topic ") and " removed" in msg_text:
                    continue

            # Skip messages that are just "(N comments)" with no substantive text
            # The actual comments are shown separately
            stripped = msg_text.strip()
            # Match patterns like:
            #   "Patch Set N:\n\n(N comment(s))"
            #   "Patch Set N: Code-Review+1\n\n(N comment(s))"
            # Where the only content after the header is "(N comment(s))"
            if re.match(r'^Patch Set \d+:[^\n]*\n\s*\(\d+ comments?\)\s*$', stripped):
                continue

            # Filter by author
            author_name = raw_msg.get("author", {}).get("name", "")
            msg_patchset = raw_msg.get("_revision_number", 0)

            if exclude_ci_bots and author_name in CI_BOT_AUTHORS:
                continue
            if exclude_lint_bots and author_name in LINT_BOT_AUTHORS:
                continue

            # Always exclude lint bot messages from older patchsets
            # (they're just noise - only current patchset lint results matter)
            if author_name in LINT_BOT_AUTHORS and msg_patchset < current_patchset:
                continue

            message = ReviewMessage.from_gerrit(raw_msg)
            messages.append(message)

        return messages

    def _organize_into_threads(self, comments: list[Comment]) -> list[CommentThread]:
        """Organize flat list of comments into threads."""
        # Index comments by ID for quick lookup
        comments_by_id = {c.id: c for c in comments}

        # Group by root comment
        threads_dict: dict[str, CommentThread] = {}
        orphan_replies: list[Comment] = []

        for comment in comments:
            if comment.in_reply_to is None:
                # This is a root comment
                threads_dict[comment.id] = CommentThread(root_comment=comment)
            else:
                orphan_replies.append(comment)

        # Now process replies
        for reply in orphan_replies:
            # Find the root of this reply chain
            root_id = self._find_root_id(reply, comments_by_id)

            if root_id and root_id in threads_dict:
                threads_dict[root_id].replies.append(reply)
            else:
                # Orphan reply - create thread with it as root
                threads_dict[reply.id] = CommentThread(root_comment=reply)

        # Sort replies within each thread by timestamp
        for thread in threads_dict.values():
            thread.replies.sort(key=lambda c: c.updated)

        # Return threads sorted by file path and line number
        threads = list(threads_dict.values())
        threads.sort(key=lambda t: (t.root_comment.file_path, t.root_comment.line or 0))

        return threads

    def _find_root_id(
        self, comment: Comment, comments_by_id: dict[str, Comment]
    ) -> Optional[str]:
        """Find the root comment ID for a reply chain."""
        visited = set()
        current = comment

        while current.in_reply_to and current.in_reply_to not in visited:
            visited.add(current.id)
            parent = comments_by_id.get(current.in_reply_to)
            if parent is None:
                return current.in_reply_to  # Parent might be root but not in our list
            current = parent

        return current.id if current.in_reply_to is None else current.in_reply_to

    def _add_code_context(
        self,
        comment: Comment,
        change_number: int,
        context_lines: int,
    ) -> None:
        """Add code context to a comment."""
        if comment.line is None:
            # Patchset-level comment, no code context
            return

        # Skip virtual files
        if comment.file_path.startswith("/"):
            return

        # Get file content for the relevant patchset
        cache_key = f"{change_number}:{comment.patch_set}:{comment.file_path}"

        if cache_key not in self._file_content_cache:
            lines = self._get_file_lines(
                change_number, comment.patch_set, comment.file_path
            )
            self._file_content_cache[cache_key] = lines

        lines = self._file_content_cache[cache_key]

        if not lines or comment.line > len(lines):
            return

        # Extract context around the target line
        start = max(0, comment.line - context_lines - 1)
        end = min(len(lines), comment.line + context_lines)

        comment.code_context = CodeContext(
            lines=lines[start:end],
            start_line=start + 1,
            end_line=end,
            target_line=comment.line,
        )

    def _get_file_lines(
        self, change_number: int, patch_set: int, file_path: str
    ) -> list[str]:
        """Get the lines of a file from a specific patch set."""
        # Get the revision ID for this patch set
        revision_id = self.client.get_revision_for_patchset(change_number, patch_set)
        if not revision_id:
            return []

        # Get the diff to reconstruct the file
        diff = self.client.get_file_diff(change_number, revision_id, file_path)
        if not diff or "content" not in diff:
            return []

        # Reconstruct file from diff sections
        lines = []
        for section in diff["content"]:
            if "ab" in section:
                # Context lines (unchanged)
                lines.extend(section["ab"])
            elif "b" in section:
                # Added lines (in new version)
                lines.extend(section["b"])
            # Skip 'a' sections (removed lines not in new version)

        return lines


def extract_comments(
    url: str,
    include_resolved: bool = False,
    include_code_context: bool = True,
    context_lines: int = 3,
    exclude_ci_bots: bool = True,
    exclude_lint_bots: bool = False,
    include_system: bool = False,
) -> ExtractedComments:
    """Convenience function to extract comments from a Gerrit URL.

    Args:
        url: Gerrit change URL
        include_resolved: Whether to include resolved comment threads
        include_code_context: Whether to fetch code context for comments
        context_lines: Number of lines of context to include around comments
        exclude_ci_bots: Whether to exclude messages from CI/build systems
            (Maloo, Jenkins, etc.). Default True.
        exclude_lint_bots: Whether to exclude messages from lint/style checkers
            (checkpatch, Janitor Bot, etc.). Default False.
        include_system: Whether to include system messages like
            "Uploaded patch set N", rebases, topic changes. Default False.

    Returns:
        ExtractedComments with all comment data
    """
    extractor = CommentExtractor()
    return extractor.extract_from_url(
        url=url,
        include_resolved=include_resolved,
        include_code_context=include_code_context,
        context_lines=context_lines,
        exclude_ci_bots=exclude_ci_bots,
        exclude_lint_bots=exclude_lint_bots,
        include_system=include_system,
    )
