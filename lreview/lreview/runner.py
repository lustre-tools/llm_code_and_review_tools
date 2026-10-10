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
import shlex
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
from .models import validate_selection
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

# Live review process groups. Agents run in their own sessions (so
# the timeout path can group-kill them), which also means a Ctrl+C
# SIGINT never reaches them on its own — an interrupted batch must
# kill them explicitly or they keep running (and billing) headless.
_RUNNING_PGIDS: set = set()
_RUNNING_PGIDS_LOCK = threading.Lock()


def kill_running_reviews(grace: float = 5.0) -> int:
    """SIGTERM every live review process group, SIGKILL survivors
    after a grace period; returns how many were signalled."""
    with _RUNNING_PGIDS_LOCK:
        pgids = list(_RUNNING_PGIDS)
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if pgids:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with _RUNNING_PGIDS_LOCK:
                if not _RUNNING_PGIDS:
                    break
            time.sleep(0.2)
        with _RUNNING_PGIDS_LOCK:
            survivors = list(_RUNNING_PGIDS)
        for pgid in survivors:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return len(pgids)

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
    per-message output_tokens are partial.

    codex reports usage once, in the final turn.completed event, so
    this stays None until its run ends (the status line falls back to
    log size, which is the liveness signal that matters) and then
    reports the real total. Last resort is the legacy estimated_tokens
    event of older claude versions.
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
    tokens, _ = parse_final_usage(log_path)
    if tokens is not None:
        return tokens
    matches = _ESTIMATED_TOKENS_RE.findall(_read_tail(log_path, 16384))
    return int(matches[-1]) if matches else None


def parse_final_usage(log_path: Path):
    """Total tokens and cost from the agent's final usage event.

    claude's stream-json result event carries both. codex's
    turn.completed event carries usage only — a ChatGPT-plan run has
    no dollar figure to report — and its input_tokens already include
    the cached ones (cached_input_tokens is a subset, and
    reasoning_output_tokens a subset of output_tokens), so the total
    is input + output and nothing else.

    Returns (tokens, cost_usd), either possibly None.
    """
    tail = _read_tail(log_path, 262144)
    for line in reversed(tail.splitlines()):
        squashed = line.replace(" ", "")
        if ('"type":"result"' not in squashed
                and '"type":"turn.completed"' not in squashed):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "turn.completed":
            usage = obj.get("usage") or {}
            tokens = (usage.get("input_tokens", 0)
                      + usage.get("output_tokens", 0))
            return (tokens or None), None
        if obj.get("type") != "result":
            continue
        usage = obj.get("usage") or {}
        tokens = sum(
            usage.get(key, 0) for key in (
                "input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens"))
        return (tokens or None), obj.get("total_cost_usd")
    return None, None


# What claude prints when --resume names a session it does not have.
_NO_SESSION_TEXT = "No conversation found with session ID"


def parse_session_id(log_path: Path) -> Optional[str]:
    """The Claude session ID a stream-json log reports, or None.

    Every claude event carries it; the init event opens the log and the
    result event closes it, so the head and the tail are enough.
    """
    try:
        with open(log_path, errors="replace") as f:
            head = f.read(65536)
    except OSError:
        return None
    for text in (head, _read_tail(log_path, 65536)):
        for line in text.splitlines():
            if '"session_id"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("session_id"):
                return obj["session_id"]
    return None


def reviewed_label(change) -> str:
    """What a review run looked at: "ps<N> <sha12>" or "commit <sha12>"."""
    if change.patchset:
        return f"ps{change.patchset} {change.sha[:12]}"
    return f"commit {change.sha[:12]}"


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
    effort: Optional[str] = None
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    duration: float = 0.0
    json_path: Optional[Path] = None
    markdown_path: Optional[Path] = None
    memory_path: Optional[Path] = None
    memory_updated: bool = False
    memory_reviews: Optional[int] = None
    session_id: Optional[str] = None
    log_path: Optional[Path] = None
    error: Optional[str] = None
    telemetry: Optional[dict] = None


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
    # With memory_db and the claude agent: fork and continue the
    # session recorded in the memory document instead of starting cold
    resume: bool = True
    # Start the agent with only what a review uses (agents.LEAN_*)
    lean: bool = False
    # claude, full mode: put the protocol and the files it always loads
    # in the system prompt and the commit in the first message
    preload: bool = False
    # Added to the agent's environment (e.g. lreview bench's offline PATH)
    env: dict = field(default_factory=dict)
    agent_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # git -C <repo> resolves relative paths against the repo, not
        # our cwd — always hand git absolute paths.
        self.repo = Path(self.repo).expanduser().resolve()
        self.results_dir = Path(self.results_dir).expanduser().resolve()
        self.worktrees_dir = Path(self.worktrees_dir).expanduser().resolve()
        self.prompts_dir = Path(self.prompts_dir).expanduser().resolve()
        get_agent(self.agent)  # fail fast on unknown agents
        # A model that cannot do the requested effort only fails once
        # codex is running, after the worktree is built — check here,
        # before anything expensive happens.
        validate_selection(self.agent, self.model, self.effort)
        if self.mode not in REVIEW_MODES:
            raise ValueError(
                f"mode must be one of {REVIEW_MODES}, got {self.mode!r}")
        if self.jobs < 1:
            raise ValueError(f"jobs must be >= 1, got {self.jobs}")


def review_prompt(config: BatchConfig,
                  change: Optional[ResolvedChange] = None,
                  session=None,
                  worktree: Optional[Path] = None) -> str:
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
    Resuming a session, it first says where the code is now: the
    conversation remembers the previous run's worktree, which is gone.

    A change with a --since focus gets a closing section that limits
    the review to what changed from the earlier version.
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
    preloaded = _preloading(config, change)
    if preloaded:
        prompt = ("Run a deep dive regression analysis of the top commit "
                  "of the git repository in the current directory, "
                  "following the Lustre Patch Analysis Protocol in your "
                  "system prompt")
    elif config.mode == "light":
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
        if session is not None:
            prompt = (f"This continues your earlier review of this "
                      f"change, which looked at {session.reviewed}. That "
                      f"run's worktree is gone: the code is now checked "
                      f"out in {worktree}, at {now}. Review it again: "
                      + prompt)
    focus = getattr(change, "since", None)
    if focus is not None:
        from .since import focus_prompt
        prompt += ".\n\n" + focus_prompt(focus, change.sha)
    if preloaded and worktree is not None:
        prompt += commit_text(worktree)
    if preloaded and config.agent == "codex":
        # The protocol first: the same text leads every review's prompt,
        # so the provider's prefix cache shares it between them.
        prompt = preload_file(config).read_text() + "\n\n" + prompt
    return prompt


# What the protocol loads for every review, in its order.  Preloading
# them saves the agent the round trips, and it skips none of them.
PRELOAD_FILES = ("review-core.md", "technical-patterns.md",
                 "subsystem/build.md", "lustre-commit-message.md",
                 "lustre-style.md", "subsystem/subsystem.md")
# A command-line argument past 128KB fails to exec; leave the commit
# out of the prompt well before that.
PRELOAD_COMMIT_LIMIT = 96 * 1024


def _preloading(config: BatchConfig, change) -> bool:
    return (config.preload and config.mode == "full"
            and config.agent in ("claude", "codex")
            and getattr(change, "provider", None) != "github")


def _prompt_on_stdin(config: BatchConfig, change) -> bool:
    """codex has no system-prompt file: its preloaded protocol leads the
    first message, which goes in on stdin."""
    return config.agent == "codex" and _preloading(config, change)


def preload_file(config: BatchConfig) -> Path:
    """The protocol and its always-loaded files as one system-prompt
    file.  It is the same for every review with the same prompts, so
    concurrent reviews share its prompt cache."""
    parts = [
        "# Lustre review protocol (preloaded by lreview)\n\n"
        f"The prompt directory is {config.prompts_dir}. The protocol "
        "(review-core.md) and the files it always loads are included "
        "below exactly as they are there; they are already loaded, so "
        "do not read them again. Load every other file the protocol "
        "calls for from the prompt directory, as it directs.\n"]
    for name in PRELOAD_FILES:
        text = (config.prompts_dir / name).read_text()
        parts.append(f"\n\n======== {name} ========\n\n{text}")
    content = "".join(parts)
    import hashlib
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    dest = config.results_dir / f".preload-{digest}.md"
    if not dest.exists():
        dest.write_text(content)
    return dest


def commit_text(worktree: Path) -> str:
    """The commit under review, for the first message."""
    try:
        shown = subprocess.run(
            ["git", "-C", str(worktree), "show", "--stat", "--patch",
             "--format=fuller", "HEAD"],
            capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if not shown:
        return ""
    if len(shown.encode()) > PRELOAD_COMMIT_LIMIT:
        return (".\n\nThe commit is too large to include here; read it "
                "with git show HEAD.")
    return (".\n\nThe commit follows, as git show --stat --patch "
            "--format=fuller HEAD prints it; there is no need to show it "
            "again.\n\n" + shown)


def compaction_settings(doc: Path) -> str:
    """Claude settings for one review: when the conversation is
    compacted, a SessionStart hook puts the memory instructions back."""
    from .memory import MEMORY_PROMPT_PATH
    text = (f"The conversation was compacted. Before continuing the "
            f"review, re-read the memory protocol {MEMORY_PROMPT_PATH} "
            f"and your review memory document {doc}, and keep saving "
            f"your notes to that document as the protocol says.")
    return json.dumps({"hooks": {"SessionStart": [{
        "matcher": "compact",
        "hooks": [{"type": "command", "command": "echo " + shlex.quote(text)}],
    }]}})


def build_agent_cmd(config: BatchConfig,
                    change: Optional[ResolvedChange] = None,
                    session=None,
                    worktree: Optional[Path] = None) -> list[str]:
    """Headless review command for the configured agent.

    All agents receive the same instruction prompt; claude runs with
    stream-json output (events appear in the log as they happen, so
    the log doubles as a liveness/token signal — text mode buffers
    everything until the end).
    """
    spec = get_agent(config.agent)
    settings = None
    if (config.memory_db is not None and change is not None
            and getattr(change, "provider", None) != "github"):
        from .memory import ensure_doc
        settings = compaction_settings(ensure_doc(config.memory_db, change))
    return spec.build_cmd(
        config.model, config.effort, config.agent_args,
        review_prompt(config, change, session, worktree),
        resume=session.session_id if session is not None else None,
        settings=settings, lean=config.lean,
        system_file=(str(preload_file(config))
                     if _preloading(config, change)
                     and config.agent == "claude" else None),
        prompt_on_stdin=_prompt_on_stdin(config, change))


def prepare_worktree(config: BatchConfig, change: ResolvedChange) -> Path:
    """Fetch the change (if needed) and create its review worktree.

    The directory name carries the pid so concurrent lreview
    invocations reviewing the same change never remove each other's
    live worktrees.
    """
    # The repository's own remotes for the project first: a private
    # project refuses the anonymous URL, which stays as the fallback.
    urls = [change.fetch_url()]
    if getattr(change, "base_url", None) and getattr(change, "project", None):
        urls = wt.gerrit_remote_urls(config.repo, change.base_url,
                                     change.project) + urls
    if not wt.commit_exists(config.repo, change.sha):
        wt.fetch_change(config.repo, urls, change.ref)
        if not wt.commit_exists(config.repo, change.sha):
            raise wt.GitError(
                f"fetched {change.ref} but {change.sha} still missing")
    if getattr(change, "base_sha", None) and not wt.commit_exists(config.repo, change.base_sha):
        wt.fetch_change(config.repo, urls, change.base_sha)
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


def _run_agent(cmd: list[str], cwd: Path, log_path: Path,
               timeout: int, extra_env: Optional[dict] = None,
               stdin_path: Optional[Path] = None) -> int:
    """Run the agent in its own process group; kill the whole group on
    timeout so MCP servers / hook children don't outlive the review.

    Raises subprocess.TimeoutExpired after the group is killed.
    """
    with open(log_path, "w") as log_file:
        env = os.environ.copy()
        # The reviewer needs its own agent credentials only. GitHub
        # credentials belong to the parent poster and must never reach
        # an agent or its shell tools.
        env.pop("GH_TOKEN", None); env.pop("GITHUB_TOKEN", None)
        if os.environ.get("CI"):
            env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        env.update(extra_env or {})
        stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=stdin,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        if stdin_path:
            stdin.close()
        with _RUNNING_PGIDS_LOCK:
            _RUNNING_PGIDS.add(proc.pid)
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
        finally:
            with _RUNNING_PGIDS_LOCK:
                _RUNNING_PGIDS.discard(proc.pid)


def _collect_json(source: Path, dest: Path, validate=None):
    """Load and copy a JSON artifact; returns (spec, error).

    Output that does not parse or that `validate` rejects is kept as
    dest.invalid, never under dest: an earlier run's manifest entry
    may name dest, and `post` would send whatever is there.
    """
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
    if validate is not None:
        try:
            validate(spec)
        except Exception as exc:  # noqa: BLE001 - any rejection
            try:
                shutil.copy(source, dest.with_suffix(".invalid"))
            except OSError:
                pass
            return None, str(exc)
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
    session=None,
) -> ReviewResult:
    """Run one headless kreview in its worktree and collect the output."""
    tag = artifact_tag(config.mode)
    json_prefix = ("review-result"
                   if getattr(change, "provider", None) == "github"
                   else "gerrit-review")
    if log_path is None:
        log_path = run_log_path(config, change)
    cmd = build_agent_cmd(config, change, session, worktree_dir)
    extra_env = {**get_agent(config.agent).env(config.lean), **config.env}
    stdin_path = None
    if _prompt_on_stdin(config, change):
        stdin_path = log_path.with_suffix(".prompt.md")
        stdin_path.write_text(
            review_prompt(config, change, session, worktree_dir))
    start = time.monotonic()

    _log(f"[{change.slug}] {console.color('cyan', 'review started')}: "
         f"{change.subject[:60]}")
    try:
        returncode = _run_agent(cmd, worktree_dir, log_path,
                                config.timeout, extra_env, stdin_path)
        if (session is not None and returncode != 0
                and _NO_SESSION_TEXT in _read_tail(log_path, 4096)):
            _log(f"[{change.slug}] note: Claude could not resume session "
                 f"{session.session_id}; starting a fresh one")
            cmd = build_agent_cmd(config, change, None, worktree_dir)
            returncode = _run_agent(cmd, worktree_dir, log_path,
                                    config.timeout, extra_env)
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
                cost_usd=cost_usd,
                error=f"{config.agent} exited {returncode}")
        if getattr(change, "provider", None) == "github":
            # A PR review writes review-result.json even when clean;
            # review-metadata.json alone is the Gerrit contract, which
            # the review-core.md prompt can lead the agent to follow.
            error = f"no {REVIEW_RESULT_NAME} produced"
            if (worktree_dir / REVIEW_JSON_NAME).is_file():
                error += f" (the agent wrote {REVIEW_JSON_NAME} instead)"
            _log(f"[{change.slug}] {console.color('red', 'FAILED')} — "
                 f"{error}, see {log_path}")
            return ReviewResult(
                change, STATUS_FAILED, mode=config.mode, duration=duration,
                log_path=log_path, model=model, tokens=tokens,
                cost_usd=cost_usd, error=error)
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
    validate = None
    if getattr(change, "provider", None) == "github":
        def validate(spec):
            validate_review_result(spec, worktree_dir, change.base_sha,
                                   change.sha)
    spec, error = _collect_json(review_json, dest_json, validate)
    if spec is None:
        _log(f"[{change.slug}] {console.color('red', 'INVALID JSON')} "
             f"output: {error}")
        return ReviewResult(
            change, STATUS_INVALID_JSON, mode=config.mode,
            duration=duration,
            log_path=log_path, model=model, tokens=tokens,
            cost_usd=cost_usd, error=error)

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

    session = None
    if memory_path is not None and config.agent == "claude" and config.resume:
        from .agents import claude_session_exists
        from .memory import read_session
        session = read_session(memory_path, config.mode)
        if session is not None and not claude_session_exists(
                session.session_id):
            _log(f"[{change.slug}] note: Claude no longer has session "
                 f"{session.session_id}; starting a fresh one from the "
                 "memory document")
            session = None
        elif session is not None:
            _log(f"[{change.slug}] resuming Claude session "
                 f"{session.session_id} ({session.reviewed})")

    try:
        result = run_review(config, change, worktree_dir,
                            log_path=log_path, session=session)
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
    result.effort = config.effort
    if config.agent == "claude" and result.log_path:
        result.session_id = parse_session_id(result.log_path)
    if result.log_path and result.log_path.is_file():
        result.telemetry = record_telemetry(config, result)
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
        # Count completed -m iterations in the document itself; the
        # counter is bumped here (deterministically), never by the
        # agent, and only for runs that finished the analysis.
        if result.status in (STATUS_FINDINGS, STATUS_CLEAN):
            from .memory import bump_review_count
            try:
                result.memory_reviews = bump_review_count(
                    memory_path,
                    doc_includes_this_run=result.memory_updated)
            except OSError as exc:
                _log(f"[{change.slug}] warning: could not bump the "
                     f"memory review counter: {exc}")
            if result.session_id:
                from .memory import record_session
                try:
                    record_session(memory_path, config.mode,
                                   result.session_id, reviewed_label(change))
                except OSError as exc:
                    _log(f"[{change.slug}] warning: could not record the "
                         f"Claude session: {exc}")
    return result


def record_telemetry(config: BatchConfig, result: ReviewResult):
    """Save where the review's time and money went beside its log, and
    return the summary the manifest keeps."""
    from .telemetry import write_telemetry
    from .prompts import _git_out
    run = {
        "mode": config.mode, "agent": config.agent, "model": config.model,
        "effort": config.effort, "lean": config.lean,
        "preload": config.preload,
        "memory": config.memory_db is not None,
        "since": getattr(result.change, "since", None) is not None,
        "prompts_rev": _git_out(config.prompts_dir, "rev-parse",
                                "--short=12", "HEAD"),
        "status": result.status, "findings": result.findings,
        "duration_s": round(result.duration, 1),
    }
    try:
        summary = write_telemetry(
            result.log_path, telemetry_path(result.log_path), run)
    except Exception as exc:  # noqa: BLE001 - never fail a review on it
        _log(f"[{result.change.slug}] warning: telemetry failed: {exc}")
        return None
    if not summary:
        return None
    keep = ("wall_s", "model_s", "tool_s", "calls", "main_calls",
            "tool_calls", "starting_context", "peak_context", "split_usd",
            "tokens")
    compact = {k: summary.get(k) for k in keep}
    compact["categories_usd"] = {
        k: round(v.get("cost", 0), 4)
        for k, v in (summary.get("categories") or {}).items()}
    return compact


def telemetry_path(log_path: Path) -> Path:
    return log_path.with_suffix(".telemetry.json")


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
    if not in_place and not config.keep_worktrees:
        stranded = wt.reap_orphan_worktrees(config.worktrees_dir)
        if stranded:
            _log(f"reaped {stranded} worktree(s) left by killed runs")

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
                agent=config.agent, effort=config.effort,
                error=str(exc)))

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
                    killed = kill_running_reviews()
                    _log(f"interrupted — killed {killed} running "
                         "review(s), cancelling pending ones")
                    pool.shutdown(wait=True, cancel_futures=True)
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
                "since": ({"ref": change.since.ref, "sha": change.since.sha}
                          if getattr(change, "since", None) else None),
                "status": result.status,
                "findings": result.findings,
                "severity": result.severity,
                "model": result.model,
                "agent": result.agent,
                "effort": result.effort,
                "tokens": result.tokens,
                "cost_usd": result.cost_usd,
                "duration_s": round(result.duration),
                "json": result.json_path.name if result.json_path else None,
                "markdown": (f"{result.markdown_path.parent.name}/"
                             f"{result.markdown_path.name}"
                             if result.markdown_path else None),
                "memory": (str(result.memory_path)
                           if result.memory_path else None),
                "memory_reviews": result.memory_reviews,
                "log": result.log_path.name if result.log_path else None,
                "telemetry": result.telemetry,
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
