"""Agent backends for lreview.

Reviews are initiated with a plain prompt that references
review-core.md directly (the review-prompts README quick-start form),
so every backend receives the same instruction text — only the
headless CLI invocation differs per agent.

Claude, codex and cursor are the verified backends. All three are run
with a JSONL event stream (claude's and cursor's --output-format
stream-json, codex's --json) that lreview parses for token usage;
only claude's carries a dollar cost. auggie, gemini and opencode are
best-effort command templates.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# A review needs a shell and the file tools.  The interactive defaults
# add some 40K tokens to every call: three dozen tools, the claude.ai
# connectors, the skills listing, and every CLAUDE.md from ~ down to
# the worktree, none of which the review prompts use.
LEAN_CLAUDE_ARGS = [
    "--tools", "Bash,Read,Write,Edit,Grep,Glob",
    "--strict-mcp-config",
    "--disable-slash-commands",
    # Keeps cwd and git status out of the cached system prompt, so
    # concurrent reviews share its cache.
    "--exclude-dynamic-system-prompt-sections",
]
# The patch under review could plant instructions, settings or skills in
# the worktree; a review loads none of them, lean or not (Patrick,
# 2026-11-06). claude reads only the user's settings, CLAUDE.md and
# skills; codex reads no project AGENTS.md and turns off each skill the
# worktree holds, by path, having no switch for the repo scope.
CLAUDE_SOURCE_ARGS = ["--setting-sources", "user"]
CODEX_SOURCE_ARGS = ["-c", "project_doc_max_bytes=0"]
CHECKOUT_SKILL_DIRS = (".agents/skills", ".codex/skills")


def checkout_skill_args(worktree: Optional[Path]) -> list[str]:
    """codex -c overrides that disable every skill in ``worktree``."""
    if worktree is None:
        return []
    found = set()
    for top in CHECKOUT_SKILL_DIRS:
        root = Path(worktree) / top
        if root.is_dir():
            for skill in root.rglob("SKILL.md"):
                found.add(str(skill))
                try:
                    found.add(str(skill.resolve()))
                except OSError:
                    pass
    if not found:
        return []
    entries = ", ".join(f"{{path = {json.dumps(path)}, enabled = false}}"
                        for path in sorted(found))
    return ["-c", f"skills.config=[{entries}]"]


# codex's equivalent: the features that add tools a review never uses.
# The user's config.toml stays in force (its service tier, for one).
LEAN_CODEX_FEATURES = (
    "apps", "browser_use", "browser_use_external", "computer_use",
    "image_generation", "multi_agent", "plugins", "remote_plugin", "goals",
    "skill_search", "skill_mcp_dependency_install", "in_app_browser",
    "sleep_tool",
)
LEAN_CODEX_ARGS = [arg for name in LEAN_CODEX_FEATURES
                   for arg in ("--disable", name)]
# Calls in a review are seconds apart, so the 1h cache Claude Code
# defaults to only makes every cache write cost 2x input, not 1.25x.
LEAN_CLAUDE_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
}


@dataclass(frozen=True)
class AgentSpec:
    """One supported agent backend."""
    name: str
    # Emits a JSONL event stream lreview parses for token usage (and,
    # for claude, cost and the model name). It is also what makes the
    # log grow during a run, so it doubles as the liveness signal.
    stream_json: bool = False
    verified: bool = True
    # The executable, when it is not the backend's name (Cursor's CLI
    # is "agent").
    command: Optional[str] = None

    @property
    def binary(self) -> str:
        return self.command or self.name

    def build_cmd(
        self,
        model: Optional[str],
        effort: Optional[str],
        extra_args: list[str],
        prompt_text: str,
        resume: Optional[str] = None,
        settings: Optional[str] = None,
        lean: bool = False,
        system_file: Optional[str] = None,
        prompt_on_stdin: bool = False,
        allowed_tools: Optional[list[str]] = None,
        worktree: Optional[Path] = None,
    ) -> list[str]:
        # resume: a session to continue (claude forks it; cursor
        # continues the chat itself). settings: claude's per-run
        # settings (hooks). Other agents ignore both.  lean: see
        # LEAN_CLAUDE_ARGS.
        # allowed_tools, claude: run with --permission-mode dontAsk and
        # this allowlist in place of --dangerously-skip-permissions.
        # The agent cannot use a tool that the list does not name, and
        # nothing prompts. cursor: see below.
        # effort: claude gets --effort; codex gets a config override
        # for model_reasoning_effort (Codex has no --effort flag).
        # gemini/opencode/cursor ignore it (the CLI warns when dropped).
        # extra_args come after so a later --agent-arg=-c can override.
        if self.name == "claude":
            cmd = ["claude", "-p", prompt_text, *CLAUDE_SOURCE_ARGS]
            if allowed_tools:
                cmd += ["--permission-mode", "dontAsk",
                        "--allowedTools", ",".join(allowed_tools)]
            else:
                cmd += ["--dangerously-skip-permissions"]
            cmd += ["--verbose", "--output-format", "stream-json"]
            if model:
                cmd += ["--model", model]
            if effort:
                cmd += ["--effort", effort]
            if resume:
                cmd += ["--resume", resume, "--fork-session"]
            if settings:
                cmd += ["--settings", settings]
            if lean:
                cmd += LEAN_CLAUDE_ARGS
            if system_file:
                cmd += ["--append-system-prompt-file", system_file]
            return cmd + extra_args
        if self.name == "cursor":
            # Like claude's stream-json, ending in a result event with
            # camelCase usage and no cost. --trust skips the prompt for
            # a workspace (each review's new worktree) not yet trusted;
            # without it the run exits at once. The effort is part of
            # the model slug (gpt-5.3-codex-high), so effort is unused.
            # allowed_tools: the rules go in the worktree's
            # .cursor/cli.json (write_cursor_permissions); the run drops
            # --force, so a shell command no rule allows is rejected.
            # The sandbox stays off: in the sandbox, Cursor runs every
            # shell command whatever the rules say.
            # --disable-indexing: never sync the worktree to Cursor's
            # servers for search. A -p run skipped it in our tests
            # (2026.10.22), but nothing documents that.
            cmd = ["agent", "-p", "--output-format", "stream-json",
                   "--trust", "--disable-indexing"]
            if allowed_tools:
                cmd += ["--sandbox", "disabled"]
            else:
                cmd += ["--force"]
            if model:
                cmd += ["--model", model]
            # Continues that chat in place: cursor cannot fork one.
            if resume:
                cmd += ["--resume", resume]
            # With no prompt argument, -p reads the prompt from stdin.
            return cmd + extra_args + ([] if prompt_on_stdin
                                       else [prompt_text])
        if allowed_tools:
            raise ValueError("--allow-tool needs the claude or cursor agent")
        if self.name == "codex":
            # --json makes exec stream JSONL events (thread/turn/item)
            # instead of prose, ending in the turn.completed usage the
            # runner reads for the token total.
            # Never fast mode: it bills reviews at a premium for speed a
            # background review does not need, whatever config.toml says.
            cmd = ["codex", "exec", "--json",
                   "--dangerously-bypass-approvals-and-sandbox",
                   "--disable", "fast_mode",
                   *CODEX_SOURCE_ARGS, *checkout_skill_args(worktree)]
            if model:
                cmd += ["-m", model]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            if lean:
                cmd += LEAN_CODEX_ARGS
            # "-" reads the prompt from stdin: a preloaded one is past
            # what one command-line argument may hold.
            return cmd + extra_args + ["-" if prompt_on_stdin else prompt_text]
        if self.name == "gemini":
            cmd = ["gemini", "--yolo"]
            if model:
                cmd += ["-m", model]
            return cmd + extra_args + ["-p", prompt_text]
        if self.name == "auggie":
            cmd = ["auggie", "--print", "--output-format", "json", "--show-cost",
                   "--log-file", "-", "--log-level", "info",
                   "--permission", "launch-process:allow"]
            if model:
                cmd += ["--model", model]
            if effort:
                cmd += ["--reasoning-effort", effort]
            return cmd + extra_args + [prompt_text]
        if self.name == "opencode":
            cmd = ["opencode", "run"]
            if model:
                cmd += ["--model", model]
            return cmd + extra_args + [prompt_text]
        raise ValueError(f"unknown agent {self.name}")

    def env(self, lean: bool = False) -> dict:
        """Environment additions for a headless review."""
        if lean and self.name == "claude":
            return dict(LEAN_CLAUDE_ENV)
        return {}

    def build_interactive_cmd(
        self,
        model: Optional[str],
        extra_args: list[str],
        prompt_text: str,
        effort: Optional[str] = None,
        resume: Optional[str] = None,
    ) -> list[str]:
        """Interactive session seeded with an initial prompt.

        The seeded prompt is submitted as the first turn and the
        session then keeps taking user input. Verified against each
        CLI's official docs/source (2026-09-04). Deliberately no
        approval-bypass flags: interactive sessions run with the
        CLI's normal permission prompts (file reads in the cwd are
        allowed by default in all four).

        resume, claude only: start from a fork of that session.
        """
        if self.name == "claude":
            cmd = ["claude"]
            if model:
                cmd += ["--model", model]
            if effort:
                cmd += ["--effort", effort]
            if resume:
                cmd += ["--resume", resume, "--fork-session"]
            return cmd + extra_args + [prompt_text]
        if self.name == "codex":
            # Bare `codex` is the TUI; the positional prompt is
            # auto-submitted as the first turn (`codex exec` is the
            # separate non-interactive mode).
            cmd = ["codex"]
            if model:
                cmd += ["-m", model]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            return cmd + extra_args + [prompt_text]
        if self.name == "gemini":
            # -i/--prompt-interactive is the explicit interactive
            # seed; -p is one-shot, and a bare positional prompt is
            # one-shot on older releases.
            cmd = ["gemini"]
            if model:
                cmd += ["-m", model]
            return cmd + extra_args + ["-i", prompt_text]
        if self.name == "auggie":
            cmd = ["auggie"]
            if model:
                cmd += ["--model", model]
            if effort:
                cmd += ["--reasoning-effort", effort]
            return cmd + extra_args + [prompt_text]
        if self.name == "cursor":
            # Each chat runs in a new worktree, as a review does.
            cmd = ["agent", "--trust", "--disable-indexing"]
            if model:
                cmd += ["--model", model]
            return cmd + extra_args + [prompt_text]
        if self.name == "opencode":
            # The root command's positional argument is a project
            # PATH, not a prompt — the prompt goes in --prompt,
            # which the TUI auto-submits. --model wants the
            # provider/model form (e.g. anthropic/claude-sonnet-4-5).
            cmd = ["opencode"]
            if model:
                cmd += ["--model", model]
            return cmd + extra_args + ["--prompt", prompt_text]
        raise ValueError(f"unknown agent {self.name}")


def write_cursor_permissions(worktree: Path, rules: list[str],
                             deny: tuple = ()) -> Path:
    """Write the cursor allowlist where the agent reads it.

    Cursor reads .cursor/cli.json in its workspace, beside the user's
    ~/.cursor/cli-config.json. Only Shell, WebFetch and Mcp rules
    gate a tool: the file tools run without an allow rule, and only a
    deny rule stops them. A deny rule wins over every allow rule.
    """
    path = worktree / ".cursor" / "cli.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(
        {"permissions": {"allow": rules, "deny": list(deny)}},
        indent=2) + "\n")
    return path


_SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]+")


def claude_session_exists(session_id: str) -> bool:
    """Whether Claude Code still keeps the session: it deletes old ones
    (cleanupPeriodDays), and another machine never had it."""
    if not _SESSION_ID_RE.fullmatch(session_id):
        return False
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    return any((Path(config_dir) / "projects").glob(f"*/{session_id}.jsonl"))


AGENTS = {
    "claude": AgentSpec(name="claude", stream_json=True),
    "codex": AgentSpec(name="codex", stream_json=True),
    "auggie": AgentSpec(name="auggie", stream_json=True, verified=False),
    "cursor": AgentSpec(name="cursor", stream_json=True, command="agent"),
    "gemini": AgentSpec(name="gemini", verified=False),
    "opencode": AgentSpec(name="opencode", verified=False),
}


def get_agent(name: str) -> AgentSpec:
    try:
        return AGENTS[name]
    except KeyError:
        raise ValueError(
            f"unknown agent '{name}' (supported: {', '.join(AGENTS)})")
