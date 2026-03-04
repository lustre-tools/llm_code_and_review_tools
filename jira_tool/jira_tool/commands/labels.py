"""Label commands: add-label, remove-label."""

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
    """Register label commands on *main*."""

    @main.command("add-label")
    @click.argument("key")
    @click.argument("labels", nargs=-1, required=True)
    @click.pass_context
    def issue_add_label(ctx: click.Context, key: str, labels: tuple[str, ...]) -> None:
        """
        Add one or more labels to an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        LABELS are the label names to add.
        """
        command = "add-label"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            client.add_labels(key, list(labels))

            data = {
                "issue_key": key,
                "labels_added": list(labels),
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("remove-label")
    @click.argument("key")
    @click.argument("labels", nargs=-1, required=True)
    @click.pass_context
    def issue_remove_label(ctx: click.Context, key: str, labels: tuple[str, ...]) -> None:
        """
        Remove one or more labels from an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        LABELS are the label names to remove.
        """
        command = "remove-label"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            client.remove_labels(key, list(labels))

            data = {
                "issue_key": key,
                "labels_removed": list(labels),
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
