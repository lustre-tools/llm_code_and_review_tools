"""Space listing command."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, ConfluenceToolError
from ._helpers import get_client, handle_error, output_result


def register(main):
    """Register the spaces command on *main*."""

    @main.command("spaces")
    @click.option("--limit", default=100, help="Maximum spaces to return (default: 100)")
    @click.option("--output", "output_field_name", help="Output only this field from each space (one per line)")
    @click.pass_context
    def spaces(ctx, limit, output_field_name):
        """List spaces on the selected instance (also a connectivity check)."""
        command = "spaces"
        pretty = ctx.obj.get("pretty", False)
        try:
            client = get_client(ctx)
            raw = client.list_spaces(limit=limit)
            server = client.config.server
            result = []
            for s in raw.get("results", []):
                links = s.get("_links", {}) or {}
                webui = links.get("webui", "")
                result.append({
                    "key": s.get("key"),
                    "name": s.get("name"),
                    "type": s.get("type"),
                    "url": f"{server}{webui}" if webui else None,
                })

            if output_field_name:
                for s in result:
                    value = s.get(output_field_name)
                    if value is not None:
                        click.echo(str(value))
                sys.exit(ExitCode.SUCCESS)

            data = {"spaces": result, "returned": len(result)}
            envelope = success_response(data, command, next_actions=["confluence search <text>"])
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)
        except (ConfluenceToolError, ConfigError) as e:
            sys.exit(handle_error(e, command, pretty))
