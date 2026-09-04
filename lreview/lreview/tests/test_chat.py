"""Tests for the interactive chat sessions over existing reviews."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from lreview.chat import (
    artifact_lines,
    chat_prompt,
    manifest_entries,
    run_chat,
)
from lreview.gerrit import ResolvedChange, change_ref


def _change(number=64616, patchset=27, sha="a" * 40):
    return ResolvedChange(
        number=number, project="fs/lustre-release",
        subject="LU-1 lod: subject", sha=sha, patchset=patchset,
        ref=change_ref(number, patchset),
        base_url="https://gerrit.invalid")


class TestManifestEntries:

    def test_orders_newest_ps_then_full(self):
        summary = {
            "64616": {"patchset": 26, "mode": "full"},
            "64616-light": {"patchset": 27, "mode": "light"},
            "99999": {"patchset": 3, "mode": "full"},
        }
        entries = manifest_entries(summary, 64616)
        assert [e[0] for e in entries] == ["64616-light", "64616"]

    def test_full_wins_same_patchset(self):
        summary = {
            "64616": {"patchset": 27, "mode": "full"},
            "64616-light": {"patchset": 27, "mode": "light"},
        }
        assert [e[0] for e in manifest_entries(summary, 64616)] == \
            ["64616", "64616-light"]

    def test_missing(self):
        assert manifest_entries({}, 64616) == []


class TestPromptAndArtifacts:

    def test_artifact_lines_only_existing_files(self, tmp_path):
        (tmp_path / "gerrit-review-64616_ps27.json").write_text("{}")
        (tmp_path / "markdown").mkdir()
        (tmp_path / "markdown" / "r.md").write_text("#")
        entries = [("64616", {
            "mode": "full",
            "json": "gerrit-review-64616_ps27.json",
            "markdown": "markdown/r.md",
            "log": "missing.log",
        })]
        lines = artifact_lines(tmp_path, None, _change(), entries)
        assert any("findings JSON" in line for line in lines)
        assert any("markdown/r.md" in line for line in lines)
        assert not any("missing.log" in line for line in lines)

    def test_artifact_lines_include_memory_doc(self, tmp_path):
        db = tmp_path / "db"
        db.mkdir()
        doc = db / "64616-LU-1_lod_subject.md"
        doc.write_text("---\nnumber: 64616\n---\nnotes")
        lines = artifact_lines(tmp_path, db, _change(), [])
        assert any(str(doc) in line for line in lines)

    def test_prompt_with_and_without_artifacts(self):
        change = _change()
        primed = chat_prompt(change, ["- full report: /x/r.md"])
        assert "64616 patchset 27" in primed
        assert "/x/r.md" in primed
        assert "never post to Gerrit" in primed
        bare = chat_prompt(change, [])
        assert "No collected review artifacts" in bare


class TestRunChat:

    @pytest.fixture
    def repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(  # noqa: E731
            ["git", "-C", str(repo), *a], check=True,
            capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run("add", ".")
        run("commit", "-q", "-m", "c")
        return repo

    @pytest.fixture
    def stub_claude(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "claude"
        record = tmp_path / "invocation.json"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps(\n"
            "    {'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(
            "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return record

    def test_chats_about_reviewed_revision(self, repo, tmp_path,
                                           stub_claude):
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        results = tmp_path / "results"
        results.mkdir()
        (results / "gerrit-review-64616_ps27.json").write_text("{}")
        (results / "summary.json").write_text(json.dumps({"64616": {
            "number": 64616, "patchset": 27, "sha": sha, "mode": "full",
            "subject": "LU-1 lod: subject", "status": "findings",
            "base_url": "https://gerrit.invalid",
            "repository": "fs/lustre-release",
            "json": "gerrit-review-64616_ps27.json",
        }}))

        rc = run_chat("64616", repo=repo, results_dir=results,
                      worktrees_dir=tmp_path / "wt", model="opus")

        assert rc == 0
        invocation = json.loads(stub_claude.read_text())
        assert invocation["argv"][:2] == ["--model", "opus"]
        prompt = invocation["argv"][-1]
        assert "64616 patchset 27" in prompt
        assert "gerrit-review-64616_ps27.json" in prompt
        # ran inside a worktree pinned to the reviewed sha...
        assert invocation["cwd"].startswith(str(tmp_path / "wt"))
        # ...which was removed again after the session
        assert not any((tmp_path / "wt").glob("kreview_*"))

    def test_alternate_agent_backend(self, repo, tmp_path, monkeypatch):
        """--agent codex launches the codex TUI with the positional
        prompt instead of claude."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        record = tmp_path / "codex-invocation.json"
        stub = bin_dir / "codex"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps(\n"
            "    {'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(
            "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        results = tmp_path / "results"
        results.mkdir()
        (results / "summary.json").write_text(json.dumps({"64616": {
            "number": 64616, "patchset": 27, "sha": sha, "mode": "full",
            "subject": "s", "status": "clean", "repo": str(repo),
            "base_url": "https://gerrit.invalid",
        }}))

        rc = run_chat("64616", results_dir=results,
                      worktrees_dir=tmp_path / "wt", agent="codex",
                      model="gpt-5")

        assert rc == 0
        invocation = json.loads(record.read_text())
        assert invocation["argv"][:2] == ["-m", "gpt-5"]
        assert "64616 patchset 27" in invocation["argv"][-1]

    def test_repo_defaults_to_manifest_entry(self, repo, tmp_path,
                                             stub_claude, monkeypatch):
        """Without --repo, chat uses the repository the change was
        reviewed from — never blindly the cwd."""
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        results = tmp_path / "results"
        results.mkdir()
        (results / "summary.json").write_text(json.dumps({"64616": {
            "number": 64616, "patchset": 27, "sha": sha, "mode": "full",
            "subject": "s", "status": "clean", "repo": str(repo),
            "base_url": "https://gerrit.invalid",
        }}))
        monkeypatch.chdir(tmp_path)  # cwd is NOT the source repo

        rc = run_chat("64616", results_dir=results,
                      worktrees_dir=tmp_path / "wt",
                      keep_worktree=True)

        assert rc == 0
        invocation = json.loads(stub_claude.read_text())
        # the worktree came from the manifest's repo, not the cwd
        head = subprocess.run(
            ["git", "-C", invocation["cwd"], "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        assert head == sha

    def test_local_review_by_sha_match(self, repo, tmp_path,
                                       stub_claude):
        """chat --local finds the local review entry for the current
        commit and primes the session with its artifacts."""
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        results = tmp_path / "results"
        results.mkdir()
        slug = f"HEAD_{sha[:7]}"
        (results / f"gerrit-review-{slug}.json").write_text("{}")
        (results / "summary.json").write_text(json.dumps({slug: {
            "number": None, "local": True, "ref_name": "HEAD",
            "patchset": None, "sha": sha, "mode": "full",
            "subject": "LU-1 lod: wip", "status": "findings",
            "json": f"gerrit-review-{slug}.json",
            "reviewed_at": "2026-09-04T10:00:00+00:00",
        }}))

        rc = run_chat("HEAD", results_dir=results, repo=repo,
                      worktrees_dir=tmp_path / "wt", local=True)

        assert rc == 0
        prompt = json.loads(stub_claude.read_text())["argv"][-1]
        assert f"local commit HEAD ({sha[:12]})" in prompt
        assert f"gerrit-review-{slug}.json" in prompt
        assert "never post to Gerrit" in prompt

    def test_local_ref_moved_discusses_reviewed_sha(self, repo, tmp_path,
                                                    stub_claude):
        """After an amend, --local matches by ref name and pins the
        session to the revision that was actually reviewed."""
        old_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        (repo / "f.txt").write_text("y\n")
        subprocess.run(["git", "-C", str(repo), "commit", "-aqm", "c2"],
                       check=True)
        results = tmp_path / "results"
        results.mkdir()
        slug = f"HEAD_{old_sha[:7]}"
        (results / "summary.json").write_text(json.dumps({slug: {
            "number": None, "local": True, "ref_name": "HEAD",
            "sha": old_sha, "mode": "full", "subject": "s",
            "status": "clean",
            "reviewed_at": "2026-09-04T10:00:00+00:00",
        }}))

        rc = run_chat("HEAD", results_dir=results, repo=repo,
                      worktrees_dir=tmp_path / "wt", local=True,
                      keep_worktree=True)

        assert rc == 0
        invocation = json.loads(stub_claude.read_text())
        assert old_sha[:12] in invocation["argv"][-1]
        head = subprocess.run(
            ["git", "-C", invocation["cwd"], "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        assert head == old_sha  # the reviewed revision, not the amend

    def test_local_without_any_review(self, repo, tmp_path, stub_claude):
        rc = run_chat(None, results_dir=tmp_path / "results", repo=repo,
                      worktrees_dir=tmp_path / "wt", local=True)
        assert rc == 0
        prompt = json.loads(stub_claude.read_text())["argv"][-1]
        assert "No collected review artifacts" in prompt

    def test_unknown_change_without_network(self, repo, tmp_path,
                                            stub_claude, monkeypatch):
        # no manifest entry and resolution fails -> clean error
        import lreview.gerrit as gerrit_mod
        monkeypatch.setattr(
            gerrit_mod, "resolve_change",
            lambda spec: (_ for _ in ()).throw(RuntimeError("offline")))
        rc = run_chat("70000", repo=repo,
                      results_dir=tmp_path / "results",
                      worktrees_dir=tmp_path / "wt")
        assert rc == 1
        assert not stub_claude.exists()
