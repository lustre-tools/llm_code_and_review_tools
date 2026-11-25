"""Tests for the one version of all tools."""

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from llm_tool_common import update_check, version

REPO = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.x",
         *args], check=True, capture_output=True, text=True).stdout.strip()


def write_version(repo, text):
    path = repo / "llm_tool_common" / "pyproject.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f'[project]\nname = "x"\nversion = "{text}"\n')


def commit(repo, name):
    (repo / name).write_text(name)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "--short=7", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout whose last bump, to 0.6.0, has one commit before it."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "install.sh").write_text("")
    write_version(path, "0.5.9")
    commit(path, "old")
    write_version(path, "0.6.0")
    commit(path, "bump")
    monkeypatch.setattr(update_check, "_REPO", path)
    return path


def publish(repo):
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


class TestRepoVersion:
    def test_the_bump_itself_is_the_plain_version(self, repo):
        assert version.repo_version() == "0.6.0"

    def test_commits_after_it_are_counted(self, repo):
        commit(repo, "a")
        sha = commit(repo, "b")
        assert version.repo_version() == f"0.6.2_g{sha}"

    def test_the_next_bump_starts_over(self, repo):
        commit(repo, "a")
        write_version(repo, "0.7.0")
        commit(repo, "bump2")
        assert version.repo_version() == "0.7.0"
        sha = commit(repo, "b")
        assert version.repo_version() == f"0.7.1_g{sha}"

    def test_another_edit_of_the_file_does_not(self, repo):
        commit(repo, "a")
        path = repo / "llm_tool_common" / "pyproject.toml"
        path.write_text(path.read_text() + "dependencies = []\n")
        sha = commit(repo, "deps")
        assert version.repo_version() == f"0.6.2_g{sha}"

    def test_a_shallow_clone_cannot_count(self, repo, tmp_path, monkeypatch):
        sha = commit(repo, "a")
        clone = tmp_path / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        f"file://{repo}", str(clone)], check=True)
        monkeypatch.setattr(update_check, "_REPO", clone)
        assert version.repo_version() == f"0.6.0_g{sha}"
        assert version.published_version() == "0.6.0"

    def test_without_git_the_plain_version(self, repo, monkeypatch):
        commit(repo, "a")
        monkeypatch.setattr(
            update_check, "_git",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
        assert version.repo_version() == "0.6.0"
        assert version.published_version() == "0.6.0"

    def test_outside_a_checkout_the_installed_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(update_check, "_REPO", tmp_path)
        monkeypatch.setattr(version.metadata, "version", lambda name: "0.6.0")
        assert version.repo_version() == "0.6.0"

    def test_neither_checkout_nor_package(self, tmp_path, monkeypatch):
        def missing(name):
            raise version.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(update_check, "_REPO", tmp_path)
        monkeypatch.setattr(version.metadata, "version", missing)
        assert version.repo_version() == "unknown"


class TestPublishedVersion:
    def test_local_commits_are_not_counted(self, repo):
        commit(repo, "a")
        publish(repo)
        commit(repo, "private")
        assert version.published_version() == "0.6.1"

    def test_no_remote_main_is_the_plain_version(self, repo):
        commit(repo, "a")
        assert version.published_version() == "0.6.0"

    def test_never_a_commit(self, repo):
        commit(repo, "a")
        publish(repo)
        assert re.fullmatch(r"\d+\.\d+\.\d+", version.published_version())


class TestOptions:
    def test_argparse(self, repo, capsys):
        parser = argparse.ArgumentParser(prog="tool")
        parser.add_argument("--version", action=version.VersionAction)
        assert parser.parse_args([]) == argparse.Namespace()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out == "tool 0.6.0\n"

    def test_click(self, repo):
        @click.command()
        @version.version_option("tool")
        def main():
            click.echo("ran")

        result = CliRunner().invoke(main, ["--version"])
        assert (result.exit_code, result.output) == (0, "tool, version 0.6.0\n")
        assert CliRunner().invoke(main, []).output == "ran\n"


def test_every_tool_has_the_same_version():
    """Only `make bump` changes it: a version line in a PR is a mistake."""
    versions = {}
    for path in REPO.glob("*/pyproject.toml"):
        project = tomllib.loads(path.read_text()).get("project", {})
        if "version" in project:
            versions[path.parent.name] = project["version"]
    assert len(versions) > 5
    assert len(set(versions.values())) == 1, versions
    assert re.fullmatch(r"\d+\.\d+\.0", versions["llm_tool_common"])
