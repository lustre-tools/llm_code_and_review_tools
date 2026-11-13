"""Machine-readable API description for the Gerrit CLI tool.

Provides the full command surface so LLMs can discover capabilities
without parsing --help text.
"""

import argparse

from llm_tool_common import Argument, Command, ToolDescription

from . import __version__


def registered_commands(parser: argparse.ArgumentParser) -> dict[str, tuple[str, argparse.ArgumentParser]]:
    """Every subcommand name and alias the parser accepts, mapped to
    (canonical name, subparser)."""
    sub_action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    canonical: dict[int, str] = {}
    names = {}
    for name, sub in sub_action.choices.items():
        canonical.setdefault(id(sub), name)
        names[name] = (canonical[id(sub)], sub)
    return names


def _argument_from_action(action: argparse.Action) -> Argument:
    if isinstance(action, argparse._SubParsersAction):
        return Argument(name=action.dest or "subcommand",
                        description=action.help or "Subcommand",
                        required=bool(action.required),
                        choices=list(action.choices))
    if action.option_strings:
        longs = [o for o in action.option_strings if o.startswith("--")]
        name = longs[0] if longs else action.option_strings[0]
    else:
        name = action.dest
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        arg_type = "boolean"
    elif action.type is int:
        arg_type = "integer"
    elif isinstance(action, (argparse._AppendAction, argparse._ExtendAction)) or \
            action.nargs in ("*", "+"):
        arg_type = "list"
    else:
        arg_type = "string"
    help_text = action.help or ""
    try:
        help_text = help_text % {"default": action.default}
    except (KeyError, TypeError, ValueError):
        pass
    default = action.default
    if default is argparse.SUPPRESS or isinstance(default, list):
        default = None
    return Argument(
        name=name,
        description=help_text,
        type=arg_type,
        required=bool(action.required),
        default=default,
        choices=[str(c) for c in action.choices] if action.choices else None,
    )


def _own_action(action: argparse.Action) -> bool:
    """Not --help, nor the global options every subparser lists."""
    if isinstance(action, argparse._HelpAction):
        return False
    return not getattr(action, "global_option", False)


def command_from_parser(name: str, sub: argparse.ArgumentParser, summary: str = "") -> Command:
    """A Command built from a subparser, for commands the table below lacks."""
    sub.prog = f"gc {name}"
    formatter = sub._get_formatter()
    formatter.add_usage(sub.usage, [a for a in sub._actions if _own_action(a)],
                        sub._mutually_exclusive_groups)
    usage = " ".join(formatter.format_help().split())
    if usage.startswith("usage: "):
        usage = usage[len("usage: "):]
    usage = usage.replace(" [-h]", "")
    return Command(
        name=name,
        description=" ".join((sub.description or summary or "").split()),
        usage=usage,
        arguments=[_argument_from_action(a) for a in sub._actions
                   if _own_action(a)],
    )


def complete_description(tool_desc: ToolDescription, parser: argparse.ArgumentParser) -> ToolDescription:
    """Add every registered command the curated table leaves out, built
    from its parser, so describe never omits a command."""
    known = {c.name for c in tool_desc.commands}
    sub_action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: a.help for a in sub_action._choices_actions}
    seen = set()
    for canon, sub in registered_commands(parser).values():
        if canon in known or canon in seen:
            continue
        seen.add(canon)
        tool_desc.commands.append(command_from_parser(canon, sub, helps.get(canon) or ""))
    return tool_desc


def get_tool_description() -> ToolDescription:
    """Return the complete gerrit-cli tool API description."""
    return ToolDescription(
        name="gerrit-cli",
        version=__version__,
        description=(
            "Gerrit code review tool for LLM agents. "
            "Extract comments, reply, review diffs, and manage patch series. "
            "Use 'gc' or 'gerrit-cli' as short aliases for 'gerrit'."
        ),
        env_vars=[
            {"name": "GERRIT_URL", "description": "Gerrit server URL", "required": "true"},
            {"name": "GERRIT_USER", "description": "Gerrit username", "required": "true"},
            {"name": "GERRIT_PASS", "description": "Gerrit HTTP password", "required": "true"},
        ],
        commands=[
            Command(
                name="check",
                description="Check configuration and authentication using a read-only account request; does not verify project publishing permissions",
                usage="gc check [--offline]",
                arguments=[Argument(name="--offline", description="Report the configuration file location without network access", type="boolean", default=False)],
                output_fields=["status", "message", "authenticated", "config_file", "config_exists"],
            ),
            # --- Core comment commands ---
            Command(
                name="comments",
                description="Get unresolved comments from a Gerrit change with code context",
                usage="gc comments <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id", required=True),
                    Argument(name="--all", description="Include resolved comments too", type="boolean", default=False),
                    Argument(name="--no-context", description="Skip code context around comments", type="boolean", default=False),
                    Argument(name="--context-lines", description="Lines of code context", type="integer", default=3),
                    Argument(name="--summary", description="Truncate code context to N lines", type="integer"),
                    Argument(name="--fields", description="Comma-separated fields per thread: index,file,line,message,author,resolved,patch_set,updated,last_updated,code_context,replies"),
                    Argument(name="--timeline", description="Every comment, root or reply, oldest first: updated, patch_set, author, file, line, thread, id (the comment id reply and batch take), reply, thread_resolved, message"),
                    Argument(name="--messages", description="Only the change messages (top-level, e.g. a bare 'BUILD'), oldest first: date, patch_set, author, message"),
                    Argument(name="--since", description="Only comments/messages/threads with activity at or after a UTC time, e.g. '2026-10-01 17:30', or an age back from now (90m, 2h, 1d, 1w); in the default listing, threads (index kept) and review_messages"),
                ],
                examples=[
                    "gc comments https://review.example.com/c/project/+/12345",
                    "gc comments 12345 --fields=index,file,message",
                    "gc comments 12345 --summary 5",
                ],
                output_fields=["change_number", "threads[].index", "threads[].file", "threads[].line", "threads[].message", "threads[].author", "threads[].code_context"],
                next_actions=["reply", "stage", "review"],
            ),
            Command(
                name="reply",
                description="Reply to a comment thread (URL optional if recently ran 'comments')",
                usage='gc reply <THREAD_INDEX|COMMENT_ID> "<MESSAGE>" [--url URL]',
                arguments=[
                    Argument(name="thread", description="Thread index from 'comments' output, or a comment id in the thread (root_comment.id). Indices shift as threads are resolved; use comment ids for more than one reply", required=True),
                    Argument(name="message", description="Reply message (required unless --done or --ack)"),
                    Argument(name="--url", description="Gerrit change URL (uses last URL from 'gc comments' if omitted)"),
                    Argument(name="--done", description="Mark as done (adds 'Done' and resolves)", type="boolean", default=False),
                    Argument(name="--ack", description="Acknowledge (adds 'Acknowledged' and resolves)", type="boolean", default=False),
                    Argument(name="--resolve", description="Mark thread as resolved", type="boolean", default=False),
                    Argument(name="--dry-run", description="Preview without posting", type="boolean", default=False),
                ],
                examples=[
                    'gc reply 0 "Fixed in next patchset"',
                    "gc reply --done 1",
                    "gc reply --ack 2",
                    'gc reply fe4b1c39_3f8c9a2e "Fixed" --resolve',
                ],
                output_fields=["change_number", "thread_index", "message", "resolved"],
                next_actions=["comments (to verify)", "reply (next thread)"],
            ),
            Command(
                name="batch",
                description="Reply to multiple comments from a JSON file",
                usage="gc batch <URL> <FILE>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id", required=True),
                    Argument(name="file", description="JSON file with replies [{comment_id | thread_index | file [+ line], message, mark_resolved}]; '-' reads the JSON from stdin", required=True),
                    Argument(name="--dry-run", description="Preview without posting", type="boolean", default=False),
                ],
                examples=["gc batch 12345 replies.json", "gc batch 12345 replies.json --dry-run"],
                next_actions=["comments (to verify replies posted)"],
            ),
            # --- Review commands ---
            Command(
                name="review",
                description="Get code diffs for review, optionally post review comments",
                usage="gc review <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id", required=True),
                    Argument(name="--changes-only", description="Show only changed lines", type="boolean", default=False),
                    Argument(name="--full-content", description="Include full file content", type="boolean", default=False),
                    Argument(name="--unified", description="Lines of diff context", type="integer", default=3),
                    Argument(name="--base", description="Base patchset for comparison", type="integer"),
                    Argument(name="--post-comments", description="Post review comments from JSON file"),
                    Argument(name="--vote", description="Code-Review vote (-2 to +2)", type="integer", choices=["-2", "-1", "0", "1", "2"]),
                    Argument(name="--message", description="Review message"),
                    Argument(name="--prefix", description="Prefix for review message and every comment"),
                    Argument(name="--tag", description="Gerrit review tag (e.g. autogenerated:ai-review)"),
                    Argument(name="--summary", description="Truncate diffs to N lines per hunk", type="integer"),
                    Argument(name="--dry-run", description="Preview without posting", type="boolean", default=False),
                ],
                examples=[
                    "gc review 12345",
                    "gc review 12345 --summary 10",
                    "gc review 12345 --post-comments comments.json --vote 1",
                    "gc review 12345 --post-comments review.json --prefix '[Marc Bot]' --dry-run",
                ],
                output_fields=["change_number", "files[].path", "files[].diff", "files[].insertions", "files[].deletions"],
                next_actions=["comments", "review-series"],
            ),
            Command(
                name="diff",
                description="Show what changed between two patchsets (default: newest against the one before)",
                usage="gc diff <URL> [PATCHSET_A [PATCHSET_B]]",
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id", required=True),
                    Argument(name="patchset_a", description="Base patchset (default: the one before patchset_b)", type="integer"),
                    Argument(name="patchset_b", description="Target patchset (default: newest)", type="integer"),
                    Argument(name="--unified", description="Lines of diff context", type="integer", default=3),
                ],
                examples=["gc diff 12345", "gc diff 12345 3", "gc diff 12345 3 5"],
                output_fields=["change_number", "patchset_a", "patchset_b", "files[].path", "files[].diff", "note"],
                next_actions=["review", "comments"],
            ),
            # --- Series commands ---
            Command(
                name="review-series",
                description="Start reviewing a patch series - lists patches and begins session",
                usage="gc review-series <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL (any patch in series)", required=True),
                    Argument(name="--urls-only", description="Output only URLs, one per line", type="boolean", default=False),
                    Argument(name="--numbers-only", description="Output only change numbers", type="boolean", default=False),
                    Argument(name="--include-abandoned", description="Include abandoned patches", type="boolean", default=False),
                    Argument(name="--no-prompt", description="Leave review_prompt out of the output", type="boolean", default=False),
                    Argument(name="--no-checkout", description="Only list the series: no checkout, no session (default: checkout first patch with comments)", type="boolean", default=False),
                ],
                examples=[
                    "gc review-series https://review.example.com/c/project/+/12345",
                    "gc review-series 12345 --no-checkout",
                ],
                output_fields=["series[]", "patches_with_comments", "review_prompt"],
                next_actions=["work-on-patch", "series-comments", "series-status"],
            ),
            Command(
                name="series-comments",
                description="Get comments for all patches in a series",
                usage="gc series-comments <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL (any patch in series)", required=True),
                    Argument(name="--all", description="Include resolved comments", type="boolean", default=False),
                    Argument(name="--no-context", description="Skip code context", type="boolean", default=False),
                    Argument(name="--context-lines", description="Lines of code context", type="integer", default=3),
                    Argument(name="--summary", description="Truncate code context to N lines", type="integer"),
                    Argument(name="--fields", description="Comma-separated fields per thread"),
                ],
                examples=["gc series-comments 12345"],
                next_actions=["work-on-patch", "stage"],
            ),
            Command(
                name="series-status",
                description="Show status, comments, and review state for each patch in a series",
                usage="gc series-status <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL (any patch in series)", required=True),
                ],
                examples=["gc series-status 12345"],
                output_fields=["patches[].number", "patches[].subject", "patches[].status", "patches[].unresolved_comments"],
                next_actions=["review-series", "work-on-patch"],
            ),
            # --- Session workflow ---
            Command(
                name="work-on-patch",
                description="Checkout a specific patch and show its comments",
                usage="gc work-on-patch <CHANGE_NUMBER_OR_URL>",
                arguments=[
                    Argument(name="target", description="Change number (e.g. 64142) or Gerrit URL", required=True),
                ],
                examples=["gc work-on-patch 12345", "gc work-on-patch https://review.whamcloud.com/12345"],
                next_actions=["stage", "finish-patch"],
            ),
            Command(
                name="finish-patch",
                description="Complete current patch work, rebase remaining patches, auto-advance",
                usage="gc finish-patch",
                arguments=[
                    Argument(name="--stay", description="Stay on current patch instead of auto-advancing", type="boolean", default=False),
                ],
                examples=["gc finish-patch"],
                next_actions=["status", "abort"],
            ),
            Command(
                name="next-patch",
                description="Move to the next patch in the series",
                usage="gc next-patch",
                arguments=[
                    Argument(name="--with-comments", description="Skip to next with unresolved comments", type="boolean", default=False),
                ],
                examples=["gc next-patch", "gc next-patch --with-comments"],
                next_actions=["stage", "finish-patch"],
            ),
            Command(
                name="status",
                description="Show current session status",
                usage="gc status",
                arguments=[],
                examples=["gc status"],
                next_actions=["work-on-patch", "finish-patch", "abort"],
            ),
            Command(
                name="abort",
                description="End session (default: discard changes; --keep-changes to preserve)",
                usage="gc abort [--keep-changes]",
                arguments=[
                    Argument(name="--keep-changes", description="Keep current git state", type="boolean", default=False),
                ],
                examples=["gc abort", "gc abort --keep-changes"],
            ),
            # --- Staging ---
            Command(
                name="stage",
                description="Stage a comment reply for later posting",
                usage='gc stage <THREAD_INDEX> ["MESSAGE"] [--done] [--ack]',
                arguments=[
                    Argument(name="thread_index", description="Thread index from 'comments' output", type="integer", required=True),
                    Argument(name="message", description="Reply message (required unless --done or --ack)"),
                    Argument(name="--done", description="Mark as done", type="boolean", default=False),
                    Argument(name="--ack", description="Acknowledge", type="boolean", default=False),
                    Argument(name="--resolve", description="Mark as resolved", type="boolean", default=False),
                    Argument(name="--url", description="Gerrit URL (optional if session active)"),
                ],
                examples=[
                    "gc stage --done 0",
                    'gc stage 1 "Fixed the null check"',
                ],
                next_actions=["staged list", "push", "finish-patch"],
            ),
            Command(
                name="push",
                description="Post all staged comment replies to Gerrit (does not push commits; see upload)",
                usage="gc push [CHANGE_NUMBER]",
                arguments=[
                    Argument(name="change_number", description="Change number (omit to push all)", type="integer"),
                    Argument(name="--dry-run", description="Preview without posting", type="boolean", default=False),
                ],
                examples=["gc push", "gc push 12345", "gc push --dry-run"],
                next_actions=["comments (to verify)"],
            ),
            # --- Commit upload ---
            Command(
                name="upload",
                description=(
                    "Push HEAD to refs/for/<branch> over HTTPS as the selected "
                    "credential set's account. One commit by default: with CHANGE, "
                    "refuses unless HEAD's Change-Id is that change's; without it, "
                    "HEAD's Change-Id finds the change (a Change-Id Gerrit does not "
                    "know becomes a new change when --project and --branch are "
                    "given). More than one commit Gerrit does not have is refused "
                    "unless --series, which uploads each to the change its own "
                    "Change-Id names. Two commits with one Change-Id (a commit "
                    "on top of a patchset where an amend was meant) are refused "
                    "as DUPLICATE_CHANGE_ID, with the command that squashes them. "
                    "A committer email not registered to the "
                    "account is rewritten to the account's name and preferred "
                    "email; author, tree and message are untouched. "
                    "With GERRIT_UPLOAD_AUTHOR naming a person, their commits "
                    "are pushed authored and signed off by the account, then "
                    "edited back to their author and message on Gerrit with "
                    "notify NONE (changes above are rebased first), so Gerrit "
                    "does not add them as a reviewer; author_edit[] lists "
                    "each, and HEAD moves to the edited commits. "
                    "--no-author-edit skips this. "
                    "--expect-patchset N refuses the push when the change has "
                    "moved past patchset N, so a concurrent uploader's "
                    "patchset is not silently replaced."
                ),
                usage="gc [--user SET] upload [CHANGE] [--series] [--branch B] [--base BRANCH] [--project P] [--topic T] [--repo PATH] [--no-amend] [--no-author-edit] [--expect-patchset N] [--dry-run]",
                arguments=[
                    Argument(name="change", description="Change number, URL or Change-Id that HEAD (with --series: one of the uploaded commits) must belong to (default: the one HEAD's Change-Id names)"),
                    Argument(name="--series", description="Upload every commit between the branch and HEAD, each to its own change by its Change-Id", type="boolean", default=False),
                    Argument(name="--branch", description="Target branch (default: the change's; required for a new change)"),
                    Argument(name="--base", description="Another server branch HEAD is built on (e.g. master-next at a reviewer's request); its commits are not counted, and the push still targets --branch"),
                    Argument(name="--project", description="Gerrit project (default: the change's; required for a new change)"),
                    Argument(name="--topic", description="Topic to set on the upload"),
                    Argument(name="--repo", description="Repository to upload from", default="."),
                    Argument(name="--no-amend", description="Refuse instead of rewriting an unregistered committer", type="boolean", default=False),
                    Argument(name="--no-author-edit", description="With GERRIT_UPLOAD_AUTHOR set, push that person's commits as they are, adding them as a reviewer, instead of as the account with the author and Signed-off-by edited back on Gerrit (notify none) after the push", type="boolean", default=False),
                    Argument(name="--expect-patchset", description="Refuse unless the change is still at this patchset, the one HEAD replaces; catches another uploader having got there first. Not for --series", type="integer"),
                    Argument(name="--dry-run", description="Do every check, print the push with the password redacted and what each commit would update or create; push nothing", type="boolean", default=False),
                ],
                examples=[
                    "gc --user patrickbot upload 12345 --dry-run",
                    "gc --user patrickbot upload 12345",
                    "gc --user patrickbot upload 12345 --series",
                    "gc --user patrickbot upload 12345 --expect-patchset 3",
                    "gc --user patrickbot upload 12345 --base master-next",
                    "gc upload --project fs/lustre-release --branch master",
                ],
                output_fields=["sha", "change_number", "url", "patchset", "new_change", "committer_amended[]", "author_edit[]", "commits[].action", "commits[].change_number", "commits[].patchset", "base", "remote_messages", "warnings"],
                next_actions=["info", "comments"],
            ),
            Command(
                name="staged list",
                description="List all staged operations",
                usage="gc staged list",
                arguments=[],
                examples=["gc staged list"],
                next_actions=["push", "staged clear"],
            ),
            Command(
                name="staged clear",
                description="Clear staged operations",
                usage="gc staged clear [CHANGE_NUMBER]",
                arguments=[
                    Argument(name="change_number", description="Change number (omit to clear all)", type="integer"),
                ],
                examples=["gc staged clear", "gc staged clear 12345"],
            ),
            # --- Reviewer management ---
            Command(
                name="reviewers",
                description="List reviewers and CCs on a change: role, when added, votes and last activity",
                usage="gc reviewers <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id", required=True),
                ],
                examples=["gc reviewers 12345"],
                output_fields=[
                    "change_number", "current_patchset", "count",
                    "reviewer_count", "cc_count", "human_participant_count",
                    "people_besides_owner",
                    "owner", "commit_author",
                    "reviewers[].account_id", "reviewers[].name",
                    "reviewers[].email", "reviewers[].username",
                    "reviewers[].role", "reviewers[].service_user",
                    "reviewers[].is_bot", "reviewers[].owner",
                    "reviewers[].commit_author", "reviewers[].approvals",
                    "reviewers[].added_at", "reviewers[].added_by",
                    "reviewers[].votes", "reviewers[].has_current_vote",
                    "reviewers[].copied_votes",
                    "reviewers[].voted_on_current_patchset",
                    "reviewers[].last_vote", "reviewers[].last_activity",
                    "reviewers[].message_count", "reviewers[].comment_count",
                    "reviewers[].in_attention_set",
                    "removed[]", "inline_comments_unavailable",
                ],
                next_actions=["add-reviewer", "remove-reviewer", "reviewer-candidates"],
            ),
            Command(
                name="reviewer-candidates",
                description="Rank reviewer candidates for a change's files: Reviewed-by trailers and authored commits per file from local git history; who MAINTAINERS names is listed beside the ranking (maintainers), not in it",
                usage="gc reviewer-candidates [URL] [--path P ...] [--repo DIR] [--ref REF] [--months N] [--widen-to N] [--load] [--asked FILE] [--compact] [--table]",
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id (optional with --path)"),
                    Argument(name="--path", description="File or directory instead of the change's files (repeatable)"),
                    Argument(name="--repo", description="lustre-release checkout to read history from", default="."),
                    Argument(name="--ref", description="History to read", default="HEAD"),
                    Argument(name="--months", description="Window in months", type="integer", default=12),
                    Argument(name="--widen-to", description="Months to widen to when the window finds fewer than 3 people not on the change; widened_from then names the first window (0 never widens)", type="integer", default=36),
                    Argument(name="--limit", description="Candidates to list, 0 for all; with --load or --asked, cut after holds are weighed (held people last)", type="integer", default=10),
                    Argument(name="--exclude", description="Name or email to leave out (repeatable)"),
                    Argument(name="--brief", description="Files as bare paths", type="boolean", default=False),
                    Argument(name="--compact", description="Per candidate only name, email, account_id, score, reviewed, last_reviewed, on_change, balanced, waiting, votes, asks, hold and senior (newcomer and inactive when true); plus the change's on_change list", type="boolean", default=False),
                    Argument(name="--table", description="Like --compact but one text row per candidate in ranked order (see columns): rank, name, email, eligible/hold/on_change, senior, Reviewed-by on the files, last review, tree-wide reviews in 12 months, then balanced, load and asks when given", type="boolean", default=False),
                    Argument(name="--load", description="Weigh by each candidate's Gerrit review load", type="boolean", default=False),
                    Argument(name="--asked", description="JSON of the caller's earlier asks per person, to weigh and hold by"),
                ],
                examples=["gc reviewer-candidates 12345 --repo ~/lustre-release --ref upstream/master",
                          "gc reviewer-candidates 12345 --load --asked asks.json",
                          "gc reviewer-candidates 12345 --load --asked asks.json --compact"],
                output_fields=[
                    "change_number", "project", "branch", "subject", "owner",
                    "commit_author", "repo", "ref", "window_months",
                    "commits_examined", "paths", "paths_without_history",
                    "count", "total_candidates",
                    "candidates[].name", "candidates[].email",
                    "candidates[].score", "candidates[].reviewed",
                    "candidates[].authored", "candidates[].review_share",
                    "candidates[].last_reviewed",
                    "candidates[].last_authored", "candidates[].file_count",
                    "candidates[].files", "candidates[].on_change",
                    "candidates[].aliases", "candidates[].account_id",
                    "candidates[].load", "candidates[].asked",
                    "candidates[].balanced", "candidates[].factors",
                    "candidates[].hold", "candidates[].senior",
                    "candidates[].reviews_12m", "candidates[].newcomer",
                    "candidates[].inactive", "candidates[].maintainer",
                    "order", "asked_by", "excluded[].reason",
                    "maintainers_file", "maintainers[].name",
                    "maintainers[].email", "maintainers[].role",
                    "maintainers[].sections", "maintainers[].files",
                    "maintainers[].rank", "maintainers[].on_change",
                    "maintainers[].excluded", "maintainers[].hold",
                ],
                next_actions=["reviewers", "add-reviewer"],
            ),
            Command(
                name="add-reviewer",
                description=("Add one or more reviewers (fuzzy name matching). All names are "
                             "matched before anyone is added. Each entry of 'added' is Gerrit's "
                             "own answer -- added_reviewer, added_cc, already_on_change or "
                             "error -- so no 'gc reviewers' is needed to confirm."),
                usage='gc add-reviewer <URL> "<NAME>" ["<NAME>" ...]',
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id", required=True),
                    Argument(name="names", description="One or more names, emails, or usernames (fuzzy matching supported)", required=True),
                    Argument(name="--cc", description="Add as CC instead of reviewer", type="boolean", default=False),
                    Argument(name="--dry-run", description="Preview without adding", type="boolean", default=False),
                ],
                examples=[
                    'gc add-reviewer 12345 "John Smith"',
                    "gc add-reviewer 12345 jsmith adilger@whamcloud.com",
                    'gc add-reviewer 12345 "john" --cc',
                ],
                next_actions=["reviewers"],
            ),
            Command(
                name="remove-reviewer",
                description="Remove a reviewer from a change",
                usage='gc remove-reviewer <URL> "<NAME>"',
                arguments=[
                    Argument(name="url", description="Gerrit change URL, number or Change-Id", required=True),
                    Argument(name="name", description="Name, email, or username", required=True),
                    Argument(name="--dry-run", description="Preview without removing", type="boolean", default=False),
                ],
                examples=['gc remove-reviewer 12345 "simmons"'],
                next_actions=["reviewers"],
            ),
            Command(
                name="search",
                description="Search for changes with Gerrit's query syntax (message:, comment:, path:, topic:, hashtag:, after:, ...)",
                usage='gc search "<QUERY>"',
                arguments=[
                    Argument(name="query", description="Gerrit search query, passed as typed", required=True),
                    Argument(name="--limit", description="Results in one page", type="integer", default=25),
                    Argument(name="--start", description="Offset of the page", type="integer", default=0),
                    Argument(name="--all", description="Follow the pages up to --max", type="boolean", default=False),
                    Argument(name="--max", description="With --all: most results", type="integer", default=500),
                ],
                examples=[
                    "gc search 'message:\"LU-12345\" after:2026-01-01 -is:abandoned'",
                    "gc search --all 'path:\"lustre/llite/file.c\" status:open'",
                ],
                output_fields=["count", "changes[].number", "changes[].subject", "changes[].status", "changes[].created", "changes[].updated", "more_results", "next_start"],
                next_actions=["info", "diff", "comments"],
            ),
            Command(
                name="find-user",
                description="Search for Gerrit users by name, email, or username",
                usage='gc find-user "<QUERY>" ["<QUERY>" ...]',
                arguments=[
                    Argument(name="queries", description="One or more search queries; several give {results: [...]}, one entry per query, in one call", required=True),
                    Argument(name="--limit", description="Max results", type="integer", default=10),
                ],
                examples=['gc find-user "farrell"', 'gc find-user "john" --limit 5',
                          'gc find-user "Jian Yu" adilger@whamcloud.com "Oleg Drokin"'],
                output_fields=["users[].username", "users[].name", "users[].email", "users[].account_id", "users[].is_bot"],
                next_actions=["add-reviewer"],
            ),
            # --- Help ---
            Command(
                name="explain",
                description="Show detailed usage and examples for a specific command",
                usage="gc explain <COMMAND>",
                arguments=[
                    Argument(name="command_name", description="Command to explain", required=True),
                ],
                examples=["gc explain reply", "gc explain add-reviewer"],
            ),
            Command(
                name="examples",
                description="Show common workflow examples",
                usage="gc examples [WORKFLOW]",
                arguments=[
                    Argument(name="workflow", description="Workflow to show", choices=["quick", "series", "staging", "reviewers", "all"], default="quick"),
                ],
                examples=["gc examples", "gc examples staging", "gc examples all"],
            ),
            Command(
                name="describe",
                description="Show this machine-readable API description",
                usage="gc describe",
                arguments=[
                    Argument(name="--command", description="Show description for a specific command only"),
                ],
                examples=["gc describe", "gc describe --command comments"],
            ),
        ],
    )
