"""Machine-readable API description for the JIRA tool.

Provides the full command surface so LLMs can discover capabilities
without parsing --help text.
"""

from llm_tool_common import Argument, Command, ToolDescription


def get_tool_description() -> ToolDescription:
    """Return the complete JIRA tool API description."""
    return ToolDescription(
        name="jira",
        version="0.1.0",
        description="JIRA CLI tool for LLM agents. Provides issue tracking operations with structured JSON output.",
        env_vars=[
            {"name": "JIRA_SERVER", "description": "JIRA server URL", "required": "true"},
            {"name": "JIRA_TOKEN", "description": "JIRA API token", "required": "true"},
        ],
        commands=[
            Command(
                name="issue get",
                description="Get issue details by key or URL",
                usage="jira issue get <KEY>",
                arguments=[
                    Argument(name="key", description="Issue key (e.g., PROJ-123) or JIRA URL", required=True),
                    Argument(name="--fields", description="Comma-separated list of fields to return"),
                    Argument(name="--output", description="Output only this field as plain text (no JSON envelope)"),
                ],
                examples=[
                    "jira issue get PROJ-123",
                    "jira issue get https://jira.example.com/browse/PROJ-123",
                    "jira issue get PROJ-123 --output status",
                ],
                output_fields=["key", "id", "summary", "description", "status", "priority", "issue_type", "project", "assignee", "reporter", "resolution", "created", "updated", "labels"],
                next_actions=["issue comments", "issue transitions", "issue attachments", "issue links"],
            ),
            Command(
                name="issue comments",
                description="Get comments for an issue with pagination",
                usage="jira issue comments <KEY> [--limit N] [--offset N]",
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                    Argument(name="--limit", description="Maximum comments to return", type="integer", default=5),
                    Argument(name="--offset", description="Skip first N comments", type="integer", default=0),
                    Argument(name="--all", description="Fetch all comments", type="boolean", default=False),
                    Argument(name="--summary-only", description="Only return comment metadata, not full content", type="boolean", default=False),
                    Argument(name="--newest-first", description="Sort newest first instead of oldest", type="boolean", default=False),
                ],
                examples=[
                    "jira issue comments PROJ-123",
                    "jira issue comments PROJ-123 --limit 10 --newest-first",
                    "jira issue comments PROJ-123 --summary-only",
                ],
                output_fields=["issue_key", "total_comments", "comments[].id", "comments[].body", "comments[].author", "comments[].created", "pagination"],
                next_actions=["issue comment (to add)", "issue get"],
            ),
            Command(
                name="issue search",
                description="Search issues using JQL",
                usage='jira issue search "<JQL>" [--limit N]',
                arguments=[
                    Argument(name="jql", description="JIRA Query Language query string", required=True),
                    Argument(name="--limit", description="Maximum results", type="integer", default=20),
                    Argument(name="--offset", description="Skip first N results", type="integer", default=0),
                    Argument(name="--fields", description="Comma-separated fields to return"),
                    Argument(name="--output", description="Output only this field from each issue (one per line)"),
                ],
                examples=[
                    'jira issue search "project = PROJ AND status = Open"',
                    'jira issue search "assignee = currentUser()" --limit 5',
                    'jira issue search "project = PROJ" --output key',
                ],
                output_fields=["jql", "issues[]", "pagination"],
                next_actions=["issue get (with a specific key from results)"],
            ),
            Command(
                name="issue comment",
                description="Add a comment to an issue",
                usage='jira issue comment <KEY> "<BODY>"',
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                    Argument(name="body", description="Comment text", required=True),
                ],
                examples=['jira issue comment PROJ-123 "Fixed in commit abc123"'],
                output_fields=["issue_key", "comment.id", "comment.body", "comment.author", "comment.created"],
                next_actions=["issue get", "issue transition"],
            ),
            Command(
                name="issue transitions",
                description="List available status transitions for an issue",
                usage="jira issue transitions <KEY>",
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                ],
                examples=["jira issue transitions PROJ-123"],
                output_fields=["issue_key", "transitions[].id", "transitions[].name", "transitions[].to_status"],
                next_actions=["issue transition (with a transition ID from results)"],
            ),
            Command(
                name="issue transition",
                description="Transition an issue to a new status",
                usage="jira issue transition <KEY> <TRANSITION_ID>",
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                    Argument(name="transition_id", description="Transition ID (from 'transitions' command)", required=True),
                    Argument(name="--comment", description="Add a comment with the transition"),
                ],
                examples=[
                    "jira issue transition PROJ-123 31",
                    'jira issue transition PROJ-123 31 --comment "Starting work"',
                ],
                output_fields=["issue_key", "transition_id", "status_before", "status_after", "comment_added"],
                next_actions=["issue get"],
            ),
            Command(
                name="issue create",
                description="Create a new issue",
                usage="jira issue create --project <KEY> --type <TYPE> --summary <TEXT>",
                arguments=[
                    Argument(name="--project", description="Project key (e.g., PROJ)", required=True),
                    Argument(name="--type", description="Issue type (e.g., Bug, Task)", required=True),
                    Argument(name="--summary", description="Issue summary", required=True),
                    Argument(name="--description", description="Issue description"),
                ],
                examples=[
                    'jira issue create --project PROJ --type Bug --summary "Login fails"',
                ],
                output_fields=["key", "id", "self"],
                next_actions=["issue get", "issue comment", "issue transition"],
            ),
            Command(
                name="issue update",
                description="Update fields on an existing issue",
                usage="jira issue update <KEY> [--summary TEXT] [--assignee USER] ...",
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                    Argument(name="--summary", description="New summary"),
                    Argument(name="--description", description="New description"),
                    Argument(name="--assignee", description="New assignee (empty string to unassign)"),
                    Argument(name="--priority", description="New priority name"),
                    Argument(name="--labels", description="Comma-separated labels (replaces existing)"),
                ],
                examples=["jira issue update PROJ-123 --assignee jdoe --priority High"],
                output_fields=["issue_key", "updated_fields"],
                next_actions=["issue get"],
            ),
            Command(
                name="issue attachments",
                description="List attachments on an issue",
                usage="jira issue attachments <KEY>",
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                ],
                examples=["jira issue attachments PROJ-123"],
                output_fields=["issue_key", "total", "attachments[].id", "attachments[].filename", "attachments[].size", "attachments[].mime_type"],
                next_actions=["attachment content (with an attachment ID from results)"],
            ),
            Command(
                name="issue links",
                description="List issue links (relationships to other issues)",
                usage="jira issue links <KEY>",
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                ],
                examples=["jira issue links PROJ-123"],
                output_fields=["issue_key", "total", "links[].direction", "links[].relationship", "links[].issue_key", "links[].issue_summary"],
                next_actions=["issue get (with a linked issue key)"],
            ),
            Command(
                name="attachment content",
                description="Download attachment content (text files only by default)",
                usage="jira attachment content <ID> [--max-size BYTES]",
                arguments=[
                    Argument(name="attachment_id", description="Numeric attachment ID", required=True),
                    Argument(name="--max-size", description="Maximum size in bytes", type="integer", default=102400),
                    Argument(name="--encoding", description="Text encoding", default="utf-8"),
                    Argument(name="--raw", description="Output raw content (no JSON envelope)", type="boolean", default=False),
                ],
                examples=[
                    "jira attachment content 12345",
                    "jira attachment content 12345 --max-size 1048576",
                ],
                output_fields=["attachment", "size_bytes", "is_text", "content"],
            ),
            Command(
                name="attachment upload",
                description="Upload a file as an attachment to an issue",
                usage="jira attachment upload <KEY> <FILE_PATH>",
                arguments=[
                    Argument(name="key", description="Issue key or JIRA URL", required=True),
                    Argument(name="file_path", description="Path to file to upload", required=True),
                    Argument(name="--filename", description="Override filename"),
                ],
                examples=["jira attachment upload PROJ-123 /tmp/screenshot.png"],
                output_fields=["issue_key", "uploaded", "attachments"],
            ),
            Command(
                name="config test",
                description="Test connectivity to JIRA server",
                usage="jira config test",
                arguments=[],
                examples=["jira config test"],
                output_fields=["connected", "server_title", "version", "base_url"],
            ),
            Command(
                name="config show",
                description="Show current configuration (token redacted)",
                usage="jira config show",
                arguments=[],
                examples=["jira config show"],
                output_fields=["server", "token", "config_path"],
            ),
            Command(
                name="describe",
                description="Show this machine-readable API description",
                usage="jira describe",
                arguments=[
                    Argument(name="--command", description="Show description for a specific command only"),
                ],
                examples=["jira describe", "jira describe --command issue.get"],
            ),
        ],
    )
