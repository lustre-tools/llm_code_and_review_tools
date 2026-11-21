"""Model catalogs for the review backends.

Claude takes short model names (opus, sonnet, fable, haiku) and one
reasoning-effort ladder that every model shares. Codex supports several
model families, and the effort ladder is *per model*. Only some models accept
"ultra", and the older gpt-5.5 / gpt-5.3-codex-spark stop at "xhigh".

Asking codex for an effort its model cannot do fails inside the agent
seconds into the run, after lreview has already fetched the change and
built a worktree, and the batch records it as a failed review. So the
pair is validated up front, before anything expensive happens.

The table is based on Codex's model catalog. Unlisted model names pass
through to the agent without effort validation.
"""

from dataclasses import dataclass
from typing import Optional

from .defaults import (  # noqa: F401 (re-exported)
    AGY_DEFAULT_MODEL, CLAUDE_DEFAULT_EFFORT, CLAUDE_DEFAULT_MODEL,
    CODEX_DEFAULT_MODEL, CURSOR_DEFAULT_MODEL)

# The union of every backend's effort ladder; individual agents and
# models accept subsets (see CLAUDE_EFFORTS and CodexModel.efforts).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")

# The ladders: up to "ultra", up to "max", and up to "xhigh" for the
# older codex models.
ULTRA_EFFORTS = EFFORT_LEVELS
MAX_EFFORTS = EFFORT_LEVELS[:-1]
XHIGH_EFFORTS = EFFORT_LEVELS[:-2]

# claude and agy --effort; "ultra" is codex-only
CLAUDE_EFFORTS = MAX_EFFORTS
AGY_EFFORTS = MAX_EFFORTS

# Agents that take a reasoning effort at all
EFFORT_AGENTS = ("claude", "codex", "auggie", "agy")


@dataclass(frozen=True)
class CodexModel:
    """One model codex can run, and the efforts it accepts."""
    slug: str
    family: str
    summary: str
    efforts: tuple
    default_effort: str
    aliases: tuple = ()


CODEX_MODELS = (
    CodexModel(
        slug="gpt-6.2-sol",
        family="GPT-6.2",
        summary="fast model for coding and everyday work",
        efforts=ULTRA_EFFORTS,
        default_effort="low",
        aliases=("sol", "sol6.2", "gpt-6.2", "gpt6.2"),
    ),
    CodexModel(
        slug="gpt-6.2-astra",
        family="GPT-6.2",
        summary="most capable model for complex coding and professional work",
        efforts=ULTRA_EFFORTS,
        default_effort="low",
        aliases=("astra", "astra6.2"),
    ),
    CodexModel(
        slug="gpt-6.1-sol",
        family="GPT-6.1",
        summary="previous generation model for coding and everyday work",
        efforts=ULTRA_EFFORTS,
        default_effort="medium",
        aliases=("sol6.1", "gpt-6.1", "gpt6.1"),
    ),
    CodexModel(
        slug="gpt-6-sol",
        family="GPT-6",
        summary="previous Sol",
        efforts=ULTRA_EFFORTS,
        default_effort="medium",
        aliases=("sol6",),
    ),
    CodexModel(
        slug="gpt-6-astra",
        family="GPT-6",
        summary="previous generation Astra for complex, demanding work",
        efforts=ULTRA_EFFORTS,
        default_effort="medium",
        aliases=("gpt-6", "gpt6"),
    ),
    CodexModel(
        slug="gpt-5.6-sol",
        family="GPT-5.6",
        summary="reliable agentic workhorse for everyday tasks",
        efforts=ULTRA_EFFORTS,
        default_effort="low",
        aliases=("sol5.6",),
    ),
    CodexModel(
        slug="gpt-5.6-terra",
        family="GPT-5.6",
        summary="balanced agentic coding model for everyday work",
        efforts=ULTRA_EFFORTS,
        default_effort="medium",
        # bare "gpt-5.6" means the balanced member of the family
        aliases=("terra", "gpt-5.6", "gpt5.6"),
    ),
    CodexModel(
        slug="gpt-5.6-luna",
        family="GPT-5.6",
        summary="fast and affordable agentic coding",
        efforts=MAX_EFFORTS,
        default_effort="medium",
        aliases=("luna",),
    ),
    CodexModel(
        slug="gpt-5.5",
        family="GPT-5.5",
        summary="previous generation, coding and general work",
        efforts=XHIGH_EFFORTS,
        default_effort="medium",
        aliases=("gpt5.5",),
    ),
    CodexModel(
        slug="gpt-5.3-codex-spark",
        family="GPT-5.3",
        summary="ultra-fast coding model",
        efforts=XHIGH_EFFORTS,
        default_effort="high",
        aliases=("spark", "codex-spark"),
    ),
)

CLAUDE_MODELS = ("sonnet", "opus", "fable", "haiku")

DEFAULT_MODELS = {
    "claude": CLAUDE_DEFAULT_MODEL,
    "codex": CODEX_DEFAULT_MODEL,
    "cursor": CURSOR_DEFAULT_MODEL,
    "agy": AGY_DEFAULT_MODEL,
}


def codex_model(name: Optional[str]) -> Optional[CodexModel]:
    """The catalog entry for a codex model slug or alias, if known."""
    if not name:
        return None
    key = name.strip().lower()
    for model in CODEX_MODELS:
        if key == model.slug or key in model.aliases:
            return model
    return None


def catalog_agent(name: Optional[str]) -> Optional[str]:
    """The agent whose catalog lists a model name, or None."""
    if not name:
        return None
    if name.strip().lower() in CLAUDE_MODELS:
        return "claude"
    if codex_model(name):
        return "codex"
    return None


def canonical_model(agent: str, name: Optional[str]) -> Optional[str]:
    """Expand an alias to the name the agent CLI expects.

    Unknown names are returned unchanged: the catalog is a
    convenience, never a gate on which models may be run.
    """
    if agent != "codex":
        return name
    model = codex_model(name)
    return model.slug if model else name


def model_efforts(agent: str, name: Optional[str]) -> Optional[tuple]:
    """Efforts a model accepts, or None when they are not known."""
    if agent == "claude":
        return CLAUDE_EFFORTS
    if agent == "agy":
        return AGY_EFFORTS
    if agent == "codex":
        model = codex_model(name)
        return model.efforts if model else None
    return None


def resolve_effort(agent: str, effort: Optional[str]) -> Optional[str]:
    """The effort a review runs at: the one asked for, else
    CLAUDE_DEFAULT_EFFORT for claude, else None (the agent's own)."""
    if effort:
        return effort
    return CLAUDE_DEFAULT_EFFORT if agent == "claude" else None


def validate_selection(agent: str, model: Optional[str],
                       effort: Optional[str]) -> None:
    """Reject a model/effort pair the agent is known to refuse.

    Raises ValueError with a message naming the accepted efforts.
    Silent when the model is unknown to the catalog -- an unlisted
    model is the agent's business, not ours.
    """
    if not effort or agent not in EFFORT_AGENTS:
        return
    efforts = model_efforts(agent, model)
    if efforts is None or effort in efforts:
        return
    if agent in ("claude", "agy"):
        raise ValueError(
            f"{agent} does not support --effort {effort} "
            f"(accepts: {', '.join(efforts)}; "
            f"'{effort}' is codex-only)")
    entry = codex_model(model)
    raise ValueError(
        f"codex model {entry.slug} does not support --effort {effort} "
        f"(accepts: {', '.join(entry.efforts)})")


def codex_catalog_lines() -> list:
    """The codex model table, one line per model, for --help output."""
    width = max(len(model.slug) for model in CODEX_MODELS)
    lines = []
    for model in CODEX_MODELS:
        mark = " (default)" if model.slug == CODEX_DEFAULT_MODEL else ""
        alias = f"  [{', '.join(model.aliases)}]" if model.aliases else ""
        lines.append(
            f"  {model.slug:<{width}}{mark}{alias}\n"
            f"  {'':<{width}}  {model.summary}\n"
            f"  {'':<{width}}  effort: {', '.join(model.efforts)} "
            f"(model default: {model.default_effort})")
    return lines


def cursor_model_lines() -> list:
    """Cursor's models for this account, from `agent --list-models`.

    The effort is part of the slug (gpt-5.3-codex-high), so the list
    is also the effort ladder.
    """
    import subprocess
    lines = [f"  {CURSOR_DEFAULT_MODEL} (default); the effort is part of "
             "the model name, --effort is not used"]
    try:
        done = subprocess.run(["agent", "--list-models"],
                              capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return lines + [f"  (could not run 'agent --list-models': {exc})"]
    models = [line.strip() for line in done.stdout.splitlines()
              if " - " in line]
    if done.returncode != 0 or not models:
        said = " ".join((done.stdout + done.stderr).split())[:200]
        return lines + [f"  ('agent --list-models' failed: {said})"]
    # Cursor's own default is not lreview's
    return lines + ["  " + line.replace("(default)", "(Cursor's default)")
                    for line in models]


def agy_model_lines() -> list:
    """Antigravity CLI's models for this account, from `agy models`."""
    import subprocess
    lines = [f"  {AGY_DEFAULT_MODEL} (default); --effort: "
             f"{', '.join(AGY_EFFORTS)}, depending on model"]
    try:
        done = subprocess.run(["agy", "models"],
                              capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return lines + [f"  (could not run 'agy models': {exc})"]
    models = []
    for raw in done.stdout.split("\n"):
        # Strip any carriage-return spinner prefix on the first line
        line = raw.rsplit("\r", 1)[-1].strip()
        if line and "Fetching available models" not in line:
            models.append(line)
    if done.returncode != 0 or not models:
        said = " ".join((done.stdout + done.stderr).split())[:200]
        return lines + [f"  ('agy models' failed: {said})"]
    return lines + ["  " + line for line in models]
