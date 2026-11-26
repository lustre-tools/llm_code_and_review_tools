"""Command-line interface for lreview.

Unlike the JSON-emitting agent tools in this repository, lreview
is an operator-facing orchestrator and prints human-readable output.

Subcommands:
    setup  - guided first-time setup (agent CLI, prompts, Gerrit)
    check  - verify the review prompts / agent CLI / Gerrit creds
    run    - review a batch of Gerrit changes in parallel
    chat   - interactive session over an existing review
    models - list the models and efforts each agent accepts
    bench  - run or report the standard benchmark set
    render - regenerate Markdown reports from review JSONs
    stats  - where reviews spent their time and money
    post   - post previously collected results to Gerrit
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from llm_tool_common import telemetry
from llm_tool_common.update_check import maybe_check_for_updates
from llm_tool_common.version import VersionAction

from .defaults import (
    AGY_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL, CODEX_DEFAULT_MODEL,
    CURSOR_DEFAULT_MODEL, DEFAULT_AGENT, DEFAULT_BACKPORT, DEFAULT_BENCH_JOBS,
    DEFAULT_JOBS, DEFAULT_LEAN, DEFAULT_MODE, DEFAULT_PREFIX, DEFAULT_PRELOAD,
    DEFAULT_RESULTS_DIRNAME, DEFAULT_STALL_RETRY, DEFAULT_STALL_TIMEOUT,
    DEFAULT_TIMEOUT, DEFAULT_WORKTREES_DIRNAME)
from .gerrit import resolve_change
from .poster import post_results
from .runner import (
    BatchConfig,
    STATUS_CLEAN,
    STATUS_FINDINGS,
    run_batch,
)
from .prompts import check_prompts, offer_setup, setup_instructions
from .stats import cmd_stats


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def _seconds(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def default_stall_timeout() -> int:
    """$LREVIEW_STALL_TIMEOUT when it is a number of seconds, else
    the built-in default."""
    try:
        return _seconds(os.environ.get("LREVIEW_STALL_TIMEOUT", ""))
    except (ValueError, argparse.ArgumentTypeError):
        return DEFAULT_STALL_TIMEOUT


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("0", "no", "off", "false")


def default_results_dir() -> str:
    """Where results go when --results-dir is not given.

    $LREVIEW_RESULTS_DIR wins; otherwise lreview-results/ in the
    llm tools checkout (gitignored), so results land in one known
    place no matter where lreview is invoked from. Installs without
    a checkout (e.g. CI's plain pip install) fall back to the old
    cwd-relative ./lreview-results.
    """
    env = os.environ.get("LREVIEW_RESULTS_DIR")
    if env:
        return env
    from .prompts import _REPO_ROOT
    if (_REPO_ROOT / ".git").exists():
        return str(_REPO_ROOT / DEFAULT_RESULTS_DIRNAME)
    return f"./{DEFAULT_RESULTS_DIRNAME}"


def default_worktrees_dir(repo: Path, results_dir: Path) -> Path:
    """Use worktrees_dir from the config file, else <results-dir>/worktrees."""
    from .config import load_config
    configured = load_config().get("worktrees_dir")
    if configured is not None:
        if not isinstance(configured, str):
            raise SystemExit("error: worktrees_dir in the config file "
                             "must be a string")
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = repo.resolve().parent / path
        path = path.resolve()
        return path
    return results_dir / DEFAULT_WORKTREES_DIRNAME


def resolve_model(agent: str, model: str = None) -> str:
    """Model to run reviews with.

    Explicit --model wins, then $LREVIEW_MODEL unless another agent's
    catalog lists that name. Use the backend's default in DEFAULT_MODELS
    when available. Otherwise, the agent CLI picks its own default.

    Codex aliases are expanded to the slug used to run, record and post
    reviews. An unrecognized name is passed through untouched.
    """
    from .models import DEFAULT_MODELS, canonical_model, catalog_agent
    if not model:
        env = os.environ.get("LREVIEW_MODEL")
        if env and catalog_agent(env) in (None, agent):
            model = env
        else:
            model = DEFAULT_MODELS.get(agent)
    return canonical_model(agent, model)


def check_lenses(args) -> bool:
    """--lenses and its lens options make sense together."""
    from .lenses import validate_lens_options
    if args.lenses:
        err = validate_lens_options(args.agent, args.mode,
                                    args.lens_model, args.lens_effort)
        if err:
            print(f"error: {err}")
            return False
    if args.lenses and getattr(args, "github", None):
        print("error: --lenses does not apply to --github reviews")
        return False
    if (args.lens_model or args.lens_effort) and not args.lenses:
        print("error: --lens-model and --lens-effort go with --lenses")
        return False
    return True


def lens_effort(args):
    from .lenses import DEFAULT_LENS_EFFORT
    if args.lens_effort:
        return args.lens_effort
    return (DEFAULT_LENS_EFFORT
            if args.lenses and args.agent == "claude" else None)


def check_selection(args, model) -> bool:
    """Report an impossible model/effort pair before any review runs.

    `model` is the name the agent will actually be given.

    Returns False when the run should not start.
    """
    from .models import EFFORT_AGENTS, validate_selection
    if args.effort and args.agent not in EFFORT_AGENTS:
        print(f"note: --effort is not supported for '{args.agent}'; "
              f"ignored ({'/'.join(EFFORT_AGENTS)} only)")
        return True
    try:
        validate_selection(args.agent, model, args.effort)
    except ValueError as exc:
        print(f"error: {exc}")
        return False
    return True


def ensure_prompts(args):
    """Resolve the review prompts, offering a clone when missing.

    Returns the resolved prompts dir Path, or None.
    """
    explicit = args.prompts_dir
    status = check_prompts(explicit=explicit, agent=args.agent)
    if status.available:
        return status

    print(f"review prompts are not ready for {args.agent}:")
    for problem in status.problems:
        print(f"  - {problem}")
    print()

    if offer_setup(Path(explicit) if explicit else None):
        status = check_prompts(explicit=explicit, agent=args.agent)
        if status.available:
            print(f"review prompts ready: {status.prompts_dir}")
            return status
        print("Clone done but the check still fails:")
        for problem in status.problems:
            print(f"  - {problem}")
    return None


# Maintainers of the bundled review-prompts submodule pin, by git
# identity: their checkouts warn + offer instead of auto-updating,
# because they are the ones who commit the submodule bump everyone
# else's pin comes from.
PROMPTS_MAINTAINERS = ("mvef@whamcloud.com", "vefmarc@gmail.com")


def _checkout_user_email():
    from .prompts import _REPO_ROOT, _git_out
    return _git_out(_REPO_ROOT, "config", "user.email")


def prompts_update_mode() -> str:
    """auto | warn | off for the prompts-freshness handling.

    An explicit $LREVIEW_PROMPTS_UPDATE always wins. Otherwise the
    git identity of the tools checkout decides: a submodule-pin
    maintainer gets "warn", everyone else the transparent "auto".
    """
    env = os.environ.get("LREVIEW_PROMPTS_UPDATE")
    if env:
        return env.lower()
    email = _checkout_user_email()
    if email and email.lower() in PROMPTS_MAINTAINERS:
        return "warn"
    return "auto"


def check_prompts_freshness(prompts_dir: Path, allow_update: bool = True):
    """Keep the resolved prompts checkout current before a run.

    When it is behind its upstream: "auto" (the default for tool
    users, who have no say over the submodule pin and just want
    current prompts) fast-forwards transparently with a one-line
    notice; "warn" (maintainers by git identity, see
    prompts_update_mode) reports and offers instead; "off" skips the
    check. Silent when freshness cannot be determined (offline, not
    a checkout); `check` passes allow_update=False and only ever
    reports.
    """
    mode = prompts_update_mode()
    if mode == "off":
        return
    from .prompts import prompts_freshness, update_prompts_checkout
    fresh = prompts_freshness(prompts_dir)
    if not fresh or fresh[0] == 0:
        return
    behind, root, ref = fresh

    if not allow_update or mode == "warn":
        print(f"note: the review prompts at {root} are {behind} "
              f"commit(s) behind {ref}")
        if not allow_update:
            return
        if sys.stdin.isatty():
            answer = input(
                "  fast-forward them now? [Y/n] ").strip().lower()
            if answer not in ("n", "no"):
                _ok, detail = update_prompts_checkout(
                    root, ref, submodule_note=True)
                print(f"  {detail}")
                return
        print(f"  (git -C {root} pull to update; "
              "LREVIEW_PROMPTS_UPDATE=off silences this check)")
        return

    ok, detail = update_prompts_checkout(root, ref)
    if ok:
        print(f"note: {detail} ({behind} commit(s) behind {ref}; "
              "LREVIEW_PROMPTS_UPDATE=warn|off to change)")
    else:
        print(f"note: the review prompts at {root} are {behind} "
              f"commit(s) behind {ref} — {detail}")


def cmd_check(args) -> int:
    from .doctor import check_agent_login, check_gerrit
    status = check_prompts(explicit=args.prompts_dir, agent=args.agent)
    if status.agent_cli and not args.no_login:
        login_ok, login_detail = check_agent_login(
            args.agent, live=args.live_login)
    else:
        login_ok, login_detail = True, "not checked"
    if args.github:
        from .doctor import check_github
        gerrit_ok, gerrit_detail = check_github()
        provider_name = "GitHub"
    else:
        gerrit_ok, gerrit_detail = check_gerrit(live=True)
        provider_name = "gerrit"

    if status.available and gerrit_ok and login_ok:
        print(f"lreview is ready for {args.agent}:")
        print(f"  agent CLI: {status.agent_cli}")
        print(f"  login:     {login_detail}")
        print(f"  prompts:   {status.prompts_dir}")
        print(f"  found via: {status.source}")
        print(f"  {provider_name}:    {gerrit_detail}")
        check_prompts_freshness(status.prompts_dir, allow_update=False)
        return 0
    print(f"lreview is NOT ready for {args.agent}:")
    for problem in status.problems:
        print(f"  - {problem}")
    if not login_ok:
        print(f"  - {login_detail}")
    if not gerrit_ok:
        print(f"  - {provider_name}: {gerrit_detail}")
    print()
    if not status.available:
        print(setup_instructions(
            Path(args.prompts_dir) if args.prompts_dir else None))
    print("Run 'lreview setup' for guided setup.")
    return 2


def cmd_setup(args) -> int:
    from .doctor import run_setup
    return run_setup(args.agent, args.prompts_dir)


def text_dump_path(args, results_dir: Path):
    """Where the plain-text dump of the batch goes, if anywhere.

    --output wins; otherwise --last gets one for free in the results
    dir, since a local batch has nowhere else to be read from.
    """
    if args.output:
        return Path(args.output).expanduser()
    if args.last:
        return results_dir / f"review-last{args.last}.txt"
    return None


def text_dump_title(args, repo: Path, count: int) -> str:
    if args.last:
        title = f"lreview: last {args.last} commit(s) of {repo.name}"
    else:
        title = f"lreview: {count} review(s)"
    if getattr(args, "since", None):
        title += f", changes since {args.since}"
    return title


def apply_since(args, repo: Path, changes, results_dir: Path):
    """Attach the --since focus to the one local change under review.

    Returns None to go on with the review, else the exit code: 1 for
    a bad REV, 0 when nothing changed since it and no review is due.
    """
    if len(changes) != 1:
        print("error: --since applies to a single commit: use --last 1, "
              "one --local ref, or the checked-out HEAD")
        return 1
    change = changes[0]
    from .since import resolve_since
    try:
        focus = resolve_since(repo, args.since, change.sha)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    if focus.unchanged:
        note = (f"{change.sha[:12]} is the same patch as --since "
                f"{args.since} ({focus.sha[:12]}): nothing changed, "
                "no review run")
        print(f"\n{note}")
        output = (None if getattr(args, "dry_run", False)
                  else text_dump_path(args, results_dir))
        if output:
            from .text import RULE
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("\n".join([
                    RULE, text_dump_title(args, repo, 0), RULE,
                    f"repository: {repo}", "", note, ""]))
                print(f"Full text dump: {output}")
            except OSError as exc:
                print(f"error: could not write {output}: {exc}")
                return 1
        return 0
    from .worktree import commit_change_id
    old_id = commit_change_id(repo, focus.sha)
    if old_id and change.change_id and old_id != change.change_id:
        print(f"  note: --since {args.since} has Change-Id {old_id}, the "
              f"reviewed commit {change.change_id}; is it really an "
              "earlier version?")
    change.since = focus
    return None


MEMORY_DEPRECATED = (
    "warning: --memory/-m is deprecated and will be removed. Rounds that "
    "read earlier notes found fewer bugs than independent rounds (8 of 20 "
    "known bugs against 11). Review without it; --lenses finds more per run.")


def print_dry_run_plan(args, changes, memory_db) -> None:
    """What `run` would do with these resolved changes."""
    model = resolve_model(args.agent, args.model)
    settings = [args.agent, f"model {model or 'agent default'}",
                f"mode {args.mode}"]
    if args.effort:
        settings.append(f"effort {args.effort}")
    if args.lenses:
        if args.agent == "agy":
            settings.append(f"lenses ({args.lens_model or 'inherit'})")
        else:
            lenses = args.lens_model or model or "default"
            settings.append(f"lenses ({lenses}, {lens_effort(args)})")
    print(f"  agent:     {', '.join(settings)}")
    if memory_db is not None:
        from .memory import find_doc
        existing = [(change, find_doc(memory_db, change))
                    for change in changes if not args.clear_memory]
        existing = [(change, doc) for change, doc in existing if doc]
        fresh = len(changes) - len(existing)
        print(f"  memory:    {len(existing)} existing document(s), "
              f"{fresh} new")
        for change, doc in existing:
            print(f"    {change.slug:<16} {doc.name}")
    if args.post:
        from .poster import resolve_prefix
        prefix = resolve_prefix(args.prefix,
                                {"model": model, "agent": args.agent})
        print(f"  post:      each reviewed Gerrit change, as '{prefix}'"
              if prefix else "  post:      each reviewed Gerrit change")
    print(f"\ndry run: {len(changes)} change(s) would be reviewed; "
          "nothing was fetched, reviewed or posted.")


def cmd_run(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    from .worktree import is_git_repo
    if not is_git_repo(repo):
        print(f"error: {repo} is not a git repository (--repo)")
        return 1

    if args.last and args.changes:
        print("error: --last N reviews the newest N commits of --repo; "
              "it takes no change arguments")
        return 1
    if args.series and (args.github or args.local or args.last
                        or not args.changes):
        print("error: --series expands Gerrit changes; give the first "
              "change of the series (not --local, --last or --github)")
        return 1
    if args.since and (args.github or (args.changes and not args.local)):
        print("error: --since works on local reviews only (--last 1, a "
              "--local ref, or the checked-out HEAD)")
        return 1

    if args.allow_tool and args.agent not in ("claude", "cursor"):
        print("error: --allow-tool needs the claude or cursor agent")
        return 1
    if not check_lenses(args):
        return 1
    vcfg = None
    if args.validate:
        if args.github:
            print("error: --validate works on Gerrit changes and local "
                  "commits, not GitHub pull requests")
            return 1
        if args.allow_tool:
            print("error: --allow-tool cannot restrict the validator, "
                  "which runs with --dangerously-skip-permissions to "
                  "drive ltvm; drop one of the two")
            return 1
        vcfg = validate_config(args)
        if vcfg is None:
            return 1
    elif (args.validate_classes or args.validate_baseline or args.keep_vms
          or args.validate_jobs != 1):
        print("error: the --validate-* options and --keep-vms need "
              "--validate")
        return 1

    if args.chat:
        if args.agent != "claude" or args.github:
            print("error: --chat needs the claude agent and does not "
                  "support --github")
            return 1
        if not args.dry_run and not (sys.stdin.isatty()
                                     and sys.stdout.isatty()):
            print("error: --chat needs a terminal")
            return 1

    prompts_status = ensure_prompts(args)
    if prompts_status is None:
        return 2
    prompts_dir = prompts_status.prompts_dir
    check_prompts_freshness(prompts_dir, allow_update=not args.dry_run)

    from .agents import get_agent
    if not get_agent(args.agent).verified:
        print(f"note: the '{args.agent}' backend is best-effort and not "
              "yet verified end-to-end; claude, codex, cursor and agy are. "
              "Use --agent-arg to adjust flags if needed.")
    from .models import resolve_effort
    args.effort = resolve_effort(args.agent, args.effort)
    if not check_selection(args, resolve_model(args.agent, args.model)):
        return 1

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
    # Gerrit changes (each in its own worktree); --last N reviews the
    # newest N commits of --repo, each in its own worktree.
    if args.github:
        if args.changes or args.local or args.last:
            print("error: --github cannot be combined with changes, "
                  "--local, or --last")
            return 1
        if args.mode != "full" or args.memory:
            # The GitHub PR review uses its own prompt and output
            # contract; the light driver and the Change-Id-keyed
            # review memory do not apply to it.
            print("error: --github supports neither --mode light "
                  "nor --memory")
            return 1
        from .github import resolve_pull_request
        try:
            change = resolve_pull_request(args.github)
        except Exception as exc:
            print(f"error: cannot resolve GitHub PR: {exc}")
            return 1
        changes.append(change)
        seen.add(f"github:{change.project}#{change.number}")
        print(f"  GitHub {change.project}#{change.number} {change.sha[:12]} (base {change.base_sha[:12]})")
    elif args.last or args.local or not args.changes:
        from .gerrit import LocalChange
        from .worktree import (commit_change_id, commit_subject,
                               recent_commits, rev_parse)
        if args.last:
            try:
                shas = recent_commits(repo, args.last)
            except Exception as exc:
                print(f"error: cannot list commits of {repo}: {exc}")
                return 1
            if len(shas) < args.last:
                print(f"error: --last {args.last} but {repo} has only "
                      f"{len(shas)} commit(s)")
                return 1
            # Name each commit by its position so the slug (and the
            # text dump) says which patch it was.
            refs = ["HEAD" if i == 0 else f"HEAD~{i}"
                    for i in range(args.last)]
        else:
            refs = args.changes or ["HEAD"]
        in_place = not args.changes and not args.last
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
        specs = list(args.changes)
        if args.series:
            from .gerrit import series_children
            expanded = []
            for spec in specs:
                try:
                    anchor = resolve_change(spec)
                    children = series_children(anchor)
                except Exception as exc:
                    print(f"error: cannot read the series of '{spec}': "
                          f"{exc}")
                    return 1
                in_flight = anchor.status in (None, "NEW")
                what = (f"{len(children.open)} in-flight child "
                        "change(s)")
                print(f"  series of {anchor.number}: "
                      + (f"itself + {what}" if in_flight else
                         f"{what}; {anchor.number} itself is "
                         f"{anchor.status.lower()}, not reviewed"))
                for number, why in children.skipped:
                    print(f"    skipped {number}: {why}")
                expanded += ([spec] if in_flight else []) + [
                    str(n) for n in children.open]
            specs = expanded
            if not specs:
                print("nothing in flight to review")
                return 0
        for spec in specs:
            try:
                change = resolve_change(spec)
            except Exception as exc:
                print(f"error: cannot resolve change '{spec}': {exc}")
                return 1
            if change.number in seen:
                if not args.series:
                    print(f"  note: {change.number} given more than "
                          "once, reviewing once")
                continue
            seen.add(change.number)
            changes.append(change)
            print(f"  {change.number} ps{change.patchset}  "
                  f"{change.subject[:70]}")

    if args.since:
        rc = apply_since(args, repo, changes, results_dir)
        if rc is not None:
            return rc

    # Warn when a change's current patchset was already reviewed and
    # posted — a re-review is fine, but reposting needs --force.
    try:
        from .manifest import read_summary
        previous = read_summary(results_dir)
    except Exception:
        previous = {}
    from .runner import artifact_tag
    mode_tag = artifact_tag(args.mode)
    for change in changes:
        if change.number is None:
            continue
        key = (f"github:{change.project}#{change.number}"
               if getattr(change, "provider", None) == "github"
               else str(change.number))
        old = previous.get(f"{key}{mode_tag}")
        if old and old.get("posted") and old.get("sha") == change.sha:
            what = (f"{change.project}#{change.number} at {change.sha[:12]}"
                    if getattr(change, "provider", None) == "github"
                    else f"{change.number} ps{change.patchset}")
            print(f"  note: {what} was already posted; posting a fresh "
                  "result needs 'post --force'")

    memory_db = None
    if args.memory:
        print(MEMORY_DEPRECATED, file=sys.stderr)
        from .memory import clear_doc, default_db_dir
        from .prompts import _REPO_ROOT
        memory_db = (Path(args.db).expanduser().resolve() if args.db
                     else default_db_dir(_REPO_ROOT))
        if args.clear_memory:
            from .memory import find_doc
            for change in changes:
                if args.dry_run:
                    doc = find_doc(memory_db, change)
                    if doc:
                        print(f"  would clear memory: {doc}")
                    continue
                removed = clear_doc(memory_db, change)
                if removed:
                    print(f"  cleared memory: {removed}")
    elif args.clear_memory:
        print("error: --clear-memory/-c requires --memory/-m")
        return 1
    elif args.no_resume:
        print("error: --no-resume requires --memory/-m")
        return 1

    print(f"\nReviewing {len(changes)} change(s), "
          f"{args.jobs} in parallel, timeout {args.timeout}s each, "
          + (f"stalled after {args.stall_timeout}s without output"
             if args.stall_timeout else "no stall limit"))
    print(f"  prompts:   {prompts_dir}")
    print(f"             (via {prompts_status.source})")
    print(f"  results:   {results_dir}")
    if memory_db is not None:
        print(f"  memory db: {memory_db}")
    if args.since:
        from .since import focus_label
        focus = changes[0].since
        print(f"  focus:     {focus_label(focus)}"
              + ("" if focus.same_base else ", rebased since"))
    print(f"  worktrees: {worktrees_dir}\n")

    if args.dry_run:
        print_dry_run_plan(args, changes, memory_db)
        return 0

    config = BatchConfig(
        repo=repo,
        results_dir=results_dir,
        worktrees_dir=worktrees_dir,
        prompts_dir=prompts_dir,
        jobs=args.jobs,
        timeout=args.timeout,
        stall_timeout=args.stall_timeout,
        stall_retry=args.stall_retry,
        keep_worktrees=args.keep_worktrees,
        mode=args.mode,
        agent=args.agent,
        model=resolve_model(args.agent, args.model),
        effort=args.effort,
        fallback_agent=getattr(args, "fallback_agent", "") or None,
        fallback_model=(resolve_model(args.fallback_agent, None)
                        if getattr(args, "fallback_agent", "") else None),
        memory_db=memory_db,
        resume=not args.no_resume,
        lean=args.lean,
        preload=args.preload,
        backport=args.backport,
        lenses=args.lenses,
        lens_model=args.lens_model,
        lens_effort=lens_effort(args),
        agent_args=args.agent_arg or [],
        allowed_tools=args.allow_tool or [],
        validate=vcfg,
    )
    try:
        results = run_batch(config, changes, in_place=in_place)
    except KeyboardInterrupt:
        print("\ninterrupted — partial results are in the manifest")
        return 130

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
        slug = result.change.slug + artifact_tag(result.mode)
        line = f"  {slug:<16} {status}"
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
        if vcfg is not None and result.status == STATUS_FINDINGS:
            print(f"  {'':<16} validation: "
                  f"{validation_text(result.validation)}")
            print_report(results_dir, result.validation,
                         postable=bool(result.change.number))

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

    memories = [(r.memory_path, r.memory_updated, r.memory_reviews)
                for r in results if r.memory_path]
    if memories:
        print("\nReview memory (what the patch does, findings, "
              "eliminated false positives):")
        for path, updated, iteration in memories:
            note = f"  (iteration {iteration})" if iteration else ""
            if not updated:
                note += "  (not updated this run)"
            print(f"  {path}{note}")

    output = text_dump_path(args, results_dir)
    if output:
        from .text import write_batch_text
        try:
            written = write_batch_text(
                output, results, repo=repo,
                title=text_dump_title(args, repo, len(results)))
            print(f"\nFull text dump: {written}")
        except OSError as exc:
            print(f"\nerror: could not write {output}: {exc}")
            failed += 1

    # Findings post their comments, a clean review a no-issues
    # message; failed, timed-out and invalid runs have nothing to post.
    reviewed = [r for r in results
                if r.status in (STATUS_FINDINGS, STATUS_CLEAN)]
    local_reviewed = [r for r in reviewed if r.change.number is None]
    postable = [r for r in reviewed if r.change.number is not None]
    if local_reviewed and args.post:
        print("\nnote: local reviews are not tied to a Gerrit change "
              "and are never posted; see the reports above.")
    if args.post:
        for r in results:
            if r.status not in (STATUS_FINDINGS, STATUS_CLEAN):
                print(f"\nnot posted: {r.change.slug} {r.status}"
                      + (f" — {r.error}" if r.error else ""))
    if postable:
        # Manifest keys of exactly this batch's reviews, so --post
        # never touches unposted results from earlier batches
        numbers = []
        for r in postable:
            c = r.change
            key = (f"github:{c.project}#{c.number}"
                   if getattr(c, "provider", None) == "github"
                   else str(c.number))
            numbers.append(f"{key}{mode_tag}")
        if args.post:
            print("\nPosting results...")
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
            print(f"\nResults saved under {results_dir}. "
                  "Inspect the reports, then post with:")
            prefix_arg = f" --prefix '{args.prefix}'" if args.prefix else ""
            changes_arg = " ".join(str(n) for n in numbers)
            print(f"  lreview post {changes_arg} "
                  f"--results-dir {results_dir}{prefix_arg}")

    if args.chat:
        from .chat import chat_after_run
        try:
            chat_after_run(config, results, in_place=in_place)
        except Exception as exc:
            print(f"error: cannot start the chat: {exc}")
            return 1
    return 1 if failed else 0


def cmd_bench(args) -> int:
    from . import bench
    data = bench.load_cases(Path(args.cases_file)
                            if getattr(args, "cases_file", None)
                            else bench.CASES_PATH)
    try:
        cases = bench.select(data["cases"], getattr(args, "cases", None),
                             getattr(args, "set", None))
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    bench_dir = Path(args.bench_dir).expanduser().resolve()

    if args.bench_cmd == "list":
        for case in cases:
            bugs = ", ".join(b["id"] for b in case.get("bugs") or []) or "-"
            print(f"{case['id']:24s} {case['change']:>6}/{case['patchset']:<3} "
                  f"{case.get('lines', 0):>4} lines  {case.get('kind', ''):8s} "
                  f"[{','.join(case.get('sets', []))}] bugs: {bugs}"
                  f"\n{'':24s} {case['subject']}")
        print(f"\n{len(cases)} case(s) in {args.cases_file or bench.CASES_PATH}")
        return 0

    if args.bench_cmd == "report":
        # A label names an arm under the bench directory, or is a path
        dirs = [Path(d).expanduser() if Path(d).expanduser().is_dir()
                else bench_dir / d for d in args.labels] or sorted(
            (p for p in bench_dir.iterdir()
             if (p / bench.RUN_FILE).exists()), key=lambda p: p.name)
        scoreds = [bench.score(d, data["cases"], judge=args.judge)
                   for d in dirs]
        if args.json:
            import json as _json
            print(_json.dumps([{"summary": bench.summarize(s, args.judge),
                                **s} for s in scoreds], indent=1))
        elif args.by_bug:
            print(bench.render_bugs(scoreds, judge=args.judge))
        else:
            print(bench.render(scoreds, judge=args.judge))
        return 0

    # run
    prompts_status = ensure_prompts(args)
    if prompts_status is None:
        return 2
    model = resolve_model(args.agent, args.model)
    from .models import resolve_effort
    args.effort = resolve_effort(args.agent, args.effort)
    if not check_selection(args, model) or not check_lenses(args):
        return 1
    label = args.label or bench.default_label(args)
    label_dir = bench_dir / label
    source = Path(args.repo).expanduser().resolve() if args.repo else None
    print(f"bench: preparing the case repositories in {bench_dir}")
    try:
        repos = bench.prepare_repos(bench_dir, cases, source)
    except (subprocess.SubprocessError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1

    allow, deny = ([], [])
    if args.offline and args.agent == "cursor":
        allow, deny = bench.offline_rules()
    elif args.offline and args.agent == "agy":
        deny = bench.agy_offline_rules()

    def config_for(results_dir: Path, repo: Path) -> BatchConfig:
        return BatchConfig(
            repo=repo, results_dir=results_dir,
            worktrees_dir=bench_dir / "worktrees",
            prompts_dir=prompts_status.prompts_dir, jobs=1,
            timeout=args.timeout, stall_timeout=args.stall_timeout,
            stall_retry=args.stall_retry, mode=args.mode, agent=args.agent,
            model=model, effort=args.effort,
            lean=args.lean, preload=args.preload,
            lenses=args.lenses, lens_model=args.lens_model,
            lens_effort=lens_effort(args),
            env=bench.offline_env(bench_dir) if args.offline else {},
            allowed_tools=allow, denied_tools=deny,
            agent_args=args.agent_arg or [])
    try:
        bench.run_bench(config_for, cases, label_dir, args.reps, repos,
                        jobs=args.jobs,
                        cases_path=Path(args.cases_file or bench.CASES_PATH))
    except KeyboardInterrupt:
        print("\ninterrupted -- finished reps are kept")
        return 130
    print()
    print(bench.render([bench.score(label_dir, data["cases"])]))
    return 0


def cmd_chat(args) -> int:
    results_dir = Path(args.results_dir).expanduser().resolve()
    worktrees_dir = (Path(args.worktrees_dir).expanduser().resolve()
                     if args.worktrees_dir else None)
    from .memory import default_db_dir
    from .prompts import _REPO_ROOT
    db_dir = (Path(args.db).expanduser().resolve() if args.db
              else default_db_dir(_REPO_ROOT))

    from .agents import get_agent
    if not get_agent(args.agent).verified:
        print(f"note: the '{args.agent}' backend is best-effort and "
              "not yet verified end-to-end; claude, codex, cursor and "
              "agy are.")
    # chat passes --model alone, never a run default
    from .models import canonical_model
    model = canonical_model(args.agent, args.model)
    if not check_selection(args, model):
        return 1

    if not args.local and not args.change:
        print("error: a change number/URL is required "
              "(or --local [REF] for a local review)")
        return 1

    from .chat import run_chat
    return run_chat(
        args.change, results_dir=results_dir,
        repo=Path(args.repo) if args.repo else None,
        worktrees_dir=worktrees_dir, db_dir=db_dir, agent=args.agent,
        model=model, effort=args.effort,
        agent_args=args.agent_arg or [], local=args.local,
        keep_worktree=args.keep_worktree, resume=not args.no_resume)


def cmd_models(args) -> int:
    """Print what each agent will accept for --model and --effort."""
    from .models import (CLAUDE_EFFORTS, CLAUDE_DEFAULT_MODEL, CLAUDE_MODELS,
                         agy_model_lines, codex_catalog_lines,
                         cursor_model_lines)
    agents = [args.agent] if args.agent else ["claude", "codex"]
    for agent in agents:
        if agent == "claude":
            print("claude")
            others = [m for m in CLAUDE_MODELS if m != CLAUDE_DEFAULT_MODEL]
            print(f"  {CLAUDE_DEFAULT_MODEL} (default), {', '.join(others)}")
            print(f"  effort: {', '.join(CLAUDE_EFFORTS)} "
                  "(default: claude's own)")
        elif agent == "codex":
            print("codex")
            print("\n".join(codex_catalog_lines()))
        elif agent == "cursor":
            print("cursor")
            print("\n".join(cursor_model_lines()))
        elif agent == "agy":
            print("agy")
            print("\n".join(agy_model_lines()))
        else:
            print(f"{agent}\n  models are the CLI's own; --effort is "
                  "not supported")
        print()
    print("Names not listed here are passed to the agent CLI unchanged.")
    return 0


def cmd_agents(args) -> int:
    """Print each backend and whether it can run a review now."""
    from .doctor import list_agents
    rows = list_agents(login=not args.no_login)
    for name, verified, state, detail in rows:
        tag = "" if verified else " (unverified)"
        print(f"{name + tag:<22} {state:<14} {detail}")
    ready = ("ready", "installed")
    return 0 if any(row[2] in ready for row in rows) else 2


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
        print("no gerrit-review-*.json or review-result-*.json files "
              f"found in {results_dir}")
        return 1
    return 0 if written or not skipped else 1


def _entry_change(entry: dict):
    """The change a manifest entry reviewed, for a later validation."""
    from .gerrit import LocalChange, ResolvedChange, change_ref
    if entry.get("local"):
        return LocalChange(ref_name=entry.get("ref_name") or "local",
                           sha=entry["sha"],
                           subject=entry.get("subject") or "")
    number, patchset = entry["number"], entry.get("patchset")
    return ResolvedChange(
        number=number, project=entry.get("repository") or "",
        subject=entry.get("subject") or "", sha=entry["sha"],
        patchset=patchset,
        ref=change_ref(number, patchset) if patchset else "",
        base_url=entry.get("base_url") or "")


# Manifest entries whose findings lreview read from Gerrit: the
# AI-review threads, or the reviewer's own drafts.
READ_SOURCES = ("gerrit", "drafts")


def _change_number(item: str) -> Optional[int]:
    """The change number of a bare number or a Gerrit URL, or None."""
    if item.isdigit():
        return int(item)
    from gerrit_cli.client import GerritCommentsClient
    try:
        return int(GerritCommentsClient.parse_gerrit_url(item)[1])
    except Exception:  # noqa: BLE001 - not a change
        return None


def _manifest_key(summary: dict, item: str) -> Optional[str]:
    """A manifest key for a request: the key itself, else the full-mode
    entry of a change number, else its light-mode entry."""
    if item in summary:
        return item
    for key in (item, f"{item}-light"):
        if key in summary:
            return key
    return None


def _validation_target(config, entry: Optional[dict], item: str,
                       drafts: bool = False):
    """What one `lreview validate` request tests.

    Returns (tested change, reviewed Revision, review spec, artifact
    slug, findings on Gerrit already, manifest fields of a review read
    from Gerrit or None).  A Gerrit change is tested on its newest
    patchset (the merged commit, once merged); a local review on its
    own commit.  Without a review of lreview's own in the manifest, the
    findings are the unresolved AI-review threads on Gerrit, or with
    `drafts` the logged-in account's unresolved drafts."""
    from gerrit_cli.client import GerritCommentsClient
    from .gerrit import change_ref, resolve_change
    from .poster import DEFAULT_TAG
    from .validate import (Revision, drafts_findings_spec,
                           gerrit_findings_spec)

    results_dir = config.results_dir
    if entry is not None and entry.get("source") not in READ_SOURCES:
        reviewed_change = _entry_change(entry)
        spec = json.loads((results_dir / entry["json"]).read_text())
        reviewed = Revision(sha=entry["sha"],
                            patchset=entry.get("patchset"))
        slug = reviewed_change.slug
        if entry.get("local"):
            return reviewed_change, reviewed, spec, slug, False, None
        client = (GerritCommentsClient(url=entry["base_url"])
                  if entry.get("base_url") else None)
        tested = resolve_change(str(entry["number"]), client=client)
        if entry.get("base_url"):
            tested.base_url = entry["base_url"]
        if tested.sha != reviewed.sha:
            _fetch_commit(config, tested, reviewed.sha,
                          change_ref(entry["number"], reviewed.patchset))
        return (tested, reviewed, spec, slug, bool(entry.get("posted")),
                None)

    number = entry["number"] if entry else item
    base_url = (entry or {}).get("base_url")
    client = GerritCommentsClient(url=base_url) if base_url else None
    tested = resolve_change(str(number), client=client)
    if base_url:
        tested.base_url = base_url
    client = client or GerritCommentsClient(url=tested.base_url)
    detail = client.get_change_detail(tested.number)
    revisions = {r.get("_number"): (sha, r.get("ref"))
                 for sha, r in (detail.get("revisions") or {}).items()}
    shas = {ps: sha for ps, (sha, _ref) in revisions.items()}
    if drafts:
        spec = drafts_findings_spec(client.get_drafts(tested.number) or {},
                                    shas)
    else:
        spec = gerrit_findings_spec(
            client.get_comments(tested.number) or {}, DEFAULT_TAG, shas)
    for entries in spec["comments"].values():
        for e in entries:
            sha = e.get("commit_id")
            if sha and sha != tested.sha:
                ref = (revisions.get(e.get("patch_set")) or (None, None))[1]
                _fetch_commit(config, tested, sha, ref or change_ref(
                    tested.number, e.get("patch_set") or tested.patchset))
    source = spec["source"]
    spec_name = f"{source}-findings-{tested.number}.json"
    (results_dir / spec_name).write_text(json.dumps(spec, indent=2) + "\n")
    fields = {"number": tested.number, "patchset": tested.patchset,
              "sha": tested.sha, "subject": tested.subject,
              "repository": tested.project, "branch": tested.branch,
              "base_url": tested.base_url, "provider": "gerrit",
              "source": source, "status": STATUS_FINDINGS,
              "json": spec_name, "mode": "full", "posted": True,
              "findings": sum(len(v) for v in spec["comments"].values())}
    return (tested, None, spec, f"{tested.number}_{source}", True, fields)


def _fetch_commit(config, change, sha: str, ref: str) -> None:
    """Fetch a reviewed patchset into the repository, so that the
    validator can compare it with the tested one."""
    from . import worktree as wt
    if wt.commit_exists(config.repo, sha):
        return
    urls = wt.gerrit_remote_urls(config.repo, change.base_url,
                                 change.project) + [change.fetch_url()]
    try:
        wt.fetch_change(config.repo, urls, ref)
    except Exception as exc:  # noqa: BLE001 - the validator can do without
        print(f"  {change.number}: could not fetch {ref}: {exc}")


def cmd_validate(args) -> int:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    from .manifest import locked_summary, read_summary
    from .runner import kill_running_reviews, prepare_worktree
    from .validate import validate_change
    from .worktree import is_git_repo, remove_worktree

    results_dir = Path(args.results_dir).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve()
    if not is_git_repo(repo):
        print(f"error: {repo} is not a git repository (--repo)")
        return 1
    try:
        summary = read_summary(results_dir)
    except FileNotFoundError:
        summary = {}
    vcfg = validate_config(args)
    if vcfg is None:
        return 1

    jobs = []  # (manifest key, entry or None, request)
    for item in args.changes:
        if args.drafts:
            number = _change_number(str(item))
            if number is None:
                print(f"error: {item} is not a Gerrit change")
                return 1
            key = f"{number}-drafts"
            jobs.append((key, summary.get(key), str(item)))
            continue
        key = _manifest_key(summary, str(item))
        entry = summary.get(key) if key else None
        if entry is None:
            if not re.fullmatch(r"\d+|https?://\S+", str(item)):
                print(f"error: {item} is not in {results_dir}/summary.json "
                      "and is not a Gerrit change")
                return 1
            jobs.append((None, None, str(item)))
            continue
        if entry.get("provider") == "github":
            print(f"error: {key}: GitHub reviews cannot be validated")
            return 1
        if entry.get("source") not in READ_SOURCES and (
                entry.get("status") != STATUS_FINDINGS
                or not entry.get("json")):
            print(f"  {key}: skipped, the review has no findings "
                  f"(status {entry.get('status')})")
            continue
        jobs.append((key, entry, str(item)))
    if not jobs:
        return 0

    worktrees_dir = (Path(args.worktrees_dir).expanduser().resolve()
                     if args.worktrees_dir
                     else default_worktrees_dir(repo, results_dir))
    results_dir.mkdir(parents=True, exist_ok=True)
    config = BatchConfig(
        repo=repo, results_dir=results_dir, worktrees_dir=worktrees_dir,
        keep_worktrees=args.keep_worktrees, lean=args.lean, validate=vcfg)

    def one(job):
        key, entry, item = job
        tested, reviewed, spec, slug, on_gerrit, fields = \
            _validation_target(config, entry, item, drafts=args.drafts)
        if fields is not None:
            key = key or str(fields["number"])
            if not fields["findings"]:
                what = "draft" if args.drafts else "AI-review thread"
                print(f"  {key}: no unresolved {what} on Gerrit")
                return key, fields, None
        worktree = prepare_worktree(config, tested)
        try:
            record = validate_change(
                config, vcfg, tested, worktree, spec,
                (entry or {}).get("mode", "full"), reviewed=reviewed,
                slug=slug, on_gerrit=on_gerrit)
        finally:
            if not config.keep_worktrees:
                remove_worktree(repo, worktree)
        return key, fields, record

    done = []  # (key, fields, record)
    pool = ThreadPoolExecutor(max_workers=vcfg.jobs)
    futures = [(job, pool.submit(one, job)) for job in jobs]
    try:
        for job, future in futures:
            try:
                done.append(future.result())
            except Exception as exc:  # noqa: BLE001 - next change
                label = job[0] or job[2]
                print(f"  {label}: validation failed: {exc}")
                if job[1] is not None:
                    done.append((job[0], None, {
                        "status": "failed", "error": str(exc), "json": None,
                        "counts": None, "posted": False}))
    except KeyboardInterrupt:
        vcfg.cancel.set()
        killed = kill_running_reviews()
        print(f"\ninterrupted — stopped {killed} validator(s); "
              "destroying their VMs")
        pool.shutdown(wait=True, cancel_futures=True)
        return 130
    finally:
        pool.shutdown(wait=True)
        entries = {key: entry for key, entry, _ in jobs if key}
        with locked_summary(results_dir) as current:
            for key, fields, record in done:
                if fields is not None:
                    if record is None and key not in current:
                        continue
                    now = current.get(key)
                    if (now is not None
                            and now.get("source") not in READ_SOURCES):
                        continue  # a review of lreview's own came first
                    current[key] = {**fields, "validation": record,
                                    "reviewed_at": datetime.now(
                                        timezone.utc).isoformat(
                                            timespec="seconds")}
                    continue
                now = current.get(key)
                # A re-review of a newer revision replaced the entry:
                # these verdicts belong to the old one.
                if now and record is not None and \
                        now.get("sha") == entries[key]["sha"]:
                    now["validation"] = record

    print("\n=== Validation summary ===")
    failed = 0
    now = read_summary(results_dir)
    keys = []
    for key, _fields, record in done:
        if record and record.get("error"):
            failed += 1
        local = bool((now.get(key) or {}).get("local"))
        print(f"  {key:<16} {validation_text(record)}")
        print_report(results_dir, record, postable=not local)
        # Only an entry in the manifest can be posted, and never a
        # local review.
        if key in now and not local and record is not None:
            keys.append(key)
    if args.post and keys:
        print("\nPosting results...")
        outcomes = post_results(results_dir, changes=keys,
                                prefix=args.prefix)
        for outcome in outcomes:
            print(f"  {outcome.number}: {outcome.status} {outcome.detail}")
            failed += outcome.status == "error"
    elif keys:
        from .validate import draft_todo, load_validation, unposted
        # A review not yet on Gerrit can go out; findings on Gerrit
        # already need a verdict to post.  A draft also takes the
        # verdict of a bug that stays private.
        ready = [k for k in keys
                 if (not now[k].get("posted")
                     and now[k].get("source") not in READ_SOURCES)
                 or (draft_todo if now[k].get("source") == "drafts"
                     else unposted)(load_validation(results_dir, now[k]))]
        if ready:
            print(f"\nPost with: lreview post {' '.join(ready)} "
                  f"--results-dir {results_dir}")
    if args.post_report and keys:
        from .validate import post_reports
        print("\nAttaching QA reports...")
        with locked_summary(results_dir) as current:
            for key in keys:
                status, detail = post_reports(results_dir, current[key])
                print(f"  {key}: {status} {detail}")
                failed += status == "error"
    return 1 if failed else 0


def cmd_post(args) -> int:
    results_dir = Path(args.results_dir).expanduser().resolve()

    # Accept bare numbers, Gerrit URLs, mode-tagged manifest keys
    # ("64620-light"), and GitHub keys ("github:owner/repo#7") alike.
    # A bare number posts every mode's entry for that change.
    import re
    changes = None
    if args.changes:
        from gerrit_cli.client import GerritCommentsClient
        changes = []
        for spec in args.changes:
            if (re.fullmatch(r"\d+-[a-z]+", str(spec))
                    or str(spec).startswith("github:")):
                changes.append(str(spec))
                continue
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
            force=args.force, dry_run=getattr(args, "dry_run", False))
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


def _add_stall_options(parser) -> None:
    parser.add_argument(
        "--stall-timeout", type=_seconds, default=default_stall_timeout(),
        metavar="SECS",
        help="End a review as stalled when its agent writes nothing for "
             "SECS seconds, naming what it was waiting on; 0 never does "
             "(default: $LREVIEW_STALL_TIMEOUT, else "
             f"{DEFAULT_STALL_TIMEOUT})")
    parser.add_argument(
        "--stall-retry", action=argparse.BooleanOptionalAction,
        default=DEFAULT_STALL_RETRY,
        help="Start a stalled review once more before reporting it "
             f"stalled (default: {'on' if DEFAULT_STALL_RETRY else 'off'})")


def _add_validate_options(parser) -> None:
    """The options of the validation phase, shared by `run --validate`
    and `lreview validate` (validate.py)."""
    from .models import EFFORT_LEVELS
    from .validate import (DEFAULT_VALIDATE_EFFORT, DEFAULT_VALIDATE_MODEL,
                           DEFAULT_VALIDATE_TARGET, DEFAULT_VALIDATE_TIMEOUT,
                           MARKERS)
    parser.add_argument(
        "--validate-classes", default=None, metavar="LIST",
        help="Finding classes to validate, comma-separated, from "
             f"{', '.join(MARKERS)}, or all (default: defect)")
    parser.add_argument(
        "--validate-jobs", type=_positive_int, default=1, metavar="N",
        help="Validations that hold VMs at the same time (default: 1)")
    parser.add_argument(
        "--validate-model", default=DEFAULT_VALIDATE_MODEL,
        help=f"Model of the validator agent, a claude session "
             f"(default: {DEFAULT_VALIDATE_MODEL})")
    parser.add_argument(
        "--validate-effort", choices=list(EFFORT_LEVELS),
        default=DEFAULT_VALIDATE_EFFORT,
        help=f"Effort of the validator agent "
             f"(default: {DEFAULT_VALIDATE_EFFORT})")
    parser.add_argument(
        "--validate-timeout", type=_seconds,
        default=DEFAULT_VALIDATE_TIMEOUT, metavar="SECS",
        help="Time limit for the validation of one change "
             f"(default: {DEFAULT_VALIDATE_TIMEOUT})")
    parser.add_argument(
        "--validate-target", default=DEFAULT_VALIDATE_TARGET,
        metavar="TARGET",
        help="ltvm target of the default VM "
             f"(default: {DEFAULT_VALIDATE_TARGET})")
    parser.add_argument(
        "--validate-baseline", action="store_true",
        help="Also run the reproducer of each confirmed finding on the "
             "parent commit, to tell a regression from an older bug")
    parser.add_argument(
        "--keep-vms", action="store_true",
        help="Leave the validator's VMs for inspection instead of "
             "destroying them")


_VALIDATE_OPTION_DEFAULTS = {
    "validate_classes": None, "validate_jobs": 1,
    "validate_baseline": False, "keep_vms": False,
}


def validate_config(args):
    """A ValidateConfig from the --validate-* options, or None after
    printing why the options cannot work."""
    import shutil as _shutil
    from .validate import ValidateConfig, parse_classes
    try:
        classes = parse_classes(args.validate_classes)
    except ValueError as exc:
        print(f"error: --validate-classes: {exc}")
        return None
    from .validate import LTVM_SETUP_HINT, ltvm_preflight
    if not _shutil.which("claude"):
        print("error: validation needs `claude` (Claude Code) on PATH")
        return None
    if not _shutil.which("ltvm"):
        print("error: validation runs on ltvm VMs, and `ltvm` is not on "
              f"PATH. {LTVM_SETUP_HINT}")
        return None
    problem = ltvm_preflight(args.validate_target)
    if problem:
        print(f"error: {problem}")
        return None
    return ValidateConfig(
        classes=classes, jobs=args.validate_jobs,
        model=args.validate_model, effort=args.validate_effort,
        timeout=args.validate_timeout, target=args.validate_target,
        # A post needs the baseline run: it shows that the change
        # brought each confirmed bug in.
        baseline=(args.validate_baseline or getattr(args, "post", False)
                  or getattr(args, "post_report", False)),
        keep_vms=args.keep_vms,
        force=getattr(args, "force", False))


def validation_text(record) -> str:
    """One line for a change's validation record."""
    from .validate import counts_text
    if not record:
        return "no finding in the selected classes"
    text = (counts_text(record["counts"]) if record.get("counts")
            else record.get("status", "failed"))
    if record.get("error"):
        text += f" ({record['error']})"
    return text


def print_report(results_dir: Path, record, postable: bool = True) -> None:
    """Each finding of a validation with its verdict and QA report, the
    index, the refuted findings file and how to post it, and the
    ticket reminder for a merged change."""
    from .validate import report_lines
    for line in report_lines(results_dir, record):
        print(f"  {'':<16}   {line}")
    if postable and record and record.get("refuted"):
        print(f"  {'':<16}   post one by hand: edit the file, then "
              f"gc review --post-comments {results_dir / record['refuted']} "
              "<change URL>  (it posts on the current patchset)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lreview",
        description="Run AI patch reviews (review-prompts review-core) on Gerrit changes or GitHub PRs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "key run options (full list: lreview run -h):\n"
            f"  -j, --jobs N     parallel reviews (default: {DEFAULT_JOBS})\n"
            f"  --mode NAME      full or light (default: {DEFAULT_MODE});\n"
            "                   light is one cheap focused pass\n"
            f"  --agent NAME     claude, codex, cursor, agy, gemini, opencode,\n"
            f"                   auggie (default: {DEFAULT_AGENT})\n"
            f"  --model NAME     claude: {CLAUDE_DEFAULT_MODEL} (default), opus, fable\n"
            f"                   codex: {CODEX_DEFAULT_MODEL} (default), astra, terra,\n"
            "                   luna, ... — see 'lreview models'\n"
            f"                   cursor: {CURSOR_DEFAULT_MODEL} (default), ... —\n"
            "                   see 'lreview models --agent cursor'\n"
            f"                   agy: {AGY_DEFAULT_MODEL} (default), ... —\n"
            "                   see 'lreview models --agent agy'\n"
            "  --effort LEVEL   low..max, ultra (codex, per model)\n"
            "  --post           post findings when the batch finishes\n"
            "  --prefix TEXT    posted-message prefix; <model> placeholder\n"
            f"                   (default: '{DEFAULT_PREFIX}')\n"
            f"  --timeout SECS   per-review limit (default: {DEFAULT_TIMEOUT})\n"
            "  --stall-timeout SECS  end a review whose agent writes\n"
            "                   nothing for SECS, and run it once more\n"
            f"                   (default: {DEFAULT_STALL_TIMEOUT})\n"
            "\n"
            "  --last N         review the newest N commits of --repo\n"
            "  --since REV      review only what changed from REV, an\n"
            "                   earlier version of the commit\n"
            "  --output FILE    plain-text dump of the whole batch\n"
            "\n"
            "examples:\n"
            "  lreview run --repo lustre-release --post -j 8 64086 64087\n"
            "  lreview run --repo ~/lustre-release --last 3 -o rev.txt\n"
            "  lreview post 64086 --force\n"
            "  lreview chat 64086          # discuss an existing review\n"
        ),
    )
    parser.add_argument("--version", action=VersionAction)
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{setup,check,run,chat,models,render,post}")

    from .agents import AGENTS
    from .models import CLAUDE_EFFORTS, EFFORT_LEVELS
    default_prefix = os.environ.get("LREVIEW_PREFIX")
    default_agent = os.environ.get("LREVIEW_AGENT", DEFAULT_AGENT)
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
    check_p.add_argument(
        "--no-login", action="store_true",
        help="skip the login check")
    check_p.add_argument(
        "--live-login", action="store_true",
        help="check claude's login with one prompt to haiku (about "
             "1.5 cents), not only the stored credential")
    check_p.add_argument("--github", action="store_true",
        help="check GitHub token (GH_TOKEN, falling back to GITHUB_TOKEN) instead of Gerrit")
    check_p.set_defaults(func=cmd_check)

    run_p = sub.add_parser(
        "run", help="Review Gerrit changes in parallel")
    run_p.add_argument(
        "changes", nargs="*",
        help="Gerrit change numbers or URLs; with --local, git refs "
             "of --repo instead. With no changes at all, the "
             "checked-out HEAD of --repo is reviewed in place; see "
             "--last N to review the newest N commits instead.")
    run_p.add_argument("--github", metavar="PR_URL",
        help="Review a canonical GitHub pull-request URL (complete base...head range)")
    run_p.add_argument(
        "--repo", default=".",
        help="Path to the source git repository (default: cwd)")
    run_p.add_argument(
        "--dry-run", action="store_true",
        help="Resolve the changes (and --series children) and show "
             "what the run would review, then stop: nothing is "
             "fetched, reviewed, posted, cleared or updated")
    run_p.add_argument(
        "--series", action="store_true",
        help="Also review every open change stacked on each given "
             "change — its children in Gerrit's relation chain, as "
             "shown from that change — in parallel like any batch")
    run_p.add_argument(
        "--local", action="store_true",
        help="Treat the change arguments as local git refs "
             "(branches/SHAs) of --repo — each reviewed in its own "
             "worktree. Local results are not postable to Gerrit.")
    run_p.add_argument(
        "--mode", choices=["full", "light"], default=DEFAULT_MODE,
        help="Review depth: 'full' (default) runs the review-prompts "
             "review-core.md deep-dive pipeline unchanged; 'light' "
             "runs the bundled single-pass light review (code + "
             "surrounding code + lustre-style.md + commit message) — "
             "much cheaper, artifacts suffixed '-light' so the two "
             "modes never overwrite each other")
    run_p.add_argument(
        "--last", "-n", type=_positive_int, default=None, metavar="N",
        help="Review the newest N commits of --repo (newest first), "
             "each on its own in a worktree. Implies a local review: "
             "results are not postable, and a plain-text dump of the "
             "whole batch is written (see --output)")
    run_p.add_argument(
        "--since", default=None, metavar="REV",
        help="The commit under review is a revision of the earlier "
             "commit REV (e.g. the patchset you started from): review "
             "only what changed from REV, reading the rest of the commit "
             "as context. Findings outside the change are limited to "
             "real bugs. One local commit only (--last 1, a --local ref, "
             "or HEAD); no review runs when nothing changed")
    run_p.add_argument(
        "--backport", action=argparse.BooleanOptionalAction,
        default=_env_flag("LREVIEW_BACKPORT", DEFAULT_BACKPORT),
        help="Review a commit with a Lustre-change: or Lustre-commit: "
             "trailer as a backport: compare it with the upstream change "
             "and classify each finding as BACKPORT, UPSTREAM or "
             "UPSTREAM-BLOCKER (default: on; $LREVIEW_BACKPORT)")
    run_p.add_argument(
        "--output", "-o", default=None, metavar="FILE",
        help="Write a plain-text dump of every review in the batch to "
             "FILE (default with --last: "
             "<results-dir>/review-last<N>.txt)")
    run_p.add_argument(
        "--jobs", "-j", type=_positive_int,
        default=DEFAULT_JOBS,
        help=f"Maximum parallel reviews (default: {DEFAULT_JOBS})")
    run_p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"Per-review timeout in seconds (default: {DEFAULT_TIMEOUT})")
    _add_stall_options(run_p)
    run_p.add_argument(
        "--results-dir", default=default_results_dir(),
        help="Where logs, review JSONs, and summary.json go "
             "(default: $LREVIEW_RESULTS_DIR, else lreview-results/ "
             "in the llm tools checkout; ./lreview-results when "
             "installed without a checkout)")
    run_p.add_argument(
        "--worktrees-dir", default=None,
        help="Where review worktrees are created (default: "
             "worktrees_dir from the config file, else "
             "<results-dir>/worktrees)")
    run_p.add_argument(
        "--keep-worktrees", action="store_true",
        help="Do not remove worktrees after each review")
    run_p.add_argument(
        "--agent", choices=sorted(AGENTS), default=default_agent,
        help="Agent to run the reviews with (default: $LREVIEW_AGENT or "
             "claude; claude, codex, cursor and agy are the verified "
             "backends, auggie, gemini and opencode are best-effort)")
    run_p.add_argument(
        "--fallback-agent", choices=[""] + sorted(AGENTS),
        default=os.environ.get("LREVIEW_FALLBACK_AGENT", ""),
        help="Review again with this agent, at its default model, when "
             "the provider's safety classifier refuses a review "
             "(default: $LREVIEW_FALLBACK_AGENT, else none)")
    run_p.add_argument(
        "--model", default=None,
        help=f"Model for the review runs. Defaults to {CLAUDE_DEFAULT_MODEL} "
             f"for claude, {CODEX_DEFAULT_MODEL} for codex, "
             f"{CURSOR_DEFAULT_MODEL} for cursor, and {AGY_DEFAULT_MODEL} "
             "for agy. $LREVIEW_MODEL overrides "
             "the default. Run 'lreview models' for available models and aliases.")
    run_p.add_argument(
        "--effort", choices=list(EFFORT_LEVELS),
        default=os.environ.get("LREVIEW_EFFORT"),
        help="Reasoning effort for claude/agy (--effort), codex "
             "(-c model_reasoning_effort=...) or auggie (--thinking); "
             "default: $LREVIEW_EFFORT, else high for claude (whatever the "
             "user's settings say), else the agent's own. Ignored for "
             "gemini/opencode/cursor (cursor model slugs carry the "
             "effort). The ladder is per model — 'ultra' is "
             "codex-only, and gpt-5.5 / gpt-5.3-codex-spark stop at "
             "xhigh; see 'lreview models'")
    run_p.add_argument(
        "--lenses", action="store_true",
        help="Lens review (claude or agy, full mode): after categorising the "
             "change, the reviewer starts five lens subagents "
             "(concurrency, lifecycle, data, semantics, build) and "
             "verifies every candidate they write. More bugs per run at "
             "about 2x the cost on claude (lenses at medium effort); "
             "see lreview/docs/lens-review-evaluation.md")
    run_p.add_argument(
        "--lens-model", default=None,
        help="Model for the --lenses lenses (claude: default the review's; "
             "agy: inherit, flash_lite, flash, or pro; default inherit)")
    run_p.add_argument(
        "--lens-effort", choices=list(CLAUDE_EFFORTS), default=None,
        help="Effort for the --lenses lenses (claude only; default: medium)")
    run_p.add_argument(
        "--chat", action="store_true",
        help="When the review ends, go on in its conversation "
             "interactively (claude; pick one of several). Claude's cache "
             "keeps it for 5 minutes, so this is far cheaper than a later "
             "'lreview chat'")
    run_p.add_argument(
        "--memory", "-m", action="store_true",
        help="Deprecated, will be removed: lowers bug recall")
    run_p.add_argument(
        "--clear-memory", "-c", action="store_true",
        help=argparse.SUPPRESS)
    run_p.add_argument(
        "--no-resume", action="store_true", help=argparse.SUPPRESS)
    run_p.add_argument(
        "--db", default=None, metavar="DIR", help=argparse.SUPPRESS)
    run_p.add_argument(
        "--lean", action=argparse.BooleanOptionalAction,
        default=_env_flag("LREVIEW_LEAN", DEFAULT_LEAN),
        help="Start the reviewer with only what a review uses -- claude: "
             "six tools, no MCP servers, skills or CLAUDE.md, a 5-minute "
             "prompt cache; codex: unused features off; agy: "
             "--disable-slash-commands (default: on; "
             "$LREVIEW_LEAN=0 turns it off)")
    run_p.add_argument(
        "--preload", action=argparse.BooleanOptionalAction,
        default=_env_flag("LREVIEW_PRELOAD", DEFAULT_PRELOAD),
        help="Full mode: give the reviewer the protocol and the files it "
             "always loads up front (claude: system prompt; codex, cursor "
             "and agy: start of the prompt, on stdin) and the commit in its first message, instead "
             "of having it read them one call at a time (default: on; "
             "$LREVIEW_PRELOAD=0 turns it off)")
    run_p.add_argument(
        "--agent-arg", "--claude-arg", action="append", dest="agent_arg",
        metavar="ARG",
        help="Extra argument passed through to the agent CLI (repeatable; "
             "use --agent-arg=--flag for arguments starting with a dash)")
    run_p.add_argument(
        "--allow-tool", action="append", dest="allow_tool", metavar="RULE",
        help="claude or cursor agent: a permission rule in that "
             "agent's syntax, for example 'Bash(git log *)' (claude) or "
             "'Shell(git:log *)' (cursor) (repeatable). With one or more "
             "rules claude runs with --permission-mode dontAsk and only "
             "these tools, in place of --dangerously-skip-permissions; "
             "cursor gets the rules in the worktree's .cursor/cli.json "
             "and runs without --force, so it rejects a shell command no "
             "rule allows (its file tools are not gated). "
             "{worktree} and {prompts} expand to the review worktree and "
             "the prompts directory: 'Edit(/{worktree}/**)'")
    run_p.add_argument(
        "--validate", action="store_true",
        help="After each review, show its findings on ltvm VMs: a "
             "claude validator builds the change, writes a reproducer "
             "for each finding, and gives it a verdict (confirmed, "
             "refuted, inconclusive, not-testable). Gerrit reviews and "
             "local commits only; see docs/validate-design.md")
    _add_validate_options(run_p)
    run_p.add_argument(
        "--post", action="store_true",
        help="Post results with findings to their provider when the batch finishes")
    run_p.add_argument(
        "--prefix", default=default_prefix,
        help="Prefix for every posted message; a <model> placeholder is "
             "replaced with the model that ran the review (default: "
             "$LREVIEW_PREFIX, else '[AI review - <model>]'; "
             "pass '' for no prefix)")
    run_p.add_argument(
        "--prompts-dir", default=default_prompts, help=prompts_help)
    run_p.set_defaults(func=cmd_run)

    bench_p = sub.add_parser(
        "bench", help="Run or report the standard benchmark set")
    bench_sub = bench_p.add_subparsers(dest="bench_cmd", required=True)
    default_bench = str(Path(default_results_dir()) / "bench")

    def bench_common(p):
        p.add_argument("--bench-dir", default=default_bench,
                       help="Where bench runs, the case repository and "
                            f"worktrees live (default: {default_bench})")
        p.add_argument("--cases-file", default=None,
                       help="A case file other than the bundled "
                            "benchmark/cases.json")
        p.add_argument("--cases", default=None,
                       help="Comma-separated case ids (overrides --set)")
        p.add_argument("--set", default="quick",
                       help="Case set: 'quick' (default; a few cheap cases "
                            "with reliable hits, for cost and quick checks), "
                            "'eval' (every validated case; expensive), or "
                            "'all'")

    bench_list = bench_sub.add_parser("list", help="Show the cases")
    bench_common(bench_list)
    bench_list.set_defaults(func=cmd_bench)

    bench_run = bench_sub.add_parser(
        "run", help="Review every case with the given options")
    bench_common(bench_run)
    bench_run.add_argument(
        "--label", default=None,
        help="Name of this arm (default: timestamp plus the options); "
             "running again with the same label adds reps to it")
    bench_run.add_argument(
        "--reps", type=_positive_int, default=1,
        help="Times to review each case (default 1)")
    bench_run.add_argument(
        "--repo", default=None,
        help="A lustre-release checkout to fetch case commits from "
             "(fast); else they come from Gerrit")
    bench_run.add_argument("--jobs", "-j", type=_positive_int,
                           default=DEFAULT_BENCH_JOBS)
    bench_run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    _add_stall_options(bench_run)
    bench_run.add_argument("--agent", choices=sorted(AGENTS),
                           default=default_agent)
    bench_run.add_argument("--model", default=None)
    bench_run.add_argument("--effort", choices=list(EFFORT_LEVELS),
                           default=os.environ.get("LREVIEW_EFFORT"))
    bench_run.add_argument("--mode", choices=["full", "light"],
                           default=DEFAULT_MODE)
    bench_run.add_argument("--lean", action=argparse.BooleanOptionalAction,
                           default=_env_flag("LREVIEW_LEAN", DEFAULT_LEAN))
    bench_run.add_argument("--preload", action=argparse.BooleanOptionalAction,
                           default=_env_flag("LREVIEW_PRELOAD", DEFAULT_PRELOAD))
    bench_run.add_argument(
        "--lenses", action="store_true",
        help="Lens review (claude or agy, full mode): after categorising the "
             "change, the reviewer starts five lens subagents "
             "(concurrency, lifecycle, data, semantics, build) and "
             "verifies every candidate they write. More bugs per run at "
             "about 2x the cost on claude (lenses at medium effort); "
             "see lreview/docs/lens-review-evaluation.md")
    bench_run.add_argument(
        "--lens-model", default=None,
        help="Model for the --lenses lenses (claude: default the review's; "
             "agy: inherit, flash_lite, flash, or pro; default inherit)")
    bench_run.add_argument(
        "--lens-effort", choices=list(CLAUDE_EFFORTS), default=None,
        help="Effort for the --lenses lenses (claude only; default: medium)")
    bench_run.add_argument(
        "--offline", action=argparse.BooleanOptionalAction, default=True,
        help="Keep the reviewer off Gerrit and JIRA (default: on): a merged "
             "change's related changes and ticket can show the later fix")
    bench_run.add_argument("--agent-arg", action="append", dest="agent_arg",
                           metavar="ARG")
    bench_run.add_argument("--prompts-dir", default=default_prompts,
                           help=prompts_help)
    bench_run.set_defaults(func=cmd_bench)

    bench_report = bench_sub.add_parser(
        "report", help="Cost, time and known bugs found, per arm")
    bench_common(bench_report)
    bench_report.add_argument(
        "labels", nargs="*",
        help="Arm labels or directories (default: every arm in the bench "
        "dir)")
    bench_report.add_argument(
        "--judge", action="store_true",
        help="Ask an LLM whether each finding is the known bug, instead "
             "of the cases' regex patterns (cached per rep)")
    bench_report.add_argument(
        "--by-bug", action="store_true",
        help="Each known bug's hit rate in each arm, instead of the summary")
    bench_report.add_argument("--json", action="store_true")
    bench_report.set_defaults(func=cmd_bench)

    stats_p = sub.add_parser(
        "stats", help="Where reviews spent their time and money")
    stats_p.add_argument(
        "paths", nargs="*",
        help="Review logs, saved .telemetry.json files, or directories "
             "of logs (default: the results directory)")
    stats_p.add_argument(
        "--last", "-n", type=_positive_int, default=20, metavar="N",
        help="From each directory, the newest N logs (default: 20)")
    stats_p.add_argument(
        "--results-dir", default=default_results_dir(),
        help="Results directory to read when no paths are given")
    stats_p.add_argument(
        "--calls", action="store_true",
        help="Also list every model call of each review")
    stats_p.add_argument(
        "--top", type=_positive_int, default=10,
        help="How many of the costliest tool outputs to list")
    stats_p.add_argument(
        "--all", dest="complete", action="store_false",
        help="Include reviews that did not finish")
    stats_p.add_argument(
        "--json", action="store_true",
        help="Print the summaries (with --calls, the full detail) as JSON")
    stats_p.set_defaults(func=cmd_stats)

    chat_p = sub.add_parser(
        "chat", help="Interactive session over an existing review "
                     "(ask about findings, how the patch works)")
    chat_p.add_argument(
        "change", nargs="?", default=None,
        help="Gerrit change number or URL (with --local: a git ref "
             "of --repo, default HEAD). The reviewed revision and "
             "its artifacts come from the results manifest when the "
             "change was reviewed before; otherwise the current "
             "patchset is resolved from Gerrit.")
    chat_p.add_argument(
        "--local", action="store_true",
        help="Discuss a local review: the change argument is a git "
             "ref of --repo (default HEAD), matched against local "
             "review results by commit — when the ref moved since "
             "(e.g. amended), the reviewed revision is discussed")
    chat_p.add_argument(
        "--repo", default=None,
        help="Path to the source git repository (default: the repo "
             "the change was reviewed from, per the results "
             "manifest; else cwd)")
    chat_p.add_argument(
        "--results-dir", default=default_results_dir(),
        help="Results directory holding the review artifacts "
             "(default: as for run)")
    chat_p.add_argument(
        "--worktrees-dir", default=None,
        help="Where the discussion worktree is created (default: as "
             "for run)")
    chat_p.add_argument(
        "--db", default=None, metavar="DIR",
        help="Memory database directory (default: as for run); the "
             "change's memory document is offered to the session "
             "read-only")
    chat_p.add_argument(
        "--agent", choices=sorted(AGENTS), default=default_agent,
        help="Agent CLI for the interactive session (default: "
             "$LREVIEW_AGENT or claude; claude is the verified "
             "backend, others are best-effort)")
    chat_p.add_argument(
        "--model", default=None,
        help="Model for the interactive session (default: the review's "
             "when continuing its conversation, else the agent's own)")
    chat_p.add_argument(
        "--effort", choices=list(EFFORT_LEVELS),
        default=os.environ.get("LREVIEW_EFFORT"),
        help="Reasoning effort for the session — claude/agy "
             "(--effort), codex (-c model_reasoning_effort=...) or auggie "
             "(--thinking); default: $LREVIEW_EFFORT or the "
             "agent's own default. Ignored for gemini/opencode/cursor; "
             "see 'lreview models' for the per-model ladders")
    chat_p.add_argument(
        "--keep-worktree", action="store_true",
        help="Keep the discussion worktree after the session ends")
    chat_p.add_argument(
        "--no-resume", action="store_true",
        help="Claude agent: start a fresh session from the report and, "
             "for a lens review, the lenses' suspicions and the reviewer's "
             "verdicts, instead of continuing the review's conversation")
    chat_p.add_argument(
        "--agent-arg", "--claude-arg", action="append", dest="agent_arg",
        metavar="ARG",
        help="Extra argument passed through to the agent CLI "
             "(repeatable; use --agent-arg=--flag for arguments "
             "starting with a dash)")
    chat_p.set_defaults(func=cmd_chat)

    models_p = sub.add_parser(
        "models", help="List the models and reasoning efforts each "
                       "agent accepts")
    models_p.add_argument(
        "--agent", choices=sorted(AGENTS), default=None,
        help="Show just this agent (default: claude and codex)")
    models_p.set_defaults(func=cmd_models)

    agents_p = sub.add_parser(
        "agents", help="List the agent backends and which are "
                       "installed and logged in")
    agents_p.add_argument(
        "--no-login", action="store_true",
        help="skip the login checks; check PATH only")
    agents_p.set_defaults(func=cmd_agents)

    render_p = sub.add_parser(
        "render", help="Render existing review JSONs to Markdown reports")
    render_p.add_argument(
        "files", nargs="*",
        help="gerrit-review-*.json or review-result-*.json files "
             "(default: all in --results-dir)")
    render_p.add_argument(
        "--results-dir", default=default_results_dir(),
        help="Results directory to render (default: as for run)")
    render_p.set_defaults(func=cmd_render)

    validate_p = sub.add_parser(
        "validate",
        help="Show the findings of earlier reviews on ltvm VMs")
    validate_p.add_argument(
        "changes", nargs="+",
        help="Change numbers, Gerrit URLs or manifest keys (64086, "
             "64086-light, a local review's key). A change with no "
             "review in --results-dir is validated from its unresolved "
             "AI-review threads on Gerrit. A Gerrit change is tested on "
             "its newest patchset.")
    validate_p.add_argument(
        "--drafts", action="store_true",
        help="Validate your own unresolved draft comments on each Gerrit "
             "change, not the AI-review threads. With --post, each "
             "verdict goes into its draft, which stays a draft: lreview "
             "publishes nothing. The results go under the key "
             "<number>-drafts")
    validate_p.add_argument(
        "--force", action="store_true",
        help="Test every finding again. By default, a second validation "
             "of the same revision tests only the inconclusive findings "
             "and keeps the other verdicts")
    validate_p.add_argument(
        "--repo", default=".",
        help="The git repository the reviews came from (default: cwd)")
    validate_p.add_argument(
        "--results-dir", default=default_results_dir(),
        help="Results directory from a previous run (default: as for run)")
    validate_p.add_argument(
        "--worktrees-dir", default=None,
        help="Where validation worktrees go (default: as for run)")
    validate_p.add_argument(
        "--keep-worktrees", action="store_true",
        help="Keep the validation worktrees")
    validate_p.add_argument(
        "--lean", action=argparse.BooleanOptionalAction, default=True,
        help="Start the validator with the lean tool set (default: on)")
    _add_validate_options(validate_p)
    validate_p.add_argument(
        "--post", action="store_true",
        help="Post the results when validation finishes: an unposted "
             "review goes out with the verdicts applied, a posted one "
             "gets a verdicts message")
    validate_p.add_argument(
        "--prefix", default=default_prefix,
        help="Prefix for every posted message (default: as for post)")
    validate_p.add_argument(
        "--post-report", action="store_true",
        help="Attach the QA report of each confirmed finding to the Jira "
             "ticket at the start of the commit subject, with a short "
             "comment. A report is attached only once. Needs the jira "
             "tool with write access")
    validate_p.set_defaults(func=cmd_validate)

    post_p = sub.add_parser(
        "post", help="Post previously collected results to Gerrit")
    post_p.add_argument(
        "changes", nargs="*",
        help="Change numbers to post (default: all unposted with findings)")
    post_p.add_argument(
        "--results-dir", default=default_results_dir(),
        help="Results directory from a previous run "
             "(default: as for run)")
    post_p.add_argument(
        "--prefix", default=default_prefix,
        help="Prefix for every posted message; a <model> placeholder is "
             "replaced with the model that ran the review (default: "
             "$LREVIEW_PREFIX, else '[AI review - <model>]'; "
             "pass '' for no prefix)")
    post_p.add_argument(
        "--force", action="store_true",
        help="Repost even if the manifest says already posted")
    post_p.add_argument("--dry-run", action="store_true",
        help="Show findings that would be posted without contacting a provider")
    post_p.set_defaults(func=cmd_post)

    from .config import apply_config
    apply_config(sub.choices)
    return parser


def _terminate_as_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> None:
    # Reviews run in sessions of their own, so a SIGTERM that only
    # ends lreview would leave them running (and billing): take the
    # Ctrl-C path, which kills them.
    import signal
    import threading
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _terminate_as_interrupt)
    with telemetry.session("lreview") as run:
        args = build_parser().parse_args(argv)
        run.command = args.command
        maybe_check_for_updates()
        sys.exit(args.func(args))


if __name__ == "__main__":
    main()
