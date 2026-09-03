"""Tests for the per-change review memory (lreview-db)."""

from pathlib import Path

import pytest

from lreview.gerrit import LocalChange, ResolvedChange
from lreview.memory import (
    MEMORY_PROMPT_PATH,
    clear_doc,
    default_db_dir,
    ensure_doc,
    find_doc,
)

CHANGE_ID = "I5cce4e0ea51c68b0c6fda1d83b694af19cad57bd"


def _gerrit_change(number=63809, change_id=CHANGE_ID):
    return ResolvedChange(
        number=number, project="fs/lustre-release",
        subject="LU-19852 lod: raidset aware stripe allocator",
        sha="a" * 40, patchset=54, ref="r",
        base_url="https://review.whamcloud.com", change_id=change_id)


def _local_change(change_id=CHANGE_ID):
    return LocalChange(
        ref_name="mybranch", sha="b" * 40,
        subject="LU-19852 lod: raidset aware stripe allocator",
        change_id=change_id)


class TestDbDir:

    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LREVIEW_DB", str(tmp_path / "mydb"))
        assert default_db_dir(tmp_path / "repo") == tmp_path / "mydb"

    def test_repo_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LREVIEW_DB", raising=False)
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        assert default_db_dir(tmp_path / "repo") == (
            tmp_path / "repo" / "lreview-db")


class TestEnsureAndFind:

    def test_creates_skeleton_with_frontmatter(self, tmp_path):
        doc = ensure_doc(tmp_path / "db", _gerrit_change())
        assert doc.name == (
            "63809-LU-19852_lod_raidset_aware_stripe_allocator.md")
        text = doc.read_text()
        assert f"change-id: {CHANGE_ID}" in text
        assert "number: 63809" in text
        assert "No notes yet" in text

    def test_find_existing_by_number(self, tmp_path):
        db = tmp_path / "db"
        first = ensure_doc(db, _gerrit_change(change_id=None))
        again = ensure_doc(db, _gerrit_change(change_id=None))
        assert first == again

    def test_local_and_gerrit_share_by_change_id(self, tmp_path):
        """A doc created by a local pre-push review is found by the
        later Gerrit review of the same patch (and vice versa)."""
        db = tmp_path / "db"
        local_doc = ensure_doc(db, _local_change())
        assert local_doc.name.startswith("I5cce4e0e-")
        gerrit_doc = find_doc(db, _gerrit_change())
        assert gerrit_doc == local_doc

    def test_local_without_change_id(self, tmp_path):
        db = tmp_path / "db"
        change = _local_change(change_id=None)
        doc = ensure_doc(db, change)
        assert doc.name.startswith("local-")
        assert find_doc(db, change) == doc

    def test_find_none(self, tmp_path):
        assert find_doc(tmp_path / "nodb", _gerrit_change()) is None


class TestClear:

    def test_clear_removes(self, tmp_path):
        db = tmp_path / "db"
        doc = ensure_doc(db, _gerrit_change())
        assert clear_doc(db, _gerrit_change()) == doc
        assert not doc.exists()
        assert clear_doc(db, _gerrit_change()) is None


class TestPromptFile:

    def test_memory_prompt_is_packaged(self):
        assert MEMORY_PROMPT_PATH.is_file()
        text = MEMORY_PROMPT_PATH.read_text()
        assert "False positives eliminated" in text
        assert "complete replacement" in text