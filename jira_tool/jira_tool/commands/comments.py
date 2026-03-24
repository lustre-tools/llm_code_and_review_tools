"""Comment commands: comments, comment, edit-comment, delete-comment."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    _normalize_comment,
    _normalize_comments,
    _parse_visibility,
    extract_issue_key,
    get_client,
    handle_error,
    output_result,
)


def register(main):
    """Register comment commands on *main*."""

    @main.command("comments")
    @click.argument("key")
    @click.option("--limit", default=10, help="Maximum number of comments to return (default: 10)")
    @click.option("--offset", default=0, help="Skip first N comments (default: 0)")
    @click.option("--all", "fetch_all", is_flag=True, help="Fetch all comments (use with caution)")
    @click.option("--summary-only", is_flag=True, help="Only return comment summary, not content")
    @click.option("--oldest-first", is_flag=True, help="Sort by oldest first (default is newest first)")
    @click.pass_context
    def issue_comments(
        ctx: click.Context, key: str, limit: int, offset: int, fetch_all: bool, summary_only: bool, oldest_first: bool
    ) -> None:
        """
        Get comments for an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

        By default returns comments in reverse chronological order (newest first).
        Use --oldest-first to get chronological order.
        """
        command = "comments"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            order_by = "created" if oldest_first else "-created"

            if fetch_all:
                raw_comments = client.get_all_comments(key, order_by=order_by)
            else:
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
                    f"jira comment {key} \"<your reply>\"",
                    f"jira get {key}",
                ],
            )
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("comment", context_settings={"ignore_unknown_options": True})
    @click.argument("key")
    @click.argument("body", default=None, required=False)
    @click.option("--body", "body_opt", default=None, help="Comment text (alternative to positional BODY)")
    @click.option("--update", "comment_id", default=None, help="Edit existing comment by ID instead of adding new")
    @click.option(
        "--visibility",
        default=None,
        help="Restrict comment visibility. Format: 'role:RoleName' or 'group:GroupName'. "
        "Use 'jira roles <PROJECT_KEY>' to list available roles.",
    )
    @click.pass_context
    def issue_comment_add(ctx: click.Context, key: str, body: str | None, body_opt: str | None,
                          comment_id: str | None, visibility: str | None) -> None:
        """
        Add or edit a comment on an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        BODY is the comment text (positional or via --body).

        Use --body when the comment text starts with a dash.
        Use --update COMMENT_ID to edit an existing comment.
        """
        body = body_opt or body
        if not body:
            raise click.UsageError("Missing comment text. Provide BODY as argument or via --body.")
        command = "edit-comment" if comment_id else "comment"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            visibility_dict = _parse_visibility(visibility) if visibility else None
            if comment_id:
                raw_comment = client.edit_comment(key, comment_id, body, visibility=visibility_dict)
            else:
                raw_comment = client.add_comment(key, body, visibility=visibility_dict)

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

    @main.command("edit-comment")
    @click.argument("key")
    @click.argument("comment_id")
    @click.argument("body")
    @click.option(
        "--visibility",
        default=None,
        help="Restrict comment visibility. Format: 'role:RoleName' or 'group:GroupName'. "
        "Use 'jira roles <PROJECT_KEY>' to list available roles.",
    )
    @click.pass_context
    def issue_comment_edit(ctx: click.Context, key: str, comment_id: str, body: str, visibility: str | None) -> None:
        """
        Edit an existing comment on an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        COMMENT_ID is the numeric comment ID to edit.
        BODY is the new comment text.
        """
        command = "edit-comment"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            visibility_dict = _parse_visibility(visibility) if visibility else None
            raw_comment = client.edit_comment(key, comment_id, body, visibility=visibility_dict)

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

    @main.command("delete-comment")
    @click.argument("key")
    @click.argument("comment_id")
    @click.pass_context
    def issue_comment_delete(ctx: click.Context, key: str, comment_id: str) -> None:
        """
        Delete a comment from an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        COMMENT_ID is the numeric comment ID to delete.
        """
        command = "delete-comment"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            client.delete_comment(key, comment_id)

            data = {
                "issue_key": key,
                "comment_id": comment_id,
                "deleted": True,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
