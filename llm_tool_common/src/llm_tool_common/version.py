"""The version of the tools: one number for the whole checkout.

Every tool's pyproject.toml carries the same X.Y.0, changed only by
``make bump``; no other commit touches it, so two changes to one tool do
not conflict over a version line.  What tells two builds apart is the
checkout's git history: the number of commits since that bump takes the
place of the 0, and the commit itself follows, as in 0.6.25_g445db69.

It is worked out when asked for, not when a tool is imported: it costs a
few git calls.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable
from importlib import metadata
from typing import Any

from . import update_check

# The file whose version line marks the bump; every tool's changes with it.
_ANCHOR = "llm_tool_common/pyproject.toml"


def _git(*args: str) -> str | None:
    try:
        r = update_check._git("-C", str(update_check._REPO), *args, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _is_checkout() -> bool:
    """Whether this file runs from a git checkout of the tools."""
    repo = update_check._REPO
    return (repo / ".git").exists() and (repo / "install.sh").is_file()


def _base() -> str:
    """The X.Y.0 of the last bump.

    From the checkout when there is one: an editable install's metadata
    keeps the version of the last ``pip install``.
    """
    if _is_checkout():
        try:
            text = (update_check._REPO / _ANCHOR).read_text()
        except OSError:
            text = ""
        match = re.search(r'^version = "([^"]+)"$', text, re.M)
        if match:
            return match.group(1)
    try:
        return metadata.version("llm-tool-common")
    except metadata.PackageNotFoundError:
        return "unknown"


def _commits_since_bump(base: str, rev: str) -> int | None:
    """Commits in *rev* since the one that set *base*, if git can tell."""
    # A shallow clone's oldest commit looks like the one that set it.
    if _git("rev-parse", "--is-shallow-repository") != "false":
        return None
    bump = _git("log", "-1", "--format=%H", f'-Sversion = "{base}"',
                "HEAD", "--", _ANCHOR)
    if not bump:
        return None
    count = _git("rev-list", "--count", f"{bump}..{rev}")
    return int(count) if count and count.isdigit() else None


def _numbered(base: str, count: int | None) -> str:
    match = re.fullmatch(r"(\d+\.\d+)\.0", base)
    if not match or count is None:
        return base
    return f"{match.group(1)}.{count}"


def repo_version() -> str:
    """The version to show, e.g. ``0.6.25_g445db69``; ``0.6.0`` at the bump."""
    base = _base()
    sha = _git("rev-parse", "--short=7", "HEAD") if _is_checkout() else None
    if not sha:
        return base
    count = _commits_since_bump(base, "HEAD")
    if count == 0:
        return base
    return f"{_numbered(base, count)}_g{sha}"


def published_version() -> str:
    """The version without a commit, counting only what is on GitHub's main.

    This is what telemetry sends: a commit that exists only here is
    nobody else's business.
    """
    base = _base()
    if not _is_checkout():
        return base
    shared = _git("merge-base", "HEAD", f"origin/{update_check.BRANCH}")
    return _numbered(base, _commits_since_bump(base, shared) if shared else None)


class VersionAction(argparse.Action):
    """argparse's ``version`` action, asking only when the option is given."""

    def __init__(self, option_strings: list[str], dest: str, **_: Any):
        super().__init__(option_strings, argparse.SUPPRESS, nargs=0,
                         default=argparse.SUPPRESS,
                         help="show program's version number and exit")

    def __call__(self, parser: argparse.ArgumentParser, *_: Any) -> None:
        # Not parser.exit(message): that prints to stderr.
        print(f"{parser.prog} {repo_version()}")
        parser.exit()


def version_option(prog_name: str) -> Callable[[Any], Any]:
    """click's ``version_option``, asking only when the option is given."""
    import click

    def show(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
        if value and not ctx.resilient_parsing:
            click.echo(f"{prog_name}, version {repo_version()}")
            ctx.exit()

    return click.option("--version", is_flag=True, expose_value=False,
                        is_eager=True, callback=show,
                        help="Show the version and exit.")
