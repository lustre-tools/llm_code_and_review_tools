import unittest
from dataclasses import dataclass

import research_views


REVISION = "a" * 40
SESSION = "11111111-2222-3333-4444-555555555555"
SUITE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class ResearchViewTests(unittest.TestCase):
    def patch(self, **changes):
        value = {"change_number": 68160, "status": "open", "patchset": 13,
                 "revision_sha": REVISION, "has_unknown_failure": True}
        value.update(changes)
        return value

    def action(self, kind="associate_bug", **changes):
        value = {"action_id": "action-1", "action_type": kind,
                 "state": "pending_approval", "version": 3,
                 "revision_sha": REVISION, "session_id": SESSION,
                 "test_group": "review-dne-part-1", "suite_name": "sanity",
                 "suite_id": SUITE, "jira_key": "LU-12345",
                 "authority": "approval", "action_budget_remaining": 1}
        if kind == "request_retest":
            value["bug_link_state"] = "succeeded"
        value.update(changes)
        return value

    def test_missing_policy_fails_closed_to_disabled(self):
        html = research_views.render_unknown_failure_control(self.patch())
        self.assertIn("Trigger policy:</strong> Disabled", html)
        self.assertIn("disabled aria-disabled='true'", html)
        self.assertNotIn("<form", html)

    def test_manual_policy_emits_revision_pinned_post(self):
        html = research_views.render_unknown_failure_control(
            self.patch(), policy={"mode": "manual"}, csrf_token="csrf",
            idempotency_token="once")
        self.assertIn("Trigger policy:</strong> Manual", html)
        self.assertIn("method='post' action='/research/investigate'", html)
        self.assertIn("name='revision_sha' value='" + REVISION + "'", html)
        self.assertIn("name='csrf_token' value='csrf'", html)
        self.assertIn("name='attempt_id' value='once'", html)
        self.assertNotIn("method='get'", html.casefold())

    def test_per_patch_controls_have_unique_accessible_heading_ids(self):
        first = research_views.render_unknown_failure_control(
            self.patch(change_number=1), policy={"mode": "manual"})
        second = research_views.render_unknown_failure_control(
            self.patch(change_number=2), policy={"mode": "manual"})
        self.assertIn("aria-labelledby='unknown-research-title-1-13'", first)
        self.assertIn("id='unknown-research-title-2-13'", second)
        self.assertNotIn("id='unknown-research-title-1-13'", second)

    def test_per_patch_disabled_reasons_have_unique_accessible_ids(self):
        first = research_views.render_unknown_failure_control(
            self.patch(change_number=1), policy={"mode": "disabled"})
        second = research_views.render_unknown_failure_control(
            self.patch(change_number=2), policy={"mode": "disabled"})
        self.assertIn("aria-describedby='unknown-research-reason-1-13'", first)
        self.assertIn("id='unknown-research-reason-2-13'", second)
        self.assertNotIn("id='unknown-research-reason-1-13'", second)

    def test_policy_form_defaults_disabled_zero_budget_and_is_revision_pinned_post(self):
        html = research_views.render_research_policy_form(
            self.patch(), csrf_token="csrf", idempotency_token="once")
        self.assertIn("method='post' action='/research/policy/prepare'", html)
        self.assertIn("<option value='disabled' selected>", html)
        self.assertIn("<option value='manual'>", html)
        self.assertIn("<option value='automatic'>", html)
        self.assertIn("name='per_revision_run_budget'", html)
        self.assertIn("value='0'", html)
        self.assertIn("name='revision_sha' value='" + REVISION + "'", html)
        self.assertIn("name='csrf_token' value='csrf'", html)
        self.assertIn("Selecting it prepares a separate confirmation page", html)
        self.assertNotIn("method='get'", html.casefold())

    def test_policy_form_preserves_manual_mode_budget_and_unique_labels(self):
        html = research_views.render_research_policy_form(
            self.patch(change_number=42),
            policy={"mode": "manual", "run_budget": 3, "version": 8})
        self.assertIn("<option value='manual' selected>", html)
        self.assertIn("value='3'", html)
        self.assertIn("for='research-policy-mode-42-13'", html)
        self.assertIn("for='research-policy-budget-42-13'", html)
        self.assertIn("name='expected_policy_version' value='8'", html)

    def test_policy_form_disables_submission_without_exact_revision(self):
        html = research_views.render_research_policy_form(
            self.patch(revision_sha="short"), policy={"mode": "automatic", "budget": 2})
        self.assertIn("disabled aria-disabled='true'", html)
        self.assertIn("policy changes are disabled", html)

    def test_automatic_policy_confirmation_is_separate_token_protected_post(self):
        html = research_views.render_research_policy_confirmation(
            self.patch(), {"mode": "automatic", "run_budget": 2, "version": 8},
            confirmation_token="signed", csrf_token="csrf", idempotency_token="once")
        self.assertIn("Confirm automatic unknown-failure research", html)
        self.assertIn("method='post' action='/research/policy/confirm'", html)
        self.assertIn("name='confirmation_token' value='signed'", html)
        self.assertIn("name='research_mode' value='automatic'", html)
        self.assertIn("name='per_revision_run_budget' value='2'", html)
        self.assertIn("name='revision_sha' value='" + REVISION + "'", html)
        self.assertIn("Read-only · Unsandboxed host worker · General network access", html)
        self.assertNotIn("method='get'", html.casefold())

    def test_research_budget_matches_store_limit(self):
        html = research_views.render_research_policy_form(self.patch())
        self.assertIn("max='20'", html)
        self.assertEqual(research_views.MAX_RESEARCH_RUN_BUDGET, 20)

    def test_automatic_confirmation_rejects_missing_token_bad_mode_budget_or_revision(self):
        cases = (
            (self.patch(), {"mode": "automatic", "budget": 1}, ""),
            (self.patch(), {"mode": "manual", "budget": 1}, "token"),
            (self.patch(), {"mode": "automatic", "budget": 0}, "token"),
            (self.patch(), {"mode": "automatic", "budget": -1}, "token"),
            (self.patch(), {"mode": "automatic", "budget": 21}, "token"),
            (self.patch(revision_sha="short"), {"mode": "automatic", "budget": 1}, "token"),
        )
        for patch, policy, token in cases:
            with self.subTest(policy=policy, revision=patch.get("revision_sha")):
                with self.assertRaises(ValueError):
                    research_views.render_research_policy_confirmation(
                        patch, policy, confirmation_token=token)

    def test_automatic_policy_describes_gates_without_manual_mutation(self):
        html = research_views.render_unknown_failure_control(
            self.patch(), policy={"mode": "automatic"})
        self.assertIn("Trigger policy:</strong> Automatic", html)
        self.assertIn("does not bypass eligibility", html)
        self.assertNotIn("<form", html)

    def test_manual_control_is_disabled_when_not_eligible(self):
        for patch in (self.patch(status="merged"), self.patch(revision_sha=None),
                      self.patch(has_unknown_failure=False), self.patch(active_run_id="run-1")):
            with self.subTest(patch=patch):
                html = research_views.render_unknown_failure_control(
                    patch, policy={"mode": "manual"})
                self.assertIn("disabled aria-disabled='true'", html)
                self.assertNotIn("<form", html)

    def test_empty_research_owner_falls_back_to_non_research_active_owner(self):
        html = research_views.render_unknown_failure_control(
            self.patch(
                active_research_run_id="",
                active_run_id="engineering-run-1",
            ),
            policy={"mode": "manual"},
        )
        self.assertIn("An active run already owns this patch.", html)
        self.assertIn("disabled aria-disabled='true'", html)
        self.assertNotIn("<form", html)

    def test_research_control_prominently_labels_read_only_unsandboxed_network(self):
        html = research_views.render_unknown_failure_control(
            self.patch(), policy={"mode": "manual"})
        self.assertIn("Read-only · Unsandboxed host worker · General network access", html)
        self.assertIn("cannot write to Gerrit, Maloo, Jenkins, or JIRA", html)

    def test_latest_research_has_evidence_recommendation_and_run_link(self):
        research = {"run_id": "run-42", "state": "waiting_human",
                    "revision_sha": REVISION, "recommendation": "link_and_retest",
                    "rationale": "A known failure matches.",
                    "question": "Approve the proposed association?"}
        evidence = [{"label": "Maloo failure", "url": "https://testing.whamcloud.com/x",
                     "detail": "sanity test_1"},
                    {"label": "Run log", "url": "/runs/run-42/log"}]
        html = research_views.render_research_session(research, evidence=evidence)
        for expected in ("State: Waiting human", "Recommendation</dt><dd>Link and retest",
                         "A known failure matches.", "Maloo failure", "sanity test_1",
                         "/runs/run-42", "Approve the proposed association?",
                         "Read-only · Unsandboxed host worker"):
            self.assertIn(expected, html)

    def test_evidence_rejects_javascript_and_protocol_relative_urls(self):
        html = research_views.render_research_session(
            {"run_id": "x", "state": "succeeded", "revision_sha": REVISION},
            evidence=[{"label": "bad", "url": "javascript:alert(1)"},
                      {"label": "also bad", "url": "//evil.example/x"}])
        self.assertNotIn("javascript:", html)
        self.assertNotIn("href='//", html)
        self.assertEqual(html.count("(link unavailable)"), 2)

    def test_step_one_card_is_non_mutating_and_exact(self):
        html = research_views.render_action_approval_card(self.action())
        for expected in ("Step 1 of 2", "Associate existing JIRA key", REVISION,
                         SESSION, "review-dne-part-1", "sanity", SUITE, "LU-12345",
                         "Authority</dt><dd>Approval", "Action budget remaining</dt><dd>1",
                         "must succeed before any retest"):
            self.assertIn(expected, html)
        self.assertIn("href='/approvals/action-1/confirm'", html)
        self.assertNotIn("<form", html)

    def test_step_two_card_requires_successful_step_one(self):
        blocked = research_views.render_action_approval_card(
            self.action("request_retest", bug_link_state="pending"))
        self.assertIn("Step 2 of 2", blocked)
        self.assertIn("must be associated successfully", blocked)
        self.assertIn("Approval unavailable", blocked)
        self.assertNotIn("/confirm'", blocked)
        ready = research_views.render_action_approval_card(self.action("request_retest"))
        self.assertIn("Review exact action", ready)
        self.assertIn("must be recorded as succeeded", ready)

    def test_approval_card_fails_closed_for_authority_budget_revision_or_state(self):
        cases = (self.action(authority="automatic"),
                 self.action(action_budget_remaining=0),
                 self.action(revision_sha="short"),
                 self.action(state="succeeded"),
                 self.action(action_id=None))
        for action in cases:
            with self.subTest(action=action):
                html = research_views.render_action_approval_card(action)
                self.assertIn("Approval unavailable", html)
                self.assertIn("aria-describedby='approval-blockers-", html)
                self.assertNotIn("/confirm'", html)

    def test_step_one_confirmation_is_token_protected_post_and_not_retest(self):
        html = research_views.render_action_confirmation(
            self.action(), confirmation_token="signed", csrf_token="csrf",
            idempotency_token="once")
        self.assertIn("Confirm JIRA association", html)
        self.assertIn("method='post' action='/approvals/action-1/approve'", html)
        self.assertIn("name='confirmation_token' value='signed'", html)
        self.assertIn("name='action_type' value='associate_bug'", html)
        self.assertIn("It does not request a retest", html)
        self.assertNotIn("method='get'", html.casefold())

    def test_step_two_confirmation_is_exactly_one_retest_after_dependency(self):
        html = research_views.render_action_confirmation(
            self.action("request_retest"), confirmation_token="signed")
        for expected in ("Confirm one retest", "exactly one retest", REVISION,
                         SESSION, "review-dne-part-1", SUITE, "LU-12345",
                         "name='test_group' value='review-dne-part-1'",
                         "name='action_type' value='request_retest'"):
            self.assertIn(expected, html)
        self.assertIn("method='post'", html)

    def test_blocked_confirmation_has_no_form_even_with_token(self):
        html = research_views.render_action_confirmation(
            self.action("request_retest", bug_link_state="pending"),
            confirmation_token="signed")
        self.assertIn("Approval unavailable", html)
        self.assertNotIn("<form", html)

    def test_approved_action_status_is_visible_but_has_no_write_control(self):
        html = research_views.render_failure_action_status(
            self.action(
                state="planned",
                approval_state="approved",
                detail="Observer will execute the accepted action.",
            )
        )
        self.assertIn("Queued for execution", html)
        self.assertIn("Observer will execute", html)
        self.assertIn("View action details", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("/confirm", html)

    def test_confirmation_requires_token(self):
        with self.assertRaises(ValueError):
            research_views.render_action_confirmation(
                self.action(), confirmation_token="")

    def test_dynamic_text_and_form_attributes_are_escaped(self):
        action = self.action(action_id="x' onclick='bad", suite_name="<script>",
                             jira_key="LU-12345")
        html = research_views.render_action_confirmation(
            action, confirmation_token="' onmouseover='bad")
        self.assertNotIn("<script>", html)
        self.assertNotIn("onclick='bad'", html)
        self.assertNotIn("onmouseover='bad'", html)
        self.assertIn("value='x&#x27; onclick=&#x27;bad'", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("%27%20onclick%3D%27bad", html)

    def test_dataclass_input_and_accessible_landmarks(self):
        @dataclass
        class Action:
            action_id: str = "dc-1"
            action_type: str = "associate_bug"
            state: str = "pending_approval"
            revision_sha: str = REVISION
            session_id: str = SESSION
            test_group: str = "group"
            suite_name: str = "sanity"
            suite_id: str = SUITE
            jira_key: str = "LU-1"
            authority: str = "approval"
            action_budget_remaining: int = 1
        html = research_views.render_action_approval_card(Action())
        self.assertIn("<article class='action-approval-card'", html)
        self.assertIn("aria-labelledby='approval-card-title-dc-1'", html)
        self.assertIn("Review exact action", html)

    def test_views_never_offer_other_external_writes(self):
        html = (research_views.render_action_approval_card(self.action())
                + research_views.render_action_approval_card(self.action("request_retest")))
        lower = html.casefold()
        for forbidden in ("vote gerrit", "upload patch", "trigger jenkins",
                          "create jira", "post review"):
            self.assertNotIn(forbidden, lower)


if __name__ == "__main__":
    unittest.main()
