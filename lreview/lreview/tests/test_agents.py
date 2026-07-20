"""Tests for the agent backends."""

import pytest

from lreview.agents import AGENTS, get_agent


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

    def test_commands_dirs_match_review_prompts_setup(self):
        # Paths from review-prompts/agents/*.sh
        assert AGENTS["claude"].commands_subdir == ".claude/commands"
        assert AGENTS["codex"].commands_subdir == ".codex/prompts"
        assert AGENTS["gemini"].commands_subdir == ".gemini/commands"
        assert AGENTS["opencode"].commands_subdir == ".opencode/commands"


class TestBuildCmd:

    def test_claude_uses_slash_command(self):
        cmd = get_agent("claude").build_cmd("opus", ["--extra"], "ignored")
        assert cmd == ["claude", "-p", "/kreview",
                       "--dangerously-skip-permissions",
                       "--verbose", "--output-format", "stream-json",
                       "--model", "opus", "--extra"]

    def test_codex_gets_prompt_text(self):
        cmd = get_agent("codex").build_cmd(None, [], "PROMPT TEXT")
        assert cmd == ["codex", "exec",
                       "--dangerously-bypass-approvals-and-sandbox",
                       "PROMPT TEXT"]
        cmd = get_agent("codex").build_cmd("gpt-5", [], "P")
        assert cmd[2:5] == ["--dangerously-bypass-approvals-and-sandbox",
                            "-m", "gpt-5"]

    def test_gemini_gets_prompt_text(self):
        cmd = get_agent("gemini").build_cmd(None, [], "PROMPT")
        assert cmd == ["gemini", "--yolo", "-p", "PROMPT"]

    def test_opencode_gets_prompt_text(self):
        cmd = get_agent("opencode").build_cmd(
            "anthropic/claude-sonnet-5", [], "PROMPT")
        assert cmd == ["opencode", "run",
                       "--model", "anthropic/claude-sonnet-5", "PROMPT"]
