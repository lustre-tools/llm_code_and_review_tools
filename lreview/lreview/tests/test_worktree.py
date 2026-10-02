"""Fetching a change: the repository's own remotes first, then the
anonymous URL."""

import subprocess
from pathlib import Path

import pytest

from lreview import worktree as wt


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


class TestGerritRemoteUrls:

    def test_remotes_for_the_project_in_any_url_form(self, repo):
        for name, url in (
                ("ex", "ssh://mvef@review.example.com:29418/ex/lustre-release"),
                ("https", "https://review.example.com/a/ex/lustre-release.git"),
                ("scp", "mvef@review.example.com:ex/lustre-release"),
                ("public", "ssh://mvef@review.example.com:29418/fs/lustre-release"),
                ("elsewhere", "ssh://git@github.com/ex/lustre-release"),
        ):
            _git(repo, "remote", "add", name, url)
        assert wt.gerrit_remote_urls(
            repo, "https://review.example.com", "ex/lustre-release") == [
            "ssh://mvef@review.example.com:29418/ex/lustre-release",
            "https://review.example.com/a/ex/lustre-release.git",
            "mvef@review.example.com:ex/lustre-release",
        ]

    def test_no_matching_remote(self, repo):
        _git(repo, "remote", "add", "origin",
             "ssh://mvef@review.example.com:29418/fs/lustre-release")
        assert wt.gerrit_remote_urls(
            repo, "https://review.example.com", "ex/lustre-release") == []


class TestFetchChange:

    @pytest.fixture
    def server(self, tmp_path):
        """A bare repository holding a change ref, reachable by file URL."""
        src = tmp_path / "src"
        src.mkdir()
        _git(src, "init", "-q")
        _git(src, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "change")
        sha = _git(src, "rev-parse", "HEAD")
        _git(src, "update-ref", "refs/changes/33/68633/2", sha)
        return src, sha

    def test_first_url_that_works_wins(self, repo, server, tmp_path):
        src, sha = server
        wt.fetch_change(repo, [str(tmp_path / "missing"), f"file://{src}"],
                        "refs/changes/33/68633/2")
        assert wt.commit_exists(repo, sha)

    def test_all_failing_names_every_url(self, repo, tmp_path):
        with pytest.raises(wt.GitError) as exc:
            wt.fetch_change(repo, [str(tmp_path / "a"), str(tmp_path / "b")],
                            "refs/changes/33/68633/2")
        assert str(tmp_path / "a") in str(exc.value)
        assert str(tmp_path / "b") in str(exc.value)

    def test_never_waits_for_a_login(self, repo, monkeypatch):
        seen = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            if "fetch" in cmd:
                seen["env"] = kwargs.get("env") or {}
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(wt.subprocess, "run", spy)
        with pytest.raises(wt.GitError):
            wt.fetch_change(repo, ["/nonexistent"], "refs/x")
        assert seen["env"].get("GIT_TERMINAL_PROMPT") == "0"


def test_prepare_worktree_tries_the_repos_remote_before_anonymous(
        repo, tmp_path, monkeypatch):
    from lreview.gerrit import ResolvedChange, change_ref
    from lreview.runner import BatchConfig, prepare_worktree
    _git(repo, "remote", "add", "ex",
         "ssh://mvef@review.example.com:29418/ex/lustre-release")
    tried = []
    monkeypatch.setattr(
        wt, "fetch_change",
        lambda repo, urls, ref: tried.append(urls) or
        (_ for _ in ()).throw(wt.GitError("stop")))
    change = ResolvedChange(
        number=68633, project="ex/lustre-release", subject="s",
        sha="a" * 40, patchset=2, ref=change_ref(68633, 2),
        base_url="https://review.example.com")
    config = BatchConfig(repo=repo, results_dir=tmp_path / "r",
                         worktrees_dir=tmp_path / "w")
    with pytest.raises(wt.GitError):
        prepare_worktree(config, change)
    assert tried == [[
        "ssh://mvef@review.example.com:29418/ex/lustre-release",
        "https://review.example.com/ex/lustre-release",
    ]]
