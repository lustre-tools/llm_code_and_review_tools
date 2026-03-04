"""Config commands: config group (show/sample/test)."""

import sys

import click

from ..config import DEFAULT_CONFIG_PATH, create_sample_config, load_config
from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    get_client,
    handle_error,
    output_result,
)


def register(main):
    """Register config commands on *main*."""

    @main.group()
    def config() -> None:
        """Configuration commands."""
        pass

    @config.command("show")
    @click.pass_context
    def config_show(ctx: click.Context) -> None:
        """Show current configuration (redacted)."""
        command = "config.show"
        pretty = ctx.obj.get("pretty", False)

        try:
            cfg = load_config(
                config_path=ctx.obj.get("config_path"),
                server_override=ctx.obj.get("server_override"),
                token_override=ctx.obj.get("token_override"),
            )

            config_data = {
                "server": cfg.server,
                "token": f"{cfg.token[:8]}...{cfg.token[-4:]}" if len(cfg.token) > 12 else "***",
                "config_path": str(DEFAULT_CONFIG_PATH),
            }

            envelope = success_response(config_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @config.command("sample")
    @click.pass_context
    def config_sample(ctx: click.Context) -> None:
        """Output a sample configuration file."""
        command = "config.sample"
        pretty = ctx.obj.get("pretty", False)

        sample = create_sample_config()
        config_data = {
            "sample_config": sample,
            "default_path": str(DEFAULT_CONFIG_PATH),
        }

        envelope = success_response(config_data, command)
        output_result(envelope, pretty)
        sys.exit(ExitCode.SUCCESS)

    @config.command("test")
    @click.pass_context
    def config_test(ctx: click.Context) -> None:
        """Test connectivity to JIRA server."""
        command = "config.test"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            server_info = client.get_server_info()

            test_data = {
                "connected": True,
                "server_title": server_info.get("serverTitle"),
                "version": server_info.get("version"),
                "base_url": server_info.get("baseUrl"),
            }

            envelope = success_response(test_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
