"""CLI entry point for JIRA tool.

Commands are organized into modules under ``commands/``.  This file
defines the top-level Click group (``main``) and registers every
command module via :func:`commands.register_all`.

For backward compatibility the helper / normalizer functions that
tests and other code import from ``jira_tool.cli`` are re-exported
here.
"""

from typing import Any

import click

from llm_tool_common.config import (
    CredentialSetError,
    apply_credential_set,
    hoist_args,
)

from .envelope import error_response_from_dict, format_json
from .errors import ErrorCode, ExitCode

# ── Re-exports (backward compat) ────────────────────────────────────
# Tests and external code do ``from jira_tool.cli import extract_field, ...``
from .commands._helpers import (  # noqa: F401 – re-exported
    ISSUE_KEY_PATTERN,
    _normalize_attachment,
    _normalize_comment,
    _normalize_comments,
    _normalize_issue,
    _parse_visibility,
    extract_field,
    extract_issue_key,
    get_client,
    handle_error,
    output_field,
    output_result,
    pass_config,
)


# ── --user support ──────────────────────────────────────────────────

def _instance_named(user: str, config_path: str | None) -> str | None:
    """The ~/.jira-tool.json instance that --user names, or None.

    Matches the instance key, then the email inside it.  A Server
    instance authenticates with a token alone and has no other name, so
    its key is the only way to reach it.
    """
    import json
    import os
    from pathlib import Path

    from .config import CONFIG_PATH_VARIABLE, DEFAULT_CONFIG_PATH

    if config_path:
        path = Path(config_path)
    else:
        path = Path(os.environ.get(CONFIG_PATH_VARIABLE) or DEFAULT_CONFIG_PATH)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    instances = data.get("instances")
    if not isinstance(instances, dict):
        return None

    wanted = user.strip().lower()
    for name in instances:
        if name.lower() == wanted:
            return name
    for name, values in instances.items():
        if not isinstance(values, dict):
            continue
        email = (values.get("auth") or {}).get("email", "")
        if email and email.strip().lower() == wanted:
            return name
    return None


def _fail_config(
    ctx: click.Context, message: str, pretty: bool, envelope: bool
) -> None:
    """Report an unusable --user and stop, in the tool's JSON contract."""
    env = error_response_from_dict(
        code=ErrorCode.CONFIG_ERROR, message=message, command="cli"
    )
    click.echo(format_json(env, pretty=pretty, full_envelope=envelope))
    ctx.exit(ExitCode.GENERAL_ERROR)


# ── Hoistable-flag support ──────────────────────────────────────────
_HOISTABLE_FLAGS = {"--pretty", "--debug", "--envelope"}
# Options that take a value and should be hoisted along with it
# --user is deliberately NOT hoisted: `jira watch LU-1 --user alice` already
# means "add alice as a watcher", and moving that to the group would quietly
# turn a watcher into a credential choice.  Written before the subcommand it
# reaches the group; -U is unambiguous and hoists from anywhere.
_HOISTABLE_OPTIONS = {"--instance", "-I", "-U"}


class JsonErrorGroup(click.Group):
    """Click group that wraps usage errors in JSON envelope and hoists global flags.

    When an LLM passes invalid arguments, Click normally prints a
    human-readable error to stderr and exits. This subclass catches
    those errors and outputs a structured JSON error envelope to stdout
    instead, maintaining the tool's JSON-only contract.

    Additionally, --pretty, --debug, and --envelope are extracted from
    anywhere in the argument list so they work in any position.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Pull hoistable flags and options out of wherever they appear
        # and inject them at the front so Click's group-level parser sees them.
        return super().parse_args(
            ctx,
            hoist_args(
                args,
                flags=tuple(_HOISTABLE_FLAGS),
                options=tuple(_HOISTABLE_OPTIONS),
            ),
        )

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as e:
            pretty = ctx.params.get("pretty", False)
            envelope = error_response_from_dict(
                code=ErrorCode.INVALID_INPUT,
                message=str(e),
                command="cli",
                details={"hint": e.format_message()} if hasattr(e, "format_message") else None,
            )
            full_env = ctx.params.get("envelope", False)
            click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
            ctx.exit(ExitCode.INVALID_INPUT)


# ── Main group ──────────────────────────────────────────────────────

@click.group(cls=JsonErrorGroup)
@click.version_option(package_name="jira-tool", prog_name="jira")
@click.option("--server", help="JIRA server URL (overrides config and env)")
@click.option("--token", help="JIRA API token (overrides config and env)")
@click.option("--config", "config_path", type=click.Path(), help="Config file path")
@click.option("--instance", "-I", default=None, help="Named instance from config (e.g., 'cloud')")
@click.option(
    "--user",
    "-U",
    default=None,
    help="Credential set to use: a [section] alias in "
         "~/.config/jira-tool/.env, an instance in ~/.jira-tool.json, or "
         "the JIRA_USER / JIRA_CLOUD_EMAIL inside one",
)
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@click.option("--envelope", is_flag=True, help="Include full response envelope (ok/data/meta wrapper)")
@click.option("--debug", is_flag=True, help="Enable debug output to stderr")
@click.pass_context
def main(
    ctx: click.Context, server: str | None, token: str | None, config_path: str | None,
    instance: str | None, user: str | None, pretty: bool, envelope: bool, debug: bool,
) -> None:
    """
    JIRA CLI tool for LLM agents.

    All commands output JSON data payload to stdout.
    Use --envelope to include the full ok/data/meta wrapper.
    Use --pretty for human-readable indented output.
    Run 'jira describe' for machine-readable API documentation.

    Configuration priority:
    1. Command-line options (--server, --token)
    2. Environment variables (JIRA_SERVER, JIRA_TOKEN)
    3. Config file (~/.jira-tool.json) with optional named instances

    Multi-instance example:
      jira get LU-20002                  # uses default instance
      jira -I cloud get EX-13727         # uses 'cloud' instance
      jira --user exa get LU-20002       # uses the [exa] credential set
    """
    ctx.ensure_object(dict)
    ctx.obj["pretty"] = pretty
    ctx.obj["envelope"] = envelope
    ctx.obj["debug"] = debug
    ctx.obj["server_override"] = server
    ctx.obj["token_override"] = token
    ctx.obj["config_path"] = config_path
    ctx.obj["instance"] = instance

    # --user reaches both credential stores: the .env sections this
    # tool shares with gerrit, maloo and jenkins, and the instances
    # map that is jira's alone.  Falling through to -I keeps one flag
    # working the same way across all four tools.
    if user:
        try:
            apply_credential_set("jira-tool", user)
        except CredentialSetError as env_error:
            instance_name = _instance_named(user, config_path)
            if instance_name is None:
                _fail_config(ctx, str(env_error), pretty, envelope)
            ctx.obj["instance"] = instance_name


# ── Register all command modules ────────────────────────────────────
from .commands import register_all  # noqa: E402

register_all(main)


if __name__ == "__main__":
    main()
