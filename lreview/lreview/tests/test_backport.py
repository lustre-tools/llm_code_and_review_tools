"""Backport detection and its prompt."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from lreview import backport
from lreview.backport import (Trailer, Upstream, backport_prompt, detect,
                              parse_trailers)
from lreview.gerrit import LocalChange
from lreview.runner import (BatchConfig, attach_backport, build_agent_cmd,
                            compaction_settings, count_findings, preload_file,
                            review_prompt)

SHA_A = "a" * 40
SHA_B = "b" * 40


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    git(path, "commit", "-q", "--allow-empty", "-m", "upstream")
    return path


def commit(repo, message):
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def config_for(tmp_path, repo, **kwargs):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("review-core.md", "technical-patterns.md",
                 "lustre-commit-message.md", "lustre-style.md"):
        (prompts / name).write_text("x")
    (prompts / "subsystem").mkdir(exist_ok=True)
    for name in ("build.md", "subsystem.md"):
        (prompts / "subsystem" / name).write_text("x")
    return BatchConfig(repo=repo, results_dir=tmp_path / "results",
                       worktrees_dir=tmp_path / "wt", prompts_dir=prompts,
                       **kwargs)


class TestParseTrailers:

    def test_master_commit_has_none(self):
        assert parse_trailers("LU-1 x: y\n\nChange-Id: I123\n") == []

    def test_landed(self):
        body = ("Lustre-change: https://review.whamcloud.com/63135\n"
                f"Lustre-commit: {SHA_A}\n")
        assert parse_trailers(body) == [
            Trailer("https://review.whamcloud.com/63135", SHA_A, True)]

    @pytest.mark.parametrize("value", [
        f"TBD (from {SHA_A})", f"TBD ({SHA_A})"])
    def test_tbd_keeps_source_hash(self, value):
        body = f"Lustre-change: https://r/1\nLustre-commit: {value}\n"
        assert parse_trailers(body) == [Trailer("https://r/1", SHA_A, False)]

    def test_bare_tbd(self):
        body = "Lustre-change: https://r/1\nLustre-commit: TBD\n"
        assert parse_trailers(body) == [Trailer("https://r/1", None, False)]

    def test_squash_keeps_pairs(self):
        body = (f"Lustre-change: https://r/1\nLustre-commit: {SHA_A}\n\n"
                f"Lustre-change: https://r/2\nLustre-commit: {SHA_B}\n")
        assert [t.commit for t in parse_trailers(body)] == [SHA_A, SHA_B]

    def test_hash_in_change_trailer(self):
        assert parse_trailers(f"Lustre-change: {SHA_A}\n") == [
            Trailer(None, SHA_A, True)]

    def test_trailer_word_in_body_text_is_ignored(self):
        assert parse_trailers("Add the Lustre-change: trailer.\n") == []


class TestDetect:

    def test_not_a_port(self, repo):
        assert detect(repo, commit(repo, "plain")) is None

    def test_landed_local_commit_needs_no_gerrit(self, repo):
        upstream = git(repo, "rev-parse", "HEAD")
        sha = commit(repo, "port\n\nLustre-change: https://r/1\n"
                           f"Lustre-commit: {upstream}\n")
        with patch.object(backport, "resolve_change") as resolve:
            found = detect(repo, sha)
        resolve.assert_not_called()
        assert found == [Upstream("https://r/1", upstream, True)]

    def test_tbd_reports_a_moved_upstream(self, repo):
        current = git(repo, "rev-parse", "HEAD")
        sha = commit(repo, "port\n\nLustre-change: https://r/1\n"
                           f"Lustre-commit: TBD (from {SHA_A})\n")
        change = MagicMock(sha=current, status="NEW")
        with patch.object(backport, "resolve_change", return_value=change):
            found = detect(repo, sha)
        assert found == [Upstream("https://r/1", current, False, SHA_A)]

    def test_tbd_that_landed(self, repo):
        current = git(repo, "rev-parse", "HEAD")
        sha = commit(repo, "port\n\nLustre-change: https://r/1\n"
                           f"Lustre-commit: TBD (from {current})\n")
        change = MagicMock(sha=current, status="MERGED")
        with patch.object(backport, "resolve_change", return_value=change):
            found = detect(repo, sha)
        assert found == [Upstream("https://r/1", current, True)]

    def test_gerrit_failure_still_reports_a_port(self, repo):
        sha = commit(repo, "port\n\nLustre-change: https://r/1\n"
                           f"Lustre-commit: {SHA_A}\n")
        with patch.object(backport, "resolve_change",
                          side_effect=OSError("offline")):
            found = detect(repo, sha)
        assert found == [Upstream("https://r/1", None, True)]


class TestPrompt:

    def test_compare_commands_and_rules(self):
        text = backport_prompt([Upstream("https://r/1", SHA_A, True)])
        assert f"{SHA_A}^!" in text and "HEAD^!" in text
        assert "UPSTREAM-BLOCKER" in text

    def test_missing_upstream_has_no_compare(self):
        text = backport_prompt([Upstream("https://r/1", None, True)])
        assert "could not get" in text and "range-diff" not in text.split(
            "# Backport review")[0]

    def test_squash_note(self):
        text = backport_prompt([Upstream("a", SHA_A, True),
                                Upstream("b", SHA_B, True)])
        assert "squashes" in text

    def test_review_prompt_only_for_a_port(self, tmp_path, repo):
        config = config_for(tmp_path, repo)
        plain = LocalChange("HEAD", SHA_A, "s")
        port = LocalChange("HEAD", SHA_A, "s",
                           backport=[Upstream("u", SHA_B, True)])
        assert "backport" not in review_prompt(config, plain).lower()
        assert "UPSTREAM-BLOCKER" in review_prompt(config, port)

    @pytest.mark.parametrize("mode", ["full", "light"])
    def test_every_mode_gets_it(self, tmp_path, repo, mode):
        config = config_for(tmp_path, repo, mode=mode)
        port = LocalChange("HEAD", SHA_A, "s",
                           backport=[Upstream("u", SHA_B, True)])
        assert "UPSTREAM-BLOCKER" in review_prompt(config, port)

    def test_preload_file_stays_shared(self, tmp_path, repo):
        config = config_for(tmp_path, repo, preload=True)
        config.results_dir.mkdir()
        port = LocalChange("HEAD", SHA_A, "s",
                           backport=[Upstream("u", SHA_B, True)])
        assert "UPSTREAM-BLOCKER" in review_prompt(config, port)
        assert "UPSTREAM-BLOCKER" not in preload_file(config).read_text()

    def test_compaction_restores_rules(self, tmp_path, repo):
        config = config_for(tmp_path, repo)
        port = LocalChange("HEAD", SHA_A, "s",
                           backport=[Upstream("u", SHA_B, True)])
        cmd = build_agent_cmd(config, port)
        settings = cmd[cmd.index("--settings") + 1]
        assert "backport-prompt.md" in settings and SHA_B in settings

    def test_no_settings_without_a_reason(self, tmp_path, repo):
        config = config_for(tmp_path, repo)
        cmd = build_agent_cmd(config, LocalChange("HEAD", SHA_A, "s"))
        assert "--settings" not in cmd

    def test_compaction_keeps_memory_text(self, tmp_path):
        assert "memory protocol" in compaction_settings(tmp_path / "d.md")


class TestAttach:

    def test_off_by_config(self, tmp_path, repo):
        sha = commit(repo, f"p\n\nLustre-commit: {SHA_A}\n")
        change = LocalChange("HEAD", sha, "p")
        attach_backport(config_for(tmp_path, repo, backport=False), change)
        assert change.backport is None

    def test_on(self, tmp_path, repo):
        upstream = git(repo, "rev-parse", "HEAD")
        sha = commit(repo, f"p\n\nLustre-commit: {upstream}\n")
        change = LocalChange("HEAD", sha, "p")
        attach_backport(config_for(tmp_path, repo, backport=True), change)
        assert change.backport[0].sha == upstream

    def test_failure_leaves_a_normal_review(self, tmp_path, repo):
        change = LocalChange("HEAD", SHA_A, "p")
        attach_backport(config_for(tmp_path, repo, backport=True), change)
        assert change.backport is None


class TestCountFindings:

    def test_notes_do_not_count(self):
        spec = {"comments": {"a.c": [
            {"line": 1, "message": "x", "unresolved": True},
            {"line": 2, "message": "(defect) UPSTREAM: y",
             "unresolved": False},
            {"line": 3, "message": "z"}]}}
        assert count_findings(spec) == 2
