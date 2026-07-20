"""Availability check and setup guidance for the kreview skill.

The kreview review prompts live in a separate repository
(https://github.com/verygreen/review-prompts/). Its setup.sh installs
the kreview command for a given agent (e.g. ~/.claude/commands/
kreview.md for claude, ~/.codex/prompts/kreview.md for codex) whose
text points at the review-core.md prompt inside the cloned repository.
This module checks that the whole chain is in place and, when it is
not, prints (and can interactively perform) the setup steps.
"""

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .agents import get_agent

REVIEW_PROMPTS_REPO = "https://github.com/verygreen/review-prompts/"

# First absolute *.md path in the installed command file is the prompt.
_PROMPT_PATH_RE = re.compile(r"(/\S+\.md)")


@dataclass
class SkillStatus:
    """Result of the kreview skill availability check."""
    available: bool
    agent: str = "claude"
    agent_cli: Optional[str] = None
    command_file: Optional[Path] = None
    prompt_path: Optional[Path] = None
    prompts_dir: Optional[Path] = None
    problems: list[str] = field(default_factory=list)


def check_skill(cmd_dir: Optional[Path] = None,
                agent: str = "claude") -> SkillStatus:
    """Check that the agent CLI, its kreview command, and the prompts
    it references all exist."""
    spec = get_agent(agent)
    cmd_dir = cmd_dir or spec.commands_dir()
    status = SkillStatus(available=False, agent=agent)

    status.agent_cli = shutil.which(agent)
    if not status.agent_cli:
        status.problems.append(
            f"{agent} CLI not found on PATH (install it first)")

    command_file = cmd_dir / "kreview.md"
    if not command_file.is_file():
        status.problems.append(
            f"/kreview slash command not installed ({command_file} missing)")
    else:
        status.command_file = command_file
        match = _PROMPT_PATH_RE.search(command_file.read_text())
        if not match:
            status.problems.append(
                f"{command_file} does not reference a prompt file")
        else:
            prompt = Path(match.group(1))
            status.prompt_path = prompt
            if not prompt.is_file():
                status.problems.append(
                    f"prompt file {prompt} missing (review-prompts repo "
                    "moved or not cloned?)")
            else:
                status.prompts_dir = prompt.parent
                gerrit_prompt = prompt.parent / "gerrit-review.md"
                if not gerrit_prompt.is_file():
                    status.problems.append(
                        f"{gerrit_prompt} missing — kreview cannot produce "
                        "gerrit-review.json without it")

    status.available = not status.problems
    return status


def setup_instructions(dest: Optional[Path] = None,
                       agent: str = "claude") -> str:
    """Return the manual setup steps for the kreview skill."""
    spec = get_agent(agent)
    dest = dest or Path.home() / "review-prompts"
    return (
        "To set up the kreview skill:\n"
        f"  git clone {REVIEW_PROMPTS_REPO} {dest}\n"
        f"  cd {dest} && ./setup.sh {agent} kernel\n"
        "This installs the kreview (and related) commands into "
        f"~/{spec.commands_subdir}/ with paths resolved to the clone "
        "location."
    )


def offer_setup(dest: Optional[Path] = None,
                agent: str = "claude") -> bool:
    """Interactively offer to clone review-prompts and run its setup.

    Returns True if setup was performed. Non-interactive sessions just
    get the instructions printed and False back.
    """
    dest = dest or Path.home() / "review-prompts"

    if not sys.stdin.isatty():
        print(setup_instructions(dest, agent))
        return False

    if dest.is_dir() and (dest / "setup.sh").is_file():
        print(f"Found existing review-prompts clone at {dest}")
    else:
        answer = input(
            f"Clone {REVIEW_PROMPTS_REPO} into {dest}? [y/N] ").strip().lower()
        if answer != "y":
            print(setup_instructions(dest, agent))
            return False
        try:
            subprocess.run(
                ["git", "clone", REVIEW_PROMPTS_REPO, str(dest)], check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"clone failed: {exc}")
            print(setup_instructions(dest, agent))
            return False

    answer = input(
        f"Run {dest}/setup.sh {agent} kernel to install the skill? "
        "[y/N] ").strip().lower()
    if answer != "y":
        print(setup_instructions(dest, agent))
        return False
    try:
        subprocess.run(
            ["./setup.sh", agent, "kernel"], cwd=str(dest), check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"setup.sh failed: {exc}")
        print(setup_instructions(dest, agent))
        return False
    return True
