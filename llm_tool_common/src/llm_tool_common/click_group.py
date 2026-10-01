"""Click groups that keep failures inside the JSON output contract.

Click reports a usage error -- an unknown command or option, a missing
or malformed argument -- as text on stderr with exit status 2, which
the contract reserves for an authentication failure, and an exception
a command does not handle as a traceback.
"""

from typing import Any, NoReturn

import click
from click.exceptions import NoArgsIsHelpError

from .decorators import error_from_exception
from .envelope import error_response_from_dict, format_json
from .errors import ErrorCode, ExitCode


class JsonUsageErrorGroup(click.Group):
    """Report click usage errors as JSON INVALID_INPUT with exit 4.

    A subclass sets ``tool_name`` for the envelope's meta.
    """

    tool_name = "cli"

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        # The group's own options are not parsed when this fails, and
        # parsing consumes the list, so --envelope is looked for first.
        envelope = "--envelope" in args
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as e:
            if isinstance(e, NoArgsIsHelpError):
                e.show()
            _usage_error(e, self.tool_name, "cli", envelope)

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as e:
            _usage_error(
                e,
                self.tool_name,
                ctx.invoked_subcommand or "cli",
                bool(ctx.params.get("envelope")),
            )


class JsonErrorGroup(JsonUsageErrorGroup):
    """Also report any exception a command lets escape as a JSON error.

    It is mapped by error_from_exception, so a ToolError keeps its own
    code and exit status.  Click's exceptions -- help, exit, abort --
    pass through.
    """

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise
        except Exception as e:
            err = error_from_exception(e)
            _emit(
                err.code,
                err.message,
                self.tool_name,
                ctx.invoked_subcommand or "cli",
                bool(ctx.params.get("envelope")),
                err.exit_code,
            )


def _usage_error(
    error: click.UsageError, tool: str, command: str, envelope: bool
) -> NoReturn:
    if isinstance(error, NoArgsIsHelpError):
        message = "Missing command."
    else:
        message = error.format_message()
    details = None
    if error.ctx is not None:
        details = {
            "usage": error.ctx.get_usage(),
            "help": f"{error.ctx.command_path} --help",
        }
    _emit(
        ErrorCode.INVALID_INPUT, message, tool, command, envelope,
        ExitCode.INVALID_INPUT, details,
    )


def _emit(
    code: str,
    message: str,
    tool: str,
    command: str,
    envelope: bool,
    exit_code: int,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    env = error_response_from_dict(code, message, tool, command, details=details)
    click.echo(format_json(env, full_envelope=envelope))
    raise click.exceptions.Exit(int(exit_code))
