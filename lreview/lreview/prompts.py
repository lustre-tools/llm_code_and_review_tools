"""Locating the review-prompts kernel prompts.

Reviews are initiated with a plain prompt referencing review-core.md
directly (per the review-prompts README quick start and its own
automation in kernel/scripts/review_one.sh) — no /kreview slash
command and no setup.sh skill installation are required. All lreview
needs is a clone of https://github.com/verygreen/review-prompts/.

The prompts directory (the one containing review-core.md and
gerrit-review.md) is resolved in this order:
1. --prompts-dir / $REVIEW_PROMPTS_DIR (repo root or its kernel/ dir)
2. the review-prompts submodule bundled with this repository
   (initialized by install.sh, or `git submodule update --init`)
3. the path referenced by a legacy ~/.claude/commands/kreview.md from
   an earlier setup.sh skill install, if present
4. ~/review-prompts/kernel
"""

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REVIEW_PROMPTS_REPO = "https://github.com/verygreen/review-prompts/"

CORE_PROMPT = "review-core.md"
GERRIT_PROMPT = "gerrit-review.md"

# Repository root when lreview runs from an editable install / git
# checkout — the bundled review-prompts submodule lives there.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# First absolute *.md path in a legacy installed command file
_PROMPT_PATH_RE = re.compile(r"(/\S+\.md)")


@dataclass
class PromptsStatus:
    """Result of the review-prompts availability check."""
    available: bool
    agent: str = "claude"
    agent_cli: Optional[str] = None
    prompts_dir: Optional[Path] = None
    source: Optional[str] = None  # how the dir was found
    problems: list[str] = field(default_factory=list)


def _kernel_dir(candidate: Path) -> Optional[Path]:
    """Return the directory holding review-core.md for a candidate
    path that may be the repo root or the kernel/ dir itself."""
    for d in (candidate / "kernel", candidate):
        if (d / CORE_PROMPT).is_file():
            return d
    return None


def _legacy_command_dir() -> Optional[Path]:
    """Prompts dir referenced by an old setup.sh skill install."""
    command_file = Path.home() / ".claude" / "commands" / "kreview.md"
    if not command_file.is_file():
        return None
    match = _PROMPT_PATH_RE.search(command_file.read_text())
    if not match:
        return None
    prompt = Path(match.group(1))
    if prompt.is_file():
        return prompt.parent
    return None


def _bundled_dir() -> Optional[Path]:
    """The review-prompts submodule bundled with this repository."""
    return _kernel_dir(_REPO_ROOT / "review-prompts")


def find_prompts_dir(explicit: Optional[Path] = None):
    """Resolve the prompts directory; returns (dir, source) or
    (None, None)."""
    if explicit:
        found = _kernel_dir(Path(explicit).expanduser())
        return found, (f"--prompts-dir/$REVIEW_PROMPTS_DIR ({explicit})"
                       if found else None)

    bundled = _bundled_dir()
    if bundled:
        return bundled, f"bundled submodule ({_REPO_ROOT / 'review-prompts'})"

    legacy = _legacy_command_dir()
    if legacy:
        return legacy, "legacy ~/.claude/commands/kreview.md install"

    default = Path.home() / "review-prompts"
    found = _kernel_dir(default)
    if found:
        return found, str(default)
    return None, None


def check_prompts(explicit: Optional[Path] = None,
                  agent: str = "claude") -> PromptsStatus:
    """Check the agent CLI and the review prompts are available."""
    status = PromptsStatus(available=False, agent=agent)

    status.agent_cli = shutil.which(agent)
    if not status.agent_cli:
        status.problems.append(
            f"{agent} CLI not found on PATH (install it first)")

    prompts_dir, source = find_prompts_dir(explicit)
    if prompts_dir is None:
        status.problems.append(
            "review-prompts not found (looked at --prompts-dir/"
            "$REVIEW_PROMPTS_DIR, a legacy kreview.md install, and "
            "~/review-prompts)")
    else:
        status.prompts_dir = prompts_dir
        status.source = source
        gerrit_prompt = prompts_dir / GERRIT_PROMPT
        if not gerrit_prompt.is_file():
            status.problems.append(
                f"{gerrit_prompt} missing — the review cannot produce "
                "gerrit-review.json without it")

    status.available = not status.problems
    return status


def _has_bundled_submodule() -> bool:
    """True when this is a git checkout with the review-prompts
    submodule registered (possibly not yet initialized)."""
    gitmodules = _REPO_ROOT / ".gitmodules"
    return (gitmodules.is_file()
            and "review-prompts" in gitmodules.read_text()
            and (_REPO_ROOT / ".git").exists())


def _init_bundled_submodule() -> bool:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "submodule", "update", "--init",
         "review-prompts"])
    return result.returncode == 0 and _bundled_dir() is not None


def setup_instructions(dest: Optional[Path] = None) -> str:
    """Return the manual setup steps for the review prompts."""
    dest = dest or Path.home() / "review-prompts"
    lines = ["To set up the review prompts:"]
    if _has_bundled_submodule():
        lines += [
            "initialize the bundled submodule:",
            f"  git -C {_REPO_ROOT} submodule update --init review-prompts",
            "or clone the repository elsewhere:",
        ]
    else:
        lines.append("clone the repository:")
    lines += [
        f"  git clone {REVIEW_PROMPTS_REPO} {dest}",
        "and point lreview at a non-default location with:",
        f"  export REVIEW_PROMPTS_DIR={dest}",
        "(no skill installation is needed — lreview references "
        "review-core.md directly)",
    ]
    return "\n".join(lines)


def offer_setup(dest: Optional[Path] = None) -> bool:
    """Interactively offer to set up the review prompts — by
    initializing the bundled submodule when available, else by
    cloning the repository.

    Returns True if setup succeeded. Non-interactive sessions just
    get the instructions printed and False back.
    """
    dest = dest or Path.home() / "review-prompts"

    if not sys.stdin.isatty():
        print(setup_instructions(dest))
        return False

    if _has_bundled_submodule():
        answer = input(
            "Initialize the bundled review-prompts submodule "
            f"({_REPO_ROOT / 'review-prompts'})? [Y/n] ").strip().lower()
        if answer not in ("n", "no"):
            if _init_bundled_submodule():
                return True
            print("submodule init failed; falling back to a clone")

    if dest.is_dir() and _kernel_dir(dest):
        print(f"Found existing review-prompts clone at {dest}")
        return True

    answer = input(
        f"Clone {REVIEW_PROMPTS_REPO} into {dest}? [y/N] ").strip().lower()
    if answer != "y":
        print(setup_instructions(dest))
        return False
    try:
        subprocess.run(
            ["git", "clone", REVIEW_PROMPTS_REPO, str(dest)], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"clone failed: {exc}")
        print(setup_instructions(dest))
        return False
    return True
