"""Parallel execution of headless kreview runs.

Each change is reviewed by a headless Claude Code process running the
/kreview slash command inside a dedicated git worktree of the source
repository. kreview writes ./gerrit-review.json (in the worktree) only
when it finds issues, and ./review-metadata.json for every completed
analysis; the runner collects both into the results directory suffixed
by <change>_ps<N> so parallel and repeated runs never overwrite each
other. review-metadata.json doubles as the completion marker: a run
that produced neither file did not finish and is recorded as failed,
not clean.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .agents import get_agent
from .gerrit import ResolvedChange
from .manifest import SUMMARY_NAME, locked_summary  # noqa: F401 (re-export)
from .ui import console
from . import worktree as wt

REVIEW_JSON_NAME = "gerrit-review.json"
METADATA_JSON_NAME = "review-metadata.json"

# Review status values recorded in the summary manifest
STATUS_FINDINGS = "findings"
STATUS_CLEAN = "clean"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_INVALID_JSON = "invalid-json"

# Grace period between SIGTERM and SIGKILL when a review times out
_KILL_GRACE_SECONDS = 15

# Seconds between status updates: fast in-place redraws on a TTY,
# sparse appended lines otherwise
PROGRESS_INTERVAL = 60
TTY_PROGRESS_INTERVAL = 10

# Short model names recognized in --model values, stream-json init
# events, and Assisted-by lines
_MODEL_NAMES = ("fable", "opus", "sonnet", "haiku")

_MODEL_JSON_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')

# Live token counter events emitted by claude's stream-json output
_ESTIMATED_TOKENS_RE = re.compile(r'"estimated_tokens"\s*:\s*(\d+)')


def _log(msg: str) -> None:
    console.event(msg)


def _read_tail(path: Path, size: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def format_tokens(count: Optional[int]) -> Optional[str]:
    if count is None:
        return None
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.1f}M"


def live_token_count(log_path: Path) -> Optional[int]:
    """Latest estimated_tokens value from the streaming log."""
    matches = _ESTIMATED_TOKENS_RE.findall(_read_tail(log_path, 16384))
    return int(matches[-1]) if matches else None


def parse_final_usage(log_path: Path):
    """Total tokens and cost from the stream-json result event.

    Returns (tokens, cost_usd), either possibly None.
    """
    tail = _read_tail(log_path, 262144)
    for line in reversed(tail.splitlines()):
        if '"type":"result"' not in line.replace(" ", ""):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "result":
            continue
        usage = obj.get("usage") or {}
        tokens = sum(
            usage.get(key, 0) for key in (
                "input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens"))
        return (tokens or None), obj.get("total_cost_usd")
    return None, None


@dataclass
class ReviewResult:
    change: ResolvedChange
    status: str
    findings: int = 0
    severity: Optional[str] = None
    model: Optional[str] = None
    agent: str = "claude"
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    duration: float = 0.0
    json_path: Optional[Path] = None
    log_path: Optional[Path] = None
    error: Optional[str] = None


@dataclass
class BatchConfig:
    repo: Path
    results_dir: Path
    worktrees_dir: Path
    jobs: int = 5
    timeout: int = 7200
    keep_worktrees: bool = False
    agent: str = "claude"
    model: Optional[str] = None
    agent_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # git -C <repo> resolves relative paths against the repo, not
        # our cwd — always hand git absolute paths.
        self.repo = Path(self.repo).expanduser().resolve()
        self.results_dir = Path(self.results_dir).expanduser().resolve()
        self.worktrees_dir = Path(self.worktrees_dir).expanduser().resolve()
        get_agent(self.agent)  # fail fast on unknown agents
        if self.jobs < 1:
            raise ValueError(f"jobs must be >= 1, got {self.jobs}")


def build_agent_cmd(config: BatchConfig) -> list[str]:
    """Headless review command for the configured agent.

    claude invokes the installed /kreview slash command with
    stream-json output (events appear in the log as they happen, so
    the log doubles as a liveness/token signal — text mode buffers
    everything until the end). Other agents get the installed kreview
    command file's content as the prompt text, which does not depend
    on the CLI's own slash-command expansion.
    """
    spec = get_agent(config.agent)
    prompt_text = ""
    if not spec.stream_json:
        prompt_text = spec.command_file().read_text()
    return spec.build_cmd(config.model, config.agent_args, prompt_text)


def prepare_worktree(config: BatchConfig, change: ResolvedChange) -> Path:
    """Fetch the change (if needed) and create its review worktree.

    The directory name carries the pid so concurrent lreview
    invocations reviewing the same change never remove each other's
    live worktrees.
    """
    if not wt.commit_exists(config.repo, change.sha):
        wt.fetch_change(config.repo, change.fetch_url(), change.ref)
        if not wt.commit_exists(config.repo, change.sha):
            raise wt.GitError(
                f"fetched {change.ref} but {change.sha} still missing")
    dest = config.worktrees_dir / f"kreview_{change.slug}.{os.getpid()}"
    if dest.exists():
        wt.remove_worktree(config.repo, dest)
        if dest.exists():
            shutil.rmtree(dest)
    # Clear registrations whose directories are gone (e.g. a worktree
    # deleted out-of-band), which would otherwise fail the add forever.
    wt.prune_worktrees(config.repo)
    wt.add_worktree(config.repo, dest, change.sha)
    return dest


def count_findings(spec: dict) -> int:
    comments = spec.get("comments") or {}
    if isinstance(comments, dict):
        return sum(len(v) for v in comments.values())
    return len(comments)


def short_model_name(text: str) -> Optional[str]:
    """Reduce a model id / Assisted-by line to a short model name."""
    lower = text.lower()
    for name in _MODEL_NAMES:
        if name in lower:
            return name
    return None


def detect_model(log_path: Optional[Path],
                 configured: Optional[str] = None) -> Optional[str]:
    """Best-effort short name of the model that ran the review.

    An explicit --model wins; otherwise the "model" field of the
    stream-json init event (start of the log) is used, falling back to
    the review's own "Assisted-by: <agent>:<model>" output line.
    """
    if configured:
        return short_model_name(configured) or configured[:20]
    if not log_path:
        return None
    try:
        raw = log_path.read_text(errors="replace")
    except OSError:
        return None

    match = _MODEL_JSON_RE.search(raw[:8000])
    if match:
        short = short_model_name(match.group(1))
        if short:
            return short

    for line in reversed(raw[-8000:].splitlines()):
        if "assisted-by:" in line.lower():
            short = short_model_name(line)
            if short:
                return short
            tail = line.rsplit(":", 1)[-1].strip()
            return tail[:20] or None
    if match:
        return match.group(1)[:20]
    return None


def _elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


class ProgressTracker:
    """Tracks running reviews and maintains the live status line.

    On a TTY the line is redrawn in place every few seconds (and
    immediately when a review starts or finishes); otherwise it is
    printed as a plain line once per PROGRESS_INTERVAL.
    """

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self._running: dict[str, tuple[float, Path]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, slug: str, log_path: Path) -> None:
        with self._lock:
            self._running[slug] = (time.monotonic(), log_path)
        self._refresh()

    def finish(self, slug: str) -> None:
        with self._lock:
            self._running.pop(slug, None)
            self.done += 1
        self._refresh()

    def status_line(self) -> Optional[str]:
        with self._lock:
            if not self._running:
                return None
            now = time.monotonic()
            parts = []
            for slug, (started, log_path) in sorted(self._running.items()):
                part = f"{slug} {_elapsed(now - started)}"
                tokens = live_token_count(log_path)
                if tokens is not None:
                    part += f" {format_tokens(tokens)} tok"
                else:
                    try:
                        part += f" {log_path.stat().st_size // 1024}KB"
                    except OSError:
                        pass
                parts.append(part)
            return (f"running: {', '.join(parts)} | "
                    f"done {self.done}/{self.total}")

    def _refresh(self) -> None:
        line = self.status_line()
        if line:
            console.status(console.color("dim", line))
        else:
            console.clear_status()

    def _heartbeat(self) -> None:
        interval = (TTY_PROGRESS_INTERVAL if console.is_tty
                    else PROGRESS_INTERVAL)
        while not self._stop.wait(interval):
            self._refresh()

    def __enter__(self) -> "ProgressTracker":
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        console.clear_status()


def _run_claude(cmd: list[str], cwd: Path, log_path: Path,
                timeout: int) -> int:
    """Run claude in its own process group; kill the whole group on
    timeout so MCP servers / hook children don't outlive the review.

    Raises subprocess.TimeoutExpired after the group is killed.
    """
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            raise


def _collect_json(source: Path, dest: Path):
    """Load and copy a JSON artifact; returns (spec, error)."""
    try:
        spec = json.loads(source.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        try:
            shutil.copy(source, dest.with_suffix(".invalid"))
        except OSError:
            pass
        return None, str(exc)
    if not isinstance(spec, dict):
        try:
            shutil.copy(source, dest.with_suffix(".invalid"))
        except OSError:
            pass
        return None, f"expected a JSON object, got {type(spec).__name__}"
    try:
        shutil.copy(source, dest)
    except OSError as exc:
        return None, str(exc)
    return spec, None


def run_review(
    config: BatchConfig,
    change: ResolvedChange,
    worktree_dir: Path,
) -> ReviewResult:
    """Run one headless kreview in its worktree and collect the output."""
    log_path = config.results_dir / f"kreview-{change.slug}.log"
    cmd = build_agent_cmd(config)
    start = time.monotonic()

    _log(f"[{change.slug}] {console.color('cyan', 'review started')}: "
         f"{change.subject[:60]}")
    try:
        returncode = _run_claude(cmd, worktree_dir, log_path, config.timeout)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        _log(f"[{change.slug}] {console.color('red', 'TIMEOUT')} "
             f"after {int(duration)}s")
        return ReviewResult(
            change, STATUS_TIMEOUT, duration=duration, log_path=log_path,
            model=detect_model(log_path, config.model),
            tokens=live_token_count(log_path),
            error=f"timed out after {config.timeout}s")
    except OSError as exc:
        return ReviewResult(
            change, STATUS_FAILED, duration=time.monotonic() - start,
            log_path=log_path, model=detect_model(None, config.model),
            error=str(exc))

    duration = time.monotonic() - start
    model = detect_model(log_path, config.model)
    tokens, cost_usd = parse_final_usage(log_path)
    if tokens is None:
        tokens = live_token_count(log_path)
    review_json = worktree_dir / REVIEW_JSON_NAME
    metadata_json = worktree_dir / METADATA_JSON_NAME

    # review-metadata.json is written for every completed analysis;
    # collect it (best effort) and use it as the completion marker.
    severity = None
    if metadata_json.is_file():
        metadata, _ = _collect_json(
            metadata_json,
            config.results_dir / f"review-metadata-{change.slug}.json")
        if metadata:
            severity = metadata.get("issue-severity-score")

    stats = _elapsed(duration)
    if tokens is not None:
        stats += f", {format_tokens(tokens)} tok"
    if cost_usd is not None:
        stats += f", ${cost_usd:.2f}"

    if not review_json.is_file():
        if returncode != 0:
            _log(f"[{change.slug}] {console.color('red', 'FAILED')} "
                 f"(exit {returncode}), see {log_path}")
            return ReviewResult(
                change, STATUS_FAILED, duration=duration, log_path=log_path,
                model=model, tokens=tokens, cost_usd=cost_usd,
                error=f"claude exited {returncode}")
        if not metadata_json.is_file():
            _log(f"[{change.slug}] {console.color('red', 'FAILED')} — "
                 f"review did not complete (no {METADATA_JSON_NAME}), "
                 f"see {log_path}")
            return ReviewResult(
                change, STATUS_FAILED, duration=duration, log_path=log_path,
                model=model, tokens=tokens, cost_usd=cost_usd,
                error=f"no {METADATA_JSON_NAME} produced — review did not "
                      "run to completion")
        _log(f"[{change.slug}] {console.color('green', 'clean')} — "
             f"no findings ({stats})")
        return ReviewResult(
            change, STATUS_CLEAN, severity=severity, model=model,
            tokens=tokens, cost_usd=cost_usd, duration=duration,
            log_path=log_path)

    dest_json = config.results_dir / f"gerrit-review-{change.slug}.json"
    spec, error = _collect_json(review_json, dest_json)
    if spec is None:
        _log(f"[{change.slug}] {console.color('red', 'INVALID JSON')} "
             f"output: {error}")
        return ReviewResult(
            change, STATUS_INVALID_JSON, duration=duration,
            log_path=log_path, model=model, tokens=tokens,
            cost_usd=cost_usd, error=error)

    findings = count_findings(spec)
    severity_color = {"urgent": "red", "high": "red",
                      "medium": "yellow"}.get(severity or "", "green")
    severity_note = (
        f", severity {console.color(severity_color, severity)}"
        if severity else "")
    _log(f"[{change.slug}] "
         f"{console.color('yellow', f'{findings} finding(s)')}"
         f"{severity_note} ({stats}) -> {dest_json.name}")
    return ReviewResult(
        change, STATUS_FINDINGS, findings=findings, severity=severity,
        model=model, tokens=tokens, cost_usd=cost_usd, duration=duration,
        json_path=dest_json, log_path=log_path)


def _review_and_cleanup(
    config: BatchConfig,
    change: ResolvedChange,
    worktree_dir: Path,
    tracker: Optional[ProgressTracker] = None,
) -> ReviewResult:
    """Worker wrapper: never lets an exception escape into the pool."""
    if tracker:
        tracker.start(
            change.slug, config.results_dir / f"kreview-{change.slug}.log")
    try:
        result = run_review(config, change, worktree_dir)
    except Exception as exc:  # noqa: BLE001 - one bad review must not
        # abort the batch or strand the other results
        _log(f"[{change.slug}] FAILED with unexpected error: {exc!r}")
        result = ReviewResult(change, STATUS_FAILED, error=repr(exc))
    finally:
        if tracker:
            tracker.finish(change.slug)
        if not config.keep_worktrees:
            wt.remove_worktree(config.repo, worktree_dir)
    result.agent = config.agent
    return result


def run_batch(
    config: BatchConfig,
    changes: list[ResolvedChange],
) -> list[ReviewResult]:
    """Prepare worktrees sequentially, then review in parallel.

    Results collected so far (including on KeyboardInterrupt) are
    always persisted to the summary manifest.
    """
    config.results_dir.mkdir(parents=True, exist_ok=True)

    prepared: list[tuple[ResolvedChange, Path]] = []
    results: list[ReviewResult] = []
    for change in changes:
        try:
            prepared.append((change, prepare_worktree(config, change)))
        except Exception as exc:  # noqa: BLE001 - record and continue
            _log(f"[{change.slug}] worktree setup failed: {exc}")
            results.append(ReviewResult(
                change, STATUS_FAILED, agent=config.agent, error=str(exc)))

    interrupted = False
    try:
        if prepared:
            pool = ThreadPoolExecutor(max_workers=config.jobs)
            with ProgressTracker(total=len(prepared)) as tracker:
                futures = [
                    pool.submit(
                        _review_and_cleanup, config, change, wtree, tracker)
                    for change, wtree in prepared
                ]
                try:
                    for future in futures:
                        results.append(future.result())
                except KeyboardInterrupt:
                    interrupted = True
                    _log("interrupted — cancelling pending reviews "
                         "(running ones finish or die with the process)")
                    pool.shutdown(wait=False, cancel_futures=True)
                    for future in futures:
                        if future.done() and not future.cancelled():
                            result = future.result()
                            if result not in results:
                                results.append(result)
                    raise
                finally:
                    if not interrupted:
                        pool.shutdown(wait=True)
                    if not config.keep_worktrees:
                        for _, wtree in prepared:
                            if wtree.exists():
                                wt.remove_worktree(config.repo, wtree)
    finally:
        update_summary(config.results_dir, results)

    return results


def update_summary(results_dir: Path, results: list[ReviewResult]) -> None:
    """Merge results into the summary manifest (keyed by change).

    A posted flag survives a re-review of the same revision; a review
    of a newer patchset resets it but keeps a last_posted record so an
    earlier posted review is never silently forgotten.
    """
    if not results:
        return
    with locked_summary(results_dir) as summary:
        for result in results:
            change = result.change
            key = str(change.number)
            old = summary.get(key)

            entry = {
                "number": change.number,
                "patchset": change.patchset,
                "sha": change.sha,
                "subject": change.subject,
                "base_url": change.base_url,
                "status": result.status,
                "findings": result.findings,
                "severity": result.severity,
                "model": result.model,
                "agent": result.agent,
                "tokens": result.tokens,
                "cost_usd": result.cost_usd,
                "duration_s": round(result.duration),
                "json": result.json_path.name if result.json_path else None,
                "log": result.log_path.name if result.log_path else None,
                "error": result.error,
                "posted": False,
                "reviewed_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
            }

            if old and old.get("posted"):
                if old.get("sha") == change.sha:
                    entry["posted"] = True
                    entry["posted_at"] = old.get("posted_at")
                    entry["posted_prefix"] = old.get("posted_prefix")
                else:
                    entry["last_posted"] = {
                        "sha": old.get("sha"),
                        "patchset": old.get("patchset"),
                        "posted_at": old.get("posted_at"),
                    }

            summary[key] = entry
