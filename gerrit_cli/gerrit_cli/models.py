"""Data models for Gerrit comments."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Author:
    """Represents a comment author."""
    name: str
    email: Optional[str] = None
    username: Optional[str] = None
    account_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "email": self.email,
            "username": self.username,
            "account_id": self.account_id,
        }

    @classmethod
    def from_gerrit(cls, data: dict) -> "Author":
        """Create Author from Gerrit API response."""
        return cls(
            name=data.get("name", "Unknown"),
            email=data.get("email"),
            username=data.get("username"),
            account_id=data.get("_account_id"),
        )


@dataclass
class CodeContext:
    """Represents the code context around a comment."""
    lines: list[str]
    start_line: int
    end_line: int
    target_line: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "lines": self.lines,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "target_line": self.target_line,
        }

    def format(self) -> str:
        """Format the code context as a string with line numbers."""
        result = []
        for i, line in enumerate(self.lines, start=self.start_line):
            marker = ">>> " if i == self.target_line else "    "
            result.append(f"{marker}{i:4}: {line}")
        return "\n".join(result)


@dataclass
class Comment:
    """Represents a single Gerrit comment."""
    id: str
    patch_set: int
    file_path: str
    line: Optional[int]
    message: str
    author: Author
    unresolved: bool
    updated: str
    in_reply_to: Optional[str] = None
    code_context: Optional[CodeContext] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "patch_set": self.patch_set,
            "file_path": self.file_path,
            "line": self.line,
            "message": self.message,
            "author": self.author.to_dict(),
            "unresolved": self.unresolved,
            "updated": self.updated,
            "in_reply_to": self.in_reply_to,
            "code_context": self.code_context.to_dict() if self.code_context else None,
        }

    def get_gerrit_url(
        self,
        base_url: str,
        project: str,
        change_number: int,
    ) -> str:
        """Generate a Gerrit URL that links to this comment's file and line.

        Args:
            base_url: Gerrit server URL (e.g., "https://review.whamcloud.com")
            project: Project path (e.g., "fs/lustre-release")
            change_number: The change number

        Returns:
            URL linking to the file at the specific line in the patchset
        """
        # Format: https://review.whamcloud.com/c/PROJECT/+/CHANGE/PATCHSET/FILE#LINE
        url = f"{base_url}/c/{project}/+/{change_number}/{self.patch_set}/{self.file_path}"
        if self.line:
            url += f"#{self.line}"
        return url

    @classmethod
    def from_gerrit(cls, file_path: str, data: dict) -> "Comment":
        """Create Comment from Gerrit API response."""
        return cls(
            id=data.get("id", ""),
            patch_set=data.get("patch_set", 0),
            file_path=file_path,
            line=data.get("line"),
            message=data.get("message", ""),
            author=Author.from_gerrit(data.get("author", {})),
            unresolved=data.get("unresolved", False),
            updated=data.get("updated", ""),
            in_reply_to=data.get("in_reply_to"),
        )


@dataclass
class CommentThread:
    """Represents a thread of comments (root + replies)."""
    root_comment: Comment
    replies: list[Comment] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        """A thread is resolved if the last comment is resolved."""
        if self.replies:
            return not self.replies[-1].unresolved
        return not self.root_comment.unresolved

    @property
    def all_comments(self) -> list[Comment]:
        """Get all comments in the thread in order."""
        return [self.root_comment] + self.replies

    def to_dict(self) -> dict:
        return {
            "root_comment": self.root_comment.to_dict(),
            "replies": [r.to_dict() for r in self.replies],
            "is_resolved": self.is_resolved,
        }


@dataclass
class ChangeInfo:
    """Represents basic info about a Gerrit change."""
    change_id: str
    change_number: int
    project: str
    branch: str
    subject: str
    status: str
    current_revision: str
    owner: Author
    url: str
    current_patchset: int = 0

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "change_number": self.change_number,
            "project": self.project,
            "branch": self.branch,
            "subject": self.subject,
            "status": self.status,
            "current_revision": self.current_revision,
            "owner": self.owner.to_dict(),
            "url": self.url,
            "current_patchset": self.current_patchset,
        }


@dataclass
class ExtractedComments:
    """Result of extracting comments from a Gerrit change."""
    change_info: ChangeInfo
    threads: list[CommentThread]
    unresolved_count: int
    total_count: int
    review_messages: list["ReviewMessage"] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "change_info": self.change_info.to_dict(),
            "threads": [t.to_dict() for t in self.threads],
            "unresolved_count": self.unresolved_count,
            "total_count": self.total_count,
            "review_messages": [m.to_dict() for m in self.review_messages],
        }
        # An empty thread list next to a non-zero total reads as "this
        # change has no comments", when it actually means every comment
        # is resolved and the default filter dropped them.  Say so, so
        # the caller reaches for --all rather than the REST API.
        if not self.threads and self.total_count:
            d["hint"] = (
                "0 of %d comment threads shown: all are resolved and "
                "only unresolved threads are listed by default. Use "
                "--all to see them." % self.total_count
            )
        return d

    def get_unresolved_threads(self) -> list[CommentThread]:
        """Get only threads that have unresolved comments."""
        return [t for t in self.threads if not t.is_resolved]

    def format_summary(self) -> str:
        """Format a summary of the extracted comments."""
        lines = [
            f"Change: {self.change_info.subject}",
            f"URL: {self.change_info.url}",
            f"Status: {self.change_info.status}",
            f"Total comments: {self.total_count}",
            f"Unresolved threads: {self.unresolved_count}",
            f"Review messages: {len(self.review_messages)}",
            "",
        ]

        # Show review messages (top-level comments)
        if self.review_messages:
            lines.append("=== Review Messages ===")
            for msg in self.review_messages:
                lines.append(f"\n--- Patch Set {msg.patch_set or 'N/A'} ---")
                lines.append(f"Author: {msg.author.name}")
                lines.append(f"Date: {msg.date}")
                # Truncate long messages
                msg_text = msg.message[:500] + "..." if len(msg.message) > 500 else msg.message
                lines.append(f"Message: {msg_text}")
            lines.append("")

        unresolved = self.get_unresolved_threads()
        if unresolved:
            lines.append("=== Unresolved Threads ===")
            for thread in unresolved:
                lines.append(f"\n--- {thread.root_comment.file_path}:{thread.root_comment.line or 'patchset'} ---")
                lines.append(f"Author: {thread.root_comment.author.name}")
                lines.append(f"Patch Set: {thread.root_comment.patch_set}")
                lines.append(f"Message: {thread.root_comment.message}")
                if thread.root_comment.code_context:
                    lines.append("Code context:")
                    lines.append(thread.root_comment.code_context.format())
                if thread.replies:
                    lines.append("Replies:")
                    for reply in thread.replies:
                        lines.append(f"  - {reply.author.name}: {reply.message[:100]}...")

        return "\n".join(lines)


@dataclass
class ReplyResult:
    """Result of replying to a comment."""
    success: bool
    comment_id: str
    message: str
    marked_resolved: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "comment_id": self.comment_id,
            "message": self.message,
            "marked_resolved": self.marked_resolved,
            "error": self.error,
        }


@dataclass
class ReviewMessage:
    """Represents a top-level review message (overall comment on a patchset).

    These are the messages posted when a reviewer submits a review,
    containing their overall feedback (as opposed to inline file comments).
    """
    id: str
    author: Author
    date: str
    message: str
    patch_set: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "author": self.author.to_dict(),
            "date": self.date,
            "message": self.message,
            "patch_set": self.patch_set,
        }

    @classmethod
    def from_gerrit(cls, data: dict) -> "ReviewMessage":
        """Create ReviewMessage from Gerrit API response."""
        return cls(
            id=data.get("id", ""),
            author=Author.from_gerrit(data.get("author", {})),
            date=data.get("date", ""),
            message=data.get("message", ""),
            patch_set=data.get("_revision_number"),
        )
