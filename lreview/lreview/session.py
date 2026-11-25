"""The Claude conversation of a review, so that chat can continue it.

Claude's prompt cache covers the tools and the system prompt as well as
the conversation: only a session started with the review's own flags
reads it, anything else pays for the whole conversation again.  So the
manifest records those flags with the session ID.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .agents import (CLAUDE_SOURCE_ARGS, LEAN_CLAUDE_ENV,
                     claude_session_dir, lean_claude_args,
                     plugins_off_settings)


@dataclass(frozen=True)
class ReviewSession:
    """A review's Claude session."""
    session_id: str
    # what it reviewed: "ps<N> <sha12>" or "commit <sha12>"
    reviewed: str
    # session_options(), or {} for a review that did not record them
    options: dict = field(default_factory=dict)


def session_options(config, change) -> dict:
    """The options of a claude review that shape its cached prefix."""
    from .runner import _preloading, preload_file
    return {
        "model": config.model, "effort": config.effort, "lean": config.lean,
        "preload": (preload_file(config).name
                    if _preloading(config, change) else None),
        "lenses": config.lenses, "lens_model": config.lens_model,
        "lens_effort": config.lens_effort,
        "agent_args": list(config.agent_args),
    }


def review_session(entry: dict, results_dir: Path,
                   change) -> Optional[ReviewSession]:
    """The session of a manifest entry's review, or None.

    Entries from before the manifest recorded it have the ID in their
    log, without the options."""
    if entry.get("agent", "claude") != "claude":
        return None
    from .runner import parse_session_id, reviewed_label
    record = entry.get("session") or {}
    session_id = record.get("id")
    if not session_id and entry.get("log"):
        session_id = parse_session_id(results_dir / entry["log"])
    if not session_id:
        return None
    options = {k: v for k, v in record.items() if k != "id"}
    if not options:  # the model and effort are known all the same
        options = {"model": entry.get("model"), "effort": entry.get("effort"),
                   "lean": True, "lenses": bool(entry.get("lenses"))}
    return ReviewSession(session_id, reviewed_label(change), options)


def launch(session: ReviewSession, worktree: Path,
           results_dir: Path) -> tuple[list[str], dict]:
    """(claude arguments, environment) that continue the session with
    the review's tools and system prompt.  Model and effort are the
    caller's."""
    return launch_options(session.options, worktree, results_dir)


def launch_options(opts: dict, worktree: Path,
                   results_dir: Path) -> tuple[list[str], dict]:
    args = list(CLAUDE_SOURCE_ARGS)
    env: dict = {}
    extra_tools: tuple = ()
    if opts.get("lenses"):
        from .lenses import LENS_REVIEW_TOOLS, agents_json, commit_diff
        from .runner import LENS_ENV
        extra_tools = LENS_REVIEW_TOOLS
        args += ["--agents", agents_json(
            opts.get("lens_model") or opts.get("model"),
            opts.get("lens_effort"), commit_diff(worktree))]
        env.update(LENS_ENV)
    if opts.get("lean"):
        off = plugins_off_settings()
        if off:
            args += ["--settings", json.dumps(off)]
        # The one review flag a user would miss: it takes /exit and
        # /model with it.  It is not part of the cached prefix.
        args += [a for a in lean_claude_args(extra_tools)
                 if a != "--disable-slash-commands"]
        env.update(LEAN_CLAUDE_ENV)
    preload = opts.get("preload") and results_dir / opts["preload"]
    if preload and preload.is_file():
        args += ["--append-system-prompt-file", str(preload)]
    return args + list(opts.get("agent_args") or []), env


def lens_transcripts(session_id: str) -> Optional[Path]:
    """Where Claude keeps the lens subagents' own conversations."""
    base = claude_session_dir(session_id)
    path = base / "subagents" if base else None
    return path if path and path.is_dir() else None
