import unittest

import app


class PatchWatcherTests(unittest.TestCase):
    def setUp(self):
        app.PATCHES.clear()

    def test_accepts_change_url_and_defaults_title(self):
        patch, error = app.add_patch(" https://review.whamcloud.com/c/123/ ")
        self.assertIsNone(error)
        self.assertEqual(patch["url"], "https://review.whamcloud.com/c/123")
        self.assertEqual(patch["title"], "123")
        self.assertEqual(patch["status"], "Pending")
        self.assertEqual(patch["lifecycle"], "Open")
        self.assertIn("last_updated", patch)

    def test_rejects_non_whamcloud_urls(self):
        for url in ("http://review.whamcloud.com/c/1", "https://example.com/c/1", "https://review.whamcloud.com/changes/1", "https://review.whamcloud.com/c/"):
            self.assertFalse(app.valid_url(url), url)

    def test_duplicate_is_rejected(self):
        app.add_patch("https://review.whamcloud.com/c/1", "First")
        patch, error = app.add_patch("https://review.whamcloud.com/c/1", "Again")
        self.assertIsNone(patch)
        self.assertIn("already", error)

    def test_page_escapes_user_values(self):
        app.add_patch("https://review.whamcloud.com/c/1", "<unsafe>")
        rendered = app.page()
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertNotIn("<unsafe>", rendered)

    def test_page_displays_review_and_ci_criteria(self):
        patch, _ = app.add_patch("https://review.whamcloud.com/c/7", "Series")
        patch.update(patchset=3, wip=True, review="+2", unresolved=2, jenkins="PASS", maloo="RUNNING")
        rendered = app.page()
        for value in ("Open", "3", "Yes", "+2 / 2", "PASS / RUNNING"):
            self.assertIn(value, rendered)


if __name__ == "__main__":
    unittest.main()
