"""Tests for the lreview CLI."""

from pathlib import Path

import pytest

from lreview.cli import build_parser, default_worktrees_dir


class TestParser:

    def test_run_defaults(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr("lreview.prompts._REPO_ROOT", tmp_path)
        monkeypatch.delenv("LREVIEW_PREFIX", raising=False)
        monkeypatch.delenv("LREVIEW_AGENT", raising=False)
        monkeypatch.delenv("LREVIEW_EFFORT", raising=False)
        monkeypatch.delenv("LREVIEW_RESULTS_DIR", raising=False)
        args = build_parser().parse_args(["run", "64086"])
        assert args.effort is None
        assert args.changes == ["64086"]
        assert args.jobs == 5
        assert args.timeout == 7200
        assert args.repo == "."
        # a tools checkout: results default into it, not the cwd
        assert args.results_dir == str(tmp_path / "lreview-results")
        assert args.worktrees_dir is None
        assert args.keep_worktrees is False
        assert args.post is False
        assert args.prefix is None
        assert args.model is None
        assert args.agent == "claude"

    def test_default_results_dir(self, tmp_path, monkeypatch):
        from lreview.cli import default_results_dir
        monkeypatch.setattr("lreview.prompts._REPO_ROOT", tmp_path)
        monkeypatch.delenv("LREVIEW_RESULTS_DIR", raising=False)
        # installed without a checkout (CI's pip install): cwd-relative
        assert default_results_dir() == "./lreview-results"
        (tmp_path / ".git").mkdir()
        assert default_results_dir() == str(tmp_path / "lreview-results")
        monkeypatch.setenv("LREVIEW_RESULTS_DIR", "/tmp/x")
        assert default_results_dir() == "/tmp/x"

    def test_default_db_dir_without_checkout(self, tmp_path, monkeypatch):
        from lreview.memory import default_db_dir
        monkeypatch.delenv("LREVIEW_DB", raising=False)
        # a repo_root that is not a git checkout (pip install in CI)
        # falls back to the cwd-relative directory
        assert default_db_dir(tmp_path) == Path("lreview-db")
        (tmp_path / ".git").mkdir()
        assert default_db_dir(tmp_path) == tmp_path / "lreview-db"

    def test_agent_selection(self, monkeypatch):
        monkeypatch.setenv("LREVIEW_AGENT", "codex")
        args = build_parser().parse_args(["run", "1"])
        assert args.agent == "codex"
        args = build_parser().parse_args(["run", "1", "--agent", "gemini"])
        assert args.agent == "gemini"
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "1", "--agent", "cursor"])

    def test_effort_flag_and_env(self, monkeypatch):
        monkeypatch.setenv("LREVIEW_EFFORT", "high")
        args = build_parser().parse_args(["run", "1"])
        assert args.effort == "high"
        args = build_parser().parse_args(["run", "1", "--effort", "max"])
        assert args.effort == "max"
        args = build_parser().parse_args(["run", "1", "--effort", "ultra"])
        assert args.effort == "ultra"
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "1", "--effort", "turbo"])

    def test_resolve_model(self, monkeypatch):
        from lreview.cli import resolve_model
        monkeypatch.delenv("LREVIEW_MODEL", raising=False)
        assert resolve_model("claude") == "opus"
        assert resolve_model("codex") == "gpt-6.1-sol"
        assert resolve_model("gemini") is None
        assert resolve_model("claude", "fable") == "fable"
        monkeypatch.setenv("LREVIEW_MODEL", "sonnet")
        assert resolve_model("claude") == "sonnet"
        assert resolve_model("claude", "fable") == "fable"

    def test_resolve_model_expands_codex_aliases(self, monkeypatch):
        from lreview.cli import resolve_model
        monkeypatch.delenv("LREVIEW_MODEL", raising=False)
        assert resolve_model("codex", "sol") == "gpt-6.1-sol"
        assert resolve_model("codex", "gpt-6") == "gpt-6-astra"
        # unknown names reach the CLI untouched
        assert resolve_model("codex", "gpt-7-nova") == "gpt-7-nova"
        monkeypatch.setenv("LREVIEW_MODEL", "luna")
        assert resolve_model("codex") == "gpt-5.6-luna"

    def test_env_model_of_another_agent_is_not_sent(self, monkeypatch):
        """One $LREVIEW_MODEL serves every agent; a name another
        agent's catalog lists falls back to this agent's default."""
        from lreview.cli import resolve_model
        monkeypatch.setenv("LREVIEW_MODEL", "opus")
        assert resolve_model("codex") == "gpt-6.1-sol"
        assert resolve_model("gemini") is None
        assert resolve_model("claude") == "opus"
        monkeypatch.setenv("LREVIEW_MODEL", "sol")
        assert resolve_model("claude") == "opus"
        assert resolve_model("opencode") is None
        assert resolve_model("codex") == "gpt-6.1-sol"
        # names in no catalog reach every agent untouched
        monkeypatch.setenv("LREVIEW_MODEL", "gpt-7-nova")
        assert resolve_model("codex") == "gpt-7-nova"
        assert resolve_model("claude") == "gpt-7-nova"
        # an explicit --model is never second-guessed
        assert resolve_model("codex", "opus") == "opus"

    def test_check_selection_rejects_an_impossible_pair(self, capsys):
        from lreview.cli import check_selection, resolve_model
        args = build_parser().parse_args(
            ["run", "1", "--agent", "codex", "--model", "luna",
             "--effort", "ultra"])
        assert check_selection(
            args, resolve_model(args.agent, args.model)) is False
        assert "gpt-5.6-luna" in capsys.readouterr().out

        args = build_parser().parse_args(
            ["run", "1", "--agent", "codex", "--model", "sol",
             "--effort", "ultra"])
        assert check_selection(
            args, resolve_model(args.agent, args.model)) is True

    def test_check_selection_notes_agents_without_effort(self, capsys):
        from lreview.cli import check_selection, resolve_model
        args = build_parser().parse_args(
            ["run", "1", "--agent", "gemini", "--effort", "high"])
        assert check_selection(
            args, resolve_model(args.agent, args.model)) is True
        assert "not supported for 'gemini'" in capsys.readouterr().out

    def test_run_options(self):
        args = build_parser().parse_args([
            "run", "64086", "64087",
            "--jobs", "8", "--post", "--prefix", "[Marc Bot]",
            "--model", "opus", "--agent-arg=--max-turns",
            "--claude-arg=80",  # legacy alias, same destination
        ])
        assert args.changes == ["64086", "64087"]
        assert args.jobs == 8
        assert args.post is True
        assert args.prefix == "[Marc Bot]"
        assert args.agent_arg == ["--max-turns", "80"]

    def test_prefix_env_default(self, monkeypatch):
        monkeypatch.setenv("LREVIEW_PREFIX", "[Env Bot]")
        args = build_parser().parse_args(["run", "1"])
        assert args.prefix == "[Env Bot]"

    def test_post_defaults(self, monkeypatch):
        monkeypatch.delenv("LREVIEW_PREFIX", raising=False)
        monkeypatch.delenv("LREVIEW_RESULTS_DIR", raising=False)
        args = build_parser().parse_args(["post"])
        assert args.changes == []
        assert args.force is False
        from lreview.cli import default_results_dir
        assert args.results_dir == default_results_dir()

    def test_prompts_freshness_modes(self, tmp_path, monkeypatch,
                                     capsys):
        """auto (default) fast-forwards transparently; warn only
        reports in non-tty; off does nothing; `check` never
        updates."""
        from lreview import cli as cli_mod
        calls = []
        monkeypatch.setattr(
            "lreview.prompts.prompts_freshness",
            lambda d: (2, tmp_path, "origin/lustre-dev"))
        monkeypatch.setattr(
            "lreview.prompts.update_prompts_checkout",
            lambda root, ref, submodule_note=False: (
                calls.append((root, ref)) or (True, "review prompts "
                                              "updated to abc s")))
        monkeypatch.setattr("sys.stdin", type("T", (), {
            "isatty": staticmethod(lambda: False)})())

        monkeypatch.delenv("LREVIEW_PROMPTS_UPDATE", raising=False)
        # neutralize the maintainer identity — this test machine's
        # checkout belongs to a maintainer
        monkeypatch.setattr("lreview.cli._checkout_user_email",
                            lambda: "someone@example.com")
        cli_mod.check_prompts_freshness(tmp_path / "kernel")
        assert calls == [(tmp_path, "origin/lustre-dev")]
        assert "updated to" in capsys.readouterr().out

        calls.clear()
        monkeypatch.setenv("LREVIEW_PROMPTS_UPDATE", "warn")
        cli_mod.check_prompts_freshness(tmp_path / "kernel")
        assert calls == []  # non-tty warn: report only
        assert "2 commit(s) behind" in capsys.readouterr().out

        monkeypatch.setenv("LREVIEW_PROMPTS_UPDATE", "off")
        cli_mod.check_prompts_freshness(tmp_path / "kernel")
        assert capsys.readouterr().out == ""

        monkeypatch.delenv("LREVIEW_PROMPTS_UPDATE", raising=False)
        calls.clear()
        cli_mod.check_prompts_freshness(tmp_path / "kernel",
                                        allow_update=False)
        assert calls == []  # `check` reports, never mutates
        assert "behind" in capsys.readouterr().out

    def test_prompts_update_mode_by_identity(self, monkeypatch):
        """Maintainers (by the checkout's git identity) get warn;
        everyone else auto; the env var overrides both."""
        from lreview.cli import prompts_update_mode
        monkeypatch.delenv("LREVIEW_PROMPTS_UPDATE", raising=False)
        monkeypatch.setattr("lreview.cli._checkout_user_email",
                            lambda: "mvef@whamcloud.com")
        assert prompts_update_mode() == "warn"
        monkeypatch.setattr("lreview.cli._checkout_user_email",
                            lambda: "colleague@example.com")
        assert prompts_update_mode() == "auto"
        monkeypatch.setattr("lreview.cli._checkout_user_email",
                            lambda: None)  # no git / no identity
        assert prompts_update_mode() == "auto"
        monkeypatch.setenv("LREVIEW_PROMPTS_UPDATE", "off")
        monkeypatch.setattr("lreview.cli._checkout_user_email",
                            lambda: "mvef@whamcloud.com")
        assert prompts_update_mode() == "off"

    def test_check_parses(self):
        args = build_parser().parse_args(["check"])
        assert args.func.__name__ == "cmd_check"

    def test_command_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_local_flag(self):
        args = build_parser().parse_args(["run", "--local"])
        assert args.local is True
        assert args.changes == []
        args = build_parser().parse_args(
            ["run", "--local", "branch1", "branch2"])
        assert args.changes == ["branch1", "branch2"]

    def test_run_no_changes_reviews_head_in_place(self, tmp_path,
                                                  monkeypatch, capsys):
        """`lreview run --repo X` alone reviews X's HEAD in place."""
        import subprocess
        from pathlib import Path
        from lreview.cli import cmd_run

        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in (["git", "init", "-q"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-q", "--allow-empty", "-m", "top subject"]):
            subprocess.run(cmd, cwd=repo, check=True)

        captured = {}

        def fake_run_batch(config, changes, in_place=False):
            captured["changes"] = changes
            captured["in_place"] = in_place
            return []

        from lreview.prompts import PromptsStatus
        monkeypatch.setattr(
            "lreview.cli.ensure_prompts",
            lambda args: PromptsStatus(
                available=True, prompts_dir=Path("/p/kernel"),
                source="test"))
        monkeypatch.setattr("lreview.cli.run_batch", fake_run_batch)

        args = build_parser().parse_args(["run", "--repo", str(repo)])
        rc = cmd_run(args)
        assert rc == 0
        assert captured["in_place"] is True
        assert len(captured["changes"]) == 1
        assert captured["changes"][0].ref_name == "HEAD"
        assert captured["changes"][0].subject == "top subject"
        assert "(in place)" in capsys.readouterr().out

    def test_github_already_posted_note_names_the_pr(self, tmp_path,
                                                     monkeypatch, capsys):
        import json
        import subprocess
        from lreview.cli import cmd_run
        from lreview.github import ResolvedGitHubPullRequest
        from lreview.prompts import PromptsStatus

        subprocess.run(["git", "init", "-q", str(tmp_path / "repo")],
                       check=True)
        pr = ResolvedGitHubPullRequest(
            "acme", "widget", 7, "Fix", "b" * 40, "a" * 40, "fix",
            "acme/widget", "https://github.com/acme/widget/pull/7")
        results = tmp_path / "results"
        results.mkdir()
        (results / "summary.json").write_text(json.dumps({
            "github:acme/widget#7": {"posted": True, "sha": "b" * 40}}))
        monkeypatch.setattr("lreview.github.resolve_pull_request",
                            lambda url: pr)
        monkeypatch.setattr(
            "lreview.cli.ensure_prompts",
            lambda args: PromptsStatus(
                available=True, prompts_dir=tmp_path, source="test"))
        monkeypatch.setattr("lreview.cli.run_batch",
                            lambda config, changes, in_place=False: [])

        args = build_parser().parse_args([
            "run", "--github", pr.url, "--repo", str(tmp_path / "repo"),
            "--results-dir", str(results)])
        assert cmd_run(args) == 0
        out = capsys.readouterr().out
        assert (f"note: acme/widget#7 at {'b' * 12} was already posted"
                in out)
        assert "psNone" not in out

    def test_jobs_must_be_positive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "1", "--jobs", "0"])
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "1", "--jobs", "-3"])


class TestVersion:

    def test_version_is_the_installed_distributions(self, capsys):
        from importlib.metadata import version
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out == f"lreview {version('lreview')}\n"

    def test_version_without_an_installed_distribution(self, monkeypatch):
        import importlib
        import importlib.metadata
        import lreview

        def not_installed(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", not_installed)
        try:
            assert importlib.reload(lreview).__version__ == "unknown"
        finally:
            monkeypatch.undo()
            importlib.reload(lreview)


class TestCmdPost:

    def test_post_accepts_urls(self, tmp_path, capsys):
        """A Gerrit URL as change spec resolves to its number."""
        from lreview.cli import cmd_post
        import argparse as ap
        import json

        results = tmp_path / "results"
        results.mkdir()
        (results / "summary.json").write_text(json.dumps({}))

        args = ap.Namespace(
            results_dir=str(results),
            changes=["https://review.whamcloud.com/c/fs/lustre-release/+/64086"],
            prefix=None, force=False)
        rc = cmd_post(args)
        # 64086 not in the (empty) manifest -> clean error, no traceback
        assert rc == 1
        out = capsys.readouterr().out
        assert "64086" in out
        assert "not found" in out

    def test_post_rejects_garbage_spec(self, tmp_path, capsys):
        from lreview.cli import cmd_post
        import argparse as ap

        args = ap.Namespace(
            results_dir=str(tmp_path), changes=["not-a-change"],
            prefix=None, force=False)
        rc = cmd_post(args)
        assert rc == 1
        assert "not a change number" in capsys.readouterr().out

    def test_dry_run_lists_only_the_named_change(self, tmp_path, capsys):
        """A dry run shows what `post 64086` would send, not every
        entry the shared results dir has accumulated."""
        from lreview.cli import cmd_post
        import argparse as ap
        import json

        results = tmp_path / "results"
        results.mkdir()
        spec = {"message": "m", "comments": {"a.c": [{"line": 1,
                                                      "message": "x"}]}}
        summary = {}
        for number in (64086, 64087):
            name = f"gerrit-review-{number}_ps1.json"
            (results / name).write_text(json.dumps(spec))
            summary[str(number)] = {
                "number": number, "patchset": 1, "sha": "a" * 40,
                "subject": "s", "base_url": "https://gerrit.invalid",
                "status": "findings", "findings": 1, "model": "opus",
                "agent": "claude", "json": name, "error": None,
                "posted": False,
            }
        (results / "summary.json").write_text(json.dumps(summary))

        args = ap.Namespace(
            results_dir=str(results), changes=["64086"],
            prefix=None, force=False, dry_run=True)
        assert cmd_post(args) == 0
        out = capsys.readouterr().out
        assert "64086" in out
        assert "64087" not in out
        assert "would post" in out


class TestDefaultWorktreesDir:

    def test_prefers_ai_worktrees_sibling(self, tmp_path):
        repo = tmp_path / "ws" / "repo"
        repo.mkdir(parents=True)
        (tmp_path / "ws" / "ai_worktrees").mkdir()
        result = default_worktrees_dir(repo, tmp_path / "results")
        assert result == tmp_path / "ws" / "ai_worktrees" / "lreview"

    def test_falls_back_to_results_dir(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        results = tmp_path / "results"
        assert default_worktrees_dir(repo, results) == results / "worktrees"


class TestLastAndOutput:
    """--last N (newest N commits of --repo) and the text dump."""

    def test_parses(self):
        args = build_parser().parse_args(
            ["run", "--last", "3", "-o", "/tmp/dump.txt"])
        assert args.last == 3
        assert args.output == "/tmp/dump.txt"
        args = build_parser().parse_args(["run", "-n", "2"])
        assert args.last == 2
        assert args.output is None

    def test_default_none(self):
        args = build_parser().parse_args(["run", "64086"])
        assert args.last is None
        assert args.output is None

    def test_rejects_zero(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "--last", "0"])

    def test_dump_path(self, tmp_path):
        from lreview.cli import text_dump_path
        args = build_parser().parse_args(["run", "--last", "4"])
        assert text_dump_path(args, tmp_path) == \
            tmp_path / "review-last4.txt"
        args = build_parser().parse_args(["run", "--last", "4", "-o", "x.txt"])
        assert text_dump_path(args, tmp_path) == Path("x.txt")
        # No --last and no --output: no dump
        args = build_parser().parse_args(["run", "64086"])
        assert text_dump_path(args, tmp_path) is None
        # --output alone still writes one, Gerrit batch or not
        args = build_parser().parse_args(["run", "64086", "-o", "x.txt"])
        assert text_dump_path(args, tmp_path) == Path("x.txt")

    def test_last_rejects_change_arguments(self, tmp_path, capsys):
        import subprocess
        from lreview.cli import cmd_run
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        args = build_parser().parse_args(
            ["run", "--last", "2", "--repo", str(tmp_path), "64086"])
        assert cmd_run(args) == 1
        assert "takes no change arguments" in capsys.readouterr().out

    def test_last_more_than_history(self, tmp_path, capsys, monkeypatch):
        import subprocess
        from lreview.cli import cmd_run
        monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "f").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "one"],
                       check=True)
        from lreview.prompts import PromptsStatus
        monkeypatch.setattr(
            "lreview.cli.ensure_prompts",
            lambda args: PromptsStatus(
                available=True, prompts_dir=tmp_path, source="test"))
        args = build_parser().parse_args(
            ["run", "--last", "5", "--repo", str(tmp_path)])
        assert cmd_run(args) == 1
        assert "has only 1 commit(s)" in capsys.readouterr().out


class TestNoResume:
    """--no-resume: with --memory, start a fresh Claude session instead
    of resuming the recorded one."""

    def _run(self, tmp_path, monkeypatch, *argv):
        import subprocess
        from pathlib import Path
        from lreview.cli import cmd_run
        from lreview.prompts import PromptsStatus

        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in (["git", "init", "-q"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-q", "--allow-empty", "-m", "top"]):
            subprocess.run(cmd, cwd=repo, check=True)
        captured = {}

        def fake_run_batch(config, changes, in_place=False):
            captured["config"] = config
            return []

        monkeypatch.setattr(
            "lreview.cli.ensure_prompts",
            lambda args: PromptsStatus(
                available=True, prompts_dir=Path("/p/kernel"),
                source="test"))
        monkeypatch.setattr("lreview.cli.run_batch", fake_run_batch)
        args = build_parser().parse_args(
            ["run", "--repo", str(repo), "--db", str(tmp_path / "db"),
             *argv])
        return cmd_run(args), captured.get("config")

    def test_memory_resumes_by_default(self, tmp_path, monkeypatch):
        rc, config = self._run(tmp_path, monkeypatch, "-m")
        assert rc == 0
        assert config.resume is True

    def test_no_resume_starts_fresh(self, tmp_path, monkeypatch):
        rc, config = self._run(tmp_path, monkeypatch, "-m", "--no-resume")
        assert rc == 0
        assert config.resume is False

    def test_no_resume_needs_memory(self, tmp_path, monkeypatch, capsys):
        rc, config = self._run(tmp_path, monkeypatch, "--no-resume")
        assert rc == 1
        assert config is None
        assert "--no-resume requires --memory" in capsys.readouterr().out


class TestRunPost:
    """`run --post` posts every reviewed change of its own batch,
    clean ones included, and says why anything was not posted."""

    def _run(self, tmp_path, monkeypatch, statuses):
        import subprocess
        from lreview.cli import cmd_run
        from lreview.gerrit import ResolvedChange, change_ref
        from lreview.prompts import PromptsStatus
        from lreview.runner import ReviewResult

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        changes = {
            n: ResolvedChange(
                number=n, project="fs/lustre-release", subject=f"s{n}",
                sha=str(n)[0] * 40, patchset=2, ref=change_ref(n, 2),
                base_url="https://gerrit.invalid")
            for n in statuses}
        posted = {}
        monkeypatch.setenv("LREVIEW_PROMPTS_UPDATE", "off")
        monkeypatch.setattr("lreview.cli.resolve_change",
                            lambda spec: changes[int(spec)])
        monkeypatch.setattr(
            "lreview.cli.ensure_prompts",
            lambda args: PromptsStatus(
                available=True, prompts_dir=Path("/p/kernel"),
                source="test"))
        monkeypatch.setattr(
            "lreview.cli.run_batch",
            lambda config, chs, in_place=False: [
                ReviewResult(c, statuses[c.number],
                             error=("claude exited 1"
                                    if statuses[c.number] == "failed"
                                    else None))
                for c in chs])

        def fake_post(results_dir, changes=None, prefix=None):
            from lreview.poster import PostOutcome
            posted["changes"] = changes
            return [PostOutcome(int(k), "posted", "ok") for k in changes]

        monkeypatch.setattr("lreview.cli.post_results", fake_post)
        args = build_parser().parse_args(
            ["run", "--repo", str(repo), "--post",
             "--results-dir", str(tmp_path / "results"),
             *[str(n) for n in statuses]])
        return cmd_run(args), posted

    def test_clean_review_is_posted(self, tmp_path, monkeypatch, capsys):
        rc, posted = self._run(tmp_path, monkeypatch, {69459: "clean"})
        assert rc == 0
        assert posted["changes"] == ["69459"]
        assert "Posting results" in capsys.readouterr().out

    def test_failed_review_says_not_posted(self, tmp_path, monkeypatch,
                                           capsys):
        rc, posted = self._run(tmp_path, monkeypatch,
                               {69459: "clean", 70001: "failed"})
        out = capsys.readouterr().out
        assert posted["changes"] == ["69459"]
        assert "not posted: 70001_ps2 failed — claude exited 1" in out


class TestRunSeries:

    def _run(self, tmp_path, monkeypatch, argv, children, status=None,
             skipped=None):
        import subprocess
        from lreview.cli import cmd_run
        from lreview.gerrit import ResolvedChange, SeriesChildren, change_ref
        from lreview.prompts import PromptsStatus
        status = status or {}
        skipped = skipped or {}

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        reviewed = {}

        def resolve(spec):
            n = int(str(spec).rstrip("/").rsplit("/", 1)[-1])
            return ResolvedChange(
                number=n, project="ex/lustre-release", subject=f"s{n}",
                sha=f"{n:040d}", patchset=1, ref=change_ref(n, 1),
                base_url="https://gerrit.invalid",
                status=status.get(n, "NEW"))

        monkeypatch.setenv("LREVIEW_PROMPTS_UPDATE", "off")
        monkeypatch.setattr("lreview.cli.resolve_change", resolve)
        monkeypatch.setattr(
            "lreview.gerrit.series_children",
            lambda change: SeriesChildren(
                open=children.get(change.number, []),
                skipped=skipped.get(change.number, [])))
        monkeypatch.setattr(
            "lreview.cli.ensure_prompts",
            lambda args: PromptsStatus(
                available=True, prompts_dir=Path("/p/kernel"),
                source="test"))

        def fake_run_batch(config, changes, in_place=False):
            reviewed["numbers"] = [c.number for c in changes]
            return []

        monkeypatch.setattr("lreview.cli.run_batch", fake_run_batch)
        args = build_parser().parse_args(
            ["run", "--repo", str(repo),
             "--results-dir", str(tmp_path / "results"), *argv])
        return cmd_run(args), reviewed

    def test_expands_to_the_children(self, tmp_path, monkeypatch, capsys):
        rc, reviewed = self._run(
            tmp_path, monkeypatch,
            ["--series", "https://review.whamcloud.com/c/ex/"
             "lustre-release/+/65382"],
            {65382: [66955, 66956, 66957]})
        assert rc == 0
        assert reviewed["numbers"] == [65382, 66955, 66956, 66957]
        assert ("series of 65382: itself + 3 in-flight child change(s)"
                in capsys.readouterr().out)

    def test_skipped_children_are_named(self, tmp_path, monkeypatch,
                                        capsys):
        rc, reviewed = self._run(
            tmp_path, monkeypatch, ["--series", "100"], {100: [101]},
            skipped={100: [(102, "abandoned"), (103, "merged")]})
        out = capsys.readouterr().out
        assert reviewed["numbers"] == [100, 101]
        assert "skipped 102: abandoned" in out
        assert "skipped 103: merged" in out

    def test_merged_base_reviews_only_its_children(self, tmp_path,
                                                   monkeypatch, capsys):
        rc, reviewed = self._run(
            tmp_path, monkeypatch, ["--series", "100"], {100: [101, 102]},
            status={100: "MERGED"})
        assert reviewed["numbers"] == [101, 102]
        assert ("100 itself is merged, not reviewed"
                in capsys.readouterr().out)

    def test_nothing_in_flight(self, tmp_path, monkeypatch, capsys):
        rc, reviewed = self._run(
            tmp_path, monkeypatch, ["--series", "100"], {100: []},
            status={100: "ABANDONED"})
        assert rc == 0
        assert "numbers" not in reviewed
        assert "nothing in flight to review" in capsys.readouterr().out

    def test_overlapping_series_reviewed_once(self, tmp_path, monkeypatch,
                                              capsys):
        rc, reviewed = self._run(
            tmp_path, monkeypatch, ["--series", "100", "101"],
            {100: [101, 102], 101: [102]})
        assert reviewed["numbers"] == [100, 101, 102]
        assert "given more than once" not in capsys.readouterr().out

    def test_without_flag_no_expansion(self, tmp_path, monkeypatch):
        rc, reviewed = self._run(tmp_path, monkeypatch, ["100"],
                                 {100: [101, 102]})
        assert reviewed["numbers"] == [100]

    @pytest.mark.parametrize("argv", [
        ["--series"],
        ["--series", "--local", "branch"],
        ["--series", "--last", "2"],
    ])
    def test_rejected_combinations(self, tmp_path, monkeypatch, capsys,
                                   argv):
        rc, reviewed = self._run(tmp_path, monkeypatch, argv, {})
        assert rc == 1
        assert "--series expands Gerrit changes" in capsys.readouterr().out
        assert "numbers" not in reviewed


class TestRunDryRun:
    """--dry-run resolves and shows the batch, then stops before
    anything is fetched, reviewed, posted, cleared or updated."""

    _run = TestRunSeries._run

    def test_series_plan_without_running(self, tmp_path, monkeypatch,
                                         capsys):
        freshness = {}
        monkeypatch.setattr(
            "lreview.cli.check_prompts_freshness",
            lambda d, allow_update=True: freshness.update(
                allow_update=allow_update))
        rc, reviewed = self._run(
            tmp_path, monkeypatch,
            ["--dry-run", "--series", "--post", "--prefix",
             "[Bot - <model>]", "65382"],
            {65382: [66955, 66956]})
        out = capsys.readouterr().out
        assert rc == 0
        assert "numbers" not in reviewed  # run_batch never called
        assert freshness == {"allow_update": False}
        assert "model opus" in out
        assert "as '[Bot - opus]'" in out
        assert "dry run: 3 change(s) would be reviewed" in out

    def test_clear_memory_is_not_applied(self, tmp_path, monkeypatch,
                                         capsys):
        from lreview.gerrit import ResolvedChange, change_ref
        from lreview.memory import ensure_doc
        db = tmp_path / "db"
        doc = ensure_doc(db, ResolvedChange(
            number=100, project="ex/lustre-release", subject="s100",
            sha=f"{100:040d}", patchset=1, ref=change_ref(100, 1),
            base_url="https://gerrit.invalid"))
        rc, reviewed = self._run(
            tmp_path, monkeypatch,
            ["--dry-run", "-m", "-c", "--db", str(db), "100"], {})
        out = capsys.readouterr().out
        assert rc == 0
        assert doc.is_file()
        assert f"would clear memory: {doc}" in out
        assert "0 existing document(s), 1 new" in out
