import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class PatchWatcherTests(unittest.TestCase):
    def setUp(self):
        app.PATCHES.clear()

    def test_accepts_change_url_and_defaults_title(self):
        patch_record, error = app.add_patch(" https://review.whamcloud.com/c/123/ ")
        self.assertIsNone(error)
        self.assertEqual(patch_record["url"], "https://review.whamcloud.com/c/123")
        self.assertEqual(patch_record["title"], "123")
        self.assertEqual(patch_record["status"], "Pending")
        self.assertEqual(patch_record["lifecycle"], "Open")
        self.assertIn("last_updated", patch_record)

    def test_accepts_full_canonical_change_url(self):
        value = "https://review.whamcloud.com/c/fs/lustre-release/+/61965/3"
        self.assertTrue(app.valid_url(value))
        patch_record, _ = app.add_patch(value)
        self.assertEqual(patch_record["title"], "61965")

    def test_rejects_non_whamcloud_urls_and_non_change_paths(self):
        for url in (
            "http://review.whamcloud.com/c/1",
            "https://example.com/c/1",
            "https://review.whamcloud.com/changes/1",
            "https://review.whamcloud.com/c/",
            "https://review.whamcloud.com/c/fs/lustre-release/+not-a-change",
        ):
            self.assertFalse(app.valid_url(url), url)

    def test_duplicate_is_rejected(self):
        app.add_patch("https://review.whamcloud.com/c/1", "First")
        patch_record, error = app.add_patch(
            "https://review.whamcloud.com/c/1", "Again"
        )
        self.assertIsNone(patch_record)
        self.assertIn("already", error)

    def test_page_escapes_user_values(self):
        app.add_patch("https://review.whamcloud.com/c/1", "<unsafe>")
        rendered = app.page()
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertNotIn("<unsafe>", rendered)

    def test_page_displays_review_and_ci_criteria_as_links(self):
        patch_record, _ = app.add_patch(
            "https://review.whamcloud.com/c/7", "LU-12345 improve watcher"
        )
        patch_record.update(
            patchset=3,
            wip=True,
            review="Ready",
            review_votes=[{"name": "Reviewer", "value": 1}],
            unresolved=2,
            jenkins="PASS",
            jenkins_url="https://build.whamcloud.com/job/lustre-reviews/42/",
            maloo="RUNNING",
            maloo_url="https://testing.whamcloud.com/test_sessions/related?jobs=x&amp=y",
        )
        rendered = app.page()
        for value in (
            "3", "WIP", "Ready", "Reviewer +1",
            "2 unresolved", "PASS", "RUNNING",
        ):
            self.assertIn(value, rendered)
        self.assertIn("href='https://review.whamcloud.com/c/7'", rendered)
        self.assertIn("href='https://jira.whamcloud.com/browse/LU-12345'", rendered)
        self.assertIn("jobs=x&amp;amp=y", rendered)

    def test_review_chips_distinguish_ready_clean_needs_and_veto(self):
        ready = app._review_chip({"review": "Ready"})
        clean = app._review_chip({
            "review": "Pending", "jenkins": "PASS", "maloo": "PASS",
            "unresolved": 0,
        })
        needs = app._review_chip({
            "review": "Pending", "jenkins": "RUNNING", "maloo": "—",
            "unresolved": 0,
        })
        veto = app._review_chip({"review": "Veto"})
        self.assertIn("✓ Ready", ready)
        self.assertIn("tone-good", ready)
        self.assertIn("✓ Clean", clean)
        self.assertIn("tone-info", clean)
        self.assertIn("! Needs", needs)
        self.assertIn("tone-warn", needs)
        self.assertIn("✕ Veto", veto)
        self.assertIn("tone-bad", veto)

    def test_ci_chips_include_service_state_text_and_tone(self):
        passed = app._ci_chip("Jenkins", "PASS")
        failed = app._ci_chip("Maloo", "FAIL")
        running = app._ci_chip("Maloo", "RUNNING")
        self.assertIn("✓ Jenkins pass", passed)
        self.assertIn("tone-good", passed)
        self.assertIn("✕ Maloo fail", failed)
        self.assertIn("tone-bad", failed)
        self.assertIn("… Maloo running", running)
        self.assertIn("tone-warn", running)

    def test_watch_state_chips_are_accessibly_labelled(self):
        attention = app._watch_chip("needs-attention")
        ready = app._watch_chip("ready")
        waiting = app._watch_chip("awaiting-ci")
        merged = app._watch_chip("merged")
        abandoned = app._watch_chip("abandoned")
        self.assertIn("✕ Needs Attention", attention)
        self.assertIn("tone-bad", attention)
        self.assertIn("✓ Ready", ready)
        self.assertIn("! Awaiting Ci", waiting)
        self.assertIn("Merged", merged)
        self.assertIn("tone-good", merged)
        self.assertIn("Abandoned", abandoned)
        self.assertIn("tone-bad", abandoned)

    def test_table_folds_lifecycle_and_places_patchset_after_ci(self):
        rendered = app.page()
        self.assertNotIn("<th>Lifecycle</th>", rendered)
        self.assertLess(
            rendered.index("<th>Jenkins / Maloo</th>"),
            rendered.index("<th>Patchset</th>"),
        )

    def test_table_has_only_global_refresh_and_overall_checked_time(self):
        patch_record, _ = app.add_patch("https://review.whamcloud.com/c/11")
        patch_record["last_checked"] = "2026-08-29T21:00:00+00:00"
        rendered = app.page()
        self.assertIn("Overall last checked: 2026-08-29T21:00:00+00:00", rendered)
        self.assertEqual(rendered.count("action='/refresh-all'"), 1)
        self.assertNotIn("action='/refresh'", rendered)
        self.assertNotIn("<th>Last checked</th>", rendered)

    def test_add_form_accepts_url_only(self):
        rendered = app.page()
        self.assertIn("name='url'", rendered)
        self.assertNotIn("name='title'", rendered)

    def test_page_includes_disabled_review_handling_stubs(self):
        rendered = app.page()
        self.assertIn("Handle reviews", rendered)
        self.assertIn("Handle simple comments", rendered)
        self.assertIn("Handle all comments", rendered)
        self.assertIn("Stub · disabled", rendered)
        self.assertEqual(rendered.count("aria-disabled='true'"), 2)
        self.assertNotIn("action='/handle-review", rendered)

    def test_ticket_requires_leading_issue_key(self):
        self.assertEqual(app.ticket_from_title("LU-12345: fix pages"), "LU-12345")
        self.assertEqual(app.ticket_from_title("EX-9 work"), "EX-9")
        self.assertEqual(app.ticket_from_title("fix mentions LU-1 later"), "")
        self.assertEqual(app.ticket_from_title("<LU-1>"), "")

    def test_jira_base_and_title_are_escaped(self):
        app.add_patch(
            "https://review.whamcloud.com/c/9", "LU-9 <script>alert(1)</script>"
        )
        rendered = app.page(jira_base="https://jira.example/browse?next=<bad>")
        self.assertIn("LU-9 &lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("next=&lt;bad&gt;/LU-9", rendered)

    def test_seed_file_loads_urls_and_refreshes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed = Path(temp_dir) / "patches.txt"
            seed.write_text(
                "# recent patches\n"
                "https://review.whamcloud.com/c/1\tLU-1 first\n"
                "https://review.whamcloud.com/c/2\n",
                encoding="utf-8",
            )
            with patch("app.refresh_patch") as refresh:
                loaded = app.load_seed_file(seed)
        self.assertEqual([item["url"] for item in loaded], [
            "https://review.whamcloud.com/c/1",
            "https://review.whamcloud.com/c/2",
        ])
        self.assertEqual(refresh.call_count, 2)

    def test_seed_file_rejects_bad_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed = Path(temp_dir) / "patches.txt"
            seed.write_text("https://example.com/c/1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid seed entry"):
                app.load_seed_file(seed)


if __name__ == "__main__":
    unittest.main()
