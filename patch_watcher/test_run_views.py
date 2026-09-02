import unittest
from dataclasses import dataclass

import run_views


class RunViewTests(unittest.TestCase):
    def patch(self, **changes):
        value = {"change_number": 68160, "status": "open", "patchset": 13,
                 "revision_sha": "a" * 40, "eligible": True}
        value.update(changes)
        return value

    def sample_run(self, **changes):
        value = {"run_id": "run-123", "version": 7, "state": "running",
                 "change_number": 68160, "patchset": 13,
                 "revision_sha": "b" * 40, "subject": "LU-12345 test patch",
                 "current_step": "Inspecting failures", "process_pid": 2345,
                 "process_memory_bytes": 268435456,
                 "runtime_remaining_seconds": 601,
                 "inactivity_remaining_seconds": 1200,
                 "absolute_remaining_seconds": 172800,
                 "last_activity_at": "2026-08-30T18:00:00Z",
                 "execution_profile": "triage", "model": "claude"}
        value.update(changes)
        return value

    def test_investigate_is_post_read_only_and_revision_pinned(self):
        html = run_views.render_investigate_control(
            self.patch(), csrf_token="csrf", idempotency_token="once")
        self.assertIn("method='post'", html)
        self.assertIn("Investigate", html)
        self.assertIn("Read-only", html)
        self.assertIn("no Gerrit or CI write capability", html)
        self.assertIn("name='revision_sha'", html)
        self.assertIn("a" * 40, html)
        self.assertNotIn("method='get'", html.casefold())

    def test_compact_investigate_preserves_exact_post_without_long_copy(self):
        html = run_views.render_investigate_control(
            self.patch(), csrf_token="csrf", idempotency_token="once",
            compact=True,
        )
        self.assertIn("class='quick-action'", html)
        self.assertIn("action='/runs/investigate'", html)
        self.assertIn("name='revision_sha'", html)
        self.assertIn("Investigate", html)
        self.assertNotIn("Starts one manually requested", html)

    def test_investigate_disabled_when_terminal_active_unpinned_or_ineligible(self):
        for patch in (self.patch(status="merged"), self.patch(active_run_id="x"),
                      self.patch(revision_sha=None), self.patch(eligible=False)):
            with self.subTest(patch=patch):
                self.assertIn("disabled aria-disabled='true'",
                              run_views.render_investigate_control(patch))

    def test_summary_shows_state_countdowns_memory_and_latest_message(self):
        html = run_views.render_run_summary(
            self.sample_run(latest_message={"body": "Still checking"}))
        for expected in ("Run: Running", "PID 2345 · 256 MiB", "10m 1s",
                         "20m 0s", "2d 0h 0m 0s", "Still checking",
                         "Exact pinned revision", "/runs/run-123"):
            self.assertIn(expected, html)

    def test_multiple_summaries_have_unique_accessible_heading_ids(self):
        first = run_views.render_run_summary(self.sample_run(run_id="run-one"))
        second = run_views.render_run_summary(self.sample_run(run_id="run-two"))
        self.assertIn("aria-labelledby='agent-run-run-one'", first)
        self.assertIn("id='agent-run-run-one'", first)
        self.assertIn("aria-labelledby='agent-run-run-two'", second)
        self.assertNotIn("id='agent-run-run-one'", second)

    def test_detail_shows_exact_revision_and_truthful_boundaries(self):
        admission = {"status": "ready", "profile_id": "host-unsandboxed-mac-v1",
                     "profile_hash": "sha256:profile", "instruction_hash": "sha256:instructions",
                     "environment_instance_id": "mac-1", "isolation_profile": "host_unsandboxed",
                     "network_profile": "host_ambient"}
        html = run_views.render_run_detail(self.sample_run(), admission=admission)
        for expected in ("Exact pinned revision", "b" * 40, "Unsandboxed host worker",
                         "General network access", "sha256:profile", "sha256:instructions"):
            self.assertIn(expected, html)
        self.assertNotIn("Sandboxed worker", html)

    def test_waiting_human_question_precedes_conversation_and_targets_answer(self):
        question = {"question_id": "q-42", "question": "Which baseline?",
                    "why": "Branches differ.", "tried": "Compared histories.",
                    "recommended": "Use master.", "choices": ["master", "maintenance"]}
        html = run_views.render_run_detail(self.sample_run(state="waiting_human", question=question))
        self.assertLess(html.index("Waiting for your decision"), html.index("Conversation"))
        for expected in ("Which baseline?", "Compared histories.",
                         "name='question_id' value='q-42'", "Answer and resume"):
            self.assertIn(expected, html)
        self.assertNotIn("action='/runs/run-123/resume'", html)

    def test_conversation_shows_delivery_states_and_timeline(self):
        messages = [{"author": "operator", "body": "First", "delivery_state": "queued"},
                    {"author": "operator", "body": "Second", "delivery_state": "acknowledged"}]
        events = [{"type": "guidance_delivered", "summary": "Agent received guidance"}]
        html = run_views.render_run_detail(self.sample_run(), messages=messages, events=events)
        for expected in ("Delivery: <strong>Queued", "Delivery: <strong>Acknowledged",
                         "Guidance delivered", "Agent received guidance"):
            self.assertIn(expected, html)

    def test_running_guidance_is_safe_by_default_and_interrupt_explicit(self):
        html = run_views.render_run_detail(self.sample_run())
        self.assertIn("value='safe_boundary'>Send guidance", html)
        self.assertIn("value='interrupt_and_send'>Interrupt and send", html)
        self.assertIn("next safe turn boundary", html)

    def test_pause_interrupt_and_resume_are_post_only(self):
        running = run_views.render_run_detail(self.sample_run())
        self.assertIn("method='post' action='/runs/run-123/pause'", running)
        self.assertIn("method='post' action='/runs/run-123/interrupt'", running)
        self.assertNotIn("action='/runs/run-123/resume'", running)
        paused = run_views.render_run_detail(self.sample_run(state="paused"))
        self.assertIn("method='post' action='/runs/run-123/resume'", paused)

    def test_detail_only_links_to_destructive_confirmation(self):
        html = run_views.render_run_detail(self.sample_run())
        self.assertIn("href='/runs/run-123/confirm?intent=cancel", html)
        self.assertIn("href='/runs/run-123/confirm?intent=kill", html)
        self.assertNotIn("action='/runs/run-123/cancel'", html)
        self.assertNotIn("action='/runs/run-123/kill'", html)
        self.assertNotIn("confirmation_token", html)

    def test_cancel_confirmation_has_token_and_post_only(self):
        html = run_views.render_destructive_confirmation(
            self.sample_run(), "cancel", confirmation_token="signed", csrf_token="csrf")
        for expected in ("Confirm stop and cancel", "method='post' action='/runs/run-123/cancel'",
                         "name='confirmation_token' value='signed'",
                         "name='expected_version' value='7'"):
            self.assertIn(expected, html)
        self.assertNotIn("method='get'", html.casefold())

    def test_kill_confirmation_precise_and_no_get_mutation(self):
        html = run_views.render_destructive_confirmation(
            self.sample_run(), "kill", confirmation_token="signed")
        self.assertIn("Confirm kill session", html)
        self.assertIn("forcibly stops the Claude process", html)
        self.assertIn("method='post' action='/runs/run-123/kill'", html)
        self.assertNotIn("href='/runs/run-123/kill", html)

    def test_confirmation_requires_valid_intent_and_token(self):
        with self.assertRaises(ValueError):
            run_views.render_destructive_confirmation(self.sample_run(), "kill", confirmation_token="")
        with self.assertRaises(ValueError):
            run_views.render_destructive_confirmation(self.sample_run(), "delete", confirmation_token="x")

    def test_terminal_run_offers_post_follow_up_not_resume(self):
        html = run_views.render_run_detail(self.sample_run(state="failed"))
        self.assertIn("method='post' action='/runs/run-123/follow-up'", html)
        self.assertIn("Start follow-up run", html)
        self.assertNotIn("action='/runs/run-123/resume'", html)

    def test_dynamic_content_and_attributes_are_escaped(self):
        run = self.sample_run(run_id="../x y/?", subject="<img src=x>", revision_sha="<&>")
        html = run_views.render_run_detail(
            run, messages=[{"author": "<admin>", "body": "<b>bad</b>"}],
            events=[{"type": "<event>", "summary": "x & y"}],
            csrf_token="' onmouseover='bad")
        for unsafe in ("<img", "<b>", "<admin>", "<event>", "onclick="):
            self.assertNotIn(unsafe, html)
        self.assertIn("/runs/..%2Fx%20y%2F%3F", html)

    def test_dataclass_input_and_accessible_labels(self):
        @dataclass
        class Run:
            run_id: str = "dataclass-run"
            state: str = "paused"
            revision_sha: str = "c" * 40
            subject: str = "Patch"
            version: int = 2
        html = run_views.render_run_detail(Run())
        for expected in ("dataclass-run", "Run: Paused", "c" * 40,
                         "<main class='run-detail'>", "<label for='guidance-message'>",
                         "aria-describedby='guidance-help'", "aria-labelledby='run-controls-title'"):
            self.assertIn(expected, html)

    def test_no_external_write_controls(self):
        lower = run_views.render_run_detail(self.sample_run()).casefold()
        for label in ("retest", "vote gerrit", "upload patch", "post comment",
                      "trigger jenkins", "rebuild", "maloo write"):
            self.assertNotIn(label, lower)


if __name__ == "__main__":
    unittest.main()
