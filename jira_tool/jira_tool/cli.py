"""CLI entry point for JIRA tool."""

import re
import sys
from typing import Any
from urllib.parse import urlparse

import click

from .client import JiraClient
from .config import DEFAULT_CONFIG_PATH, JiraConfig, create_sample_config, load_config
from .describe import get_tool_description
from .envelope import error_response, error_response_from_dict, format_json, success_response
from .errors import ConfigError, ErrorCode, ExitCode, JiraToolError

# Global options for all commands
pass_config = click.make_pass_decorator(JiraConfig, ensure=True)


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout."""
    click.echo(format_json(envelope, pretty=pretty))


def extract_field(data: dict[str, Any], field_path: str) -> Any:
    """
    Extract a field from data using dot notation.

    Args:
        data: Data dictionary
        field_path: Field path (e.g., "key", "status", "assignee.name")

    Returns:
        Field value or None if not found
    """
    parts = field_path.split(".")
    value = data
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def output_field(data: dict[str, Any], field_path: str) -> None:
    """Output a single field value to stdout (plain text, no JSON)."""
    value = extract_field(data, field_path)
    if value is not None:
        click.echo(str(value))


# Pattern for JIRA issue keys: PROJECT-123
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def extract_issue_key(key_or_url: str) -> str:
    """
    Extract issue key from a key or URL.

    Accepts:
        - Bare key: "PROJ-123" -> "PROJ-123"
        - Browse URL: "https://jira.example.com/browse/PROJ-123" -> "PROJ-123"
        - REST URL: "https://jira.example.com/rest/api/2/issue/PROJ-123" -> "PROJ-123"

    Args:
        key_or_url: Issue key or JIRA URL

    Returns:
        Extracted issue key

    Raises:
        ValueError: If the input doesn't contain a valid issue key
    """
    # If it looks like an issue key, return it
    if ISSUE_KEY_PATTERN.match(key_or_url):
        return key_or_url

    # Try to parse as URL
    try:
        parsed = urlparse(key_or_url)
        if parsed.scheme in ("http", "https") and parsed.path:
            # Handle /browse/PROJ-123 URLs
            if "/browse/" in parsed.path:
                key = parsed.path.split("/browse/")[-1].split("/")[0].split("?")[0]
                if ISSUE_KEY_PATTERN.match(key):
                    return key
            # Handle /rest/api/.../issue/PROJ-123 URLs
            if "/issue/" in parsed.path:
                key = parsed.path.split("/issue/")[-1].split("/")[0].split("?")[0]
                if ISSUE_KEY_PATTERN.match(key):
                    return key
    except Exception:
        pass

    # Fallback: try to find an issue key pattern anywhere in the string
    match = re.search(r"[A-Z][A-Z0-9_]+-\d+", key_or_url)
    if match:
        return match.group(0)

    # If nothing worked, return the original (let JIRA API report the error)
    return key_or_url


def handle_error(error: JiraToolError, command: str, pretty: bool) -> int:
    """Handle error and output error envelope."""
    envelope = error_response(error, command)
    output_result(envelope, pretty)
    return error.exit_code


class JsonErrorGroup(click.Group):
    """Click group that wraps usage errors in JSON envelope.

    When an LLM passes invalid arguments, Click normally prints a
    human-readable error to stderr and exits. This subclass catches
    those errors and outputs a structured JSON error envelope to stdout
    instead, maintaining the tool's JSON-only contract.
    """

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as e:
            pretty = ctx.params.get("pretty", False)
            envelope = error_response_from_dict(
                code=ErrorCode.INVALID_INPUT,
                message=str(e),
                command="cli",
                details={"hint": e.format_message()} if hasattr(e, "format_message") else None,
            )
            click.echo(format_json(envelope, pretty=pretty))
            ctx.exit(ExitCode.INVALID_INPUT)


@click.group(cls=JsonErrorGroup)
@click.option("--server", envvar="JIRA_SERVER", help="JIRA server URL")
@click.option("--token", envvar="JIRA_TOKEN", help="JIRA API token")
@click.option("--config", "config_path", type=click.Path(), help="Config file path")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@click.option("--debug", is_flag=True, help="Enable debug output to stderr")
@click.pass_context
def main(
    ctx: click.Context, server: str | None, token: str | None, config_path: str | None, pretty: bool, debug: bool
) -> None:
    """
    JIRA CLI tool for LLM agents.

    All commands output JSON to stdout. Use --pretty for human-readable output.
    Run 'jira describe' for machine-readable API documentation.

    Configuration priority:
    1. Command-line options (--server, --token)
    2. Environment variables (JIRA_SERVER, JIRA_TOKEN)
    3. Config file (~/.jira-tool.json)
    """
    ctx.ensure_object(dict)
    ctx.obj["pretty"] = pretty
    ctx.obj["debug"] = debug
    ctx.obj["server_override"] = server
    ctx.obj["token_override"] = token
    ctx.obj["config_path"] = config_path


def get_client(ctx: click.Context) -> JiraClient:
    """Get configured JIRA client from context."""
    config = load_config(
        config_path=ctx.obj.get("config_path"),
        server_override=ctx.obj.get("server_override"),
        token_override=ctx.obj.get("token_override"),
    )
    return JiraClient(config)


# =============================================================================
# Describe Command
# =============================================================================


@main.command("describe")
@click.option("--command", "command_name", help="Show description for a specific command only")
@click.pass_context
def describe(ctx: click.Context, command_name: str | None) -> None:
    """
    Show machine-readable API description.

    Returns a JSON document describing all available commands, their
    arguments, expected output fields, and suggested next actions.
    Use this to discover what the tool can do.
    """
    pretty = ctx.obj.get("pretty", False)
    tool_desc = get_tool_description()

    if command_name:
        # Normalize: "issue.get" -> "issue get", or accept either form
        normalized = command_name.replace(".", " ")
        matching = [c for c in tool_desc.commands if c.name == normalized]
        if not matching:
            envelope = error_response_from_dict(
                code=ErrorCode.INVALID_INPUT,
                message=f"Unknown command: {command_name}",
                command="describe",
                details={
                    "available_commands": [c.name for c in tool_desc.commands],
                },
            )
            output_result(envelope, pretty)
            sys.exit(ExitCode.INVALID_INPUT)
        data = matching[0].to_dict()
    else:
        data = tool_desc.to_dict()

    envelope = success_response(data, "describe")
    output_result(envelope, pretty)
    sys.exit(ExitCode.SUCCESS)


# =============================================================================
# Issue Commands
# =============================================================================


@main.group()
def issue() -> None:
    """Issue operations."""
    pass


@issue.command("get")
@click.argument("key")
@click.option("--fields", help="Comma-separated list of fields to return")
@click.option("--output", "output_field_name", help="Output only this field (plain text, no JSON envelope)")
@click.pass_context
def issue_get(ctx: click.Context, key: str, fields: str | None, output_field_name: str | None) -> None:
    """
    Get issue details.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

    Returns issue summary, description, status, and other core fields.

    Use --output to extract a single field (e.g., --output key, --output status).
    """
    command = "issue.get"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        field_list = fields.split(",") if fields else None
        raw_issue = client.get_issue(key, fields=field_list)

        # Normalize output to agent-friendly format
        issue_data = _normalize_issue(raw_issue)

        # If --output specified, just output that field
        if output_field_name:
            output_field(issue_data, output_field_name)
            sys.exit(ExitCode.SUCCESS)

        issue_key = issue_data.get("key", key)
        envelope = success_response(
            issue_data,
            command,
            next_actions=[
                f"jira issue comments {issue_key}",
                f"jira issue transitions {issue_key}",
                f"jira issue attachments {issue_key}",
                f"jira issue links {issue_key}",
            ],
        )
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("comments")
@click.argument("key")
@click.option("--limit", default=5, help="Maximum number of comments to return (default: 5)")
@click.option("--offset", default=0, help="Skip first N comments (default: 0)")
@click.option("--all", "fetch_all", is_flag=True, help="Fetch all comments (use with caution)")
@click.option("--summary-only", is_flag=True, help="Only return comment summary, not content")
@click.option("--newest-first", is_flag=True, help="Sort by newest first (default is oldest first)")
@click.pass_context
def issue_comments(
    ctx: click.Context, key: str, limit: int, offset: int, fetch_all: bool, summary_only: bool, newest_first: bool
) -> None:
    """
    Get comments for an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

    By default returns comments in chronological order (oldest first).
    Use --newest-first to reverse the order.
    """
    command = "issue.comments"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        if fetch_all:
            limit = 1000  # JIRA max

        order_by = "-created" if newest_first else "created"
        raw_comments = client.get_comments(key, start_at=offset, max_results=limit, order_by=order_by)

        # Build response data
        comments_data = _normalize_comments(raw_comments, summary_only=summary_only)
        comments_data["issue_key"] = key
        comments_data["pagination"] = {
            "offset": offset,
            "limit": limit,
            "returned": len(comments_data.get("comments", [])),
            "total": raw_comments.get("total", 0),
        }

        envelope = success_response(
            comments_data,
            command,
            next_actions=[
                f"jira issue comment {key} \"<your reply>\"",
                f"jira issue get {key}",
            ],
        )
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("search")
@click.argument("jql")
@click.option("--limit", default=20, help="Maximum results to return (default: 20)")
@click.option("--offset", default=0, help="Skip first N results (default: 0)")
@click.option("--fields", help="Comma-separated list of fields to return")
@click.option("--output", "output_field_name", help="Output only this field from each issue (one per line)")
@click.pass_context
def issue_search(ctx: click.Context, jql: str, limit: int, offset: int, fields: str | None, output_field_name: str | None) -> None:
    """
    Search for issues using JQL.

    JQL is the JIRA Query Language query string.

    Example: jira issue search "project = PROJ AND status = Open"

    Use --output to extract a field from each result (e.g., --output key).
    """
    command = "issue.search"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

        field_list = fields.split(",") if fields else None
        raw_results = client.search_issues(jql, fields=field_list, start_at=offset, max_results=limit)

        # Normalize results
        issues = [_normalize_issue(issue) for issue in raw_results.get("issues", [])]

        # If --output specified, output that field from each issue
        if output_field_name:
            for issue in issues:
                value = extract_field(issue, output_field_name)
                if value is not None:
                    click.echo(str(value))
            sys.exit(ExitCode.SUCCESS)

        search_data = {
            "jql": jql,
            "issues": issues,
            "pagination": {
                "offset": raw_results.get("startAt", offset),
                "limit": raw_results.get("maxResults", limit),
                "returned": len(raw_results.get("issues", [])),
                "total": raw_results.get("total", 0),
            },
        }

        envelope = success_response(
            search_data,
            command,
            next_actions=["jira issue get <KEY> -- get details for a specific result"],
        )
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("comment")
@click.argument("key")
@click.argument("body")
@click.pass_context
def issue_comment_add(ctx: click.Context, key: str, body: str) -> None:
    """
    Add a comment to an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
    BODY is the comment text.
    """
    command = "issue.comment"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        raw_comment = client.add_comment(key, body)

        # Normalize response
        comment_data = {
            "issue_key": key,
            "comment": _normalize_comment(raw_comment),
        }

        envelope = success_response(comment_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("transitions")
@click.argument("key")
@click.pass_context
def issue_transitions_list(ctx: click.Context, key: str) -> None:
    """
    List available transitions for an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
    """
    command = "issue.transitions"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        raw_transitions = client.get_transitions(key)

        # Normalize response
        transitions_data = {
            "issue_key": key,
            "transitions": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "to_status": t.get("to", {}).get("name"),
                }
                for t in raw_transitions.get("transitions", [])
            ],
        }

        envelope = success_response(
            transitions_data,
            command,
            next_actions=[
                f"jira issue transition {key} <ID> -- use an ID from the list above",
            ],
        )
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("transition")
@click.argument("key")
@click.argument("transition_id")
@click.option("--comment", help="Add a comment with the transition")
@click.pass_context
def issue_transition(ctx: click.Context, key: str, transition_id: str, comment: str | None) -> None:
    """
    Transition an issue to a new state.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
    TRANSITION_ID is the transition ID (use 'transitions' command to list available).
    """
    command = "issue.transition"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        # Get current status before transition
        issue_before = client.get_issue(key, fields=["status"])
        status_before = issue_before.get("fields", {}).get("status", {}).get("name", "Unknown")

        # Perform transition
        client.do_transition(key, transition_id, comment=comment)

        # Get new status after transition
        issue_after = client.get_issue(key, fields=["status"])
        status_after = issue_after.get("fields", {}).get("status", {}).get("name", "Unknown")

        transition_data = {
            "issue_key": key,
            "transition_id": transition_id,
            "status_before": status_before,
            "status_after": status_after,
            "comment_added": comment is not None,
        }

        envelope = success_response(transition_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("create")
@click.option("--project", required=True, help="Project key (e.g., PROJ)")
@click.option("--type", "issue_type", required=True, help="Issue type (e.g., Bug, Task)")
@click.option("--summary", required=True, help="Issue summary")
@click.option("--description", help="Issue description")
@click.pass_context
def issue_create(ctx: click.Context, project: str, issue_type: str, summary: str, description: str | None) -> None:
    """
    Create a new issue.

    Requires --project, --type, and --summary options.
    """
    command = "issue.create"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

        raw_issue = client.create_issue(
            project_key=project,
            issue_type=issue_type,
            summary=summary,
            description=description,
        )

        create_data = {
            "key": raw_issue.get("key"),
            "id": raw_issue.get("id"),
            "self": raw_issue.get("self"),
        }

        envelope = success_response(create_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("update")
@click.argument("key")
@click.option("--summary", help="New issue summary")
@click.option("--description", help="New issue description")
@click.option("--assignee", help="New assignee username (use empty string to unassign)")
@click.option("--priority", help="New priority name (e.g., High, Medium, Low)")
@click.option("--labels", help="Comma-separated list of labels (replaces existing)")
@click.pass_context
def issue_update(
    ctx: click.Context,
    key: str,
    summary: str | None,
    description: str | None,
    assignee: str | None,
    priority: str | None,
    labels: str | None,
) -> None:
    """
    Update an existing issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

    At least one field must be specified to update.
    """
    command = "issue.update"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        # Parse labels if provided
        label_list = [l.strip() for l in labels.split(",")] if labels else None

        # Check if any updates are requested
        if all(v is None for v in [summary, description, assignee, priority, label_list]):
            from .errors import InvalidInputError, ErrorCode
            raise InvalidInputError(
                code=ErrorCode.INVALID_INPUT,
                message="No fields specified to update. Use --summary, --description, --assignee, --priority, or --labels.",
            )

        # Get current issue state for comparison
        issue_before = client.get_issue(key, fields=["summary", "status", "assignee", "priority", "labels"])

        # Perform update
        client.update_issue(
            key=key,
            summary=summary,
            description=description,
            assignee=assignee,
            priority=priority,
            labels=label_list,
        )

        # Get updated issue
        issue_after = client.get_issue(key, fields=["summary", "status", "assignee", "priority", "labels"])

        update_data = {
            "issue_key": key,
            "updated_fields": [],
        }

        # Track what changed
        if summary is not None:
            update_data["updated_fields"].append("summary")
        if description is not None:
            update_data["updated_fields"].append("description")
        if assignee is not None:
            update_data["updated_fields"].append("assignee")
            update_data["assignee"] = issue_after.get("fields", {}).get("assignee", {})
            if update_data["assignee"]:
                update_data["assignee"] = update_data["assignee"].get("displayName")
        if priority is not None:
            update_data["updated_fields"].append("priority")
            update_data["priority"] = issue_after.get("fields", {}).get("priority", {}).get("name")
        if label_list is not None:
            update_data["updated_fields"].append("labels")
            update_data["labels"] = issue_after.get("fields", {}).get("labels", [])

        envelope = success_response(update_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("attachments")
@click.argument("key")
@click.pass_context
def issue_attachments(ctx: click.Context, key: str) -> None:
    """
    List attachments for an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

    Returns attachment metadata including filename, size, and content URL.
    """
    command = "issue.attachments"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        # Get issue with attachment field
        raw_issue = client.get_issue(key, fields=["attachment"])
        attachments = raw_issue.get("fields", {}).get("attachment", [])

        # Normalize attachments
        attachments_data = {
            "issue_key": key,
            "total": len(attachments),
            "attachments": [_normalize_attachment(a) for a in attachments],
        }

        envelope = success_response(attachments_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("links")
@click.argument("key")
@click.pass_context
def issue_links(ctx: click.Context, key: str) -> None:
    """
    List issue links (relationships to other issues).

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

    Shows relationships like: blocks, is blocked by, relates to, duplicates, etc.
    """
    command = "issue.links"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        # Get issue with issuelinks field
        raw_issue = client.get_issue(key, fields=["issuelinks"])
        raw_links = raw_issue.get("fields", {}).get("issuelinks", [])

        # Normalize links
        links = []
        for link in raw_links:
            link_type = link.get("type", {})
            # Each link has either inwardIssue or outwardIssue
            if "inwardIssue" in link:
                linked_issue = link["inwardIssue"]
                direction = "inward"
                relationship = link_type.get("inward", "related to")
            elif "outwardIssue" in link:
                linked_issue = link["outwardIssue"]
                direction = "outward"
                relationship = link_type.get("outward", "relates to")
            else:
                continue

            links.append({
                "direction": direction,
                "relationship": relationship,
                "link_type": link_type.get("name"),
                "issue_key": linked_issue.get("key"),
                "issue_summary": linked_issue.get("fields", {}).get("summary"),
                "issue_status": linked_issue.get("fields", {}).get("status", {}).get("name"),
            })

        links_data = {
            "issue_key": key,
            "total": len(links),
            "links": links,
        }

        envelope = success_response(links_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("worklogs")
@click.argument("key")
@click.option("--limit", default=20, help="Maximum worklogs to return (default: 20)")
@click.option("--offset", default=0, help="Skip first N worklogs (default: 0)")
@click.pass_context
def issue_worklogs(ctx: click.Context, key: str, limit: int, offset: int) -> None:
    """
    List worklogs (time tracking entries) for an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
    """
    command = "issue.worklogs"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        raw_worklogs = client.get_worklogs(key, start_at=offset, max_results=limit)

        # Normalize worklogs
        worklogs = []
        for wl in raw_worklogs.get("worklogs", []):
            author = wl.get("author", {})
            worklogs.append({
                "id": wl.get("id"),
                "author": author.get("displayName"),
                "time_spent": wl.get("timeSpent"),
                "time_spent_seconds": wl.get("timeSpentSeconds"),
                "comment": wl.get("comment"),
                "started": wl.get("started"),
                "created": wl.get("created"),
                "updated": wl.get("updated"),
            })

        worklogs_data = {
            "issue_key": key,
            "total": raw_worklogs.get("total", len(worklogs)),
            "worklogs": worklogs,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "returned": len(worklogs),
            },
        }

        envelope = success_response(worklogs_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("worklog")
@click.argument("key")
@click.argument("time_spent")
@click.option("--comment", help="Comment for the worklog entry")
@click.pass_context
def issue_worklog_add(ctx: click.Context, key: str, time_spent: str, comment: str | None) -> None:
    """
    Add a worklog entry to an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
    TIME_SPENT is in JIRA format (e.g., "2h 30m", "1d", "30m").
    """
    command = "issue.worklog"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        raw_worklog = client.add_worklog(key, time_spent, comment=comment)

        worklog_data = {
            "issue_key": key,
            "worklog": {
                "id": raw_worklog.get("id"),
                "time_spent": raw_worklog.get("timeSpent"),
                "time_spent_seconds": raw_worklog.get("timeSpentSeconds"),
                "comment": raw_worklog.get("comment"),
                "started": raw_worklog.get("started"),
            },
        }

        envelope = success_response(worklog_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("watchers")
@click.argument("key")
@click.pass_context
def issue_watchers(ctx: click.Context, key: str) -> None:
    """
    List watchers for an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
    """
    command = "issue.watchers"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        raw_watchers = client.get_watchers(key)

        # Normalize watchers
        watchers = []
        for w in raw_watchers.get("watchers", []):
            watchers.append({
                "name": w.get("name"),
                "display_name": w.get("displayName"),
                "email": w.get("emailAddress"),
                "active": w.get("active"),
            })

        watchers_data = {
            "issue_key": key,
            "count": raw_watchers.get("watchCount", len(watchers)),
            "is_watching": raw_watchers.get("isWatching", False),
            "watchers": watchers,
        }

        envelope = success_response(watchers_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("watch")
@click.argument("key")
@click.option("--user", help="Username to add as watcher (default: current user)")
@click.pass_context
def issue_watch(ctx: click.Context, key: str, user: str | None) -> None:
    """
    Add a watcher to an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

    By default, adds the current authenticated user as a watcher.
    Use --user to add a different user.
    """
    command = "issue.watch"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        # If no user specified, get current user from server
        if user is None:
            # Get current user from the myself endpoint
            myself = client._request("GET", "myself")
            user = myself.get("name")
            if not user:
                from .errors import InvalidInputError, ErrorCode
                raise InvalidInputError(
                    code=ErrorCode.INVALID_INPUT,
                    message="Could not determine current user. Please specify --user.",
                )

        client.add_watcher(key, user)

        watch_data = {
            "issue_key": key,
            "user": user,
            "action": "added",
        }

        envelope = success_response(watch_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@issue.command("unwatch")
@click.argument("key")
@click.option("--user", help="Username to remove as watcher (default: current user)")
@click.pass_context
def issue_unwatch(ctx: click.Context, key: str, user: str | None) -> None:
    """
    Remove a watcher from an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

    By default, removes the current authenticated user as a watcher.
    Use --user to remove a different user.
    """
    command = "issue.unwatch"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        # If no user specified, get current user from server
        if user is None:
            # Get current user from the myself endpoint
            myself = client._request("GET", "myself")
            user = myself.get("name")
            if not user:
                from .errors import InvalidInputError, ErrorCode
                raise InvalidInputError(
                    code=ErrorCode.INVALID_INPUT,
                    message="Could not determine current user. Please specify --user.",
                )

        client.remove_watcher(key, user)

        unwatch_data = {
            "issue_key": key,
            "user": user,
            "action": "removed",
        }

        envelope = success_response(unwatch_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


# =============================================================================
# Attachment Commands
# =============================================================================


@main.group()
def attachment() -> None:
    """Attachment operations."""
    pass


@attachment.command("get")
@click.argument("attachment_id")
@click.pass_context
def attachment_get(ctx: click.Context, attachment_id: str) -> None:
    """
    Get attachment metadata.

    ATTACHMENT_ID is the numeric attachment ID.
    """
    command = "attachment.get"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

        raw_attachment = client.get_attachment(attachment_id)
        attachment_data = _normalize_attachment(raw_attachment)

        envelope = success_response(attachment_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@attachment.command("content")
@click.argument("attachment_id")
@click.option("--max-size", default=102400, help="Maximum size in bytes (default: 100KB, 0 for no limit)")
@click.option("--encoding", default="utf-8", help="Text encoding (default: utf-8)")
@click.option("--raw", is_flag=True, help="Output raw content to stdout (no JSON envelope)")
@click.pass_context
def attachment_content(ctx: click.Context, attachment_id: str, max_size: int, encoding: str, raw: bool) -> None:
    """
    Get attachment content.

    ATTACHMENT_ID is the numeric attachment ID.

    By default, limits to 100KB and decodes as UTF-8 text.
    Use --raw to output content directly (useful for piping).

    Note: Binary files may not display correctly without --raw.
    """
    command = "attachment.content"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

        content_bytes, metadata = client.get_attachment_content(attachment_id, max_size=max_size)

        if raw:
            # Output raw content directly
            sys.stdout.buffer.write(content_bytes)
            sys.exit(ExitCode.SUCCESS)

        # Try to decode as text
        try:
            content_text = content_bytes.decode(encoding)
        except UnicodeDecodeError:
            # For binary files, indicate it's binary
            content_text = None

        content_data = {
            "attachment": _normalize_attachment(metadata),
            "size_bytes": len(content_bytes),
            "encoding": encoding if content_text else None,
            "is_text": content_text is not None,
            "content": content_text,
            "content_truncated": False,
        }

        if content_text is None:
            content_data["note"] = "Binary content - use --raw flag to download"

        envelope = success_response(content_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@attachment.command("upload")
@click.argument("key")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--filename", help="Override filename (default: use file's basename)")
@click.pass_context
def attachment_upload(ctx: click.Context, key: str, file_path: str, filename: str | None) -> None:
    """
    Upload an attachment to an issue.

    KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
    FILE_PATH is the path to the file to upload.
    """
    command = "attachment.upload"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        key = extract_issue_key(key)

        result = client.upload_attachment(key, file_path, filename=filename)

        # JIRA returns a list of attachments (usually just one)
        attachments = [_normalize_attachment(a) for a in result] if result else []

        upload_data = {
            "issue_key": key,
            "uploaded": len(attachments),
            "attachments": attachments,
        }

        envelope = success_response(upload_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


# =============================================================================
# Config Commands
# =============================================================================


@main.group()
def config() -> None:
    """Configuration commands."""
    pass


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show current configuration (redacted)."""
    command = "config.show"
    pretty = ctx.obj.get("pretty", False)

    try:
        config = load_config(
            config_path=ctx.obj.get("config_path"),
            server_override=ctx.obj.get("server_override"),
            token_override=ctx.obj.get("token_override"),
        )

        config_data = {
            "server": config.server,
            "token": f"{config.token[:8]}...{config.token[-4:]}" if len(config.token) > 12 else "***",
            "config_path": str(DEFAULT_CONFIG_PATH),
        }

        envelope = success_response(config_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


@config.command("sample")
@click.pass_context
def config_sample(ctx: click.Context) -> None:
    """Output a sample configuration file."""
    command = "config.sample"
    pretty = ctx.obj.get("pretty", False)

    sample = create_sample_config()
    config_data = {
        "sample_config": sample,
        "default_path": str(DEFAULT_CONFIG_PATH),
    }

    envelope = success_response(config_data, command)
    output_result(envelope, pretty)
    sys.exit(ExitCode.SUCCESS)


@config.command("test")
@click.pass_context
def config_test(ctx: click.Context) -> None:
    """Test connectivity to JIRA server."""
    command = "config.test"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)
        server_info = client.get_server_info()

        test_data = {
            "connected": True,
            "server_title": server_info.get("serverTitle"),
            "version": server_info.get("version"),
            "base_url": server_info.get("baseUrl"),
        }

        envelope = success_response(test_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    except JiraToolError as e:
        sys.exit(handle_error(e, command, pretty))
    except ConfigError as e:
        sys.exit(handle_error(e, command, pretty))


# =============================================================================
# Helper Functions
# =============================================================================


def _normalize_issue(raw_issue: dict[str, Any]) -> dict[str, Any]:
    """Normalize JIRA issue to agent-friendly format."""
    fields = raw_issue.get("fields", {})

    # Extract assignee
    assignee = fields.get("assignee")
    assignee_name = assignee.get("displayName") if assignee else None

    # Extract reporter
    reporter = fields.get("reporter")
    reporter_name = reporter.get("displayName") if reporter else None

    # Extract status
    status = fields.get("status", {})
    status_name = status.get("name") if status else None

    # Extract priority
    priority = fields.get("priority", {})
    priority_name = priority.get("name") if priority else None

    # Extract issue type
    issue_type = fields.get("issuetype", {})
    issue_type_name = issue_type.get("name") if issue_type else None

    # Extract project
    project = fields.get("project", {})
    project_key = project.get("key") if project else None

    # Extract resolution
    resolution = fields.get("resolution", {})
    resolution_name = resolution.get("name") if resolution else None

    return {
        "key": raw_issue.get("key"),
        "id": raw_issue.get("id"),
        "self": raw_issue.get("self"),
        "summary": fields.get("summary"),
        "description": fields.get("description"),
        "status": status_name,
        "priority": priority_name,
        "issue_type": issue_type_name,
        "project": project_key,
        "assignee": assignee_name,
        "reporter": reporter_name,
        "resolution": resolution_name,
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "labels": fields.get("labels", []),
    }


def _normalize_comment(raw_comment: dict[str, Any]) -> dict[str, Any]:
    """Normalize JIRA comment to agent-friendly format."""
    author = raw_comment.get("author", {})
    update_author = raw_comment.get("updateAuthor", {})

    return {
        "id": raw_comment.get("id"),
        "body": raw_comment.get("body"),
        "author": author.get("displayName"),
        "author_email": author.get("emailAddress"),
        "created": raw_comment.get("created"),
        "updated": raw_comment.get("updated"),
        "update_author": update_author.get("displayName") if update_author else None,
    }


def _normalize_comments(raw_comments: dict[str, Any], summary_only: bool = False) -> dict[str, Any]:
    """Normalize JIRA comments response to agent-friendly format."""
    comments = raw_comments.get("comments", [])
    total = raw_comments.get("total", len(comments))

    result: dict[str, Any] = {
        "total_comments": total,
    }

    if comments:
        # Get date range
        dates = [c.get("created") for c in comments if c.get("created")]
        if dates:
            result["oldest_in_batch"] = min(dates)
            result["newest_in_batch"] = max(dates)

    if not summary_only:
        result["comments"] = [_normalize_comment(c) for c in comments]
    else:
        # Summary only - just metadata
        result["comments_summary"] = [
            {
                "id": c.get("id"),
                "author": c.get("author", {}).get("displayName"),
                "created": c.get("created"),
                "body_preview": (c.get("body", "")[:100] + "...")
                if len(c.get("body", "")) > 100
                else c.get("body", ""),
            }
            for c in comments
        ]

    return result


def _normalize_attachment(raw_attachment: dict[str, Any]) -> dict[str, Any]:
    """Normalize JIRA attachment to agent-friendly format."""
    author = raw_attachment.get("author", {})

    # Format size in human-readable form
    size_bytes = raw_attachment.get("size", 0)
    if size_bytes < 1024:
        size_human = f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        size_human = f"{size_bytes / 1024:.1f} KB"
    else:
        size_human = f"{size_bytes / (1024 * 1024):.1f} MB"

    return {
        "id": raw_attachment.get("id"),
        "filename": raw_attachment.get("filename"),
        "size": size_bytes,
        "size_human": size_human,
        "mime_type": raw_attachment.get("mimeType"),
        "author": author.get("displayName") if author else None,
        "created": raw_attachment.get("created"),
        "content_url": raw_attachment.get("content"),
    }


if __name__ == "__main__":
    main()
