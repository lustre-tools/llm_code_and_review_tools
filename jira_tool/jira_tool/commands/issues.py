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
    resolve_cloud_user,
)


def register(main):
    """Register issue commands on *main*."""

    @main.command("get")
    @click.argument("key")
    @click.option("--fields", help="Comma-separated list of fields to return")
    @click.option("--output", "output_field_name", help="Output only this field (plain text, no JSON envelope)")
    @click.option("--comments", "include_comments", default=None, type=int,
                  help="Include N comments inline (e.g. --comments 5, --comments 50)")
    @click.pass_context
    def issue_get(ctx: click.Context, key: str, fields: str | None, output_field_name: str | None, include_comments: int | None) -> None:
        """
        Get issue details.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

        Returns issue summary, description, status, and other core fields.

        Use --comments N to include N comments inline (e.g. --comments 5).
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
                raw_comments = client.get_comments(key, start_at=0, max_results=include_comments, order_by="-created")
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
                next_actions.insert(0, f"jira get {issue_key} --comments 5")

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
    @click.option("--labels", help="Comma-separated labels to set on the new issue")
    @click.option("--epic", help="Epic issue key (e.g., PROJ-100) to add this issue to")
    @click.pass_context
    def issue_create(
        ctx: click.Context, project: str, issue_type: str, summary: str,
        description: str | None, labels: str | None, epic: str | None,
    ) -> None:
        """
        Create a new issue.

        Requires --project, --type, and --summary options.
        Use --labels to set labels at creation time.
        Use --epic to add the issue to an epic.
        """
        command = "create"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            fields: dict[str, Any] = {}
            if labels:
                fields["labels"] = [l.strip() for l in labels.split(",")]
            if epic:
                config = ctx.obj.get("config")
                epic_field = config.get_extra("epic_link_field", "customfield_10092") if config else "customfield_10092"
                fields[epic_field] = extract_issue_key(epic)
            # JIRA requires "Epic Name" (customfield_10093) when creating Epics
            if issue_type.lower() == "epic":
                config = ctx.obj.get("config")
                epic_name_field = config.get_extra("epic_name_field", "customfield_10093") if config else "customfield_10093"
                fields[epic_name_field] = summary

            raw_issue = client.create_issue(
                project_key=project,
                issue_type=issue_type,
                summary=summary,
                description=description,
                fields=fields if fields else None,
            )

            create_data: dict[str, Any] = {
                "key": raw_issue.get("key"),
                "id": raw_issue.get("id"),
                "self": raw_issue.get("self"),
            }
            if labels:
                create_data["labels"] = fields["labels"]
            if epic:
                create_data["epic"] = extract_issue_key(epic)

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
    @click.option("--labels", help="Comma-separated labels to add (keeps existing labels)")
    @click.option("--remove-labels", help="Comma-separated labels to remove")
    @click.option("--replace-labels", help="Comma-separated labels that replace ALL existing labels")
    @click.option("--epic", help="Epic issue key to move this issue to (e.g., PROJ-100)")
    @click.pass_context
    def issue_update(
        ctx: click.Context,
        key: str,
        summary: str | None,
        description: str | None,
        assignee: str | None,
        priority: str | None,
        labels: str | None,
        remove_labels: str | None,
        replace_labels: str | None,
        epic: str | None,
    ) -> None:
        """
        Update an existing issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

        At least one field must be specified to update.

        --labels adds to existing labels (additive).
        --remove-labels removes specific labels.
        --replace-labels replaces all existing labels.
        --replace-labels cannot be combined with --labels or --remove-labels.
        --epic sets or changes the epic link.
        """
        command = "update"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            # Parse labels
            add_label_list = [l.strip() for l in labels.split(",")] if labels else None
            remove_label_list = [l.strip() for l in remove_labels.split(",")] if remove_labels else None
            replace_label_list = [l.strip() for l in replace_labels.split(",")] if replace_labels else None

            # Conflict guard
            if replace_label_list and (add_label_list or remove_label_list):
                from ..errors import ErrorCode, InvalidInputError
                raise InvalidInputError(
                    code=ErrorCode.INVALID_INPUT,
                    message="--replace-labels cannot be combined with --labels or --remove-labels.",
                )

            # Check if any updates are requested
            if all(v is None for v in [summary, description, assignee, priority, add_label_list, remove_label_list, replace_label_list, epic]):
                from ..errors import ErrorCode, InvalidInputError
                raise InvalidInputError(
                    code=ErrorCode.INVALID_INPUT,
                    message="No fields specified to update. Use --summary, --description, --assignee, --priority, --labels, --remove-labels, --replace-labels, or --epic.",
                )

            # Build extra fields for the update call
            extra_fields: dict[str, Any] = {}
            if epic is not None:
                config = ctx.obj.get("config")
                epic_field = config.get_extra("epic_link_field", "customfield_10092") if config else "customfield_10092"
                extra_fields[epic_field] = extract_issue_key(epic) if epic else None

            # On Cloud, resolve display names/emails to accountIds for assignee
            if assignee and client.config.is_cloud and assignee != "":
                assignee = resolve_cloud_user(client, assignee)

            # Get current issue state for comparison
            issue_before = client.get_issue(key, fields=["summary", "status", "assignee", "priority", "labels"])

            # Perform update (non-label fields + replace-labels + epic)
            if any(v is not None for v in [summary, description, assignee, priority, replace_label_list]) or extra_fields:
                client.update_issue(
                    key=key,
                    summary=summary,
                    description=description,
                    assignee=assignee,
                    priority=priority,
                    labels=replace_label_list,
                    fields=extra_fields if extra_fields else None,
                )

            # Additive labels via the update API (add operation)
            if add_label_list:
                client.add_labels(key, add_label_list)

            # Remove labels
            if remove_label_list:
                client.remove_labels(key, remove_label_list)

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
            if add_label_list is not None or remove_label_list is not None or replace_label_list is not None:
                update_data["updated_fields"].append("labels")
                update_data["labels"] = issue_after.get("fields", {}).get("labels", [])
            if epic is not None:
                update_data["updated_fields"].append("epic")
                update_data["epic"] = extract_issue_key(epic)

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
                   priority=None, labels=None, remove_labels=None,
                   replace_labels=None, epic=None)

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
                    f"jira transition {key} <NAME_OR_ID> -- use a transition name or ID from the list above",
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
    @click.argument("transition_name_or_id")
    @click.option("--comment", help="Add a comment with the transition")
    @click.option("--resolution", help="Resolution name (e.g., Fixed, \"Won't Do\", Duplicate)")
    @click.pass_context
    def issue_transition(ctx: click.Context, key: str, transition_name_or_id: str, comment: str | None, resolution: str | None) -> None:
        """
        Transition an issue to a new state.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        TRANSITION_NAME_OR_ID is the transition name (e.g., "Start Progress",
        "In Progress") or numeric ID. Names are matched case-insensitively
        against both transition names and target status names.

        Use --resolution to set a resolution when closing (e.g., Fixed, "Won't Do").
        """
        command = "transition"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            # Resolve name to ID if not purely numeric
            transition_id = transition_name_or_id
            if not transition_name_or_id.isdigit():
                raw_transitions = client.get_transitions(key)
                needle = transition_name_or_id.lower()
                transitions = raw_transitions.get("transitions", [])

                # Priority: exact transition name > exact target status >
                #           substring transition name > substring target status
                matched = None
                for t in transitions:
                    if t.get("name", "").lower() == needle:
                        matched = t
                        break
                    if t.get("to", {}).get("name", "").lower() == needle:
                        matched = t
                        # Don't break — prefer exact transition-name match

                # Fallback: substring match (e.g., "Close" matches "Close Issue" or "Closed")
                if matched is None:
                    substring_matches = []
                    for t in transitions:
                        t_name = t.get("name", "").lower()
                        to_name = t.get("to", {}).get("name", "").lower()
                        if needle in t_name or needle in to_name:
                            substring_matches.append(t)
                    if len(substring_matches) == 1:
                        matched = substring_matches[0]
                    elif len(substring_matches) > 1:
                        # Ambiguous — show the matches
                        options = [
                            f"  {t.get('id')}: {t.get('name')} -> {t.get('to', {}).get('name')}"
                            for t in substring_matches
                        ]
                        from ..errors import ErrorCode, InvalidInputError
                        raise InvalidInputError(
                            code=ErrorCode.INVALID_INPUT,
                            message=f"Ambiguous match for '{transition_name_or_id}'. Did you mean:\n" + "\n".join(options),
                        )

                if matched is None:
                    available = [
                        f"  {t.get('id')}: {t.get('name')} -> {t.get('to', {}).get('name')}"
                        for t in transitions
                    ]
                    from ..errors import ErrorCode, InvalidInputError
                    raise InvalidInputError(
                        code=ErrorCode.INVALID_INPUT,
                        message=f"No transition matching '{transition_name_or_id}'. Available:\n" + "\n".join(available),
                    )
                transition_id = matched["id"]

            # Build fields for the transition (e.g., resolution)
            fields: dict[str, Any] | None = None
            if resolution:
                fields = {"resolution": {"name": resolution}}

            # Get current status before transition
            issue_before = client.get_issue(key, fields=["status"])
            status_before = issue_before.get("fields", {}).get("status", {}).get("name", "Unknown")

            # Perform transition
            client.do_transition(key, transition_id, comment=comment, fields=fields)

            # Get new status after transition
            issue_after = client.get_issue(key, fields=["status", "resolution"])
            status_after = issue_after.get("fields", {}).get("status", {}).get("name", "Unknown")

            transition_data: dict[str, Any] = {
                "issue_key": key,
                "transition_id": transition_id,
                "status_before": status_before,
                "status_after": status_after,
                "comment_added": comment is not None,
            }
            if resolution:
                res = issue_after.get("fields", {}).get("resolution")
                transition_data["resolution"] = res.get("name") if res else resolution

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
    @click.option("--type", "subtask_type", default=None,
                  help="Subtask issue type name (auto-detected from project if omitted)")
    @click.option("--labels", help="Comma-separated labels to set on the subtask")
    @click.pass_context
    def issue_create_subtask(
        ctx: click.Context, parent_key: str, summary: str,
        description: str | None, subtask_type: str | None,
        labels: str | None,
    ) -> None:
        """
        Create a subtask under a parent issue.

        PARENT_KEY is the parent issue key (e.g., PROJ-123).

        The subtask issue type is auto-detected from the project's
        available types (first type where subtask=true). Use --type
        to override if the project has multiple subtask types.
        """
        command = "create-subtask"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            parent_key = extract_issue_key(parent_key)

            # Get the parent's project key
            project_key = parent_key.rsplit("-", 1)[0]

            # Auto-detect subtask type if not specified
            if subtask_type is None:
                issue_types = client.get_issue_types(project_key)
                subtask_types = [t for t in issue_types if t.get("subtask")]
                if not subtask_types:
                    from ..errors import ErrorCode, InvalidInputError
                    raise InvalidInputError(
                        code=ErrorCode.INVALID_INPUT,
                        message=f"No subtask issue types found for project {project_key}. "
                                f"Use --type to specify one explicitly.",
                    )
                subtask_type = subtask_types[0]["name"]

            fields: dict[str, Any] = {"parent": {"key": parent_key}}
            if labels:
                fields["labels"] = [l.strip() for l in labels.split(",")]

            result = client.create_issue(
                project_key=project_key,
                issue_type=subtask_type,
                summary=summary,
                description=description,
                fields=fields,
            )

            data: dict[str, Any] = {
                "key": result.get("key"),
                "id": result.get("id"),
                "parent_key": parent_key,
                "type": subtask_type,
            }
            if labels:
                data["labels"] = fields["labels"]

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
