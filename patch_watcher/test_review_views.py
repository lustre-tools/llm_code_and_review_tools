import unittest

from patch_watcher.review_views import (
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

    def test_start_control_offers_both_modes_when_eligible(self):
        html = render_review_start_control(
            self.patch(), csrf_token="csrf", idempotency_token="request",
        )
        self.assertIn("Handle simple comments", html)
        self.assertIn("Handle all comments", html)
        self.assertNotIn(" disabled", html)

    def test_control_fails_closed_without_unresolved_comments_or_an_owner(self):
        for updates, reason in (
            ({"unresolved": 0}, "no unresolved review comments"),
            ({"active_run_id": "pw-review-active"}, "already owns this patch"),
        ):
            with self.subTest(reason=reason):
                html = render_review_start_control(
                    self.patch(**updates), csrf_token="csrf",
                    idempotency_token="request",
                )
                self.assertIn(" disabled", html)
                self.assertIn(reason, html)

    def test_confirmation_binds_snapshot_and_has_no_later_approval(self):
        html = render_review_start_confirmation(
            self.patch(), self.snapshot(), mode="simple",
            confirmation_token="signed", idempotency_token="request",
            confirmation_expires_at="123", csrf_token="csrf",
        )
        self.assertIn("a" * 64, html)
        self.assertIn("There is no later upload confirmation", html)
        self.assertIn("uploads the new patchset itself with the gerrit CLI", html)
        self.assertIn("The controller does not upload on its behalf", html)

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
        self.assertIn("the controller posts nothing", html)


if __name__ == "__main__":
    unittest.main()
