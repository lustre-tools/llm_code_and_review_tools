import re
import unittest
from dataclasses import dataclass

from patch_watcher import run_views

# The complete write surface of the run detail page, per run state.  A newly
# added form -- a retest, a Gerrit vote, an upload -- has to be listed here
# deliberately, because an unlisted POST route fails the test below.
WRITE_ROUTES = {
    "queued": {"/runs/run-123/guidance", "/runs/run-123/pause"},
    "preparing": {"/runs/run-123/guidance", "/runs/run-123/interrupt",
                  "/runs/run-123/pause"},
    "running": {"/runs/run-123/guidance", "/runs/run-123/interrupt",
                "/runs/run-123/pause"},
    "waiting_external": {"/runs/run-123/guidance", "/runs/run-123/pause",
                         "/runs/run-123/resume"},
    "blocked": {"/runs/run-123/guidance", "/runs/run-123/pause",
                "/runs/run-123/resume"},
    "paused": {"/runs/run-123/guidance", "/runs/run-123/resume"},
    "waiting_human": {"/runs/run-123/guidance"},
    "failed": {"/runs/run-123/follow-up"},
    "succeeded": {"/runs/run-123/follow-up"},
    "cancelled": {"/runs/run-123/follow-up"},
}


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

    def test_boundary_statement_follows_capability_not_session_profile(self):
        """The safety text describes what the agent may do, nothing else.

        `request_investigation` mints a read-only run whose SESSION profile is
        still "engineering"; the controller then starts it with
        capability_profile="read_only" -- Read/Glob/Grep, --safe-mode
        --restricted, service credentials scrubbed. Reading the session
        profile made that page promise a host shell, real credentials, and its
        own Gerrit and CI writes.
        """
        read_only = run_views.render_run_detail(self.sample_run(
            execution_profile="engineering", profile="engineering",
            capability_profile="read_only",
        ))
        self.assertIn("Read-only investigation", read_only)
        self.assertIn("Read-only run:", read_only)
        self.assertNotIn("Engineering boundary", read_only)
        self.assertNotIn("real service credentials", read_only)

        engineering = run_views.render_run_detail(self.sample_run(
            execution_profile="engineering", profile="engineering",
            capability_profile="full",
        ))
        self.assertIn("Engineering boundary", engineering)
        self.assertIn("real service credentials", engineering)

        # `source_edit` and `source_edit_ltvm` were removed from the runner
        # with the MCP server the second required, so no run can carry them.
        # An unknown profile must fall to read-only rather than be treated as
        # a grant.
        for retired in ("source_edit", "source_edit_ltvm"):
            rendered = run_views.render_run_detail(self.sample_run(
                execution_profile="engineering", profile="engineering",
                capability_profile=retired,
            ))
            self.assertIn("Read-only investigation", rendered)
            self.assertNotIn("real service credentials", rendered)

    def test_run_with_no_projected_capability_makes_no_write_claim(self):
        """An unstated capability must not be read as a granted one."""
        rendered = run_views.render_run_detail(self.sample_run(
            execution_profile="engineering", profile="engineering",
        ))
        self.assertIn("Read-only run:", rendered)
        self.assertNotIn("Engineering boundary", rendered)

    def test_no_controller_event_type_renders_a_mangled_initialism(self):
        """Every event the controller can emit gets a readable timeline label.

        `.capitalize()` lowercases everything after the first letter, so a new
        `ltvm_*` event type renders as "Ltvm ..." until HUMAN_LABELS learns it.
        The controller's own `*_EVENT` constants are the authoritative list, so
        this fails the moment one is added without a label.
        """
        from patch_watcher import run_controller

        event_types = sorted({
            value for name, value in vars(run_controller).items()
            if name.endswith("_EVENT") and isinstance(value, str)
        })
        self.assertIn("ltvm_prefix_baseline_unprovable", event_types)
        for event_type in event_types:
            rendered = run_views.render_run_detail(
                self.sample_run(),
                events=[{"event_type": event_type,
                         "created_at": "2026-09-08T12:00:00Z",
                         "summary": "detail"}],
            )
            label = re.search(r"<strong>([^<]*)</strong>: detail", rendered)
            self.assertIsNotNone(label, event_type)
            self.assertNotIn("Ltvm", label.group(1), event_type)
            if event_type.startswith("ltvm_"):
                self.assertTrue(
                    label.group(1).startswith("LTVM "), event_type
                )

    def write_routes(self, html):
        """Return every route the page can POST to, proving each form is a POST."""
        forms = re.findall(r"<form[^>]*>", html)
        self.assertTrue(forms, "the page rendered no form at all")
        routes = set()
        for form in forms:
            self.assertIn("method='post'", form)
            match = re.search(r"action='([^']*)'", form)
            self.assertIsNotNone(match, f"form without an action: {form}")
            routes.add(match.group(1))
        return routes

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

    def test_detail_shows_exact_pinned_revision(self):
        html = run_views.render_run_detail(self.sample_run())
        for expected in ("Exact pinned revision", "b" * 40):
            self.assertIn(expected, html)

    def test_waiting_human_question_precedes_conversation_and_targets_answer(self):
        question = {"question_id": "q-42", "question": "Which baseline?",
                    "why": "Branches differ.", "tried": "Compared histories.",
                    "recommended": "Use master.", "choices": ["master", "maintenance"]}
        html = run_views.render_run_detail(self.sample_run(state="waiting_human", question=question))
        self.assertLess(html.index("Waiting for your decision"), html.index("Chat with this run"))
        for expected in ("Which baseline?", "Compared histories.",
                         "name='question_id' value='q-42'", "Answer and resume"):
            self.assertIn(expected, html)
        self.assertNotIn("action='/runs/run-123/resume'", html)

    def test_the_chat_panel_reads_as_a_conversation(self):
        """Transcript above the box you answer it in, sides distinguished,
        and no "Delivery: Recorded" on every line the agent says."""
        messages = [
            {"author": "agent", "body": "Build is compiling.",
             "created_at": "2026-09-11T15:32:41+00:00", "delivery_state": "recorded"},
            {"author": "operator", "body": "Stop after this one.",
             "created_at": "2026-09-11T15:33:02+00:00", "delivery_state": "queued"},
        ]
        html = run_views.render_run_detail(self.sample_run(), messages=messages)
        self.assertIn("Chat with this run", html)
        self.assertIn("chat-live", html)                       # it says it is live
        self.assertIn("run-message agent", html)
        self.assertIn("run-message operator", html)
        self.assertIn(">You</span>", html)                     # the operator is "You"
        self.assertIn(">15:32:41</time>", html)                # clock, not a full stamp
        self.assertIn("Delivery: <strong>Queued", html)        # a queued send still says so
        self.assertEqual(html.count("Delivery: <strong>Recorded"), 0)
        self.assertLess(html.index("Build is compiling."), html.index("<textarea"))
        self.assertIn("data-poll='/runs/run-123/messages'", html)

    def test_a_finished_run_is_not_polled(self):
        html = run_views.render_run_detail(self.sample_run(state="succeeded"))
        self.assertIn("chat-done", html)
        self.assertNotIn("data-poll", html)
        self.assertNotIn("setInterval", html)

    def test_the_transcript_fragment_stands_alone(self):
        """What the poll swaps in: rows only, no page furniture."""
        fragment = run_views.render_chat_messages([
            {"author": "agent", "body": "Rebase complete.",
             "created_at": "2026-09-11T15:40:00+00:00", "delivery_state": "recorded"},
        ])
        self.assertIn("Rebase complete.", fragment)
        self.assertIn("run-message agent", fragment)
        self.assertNotIn("<section", fragment)
        self.assertNotIn("<textarea", fragment)
        self.assertIn("Nothing said yet", run_views.render_chat_messages([]))

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
        self.assertIn("Start follow-up investigation", html)
        self.assertNotIn("action='/runs/run-123/resume'", html)

    def test_terminal_follow_up_controls_are_distinct_and_honest(self):
        """Both follow-up controls said "Start follow-up run" and behaved
        differently: one requires a message, the other does not.  Neither
        started a run of the original kind."""

        html = run_views.render_run_detail(
            self.sample_run(state="failed", run_kind="review comment")
        )
        self.assertIn(
            "Start follow-up investigation with this message", html
        )
        self.assertIn(
            "Start follow-up investigation without a message", html
        )
        self.assertNotIn(">Start follow-up run<", html)
        self.assertIn("new read-only investigation", html)
        self.assertIn("does not start another review comment run", html)

    def test_failed_run_shows_the_recorded_failure_code_and_summary(self):
        """Both are stored on the session and were rendered by no view."""

        run = self.sample_run(
            state="failed",
            failure_code="checkout_unavailable",
            failure_summary=(
                "no checkout available for this run: no free checkout in the pool"
            ),
        )
        detail = run_views.render_run_detail(run)
        self.assertIn("Why this run ended", detail)
        self.assertIn("no free checkout in the pool", detail)
        self.assertIn("checkout_unavailable", detail)
        card = run_views.render_run_summary(run)
        self.assertIn("no free checkout in the pool", card)
        self.assertIn("checkout_unavailable", card)

    def test_successful_run_renders_no_empty_failure_section(self):
        html = run_views.render_run_detail(self.sample_run(state="succeeded"))
        self.assertNotIn("Why this run ended", html)
        self.assertNotIn("run-failure", html)

    def test_event_type_initialisms_are_not_mangled(self):
        """The same class of bug as the "Ci Failed" one fixed in app.py."""

        html = run_views.render_run_detail(
            self.sample_run(),
            events=[{"event_type": "ltvm_cleanup_abandoned",
                     "summary": "gave up after 3 attempts"}],
        )
        self.assertIn("LTVM cleanup abandoned", html)
        self.assertNotIn("Ltvm cleanup abandoned", html)

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

    def test_detail_offers_exactly_the_listed_write_routes_and_no_others(self):
        for state, expected in WRITE_ROUTES.items():
            with self.subTest(state=state):
                html = run_views.render_run_detail(self.sample_run(state=state))
                self.assertEqual(self.write_routes(html), expected)


if __name__ == "__main__":
    unittest.main()
