"""Shared helpers used by command modules.

These are re-exported from cli.py for backward compatibility.
"""

import re
import sys
from typing import Any
from urllib.parse import urlparse

import click

from ..client import JiraClient
from ..config import DEFAULT_CONFIG_PATH, JiraConfig, load_config
from ..envelope import error_response, error_response_from_dict, format_json, success_response
from ..errors import ConfigError, ErrorCode, ExitCode, JiraToolError

# Global options for all commands
pass_config = click.make_pass_decorator(JiraConfig, ensure=True)


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout.

    Automatically checks the click context for the --envelope flag.
    """
    ctx = click.get_current_context(silent=True)
    full_envelope = ctx.obj.get("envelope", False) if ctx and ctx.obj else False
    click.echo(format_json(envelope, pretty=pretty, full_envelope=full_envelope))


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
    """Output a single field value to stdout (plain text, no JSON envelope)."""
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


def get_client(ctx: click.Context) -> JiraClient:
    """Get configured JIRA client from context."""
    config = load_config(
        config_path=ctx.obj.get("config_path"),
        server_override=ctx.obj.get("server_override"),
        token_override=ctx.obj.get("token_override"),
        instance=ctx.obj.get("instance"),
    )
    ctx.obj["config"] = config
    return JiraClient(config, debug=ctx.obj.get("debug", False))


def _parse_visibility(value: str) -> dict[str, str]:
    """
    Parse a visibility string like 'role:Developers' or 'group:jira-users'.

    Args:
        value: Visibility string in 'type:value' format

    Returns:
        Dict with "type" and "value" keys for the JIRA API

    Raises:
        click.BadParameter: If the format is invalid
    """
    if ":" not in value:
        raise click.BadParameter(
            f"Invalid visibility format: '{value}'. "
            "Expected 'role:RoleName' or 'group:GroupName'.",
            param_hint="'--visibility'",
        )
    vis_type, vis_value = value.split(":", 1)
    vis_type = vis_type.strip().lower()
    vis_value = vis_value.strip()
    if vis_type not in ("role", "group"):
        raise click.BadParameter(
            f"Invalid visibility type: '{vis_type}'. Must be 'role' or 'group'.",
            param_hint="'--visibility'",
        )
    if not vis_value:
        raise click.BadParameter(
            "Visibility value cannot be empty.",
            param_hint="'--visibility'",
        )
    return {"type": vis_type, "value": vis_value}


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

    # Extract parent (for sub-tasks / technical tasks)
    parent = fields.get("parent")
    parent_key = parent.get("key") if parent else None

    # Extract epic link (customfield_10092)
    epic_link = fields.get("customfield_10092")

    # Extract subtask flag
    is_subtask = issue_type.get("subtask", False) if issue_type else False

    result: dict[str, Any] = {
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

    if parent_key:
        result["parent"] = parent_key
    if epic_link:
        result["epic_link"] = epic_link
    if is_subtask:
        result["subtask"] = True

    return result


def _normalize_comment(raw_comment: dict[str, Any]) -> dict[str, Any]:
    """Normalize JIRA comment to agent-friendly format."""
    author = raw_comment.get("author", {})
    update_author = raw_comment.get("updateAuthor", {})

    result = {
        "id": raw_comment.get("id"),
        "body": raw_comment.get("body"),
        "author": author.get("displayName"),
        "author_email": author.get("emailAddress"),
        "created": raw_comment.get("created"),
        "updated": raw_comment.get("updated"),
        "update_author": update_author.get("displayName") if update_author else None,
    }

    visibility = raw_comment.get("visibility")
    if visibility:
        result["visibility"] = {
            "type": visibility.get("type"),
            "value": visibility.get("value"),
        }

    return result


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
