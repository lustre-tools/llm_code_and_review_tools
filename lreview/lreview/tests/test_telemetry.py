"""Tests for the per-review telemetry and lreview stats."""

import json
from argparse import Namespace

import pytest

from lreview.stats import cmd_stats, render
from lreview.telemetry import categorize, parse_log, write_telemetry

MODEL = "claude-opus-5-5"


def _assistant(mid, ts, usage, content):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": MODEL, "usage": usage,
                        "content": content}}


def _usage(new, write, read, out):
    return {"input_tokens": new, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read, "output_tokens": out,
            "cache_creation": {"ephemeral_1h_input_tokens": write,
                               "ephemeral_5m_input_tokens": 0}}


def _result(ts_ms, out, cost):
    return {"type": "result", "subtype": "success", "duration_ms": ts_ms,
            "duration_api_ms": ts_ms, "num_turns": 3,
            "total_cost_usd": cost,
            "usage": {"input_tokens": 3, "output_tokens": out,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0,
                      "output_tokens_details": {"thinking_tokens": 900}},
            "modelUsage": {MODEL: {"costUSD": cost},
                           "claude-haiku-4-5": {"costUSD": 0.001}}}


@pytest.fixture
def review_log(tmp_path):
    """Three calls: read a prompt file, git show, then finish.  The
    per-message output counts are the starting ones (10 each); the
    result event says 1500 in all, and the thinking estimates put 1000
    of the missing 1470 on the second call."""
    events = [
        {"type": "system", "subtype": "init", "model": MODEL,
         "session_id": "s1", "tools": ["Bash", "Read"],
         "mcp_servers": []},
        {"type": "system", "subtype": "thinking_tokens",
         "estimated_tokens_delta": 200},
        _assistant("m1", "2026-10-09T10:00:00.000Z", _usage(1, 10000, 0, 10),
                   [{"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "cat /x/review-prompts/kernel/"
                                          "review-core.md"}}]),
        {"type": "user", "timestamp": "2026-10-09T10:00:01.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": "p" * 8000}]}},
        {"type": "system", "subtype": "thinking_tokens",
         "estimated_tokens_delta": 1000},
        _assistant("m2", "2026-10-09T10:00:11.000Z",
                   _usage(1, 2010, 10000, 10),
                   [{"type": "tool_use", "id": "t2", "name": "Bash",
                     "input": {"command": "git show HEAD"}}]),
        {"type": "user", "timestamp": "2026-10-09T10:00:13.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                                  "content": "d" * 4000}]}},
        _assistant("m3", "2026-10-09T10:00:20.000Z",
                   _usage(1, 1010, 12010, 10),
                   [{"type": "text", "text": "done"}]),
        _result(20000, 1500, 0.2),
    ]
    log = tmp_path / "kreview-1_ps1-20261009-100000.1.log"
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return log


class TestParseLog:

    def test_calls_and_timing(self, review_log):
        tel = parse_log(review_log)
        assert tel.complete
        assert tel.model == MODEL
        assert [c.context for c in tel.calls] == [10001, 12011, 13021]
        assert tel.starting_context == 10001
        assert tel.peak_context == 13021
        # m2 waited from the t1 result (10:00:01) to 10:00:11
        assert tel.calls[1].seconds == pytest.approx(10.0)
        assert tel.tool_seconds() == pytest.approx(3.0)
        assert tel.side_models == {"claude-haiku-4-5": 0.001}

    def test_output_shortfall_goes_to_the_thinking_calls(self, review_log):
        tel = parse_log(review_log)
        assert sum(c.output for c in tel.calls) == pytest.approx(1500, abs=2)
        # 1470 missing, split 200:1000:0 by the streamed thinking
        assert tel.calls[0].output == 10 + 245
        assert tel.calls[1].output == 10 + 1225

    def test_tool_output_costs(self, review_log):
        tel = parse_log(review_log)
        prompt, git = tel.tools
        assert (prompt.category, git.category) == ("prompt", "git")
        # m2's context grew by 2010 over m1, less m1's 255 output tokens
        assert prompt.tokens == 2010 - 255
        assert prompt.later_calls == 2
        assert prompt.write_cost == pytest.approx(prompt.tokens * 8e-6)
        assert prompt.reread_cost == pytest.approx(prompt.tokens * 0.2e-6)
        assert git.later_calls == 1 and git.reread_cost == 0

    def test_split_matches_the_calls(self, review_log):
        tel = parse_log(review_log)
        split = tel.split()
        assert set(split) == {"input", "cache_write", "cache_read", "output"}
        assert sum(split.values()) == pytest.approx(
            sum(c.cost for c in tel.calls))
        assert split["cache_write"] == pytest.approx(13020 * 8e-6)

    def test_codex_log_has_totals_only(self, tmp_path):
        log = tmp_path / "kreview-2_ps1-x.1.log"
        log.write_text(
            json.dumps({"type": "thread.started"}) + "\n"
            + json.dumps({"type": "turn.completed",
                          "usage": {"input_tokens": 100,
                                    "output_tokens": 20}}) + "\n")
        tel = parse_log(log)
        assert tel.agent == "codex" and tel.complete
        assert tel.reported_tokens == 120 and tel.calls == []

    def test_cursor_log_has_totals_only(self, tmp_path):
        log = tmp_path / "kreview-3_ps1-x.1.log"
        log.write_text(
            json.dumps({"type": "system", "subtype": "init",
                        "model": "Auto Balance"}) + "\n"
            + json.dumps({"type": "result", "subtype": "success",
                          "usage": {"inputTokens": 100, "outputTokens": 20,
                                    "cacheReadTokens": 500,
                                    "cacheWriteTokens": 0}}) + "\n")
        tel = parse_log(log)
        assert tel.agent == "cursor" and tel.complete
        assert tel.reported_tokens == 620
        s = tel.summary()
        assert s["cost_usd"] is None
        assert s["tokens"]["cache_read"] == 500
        assert s["tokens"]["output"] == 20

    def test_cursor_tool_calls(self, tmp_path):
        def tool(sub, call_id, ms, kind, args, result=None):
            body = {"args": args}
            if result is not None:
                body["result"] = result
            return json.dumps({"type": "tool_call", "subtype": sub,
                               "call_id": call_id, "timestamp_ms": ms,
                               "tool_call": {kind: body}}) + "\n"
        log = tmp_path / "kreview-4_ps1-x.1.log"
        log.write_text(
            tool("started", "a", 1000, "shellToolCall",
                 {"command": "git -C /w show HEAD"})
            + tool("completed", "a", 3000, "shellToolCall",
                   {"command": "git -C /w show HEAD"},
                   {"success": {"stdout": "x" * 400}})
            + tool("started", "b", 4000, "shellToolCall",
                   {"command": "cd /w && git log"})
            + tool("completed", "b", 4500, "shellToolCall",
                   {"command": "cd /w && git log"}, {"rejected": {}})
            + json.dumps({"type": "result", "subtype": "success",
                          "duration_ms": 6000,
                          "usage": {"inputTokens": 1, "outputTokens": 1,
                                    "cacheReadTokens": 0,
                                    "cacheWriteTokens": 0}}) + "\n")
        tel = parse_log(log)
        assert tel.agent == "cursor"
        show, rejected = tel.tools
        assert (show.name, show.category, show.seconds) == ("Bash", "git", 2.0)
        assert show.chars == 400 and show.tokens == 100 and not show.error
        assert rejected.error
        assert tel.started == 1.0 and tel.finished == 4.5
        assert tel.summary()["tool_calls"] == 2

    def test_agy_log(self, tmp_path):
        events = [
            {"event": "init", "conversation_id": "conv-42",
             "init": {"model": "gemini-4-neon-high", "cwd": "/w",
                      "tools": ["run_command", "read_file"],
                      "permission_mode": "always-proceed"}},
            {"event": "step_update",
             "step_update": {"conversation_id": "conv-42", "step_index": 0,
                             "state": "DONE", "step_type": "user_input"}},
            {"event": "step_update",
             "step_update": {"conversation_id": "conv-42", "step_index": 1,
                             "state": "DONE", "step_type": "agent_response",
                             "duration_seconds": 2.0,
                             "usage": {"input_tokens": 10000,
                                       "output_tokens": 50,
                                       "thinking_tokens": 20,
                                       "cache_read_tokens": 0,
                                       "total_tokens": 10050}}},
            {"event": "step_update",
             "step_update": {"conversation_id": "conv-42", "step_index": 2,
                             "state": "ACTIVE", "step_type": "tool",
                             "tool_name": "run_command",
                             "tool_info": {"name": "run_command",
                                           "parameters": {
                                               "CommandLine": "git show HEAD"}}}},
            {"event": "step_update",
             "step_update": {"conversation_id": "conv-42", "step_index": 2,
                             "state": "DONE", "step_type": "tool",
                             "tool_name": "run_command",
                             "duration_seconds": 0.5,
                             "tool_info": {"name": "run_command",
                                           "parameters": {
                                               "CommandLine": "git show HEAD"},
                                           "output": "d" * 800}}},
            {"event": "step_update",
             "step_update": {"conversation_id": "conv-42", "step_index": 3,
                             "state": "ERROR", "step_type": "tool",
                             "tool_name": "read_file",
                             "duration_seconds": 0.1,
                             "tool_info": {"name": "read_file",
                                           "parameters": {"Path": "/w/a.c"},
                                           "error": {"type": "NotFound",
                                                     "message": "missing"}}}},
            {"event": "step_update",
             "step_update": {"conversation_id": "conv-42", "step_index": 4,
                             "state": "DONE", "step_type": "agent_response",
                             "duration_seconds": 1.5,
                             "usage": {"input_tokens": 1200,
                                       "output_tokens": 30,
                                       "thinking_tokens": 10,
                                       "cache_read_tokens": 10000,
                                       "total_tokens": 1230}}},
            {"event": "step_update",
             "step_update": {"conversation_id": "conv-42", "step_index": 5,
                             "state": "DONE", "step_type": "checkpoint",
                             "duration_seconds": 0.2,
                             "usage": {"input_tokens": 100,
                                       "output_tokens": 5,
                                       "thinking_tokens": 0,
                                       "cache_read_tokens": 0,
                                       "total_tokens": 105}}},
            {"event": "result",
             "result": {"conversation_id": "conv-42", "status": "SUCCESS",
                        "response": "done\n", "duration_seconds": 4.5,
                        "num_turns": 1,
                        "usage": {"input_tokens": 11300,
                                  "output_tokens": 85,
                                  "thinking_tokens": 30,
                                  "cache_read_tokens": 10000,
                                  "total_tokens": 11385}}},
        ]
        log = tmp_path / "kreview-6_ps1-x.1.log"
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        tel = parse_log(log)
        assert tel.agent == "agy" and tel.complete
        assert tel.model == "gemini-4-neon-high"
        assert tel.session_id == "conv-42"
        assert tel.reported_tokens == 21385
        assert len(tel.calls) == 2
        assert tel.starting_context == 10000
        assert tel.peak_context == 11200
        git_tool, read_tool = tel.tools
        assert (git_tool.name, git_tool.category, git_tool.seconds) == (
            "Bash", "git", 0.5)
        assert not git_tool.error
        assert (read_tool.name, read_tool.error) == ("Read", True)
        s = tel.summary()
        assert s["cost_usd"] is None
        assert s["wall_s"] == 4.5
        assert s["model_s"] == 3.5
        # step 2 (0.5s) and step 3 (0.1s) ran in the same turn, so wall tool
        # time overlaps to max(0.5, 0.1) = 0.5 rather than summing to 0.6.
        assert s["tool_s"] == 0.5
        assert s["tokens"] == {
            "new_input": 11300, "cache_write": 0,
            "cache_read": 10000, "output": 85, "thinking": 30}
        assert "cost total -, median -" in render([s], [tel.to_json()])

    def test_agy_read_file_byte_sizing(self, tmp_path):
        """agy's read_file emits 'N lines, M bytes'; scale by StartLine..EndLine
        so Read outputs get their real share of context growth."""
        events = [
            {"event": "init", "conversation_id": "c",
             "init": {"model": "gemini-4-neon"}},
            {"event": "step_update",
             "step_update": {"step_index": 1, "state": "DONE",
                             "step_type": "agent_response",
                             "duration_seconds": 1.0,
                             "usage": {"input_tokens": 10000,
                                       "cache_read_tokens": 0,
                                       "output_tokens": 100}}},
            {"event": "step_update",
             "step_update": {"step_index": 2, "state": "DONE",
                             "step_type": "tool", "tool_name": "read_file",
                             "duration_seconds": 0.2,
                             "tool_info": {"name": "read_file",
                                           "parameters": {"Path": "/w/full.c"},
                                           "output": "100 lines, 8000 bytes"}}},
            {"event": "step_update",
             "step_update": {"step_index": 3, "state": "DONE",
                             "step_type": "tool", "tool_name": "read_file",
                             "duration_seconds": 0.3,
                             "tool_info": {"name": "read_file",
                                           "parameters": {"Path": "/w/big.c",
                                                          "StartLine": 1,
                                                          "EndLine": 100},
                                           "output": "400 lines, 16000 bytes"}}},
            {"event": "step_update",
             "step_update": {"step_index": 4, "state": "DONE",
                             "step_type": "agent_response",
                             "duration_seconds": 1.0,
                             "usage": {"input_tokens": 3100,
                                       "cache_read_tokens": 10000,
                                       "output_tokens": 20}}},
            {"event": "result",
             "result": {"conversation_id": "c", "status": "SUCCESS",
                        "duration_seconds": 2.4, "num_turns": 1,
                        "usage": {"input_tokens": 13100,
                                  "cache_read_tokens": 10000,
                                  "output_tokens": 120}}},
        ]
        log = tmp_path / "kreview-7_ps1-x.1.log"
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        tel = parse_log(log)
        full_read, slice_read = tel.tools
        assert full_read.chars == 8000
        assert slice_read.chars == 4000
        # Context grew by 3100 - 100 = 3000 tokens, split 2:1 (8000 vs 4000)
        assert full_read.tokens == 2000
        assert slice_read.tokens == 1000
        assert tel.tool_seconds() == pytest.approx(0.3)

    def test_agy_subagent_step_counted(self, tmp_path):
        log = tmp_path / "kreview-9_ps1-x.1.log"
        events = [
            {"event": "init", "conversation_id": "c-sub",
             "init": {"model": "gemini-4-neon-high", "tools": ["invoke_subagent"]}},
            {"event": "step_update", "step_update": {
                "step_index": 1, "step_type": "agent_response", "state": "DONE",
                "duration_seconds": 1.0,
                "usage": {"input_tokens": 500, "cache_read_tokens": 0,
                          "output_tokens": 50}}},
            {"event": "step_update", "step_update": {
                "step_index": 2, "step_type": "subagent", "state": "ACTIVE",
                "tool_name": "invoke_subagent",
                "subagent_info": {"subagents": [
                    {"type_name": "lens", "role": "Concurrency Lens Reviewer"}]}}},
            {"event": "step_update", "step_update": {
                "step_index": 2, "step_type": "subagent", "state": "DONE",
                "tool_name": "invoke_subagent", "duration_seconds": 0.1,
                "subagent_info": {"subagents": [
                    {"type_name": "lens", "role": "Concurrency Lens Reviewer"}]}}},
            # Duplicate DONE for the same step_index is not double-counted
            {"event": "step_update", "step_update": {
                "step_index": 2, "step_type": "subagent", "state": "DONE",
                "tool_name": "invoke_subagent", "duration_seconds": 0.1,
                "subagent_info": {"subagents": [
                    {"type_name": "lens", "role": "Concurrency Lens Reviewer"}]}}},
            # A failed subagent launch does not increment tel.subagents
            {"event": "step_update", "step_update": {
                "step_index": 3, "step_type": "subagent", "state": "ERROR",
                "tool_name": "invoke_subagent", "duration_seconds": 0.05,
                "subagent_info": {"subagents": [
                    {"type_name": "lens", "role": "Build Lens Reviewer"}]}}},
            {"event": "result", "result": {
                "conversation_id": "c-sub", "status": "SUCCESS",
                "duration_seconds": 12.0, "num_turns": 1,
                "usage": {"input_tokens": 1000, "cache_read_tokens": 500,
                          "output_tokens": 100, "thinking_tokens": 20}}},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        tel = parse_log(log)
        assert tel.subagents == 1
        assert len(tel.tools) == 2
        assert tel.tools[0].name == "Agent"
        assert tel.tools[0].category == "subagent"
        assert "Concurrency Lens Reviewer" in tel.tools[0].what
        assert tel.tools[1].error is True

    def test_write_telemetry(self, review_log, tmp_path):
        dest = tmp_path / "t.json"
        summary = write_telemetry(review_log, dest, {"lean": True})
        data = json.loads(dest.read_text())
        assert data["run"] == {"lean": True}
        assert len(data["calls_detail"]) == 3
        assert summary["cost_usd"] == 0.2
        assert summary["categories"]["prompt"]["count"] == 1


@pytest.mark.parametrize("name,text,expected", [
    ("Bash", "cat /a/review-prompts/kernel/callstack.md", "prompt"),
    ("Bash", "curl https://review.whamcloud.com/changes/1", "gerrit"),
    ("Bash", "git -C /w show HEAD", "git"),
    ("Read", "/home/u/.claude/projects/x/tool-results/b.txt", "spilled"),
    ("Grep", "ll_file_open", "source"),
    ("Write", "gerrit-review.json", "write"),
])
def test_categorize(name, text, expected):
    assert categorize(name, text) == expected


class TestStats:

    def test_render(self, review_log):
        tel = parse_log(review_log)
        text = render([tel.summary()], [tel.to_json()], calls=True)
        assert "1 review(s)" in text
        assert "prompt" in text and "calls of" in text

    def test_render_without_costs(self, tmp_path):
        """codex and cursor report no dollar cost."""
        log = tmp_path / "kreview-5_ps1-x.1.log"
        log.write_text(json.dumps({"type": "result", "subtype": "success",
                                   "usage": {"inputTokens": 1}}) + "\n")
        tel = parse_log(log)
        text = render([tel.summary()], [tel.to_json()])
        assert "1 review(s): cost total -, median -" in text

    def test_cmd_stats_reads_a_directory(self, review_log, capsys):
        args = Namespace(paths=[str(review_log.parent)], results_dir=".",
                         last=5, complete=True, json=True, calls=False,
                         top=3)
        assert cmd_stats(args) == 0
        rows = json.loads(capsys.readouterr().out)
        assert [r["log"] for r in rows] == [review_log.name]

    def test_no_logs(self, tmp_path):
        assert render([]) == "no review logs found"


def test_codex_log(tmp_path):
    log = tmp_path / "kreview-3_ps1-x.1.log"
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {
            "id": "item_1", "type": "command_execution",
            "command": "/bin/bash -lc 'git show HEAD'",
            "aggregated_output": "x" * 400, "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "item_2",
                                            "type": "agent_message",
                                            "text": "done"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 2_000_000, "cached_input_tokens": 1_500_000,
            "output_tokens": 9000, "reasoning_output_tokens": 3000}}]
    log.write_text("\n".join(json.dumps(e) for e in events))
    s = parse_log(log).summary()
    assert s["agent"] == "codex" and s["cost_usd"] is None
    assert s["tokens"]["cache_read"] == 1_500_000
    assert s["tokens"]["new_input"] == 500_000
    assert s["plan_pct_est"] == pytest.approx(1.5 * 0.057)
    assert s["categories"]["git"]["count"] == 1
    assert s["main_calls"] == 2
