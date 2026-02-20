"""
Gerrit patch series finder.

Find all patches in a series given a link to any patch in the series.

This tool traces the LINEAR parent chain of commits:
1. Finds the tip of the series (commit with no children in the related set)
2. Walks backwards through parent commits to the base
3. Returns patches in order from base to tip

NOTE: This follows a single linear path. If the series has branches (multiple
children from a single commit), only one path is followed. Use the Gerrit web
UI's "Related Changes" view to see the full graph of related changes.
"""

import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from .client import GerritCommentsClient
from .extractor import CommentExtractor
from .models import CommentThread, ReviewMessage


@dataclass
class PatchInfo:
    """Information about a single patch in a series."""
    change_number: int
    subject: str
    commit: str
    parent_commit: str
    status: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_number": self.change_number,
            "subject": self.subject,
            "commit": self.commit,
            "parent_commit": self.parent_commit,
            "status": self.status,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_url: str = "") -> "PatchInfo":
        """Create PatchInfo from a dictionary.

        Args:
            data: Dictionary with patch info (at minimum: change_number, subject)
            base_url: Base URL for constructing patch URL if not in data

        Returns:
            PatchInfo instance
        """
        change_number = data.get("change_number", 0)
        url = data.get("url", "")
        if not url and base_url:
            url = f"{base_url}/{change_number}"
        return cls(
            change_number=change_number,
            subject=data.get("subject", ""),
            commit=data.get("commit", ""),
            parent_commit=data.get("parent_commit", ""),
            status=data.get("status", ""),
            url=url,
        )


@dataclass
class StaleChangeInfo:
    """Information about a stale change (one with a newer patchset)."""
    change_number: int
    old_revision: int  # The revision in the current chain
    current_revision: int  # The latest revision available
    still_in_series: bool  # True if current revision is still connected to series
    subject: str = ""


@dataclass
class PatchSeries:
    """A series of related patches."""
    patches: list[PatchInfo] = field(default_factory=list)
    target_change: Optional[int] = None  # The change that was queried
    target_position: Optional[int] = None  # 1-indexed position in series
    tip_change: Optional[int] = None  # The tip of the series
    base_change: Optional[int] = None  # The base of the series
    error: Optional[str] = None  # Error message if series is invalid
    stale_changes: list[int] = field(default_factory=list)  # Changes with newer patchsets
    stale_info: list[StaleChangeInfo] = field(default_factory=list)  # Detailed stale info
    needs_reintegration: bool = False  # True if stale changes can be reintegrated

    def __len__(self) -> int:
        return len(self.patches)

    def get_change_numbers(self) -> list[int]:
        """Return list of change numbers in order (base to tip)."""
        return [p.change_number for p in self.patches]

    def get_urls(self) -> list[str]:
        """Return list of URLs in order (base to tip)."""
        return [p.url for p in self.patches]

    def format_summary(self) -> str:
        """Format a human-readable summary of the series."""
        lines = [
            "=" * 70,
            f"PATCH SERIES ({len(self.patches)} patches)",
            "=" * 70,
            "",
        ]

        if self.target_change and self.target_position:
            lines.append(f"Queried change {self.target_change} is at position {self.target_position}/{len(self.patches)}")
            lines.append("")

        for i, patch in enumerate(self.patches, 1):
            marker = " <-- queried" if patch.change_number == self.target_change else ""
            lines.append(f"{i:3}. {patch.change_number:5} | {patch.subject[:50]}{marker}")

        lines.append("")
        lines.append("=" * 70)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_patches": len(self.patches),
            "target_change": self.target_change,
            "target_position": self.target_position,
            "tip_change": self.tip_change,
            "base_change": self.base_change,
            "patches": [p.to_dict() for p in self.patches],
        }


@dataclass
class PatchComments:
    """Comments for a single patch in a series."""
    change_number: int
    subject: str
    url: str
    current_patchset: int
    threads: list[CommentThread] = field(default_factory=list)
    review_messages: list[ReviewMessage] = field(default_factory=list)

    @property
    def unresolved_count(self) -> int:
        """Number of unresolved comment threads."""
        return sum(1 for t in self.threads if not t.is_resolved)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_number": self.change_number,
            "subject": self.subject,
            "url": self.url,
            "current_patchset": self.current_patchset,
            "unresolved_count": self.unresolved_count,
            "threads": [self._thread_to_dict(t) for t in self.threads],
            "review_messages": [m.to_dict() for m in self.review_messages],
        }

    def _thread_to_dict(self, thread: CommentThread) -> dict[str, Any]:
        """Convert a CommentThread to dict with full details."""
        root = thread.root_comment
        return {
            "file_path": root.file_path,
            "line": root.line,
            "patch_set": root.patch_set,
            "author": {
                "name": root.author.name,
                "email": root.author.email,
                "username": root.author.username,
            },
            "message": root.message,
            "updated": root.updated,
            "is_resolved": thread.is_resolved,
            "code_context": root.code_context.to_dict() if root.code_context else None,
            "replies": [
                {
                    "author": {"name": r.author.name, "email": r.author.email},
                    "message": r.message,
                    "updated": r.updated,
                }
                for r in thread.replies
            ],
        }

    def format_summary(self) -> str:
        """Format a human-readable summary of this patch's comments."""
        lines = [
            f"Change {self.change_number}: {self.subject}",
            f"  URL: {self.url}",
            f"  Current patchset: {self.current_patchset}",
            f"  Unresolved threads: {self.unresolved_count}",
            f"  Review messages: {len(self.review_messages)}",
        ]

        # Show review messages (top-level comments)
        if self.review_messages:
            lines.append("")
            lines.append("  === Review Messages ===")
            for i, msg in enumerate(self.review_messages):
                lines.append("")
                lines.append(f"  [msg {i}] Patch Set {msg.patch_set or 'N/A'}")
                lines.append(f"      Author: {msg.author.name}")
                lines.append(f"      Date: {msg.date}")
                # Show first 200 chars of message
                msg_preview = msg.message.replace('\n', ' ')[:200]
                lines.append(f"      Message: {msg_preview}{'...' if len(msg.message) > 200 else ''}")

        # Show inline comment threads
        if self.threads:
            lines.append("")
            lines.append("  === Inline Comments ===")

        for i, thread in enumerate(self.threads):
            root = thread.root_comment
            location = f"{root.file_path}:{root.line}" if root.line else f"{root.file_path} (file-level)"
            lines.append("")
            lines.append(f"  [{i}] {location} (patchset {root.patch_set})")
            lines.append(f"      Author: {root.author.name}")
            # Show first 100 chars of message
            msg_preview = root.message.replace('\n', ' ')[:100]
            lines.append(f"      Message: {msg_preview}{'...' if len(root.message) > 100 else ''}")

            if root.code_context:
                lines.append("      Context:")
                for ctx_line in root.code_context.format().split('\n')[:5]:
                    lines.append(f"        {ctx_line}")

        return "\n".join(lines)


@dataclass
class SeriesComments:
    """All unresolved comments across a patch series."""
    series: PatchSeries
    patches_with_comments: list[PatchComments] = field(default_factory=list)

    @property
    def total_unresolved(self) -> int:
        """Total number of unresolved threads across all patches."""
        return sum(p.unresolved_count for p in self.patches_with_comments)

    @property
    def patches_with_unresolved(self) -> int:
        """Number of patches that have unresolved comments."""
        return sum(1 for p in self.patches_with_comments if p.unresolved_count > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "series": self.series.to_dict(),
            "total_unresolved": self.total_unresolved,
            "patches_with_unresolved": self.patches_with_unresolved,
            "patches": [p.to_dict() for p in self.patches_with_comments],
        }

    def format_summary(self) -> str:
        """Format a human-readable summary of all comments."""
        lines = [
            "=" * 70,
            f"SERIES COMMENTS ({self.total_unresolved} unresolved across {self.patches_with_unresolved} patches)",
            "=" * 70,
            f"Series: {len(self.series)} patches from {self.series.base_change} to {self.series.tip_change}",
            "",
        ]

        for patch_comments in self.patches_with_comments:
            if patch_comments.unresolved_count == 0:
                continue

            lines.append("-" * 70)
            lines.append(patch_comments.format_summary())

        lines.append("")
        lines.append("=" * 70)
        return "\n".join(lines)


class SeriesFinder:
    """Find all patches in a Gerrit series."""

    def __init__(self, client: Optional[GerritCommentsClient] = None):
        self.client = client or GerritCommentsClient()

    def find_series(self, url: str, include_abandoned: bool = False) -> PatchSeries:
        """
        Find all patches in the linear series containing the given change.

        Traces the parent chain from tip to base, returning patches in order.
        Only follows one linear path - branches are not included.

        Args:
            url: Gerrit change URL (e.g., https://review.whamcloud.com/c/fs/lustre-release/+/61965)
            include_abandoned: If True, include abandoned patches in the series

        Returns:
            PatchSeries object containing all patches in order (base to tip)
        """
        # Parse the URL to get change number
        # parse_gerrit_url returns (base_url, change_number)
        _, change_number = self.client.parse_gerrit_url(url)

        return self.find_series_by_change(change_number, include_abandoned)

    def find_series_by_change(self, change_number: int, include_abandoned: bool = False) -> PatchSeries:
        """
        Find all patches in the series containing the given change number.

        Args:
            change_number: Gerrit change number
            include_abandoned: If True, include abandoned patches in the series

        Returns:
            PatchSeries object containing all patches in order (base to tip)
        """
        # Step 1: Get related changes from the given change
        related = self._get_related_changes(change_number)

        if not related:
            # No related changes - this is a standalone patch
            detail = self.client.get_change_detail(change_number)
            patch = self._make_patch_info(change_number, detail)
            return PatchSeries(
                patches=[patch] if patch else [],
                target_change=change_number,
                target_position=1,
                tip_change=change_number,
                base_change=change_number,
            )

        # Step 2: Build commit maps from related changes
        status_filter = ['NEW'] if not include_abandoned else ['NEW', 'ABANDONED']
        changes_map = self._build_commit_map(related, status_filter)

        if not changes_map:
            return PatchSeries(target_change=change_number)

        # Step 3: Find the tip of the series (change with no children)
        tip_commit = self._find_tip(changes_map)

        if not tip_commit:
            return PatchSeries(target_change=change_number)

        tip_change = changes_map[tip_commit]['change']

        # Step 4: If the tip is different from our starting point,
        # get related changes from the tip for a complete view
        if tip_change != change_number:
            related = self._get_related_changes(tip_change)
            changes_map = self._build_commit_map(related, status_filter)
            tip_commit = self._find_tip(changes_map)
            if not tip_commit:
                return PatchSeries(target_change=change_number)

        # Step 5: Walk backwards from tip to build the chain
        chain = self._walk_chain_backwards(changes_map, tip_commit)

        # Step 6: Check for stale changes (patches with newer patchsets)
        stale_changes, stale_info, error, needs_reintegration = self._check_stale_changes(
            chain, tip_change
        )

        # Step 7: Build PatchSeries result
        patches = []
        target_position = None

        for i, info in enumerate(chain, 1):
            patch = PatchInfo(
                change_number=info['change'],
                subject=info['subject'],
                commit=info['commit'][:12],
                parent_commit=info['parent'][:12] if info['parent'] else '',
                status=info['status'],
                url=f"{self.client.url}/{info['change']}",
            )
            patches.append(patch)

            if info['change'] == change_number:
                target_position = i

        return PatchSeries(
            patches=patches,
            target_change=change_number,
            target_position=target_position,
            tip_change=patches[-1].change_number if patches else None,
            base_change=patches[0].change_number if patches else None,
            error=error,
            stale_changes=stale_changes,
            stale_info=stale_info,
            needs_reintegration=needs_reintegration,
        )

    def get_series_comments(
        self,
        url: str,
        include_resolved: bool = False,
        include_code_context: bool = True,
        context_lines: int = 3,
        exclude_ci_bots: bool = True,
        exclude_lint_bots: bool = False,
        show_progress: bool = False,
        include_system: bool = False,
    ) -> SeriesComments:
        """
        Get all unresolved comments from all patches in a series.

        Args:
            url: Gerrit change URL (any patch in the series)
            include_resolved: If True, include resolved comments
            include_code_context: If True, include surrounding code for each comment
            context_lines: Number of lines of code context
            exclude_ci_bots: Whether to exclude messages from CI/build systems
                (Maloo, Jenkins, etc.). Default True.
            exclude_lint_bots: Whether to exclude messages from lint/style checkers
                (checkpatch, Janitor Bot, etc.). Default False.
            show_progress: If True, show progress indicator to stderr
            include_system: If True, include system messages

        Returns:
            SeriesComments object with comments grouped by patch
        """
        # First find the series
        series = self.find_series(url)

        # Create extractor with same client
        extractor = CommentExtractor(client=self.client)

        patches_with_comments = []
        total = len(series.patches)

        for i, patch in enumerate(series.patches, 1):
            if show_progress:
                print(
                    f"\rFetching comments... ({i}/{total}) {patch.change_number}",
                    end="", flush=True, file=sys.stderr
                )
            # Extract comments for this patch
            try:
                extracted = extractor.extract_from_change(
                    change_number=patch.change_number,
                    include_resolved=include_resolved,
                    include_code_context=include_code_context,
                    context_lines=context_lines,
                    exclude_ci_bots=exclude_ci_bots,
                    exclude_lint_bots=exclude_lint_bots,
                    include_system=include_system,
                )

                # Include patches that have threads or review messages
                if extracted.threads or extracted.review_messages:
                    # Get max patchset from threads or review messages
                    max_patchset = 0
                    if extracted.threads:
                        max_patchset = max(t.root_comment.patch_set for t in extracted.threads)
                    if extracted.review_messages:
                        msg_patchset = max((m.patch_set or 0) for m in extracted.review_messages)
                        max_patchset = max(max_patchset, msg_patchset)

                    patch_comments = PatchComments(
                        change_number=patch.change_number,
                        subject=patch.subject,
                        url=patch.url,
                        current_patchset=max_patchset,
                        threads=extracted.threads,
                        review_messages=extracted.review_messages,
                    )
                    patches_with_comments.append(patch_comments)
            except Exception:
                # Skip patches that fail to extract (might be permission issues)
                continue

        if show_progress:
            # Clear the progress line
            print("\r" + " " * 60 + "\r", end="", file=sys.stderr)

        return SeriesComments(
            series=series,
            patches_with_comments=patches_with_comments,
        )

    def _get_related_changes(self, change_number: int) -> list[dict[str, Any]]:
        """Get related changes for a change."""
        try:
            response = self.client.rest.get(
                f"/changes/{change_number}/revisions/current/related"
            )
            return response.get('changes', [])
        except Exception:
            return []

    def _build_commit_map(
        self,
        related: list[dict[str, Any]],
        status_filter: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Build a map of commit -> change info from related changes."""
        changes_map = {}

        for c in related:
            status = c.get('status', '')
            if status not in status_filter:
                continue

            commit_info = c.get('commit', {})
            commit_id = commit_info.get('commit', '')
            parents = commit_info.get('parents', [])
            parent_id = parents[0].get('commit', '') if parents else ''

            if commit_id:
                changes_map[commit_id] = {
                    'change': c.get('_change_number'),
                    'subject': commit_info.get('subject', ''),
                    'parent': parent_id,
                    'commit': commit_id,
                    'status': status,
                    'revision_number': c.get('_revision_number'),
                    'current_revision_number': c.get('_current_revision_number'),
                }

        return changes_map

    def _find_tip(self, changes_map: dict[str, dict[str, Any]]) -> Optional[str]:
        """Find the tip commit (the one with no children in the set)."""
        # Build set of all parents
        all_parents = {info['parent'] for info in changes_map.values() if info['parent']}

        # Tip is a commit that is not a parent of any other commit in the set
        for commit_id in changes_map:
            if commit_id not in all_parents:
                return commit_id

        # Fallback: return any commit if we can't find a clear tip
        return next(iter(changes_map), None)

    def _walk_chain_backwards(
        self,
        changes_map: dict[str, dict[str, Any]],
        tip_commit: str
    ) -> list[dict[str, Any]]:
        """Walk from tip backwards through parents to build the chain."""
        chain = []
        current = tip_commit
        visited = set()

        while current and current not in visited:
            visited.add(current)

            if current in changes_map:
                chain.append(changes_map[current])
                parent = changes_map[current]['parent']

                if parent in changes_map:
                    current = parent
                else:
                    break
            else:
                break

        # Reverse to get base-to-tip order
        chain.reverse()
        return chain

    def _check_stale_changes(
        self,
        chain: list[dict[str, Any]],
        tip_change: int
    ) -> tuple[list[int], list[StaleChangeInfo], Optional[str], bool]:
        """Check for changes in the chain that have newer patchsets.

        Args:
            chain: List of change info dicts from walking the chain
            tip_change: The change number of the tip of the series

        Returns:
            Tuple of (stale_change_numbers, stale_info, error_message, needs_reintegration)
            - stale_change_numbers: List of change numbers with newer patchsets
            - stale_info: Detailed info about each stale change
            - error_message: Error if a stale change is no longer in series, None otherwise
            - needs_reintegration: True if stale changes can be auto-reintegrated
        """
        stale_changes: list[int] = []
        stale_info: list[StaleChangeInfo] = []

        for info in chain:
            rev_num = info.get('revision_number')
            curr_rev_num = info.get('current_revision_number')
            change_num = info.get('change')
            subject = info.get('subject', '')

            # Skip if revision numbers or change number not available (shouldn't happen)
            if rev_num is None or curr_rev_num is None or change_num is None:
                continue

            # Check if this change has a newer patchset
            if rev_num < curr_rev_num:
                stale_changes.append(change_num)

                # Check if the newer patchset is still in the series
                # by fetching related changes from the stale change's current revision
                still_in_series = True
                try:
                    stale_related = self._get_related_changes(change_num)

                    # Check if the tip is still reachable from the stale change
                    tip_found = False
                    for related in stale_related:
                        if related.get('_change_number') == tip_change:
                            tip_found = True
                            break

                    if not tip_found:
                        still_in_series = False
                except Exception:
                    # If we can't check, assume still in series
                    pass

                stale_info.append(StaleChangeInfo(
                    change_number=change_num,
                    old_revision=rev_num,
                    current_revision=curr_rev_num,
                    still_in_series=still_in_series,
                    subject=subject,
                ))

                if not still_in_series:
                    # The stale change's current patchset is not connected to the tip
                    # This means it was pulled out of the series
                    return stale_changes, stale_info, (
                        f"Change {change_num} has been updated (patchset {rev_num} -> "
                        f"{curr_rev_num}) and is no longer part of this series. "
                        f"The series structure is inconsistent. Please check the "
                        f"change on Gerrit and resolve manually."
                    ), False

        if stale_changes:
            # All stale changes are still in the series, but have newer patchsets
            # These can be reintegrated
            change_list = ", ".join(str(c) for c in stale_changes)
            return stale_changes, stale_info, (
                f"The following changes have newer patchsets: {change_list}. "
                f"The series can be reintegrated automatically."
            ), True

        return [], [], None, False

    def _make_patch_info(self, change_number: int, detail: dict[str, Any]) -> Optional[PatchInfo]:
        """Create PatchInfo from change detail."""
        if not detail:
            return None

        current_rev = detail.get('current_revision', '')
        revisions = detail.get('revisions', {})
        rev_info = revisions.get(current_rev, {})
        commit_info = rev_info.get('commit', {})
        parents = commit_info.get('parents', [])

        return PatchInfo(
            change_number=change_number,
            subject=detail.get('subject', ''),
            commit=current_rev[:12] if current_rev else '',
            parent_commit=parents[0].get('commit', '')[:12] if parents else '',
            status=detail.get('status', ''),
            url=f"{self.client.url}/{change_number}",
        )


def find_series(url: str, include_abandoned: bool = False) -> PatchSeries:
    """
    Convenience function to find all patches in a series.

    Args:
        url: Gerrit change URL
        include_abandoned: If True, include abandoned patches

    Returns:
        PatchSeries object
    """
    finder = SeriesFinder()
    return finder.find_series(url, include_abandoned)


def find_series_by_change(change_number: int, include_abandoned: bool = False) -> PatchSeries:
    """
    Convenience function to find all patches in a series by change number.

    Args:
        change_number: Gerrit change number
        include_abandoned: If True, include abandoned patches

    Returns:
        PatchSeries object
    """
    finder = SeriesFinder()
    return finder.find_series_by_change(change_number, include_abandoned)


def get_series_comments(
    url: str,
    include_resolved: bool = False,
    include_code_context: bool = True,
    context_lines: int = 3,
    exclude_ci_bots: bool = True,
    exclude_lint_bots: bool = False,
) -> SeriesComments:
    """
    Get all unresolved comments from all patches in a series.

    Args:
        url: Gerrit change URL (any patch in the series)
        include_resolved: If True, include resolved comments
        include_code_context: If True, include surrounding code for each comment
        context_lines: Number of lines of code context
        exclude_ci_bots: Whether to exclude messages from CI/build systems
            (Maloo, Jenkins, etc.). Default True.
        exclude_lint_bots: Whether to exclude messages from lint/style checkers
            (checkpatch, Janitor Bot, etc.). Default False.

    Returns:
        SeriesComments object with comments grouped by patch
    """
    finder = SeriesFinder()
    return finder.get_series_comments(
        url=url,
        include_resolved=include_resolved,
        include_code_context=include_code_context,
        context_lines=context_lines,
        exclude_ci_bots=exclude_ci_bots,
        exclude_lint_bots=exclude_lint_bots,
    )
