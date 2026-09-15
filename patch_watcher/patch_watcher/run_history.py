"""What earlier runs on this work already did, for the next agent.

Every run starts with no memory of the ones before it.  That is right for the
checkout, which is reset, and wrong for the thinking: a run has repeatedly
re-derived a conclusion a previous run reached, argued with a reviewer who had
already been answered, or made an edit an earlier run had made and lost.

So each run is handed the closing summary of the ones before it, newest first,
and the run id it can read in full if a summary is not enough.  Summaries
rather than transcripts because a transcript is tens of thousands of tokens
and a run has a wall-clock budget; the transcript is one command away when the
summary raises a question.

"Closing summary" is whatever the run actually left behind: the report it
wrote, or the last thing it said.  The second is not an accident -- every run
is asked to keep its most recent message a standing account of itself,
precisely because the runs whose history is most worth having are the ones
killed by a deadline or a restart, which never reach a closing step at all.
"""

from __future__ import annotations

import argparse
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from patch_watcher.session_state import (
    DEFAULT_SESSION_DATABASE,
    SessionStateStore,
)

MAX_SUMMARY_CHARS = 1200
DEFAULT_LIMIT = 8


@dataclass(frozen=True)
class PriorRun:
    """One finished run, as the next agent needs to see it."""

    run_id: str
    patch_id: str
    ended_at: datetime
    outcome: str
    summary: str
    source: str
    failure: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "patch_id": self.patch_id,
            "ended_at": self.ended_at.isoformat(),
            "outcome": self.outcome,
            "summary": self.summary,
            "summary_source": self.source,
            "failure": self.failure,
        }


def _closing_summary(store, session) -> tuple[str, str]:
    """What this run left behind, where that came from, and why it stopped.

    The failure reason is deliberately NOT the summary.  "shell interpreters
    are not permitted in safe commands" is a fact about the machinery and
    tells the next agent nothing; the same run's last message was "patchset 6
    uploaded, replies posted", which tells it everything.  So the work comes
    first -- the report if there is one, otherwise where the run had got to --
    and the reason it stopped is carried alongside rather than instead.
    """
    terminal = None
    try:
        terminal = store.get_terminal_result(session.session_id)
    except Exception:
        terminal = None
    failure = str(getattr(terminal, "failure_summary", "") or "").strip()
    failure = " ".join(failure.split())[:MAX_SUMMARY_CHARS]
    result = getattr(terminal, "result", None) if terminal is not None else None
    if isinstance(result, Mapping):
        summary = str(result.get("summary") or "").strip()
        if summary:
            return summary[:MAX_SUMMARY_CHARS], "report", failure
    try:
        messages = store.recent_messages(session.session_id, limit=5)
    except Exception:
        messages = []
    for message in reversed(messages):
        body = str(getattr(message, "body", "") or "").strip()
        if body:
            return body[:MAX_SUMMARY_CHARS], "last message", failure
    return failure, ("failure" if failure else "nothing recorded"), failure


def prior_runs(
    store,
    patch_ids: Collection[object],
    *,
    exclude_run_id: str = "",
    limit: int = DEFAULT_LIMIT,
) -> list[PriorRun]:
    """Finished runs on any of these changes, newest first.

    ``patch_ids`` is a collection because work is handled as a group: a run on
    one patch of a series is the previous run on the whole series, and hiding
    it from the next agent would be the same mistake as handing the agent one
    change and asking about the chain.
    """
    if store is None:
        return []
    wanted = {str(value) for value in patch_ids if str(value)}
    if not wanted:
        return []
    try:
        sessions = store.list_sessions(include_terminal=True)
    except Exception:
        return []
    finished = [
        session for session in sessions
        if str(session.patch_id) in wanted
        and session.run_id != exclude_run_id
        and getattr(session, "state", "") in {
            "succeeded", "failed", "cancelled", "stale", "resource_exhausted",
        }
    ]
    finished.sort(key=lambda item: item.state_changed_at, reverse=True)
    runs = []
    for session in finished[: max(0, int(limit))]:
        summary, source, failure = _closing_summary(store, session)
        runs.append(
            PriorRun(
                run_id=session.run_id,
                patch_id=str(session.patch_id),
                ended_at=session.state_changed_at,
                outcome=str(session.state),
                summary=summary,
                source=source,
                failure=failure,
            )
        )
    return runs


def render_prior_runs(runs: Collection[PriorRun]) -> str:
    """The prompt section, or empty when there is nothing to say.

    Empty rather than "no previous runs": a heading that says nothing is a
    heading the agent has to read to discover it can be ignored.
    """
    items = list(runs)
    if not items:
        return ""
    lines = [
        "Runs that came before this one, newest first. They worked on the same "
        "change or its group. Read these before re-deriving anything: a "
        "conclusion already reached, a reviewer already answered, or an edit "
        "already made is work you do not need to do twice.",
        "",
        "Each is a summary, not a transcript. When a summary raises a question "
        "the answer is in the full run: `pw-transcript <run id>` prints its "
        "messages and its report. Where a run reported, this is its own "
        "account; where it was stopped first, this is the standing summary it "
        "was asked to keep, so it is where that run had got to rather than a "
        "conclusion it reached.",
        "",
    ]
    for run in items:
        stopped = f", stopped by: {run.failure}" if run.failure else ""
        lines.append(
            f"- `{run.run_id}` on change {run.patch_id}, "
            f"{run.ended_at:%Y-%m-%d %H:%M}Z, ended {run.outcome}"
            f" ({run.source}{stopped}):"
        )
        body = " ".join(str(run.summary).split()) or "(nothing recorded)"
        lines.append(f"  {body}")
    return "\n".join(lines)


def transcript(store, run_id: str, *, limit: int = 0) -> str:
    """One finished run, in full, for an agent that wants more than a summary.

    The run's own files are gone by the time anyone asks: terminal cleanup
    deletes the run directory, and with it the stream the CLI wrote.  What
    survives is the session store, which has every message and every runner
    event, so that is what this reads.
    """
    wanted = str(run_id or "").strip()
    if store is None or not wanted:
        return f"No run named {wanted!r}."
    session = next(
        (
            item for item in store.list_sessions(include_terminal=True)
            if item.run_id == wanted
        ),
        None,
    )
    if session is None:
        return f"No run named {wanted!r}."
    lines = [
        f"# {session.run_id}",
        f"change {session.patch_id} patchset {session.patchset} "
        f"revision {session.revision}",
        f"started {session.started_at:%Y-%m-%d %H:%M:%S}Z, "
        f"ended {session.state_changed_at:%Y-%m-%d %H:%M:%S}Z as {session.state}",
        "",
    ]
    summary, source, failure = _closing_summary(store, session)
    if summary:
        lines += [f"## Closing summary ({source})", "", summary, ""]
    if failure:
        lines += ["## Why it stopped", "", failure, ""]
    messages = store.recent_messages(
        session.session_id, limit=limit if limit > 0 else 200
    )
    lines.append("## Messages")
    lines.append("")
    for message in messages:
        lines.append(
            f"[{message.created_at:%H:%M:%S}] {message.author}: "
            + " ".join(str(message.body or "").split())
        )
    terminal = store.get_terminal_result(session.session_id)
    result = getattr(terminal, "result", None) if terminal is not None else None
    if isinstance(result, Mapping) and result.get("comment_results"):
        lines += ["", "## What it decided per comment", ""]
        for item in result["comment_results"]:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"- {item.get('comment_id')}: {item.get('disposition')} -- "
                + " ".join(str(item.get("summary") or "").split())
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pw-transcript",
        description="Print a finished Patch Watcher run: what it said and what it concluded.",
    )
    parser.add_argument("run_id", help="the run id, as given in the run history")
    parser.add_argument(
        "--database", default=None, help="session store to read (defaults to the live one)"
    )
    arguments = parser.parse_args(argv)
    store = SessionStateStore(arguments.database or DEFAULT_SESSION_DATABASE)
    print(transcript(store, arguments.run_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
