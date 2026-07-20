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

    def test_slug_and_fetch_url(self):
        change = ResolvedChange(
            number=64086, project="fs/lustre-release", subject="s",
            sha="a" * 40, patchset=40, ref="refs/changes/86/64086/40",
            base_url="https://review.whamcloud.com/")
        assert change.slug == "64086_ps40"
        assert change.fetch_url() == (
            "https://review.whamcloud.com/fs/lustre-release")
