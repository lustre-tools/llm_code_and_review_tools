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


def handle_errors(
    tool: str,
    command: str,
    not_found_msg: str | None = None,
) -> Callable:
    """Decorator that catches common exceptions and outputs JSON errors.

    Handles:
    - requests.HTTPError (404 -> NOT_FOUND, others -> API_ERROR)
    - requests.ConnectionError -> CONNECTION_ERROR
    - requests.Timeout -> TIMEOUT
    - Exception -> API_ERROR

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
            try:
                return func(*args, **kwargs)
            except requests.HTTPError as e:
                status = (
                    e.response.status_code
                    if e.response is not None
                    else None
                )
                if status == 404:
                    msg = not_found_msg or f"Resource not found"
                    _emit_error("NOT_FOUND", msg, tool, command, pretty)
                elif status == 401 or status == 403:
                    _emit_error(
                        "AUTH_FAILED",
                        f"Authentication failed (HTTP {status})",
                        tool, command, pretty,
                    )
                else:
                    _emit_error(
                        "API_ERROR",
                        f"HTTP {status}: {e}",
                        tool, command, pretty,
                    )
            except requests.ConnectionError as e:
                _emit_error(
                    "CONNECTION_ERROR",
                    f"Connection failed: {e}",
                    tool, command, pretty,
                )
            except requests.Timeout as e:
                _emit_error(
                    "TIMEOUT",
                    f"Request timed out: {e}",
                    tool, command, pretty,
                )
            except Exception as exc:
                _emit_error(
                    "API_ERROR", str(exc), tool, command, pretty
                )
        return wrapper
    return decorator


def _emit_error(
    code: str, message: str, tool: str, command: str, pretty: bool
) -> None:
    """Output a JSON error envelope and exit."""
    env = error_response_from_dict(code, message, tool, command)
    click.echo(format_json(env, pretty=pretty))
    sys.exit(1)
