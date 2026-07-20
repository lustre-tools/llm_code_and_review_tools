"""Agent backends for lreview.

The kreview prompts from review-prompts support several coding agents
(setup.sh claude|codex|gemini|opencode). lreview can drive any of
them headless: the prompt itself mandates the ./gerrit-review.json and
./review-metadata.json output files, so collection and posting are
agent-agnostic — only the skill location and the headless invocation
differ per agent.

Claude is the verified backend (and the only one whose stream-json
output gives live token counts, final usage/cost, and model
detection). The other backends are best-effort command templates: the
installed kreview command file's content is passed as the prompt text,
so they do not depend on each CLI's slash-command expansion.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AgentSpec:
    """One supported agent backend."""
    name: str
    # Where review-prompts' setup.sh installs the kreview command for
    # this agent (relative to $HOME) — from review-prompts/agents/*.sh.
    commands_subdir: str
    # Only claude emits the stream-json events lreview parses for
    # live tokens, final usage, and the model name.
    stream_json: bool = False
    verified: bool = True

    def commands_dir(self) -> Path:
        return Path.home() / self.commands_subdir

    def command_file(self) -> Path:
        return self.commands_dir() / "kreview.md"

    def build_cmd(
        self,
        model: Optional[str],
        extra_args: list[str],
        prompt_text: str,
    ) -> list[str]:
        if self.name == "claude":
            cmd = ["claude", "-p", "/kreview",
                   "--dangerously-skip-permissions",
                   "--verbose", "--output-format", "stream-json"]
            if model:
                cmd += ["--model", model]
            return cmd + extra_args
        if self.name == "codex":
            cmd = ["codex", "exec",
                   "--dangerously-bypass-approvals-and-sandbox"]
            if model:
                cmd += ["-m", model]
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


AGENTS = {
    "claude": AgentSpec(
        name="claude", commands_subdir=".claude/commands",
        stream_json=True),
    "codex": AgentSpec(
        name="codex", commands_subdir=".codex/prompts", verified=False),
    "gemini": AgentSpec(
        name="gemini", commands_subdir=".gemini/commands", verified=False),
    "opencode": AgentSpec(
        name="opencode", commands_subdir=".opencode/commands",
        verified=False),
}


def get_agent(name: str) -> AgentSpec:
    try:
        return AGENTS[name]
    except KeyError:
        raise ValueError(
            f"unknown agent '{name}' (supported: {', '.join(AGENTS)})")
