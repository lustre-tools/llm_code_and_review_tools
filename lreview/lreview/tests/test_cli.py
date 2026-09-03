"""Tests for the lreview CLI."""

from pathlib import Path

import pytest

from lreview.cli import build_parser, default_worktrees_dir


class TestParser:

    def test_run_defaults(self, monkeypatch):
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
        # editable checkout: results default into the llm tools repo,
        # not the cwd
        from lreview.prompts import _REPO_ROOT
        assert args.results_dir == str(_REPO_ROOT / "lreview-results")
        assert args.worktrees_dir is None
        assert args.keep_worktrees is False
        assert args.post is False
        assert args.prefix is None
        assert args.model is None
        assert args.agent == "claude"

    def test_default_results_dir(self, monkeypatch):
        from lreview.cli import default_results_dir
        from lreview.prompts import _REPO_ROOT
        monkeypatch.delenv("LREVIEW_RESULTS_DIR", raising=False)
        # this test runs from an editable checkout, so the repo wins
        assert default_results_dir() == str(_REPO_ROOT / "lreview-results")
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
        monkeypatch.delenv("LREVIEW_RESULTS_DIR", raising=False)
        args = build_parser().parse_args(["post"])
        assert args.changes == []
        assert args.force is False
        from lreview.cli import default_results_dir
        assert args.results_dir == default_results_dir()

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

        monkeypatch.setattr("lreview.cli.ensure_prompts",
                            lambda args: Path("/p/kernel"))
        monkeypatch.setattr("lreview.cli.run_batch", fake_run_batch)

        args = build_parser().parse_args(["run", "--repo", str(repo)])
        rc = cmd_run(args)
        assert rc == 0
        assert captured["in_place"] is True
        assert len(captured["changes"]) == 1
        assert captured["changes"][0].ref_name == "HEAD"
        assert captured["changes"][0].subject == "top subject"
        assert "(in place)" in capsys.readouterr().out

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
        monkeypatch.setattr("lreview.cli.ensure_prompts",
                            lambda args: tmp_path)
        args = build_parser().parse_args(
            ["run", "--last", "5", "--repo", str(tmp_path)])
        assert cmd_run(args) == 1
        assert "has only 1 commit(s)" in capsys.readouterr().out
