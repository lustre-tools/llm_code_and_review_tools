import unittest
from types import SimpleNamespace

from external_action_views import (
    render_jenkins_retrigger_confirmation,
    render_jenkins_retrigger_control,
    render_review_reply_confirmation,
    render_review_reply_control,
)


class ExternalActionViewTests(unittest.TestCase):
    def test_review_reply_control_is_disabled_until_explicitly_eligible(self):
        html = render_review_reply_control(
            run_id="run-1", enabled=False, eligible=True, csrf_token="csrf",
        )
        self.assertIn("Post Gerrit replies", html)
        self.assertIn("disabled", html)
        html = render_review_reply_control(
            run_id="run-1", enabled=True, eligible=True, csrf_token="csrf",
        )
        self.assertNotIn("aria-disabled", html)

    def test_review_confirmation_names_exact_binding(self):
        plan = SimpleNamespace(
            reply_id="reply-1", change_number=7, patchset=3,
            revision_sha="a" * 40, snapshot_sha256="b" * 64,
            draft_sha256="c" * 64, binding_digest="d" * 64,
            payload={"comments": {"x.c": [{"message": "done"}]}},
        )
        html = render_review_reply_confirmation(
            plan, token="token", expires_at="123", csrf_token="csrf",
        )
        self.assertIn("change 7, PS 3", html)
        self.assertIn("a" * 40, html)
        self.assertIn("b" * 64, html)
        self.assertIn("d" * 64, html)
        self.assertIn("Target: <code>x.c</code>", html)
        self.assertIn("<pre>done</pre>", html)

    def test_review_confirmation_shows_and_escapes_exact_reply_targets(self):
        plan = SimpleNamespace(
            reply_id="reply-1", change_number=7, patchset=3,
            revision_sha="a" * 40, snapshot_sha256="b" * 64,
            draft_sha256="c" * 64, binding_digest="d" * 64,
            payload={"comments": {
                "src/<unsafe>.c": [{
                    "line": 17,
                    "message": "Use x < y & keep 'quotes'.\nSecond line.",
                }],
                "/PATCHSET_LEVEL": [{"message": "Patch-level reply."}],
            }},
        )
        html = render_review_reply_confirmation(
            plan, token="token", expires_at="123", csrf_token="csrf",
        )
        self.assertIn("Target: <code>src/&lt;unsafe&gt;.c:17</code>", html)
        self.assertIn(
            "<pre>Use x &lt; y &amp; keep &#x27;quotes&#x27;.\nSecond line.</pre>",
            html,
        )
        self.assertIn("Target: <code>/PATCHSET_LEVEL</code>", html)
        self.assertIn("<pre>Patch-level reply.</pre>", html)
        self.assertNotIn("src/<unsafe>.c", html)

    def test_review_confirmation_rejects_unbounded_reply_message(self):
        plan = SimpleNamespace(
            reply_id="reply-1", change_number=7, patchset=3,
            revision_sha="a" * 40, snapshot_sha256="b" * 64,
            draft_sha256="c" * 64, binding_digest="d" * 64,
            payload={"comments": {"x.c": [{"line": 1, "message": "x" * 4_001}]}},
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            render_review_reply_confirmation(
                plan, token="token", expires_at="123", csrf_token="csrf",
            )

    def test_jenkins_control_requires_exact_failed_patch(self):
        patch = {
            "change_number": 7, "patchset": 3, "revision_sha": "a" * 40,
            "revision_ref": "refs/changes/07/7/3", "project": "fs/lustre-release",
            "lifecycle": "Open", "jenkins": "FAILURE",
            "jenkins_url": "https://build.whamcloud.com/job/lustre-reviews/2/",
        }
        html = render_jenkins_retrigger_control(
            patch, enabled=True, csrf_token="csrf",
        )
        self.assertIn("Retrigger Jenkins", html)
        self.assertNotIn("aria-disabled", html)
        self.assertNotIn("idempotency_token", html)

    def test_jenkins_ambiguous_state_exposes_reconciliation(self):
        plan = SimpleNamespace(
            action_id="action-1", state="ambiguous",
            summary="Dispatch outcome is uncertain.",
        )
        html = render_jenkins_retrigger_control(
            {}, plan=plan, enabled=False, csrf_token="csrf",
        )
        self.assertIn("Jenkins retrigger:", html)
        self.assertIn("ambiguous", html)
        self.assertIn(
            "action='/jenkins-retriggers/action-1/reconcile'", html,
        )
        self.assertIn("Dispatch outcome is uncertain.", html)

    def test_jenkins_confirmation_names_exact_build_and_revision(self):
        plan = SimpleNamespace(
            action_id="action-1", job_name="lustre-reviews", build_number=2,
            change_number=7, patchset=3, revision_sha="a" * 40,
            snapshot_sha256="b" * 64, binding_digest="c" * 64,
        )
        html = render_jenkins_retrigger_confirmation(
            plan, token="token", expires_at="123", csrf_token="csrf",
        )
        self.assertIn("lustre-reviews", html)
        self.assertIn("#2", html)
        self.assertIn("a" * 40, html)
        self.assertIn("c" * 64, html)


if __name__ == "__main__":
    unittest.main()
