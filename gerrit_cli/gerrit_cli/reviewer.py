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

import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from .client import GerritCommentsClient, with_patchset_comment
from .mode import result_fields
from .models import Author, ChangeInfo
from .parallel import parallel_map


@dataclass
class DiffLine:
    """Represents a single line in a diff."""
    line_number_old: Optional[int]  # Line number in old file (None if added)
    line_number_new: Optional[int]  # Line number in new file (None if deleted)
    content: str
    type: str  # 'context', 'added', 'deleted'

    def format(self) -> str:
        """Format the line in standard unified diff format.

        Output: +/-/space prefix followed by the content.
        Line numbers are available in the hunk @@ header; per-line numbers
        are omitted to keep output compact and unambiguously parseable.
        """
        if self.type == 'added':
            return f"+{self.content}"
        elif self.type == 'deleted':
            return f"-{self.content}"
        else:
            return f" {self.content}"


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


def _is_change(section: dict) -> bool:
    return "a" in section or "b" in section


def content_to_hunks(
    content: list[dict], context_lines: Optional[int] = 3,
) -> list[DiffHunk]:
    """Turn a Gerrit DiffInfo content list into unified diff hunks.

    Gerrit returns every unchanged line of the file and ignores the
    diff endpoint's context parameter, so the context is cut here:
    context_lines unchanged lines are kept on each side of a change,
    and None keeps them all.
    """
    hunks: list[DiffHunk] = []
    hunk: Optional[DiffHunk] = None
    old = new = 1

    # Whether a change follows section i before any skip: only then
    # does its tail lead into a hunk.
    change_after = [False] * len(content)
    seen = False
    for i in range(len(content) - 1, -1, -1):
        change_after[i] = seen
        if content[i].get("skip"):
            seen = False
        if _is_change(content[i]):
            seen = True

    def open_hunk() -> DiffHunk:
        nonlocal hunk
        if hunk is None:
            hunk = DiffHunk(old_start=old, old_count=0,
                            new_start=new, new_count=0)
        return hunk

    def close_hunk() -> None:
        nonlocal hunk
        if hunk is not None and hunk.lines:
            hunks.append(hunk)
        hunk = None

    def add_context(lines: list[str]) -> None:
        nonlocal old, new
        if not lines:
            return
        h = open_hunk()
        for text in lines:
            h.lines.append(DiffLine(old, new, text, "context"))
            h.old_count += 1
            h.new_count += 1
            old += 1
            new += 1

    for i, section in enumerate(content):
        if "ab" in section:
            lines = section["ab"]
            if context_lines is None:
                add_context(lines)
                continue
            lead = context_lines if hunk is not None else 0
            trail = context_lines if change_after[i] else 0
            if lead + trail >= len(lines):
                if hunk is not None or change_after[i]:
                    add_context(lines)
                else:
                    old += len(lines)
                    new += len(lines)
                continue
            add_context(lines[:lead])
            close_hunk()
            skipped = len(lines) - lead - trail
            old += skipped
            new += skipped
            add_context(lines[len(lines) - trail:] if trail else [])
        elif _is_change(section):
            h = open_hunk()
            for text in section.get("a", []):
                h.lines.append(DiffLine(old, None, text, "deleted"))
                h.old_count += 1
                old += 1
            for text in section.get("b", []):
                h.lines.append(DiffLine(None, new, text, "added"))
                h.new_count += 1
                new += 1
        if section.get("skip"):
            close_hunk()
            old += section["skip"]
            new += section["skip"]

    close_hunk()
    return hunks


# The context a unified diff from Gerrit's /patch endpoint carries.
PATCH_CONTEXT = 3
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def patch_to_contents(patch: str) -> dict[str, list[dict]]:
    """Each file of a unified diff as Gerrit DiffInfo content.

    The unchanged lines between hunks, which a unified diff leaves out,
    become "skip" sections, so content_to_hunks cuts the same hunks as
    from the per-file diff endpoint, for any context up to
    PATCH_CONTEXT.  Keyed by the new path (the old one for a deleted
    file).  A file with no hunks (binary, mode-only) is absent.
    """
    contents: dict[str, list[dict]] = {}
    content: list[dict] | None = None
    old_path = ""
    next_old = 1
    rem_old = rem_new = 0
    for line in patch.split("\n"):
        if rem_old or rem_new:
            tag, text = line[:1], line[1:]
            if tag == " ":
                kind = "ab"
                rem_old -= 1
                rem_new -= 1
                next_old += 1
            elif tag == "-":
                kind = "a"
                rem_old -= 1
                next_old += 1
            elif tag == "+":
                kind = "b"
                rem_new -= 1
            else:
                continue  # "\ No newline at end of file"
            last = content[-1] if content else {}
            if kind == "ab" and "ab" in last:
                last["ab"].append(text)
            elif kind == "a" and _is_change(last) and "b" not in last:
                last.setdefault("a", []).append(text)
            elif kind == "b" and _is_change(last):
                last.setdefault("b", []).append(text)
            else:
                content.append({kind: [text]})
            continue
        if line.startswith("diff --git "):
            content = None
        elif line.startswith("--- ") and content is None:
            old_path = line[4:]
        elif line.startswith("+++ ") and content is None:
            new_path = line[4:]
            path = old_path if new_path == "/dev/null" else new_path
            content = contents.setdefault(path[2:], [])
            next_old = 1
        elif content is not None and (m := _HUNK_HEADER.match(line)):
            start = int(m.group(1))
            rem_old = int(m.group(2) or 1)
            rem_new = int(m.group(4) or 1)
            if rem_old == 0:
                start += 1  # a pure insertion names the line before it
            if start > next_old:
                content.append({"skip": start - next_old})
            next_old = start
    return contents


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
            **({"new_content": self.new_content}
               if self.new_content is not None else {}),
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
            **result_fields(),
        }


def normalize_review_comments(comments: Any) -> list[dict[str, Any]]:
    """Normalize review comments to the flat list format.

    Accepts either the flat list format used by post_review
    ([{path, line, message, ...}, ...]) or the Gerrit REST API
    format ({path: [{line, message, ...}, ...]}) and returns the
    flat list form. In the dict format the outer key is
    authoritative: a stray "path" key inside an entry is ignored.
    """
    if not comments:
        return []
    if isinstance(comments, dict):
        flat: list[dict[str, Any]] = []
        for path, entries in comments.items():
            for entry in entries:
                flat.append({**entry, "path": path})
        return flat
    return list(comments)


def apply_review_prefix(prefix: Optional[str], message: str) -> str:
    """Prepend a prefix (e.g. "[AI review - opus]") to a message.

    The prefix is rendered as its own bold paragraph so the message
    isn't bunched up against it — which also keeps block-level
    markdown at the start of the message (code fences, lists,
    quotes) rendering correctly.
    """
    if not prefix:
        return message
    prefix = prefix.strip()
    if not prefix:
        return message
    return f"**{prefix}**\n\n{message}"


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
        base: Optional[int] = None,
    ) -> ReviewData:
        """Get all data needed to review a change.

        Args:
            url: Gerrit change URL
            include_file_content: Whether to fetch full file content (slower)
            context_lines: Lines of context around each diff hunk. Pass None
                to fetch the full file as context (Gerrit default, but very
                verbose for large files). Default is 3.
            base: Patchset to diff against; None for the parent commit.

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
            base=base,
        )

    def get_review_data_for_change(
        self,
        change_number: int,
        revision: str = "current",
        include_file_content: bool = False,
        context_lines: Optional[int] = 3,
        base: Optional[int] = None,
    ) -> ReviewData:
        """Get all data needed to review a change by number.

        Args:
            change_number: The change number
            revision: Revision to review ("current" or specific SHA)
            include_file_content: Whether to fetch full file content
            context_lines: Lines of context per hunk. None = full file context.
            base: Patchset to diff against; None for the parent commit.

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
            change_number, revision, include_file_content, context_lines,
            base=base,
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
        base: Optional[int] = None,
    ) -> list[FileChange]:
        """Get all changed files for a revision."""
        query = f"?base={base}" if base is not None else ""
        files_response = self.client.rest.get(
            f"/changes/{change_number}/revisions/{revision}/files{query}"
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

            files.append(file_change)

        need_diff = self._diffs_from_patch(
            change_number, revision, files, context_lines, base)

        # What is left is a request per file: made several at a time,
        # each filling in only its own FileChange.
        diff_ids = {id(f) for f in need_diff}

        def fill(file_change: FileChange) -> None:
            if id(file_change) in diff_ids:
                self._add_diff_to_file(change_number, revision, file_change,
                                       context_lines, base=base)
            if include_file_content:
                self._add_content_to_file(change_number, revision, file_change)

        parallel_map(fill, files if include_file_content else need_diff)

        # Sort by path
        files.sort(key=lambda f: f.path)
        return files

    def _diffs_from_patch(
        self,
        change_number: int,
        revision: str,
        files: list[FileChange],
        context_lines: int | None,
        base: int | None,
    ) -> list[FileChange]:
        """Fill in what one /patch request can; return the files it can't.

        One request for the whole change instead of one per file,
        which also sends every unchanged line.  It carries
        PATCH_CONTEXT lines of context and has no base, and a file
        is taken from it only when its line counts agree with
        Gerrit's own file list.
        """
        if base is not None or context_lines is None or context_lines > PATCH_CONTEXT:
            return files
        try:
            patch = self.client.rest.get(
                f"/changes/{change_number}/revisions/{revision}/patch")
            contents = patch_to_contents(patch) if isinstance(patch, str) else {}
        except Exception:
            return files
        rest = []
        for fc in files:
            content = contents.get(fc.path)
            if content is None or (
                sum(len(s.get("b", [])) for s in content) != fc.lines_added
                or sum(len(s.get("a", [])) for s in content) != fc.lines_deleted
            ):
                rest.append(fc)
                continue
            fc.hunks = content_to_hunks(content, context_lines)
        return rest

    def _add_diff_to_file(
        self,
        change_number: int,
        revision: str,
        file_change: FileChange,
        context_lines: Optional[int] = 3,
        base: Optional[int] = None,
    ) -> None:
        """Add diff hunks to a FileChange.

        Args:
            context_lines: Lines of context around each changed hunk.
                None keeps the whole file as context (very large).
                0 means changed lines only, no surrounding context.
                3 (default) gives a compact, readable diff.
            base: Patchset to diff against; None for the parent commit.
        """
        encoded_path = quote(file_change.path, safe="")
        query = f"?base={base}" if base is not None else ""
        try:
            diff = self.client.rest.get(
                f"/changes/{change_number}/revisions/{revision}"
                f"/files/{encoded_path}/diff{query}")
        except Exception as e:
            print(f"Error getting diff for {file_change.path}: {e}",
                  file=sys.stderr)
            return

        if "content" not in diff:
            return

        hunks = content_to_hunks(diff["content"], context_lines)
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
            print(f"Error getting content for {file_change.path}: {e}",
                  file=sys.stderr)

    def post_review(
        self,
        change_number: int,
        comments: Optional[Any] = None,
        message: Optional[str] = None,
        vote: Optional[int] = None,
        revision: str = "current",
        prefix: Optional[str] = None,
        tag: Optional[str] = None,
        thread: bool = True,
    ) -> ReviewResult:
        """Post a code review with comments.

        Args:
            change_number: The change number
            comments: Either a flat list of comment dicts with keys:
                path, line, message, unresolved (optional), or the
                Gerrit REST format dict mapping path -> comment list
            message: Overall review message, posted as a resolved
                patchset-level comment (see with_patchset_comment)
            vote: Code-Review vote (-2 to +2), None to not vote
            revision: Revision to review
            prefix: Text prepended to the review message and every
                comment (e.g. "[Marc Bot]")
            tag: Gerrit review tag (e.g. "autogenerated:ai-review")
            thread: False posts the message as a bare change message,
                a change log entry nobody can reply to

        Returns:
            ReviewResult with success status
        """
        try:
            comments = normalize_review_comments(comments)
            review_input: dict[str, Any] = {}

            if vote is not None:
                review_input["labels"] = {"Code-Review": vote}

            if tag:
                review_input["tag"] = tag

            if comments:
                comments_dict: dict[str, list[dict[str, Any]]] = {}
                for comment in comments:
                    path = comment["path"]
                    if path not in comments_dict:
                        comments_dict[path] = []

                    comment_input = {
                        "message": apply_review_prefix(prefix, comment["message"]),
                        "unresolved": comment.get("unresolved", True),
                    }
                    if comment.get("line") is not None:
                        comment_input["line"] = comment["line"]
                    if comment.get("range") is not None:
                        comment_input["range"] = comment["range"]
                    if comment.get("side") is not None:
                        comment_input["side"] = comment["side"]
                    comments_dict[path].append(comment_input)

                review_input["comments"] = comments_dict

            if message and thread:
                review_input["comments"] = with_patchset_comment(
                    review_input.get("comments"),
                    apply_review_prefix(prefix, message),
                )
            elif message:
                review_input["message"] = apply_review_prefix(prefix, message)

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
    comments: Optional[Any] = None,
    message: Optional[str] = None,
    vote: Optional[int] = None,
    prefix: Optional[str] = None,
    tag: Optional[str] = None,
    thread: bool = True,
) -> ReviewResult:
    """Convenience function to post a review.

    Args:
        change_number: The change number
        comments: Comment list or Gerrit REST path -> comments dict
        message: Overall review message
        vote: Code-Review vote
        prefix: Text prepended to the message and every comment
        tag: Gerrit review tag (e.g. "autogenerated:ai-review")
        thread: False posts the message as a bare change message

    Returns:
        ReviewResult
    """
    reviewer = CodeReviewer()
    return reviewer.post_review(
        change_number=change_number,
        comments=comments,
        message=message,
        vote=vote,
        prefix=prefix,
        tag=tag,
        thread=thread,
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
