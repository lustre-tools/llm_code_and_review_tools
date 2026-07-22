"""Search command."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, ConfluenceToolError
from ._helpers import get_client, handle_error, normalize_result, output_result


def _escape_cql_text(text: str) -> str:
    """Escape a user string for use inside a CQL ``text ~ "..."`` term."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def register(main):
    """Register the search command on *main*."""

    @main.command("search")
    @click.argument("query")
    @click.option("--space", help="Restrict to a space key")
    @click.option("--cql", "is_cql", is_flag=True, help="Treat the query as a raw CQL expression")
    @click.option("--limit", default=25, help="Maximum results to return (default: 25)")
    @click.option("--output", "output_field_name", help="Output only this field from each result (one per line)")
    @click.pass_context
    def search(ctx, query, space, is_cql, limit, output_field_name):
        """Search pages by text, or pass a raw CQL query with --cql.

        Plain text is wrapped as `type = page AND text ~ "<query>"`.
        """
        command = "search"
        pretty = ctx.obj.get("pretty", False)
        try:
            client = get_client(ctx)

            if is_cql:
                cql = query
            else:
                cql = f'type = page AND text ~ "{_escape_cql_text(query)}"'
            if space:
                cql = f'({cql}) AND space = "{space}"'

            raw = client.search(cql, limit=limit, expand="space")
            server = client.config.server
            results = [normalize_result(r, server) for r in raw.get("results", [])]

            if output_field_name:
                for r in results:
                    value = r.get(output_field_name)
                    if value is not None:
                        click.echo(str(value))
                sys.exit(ExitCode.SUCCESS)

            data = {
                "cql": cql,
                "results": results,
                "returned": len(results),
                "total": raw.get("totalSize", raw.get("size", len(results))),
            }
            envelope = success_response(
                data, command,
                next_actions=["confluence get <id> -- read a specific page"],
            )
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)
        except (ConfluenceToolError, ConfigError) as e:
            sys.exit(handle_error(e, command, pretty))
