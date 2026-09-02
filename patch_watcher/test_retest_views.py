import unittest

from retest_views import (
    render_action_confirmation,
    render_enable_confirmation,
    render_global_retest_status,
    render_policy_confirmation,
    render_retest_control,
)


class RetestViewTests(unittest.TestCase):
    def test_global_gate_is_truthful_and_get_is_confirmation_only(self):
        off = render_global_retest_status(
            execution_enabled=False, csrf_token="csrf<&"
        )
        self.assertIn("Global execution: Disabled", off)
        self.assertIn("href='/automation/global/confirm-enable'", off)
        self.assertNotIn("method='get'", off.casefold())
        on = render_global_retest_status(
            execution_enabled=True, csrf_token="csrf"
        )
        self.assertIn("Global execution: Enabled", on)
        self.assertIn("method='post' action='/automation/global/disable'", on)

    def test_patch_control_has_all_modes_dry_run_and_escapes(self):
        html = render_retest_control(
            {"change_number": "12<", "revision_sha": "abc&"},
            {"mode": "approval", "max_actions": 2},
            evaluation={"status": "waiting_approval", "reason": "Bug <linked>"},
            timeline=[{
                "created_at": "now<", "event_type": "decision",
                "summary": "No > action",
            }],
            csrf_token="csrf<&",
        )
        for mode in ("disabled", "advise", "approval", "automatic"):
            self.assertIn(f"value='{mode}'", html)
        self.assertIn("action='/automation/dry-run'", html)
        self.assertIn("Bug &lt;linked&gt;", html)
        self.assertNotIn("12<", html)
        self.assertNotIn("abc&'", html)

    def test_unpinned_patch_controls_are_disabled(self):
        html = render_retest_control(
            {}, {"mode": "disabled"}, csrf_token="csrf"
        )
        self.assertGreaterEqual(html.count(" disabled"), 2)

    def test_dangerous_changes_have_explicit_post_confirmations(self):
        global_html = render_enable_confirmation(csrf_token="csrf")
        self.assertIn("method='post' action='/automation/global/enable'", global_html)
        self.assertNotIn("method='get'", global_html.casefold())
        policy_html = render_policy_confirmation(
            change_number="12", revision_sha="a" * 40,
            max_actions=1, csrf_token="csrf",
        )
        self.assertIn("method='post' action='/automation/policy/confirm'", policy_html)
        self.assertIn("Confirm Automatic policy", policy_html)

    def test_approval_control_links_to_non_mutating_review_page(self):
        rendered = render_retest_control(
            {"change_number": 68160, "revision_sha": "a" * 40},
            {"mode": "approval", "action_budget": 1},
            approval_action={
                "action_id": "action-1",
                "session_id": "session-7",
                "jira_ticket": "LU-7",
            },
            csrf_token="csrf",
        )
        self.assertIn("Approval required", rendered)
        self.assertIn("href='/automation/actions/action-1/confirm'", rendered)
        self.assertNotIn("action='/automation/actions/action-1/approve'", rendered)

    def test_action_confirmation_posts_exact_revision_with_csrf(self):
        rendered = render_action_confirmation(
            action_id="action-1",
            change_number="68160",
            revision_sha="a" * 40,
            session_id="session-7",
            jira_ticket="LU-7",
            csrf_token="csrf-token",
        )
        self.assertIn("action='/automation/actions/action-1/approve'", rendered)
        self.assertIn("name='revision_sha' value='" + "a" * 40 + "'", rendered)
        self.assertIn("csrf-token", rendered)


if __name__ == "__main__":
    unittest.main()
