"""pw-cli: ask Patch Watcher what tagging it on a Gerrit change asks.

Patch Watcher's /tagger/v1/ routes take a Gerrit username and HTTP password,
check them with Gerrit, and then do what tagging the bot on the change would
do for that account, as Patch Watcher's roles allow it: set the change's
level (retest and shepherd for anyone), stop, halt or release it, find
reviewers, run an AI review, start a request run, or pass on feedback.
Backlog work is not offered.  The login is gerrit-cli's, from
~/.config/gerrit-cli/.env (or GERRIT_USER and GERRIT_PASS), and --user picks
another of its credential sets.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any
from urllib.parse import urlparse

import click
import requests
from llm_tool_common.click_group import JsonErrorGroup
from llm_tool_common.config import (
    CredentialSetError,
    apply_credential_set,
    hoist_args,
    load_env_files,
)
from llm_tool_common.envelope import format_json, success_response
from llm_tool_common.errors import ErrorCode, ExitCode, ToolError
from llm_tool_common.update_check import maybe_check_for_updates
from llm_tool_common.version import version_option

TOOL_NAME = "pw-cli"
DEFAULT_URL = "https://patchwatcher.mulberrytree.us"
URL_ENV = "PW_CLI_URL"
TIMEOUT = 60

# A bare number, or a Gerrit URL in any of its usual forms.
_CHANGE_RES = (
    re.compile(r"\s*([1-9][0-9]{0,11})\s*"),
    re.compile(r"https?://[^/]+/(?:c/)?(?:.*/\+/)?([1-9][0-9]{0,11})(?:/[0-9]*)?/?(?:[?#].*)?"),
    re.compile(r"https?://[^/]+/#/c/(?:.*/\+/)?([1-9][0-9]{0,11})(?:/[0-9]*)?/?"),
)

# Errors that belong to one change, so a list of changes carries on past them.
_PER_CHANGE = frozenset({"NOT_FOUND", "NOT_AUTHORIZED", "CONFLICT", "INVALID_INPUT", "RATE_LIMITED"})
_EXIT = {
    "AUTH_FAILED": ExitCode.AUTH_ERROR, "AUTH_MISSING": ExitCode.AUTH_ERROR,
    "NOT_FOUND": ExitCode.NOT_FOUND, "INVALID_INPUT": ExitCode.INVALID_INPUT,
    "CONNECTION_ERROR": ExitCode.NETWORK_ERROR, "TIMEOUT": ExitCode.NETWORK_ERROR,
}

_FULL_ENVELOPE = False


def _fail(code: str, message: str, **details: Any) -> ToolError:
    return ToolError(
        code, message, details={k: v for k, v in details.items() if v is not None},
        exit_code=_EXIT.get(code, ExitCode.GENERAL_ERROR),
    )


def parse_change(value: str) -> int:
    for pattern in _CHANGE_RES:
        match = pattern.fullmatch(value)
        if match:
            return int(match.group(1))
    raise _fail(ErrorCode.INVALID_INPUT, f"Not a Gerrit change number or URL: {value!r}")


def base_url() -> str:
    url = (os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
    parsed = urlparse(url)
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise _fail(
            ErrorCode.CONFIG_ERROR,
            f"{URL_ENV} must be an https URL: the Gerrit password goes with every request.",
        )
    return url


def credentials() -> tuple[str, str]:
    user, password = os.environ.get("GERRIT_USER"), os.environ.get("GERRIT_PASS")
    if not user or not password:
        raise _fail(
            ErrorCode.AUTH_MISSING,
            "Set GERRIT_USER and GERRIT_PASS, or configure gerrit-cli "
            "(~/.config/gerrit-cli/.env). The password is the HTTP one, from "
            "Gerrit's Settings > HTTP Credentials.",
        )
    return user, password


def _server_error(status: int, payload: Any) -> ToolError:
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or f"Patch Watcher answered HTTP {status}.")
    server_code = error.get("code")
    if status == 401:
        code = ErrorCode.AUTH_FAILED
    elif status == 403:
        code = "NOT_AUTHORIZED"
    elif status == 404:
        code = ErrorCode.NOT_FOUND
    elif status in (400, 413, 415):
        code = ErrorCode.INVALID_INPUT
    elif status == 409:
        code = "CONFLICT"
    elif status == 429:
        code = "RATE_LIMITED"
    elif 300 <= status < 400:
        code = ErrorCode.SERVER_ERROR
        message = (
            f"Patch Watcher answered HTTP {status} (a redirect, probably to a login "
            "page): the server is not open to Gerrit logins at this URL."
        )
    else:
        code = ErrorCode.SERVER_ERROR
    return _fail(code, message, http_status=status, server_code=server_code,
                 server_details=error.get("details"))


def call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{base_url()}/tagger/v1/{path}"
    try:
        response = requests.request(
            method, url, auth=credentials(), json=body if method == "POST" else None,
            timeout=TIMEOUT, allow_redirects=False,
            headers={"Accept": "application/json"},
        )
    except requests.Timeout as exc:
        raise _fail(ErrorCode.TIMEOUT, f"Patch Watcher did not answer in {TIMEOUT}s.") from exc
    except requests.RequestException as exc:
        raise _fail(ErrorCode.CONNECTION_ERROR, f"Could not reach Patch Watcher: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200 or not isinstance(payload, dict):
        raise _server_error(response.status_code, payload)
    return payload


def _emit(data: Any, command: str) -> None:
    click.echo(format_json(success_response(data, TOOL_NAME, command), full_envelope=_FULL_ENVELOPE))


def _each(changes: tuple[str, ...], command: str, act) -> None:
    """Act on each change; one change's refusal does not stop the rest."""
    numbers = [parse_change(value) for value in changes]
    results, failed = [], None
    for number in numbers:
        try:
            results.append(act(number))
        except ToolError as exc:
            if exc.code not in _PER_CHANGE or len(numbers) == 1:
                raise
            results.append({"change": number, "error": exc.to_dict()})
            failed = failed or exc
    _emit(results, command)
    if failed is not None:
        sys.exit(int(failed.exit_code))


class RetestGroup(JsonErrorGroup):
    """Global options work in any position."""

    tool_name = TOOL_NAME

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        return super().parse_args(
            ctx, hoist_args(args, flags=("--envelope",), options=("--user", "-U"))
        )


@click.group(cls=RetestGroup)
@version_option("pw-cli")
@click.option("--envelope", is_flag=True, help="Include full response envelope (ok/data/meta wrapper)")
@click.option(
    "--user", "-U", default=None,
    help="gerrit-cli credential set to use: a [section] alias or a GERRIT_USER "
         "from ~/.config/gerrit-cli/.env",
)
def main(envelope: bool, user: str | None) -> None:
    """Ask Patch Watcher, as your Gerrit account, what tagging it on a change asks.

    CHANGE is a change number or a Gerrit URL.  Output is JSON.  What you may
    do is what your tags may: `whoami` says.
    """
    maybe_check_for_updates()
    global _FULL_ENVELOPE
    _FULL_ENVELOPE = envelope
    try:
        load_env_files("gerrit-cli")
        if user:
            apply_credential_set("gerrit-cli", user)
    except (CredentialSetError, FileNotFoundError) as exc:
        raise _fail(ErrorCode.CONFIG_ERROR, str(exc)) from exc


@main.command()
def whoami() -> None:
    """The Gerrit account Patch Watcher sees, and what it may do."""
    _emit(call("GET", "whoami"), "whoami")


@main.command()
@click.argument("changes", nargs=-1, required=True)
def status(changes: tuple[str, ...]) -> None:
    """Whether each change is watched, at what level, and any halt."""
    _each(changes, "status", lambda number: call("GET", f"changes/{number}"))


@main.command()
@click.argument("changes", nargs=-1, required=True)
def retest(changes: tuple[str, ...]) -> None:
    """Watch each change at retest: retest CI failures known bugs cover, triage the rest."""
    _each(changes, "retest", lambda number: call(
        "POST", f"changes/{number}/watch", {"level": "retest"},
    ))


@main.command()
@click.argument("changes", nargs=-1, required=True)
def shepherd(changes: tuple[str, ...]) -> None:
    """Watch each change at shepherd: retest, and once CI passes find and ping reviewers."""
    _each(changes, "shepherd", lambda number: call(
        "POST", f"changes/{number}/watch", {"level": "shepherd"},
    ))


@main.command()
@click.argument("changes", nargs=-1, required=True)
@click.option(
    "--level", "-l", required=True,
    help="retest, shepherd, smallfix, bigfix or own; whoami lists the ones you may set.",
)
def watch(changes: tuple[str, ...], level: str) -> None:
    """Watch each change at LEVEL, or move it to LEVEL."""
    _each(changes, "watch", lambda number: call(
        "POST", f"changes/{number}/watch", {"level": level},
    ))


@main.command()
@click.argument("changes", nargs=-1, required=True)
def stop(changes: tuple[str, ...]) -> None:
    """Stop watching each change, and stop any run working on it."""
    _each(changes, "stop", lambda number: call("POST", f"changes/{number}/unwatch", {}))


@main.command()
@click.argument("change")
@click.argument("reason", nargs=-1, required=True)
def halt(change: str, reason: tuple[str, ...]) -> None:
    """Halt a change you had watched until you release it; REASON says why."""
    number = parse_change(change)
    _emit(call("POST", f"changes/{number}/halt", {"reason": " ".join(reason)}), "halt")


@main.command("clear-halt")
@click.argument("change")
@click.option("--note", default=None, help="Why the halt can go.")
def clear_halt(change: str, note: str | None) -> None:
    """Release a halt you set on a change."""
    number = parse_change(change)
    body = {"note": note} if note else {}
    _emit(call("POST", f"changes/{number}/clear-halt", body), "clear-halt")



def _text(words: tuple[str, ...]) -> str:
    """The words given, or standard input for a lone "-"."""
    if words == ("-",):
        return click.get_text_stream("stdin").read()
    return " ".join(words)


@main.command()
@click.argument("changes", nargs=-1, required=True)
def reviewers(changes: tuple[str, ...]) -> None:
    """Have the bot find and add reviewers to each change, once."""
    _each(changes, "reviewers", lambda number: call("POST", f"changes/{number}/reviewers", {}))


@main.command()
@click.argument("changes", nargs=-1, required=True)
@click.option("--light", is_flag=True, help="A quicker single pass.")
def lreview(changes: tuple[str, ...], light: bool) -> None:
    """Run an AI code review of each change's current patchset, posted on it."""
    body = {"mode": "light" if light else "full"}
    _each(changes, "lreview", lambda number: call("POST", f"changes/{number}/lreview", body))


@main.command()
@click.argument("changes", nargs=-1, required=True)
def rebase(changes: tuple[str, ...]) -> None:
    """Have the bot rebase each change onto master, once. Anyone may ask,
    on any change; it counts toward your 10 requests a day."""
    _each(changes, "rebase", lambda number: call("POST", f"changes/{number}/rebase", {}))


@main.command()
@click.argument("change")
@click.argument("text", nargs=-1, required=True)
def request(change: str, text: tuple[str, ...]) -> None:
    """Start one run on CHANGE that does what TEXT asks ("-" reads it from stdin).

    The request is posted on the change, and the run answers it there.
    """
    number = parse_change(change)
    _emit(call("POST", f"changes/{number}/request", {"text": _text(text)}), "request")


@main.command()
@click.argument("change")
@click.argument("text", nargs=-1, required=True)
def feedback(change: str, text: tuple[str, ...]) -> None:
    """Pass a note about the bot's work on CHANGE to its operators ("-" reads stdin)."""
    number = parse_change(change)
    _emit(call("POST", f"changes/{number}/feedback", {"text": _text(text)}), "feedback")


if __name__ == "__main__":
    main()
