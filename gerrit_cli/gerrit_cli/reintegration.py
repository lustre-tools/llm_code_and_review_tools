"""Reintegration logic for handling stale patches in a series.

When patches in a series have newer patchsets, we need to "reintegrate" by:
1. Checking out the newer version of each stale patch
2. Cherry-picking all descendant patches onto it
3. Repeating for each stale patch in base-to-tip order
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import git_utils


@dataclass
class StaleChangeInfo:
    """Information about a stale change that needs reintegration."""
    change_number: int
    old_revision: int
    current_revision: int
    subject: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StaleChangeInfo":
        return cls(**data)


@dataclass
class ReintegrationState:
    """State for an ongoing reintegration operation.

    This is stored as part of the RebaseSession but encapsulates
    all reintegration-specific state.
    """
    active: bool = False
    stale_changes: list[dict[str, Any]] = field(default_factory=list)  # List of StaleChangeInfo dicts
    current_stale_idx: int = 0  # Which stale change we're processing
    pending_descendants: list[int] = field(default_factory=list)  # Descendants waiting to be cherry-picked

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReintegrationState":
        return cls(**data)

    @property
    def current_stale(self) -> Optional[dict[str, Any]]:
        """Get the current stale change being processed."""
        if self.current_stale_idx < len(self.stale_changes):
            return self.stale_changes[self.current_stale_idx]
        return None

    @property
    def is_complete(self) -> bool:
        """Check if reintegration is complete."""
        return self.current_stale_idx >= len(self.stale_changes)


class ReintegrationManager:
    """Manages the reintegration of stale patches.

    This class handles the complex state machine for reintegrating
    stale patches into a series. It's used by RebaseManager.
    """

    def __init__(self, git_runner: git_utils.GitRunner | None = None):
        self._git = git_runner or git_utils.GitRunner()

    def create_state(
        self,
        stale_info: list["StaleChangeInfo"],
        series_patches: list[dict],
    ) -> tuple[ReintegrationState, list[int]]:
        """Create initial reintegration state.

        Args:
            stale_info: List of stale change info objects
            series_patches: All patches in the series (base-to-tip order)

        Returns:
            Tuple of (state, descendants_to_cherry_pick)
        """
        stale_dicts = [s.to_dict() if hasattr(s, 'to_dict') else {
            'change_number': s.change_number,
            'old_revision': s.old_revision,
            'current_revision': s.current_revision,
            'subject': s.subject,
        } for s in stale_info]

        # Find descendants of first stale change
        first_stale = stale_info[0]
        descendants = self._find_descendants(
            first_stale.change_number, series_patches
        )

        state = ReintegrationState(
            active=True,
            stale_changes=stale_dicts,
            current_stale_idx=0,
            pending_descendants=descendants,
        )

        return state, descendants

    def _find_descendants(
        self,
        stale_change: int,
        series_patches: list[dict],
    ) -> list[int]:
        """Find all descendants of a stale change in the series.

        In a base-to-tip ordered list, descendants are all patches
        after the stale one.
        """
        stale_idx = None
        for idx, p in enumerate(series_patches):
            cn = p.get('change_number') or getattr(p, 'change_number', None)
            if cn == stale_change:
                stale_idx = idx
                break

        if stale_idx is None:
            return []

        # Descendants are indices > stale_idx
        descendants: list[int] = []
        for i in range(stale_idx + 1, len(series_patches)):
            p = series_patches[i]
            cn = p.get('change_number') if isinstance(p, dict) else getattr(p, 'change_number', None)
            if cn is not None:
                descendants.append(cn)

        return descendants

    def get_next_descendant(self, state: ReintegrationState) -> Optional[int]:
        """Get the next descendant to cherry-pick."""
        if state.pending_descendants:
            return state.pending_descendants[0]
        return None

    def mark_descendant_done(
        self,
        state: ReintegrationState,
        change_number: int,
        rebased_changes: list[int],
    ) -> None:
        """Mark a descendant as successfully cherry-picked."""
        if change_number in state.pending_descendants:
            state.pending_descendants.remove(change_number)
        if change_number not in rebased_changes:
            rebased_changes.append(change_number)

    def mark_descendant_skipped(
        self,
        state: ReintegrationState,
        change_number: int,
        skipped_changes: list[int],
    ) -> None:
        """Mark a descendant as skipped."""
        if change_number in state.pending_descendants:
            state.pending_descendants.remove(change_number)
        if change_number not in skipped_changes:
            skipped_changes.append(change_number)

    def advance_to_next_stale(
        self,
        state: ReintegrationState,
        series_patches: list[dict],
    ) -> bool:
        """Advance to the next stale change.

        Returns:
            True if there's another stale change to process, False if complete
        """
        state.current_stale_idx += 1

        if state.is_complete:
            state.active = False
            return False

        # Find descendants of the new current stale change
        current = state.current_stale
        if current:
            state.pending_descendants = self._find_descendants(
                current['change_number'], series_patches
            )

        return True

    def format_conflict_message(
        self,
        change_number: int,
        subject: str,
    ) -> str:
        """Format a message for a cherry-pick conflict."""
        return f"""
{'='*70}
CHERRY-PICK CONFLICT during reintegration
{'='*70}

Change {change_number}: {subject}

Please resolve the conflicts, then:
  git add <resolved files>
  gerrit continue-reintegration

Or to skip this change:
  gerrit skip-reintegration
{'='*70}
"""

    def format_start_message(
        self,
        stale_change: int,
        old_rev: int,
        new_rev: int,
        subject: str,
        descendants: list[int],
        series_patches: list[dict],
    ) -> str:
        """Format a message for starting reintegration."""
        lines = [
            "",
            "=" * 70,
            "SERIES REINTEGRATION REQUIRED",
            "=" * 70,
            "",
            f"Change {stale_change} has a newer patchset (v{old_rev} -> v{new_rev}).",
            f"Subject: {subject}",
            "",
            f"The following {len(descendants)} change(s) will be rebased onto the newer version:",
        ]

        for cn in descendants:
            patch = next(
                (p for p in series_patches
                 if (p.get('change_number') or getattr(p, 'change_number', None)) == cn),
                None
            )
            subj = patch.get('subject', 'Unknown') if patch else 'Unknown'
            lines.append(f"  - {cn}: {subj}")

        return "\n".join(lines)

    def format_complete_message(
        self,
        rebased_count: int,
        skipped_count: int,
    ) -> str:
        """Format a message for completing reintegration."""
        lines = [
            "",
            "=" * 70,
            "REINTEGRATION COMPLETE",
            "=" * 70,
            "",
            f"Successfully rebased {rebased_count} change(s).",
        ]
        if skipped_count > 0:
            lines.append(f"Skipped {skipped_count} change(s).")
        lines.append("")
        lines.append("Continuing with normal review workflow...")
        return "\n".join(lines)
