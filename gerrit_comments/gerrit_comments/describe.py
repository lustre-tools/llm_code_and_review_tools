"""Machine-readable API description for the Gerrit comments tool.

Provides the full command surface so LLMs can discover capabilities
without parsing --help text.
"""

from llm_tool_common import Argument, Command, ToolDescription


def get_tool_description() -> ToolDescription:
    """Return the complete gerrit-comments tool API description."""
    return ToolDescription(
        name="gerrit-comments",
        version="0.1.0",
        description=(
            "Gerrit code review tool for LLM agents. "
            "Extract comments, reply, review diffs, and manage patch series. "
            "Use 'gc' as a short alias for 'gerrit-comments'."
        ),
        env_vars=[
            {"name": "GERRIT_URL", "description": "Gerrit server URL", "required": "true"},
            {"name": "GERRIT_USER", "description": "Gerrit username", "required": "true"},
            {"name": "GERRIT_PASS", "description": "Gerrit HTTP password", "required": "true"},
        ],
        commands=[
            # --- Core comment commands ---
            Command(
                name="comments",
                description="Get unresolved comments from a Gerrit change with code context",
                usage="gc comments <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL or number", required=True),
                    Argument(name="--all", description="Include resolved comments too", type="boolean", default=False),
                    Argument(name="--no-context", description="Skip code context around comments", type="boolean", default=False),
                    Argument(name="--context-lines", description="Lines of code context", type="integer", default=3),
                    Argument(name="--summary", description="Truncate code context to N lines", type="integer"),
                    Argument(name="--fields", description="Comma-separated fields per thread: index,file,line,message,author,resolved,patch_set,code_context"),
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
                usage='gc reply <THREAD_INDEX> "<MESSAGE>" [--url URL]',
                arguments=[
                    Argument(name="thread_index", description="Thread index from 'comments' output", type="integer", required=True),
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
                ],
                output_fields=["change_number", "thread_index", "message", "resolved"],
                next_actions=["comments (to verify)", "reply (next thread)"],
            ),
            Command(
                name="batch",
                description="Reply to multiple comments from a JSON file",
                usage="gc batch <URL> <FILE>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL or number", required=True),
                    Argument(name="file", description="JSON file with replies [{thread_index, message, mark_resolved}]", required=True),
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
                    Argument(name="url", description="Gerrit change URL or number", required=True),
                    Argument(name="--changes-only", description="Show only changed lines", type="boolean", default=False),
                    Argument(name="--full-content", description="Include full file content", type="boolean", default=False),
                    Argument(name="--unified", description="Lines of diff context", type="integer", default=3),
                    Argument(name="--base", description="Base patchset for comparison", type="integer"),
                    Argument(name="--post-comments", description="Post review comments from JSON file"),
                    Argument(name="--vote", description="Code-Review vote (-2 to +2)", type="integer", choices=["-2", "-1", "0", "1", "2"]),
                    Argument(name="--message", description="Review message"),
                    Argument(name="--summary", description="Truncate diffs to N lines per hunk", type="integer"),
                    Argument(name="--dry-run", description="Preview without posting", type="boolean", default=False),
                ],
                examples=[
                    "gc review 12345",
                    "gc review 12345 --summary 10",
                    "gc review 12345 --post-comments comments.json --vote 1",
                ],
                output_fields=["change_number", "files[].path", "files[].diff", "files[].insertions", "files[].deletions"],
                next_actions=["comments", "review-series"],
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
                    Argument(name="--no-prompt", description="Skip AI review prompt", type="boolean", default=False),
                    Argument(name="--checkout", description="Checkout first patch with comments", type="boolean", default=False),
                ],
                examples=[
                    "gc review-series https://review.example.com/c/project/+/12345",
                    "gc review-series 12345 --checkout",
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
                usage="gc work-on-patch <CHANGE_NUMBER> [URL]",
                arguments=[
                    Argument(name="change_number", description="Change number to work on", type="integer", required=True),
                    Argument(name="url", description="Gerrit URL (optional if session active)"),
                ],
                examples=["gc work-on-patch 12345"],
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
                description="Post all staged comment replies to Gerrit",
                usage="gc push [CHANGE_NUMBER]",
                arguments=[
                    Argument(name="change_number", description="Change number (omit to push all)", type="integer"),
                    Argument(name="--dry-run", description="Preview without posting", type="boolean", default=False),
                ],
                examples=["gc push", "gc push 12345", "gc push --dry-run"],
                next_actions=["comments (to verify)"],
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
                description="List reviewers and their votes on a change",
                usage="gc reviewers <URL>",
                arguments=[
                    Argument(name="url", description="Gerrit change URL or number", required=True),
                ],
                examples=["gc reviewers 12345"],
                output_fields=["reviewers[].name", "reviewers[].username", "reviewers[].votes"],
                next_actions=["add-reviewer", "remove-reviewer"],
            ),
            Command(
                name="add-reviewer",
                description="Add a reviewer (supports fuzzy name matching)",
                usage='gc add-reviewer <URL> "<NAME>"',
                arguments=[
                    Argument(name="url", description="Gerrit change URL or number", required=True),
                    Argument(name="name", description="Name, email, or username (fuzzy matching supported)", required=True),
                    Argument(name="--cc", description="Add as CC instead of reviewer", type="boolean", default=False),
                    Argument(name="--dry-run", description="Preview without adding", type="boolean", default=False),
                ],
                examples=[
                    'gc add-reviewer 12345 "John Smith"',
                    "gc add-reviewer 12345 jsmith",
                    'gc add-reviewer 12345 "john" --cc',
                ],
                next_actions=["reviewers"],
            ),
            Command(
                name="remove-reviewer",
                description="Remove a reviewer from a change",
                usage='gc remove-reviewer <URL> "<NAME>"',
                arguments=[
                    Argument(name="url", description="Gerrit change URL or number", required=True),
                    Argument(name="name", description="Name, email, or username", required=True),
                    Argument(name="--dry-run", description="Preview without removing", type="boolean", default=False),
                ],
                examples=['gc remove-reviewer 12345 "simmons"'],
                next_actions=["reviewers"],
            ),
            Command(
                name="find-user",
                description="Search for Gerrit users by name, email, or username",
                usage='gc find-user "<QUERY>"',
                arguments=[
                    Argument(name="query", description="Search query", required=True),
                    Argument(name="--limit", description="Max results", type="integer", default=10),
                ],
                examples=['gc find-user "farrell"', 'gc find-user "john" --limit 5'],
                output_fields=["users[].username", "users[].name", "users[].email"],
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
