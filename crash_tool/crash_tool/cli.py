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

from .session import CommandResult, SessionResult, run_drgn_triage, run_session

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

    Two backends, one CLI:

    \b
    drgn (via lustre-drgn-tools) -- PREFERRED:
      Programmatic access to all kernel data structures with
      full type information. Used for Lustre analysis (OBD
      devices, LDLM locks, dk log, RPC queues, OSC stats)
      and capable of any generic kernel analysis too. drgn
      has built-in helpers for tasks, stacks, memory, slab
      caches, block devices, dmesg, and more.
      Used by: 'recipes lustre'

    \b
    crash binary (Red Hat crash utility) -- LEGACY:
      Interactive crash dump tool with built-in commands.
      The overview/backtrace/memory/io recipes still use it,
      but everything they do can also be done with drgn.
      Useful for ad-hoc one-off queries if you already know
      crash commands.
      Used by: 'run', 'script', other recipes

    Start with 'recipes lustre' for Lustre problems.
    The crash-binary recipes remain available but are not
    required -- drgn is strictly more capable.
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
@click.option("--crash-bin", default=None, help="Path to crash binary.")
@click.option("--mod-dir", default=None, help="Directory with .ko files to load via 'mod -S'.")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
@click.pass_context
def recipes(
    ctx: click.Context,
    recipe: str | None,
    vmlinux: str | None,
    vmcore: str | None,
    timeout: int,
    crash_bin: str | None,
    mod_dir: str | None,
    pretty: bool,
) -> None:
    """Run a pre-built analysis recipe.

    Without arguments, lists available recipes.  With a recipe
    name, runs that recipe's commands and returns results.

    Recipes:

        overview    - System info, uptime, panic message, and task summary
        backtrace   - All CPU backtraces and panic task detail
        memory      - Memory usage, slab info, and VM stats
        lustre      - Lustre state (requires --mod-dir with Lustre .ko files)
        io          - Block I/O state and hung task detection
    """
    pretty = pretty or ctx.obj.get("pretty", False)
    full_env = ctx.obj.get("envelope", False)

    available = _get_recipes()

    if recipe is None:
        # List recipes
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
    commands = recipe_def["commands"]
    needs_mods = recipe_def.get("needs_modules", False)
    use_drgn = recipe_def.get("use_drgn", False)

    if needs_mods and not mod_dir:
        envelope = error_response_from_dict(
            code="INVALID_INPUT",
            message=f"Recipe '{recipe}' requires --mod-dir to load Lustre module symbols",
            tool=TOOL_NAME,
            command="recipes",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(2)

    if use_drgn and not (vmcore and vmlinux):
        envelope = error_response_from_dict(
            code="INVALID_INPUT",
            message=f"Recipe '{recipe}' requires --vmcore and --vmlinux",
            tool=TOOL_NAME,
            command="recipes",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(2)

    drgn_only = recipe_def.get("drgn_only", False)

    # For drgn-only recipes, skip crash entirely
    if drgn_only:
        drgn_result = run_drgn_triage(
            vmcore=vmcore,
            vmlinux=vmlinux,
            mod_dir=mod_dir,
            timeout=timeout,
        )
        data = {
            "recipe": recipe,
            "description": recipe_def["description"],
            **drgn_result,
        }
        envelope = success_response(data, tool=TOOL_NAME, command="recipes")
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        return

    try:
        sr = run_session(
            commands=commands,
            vmlinux=vmlinux,
            vmcore=vmcore,
            crash_binary=crash_bin,
            timeout=timeout,
            mod_dir=mod_dir if needs_mods else None,
        )
    except Exception as e:
        envelope = error_response_from_dict(
            code="CRASH_ERROR",
            message=str(e),
            tool=TOOL_NAME,
            command="recipes",
        )
        click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
        sys.exit(1)

    data = {
        "recipe": recipe,
        "description": recipe_def["description"],
        **_format_session(sr),
    }

    # Run drgn triage for recipes that request it
    if use_drgn:
        drgn_result = run_drgn_triage(
            vmcore=vmcore,
            vmlinux=vmlinux,
            mod_dir=mod_dir,
            timeout=timeout,
        )
        data["drgn_triage"] = drgn_result

    envelope = success_response(data, tool=TOOL_NAME, command="recipes")
    click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))


def _get_recipes() -> dict[str, dict[str, Any]]:
    """Return the built-in recipe definitions."""
    return {
        "overview": {
            "description": "System info, uptime, panic message, and task summary",
            "commands": [
                "sys",
                "bt",
                "log -T | tail -50",
                "ps -m | head -40",
            ],
        },
        "backtrace": {
            "description": "All CPU backtraces and panic task detail",
            "commands": [
                "bt -a",
                "bt -f",
            ],
        },
        "memory": {
            "description": "Memory usage, slab info, and VM stats",
            "commands": [
                "kmem -i",
                "kmem -s | head -60",
            ],
        },
        "lustre": {
            "description": "Lustre triage via drgn (requires --mod-dir)",
            "needs_modules": True,
            "use_drgn": True,
            "drgn_only": True,
            "commands": [],
            # drgn triage provides everything: overview, backtrace,
            # OBD devices, LDLM locks, dk log, kernel log, RPCs,
            # stack trace grouping, D-state analysis, OSC stats,
            # and diagnosis hints.
        },
        "io": {
            "description": "Block I/O state and hung task detection",
            "commands": [
                "dev -d",
                'ps | grep " UN "',
                "foreach UN bt",
            ],
        },
    }
