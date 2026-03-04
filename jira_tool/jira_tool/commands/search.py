"""Search and describe commands."""

import sys

import click

from ..describe import get_tool_description
from ..envelope import error_response_from_dict, success_response
from ..errors import ConfigError, ErrorCode, ExitCode, JiraToolError
from ._helpers import (
    _normalize_issue,
    extract_field,
    get_client,
    handle_error,
    output_result,
)


def register(main):
    """Register search commands on *main*."""

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
            # Normalize: "get" or "issue.get" -> "get", accept either form
            normalized = command_name.replace(".", " ").replace("issue ", "")
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

    @main.command("search")
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

        Example: jira search "project = PROJ AND status = Open"

        Use --output to extract a field from each result (e.g., --output key).
        """
        command = "search"
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
                next_actions=["jira get <KEY> -- get details for a specific result"],
            )
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
