"""Lens reviews (--lenses): five lens subagents feed the reviewer.

After categorising the change, the reviewer starts five subagents at
once, one per class of bug (lens.md). Each digs into its class across
the whole change and writes every candidate it cannot rule out to
review-context/LENS-<lens>.json; the reviewer verifies them all in the
protocol's own Task 2/3 and reports as usual. On lreview bench this
found more known bugs in one run than two or three ordinary runs
together, at the same precision (docs/lens-review-evaluation.md).

The lenses run as the custom agent type "lens", defined per review with
claude's --agents, so their model and effort are set here, apart from
the reviewer's.
"""

import json
from pathlib import Path
from typing import Optional

LENS_PROMPT_PATH = Path(__file__).resolve().parent / "lens.md"
# TASK 1D says "five": keep them in step.
LENSES = ("concurrency", "lifecycle", "data", "semantics", "build")
LENS_AGENT = "lens"
# What a lens runs with: it reads code and writes its LENS file.
LENS_TOOLS = ("Bash", "Read", "Write", "Grep", "Glob")
# Tools the reviewer needs on top of the lean set: Agent starts the
# lenses; TodoWrite was in the measured setup.
LENS_REVIEW_TOOLS = ("Agent", "TodoWrite")
LENS_OUTPUT_DIR = "review-context"
DEFAULT_LENS_EFFORT = "medium"

# The protocol section this task goes in front of.
TASK_2_HEADING = "### Task 2: Analyze the changes for regressions"


def task_text(prompts_dir: Path) -> str:
    """TASK 1D: start the lenses, then verify what they find."""
    names = ", ".join(f"`{lens}`" for lens in LENSES)
    return f"""### TASK 1D: Specialist lenses (mandatory when the Agent tool is available)

One reviewer carrying every check through every change misses bugs that a
focused pass finds.  After TASK 1C, launch five specialist subagents **in a
single message** (five Agent tool calls, so they run in parallel), one per
lens: {names}.  Use
`subagent_type: "{LENS_AGENT}"` when that agent type exists, else the default.  Each
prompt says:

    Read {LENS_PROMPT_PATH} and follow it.
    Lens: <lens>
    Prompt directory: {prompts_dir}
    Commit: HEAD of the repository in <current directory>
    Output directory: <current directory>/{LENS_OUTPUT_DIR}
    Changed functions and CHANGE categories: <your TASK 1C list>

Give both directories as absolute paths.  While they run you have nothing
to wait on: when they return, read every
`./{LENS_OUTPUT_DIR}/LENS-*.json`.  Every candidate is a suspect bug for Task
2; treat it like one you found yourself, verify it against the code in Task
2 and Task 3, and report it or eliminate it with a reason.  A lens
candidate is not evidence by itself and a lens's silence proves nothing;
still do your own Task 2 analysis.  Output:

    LENSES: <N> candidates from <M> lenses; <K> confirmed, <E> eliminated

If the Agent tool is not available, skip this task.

"""


def with_task(protocol: str, prompts_dir: Path) -> str:
    """review-core.md with TASK 1D in front of Task 2, where it was
    measured; at the end when that heading has moved."""
    task = task_text(prompts_dir)
    if TASK_2_HEADING in protocol:
        return protocol.replace(TASK_2_HEADING, task + TASK_2_HEADING, 1)
    return protocol.rstrip("\n") + "\n\n" + task


def agents_json(model: Optional[str], effort: Optional[str]) -> str:
    """claude --agents: the lens agent type with its model and effort."""
    spec = {"description": "Specialist lens reviewer for one Lustre "
                           "commit; follows the lens.md it is pointed at",
            "prompt": "You are a specialist Lustre kernel code reviewer. "
                      "Follow the lens instructions you are given "
                      "exactly.",
            "tools": list(LENS_TOOLS)}
    if model:
        spec["model"] = model
    if effort:
        spec["effort"] = effort
    return json.dumps({LENS_AGENT: spec})


def lens_files(repo_dir: Path) -> list:
    return sorted((repo_dir / LENS_OUTPUT_DIR).glob("LENS-*.json"))
