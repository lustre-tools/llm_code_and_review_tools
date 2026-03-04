"""Worklog commands: worklogs, worklog."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    extract_issue_key,
    get_client,
    handle_error,
    output_result,
)


def register(main):
    """Register worklog commands on *main*."""

    @main.command("worklogs")
    @click.argument("key")
    @click.option("--limit", default=20, help="Maximum worklogs to return (default: 20)")
    @click.option("--offset", default=0, help="Skip first N worklogs (default: 0)")
    @click.pass_context
    def issue_worklogs(ctx: click.Context, key: str, limit: int, offset: int) -> None:
        """
        List worklogs (time tracking entries) for an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        """
        command = "worklogs"
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

    @main.command("worklog")
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
        command = "worklog"
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
