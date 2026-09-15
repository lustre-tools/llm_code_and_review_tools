"""Tests for review-prompts location and availability checks."""

from pathlib import Path
from unittest.mock import patch

import pytest

from lreview.prompts import (
    check_prompts,
    find_prompts_dir,
    setup_instructions,
)


@pytest.fixture(autouse=True)
def no_real_repo(monkeypatch, tmp_path):
    """Isolate from the real checkout's bundled submodule."""
    monkeypatch.setattr("lreview.prompts._REPO_ROOT",
                        tmp_path / "norepo")


def _make_prompts(base: Path) -> Path:
    d = base / "kernel"
    d.mkdir(parents=True)
    (d / "review-core.md").write_text("# review core\n")
    (d / "gerrit-review.md").write_text("# gerrit output\n")
    return base


@pytest.fixture
def clone(tmp_path):
    """A fake review-prompts clone with the needed kernel prompts."""
    return _make_prompts(tmp_path / "review-prompts")


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

    def test_bundled_submodule(self, monkeypatch, tmp_path, no_legacy):
        repo = tmp_path / "llmrepo"
        _make_prompts(repo / "review-prompts")
        monkeypatch.setattr("lreview.prompts._REPO_ROOT", repo)
        found, source = find_prompts_dir()
        assert found == repo / "review-prompts" / "kernel"
        assert "bundled submodule" in source

    def test_bundled_beats_legacy(self, monkeypatch, tmp_path, clone,
                                  no_legacy):
        repo = tmp_path / "llmrepo"
        _make_prompts(repo / "review-prompts")
        monkeypatch.setattr("lreview.prompts._REPO_ROOT", repo)
        commands = no_legacy / ".claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "kreview.md").write_text(
            f"Read the prompt {clone}/kernel/review-core.md\n")
        found, source = find_prompts_dir()
        assert found == repo / "review-prompts" / "kernel"
        assert "bundled" in source


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

    def test_mentions_submodule_when_bundled(self, monkeypatch, tmp_path):
        repo = tmp_path / "llmrepo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".gitmodules").write_text(
            '[submodule "review-prompts"]\n\tpath = review-prompts\n')
        monkeypatch.setattr("lreview.prompts._REPO_ROOT", repo)
        text = setup_instructions()
        assert "submodule update --init review-prompts" in text
        assert "git clone" in text  # fallback still listed


class TestPromptsFreshness:

    @pytest.fixture
    def clone_pair(self, tmp_path):
        """An 'upstream' repo and a clone of it, upstream one ahead."""
        import subprocess

        def git(cwd, *args):
            subprocess.run(["git", "-C", str(cwd), *args], check=True,
                           capture_output=True)

        upstream = tmp_path / "upstream"
        upstream.mkdir()
        git(upstream, "init", "-q")
        git(upstream, "config", "user.email", "t@example.com")
        git(upstream, "config", "user.name", "T")
        (upstream / "kernel").mkdir()
        (upstream / "kernel" / "review-core.md").write_text("core")
        git(upstream, "add", ".")
        git(upstream, "commit", "-qm", "c1")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(upstream), str(clone)],
                       check=True, capture_output=True)
        (upstream / "kernel" / "new.md").write_text("new knowledge")
        git(upstream, "add", ".")
        git(upstream, "commit", "-qm", "c2 newer prompts")
        return upstream, clone

    def test_detects_behind_and_updates(self, clone_pair):
        from lreview.prompts import (prompts_freshness,
                                     update_prompts_checkout)
        _upstream, clone = clone_pair
        fresh = prompts_freshness(clone / "kernel")
        assert fresh is not None
        behind, root, ref = fresh
        assert behind == 1
        assert root == clone

        ok, detail = update_prompts_checkout(root, ref)
        assert ok, detail
        assert (clone / "kernel" / "new.md").is_file()
        assert prompts_freshness(clone / "kernel")[0] == 0

    def test_detached_checkout_updates(self, clone_pair):
        """The bundled submodule is a detached checkout — the update
        moves its detached HEAD rather than needing a branch."""
        import subprocess
        from lreview.prompts import (prompts_freshness,
                                     update_prompts_checkout)
        _upstream, clone = clone_pair
        subprocess.run(["git", "-C", str(clone), "checkout", "-q",
                        "--detach", "HEAD"], check=True)
        behind, root, ref = prompts_freshness(clone / "kernel")
        assert behind == 1
        ok, detail = update_prompts_checkout(root, ref)
        assert ok, detail
        assert (clone / "kernel" / "new.md").is_file()

    def test_not_a_checkout_is_none(self, tmp_path):
        from lreview.prompts import prompts_freshness
        plain = tmp_path / "plain"
        (plain / "kernel").mkdir(parents=True)
        assert prompts_freshness(plain / "kernel") is None

    def test_local_changes_fail_ff_gracefully(self, clone_pair):
        from lreview.prompts import (prompts_freshness,
                                     update_prompts_checkout)
        _upstream, clone = clone_pair
        import subprocess
        # diverge the clone with its own commit
        (clone / "local.md").write_text("mine")
        subprocess.run(["git", "-C", str(clone), "add", "."],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "-c",
                        "user.email=t@example.com", "-c", "user.name=T",
                        "commit", "-qm", "local"], check=True,
                       capture_output=True)
        behind, root, ref = prompts_freshness(clone / "kernel")
        assert behind == 1
        ok, detail = update_prompts_checkout(root, ref)
        assert not ok
        assert "manually" in detail
