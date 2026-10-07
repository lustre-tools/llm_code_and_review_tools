"""Tests for Gerrit change resolution."""

from unittest.mock import MagicMock

from lreview.gerrit import ResolvedChange, change_ref, resolve_change


def _detail(number=64086, ps=40, sha="a" * 40, ref=None):
    revision = {"_number": ps}
    if ref:
        revision["ref"] = ref
    return {
        "project": "fs/lustre-release",
        "subject": "LU-12668 lov: handle ESHUTDOWN for LSEEK on EC files",
        "current_revision": sha,
        "revisions": {sha: revision},
    }


def test_suite_never_sees_the_developers_gerrit_config():
    """conftest.py pins the Gerrit configuration gerrit_cli reads at
    import, so results do not depend on who runs the suite."""
    import os
    from gerrit_cli import client
    assert client.DEFAULT_GERRIT_URL == "https://gerrit.invalid"
    assert "GERRIT_USER" not in os.environ
    assert "GERRIT_PASS" not in os.environ


class TestChangeRef:

    def test_two_digit_suffix(self):
        assert change_ref(64086, 40) == "refs/changes/86/64086/40"

    def test_single_digit_padded(self):
        assert change_ref(64007, 2) == "refs/changes/07/64007/2"


class TestResolveChange:

    def test_resolves_fields(self):
        client = MagicMock()
        client.get_change_detail.return_value = _detail(
            ref="refs/changes/86/64086/40")
        change = resolve_change(
            "https://review.whamcloud.com/c/fs/lustre-release/+/64086",
            client=client)

        assert change.number == 64086
        assert change.project == "fs/lustre-release"
        assert change.patchset == 40
        assert change.sha == "a" * 40
        assert change.ref == "refs/changes/86/64086/40"
        assert change.base_url == "https://review.whamcloud.com"

    def test_ref_constructed_when_missing(self):
        client = MagicMock()
        client.get_change_detail.return_value = _detail()
        change = resolve_change("64086", client=client)
        assert change.ref == "refs/changes/86/64086/40"

    def test_url_pinned_patchset_honored(self):
        client = MagicMock()
        detail = _detail(ps=40, sha="a" * 40)
        detail["revisions"]["b" * 40] = {
            "_number": 38, "ref": "refs/changes/86/64086/38"}
        client.get_change_detail.return_value = detail
        change = resolve_change(
            "https://review.whamcloud.com/c/fs/lustre-release/+/64086/38",
            client=client)
        assert change.patchset == 38
        assert change.sha == "b" * 40
        assert change.ref == "refs/changes/86/64086/38"

    def test_url_pinned_unknown_patchset_raises(self):
        import pytest
        client = MagicMock()
        client.get_change_detail.return_value = _detail(ps=40)
        with pytest.raises(ValueError, match="no patchset 99"):
            resolve_change(
                "https://review.whamcloud.com/c/fs/lustre-release/+/64086/99",
                client=client)

    def test_local_change_slug_sanitized(self):
        from lreview.gerrit import LocalChange
        change = LocalChange(
            ref_name="gerrit/claude/LU-1234_foo bar", sha="abcdef1" + "0" * 33,
            subject="s")
        assert change.slug == "gerrit_claude_LU-1234_foo_bar_abcdef1"
        assert change.number is None
        assert change.fetch_url() == ""

    def test_slug_and_fetch_url(self):
        change = ResolvedChange(
            number=64086, project="fs/lustre-release", subject="s",
            sha="a" * 40, patchset=40, ref="refs/changes/86/64086/40",
            base_url="https://review.whamcloud.com/")
        assert change.slug == "64086_ps40"
        assert change.fetch_url() == (
            "https://review.whamcloud.com/fs/lustre-release")


class TestSeriesChildren:
    """Children are what Gerrit's /related shows above the change:
    descendants first (newest on top), the change, then ancestors."""

    def _client(self, related):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.rest.get.return_value = {"changes": [
            {"_change_number": n, "status": status}
            for n, status in related]}
        return client

    def _change(self, number=100):
        from lreview.gerrit import ResolvedChange, change_ref
        return ResolvedChange(
            number=number, project="ex/lustre-release", subject="s",
            sha="a" * 40, patchset=3, ref=change_ref(number, 3),
            base_url="https://gerrit.invalid")

    def test_children_base_to_tip_ancestors_excluded(self):
        from lreview.gerrit import series_children
        client = self._client([(103, "NEW"), (102, "NEW"), (101, "NEW"),
                               (100, "NEW"), (99, "NEW"), (98, "NEW")])
        assert series_children(self._change(), client) == [101, 102, 103]
        # asked for the reviewed revision's relation chain
        client.rest.get.assert_called_once_with(
            f"/changes/100/revisions/{'a' * 40}/related")

    def test_only_open_children(self):
        from lreview.gerrit import series_children
        client = self._client([(103, "NEW"), (102, "ABANDONED"),
                               (101, "MERGED"), (100, "NEW")])
        assert series_children(self._change(), client) == [103]

    def test_tip_and_standalone_have_none(self):
        from lreview.gerrit import series_children
        assert series_children(
            self._change(), self._client([(100, "NEW"), (99, "NEW")])) == []
        assert series_children(self._change(), self._client([])) == []
