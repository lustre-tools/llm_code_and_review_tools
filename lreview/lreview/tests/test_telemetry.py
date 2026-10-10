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
