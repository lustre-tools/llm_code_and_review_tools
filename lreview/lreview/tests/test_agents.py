"""Tests for the agent backends."""

import json
import os

import pytest

from lreview.agents import AGENTS, get_agent
from lreview.agents import enabled_plugins as _REAL_ENABLED_PLUGINS

PROMPT = ("Using the prompt /p/review-core.md run a deep dive "
          "regression analysis of the top commit")


class TestRegistry:

    def test_supported_agents(self):
        assert set(AGENTS) == {"claude", "codex", "gemini", "opencode",
                               "auggie", "cursor", "agy"}
        assert AGENTS["auggie"].stream_json is True
        assert AGENTS["auggie"].verified is False
        for name in ("claude", "codex", "cursor", "agy"):
            assert AGENTS[name].stream_json is True
            assert AGENTS[name].verified is True
        for name in ("gemini", "opencode"):
            assert AGENTS[name].stream_json is False
            assert AGENTS[name].verified is False

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError, match="unknown agent"):
            get_agent("windsurf")


class TestBuildCmd:

    def test_auggie_print_and_billing(self):
        cmd = get_agent("auggie").build_cmd("model-id", "high", ["--extra"], PROMPT)
        assert cmd == ["auggie", "--print", "--output-format", "json", "--show-cost",
                       "--log-file", "-", "--log-level", "info",
                       "--permission", "launch-process:allow", "--model", "model-id",
                       "--reasoning-effort", "high", "--extra", PROMPT]

    def test_cursor_stream_json_ignores_effort(self):
        cmd = get_agent("cursor").build_cmd(
            "gpt-5.3-codex-high", "high", ["--extra"], PROMPT)
        assert cmd == ["agent", "-p", "--output-format", "stream-json",
                       "--trust", "--disable-indexing", "--force",
                       "--model", "gpt-5.3-codex-high", "--extra", PROMPT]

    def test_cursor_allowlist_drops_force_and_sandbox(self):
        """In the sandbox, Cursor runs every shell command whatever the
        rules say, so an allowlist run turns it off."""
        cmd = get_agent("cursor").build_cmd(
            None, None, [], PROMPT, allowed_tools=["Shell(git:log *)"])
        assert cmd == ["agent", "-p", "--output-format", "stream-json",
                       "--trust", "--disable-indexing",
                       "--sandbox", "disabled", PROMPT]

    def test_write_cursor_permissions(self, tmp_path):
        import json
        from lreview.agents import write_cursor_permissions
        path = write_cursor_permissions(tmp_path, ["Shell(git:log *)"])
        assert path == tmp_path / ".cursor" / "cli.json"
        assert json.loads(path.read_text()) == {
            "permissions": {"allow": ["Shell(git:log *)"], "deny": []}}
        write_cursor_permissions(tmp_path, ["Shell(*)"], ("Shell(curl:*)",))
        assert json.loads(path.read_text())["permissions"] == {
            "allow": ["Shell(*)"], "deny": ["Shell(curl:*)"]}

    def test_cursor_prompt_on_stdin_and_resume(self):
        cmd = get_agent("cursor").build_cmd(
            None, None, [], PROMPT, resume="chat-1", prompt_on_stdin=True)
        assert cmd[-2:] == ["--resume", "chat-1"]
        assert PROMPT not in cmd

    def test_cursor_runs_the_agent_binary(self):
        assert get_agent("cursor").binary == "agent"
        assert get_agent("claude").binary == "claude"

    def test_claude_prompt_and_stream_json(self):
        cmd = get_agent("claude").build_cmd(
            "opus", None, ["--extra"], PROMPT)
        assert cmd == ["claude", "-p", PROMPT,
                       "--setting-sources", "user",
                       "--dangerously-skip-permissions",
                       "--verbose", "--output-format", "stream-json",
                       "--model", "opus", "--extra"]

    def test_claude_allowlist_replaces_skip_permissions(self):
        cmd = get_agent("claude").build_cmd(
            "opus", None, [], PROMPT,
            allowed_tools=["Read", "Bash(git log *)"])
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd[5:9] == ["--permission-mode", "dontAsk",
                            "--allowedTools", "Read,Bash(git log *)"]

    def test_allowlist_needs_claude(self):
        with pytest.raises(ValueError, match="needs the claude or cursor agent"):
            get_agent("codex").build_cmd(None, None, [], PROMPT,
                                         allowed_tools=["Read"])

    def test_claude_effort(self):
        cmd = get_agent("claude").build_cmd("opus", "xhigh", [], PROMPT)
        assert cmd[-4:] == ["--model", "opus", "--effort", "xhigh"]

    def test_codex_streams_json_events(self):
        # --json makes exec emit the JSONL event stream the runner
        # parses for the token total, instead of prose.
        cmd = get_agent("codex").build_cmd(None, None, [], PROMPT)
        assert cmd == [
            "codex", "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable", "fast_mode",
            "-c", "project_doc_max_bytes=0",
            PROMPT,
        ]

    def test_codex_effort_maps_to_config_override(self):
        cmd = get_agent("codex").build_cmd(None, "high", [], PROMPT)
        assert cmd == [
            "codex", "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable", "fast_mode",
            "-c", "project_doc_max_bytes=0",
            "-c", 'model_reasoning_effort="high"',
            PROMPT,
        ]
        cmd = get_agent("codex").build_cmd("gpt-5.6-sol", None, [], PROMPT)
        assert cmd[3:10] == ["--dangerously-bypass-approvals-and-sandbox",
                             "--disable", "fast_mode",
                             "-c", "project_doc_max_bytes=0",
                             "-m", "gpt-5.6-sol"]
        # --agent-arg after effort so a later -c can override
        cmd = get_agent("codex").build_cmd(
            "gpt-6-astra", "ultra", ["-c", 'model_reasoning_effort="low"'],
            PROMPT)
        assert cmd == [
            "codex", "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable", "fast_mode",
            "-c", "project_doc_max_bytes=0",
            "-m", "gpt-6-astra",
            "-c", 'model_reasoning_effort="ultra"',
            "-c", 'model_reasoning_effort="low"',
            PROMPT,
        ]

    def test_nothing_is_loaded_from_the_worktree(self, tmp_path):
        # A patch could plant CLAUDE.md, settings or skills in the
        # worktree; neither agent may load them.
        cmd = get_agent("claude").build_cmd(None, None, [], PROMPT)
        assert cmd[3:5] == ["--setting-sources", "user"]
        skill = tmp_path / ".agents" / "skills" / "evil" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("x")
        cmd = get_agent("codex").build_cmd(None, None, [], PROMPT,
                                           worktree=tmp_path)
        assert "project_doc_max_bytes=0" in cmd
        override = cmd[cmd.index("project_doc_max_bytes=0") + 2]
        import tomllib
        parsed = tomllib.loads(override.replace("skills.config=",
                                                "config=", 1))
        assert {"path": str(skill), "enabled": False} in parsed["config"]

    def test_agy_stream_json_and_options(self):
        cmd = get_agent("agy").build_cmd(
            "gemini-4-neon", "high", ["--extra"], PROMPT, lean=True)
        assert cmd == [
            "agy", "--output-format", "stream-json",
            "--dangerously-skip-permissions", "--disable-slash-commands",
            "--model", "gemini-4-neon", "--effort", "high",
            "--extra", "-p", PROMPT,
        ]

    def test_agy_prompt_on_stdin_and_resume(self):
        cmd = get_agent("agy").build_cmd(
            None, None, [], PROMPT, resume="conv-1", prompt_on_stdin=True)
        assert cmd == [
            "agy", "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--input-format", "stream-json",
            "--conversation", "conv-1",
        ]
        assert PROMPT not in cmd

    def test_agy_denied_tools_settings(self):
        cmd = get_agent("agy").build_cmd(
            None, None, [], PROMPT,
            denied_tools=["read_url(*)", "command(curl)"])
        i = cmd.index("--settings")
        assert json.loads(cmd[i + 1]) == {
            "permissions": {"deny": ["read_url(*)", "command(curl)"]}}

    def test_write_agy_offline_hooks(self, tmp_path):
        import subprocess
        from lreview.agents import write_agy_offline_hooks
        # Pre-existing hooks are preserved alongside lreview-offline
        existing = tmp_path / ".agents" / "hooks.json"
        existing.parent.mkdir()
        existing.write_text(json.dumps({"user-hook": {"Stop": []}}))
        path = write_agy_offline_hooks(tmp_path)
        assert path == existing
        data = json.loads(path.read_text())
        assert "user-hook" in data
        entry = data["lreview-offline"]["PreToolUse"][0]
        assert "search_web" in entry["matcher"]
        assert "browser_.*" in entry["matcher"]
        out = subprocess.run(entry["hooks"][0]["command"], shell=True,
                             capture_output=True, text=True, check=True).stdout
        assert json.loads(out)["decision"] == "deny"

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
            "gpt-6-astra", [], PROMPT)[:3] == ["codex", "-m",
                                               "gpt-6-astra"]

    def test_codex_interactive_effort(self):
        cmd = get_agent("codex").build_interactive_cmd(
            "gpt-5.6-sol", [], PROMPT, effort="medium")
        assert cmd == [
            "codex", "-m", "gpt-5.6-sol",
            "-c", 'model_reasoning_effort="medium"',
            PROMPT,
        ]

    def test_agy_uses_prompt_interactive_flag(self):
        cmd = get_agent("agy").build_interactive_cmd(
            "gemini-4-neon", ["--add-dir", "/x"], PROMPT,
            effort="medium", resume="conv-1")
        assert cmd == [
            "agy", "--model", "gemini-4-neon", "--effort", "medium",
            "--conversation", "conv-1", "--add-dir", "/x", "-i", PROMPT,
        ]

    def test_gemini_uses_prompt_interactive_flag(self):
        cmd = get_agent("gemini").build_interactive_cmd(None, [], PROMPT)
        assert cmd == ["gemini", "-i", PROMPT]

    def test_opencode_prompt_is_a_flag_not_positional(self):
        cmd = get_agent("opencode").build_interactive_cmd(
            "anthropic/claude-sonnet-5", [], PROMPT)
        assert cmd == ["opencode",
                       "--model", "anthropic/claude-sonnet-5",
                       "--prompt", PROMPT]

    def test_cursor_chat_trusts_the_new_worktree(self):
        cmd = get_agent("cursor").build_interactive_cmd(
            "composer-2.5", [], PROMPT)
        assert cmd == ["agent", "--trust", "--disable-indexing",
                       "--model", "composer-2.5", PROMPT]

    def test_no_bypass_flags_in_interactive_mode(self):
        for name in ("claude", "codex", "gemini", "opencode", "auggie",
                     "cursor", "agy"):
            cmd = get_agent(name).build_interactive_cmd(None, [], PROMPT)
            joined = " ".join(cmd)
            assert "yolo" not in joined
            assert "--force" not in joined
            assert "dangerously" not in joined
            assert "bypass" not in joined


class TestLean:

    def test_claude_lean_args_and_env(self):
        from lreview.agents import LEAN_CLAUDE_ARGS
        spec = get_agent("claude")
        cmd = spec.build_cmd("opus", None, ["--extra"], PROMPT, lean=True)
        assert cmd[-len(LEAN_CLAUDE_ARGS) - 1:-1] == LEAN_CLAUDE_ARGS
        assert cmd[-1] == "--extra"
        assert spec.env(lean=True) == {
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}
        assert spec.env(lean=False) == {}

    def test_lean_is_claude_only(self):
        cmd = get_agent("codex").build_cmd(None, None, [], PROMPT, lean=True)
        assert cmd[-1] == PROMPT and "--tools" not in cmd
        assert get_agent("codex").env(lean=True) == {}

    def test_system_file(self):
        cmd = get_agent("claude").build_cmd(
            None, None, [], PROMPT, system_file="/r/.preload-x.md")
        assert cmd[-2:] == ["--append-system-prompt-file", "/r/.preload-x.md"]


class TestCodexLean:

    def test_lean_disables_unused_features(self):
        from lreview.agents import LEAN_CODEX_ARGS
        cmd = get_agent("codex").build_cmd(None, None, [], PROMPT, lean=True)
        assert cmd[-1] == PROMPT
        assert cmd[-1 - len(LEAN_CODEX_ARGS):-1] == LEAN_CODEX_ARGS
        assert "plugins" in LEAN_CODEX_ARGS

    def test_prompt_on_stdin(self):
        cmd = get_agent("codex").build_cmd(None, None, [], PROMPT,
                                           prompt_on_stdin=True)
        assert cmd[-1] == "-" and PROMPT not in cmd


class TestUserPlugins:
    """A lean claude review switches off the user's plugins."""

    def _fake_claude(self, tmp_path, monkeypatch, output):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text("#!/bin/sh\n[ \"$1 $2 $3\" = \"plugin list --json\" ] "
                        "|| exit 9\ncat <<'END'\n" + output + "\nEND\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    def test_lists_only_the_enabled_ones(self, tmp_path, monkeypatch):
        self._fake_claude(tmp_path, monkeypatch, json.dumps([
            {"id": "hooks-plugin@market", "enabled": True},
            {"id": "off@market", "enabled": False},
            {"id": "other-plugin@synced", "enabled": True}]))
        assert _REAL_ENABLED_PLUGINS.__wrapped__() == (
            "hooks-plugin@market", "other-plugin@synced")

    def test_a_claude_that_cannot_say_means_none(self, tmp_path,
                                                 monkeypatch):
        self._fake_claude(tmp_path, monkeypatch, "not json")
        assert _REAL_ENABLED_PLUGINS.__wrapped__() == ()

    def test_settings_turn_each_one_off(self, monkeypatch):
        from lreview import agents
        monkeypatch.setattr(agents, "enabled_plugins",
                            lambda: ("a@m", "b@m"))
        assert agents.plugins_off_settings() == {
            "enabledPlugins": {"a@m": False, "b@m": False}}
        monkeypatch.setattr(agents, "enabled_plugins", lambda: ())
        assert agents.plugins_off_settings() == {}
