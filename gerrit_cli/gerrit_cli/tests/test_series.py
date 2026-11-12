"""Tests for the series finder module."""

from unittest.mock import Mock, patch

from gerrit_cli.models import Author, Comment, CommentThread
from gerrit_cli.series import (
    PatchComments,
    PatchInfo,
    PatchSeries,
    SeriesComments,
    SeriesFinder,
    find_series,
    find_series_by_change,
    get_series_comments,
)


class TestPatchInfo:
    """Tests for PatchInfo dataclass."""

    def test_to_dict(self):
        patch = PatchInfo(
            change_number=12345,
            subject="Test subject",
            commit="abc123def456",
            parent_commit="789xyz012345",
            status="NEW",
            url="https://example.com/12345",
        )
        result = patch.to_dict()

        assert result["change_number"] == 12345
        assert result["subject"] == "Test subject"
        assert result["commit"] == "abc123def456"
        assert result["parent_commit"] == "789xyz012345"
        assert result["status"] == "NEW"
        assert result["url"] == "https://example.com/12345"

    def test_from_dict_full(self):
        """Test from_dict with all fields provided."""
        data = {
            "change_number": 54321,
            "subject": "Another subject",
            "commit": "def789abc123",
            "parent_commit": "123abc456def",
            "status": "MERGED",
            "url": "https://example.com/54321",
        }
        patch = PatchInfo.from_dict(data)

        assert patch.change_number == 54321
        assert patch.subject == "Another subject"
        assert patch.commit == "def789abc123"
        assert patch.parent_commit == "123abc456def"
        assert patch.status == "MERGED"
        assert patch.url == "https://example.com/54321"

    def test_from_dict_minimal_with_base_url(self):
        """Test from_dict with minimal data and base_url."""
        data = {
            "change_number": 99999,
            "subject": "Minimal patch",
        }
        patch = PatchInfo.from_dict(data, base_url="https://review.example.com")

        assert patch.change_number == 99999
        assert patch.subject == "Minimal patch"
        assert patch.commit == ""
        assert patch.parent_commit == ""
        assert patch.status == ""
        assert patch.url == "https://review.example.com/99999"

    def test_from_dict_roundtrip(self):
        """Test to_dict then from_dict produces equivalent object."""
        original = PatchInfo(
            change_number=12345,
            subject="Roundtrip test",
            commit="abc123",
            parent_commit="xyz789",
            status="NEW",
            url="https://example.com/12345",
        )
        data = original.to_dict()
        restored = PatchInfo.from_dict(data)

        assert restored.change_number == original.change_number
        assert restored.subject == original.subject
        assert restored.commit == original.commit
        assert restored.parent_commit == original.parent_commit
        assert restored.status == original.status
        assert restored.url == original.url


class TestPatchSeries:
    """Tests for PatchSeries dataclass."""

    def test_len(self):
        series = PatchSeries(
            patches=[
                PatchInfo(1, "s1", "c1", "p1", "NEW", "u1"),
                PatchInfo(2, "s2", "c2", "p2", "NEW", "u2"),
                PatchInfo(3, "s3", "c3", "p3", "NEW", "u3"),
            ]
        )
        assert len(series) == 3

    def test_format_summary(self):
        series = PatchSeries(
            patches=[
                PatchInfo(100, "First patch", "c1", "p1", "NEW", "u1"),
                PatchInfo(101, "Second patch", "c2", "p2", "NEW", "u2"),
            ],
            target_change=101,
            target_position=2,
        )
        summary = series.format_summary()

        assert "2 patches" in summary
        assert "100" in summary
        assert "101" in summary
        assert "First patch" in summary
        assert "Second patch" in summary
        assert "queried" in summary

    def test_to_dict(self):
        series = PatchSeries(
            patches=[
                PatchInfo(100, "s1", "c1", "p1", "NEW", "u1"),
            ],
            target_change=100,
            target_position=1,
            tip_change=100,
            base_change=100,
        )
        result = series.to_dict()

        assert result["total_patches"] == 1
        assert result["target_change"] == 100
        assert result["target_position"] == 1
        assert result["tip_change"] == 100
        assert result["base_change"] == 100
        assert len(result["patches"]) == 1


class TestSeriesFinder:
    """Tests for SeriesFinder class."""

    def test_build_commit_map(self):
        finder = SeriesFinder(client=Mock())

        related = [
            {
                "status": "NEW",
                "_change_number": 100,
                "commit": {
                    "commit": "abc123",
                    "subject": "Test 1",
                    "parents": [{"commit": "parent1"}],
                },
            },
            {
                "status": "ABANDONED",
                "_change_number": 101,
                "commit": {
                    "commit": "def456",
                    "subject": "Test 2",
                    "parents": [{"commit": "parent2"}],
                },
            },
            {
                "status": "NEW",
                "_change_number": 102,
                "commit": {
                    "commit": "ghi789",
                    "subject": "Test 3",
                    "parents": [{"commit": "abc123"}],
                },
            },
        ]

        # Only NEW
        result = finder._build_commit_map(related, ["NEW"])
        assert len(result) == 2
        assert "abc123" in result
        assert "ghi789" in result
        assert "def456" not in result

        # NEW and ABANDONED
        result = finder._build_commit_map(related, ["NEW", "ABANDONED"])
        assert len(result) == 3

    def test_find_commit_for_change(self):
        finder = SeriesFinder(client=Mock())

        changes_map = {
            "commit1": {"change": 100, "parent": "base", "commit": "commit1"},
            "commit2": {"change": 101, "parent": "commit1", "commit": "commit2"},
        }

        assert finder._find_commit_for_change(changes_map, 100) == "commit1"
        assert finder._find_commit_for_change(changes_map, 101) == "commit2"
        assert finder._find_commit_for_change(changes_map, 999) is None

    def test_walk_chain_backwards(self):
        finder = SeriesFinder(client=Mock())

        changes_map = {
            "commit1": {"change": 100, "parent": "base", "commit": "commit1", "subject": "s1", "status": "NEW"},
            "commit2": {"change": 101, "parent": "commit1", "commit": "commit2", "subject": "s2", "status": "NEW"},
            "commit3": {"change": 102, "parent": "commit2", "commit": "commit3", "subject": "s3", "status": "NEW"},
        }

        chain = finder._walk_chain_backwards(changes_map, "commit3")

        assert len(chain) == 3
        assert chain[0]["change"] == 100  # Base first
        assert chain[1]["change"] == 101
        assert chain[2]["change"] == 102  # Tip last

    @patch.object(SeriesFinder, '_get_related_changes')
    def test_find_series_empty_related(self, mock_get_related):
        mock_client = Mock()
        mock_client.parse_gerrit_url.return_value = ("http://test", 100)
        mock_client.url = "http://test"
        mock_client.get_change_detail.return_value = {
            "subject": "Standalone patch",
            "status": "NEW",
            "current_revision": "abc123",
            "revisions": {
                "abc123": {
                    "commit": {
                        "parents": [{"commit": "parent"}],
                    }
                }
            },
        }

        finder = SeriesFinder(client=mock_client)
        mock_get_related.return_value = []

        series = finder.find_series("http://test/100")

        assert len(series) == 1
        assert series.patches[0].change_number == 100

    @patch.object(SeriesFinder, '_get_related_changes')
    def test_find_series_with_chain(self, mock_get_related):
        """Walking backward from target 101 yields only its dependencies."""
        mock_client = Mock()
        mock_client.parse_gerrit_url.return_value = ("http://test", 101)
        mock_client.url = "http://test"

        finder = SeriesFinder(client=mock_client)

        # Related returns all 3 commits (including child 102 above target)
        mock_get_related.return_value = [
            {
                "status": "NEW",
                "_change_number": 102,
                "commit": {"commit": "c3", "subject": "Third", "parents": [{"commit": "c2"}]},
            },
            {
                "status": "NEW",
                "_change_number": 101,
                "commit": {"commit": "c2", "subject": "Second", "parents": [{"commit": "c1"}]},
            },
            {
                "status": "NEW",
                "_change_number": 100,
                "commit": {"commit": "c1", "subject": "First", "parents": [{"commit": "base"}]},
            },
        ]

        series = finder.find_series("http://test/101")

        # Only 100 (dep) and 101 (target) — 102 is a child, excluded
        assert len(series) == 2
        assert series.patches[0].change_number == 100  # Base
        assert series.patches[1].change_number == 101  # Target (= tip)
        assert series.target_change == 101
        assert series.target_position == 2

    @patch.object(SeriesFinder, '_get_related_changes')
    def test_find_series_from_tip_includes_all(self, mock_get_related):
        """Querying from the actual tip returns the full chain."""
        mock_client = Mock()
        mock_client.parse_gerrit_url.return_value = ("http://test", 102)
        mock_client.url = "http://test"

        finder = SeriesFinder(client=mock_client)

        mock_get_related.return_value = [
            {
                "status": "NEW",
                "_change_number": 102,
                "commit": {"commit": "c3", "subject": "Third", "parents": [{"commit": "c2"}]},
            },
            {
                "status": "NEW",
                "_change_number": 101,
                "commit": {"commit": "c2", "subject": "Second", "parents": [{"commit": "c1"}]},
            },
            {
                "status": "NEW",
                "_change_number": 100,
                "commit": {"commit": "c1", "subject": "First", "parents": [{"commit": "base"}]},
            },
        ]

        series = finder.find_series("http://test/102")

        assert len(series) == 3
        assert series.patches[0].change_number == 100
        assert series.patches[1].change_number == 101
        assert series.patches[2].change_number == 102
        assert series.target_change == 102
        assert series.target_position == 3

    @patch.object(SeriesFinder, '_get_related_changes')
    def test_detect_stale_change_still_in_series(self, mock_get_related):
        """Test detection of a patch with newer patchset still in series."""
        mock_client = Mock()
        mock_client.parse_gerrit_url.return_value = ("http://test", 101)
        mock_client.url = "http://test"

        finder = SeriesFinder(client=mock_client)

        # Chain where change 101 has newer patchset (rev 1 vs current 2)
        # but still in the series (tip 102 is reachable)
        mock_get_related.return_value = [
            {
                "status": "NEW",
                "_change_number": 102,
                "_revision_number": 1,
                "_current_revision_number": 1,
                "commit": {"commit": "c3", "subject": "Third", "parents": [{"commit": "c2"}]},
            },
            {
                "status": "NEW",
                "_change_number": 101,
                "_revision_number": 1,
                "_current_revision_number": 2,  # Stale!
                "commit": {"commit": "c2", "subject": "Second", "parents": [{"commit": "c1"}]},
            },
            {
                "status": "NEW",
                "_change_number": 100,
                "_revision_number": 1,
                "_current_revision_number": 1,
                "commit": {"commit": "c1", "subject": "First", "parents": [{"commit": "base"}]},
            },
        ]

        series = finder.find_series("http://test/101")

        # Backward walk from 101 yields 100 + 101 (not 102)
        assert len(series) == 2
        assert 101 in series.stale_changes
        assert series.error is not None
        assert "newer patchsets" in series.error
        assert "reintegrated automatically" in series.error
        assert series.needs_reintegration is True
        # Check stale_info is populated
        assert len(series.stale_info) == 1
        assert series.stale_info[0].change_number == 101
        assert series.stale_info[0].old_revision == 1
        assert series.stale_info[0].current_revision == 2
        assert series.stale_info[0].still_in_series is True

    @patch.object(SeriesFinder, '_get_related_changes')
    def test_detect_stale_change_no_longer_in_series(self, mock_get_related):
        """Test detection of a patch pulled out of the series."""
        mock_client = Mock()
        # Query from the tip (102) to get the full chain
        mock_client.parse_gerrit_url.return_value = ("http://test", 102)
        mock_client.url = "http://test"

        finder = SeriesFinder(client=mock_client)

        # First call: chain from tip perspective (stale view)
        # Second call: stale change's current revision (tip not reachable)
        call_count = [0]

        def mock_related(change_num):
            call_count[0] += 1
            if change_num == 101:
                # Stale change's current revision only sees itself (pulled out)
                return [
                    {
                        "status": "NEW",
                        "_change_number": 101,
                        "_revision_number": 2,
                        "_current_revision_number": 2,
                        "commit": {"commit": "new_c2", "subject": "Second rebased",
                                   "parents": [{"commit": "other_base"}]},
                    },
                ]
            else:
                # Original chain from tip (102)
                return [
                    {
                        "status": "NEW",
                        "_change_number": 102,
                        "_revision_number": 1,
                        "_current_revision_number": 1,
                        "commit": {"commit": "c3", "subject": "Third",
                                   "parents": [{"commit": "c2"}]},
                    },
                    {
                        "status": "NEW",
                        "_change_number": 101,
                        "_revision_number": 1,
                        "_current_revision_number": 2,  # Stale!
                        "commit": {"commit": "c2", "subject": "Second",
                                   "parents": [{"commit": "c1"}]},
                    },
                    {
                        "status": "NEW",
                        "_change_number": 100,
                        "_revision_number": 1,
                        "_current_revision_number": 1,
                        "commit": {"commit": "c1", "subject": "First",
                                   "parents": [{"commit": "base"}]},
                    },
                ]

        mock_get_related.side_effect = mock_related

        series = finder.find_series("http://test/102")

        assert 101 in series.stale_changes
        assert series.error is not None
        assert "no longer part of this series" in series.error

    @patch.object(SeriesFinder, '_get_related_changes')
    def test_no_stale_changes(self, mock_get_related):
        """Test that no error when all changes are current."""
        mock_client = Mock()
        mock_client.parse_gerrit_url.return_value = ("http://test", 101)
        mock_client.url = "http://test"

        finder = SeriesFinder(client=mock_client)

        # All changes have matching revision numbers
        mock_get_related.return_value = [
            {
                "status": "NEW",
                "_change_number": 102,
                "_revision_number": 3,
                "_current_revision_number": 3,
                "commit": {"commit": "c3", "subject": "Third", "parents": [{"commit": "c2"}]},
            },
            {
                "status": "NEW",
                "_change_number": 101,
                "_revision_number": 2,
                "_current_revision_number": 2,
                "commit": {"commit": "c2", "subject": "Second", "parents": [{"commit": "c1"}]},
            },
            {
                "status": "NEW",
                "_change_number": 100,
                "_revision_number": 1,
                "_current_revision_number": 1,
                "commit": {"commit": "c1", "subject": "First", "parents": [{"commit": "base"}]},
            },
        ]

        series = finder.find_series("http://test/101")

        # Backward walk from 101: only 100 + 101 (102 is a child, excluded)
        assert len(series) == 2
        assert series.stale_changes == []
        assert series.error is None


class TestPatchComments:
    """Tests for PatchComments dataclass."""

    def _make_comment(self, msg="Test", line=10, author_name="Tester"):
        return Comment(
            id="comment1",
            patch_set=1,
            file_path="test.c",
            line=line,
            message=msg,
            author=Author(name=author_name, email="test@example.com", username="tester"),
            unresolved=True,
            updated="2024-01-01",
        )

    def _make_thread(self, resolved=False):
        comment = self._make_comment()
        comment.unresolved = not resolved
        return CommentThread(root_comment=comment, replies=[])

    def test_unresolved_count(self):
        threads = [
            self._make_thread(resolved=False),
            self._make_thread(resolved=False),
            self._make_thread(resolved=True),
        ]
        pc = PatchComments(
            change_number=100,
            subject="Test",
            url="http://test/100",
            current_patchset=5,
            threads=threads,
        )
        assert pc.unresolved_count == 2

    def test_to_dict(self):
        pc = PatchComments(
            change_number=100,
            subject="Test patch",
            url="http://test/100",
            current_patchset=5,
            threads=[self._make_thread()],
        )
        result = pc.to_dict()

        assert result["change_number"] == 100
        assert result["subject"] == "Test patch"
        assert result["url"] == "http://test/100"
        assert result["current_patchset"] == 5
        assert result["unresolved_count"] == 1
        assert len(result["threads"]) == 1

    def test_format_summary(self):
        pc = PatchComments(
            change_number=100,
            subject="Test patch",
            url="http://test/100",
            current_patchset=5,
            threads=[self._make_thread()],
        )
        summary = pc.format_summary()

        assert "100" in summary
        assert "Test patch" in summary
        assert "http://test/100" in summary
        assert "patchset" in summary.lower()


class TestSeriesComments:
    """Tests for SeriesComments dataclass."""

    def _make_patch_comments(self, change_num, unresolved_count):
        threads = []
        for i in range(unresolved_count):
            comment = Comment(
                id=f"c{i}",
                patch_set=1,
                file_path="test.c",
                line=i + 1,
                message=f"Comment {i}",
                author=Author(name="Tester", email="test@example.com", username="tester"),
                unresolved=True,
                updated="2024-01-01",
            )
            threads.append(CommentThread(root_comment=comment, replies=[]))

        return PatchComments(
            change_number=change_num,
            subject=f"Patch {change_num}",
            url=f"http://test/{change_num}",
            current_patchset=1,
            threads=threads,
        )

    def test_total_unresolved(self):
        series = PatchSeries(patches=[])
        sc = SeriesComments(
            series=series,
            patches_with_comments=[
                self._make_patch_comments(100, 2),
                self._make_patch_comments(101, 3),
                self._make_patch_comments(102, 0),
            ],
        )
        assert sc.total_unresolved == 5

    def test_patches_with_unresolved(self):
        series = PatchSeries(patches=[])
        sc = SeriesComments(
            series=series,
            patches_with_comments=[
                self._make_patch_comments(100, 2),
                self._make_patch_comments(101, 0),
                self._make_patch_comments(102, 1),
            ],
        )
        assert sc.patches_with_unresolved == 2

    def test_to_dict(self):
        series = PatchSeries(
            patches=[PatchInfo(100, "s", "c", "p", "NEW", "u")],
            target_change=100,
        )
        sc = SeriesComments(
            series=series,
            patches_with_comments=[self._make_patch_comments(100, 1)],
        )
        result = sc.to_dict()

        assert "series" in result
        assert result["total_unresolved"] == 1
        assert result["patches_with_unresolved"] == 1
        assert len(result["patches"]) == 1

    def test_format_summary(self):
        series = PatchSeries(
            patches=[PatchInfo(100, "s", "c", "p", "NEW", "u")],
            base_change=100,
            tip_change=100,
        )
        sc = SeriesComments(
            series=series,
            patches_with_comments=[self._make_patch_comments(100, 2)],
        )
        summary = sc.format_summary()

        assert "2 unresolved" in summary
        assert "1 patches" in summary


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    @patch('gerrit_cli.series.SeriesFinder')
    def test_find_series(self, MockFinder):
        mock_finder = Mock()
        mock_finder.find_series.return_value = PatchSeries(patches=[])
        MockFinder.return_value = mock_finder

        find_series("http://test/100")

        mock_finder.find_series.assert_called_once_with("http://test/100", False)

    @patch('gerrit_cli.series.SeriesFinder')
    def test_find_series_by_change(self, MockFinder):
        mock_finder = Mock()
        mock_finder.find_series_by_change.return_value = PatchSeries(patches=[])
        MockFinder.return_value = mock_finder

        find_series_by_change(12345, include_abandoned=True)

        mock_finder.find_series_by_change.assert_called_once_with(12345, True)

    @patch('gerrit_cli.series.SeriesFinder')
    def test_get_series_comments(self, MockFinder):
        mock_finder = Mock()
        mock_finder.get_series_comments.return_value = SeriesComments(
            series=PatchSeries(patches=[]),
            patches_with_comments=[],
        )
        MockFinder.return_value = mock_finder

        get_series_comments("http://test/100", include_resolved=True)

        mock_finder.get_series_comments.assert_called_once_with(
            url="http://test/100",
            include_resolved=True,
            include_code_context=True,
            context_lines=3,
            exclude_ci_bots=True,
            exclude_lint_bots=True,
        )
