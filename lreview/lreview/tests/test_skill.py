"""Tests for the kreview skill availability check."""

from pathlib import Path
from unittest.mock import patch

import pytest

from lreview.skill import check_skill, setup_instructions


@pytest.fixture
def prompts_dir(tmp_path):
    """A fake review-prompts kernel directory with the needed prompts."""
    d = tmp_path / "review-prompts" / "kernel"
    d.mkdir(parents=True)
    (d / "review-core.md").write_text("# review core\n")
    (d / "gerrit-review.md").write_text("# gerrit output\n")
    return d


@pytest.fixture
def commands(tmp_path):
    d = tmp_path / "commands"
    d.mkdir()
    return d


def _install_command(commands: Path, prompt: Path) -> None:
    (commands / "kreview.md").write_text(
        f"Read the prompt {prompt}\n\nDo a deep dive analysis.\n")


class TestCheckSkill:

    def test_all_present(self, commands, prompts_dir):
        _install_command(commands, prompts_dir / "review-core.md")
        with patch("lreview.skill.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_skill(commands)
        assert status.available is True
        assert status.prompt_path == prompts_dir / "review-core.md"
        assert status.prompts_dir == prompts_dir

    def test_missing_claude_cli(self, commands, prompts_dir):
        _install_command(commands, prompts_dir / "review-core.md")
        with patch("lreview.skill.shutil.which", return_value=None):
            status = check_skill(commands)
        assert status.available is False
        assert any("claude CLI" in p for p in status.problems)

    def test_missing_command_file(self, commands):
        with patch("lreview.skill.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_skill(commands)
        assert status.available is False
        assert any("slash command not installed" in p
                   for p in status.problems)

    def test_missing_prompt_file(self, commands, tmp_path):
        _install_command(commands, tmp_path / "nowhere" / "review-core.md")
        with patch("lreview.skill.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_skill(commands)
        assert status.available is False
        assert any("missing" in p for p in status.problems)

    def test_missing_gerrit_prompt(self, commands, prompts_dir):
        (prompts_dir / "gerrit-review.md").unlink()
        _install_command(commands, prompts_dir / "review-core.md")
        with patch("lreview.skill.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_skill(commands)
        assert status.available is False
        assert any("gerrit-review.json" in p for p in status.problems)

    def test_command_without_path(self, commands):
        (commands / "kreview.md").write_text("no path here\n")
        with patch("lreview.skill.shutil.which",
                   return_value="/usr/bin/claude"):
            status = check_skill(commands)
        assert status.available is False


class TestSetupInstructions:

    def test_mentions_clone_and_setup(self, tmp_path):
        text = setup_instructions(tmp_path / "rp")
        assert "git clone" in text
        assert "setup.sh claude kernel" in text
