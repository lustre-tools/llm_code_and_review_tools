"""Opt-in local timing for the tools in this checkout: where a run's
time goes, kept in a file and never printed or sent.

A tool's output is read by an agent, so timing cannot go there; and
the anonymous weekly telemetry (telemetry.py) carries counts, never
arguments.  This is the third thing: when switched on, each run of a
tool appends one JSON line to a local file saying how long it took,
how much of that was spent waiting on each HTTP request and child
process, and how much was the tool itself.  ``llm-tools-timing``
summarises the file.

Switches, first match wins:

  * ``LLM_TOOLS_TIMING`` in the environment: ``0``/``off`` disables,
    ``1``/``on`` records to the default file, anything else is the
    file to record to (an agent service sets this per run);
  * ``llm-tools-timing on [--file PATH]`` / ``off``, kept in the
    ``timing`` block of ``~/.config/llm-tools/config.json``.

Off by default.  The default file is
``~/.local/state/llm-tools/timing.jsonl``; at 20 MB it is moved to
``timing.jsonl.1`` and a new one started.

A record holds the command line, so it can be replayed; an option
named like a secret (``--token``, ``--password``) has its value
replaced, and every argument is cut to 160 characters.  The file is
created mode 0600.

As in telemetry.py, nothing here may fail or slow a command: hooks
only append to a list, the record is written once at exit, and every
error is swallowed.  Inactive under pytest.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import statistics
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ENV = "LLM_TOOLS_TIMING"
MAX_BYTES = 20 * 1024 * 1024
MAX_CALLS = 300
MAX_ARG_LEN = 160
MAX_ARGS = 40
SECRET_OPT = re.compile(r"^--?[\w-]*(token|pass|password|secret|apikey|api-key)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Where, and whether
# ---------------------------------------------------------------------------


def _config_block() -> dict[str, Any]:
    from .telemetry import _config_file, _load_json

    block = _load_json(_config_file()).get("timing")
    return block if isinstance(block, dict) else {}


def _default_file() -> Path:
    from .config import user_state_dir

    return user_state_dir() / "timing.jsonl"


def target() -> Path | None:
    """The file this run records to, or None when timing is off."""
    raw = os.environ.get(ENV, "").strip()
    if raw.lower() in ("0", "no", "false", "off"):
        return None
    if raw.lower() in ("1", "yes", "true", "on"):
        return _default_file()
    if raw:
        return Path(raw).expanduser()
    block = _config_block()
    if not block.get("enabled"):
        return None
    return Path(block["file"]).expanduser() if block.get("file") else _default_file()


def _under_test() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _argv(argv: list[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for arg in argv[:MAX_ARGS]:
        if hide_next:
            out.append("<redacted>")
            hide_next = False
            continue
        name, eq, _ = arg.partition("=")
        if SECRET_OPT.match(name):
            if eq:
                out.append(f"{name}=<redacted>")
            else:
                out.append(arg)
                hide_next = True
            continue
        out.append(arg if len(arg) <= MAX_ARG_LEN else arg[:MAX_ARG_LEN] + "...")
    if len(argv) > MAX_ARGS:
        out.append(f"...+{len(argv) - MAX_ARGS}")
    return out


def _busy_ms(spans: list[tuple[float, float]]) -> float:
    """Wall time covered by at least one span: concurrent requests
    count once."""
    total = 0.0
    end = float("-inf")
    for s, e in sorted(spans):
        if e <= end:
            continue
        total += e - max(s, end)
        end = e
    return total


class Recorder:
    """The calls of one run.  Hooks may fire from worker threads."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.cpu0 = time.process_time()
        self.calls: list[dict[str, Any]] = []
        self.spans: list[tuple[float, float]] = []
        # Totals over every call; the list above keeps only the first
        # MAX_CALLS.
        self.http_n = 0
        self.http_bytes = 0
        self.lock = threading.Lock()

    def add(self, start: float, entry: dict[str, Any]) -> None:
        end = time.perf_counter()
        entry = {"t": round((start - self.t0) * 1000),
                 "ms": round((end - start) * 1000), **entry}
        with self.lock:
            self.spans.append((start, end))
            if entry["kind"] == "http":
                self.http_n += 1
                self.http_bytes += entry.get("bytes") or 0
            if len(self.calls) < MAX_CALLS:
                self.calls.append(entry)

    def record(self, tool: str, command: str | None, exit_code: Any) -> dict[str, Any]:
        wall = (time.perf_counter() - self.t0) * 1000
        busy = _busy_ms(self.spans) * 1000
        from .telemetry import _caller

        return {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "tool": tool,
            "command": command,
            "argv": _argv(sys.argv[1:]),
            "exit": exit_code,
            "caller": _caller(),
            "pid": os.getpid(),
            "wall_ms": round(wall),
            # Waiting on the network or a child; the rest is the tool.
            "wait_ms": round(busy),
            "self_ms": round(wall - busy),
            # CPU spent before main() is almost all imports.
            "startup_cpu_ms": round(self.cpu0 * 1000),
            "cpu_ms": round(time.process_time() * 1000),
            "http_n": self.http_n,
            "http_bytes": self.http_bytes,
            "calls": self.calls,
            "calls_dropped": max(0, len(self.spans) - len(self.calls)),
        }


_current: Recorder | None = None
_installed = False


def _install_hooks() -> None:
    """Wrap requests' transport and subprocess.run, once per process.

    The wrappers look up the current recorder on each call, so they
    cost one global read when no run is being recorded.
    """
    global _installed
    if _installed:
        return
    _installed = True
    import subprocess

    real_run = subprocess.run

    def run(*args: Any, **kwargs: Any) -> Any:
        rec = _current
        if rec is None:
            return real_run(*args, **kwargs)
        start = time.perf_counter()
        cmd = args[0] if args else kwargs.get("args")
        words = [cmd] if isinstance(cmd, str) else [str(w) for w in cmd or []]
        # "git -C <repo> -c k=v fetch ..." names the work by "git fetch".
        while len(words) > 2 and words[1] in ("-C", "-c"):
            del words[1:3]
        words = words[:4]
        entry: dict[str, Any] = {"kind": "proc",
                                 "cmd": " ".join(str(w) for w in words)[:MAX_ARG_LEN]}
        try:
            result = real_run(*args, **kwargs)
            entry["rc"] = result.returncode
            return result
        except BaseException as e:
            entry["rc"] = type(e).__name__
            raise
        finally:
            rec.add(start, entry)

    subprocess.run = run  # type: ignore[assignment]

    try:
        from requests.adapters import HTTPAdapter
    except ImportError:
        return
    real_send = HTTPAdapter.send

    def send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        rec = _current
        if rec is None:
            return real_send(self, request, *args, **kwargs)
        start = time.perf_counter()
        url = urlsplit(request.url or "")
        entry: dict[str, Any] = {
            "kind": "http", "m": request.method, "host": url.hostname,
            "path": url.path[:MAX_ARG_LEN],
        }
        if url.query:
            entry["q"] = url.query[:MAX_ARG_LEN]
        try:
            resp = real_send(self, request, *args, **kwargs)
            entry["status"] = resp.status_code
            if not kwargs.get("stream"):
                # requests reads a non-streamed body right after this
                # returns; reading it here keeps the transfer in the span.
                entry["bytes"] = len(resp.content or b"")
            return resp
        except BaseException as e:
            entry["status"] = type(e).__name__
            raise
        finally:
            rec.add(start, entry)

    HTTPAdapter.send = send  # type: ignore[method-assign]


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


@contextlib.contextmanager
def recording(tool: str, run: Any) -> Iterator[None]:
    """Wrap a tool's main(): times the run when timing is on.

    *run* is telemetry's Run, read at exit for the command name.
    """
    global _current
    try:
        path = None if _under_test() else target()
        if path is not None:
            _install_hooks()
            _current = Recorder()
    except Exception as e:  # noqa: BLE001
        log.debug("timing: %s", e)
        path = None
    if path is None:
        yield
        return
    rec = _current
    exit_code: Any = 0
    try:
        yield
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) or e.code is None else 1
        raise
    except BaseException as e:
        exit_code = type(e).__name__
        raise
    finally:
        _current = None
        try:
            if rec is not None:
                data = rec.record(tool, getattr(run, "command", None), exit_code or 0)
                _append(path, json.dumps(data, separators=(",", ":")) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("timing: %s", e)


# ---------------------------------------------------------------------------
# llm-tools-timing: status / on / off / summary / slowest / endpoints / clear
# ---------------------------------------------------------------------------


def _load(path: Path, since: timedelta | None, tool: str | None) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - since if since else None
    out = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if tool and r.get("tool") != tool:
                continue
            if cutoff and datetime.fromisoformat(r["ts"]) < cutoff:
                continue
            out.append(r)
    return out


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def _stats(values: list[float]) -> dict[str, float]:
    return {"p50": round(statistics.median(values)), "p90": round(_pct(values, 0.9)),
            "max": round(max(values))}


_ID = re.compile(r"(?<=/)(\d{3,}|[0-9a-f]{40}|[A-Z][A-Z0-9_]+-\d+)(?=/|$)")


def _endpoint(call: dict[str, Any]) -> str:
    """A request's path with its IDs folded: /a/changes/{id}/comments."""
    path = call.get("path", "")
    path = re.sub(r"/files/[^/]+", "/files/{file}", path)
    path = re.sub(r"/changes/[^/]+", "/changes/{id}", path)
    path = _ID.sub("{id}", path)
    return f"{call.get('m')} {call.get('host')}{path}"


def summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        groups[(r["tool"], r.get("command") or "?")].append(r)
    rows = []
    for (tool, command), rs in groups.items():
        rows.append({
            "tool": tool, "command": command, "runs": len(rs),
            "wall_ms": _stats([r["wall_ms"] for r in rs]),
            "wait_ms_p50": round(statistics.median(r["wait_ms"] for r in rs)),
            "self_ms_p50": round(statistics.median(r["self_ms"] for r in rs)),
            "http_n_p50": statistics.median(r["http_n"] for r in rs),
            "http_kb_p50": round(statistics.median(r["http_bytes"] for r in rs) / 1024),
        })
    return sorted(rows, key=lambda x: -x["wall_ms"]["p50"] * x["runs"])


def endpoints(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        for c in r.get("calls", []):
            if c.get("kind") == "http":
                groups[_endpoint(c)].append(c)
    rows = [{"endpoint": ep, "n": len(cs), "ms": _stats([c["ms"] for c in cs]),
             "kb_p50": round(statistics.median(c.get("bytes") or 0 for c in cs) / 1024)}
            for ep, cs in groups.items()]
    return sorted(rows, key=lambda x: -x["ms"]["p50"] * x["n"])


def _since(raw: str | None) -> timedelta | None:
    if not raw:
        return None
    m = re.fullmatch(r"(\d+)([smhd])", raw)
    if not m:
        raise argparse.ArgumentTypeError(f"bad --since {raw!r}: use e.g. 30m, 6h, 2d")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def _set(enabled: bool, file: str | None) -> None:
    from .telemetry import _config_file, _load_json, _write_json

    path = _config_file()
    data = _load_json(path)
    block = {"enabled": enabled}
    if file:
        block["file"] = str(Path(file).expanduser().resolve())
    data["timing"] = block
    _write_json(path, data, mode=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-tools-timing",
        description="Local timing of jira, gerrit and the other tools' runs.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="on/off and where it records")
    on = sub.add_parser("on", help="record every run")
    on.add_argument("--file", help=f"record here (default {_default_file()})")
    sub.add_parser("off", help="stop recording")
    for name, text in (("summary", "per command: runs, wall p50/p90, wait vs self"),
                       ("slowest", "the slowest runs, with their command lines"),
                       ("endpoints", "per HTTP endpoint: calls, latency, size")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--file", help="read this file instead")
        p.add_argument("--since", help="only runs this recent: 30m, 6h, 2d")
        p.add_argument("--tool", help="only this tool (gerrit, jira, ...)")
        if name == "slowest":
            p.add_argument("-n", type=int, default=20)
    sub.add_parser("clear", help="delete the recorded runs")
    args = parser.parse_args(argv)

    path = target() or _default_file()
    if args.action in ("on", "off"):
        _set(args.action == "on", getattr(args, "file", None))
        args.action = "status"
        path = target() or _default_file()
    if args.action == "status":
        size = path.stat().st_size if path.exists() else 0
        print(json.dumps({"enabled": target() is not None,
                          "env": os.environ.get(ENV),
                          "file": str(path), "bytes": size}))
        return 0
    if args.action == "clear":
        for p in (path, path.with_name(path.name + ".1")):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        return 0

    src = Path(args.file).expanduser() if args.file else path
    records = _load(src, _since(args.since), args.tool)
    if args.action == "summary":
        out: Any = summary(records)
    elif args.action == "endpoints":
        out = endpoints(records)
    else:
        out = [{k: r.get(k) for k in ("ts", "tool", "argv", "exit", "wall_ms",
                                      "wait_ms", "self_ms", "http_n", "http_bytes")}
               for r in sorted(records, key=lambda r: -r["wall_ms"])[:args.n]]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
