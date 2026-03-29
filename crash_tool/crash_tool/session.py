"""Crash session management — runs crash non-interactively.

Handles launching crash, sending commands, collecting per-command
output, and tearing down cleanly.  All output is captured and
returned as structured data — no interactive terminal required.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# Sentinel used to delimit command output boundaries.
_SENTINEL_PREFIX = "__CRASH_TOOL_DELIM_"


@dataclass
class CommandResult:
    """Result of a single crash command."""
    command: str
    output: str
    error: bool = False
    error_message: str = ""


@dataclass
class SessionResult:
    """Result of a crash session (one or more commands)."""
    commands: list[CommandResult] = field(default_factory=list)
    init_output: str = ""
    crash_stderr: str = ""
    return_code: int = 0


def find_crash_binary() -> str:
    """Locate the crash binary."""
    path = shutil.which("crash")
    if path:
        return path
    # Common locations
    for candidate in ["/usr/bin/crash", "/usr/sbin/crash", "/usr/local/bin/crash"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("crash binary not found in PATH or standard locations")


def run_session(
    commands: list[str],
    vmlinux: str | None = None,
    vmcore: str | None = None,
    crash_binary: str | None = None,
    timeout: int = 120,
    minimal: bool = False,
    extra_args: list[str] | None = None,
    mod_dir: str | None = None,
    pre_commands: list[str] | None = None,
) -> SessionResult:
    """Run one or more commands in a single crash session.

    Each command's output is captured individually using sentinel
    delimiters, so callers get clean per-command results.

    Args:
        commands: List of crash commands to execute.
        vmlinux: Path to vmlinux (debug kernel).  Optional if crash
            can auto-detect (e.g. running kernel with debuginfo).
        vmcore: Path to vmcore dump file.  If None, analyzes the
            running kernel via /dev/crash or /proc/kcore.
        crash_binary: Path to crash binary.  Auto-detected if None.
        timeout: Maximum seconds to wait for the session.
        minimal: Use --minimal mode (faster init, fewer commands).
        extra_args: Additional arguments to pass to crash.
        mod_dir: Directory containing .ko files to load via 'mod -S'.
        pre_commands: Commands to run before user commands (output
            not captured in per-command results).

    Returns:
        SessionResult with per-command output.
    """
    if crash_binary is None:
        crash_binary = find_crash_binary()

    # Build the command input with sentinels between commands.
    # We use 'p (int)<unique_number>' which outputs
    # "$N = <unique_number>" — reliable and always available.
    input_lines: list[str] = []
    sentinels: list[str] = []

    # Pre-commands: module loading, setup, etc. (output goes to init)
    if mod_dir:
        input_lines.append(f"mod -S {mod_dir}")
    if pre_commands:
        input_lines.extend(pre_commands)

    # Emit a sentinel BEFORE each command.  This way:
    #   chunks[0] = init output (everything before first sentinel)
    #   chunks[1] = command 0 output (between sentinel 0 and 1)
    #   chunks[N+1] = command N output
    for i, cmd in enumerate(commands):
        sentinel_val = 7777000 + i
        sentinels.append(str(sentinel_val))
        input_lines.append(f"p (int){sentinel_val}")
        input_lines.append(cmd)
    # Final sentinel after last command
    final_val = 7777000 + len(commands)
    sentinels.append(str(final_val))
    input_lines.append(f"p (int){final_val}")

    input_lines.append("quit")
    input_text = "\n".join(input_lines) + "\n"

    # Build crash argv
    argv = [crash_binary, "-s"]
    if minimal:
        argv.append("--minimal")
    if extra_args:
        argv.extend(extra_args)
    if vmlinux:
        argv.append(vmlinux)
    if vmcore:
        argv.append(vmcore)

    try:
        proc = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result = SessionResult(return_code=-1, crash_stderr="crash session timed out")
        for cmd in commands:
            result.commands.append(CommandResult(
                command=cmd, output="", error=True,
                error_message=f"Session timed out after {timeout}s",
            ))
        return result
    except FileNotFoundError:
        result = SessionResult(return_code=-1, crash_stderr=f"crash binary not found: {crash_binary}")
        return result

    # Parse output into per-command chunks using sentinels.
    raw_output = proc.stdout
    result = SessionResult(
        crash_stderr=proc.stderr.strip(),
        return_code=proc.returncode,
    )

    # Split on sentinel lines.  The output looks like:
    #   <init output>
    #   $1 = 7777000        <- sentinel before cmd 0
    #   <command 0 output>
    #   $2 = 7777001        <- sentinel before cmd 1
    #   <command 1 output>
    #   $3 = 7777002        <- final sentinel
    # So chunks[0] = init, chunks[i+1] = command i output.
    chunks = _split_on_sentinels(raw_output, sentinels)

    if chunks:
        result.init_output = chunks[0].strip()
        for i, cmd in enumerate(commands):
            chunk_idx = i + 1
            if chunk_idx < len(chunks):
                output = chunks[chunk_idx].strip()
                # Detect crash error messages in output
                err = _detect_error(output)
                result.commands.append(CommandResult(
                    command=cmd,
                    output=output,
                    error=bool(err),
                    error_message=err,
                ))
            else:
                result.commands.append(CommandResult(
                    command=cmd, output="", error=True,
                    error_message="No output captured (crash may have exited early)",
                ))
    else:
        # No sentinels found — dump everything as init_output
        result.init_output = raw_output.strip()
        for cmd in commands:
            result.commands.append(CommandResult(
                command=cmd, output="", error=True,
                error_message="Could not parse command output",
            ))

    return result


def _split_on_sentinels(output: str, sentinels: list[str]) -> list[str]:
    """Split output on sentinel lines, returning chunks between them.

    Returns a list where chunks[0] is everything before the first
    sentinel, chunks[1] is between sentinel 0 and 1, etc.

    Sentinels are unique integer values.  crash's 'p (int)N' command
    outputs lines like: '$42 = 7777000' or '$42 = 7777000\n'.
    We match on the sentinel value at the end of such lines.
    """
    if not sentinels:
        return [output]

    # Match lines like: $42 = 7777000
    escaped = [re.escape(s) for s in sentinels]
    pattern = re.compile(
        r"^\$\d+\s*=\s*(?:" + "|".join(escaped) + r")\s*$",
        re.MULTILINE,
    )

    parts = pattern.split(output)
    return parts


def _detect_error(output: str) -> str:
    """Detect common crash error patterns in command output."""
    error_patterns = [
        (r"crash: invalid command:", "invalid command"),
        (r"crash: cannot resolve", "symbol resolution failed"),
        (r"crash: cannot read", "memory read failed"),
        (r"crash: invalid address", "invalid address"),
        (r"No such file or directory", "file not found"),
        (r"crash: .*not found in namelist", "symbol not in namelist"),
        (r"bt: invalid task or pid", "invalid task/pid"),
    ]
    for pat, msg in error_patterns:
        m = re.search(pat, output)
        if m:
            return msg
    return ""


def _find_drgn_tools_dir() -> str | None:
    """Locate the lustre-drgn-tools directory."""
    candidates = [
        Path.home() / "llm_code_and_review_tools" / "lustre-drgn-tools",
        Path(__file__).parent.parent.parent / "lustre-drgn-tools",
    ]
    for p in candidates:
        if (p / "lustre_triage.py").exists():
            return str(p)
    return None


def run_drgn_triage(
    vmcore: str,
    vmlinux: str,
    mod_dir: str | None = None,
    timeout: int = 120,
) -> dict:
    """Run lustre_triage.py and return parsed JSON result.

    Returns a dict with the triage output, or an error dict.
    """
    tools_dir = _find_drgn_tools_dir()
    if not tools_dir:
        return {"error": "lustre-drgn-tools not found"}

    script = os.path.join(tools_dir, "lustre_triage.py")
    argv = [
        sys.executable, script,
        "--vmcore", vmcore,
        "--vmlinux", vmlinux,
    ]
    if mod_dir:
        argv.extend(["--mod-dir", mod_dir])

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"drgn triage timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}

    if proc.returncode != 0:
        # Try to parse partial output; fall back to error
        stderr = proc.stderr.strip()
        if proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError:
                pass
        return {"error": f"drgn triage failed (rc={proc.returncode})", "stderr": stderr}

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "drgn triage returned invalid JSON", "raw": proc.stdout[:500]}
