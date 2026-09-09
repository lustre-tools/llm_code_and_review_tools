"""Tests for the Claude CLI session adapter.

The fixtures here are transcribed from real `claude` 2.1.263 output rather than
invented, so a CLI behaviour change shows up as a test failure instead of a
silent production break.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from patch_watcher.cli_sessions import (
    ClaudeCli,
    CliSession,
    CliSessionError,
    project_slug,
)

# Verbatim from `claude --bg` on 2.1.263.
BACKGROUNDED = (
    "Starting background service…\n"
    "backgrounded · 43518af3\n"
    "  claude agents             list sessions\n"
    "  claude attach 43518af3    open in this terminal\n"
)
WOKE = (
    "note: woke session 61aa8a0e with its saved options (--model, --permission-mode).\n"
    "backgrounded · 61aa8a0e · memory retention task\n"
)
FORKED = (
    "note: session 61aa8a0e is already running in the background, so this started a "
    "copy as 58c26e9d. `claude attach 61aa8a0e` opens the original.\n"
    "backgrounded · 58c26e9d\n"
)


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode,
        stdout=stdout.encode(), stderr=stderr.encode(),
    )


class FakeRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if not self.responses:
            return completed("")
        value = self.responses.pop(0)
        return value(command) if callable(value) else value


class ProjectSlugTests(unittest.TestCase):
    def test_slug_matches_the_real_cli_layout(self):
        # Observed directly: this cwd produced exactly this directory under
        # ~/.claude/projects, including the doubled dash where a path segment
        # already began with one.
        self.assertEqual(
            project_slug("/tmp/claude-1000/-home-paf-x/scratchpad/bgtest3"),
            "-tmp-claude-1000--home-paf-x-scratchpad-bgtest3",
        )

    def test_dots_become_dashes(self):
        self.assertEqual(project_slug("/tmp/a.b/c"), "-tmp-a-b-c")

    def test_underscores_become_dashes(self):
        # Proved live: a session in /tmp/pw_slug_test wrote its transcript to
        # ~/.claude/projects/-tmp-pw-slug-test. Missing this made every
        # transcript read silently return nothing, and BOTH default pool roots
        # contain an underscore.
        self.assertEqual(project_slug("/tmp/pw_slug_test"), "-tmp-pw-slug-test")
        self.assertEqual(
            project_slug("/home/paf/llm_code_and_review_tools"),
            "-home-paf-llm-code-and-review-tools",
        )


class StartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)

    def test_start_returns_the_short_id_and_bypasses_permissions(self):
        runner = FakeRunner(completed(BACKGROUNDED))
        cli = ClaudeCli(runner=runner)
        self.assertEqual(cli.start("do the thing", cwd=self.cwd), "43518af3")
        command, kwargs = runner.calls[0]
        self.assertEqual(command[:4], ["claude", "--bg", "--permission-mode", "bypassPermissions"])
        self.assertEqual(command[-1], "do the thing")
        self.assertEqual(kwargs["cwd"], str(self.cwd))

    def test_json_schema_is_passed_as_compact_json(self):
        runner = FakeRunner(completed(BACKGROUNDED))
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        ClaudeCli(runner=runner).start("x", cwd=self.cwd, json_schema=schema)
        command, _ = runner.calls[0]
        self.assertIn("--json-schema", command)
        self.assertEqual(
            json.loads(command[command.index("--json-schema") + 1]), schema
        )

    def test_an_empty_prompt_is_refused_before_launching(self):
        runner = FakeRunner()
        with self.assertRaises(CliSessionError):
            ClaudeCli(runner=runner).start("   ", cwd=self.cwd)
        self.assertEqual(runner.calls, [])

    def test_a_failing_cli_raises_rather_than_returning_a_bad_id(self):
        cli = ClaudeCli(runner=FakeRunner(completed("", returncode=1, stderr="boom")))
        with self.assertRaises(CliSessionError):
            cli.start("x", cwd=self.cwd)

    def test_unparseable_output_raises(self):
        cli = ClaudeCli(runner=FakeRunner(completed("nothing useful here")))
        with self.assertRaises(CliSessionError):
            cli.start("x", cwd=self.cwd)

    def test_a_stray_hex_token_is_not_mistaken_for_a_session_id(self):
        # Proved against the real parser: without requiring the launch line,
        # these yield "cafebabe" / "1a2b3c4d" -- a plausible, wrong id that
        # sends the caller to a session that never existed.
        for noise in (
            "warning: cache dir /tmp/cafebabe/x is stale",
            "wrote /home/u/.cache/1a2b3c4d/session",
        ):
            with self.subTest(noise=noise):
                cli = ClaudeCli(runner=FakeRunner(completed(noise)))
                with self.assertRaises(CliSessionError):
                    cli.start("x", cwd=self.cwd)


class ListingTests(unittest.TestCase):
    PAYLOAD = json.dumps([
        {"pid": 1290213, "id": "43518af3", "cwd": "/w", "kind": "background",
         "startedAt": 1788801094453, "sessionId": "43518af3-1fff-4265-bbd9-f999183a38e8",
         "name": "fix the build", "status": "idle", "state": "done"},
        {"pid": None, "id": "7f5fffa7", "cwd": "/w", "kind": "background",
         "startedAt": 1788801194453, "sessionId": "7f5fffa7-0000-0000-0000-000000000000",
         "name": "stopped one", "status": None, "state": "done"},
    ])

    def test_sessions_are_parsed_from_agents_json(self):
        cli = ClaudeCli(runner=FakeRunner(completed(self.PAYLOAD)))
        sessions = cli.sessions()
        self.assertEqual([s.short_id for s in sessions], ["43518af3", "7f5fffa7"])
        self.assertEqual(sessions[0].name, "fix the build")

    def test_liveness_uses_the_pid_not_the_reported_status(self):
        # Measured: a session actively working reports status null and state
        # "working". Trusting status alone called it stopped, and the resume
        # that followed forked a copy. The pid is authoritative.
        import os
        working = CliSession(
            short_id="a", session_id="s", cwd="/w", kind="background", name="n",
            status=None, state="working", pid=os.getpid(), started_at=1,
        )
        self.assertTrue(working.running)

        dead = CliSession(
            short_id="b", session_id="s", cwd="/w", kind="background", name="n",
            status=None, state="done", pid=2 ** 22 - 1, started_at=1,
        )
        self.assertFalse(dead.running)

    def test_without_a_pid_the_reported_fields_decide(self):
        live = CliSession(
            short_id="a", session_id="s", cwd="/w", kind="background", name="n",
            status=None, state="working", pid=None, started_at=1,
        )
        idle = CliSession(
            short_id="b", session_id="s", cwd="/w", kind="background", name="n",
            status="idle", state="done", pid=None, started_at=1,
        )
        stopped = CliSession(
            short_id="c", session_id="s", cwd="/w", kind="background", name="n",
            status=None, state="done", pid=None, started_at=1,
        )
        self.assertTrue(live.running)
        self.assertTrue(idle.running)
        self.assertFalse(stopped.running)

    def test_non_json_output_raises(self):
        cli = ClaudeCli(runner=FakeRunner(completed("not json")))
        with self.assertRaises(CliSessionError):
            cli.sessions()

    def test_empty_listing_is_not_an_error(self):
        # Both shapes the CLI actually produces for "nothing running": an
        # empty JSON array, and no output at all.  Asserting the issued argv
        # as well keeps a listing that never runs from passing as "empty".
        for stdout in ("[]", ""):
            with self.subTest(stdout=stdout):
                runner = FakeRunner(completed(stdout))
                self.assertEqual(ClaudeCli(runner=runner).sessions(), ())
                self.assertEqual(
                    [command for command, _ in runner.calls],
                    [["claude", "agents", "--json", "--all"]],
                )


class SendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)

    @staticmethod
    def listing(status):
        return json.dumps([{
            "id": "61aa8a0e", "sessionId": "61aa8a0e-89e0-4c92-8d6a-539f0d30b83c",
            "cwd": "/w", "kind": "background", "name": "n",
            "status": status, "state": "done", "pid": None, "startedAt": 1,
        }])

    def test_a_live_session_is_stopped_before_resuming(self):
        runner = FakeRunner(
            completed(self.listing("idle")),   # session(): still running
            completed("stopped 61aa8a0e"),     # stop
            completed(self.listing(None)),     # wait_until_stopped: gone
            completed(WOKE),                   # resume
        )
        cli = ClaudeCli(runner=runner)
        result = cli.send(
            "61aa8a0e", "61aa8a0e-89e0-4c92-8d6a-539f0d30b83c", "carry on", cwd=self.cwd
        )
        self.assertEqual(result.short_id, "61aa8a0e")
        self.assertFalse(result.forked)
        issued = [call[0][1] for call in runner.calls]
        self.assertEqual(issued, ["agents", "stop", "agents", "--bg"])

    def test_an_already_stopped_session_is_resumed_without_stopping(self):
        runner = FakeRunner(completed(self.listing(None)), completed(WOKE))
        cli = ClaudeCli(runner=runner)
        result = cli.send(
            "61aa8a0e", "61aa8a0e-89e0-4c92-8d6a-539f0d30b83c", "go", cwd=self.cwd
        )
        self.assertEqual(result.short_id, "61aa8a0e")
        self.assertEqual(result.session_id, "61aa8a0e-89e0-4c92-8d6a-539f0d30b83c")
        self.assertFalse(result.forked)
        issued = [call[0][1] for call in runner.calls]
        self.assertEqual(issued, ["agents", "--bg"])

    def test_a_session_that_will_not_stop_refuses_rather_than_forking(self):
        # Resuming a live session silently forks a copy under a new id. That
        # would strand the original and split the conversation, so refuse.
        runner = FakeRunner(
            completed(self.listing("idle")),
            completed("stopped"),
            *[completed(self.listing("idle")) for _ in range(6)],
        )
        cli = ClaudeCli(runner=runner)
        with self.assertRaises(CliSessionError) as caught:
            cli.send(
                "61aa8a0e", "61aa8a0e-89e0-4c92-8d6a-539f0d30b83c", "go",
                cwd=self.cwd, stop_timeout=0.01,
            )
        self.assertIn("fork", str(caught.exception))
        self.assertNotIn("--bg", [call[0][1] for call in runner.calls])

    def test_an_unlisted_fork_raises_instead_of_pairing_the_wrong_ids(self):
        # Returning the new short id beside the OLD session id would send the
        # caller to a transcript that never updates again.
        runner = FakeRunner(
            completed(self.listing(None)), completed(FORKED), completed("[]"),
        )
        with self.assertRaises(CliSessionError) as caught:
            ClaudeCli(runner=runner).send(
                "61aa8a0e", "61aa8a0e-89e0-4c92-8d6a-539f0d30b83c", "go", cwd=self.cwd
            )
        self.assertIn("not listed yet", str(caught.exception))

    def test_a_fork_is_reported_with_the_copy_s_session_id(self):
        # A fork writes a DIFFERENT transcript. Silently returning the old
        # session id makes the caller poll a file that will never update --
        # exactly the failure the live smoke test hit.
        forked_listing = json.dumps([{
            "id": "58c26e9d", "sessionId": "58c26e9d-aaaa-bbbb-cccc-dddddddddddd",
            "cwd": "/w", "kind": "background", "name": "n",
            "status": "idle", "state": "done", "pid": None, "startedAt": 1,
        }])
        runner = FakeRunner(
            completed(self.listing(None)), completed(FORKED), completed(forked_listing),
        )
        result = ClaudeCli(runner=runner).send(
            "61aa8a0e", "61aa8a0e-89e0-4c92-8d6a-539f0d30b83c", "go", cwd=self.cwd
        )
        self.assertTrue(result.forked)
        self.assertEqual(result.short_id, "58c26e9d")
        self.assertEqual(result.session_id, "58c26e9d-aaaa-bbbb-cccc-dddddddddddd")

    def test_an_empty_message_is_refused(self):
        runner = FakeRunner()
        with self.assertRaises(CliSessionError):
            ClaudeCli(runner=runner).send("a", "b", "  ", cwd=self.cwd)
        self.assertEqual(runner.calls, [])


class TranscriptTests(unittest.TestCase):
    SESSION = "cfe55479-d087-4950-a0fb-edf6f6b1d0bd"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.cwd = root / "work"
        self.cwd.mkdir()
        self.projects = root / "projects"
        self.cli = ClaudeCli(runner=FakeRunner(), projects_root=self.projects)
        self.path = self.cli.transcript_path(self.SESSION, self.cwd)
        self.path.parent.mkdir(parents=True)

    def write(self, *events):
        self.path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def test_missing_transcript_is_empty_not_an_error(self):
        self.assertEqual(self.cli.transcript(self.SESSION, self.cwd), ())

    def test_assistant_text_is_extracted_in_order(self):
        self.write(
            {"type": "user"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": " second "}]}},
        )
        self.assertEqual(self.cli.assistant_text(self.SESSION, self.cwd), ("first", "second"))

    def test_structured_result_is_read_from_the_tool_use_block(self):
        # Verbatim shape from a real --json-schema run.
        self.write({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": ""},
            {"type": "tool_use", "id": "toolu_01", "name": "StructuredOutput",
             "input": {"answer": "BANANA"}},
        ]}})
        self.assertEqual(
            self.cli.structured_result(self.SESSION, self.cwd), {"answer": "BANANA"}
        )

    def test_the_newest_structured_result_wins(self):
        self.write(
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "StructuredOutput", "input": {"n": 1}}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "StructuredOutput", "input": {"n": 2}}]}},
        )
        self.assertEqual(self.cli.structured_result(self.SESSION, self.cwd), {"n": 2})

    def test_no_structured_result_returns_none(self):
        self.write({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        self.assertIsNone(self.cli.structured_result(self.SESSION, self.cwd))

    def test_a_partially_written_final_line_is_skipped(self):
        # A live read can catch the file mid-flush; that must not raise.
        self.path.write_text(
            json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": "ok"}]}})
            + "\n" + '{"type": "assist'
        )
        self.assertEqual(self.cli.assistant_text(self.SESSION, self.cwd), ("ok",))

    def test_other_tool_use_blocks_are_ignored(self):
        self.write({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
        self.assertIsNone(self.cli.structured_result(self.SESSION, self.cwd))


if __name__ == "__main__":
    unittest.main()
