"""Error handling decorators for LLM CLI tool commands.

Provides a decorator that wraps click command functions with
standardized HTTP error handling, eliminating the repeated
try/except/output boilerplate across tools.
"""

import functools
import sys
from typing import Any, Callable

import click
import requests

from .envelope import error_response_from_dict, format_json
from .errors import (
    AuthError,
    ErrorCode,
    ExitCode,
    NetworkError,
    NotFoundError,
    ToolError,
)


def error_from_exception(
    exc: Exception, not_found_msg: str | None = None
) -> ToolError:
    """The contract error, with its exit code, for an escaped exception.

    A ToolError is returned as it is.  An HTTP 404 is NOT_FOUND (exit
    3), 401/403 AUTH_FAILED (2), a connection failure or timeout a
    network error (5); anything else is API_ERROR (1).
    """
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, requests.HTTPError):
        status = (
            exc.response.status_code if exc.response is not None else None
        )
        if status == 404:
            return NotFoundError(
                ErrorCode.NOT_FOUND, not_found_msg or "Resource not found"
            )
        if status in (401, 403):
            return AuthError(
                f"Authentication failed (HTTP {status})", http_status=status
            )
        return ToolError(ErrorCode.API_ERROR, f"HTTP {status}: {exc}")
    if isinstance(exc, requests.ConnectionError):
        return NetworkError(
            ErrorCode.CONNECTION_ERROR, f"Connection failed: {exc}"
        )
    if isinstance(exc, requests.Timeout):
        return NetworkError(ErrorCode.TIMEOUT, f"Request timed out: {exc}")
    return ToolError(ErrorCode.API_ERROR, str(exc))


def handle_errors(
    tool: str,
    command: str,
    not_found_msg: str | None = None,
) -> Callable:
    """Decorator that catches common exceptions and outputs JSON errors.

    Maps the exception with :func:`error_from_exception` and exits
    with the contract's code for it.

    Args:
        tool: Tool name for the error envelope (e.g. "jenkins").
        command: Command name for the error envelope.
        not_found_msg: Custom 404 message. If None, uses a generic one.

    Usage::

        @main.command()
        @click.option("--pretty", is_flag=True)
        @handle_errors("jenkins", "builds", not_found_msg="Job not found")
        def builds(pretty, **kwargs):
            # No try/except needed — errors are caught by decorator
            client = _make_client(...)
            data = client.get_builds(...)
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Extract pretty flag from kwargs or click context
            pretty = kwargs.get("pretty", False)
            # Check for full_envelope flag from click context
            ctx = click.get_current_context(silent=True)
            full_env = False
            if ctx and ctx.parent and ctx.parent.params:
                full_env = ctx.parent.params.get("envelope", False)
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                err = error_from_exception(exc, not_found_msg)
                _emit_error(
                    err.code, err.message, tool, command, pretty, full_env,
                    err.exit_code,
                )
        return wrapper
    return decorator


def _emit_error(
    code: str, message: str, tool: str, command: str, pretty: bool,
    full_envelope: bool = False, exit_code: int = ExitCode.GENERAL_ERROR,
) -> None:
    """Output a JSON error envelope and exit."""
    env = error_response_from_dict(code, message, tool, command)
    click.echo(format_json(env, pretty=pretty, full_envelope=full_envelope))
    sys.exit(exit_code)
