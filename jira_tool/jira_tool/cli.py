"""CLI entry point for JIRA tool."""

import sys
from typing import Any

import click

from .client import JiraClient
from .config import DEFAULT_CONFIG_PATH, JiraConfig, create_sample_config, load_config
from .envelope import error_response, format_json, success_response
from .errors import ConfigError, ExitCode, JiraToolError

# Global options for all commands
pass_config = click.make_pass_decorator(JiraConfig, ensure=True)


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout."""
    click.echo(format_json(envelope, pretty=pretty))


def handle_error(error: JiraToolError, command: str, pretty: bool) -> int:
    """Handle error and output error envelope."""
    envelope = error_response(error, command)
    output_result(envelope, pretty)
    return error.exit_code


@click.group()
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
# Issue Commands
# =============================================================================


@main.group()
def issue() -> None:
    """Issue operations."""
    pass


@issue.command("get")
@click.argument("key")
@click.option("--fields", help="Comma-separated list of fields to return")
@click.pass_context
def issue_get(ctx: click.Context, key: str, fields: str | None) -> None:
    """
    Get issue details.

    KEY is the issue key (e.g., PROJ-123).

    Returns issue summary, description, status, and other core fields.
    """
    command = "issue.get"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

        field_list = fields.split(",") if fields else None
        raw_issue = client.get_issue(key, fields=field_list)

        # Normalize output to agent-friendly format
        issue_data = _normalize_issue(raw_issue)

        envelope = success_response(issue_data, command)
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
@click.pass_context
def issue_comments(ctx: click.Context, key: str, limit: int, offset: int, fetch_all: bool, summary_only: bool) -> None:
    """
    Get comments for an issue.

    KEY is the issue key (e.g., PROJ-123).

    By default returns the last 5 comments to manage LLM context size.
    Use --limit and --offset for pagination.
    """
    command = "issue.comments"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

        if fetch_all:
            limit = 1000  # JIRA max

        raw_comments = client.get_comments(key, start_at=offset, max_results=limit)

        # Build response data
        comments_data = _normalize_comments(raw_comments, summary_only=summary_only)
        comments_data["issue_key"] = key
        comments_data["pagination"] = {
            "offset": offset,
            "limit": limit,
            "returned": len(comments_data.get("comments", [])),
            "total": raw_comments.get("total", 0),
        }

        envelope = success_response(comments_data, command)
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
@click.pass_context
def issue_search(ctx: click.Context, jql: str, limit: int, offset: int, fields: str | None) -> None:
    """
    Search for issues using JQL.

    JQL is the JIRA Query Language query string.

    Example: jira issue search "project = PROJ AND status = Open"
    """
    command = "issue.search"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

        field_list = fields.split(",") if fields else None
        raw_results = client.search_issues(jql, fields=field_list, start_at=offset, max_results=limit)

        # Normalize results
        search_data = {
            "jql": jql,
            "issues": [_normalize_issue(issue) for issue in raw_results.get("issues", [])],
            "pagination": {
                "offset": raw_results.get("startAt", offset),
                "limit": raw_results.get("maxResults", limit),
                "returned": len(raw_results.get("issues", [])),
                "total": raw_results.get("total", 0),
            },
        }

        envelope = success_response(search_data, command)
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

    KEY is the issue key (e.g., PROJ-123).
    BODY is the comment text.
    """
    command = "issue.comment"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

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

    KEY is the issue key (e.g., PROJ-123).
    """
    command = "issue.transitions"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

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

        envelope = success_response(transitions_data, command)
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

    KEY is the issue key (e.g., PROJ-123).
    TRANSITION_ID is the transition ID (use 'transitions' command to list available).
    """
    command = "issue.transition"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

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


@issue.command("attachments")
@click.argument("key")
@click.pass_context
def issue_attachments(ctx: click.Context, key: str) -> None:
    """
    List attachments for an issue.

    KEY is the issue key (e.g., PROJ-123).

    Returns attachment metadata including filename, size, and content URL.
    """
    command = "issue.attachments"
    pretty = ctx.obj.get("pretty", False)

    try:
        client = get_client(ctx)

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
