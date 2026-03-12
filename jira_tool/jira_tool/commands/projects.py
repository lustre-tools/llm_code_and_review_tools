"""Project commands: roles, components, set-component, versions, set-fix-version."""

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
    """Register project commands on *main*."""

    @main.command("roles")
    @click.argument("project_key")
    @click.pass_context
    def project_roles(ctx: click.Context, project_key: str) -> None:
        """
        List available roles for a project.

        PROJECT_KEY is the project key (e.g., LU, EX).
        Useful for discovering valid values for --visibility on the comment command.
        """
        command = "roles"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw = client.get_project_roles(project_key)

            # raw is a dict like {"Developers": "https://...role/10001", "Administrators": "..."}
            roles = sorted(raw.keys())

            data = {
                "project_key": project_key,
                "total": len(roles),
                "roles": roles,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("components")
    @click.argument("project_key")
    @click.pass_context
    def project_components(ctx: click.Context, project_key: str) -> None:
        """
        List components for a project.

        PROJECT_KEY is the project key (e.g., LU, EX).
        """
        command = "components"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw = client.get_project_components(project_key)
            components = []
            for c in raw:
                components.append({
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "description": c.get("description"),
                })

            data = {
                "project_key": project_key,
                "total": len(components),
                "components": components,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("set-component")
    @click.argument("key")
    @click.argument("components", nargs=-1, required=True)
    @click.option("--replace", is_flag=True, default=False,
                  help="Replace all existing components instead of adding")
    @click.pass_context
    def issue_set_component(ctx: click.Context, key: str, components: tuple[str, ...], replace: bool) -> None:
        """
        Add components to an issue (keeps existing).

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        COMPONENTS are the component names to add.
        Use --replace to replace all existing components.
        """
        command = "set-component"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            if replace:
                client.set_components(key, list(components))
            else:
                client.add_components(key, list(components))

            data = {
                "issue_key": key,
                "components": list(components),
                "mode": "replace" if replace else "add",
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("remove-component")
    @click.argument("key")
    @click.argument("components", nargs=-1, required=True)
    @click.pass_context
    def issue_remove_component(ctx: click.Context, key: str, components: tuple[str, ...]) -> None:
        """
        Remove components from an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        COMPONENTS are the component names to remove.
        """
        command = "remove-component"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            client.remove_components(key, list(components))

            data = {
                "issue_key": key,
                "components_removed": list(components),
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("versions")
    @click.argument("project_key")
    @click.pass_context
    def project_versions(ctx: click.Context, project_key: str) -> None:
        """
        List versions for a project.

        PROJECT_KEY is the project key (e.g., LU, EX).
        """
        command = "versions"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw = client.get_project_versions(project_key)
            versions = []
            for v in raw:
                versions.append({
                    "id": v.get("id"),
                    "name": v.get("name"),
                    "released": v.get("released", False),
                    "archived": v.get("archived", False),
                    "release_date": v.get("releaseDate"),
                })

            data = {
                "project_key": project_key,
                "total": len(versions),
                "versions": versions,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("set-fix-version")
    @click.argument("key")
    @click.argument("versions", nargs=-1, required=True)
    @click.option("--replace", is_flag=True, default=False,
                  help="Replace all existing fix versions instead of adding")
    @click.pass_context
    def issue_set_fix_version(ctx: click.Context, key: str, versions: tuple[str, ...], replace: bool) -> None:
        """
        Add fix versions to an issue (keeps existing).

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        VERSIONS are the version names to add.
        Use --replace to replace all existing fix versions.
        """
        command = "set-fix-version"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            if replace:
                client.set_fix_versions(key, list(versions))
            else:
                client.add_fix_versions(key, list(versions))

            data = {
                "issue_key": key,
                "fix_versions": list(versions),
                "mode": "replace" if replace else "add",
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("remove-fix-version")
    @click.argument("key")
    @click.argument("versions", nargs=-1, required=True)
    @click.pass_context
    def issue_remove_fix_version(ctx: click.Context, key: str, versions: tuple[str, ...]) -> None:
        """
        Remove fix versions from an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        VERSIONS are the version names to remove.
        """
        command = "remove-fix-version"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            client.remove_fix_versions(key, list(versions))

            data = {
                "issue_key": key,
                "fix_versions_removed": list(versions),
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
