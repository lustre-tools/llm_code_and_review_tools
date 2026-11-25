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
from lreview.models import codex_model


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

    def _codex_chat(self, repo, tmp_path, monkeypatch):
        """A stub codex recording its argv, a manifest entry for 64616,
        and a runner for `lreview chat 64616 --agent codex ARGS`."""
        from lreview.cli import build_parser
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        record = tmp_path / "codex-invocation.json"
        stub = bin_dir / "codex"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(
            "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.delenv("LREVIEW_MODEL", raising=False)
        monkeypatch.delenv("LREVIEW_EFFORT", raising=False)

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

        def chat(*extra):
            args = build_parser().parse_args([
                "chat", "64616", "--agent", "codex", *extra,
                "--results-dir", str(results), "--db", str(tmp_path / "db"),
                "--worktrees-dir", str(tmp_path / "wt")])
            return args.func(args)
        return chat, record

    def test_cli_expands_codex_aliases(self, repo, tmp_path, monkeypatch):
        """`chat --model sol` runs the model its effort was checked
        against, as `run --model sol` does, not the bare alias."""
        chat, record = self._codex_chat(repo, tmp_path, monkeypatch)
        assert chat("--model", "sol") == 0
        assert json.loads(record.read_text())[:2] == [
            "-m", codex_model("sol").slug]

    def test_cli_checks_effort_against_the_session_model(
            self, repo, tmp_path, monkeypatch):
        """Without --model, chat starts codex on its own default, so a
        $LREVIEW_MODEL meant for `run` must not veto the effort."""
        chat, record = self._codex_chat(repo, tmp_path, monkeypatch)
        monkeypatch.setenv("LREVIEW_MODEL", "luna")
        assert chat("--effort", "ultra") == 0
        argv = json.loads(record.read_text())
        assert "-m" not in argv
        assert 'model_reasoning_effort="ultra"' in argv
        # the model chat does pass is still checked
        assert chat("--model", "luna", "--effort", "ultra") == 1

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

    def _reviewed(self, repo, tmp_path, monkeypatch, sessions=(),
                  mode="full", on_disk=True):
        """A reviewed change whose memory doc records `sessions`
        ((mode, id) pairs); Claude keeps those files when on_disk."""
        from lreview.memory import ensure_doc, record_session
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        results = tmp_path / "results"
        results.mkdir()
        key = "64616" if mode == "full" else "64616-light"
        (results / "summary.json").write_text(json.dumps({key: {
            "number": 64616, "patchset": 27, "sha": sha, "mode": mode,
            "subject": "LU-1 lod: subject", "status": "findings",
            "base_url": "https://gerrit.invalid",
            "repository": "fs/lustre-release",
        }}))
        db = tmp_path / "db"
        doc = ensure_doc(db, _change(sha=sha))
        config_dir = tmp_path / "claude-config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        for session_mode, session_id in sessions:
            record_session(doc, session_mode, session_id,
                           f"ps27 {sha[:12]}")
            if on_disk:
                path = config_dir / "projects" / "-wt" / f"{session_id}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
        return results, db, doc

    def _chat(self, repo, tmp_path, results, db, **kwargs):
        rc = run_chat("64616", repo=repo, results_dir=results,
                      worktrees_dir=tmp_path / "wt", db_dir=db, **kwargs)
        assert rc == 0
        return json.loads((tmp_path / "invocation.json").read_text())

    def test_resumes_a_fork_of_the_review_session(self, repo, tmp_path,
                                                  stub_claude, monkeypatch):
        from lreview.memory import read_session
        results, db, doc = self._reviewed(repo, tmp_path, monkeypatch,
                                          sessions=[("full", "sess-9")])
        invocation = self._chat(repo, tmp_path, results, db)
        argv = invocation["argv"]
        assert argv[argv.index("--resume") + 1] == "sess-9"
        assert "--fork-session" in argv
        prompt = argv[-1]
        assert "is finished" in prompt and "continuing it" in prompt
        assert invocation["cwd"] in prompt
        # the chat's own session is a fork that is never recorded
        assert read_session(doc, "full").session_id == "sess-9"

    def test_no_resume_starts_a_fresh_chat(self, repo, tmp_path,
                                           stub_claude, monkeypatch):
        results, db, _ = self._reviewed(repo, tmp_path, monkeypatch,
                                        sessions=[("full", "sess-9")])
        argv = self._chat(repo, tmp_path, results, db,
                          resume=False)["argv"]
        assert "--resume" not in argv
        assert argv[-1].startswith("Interactive discussion of")

    def test_a_session_claude_lost_starts_fresh(self, repo, tmp_path,
                                                stub_claude, monkeypatch):
        results, db, _ = self._reviewed(repo, tmp_path, monkeypatch,
                                        sessions=[("full", "sess-9")],
                                        on_disk=False)
        argv = self._chat(repo, tmp_path, results, db)["argv"]
        assert "--resume" not in argv
        assert argv[-1].startswith("Interactive discussion of")

    def test_a_light_review_resumes_its_light_session(
            self, repo, tmp_path, stub_claude, monkeypatch):
        results, db, _ = self._reviewed(
            repo, tmp_path, monkeypatch, mode="light",
            sessions=[("full", "deep"), ("light", "quick")])
        argv = self._chat(repo, tmp_path, results, db)["argv"]
        assert argv[argv.index("--resume") + 1] == "quick"

    def test_other_agents_ignore_resume(self):
        from lreview.agents import get_agent
        cmd = get_agent("codex").build_interactive_cmd(
            None, [], "PROMPT", resume="sess-9")
        assert "--resume" not in cmd

    def test_cli_no_resume(self, monkeypatch):
        from lreview.cli import build_parser, cmd_chat
        captured = {}
        monkeypatch.setattr("lreview.chat.run_chat",
                            lambda *a, **kw: captured.update(kw) or 0)
        args = build_parser().parse_args(["chat", "64616", "--no-resume"])
        assert cmd_chat(args) == 0
        assert captured["resume"] is False
        args = build_parser().parse_args(["chat", "64616"])
        cmd_chat(args)
        assert captured["resume"] is True


class TestReviewSession:
    """chat continues the review's own conversation, found in the
    manifest, or in the log of a review that did not record it."""

    OPTIONS = {"model": "sonnet", "effort": "high", "lean": True,
               "preload": ".preload-abc.md", "lenses": True,
               "lens_model": None, "lens_effort": "medium",
               "agent_args": ["--x"]}

    @pytest.fixture
    def world(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        for cmd in (["git", "init", "-q", str(repo)],
                    ["git", "-C", str(repo), "-c", "user.email=t@t", "-c",
                     "user.name=t", "commit", "-q", "--allow-empty", "-m",
                     "c"]):
            subprocess.run(cmd, check=True)
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True).stdout.strip()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "claude"
        record = tmp_path / "invocation.json"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps(\n"
            "    {'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
            "     'env': dict(os.environ)}))\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(
            "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        config_dir = tmp_path / "claude-config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        session = config_dir / "projects" / "-wt" / "sess-9.jsonl"
        (session.with_suffix("") / "subagents").mkdir(parents=True)
        session.write_text("{}\n")
        results = tmp_path / "results"
        results.mkdir()
        (results / ".preload-abc.md").write_text("protocol")

        class World:
            pass
        w = World()
        w.repo, w.sha, w.results, w.tmp = repo, sha, results, tmp_path
        w.session_file = session
        w.invocation = lambda: json.loads(record.read_text())

        def entry(**kw):
            (results / "summary.json").write_text(json.dumps({"64616": {
                "number": 64616, "patchset": 27, "sha": sha, "mode": "full",
                "subject": "LU-1 lod: subject", "status": "findings",
                "agent": "claude", "base_url": "https://gerrit.invalid",
                "repository": "fs/lustre-release", **kw}}))
        w.entry = entry

        def chat(**kw):
            assert run_chat("64616", repo=repo, results_dir=results,
                            worktrees_dir=tmp_path / "wt", **kw) == 0
            return w.invocation()
        w.chat = chat
        return w

    def test_continues_with_the_reviews_own_flags(self, world):
        world.entry(session={"id": "sess-9", **self.OPTIONS})
        got = world.chat()
        argv = got["argv"]
        assert argv[argv.index("--resume") + 1] == "sess-9"
        assert "--fork-session" in argv
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[argv.index("--effort") + 1] == "high"
        assert argv[argv.index("--tools") + 1].endswith("Agent,TodoWrite")
        assert "lens" in json.loads(argv[argv.index("--agents") + 1])
        assert argv[argv.index("--append-system-prompt-file") + 1] == str(
            world.results / ".preload-abc.md")
        assert "--x" in argv
        assert got["env"]["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "5m"
        assert got["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
        assert "dangerously" not in " ".join(argv)
        assert "subagents/*.jsonl" in argv[-1]
        assert "suspicions" not in argv[-1]

    def test_the_review_command_has_the_same_cached_flags(self, world):
        """What chat passes for the tools and the system prompt is what
        the review passed, or the prompt cache misses."""
        from lreview.runner import BatchConfig, build_agent_cmd
        from lreview.session import ReviewSession, launch, session_options
        prompts = world.tmp / "prompts"
        (prompts / "subsystem").mkdir(parents=True)
        from lreview.runner import PRELOAD_FILES
        for name in PRELOAD_FILES:
            (prompts / name).write_text(name)
        config = BatchConfig(
            repo=world.repo, results_dir=world.results,
            worktrees_dir=world.tmp / "wt", prompts_dir=prompts,
            model="sonnet", effort="high", lean=True, preload=True,
            lenses=True, lens_effort="medium")
        change = _change(sha=world.sha)
        review = build_agent_cmd(config, change, worktree=world.repo)
        args, _ = launch(
            ReviewSession("s", "r", session_options(config, change)),
            world.repo, world.results)
        for flag in ("--setting-sources", "--agents", "--tools",
                     "--append-system-prompt-file"):
            assert (args[args.index(flag) + 1]
                    == review[review.index(flag) + 1]), flag
        assert "--disable-slash-commands" not in args  # keeps /exit
        for flag in ("--strict-mcp-config",
                     "--exclude-dynamic-system-prompt-sections"):
            assert flag in args and flag in review

    def test_an_older_review_has_its_session_in_the_log(self, world):
        (world.results / "r.log").write_text(
            '{"type":"system","subtype":"init","session_id":"sess-9"}\n')
        world.entry(log="r.log", model="sonnet", effort="high",
                    lenses="sonnet/medium")
        argv = world.chat()["argv"]
        assert argv[argv.index("--resume") + 1] == "sess-9"
        assert argv[argv.index("--model") + 1] == "sonnet"

    def test_chats_own_model_wins(self, world):
        world.entry(session={"id": "sess-9", **self.OPTIONS})
        argv = world.chat(model="opus")["argv"]
        assert argv[argv.index("--model") + 1] == "opus"

    LENS_LOG = "\n".join(json.dumps(e) for e in (
        {"type": "system", "session_id": "sess-9"},
        {"type": "assistant", "parent_tool_use_id": "t", "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {
                "command": "mkdir -p c; cat > c/LENS-data.json <<'EOF'\n"
                           "{\"claim\": \"overflow\"}\nEOF\necho ok"}}]}},
        {"type": "assistant", "parent_tool_use_id": "t", "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {
                "command": "echo '{\"candidates\":[]}' > "
                           "c/LENS-build.json"}}]}},
        {"type": "assistant", "message": {"content": [{
            "type": "text",
            "text": "LENS DISPOSITIONS:\n- data 1: ELIMINATED (a.c:7)"}]}},
    ))

    def test_no_resume_starts_from_the_lens_dossier(self, world):
        (world.results / "r.log").write_text(self.LENS_LOG)
        (world.results / "r.md").write_text("THE REPORT TEXT")
        world.entry(log="r.log", markdown="r.md",
                    session={"id": "sess-9", **self.OPTIONS})
        argv = world.chat(resume=False)["argv"]
        assert "--resume" not in argv and "--agents" not in argv
        assert "--add-dir" not in argv
        assert "THE REPORT TEXT" in argv[-1]
        assert '{"claim": "overflow"}' in argv[-1]
        assert '{"candidates":[]}' in argv[-1]
        assert "data 1: ELIMINATED (a.c:7)" in argv[-1]

    def test_a_lost_session_starts_from_the_lens_dossier(self, world,
                                                         capsys):
        world.session_file.unlink()
        (world.results / "r.log").write_text(self.LENS_LOG)
        world.entry(log="r.log", session={"id": "sess-9", **self.OPTIONS})
        argv = world.chat()["argv"]
        assert "--resume" not in argv
        assert "overflow" in argv[-1]
        assert "no longer has the review session" in capsys.readouterr().out

    def test_another_agents_review_is_not_resumed(self, world):
        world.entry(agent="codex", session={"id": "sess-9"})
        assert "--resume" not in world.chat()["argv"]

    def _result(self, world, number=64616, **kw):
        from lreview.runner import ReviewResult
        change = _change(number=number, sha=world.sha)
        return ReviewResult(change, kw.pop("status", "findings"),
                            session_id=kw.pop("session_id", "sess-9"),
                            session_options=self.OPTIONS, **kw)

    def _after_run(self, world, results, **kw):
        from lreview.chat import chat_after_run
        from lreview.runner import BatchConfig
        world.entry()
        return chat_after_run(BatchConfig(
            repo=world.repo, results_dir=world.results,
            worktrees_dir=world.tmp / "wt"), results, **kw)

    def test_run_chat_goes_on_in_the_review(self, world):
        assert self._after_run(world, [self._result(world)]) == 0
        got = world.invocation()
        assert got["argv"][got["argv"].index("--resume") + 1] == "sess-9"
        # the review's worktree path, gone again afterwards
        assert got["cwd"].endswith(f"kreview_64616_ps27.{os.getpid()}")
        assert not Path(got["cwd"]).exists()

    def test_run_chat_in_place_keeps_the_repo(self, world):
        self._after_run(world, [self._result(world)], in_place=True)
        assert world.invocation()["cwd"] == str(world.repo)
        assert world.repo.is_dir()

    def test_run_chat_asks_which_of_several(self, world, monkeypatch):
        answers = iter(["9", "2"])
        monkeypatch.setattr("builtins.input", lambda p: next(answers))
        self._after_run(world, [
            self._result(world, number=1), self._result(world, number=2),
            self._result(world, number=3, status="failed")])
        assert "Gerrit change 2 " in world.invocation()["argv"][-1]

    def test_run_chat_without_a_finished_review(self, world, capsys):
        self._after_run(world, [self._result(world, status="failed")])
        assert "no finished claude review" in capsys.readouterr().out
        assert not (world.tmp / "invocation.json").exists()
