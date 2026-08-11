"""Tests for review-prompts location and availability checks."""

from pathlib import Path
from unittest.mock import patch

import pytest

from lreview.prompts import (
    check_prompts,
    find_prompts_dir,
    setup_instructions,
)


@pytest.fixture
def clone(tmp_path):
    """A fake review-prompts clone with the needed kernel prompts."""
    d = tmp_path / "review-prompts" / "kernel"
    d.mkdir(parents=True)
    (d / "review-core.md").write_text("# review core\n")
    (d / "gerrit-review.md").write_text("# gerrit output\n")
    return tmp_path / "review-prompts"


@pytest.fixture
def no_legacy(monkeypatch, tmp_path):
    """Point HOME somewhere empty so no legacy install is found."""
    monkeypatch.setenv("HOME", str(tmp_path / "emptyhome"))
    (tmp_path / "emptyhome").mkdir()
    # pathlib caches nothing, but Path.home() reads HOME at call time
    return tmp_path / "emptyhome"


class TestFindPromptsDir:

    def test_explicit_repo_root(self, clone):
        found, source = find_prompts_dir(explicit=clone)
        assert found == clone / "kernel"
        assert str(clone) in source

    def test_explicit_kernel_dir(self, clone):
        found, _ = find_prompts_dir(explicit=clone / "kernel")
        assert found == clone / "kernel"

    def test_explicit_wrong_dir(self, tmp_path):
        found, source = find_prompts_dir(explicit=tmp_path)
        assert found is None
        assert source is None

    def test_legacy_command_file(self, clone, no_legacy):
        commands = no_legacy / ".claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "kreview.md").write_text(
            f"Read the prompt {clone}/kernel/review-core.md\n")
        found, source = find_prompts_dir()
        assert found == clone / "kernel"
        assert "legacy" in source

    def test_home_default(self, clone, no_legacy):
        home_clone = no_legacy / "review-prompts" / "kernel"
        home_clone.mkdir(parents=True)
        (home_clone / "review-core.md").write_text("x")
        found, _ = find_prompts_dir()
        assert found == home_clone

    def test_nothing_found(self, no_legacy):
        assert find_prompts_dir() == (None, None)


class TestCheckPrompts:

    def test_all_present(self, clone):
        with patch("lreview.prompts.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_prompts(explicit=clone)
        assert status.available is True
        assert status.prompts_dir == clone / "kernel"

    def test_missing_agent_cli(self, clone):
        with patch("lreview.prompts.shutil.which", return_value=None):
            status = check_prompts(explicit=clone, agent="codex")
        assert status.available is False
        assert any("codex CLI" in p for p in status.problems)

    def test_missing_prompts(self, tmp_path, no_legacy):
        with patch("lreview.prompts.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_prompts(explicit=tmp_path / "nowhere")
        assert status.available is False
        assert any("review-prompts not found" in p
                   for p in status.problems)

    def test_missing_gerrit_prompt(self, clone):
        (clone / "kernel" / "gerrit-review.md").unlink()
        with patch("lreview.prompts.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_prompts(explicit=clone)
        assert status.available is False
        assert any("gerrit-review.json" in p for p in status.problems)


class TestSetupInstructions:

    def test_mentions_clone_only(self, tmp_path):
        text = setup_instructions(tmp_path / "rp")
        assert "git clone" in text
        assert "no skill installation is needed" in text
        assert "setup.sh" not in text
