"""Config commands: config group (show/sample/test)."""

import sys
from typing import Any

import click

from ..client import JiraClient
from ..config import (
    DEFAULT_CONFIG_PATH,
    config_file_path,
    create_sample_config,
    load_config,
)
from ..envelope import success_response
from ..errors import AuthError, ConfigError, ExitCode, JiraToolError, NotFoundError
from ._helpers import (
    get_client,
    handle_error,
    output_result,
)


def _permissions_report(
    client: JiraClient, project: str | None
) -> dict[str, Any]:
    """Granted and denied permissions, in *project* or every visible one.

    Projects with the same permissions share an entry: a Cloud site can
    have a hundred projects and a handful of permission schemes.
    """
    groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    global_report: dict[str, list[str]] = {}
    errors: dict[str, str] = {}

    def split(entries: dict[str, dict[str, Any]], want_global: bool
              ) -> tuple[list[str], list[str]]:
        granted: list[str] = []
        denied: list[str] = []
        for key in sorted(entries):
            if (entries[key].get("type") == "GLOBAL") == want_global:
                have = entries[key].get("havePermission")
                (granted if have else denied).append(key)
        return granted, denied

    for key in [project] if project else client.visible_project_keys():
        try:
            entries = client.get_my_permissions(key)
        except (NotFoundError, AuthError) as e:
            if project or e.http_status == 401:
                raise
            errors[key] = e.message
            continue
        if not global_report:
            granted, denied = split(entries, True)
            global_report = {"granted": granted, "denied": denied}
        granted, denied = split(entries, False)
        groups.setdefault((tuple(granted), tuple(denied)), []).append(key)

    report: dict[str, Any] = {
        "global": global_report,
        "projects": [
            {"keys": sorted(keys), "granted": list(granted),
             "denied": list(denied)}
            for (granted, denied), keys in sorted(
                groups.items(), key=lambda item: (-len(item[1]), item[1]))
        ],
    }
    if errors:
        report["errors"] = errors
    return report


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
    @click.option(
        "--permissions", is_flag=True,
        help="Also list your permissions, in --project or in every project "
             "you can see (one request each)",
    )
    @click.pass_context
    def config_test(
        ctx: click.Context, project: str | None, permissions: bool
    ) -> None:
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
            report = None
            if permissions and client.config.authenticated:
                report = _permissions_report(client, project)
            if project:
                test_data["project"] = project
                if report:
                    can_create = "CREATE_ISSUES" in report["projects"][0]["granted"]
                else:
                    can_create = (
                        client.config.authenticated
                        and client.can_create_issues(project)
                    )
                test_data["can_create"] = can_create
            if permissions:
                test_data["permissions"] = report

            envelope = success_response(test_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
