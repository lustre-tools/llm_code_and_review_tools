"""Config commands: config group (show/sample/test)."""

import sys

import click

from ..config import (
    DEFAULT_CONFIG_PATH,
    config_file_path,
    create_sample_config,
    load_config,
)
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
                instance=ctx.obj.get("instance"),
            )

            config_data = {
                "server": cfg.server,
                "auth_type": cfg.auth_type,
                "token": f"{cfg.token[:8]}...{cfg.token[-4:]}" if len(cfg.token) > 12 else "***",
                "config_path": str(config_file_path(ctx.obj.get("config_path"))),
            }
            if cfg.email:
                config_data["email"] = cfg.email
            instance = ctx.obj.get("instance")
            if instance:
                config_data["instance"] = instance

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
    @click.option(
        "--project",
        help="Also report whether you may create issues in this project",
    )
    @click.pass_context
    def config_test(ctx: click.Context, project: str | None) -> None:
        """Test connectivity to JIRA server, and the token if one is set."""
        command = "config.test"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx, project=project)
            server_info = client.get_server_info()
            authenticated: bool | None = False
            user = None
            if client.config.authenticated:
                myself = client.check_token()
                authenticated = True if myself else None
                user = (myself or {}).get("name") or (myself or {}).get("displayName")

            test_data = {
                "connected": True,
                "authenticated": authenticated,
                "user": user,
                "server_title": server_info.get("serverTitle"),
                "version": server_info.get("version"),
                "base_url": server_info.get("baseUrl"),
            }
            if project:
                test_data["project"] = project
                test_data["can_create"] = (
                    client.config.authenticated
                    and client.can_create_issues(project)
                )

            envelope = success_response(test_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
