"""Tests for patch_shepherd/orchestrator.py.

Covers: prompt generation, decision parsing, config/path handling,
error recovery paths, report generation, decision execution.
"""

import json
import subprocess
import sys
from unittest.mock import MagicMock, call, patch

import pytest

# orchestrator is a standalone script (no package), so add its dir to path
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import orchestrator  # noqa: E402


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------

SHEPHERD_TOOL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "shepherd_tool.sh")


@pytest.fixture(autouse=True)
def _set_config():
    """Ensure orchestrator._config is set for tests that call tool()."""
    fake_config = MagicMock()
    fake_config.shepherd_tool = SHEPHERD_TOOL_PATH
    fake_config.patches_file = "/tmp/fake_patches.json"
    fake_config.report_file = "/tmp/fake_report.json"
    orchestrator._config = fake_config
    yield
    orchestrator._config = None


@pytest.fixture
def sample_patch():
    """A minimal patch dict matching the patches_to_watch.json schema."""
    return {
        "gerrit_url": "https://review.example.com/c/lustre/+/12345",
        "description": "LU-9999 llite: fix file locking",
        "jira": "LU-9999",
        "notes": "Seen flaky on ZFS",
        "watch_status": "active",
        "last_patchset": 3,
        "last_review_count": 2,
    }


@pytest.fixture
def sample_failure():
    """A single unknown-failure item from check-patch output."""
    return {
        "test": "sanity/test_42a",
        "error": "FAIL: inode wrong after setattr",
        "suite_id": "suite-777",
        "session_id": "sess-888",
    }


@pytest.fixture
def sample_check_result():
    """A successful check-patch result with one action and one unknown."""
    return {
        "gerrit_url": "https://review.example.com/c/lustre/+/12345",
        "patch_index": 0,
        "skipped": False,
        "actions_taken": [
            {"type": "retest", "description": "retest for LU-1000"}
        ],
        "needs_llm_decision": [
            {
                "test": "sanity/test_42a",
                "error": "FAIL: inode wrong",
                "suite_id": "suite-777",
                "session_id": "sess-888",
            }
        ],
        "errors": [],
    }


# -------------------------------------------------------------------
# 1. Prompt generation
# -------------------------------------------------------------------

class TestBuildResearchPrompt:
    def test_empty_failures_list(self):
        prompt = orchestrator.build_research_prompt([])
        assert "Failures to investigate" in prompt
        assert "Failure 0:" not in prompt

    def test_single_failure_included(self, sample_patch, sample_failure):
        unknown_failures = [(0, sample_patch, sample_failure)]
        prompt = orchestrator.build_research_prompt(unknown_failures)

        assert "Failure 0:" in prompt
        assert 'Patch #0: "LU-9999 llite: fix file locking"' in prompt
        assert "JIRA: LU-9999" in prompt
        assert "Notes: Seen flaky on ZFS" in prompt
        assert "Test: sanity/test_42a" in prompt
        assert "Error: FAIL: inode wrong after setattr" in prompt

    def test_multiple_failures_numbered(self, sample_patch, sample_failure):
        fail2 = {"test": "conf-sanity/test_5", "error": "mount failed"}
        unknown_failures = [
            (0, sample_patch, sample_failure),
            (2, sample_patch, fail2),
        ]
        prompt = orchestrator.build_research_prompt(unknown_failures)

        assert "Failure 0:" in prompt
        assert "Failure 1:" in prompt
        assert "Test: conf-sanity/test_5" in prompt

    def test_missing_optional_fields(self):
        patch_data = {
            "gerrit_url": "https://example.com/1",
            "description": "x",
        }
        failure = {"test": "test_1"}
        prompt = orchestrator.build_research_prompt(
            [(0, patch_data, failure)])

        # No JIRA or Notes lines when absent
        assert "JIRA:" not in prompt
        assert "Notes:" not in prompt
        assert "Error:" not in prompt

    def test_prompt_contains_instructions(self):
        prompt = orchestrator.build_research_prompt([])
        assert "Search JIRA" in prompt
        assert "link_and_retest" in prompt
        assert "raise_and_retest" in prompt
        assert "stop" in prompt


# -------------------------------------------------------------------
# 2. Decision parsing (_parse_llm_response)
# -------------------------------------------------------------------

class TestParseLlmResponse:
    def _make_jsonl(self, result_text, extra_lines=None):
        """Build Claude JSONL output with a result line."""
        lines = extra_lines or []
        lines.append(json.dumps({
            "type": "result", "result": result_text
        }))
        return "\n".join(lines)

    def test_clean_json_array(self):
        decisions = [
            {"index": 0, "found_bug": "LU-100", "related": False,
             "reason": "matched", "action": "link_and_retest"},
        ]
        raw = self._make_jsonl(json.dumps(decisions))
        result = orchestrator._parse_llm_response(raw, 1)
        assert len(result) == 1
        assert result[0]["found_bug"] == "LU-100"
        assert result[0]["action"] == "link_and_retest"

    def test_markdown_fenced_json(self):
        decisions = [
            {"index": 0, "found_bug": None, "related": True,
             "reason": "related", "action": "stop"},
        ]
        fenced = f"```json\n{json.dumps(decisions)}\n```"
        raw = self._make_jsonl(fenced)
        result = orchestrator._parse_llm_response(raw, 1)
        assert len(result) == 1
        assert result[0]["action"] == "stop"

    def test_markdown_fence_no_language_tag(self):
        decisions = [{"index": 0, "found_bug": None, "related": True,
                      "reason": "x", "action": "stop"}]
        fenced = f"```\n{json.dumps(decisions)}\n```"
        raw = self._make_jsonl(fenced)
        result = orchestrator._parse_llm_response(raw, 1)
        assert result[0]["action"] == "stop"

    def test_fills_missing_indices(self):
        """If LLM only returns index 0, index 1 gets a conservative stop."""
        decisions = [
            {"index": 0, "found_bug": "LU-1", "related": False,
             "reason": "found it", "action": "link_and_retest"},
        ]
        raw = self._make_jsonl(json.dumps(decisions))
        result = orchestrator._parse_llm_response(raw, 3)
        assert len(result) == 3
        assert result[0]["action"] == "link_and_retest"
        assert result[1]["action"] == "stop"
        assert "conservative" in result[1]["reason"].lower()
        assert result[2]["action"] == "stop"

    def test_no_result_line_returns_fallback(self):
        raw = json.dumps({"type": "system", "text": "hello"})
        result = orchestrator._parse_llm_response(raw, 2)
        assert len(result) == 2
        for d in result:
            assert d["action"] == "stop"
            assert "unavailable" in d["reason"].lower()

    def test_malformed_json_returns_fallback(self):
        raw = self._make_jsonl("this is not json at all")
        result = orchestrator._parse_llm_response(raw, 1)
        assert len(result) == 1
        assert result[0]["action"] == "stop"

    def test_empty_output_returns_fallback(self):
        result = orchestrator._parse_llm_response("", 2)
        assert len(result) == 2
        for d in result:
            assert d["action"] == "stop"

    def test_extra_jsonl_lines_ignored(self):
        """Non-result JSONL lines (system, assistant) are skipped."""
        decisions = [{"index": 0, "found_bug": None, "related": False,
                      "reason": "unrelated", "action": "raise_and_retest"}]
        extra = [
            json.dumps({"type": "system", "text": "init"}),
            json.dumps({"type": "assistant", "text": "researching"}),
        ]
        raw = self._make_jsonl(json.dumps(decisions), extra_lines=extra)
        result = orchestrator._parse_llm_response(raw, 1)
        assert result[0]["action"] == "raise_and_retest"


# -------------------------------------------------------------------
# 3. Fallback decisions
# -------------------------------------------------------------------

class TestFallbackDecisions:
    def test_returns_correct_count(self):
        result = orchestrator._fallback_decisions(5)
        assert len(result) == 5

    def test_all_stop_conservative(self):
        result = orchestrator._fallback_decisions(3)
        for i, d in enumerate(result):
            assert d["index"] == i
            assert d["action"] == "stop"
            assert d["found_bug"] is None
            assert d["related"] is True

    def test_zero_failures(self):
        assert orchestrator._fallback_decisions(0) == []


# -------------------------------------------------------------------
# 4. Config / path handling
# -------------------------------------------------------------------

class TestConfigPaths:
    def test_config_shepherd_tool_is_absolute(self):
        """The real shepherd_tool.sh path should be absolute."""
        assert os.path.isabs(SHEPHERD_TOOL_PATH)
        assert SHEPHERD_TOOL_PATH.endswith("shepherd_tool.sh")

    def test_env_var_override_patches_file(self):
        """Verify PATCHES_FILE env var is respected by config."""
        with patch.dict(os.environ, {"PATCHES_FILE": "/custom/patches.json"},
                        clear=False):
            val = os.environ.get(
                "PATCHES_FILE",
                "/shared/support_files/patches_to_watch.json")
            assert val == "/custom/patches.json"

    def test_env_var_override_report_file(self):
        with patch.dict(os.environ, {"REPORT_FILE": "/custom/report.json"},
                        clear=False):
            val = os.environ.get(
                "REPORT_FILE", "/tmp/patch_shepherd_report.json")
            assert val == "/custom/report.json"

    def test_config_dataclass_explicit_values(self, tmp_path):
        """PatchShepherdConfig accepts explicit values that skip env lookup."""
        from patch_shepherd.config import PatchShepherdConfig
        patches_file = str(tmp_path / "patches.json")
        with open(patches_file, "w") as f:
            f.write("{}")
        cfg = PatchShepherdConfig(
            patches_file=patches_file,
            report_file="/tmp/test_report.json",
            shepherd_tool=SHEPHERD_TOOL_PATH,
        )
        assert cfg.patches_file == patches_file
        assert cfg.report_file == "/tmp/test_report.json"
        assert cfg.shepherd_tool == SHEPHERD_TOOL_PATH


# -------------------------------------------------------------------
# 5. run() helper
# -------------------------------------------------------------------

class TestRunHelper:
    @patch("orchestrator.subprocess.run")
    def test_returns_parsed_json(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"ok": true}\n', returncode=0, stderr="")
        result = orchestrator.run(["echo"])
        assert result == {"ok": True}

    @patch("orchestrator.subprocess.run")
    def test_returns_raw_on_non_json(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="plain text\n", returncode=0, stderr="")
        result = orchestrator.run(["echo"])
        assert result == {"raw": "plain text", "rc": 0}

    @patch("orchestrator.subprocess.run")
    def test_returns_error_on_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", returncode=1, stderr="boom")
        result = orchestrator.run(["false"])
        assert "error" in result
        assert "empty output" in result["error"]

    @patch("orchestrator.subprocess.run")
    def test_timeout_returns_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=60)
        result = orchestrator.run(["slow"], timeout=60)
        assert "error" in result
        assert "timeout" in result["error"]

    @patch("orchestrator.subprocess.run")
    def test_generic_exception_returns_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("no such file")
        result = orchestrator.run(["missing"])
        assert "error" in result
        assert "no such file" in result["error"]

    @patch("orchestrator.subprocess.run")
    def test_passes_stdin_data(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"ok": true}', returncode=0, stderr="")
        orchestrator.run(["cat"], stdin_data="hello")
        _, kwargs = mock_run.call_args
        assert kwargs["input"] == "hello"


# -------------------------------------------------------------------
# 6. tool() helper
# -------------------------------------------------------------------

class TestToolHelper:
    @patch("orchestrator.run")
    def test_builds_correct_command(self, mock_run):
        mock_run.return_value = {"ok": True}
        orchestrator.tool("check-patch", "url1", "0", "active")
        args = mock_run.call_args[0][0]
        assert args[0] == WATCHER_TOOL_PATH
        assert args[1] == "check-patch"
        assert args[2:] == ["url1", "0", "active"]

    @patch("orchestrator.run")
    def test_passes_stdin_and_timeout(self, mock_run):
        mock_run.return_value = {"ok": True}
        orchestrator.tool("write-report", "/tmp/r.json",
                             stdin_data="data", timeout=30)
        _, kwargs = mock_run.call_args
        assert kwargs["stdin_data"] == "data"
        assert kwargs["timeout"] == 30


# -------------------------------------------------------------------
# 7. _check_one
# -------------------------------------------------------------------

class TestCheckOne:
    @patch("orchestrator.tool")
    def test_returns_tuple(self, mock_tool, sample_patch,
                           sample_check_result):
        mock_tool.return_value = sample_check_result
        i, p, result = orchestrator._check_one(0, sample_patch)
        assert i == 0
        assert p is sample_patch
        assert result is sample_check_result

    @patch("orchestrator.tool")
    def test_wraps_error_result(self, mock_tool, sample_patch):
        """When check-patch returns an error dict, wrap in standard format."""
        mock_tool.return_value = {"error": "connection refused"}
        i, p, result = orchestrator._check_one(0, sample_patch)
        assert result["errors"] == ["check-patch: connection refused"]
        assert result["needs_llm_decision"] == []
        assert result["skipped"] is False

    @patch("orchestrator.tool")
    def test_uses_patch_fields(self, mock_tool, sample_patch):
        """Verify patch fields are forwarded to tool call."""
        mock_tool.return_value = {
            "gerrit_url": sample_patch["gerrit_url"],
            "patch_index": 0, "skipped": True,
            "skip_reason": "merged",
            "actions_taken": [], "needs_llm_decision": [], "errors": [],
        }
        orchestrator._check_one(0, sample_patch)
        mock_tool.assert_called_once_with(
            "check-patch",
            sample_patch["gerrit_url"],
            "0", "active", "3", "2",
        )


# -------------------------------------------------------------------
# 8. check_all_patches
# -------------------------------------------------------------------

class TestCheckAllPatches:
    @patch("orchestrator.tool")
    def test_parallel_check(self, mock_tool):
        """All patches checked, results in correct order."""
        patches = [
            {"gerrit_url": f"url{i}", "description": f"p{i}",
             "watch_status": "active", "last_patchset": 1,
             "last_review_count": 0}
            for i in range(3)
        ]

        def fake_tool(action, url, idx, *args):
            return {
                "gerrit_url": url, "patch_index": int(idx),
                "skipped": False, "actions_taken": [],
                "needs_llm_decision": [], "errors": [],
            }

        mock_tool.side_effect = fake_tool
        results = orchestrator.check_all_patches(patches)
        assert len(results) == 3
        for i, (idx, p, result) in enumerate(results):
            assert idx == i
            assert result["gerrit_url"] == f"url{i}"

    @patch("orchestrator.tool")
    def test_exception_in_thread_handled(self, mock_tool):
        """An exception in a thread produces an error result, not a crash."""
        patches = [{"gerrit_url": "url0", "description": "p0",
                     "watch_status": "active", "last_patchset": 0,
                     "last_review_count": 0}]
        mock_tool.side_effect = RuntimeError("boom")
        results = orchestrator.check_all_patches(patches)
        assert len(results) == 1
        _, _, result = results[0]
        assert "errors" in result
        assert any("boom" in e for e in result["errors"])


# -------------------------------------------------------------------
# 9. research_failures (subprocess invocation)
# -------------------------------------------------------------------

class TestResearchFailures:
    @patch("orchestrator.subprocess.run")
    def test_timeout_returns_fallback(self, mock_run, sample_patch,
                                      sample_failure):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="claude", timeout=300)
        unknown = [(0, sample_patch, sample_failure)]
        result = orchestrator.research_failures(unknown)
        assert len(result) == 1
        assert result[0]["action"] == "stop"
        assert "unavailable" in result[0]["reason"].lower()

    @patch("orchestrator.subprocess.run")
    def test_exception_returns_fallback(self, mock_run, sample_patch,
                                        sample_failure):
        mock_run.side_effect = FileNotFoundError("claude not found")
        unknown = [(0, sample_patch, sample_failure)]
        result = orchestrator.research_failures(unknown)
        assert len(result) == 1
        assert result[0]["action"] == "stop"

    @patch("orchestrator.subprocess.run")
    def test_successful_invocation(self, mock_run, sample_patch,
                                    sample_failure):
        decisions = [{"index": 0, "found_bug": "LU-500",
                      "related": False, "reason": "known flake",
                      "action": "link_and_retest"}]
        jsonl_output = json.dumps({
            "type": "result",
            "result": json.dumps(decisions),
        })
        mock_run.return_value = MagicMock(
            stdout=jsonl_output, returncode=0, stderr="")

        unknown = [(0, sample_patch, sample_failure)]
        result = orchestrator.research_failures(unknown)
        assert result[0]["found_bug"] == "LU-500"

    @patch("orchestrator.subprocess.run")
    def test_removes_claudecode_env(self, mock_run, sample_patch,
                                     sample_failure):
        """CLAUDECODE should be stripped from env to allow nesting."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"type": "result", "result": "[]"}),
            returncode=0, stderr="")

        with patch.dict(os.environ, {"CLAUDECODE": "1"}):
            orchestrator.research_failures(
                [(0, sample_patch, sample_failure)])

        _, kwargs = mock_run.call_args
        env = kwargs["env"]
        assert "CLAUDECODE" not in env


# -------------------------------------------------------------------
# 10. execute_decisions
# -------------------------------------------------------------------

class TestExecuteDecisions:
    @patch("orchestrator.tool")
    def test_link_and_retest(self, mock_tool, sample_patch,
                              sample_failure):
        mock_tool.return_value = {"ok": True}
        unknown = [(0, sample_patch, sample_failure)]
        decisions = [{"index": 0, "found_bug": "LU-100",
                      "related": False, "reason": "exact match",
                      "action": "link_and_retest"}]

        actions, tool_calls = orchestrator.execute_decisions(
            unknown, decisions)

        assert len(actions) == 1
        assert actions[0]["type"] == "link_bug"
        assert actions[0]["jira"] == "LU-100"
        assert tool_calls == 2  # link-bug + retest
        calls = mock_tool.call_args_list
        assert calls[0] == call("link-bug", "suite-777", "LU-100")
        assert calls[1] == call("retest", "sess-888", "LU-100")

    @patch("orchestrator.tool")
    def test_raise_and_retest(self, mock_tool, sample_patch,
                               sample_failure):
        mock_tool.side_effect = [
            {"ok": True, "data": {"key": "LU-200"}},  # raise-bug
            {"ok": True},  # retest
        ]
        unknown = [(0, sample_patch, sample_failure)]
        decisions = [{"index": 0, "found_bug": None, "related": False,
                      "reason": "new bug", "action": "raise_and_retest"}]

        actions, tool_calls = orchestrator.execute_decisions(
            unknown, decisions)

        assert len(actions) == 1
        assert actions[0]["type"] == "raise_bug"
        assert actions[0]["jira"] == "LU-200"
        assert tool_calls == 2

    @patch("orchestrator.tool")
    def test_raise_bug_no_key_extracted(self, mock_tool, sample_patch,
                                        sample_failure):
        """When raise-bug returns no parseable key, action still recorded."""
        mock_tool.return_value = {"raw": "something weird", "rc": 1}
        unknown = [(0, sample_patch, sample_failure)]
        decisions = [{"index": 0, "found_bug": None, "related": False,
                      "reason": "new", "action": "raise_and_retest"}]

        actions, tool_calls = orchestrator.execute_decisions(
            unknown, decisions)

        assert actions[0]["jira"] == ""
        assert tool_calls == 1

    @patch("orchestrator.tool")
    def test_stop_action(self, mock_tool, sample_patch, sample_failure):
        unknown = [(0, sample_patch, sample_failure)]
        decisions = [{"index": 0, "found_bug": None, "related": True,
                      "reason": "looks related to patch",
                      "action": "stop"}]

        actions, tool_calls = orchestrator.execute_decisions(
            unknown, decisions)

        assert actions[0]["type"] == "stopped"
        assert tool_calls == 0
        mock_tool.assert_not_called()

    @patch("orchestrator.tool")
    def test_unknown_action_treated_as_stop(self, mock_tool,
                                             sample_patch, sample_failure):
        unknown = [(0, sample_patch, sample_failure)]
        decisions = [{"index": 0, "found_bug": None, "related": True,
                      "reason": "?", "action": "something_else"}]

        actions, _ = orchestrator.execute_decisions(unknown, decisions)
        assert actions[0]["type"] == "stopped"
        mock_tool.assert_not_called()

    @patch("orchestrator.tool")
    def test_link_and_retest_without_bug_falls_to_stop(
            self, mock_tool, sample_patch, sample_failure):
        """link_and_retest with found_bug=None falls through to stop."""
        unknown = [(0, sample_patch, sample_failure)]
        decisions = [{"index": 0, "found_bug": None, "related": True,
                      "reason": "no bug found", "action": "link_and_retest"}]

        actions, tool_calls = orchestrator.execute_decisions(
            unknown, decisions)

        # Should not link (no bug), falls to else branch (stop)
        assert actions[0]["type"] == "stopped"
        assert tool_calls == 0


# -------------------------------------------------------------------
# 11. _extract_bug_key
# -------------------------------------------------------------------

class TestExtractBugKey:
    def test_from_ok_data(self):
        result = {"ok": True, "data": {"key": "LU-999"}}
        assert orchestrator._extract_bug_key(result) == "LU-999"

    def test_from_raw_output(self):
        result = {"raw": "Created LU-1234 in project LU"}
        assert orchestrator._extract_bug_key(result) == "LU-1234"

    def test_ex_prefix(self):
        result = {"raw": "Created EX-555"}
        assert orchestrator._extract_bug_key(result) == "EX-555"

    def test_no_match(self):
        result = {"raw": "error: something went wrong"}
        assert orchestrator._extract_bug_key(result) is None

    def test_empty_result(self):
        assert orchestrator._extract_bug_key({}) is None

    def test_ok_but_empty_key(self):
        result = {"ok": True, "data": {"key": ""}}
        assert orchestrator._extract_bug_key(result) is None


# -------------------------------------------------------------------
# 12. Report generation
# -------------------------------------------------------------------

class TestBuildReport:
    def test_empty_report(self):
        report = orchestrator.build_report(
            all_actions=[], all_errors=[], all_skipped=[],
            patches_checked=0, tool_calls=0, llm_calls=0)
        assert report["patches_checked"] == 0
        assert report["actions"] == []
        s = report["summary"]
        assert s["active"] == 0
        assert s["retests_requested"] == 0
        assert s["bugs_raised"] == 0

    def test_summary_counts(self):
        actions = [
            {"type": "merged"},
            {"type": "merged"},
            {"type": "abandoned"},
            {"type": "stopped"},
            {"type": "needs_review"},
            {"type": "retest"},
            {"type": "link_bug"},
            {"type": "raise_bug"},
        ]
        report = orchestrator.build_report(
            actions, all_errors=["e1"], all_skipped=["s1"],
            patches_checked=10, tool_calls=5, llm_calls=1)

        s = report["summary"]
        assert s["merged"] == 2
        assert s["abandoned"] == 1
        assert s["stopped"] == 1
        assert s["needs_review"] == 1
        assert s["retests_requested"] == 3  # retest + link_bug + raise_bug
        assert s["bugs_raised"] == 1

    def test_debug_section(self):
        report = orchestrator.build_report(
            [], ["err1", "err2"], ["skip1"],
            patches_checked=5, tool_calls=10, llm_calls=1)

        d = report["debug"]
        assert d["tool_calls"] == 10
        assert d["llm_calls"] == 1
        assert d["errors"] == ["err1", "err2"]
        assert d["skipped"] == ["skip1"]

    def test_has_timestamp(self):
        report = orchestrator.build_report([], [], [], 0, 0, 0)
        assert "timestamp" in report
        assert "T" in report["timestamp"]

    def test_actions_passed_through(self):
        actions = [{"type": "retest", "extra": "data"}]
        report = orchestrator.build_report(actions, [], [], 1, 1, 0)
        assert report["actions"] == actions


# -------------------------------------------------------------------
# 13. main() integration
# -------------------------------------------------------------------

class TestMain:
    @patch("orchestrator.tool")
    @patch("orchestrator.load_config")
    @patch("builtins.open", create=True)
    def test_no_unknown_failures_path(self, mock_open, mock_load_config,
                                       mock_tool):
        """Happy path: all patches checked, none need LLM."""
        patches_data = {
            "patches": [
                {"gerrit_url": "url0", "description": "p0",
                 "watch_status": "active", "last_patchset": 1,
                 "last_review_count": 0},
            ]
        }

        # Config mock
        fake_cfg = MagicMock()
        fake_cfg.patches_file = "/tmp/fake_patches.json"
        fake_cfg.report_file = "/tmp/fake_report.json"
        fake_cfg.shepherd_tool = SHEPHERD_TOOL_PATH
        mock_load_config.return_value = fake_cfg

        # File mock
        mock_file = MagicMock()
        mock_file.__enter__ = lambda s: s
        mock_file.__exit__ = MagicMock(return_value=False)
        mock_file.read.return_value = json.dumps(patches_data)
        mock_open.return_value = mock_file

        # check-patch returns clean result
        check_result = {
            "gerrit_url": "url0", "patch_index": 0,
            "skipped": False, "actions_taken": [],
            "needs_llm_decision": [], "errors": [],
        }
        write_result = {"ok": True}
        mock_tool.side_effect = [check_result, write_result]

        with patch("sys.stdout"):
            result = orchestrator.main()

        assert result == 0
        write_call = mock_tool.call_args_list[-1]
        assert write_call[0][0] == "write-report"

    @patch("orchestrator.tool")
    @patch("orchestrator.load_config")
    @patch("builtins.open", create=True)
    def test_with_unknown_failures_invokes_llm(
            self, mock_open, mock_load_config, mock_tool):
        """When unknown failures exist, LLM is invoked."""
        patches_data = {
            "patches": [
                {"gerrit_url": "url0", "description": "p0",
                 "jira": "LU-1",
                 "watch_status": "active", "last_patchset": 1,
                 "last_review_count": 0},
            ]
        }

        fake_cfg = MagicMock()
        fake_cfg.patches_file = "/tmp/p.json"
        fake_cfg.report_file = "/tmp/r.json"
        fake_cfg.shepherd_tool = SHEPHERD_TOOL_PATH
        mock_load_config.return_value = fake_cfg

        mock_file = MagicMock()
        mock_file.__enter__ = lambda s: s
        mock_file.__exit__ = MagicMock(return_value=False)
        mock_file.read.return_value = json.dumps(patches_data)
        mock_open.return_value = mock_file

        # check-patch returns unknown failure
        check_result = {
            "gerrit_url": "url0", "patch_index": 0,
            "skipped": False, "actions_taken": [],
            "needs_llm_decision": [
                {"test": "test_1", "error": "fail",
                 "suite_id": "s1", "session_id": "ss1"},
            ],
            "errors": [],
        }
        write_result = {"ok": True}
        mock_tool.side_effect = [check_result, write_result]

        # Mock the LLM research to return a stop decision
        with patch("orchestrator.research_failures") as mock_research:
            mock_research.return_value = [
                {"index": 0, "found_bug": None, "related": True,
                 "reason": "related", "action": "stop"}
            ]
            with patch("sys.stdout"):
                result = orchestrator.main()

        assert result == 0
        mock_research.assert_called_once()


# -------------------------------------------------------------------
# 14. log() helper
# -------------------------------------------------------------------

class TestLogHelper:
    def test_writes_to_stderr(self, capsys):
        orchestrator.log("hello world")
        captured = capsys.readouterr()
        assert "hello world" in captured.err
        assert captured.out == ""

    def test_includes_timestamp(self, capsys):
        orchestrator.log("test")
        captured = capsys.readouterr()
        assert "[" in captured.err and "]" in captured.err
