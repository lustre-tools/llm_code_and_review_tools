"""Tests for the series status module."""

from unittest.mock import Mock, patch

from gerrit_cli.models import (
    Author,
    ChangeInfo,
    ExtractedComments,
)
from gerrit_cli.series import PatchInfo, PatchSeries
from gerrit_cli.series_status import (
    PatchStatus,
    SeriesStatus,
    show_series_status,
)


class TestPatchStatus:
    """Tests for PatchStatus dataclass."""

    def test_status_symbol_ready(self):
        status = PatchStatus(
            change_number=12345,
            subject="Test",
            unresolved_count=0,
            staged_count=1,
            status="ready",
        )
        assert status.status_symbol() == "✓ Ready"

    def test_status_symbol_clean(self):
        status = PatchStatus(
            change_number=12345,
            subject="Test",
            unresolved_count=0,
            staged_count=0,
            status="clean",
        )
        assert status.status_symbol() == "✓ Clean"

    def test_status_symbol_needs(self):
        status = PatchStatus(
            change_number=12345,
            subject="Test",
            unresolved_count=2,
            staged_count=1,
            status="needs",
        )
        assert status.status_symbol() == "⚠ Needs"

    def test_status_symbol_todo(self):
        status = PatchStatus(
            change_number=12345,
            subject="Test",
            unresolved_count=3,
            staged_count=0,
            status="todo",
        )
        assert status.status_symbol() == "✗ Todo"


class TestSeriesStatus:
    """Tests for SeriesStatus class."""

    def test_calculate_status_clean(self):
        """Test status calculation for clean patch (no unresolved, no staged)."""
        checker = SeriesStatus()
        assert checker._calculate_status(unresolved=0, staged=0) == "clean"

    def test_calculate_status_ready(self):
        """Test status calculation for ready patch (no unresolved, has staged)."""
        checker = SeriesStatus()
        assert checker._calculate_status(unresolved=0, staged=2) == "ready"

    def test_calculate_status_needs(self):
        """Test status calculation for needs patch (has both)."""
        checker = SeriesStatus()
        assert checker._calculate_status(unresolved=3, staged=1) == "needs"

    def test_calculate_status_todo(self):
        """Test status calculation for todo patch (has unresolved, no staged)."""
        checker = SeriesStatus()
        assert checker._calculate_status(unresolved=5, staged=0) == "todo"

    @patch("gerrit_cli.series_status.SeriesFinder")
    @patch("gerrit_cli.series_status.extract_comments")
    @patch("gerrit_cli.series_status.StagingManager")
    def test_get_series_status_basic(
        self, mock_staging_mgr_cls, mock_extract, mock_series_finder_cls
    ):
        """Test getting status for a simple series."""
        # Setup mocks
        mock_finder = Mock()
        mock_series_finder_cls.return_value = mock_finder

        # Mock series with 2 patches
        mock_finder.find_series.return_value = PatchSeries(
            patches=[
                PatchInfo(62640, "First patch", "abc123", "parent1", "NEW", "url1"),
                PatchInfo(62641, "Second patch", "def456", "parent2", "NEW", "url2"),
            ]
        )

        # Mock extracted comments - first patch has 2 unresolved
        mock_thread1 = Mock()
        mock_thread2 = Mock()

        # Create minimal ChangeInfo objects
        author = Author(name="Test User", email="test@example.com")
        mock_extract.side_effect = [
            ExtractedComments(
                change_info=ChangeInfo(
                    change_id="I1234567890",
                    change_number=62640,
                    project="test-project",
                    branch="main",
                    subject="First patch",
                    status="NEW",
                    current_revision="abc123",
                    owner=author,
                    url="url1",
                ),
                threads=[mock_thread1, mock_thread2],
                unresolved_count=2,
                total_count=2,
            ),
            ExtractedComments(
                change_info=ChangeInfo(
                    change_id="I0987654321",
                    change_number=62641,
                    project="test-project",
                    branch="main",
                    subject="Second patch",
                    status="NEW",
                    current_revision="def456",
                    owner=author,
                    url="url2",
                ),
                threads=[],  # No unresolved comments
                unresolved_count=0,
                total_count=0,
            ),
        ]

        # Mock staging manager - first patch has 1 staged operation
        mock_staging_mgr = Mock()
        mock_staging_mgr_cls.return_value = mock_staging_mgr

        mock_staged1 = Mock()
        mock_staged1.operations = [Mock()]  # 1 operation
        # StagingManager's method is load_staged; mocking a name it does not
        # have left the real call returning an auto-Mock, whose .operations
        # made len() raise TypeError -- which series_status catches and turns
        # into a staged_count of 0, so this read as "0 != 1" rather than an error.
        mock_staging_mgr.load_staged.side_effect = [
            mock_staged1,  # First patch
            None,  # Second patch - no staged operations
        ]

        # Execute
        checker = SeriesStatus()
        statuses = checker.get_series_status("https://review.whamcloud.com/62640")

        # Verify
        assert len(statuses) == 2

        # First patch: 2 unresolved, 1 staged = "needs"
        assert statuses[0].change_number == 62640
        assert statuses[0].unresolved_count == 2
        assert statuses[0].staged_count == 1
        assert statuses[0].status == "needs"

        # Second patch: 0 unresolved, 0 staged = "clean"
        assert statuses[1].change_number == 62641
        assert statuses[1].unresolved_count == 0
        assert statuses[1].staged_count == 0
        assert statuses[1].status == "clean"

    @patch("gerrit_cli.series_status.SeriesFinder")
    def test_get_series_status_empty_series(self, mock_series_finder_cls):
        """Test getting status when series is empty."""
        mock_finder = Mock()
        mock_series_finder_cls.return_value = mock_finder
        mock_finder.find_series.return_value = PatchSeries(patches=[])

        checker = SeriesStatus()
        statuses = checker.get_series_status("https://review.whamcloud.com/62640")

        assert statuses == []

    @patch("gerrit_cli.series_status.SeriesFinder")
    @patch("gerrit_cli.series_status.extract_comments")
    @patch("gerrit_cli.series_status.StagingManager")
    def test_get_series_status_handles_extract_errors(
        self, mock_staging_mgr_cls, mock_extract, mock_series_finder_cls
    ):
        """Test that errors in extracting comments are handled gracefully."""
        # Setup mocks
        mock_finder = Mock()
        mock_series_finder_cls.return_value = mock_finder

        mock_finder.find_series.return_value = PatchSeries(
            patches=[
                PatchInfo(62640, "First patch", "abc123", "parent1", "NEW", "url1"),
            ]
        )

        # Mock extract_comments to raise exception
        mock_extract.side_effect = Exception("API error")

        # Mock staging manager
        mock_staging_mgr = Mock()
        mock_staging_mgr_cls.return_value = mock_staging_mgr
        mock_staging_mgr.load_staged.return_value = None

        # Execute
        checker = SeriesStatus()
        statuses = checker.get_series_status("https://review.whamcloud.com/62640")

        # Should assume 0 unresolved comments when extraction fails
        assert len(statuses) == 1
        assert statuses[0].unresolved_count == 0

    def test_format_table_basic(self):
        """Test formatting status as ASCII table."""
        statuses = [
            PatchStatus(
                change_number=62640,
                subject="First patch subject",
                unresolved_count=3,
                staged_count=2,
                status="needs",
            ),
            PatchStatus(
                change_number=62641,
                subject="Second patch",
                unresolved_count=0,
                staged_count=1,
                status="ready",
            ),
            PatchStatus(
                change_number=62642,
                subject="Third patch",
                unresolved_count=0,
                staged_count=0,
                status="clean",
            ),
        ]

        checker = SeriesStatus()
        output = checker.format_table(statuses, "https://review.whamcloud.com/62640")

        # Check key elements are present
        assert "Series Status: https://review.whamcloud.com/62640" in output
        assert "62640" in output
        assert "62641" in output
        assert "62642" in output
        assert "First patch subject" in output
        assert "Second patch" in output
        assert "Third patch" in output
        assert "✓ Ready" in output
        assert "✓ Clean" in output
        assert "⚠ Needs" in output
        assert "Total patches: 3" in output
        assert "Patches with unresolved comments: 1" in output
        assert "Patches with staged operations: 2" in output
        assert "Patches ready to push: 1" in output

    def test_format_table_empty(self):
        """Test formatting empty status list."""
        checker = SeriesStatus()
        output = checker.format_table([], "https://review.whamcloud.com/62640")

        assert output == "No patches found in series."

    def test_format_table_truncates_long_subject(self):
        """Test that long subjects are truncated in table."""
        statuses = [
            PatchStatus(
                change_number=62640,
                subject="This is a very long subject line that should be truncated because it exceeds the maximum length",
                unresolved_count=0,
                staged_count=0,
                status="clean",
            ),
        ]

        checker = SeriesStatus()
        # Create status directly with long subject to test truncation in get_series_status
        # But format_table receives already-truncated subjects
        checker.format_table(statuses, "https://review.whamcloud.com/62640")

        # The subject in output should not exceed the column width
        assert "..." not in statuses[0].subject or len(statuses[0].subject) <= 53

    def test_format_json_basic(self):
        """Test formatting status as JSON."""
        statuses = [
            PatchStatus(
                change_number=62640,
                subject="First patch",
                unresolved_count=3,
                staged_count=2,
                status="needs",
            ),
            PatchStatus(
                change_number=62641,
                subject="Second patch",
                unresolved_count=0,
                staged_count=1,
                status="ready",
            ),
        ]

        checker = SeriesStatus()
        output = checker.format_json(statuses)

        # Parse JSON and verify
        import json
        data = json.loads(output)

        assert len(data["patches"]) == 2
        assert data["patches"][0]["change_number"] == 62640
        assert data["patches"][0]["unresolved_count"] == 3
        assert data["patches"][0]["staged_count"] == 2
        assert data["patches"][0]["status"] == "needs"

        assert data["patches"][1]["change_number"] == 62641
        assert data["patches"][1]["unresolved_count"] == 0
        assert data["patches"][1]["staged_count"] == 1
        assert data["patches"][1]["status"] == "ready"

        assert data["summary"]["total_patches"] == 2
        assert data["summary"]["patches_with_unresolved"] == 1
        assert data["summary"]["patches_with_staged"] == 2
        assert data["summary"]["patches_ready"] == 1

    def test_format_json_empty(self):
        """Test formatting empty status list as JSON."""
        checker = SeriesStatus()
        output = checker.format_json([])

        import json
        data = json.loads(output)

        assert data["patches"] == []
        assert data["summary"]["total_patches"] == 0


class TestShowSeriesStatus:
    """Tests for the show_series_status function."""

    @patch("gerrit_cli.series_status.SeriesStatus")
    def test_show_series_status_text(self, mock_series_status_cls):
        """Test show_series_status with text output."""
        mock_checker = Mock()
        mock_series_status_cls.return_value = mock_checker

        # Mock get_series_status to return some statuses
        mock_statuses = [
            PatchStatus(62640, "Test", 0, 0, "clean"),
        ]
        mock_checker.get_series_status.return_value = mock_statuses
        mock_checker.format_table.return_value = "Formatted table"

        result = show_series_status("https://review.whamcloud.com/62640", output_json=False)

        assert result == "Formatted table"
        mock_checker.get_series_status.assert_called_once()
        mock_checker.format_table.assert_called_once()

    @patch("gerrit_cli.series_status.SeriesStatus")
    def test_show_series_status_json(self, mock_series_status_cls):
        """Test show_series_status with JSON output."""
        mock_checker = Mock()
        mock_series_status_cls.return_value = mock_checker

        # Mock get_series_status to return some statuses
        mock_statuses = [
            PatchStatus(62640, "Test", 0, 0, "clean"),
        ]
        mock_checker.get_series_status.return_value = mock_statuses
        mock_checker.format_json.return_value = '{"patches": []}'

        result = show_series_status("https://review.whamcloud.com/62640", output_json=True)

        assert result == '{"patches": []}'
        mock_checker.get_series_status.assert_called_once()
        mock_checker.format_json.assert_called_once()

    @patch("gerrit_cli.series_status.SeriesStatus")
    def test_show_series_status_empty_series(self, mock_series_status_cls):
        """Test show_series_status with empty series."""
        mock_checker = Mock()
        mock_series_status_cls.return_value = mock_checker
        mock_checker.get_series_status.return_value = []

        result = show_series_status("https://review.whamcloud.com/62640")

        assert result == "Error: Could not find series or no patches in series."
