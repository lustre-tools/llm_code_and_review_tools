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
