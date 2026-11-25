"""Interactive discussion sessions over existing review results.

`lreview chat <change>` checks the reviewed revision out into a
worktree and starts an interactive claude session 
that continues a fork of the review's own conversation (session.py).
Without one -- Claude lost it, another agent, or --no-resume -- the
session starts from the collected artifacts instead: findings JSON,
report, and from a lens review's log what each lens suspected and how
the reviewer settled it.  `lreview run --chat` goes on in the review's
conversation as soon as the review ends.
The reviewed revision comes from the manifest when the change was
reviewed before — the session then matches the report byte-for-byte
and needs no network — and is resolved from Gerrit otherwise.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from .defaults import DEFAULT_AGENT
from .gerrit import LocalChange, ResolvedChange, change_ref
from .manifest import read_summary
from .runner import (STATUS_CLEAN, STATUS_FINDINGS, BatchConfig,
                     artifact_tag, prepare_worktree, reviewed_label)
from .session import (ReviewSession, launch, launch_options,
                      lens_transcripts, review_session)
from . import worktree as wt


def manifest_entries(summary: dict, number: int) -> list[tuple[str, dict]]:
    """(key, entry) pairs for a change, newest patchset first; full
    mode before light within the same patchset."""
    out = []
    for key in (str(number), f"{number}-light"):
        entry = summary.get(key)
        if entry:
            out.append((key, entry))
    out.sort(key=lambda pair: (-(pair[1].get("patchset") or 0),
                               pair[1].get("mode", "full") != "full"))
    return out


def artifact_lines(results_dir: Path, db_dir: Optional[Path],
                   change, entries) -> list[str]:
    """Bullet lines naming the existing artifacts of this change."""
    lines = []
    for _key, entry in entries:
        mode = entry.get("mode", "full")
        tag = artifact_tag(mode)
        named = [
            ("review findings JSON (only when findings)",
             entry.get("json")),
            ("human-readable report", entry.get("markdown")),
            ("severity metadata",
             f"review-metadata-{change.slug}{tag}.json"),
            ("pre-existing bugs the review noticed",
             f"pre-existing-{change.slug}{tag}.json"),
            ("review event log (JSONL, large — only for provenance "
             "questions)", entry.get("log")),
        ]
        for label, name in named:
            if not name:
                continue
            path = results_dir / name
            if path.is_file():
                lines.append(f"- {mode} {label}: {path}")
    if db_dir is not None:
        from .memory import find_doc
        memory = find_doc(db_dir, change)
        if memory:
            lines.append(
                "- review memory document (accumulated analysis across "
                "runs and patchsets: mechanism notes, verified-OK "
                f"areas, eliminated false positives, review-thread "
                f"dispositions): {memory}")
    return lines


def chat_prompt(change, artifacts: list[str], session=None,
                worktree: Optional[Path] = None, report: str = "",
                dossier: str = "", transcripts: Optional[Path] = None) -> str:
    """The chat's first message. Continuing the review's own session,
    it says where the code is now: the conversation remembers the
    review's worktree, which is gone."""
    if change.number is None:
        ref_name = getattr(change, "ref_name", "local")
        what = (f"local commit {ref_name} ({change.sha[:12]}) — "
                f"\"{change.subject}\"")
    else:
        where = (f"patchset {change.patchset}" if change.patchset
                 else f"commit {change.sha[:12]}")
        what = (f"Gerrit change {change.number} {where} — "
                f"\"{change.subject}\"")
    header = (f"Interactive discussion of {what}. The change is checked "
              "out at HEAD of this worktree; `git show HEAD` is the "
              "patch under discussion.")
    if session is not None:
        header = (f"The review of {what} is finished (it looked at "
                  f"{session.reviewed}): you are continuing it as an "
                  "interactive discussion with the user, so the review "
                  "protocol and its output files no longer apply. The "
                  "review's "
                  "worktree is gone: the change is checked out at HEAD "
                  f"of {worktree}; `git show HEAD` is the patch under "
                  "discussion.")
    if artifacts:
        listing = ("Artifacts from earlier lreview runs of this "
                   "change:\n" + "\n".join(artifacts))
        opening = ("Start by reading the patch and the report, then give "
                   "a short summary of what the patch does and the "
                   "findings, and wait for questions.")
        if session is not None:
            opening = ("From what you already have, give a short summary "
                       "of what the patch does and the findings, and wait "
                       "for questions.")
    else:
        listing = ("No collected review artifacts were found for this "
                   "change — work from the checkout alone.")
        opening = ("Start by reading the patch, then give a short "
                   "summary of what it does and wait for questions.")
    if transcripts is not None:
        listing += (f"\nThe lens subagents' own conversations: "
                    f"{transcripts}/*.jsonl")
    if report:
        listing += f"\n\nThe report:\n\n{report}"
    if dossier:
        listing += ("\n\nThis was a lens review. The lenses' suspicions "
                    "and the reviewer's verdicts, from its log:\n\n"
                    + dossier)
    rules = ("When discussing a finding, verify every claim against "
             "the code in this worktree instead of trusting the "
             "report. Ground rules: do not modify the checkout, never "
             "post to Gerrit or write to JIRA, and leave a listed memory "
             "document alone unless asked to record something.")
    return f"{header}\n\n{listing}\n\n{opening} {rules}"


def local_entries(summary: dict, sha: str, ref: str):
    """Local-review manifest entries for a ref: exact-sha matches
    first (the reviewed revision is checked out), else entries whose
    ref name matches (the ref moved since — e.g. amended); full mode
    before light."""
    matches = [(k, e) for k, e in summary.items()
               if e.get("local") and e.get("sha") == sha]
    moved = not matches
    if moved:
        matches = [(k, e) for k, e in summary.items()
                   if e.get("local") and e.get("ref_name") == ref]
    matches.sort(key=lambda pair: (
        pair[1].get("reviewed_at") or "", pair[1].get("mode") == "full"),
        reverse=True)
    return matches, moved


def _resolve_local_target(spec, summary, repo: Path):
    """(change, entries) for a --local chat, or (None, error-str)."""
    ref = str(spec or "HEAD")
    try:
        sha = wt.rev_parse(repo, ref)
    except Exception as exc:
        return None, f"cannot resolve local ref '{ref}': {exc}"
    entries, moved = local_entries(summary, sha, ref)
    if entries:
        entry = entries[0][1]
        change = LocalChange(
            ref_name=entry.get("ref_name") or ref, sha=entry["sha"],
            subject=entry.get("subject") or "",
            change_id=wt.commit_change_id(repo, entry["sha"]))
        print(f"  {change.slug}  {change.subject[:60]}")
        print(f"  reviewed revision {change.sha[:12]} "
              f"({entry.get('mode', 'full')} review, "
              f"status {entry.get('status')})")
        if moved:
            print(f"  note: {ref} now points at {sha[:12]} — "
                  "discussing the reviewed revision; re-run the "
                  "review for the new state")
    else:
        change = LocalChange(
            ref_name=ref, sha=sha,
            subject=wt.commit_subject(repo, sha),
            change_id=wt.commit_change_id(repo, sha))
        print(f"  {change.slug}  {change.subject[:60]}")
        print("  no review artifacts on record — starting from the "
              "patch alone")
    return (change, entries), None


def run_chat(
    spec,
    results_dir: Path,
    repo: Optional[Path] = None,
    worktrees_dir: Optional[Path] = None,
    db_dir: Optional[Path] = None,
    agent: str = DEFAULT_AGENT,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    agent_args: Optional[list[str]] = None,
    local: bool = False,
    keep_worktree: bool = False,
    resume: bool = True,
) -> int:
    from .agents import get_agent
    agent_spec = get_agent(agent)  # fail fast on unknown agents

    try:
        summary = read_summary(results_dir)
    except FileNotFoundError:
        summary = {}

    if local:
        # A local ref needs the repo up front to resolve it — --repo
        # or the cwd, exactly like `run --local`.
        repo_source = "--repo" if repo else "cwd — pass --repo if wrong"
        repo = Path(repo or ".").expanduser().resolve()
        if not wt.is_git_repo(repo):
            print(f"error: {repo} is not a git repository "
                  f"(from {repo_source})")
            return 1
        target, error = _resolve_local_target(spec, summary, repo)
        if target is None:
            print(f"error: {error}")
            return 1
        change, entries = target
        print(f"  repo: {repo} ({repo_source})")
        if not wt.commit_exists(repo, change.sha):
            print(f"error: reviewed commit {change.sha[:12]} no longer "
                  f"exists in {repo} (rebased away?)")
            return 1
        return _launch(agent_spec, change, entries, repo, results_dir,
                       worktrees_dir, db_dir, model, effort, agent_args,
                       keep_worktree, resume)

    from gerrit_cli.client import GerritCommentsClient
    try:
        _, number = GerritCommentsClient.parse_gerrit_url(str(spec))
    except Exception:
        print(f"error: '{spec}' is not a change number or Gerrit URL")
        return 1

    entries = manifest_entries(summary, number)

    if entries:
        # Chat about the revision that was actually reviewed, so the
        # artifacts match the checkout — no network needed.
        entry = entries[0][1]
        patchset = entry.get("patchset")
        change = ResolvedChange(
            number=number,
            project=entry.get("repository") or "fs/lustre-release",
            subject=entry.get("subject") or f"change {number}",
            sha=entry["sha"], patchset=patchset,
            ref=change_ref(number, patchset) if patchset else "",
            base_url=(entry.get("base_url")
                      or os.environ.get("GERRIT_URL",
                                        "https://review.whamcloud.com")))
        print(f"  {number} ps{patchset}  {change.subject[:60]}")
        print(f"  reviewed revision {change.sha[:12]} "
              f"({entry.get('mode', 'full')} review, "
              f"status {entry.get('status')})")
    else:
        entry = {}
        from .gerrit import resolve_change
        try:
            change = resolve_change(spec)
        except Exception as exc:
            print(f"error: cannot resolve change '{spec}': {exc}")
            return 1
        print(f"  {change.number} ps{change.patchset}  "
              f"{change.subject[:60]}")
        print("  no review artifacts on record — starting from the "
              "patch alone")

    # Without --repo, prefer the repository the change was reviewed
    # from (recorded in the manifest) over the cwd — fetching a
    # Lustre change into whatever repo the shell happens to be in is
    # the footgun this avoids.
    repo_source = "--repo"
    if repo is None:
        if entry.get("repo"):
            repo, repo_source = Path(entry["repo"]), "review manifest"
        else:
            repo, repo_source = Path("."), "cwd — pass --repo if wrong"
    repo = Path(repo).expanduser().resolve()
    if not wt.is_git_repo(repo):
        print(f"error: {repo} is not a git repository "
              f"(from {repo_source})")
        return 1
    print(f"  repo: {repo} ({repo_source})")

    return _launch(agent_spec, change, entries, repo, results_dir,
                   worktrees_dir, db_dir, model, effort, agent_args,
                   keep_worktree, resume)


def _launch(agent_spec, change, entries, repo, results_dir,
            worktrees_dir, db_dir, model, effort, agent_args,
            keep_worktree, resume=True) -> int:
    if worktrees_dir is None:
        from .cli import default_worktrees_dir
        worktrees_dir = default_worktrees_dir(repo, results_dir)

    config = BatchConfig(repo=repo, results_dir=results_dir,
                         worktrees_dir=worktrees_dir)
    try:
        if not wt.commit_exists(config.repo, change.sha):
            print(f"  fetching {change.ref} "
                  "— a first fetch into this repo can take minutes...")
        print("  creating worktree...")
        worktree_dir = prepare_worktree(config, change)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:
        print(f"error: cannot check the change out: {exc}")
        return 1

    session = None
    if agent_spec.name == "claude" and resume:
        session = find_session(change, entries, results_dir, db_dir)
    return _interact(agent_spec, change, entries, worktree_dir, results_dir,
                     db_dir, model, effort, agent_args, session,
                     cleanup_repo=None if keep_worktree else config.repo)


def find_session(change, entries, results_dir, db_dir=None):
    """The review's Claude session, when Claude still has it."""
    from .agents import claude_session_exists
    session = (review_session(entries[0][1], results_dir, change)
               if entries else None)
    if session is None and db_dir is not None:
        # a review run with the deprecated --memory
        from .memory import find_doc, read_session
        doc = find_doc(db_dir, change)
        mode = entries[0][1].get("mode", "full") if entries else "full"
        session = read_session(doc, mode) if doc else None
        if session is not None:
            session = ReviewSession(session.session_id, session.reviewed)
    if session is None:
        return None
    if not claude_session_exists(session.session_id):
        print(f"  note: Claude no longer has the review session "
              f"{session.session_id}; starting a fresh one")
        return None
    print(f"  continuing a fork of the review session "
          f"{session.session_id} ({session.reviewed})")
    return session


def _interact(agent_spec, change, entries, worktree_dir, results_dir,
              db_dir, model, effort, agent_args, session,
              cleanup_repo=None) -> int:
    """Run the interactive session in worktree_dir, which is removed
    from cleanup_repo afterwards."""
    session_args, env, report, dossier, transcripts = [], {}, "", "", None
    if session is not None:
        # The review's model and effort: another model cannot read the
        # review's prompt cache.
        model = model or session.options.get("model")
        effort = effort or session.options.get("effort")
        session_args, env = launch(session, worktree_dir, results_dir)
        transcripts = lens_transcripts(session.session_id)
    else:
        if agent_spec.name == "claude":
            # as a review: nothing from the checkout or CLAUDE.md files,
            # which would also prompt in every new worktree
            session_args, env = launch_options({"lean": True}, worktree_dir,
                                               results_dir)
        # in the message itself: reading files outside the worktree
        # asks for permission
        if entries and entries[0][1].get("markdown"):
            try:
                report = (results_dir / entries[0][1]["markdown"]).read_text()
            except OSError:
                pass
        if entries and entries[0][1].get("log"):
            from .lenses import dossier as lens_dossier
            dossier = lens_dossier(results_dir / entries[0][1]["log"])

    prompt = chat_prompt(
        change, artifact_lines(results_dir, db_dir, change, entries),
        session=session, worktree=worktree_dir, report=report,
        dossier=dossier, transcripts=transcripts)
    cmd = agent_spec.build_interactive_cmd(
        model, list(agent_args or []), prompt, effort=effort,
        resume=session.session_id if session is not None else None,
        session_args=tuple(session_args))

    print(f"\nStarting interactive session in {worktree_dir}\n")
    try:
        return subprocess.call(cmd, cwd=str(worktree_dir),
                               env={**os.environ, **env})
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        if cleanup_repo is not None:
            wt.remove_worktree(cleanup_repo, worktree_dir)


def pick_result(results):
    """The finished claude review to discuss: the only one, else the
    one the user picks; None for none."""
    offered = [r for r in results
               if r.session_id and r.status in (STATUS_FINDINGS, STATUS_CLEAN)]
    if not offered:
        print("\nno finished claude review to discuss")
        return None
    if len(offered) == 1:
        return offered[0]
    print("\nDiscuss which review?")
    for i, r in enumerate(offered, 1):
        print(f"  {i}. {r.change.slug}  {r.status}"
              f" ({r.findings})  {r.change.subject[:50]}")
    while True:
        try:
            answer = input(f"1-{len(offered)}, Enter for none: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(offered):
            return offered[int(answer) - 1]


def chat_after_run(config, results, in_place: bool = False) -> int:
    """`run --chat`: go on in the review's own conversation."""
    result = pick_result(results)
    if result is None:
        return 0
    change = result.change
    summary = read_summary(config.results_dir)
    key = str(change.number) if change.number else change.slug
    key += artifact_tag(result.mode)
    entries = [(key, summary[key])] if key in summary else []
    # The review's own path again (it is named after change and pid):
    # the conversation's file names stay valid.
    worktree_dir = (config.repo if in_place
                    else prepare_worktree(config, change))
    session = ReviewSession(result.session_id, reviewed_label(change),
                            result.session_options or {})
    from .agents import get_agent
    return _interact(
        get_agent("claude"), change, entries, worktree_dir,
        config.results_dir, None, None, None, None, session,
        cleanup_repo=(None if in_place or config.keep_worktrees
                      else config.repo))
