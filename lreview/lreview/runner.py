"""Parallel execution of headless review runs.

Each change is reviewed by a headless agent process, prompted with the
review-prompts review-core.md instruction, inside a dedicated git
worktree of the source repository. The prompt writes
./gerrit-review.json (in the worktree) only when it finds issues, and
./review-metadata.json for every completed analysis; the runner
collects both into the results directory suffixed by <change>_ps<N>
so parallel and repeated runs never overwrite each other.
review-metadata.json doubles as the completion marker: a run that
produced neither file did not finish and is recorded as failed, not
clean.
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
from .artifacts import REVIEW_RESULT_NAME, validate_review_result
from .manifest import SUMMARY_NAME, locked_summary  # noqa: F401 (re-export)
from .markdown import write_review_markdown
from .ui import console, elapsed as _elapsed, format_tokens  # noqa: F401
from . import worktree as wt

REVIEW_JSON_NAME = "gerrit-review.json"
METADATA_JSON_NAME = "review-metadata.json"

# Review modes: "full" runs the review-prompts review-core.md
# pipeline; "light" runs the bundled single-pass light-prompt.md
REVIEW_MODES = ("full", "light")
LIGHT_PROMPT_PATH = Path(__file__).resolve().parent / "light-prompt.md"

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


def live_token_count(log_path: Path) -> Optional[int]:
    """Tokens consumed so far, from the stream-json usage events.

    Sums input/cache-creation/cache-read/output over the assistant
    messages seen so far, deduplicated by message id (the same
    message can be logged more than once; the last one wins). This
    tracks the final result-event total to ~1% — only the
    per-message output_tokens are partial. Falls back to the legacy
    estimated_tokens event when the log carries no usage (other
    agents, older claude versions).
    """
    usage_by_id: dict = {}
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # e.g. a line still being written
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    usage_by_id[msg.get("id")] = usage
    except OSError:
        return None
    if usage_by_id:
        return sum(
            usage.get(key, 0)
            for usage in usage_by_id.values()
            for key in ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "output_tokens"))
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


def artifact_tag(mode: str) -> str:
    """Filename/manifest-key suffix separating review modes.

    Full-mode artifacts keep their historical unsuffixed names; other
    modes are namespaced so a light run never overwrites or
    supersedes a full run's results for the same change+patchset.
    """
    return "" if mode == "full" else f"-{mode}"


@dataclass
class ReviewResult:
    change: ResolvedChange
    status: str
    mode: str = "full"
    findings: int = 0
    severity: Optional[str] = None
    model: Optional[str] = None
    agent: str = "claude"
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    duration: float = 0.0
    json_path: Optional[Path] = None
    markdown_path: Optional[Path] = None
    memory_path: Optional[Path] = None
    memory_updated: bool = False
    log_path: Optional[Path] = None
    error: Optional[str] = None


@dataclass
class BatchConfig:
    repo: Path
    results_dir: Path
    worktrees_dir: Path
    prompts_dir: Path = Path.home() / "review-prompts" / "kernel"
    jobs: int = 5
    timeout: int = 7200
    keep_worktrees: bool = False
    mode: str = "full"
    agent: str = "claude"
    model: Optional[str] = None
    effort: Optional[str] = None
    # When set, the lreview-db directory: reviews read their per-change
    # memory document before analyzing and rewrite it afterwards
    memory_db: Optional[Path] = None
    agent_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # git -C <repo> resolves relative paths against the repo, not
        # our cwd — always hand git absolute paths.
        self.repo = Path(self.repo).expanduser().resolve()
        self.results_dir = Path(self.results_dir).expanduser().resolve()
        self.worktrees_dir = Path(self.worktrees_dir).expanduser().resolve()
        self.prompts_dir = Path(self.prompts_dir).expanduser().resolve()
        get_agent(self.agent)  # fail fast on unknown agents
        if self.mode not in REVIEW_MODES:
            raise ValueError(
                f"mode must be one of {REVIEW_MODES}, got {self.mode!r}")
        if self.jobs < 1:
            raise ValueError(f"jobs must be >= 1, got {self.jobs}")


def review_prompt(config: BatchConfig,
                  change: Optional[ResolvedChange] = None) -> str:
    """The instruction that initiates a review.

    This is the review-prompts README quick-start form (also what its
    own kernel/scripts automation uses) — a plain prompt referencing
    review-core.md by absolute path; the wording "deep dive
    regression analysis" is deliberate, per the README it gets better
    prompt compliance than calling it a review. The worktree's HEAD
    is the patch, so "the top commit" needs no SHA.

    In light mode the instruction references the bundled
    light-prompt.md instead — one focused pass, with the review-prompts
    directory passed along so the driver can load lustre-style.md.

    With memory enabled, the prompt additionally points at the
    memory-protocol instructions and the change's memory document.
    """
    if getattr(change, "provider", None) == "github":
        # GitHub PR reviews use their own prompt and output contract
        # (review-result.json); --mode and --memory do not apply.
        return (f"Using {config.prompts_dir}/review-core.md, run a deep dive "
                f"regression analysis of the complete pull request range "
                f"{change.base_sha}...{change.sha}. Read the full range, not just HEAD. "
                f"Write {REVIEW_RESULT_NAME} version 1 with message and findings; inline "
                "findings must name added PR lines, while commit-message and general findings "
                "use location_kind commit_message or summary with null path and line.")
    if config.mode == "light":
        prompt = (f"Using the prompt {LIGHT_PROMPT_PATH} run a light "
                  "regression review of the top commit; the "
                  "review-prompts knowledge directory is "
                  f"{config.prompts_dir}")
    else:
        prompt = (f"Using the prompt {config.prompts_dir}/review-core.md "
                  "run a deep dive regression analysis of the top commit")
    if config.memory_db is not None and change is not None:
        from .memory import MEMORY_PROMPT_PATH, ensure_doc
        doc = ensure_doc(config.memory_db, change)
        now = (f"patchset {change.patchset} ({change.sha[:12]})"
               if change.patchset else f"commit {change.sha[:12]}")
        prompt += (f". Additionally follow the instructions in "
                   f"{MEMORY_PROMPT_PATH} — your review memory "
                   f"document for this change is {doc}; you are "
                   f"reviewing {now}")
    return prompt


def build_agent_cmd(config: BatchConfig,
                    change: Optional[ResolvedChange] = None) -> list[str]:
    """Headless review command for the configured agent.

    All agents receive the same instruction prompt; claude runs with
    stream-json output (events appear in the log as they happen, so
    the log doubles as a liveness/token signal — text mode buffers
    everything until the end).
    """
    spec = get_agent(config.agent)
    return spec.build_cmd(config.model, config.effort, config.agent_args,
                          review_prompt(config, change))


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
    if getattr(change, "base_sha", None) and not wt.commit_exists(config.repo, change.base_sha):
        wt.fetch_change(config.repo, change.fetch_url(), change.base_sha)
        if not wt.commit_exists(config.repo, change.base_sha):
            raise wt.GitError(f"fetched base {change.base_sha} but it is still missing")
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
    if isinstance(spec.get("findings"), list):
        return len(spec["findings"])
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
        env = os.environ.copy()
        # The reviewer needs Claude OAuth only. GitHub credentials belong to
        # the parent poster and must never reach an agent or its shell tools.
        env.pop("GH_TOKEN", None); env.pop("GITHUB_TOKEN", None)
        if os.environ.get("CI"):
            env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
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


def run_log_path(config: BatchConfig, change: ResolvedChange) -> Path:
    """Per-run log file, stamped with the start time (plus pid, so
    concurrent invocations reviewing the same change never share a
    log). Every run's log is preserved — they are the only ground
    truth for comparing runs — and summary.json records which log
    belongs to the current entry."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return (config.results_dir /
            f"kreview-{change.slug}{artifact_tag(config.mode)}"
            f"-{stamp}.{os.getpid()}.log")


def run_review(
    config: BatchConfig,
    change: ResolvedChange,
    worktree_dir: Path,
    log_path: Optional[Path] = None,
) -> ReviewResult:
    """Run one headless kreview in its worktree and collect the output."""
    tag = artifact_tag(config.mode)
    json_prefix = ("review-result"
                   if getattr(change, "provider", None) == "github"
                   else "gerrit-review")
    if log_path is None:
        log_path = run_log_path(config, change)
    cmd = build_agent_cmd(config, change)
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
            change, STATUS_TIMEOUT, mode=config.mode, duration=duration,
            log_path=log_path,
            model=detect_model(log_path, config.model),
            tokens=live_token_count(log_path),
            error=f"timed out after {config.timeout}s")
    except OSError as exc:
        return ReviewResult(
            change, STATUS_FAILED, mode=config.mode,
            duration=time.monotonic() - start,
            log_path=log_path, model=detect_model(None, config.model),
            error=str(exc))

    duration = time.monotonic() - start
    model = detect_model(log_path, config.model)
    tokens, cost_usd = parse_final_usage(log_path)
    if tokens is None:
        tokens = live_token_count(log_path)
    review_json = worktree_dir / (REVIEW_RESULT_NAME if getattr(change, "provider", None) == "github" else REVIEW_JSON_NAME)
    metadata_json = worktree_dir / METADATA_JSON_NAME

    memory_doc = None
    if (config.memory_db is not None
            and getattr(change, "provider", None) != "github"):
        from .memory import ensure_doc
        try:
            memory_doc = ensure_doc(config.memory_db, change)
        except OSError:
            pass

    # review-metadata.json is written for every completed analysis;
    # collect it (best effort) and use it as the completion marker.
    severity = None
    if metadata_json.is_file():
        metadata, _ = _collect_json(
            metadata_json,
            config.results_dir / f"review-metadata-{change.slug}{tag}.json")
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
                change, STATUS_FAILED, mode=config.mode, duration=duration,
                log_path=log_path, model=model, tokens=tokens,
                cost_usd=cost_usd, error=f"claude exited {returncode}")
        if not metadata_json.is_file():
            _log(f"[{change.slug}] {console.color('red', 'FAILED')} — "
                 f"review did not complete (no {METADATA_JSON_NAME}), "
                 f"see {log_path}")
            return ReviewResult(
                change, STATUS_FAILED, mode=config.mode, duration=duration,
                log_path=log_path, model=model, tokens=tokens,
                cost_usd=cost_usd,
                error=f"no {METADATA_JSON_NAME} produced — review did not "
                      "run to completion")
        # A completed clean review supersedes earlier findings
        # artifacts for the same change+patchset: drop the stale
        # findings JSON so the results directory never shows findings
        # the latest run withdrew, and write the report saying clean.
        dest_json = (config.results_dir /
                     f"{json_prefix}-{change.slug}{tag}.json")
        for stale in (dest_json, dest_json.with_suffix(".invalid")):
            if stale.exists():
                try:
                    stale.unlink()
                    _log(f"[{change.slug}] note: removed superseded "
                         f"{stale.name}")
                except OSError:
                    pass
        markdown_path = None
        try:
            markdown_path = write_review_markdown(
                config.results_dir, change, None, severity=severity,
                model=model, tokens=tokens, cost_usd=cost_usd,
                duration=duration, memory=memory_doc, tag=tag)
        except Exception as exc:  # noqa: BLE001 - the report is a
            # convenience; never fail the review over it
            _log(f"[{change.slug}] warning: markdown report failed: {exc}")
        _log(f"[{change.slug}] {console.color('green', 'clean')} — "
             f"no findings ({stats})")
        return ReviewResult(
            change, STATUS_CLEAN, mode=config.mode, severity=severity,
            model=model,
            tokens=tokens, cost_usd=cost_usd, duration=duration,
            markdown_path=markdown_path, log_path=log_path)

    dest_json = (config.results_dir /
                 f"{json_prefix}-{change.slug}{tag}.json")
    spec, error = _collect_json(review_json, dest_json)
    if spec is None:
        _log(f"[{change.slug}] {console.color('red', 'INVALID JSON')} "
             f"output: {error}")
        return ReviewResult(
            change, STATUS_INVALID_JSON, mode=config.mode,
            duration=duration,
            log_path=log_path, model=model, tokens=tokens,
            cost_usd=cost_usd, error=error)
    if getattr(change, "provider", None) == "github":
        try:
            validate_review_result(spec, worktree_dir, change.base_sha,
                                   change.sha)
        except Exception as exc:
            return ReviewResult(
                change, STATUS_INVALID_JSON, mode=config.mode,
                duration=duration, log_path=log_path, model=model,
                tokens=tokens, cost_usd=cost_usd, error=str(exc))

    findings = count_findings(spec)
    markdown_path = None
    try:
        markdown_path = write_review_markdown(
            config.results_dir, change, spec, severity=severity,
            model=model, tokens=tokens, cost_usd=cost_usd,
            duration=duration, memory=memory_doc, tag=tag)
    except Exception as exc:  # noqa: BLE001 - the report is a
        # convenience; never fail the review over it
        _log(f"[{change.slug}] warning: markdown report failed: {exc}")

    severity_color = {"urgent": "red", "high": "red",
                      "medium": "yellow"}.get(severity or "", "green")
    severity_note = (
        f", severity {console.color(severity_color, severity)}"
        if severity else "")
    _log(f"[{change.slug}] "
         f"{console.color('yellow', f'{findings} finding(s)')}"
         f"{severity_note} ({stats}) -> {dest_json.name}")
    return ReviewResult(
        change, STATUS_FINDINGS, mode=config.mode, findings=findings,
        severity=severity,
        model=model, tokens=tokens, cost_usd=cost_usd, duration=duration,
        json_path=dest_json, markdown_path=markdown_path,
        log_path=log_path)


def _review_and_cleanup(
    config: BatchConfig,
    change: ResolvedChange,
    worktree_dir: Path,
    tracker: Optional[ProgressTracker] = None,
    cleanup: bool = True,
) -> ReviewResult:
    """Worker wrapper: never lets an exception escape into the pool."""
    log_path = run_log_path(config, change)
    if tracker:
        tracker.start(change.slug, log_path)

    memory_path = None
    memory_before = None
    # GitHub PR reviews use their own prompt/output contract and have
    # no Gerrit Change-Id — the review memory does not apply to them.
    if (config.memory_db is not None
            and getattr(change, "provider", None) != "github"):
        from .memory import ensure_doc
        try:
            memory_path = ensure_doc(config.memory_db, change)
            # content comparison, not mtime — filesystem timestamp
            # granularity can lump a fast run into one tick
            memory_before = memory_path.read_bytes()
        except OSError as exc:
            _log(f"[{change.slug}] warning: memory doc unavailable: {exc}")

    try:
        result = run_review(config, change, worktree_dir,
                            log_path=log_path)
    except Exception as exc:  # noqa: BLE001 - one bad review must not
        # abort the batch or strand the other results
        _log(f"[{change.slug}] FAILED with unexpected error: {exc!r}")
        result = ReviewResult(change, STATUS_FAILED,
                              mode=config.mode, error=repr(exc))
    finally:
        if tracker:
            tracker.finish(change.slug)
        if cleanup and not config.keep_worktrees:
            wt.remove_worktree(config.repo, worktree_dir)

    result.agent = config.agent
    if memory_path is not None:
        result.memory_path = memory_path
        try:
            result.memory_updated = (
                memory_path.read_bytes() != memory_before)
        except OSError:
            result.memory_updated = False
        if not result.memory_updated and result.status in (
                STATUS_FINDINGS, STATUS_CLEAN):
            _log(f"[{change.slug}] warning: the review did not update "
                 f"its memory document ({memory_path.name})")
    return result


def _stash_stale_artifacts(config: BatchConfig, repo_dir: Path) -> None:
    """Move pre-existing review artifacts out of an in-place repo so a
    stale gerrit-review.json is never collected as this run's result."""
    for name in (REVIEW_JSON_NAME, REVIEW_RESULT_NAME, METADATA_JSON_NAME):
        path = repo_dir / name
        if path.exists():
            dest = config.results_dir / f"stale-{os.getpid()}-{name}"
            shutil.move(str(path), dest)
            _log(f"note: moved pre-existing {name} from the repo "
                 f"to {dest}")


def _remove_artifacts(repo_dir: Path) -> None:
    for name in (REVIEW_JSON_NAME, REVIEW_RESULT_NAME, METADATA_JSON_NAME):
        try:
            (repo_dir / name).unlink()
        except OSError:
            pass


def run_batch(
    config: BatchConfig,
    changes: list[ResolvedChange],
    in_place: bool = False,
) -> list[ReviewResult]:
    """Prepare worktrees sequentially, then review in parallel.

    With in_place=True (single local review of the checked-out HEAD),
    the review runs directly in config.repo — no worktree is created
    or removed, and the generated artifact files are cleaned from the
    repo after collection.

    Results collected so far (including on KeyboardInterrupt) are
    always persisted to the summary manifest.
    """
    if in_place and len(changes) != 1:
        raise ValueError("in_place reviews take exactly one change")
    config.results_dir.mkdir(parents=True, exist_ok=True)

    # (change, directory, cleanup?) — in-place runs use the repo
    # itself and must never be removed
    prepared: list[tuple[ResolvedChange, Path, bool]] = []
    results: list[ReviewResult] = []
    for change in changes:
        if in_place:
            _stash_stale_artifacts(config, config.repo)
            prepared.append((change, config.repo, False))
            continue
        try:
            prepared.append(
                (change, prepare_worktree(config, change), True))
        except Exception as exc:  # noqa: BLE001 - record and continue
            _log(f"[{change.slug}] worktree setup failed: {exc}")
            results.append(ReviewResult(
                change, STATUS_FAILED, mode=config.mode,
                agent=config.agent, error=str(exc)))

    interrupted = False
    try:
        if prepared:
            pool = ThreadPoolExecutor(max_workers=config.jobs)
            with ProgressTracker(total=len(prepared)) as tracker:
                futures = [
                    pool.submit(
                        _review_and_cleanup, config, change, wtree,
                        tracker, cleanup)
                    for change, wtree, cleanup in prepared
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
                        for _, wtree, cleanup in prepared:
                            if cleanup and wtree.exists():
                                wt.remove_worktree(config.repo, wtree)
                    if in_place:
                        _remove_artifacts(config.repo)
    finally:
        update_summary(config.results_dir, results, repo=config.repo)

    return results


def update_summary(results_dir: Path, results: list[ReviewResult],
                   repo: Optional[Path] = None) -> None:
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
            # Local reviews have no change number; key them by slug;
            # GitHub PRs by repo#number. Non-full modes are namespaced
            # (e.g. "64620-light") so a light run never replaces a
            # full run's manifest entry.
            key = (f"github:{change.project}#{change.number}"
                   if getattr(change, "provider", None) == "github"
                   else str(change.number) if change.number else change.slug)
            key += artifact_tag(result.mode)
            old = summary.get(key)

            entry = {
                "provider": getattr(change, "provider", "gerrit" if change.number else "local"),
                "number": change.number,
                "local": change.number is None,
                "mode": result.mode,
                "ref_name": getattr(change, "ref_name", None),
                "patchset": change.patchset,
                "sha": change.sha,
                "head_sha": change.sha,
                "base_sha": getattr(change, "base_sha", None),
                "repository": getattr(change, "project", None),
                "web_url": getattr(change, "url", None),
                "subject": change.subject,
                "base_url": change.base_url,
                "repo": str(repo) if repo else None,
                "status": result.status,
                "findings": result.findings,
                "severity": result.severity,
                "model": result.model,
                "agent": result.agent,
                "tokens": result.tokens,
                "cost_usd": result.cost_usd,
                "duration_s": round(result.duration),
                "json": result.json_path.name if result.json_path else None,
                "markdown": (f"{result.markdown_path.parent.name}/"
                             f"{result.markdown_path.name}"
                             if result.markdown_path else None),
                "memory": (str(result.memory_path)
                           if result.memory_path else None),
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
