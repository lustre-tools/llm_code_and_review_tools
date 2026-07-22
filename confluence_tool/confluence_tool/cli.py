"""CLI entry point for the Confluence tool.

Defines the top-level Click group and registers every command module.
"""

from typing import Any

import click

from .envelope import error_response_from_dict, format_json
from .errors import ErrorCode, ExitCode

# ── Hoistable-flag support ──────────────────────────────────────────
_HOISTABLE_FLAGS = {"--pretty", "--debug", "--envelope"}
_HOISTABLE_OPTIONS = {"--instance", "-I"}


class JsonErrorGroup(click.Group):
    """Click group that wraps usage errors in a JSON envelope and hoists global flags."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        hoisted: list[str] = []
        remaining: list[str] = []
        skip_next = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg in _HOISTABLE_FLAGS:
                hoisted.append(arg)
            elif arg in _HOISTABLE_OPTIONS:
                hoisted.append(arg)
                if i + 1 < len(args):
                    hoisted.append(args[i + 1])
                    skip_next = True
            elif any(arg.startswith(f"{opt}=") for opt in _HOISTABLE_OPTIONS):
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
            full_env = ctx.params.get("envelope", False)
            click.echo(format_json(envelope, pretty=pretty, full_envelope=full_env))
            ctx.exit(ExitCode.INVALID_INPUT)


@click.group(cls=JsonErrorGroup)
@click.version_option(package_name="confluence-tool", prog_name="confluence")
@click.option("--server", help="Confluence server URL (overrides config and env)")
@click.option("--config", "config_path", type=click.Path(), help="Config file path")
@click.option("--instance", "-I", default=None, help="Named instance from config (e.g., 'cloud', 'whamcloud')")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output")
@click.option("--envelope", is_flag=True, help="Include full response envelope (ok/data/meta wrapper)")
@click.option("--debug", is_flag=True, help="Enable debug output to stderr")
@click.pass_context
def main(
    ctx: click.Context, server: str | None, config_path: str | None,
    instance: str | None, pretty: bool, envelope: bool, debug: bool,
) -> None:
    """Read-only Confluence CLI for LLM agents.

    All commands output a JSON data payload to stdout. Use --envelope for the
    full ok/data/meta wrapper. Run 'confluence describe' for the API surface.

    Multi-instance:
      confluence search 'nodemap'                 # default instance ('cloud')
      confluence -I whamcloud search 'lnet'       # public Lustre community wiki
    """
    ctx.ensure_object(dict)
    ctx.obj["pretty"] = pretty
    ctx.obj["envelope"] = envelope
    ctx.obj["debug"] = debug
    ctx.obj["server_override"] = server
    ctx.obj["config_path"] = config_path
    ctx.obj["instance"] = instance


from .commands import register_all  # noqa: E402

register_all(main)


if __name__ == "__main__":
    main()
