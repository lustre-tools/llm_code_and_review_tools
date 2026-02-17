"""Code review tool for Gerrit changes.

This module provides functionality to:
1. Fetch change details and modified files from Gerrit
2. Get diffs and file content for review
3. Post review comments with line-specific feedback

Example usage:
    from gerrit_cli import CodeReviewer

    reviewer = CodeReviewer()
    review_data = reviewer.get_review_data("https://review.whamcloud.com/c/fs/lustre-release/+/62796")

    # Review data contains:
    # - change_info: Basic change details
    # - files: List of FileChange objects with diffs and content

    for file_change in review_data.files:
        print(f"File: {file_change.path}")
        print(f"Status: {file_change.status}")
        print(f"Lines added: {file_change.lines_added}")
        print(f"Lines deleted: {file_change.lines_deleted}")
        print(f"Diff:\\n{file_change.format_diff()}")

    # Post a review with comments
    reviewer.post_review(
        change_number=62796,
        comments=[
            {"path": "file.c", "line": 42, "message": "Consider using const here"},
        ],
        message="Overall looks good, minor suggestions below.",
        vote=0,  # -2 to +2 for Code-Review
    )
"""

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from .client import GerritCommentsClient
from .models import Author, ChangeInfo


@dataclass
class DiffLine:
    """Represents a single line in a diff."""
    line_number_old: Optional[int]  # Line number in old file (None if added)
    line_number_new: Optional[int]  # Line number in new file (None if deleted)
    content: str
    type: str  # 'context', 'added', 'deleted'

    def format(self) -> str:
        """Format the line for display."""
        if self.type == 'added':
            prefix = '+'
            line_num = f"    {self.line_number_new:4d}"
        elif self.type == 'deleted':
            prefix = '-'
            line_num = f"{self.line_number_old:4d}    "
        else:
            prefix = ' '
            line_num = f"{self.line_number_old:4d} {self.line_number_new:4d}"
        return f"{line_num} {prefix} {self.content}"


@dataclass
class DiffHunk:
    """Represents a hunk/section in a diff."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)

    def format(self) -> str:
        """Format the hunk for display."""
        header = f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"
        lines = [header] + [line.format() for line in self.lines]
        return "\n".join(lines)


@dataclass
class FileChange:
    """Represents a changed file in a Gerrit change."""
    path: str
    status: str  # 'A' (added), 'D' (deleted), 'M' (modified), 'R' (renamed)
    old_path: Optional[str]  # For renames
    lines_added: int
    lines_deleted: int
    size_delta: int
    hunks: list[DiffHunk] = field(default_factory=list)
    new_content: Optional[str] = None  # Full content of new file
    old_content: Optional[str] = None  # Full content of old file (if available)

    def format_diff(self) -> str:
        """Format the complete diff for this file."""
        header_lines = [
            f"--- a/{self.old_path or self.path}",
            f"+++ b/{self.path}",
        ]
        hunks = [hunk.format() for hunk in self.hunks]
        return "\n".join(header_lines + hunks)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "old_path": self.old_path,
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "size_delta": self.size_delta,
            "diff": self.format_diff(),
            "new_content": self.new_content,
        }


@dataclass
class ReviewComment:
    """A comment to post as part of a review."""
    path: str
    line: int
    message: str
    unresolved: bool = True

    def to_gerrit_format(self) -> dict:
        """Convert to Gerrit API format."""
        return {
            "line": self.line,
            "message": self.message,
            "unresolved": self.unresolved,
        }


@dataclass
class ReviewData:
    """Complete data for reviewing a Gerrit change."""
    change_info: ChangeInfo
    files: list[FileChange]
    commit_message: str
    parent_commit: Optional[str]

    def to_dict(self) -> dict:
        return {
            "change_info": self.change_info.to_dict(),
            "files": [f.to_dict() for f in self.files],
            "commit_message": self.commit_message,
            "parent_commit": self.parent_commit,
        }

    def format_for_review(self) -> str:
        """Format all changes for human/AI review."""
        lines = [
            "=" * 70,
            f"CHANGE: {self.change_info.subject}",
            "=" * 70,
            f"Project: {self.change_info.project}",
            f"Branch: {self.change_info.branch}",
            f"Author: {self.change_info.owner.name}",
            f"Status: {self.change_info.status}",
            f"URL: {self.change_info.url}",
            "",
            "COMMIT MESSAGE:",
            "-" * 40,
            self.commit_message,
            "",
            f"FILES CHANGED ({len(self.files)}):",
            "-" * 40,
        ]

        for f in self.files:
            status_map = {'A': 'added', 'D': 'deleted', 'M': 'modified', 'R': 'renamed'}
            status = status_map.get(f.status, f.status)
            lines.append(f"  {f.path} ({status}, +{f.lines_added}/-{f.lines_deleted})")

        lines.append("")
        lines.append("=" * 70)
        lines.append("DIFFS:")
        lines.append("=" * 70)

        for f in self.files:
            lines.append("")
            lines.append(f"### {f.path} ###")
            lines.append(f.format_diff())

        return "\n".join(lines)


@dataclass
class ReviewResult:
    """Result of posting a review."""
    success: bool
    change_number: int
    comments_posted: int
    message: Optional[str]
    vote: Optional[int]
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "change_number": self.change_number,
            "comments_posted": self.comments_posted,
            "message": self.message,
            "vote": self.vote,
            "error": self.error,
        }


class CodeReviewer:
    """Tool for reviewing Gerrit code changes."""

    def __init__(self, client: Optional[GerritCommentsClient] = None):
        """Initialize the reviewer.

        Args:
            client: Optional GerritCommentsClient. Creates default if not provided.
        """
        self.client = client or GerritCommentsClient()

    def get_review_data(
        self,
        url: str,
        include_file_content: bool = False,
        context_lines: Optional[int] = 3,
    ) -> ReviewData:
        """Get all data needed to review a change.

        Args:
            url: Gerrit change URL
            include_file_content: Whether to fetch full file content (slower)
            context_lines: Lines of context around each diff hunk. Pass None
                to fetch the full file as context (Gerrit default, but very
                verbose for large files). Default is 3.

        Returns:
            ReviewData with change info and all file diffs
        """
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        if base_url != self.client.url:
            self.client = GerritCommentsClient(url=base_url)

        return self.get_review_data_for_change(
            change_number=change_number,
            include_file_content=include_file_content,
            context_lines=context_lines,
        )

    def get_review_data_for_change(
        self,
        change_number: int,
        revision: str = "current",
        include_file_content: bool = False,
        context_lines: Optional[int] = 3,
    ) -> ReviewData:
        """Get all data needed to review a change by number.

        Args:
            change_number: The change number
            revision: Revision to review ("current" or specific SHA)
            include_file_content: Whether to fetch full file content
            context_lines: Lines of context per hunk. None = full file context.

        Returns:
            ReviewData with change info and all file diffs
        """
        # Get change details with commit info
        change = self.client.rest.get(
            f"/changes/{change_number}?"
            "o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_ACCOUNTS"
        )

        # Build change info
        project = change.get("project", "")
        current_rev = change.get("current_revision", "")
        revisions = change.get("revisions", {})
        rev_data = revisions.get(current_rev, {})
        commit_data = rev_data.get("commit", {})

        change_info = ChangeInfo(
            change_id=change.get("id", ""),
            change_number=change_number,
            project=project,
            branch=change.get("branch", ""),
            subject=change.get("subject", ""),
            status=change.get("status", ""),
            current_revision=current_rev,
            owner=Author.from_gerrit(change.get("owner", {})),
            url=self.client.format_change_url(project, change_number),
        )

        # Get commit message
        commit_message = commit_data.get("message", "")
        parent_commit = None
        parents = commit_data.get("parents", [])
        if parents:
            parent_commit = parents[0].get("commit")

        # Get list of files
        files = self._get_files_for_revision(
            change_number, revision, include_file_content, context_lines
        )

        return ReviewData(
            change_info=change_info,
            files=files,
            commit_message=commit_message,
            parent_commit=parent_commit,
        )

    def _get_files_for_revision(
        self,
        change_number: int,
        revision: str,
        include_file_content: bool,
        context_lines: Optional[int] = 3,
    ) -> list[FileChange]:
        """Get all changed files for a revision."""
        # Get file list
        files_response = self.client.rest.get(
            f"/changes/{change_number}/revisions/{revision}/files"
        )

        files = []
        for path, file_info in files_response.items():
            if path == "/COMMIT_MSG":
                continue  # Skip commit message pseudo-file

            file_change = FileChange(
                path=path,
                status=file_info.get("status", "M"),
                old_path=file_info.get("old_path"),
                lines_added=file_info.get("lines_inserted", 0),
                lines_deleted=file_info.get("lines_deleted", 0),
                size_delta=file_info.get("size_delta", 0),
            )

            # Get diff for this file
            self._add_diff_to_file(change_number, revision, file_change, context_lines)

            # Optionally get full file content
            if include_file_content:
                self._add_content_to_file(change_number, revision, file_change)

            files.append(file_change)

        # Sort by path
        files.sort(key=lambda f: f.path)
        return files

    def _add_diff_to_file(
        self,
        change_number: int,
        revision: str,
        file_change: FileChange,
        context_lines: Optional[int] = 3,
    ) -> None:
        """Add diff hunks to a FileChange.

        Args:
            context_lines: Lines of context around each changed hunk.
                None means the Gerrit default (full file as context, very large).
                0 means changed lines only, no surrounding context.
                3 (default) gives a compact, readable diff.
        """
        encoded_path = quote(file_change.path, safe="")
        try:
            if context_lines is None:
                diff_url = f"/changes/{change_number}/revisions/{revision}/files/{encoded_path}/diff"
            else:
                diff_url = f"/changes/{change_number}/revisions/{revision}/files/{encoded_path}/diff?context={context_lines}"
            diff = self.client.rest.get(diff_url)
        except Exception as e:
            print(f"Error getting diff for {file_change.path}: {e}")
            return

        if "content" not in diff:
            return

        # Parse diff content into hunks
        content_sections = diff.get("content", [])
        diff.get("meta_a", {})
        diff.get("meta_b", {})

        # Track line numbers
        old_line = 1
        new_line = 1

        current_hunk = None
        hunks = []

        for section in content_sections:
            if "ab" in section:
                # Context lines (unchanged)
                for line_content in section["ab"]:
                    if current_hunk is None:
                        current_hunk = DiffHunk(
                            old_start=old_line,
                            old_count=0,
                            new_start=new_line,
                            new_count=0,
                        )
                    current_hunk.lines.append(DiffLine(
                        line_number_old=old_line,
                        line_number_new=new_line,
                        content=line_content,
                        type="context",
                    ))
                    current_hunk.old_count += 1
                    current_hunk.new_count += 1
                    old_line += 1
                    new_line += 1

            elif "a" in section or "b" in section:
                # Changed lines
                if current_hunk is None:
                    current_hunk = DiffHunk(
                        old_start=old_line,
                        old_count=0,
                        new_start=new_line,
                        new_count=0,
                    )

                # Deleted lines
                for line_content in section.get("a", []):
                    current_hunk.lines.append(DiffLine(
                        line_number_old=old_line,
                        line_number_new=None,
                        content=line_content,
                        type="deleted",
                    ))
                    current_hunk.old_count += 1
                    old_line += 1

                # Added lines
                for line_content in section.get("b", []):
                    current_hunk.lines.append(DiffLine(
                        line_number_old=None,
                        line_number_new=new_line,
                        content=line_content,
                        type="added",
                    ))
                    current_hunk.new_count += 1
                    new_line += 1

            # Check if we should finalize this hunk (skip marker)
            if section.get("skip"):
                if current_hunk and current_hunk.lines:
                    hunks.append(current_hunk)
                current_hunk = None
                skip_count = section.get("skip", 0)
                old_line += skip_count
                new_line += skip_count

        # Add final hunk
        if current_hunk and current_hunk.lines:
            hunks.append(current_hunk)

        file_change.hunks = hunks

    def _add_content_to_file(
        self,
        change_number: int,
        revision: str,
        file_change: FileChange,
    ) -> None:
        """Add full file content to a FileChange."""
        if file_change.status == "D":
            # Deleted file, no new content
            return

        encoded_path = quote(file_change.path, safe="")
        try:
            # Content comes base64 encoded
            import base64
            content = self.client.rest.get(
                f"/changes/{change_number}/revisions/{revision}/files/{encoded_path}/content"
            )
            if isinstance(content, str):
                try:
                    file_change.new_content = base64.b64decode(content).decode("utf-8")
                except Exception:
                    file_change.new_content = content
        except Exception as e:
            print(f"Error getting content for {file_change.path}: {e}")

    def post_review(
        self,
        change_number: int,
        comments: Optional[list[dict[str, Any]]] = None,
        message: Optional[str] = None,
        vote: Optional[int] = None,
        revision: str = "current",
    ) -> ReviewResult:
        """Post a code review with comments.

        Args:
            change_number: The change number
            comments: List of comment dicts with keys: path, line, message, unresolved (optional)
            message: Overall review message
            vote: Code-Review vote (-2 to +2), None to not vote
            revision: Revision to review

        Returns:
            ReviewResult with success status
        """
        try:
            review_input: dict[str, Any] = {}

            if message:
                review_input["message"] = message

            if vote is not None:
                review_input["labels"] = {"Code-Review": vote}

            if comments:
                comments_dict: dict[str, list[dict[str, Any]]] = {}
                for comment in comments:
                    path = comment["path"]
                    if path not in comments_dict:
                        comments_dict[path] = []

                    comment_input = {
                        "line": comment["line"],
                        "message": comment["message"],
                        "unresolved": comment.get("unresolved", True),
                    }
                    comments_dict[path].append(comment_input)

                review_input["comments"] = comments_dict

            self.client.rest.post(
                f"/changes/{change_number}/revisions/{revision}/review",
                json=review_input,
            )

            return ReviewResult(
                success=True,
                change_number=change_number,
                comments_posted=len(comments) if comments else 0,
                message=message,
                vote=vote,
            )

        except Exception as e:
            return ReviewResult(
                success=False,
                change_number=change_number,
                comments_posted=0,
                message=message,
                vote=vote,
                error=str(e),
            )

    def post_comment(
        self,
        change_number: int,
        path: str,
        line: int,
        message: str,
        unresolved: bool = True,
        revision: str = "current",
    ) -> ReviewResult:
        """Post a single comment on a specific line.

        Args:
            change_number: The change number
            path: File path
            line: Line number
            message: Comment message
            unresolved: Whether to mark as unresolved
            revision: Revision to comment on

        Returns:
            ReviewResult
        """
        return self.post_review(
            change_number=change_number,
            comments=[{
                "path": path,
                "line": line,
                "message": message,
                "unresolved": unresolved,
            }],
            revision=revision,
        )

    def post_patchset_comment(
        self,
        change_number: int,
        message: str,
        unresolved: bool = True,
        revision: str = "current",
    ) -> ReviewResult:
        """Post a patchset-level comment (not tied to any file or line).

        This posts a comment on the special /PATCHSET_LEVEL pseudo-file,
        which appears as a general comment on the change, separate from
        the overall review message.

        Args:
            change_number: The change number
            message: Comment message
            unresolved: Whether to mark as unresolved
            revision: Revision to comment on

        Returns:
            ReviewResult
        """
        try:
            comment_input = {
                "message": message,
                "unresolved": unresolved,
            }

            review_input = {
                "comments": {
                    "/PATCHSET_LEVEL": [comment_input]
                }
            }

            self.client.rest.post(
                f"/changes/{change_number}/revisions/{revision}/review",
                json=review_input,
            )

            return ReviewResult(
                success=True,
                change_number=change_number,
                comments_posted=1,
                message=None,
                vote=None,
            )

        except Exception as e:
            return ReviewResult(
                success=False,
                change_number=change_number,
                comments_posted=0,
                message=None,
                vote=None,
                error=str(e),
            )


def get_review_data(url: str, include_file_content: bool = False) -> ReviewData:
    """Convenience function to get review data from a Gerrit URL.

    Args:
        url: Gerrit change URL
        include_file_content: Whether to fetch full file content

    Returns:
        ReviewData with change info and diffs
    """
    reviewer = CodeReviewer()
    return reviewer.get_review_data(url, include_file_content)


def post_review(
    change_number: int,
    comments: Optional[list[dict[str, Any]]] = None,
    message: Optional[str] = None,
    vote: Optional[int] = None,
) -> ReviewResult:
    """Convenience function to post a review.

    Args:
        change_number: The change number
        comments: List of comment dicts
        message: Overall review message
        vote: Code-Review vote

    Returns:
        ReviewResult
    """
    reviewer = CodeReviewer()
    return reviewer.post_review(
        change_number=change_number,
        comments=comments,
        message=message,
        vote=vote,
    )


def post_patchset_comment(
    change_number: int,
    message: str,
    unresolved: bool = True,
) -> ReviewResult:
    """Convenience function to post a patchset-level comment.

    Args:
        change_number: The change number
        message: Comment message
        unresolved: Whether to mark as unresolved

    Returns:
        ReviewResult
    """
    reviewer = CodeReviewer()
    return reviewer.post_patchset_comment(
        change_number=change_number,
        message=message,
        unresolved=unresolved,
    )
