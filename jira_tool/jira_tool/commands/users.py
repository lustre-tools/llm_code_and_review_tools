"""User commands: users."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    get_client,
    handle_error,
    output_result,
)


def register(main):
    """Register user commands on *main*."""

    @main.command("users")
    @click.argument("query")
    @click.option("--limit", default=10, help="Maximum results to return (default: 10)")
    @click.pass_context
    def user_search(ctx: click.Context, query: str, limit: int) -> None:
        """
        Search for users by name, username, or email.

        QUERY is the search string.
        """
        command = "users"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw_users = client.search_users(query, max_results=limit)

            users = []
            for u in raw_users:
                users.append({
                    "name": u.get("name"),
                    "display_name": u.get("displayName"),
                    "email": u.get("emailAddress"),
                    "active": u.get("active"),
                })

            data = {
                "query": query,
                "total": len(users),
                "users": users,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
