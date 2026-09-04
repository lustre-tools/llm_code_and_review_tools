import re
import unittest
from dataclasses import dataclass

import lane_views


class LaneViewTests(unittest.TestCase):
    def test_missing_summary_state_is_visibly_inert(self):
        html = lane_views.render_autonomous_lane_summary()
        self.assertIn("Global kill switch: Unknown (treated as disabled)", html)
        self.assertIn("Configured lane: <strong>unknown</strong>", html)
        self.assertIn("Budgets are unknown; no autonomous action is permitted", html)
        self.assertIn("does not grant credentials or broader worker authority", html)
        self.assertNotIn("Global kill switch: Enabled", html)

    def test_summary_has_post_only_global_project_and_replay_controls(self):
        html = lane_views.render_autonomous_lane_summary({
            "global_enabled": True,
            "lane": {"name": "safe-retest", "version": 3},
            "project_overrides": [{
                "project": "fs/lustre-release",
                "mode": "disabled",
                "effective_enabled": False,
            }],
            "replay": {"state": "complete", "summary": "12 observations evaluated"},
        }, csrf_token="token<&'\"")
        self.assertIn("Global kill switch: Enabled", html)
        self.assertIn("safe-retest", html)
        self.assertIn("version 3", html)
        self.assertIn("fs/lustre-release", html)
        self.assertIn("Disable / kill switch", html)
        self.assertIn("12 observations evaluated", html)
        self.assertGreaterEqual(html.count("method='post'"), 3)
        self.assertEqual(html.count("name='csrf_token'"), html.count("<form"))
        self.assertNotRegex(html, r"(?i)<form[^>]+method=['\"]get")
        self.assertNotIn("token<&", html)

    def test_patch_control_shows_exact_revision_rejection_and_override(self):
        revision = "a" * 40
        html = lane_views.render_patch_lane_controls(
            {"change_number": 68541, "patchset": 7, "revision_sha": revision},
            policy={
                "lane_name": "deterministic-retest",
                "lane_version": "v1",
                "mode": "enabled",
                "effective_enabled": True,
            },
            evaluation={
                "eligible": False,
                "code": "non_maloo_minus_one",
                "explanation": "Human review blocks automation.",
                "change_number": 68541,
                "patchset": 7,
                "revision_sha": revision,
            },
            csrf_token="csrf",
        )
        self.assertIn("Patch: Enabled", html)
        self.assertIn("deterministic-retest", html)
        self.assertIn("Rejected", html)
        self.assertIn("change 68541, PS 7", html)
        self.assertIn(revision, html)
        self.assertIn("non_maloo_minus_one", html)
        self.assertIn("Human review blocks automation.", html)
        self.assertIn("does not grant credentials", html)
        self.assertRegex(html, r"<option value='enabled' selected>")

    def test_patch_replay_is_bound_to_exact_revision(self):
        revision = "b" * 40
        html = lane_views.render_patch_lane_controls(
            {"change_number": 91, "patchset": 4, "revision_sha": revision},
            replay={"status": "dry_run", "summary": "Would reject"},
            csrf_token="csrf",
        )
        self.assertIn("Replay: Dry run · Would reject", html)
        self.assertIn("name='change_number' value='91'", html)
        self.assertIn("name='patchset' value='4'", html)
        self.assertIn(f"name='revision_sha' value='{revision}'", html)
        self.assertEqual(html.count("name='csrf_token'"), html.count("<form"))

    def test_stale_eligible_decision_cannot_look_current(self):
        html = lane_views.render_patch_lane_controls(
            {"change_number": 91, "patchset": 5, "revision_sha": "b" * 40},
            evaluation={
                "eligible": True, "code": "eligible", "explanation": "Matched.",
                "change_number": 91, "patchset": 4, "revision_sha": "a" * 40,
            },
        )
        self.assertIn("Stale decision", html)
        self.assertIn("cannot authorize the current patch revision", html)
        self.assertNotIn("class='lane-eligibility tone-good'>Eligible", html)

    def test_missing_evaluation_identity_is_not_invented_from_patch(self):
        html = lane_views.render_patch_lane_controls(
            {"change_number": 91, "patchset": 5, "revision_sha": "b" * 40},
            evaluation={"eligible": False, "explanation": "No evidence."},
        )
        self.assertIn("Exact revision identity unavailable", html)
        decision = html.split("class='lane-latest-evaluation'", 1)[1].split("</div>", 1)[0]
        self.assertNotIn("revision <code>", decision)

    def test_budgets_and_outcomes_are_visible_and_recent_outcomes_bounded(self):
        outcomes = [
            {"state": "complete", "summary": f"outcome {index}", "created_at": f"t{index}"}
            for index in range(12)
        ]
        html = lane_views.render_autonomous_lane_summary({
            "budgets": {"max_actions": 2, "max_runtime_minutes": 20},
            "outcomes": outcomes,
        })
        self.assertIn("Max actions</dt><dd>2", html)
        self.assertIn("Max runtime minutes</dt><dd>20", html)
        self.assertNotIn("outcome 0", html)
        self.assertNotIn("outcome 3", html)
        self.assertIn("outcome 4", html)
        self.assertIn("outcome 11", html)
        self.assertEqual(html.count("class='lane-outcome-state'"), 8)

    def test_dynamic_text_and_attributes_are_escaped(self):
        html = lane_views.render_autonomous_lane_summary({
            "lane_name": "lane<script>",
            "lane_version": "v<&>",
            "project_overrides": [{
                "project": "proj'\"<x>", "mode": "enabled", "enabled": True,
            }],
            "outcomes": [{
                "state": "bad<script>", "summary": "result & <boom>",
                "created_at": "time<&>",
            }],
            "replay": {"state": "bad<x>", "summary": "replay <unsafe>"},
        }, csrf_token="csrf<script>")
        for unsafe in ("<script>", "<boom>", "<unsafe>", "<x>"):
            self.assertNotIn(unsafe, html)
        self.assertIn("lane&lt;script&gt;", html)
        self.assertIn("result &amp; &lt;boom&gt;", html)
        self.assertIn("replay &lt;unsafe&gt;", html)
        self.assertNotIn("csrf<script>", html)

    def test_dataclass_and_to_dict_inputs_are_supported(self):
        @dataclass
        class Evaluation:
            eligible: bool
            code: str
            explanation: str
            change_number: int
            patchset: int
            revision_sha: str

        class Policy:
            def to_dict(self):
                return {
                    "lane_name": "lane-one", "lane_version": 9,
                    "mode": "disabled", "effective_enabled": False,
                }

        html = lane_views.render_patch_lane_controls(
            {"change_number": 8, "patchset": 2, "revision_sha": "c" * 40},
            policy=Policy(),
            evaluation=Evaluation(False, "budget_exhausted", "No actions left.", 8, 2, "c" * 40),
        )
        self.assertIn("lane-one", html)
        self.assertIn("version 9", html)
        self.assertIn("Patch: Disabled", html)
        self.assertIn("budget_exhausted", html)

    def test_every_form_carries_caller_supplied_csrf(self):
        summary = lane_views.render_autonomous_lane_summary({
            "projects": [{"name": "one"}, {"name": "two"}],
        }, csrf_token="exact-csrf")
        patch = lane_views.render_patch_lane_controls(
            {"change_number": 1, "patchset": 1, "revision_sha": "d" * 40},
            csrf_token="exact-csrf",
        )
        for html in (summary, patch):
            forms = re.findall(r"<form\b.*?</form>", html)
            self.assertTrue(forms)
            for form in forms:
                self.assertEqual(form.count("name='csrf_token' value='exact-csrf'"), 1)


if __name__ == "__main__":
    unittest.main()
