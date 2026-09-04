import unittest

from review_views import (
    render_review_result,
    render_review_start_confirmation,
    render_review_start_control,
)


class ReviewViewsTests(unittest.TestCase):
    def patch(self, **updates):
        value = {
            "change_number": 68541, "patchset": 3,
            "revision_sha": "d" * 40,
            "revision_ref": "refs/changes/41/68541/3",
            "project": "fs/lustre-release", "lifecycle": "Open",
            "unresolved": 2, "active_run_id": "",
        }
        value.update(updates)
        return value

    def snapshot(self):
        return {
            "snapshot_sha256": "a" * 64,
            "threads": [{"thread_id": "one"}, {"thread_id": "two"}],
        }

    def test_start_control_is_truthful_about_auto_upload_and_draft_replies(self):
        html = render_review_start_control(
            self.patch(), csrf_token="csrf", idempotency_token="request",
            upload_enabled=True,
        )
        self.assertIn("Handle simple comments", html)
        self.assertIn("Handle all comments", html)
        self.assertIn("upload one new patchset automatically", html)
        self.assertIn("separate controller action", html)
        self.assertNotIn(" disabled", html)

    def test_control_fails_closed_when_upload_is_disabled(self):
        html = render_review_start_control(
            self.patch(), csrf_token="csrf", idempotency_token="request",
            upload_enabled=False,
        )
        self.assertIn("disabled", html)
        self.assertIn("kill switch", html)

    def test_confirmation_binds_snapshot_and_has_no_later_approval(self):
        html = render_review_start_confirmation(
            self.patch(), self.snapshot(), mode="simple",
            confirmation_token="signed", idempotency_token="request",
            confirmation_expires_at="123", csrf_token="csrf",
        )
        self.assertIn("a" * 64, html)
        self.assertIn("There is no later upload confirmation", html)
        self.assertIn("separate controller action", html)

    def test_result_escapes_untrusted_reply_and_has_no_post_control(self):
        html = render_review_result(
            {"request_kind": "review_comments", "review_mode": "all",
             "review_snapshot_sha256": "a" * 64},
            {"comment_results": [{
                "comment_id": "c1", "assessment": "simple",
                "disposition": "reply_draft",
                "summary": "No code change", "reply_draft": "<script>bad()</script>",
                "changed_files": [],
            }]},
        )
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("Post reply", html)


if __name__ == "__main__":
    unittest.main()
