"""Meta/documentation commands: search, explain, examples, describe."""

import sys

from ..errors import ErrorCode, ExitCode
from ._helpers import _cli, output_error, output_success


# Command explanations with detailed usage and examples
COMMAND_EXPLANATIONS = {
    "comments": {
        "summary": "Get unresolved comments from a Gerrit change",
        "description": """
The 'comments' command extracts comment threads from a Gerrit change URL.
By default, it only shows unresolved comments. Use --all to include resolved ones.

Each comment thread is assigned an index (0, 1, 2, ...) that you can use with
other commands like 'reply' or 'stage'.
""",
        "examples": [
            {
                "command": "gc comments https://review.example.com/c/project/+/12345",
                "description": "Get all unresolved comments (JSON output)",
            },
            {
                "command": "gc comments --pretty https://review.example.com/c/project/+/12345",
                "description": "Get comments with human-readable JSON",
            },
            {
                "command": "gc comments --all https://review.example.com/c/project/+/12345",
                "description": "Include resolved comments",
            },
            {
                "command": "gc comments --no-context https://review.example.com/c/project/+/12345",
                "description": "Skip code context around comments",
            },
        ],
        "related": ["reply", "stage", "series-comments"],
    },
    "reply": {
        "summary": "Reply to a comment thread",
        "description": """
The 'reply' command posts a reply to a specific comment thread. You identify
the thread by its index from the 'comments' output.

URL is optional - if you recently ran 'gc comments URL', the URL is remembered
and reused automatically. Use --url to override.

Common patterns:
- Use --done to mark a comment as addressed (adds "Done" and resolves)
- Use --ack to acknowledge without action (adds "Acknowledged" and resolves)
- Use --resolve with a custom message to resolve the thread
""",
        "examples": [
            {
                "command": "gc reply 0 \"Fixed in the latest patchset\"",
                "description": "Reply to thread 0 (uses last URL from 'gc comments')",
            },
            {
                "command": "gc reply 0 --done",
                "description": "Mark thread 0 as done (resolved)",
            },
            {
                "command": "gc reply 2 --ack",
                "description": "Acknowledge thread 2",
            },
            {
                "command": "gc reply 1 \"Will fix\" --resolve",
                "description": "Reply and resolve with custom message",
            },
            {
                "command": "gc reply 0 --done --url URL",
                "description": "Explicit URL (overrides remembered URL)",
            },
        ],
        "related": ["comments", "stage", "batch"],
    },
    "stage": {
        "summary": "Stage a comment reply for later posting",
        "description": """
The 'stage' command queues a reply without immediately posting it. This is
useful when addressing multiple comments - you can stage all replies and
then post them together with 'push'.

If you're in an active session (from review-series), the URL is optional.
""",
        "examples": [
            {
                "command": "gc stage 0 \"Fixed\"",
                "description": "Stage a reply to thread 0 (uses session URL)",
            },
            {
                "command": "gc stage --done 1",
                "description": "Stage thread 1 as done",
            },
            {
                "command": "gc stage --url URL 2 \"Will address later\"",
                "description": "Stage with explicit URL",
            },
        ],
        "related": ["push", "staged", "reply"],
    },
    "push": {
        "summary": "Post all staged comment replies",
        "description": """
The 'push' command posts all staged replies to Gerrit. Use --dry-run to
preview what would be posted without actually sending.
""",
        "examples": [
            {
                "command": "gc push 12345",
                "description": "Push staged replies for change 12345",
            },
            {
                "command": "gc push --dry-run 12345",
                "description": "Preview what would be pushed",
            },
            {
                "command": "gc push",
                "description": "Push all staged replies for all changes",
            },
        ],
        "related": ["stage", "staged"],
    },
    "staged": {
        "summary": "Manage staged comment replies",
        "description": """
The 'staged' command group helps you view and manage queued replies.

Subcommands:
- list: Show all staged operations
- show <change>: Show staged ops for a specific change
- remove <change> <index>: Remove a specific staged reply
- clear [change]: Clear staged replies (all or for one change)
- refresh <change>: Update patchset number after amending
""",
        "examples": [
            {
                "command": "gc staged list",
                "description": "List all staged operations",
            },
            {
                "command": "gc staged show 12345",
                "description": "Show staged ops for change 12345",
            },
            {
                "command": "gc staged remove 12345 0",
                "description": "Remove first staged op for change 12345",
            },
            {
                "command": "gc staged clear",
                "description": "Clear all staged operations",
            },
            {
                "command": "gc staged refresh 12345",
                "description": "Update patchset after amending",
            },
        ],
        "related": ["stage", "push"],
    },
    "review-series": {
        "summary": "Start reviewing a patch series",
        "description": """
The 'review-series' command is the main entry point for AI-assisted patch
series review. It finds all related patches, shows comment counts, and
optionally checks out the first patch with comments.

This command:
1. Finds all patches in the series (following relations)
2. Counts unresolved comments on each patch
3. Checks out the first patch with comments (unless --no-checkout)
4. Starts a session for tracking your progress
""",
        "examples": [
            {
                "command": "gc review-series https://review.example.com/c/project/+/12345",
                "description": "Start reviewing - checkout first patch with comments",
            },
            {
                "command": "gc review-series --no-checkout URL",
                "description": "Just show series info without checkout",
            },
            {
                "command": "gc review-series --urls-only URL",
                "description": "List patch URLs only (plain text)",
            },
            {
                "command": "gc review-series --numbers-only URL",
                "description": "List change numbers only",
            },
        ],
        "related": ["work-on-patch", "finish-patch", "status"],
    },
    "work-on-patch": {
        "summary": "Start working on a specific patch",
        "description": """
Checkout a specific patch and show its comments. Accepts a change number
or a full Gerrit URL. When given a change number, the URL is derived from
the GERRIT_URL environment variable. Fetches from Gerrit automatically if
the patch is not already in the local git history.
""",
        "examples": [
            {
                "command": "gc work-on-patch 12345",
                "description": "Checkout change 12345 (derives URL from GERRIT_URL)",
            },
            {
                "command": "gc work-on-patch https://review.whamcloud.com/12345",
                "description": "Checkout change 12345 using full URL",
            },
        ],
        "related": ["review-series", "next-patch", "finish-patch"],
    },
    "finish-patch": {
        "summary": "Finish current patch and rebase the series",
        "description": """
After making changes and staging replies, use 'finish-patch' to:
1. Rebase all dependent patches on your changes
2. Auto-advance to the next patch with comments (unless --stay)

Always commit your changes before running this command.
""",
        "examples": [
            {
                "command": "gc finish-patch",
                "description": "Finish patch, rebase series, advance to next",
            },
            {
                "command": "gc finish-patch --stay",
                "description": "Finish and rebase, but stay on current patch",
            },
        ],
        "related": ["work-on-patch", "next-patch", "abort"],
    },
    "next-patch": {
        "summary": "Move to the next patch in the series",
        "description": """
Skip to the next patch without rebasing. Use this when you don't have
changes on the current patch or want to review without modifying.
""",
        "examples": [
            {
                "command": "gc next-patch",
                "description": "Move to the next patch",
            },
            {
                "command": "gc next-patch --with-comments",
                "description": "Skip to next patch that has comments",
            },
        ],
        "related": ["work-on-patch", "finish-patch", "status"],
    },
    "status": {
        "summary": "Show current session status",
        "description": """
Display information about the active session: which patch you're on,
remaining patches, staged replies, etc.
""",
        "examples": [
            {
                "command": "gc status",
                "description": "Show current session status",
            },
        ],
        "related": ["review-series", "work-on-patch"],
    },
    "abort": {
        "summary": "End the current session",
        "description": """
End the session and optionally discard changes. By default, this restores
the original git state. Use --keep-changes to preserve your work.
""",
        "examples": [
            {
                "command": "gc abort",
                "description": "End session and discard changes",
            },
            {
                "command": "gc abort --keep-changes",
                "description": "End session but keep git state",
            },
        ],
        "related": ["status", "review-series"],
    },
    "review": {
        "summary": "Get code changes for review",
        "description": """
Get the diff and file changes from a Gerrit change for code review.
Can also post review comments from a JSON file.
""",
        "examples": [
            {
                "command": "gc review URL",
                "description": "Get changes for review (JSON)",
            },
            {
                "command": "gc review --pretty URL",
                "description": "Get changes with readable JSON",
            },
            {
                "command": "gc review --full-content URL",
                "description": "Include full file contents",
            },
            {
                "command": "gc review --post-comments review.json URL",
                "description": "Post review comments from file",
            },
        ],
        "related": ["comments", "review-series"],
    },
    "series-comments": {
        "summary": "Get comments from all patches in a series",
        "description": """
Extract comments from every patch in a series in one call. Useful for
getting an overview of all feedback across the entire series.
""",
        "examples": [
            {
                "command": "gc series-comments URL",
                "description": "Get all unresolved comments in series",
            },
            {
                "command": "gc series-comments --all URL",
                "description": "Include resolved comments",
            },
            {
                "command": "gc series-comments --pretty URL",
                "description": "Human-readable output",
            },
        ],
        "related": ["comments", "review-series", "series-status"],
    },
    "series-status": {
        "summary": "Show status dashboard for a patch series",
        "description": """
Display a summary of all patches in a series: their status, comment counts,
review votes, and other metadata.
""",
        "examples": [
            {
                "command": "gc series-status URL",
                "description": "Show series status dashboard",
            },
        ],
        "related": ["review-series", "series-comments"],
    },
    "add-reviewer": {
        "summary": "Add a reviewer to a change",
        "description": """
Add a reviewer or CC to a Gerrit change. Supports fuzzy name matching -
just provide a partial name and it will find matches.

If multiple users match, you'll be shown the options and asked to be
more specific (use email or username for exact match).
""",
        "examples": [
            {
                "command": "gc add-reviewer URL \"John Smith\"",
                "description": "Add John Smith as reviewer (fuzzy match)",
            },
            {
                "command": "gc add-reviewer URL john@example.com",
                "description": "Add by email (exact match)",
            },
            {
                "command": "gc add-reviewer --cc URL jsmith",
                "description": "Add as CC instead of reviewer",
            },
        ],
        "related": ["remove-reviewer", "reviewers", "find-user"],
    },
    "remove-reviewer": {
        "summary": "Remove a reviewer from a change",
        "description": """
Remove a reviewer from a Gerrit change. Matches against current reviewers
by name, email, or username.
""",
        "examples": [
            {
                "command": "gc remove-reviewer URL \"John Smith\"",
                "description": "Remove John Smith from reviewers",
            },
            {
                "command": "gc remove-reviewer URL jsmith",
                "description": "Remove by username",
            },
        ],
        "related": ["add-reviewer", "reviewers"],
    },
    "reviewers": {
        "summary": "List reviewers on a change",
        "description": """
Show all reviewers and their votes on a Gerrit change.
""",
        "examples": [
            {
                "command": "gc reviewers URL",
                "description": "List all reviewers and their votes",
            },
            {
                "command": "gc reviewers --pretty URL",
                "description": "Human-readable output",
            },
        ],
        "related": ["add-reviewer", "remove-reviewer"],
    },
    "find-user": {
        "summary": "Search for users by name",
        "description": """
Search for Gerrit users by name, email, or username. Useful for finding
the exact username before adding as a reviewer.
""",
        "examples": [
            {
                "command": "gc find-user \"John\"",
                "description": "Search for users named John",
            },
            {
                "command": "gc find-user --limit 20 \"smith\"",
                "description": "Get up to 20 results matching smith",
            },
        ],
        "related": ["add-reviewer", "reviewers"],
    },
    "batch": {
        "summary": "Reply to multiple comments from a JSON file",
        "description": """
Post multiple replies at once from a JSON file. The file should contain
an array of objects with thread_index, message, and optionally mark_resolved.
""",
        "examples": [
            {
                "command": "gc batch URL replies.json",
                "description": "Post all replies from replies.json",
            },
        ],
        "related": ["reply", "stage", "push"],
    },
    "interactive": {
        "summary": "Interactive mode for reviewing comments",
        "description": """
Review and reply to comments in an interactive terminal interface.
""",
        "examples": [
            {
                "command": "gc interactive URL",
                "description": "Start interactive review mode",
            },
            {
                "command": "gc i URL",
                "description": "Short alias for interactive",
            },
        ],
        "related": ["comments", "review-series"],
    },
    "continue-reintegration": {
        "summary": "Continue reintegration after resolving conflicts",
        "description": """
After resolving merge conflicts during reintegration, use this command
to continue the cherry-pick process.
""",
        "examples": [
            {
                "command": "gc continue-reintegration",
                "description": "Continue after resolving conflicts",
            },
        ],
        "related": ["skip-reintegration", "finish-patch"],
    },
    "skip-reintegration": {
        "summary": "Skip current change during reintegration",
        "description": """
Skip a conflicting change during reintegration and move to the next one.
""",
        "examples": [
            {
                "command": "gc skip-reintegration",
                "description": "Skip current conflicting change",
            },
        ],
        "related": ["continue-reintegration", "finish-patch"],
    },
}

# Aliases for command lookup
COMMAND_ALIASES = {
    "extract": "comments",
    "i": "interactive",
}


# Workflow examples for the 'examples' command
WORKFLOW_EXAMPLES = {
    "quick": {
        "title": "Quick Start - Single Change Review",
        "description": "The fastest way to review and reply to comments on a single change.",
        "examples": [
            ("gc comments URL", "Get unresolved comments (remembers URL)"),
            ("gc reply 0 --done", "Mark comment 0 as done (uses remembered URL)"),
            ("gc reply 1 \"Fixed in latest PS\"", "Reply to comment 1"),
        ],
    },
    "staging": {
        "title": "Staging Workflow - Batch Multiple Replies",
        "description": "Stage multiple replies locally, review them, then push all at once.",
        "examples": [
            ("gc comments URL", "Get comments to address"),
            ("gc stage --done 0", "Stage 'Done' for thread 0"),
            ("gc stage 1 \"Will fix in follow-up\"", "Stage reply for thread 1"),
            ("gc stage --ack 2", "Stage acknowledgment for thread 2"),
            ("gc staged list", "Review all staged replies"),
            ("gc push --dry-run CHANGE_ID", "Preview what will be posted"),
            ("gc push CHANGE_ID", "Post all staged replies"),
        ],
    },
    "series": {
        "title": "Series Workflow - Multi-Patch Review Session",
        "description": "Review a series of related patches interactively with rebase support.",
        "examples": [
            ("gc review-series URL", "Start session for a patch series"),
            ("gc status", "Check current session state"),
            ("gc comments", "Get comments for current patch"),
            ("gc stage --done 0", "Stage reply"),
            ("gc finish-patch", "Complete current patch, move to next"),
            ("gc push CHANGE_ID", "Push staged replies for a change"),
            ("gc abort", "Exit session without finishing"),
        ],
    },
    "reviewers": {
        "title": "Reviewer Management",
        "description": "Add, remove, and find reviewers on changes.",
        "examples": [
            ("gc reviewers URL", "List current reviewers"),
            ("gc find-user john", "Search for users by name"),
            ("gc add-reviewer URL username", "Add a reviewer"),
            ("gc add-reviewer --cc URL username", "Add as CC only"),
            ("gc remove-reviewer URL username", "Remove a reviewer"),
        ],
    },
}


def cmd_search(args):
    """Search Gerrit for changes matching a query."""
    cli = _cli()
    command = "search"
    pretty = getattr(args, 'pretty', False)

    try:
        client = cli.GerritCommentsClient()
        results = client.search_changes(
            query=args.query,
            limit=args.limit,
            start=args.start,
        )

        changes = []
        for change in results:
            owner = change.get("owner", {})
            entry = {
                "number": change.get("_number"),
                "subject": change.get("subject", ""),
                "project": change.get("project", ""),
                "branch": change.get("branch", ""),
                "status": change.get("status", ""),
                "owner": owner.get("name", owner.get("email", "")),
                "updated": change.get("updated", ""),
                "url": f"{client.rest.url}/#/c/{change.get('_number')}/",
            }
            if change.get("topic"):
                entry["topic"] = change["topic"]
            insertions = change.get("insertions", 0)
            deletions = change.get("deletions", 0)
            if insertions or deletions:
                entry["size"] = f"+{insertions}/-{deletions}"
            changes.append(entry)

        data = {
            "query": args.query,
            "count": len(changes),
            "changes": changes,
        }
        if len(results) == args.limit:
            data["more_results"] = True
            data["next_start"] = args.start + args.limit

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_explain(args):
    """Show detailed usage for a specific command."""
    command_name = args.command_name.lower().replace("_", "-")

    # Resolve aliases
    command_name = COMMAND_ALIASES.get(command_name, command_name)

    if command_name not in COMMAND_EXPLANATIONS:
        available = sorted(COMMAND_EXPLANATIONS.keys())
        print(f"Unknown command: {command_name}", file=sys.stderr)
        print(f"\nAvailable commands:", file=sys.stderr)
        for cmd in available:
            info = COMMAND_EXPLANATIONS[cmd]
            print(f"  {cmd:20} {info['summary']}", file=sys.stderr)
        sys.exit(1)

    info = COMMAND_EXPLANATIONS[command_name]

    # Format output
    output = []
    output.append(f"gc {command_name} - {info['summary']}")
    output.append("=" * 60)
    output.append("")
    output.append("DESCRIPTION:")
    output.append(info['description'].strip())
    output.append("")
    output.append("EXAMPLES:")
    for ex in info['examples']:
        output.append(f"  $ {ex['command']}")
        output.append(f"    {ex['description']}")
        output.append("")

    if info.get('related'):
        output.append("RELATED COMMANDS:")
        output.append(f"  {', '.join(info['related'])}")
        output.append("")

    output.append(f"For full argument list, use: gc {command_name} --help")

    print("\n".join(output))


def cmd_examples(args):
    """Show common usage examples and workflows."""
    workflow = getattr(args, 'workflow', 'quick') or 'quick'

    if workflow == "all":
        # Show all workflows
        workflows_to_show = ["quick", "staging", "series", "reviewers"]
    else:
        workflows_to_show = [workflow]

    output = []
    output.append("=" * 60)
    output.append("GERRIT-COMMENTS EXAMPLES")
    output.append("=" * 60)
    output.append("")

    for wf_name in workflows_to_show:
        wf = WORKFLOW_EXAMPLES[wf_name]
        output.append(f"## {wf['title']}")
        output.append("")
        output.append(wf['description'])
        output.append("")

        for cmd, desc in wf['examples']:
            output.append(f"  $ {cmd}")
            output.append(f"    # {desc}")
        output.append("")

    output.append("-" * 60)
    output.append("Tips:")
    output.append("  - Use 'gc explain <command>' for detailed help on any command")
    output.append("  - URL can be a full Gerrit URL or just a change number")
    output.append("  - Most commands support --pretty for readable JSON output")
    output.append("")
    output.append("Workflows: quick, staging, series, reviewers, all")
    output.append("  $ gc examples staging    # Show staging workflow")
    output.append("  $ gc examples all        # Show all workflows")

    print("\n".join(output))


def cmd_describe(args):
    """Show machine-readable API description."""
    from ..describe import get_tool_description

    pretty = getattr(args, 'pretty', False)
    command_name = getattr(args, 'command_name', None)
    tool_desc = get_tool_description()

    if command_name:
        normalized = command_name.replace(".", " ")
        matching = [c for c in tool_desc.commands if c.name == normalized]
        if not matching:
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                f"Unknown command: {command_name}",
                "describe",
                pretty,
            ))
        data = matching[0].to_dict()
    else:
        data = tool_desc.to_dict()

    output_success(data, "describe", pretty)
    sys.exit(ExitCode.SUCCESS)
