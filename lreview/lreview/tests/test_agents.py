"""Tests for the agent backends."""

import pytest

from lreview.agents import AGENTS, get_agent

PROMPT = ("Using the prompt /p/review-core.md run a deep dive "
          "regression analysis of the top commit")


class TestRegistry:

    def test_supported_agents(self):
        assert set(AGENTS) == {"claude", "codex", "gemini", "opencode"}
        assert AGENTS["claude"].stream_json is True
        assert AGENTS["claude"].verified is True
        for name in ("codex", "gemini", "opencode"):
            assert AGENTS[name].stream_json is False
            assert AGENTS[name].verified is False

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError, match="unknown agent"):
            get_agent("cursor")


class TestBuildCmd:

    def test_claude_prompt_and_stream_json(self):
        cmd = get_agent("claude").build_cmd(
            "opus", None, ["--extra"], PROMPT)
        assert cmd == ["claude", "-p", PROMPT,
                       "--dangerously-skip-permissions",
                       "--verbose", "--output-format", "stream-json",
                       "--model", "opus", "--extra"]

    def test_claude_effort(self):
        cmd = get_agent("claude").build_cmd("opus", "xhigh", [], PROMPT)
        assert cmd[-4:] == ["--model", "opus", "--effort", "xhigh"]

    def test_codex_gets_prompt_text_and_ignores_effort(self):
        cmd = get_agent("codex").build_cmd(None, "high", [], PROMPT)
        assert cmd == ["codex", "exec",
                       "--dangerously-bypass-approvals-and-sandbox",
                       PROMPT]
        cmd = get_agent("codex").build_cmd("gpt-5", None, [], PROMPT)
        assert cmd[2:5] == ["--dangerously-bypass-approvals-and-sandbox",
                            "-m", "gpt-5"]

    def test_gemini_gets_prompt_text(self):
        cmd = get_agent("gemini").build_cmd(None, None, [], PROMPT)
        assert cmd == ["gemini", "--yolo", "-p", PROMPT]

    def test_opencode_gets_prompt_text(self):
        cmd = get_agent("opencode").build_cmd(
            "anthropic/claude-sonnet-5", None, [], PROMPT)
        assert cmd == ["opencode", "run",
                       "--model", "anthropic/claude-sonnet-5", PROMPT]


class TestBuildInteractiveCmd:
    """Interactive argv shapes, verified against each CLI's official
    docs: claude and codex take a bare positional prompt; gemini
    needs -i (bare positional / -p are one-shot); opencode's root
    positional is a project path, so the prompt goes in --prompt."""

    def test_claude_positional_prompt(self):
        cmd = get_agent("claude").build_interactive_cmd(
            "opus", ["--add-dir", "/x"], PROMPT)
        assert cmd == ["claude", "--model", "opus",
                       "--add-dir", "/x", PROMPT]

    def test_codex_positional_prompt(self):
        cmd = get_agent("codex").build_interactive_cmd(None, [], PROMPT)
        assert cmd == ["codex", PROMPT]
        assert get_agent("codex").build_interactive_cmd(
            "gpt-5", [], PROMPT)[:3] == ["codex", "-m", "gpt-5"]

    def test_gemini_uses_prompt_interactive_flag(self):
        cmd = get_agent("gemini").build_interactive_cmd(None, [], PROMPT)
        assert cmd == ["gemini", "-i", PROMPT]

    def test_opencode_prompt_is_a_flag_not_positional(self):
        cmd = get_agent("opencode").build_interactive_cmd(
            "anthropic/claude-sonnet-5", [], PROMPT)
        assert cmd == ["opencode",
                       "--model", "anthropic/claude-sonnet-5",
                       "--prompt", PROMPT]

    def test_no_bypass_flags_in_interactive_mode(self):
        for name in ("claude", "codex", "gemini", "opencode"):
            cmd = get_agent(name).build_interactive_cmd(None, [], PROMPT)
            joined = " ".join(cmd)
            assert "yolo" not in joined
            assert "dangerously" not in joined
            assert "bypass" not in joined
