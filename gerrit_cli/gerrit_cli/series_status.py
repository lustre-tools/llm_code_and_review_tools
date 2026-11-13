"""Series status dashboard for patch series."""

import json
from dataclasses import dataclass

from .extractor import extract_comments
from .parallel import parallel_map
from .series import PatchInfo, SeriesFinder
from .staging import StagingManager


@dataclass
class PatchStatus:
    """Status information for a single patch in a series."""
    change_number: int
    subject: str
    unresolved_count: int
    staged_count: int
    status: str  # "ready", "clean", "needs", "todo"

    def status_symbol(self) -> str:
        """Get the status symbol for display."""
        symbols = {
            "ready": "✓ Ready",
            "clean": "✓ Clean",
            "needs": "⚠ Needs",
            "todo": "✗ Todo",
        }
        return symbols.get(self.status, "? Unknown")


class SeriesStatus:
    """Calculate and display status for patch series."""

    def __init__(self):
        self.series_finder = SeriesFinder()
        self.staging_manager = StagingManager()

    def get_series_status(self, url: str) -> list[PatchStatus]:
        """Get status information for all patches in a series.

        Args:
            url: URL to any patch in the series

        Returns:
            List of PatchStatus objects, one per patch
        """
        # Find all patches in series
        series = self.series_finder.find_series(url)

        if not series or not series.patches:
            return []

        client = self.series_finder.client

        def status_of(patch: PatchInfo) -> PatchStatus:
            # Count unresolved comments
            try:
                extracted = extract_comments(
                    url=patch.url,
                    include_resolved=False,
                    include_code_context=False,
                    client=client,
                )
                unresolved_count = len(extracted.threads)
            except Exception:
                # If we can't fetch comments, assume 0
                unresolved_count = 0

            # Check for staged operations
            staged_count = 0
            try:
                staged = self.staging_manager.load_staged(patch.change_number)
                if staged:
                    staged_count = len(staged.operations)
            except Exception:
                staged_count = 0

            # Determine status
            status = self._calculate_status(unresolved_count, staged_count)

            # Truncate subject if too long
            subject = patch.subject
            if len(subject) > 50:
                subject = subject[:47] + "..."

            return PatchStatus(
                change_number=patch.change_number,
                subject=subject,
                unresolved_count=unresolved_count,
                staged_count=staged_count,
                status=status,
            )

        statuses = parallel_map(status_of, series.patches)

        return statuses

    def _calculate_status(self, unresolved: int, staged: int) -> str:
        """Calculate status based on unresolved and staged counts.

        Returns:
            "ready" - Has staged operations, no new unresolved comments
            "clean" - No unresolved comments, no staged operations
            "needs" - Has both unresolved and staged (partially addressed)
            "todo" - Has unresolved comments, no staged operations
        """
        if unresolved == 0 and staged == 0:
            return "clean"
        elif unresolved == 0 and staged > 0:
            return "ready"
        elif unresolved > 0 and staged > 0:
            return "needs"
        else:  # unresolved > 0 and staged == 0
            return "todo"

    def format_table(self, statuses: list[PatchStatus], series_url: str) -> str:
        """Format status information as an ASCII table.

        Args:
            statuses: List of PatchStatus objects
            series_url: Base URL for the series

        Returns:
            Formatted table string
        """
        if not statuses:
            return "No patches found in series."

        # Build output
        lines = []
        lines.append(f"Series Status: {series_url}")
        lines.append("")

        # Table header
        lines.append("┌─────────┬──────────────────────────────────────────────────────┬────────────┬──────────┬─────────┐")
        lines.append("│ Change  │ Subject                                              │ Unresolved │ Staged   │ Status  │")
        lines.append("├─────────┼──────────────────────────────────────────────────────┼────────────┼──────────┼─────────┤")

        # Table rows
        for status in statuses:
            change = str(status.change_number).ljust(7)
            subject = status.subject.ljust(52)
            unresolved = str(status.unresolved_count).ljust(10)
            staged = str(status.staged_count).ljust(8)
            status_symbol = status.status_symbol().ljust(7)

            lines.append(f"│ {change} │ {subject} │ {unresolved} │ {staged} │ {status_symbol} │")

        lines.append("└─────────┴──────────────────────────────────────────────────────┴────────────┴──────────┴─────────┘")

        # Summary
        lines.append("")
        lines.append("Summary:")
        lines.append(f"  Total patches: {len(statuses)}")

        patches_with_unresolved = sum(1 for s in statuses if s.unresolved_count > 0)
        patches_with_staged = sum(1 for s in statuses if s.staged_count > 0)
        patches_ready = sum(1 for s in statuses if s.status == "ready")

        lines.append(f"  Patches with unresolved comments: {patches_with_unresolved}")
        lines.append(f"  Patches with staged operations: {patches_with_staged}")
        lines.append(f"  Patches ready to push: {patches_ready}")

        # Legend
        lines.append("")
        lines.append("Legend:")
        lines.append("  ✓ Ready: Has staged operations, no new unresolved comments")
        lines.append("  ✓ Clean: No unresolved comments, no staged operations")
        lines.append("  ⚠ Needs: Has both unresolved and staged (partially addressed)")
        lines.append("  ✗ Todo: Has unresolved comments, no staged operations")

        return "\n".join(lines)

    def format_json(self, statuses: list[PatchStatus]) -> str:
        """Format status information as JSON.

        Args:
            statuses: List of PatchStatus objects

        Returns:
            JSON string
        """
        data = {
            "patches": [
                {
                    "change_number": s.change_number,
                    "subject": s.subject,
                    "unresolved_count": s.unresolved_count,
                    "staged_count": s.staged_count,
                    "status": s.status,
                }
                for s in statuses
            ],
            "summary": {
                "total_patches": len(statuses),
                "patches_with_unresolved": sum(1 for s in statuses if s.unresolved_count > 0),
                "patches_with_staged": sum(1 for s in statuses if s.staged_count > 0),
                "patches_ready": sum(1 for s in statuses if s.status == "ready"),
            }
        }
        return json.dumps(data, indent=2)


def show_series_status(url: str, output_json: bool = False) -> str:
    """Show status for all patches in a series.

    Args:
        url: URL to any patch in the series
        output_json: If True, output as JSON

    Returns:
        Formatted output string
    """
    status_checker = SeriesStatus()
    statuses = status_checker.get_series_status(url)

    if not statuses:
        return "Error: Could not find series or no patches in series."

    if output_json:
        return status_checker.format_json(statuses)
    else:
        # Extract base URL from the input
        if "/c/" in url:
            base_url = url.split("/c/")[0]
        else:
            base_url = url.rsplit("/", 1)[0]

        # Use first patch's change number for the series URL
        series_url = f"{base_url}/{statuses[0].change_number}"

        return status_checker.format_table(statuses, series_url)
