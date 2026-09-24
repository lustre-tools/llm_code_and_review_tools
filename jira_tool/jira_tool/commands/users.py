"""User commands: users."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError, NotFoundError
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

        QUERY is the search string.  On a server where search finds
        nothing (no Browse Users permission), QUERY is looked up as an
        exact username instead; "active" false is a deactivated account.
        """
        command = "users"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw_users = client.search_users(query, max_results=limit)
            if not raw_users and not client.config.is_cloud:
                # Search needs Browse Users, which the Whamcloud accounts
                # lack; an exact username still resolves.
                try:
                    raw_users = [client.get_user(query)]
                except NotFoundError:
                    raw_users = []

            users = []
            for u in raw_users:
                user_data = {
                    "name": u.get("name"),
                    "display_name": u.get("displayName"),
                    "email": u.get("emailAddress"),
                    "active": u.get("active"),
                }
                # Cloud uses accountId instead of name for user identity
                if u.get("accountId"):
                    user_data["account_id"] = u["accountId"]
                users.append(user_data)

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
