"""Page read command (``get``)."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, ConfluenceToolError
from ._helpers import extract_page_id, get_client, handle_error, normalize_page, output_result


def register(main):
    """Register the get command on *main*."""

    @main.command("get")
    @click.argument("id_or_url")
    @click.option("--output", "output_field_name", help="Output only this field as plain text")
    @click.pass_context
    def get(ctx, id_or_url, output_field_name):
        """Get a page's content by id or Confluence URL.

        The HTML body is rendered to plain text in the `text` field.
        """
        command = "get"
        pretty = ctx.obj.get("pretty", False)
        try:
            client = get_client(ctx)
            page_id = extract_page_id(id_or_url)
            raw = client.get_page(page_id)
            data = normalize_page(raw, client.config.server)

            if output_field_name:
                value = data.get(output_field_name)
                if value is not None:
                    click.echo(str(value))
                sys.exit(ExitCode.SUCCESS)

            envelope = success_response(
                data, command,
                next_actions=["confluence search <text> -- find related pages"],
            )
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)
        except (ConfluenceToolError, ConfigError) as e:
            sys.exit(handle_error(e, command, pretty))
