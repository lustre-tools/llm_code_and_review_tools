"""Model catalogs for the review backends.

Claude takes short model names (opus, sonnet, fable, haiku) and one
reasoning-effort ladder that every model shares. Codex does not: it
fronts several model families -- GPT-6 and GPT-5.6 are the current
ones -- and the effort ladder is *per model*. Only some models accept
"ultra", and the older gpt-5.5 / gpt-5.3-codex-spark stop at "xhigh".

Asking codex for an effort its model cannot do fails inside the agent
seconds into the run, after lreview has already fetched the change and
built a worktree, and the batch records it as a failed review. So the
pair is validated up front, before anything expensive happens.

The table is a snapshot of codex's own model list (codex-cli 0.153.4,
2026-09-09). A name that is not in it is passed through to the agent
untouched and not effort-checked, so a model released after this table
was written still works -- the table constrains only what it knows.
"""

from dataclasses import dataclass
from typing import Optional

# The union of every backend's effort ladder; individual agents and
# models accept subsets (see CLAUDE_EFFORTS and CodexModel.efforts).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")

# claude --effort; "ultra" is codex-only
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Agents that take a reasoning effort at all
EFFORT_AGENTS = ("claude", "codex")


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
        slug="gpt-6-astra",
        family="GPT-6",
        summary="most capable; complex, demanding work",
        efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_effort="medium",
        aliases=("astra", "gpt-6", "gpt6"),
    ),
    CodexModel(
        slug="gpt-5.6-sol",
        family="GPT-5.6",
        summary="reliable agentic workhorse for everyday tasks",
        efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_effort="low",
        aliases=("sol",),
    ),
    CodexModel(
        slug="gpt-5.6-terra",
        family="GPT-5.6",
        summary="balanced agentic coding model for everyday work",
        efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_effort="medium",
        # bare "gpt-5.6" means the balanced member of the family
        aliases=("terra", "gpt-5.6", "gpt5.6"),
    ),
    CodexModel(
        slug="gpt-5.6-luna",
        family="GPT-5.6",
        summary="fast and affordable agentic coding",
        efforts=("low", "medium", "high", "xhigh", "max"),
        default_effort="medium",
        aliases=("luna",),
    ),
    CodexModel(
        slug="gpt-5.5",
        family="GPT-5.5",
        summary="previous generation, coding and general work",
        efforts=("low", "medium", "high", "xhigh"),
        default_effort="medium",
        aliases=("gpt5.5",),
    ),
    CodexModel(
        slug="gpt-5.3-codex-spark",
        family="GPT-5.3",
        summary="ultra-fast coding model",
        efforts=("low", "medium", "high", "xhigh"),
        default_effort="high",
        aliases=("spark", "codex-spark"),
    ),
)

# Reviews default to the most capable model of each agent, as claude
# reviews default to opus. Override with --model / $LREVIEW_MODEL.
CODEX_DEFAULT_MODEL = "gpt-6-astra"

CLAUDE_DEFAULT_MODEL = "opus"

DEFAULT_MODELS = {
    "claude": CLAUDE_DEFAULT_MODEL,
    "codex": CODEX_DEFAULT_MODEL,
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
    if agent == "codex":
        model = codex_model(name)
        return model.efforts if model else None
    return None


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
    if agent == "claude":
        raise ValueError(
            f"claude does not support --effort {effort} "
            f"(accepts: {', '.join(CLAUDE_EFFORTS)}; "
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
