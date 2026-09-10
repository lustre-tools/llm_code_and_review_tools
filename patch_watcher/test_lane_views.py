import inspect
import re
import unittest

from patch_watcher import lane_views


class LaneViewTests(unittest.TestCase):
    def test_missing_summary_state_is_visibly_inert(self):
        html = lane_views.render_autonomous_lane_summary()
        self.assertIn("Unattended actions: Unknown (treated as disabled)", html)
        self.assertIn("One rule is configured: <strong>unknown</strong>", html)
        self.assertIn("Budgets are unknown; no unattended action is permitted", html)
        self.assertIn("grants no credentials and no broader authority", html)
        self.assertNotIn("Unattended actions: Enabled", html)

    def test_summary_is_status_only_apart_from_replay(self):
        """The lane's own global and project switches are gone: a patch's level
        enrols it, and re-applies that every poll, so a switch here would have
        been silently undone.  What remains is a badge, what the rule does,
        and the dry-run control -- the one form left."""

        html = lane_views.render_autonomous_lane_summary({
            "global_enabled": True,
            "lane": {"name": "safe-retest", "version": 3},
            "replay": {"state": "complete", "summary": "12 observations evaluated"},
        }, csrf_token="token<&'\"")
        self.assertIn("Unattended actions: Enabled", html)
        self.assertIn("safe-retest", html)
        self.assertIn("version 3", html)
        self.assertIn("nothing to switch here", html)
        self.assertIn("12 observations evaluated", html)
        self.assertNotIn("Project overrides", html)
        self.assertNotIn("Allow unattended actions", html)
        self.assertNotIn("Disable / kill switch", html)
        self.assertEqual(html.count("<form"), 1)
        self.assertEqual(html.count("name='csrf_token'"), 1)
        self.assertNotRegex(html, r"(?i)<form[^>]+method=['\"]get")
        self.assertNotIn("token<&", html)

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
        class Status:
            def to_dict(self):
                return {
                    "global_enabled": False,
                    "lane": {"name": "lane-one", "version": 9},
                    "replay": {"state": "pending", "summary": "not yet"},
                }

        html = lane_views.render_autonomous_lane_summary(Status())
        self.assertIn("lane-one", html)
        self.assertIn("version 9", html)
        self.assertIn("Unattended actions: Disabled", html)
        self.assertIn("not yet", html)

    def test_every_form_carries_caller_supplied_csrf(self):
        summary = lane_views.render_autonomous_lane_summary({}, csrf_token="exact-csrf")
        for html in (summary,):
            forms = re.findall(r"<form\b.*?</form>", html)
            self.assertTrue(forms)
            for form in forms:
                self.assertEqual(form.count("name='csrf_token' value='exact-csrf'"), 1)


if __name__ == "__main__":
    unittest.main()


class SwitchBadgePolarityTests(unittest.TestCase):
    """A safety badge must never be labelled as the inverse of its value.

    The global badge read "Global kill switch: Enabled", in green, precisely
    when lanes were live and permitted to make remote Maloo writes. An operator
    scanning for "is automation stopped" would read that as yes.
    """

    def test_enabled_means_the_named_thing_is_on(self):
        self.assertIn("Unattended actions: Enabled", lane_views._switch_badge(
            "Unattended actions", True))
        self.assertIn("tone-good", lane_views._switch_badge("Unattended actions", True))

    def test_no_badge_label_reads_inverted_against_its_value(self):
        # Only the _switch_badge CALL SITES matter -- "kill switch" is a fine
        # word elsewhere (the Disable dropdown option, prose about bypassing
        # one). What must never happen is a badge whose label names the
        # opposite of the boolean it renders.
        import ast

        tree = ast.parse(inspect.getsource(lane_views))
        labels = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "_switch_badge"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        self.assertTrue(labels, "no _switch_badge call sites found")
        for label in labels:
            with self.subTest(label=label):
                self.assertNotIn("kill", label.casefold())
                self.assertNotIn("disable", label.casefold())

    def test_unknown_fails_closed_and_says_so(self):
        badge = lane_views._switch_badge("Unattended actions", None)
        self.assertIn("Unknown (treated as disabled)", badge)
        self.assertIn("tone-neutral", badge)
