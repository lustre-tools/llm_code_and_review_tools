"""Agent backends for lreview.

Reviews are initiated with a plain prompt that references
review-core.md directly (the review-prompts README quick-start form),
so every backend receives the same instruction text — only the
headless CLI invocation differs per agent.

Claude is the verified backend (and the only one whose stream-json
output gives live token counts, final usage/cost, and model
detection). The other backends are best-effort command templates.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AgentSpec:
    """One supported agent backend."""
    name: str
    # Only claude emits the stream-json events lreview parses for
    # live tokens, final usage, and the model name.
    stream_json: bool = False
    verified: bool = True

    def build_cmd(
        self,
        model: Optional[str],
        extra_args: list[str],
        prompt_text: str,
    ) -> list[str]:
        if self.name == "claude":
            cmd = ["claude", "-p", prompt_text,
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
    "claude": AgentSpec(name="claude", stream_json=True),
    "codex": AgentSpec(name="codex", verified=False),
    "gemini": AgentSpec(name="gemini", verified=False),
    "opencode": AgentSpec(name="opencode", verified=False),
}


def get_agent(name: str) -> AgentSpec:
    try:
        return AGENTS[name]
    except KeyError:
        raise ValueError(
            f"unknown agent '{name}' (supported: {', '.join(AGENTS)})")
