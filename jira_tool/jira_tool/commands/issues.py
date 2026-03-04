"""Issue CRUD commands: get, create, update, assign, transitions, subtasks."""

import sys
from typing import Any

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    _normalize_comments,
    _normalize_issue,
    extract_field,
    extract_issue_key,
    get_client,
    handle_error,
    output_field,
    output_result,
)


def register(main):
    """Register issue commands on *main*."""

    @main.command("get")
    @click.argument("key")
    @click.option("--fields", help="Comma-separated list of fields to return")
    @click.option("--output", "output_field_name", help="Output only this field (plain text, no JSON envelope)")
    @click.option("--comments", "include_comments", is_flag=True, default=False,
                  help="Include first 5 comments inline")
    @click.pass_context
    def issue_get(ctx: click.Context, key: str, fields: str | None, output_field_name: str | None, include_comments: bool) -> None:
        """
        Get issue details.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

        Returns issue summary, description, status, and other core fields.

        Use --comments to include comments inline (default: 5).
        Use --output to extract a single field (e.g., --output key, --output status).
        """
        command = "get"
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

            # If --comments specified, fetch and inline comments
            if include_comments:
                raw_comments = client.get_comments(key, start_at=0, max_results=5, order_by="-created")
                comments_data = _normalize_comments(raw_comments)
                issue_data["comments"] = comments_data.get("comments", [])
                issue_data["total_comments"] = comments_data.get("total_comments", 0)

            issue_key = issue_data.get("key", key)
            next_actions = [
                f"jira comments {issue_key}",
                f"jira transitions {issue_key}",
                f"jira attachments {issue_key}",
                f"jira links {issue_key}",
            ]
            if not include_comments:
                # Suggest --comments if they didn't use it
                next_actions.insert(0, f"jira get {issue_key} --comments")

            envelope = success_response(
                issue_data,
                command,
                next_actions=next_actions,
            )
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("create")
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
        command = "create"
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

    @main.command("update")
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
        command = "update"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            # Parse labels if provided
            label_list = [l.strip() for l in labels.split(",")] if labels else None

            # Check if any updates are requested
            if all(v is None for v in [summary, description, assignee, priority, label_list]):
                from ..errors import InvalidInputError, ErrorCode
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

            update_data: dict[str, Any] = {
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

    @main.command("assign")
    @click.argument("key")
    @click.argument("assignee")
    @click.pass_context
    def assign(ctx: click.Context, key: str, assignee: str) -> None:
        """
        Assign an issue to a user.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        ASSIGNEE is the username. Use "" to unassign.
        """
        ctx.invoke(issue_update, key=key, assignee=assignee,
                   summary=None, description=None,
                   priority=None, labels=None)

    @main.command("transitions")
    @click.argument("key")
    @click.pass_context
    def issue_transitions_list(ctx: click.Context, key: str) -> None:
        """
        List available transitions for an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        """
        command = "transitions"
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
                    f"jira transition {key} <ID> -- use an ID from the list above",
                ],
            )
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("transition")
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
        command = "transition"
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

    @main.command("issue-types")
    @click.argument("project_key", required=False, default=None)
    @click.pass_context
    def issue_types_list(ctx: click.Context, project_key: str | None) -> None:
        """
        List available issue types.

        PROJECT_KEY is an optional project key (e.g., LU, EX).
        If provided, shows only issue types valid for that project.
        If omitted, shows all issue types on the server.
        """
        command = "issue-types"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw_types = client.get_issue_types(project_key)

            types = []
            for it in raw_types:
                entry: dict[str, Any] = {
                    "id": it.get("id"),
                    "name": it.get("name"),
                    "subtask": it.get("subtask", False),
                }
                desc = it.get("description")
                if desc:
                    entry["description"] = desc
                types.append(entry)

            data: dict[str, Any] = {
                "total": len(types),
                "issue_types": types,
            }
            if project_key:
                data["project_key"] = project_key

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("create-subtask")
    @click.argument("parent_key")
    @click.option("--summary", required=True, help="Subtask summary")
    @click.option("--description", default=None, help="Subtask description")
    @click.pass_context
    def issue_create_subtask(ctx: click.Context, parent_key: str, summary: str, description: str | None) -> None:
        """
        Create a subtask under a parent issue.

        PARENT_KEY is the parent issue key (e.g., PROJ-123).
        """
        command = "create-subtask"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            parent_key = extract_issue_key(parent_key)

            # Get the parent's project key
            project_key = parent_key.rsplit("-", 1)[0]

            result = client.create_issue(
                project_key=project_key,
                issue_type="Sub-task",
                summary=summary,
                description=description,
                fields={"parent": {"key": parent_key}},
            )

            data = {
                "key": result.get("key"),
                "id": result.get("id"),
                "parent_key": parent_key,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("subtasks")
    @click.argument("key")
    @click.pass_context
    def issue_subtasks(ctx: click.Context, key: str) -> None:
        """
        List subtasks of an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        """
        command = "subtasks"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            raw_issue = client.get_issue(key, fields=["subtasks"])
            raw_subtasks = raw_issue.get("fields", {}).get("subtasks", [])

            subtasks = []
            for st in raw_subtasks:
                subtasks.append({
                    "key": st.get("key"),
                    "summary": st.get("fields", {}).get("summary"),
                    "status": st.get("fields", {}).get("status", {}).get("name"),
                })

            data = {
                "issue_key": key,
                "total": len(subtasks),
                "subtasks": subtasks,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("delete-subtask")
    @click.argument("key")
    @click.pass_context
    def issue_delete_subtask(ctx: click.Context, key: str) -> None:
        """
        Delete a subtask.

        KEY is the subtask issue key (e.g., PROJ-124) or a JIRA URL.
        """
        command = "delete-subtask"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            client.delete_issue(key)

            data = {
                "issue_key": key,
                "deleted": True,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
