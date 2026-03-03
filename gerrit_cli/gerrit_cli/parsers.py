"""Argparse parser definitions for gerrit CLI.

This module contains all the argparse subparser definitions, keeping
the main cli.py focused on command implementations.
"""



def add_extract_parser(subparsers):
    """Add the 'comments' subcommand parser (with 'extract' as alias)."""
    parser = subparsers.add_parser(
        "comments",
        aliases=["extract"],
        help="Get comments from a Gerrit change",
        description="Get unresolved comments from a Gerrit change URL",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Include resolved comments (default: unresolved only)",
    )
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Don't include code context around comments",
    )
    parser.add_argument(
        "--context-lines", "-c",
        type=int,
        default=3,
        help="Lines of code context (default: 3)",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    parser.add_argument(
        "--summary", "-s",
        nargs="?",
        const=10,
        type=int,
        metavar="LINES",
        help="Truncate code context to N lines (default: 10). Shows hint to use without --summary for full content.",
    )
    parser.add_argument(
        "--fields",
        type=str,
        default=None,
        help="Comma-separated fields to include per thread. "
             "Available: index,file,line,message,author,resolved,patch_set,code_context. "
             "Example: --fields=index,file,message",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help="Include system and CI bot messages (patchset uploads, rebases, "
             "Maloo results, Jenkins builds, Autotest retests)",
    )
    parser.add_argument(
        "--include-ci",
        action="store_true",
        help="Include CI/build bot messages only (Maloo, Jenkins, Autotest). "
             "Also enabled by --include-system.",
    )
    return parser


def add_reply_parser(subparsers):
    """Add the 'reply' subcommand parser."""
    parser = subparsers.add_parser(
        "reply",
        help="Reply to a comment",
        description="Reply to a comment thread. URL is optional if you recently ran 'gc comments'.",
    )
    parser.add_argument(
        "thread_index",
        type=int,
        help="Thread index from 'comments' output",
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="Reply message (required unless --done or --ack)",
    )
    parser.add_argument(
        "--url", "-u",
        help="Gerrit change URL (uses last-used URL from 'gc comments' if omitted)",
    )
    parser.add_argument(
        "--done", "-d",
        action="store_true",
        help="Mark as done (adds 'Done' message and resolves)",
    )
    parser.add_argument(
        "--ack", "-a",
        action="store_true",
        help="Acknowledge (adds 'Acknowledged' message and resolves)",
    )
    parser.add_argument(
        "--resolve", "-r",
        action="store_true",
        help="Mark thread as resolved after reply",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be posted without actually posting",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    return parser


def add_batch_parser(subparsers):
    """Add the 'batch' subcommand parser."""
    parser = subparsers.add_parser(
        "batch",
        help="Reply to multiple comments from JSON file",
        description="Post multiple replies from a JSON file",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "file",
        help="JSON file with replies [{thread_index, message, mark_resolved}]",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be posted without actually posting",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    return parser


def add_review_parser(subparsers):
    """Add the 'review' subcommand parser."""
    parser = subparsers.add_parser(
        "review",
        help="Get code changes for review",
        description="Get diffs and changes from a Gerrit change for code review. "
                    "By default returns a compact diff with 3 lines of context per hunk "
                    "(equivalent to 'git diff -U3'). Use --full-context for the full file, "
                    "or --changes-only for just the changed lines.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    parser.add_argument(
        "--changes-only", "-c",
        action="store_true",
        dest="changes_only",
        help="Show only changed lines, no surrounding context",
    )
    parser.add_argument(
        "--full-context",
        action="store_true",
        dest="full_context",
        help="Include the full file as context around each change (very verbose; "
             "use for in-depth review of small files)",
    )
    parser.add_argument(
        "--full-content", "-f",
        action="store_true",
        dest="full_content",
        help="Fetch and include the complete new file content alongside the diff",
    )
    parser.add_argument(
        "--unified", "-u",
        type=int,
        default=3,
        metavar="N",
        help="Lines of context around each changed hunk (default: 3). "
             "Ignored if --full-context or --changes-only is given.",
    )
    parser.add_argument(
        "--base", "-b",
        type=int,
        default=None,
        help="Base patchset for comparison (default: parent commit)",
    )
    parser.add_argument(
        "--post-comments",
        metavar="FILE",
        help="Post review comments from JSON file",
    )
    parser.add_argument(
        "--vote",
        type=int,
        choices=[-2, -1, 0, 1, 2],
        help="Code-Review vote (-2 to +2)",
    )
    parser.add_argument(
        "--message", "-m",
        help="Review message",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be posted without actually posting (with --post-comments)",
    )
    parser.add_argument(
        "--summary", "-s",
        nargs="?",
        const=10,
        type=int,
        metavar="LINES",
        help="Truncate diffs to N lines per hunk (default: 10). Shows hint to use without --summary for full content.",
    )
    return parser


def add_series_comments_parser(subparsers):
    """Add the 'series-comments' subcommand parser."""
    parser = subparsers.add_parser(
        "series-comments",
        help="Get comments for all patches in a series",
        description="Extract comments from all patches in a Gerrit series",
    )
    parser.add_argument("url", help="Gerrit change URL or number (any patch in the series)")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Include resolved comments",
    )
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Don't include code context around comments",
    )
    parser.add_argument(
        "--context-lines", "-c",
        type=int,
        default=3,
        help="Lines of code context (default: 3)",
    )
    parser.add_argument(
        "--summary", "-s",
        nargs="?",
        const=10,
        type=int,
        metavar="LINES",
        help="Truncate code context to N lines (default: 10). Shows hint to use without --summary for full content.",
    )
    parser.add_argument(
        "--fields",
        type=str,
        default=None,
        help="Comma-separated fields to include per thread. "
             "Available: index,file,line,message,author,resolved,patch_set,code_context. "
             "Example: --fields=index,file,message",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help="Include system and CI bot messages (patchset uploads, rebases, "
             "Maloo results, Jenkins builds, Autotest retests)",
    )
    parser.add_argument(
        "--include-ci",
        action="store_true",
        help="Include CI/build bot messages only (Maloo, Jenkins, Autotest). "
             "Also enabled by --include-system.",
    )
    return parser


def add_review_series_parser(subparsers):
    """Add the 'review-series' subcommand parser."""
    parser = subparsers.add_parser(
        "review-series",
        help="Start reviewing a patch series - shows patches and AI prompt",
        description="List all patches in a series and show the AI review prompt. "
                    "This is the main entry point for AI-assisted patch series review.",
    )
    parser.add_argument("url", help="Gerrit change URL or number (any patch in the series)")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    parser.add_argument(
        "--urls-only", "-u",
        action="store_true",
        help="Output only URLs (one per line)",
    )
    parser.add_argument(
        "--numbers-only", "-n",
        action="store_true",
        help="Output only change numbers (one per line)",
    )
    parser.add_argument(
        "--include-abandoned", "-a",
        action="store_true",
        help="Include abandoned patches in the series",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip showing AI review prompt",
    )
    parser.add_argument(
        "--checkout", "-c",
        action="store_true",
        help="Checkout the first patch with comments and start a session",
    )
    return parser


def add_interactive_parser(subparsers):
    """Add the 'interactive' subcommand parser."""
    parser = subparsers.add_parser(
        "interactive", aliases=["i"],
        help="Interactive mode for reviewing comments",
        description="Review and reply to comments interactively",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "--vim",
        action="store_true",
        help="Use vim-based interactive mode with tmux",
    )
    return parser


def add_series_status_parser(subparsers):
    """Add the 'series-status' subcommand parser."""
    parser = subparsers.add_parser(
        "series-status",
        help="Show status of all patches in a series",
        description="Display status, comments, and review state for each patch",
    )
    parser.add_argument("url", help="Gerrit change URL or number (any patch in series)")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    return parser


def add_work_on_patch_parser(subparsers):
    """Add the 'work-on-patch' subcommand parser."""
    parser = subparsers.add_parser(
        "work-on-patch",
        help="Start working on a patch (checkout and show comments)",
        description=(
            "Checkout a specific patch and show its comments. "
            "Accepts a change number (e.g. 64142) or a full Gerrit URL. "
            "When given a change number, the URL is derived from the GERRIT_URL "
            "environment variable."
        ),
    )
    parser.add_argument(
        "target",
        help="Change number or Gerrit URL",
    )
    return parser


def add_next_patch_parser(subparsers):
    """Add the 'next-patch' subcommand parser."""
    parser = subparsers.add_parser(
        "next-patch",
        help="Move to the next patch in the series",
        description="Checkout the next patch in the current series session.",
    )
    parser.add_argument(
        "--with-comments",
        action="store_true",
        help="Skip to next patch with unresolved comments",
    )
    return parser


def add_finish_patch_parser(subparsers):
    """Add the 'finish-patch' subcommand parser."""
    parser = subparsers.add_parser(
        "finish-patch",
        help="Finish current patch and rebase the series",
        description="Rebase remaining patches on top of changes.",
    )
    parser.add_argument(
        "--stay",
        action="store_true",
        help="Stay on current patch (don't auto-advance)",
    )
    return parser


def add_abort_session_parser(subparsers):
    """Add the 'abort' subcommand parser."""
    parser = subparsers.add_parser(
        "abort",
        help="End session (default: discard changes)",
        description="End the current session. By default, returns to the "
                    "original git state. Use --keep-changes to preserve "
                    "the current git state.",
    )
    parser.add_argument(
        "--keep-changes", "-k",
        action="store_true",
        help="Keep current git state (don't restore original)",
    )
    return parser


def add_rebase_status_parser(subparsers):
    """Add the 'status' subcommand parser."""
    parser = subparsers.add_parser(
        "status",
        help="Show current session status",
        description="Display the current rebase session status.",
    )
    return parser


def add_stage_reply_parser(subparsers):
    """Add the 'stage' subcommand parser."""
    parser = subparsers.add_parser(
        "stage",
        help="Stage a comment reply (without posting)",
        description="Stage a reply to be posted later with 'push'.",
    )
    parser.add_argument(
        "thread_index",
        type=int,
        help="Thread index from 'extract' output",
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="Reply message (required unless --done or --ack)",
    )
    parser.add_argument(
        "--done", "-d",
        action="store_true",
        help="Mark as done",
    )
    parser.add_argument(
        "--ack", "-a",
        action="store_true",
        help="Acknowledge",
    )
    parser.add_argument(
        "--resolve", "-r",
        action="store_true",
        help="Mark as resolved",
    )
    parser.add_argument(
        "--url",
        help="Gerrit change URL (optional if session active)",
    )
    return parser


def add_push_parser(subparsers):
    """Add the 'push' subcommand parser."""
    parser = subparsers.add_parser(
        "push",
        help="Push all staged operations",
        description="Post all staged comment replies to Gerrit.",
    )
    parser.add_argument(
        "change_number",
        type=int,
        nargs="?",
        help="Change number (optional, pushes all if not specified)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be pushed without posting",
    )
    return parser


def add_staged_parser(subparsers, handlers):
    """Add the 'staged' command with subcommands for managing staged replies."""
    parser = subparsers.add_parser(
        "staged",
        help="Manage staged comment replies",
        description="View and manage staged comment replies.",
    )
    staged_sub = parser.add_subparsers(dest="staged_command")

    # staged list (default when no subcommand)
    list_parser = staged_sub.add_parser(
        "list",
        help="List all staged operations (default)",
        description="Show all staged comment replies.",
    )
    list_parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    list_parser.set_defaults(func=handlers['staged_list'])

    # staged show <change>
    show_parser = staged_sub.add_parser(
        "show",
        help="Show staged operations for a change",
        description="Show staged operations for a specific change.",
    )
    show_parser.add_argument("change_number", type=int, help="Change number")
    show_parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output (default: compact JSON)",
    )
    show_parser.set_defaults(func=handlers['staged_show'])

    # staged remove <change> <index>
    remove_parser = staged_sub.add_parser(
        "remove",
        help="Remove a staged operation",
        description="Remove a staged operation by index.",
    )
    remove_parser.add_argument("change_number", type=int, help="Change number")
    remove_parser.add_argument("operation_index", type=int, help="Operation index")
    remove_parser.set_defaults(func=handlers['staged_remove'])

    # staged clear <change>
    clear_parser = staged_sub.add_parser(
        "clear",
        help="Clear staged operations for a change",
        description="Remove all staged operations for a change.",
    )
    clear_parser.add_argument(
        "change_number",
        type=int,
        nargs="?",
        help="Change number (omit to clear all)",
    )
    clear_parser.set_defaults(func=handlers['staged_clear'])

    # staged refresh <change>
    refresh_parser = staged_sub.add_parser(
        "refresh",
        help="Refresh staged operations for new patchset",
        description="Update staged operations after new patchset.",
    )
    refresh_parser.add_argument("change_number", type=int, help="Change number")
    refresh_parser.set_defaults(func=handlers['staged_refresh'])

    # Set default to list when no subcommand given
    parser.set_defaults(func=handlers['staged_list'], staged_command='list')

    return parser


def add_continue_reintegration_parser(subparsers):
    """Add the 'continue-reintegration' subcommand parser."""
    parser = subparsers.add_parser(
        "continue-reintegration",
        help="Continue reintegration after resolving conflicts",
        description="Continue cherry-picking after conflict resolution.",
    )
    return parser


def add_skip_reintegration_parser(subparsers):
    """Add the 'skip-reintegration' subcommand parser."""
    parser = subparsers.add_parser(
        "skip-reintegration",
        help="Skip current change during reintegration",
        description="Skip the current conflicting change.",
    )
    return parser


def add_reviewers_parser(subparsers):
    """Add the 'reviewers' subcommand parser."""
    parser = subparsers.add_parser(
        "reviewers",
        help="List reviewers on a change",
        description="Show all reviewers and their votes on a Gerrit change.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_add_reviewer_parser(subparsers):
    """Add the 'add-reviewer' subcommand parser."""
    parser = subparsers.add_parser(
        "add-reviewer",
        help="Add a reviewer to a change (supports fuzzy name matching)",
        description="Add a reviewer to a Gerrit change. Supports fuzzy matching "
                    "on names - just provide a partial name and it will find matches.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "name",
        help="Reviewer name, email, or username (fuzzy matching supported)",
    )
    parser.add_argument(
        "--cc",
        action="store_true",
        help="Add as CC instead of reviewer",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show who would be added without actually adding",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_remove_reviewer_parser(subparsers):
    """Add the 'remove-reviewer' subcommand parser."""
    parser = subparsers.add_parser(
        "remove-reviewer",
        help="Remove a reviewer from a change",
        description="Remove a reviewer from a Gerrit change.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "name",
        help="Reviewer name, email, or username",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show who would be removed without actually removing",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_abandon_parser(subparsers):
    """Add the 'abandon' subcommand parser."""
    parser = subparsers.add_parser(
        "abandon",
        help="Abandon a Gerrit change",
        description="Abandon a Gerrit change with an optional message.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "message",
        nargs="?",
        default="",
        help="Optional message explaining why (default: none)",
    )
    parser.add_argument(
        "--message", "-m",
        dest="message_flag",
        default=None,
        help="Optional message explaining why (alternative to positional)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be abandoned without actually doing it",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_info_parser(subparsers):
    """Add the 'info' subcommand parser."""
    parser = subparsers.add_parser(
        "info",
        help="Quick overview of a change (patchsets, reviews, CI)",
        description="Show patchset upload dates, review scores, "
                    "and CI status in one shot.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "--show-bots",
        action="store_true",
        help="Include bot accounts in reviewer list (filtered by default)",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_watch_parser(subparsers):
    """Add the 'watch' subcommand parser."""
    parser = subparsers.add_parser(
        "watch",
        help="Check CI status on a list of watched patches",
        description="Read a JSON file of watched patches and run Maloo "
                    "triage on each one. The JSON file should be an array "
                    "of objects with at least a 'gerrit_url' field.",
    )
    parser.add_argument(
        "file",
        help="JSON file with watched patches (array of {gerrit_url, ...})",
    )
    return parser


def add_maloo_parser(subparsers):
    """Add the 'maloo' subcommand parser."""
    parser = subparsers.add_parser(
        "maloo",
        help="Triage Maloo test results for a change",
        description="Parse Maloo test messages and show a summary "
                    "of enforced/optional pass/fail results. "
                    "Accepts multiple URLs for batch mode.",
    )
    parser.add_argument("url", nargs="+", help="Gerrit change URL(s) or number(s)")
    parser.add_argument(
        "--patchset", "-r",
        type=int,
        default=None,
        help="Patchset number (default: latest). Only for single-URL mode.",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_checkout_parser(subparsers):
    """Add the 'checkout' subcommand parser."""
    parser = subparsers.add_parser(
        "checkout",
        aliases=["co"],
        help="Fetch and checkout a Gerrit change",
        description="Fetch a Gerrit change ref and checkout the source. "
                    "Detaches HEAD by default; use --branch to create a branch.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "--patchset", "-r",
        type=int,
        default=None,
        help="Patchset number (default: latest)",
    )
    parser.add_argument(
        "--branch", "-b",
        default=None,
        help="Create a branch with this name instead of detaching HEAD",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_find_user_parser(subparsers):
    """Add the 'find-user' subcommand parser."""
    parser = subparsers.add_parser(
        "find-user",
        help="Search for users by name (fuzzy matching)",
        description="Search for Gerrit users by name, email, or username. "
                    "Useful for finding the exact username before adding as reviewer.",
    )
    parser.add_argument(
        "query",
        help="Name, email, or username to search for",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=10,
        help="Maximum number of results (default: 10)",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_explain_parser(subparsers):
    """Add the 'explain' subcommand parser."""
    parser = subparsers.add_parser(
        "explain",
        help="Show detailed usage and examples for a command",
        description="Get detailed documentation with examples for a specific command. "
                    "This helps LLMs and users understand typical workflows and patterns.",
    )
    parser.add_argument(
        "command_name",
        help="Command to explain (e.g., 'add-reviewer', 'stage', 'review-series')",
    )
    return parser


def add_examples_parser(subparsers):
    """Add the 'examples' subcommand parser."""
    parser = subparsers.add_parser(
        "examples",
        help="Show common usage examples and workflows",
        description="Display practical examples showing typical gerrit workflows. "
                    "This helps LLMs and users quickly understand how to use the tool.",
    )
    parser.add_argument(
        "workflow",
        nargs="?",
        choices=["quick", "series", "staging", "reviewers", "all"],
        default="quick",
        help="Workflow to show examples for (default: quick)",
    )
    return parser


def add_done_parser(subparsers):
    """Add the 'done' shortcut command parser."""
    parser = subparsers.add_parser(
        "done",
        help="Mark a comment as done (shortcut for 'reply --done')",
        description="Quickly mark a comment thread as resolved with 'Done' message. "
                    "This is a shortcut for 'gc reply --done <url> <thread_index>'.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "thread_index",
        type=int,
        help="Thread index from 'comments' output",
    )
    parser.add_argument(
        "message",
        nargs="?",
        default="Done",
        help="Custom message (default: 'Done')",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_ack_parser(subparsers):
    """Add the 'ack' shortcut command parser."""
    parser = subparsers.add_parser(
        "ack",
        help="Acknowledge a comment (shortcut for 'reply --ack')",
        description="Quickly acknowledge a comment thread with 'Acknowledged' message. "
                    "This is a shortcut for 'gc reply --ack <url> <thread_index>'.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "thread_index",
        type=int,
        help="Thread index from 'comments' output",
    )
    parser.add_argument(
        "message",
        nargs="?",
        default="Acknowledged",
        help="Custom message (default: 'Acknowledged')",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_describe_parser(subparsers):
    """Add the 'describe' subcommand parser."""
    parser = subparsers.add_parser(
        "describe",
        help="Show machine-readable API description (for LLMs)",
        description="Returns a structured JSON document describing all available commands, "
                    "their arguments, types, defaults, output fields, and suggested next "
                    "actions. Use this to discover what the tool can do.",
    )
    parser.add_argument(
        "--command",
        dest="command_name",
        help="Show description for a specific command only",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_set_topic_parser(subparsers):
    """Add the 'set-topic' subcommand parser."""
    parser = subparsers.add_parser(
        "set-topic",
        help="Set the topic on a Gerrit change",
        description="Set or update the topic label on a Gerrit change. "
                    "Topics group related changes together.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument("topic", help="Topic name to set")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_restore_parser(subparsers):
    """Add the 'restore' subcommand parser."""
    parser = subparsers.add_parser(
        "restore",
        help="Restore an abandoned Gerrit change",
        description="Restore a previously abandoned change back to active status.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "-m", "--message",
        help="Optional message explaining the restore",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_rebase_parser(subparsers):
    """Add the 'rebase' subcommand parser."""
    parser = subparsers.add_parser(
        "rebase",
        help="Trigger a server-side rebase",
        description="Rebase a change on the server without checking it out locally. "
                    "Useful for keeping a series current with its parent branch.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_vote_parser(subparsers):
    """Add the 'vote' subcommand parser."""
    parser = subparsers.add_parser(
        "vote",
        aliases=["label"],
        help="Set a review label (Code-Review, Verified, etc.)",
        description="Set a review label/vote on a Gerrit change. "
                    "Example: gc vote <url> Code-Review +2",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument("label", help="Label name (e.g. Code-Review, Verified)")
    parser.add_argument(
        "score", type=int,
        help="Score value (e.g. -2, -1, 0, +1, +2)",
    )
    parser.add_argument(
        "-m", "--message",
        help="Optional review message",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_diff_parser(subparsers):
    """Add the 'diff' subcommand parser."""
    parser = subparsers.add_parser(
        "diff",
        help="Show what changed between two patchsets",
        description="Compare two patchsets of a change to see what was modified. "
                    "Useful for re-review after updates. "
                    "Example: gc diff <url> 3 5",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument("patchset_a", type=int, help="Base patchset number")
    parser.add_argument(
        "patchset_b", type=int, nargs="?",
        help="Target patchset number (default: latest)",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_message_parser(subparsers):
    """Add the 'message' subcommand parser."""
    parser = subparsers.add_parser(
        "message",
        help="Post a top-level message on a Gerrit change",
        description="Post a top-level review message (not a file comment) "
                    "on a Gerrit change. This is the equivalent of leaving "
                    "a comment in the Gerrit web UI.",
    )
    parser.add_argument("url", help="Gerrit change URL or number")
    parser.add_argument("text", help="Message text to post")
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def add_search_parser(subparsers):
    """Add the 'search' subcommand parser."""
    parser = subparsers.add_parser(
        "search",
        aliases=["s"],
        help="Search for Gerrit changes",
        description="Search Gerrit for changes matching a query. Uses the "
                    "same query syntax as the Gerrit web UI search bar. "
                    "Common operators: owner, reviewer, project, branch, "
                    "topic, status, label, message, age, is.",
    )
    parser.add_argument(
        "query",
        help="Gerrit search query (e.g. 'owner:self status:open', "
             "'project:fs/lustre-release topic:LU-12345')",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=25,
        help="Maximum number of results (default: 25)",
    )
    parser.add_argument(
        "--start", "-S",
        type=int,
        default=0,
        help="Offset for pagination (default: 0)",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    return parser


def setup_parsers(subparsers, handlers):
    """Set up all subparsers and bind them to command handlers.

    Args:
        subparsers: The argparse subparsers object
        handlers: Dict mapping command names to handler functions

    Returns:
        None - parsers are added to subparsers in-place
    """
    # Core comment commands
    add_extract_parser(subparsers).set_defaults(func=handlers['comments'])
    add_reply_parser(subparsers).set_defaults(func=handlers['reply'])
    add_batch_parser(subparsers).set_defaults(func=handlers['batch'])

    # Main entry points
    add_review_series_parser(subparsers).set_defaults(func=handlers['series'])
    add_review_parser(subparsers).set_defaults(func=handlers['review'])

    # Utility commands
    add_series_comments_parser(subparsers).set_defaults(
        func=handlers['series_comments'])
    add_series_status_parser(subparsers).set_defaults(
        func=handlers['series_status'])

    # Interactive mode
    add_interactive_parser(subparsers).set_defaults(
        func=handlers['interactive'])

    # Session workflow commands
    add_work_on_patch_parser(subparsers).set_defaults(
        func=handlers['work_on_patch'])
    add_next_patch_parser(subparsers).set_defaults(func=handlers['next_patch'])
    add_finish_patch_parser(subparsers).set_defaults(
        func=handlers['finish_patch'])
    add_abort_session_parser(subparsers).set_defaults(
        func=handlers['abort'])
    add_rebase_status_parser(subparsers).set_defaults(
        func=handlers['status'])

    # Staging commands
    add_stage_reply_parser(subparsers).set_defaults(func=handlers['stage'])
    add_push_parser(subparsers).set_defaults(func=handlers['push'])
    add_staged_parser(subparsers, handlers)  # Has its own subcommands

    # Reintegration commands
    add_continue_reintegration_parser(subparsers).set_defaults(
        func=handlers['continue_reintegration'])
    add_skip_reintegration_parser(subparsers).set_defaults(
        func=handlers['skip_reintegration'])

    # Reviewer commands
    add_reviewers_parser(subparsers).set_defaults(func=handlers['reviewers'])
    add_add_reviewer_parser(subparsers).set_defaults(func=handlers['add_reviewer'])
    add_remove_reviewer_parser(subparsers).set_defaults(
        func=handlers['remove_reviewer'])
    add_find_user_parser(subparsers).set_defaults(func=handlers['find_user'])
    add_abandon_parser(subparsers).set_defaults(func=handlers['abandon'])
    add_checkout_parser(subparsers).set_defaults(func=handlers['checkout'])

    # Test result triage
    add_maloo_parser(subparsers).set_defaults(func=handlers['maloo'])
    add_watch_parser(subparsers).set_defaults(func=handlers['watch'])

    # Change overview
    add_info_parser(subparsers).set_defaults(func=handlers['info'])

    # Search
    add_search_parser(subparsers).set_defaults(func=handlers['search'])

    # Change management
    add_set_topic_parser(subparsers).set_defaults(
        func=handlers['set_topic'])
    add_restore_parser(subparsers).set_defaults(func=handlers['restore'])
    add_rebase_parser(subparsers).set_defaults(func=handlers['rebase'])

    # Review operations
    add_vote_parser(subparsers).set_defaults(func=handlers['vote'])
    add_diff_parser(subparsers).set_defaults(func=handlers['diff'])

    # Top-level messaging
    add_message_parser(subparsers).set_defaults(func=handlers['message'])

    # Help/documentation commands
    add_explain_parser(subparsers).set_defaults(func=handlers['explain'])
    add_examples_parser(subparsers).set_defaults(func=handlers['examples'])

    # Shortcut commands
    add_done_parser(subparsers).set_defaults(func=handlers['done'])
    add_ack_parser(subparsers).set_defaults(func=handlers['ack'])

    # Self-description (LLM discoverability)
    add_describe_parser(subparsers).set_defaults(func=handlers['describe'])
