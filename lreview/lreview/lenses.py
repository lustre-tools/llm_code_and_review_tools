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
import re
from pathlib import Path
from typing import Optional

LENS_PROMPT_PATH = Path(__file__).resolve().parent / "lens.md"
# TASK 1D says "five": keep them in step.
LENSES = ("concurrency", "lifecycle", "data", "semantics", "build")
LENS_AGENT = "lens"
LENS_AGENTS = ("claude", "agy")
# What a lens runs with: it reads code and writes its LENS file.
LENS_TOOLS = ("Bash", "Read", "Write", "Grep", "Glob")
# agy custom subagent frontmatter accepts only these four model tiers;
# a specific model slug causes agy to silently ignore the agent file.
AGY_SUBAGENT_MODELS = ("inherit", "flash_lite", "flash", "pro")
AGY_LENS_TOOLS = ("run_command", "read_file", "write_file", "edit_file",
                  "grep_search", "find_by_name")
AGY_LENS_AGENT_REL = Path(".agents") / "agents" / f"{LENS_AGENT}.md"
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

def validate_lens_options(agent: str, mode: str,
                          lens_model: Optional[str] = None,
                          lens_effort: Optional[str] = None) -> Optional[str]:
    """Return an error string if a --lenses combination is unsupported,
    else None."""
    if agent not in LENS_AGENTS or mode != "full":
        return "--lenses needs the claude or agy agent and --mode full"
    if agent == "agy":
        if lens_effort:
            return ("--lens-effort is claude-only "
                    "(agy subagents inherit the review's effort)")
        if lens_model and lens_model not in AGY_SUBAGENT_MODELS:
            return (f"--lens-model for agy must be one of "
                    f"{', '.join(AGY_SUBAGENT_MODELS)}, got {lens_model!r}")
    return None


def task_text(prompts_dir: Path, agent: str = "claude") -> str:
    """TASK 1D: start the lenses, then settle what they find."""
    names = ", ".join(f"`{lens}`" for lens in LENSES)
    if agent == "agy":
        head = f"""### TASK 1D: Specialist lenses (mandatory when the invoke_subagent tool is available)

One reviewer carrying every check through every change misses bugs that a
focused pass finds.  Do this for every change, however small: the size of
a change says little about its bugs.  After TASK 1C, launch five specialist subagents **in a
single message** (five `invoke_subagent` tool calls, so they run in parallel), one per
lens: {names}.  Use `TypeName: "{LENS_AGENT}"` (when that subagent type is
available, else `"self"`), `Workspace: "inherit"`, and a distinct `Role` per
lens (for example `"Concurrency Lens Reviewer"`).  Each `Prompt` says:

    Follow the lens instructions in your system prompt (if it has none,
    read {LENS_PROMPT_PATH} and follow that).
    Lens: <lens>
    Prompt directory: {prompts_dir}
    Commit: HEAD of the repository in <current directory>
    Output directory: <current directory>/{LENS_OUTPUT_DIR}
    Changed functions and CHANGE categories: <your TASK 1C list>

Give both directories as absolute paths.  After launching all five
subagents, end your turn with no further tool calls (do not call
`schedule`, `manage_subagents`, or poll the directory, and do not start
Task 2 while lenses are still running): the system wakes you
automatically as each subagent finishes.  If woken up before all five
have reported back, end your turn again with no tool calls until all five
have completed.  Once all five have reported back, read every
`./{LENS_OUTPUT_DIR}/LENS-*.json`.  A lens candidate is not evidence by
itself and a lens's silence proves nothing; still do your own Task 2
analysis.

"""
        skip_line = "\nIf the invoke_subagent tool is not available, skip this task.\n\n"
    else:
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
        skip_line = "\nIf the Agent tool is not available, skip this task.\n\n"
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
    return head + settle + skip_line


def with_task(protocol: str, prompts_dir: Path, agent: str = "claude") -> str:
    """review-core.md with TASK 1D in front of Task 2, where it was
    measured; at the end when that heading has moved."""
    task = task_text(prompts_dir, agent=agent)
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


def agy_lens_agent_markdown(model: Optional[str] = None,
                            diff: Optional[str] = None) -> str:
    """<worktree>/.agents/agents/lens.md for an agy --lenses review."""
    tier = model or "inherit"
    if tier not in AGY_SUBAGENT_MODELS:
        raise ValueError(
            f"--lens-model for agy must be one of "
            f"{', '.join(AGY_SUBAGENT_MODELS)}, got {tier!r}")
    tools_yaml = "\n".join(f"  - {tool}" for tool in AGY_LENS_TOOLS)
    frontmatter = (
        "---\n"
        f"name: {LENS_AGENT}\n"
        "description: Specialist lens reviewer for one Lustre commit; "
        "follows the lens instructions in its system prompt\n"
        "subagent: true\n"
        "mainAgent: false\n"
        f"model: {tier}\n"
        "tools:\n"
        f"{tools_yaml}\n"
        "---\n\n"
    )
    return frontmatter + lens_prompt(diff)


def write_agy_lens_agent(worktree: Path, model: Optional[str] = None,
                         diff: Optional[str] = None) -> Path:
    """Write <worktree>/.agents/agents/lens.md for an agy --lenses review."""
    path = worktree / AGY_LENS_AGENT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    path.write_text(agy_lens_agent_markdown(model, diff))
    return path


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


# What chat gets of a lens review at most; a review writes 6 KB on average.
DOSSIER_MAX = 60_000
_LENS_FILE_RE = re.compile(r"LENS-(%s)\.json" % "|".join(LENSES))
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\1\b", re.S)
_VERDICT_RE = re.compile(
    r"LENS DISPOSITIONS|^LENSES: |\b(?:CONFIRMED|ELIMINATED|PRE-EXISTING|"
    r"UNSETTLED)\b", re.M)


def dossier(log_path: Path) -> str:
    """What the lenses suspected and how the reviewer settled it, from
    a claude review's log; "" when the log has none.  The lenses' files
    themselves go with the review's worktree."""
    candidates: dict = {}
    verdicts: list = []
    try:
        lines = open(log_path, errors="replace")
    except OSError:
        return ""
    with lines:
        for line in lines:
            if "LENS" not in line and not _VERDICT_RE.search(line):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if event.get("parent_tool_use_id"):
                    if block.get("type") == "tool_use":
                        found = _lens_write(block.get("input") or {})
                        if found:
                            candidates[found[0]] = found[1]
                elif (block.get("type") == "text"
                        and _VERDICT_RE.search(block["text"])):
                    verdicts.append(block["text"].strip())
    parts = [f"### {lens}\n{candidates[lens]}"
             for lens in LENSES if lens in candidates]
    if not parts:
        return ""
    text = "## What the lenses suspected\n\n" + "\n\n".join(parts)
    text += "\n\n## How the reviewer settled it\n\n" + (
        "\n\n".join(verdicts) or "The review's log has no verdicts.")
    if len(text) > DOSSIER_MAX:
        text = text[:DOSSIER_MAX] + "\n[cut]"
    return text


def _lens_write(tool_input: dict) -> Optional[tuple]:
    """(lens, JSON text) when a lens's tool call writes its file."""
    if "file_path" in tool_input:
        where, text = tool_input["file_path"], tool_input.get("content")
    else:
        where = command = tool_input.get("command") or ""
        heredoc = _HEREDOC_RE.search(command)
        if heredoc:
            text = heredoc.group(2)
        else:  # echoed, or written by a script that holds the JSON
            text = (command[command.find("{"):command.rfind("}") + 1]
                    if "candidates" in command else None)
    match = _LENS_FILE_RE.search(where)
    if not match or not text:
        return None
    return match.group(1), text.strip()
