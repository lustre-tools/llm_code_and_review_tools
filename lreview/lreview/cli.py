"""Command-line interface for lreview.

Unlike the JSON-emitting agent tools in this repository, lreview
is an operator-facing orchestrator and prints human-readable output.

Subcommands:
    setup  - guided first-time setup (agent CLI, prompts, Gerrit)
    check  - verify the review prompts / agent CLI / Gerrit creds
    run    - review a batch of Gerrit changes in parallel
    post   - post previously collected results to Gerrit
"""

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .gerrit import resolve_change
from .poster import post_results
from .runner import (
    BatchConfig,
    STATUS_CLEAN,
    STATUS_FINDINGS,
    run_batch,
)
from .prompts import check_prompts, offer_setup, setup_instructions


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def default_worktrees_dir(repo: Path, results_dir: Path) -> Path:
    """Prefer the workspace ai_worktrees/ convention when present."""
    ai_worktrees = repo.resolve().parent / "ai_worktrees"
    if ai_worktrees.is_dir():
        return ai_worktrees / "lreview"
    return results_dir / "worktrees"


def resolve_model(agent: str, model: str = None) -> str:
    """Model to run reviews with.

    Explicit --model wins, then $LREVIEW_MODEL; claude defaults to
    opus, other agents fall back to their own default model.
    """
    if model:
        return model
    env = os.environ.get("LREVIEW_MODEL")
    if env:
        return env
    return "opus" if agent == "claude" else None


def ensure_prompts(args):
    """Resolve the review prompts, offering a clone when missing.

    Returns the resolved prompts dir Path, or None.
    """
    explicit = args.prompts_dir
    status = check_prompts(explicit=explicit, agent=args.agent)
    if status.available:
        return status.prompts_dir

    print(f"review prompts are not ready for {args.agent}:")
    for problem in status.problems:
        print(f"  - {problem}")
    print()

    if offer_setup(Path(explicit) if explicit else None):
        status = check_prompts(explicit=explicit, agent=args.agent)
        if status.available:
            print(f"review prompts ready: {status.prompts_dir}")
            return status.prompts_dir
        print("Clone done but the check still fails:")
        for problem in status.problems:
            print(f"  - {problem}")
    return None


def cmd_check(args) -> int:
    from .doctor import check_gerrit
    status = check_prompts(explicit=args.prompts_dir, agent=args.agent)
    gerrit_ok, gerrit_detail = check_gerrit(live=True)

    if status.available and gerrit_ok:
        print(f"lreview is ready for {args.agent}:")
        print(f"  agent CLI: {status.agent_cli}")
        print(f"  prompts:   {status.prompts_dir}")
        print(f"  found via: {status.source}")
        print(f"  gerrit:    {gerrit_detail}")
        return 0
    print(f"lreview is NOT ready for {args.agent}:")
    for problem in status.problems:
        print(f"  - {problem}")
    if not gerrit_ok:
        print(f"  - gerrit: {gerrit_detail}")
    print()
    if not status.available:
        print(setup_instructions(
            Path(args.prompts_dir) if args.prompts_dir else None))
    print("Run 'lreview setup' for guided setup.")
    return 2


def cmd_setup(args) -> int:
    from .doctor import run_setup
    return run_setup(args.agent, args.prompts_dir)


def cmd_run(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    from .worktree import is_git_repo
    if not is_git_repo(repo):
        print(f"error: {repo} is not a git repository (--repo)")
        return 1

    prompts_dir = ensure_prompts(args)
    if prompts_dir is None:
        return 2

    from .agents import get_agent
    if not get_agent(args.agent).verified:
        print(f"note: the '{args.agent}' backend is best-effort and not "
              "yet verified end-to-end; only claude is. Use --agent-arg "
              "to adjust flags if needed.")

    results_dir = Path(args.results_dir).expanduser().resolve()
    worktrees_dir = (
        Path(args.worktrees_dir).expanduser().resolve()
        if args.worktrees_dir
        else default_worktrees_dir(repo, results_dir))

    # Resolve everything up front so a typo fails fast, before any
    # long-running review starts.
    changes = []
    seen = set()
    in_place = False
    # No changes at all = review the checked-out HEAD of --repo, in
    # place; --local makes the positional args local refs instead of
    # Gerrit changes (each in its own worktree).
    if args.local or not args.changes:
        from .gerrit import LocalChange
        from .worktree import commit_subject, rev_parse
        refs = args.changes or ["HEAD"]
        in_place = not args.changes
        from .worktree import commit_change_id
        for ref in refs:
            try:
                sha = rev_parse(repo, ref)
            except Exception as exc:
                print(f"error: cannot resolve local ref '{ref}': {exc}")
                return 1
            if sha in seen:
                print(f"  note: {ref} resolves to an already-listed "
                      "commit, reviewing once")
                continue
            seen.add(sha)
            change = LocalChange(ref_name=ref, sha=sha,
                                 subject=commit_subject(repo, sha),
                                 change_id=commit_change_id(repo, sha))
            changes.append(change)
            where = "in place" if in_place else "worktree"
            print(f"  {change.slug}  {change.subject[:60]} ({where})")
    else:
        for spec in args.changes:
            try:
                change = resolve_change(spec)
            except Exception as exc:
                print(f"error: cannot resolve change '{spec}': {exc}")
                return 1
            if change.number in seen:
                print(f"  note: {change.number} given more than once, "
                      "reviewing once")
                continue
            seen.add(change.number)
            changes.append(change)
            print(f"  {change.number} ps{change.patchset}  "
                  f"{change.subject[:70]}")

    # Warn when a change's current patchset was already reviewed and
    # posted — a re-review is fine, but reposting needs --force.
    try:
        from .manifest import read_summary
        previous = read_summary(results_dir)
    except Exception:
        previous = {}
    for change in changes:
        if change.number is None:
            continue
        old = previous.get(str(change.number))
        if old and old.get("posted") and old.get("sha") == change.sha:
            print(f"  note: {change.number} ps{change.patchset} was already "
                  "posted; posting a fresh result needs 'post --force'")

    memory_db = None
    if args.memory:
        from .memory import clear_doc, default_db_dir
        from .prompts import _REPO_ROOT
        memory_db = (Path(args.db).expanduser().resolve() if args.db
                     else default_db_dir(_REPO_ROOT))
        if args.clear_memory:
            for change in changes:
                removed = clear_doc(memory_db, change)
                if removed:
                    print(f"  cleared memory: {removed}")
    elif args.clear_memory:
        print("error: --clear-memory/-c requires --memory/-m")
        return 1

    print(f"\nReviewing {len(changes)} change(s), "
          f"{args.jobs} in parallel, timeout {args.timeout}s each")
    print(f"  results:   {results_dir}")
    if memory_db is not None:
        print(f"  memory db: {memory_db}")
    print(f"  worktrees: {worktrees_dir}\n")

    config = BatchConfig(
        repo=repo,
        results_dir=results_dir,
        worktrees_dir=worktrees_dir,
        prompts_dir=prompts_dir,
        jobs=args.jobs,
        timeout=args.timeout,
        keep_worktrees=args.keep_worktrees,
        agent=args.agent,
        model=resolve_model(args.agent, args.model),
        effort=args.effort,
        memory_db=memory_db,
        agent_args=args.agent_arg or [],
    )
    if args.effort and args.agent != "claude":
        print(f"note: --effort is claude-only; ignored for "
              f"'{args.agent}'")
    results = run_batch(config, changes, in_place=in_place)

    from .runner import format_tokens
    from .ui import console
    print("\n=== Batch summary ===")
    failed = 0
    for result in results:
        if result.status == STATUS_FINDINGS:
            status = console.color("yellow", result.status)
        elif result.status == STATUS_CLEAN:
            status = console.color("green", result.status)
        else:
            status = console.color("red", result.status)
        line = f"  {result.change.slug:<16} {status}"
        if result.status == STATUS_FINDINGS:
            line += f" ({result.findings} finding(s)"
            if result.severity:
                line += f", severity {result.severity}"
            line += ")"
        elif result.status not in (STATUS_CLEAN,):
            failed += 1
            if result.error:
                line += f" — {result.error}"
        if result.tokens is not None:
            line += f"  [{format_tokens(result.tokens)} tok"
            if result.cost_usd is not None:
                line += f", ${result.cost_usd:.2f}"
            line += "]"
        print(line)

    total_tokens = sum(r.tokens for r in results if r.tokens)
    total_cost = sum(r.cost_usd for r in results if r.cost_usd)
    if total_tokens:
        totals = f"  total: {format_tokens(total_tokens)} tokens"
        if total_cost:
            totals += f", ${total_cost:.2f}"
        print(totals)

    reports = [r.markdown_path for r in results if r.markdown_path]
    if reports:
        print("\nHuman-readable reports:")
        for report in reports:
            print(f"  {report}")

    memories = [(r.memory_path, r.memory_updated)
                for r in results if r.memory_path]
    if memories:
        print("\nReview memory (what the patch does, findings, "
              "eliminated false positives):")
        for path, updated in memories:
            note = "" if updated else "  (not updated this run)"
            print(f"  {path}{note}")

    with_findings = [r for r in results if r.status == STATUS_FINDINGS]
    local_findings = [r for r in with_findings if r.change.number is None]
    with_findings = [r for r in with_findings
                     if r.change.number is not None]
    if local_findings and args.post:
        print("\nnote: local reviews are not tied to a Gerrit change "
              "and are never posted; see the reports above.")
    if with_findings:
        numbers = [r.change.number for r in with_findings]
        if args.post:
            print("\nPosting results to Gerrit...")
            try:
                outcomes = post_results(
                    results_dir, changes=numbers, prefix=args.prefix)
            except Exception as exc:
                print(f"error: posting failed: {exc}")
                return 1
            for outcome in outcomes:
                print(f"  {outcome.number}: {outcome.status} "
                      f"{outcome.detail}")
            failed += sum(1 for o in outcomes if o.status == "error")
        else:
            print(f"\nReview JSONs saved under {results_dir}. "
                  "Inspect them, then post with:")
            prefix_arg = f" --prefix '{args.prefix}'" if args.prefix else ""
            changes_arg = " ".join(str(n) for n in numbers)
            print(f"  lreview post {changes_arg} "
                  f"--results-dir {results_dir}{prefix_arg}")

    return 1 if failed else 0


def cmd_render(args) -> int:
    from .markdown import render_existing
    results_dir = Path(args.results_dir).expanduser().resolve()
    files = [Path(f) for f in args.files] if args.files else None
    written, skipped = render_existing(files=files,
                                       results_dir=results_dir)
    for path in written:
        print(f"  {path}")
    for path, reason in skipped:
        print(f"  skipped {path}: {reason}")
    if not written and not skipped:
        print(f"no gerrit-review-*.json files found in {results_dir}")
        return 1
    return 0 if written or not skipped else 1


def cmd_post(args) -> int:
    results_dir = Path(args.results_dir).expanduser().resolve()

    # Accept bare numbers and Gerrit URLs alike.
    changes = None
    if args.changes:
        from gerrit_cli.client import GerritCommentsClient
        changes = []
        for spec in args.changes:
            try:
                _, number = GerritCommentsClient.parse_gerrit_url(str(spec))
            except Exception:
                print(f"error: '{spec}' is not a change number or "
                      "Gerrit URL")
                return 1
            changes.append(number)

    try:
        outcomes = post_results(
            results_dir, changes=changes, prefix=args.prefix,
            force=args.force)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 1
    except KeyError as exc:
        print(f"error: {exc.args[0]}")
        return 1

    from .ui import console
    errors = 0
    for outcome in outcomes:
        color = {"posted": "green", "error": "red"}.get(
            outcome.status, "yellow")
        print(f"  {outcome.number}: "
              f"{console.color(color, outcome.status)} {outcome.detail}")
        if outcome.status == "error":
            errors += 1
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lreview",
        description="Run AI patch reviews (review-prompts review-core) "
                    "on Gerrit changes in parallel and post the results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "key run options (full list: lreview run -h):\n"
            "  -j, --jobs N     parallel reviews (default: 5)\n"
            "  --agent NAME     claude (default), codex, gemini, opencode\n"
            "  --model NAME     opus (default), sonnet, fable, ...\n"
            "  --post           post findings when the batch finishes\n"
            "  --prefix TEXT    posted-message prefix; <model> placeholder\n"
            "                   (default: '[AI review - <model>]')\n"
            "  --timeout SECS   per-review limit (default: 7200)\n"
            "\n"
            "examples:\n"
            "  lreview run --repo lustre-release --post -j 8 64086 64087\n"
            "  lreview post 64086 --force\n"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{setup,check,run,render,post}")

    from .agents import AGENTS
    default_prefix = os.environ.get("LREVIEW_PREFIX")
    default_agent = os.environ.get("LREVIEW_AGENT", "claude")
    default_prompts = os.environ.get("REVIEW_PROMPTS_DIR") or None
    prompts_help = (
        "Path to the review-prompts clone (repo root or its kernel/ "
        "dir; default: $REVIEW_PROMPTS_DIR, a legacy kreview.md "
        "install, or ~/review-prompts)")

    setup_p = sub.add_parser(
        "setup", help="Guided first-time setup: agent CLI, review "
                      "prompts, Gerrit credentials")
    setup_p.add_argument(
        "--agent", choices=sorted(AGENTS), default=default_agent,
        help="Agent to set up (default: $LREVIEW_AGENT or claude)")
    setup_p.add_argument(
        "--prompts-dir", default=default_prompts, help=prompts_help)
    setup_p.set_defaults(func=cmd_setup)

    check_p = sub.add_parser(
        "check", help="Verify agent CLI, review prompts, and Gerrit "
                      "credentials")
    check_p.add_argument(
        "--agent", choices=sorted(AGENTS), default=default_agent,
        help="Agent to check (default: $LREVIEW_AGENT or claude)")
    check_p.add_argument(
        "--prompts-dir", default=default_prompts, help=prompts_help)
    check_p.set_defaults(func=cmd_check)

    run_p = sub.add_parser(
        "run", help="Review Gerrit changes in parallel")
    run_p.add_argument(
        "changes", nargs="*",
        help="Gerrit change numbers or URLs; with --local, git refs "
             "of --repo instead. With no changes at all, the "
             "checked-out HEAD of --repo is reviewed in place.")
    run_p.add_argument(
        "--repo", default=".",
        help="Path to the source git repository (default: cwd)")
    run_p.add_argument(
        "--local", action="store_true",
        help="Treat the change arguments as local git refs "
             "(branches/SHAs) of --repo — each reviewed in its own "
             "worktree. Local results are not postable to Gerrit.")
    run_p.add_argument(
        "--jobs", "-j", type=_positive_int, default=5,
        help="Maximum parallel reviews (default: 5)")
    run_p.add_argument(
        "--timeout", type=int, default=7200,
        help="Per-review timeout in seconds (default: 7200)")
    run_p.add_argument(
        "--results-dir", default="./lreview-results",
        help="Where logs, review JSONs, and summary.json go "
             "(default: ./lreview-results)")
    run_p.add_argument(
        "--worktrees-dir", default=None,
        help="Where review worktrees are created (default: "
             "<repo>/../ai_worktrees/lreview if ai_worktrees exists, "
             "else <results-dir>/worktrees)")
    run_p.add_argument(
        "--keep-worktrees", action="store_true",
        help="Do not remove worktrees after each review")
    run_p.add_argument(
        "--agent", choices=sorted(AGENTS), default=default_agent,
        help="Agent to run the reviews with (default: $LREVIEW_AGENT or "
             "claude; claude is the verified backend, others are "
             "best-effort)")
    run_p.add_argument(
        "--model", default=None,
        help="Model for the review runs, e.g. opus, sonnet, fable "
             "(default: $LREVIEW_MODEL, else opus for claude; other "
             "agents use their own default)")
    run_p.add_argument(
        "--effort", choices=["low", "medium", "high", "xhigh", "max"],
        default=os.environ.get("LREVIEW_EFFORT"),
        help="Reasoning effort for the claude review runs "
             "(default: $LREVIEW_EFFORT or claude's own default; "
             "claude-only)")
    run_p.add_argument(
        "--memory", "-m", action="store_true",
        help="Use per-change review memory: read the change's notes "
             "document from the lreview-db before analyzing (what the "
             "patch does, prior findings, eliminated false positives) "
             "and rewrite it afterwards. Without this flag the db is "
             "neither read nor written.")
    run_p.add_argument(
        "--clear-memory", "-c", action="store_true",
        help="With --memory: delete the change's memory document "
             "first, starting its notes from scratch")
    run_p.add_argument(
        "--db", default=None, metavar="DIR",
        help="Memory database directory (default: $LREVIEW_DB, else "
             "lreview-db/ in this repository — gitignored)")
    run_p.add_argument(
        "--agent-arg", "--claude-arg", action="append", dest="agent_arg",
        metavar="ARG",
        help="Extra argument passed through to the agent CLI (repeatable; "
             "use --agent-arg=--flag for arguments starting with a dash)")
    run_p.add_argument(
        "--post", action="store_true",
        help="Post results with findings to Gerrit when the batch finishes")
    run_p.add_argument(
        "--prefix", default=default_prefix,
        help="Prefix for every posted message; a <model> placeholder is "
             "replaced with the model that ran the review (default: "
             "$LREVIEW_PREFIX, else '[AI review - <model>]'; "
             "pass '' for no prefix)")
    run_p.add_argument(
        "--prompts-dir", default=default_prompts, help=prompts_help)
    run_p.set_defaults(func=cmd_run)

    render_p = sub.add_parser(
        "render", help="Render existing review JSONs to Markdown reports")
    render_p.add_argument(
        "files", nargs="*",
        help="gerrit-review-*.json files (default: all in --results-dir)")
    render_p.add_argument(
        "--results-dir", default="./lreview-results",
        help="Results directory to render (default: ./lreview-results)")
    render_p.set_defaults(func=cmd_render)

    post_p = sub.add_parser(
        "post", help="Post previously collected results to Gerrit")
    post_p.add_argument(
        "changes", nargs="*",
        help="Change numbers to post (default: all unposted with findings)")
    post_p.add_argument(
        "--results-dir", default="./lreview-results",
        help="Results directory from a previous run "
             "(default: ./lreview-results)")
    post_p.add_argument(
        "--prefix", default=default_prefix,
        help="Prefix for every posted message; a <model> placeholder is "
             "replaced with the model that ran the review (default: "
             "$LREVIEW_PREFIX, else '[AI review - <model>]'; "
             "pass '' for no prefix)")
    post_p.add_argument(
        "--force", action="store_true",
        help="Repost even if the manifest says already posted")
    post_p.set_defaults(func=cmd_post)

    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
