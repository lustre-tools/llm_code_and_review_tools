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


class TestBumpReviewCount:

    def test_skeleton_counts_from_zero(self, tmp_path):
        from lreview.memory import bump_review_count, ensure_doc
        doc = ensure_doc(tmp_path, _gerrit_change())
        assert "reviews: 0" in doc.read_text()
        assert bump_review_count(doc) == 1
        assert bump_review_count(doc) == 2
        text = doc.read_text()
        assert "reviews: 2" in text
        assert "reviews: 0" not in text

    def test_pre_counter_doc_seeded_from_history(self, tmp_path):
        from lreview.memory import bump_review_count
        doc = tmp_path / "64616-old.md"
        doc.write_text(
            "---\nnumber: 64616\nsubject: s\n"
            "last-reviewed: ps26 aaaa 2026-09-03\n---\n\n"
            "# notes\n\n## History\n"
            "- 2026-09-03 ps26 aaa: 3 findings\n"
            "- 2026-09-04 ps27 bbb: 0 findings\n")
        assert bump_review_count(doc) == 3
        text = doc.read_text()
        # inserted into the frontmatter, above last-reviewed
        assert text.index("reviews: 3") < text.index("last-reviewed:")

    def test_seeding_does_not_double_count_current_run(self, tmp_path):
        """The bump runs after the agent's rewrite; when the doc was
        updated this run its History already contains the current
        run's bullet, so the seed must not add one on top (the
        68361 reviews:4-after-3-runs bug)."""
        from lreview.memory import bump_review_count
        doc = tmp_path / "68361-x.md"
        doc.write_text(
            "---\nnumber: 68361\nsubject: s\n"
            "last-reviewed: ps9 bd6c 2026-09-10\n---\n\n## History\n"
            "- 2026-09-10 ps9 aaa: 4 findings\n"
            "- 2026-09-10 ps9 aaa (2nd run): 5 findings\n"
            "- 2026-09-10 ps9 aaa (3rd run): 5 findings\n")
        assert bump_review_count(doc, doc_includes_this_run=True) == 3
        # and once the line exists, later bumps are plain increments
        assert bump_review_count(doc, doc_includes_this_run=True) == 4

    def test_body_reviews_text_not_confused_with_counter(self, tmp_path):
        from lreview.memory import bump_review_count
        doc = tmp_path / "d.md"
        doc.write_text("---\nreviews: 5\n---\n\n"
                       "body line\nreviews: 99\n")
        assert bump_review_count(doc) == 6
        assert "reviews: 99" in doc.read_text()  # body untouched


class TestMemoryProtocolContract:
    """Guard the load-bearing pieces of the bundled protocol text —
    a future edit must not silently drop them."""

    def test_protocol_mandates_checkpointing(self):
        from lreview.memory import MEMORY_PROMPT_PATH
        text = MEMORY_PROMPT_PATH.read_text()
        # mid-run checkpoints with an explicit incomplete-run marker,
        # so a run killed by a rate limit / timeout keeps its notes
        assert "INCOMPLETE RUN" in text
        assert "Checkpoint during the analysis" in text
        # only a completed run may claim last-reviewed
        assert "UNCHANGED" in text
        # limited (light/partial) passes preserve unexamined content
        assert "must not shrink the document" in text
        # the iteration counter belongs to lreview, not the agent
        assert "never edit or remove it" in text
