"""Meta commands: describe, config-sample, check."""

import sys

import click

from ..config import create_sample_config
from ..describe import get_tool_description
from ..envelope import error_response_from_dict, success_response
from ..errors import ConfigError, ErrorCode, ExitCode, ConfluenceToolError
from ._helpers import get_client, handle_error, output_result


def register(main):
    """Register meta commands on *main*."""

    @main.command("describe")
    @click.option("--command", "command_name", help="Show description for a specific command only")
    @click.pass_context
    def describe(ctx, command_name):
        """Show machine-readable API description."""
        pretty = ctx.obj.get("pretty", False)
        tool_desc = get_tool_description()
        if command_name:
            normalized = command_name.replace(".", " ").strip()
            matching = [c for c in tool_desc.commands if c.name == normalized]
            if not matching:
                envelope = error_response_from_dict(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Unknown command: {command_name}",
                    command="describe",
                    details={"available_commands": [c.name for c in tool_desc.commands]},
                )
                output_result(envelope, pretty)
                sys.exit(ExitCode.INVALID_INPUT)
            data = matching[0].to_dict()
        else:
            data = tool_desc.to_dict()
        output_result(success_response(data, "describe"), pretty)
        sys.exit(ExitCode.SUCCESS)

    @main.command("config-sample")
    @click.pass_context
    def config_sample(ctx):
        """Print a sample ~/.confluence-tool.json to stdout."""
        click.echo(create_sample_config())
        sys.exit(ExitCode.SUCCESS)

    @main.command("check")
    @click.pass_context
    def check(ctx):
        """Verify connectivity/auth for the selected instance."""
        command = "check"
        pretty = ctx.obj.get("pretty", False)
        try:
            client = get_client(ctx)
            client.list_spaces(limit=1)
            data = {
                "ok": True,
                "server": client.config.server,
                "is_cloud": client.config.is_cloud,
                "anonymous": client.config.is_anonymous,
            }
            output_result(success_response(data, command), pretty)
            sys.exit(ExitCode.SUCCESS)
        except (ConfluenceToolError, ConfigError) as e:
            sys.exit(handle_error(e, command, pretty))
