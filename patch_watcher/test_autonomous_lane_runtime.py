import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autonomous_lane import (
    BUILTIN_LANES,
    DETERMINISTIC_RETEST_LANE,
    LaneControlStore,
    LaneDecisionHistory,
    LaneRef,
)
from autonomous_lane_runtime import AutonomousLaneRuntime
from retest_policy import (
    JiraBugLink,
    MalooFailure,
    RetestBudget,
    RetestPolicy,
    RevisionSnapshot,
    evaluate_retests,
)


REVISION = "a" * 40


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.controls = LaneControlStore(root / "lanes.json")
        state = self.controls.set_global_enabled(True, expected_generation=0)
        state = self.controls.set_project_enabled(
            "fs/lustre-release", True, expected_generation=state.generation
        )
        self.controls.set_patch_lane(
            "fs/lustre-release", "68541", LaneRef(DETERMINISTIC_RETEST_LANE, 1),
            True, expected_generation=state.generation,
        )
        self.standing = SimpleNamespace(trigger_mode="automatic", test_failures="deterministic")
        self.runtime = AutonomousLaneRuntime(
            self.controls, LaneDecisionHistory(root / "history.jsonl"),
            standing_policy=lambda _patch: self.standing,
        )
        self.patch = SimpleNamespace(
            project="fs/lustre-release", patch_id="68541",
        )

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return RevisionSnapshot(
            "https://review.whamcloud.com", 68541, 3, REVISION, "open", True,
            True, True, (),
            (MalooFailure(
                "session-1", "review-dne", "sanity", True,
                (JiraBugLink("LU-123", True, "maloo:accepted"),),
                ("101",), True, True, "failure-1",
            ),), (),
        )

    def evaluation(self, snapshot=None):
        snapshot = snapshot or self.snapshot()
        return evaluate_retests(
            snapshot,
            RetestPolicy("automatic", True, "v1", 1),
            RetestBudget(1, 0),
        )

    def test_enrolled_safe_exact_revision_is_admitted_and_audited(self):
        snapshot = self.snapshot()
        admission = self.runtime.evaluate_retest(
            self.patch, snapshot, self.evaluation(snapshot),
            primary_global_enabled=True, actions_used=0,
        )
        self.assertTrue(admission.enrolled)
        self.assertTrue(admission.decision.eligible)
        self.assertEqual(len(self.runtime.history.list()), 1)

    def test_lane_does_not_grant_missing_standing_authority(self):
        self.standing.trigger_mode = "manual"
        snapshot = self.snapshot()
        admission = self.runtime.evaluate_retest(
            self.patch, snapshot, self.evaluation(snapshot),
            primary_global_enabled=True, actions_used=0,
        )
        self.assertEqual(admission.decision.code, "standing_policy_not_authorized")

    def test_unenrolled_patch_preserves_legacy_path(self):
        other = SimpleNamespace(project="fs/lustre-release", patch_id="999")
        snapshot = self.snapshot()
        admission = self.runtime.evaluate_retest(
            other, snapshot, self.evaluation(snapshot),
            primary_global_enabled=True, actions_used=0,
        )
        self.assertFalse(admission.enrolled)
        self.assertIsNone(admission.decision)

    def test_final_authorization_rechecks_kill_switches(self):
        request = {"autonomous_lane": {
            "name": DETERMINISTIC_RETEST_LANE, "version": 1,
            "definition_digest": BUILTIN_LANES[0].definition_digest,
            "capability": "request_retest", "project": "fs/lustre-release",
            "patch_id": "68541",
        }}
        self.assertTrue(self.runtime.authorize_request(
            request, SimpleNamespace(project="fs/lustre-release", patch_id="68541"),
            primary_global_enabled=True, policy_mode="automatic",
        ))
        state = self.controls.load()
        self.controls.set_global_enabled(False, expected_generation=state.generation)
        self.assertFalse(self.runtime.authorize_request(
            request, SimpleNamespace(project="fs/lustre-release", patch_id="68541"),
            primary_global_enabled=True, policy_mode="automatic",
        ))


if __name__ == "__main__":
    unittest.main()
