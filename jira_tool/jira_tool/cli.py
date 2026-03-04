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


# ── Hoistable-flag support ──────────────────────────────────────────
_HOISTABLE_FLAGS = {"--pretty", "--debug"}


class JsonErrorGroup(click.Group):
    """Click group that wraps usage errors in JSON envelope and hoists global flags.

    When an LLM passes invalid arguments, Click normally prints a
    human-readable error to stderr and exits. This subclass catches
    those errors and outputs a structured JSON error envelope to stdout
    instead, maintaining the tool's JSON-only contract.

    Additionally, --pretty and --debug are extracted from anywhere in
    the argument list so they work in any position.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Pull hoistable flags out of wherever they appear and
        # inject them at the front so Click's group-level parser sees them.
        hoisted = []
        remaining = []
        for arg in args:
            if arg in _HOISTABLE_FLAGS:
                hoisted.append(arg)
            else:
                remaining.append(arg)
        return super().parse_args(ctx, hoisted + remaining)

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
            click.echo(format_json(envelope, pretty=pretty))
            ctx.exit(ExitCode.INVALID_INPUT)


# ── Main group ──────────────────────────────────────────────────────

@click.group(cls=JsonErrorGroup)
@click.option("--server", envvar="JIRA_SERVER", help="JIRA server URL")
@click.option("--token", envvar="JIRA_TOKEN", help="JIRA API token")
@click.option("--config", "config_path", type=click.Path(), help="Config file path")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@click.option("--debug", is_flag=True, help="Enable debug output to stderr")
@click.pass_context
def main(
    ctx: click.Context, server: str | None, token: str | None, config_path: str | None, pretty: bool, debug: bool
) -> None:
    """
    JIRA CLI tool for LLM agents.

    All commands output JSON to stdout. Use --pretty for human-readable output.
    Run 'jira describe' for machine-readable API documentation.

    Configuration priority:
    1. Command-line options (--server, --token)
    2. Environment variables (JIRA_SERVER, JIRA_TOKEN)
    3. Config file (~/.jira-tool.json)
    """
    ctx.ensure_object(dict)
    ctx.obj["pretty"] = pretty
    ctx.obj["debug"] = debug
    ctx.obj["server_override"] = server
    ctx.obj["token_override"] = token
    ctx.obj["config_path"] = config_path


# ── Register all command modules ────────────────────────────────────
from .commands import register_all  # noqa: E402

register_all(main)


if __name__ == "__main__":
    main()
