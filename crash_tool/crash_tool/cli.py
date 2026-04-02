"""CLI entry point for crash-tool.

Provides non-interactive, LLM-friendly access to the crash utility
for kernel crash dump analysis.
"""

import json
import sys
from typing import Any

import click

from llm_tool_common import (
    error_response_from_dict,
    format_json,
    success_response,
)

from .session import (
    CommandResult,
    SessionResult,
    run_drgn_kernel_triage,
    run_drgn_triage,
    run_session,
)

TOOL_NAME = "crash-tool"


def _format_command_result(cr: CommandResult) -> dict[str, Any]:
    """Convert a CommandResult to a JSON-friendly dict."""
    d: dict[str, Any] = {
        "command": cr.command,
        "output": cr.output,
    }
    if cr.error:
        d["error"] = True
        d["error_message"] = cr.error_message
    return d


def _format_session(sr: SessionResult) -> dict[str, Any]:
    """Convert a SessionResult to a JSON-friendly dict."""
    d: dict[str, Any] = {
        "commands": [_format_command_result(c) for c in sr.commands],
        "return_code": sr.return_code,
    }
    if sr.init_output:
        d["init_output"] = sr.init_output
    if sr.crash_stderr:
        d["stderr"] = sr.crash_stderr
    return d


# ── CLI ───────────────────────────────────────────────────────────


class CrashGroup(click.Group):
    """Click group with JSON error wrapping."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as e:
            pretty = ctx.params.get("pretty", False)
            envelope = error_response_from_dict(
                code="INVALID_INPUT",
                message=str(e),
                tool=TOOL_NAME,
                command="cli",
            )
            full_env = ctx.params.get("envelope", False)
            click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
            ctx.exit(2)


@click.group(cls=CrashGroup)
@click.version_option(package_name="crash-tool", prog_name="crash-tool")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
@click.option("--envelope", is_flag=True, help="Wrap output in full envelope.")
@click.pass_context
def main(ctx: click.Context, pretty: bool, envelope: bool) -> None:
    """Non-interactive crash dump analysis for LLM agents.

    All recipes use drgn for structured, typed kernel analysis.

    \b
    Subcommands:
      recipes   Run pre-built analyses (overview, backtrace,
                memory, io, lustre). All use drgn.
      run       Send arbitrary commands to the crash binary
                (legacy, for ad-hoc queries).
      script    Run crash commands from a file (legacy).

    Start with 'recipes lustre' for Lustre problems, or
    'recipes overview' for generic kernel analysis.
    """
    ctx.ensure_object(dict)
    ctx.obj["pretty"] = pretty
    ctx.obj["envelope"] = envelope


@main.command()
@click.argument("commands", nargs=-1, required=True)
@click.option("--vmlinux", default=None, help="Path to vmlinux debug kernel.")
@click.option("--vmcore", default=None, help="Path to vmcore dump file.")
@click.option("--timeout", default=120, type=int, help="Session timeout in seconds.")
@click.option("--minimal", is_flag=True, help="Use --minimal mode (faster init).")
@click.option("--crash-bin", default=None, help="Path to crash binary.")
@click.option("--mod-dir", default=None, help="Directory with .ko files to load via 'mod -S'.")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
@click.pass_context
def run(
    ctx: click.Context,
    commands: tuple[str, ...],
    vmlinux: str | None,
    vmcore: str | None,
    timeout: int,
    minimal: bool,
    crash_bin: str | None,
    mod_dir: str | None,
    pretty: bool,
) -> None:
    """Run one or more crash commands and return structured output.

    Each command is run in sequence within a single crash session.
    Output is returned as JSON with per-command results.

    Examples:

        crash-tool run "bt -a" "log" --vmcore /var/crash/vmcore

        crash-tool run "ps" "files 1234" --vmlinux /boot/vmlinux

        crash-tool run --mod-dir /path/to/lustre/kos "sym obd_devs"
    """
    pretty = pretty or ctx.obj.get("pretty", False)
    full_env = ctx.obj.get("envelope", False)

    try:
        sr = run_session(
            commands=list(commands),
            vmlinux=vmlinux,
            vmcore=vmcore,
            crash_binary=crash_bin,
            timeout=timeout,
            minimal=minimal,
            mod_dir=mod_dir,
        )
    except FileNotFoundError as e:
        envelope = error_response_from_dict(
            code="NOT_FOUND",
            message=str(e),
            tool=TOOL_NAME,
            command="run",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(1)
    except Exception as e:
        envelope = error_response_from_dict(
            code="CRASH_ERROR",
            message=str(e),
            tool=TOOL_NAME,
            command="run",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(1)

    data = _format_session(sr)
    envelope = success_response(data, tool=TOOL_NAME, command="run")
    click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))

    if sr.return_code != 0:
        sys.exit(1)


@main.command()
@click.argument("script_file", type=click.Path(exists=True))
@click.option("--vmlinux", default=None, help="Path to vmlinux debug kernel.")
@click.option("--vmcore", default=None, help="Path to vmcore dump file.")
@click.option("--timeout", default=120, type=int, help="Session timeout in seconds.")
@click.option("--minimal", is_flag=True, help="Use --minimal mode (faster init).")
@click.option("--crash-bin", default=None, help="Path to crash binary.")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
@click.pass_context
def script(
    ctx: click.Context,
    script_file: str,
    vmlinux: str | None,
    vmcore: str | None,
    timeout: int,
    minimal: bool,
    crash_bin: str | None,
    pretty: bool,
) -> None:
    """Run commands from a file and return structured output.

    Reads one command per line from SCRIPT_FILE.  Blank lines and
    lines starting with # are skipped.
    """
    pretty = pretty or ctx.obj.get("pretty", False)
    full_env = ctx.obj.get("envelope", False)

    with open(script_file) as f:
        commands = [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

    if not commands:
        envelope = error_response_from_dict(
            code="INVALID_INPUT",
            message="Script file contains no commands",
            tool=TOOL_NAME,
            command="script",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(2)

    try:
        sr = run_session(
            commands=commands,
            vmlinux=vmlinux,
            vmcore=vmcore,
            crash_binary=crash_bin,
            timeout=timeout,
            minimal=minimal,
        )
    except Exception as e:
        envelope = error_response_from_dict(
            code="CRASH_ERROR",
            message=str(e),
            tool=TOOL_NAME,
            command="script",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(1)

    data = _format_session(sr)
    envelope = success_response(data, tool=TOOL_NAME, command="script")
    click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))


@main.command(name="recipes")
@click.argument("recipe", required=False, default=None)
@click.option("--vmlinux", default=None, help="Path to vmlinux debug kernel.")
@click.option("--vmcore", default=None, help="Path to vmcore dump file.")
@click.option("--timeout", default=300, type=int, help="Session timeout in seconds.")
@click.option("--mod-dir", default=None, help="Directory with Lustre .ko files.")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
@click.pass_context
def recipes(
    ctx: click.Context,
    recipe: str | None,
    vmlinux: str | None,
    vmcore: str | None,
    timeout: int,
    mod_dir: str | None,
    pretty: bool,
) -> None:
    """Run a pre-built analysis recipe.  All recipes use drgn.

    Without arguments, lists available recipes.  With a recipe
    name, runs that recipe and returns structured JSON results.

    \b
    Recipes:
        overview    System info, uptime, panic message, task summary
        backtrace   All CPU backtraces and panic task detail
        memory      Memory usage and slab cache stats
        io          Block devices and D-state (hung) tasks
        lustre      Full Lustre triage (requires --mod-dir)
    """
    pretty = pretty or ctx.obj.get("pretty", False)
    full_env = ctx.obj.get("envelope", False)

    available = _get_recipes()

    if recipe is None:
        data = {
            "recipes": {
                name: info["description"]
                for name, info in available.items()
            }
        }
        envelope = success_response(data, tool=TOOL_NAME, command="recipes")
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        return

    if recipe not in available:
        envelope = error_response_from_dict(
            code="NOT_FOUND",
            message=f"Unknown recipe: {recipe}. Available: {', '.join(available.keys())}",
            tool=TOOL_NAME,
            command="recipes",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(2)

    recipe_def = available[recipe]

    if not (vmcore and vmlinux):
        envelope = error_response_from_dict(
            code="INVALID_INPUT",
            message=f"Recipe '{recipe}' requires --vmcore and --vmlinux",
            tool=TOOL_NAME,
            command="recipes",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(2)

    if recipe_def.get("needs_modules") and not mod_dir:
        envelope = error_response_from_dict(
            code="INVALID_INPUT",
            message=f"Recipe '{recipe}' requires --mod-dir for Lustre module symbols",
            tool=TOOL_NAME,
            command="recipes",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(2)

    # All recipes use drgn
    if recipe == "lustre":
        drgn_result = run_drgn_triage(
            vmcore=vmcore,
            vmlinux=vmlinux,
            mod_dir=mod_dir,
            timeout=timeout,
        )
    else:
        drgn_result = run_drgn_kernel_triage(
            vmcore=vmcore,
            vmlinux=vmlinux,
            analyses=recipe_def["analyses"],
            timeout=timeout,
        )

    data = {
        "recipe": recipe,
        "description": recipe_def["description"],
        **drgn_result,
    }
    envelope = success_response(data, tool=TOOL_NAME, command="recipes")
    click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))


def _get_recipes() -> dict[str, dict[str, Any]]:
    """Return the built-in recipe definitions."""
    return {
        "overview": {
            "description": "System info, uptime, panic message, and task summary",
            "analyses": ["overview", "dmesg"],
        },
        "backtrace": {
            "description": "All CPU backtraces and panic task detail",
            "analyses": ["backtrace"],
        },
        "memory": {
            "description": "Memory usage and slab cache stats",
            "analyses": ["memory"],
        },
        "io": {
            "description": "Block devices and D-state (hung) tasks",
            "analyses": ["io"],
        },
        "lustre": {
            "description": "Full Lustre triage via drgn (requires --mod-dir)",
            "needs_modules": True,
            "analyses": [],  # handled separately via lustre_triage.py
        },
    }
