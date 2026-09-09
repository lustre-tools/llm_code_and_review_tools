"""Agent backends for lreview.

Reviews are initiated with a plain prompt that references
review-core.md directly (the review-prompts README quick-start form),
so every backend receives the same instruction text — only the
headless CLI invocation differs per agent.

Claude and codex are the verified backends. Both are run with a JSONL
event stream (claude's --output-format stream-json, codex's --json)
that lreview parses for token usage; only claude's carries a dollar
cost and the model name. gemini and opencode are best-effort command
templates.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AgentSpec:
    """One supported agent backend."""
    name: str
    # Emits a JSONL event stream lreview parses for token usage (and,
    # for claude, cost and the model name). It is also what makes the
    # log grow during a run, so it doubles as the liveness signal.
    stream_json: bool = False
    verified: bool = True

    def build_cmd(
        self,
        model: Optional[str],
        effort: Optional[str],
        extra_args: list[str],
        prompt_text: str,
    ) -> list[str]:
        # effort: claude gets --effort; codex gets a config override
        # for model_reasoning_effort (Codex has no --effort flag).
        # gemini/opencode ignore it (the CLI warns when dropped).
        # extra_args come after so a later --agent-arg=-c can override.
        if self.name == "claude":
            cmd = ["claude", "-p", prompt_text,
                   "--dangerously-skip-permissions",
                   "--verbose", "--output-format", "stream-json"]
            if model:
                cmd += ["--model", model]
            if effort:
                cmd += ["--effort", effort]
            return cmd + extra_args
        if self.name == "codex":
            # --json makes exec stream JSONL events (thread/turn/item)
            # instead of prose, ending in the turn.completed usage the
            # runner reads for the token total.
            cmd = ["codex", "exec", "--json",
                   "--dangerously-bypass-approvals-and-sandbox"]
            if model:
                cmd += ["-m", model]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            return cmd + extra_args + [prompt_text]
        if self.name == "gemini":
            cmd = ["gemini", "--yolo"]
            if model:
                cmd += ["-m", model]
            return cmd + extra_args + ["-p", prompt_text]
        if self.name == "opencode":
            cmd = ["opencode", "run"]
            if model:
                cmd += ["--model", model]
            return cmd + extra_args + [prompt_text]
        raise ValueError(f"unknown agent {self.name}")

    def build_interactive_cmd(
        self,
        model: Optional[str],
        extra_args: list[str],
        prompt_text: str,
        effort: Optional[str] = None,
    ) -> list[str]:
        """Interactive session seeded with an initial prompt.

        The seeded prompt is submitted as the first turn and the
        session then keeps taking user input. Verified against each
        CLI's official docs/source (2026-09-04). Deliberately no
        approval-bypass flags: interactive sessions run with the
        CLI's normal permission prompts (file reads in the cwd are
        allowed by default in all four).
        """
        if self.name == "claude":
            cmd = ["claude"]
            if model:
                cmd += ["--model", model]
            if effort:
                cmd += ["--effort", effort]
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


AGENTS = {
    "claude": AgentSpec(name="claude", stream_json=True),
    "codex": AgentSpec(name="codex", stream_json=True),
    "gemini": AgentSpec(name="gemini", verified=False),
    "opencode": AgentSpec(name="opencode", verified=False),
}


def get_agent(name: str) -> AgentSpec:
    try:
        return AGENTS[name]
    except KeyError:
        raise ValueError(
            f"unknown agent '{name}' (supported: {', '.join(AGENTS)})")
