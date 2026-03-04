"""Watcher commands: watchers, watch, unwatch."""

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
    """Register watcher commands on *main*."""

    @main.command("watchers")
    @click.argument("key")
    @click.pass_context
    def issue_watchers(ctx: click.Context, key: str) -> None:
        """
        List watchers for an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        """
        command = "watchers"
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

    @main.command("watch")
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
        command = "watch"
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
                    from ..errors import InvalidInputError, ErrorCode
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

    @main.command("unwatch")
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
        command = "unwatch"
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
                    from ..errors import InvalidInputError, ErrorCode
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
