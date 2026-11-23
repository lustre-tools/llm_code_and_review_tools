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

from .defaults import DEFAULT_AGENT

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
CODEX_PLAN_PCT_PER_MTOK = 0.057

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
    for key in ("command", "CommandLine", "file_path", "Path", "TargetFile",
                "pattern", "Pattern", "Query", "path", "DirectoryPath",
                "SearchPath", "url", "Url", "description", "prompt",
                "Prompt", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            value = " ".join(value.split())
            if key == "pattern" and tool_input.get("path"):
                value += f" in {tool_input['path']}"
            elif key == "Query" and tool_input.get("SearchPath"):
                value += f" in {tool_input['SearchPath']}"
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


# The result event's usage keys: claude's, agy's cache_read_tokens,
# then cursor's (camelCase). Each set is disjoint, cached input
# included once.
RESULT_USAGE_KEYS = (
    "input_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens", "cache_read_tokens", "output_tokens",
    "inputTokens", "cacheWriteTokens", "cacheReadTokens", "outputTokens")


def result_tokens(usage: dict) -> int:
    """Total tokens in a result event's usage (claude, cursor, or agy)."""
    return sum(usage.get(key, 0) or 0 for key in RESULT_USAGE_KEYS)


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
    agent: str = DEFAULT_AGENT
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
    # codex reports one usage total and no per-call detail
    codex_usage: dict = field(default_factory=dict)
    codex_messages: int = 0
    # cursor reports its totals once, in the result event, and no
    # per-call usage
    cursor_usage: dict = field(default_factory=dict)
    # agy reports cumulative usage in its result event (including any
    # checkpoint calls) and per-step usage on agent_response steps
    agy_usage: dict = field(default_factory=dict)
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

    def _tokens(self) -> dict:
        if self.codex_usage:
            u = self.codex_usage
            return {"new_input": u["input_tokens"] - u["cached_input_tokens"],
                    "cache_write": 0,
                    "cache_read": u["cached_input_tokens"],
                    "output": u["output_tokens"],
                    "thinking": u["reasoning_output_tokens"]}
        if self.cursor_usage:
            u = self.cursor_usage
            return {"new_input": u["inputTokens"],
                    "cache_write": u["cacheWriteTokens"],
                    "cache_read": u["cacheReadTokens"],
                    "output": u["outputTokens"],
                    "thinking": None}
        if self.agy_usage:
            u = self.agy_usage
            return {"new_input": u["input_tokens"],
                    "cache_write": 0,
                    "cache_read": u["cache_read_tokens"],
                    "output": u["output_tokens"],
                    "thinking": u["thinking_tokens"]}
        return {"new_input": sum(c.new_input for c in self.calls),
                "cache_write": sum(c.cache_write for c in self.calls),
                "cache_read": sum(c.cache_read for c in self.calls),
                "output": sum(c.output for c in self.calls),
                "thinking": self.thinking_tokens}

    def plan_estimate(self) -> Optional[float]:
        """codex: an upper bound on the share of the weekly plan the
        review used, from cached input (measured 2026-09-30 on
        gpt-6.1-sol at 0.057% per million; nearly all of a run's
        volume is cached input)."""
        if not self.codex_usage:
            return None
        return round(self.codex_usage["cached_input_tokens"] / 1e6
                     * CODEX_PLAN_PCT_PER_MTOK, 4)

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
            "main_calls": (len(main) if not self.codex_usage
                           else len(self.tools) + 1),
            "subagents": self.subagents,
            "tool_calls": len(self.tools),
            "cost_usd": (round(self.reported_cost, 4)
                         if self.reported_cost is not None
                         else None if self.agent in ("codex", "cursor", "agy")
                         else round(computed, 4)),
            "computed_cost_usd": round(computed, 4),
            "split_usd": {k: round(v, 4) for k, v in split.items()},
            "side_models_usd": {k: round(v, 4)
                                for k, v in self.side_models.items()},
            "tokens": self._tokens(),
            "plan_pct_est": self.plan_estimate(),
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


# cursor's tool kinds, by the names claude gives the same tools
_CURSOR_TOOLS = {"shell": "Bash", "read": "Read", "edit": "Edit",
                 "write": "Write", "grep": "Grep", "glob": "Glob",
                 "ls": "LS", "delete": "Delete"}


def cursor_tool(event: dict):
    """(name, args, result) of a cursor tool_call event."""
    call = event.get("tool_call") or {}
    for key, body in call.items():
        if key.endswith("ToolCall") and isinstance(body, dict):
            kind = key[:-len("ToolCall")]
            name = _CURSOR_TOOLS.get(kind, kind[:1].upper() + kind[1:])
            return name, body.get("args") or {}, body.get("result")
    return "?", {}, None


def _cursor_tool(event: dict, ts, tools: dict, call: int) -> None:
    """Record one cursor tool_call event: started issues the call,
    completed answers it."""
    call_id = event.get("call_id") or f"cu{len(tools)}"
    name, args, result = cursor_tool(event)
    tool = tools.get(call_id)
    if tool is None:
        tool = tools[call_id] = ToolUse(
            id=call_id, name=name, what=_describe(name, args),
            category=categorize(name, json.dumps(args)),
            call=max(call, 0), agent="main", issued=ts)
    if event.get("subtype") != "completed":
        return
    tool.answered = ts
    result = result if isinstance(result, dict) else {}
    # "success", or "rejected", "error", "writePermissionDenied", ...
    outcome = next(iter(result), "")
    tool.error = outcome != "success"
    body = result.get(outcome)
    if isinstance(body, dict):
        tool.chars = len(str(body.get("content") or body.get("stdout")
                             or body.get("errorMessage") or ""))
    # No per-call usage to measure the context growth by, as for codex
    tool.tokens = tool.chars // CHARS_PER_TOKEN


# agy's tool names, mapped to the canonical names used in categorize()
_AGY_TOOLS = {
    "run_command": "Bash",
    "read_file": "Read",
    "view_file": "Read",
    "write_file": "Write",
    "write_to_file": "Write",
    "edit_file": "Edit",
    "replace_file_content": "Edit",
    "multi_replace_file_content": "Edit",
    "sed_file": "Edit",
    "grep_search": "Grep",
    "find_by_name": "Glob",
    "list_dir": "LS",
    "search_web": "WebSearch",
    "read_url_content": "WebFetch",
    "invoke_subagent": "Agent",
}


_AGY_READ_SUMMARY_RE = re.compile(r"^(\d+) lines?, (\d+) bytes?$")


def _agy_tool_chars(name: str, params: dict, info: dict) -> int:
    """Output size in chars; parses agy read_file's 'N lines, M bytes' summary."""
    out_text = info.get("output")
    if out_text is None:
        err = info.get("error")
        if isinstance(err, dict):
            out_text = err.get("message")
    text = str(out_text or "")
    if name == "Read":
        match = _AGY_READ_SUMMARY_RE.match(text.strip())
        if match:
            total_lines = int(match.group(1))
            total_bytes = int(match.group(2))
            start = params.get("StartLine")
            end = params.get("EndLine")
            if total_lines > 0 and (isinstance(start, int)
                                    or isinstance(end, int)):
                s = start if isinstance(start, int) and start >= 1 else 1
                e = end if isinstance(end, int) and end >= s else total_lines
                read_lines = min(total_lines, max(1, e - s + 1))
                return max(1, total_bytes * read_lines // total_lines)
            return total_bytes
    return len(text)


def agy_tool(step_update: dict):
    """(name, params, info) of an agy tool or subagent step_update."""
    info = step_update.get("tool_info")
    info = info if isinstance(info, dict) else {}
    raw_name = step_update.get("tool_name") or info.get("name") or "?"
    name = _AGY_TOOLS.get(raw_name, raw_name)
    params = info.get("parameters")
    params = params if isinstance(params, dict) else {}
    if not params:
        sub_info = step_update.get("subagent_info")
        subagents = (sub_info.get("subagents")
                     if isinstance(sub_info, dict) else None) or []
        if subagents and isinstance(subagents[0], dict):
            s = subagents[0]
            label = s.get("type_name") or "subagent"
            if s.get("role"):
                label = f"{label} ({s['role']})"
            params = {"description": label}
    return name, params, info


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
    agy_clock = 1.0             # synthetic clock >= 1.0 (truthy) for agy durations

    with open(path, errors="replace") as handle:
        for line in handle:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            agy_event = event.get("event")
            if agy_event == "init":
                tel.agent = "agy"
                tel.session_id = event.get("conversation_id") or tel.session_id
                init = event.get("init") or {}
                tel.model = init.get("model") or tel.model
                tel.tool_count = len(init.get("tools") or [])
                continue
            if agy_event == "step_update":
                tel.agent = "agy"
                su = event.get("step_update") or {}
                stype = su.get("step_type")
                state = su.get("state")
                idx = su.get("step_index")
                dur = su.get("duration_seconds") or 0.0
                if stype == "agent_response" and state == "DONE":
                    start = agy_clock
                    agy_clock += dur
                    usage = su.get("usage") or {}
                    call = Call(
                        index=len(order), agent="main", model=tel.model,
                        start=start, end=agy_clock, label=label,
                        new_input=usage.get("input_tokens", 0) or 0,
                        cache_read=usage.get("cache_read_tokens", 0) or 0,
                        output=usage.get("output_tokens", 0) or 0,
                        thinking=usage.get("thinking_tokens", 0) or 0)
                    order.append(call)
                elif stype in ("tool", "subagent"):
                    call_id = f"agy:{idx}" if idx is not None else f"agy:{len(tools)}"
                    name, params, info = agy_tool(su)
                    tool = tools.get(call_id)
                    if tool is None:
                        call_idx = max(len(order) - 1, 0)
                        tool = tools[call_id] = ToolUse(
                            id=call_id, name=name, what=_describe(name, params),
                            category=categorize(name, json.dumps(params)),
                            call=call_idx, agent="main")
                        if order:
                            order[-1].tools.append(call_id)
                    if state in ("DONE", "ERROR"):
                        already_done = tool.answered is not None
                        # Overlap parallel tool calls from the requesting call's end.
                        issued = order[-1].end if order else agy_clock
                        tool.issued = issued
                        tool.answered = issued + dur
                        agy_clock = max(agy_clock, tool.answered)
                        err = info.get("error")
                        tool.error = bool(err) or state == "ERROR"
                        tool.chars = _agy_tool_chars(name, params, info)
                        if state == "DONE" and not already_done:
                            sub_info = su.get("subagent_info") or {}
                            subagents = sub_info.get("subagents") or []
                            if subagents:
                                tel.subagents += len(subagents)
                            elif name == "Agent":
                                tel.subagents += 1
                continue
            if agy_event == "result":
                tel.agent = "agy"
                res = event.get("result") or {}
                tel.complete = res.get("status") == "SUCCESS"
                tel.session_id = res.get("conversation_id") or tel.session_id
                tel.reported_turns = res.get("num_turns")
                if res.get("duration_seconds"):
                    tel.reported_seconds = res["duration_seconds"]
                usage = res.get("usage") or {}
                tel.agy_usage = {k: usage.get(k, 0) or 0 for k in (
                    "input_tokens", "cache_read_tokens",
                    "output_tokens", "thinking_tokens")}
                tel.reported_tokens = result_tokens(usage)
                tel.reported_output = usage.get("output_tokens")
                tel.thinking_tokens = usage.get("thinking_tokens")
                continue

            kind = event.get("type")
            ts = _ts(event.get("timestamp"))
            if ts is None and event.get("timestamp_ms"):
                ts = event["timestamp_ms"] / 1000  # cursor
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

            if kind == "tool_call":
                tel.agent = "cursor"
                _cursor_tool(event, ts, tools, len(order) - 1)
                if ts:
                    tel.started = tel.started or ts
                    tel.finished = ts
                continue

            if kind == "thread.started":
                tel.agent = "codex"
                continue
            if kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "command_execution":
                    command = item.get("command") or ""
                    what = "Bash: " + " ".join(command.split())[:160]
                    tools[item.get("id") or f"cx{len(tools)}"] = ToolUse(
                        id=item.get("id") or "", name="Bash", what=what,
                        category=categorize("Bash", command), call=len(tools),
                        agent="main",
                        chars=len(item.get("aggregated_output") or ""),
                        tokens=len(item.get("aggregated_output") or "")
                        // CHARS_PER_TOKEN,
                        error=item.get("exit_code") not in (0, None))
                elif item.get("type") == "agent_message":
                    tel.codex_messages += 1
                continue
            if kind == "turn.completed":
                tel.agent = "codex"
                usage = event.get("usage") or {}
                # codex counts cached input inside input_tokens
                tel.codex_usage = {k: usage.get(k, 0) or 0 for k in (
                    "input_tokens", "cached_input_tokens", "output_tokens",
                    "reasoning_output_tokens")}
                tel.reported_tokens = (usage.get("input_tokens", 0)
                                       + usage.get("output_tokens", 0))
                tel.reported_output = usage.get("output_tokens")
                tel.thinking_tokens = usage.get("reasoning_output_tokens")
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
                tel.reported_tokens = result_tokens(usage)
                details = usage.get("output_tokens_details") or {}
                tel.thinking_tokens = details.get("thinking_tokens")
                tel.reported_output = usage.get("output_tokens")
                if "inputTokens" in usage:
                    tel.agent = "cursor"
                    tel.cursor_usage = {k: usage.get(k, 0) or 0 for k in (
                        "inputTokens", "cacheWriteTokens", "cacheReadTokens",
                        "outputTokens")}
                    tel.reported_output = tel.cursor_usage["outputTokens"]
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
    if tel.agent == "agy":
        return
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
                # Written at whichever cache rate the call that read it used
                p_write = (prices[2] if nxt.cache_write_1h
                           or not nxt.cache_write else prices[1]) / 1e6
                p_read = prices[3] / 1e6
                tool.write_cost = tool.tokens * p_write
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
