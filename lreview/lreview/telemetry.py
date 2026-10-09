"""Where a review's time and money went, from its stream-json log.

Every model call re-reads the whole conversation, most of it from the
prompt cache, and writes what is new since the last call into it.  So a
review's cost is three things: what the calls wrote to the cache (each
token once, at a premium), what they read back (every earlier token, on
every later call), and what the model wrote (thinking included).  The
log carries the usage of every call and a timestamp on every message,
which is enough to say exactly what each call cost and how long the
model and the tools took; what a single tool output weighs is estimated
from the context growth of the call that read it, shared among that
step's outputs by size.

Codex logs carry one usage total and no per-call detail; they get
totals only.
"""

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# $ per million tokens: input, 5m cache write, 1h cache write, cache
# read, output.  A model missing here is priced from the run's own
# result event (its total is exact; the split is then left out).
PRICES = {
    "claude-fable-5-1": (10.0, 12.5, 20.0, 1.0, 50.0),
    "claude-fable-5": (10.0, 12.5, 20.0, 1.0, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-opus-5": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-8": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-sonnet-5-5": (2.0, 2.5, 4.0, 0.20, 10.0),
    "claude-sonnet-5": (2.0, 2.5, 4.0, 0.20, 10.0),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5": (1.0, 1.25, 2.0, 0.10, 5.0),
}

CHARS_PER_TOKEN = 4

# What a tool call was for.  Order matters: the first match wins.
CATEGORIES = (
    ("prompt", re.compile(
        r"review-prompts|light-prompt\.md|memory-protocol|lreview-db")),
    ("gerrit", re.compile(r"review\.whamcloud|\bgerrit\b|\bgc\s|/changes/")),
    ("jira", re.compile(r"jira\.whamcloud|\bjira\b")),
    ("git", re.compile(
        r"\bgit\s+(-C\s+\S+\s+)?(show|log|diff|blame|grep|rev-parse|"
        r"rev-list|cat-file|describe|merge-base|ls-files|branch|tag)")),
    ("result", re.compile(r"gerrit-review\.json|review-metadata\.json|"
                          r"review-result\.json|json\.tool")),
    ("checkpatch", re.compile(r"checkpatch|\bmake\b|\bgcc\b|\bsparse\b")),
)


def _ts(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _prices(model: str):
    for name, prices in PRICES.items():
        if model.startswith(name):
            return prices
    return None


def _describe(name: str, tool_input) -> str:
    """One line saying what a tool call did."""
    if not isinstance(tool_input, dict):
        return name
    for key in ("command", "file_path", "pattern", "path", "url",
                "description", "prompt", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            value = " ".join(value.split())
            if key == "pattern" and tool_input.get("path"):
                value += f" in {tool_input['path']}"
            return f"{name}: {value[:160]}"
    return name


# Where Claude Code puts a tool output too long to return inline; the
# agent then has to read it back with another call.
_SPILLED = re.compile(r"/tool-results/|/tmp/claude-\d+/")


def categorize(name: str, text: str) -> str:
    if _SPILLED.search(text) and name in ("Read", "Bash", "Grep"):
        return "spilled"
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "write"
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskList"):
        return "todo"
    if name in ("Agent", "Task"):
        return "subagent"
    if name.startswith("mcp__"):
        return "mcp"
    if name in ("WebFetch", "WebSearch"):
        return "web"
    for category, pattern in CATEGORIES:
        if pattern.search(text):
            return category
    if name in ("Read", "Grep", "Glob", "Bash", "LSP"):
        return "source"
    return "other"


def _result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += len(block.get("text") or "")
                if block.get("type") == "image":
                    total += 6000
        return total
    return 0


@dataclass
class Call:
    """One model call."""
    index: int
    agent: str              # "main", or the tool_use id of a subagent
    model: str
    start: Optional[float]
    end: Optional[float]
    new_input: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    tools: list = field(default_factory=list)   # tool_use ids
    label: str = ""         # the agent's task summary at the time
    thinking: int = 0       # streamed thinking-token estimate
    cost: float = 0.0

    @property
    def context(self) -> int:
        return self.new_input + self.cache_write + self.cache_read

    @property
    def seconds(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return max(0.0, self.end - self.start)


@dataclass
class ToolUse:
    """One tool call and what its output cost."""
    id: str
    name: str
    what: str
    category: str
    call: int               # index of the call that asked for it
    agent: str
    issued: Optional[float] = None
    answered: Optional[float] = None
    chars: int = 0
    error: bool = False
    tokens: int = 0         # its share of the next call's context growth
    later_calls: int = 0    # calls of the same agent that read it again
    write_cost: float = 0.0
    reread_cost: float = 0.0

    @property
    def seconds(self) -> float:
        if self.issued is None or self.answered is None:
            return 0.0
        return max(0.0, self.answered - self.issued)

    @property
    def cost(self) -> float:
        return self.write_cost + self.reread_cost


@dataclass
class Telemetry:
    log: str
    agent: str = "claude"
    model: str = ""
    session_id: Optional[str] = None
    version: Optional[str] = None
    calls: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    started: Optional[float] = None
    finished: Optional[float] = None
    # The agent's own totals, from its result event
    reported_cost: Optional[float] = None
    reported_tokens: Optional[int] = None
    reported_seconds: Optional[float] = None
    reported_api_seconds: Optional[float] = None
    reported_turns: Optional[int] = None
    thinking_tokens: Optional[int] = None
    reported_output: Optional[int] = None
    side_models: dict = field(default_factory=dict)
    subagents: int = 0
    mcp_servers: list = field(default_factory=list)
    tool_count: int = 0
    complete: bool = False

    # -- derived -------------------------------------------------------
    def main_calls(self):
        return [c for c in self.calls if c.agent == "main"]

    @property
    def wall(self) -> float:
        span = (self.finished - self.started
                if self.started and self.finished else 0.0)
        return max(self.reported_seconds or 0.0, span)

    @property
    def starting_context(self) -> int:
        main = self.main_calls()
        return main[0].context if main else 0

    @property
    def peak_context(self) -> int:
        return max((c.context for c in self.calls), default=0)

    def split(self) -> dict:
        """Dollars by token class, from the per-call usage."""
        out = defaultdict(float)
        for call in self.calls:
            prices = _prices(call.model)
            if not prices:
                continue
            p_in, p_w5, p_w1, p_read, p_out = (x / 1e6 for x in prices)
            out["input"] += call.new_input * p_in
            out["cache_write"] += ((call.cache_write - call.cache_write_1h)
                                   * p_w5 + call.cache_write_1h * p_w1)
            out["cache_read"] += call.cache_read * p_read
            out["output"] += call.output * p_out
        return dict(out)

    def model_seconds(self) -> float:
        return sum(c.seconds for c in self.main_calls())

    def tool_seconds(self) -> float:
        """Wall time the main agent spent waiting for tools: per step,
        from the request to the last answer (parallel calls overlap)."""
        by_call = defaultdict(list)
        for tool in self.tools:
            if tool.agent == "main" and tool.issued and tool.answered:
                by_call[tool.call].append(tool)
        total = 0.0
        for tools in by_call.values():
            total += (max(t.answered for t in tools)
                      - min(t.issued for t in tools))
        return total

    def by_category(self) -> dict:
        out = defaultdict(lambda: defaultdict(float))
        for tool in self.tools:
            row = out[tool.category]
            row["count"] += 1
            row["tokens"] += tool.tokens
            row["cost"] += tool.cost
            row["seconds"] += tool.seconds
        return {k: dict(v) for k, v in out.items()}

    def by_label(self) -> dict:
        """Calls, time and money per task summary the agent announced:
        a rough phase breakdown."""
        out = defaultdict(lambda: defaultdict(float))
        for call in self.main_calls():
            row = out[call.label or "(start)"]
            row["calls"] += 1
            row["cost"] += call.cost
            row["seconds"] += call.seconds
            row["first"] = row.get("first", call.index) or call.index
        return {k: dict(v) for k, v in out.items()}

    def summary(self) -> dict:
        split = self.split()
        main = self.main_calls()
        computed = sum(split.values())
        carried = sum(t.tokens for t in self.tools)
        return {
            "log": self.log,
            "agent": self.agent,
            "model": self.model,
            "complete": self.complete,
            "wall_s": round(self.wall, 1),
            "model_s": round(self.model_seconds(), 1),
            "tool_s": round(self.tool_seconds(), 1),
            "api_s": (round(self.reported_api_seconds, 1)
                      if self.reported_api_seconds else None),
            "calls": len(self.calls),
            "main_calls": len(main),
            "subagents": self.subagents,
            "tool_calls": len(self.tools),
            "cost_usd": (round(self.reported_cost, 4)
                         if self.reported_cost is not None
                         else round(computed, 4)),
            "computed_cost_usd": round(computed, 4),
            "split_usd": {k: round(v, 4) for k, v in split.items()},
            "side_models_usd": {k: round(v, 4)
                                for k, v in self.side_models.items()},
            "tokens": {
                "new_input": sum(c.new_input for c in self.calls),
                "cache_write": sum(c.cache_write for c in self.calls),
                "cache_read": sum(c.cache_read for c in self.calls),
                "output": sum(c.output for c in self.calls),
                "thinking": self.thinking_tokens,
            },
            "starting_context": self.starting_context,
            "peak_context": self.peak_context,
            "tool_output_tokens": carried,
            "categories": {k: {kk: round(vv, 4) for kk, vv in v.items()}
                           for k, v in self.by_category().items()},
        }

    def to_json(self) -> dict:
        data = self.summary()
        data["calls_detail"] = [
            {**asdict(c), "context": c.context, "seconds": round(c.seconds, 2)}
            for c in self.calls]
        data["tools_detail"] = [
            {**asdict(t), "seconds": round(t.seconds, 2),
             "cost": round(t.cost, 5)} for t in self.tools]
        data["phases"] = self.by_label()
        return data


def parse_log(path: Path) -> Telemetry:
    """Read a review's stream-json log into per-call telemetry."""
    path = Path(path)
    tel = Telemetry(log=path.name)
    calls: dict = {}            # message id -> Call
    order: list = []
    tools: dict = {}            # tool_use id -> ToolUse
    label = ""
    last_ts = {}                # agent -> timestamp of its last event
    pending_thinking = 0        # streamed before the call's first event

    with open(path, errors="replace") as handle:
        for line in handle:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            ts = _ts(event.get("timestamp"))
            agent = event.get("parent_tool_use_id") or "main"

            if kind == "system":
                sub = event.get("subtype")
                if sub == "init":
                    tel.model = event.get("model") or tel.model
                    tel.session_id = event.get("session_id")
                    tel.version = event.get("claude_code_version")
                    tel.tool_count = len(event.get("tools") or [])
                    tel.mcp_servers = [s.get("name") for s in
                                       event.get("mcp_servers") or []]
                elif sub == "thinking_tokens":
                    pending_thinking += (event.get(
                        "estimated_tokens_delta") or 0)
                elif sub == "task_summary" and event.get("detail"):
                    label = event["detail"]
                    tel.labels.append((len(order), label))
                continue

            if kind == "thread.started":
                tel.agent = "codex"
                continue
            if kind == "turn.completed":
                tel.agent = "codex"
                usage = event.get("usage") or {}
                tel.reported_tokens = (usage.get("input_tokens", 0)
                                       + usage.get("output_tokens", 0))
                tel.complete = True
                continue

            if kind == "assistant":
                msg = event.get("message") or {}
                mid = msg.get("id") or f"anon-{len(order)}"
                call = calls.get(mid)
                if call is None:
                    start = last_ts.get(agent) or tel.started
                    call = Call(index=len(order), agent=agent,
                                model=msg.get("model") or tel.model,
                                start=start, end=ts, label=label)
                    calls[mid] = call
                    order.append(call)
                    call.thinking, pending_thinking = pending_thinking, 0
                    if tel.started is None:
                        tel.started = ts
                        call.start = ts
                if ts:
                    call.end = ts
                usage = msg.get("usage") or {}
                if usage:
                    call.new_input = usage.get("input_tokens", 0) or 0
                    call.cache_write = (usage.get(
                        "cache_creation_input_tokens", 0) or 0)
                    call.cache_read = (usage.get(
                        "cache_read_input_tokens", 0) or 0)
                    call.output = max(call.output,
                                      usage.get("output_tokens", 0) or 0)
                    creation = usage.get("cache_creation") or {}
                    call.cache_write_1h = (creation.get(
                        "ephemeral_1h_input_tokens", 0) or 0)
                for block in msg.get("content") or []:
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "?")
                    what = _describe(name, block.get("input"))
                    text = json.dumps(block.get("input"))
                    tools[block["id"]] = ToolUse(
                        id=block["id"], name=name, what=what,
                        category=categorize(name, text),
                        call=call.index, agent=agent, issued=ts)
                    call.tools.append(block["id"])
                    if name in ("Agent", "Task"):
                        tel.subagents += 1
                if ts:
                    last_ts[agent] = ts
                    tel.finished = ts
                continue

            if kind == "user":
                msg = event.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_result"):
                            tool = tools.get(block.get("tool_use_id"))
                            if tool is None:
                                continue
                            tool.answered = ts
                            tool.chars = _result_chars(block.get("content"))
                            tool.error = bool(block.get("is_error"))
                if ts:
                    last_ts[agent] = ts
                    tel.finished = ts
                continue

            if kind == "result":
                tel.complete = event.get("subtype") == "success"
                tel.reported_cost = event.get("total_cost_usd")
                tel.reported_turns = event.get("num_turns")
                if event.get("duration_ms"):
                    tel.reported_seconds = event["duration_ms"] / 1000
                if event.get("duration_api_ms"):
                    tel.reported_api_seconds = event["duration_api_ms"] / 1000
                usage = event.get("usage") or {}
                tel.reported_tokens = sum(usage.get(k, 0) or 0 for k in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"))
                details = usage.get("output_tokens_details") or {}
                tel.thinking_tokens = details.get("thinking_tokens")
                tel.reported_output = usage.get("output_tokens")
                for name, entry in (event.get("modelUsage") or {}).items():
                    if not name.startswith(tel.model or "\0"):
                        tel.side_models[name] = entry.get("costUSD") or 0.0

    tel.calls = order
    tel.tools = list(tools.values())
    _attribute(tel)
    return tel


def _fix_output(tel: Telemetry) -> None:
    """Claude Code logs each message's output count as the message
    starts, so the per-call figures miss most of the thinking.  Give
    the shortfall against the result event's total back to the calls,
    by the thinking each streamed."""
    main_model = tel.model or ""
    calls = [c for c in tel.calls if c.model.startswith(main_model)]
    if not tel.reported_output or not calls:
        return
    missing = tel.reported_output - sum(c.output for c in calls)
    if missing <= 0:
        return
    weights = [c.thinking for c in calls]
    if not sum(weights):
        weights = [c.seconds or 1.0 for c in calls]
    total = sum(weights)
    for call, weight in zip(calls, weights):
        call.output += int(missing * weight / total)


def _attribute(tel: Telemetry) -> None:
    """Price every call, and charge each tool output for the cache
    write it caused and every later re-read of it."""
    _fix_output(tel)
    for call in tel.calls:
        prices = _prices(call.model)
        if not prices:
            continue
        p_in, p_w5, p_w1, p_read, p_out = (x / 1e6 for x in prices)
        call.cost = (call.new_input * p_in
                     + (call.cache_write - call.cache_write_1h) * p_w5
                     + call.cache_write_1h * p_w1
                     + call.cache_read * p_read + call.output * p_out)

    by_agent = defaultdict(list)
    for call in tel.calls:
        by_agent[call.agent].append(call)
    tools_by_call = defaultdict(list)
    for tool in tel.tools:
        tools_by_call[(tool.agent, tool.call)].append(tool)

    for agent, calls in by_agent.items():
        for pos, call in enumerate(calls):
            outputs = tools_by_call.get((agent, call.index))
            if not outputs or pos + 1 >= len(calls):
                for tool in outputs or ():
                    tool.tokens = tool.chars // CHARS_PER_TOKEN
                continue
            nxt = calls[pos + 1]
            # What the next call read beyond this one: this call's own
            # output plus the tool results it asked for.
            grown = nxt.context - call.context - call.output
            chars = sum(t.chars for t in outputs) or 1
            for tool in outputs:
                estimate = tool.chars // CHARS_PER_TOKEN
                share = (grown * tool.chars // chars if grown > 0
                         else estimate)
                # Thinking is dropped from later contexts, so growth can
                # undershoot; never trust it below a quarter of the size.
                tool.tokens = max(share, estimate // 4)
                later = calls[pos + 1:]
                tool.later_calls = len(later)
                prices = _prices(nxt.model)
                if not prices:
                    continue
                p_w1, p_read = prices[2] / 1e6, prices[3] / 1e6
                tool.write_cost = tool.tokens * p_w1
                tool.reread_cost = tool.tokens * p_read * (len(later) - 1)


def write_telemetry(log_path: Path, dest: Path,
                    run: Optional[dict] = None) -> Optional[dict]:
    """Parse a finished review's log and save the full breakdown next
    to it, with `run` (how lreview ran it); returns the summary, or
    None when the log cannot be read."""
    try:
        tel = parse_log(log_path)
    except OSError:
        return None
    data = tel.to_json()
    data["run"] = run or {}
    try:
        dest.write_text(json.dumps(data, indent=1))
    except OSError:
        pass
    return tel.summary()
