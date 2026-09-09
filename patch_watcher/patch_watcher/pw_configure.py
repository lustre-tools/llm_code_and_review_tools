"""Interactive credential setup for a Patch Watcher host.

This is the replacement for the deleted worker-admission contract: instead of
attesting a sandbox, we make the box correct once and let `pw_doctor` say
whether it still is.

It writes only the private per-tool config files the installed CLIs already
read, at mode 0600, and never overwrites an existing value without being told
to.  Nothing here contacts a network service.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Field:
    key: str
    prompt: str
    secret: bool = False
    default: str = ""
    required: bool = True


@dataclass(frozen=True)
class ToolConfig:
    name: str
    path: Path
    fields: Sequence[Field]
    note: str = ""


def tool_configs(home: Path | None = None) -> tuple[ToolConfig, ...]:
    home = home or Path.home()
    config = home / ".config"
    return (
        ToolConfig(
            name="gerrit",
            path=config / "gerrit-cli" / ".env",
            note="Generate the HTTP password in Gerrit under Settings -> HTTP Credentials.",
            fields=(
                Field("GERRIT_URL", "Gerrit URL", default="https://review.whamcloud.com"),
                Field("GERRIT_USER", "Gerrit username"),
                Field("GERRIT_PASS", "Gerrit HTTP password", secret=True),
            ),
        ),
        ToolConfig(
            name="patch-watcher",
            path=config / "patch-watcher" / "config",
            note="Patch Watcher reads its own private copy; it never reads credentials from the environment.",
            fields=(
                Field("GERRIT_URL", "Gerrit URL", default="https://review.whamcloud.com"),
                Field("GERRIT_USER", "Gerrit username"),
                Field("GERRIT_PASS", "Gerrit HTTP password", secret=True),
                Field("REFRESH_INTERVAL_SECONDS", "Poll interval (seconds)",
                      default="300", required=False),
            ),
        ),
        ToolConfig(
            name="jira",
            path=config / "jira-tool" / ".env",
            # JIRA_SERVER, not JIRA_URL: jira_tool/config.py reads JIRA_SERVER
            # and JIRA_TOKEN out of this file and nothing else, so a file
            # naming the URL JIRA_URL configured nothing -- `jira get LU-1`
            # answered "Server URL not configured".  There is no username to
            # ask for either; the tool authenticates with the bearer token
            # alone, and the JIRA_USER we used to write was read by nobody.
            fields=(
                Field("JIRA_SERVER", "Jira URL", default="https://jira.whamcloud.com"),
                Field("JIRA_TOKEN", "Jira API token", secret=True),
            ),
        ),
        # The two URLs below carry the CLIs' own defaults (jenkins_tool and
        # maloo_tool config.py).  Without a default they were required with
        # nothing to accept, so pressing Enter -- the documented way to keep
        # the shown value -- made configure_tool abandon the whole tool and
        # write no file at all, which pw-doctor then reported as unconfigured.
        ToolConfig(
            name="jenkins",
            path=config / "jenkins-tool" / ".env",
            fields=(
                Field("JENKINS_URL", "Jenkins URL", default="https://build.whamcloud.com"),
                Field("JENKINS_USER", "Jenkins username"),
                Field("JENKINS_TOKEN", "Jenkins API token", secret=True),
            ),
        ),
        ToolConfig(
            name="maloo",
            path=config / "maloo-tool" / ".env",
            fields=(
                Field("MALOO_URL", "Maloo URL", default="https://testing.whamcloud.com"),
                Field("MALOO_USER", "Maloo username"),
                Field("MALOO_PASS", "Maloo password", secret=True),
            ),
        ),
    )


def read_env(path: Path) -> dict[str, str]:
    """Parse an existing KEY=VALUE file, ignoring comments and blanks."""

    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    """Write the file privately and atomically.

    Truncating in place would destroy the operator's existing credentials if
    the process died between the truncate and the write, or if the write were
    short.  A temp file in the same directory plus ``os.replace`` means the
    file is either the old contents or the new ones, never neither.  The rest
    of this codebase already does it this way (``standing_policy._write``,
    ``claude_runner._atomic_private_json``); this one did not.

    A value containing a newline would corrupt the file, so it is refused
    rather than silently written.
    """

    for key, value in values.items():
        if "\n" in str(value) or "\r" in str(value):
            raise ValueError(f"{key} must not contain a line break")

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    body = "".join(f"{key}={value}\n" for key, value in values.items())
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except OSError:
        os.unlink(temporary)
        raise
    os.chmod(path, 0o600)


def configure_tool(
    tool: ToolConfig,
    *,
    ask: Callable[[str], str],
    ask_secret: Callable[[str], str],
    out=sys.stdout,
    reconfigure: bool = False,
) -> bool:
    """Prompt for one tool's settings.  Returns True when the file changed."""

    existing = read_env(tool.path)
    missing = [f for f in tool.fields if f.required and not existing.get(f.key)]
    if not missing and not reconfigure:
        print(f"  {tool.name}: already configured ({tool.path})", file=out)
        return False
    print(f"\n  {tool.name} -> {tool.path}", file=out)
    if tool.note:
        print(f"    {tool.note}", file=out)
    values = dict(existing)
    for field_spec in tool.fields:
        current = existing.get(field_spec.key, "") or field_spec.default
        shown = "****" if (field_spec.secret and existing.get(field_spec.key)) else current
        suffix = f" [{shown}]" if shown else ""
        answer = (ask_secret if field_spec.secret else ask)(
            f"    {field_spec.prompt}{suffix}: "
        ).strip()
        if not answer:
            answer = existing.get(field_spec.key, "") or field_spec.default
        if field_spec.required and not answer:
            print(f"    {field_spec.key} is required; skipping {tool.name}", file=out)
            return False
        if answer:
            values[field_spec.key] = answer
    if values == existing:
        return False
    write_env(tool.path, values)
    print(f"    wrote {tool.path} (mode 0600)", file=out)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import getpass

    parser = argparse.ArgumentParser(
        description="Configure the credentials a Patch Watcher host needs"
    )
    parser.add_argument(
        "--reconfigure", action="store_true",
        help="prompt for every value, not only the missing ones",
    )
    parser.add_argument("--only", action="append", default=[], help="configure one tool")
    options = parser.parse_args(list(argv) if argv is not None else None)

    if not sys.stdin.isatty():
        print("pw-configure needs a terminal; run it interactively.")
        return 2

    tools = tool_configs()
    if options.only:
        wanted = {name.lower() for name in options.only}
        tools = tuple(t for t in tools if t.name.lower() in wanted)
        if not tools:
            print(f"no such tool; known: {', '.join(t.name for t in tool_configs())}")
            return 2

    print("Patch Watcher host configuration")
    print("Press Enter to keep the value shown in brackets.")
    changed = 0
    for tool in tools:
        if configure_tool(
            tool, ask=input, ask_secret=getpass.getpass,
            reconfigure=options.reconfigure,
        ):
            changed += 1

    print(f"\n{changed} file(s) written.")
    print("\nTwo things this cannot do for you:")
    print("  1. Accept the background-agent disclaimer, which unattended runs need:")
    print("       claude --dangerously-skip-permissions      (once, interactively)")
    print("  2. Declare the checkout pool agents may use:")
    print("       ~/.config/patch-watcher/checkout-pool.json")
    print('       {"root": "<checkouts root>", "checkouts": [1, 2, 3]}')
    print("     Do not list a checkout you work in yourself; an agent resets it.")
    print("\nThen run:  pw-doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
