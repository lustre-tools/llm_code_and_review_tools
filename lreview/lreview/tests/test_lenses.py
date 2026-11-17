"""Tests for --lenses: the lens agent, TASK 1D and what a review gets."""

import json
from pathlib import Path

import pytest

from lreview.lenses import (LENS_REVIEW_TOOLS, LENS_AGENT, LENS_PROMPT_PATH,
                            LENSES, TASK_2_HEADING, agents_json, lens_files,
                            task_text, with_task)
from lreview.runner import (BatchConfig, _remove_artifacts, build_agent_cmd,
                            lens_label, preload_file, review_prompt)

PROTOCOL = ("# Protocol\n\n### TASK 1C: CHANGE category printing\n\nx\n\n"
            + TASK_2_HEADING + "\n\ny\n")


def _prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    (d / "subsystem").mkdir(parents=True, exist_ok=True)
    for name in ("technical-patterns.md", "lustre-commit-message.md",
                 "lustre-style.md", "subsystem/build.md",
                 "subsystem/subsystem.md"):
        (d / name).write_text(f"{name}\n")
    (d / "review-core.md").write_text(PROTOCOL)
    return d


def _config(tmp_path: Path, **kwargs) -> BatchConfig:
    kwargs.setdefault("prompts_dir", _prompts(tmp_path))
    (tmp_path / "results").mkdir(exist_ok=True)
    return BatchConfig(repo=tmp_path, results_dir=tmp_path / "results",
                       worktrees_dir=tmp_path / "worktrees", **kwargs)


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class TestLensPrompt:

    def test_ships_with_a_section_per_lens(self):
        text = LENS_PROMPT_PATH.read_text()
        for lens in LENSES:
            assert f"### {lens}" in text
        assert "<output directory>/LENS-<lens>.json" in text

    def test_task_names_every_lens_and_the_paths(self, tmp_path):
        text = task_text(tmp_path / "p")
        assert text.startswith("### TASK 1D")
        assert "launch five specialist subagents" in text
        assert len(LENSES) == 5
        for lens in LENSES:
            assert f"`{lens}`" in text
        assert f"Read {LENS_PROMPT_PATH} and follow it." in text
        assert f"Prompt directory: {tmp_path / 'p'}" in text
        assert f'subagent_type: "{LENS_AGENT}"' in text
        assert "Output directory: <current directory>/review-context" in text
        assert "absolute paths" in text

    def test_task_sets_how_candidates_are_settled(self, tmp_path):
        text = task_text(tmp_path / "p")
        assert "Eliminate a candidate only by pointing at what prevents it" in text
        assert "git show HEAD^:<file>" in text
        assert "report it as `(minor)`" in text
        assert "`lustre/tests/`" in text
        assert "LENS DISPOSITIONS:" in text
        assert text.index("LENS DISPOSITIONS:") < text.index("LENSES: <N>")

    def test_lens_says_whether_the_patch_introduced_it(self):
        text = LENS_PROMPT_PATH.read_text()
        assert "git show HEAD^:<file>" in text
        assert '"introduced"' in text

    def test_task_goes_in_front_of_task_2(self, tmp_path):
        out = with_task(PROTOCOL, tmp_path)
        assert out.index("### TASK 1D") < out.index(TASK_2_HEADING)
        assert out.index("### TASK 1C") < out.index("### TASK 1D")
        assert out.count(TASK_2_HEADING) == 1

    def test_task_is_appended_when_task_2_moved(self, tmp_path):
        out = with_task("# Protocol\n\nno such heading\n", tmp_path)
        assert out.startswith("# Protocol")
        assert "### TASK 1D" in out


class TestLensAgent:

    def test_model_effort_and_tools(self):
        spec = json.loads(agents_json("sonnet", "medium"))[LENS_AGENT]
        assert spec["model"] == "sonnet" and spec["effort"] == "medium"
        assert spec["tools"] == ["Bash", "Read", "Write", "Grep", "Glob"]
        assert spec["description"] and spec["prompt"]

    def test_unset_model_and_effort_are_left_out(self):
        spec = json.loads(agents_json(None, None))[LENS_AGENT]
        assert "model" not in spec and "effort" not in spec


class TestReviewCommand:

    def test_lean_review_gets_the_agent_tool_and_the_lens_agent(
            self, tmp_path):
        config = _config(tmp_path, model="sonnet", lean=True, preload=True,
                         lenses=True, lens_effort="medium")
        cmd = build_agent_cmd(config)
        tools = _arg(cmd, "--tools").split(",")
        assert set(LENS_REVIEW_TOOLS) <= set(tools) and "Bash" in tools
        spec = json.loads(_arg(cmd, "--agents"))[LENS_AGENT]
        assert spec["model"] == "sonnet" and spec["effort"] == "medium"

    def test_lens_model_overrides_the_review_model(self, tmp_path):
        config = _config(tmp_path, model="opus", lenses=True,
                         lens_model="sonnet", lens_effort="high")
        spec = json.loads(_arg(build_agent_cmd(config), "--agents"))
        assert spec[LENS_AGENT]["model"] == "sonnet"
        assert lens_label(config) == "sonnet/high"

    def test_plain_review_has_neither(self, tmp_path):
        config = _config(tmp_path, lean=True, preload=True)
        cmd = build_agent_cmd(config)
        assert "Agent" not in _arg(cmd, "--tools").split(",")
        assert "--agents" not in cmd
        assert lens_label(config) is None

    def test_allowlist_review_may_use_the_agent_tool(self, tmp_path):
        config = _config(tmp_path, lenses=True, lens_effort="medium",
                         allowed_tools=["Read", "Bash(git *)"])
        allowed = _arg(build_agent_cmd(config), "--allowedTools").split(",")
        assert allowed[:2] == ["Read", "Bash(git *)"]
        assert set(LENS_REVIEW_TOOLS) <= set(allowed)

    def test_preload_carries_task_1d(self, tmp_path):
        config = _config(tmp_path, preload=True, lenses=True,
                         lens_effort="medium")
        text = preload_file(config).read_text()
        assert text.index("### TASK 1D") < text.index(TASK_2_HEADING)
        assert "lreview adds TASK 1D" in text
        plain = preload_file(_config(tmp_path, preload=True)).read_text()
        assert "TASK 1D" not in plain

    def test_without_preload_the_prompt_carries_it(self, tmp_path):
        config = _config(tmp_path, preload=False, lenses=True,
                         lens_effort="medium")
        prompt = review_prompt(config)
        assert "between TASK 1C and Task 2" in prompt
        assert "### TASK 1D" in prompt
        assert "task.." not in prompt


class TestValidation:

    @pytest.mark.parametrize("kwargs", [{"agent": "codex"},
                                        {"mode": "light"}])
    def test_claude_full_mode_only(self, tmp_path, kwargs):
        with pytest.raises(ValueError, match="--lenses needs"):
            _config(tmp_path, lenses=True, **kwargs)

    def test_lens_effort_is_checked(self, tmp_path):
        with pytest.raises(ValueError, match="ultra"):
            _config(tmp_path, lenses=True, lens_effort="ultra")


class TestInPlaceArtifacts:

    def test_lens_files_go_and_the_rest_stays(self, tmp_path):
        ctx = tmp_path / "review-context"
        ctx.mkdir()
        (ctx / "LENS-data.json").write_text("{}")
        (ctx / "notes.txt").write_text("mine")
        assert [p.name for p in lens_files(tmp_path)] == ["LENS-data.json"]
        _remove_artifacts(tmp_path)
        assert not (ctx / "LENS-data.json").exists()
        assert (ctx / "notes.txt").read_text() == "mine"

    def test_an_emptied_directory_goes(self, tmp_path):
        ctx = tmp_path / "review-context"
        ctx.mkdir()
        (ctx / "LENS-build.json").write_text("{}")
        _remove_artifacts(tmp_path)
        assert not ctx.exists()


class TestCli:

    def _args(self, *argv):
        from lreview.cli import build_parser
        return build_parser().parse_args(["run", "1", *argv])

    def test_needs_claude_and_full_mode(self, capsys):
        from lreview.cli import check_lenses
        assert not check_lenses(self._args("--lenses", "--agent", "codex"))
        assert not check_lenses(self._args("--lenses", "--mode", "light"))
        assert "--lenses needs" in capsys.readouterr().out
        assert check_lenses(self._args("--lenses"))

    def test_not_for_github_reviews(self, capsys):
        from lreview.cli import build_parser, check_lenses
        args = build_parser().parse_args(
            ["run", "--lenses", "--github",
             "https://github.com/acme/widget/pull/7"])
        assert not check_lenses(args)
        assert "--github" in capsys.readouterr().out

    def test_config_file_can_turn_it_on(self, tmp_path, monkeypatch):
        from lreview.cli import build_parser
        cfg = tmp_path / "c.toml"
        cfg.write_text('[run]\nlenses = true\nlens-effort = "high"\n')
        monkeypatch.setenv("LREVIEW_CONFIG", str(cfg))
        args = build_parser().parse_args(["run", "1"])
        assert args.lenses and args.lens_effort == "high"

    def test_lens_options_need_lenses(self, capsys):
        from lreview.cli import check_lenses
        assert not check_lenses(self._args("--lens-effort", "high"))
        assert "go with --lenses" in capsys.readouterr().out

    def test_lens_effort_defaults_to_medium(self):
        from lreview.cli import lens_effort
        assert lens_effort(self._args("--lenses")) == "medium"
        assert lens_effort(self._args("--lenses", "--lens-effort",
                                      "high")) == "high"
        assert lens_effort(self._args()) is None

    def test_bench_label_says_lenses(self):
        from lreview.bench import default_label
        from lreview.cli import build_parser
        args = build_parser().parse_args(["bench", "run", "--lenses"])
        assert "lenses" in default_label(args)
