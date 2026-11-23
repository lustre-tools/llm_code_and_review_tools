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
# The commit's diff goes into the lens agent's prompt up to this size;
# a larger one the lenses read themselves.  --agents is one argument,
# which Linux caps at 128 KiB.
INLINE_DIFF_MAX = 60_000

# The protocol section this task goes in front of.
TASK_2_HEADING = "### Task 2: Analyze the changes for regressions"


# How the reviewer settles a lens candidate.  On lreview bench it
# dropped much of what the lenses found with reasons it never checked.
SETTLE_RULES = """\
- Eliminate a candidate (ELIMINATED) only by pointing at what prevents
  it: the lock that is held, the check that runs first, the ordering that
  guarantees it, or the code that shows the claim is wrong.  "Low
  confidence", "speculative", "debug only", "unlikely" and "an existing
  pattern" are not reasons on their own.
- "Pre-existing" or "unchanged" needs the parent commit: read the code in
  `git show HEAD^:<file>` and compare it with what the patch does.  Code
  the patch adds, moves, reorders or calls from a new place is the
  patch's, even where similar code existed before; so is an old bug the
  patch makes reachable or more likely.  A real bug that the patch did
  none of this to is PRE-EXISTING, not ELIMINATED.
- An assumption about how the code is used ("no caller does that", "that
  configuration is not deployed") eliminates a candidate only when the
  code enforces it.
- A candidate you can neither confirm nor eliminate this way is
  UNSETTLED, not ELIMINATED.
- A candidate about a test script under `lustre/tests/` is CONFIRMED when
  the test can give a wrong result: it passes without exercising the
  change, fails in a configuration the test suite runs (interop with
  older servers or routers, for one), or carries on after its own setup
  failed.  The EXCLUSIONS rule on test scripts does not cover these.
"""

PREEXISTING_JSON_NAME = "pre-existing.json"

REPORTING = f"""\
Report CONFIRMED candidates as findings.  Report each UNSETTLED one as
`(minor)`, saying what the possible bug is and what could not be settled.
PRE-EXISTING bugs are not inline comments: list them at the end of the
`message` in gerrit-review.json under "Pre-existing issues (not
introduced by this change)", one line each with file:line, and also write
them to `./{PREEXISTING_JSON_NAME}` as
`{{"issues": [{{"file": ..., "line": ..., "function": ..., "claim": ...,
"evidence": ...}}]}}`.  Write gerrit-review.json for them even when they
are the only thing to report."""

def task_text(prompts_dir: Path) -> str:
    """TASK 1D: start the lenses, then settle what they find."""
    names = ", ".join(f"`{lens}`" for lens in LENSES)
    head = f"""### TASK 1D: Specialist lenses (mandatory when the Agent tool is available)

One reviewer carrying every check through every change misses bugs that a
focused pass finds.  Do this for every change, however small: the size of
a change says little about its bugs.  After TASK 1C, launch five specialist subagents **in a
single message** (five Agent tool calls, so they run in parallel), one per
lens: {names}.  Use
`subagent_type: "{LENS_AGENT}"` when that agent type exists, else the default.  Each
prompt says:

    Follow the lens instructions in your system prompt (if it has none,
    read {LENS_PROMPT_PATH} and follow that).
    Lens: <lens>
    Prompt directory: {prompts_dir}
    Commit: HEAD of the repository in <current directory>
    Output directory: <current directory>/{LENS_OUTPUT_DIR}
    Changed functions and CHANGE categories: <your TASK 1C list>

Give both directories as absolute paths.  While they run you have nothing
to wait on: when they return, read every
`./{LENS_OUTPUT_DIR}/LENS-*.json`.  A lens candidate is not evidence by
itself and a lens's silence proves nothing; still do your own Task 2
analysis.

"""
    settle = f"""Every candidate is a suspect bug for Task 2; treat it like one you found
yourself and verify it against the code in Task 2 and Task 3.  Lens
candidates are lost far more often by being dismissed than by being
wrong.  Settle each one on its own:

{SETTLE_RULES}
{REPORTING}

Before Task 3, list every candidate with its disposition:

    LENS DISPOSITIONS:
    - <lens> <n>: CONFIRMED | ELIMINATED (<file:line that prevents it>) |
      PRE-EXISTING (<HEAD^ file:line>) | UNSETTLED

then output:

    LENSES: <N> candidates from <M> lenses; <K> confirmed, <E> eliminated, <P> pre-existing, <U> unsettled
"""
    return head + settle + "\nIf the Agent tool is not available, skip this task.\n\n"


def with_task(protocol: str, prompts_dir: Path) -> str:
    """review-core.md with TASK 1D in front of Task 2, where it was
    measured; at the end when that heading has moved."""
    task = task_text(prompts_dir)
    if TASK_2_HEADING in protocol:
        return protocol.replace(TASK_2_HEADING, task + TASK_2_HEADING, 1)
    return protocol.rstrip("\n") + "\n\n" + task


def lens_prompt(commit_diff: Optional[str] = None) -> str:
    """The lens agent's system prompt: lens.md, and the commit's diff
    when it is small enough.  The five lenses of a review share it, so
    it is cached once instead of each lens reading the same files."""
    text = ("You are a specialist Lustre kernel code reviewer.  Follow "
            "these lens instructions exactly.\n\n"
            + LENS_PROMPT_PATH.read_text())
    if commit_diff and len(commit_diff) <= INLINE_DIFF_MAX:
        text = text.replace(
            "1. `git show HEAD` and read the whole diff once.",
            "1. Read the whole diff once: it is at the end of these "
            "instructions (`git show HEAD`).", 1)
        text += ("\n\n## The commit under review (`git show HEAD`)\n\n"
                 "```\n" + commit_diff + "\n```\n")
    return text


def commit_diff(worktree: Optional[Path]) -> Optional[str]:
    """`git show HEAD` in the review's worktree, None when unavailable."""
    if worktree is None:
        return None
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(worktree), "show", "HEAD"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def agents_json(model: Optional[str], effort: Optional[str],
                diff: Optional[str] = None) -> str:
    """claude --agents: the lens agent type with its model and effort;
    diff is the commit, for the lens prompt."""
    spec = {"description": "Specialist lens reviewer for one Lustre "
                           "commit; follows the lens instructions in its "
                           "prompt",
            "prompt": lens_prompt(diff),
            "tools": list(LENS_TOOLS)}
    if model:
        spec["model"] = model
    if effort:
        spec["effort"] = effort
    return json.dumps({LENS_AGENT: spec})


def lens_files(repo_dir: Path) -> list:
    return sorted((repo_dir / LENS_OUTPUT_DIR).glob("LENS-*.json"))


def record_preexisting(path: Path, results_dir: Path, slug: str,
                       change_label: str, sha: str) -> int:
    """Keep the pre-existing bugs a review reported: a copy beside the
    review's other files, and one line each in pre-existing.jsonl, the
    database across reviews.  Each line has a key from file and
    function (the line when there is no function): reviews word the
    same bug differently, so the key finds reports about the same
    place and the claims tell them apart.  Returns how many were
    recorded."""
    import hashlib
    import time
    try:
        issues = json.loads(path.read_text()).get("issues") or []
    except (OSError, ValueError, AttributeError):
        return 0
    issues = [i for i in issues if isinstance(i, dict)]
    if not issues:
        return 0
    (results_dir / f"pre-existing-{slug}.json").write_text(
        json.dumps({"issues": issues}, indent=1))
    with open(results_dir / "pre-existing.jsonl", "a") as db:
        for i in issues:
            where = i.get("function") or f"line {i.get('line')}"
            key = hashlib.sha256(json.dumps(
                [i.get("file"), where]).encode()).hexdigest()[:16]
            db.write(json.dumps({"key": key, "change": change_label, "sha": sha,
                                 "date": time.strftime("%Y-%m-%d"), **i}) + "\n")
    return len(issues)
