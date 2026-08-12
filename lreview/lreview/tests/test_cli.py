"""Tests for the lreview CLI."""

from pathlib import Path

import pytest

from lreview.cli import build_parser, default_worktrees_dir


class TestParser:

    def test_run_defaults(self, monkeypatch):
        monkeypatch.delenv("LREVIEW_PREFIX", raising=False)
        monkeypatch.delenv("LREVIEW_AGENT", raising=False)
        monkeypatch.delenv("LREVIEW_EFFORT", raising=False)
        args = build_parser().parse_args(["run", "64086"])
        assert args.effort is None
        assert args.changes == ["64086"]
        assert args.jobs == 5
        assert args.timeout == 7200
        assert args.repo == "."
        assert args.results_dir == "./lreview-results"
        assert args.worktrees_dir is None
        assert args.keep_worktrees is False
        assert args.post is False
        assert args.prefix is None
        assert args.model is None
        assert args.agent == "claude"

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
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "1", "--effort", "turbo"])

    def test_resolve_model(self, monkeypatch):
        from lreview.cli import resolve_model
        monkeypatch.delenv("LREVIEW_MODEL", raising=False)
        assert resolve_model("claude") == "opus"
        assert resolve_model("codex") is None
        assert resolve_model("claude", "fable") == "fable"
        monkeypatch.setenv("LREVIEW_MODEL", "sonnet")
        assert resolve_model("claude") == "sonnet"
        assert resolve_model("claude", "fable") == "fable"

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
        args = build_parser().parse_args(["post"])
        assert args.changes == []
        assert args.force is False
        assert args.results_dir == "./lreview-results"

    def test_check_parses(self):
        args = build_parser().parse_args(["check"])
        assert args.func.__name__ == "cmd_check"

    def test_command_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_jobs_must_be_positive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "1", "--jobs", "0"])
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "1", "--jobs", "-3"])


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
