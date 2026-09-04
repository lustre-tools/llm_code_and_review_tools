"""Interactive discussion sessions over existing review results.

`lreview chat <change>` checks the reviewed revision out into a
worktree and starts an interactive claude session primed with the
collected review artifacts (findings JSON, report, memory document),
so findings can be questioned and the patch explored conversationally.
The reviewed revision comes from the manifest when the change was
reviewed before — the session then matches the report byte-for-byte
and needs no network — and is resolved from Gerrit otherwise.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from .gerrit import LocalChange, ResolvedChange, change_ref
from .manifest import read_summary
from .runner import BatchConfig, artifact_tag, prepare_worktree
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


def chat_prompt(change, artifacts: list[str]) -> str:
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
    if artifacts:
        listing = ("Artifacts from earlier lreview runs of this "
                   "change:\n" + "\n".join(artifacts))
        opening = ("Start by reading the report and the memory document "
                   "(when listed), then give a short summary of what "
                   "the patch does and the current findings, and wait "
                   "for questions.")
    else:
        listing = ("No collected review artifacts were found for this "
                   "change — work from the checkout alone.")
        opening = ("Start by reading the patch, then give a short "
                   "summary of what it does and wait for questions.")
    rules = ("When discussing a finding, verify every claim against "
             "the code in this worktree instead of trusting the "
             "report. Ground rules: do not modify the checkout, never "
             "post to Gerrit or write to JIRA, and touch the memory "
             "document only when explicitly asked to record something.")
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
    agent: str = "claude",
    model: Optional[str] = None,
    effort: Optional[str] = None,
    agent_args: Optional[list[str]] = None,
    local: bool = False,
    keep_worktree: bool = False,
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
                       keep_worktree)

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
                   keep_worktree)


def _launch(agent_spec, change, entries, repo, results_dir,
            worktrees_dir, db_dir, model, effort, agent_args,
            keep_worktree) -> int:
    if worktrees_dir is None:
        from .cli import default_worktrees_dir
        worktrees_dir = default_worktrees_dir(repo, results_dir)

    config = BatchConfig(repo=repo, results_dir=results_dir,
                         worktrees_dir=worktrees_dir)
    try:
        if not wt.commit_exists(config.repo, change.sha):
            print(f"  fetching {change.ref} from {change.fetch_url()} "
                  "— a first fetch into this repo can take minutes...")
        print("  creating worktree...")
        worktree_dir = prepare_worktree(config, change)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:
        print(f"error: cannot check the change out: {exc}")
        return 1

    prompt = chat_prompt(
        change, artifact_lines(results_dir, db_dir, change, entries))
    cmd = agent_spec.build_interactive_cmd(
        model, list(agent_args or []), prompt, effort=effort)


    print(f"\nStarting interactive session in {worktree_dir}\n")
    try:
        return subprocess.call(cmd, cwd=str(worktree_dir))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        if not keep_worktree:
            wt.remove_worktree(config.repo, worktree_dir)
