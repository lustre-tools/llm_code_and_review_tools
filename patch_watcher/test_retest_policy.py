import dataclasses
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from retest_policy import (
    JiraBugLink,
    MalooFailure,
    PendingRetest,
    RetestBudget,
    RetestPolicy,
    RevisionSnapshot,
    ReviewVote,
    evaluate_retests,
    failure_fingerprint,
)


SHA = "0123456789abcdef0123456789abcdef01234567"


class RetestPolicyTests(unittest.TestCase):
    def bug(self, key="LU-12345", accepted=True, evidence="Maloo suite link"):
        return JiraBugLink(key, accepted, evidence)

    def failure(self, **overrides):
        values = {
            "session_id": "session-900",
            "test_group": "group-7",
            "suite": "sanity", "enforced": True,
            "linked_bugs": (self.bug(),),
            "failing_subtests": ("test_17",),
            "remote_failure_id": "failure-1",
        }
        values.update(overrides)
        return MalooFailure(**values)

    def snapshot(self, **overrides):
        values = {
            "gerrit_server": "https://review.whamcloud.com",
            "change_number": 68160, "patchset_number": 4,
            "revision_sha": SHA, "lifecycle": "open", "is_current": True,
            "revision_state_complete": True, "maloo_state_complete": True,
            "review_votes": (), "maloo_failures": (self.failure(),), "pending_retests": (),
        }
        values.update(overrides)
        return RevisionSnapshot(**values)

    def policy(self, mode="automatic", global_execution_enabled=True, **overrides):
        values = {
            "mode": mode, "global_execution_enabled": global_execution_enabled,
            "policy_version": "policy-v3", "max_new_actions": 1,
        }
        values.update(overrides)
        return RetestPolicy(**values)

    def budget(self, **overrides):
        values = {"max_actions": 3, "actions_used": 0, "existing_action_keys": frozenset()}
        values.update(overrides)
        return RetestBudget(**values)

    def evaluate(self, snapshot=None, policy=None, budget=None):
        return evaluate_retests(snapshot or self.snapshot(), policy or self.policy(), budget or self.budget())

    def test_global_safety_suppressions_are_precise(self):
        cases = (
            (
                self.snapshot(revision_state_complete=False), self.policy(),
                "revision_unknown", "incomplete",
            ),
            (
                self.snapshot(is_current=False), self.policy(),
                "stale_revision", "not current",
            ),
            (
                self.snapshot(lifecycle="merged"), self.policy(),
                "terminal_change", "merged",
            ),
            (
                self.snapshot(lifecycle="abandoned"), self.policy(),
                "terminal_change", "abandoned",
            ),
            (
                self.snapshot(maloo_state_complete=False), self.policy(),
                "maloo_state_unknown", "incomplete",
            ),
            (
                self.snapshot(), self.policy(mode="disabled"),
                "policy_disabled", "disabled",
            ),
        )
        for snapshot, policy, code, text in cases:
            with self.subTest(code=code, text=text):
                result = self.evaluate(snapshot=snapshot, policy=policy)
                self.assertEqual(result.status, "suppressed")
                self.assertEqual(result.reason_code, code)
                self.assertIn(text, result.reason)
                self.assertEqual(result.decisions, ())

    def test_non_maloo_negative_review_stops_flow_before_maloo(self):
        votes = (
            ReviewVote("Alice", "Code-Review", -1, "human", "Please revise"),
            ReviewVote("BobBot", "Code-Review", -2, "bot"),
        )
        result = self.evaluate(snapshot=self.snapshot(review_votes=votes, maloo_state_complete=False))
        self.assertEqual(result.reason_code, "non_maloo_review_veto")
        self.assertIn("Alice (-1)", result.reason)
        self.assertIn("BobBot (-2)", result.reason)
        self.assertIn("was not evaluated", result.reason)

    def test_maloo_negative_review_is_ci_not_review_gate(self):
        result = self.evaluate(
            snapshot=self.snapshot(review_votes=(ReviewVote("Maloo", "Code-Review", -1, "maloo"),))
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.reason_code, "automatic_retest_ready")

    def test_no_enforced_failure_means_no_action(self):
        result = self.evaluate(
            snapshot=self.snapshot(maloo_failures=(self.failure(enforced=False),))
        )
        self.assertEqual(result.status, "no_action")
        self.assertEqual(result.reason_code, "no_enforced_failures")
        self.assertEqual(result.decisions, ())

    def test_modes_are_visible_and_only_enabled_automatic_authorizes(self):
        cases = (
            ("advise", True, "advice", "advice_only", False),
            ("approval", True, "waiting_approval", "approval_required", False),
            ("approval", False, "waiting_approval", "approval_required", False),
            ("automatic", False, "suppressed", "global_execution_disabled", False),
            ("automatic", True, "ready", "automatic_retest_ready", True),
        )
        for mode, global_enabled, outcome, code, allowed in cases:
            with self.subTest(mode=mode, global_enabled=global_enabled):
                result = self.evaluate(policy=self.policy(mode, global_enabled))
                decision = result.decisions[0]
                self.assertEqual(result.status, outcome)
                self.assertEqual(decision.outcome, outcome)
                self.assertEqual(decision.reason_code, code)
                self.assertIsNotNone(decision.action)
                self.assertEqual(decision.action.execution_allowed, allowed)
                self.assertTrue(result.dry_run)
        approval_off = self.evaluate(policy=self.policy("approval", False)).decisions[0]
        self.assertIn("global gate applies only to automatic", approval_off.reason)

    def test_failures_across_groups_in_one_session_coalesce_to_one_action(self):
        failures = (
            self.failure(suite="sanity", remote_failure_id="one", linked_bugs=(self.bug("LU-2"),)),
            self.failure(
                test_group="group-8", suite="recovery-small", remote_failure_id="two",
                linked_bugs=(self.bug("LU-1"),),
            ),
        )
        result = self.evaluate(snapshot=self.snapshot(maloo_failures=failures))
        self.assertEqual(len(result.decisions), 1)
        decision = result.decisions[0]
        self.assertEqual(decision.suites, ("recovery-small", "sanity"))
        self.assertEqual(len(decision.failure_fingerprints), 2)
        self.assertEqual(decision.action.jira_justification, "LU-1")
        self.assertEqual(decision.action.all_linked_bug_keys, ("LU-1", "LU-2"))
        self.assertEqual(len(decision.action.linked_bug_evidence), 2)
        self.assertEqual(decision.action.test_groups, ("group-7", "group-8"))
        self.assertEqual(decision.action.action_key.count("maloo-retest:"), 1)

    def test_unknown_suite_blocks_entire_session_without_inventing_bug(self):
        failures = (
            self.failure(suite="known", linked_bugs=(self.bug("LU-9"),)),
            self.failure(suite="unknown", remote_failure_id="two", linked_bugs=()),
        )
        result = self.evaluate(snapshot=self.snapshot(maloo_failures=failures))
        self.assertEqual(result.status, "investigate")
        decision = result.decisions[0]
        self.assertEqual(decision.outcome, "investigate")
        self.assertEqual(decision.reason_code, "investigate_phase_2")
        self.assertIn("unknown", decision.reason)
        self.assertIsNone(decision.action)
        self.assertEqual(decision.linked_bug_keys, ("LU-9",))

    def test_incomplete_bug_link_state_requires_investigation(self):
        failure = self.failure(bug_links_complete=False)
        decision = self.evaluate(snapshot=self.snapshot(maloo_failures=(failure,))).decisions[0]
        self.assertEqual(decision.reason_code, "investigate_phase_2")
        self.assertIsNone(decision.action)

    def test_pending_retest_precedes_link_research_and_does_not_duplicate(self):
        pending = PendingRetest("session-900", "group-7", "remote-55")
        unknown = self.failure(linked_bugs=(), bug_links_complete=False)
        result = self.evaluate(
            snapshot=self.snapshot(maloo_failures=(unknown,), pending_retests=(pending,))
        )
        decision = result.decisions[0]
        self.assertEqual(decision.outcome, "waiting_external")
        self.assertEqual(decision.reason_code, "pending_retest")
        self.assertIn("remote-55", decision.reason)
        self.assertIsNone(decision.action)

    def test_existing_action_key_coalesces_repeated_poll(self):
        first = self.evaluate()
        key = first.decisions[0].action.action_key
        repeated = self.evaluate(budget=self.budget(existing_action_keys=frozenset({key})))
        decision = repeated.decisions[0]
        self.assertEqual(decision.outcome, "waiting_external")
        self.assertEqual(decision.reason_code, "action_already_recorded")
        self.assertFalse(decision.action.execution_allowed)
        self.assertEqual(decision.action.action_key, key)

    def test_budget_exhaustion_preserves_non_executable_preview(self):
        result = self.evaluate(budget=self.budget(max_actions=2, actions_used=2))
        decision = result.decisions[0]
        self.assertEqual(decision.outcome, "suppressed")
        self.assertEqual(decision.reason_code, "action_budget_exhausted")
        self.assertFalse(decision.action.execution_allowed)

    def test_action_limit_selects_first_group_deterministically(self):
        failures = (
            self.failure(session_id="session-z", test_group="group-z"),
            self.failure(session_id="session-a", test_group="group-a", remote_failure_id="two"),
        )
        result = self.evaluate(snapshot=self.snapshot(maloo_failures=failures))
        self.assertEqual(len(result.decisions), 2)
        self.assertEqual(result.decisions[0].session_id, "session-a")
        self.assertEqual(result.decisions[0].outcome, "ready")
        self.assertEqual(result.decisions[1].reason_code, "evaluation_action_limit")

    def test_fingerprints_are_stable_across_input_order_and_link_order(self):
        one = self.failure(
            suite="suite-a", remote_failure_id="a", failing_subtests=("two", "one"),
            linked_bugs=(self.bug("LU-2"), self.bug("LU-1", evidence="one")),
        )
        two = self.failure(
            suite="suite-b", remote_failure_id="b", failing_subtests=("z",),
            linked_bugs=(self.bug("LU-3"),),
        )
        left = self.evaluate(snapshot=self.snapshot(maloo_failures=(one, two)))
        one_reordered = dataclasses.replace(one, linked_bugs=tuple(reversed(one.linked_bugs)))
        right = self.evaluate(snapshot=self.snapshot(maloo_failures=(two, one_reordered)))
        self.assertEqual(left.to_dict(), right.to_dict())
        self.assertEqual(
            failure_fingerprint(self.snapshot(), one),
            failure_fingerprint(self.snapshot(), one_reordered),
        )

    def test_action_fingerprint_changes_with_bug_evidence_not_failure_identity(self):
        original = self.failure(linked_bugs=(self.bug("LU-1", evidence="first"),))
        revised = dataclasses.replace(
            original, linked_bugs=(self.bug("LU-1", evidence="updated evidence"),)
        )
        first = self.evaluate(snapshot=self.snapshot(maloo_failures=(original,))).decisions[0]
        second = self.evaluate(snapshot=self.snapshot(maloo_failures=(revised,))).decisions[0]
        self.assertEqual(first.failure_fingerprints, second.failure_fingerprints)
        self.assertEqual(first.trigger_fingerprint, second.trigger_fingerprint)
        self.assertEqual(first.action.action_key, second.action.action_key)
        self.assertNotEqual(first.action.action_fingerprint, second.action.action_fingerprint)

    def test_parallel_evaluation_is_race_independent_and_side_effect_free(self):
        snapshot = self.snapshot()
        policy = self.policy()
        budget = self.budget()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _index: evaluate_retests(snapshot, policy, budget), range(100)))
        canonical = json.dumps(results[0].to_dict(), sort_keys=True)
        self.assertTrue(all(json.dumps(result.to_dict(), sort_keys=True) == canonical for result in results))
        self.assertTrue(all(result.decisions[0].action.execution_allowed for result in results))

    def test_validation_rejects_unsafe_or_ambiguous_inputs(self):
        with self.assertRaisesRegex(ValueError, "Jira issue key"):
            self.bug("not-a-ticket")
        with self.assertRaisesRegex(ValueError, "full hexadecimal"):
            self.snapshot(revision_sha="deadbeef")
        with self.assertRaisesRegex(ValueError, "unsupported retest policy"):
            self.policy(mode="whatever")
        with self.assertRaisesRegex(ValueError, "within the action budget"):
            self.budget(max_actions=1, actions_used=2)

    def test_output_is_json_serializable_and_contains_no_unprovided_bug(self):
        failure = self.failure(linked_bugs=(self.bug("LU-44", accepted=False),))
        result = self.evaluate(snapshot=self.snapshot(maloo_failures=(failure,)))
        encoded = json.dumps(result.to_dict(), sort_keys=True)
        self.assertIn("LU-44", encoded)
        self.assertNotIn("LU-12345", encoded)
        self.assertEqual(result.decisions[0].reason_code, "investigate_phase_2")


if __name__ == "__main__":
    unittest.main()
