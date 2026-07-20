"""Command-line interface for lreview.

Unlike the JSON-emitting agent tools in this repository, lreview
is an operator-facing orchestrator and prints human-readable output.

Subcommands:
    check  - verify the kreview skill / claude CLI are set up
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
from .skill import check_skill, offer_setup, setup_instructions


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


def ensure_skill(setup_dest: Path, agent: str = "claude") -> bool:
    """Check the kreview skill; offer setup when missing."""
    status = check_skill(agent=agent)
    if status.available:
        return True

    print(f"kreview skill is not ready for {agent}:")
    for problem in status.problems:
        print(f"  - {problem}")
    print()

    if offer_setup(setup_dest, agent):
        status = check_skill(agent=agent)
        if status.available:
            print("kreview skill installed.")
            return True
        print("Setup ran but the skill check still fails:")
        for problem in status.problems:
            print(f"  - {problem}")
    return False


def cmd_check(args) -> int:
    status = check_skill(agent=args.agent)
    if status.available:
        print(f"kreview skill is ready for {args.agent}:")
        print(f"  agent CLI:    {status.agent_cli}")
        print(f"  command file: {status.command_file}")
        print(f"  prompt:       {status.prompt_path}")
        return 0
    print(f"kreview skill is NOT ready for {args.agent}:")
    for problem in status.problems:
        print(f"  - {problem}")
    print()
    print(setup_instructions(args.setup_dest, args.agent))
    return 2


def cmd_run(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    from .worktree import is_git_repo
    if not is_git_repo(repo):
        print(f"error: {repo} is not a git repository (--repo)")
        return 1

    if not ensure_skill(args.setup_dest, args.agent):
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
        print(f"  {change.number} ps{change.patchset}  {change.subject[:70]}")

    # Warn when a change's current patchset was already reviewed and
    # posted — a re-review is fine, but reposting needs --force.
    try:
        from .manifest import read_summary
        previous = read_summary(results_dir)
    except Exception:
        previous = {}
    for change in changes:
        old = previous.get(str(change.number))
        if old and old.get("posted") and old.get("sha") == change.sha:
            print(f"  note: {change.number} ps{change.patchset} was already "
                  "posted; posting a fresh result needs 'post --force'")

    print(f"\nReviewing {len(changes)} change(s), "
          f"{args.jobs} in parallel, timeout {args.timeout}s each")
    print(f"  results:   {results_dir}")
    print(f"  worktrees: {worktrees_dir}\n")

    config = BatchConfig(
        repo=repo,
        results_dir=results_dir,
        worktrees_dir=worktrees_dir,
        jobs=args.jobs,
        timeout=args.timeout,
        keep_worktrees=args.keep_worktrees,
        agent=args.agent,
        model=resolve_model(args.agent, args.model),
        agent_args=args.agent_arg or [],
    )
    results = run_batch(config, changes)

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

    with_findings = [r for r in results if r.status == STATUS_FINDINGS]
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
        description="Run the kreview AI review skill on Gerrit changes in "
                    "parallel and post the results.",
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
    sub = parser.add_subparsers(dest="command", required=True)

    from .agents import AGENTS
    default_prefix = os.environ.get("LREVIEW_PREFIX")
    default_agent = os.environ.get("LREVIEW_AGENT", "claude")
    default_setup_dest = Path(
        os.environ.get("REVIEW_PROMPTS_DIR", Path.home() / "review-prompts"))

    check_p = sub.add_parser(
        "check", help="Verify the kreview skill and agent CLI are set up")
    check_p.add_argument(
        "--agent", choices=sorted(AGENTS), default=default_agent,
        help="Agent to check (default: $LREVIEW_AGENT or claude)")
    check_p.add_argument(
        "--setup-dest", type=Path, default=default_setup_dest,
        help="Where review-prompts would be cloned "
             "(default: $REVIEW_PROMPTS_DIR or ~/review-prompts)")
    check_p.set_defaults(func=cmd_check)

    run_p = sub.add_parser(
        "run", help="Review Gerrit changes in parallel")
    run_p.add_argument(
        "changes", nargs="+",
        help="Gerrit change numbers or URLs")
    run_p.add_argument(
        "--repo", default=".",
        help="Path to the source git repository (default: cwd)")
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
        "--setup-dest", type=Path, default=default_setup_dest,
        help="Where to clone review-prompts if the skill is missing")
    run_p.set_defaults(func=cmd_run)

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
