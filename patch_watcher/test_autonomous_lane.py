import dataclasses
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autonomous_lane import (
    BUILTIN_LANES,
    DETERMINISTIC_RETEST_LANE,
    DETERMINISTIC_RETEST_VERSION,
    AutonomousLaneConflict,
    AutonomousLaneError,
    LaneControlSnapshot,
    LaneControlStore,
    LaneDecisionHistory,
    LaneObservation,
    LaneRef,
    NormalizedTestFailure,
    PatchLaneControl,
    ProjectControl,
    RevisionIdentity,
    decide_lane,
    dry_run,
)


REVISION = "a" * 40
NEXT_REVISION = "b" * 40
EVIDENCE = "c" * 64
FAILURE = "d" * 64


def identity(*, revision=REVISION, patchset=3):
    return RevisionIdentity("fs/lustre-release", "68541", 68541, patchset, revision)


def enabled_controls(*, generation=7, patch_enabled=True, lane=None):
    return LaneControlSnapshot(
        generation=generation,
        global_enabled=True,
        projects=(ProjectControl("fs/lustre-release", True),),
        patches=(
            PatchLaneControl(
                "fs/lustre-release",
                "68541",
                lane or LaneRef(DETERMINISTIC_RETEST_LANE, DETERMINISTIC_RETEST_VERSION),
                patch_enabled,
            ),
        ),
    )


def observation(**changes):
    values = {
        "identity": identity(),
        "current_identity": identity(),
        "evidence_kind": "test_failure",
        "evidence_id": "maloo-991",
        "evidence_fingerprint": EVIDENCE,
        "source": "maloo",
        "change_state": "open",
        "failures": (
            NormalizedTestFailure("sanity", "101", FAILURE, "deterministic"),
        ),
        "standing_policy_authorized": True,
        "primary_global_enabled": True,
        "base_evaluation_permits": True,
    }
    values.update(changes)
    return LaneObservation(**values)


class LaneModelTests(unittest.TestCase):
    def test_builtin_lane_is_named_versioned_and_has_tight_budget(self):
        self.assertEqual(len(BUILTIN_LANES), 1)
        lane = BUILTIN_LANES[0]
        self.assertEqual(lane.ref, LaneRef("deterministic-test-retest", 1))
        self.assertEqual(lane.capabilities, ("request_retest",))
        self.assertEqual(lane.budgets.actions, 1)
        self.assertEqual(lane.budgets.remote_writes, 1)
        self.assertEqual(lane.budgets.agent_runs, 0)
        self.assertTrue(lane.definition_digest.startswith("sha256:"))

    def test_every_default_switch_is_off(self):
        controls = LaneControlSnapshot()
        self.assertFalse(controls.global_enabled)
        decision = decide_lane(observation(), controls)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "global_disabled")
        self.assertEqual(decision.actions, ())

    def test_eligible_decision_is_exact_explainable_and_budgeted(self):
        decision = decide_lane(observation(), enabled_controls())
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.code, "eligible")
        self.assertEqual(decision.actions, ("request_retest",))
        self.assertEqual(decision.capabilities, ("request_retest",))
        self.assertEqual(decision.control_generation, 7)
        self.assertEqual(decision.definition_digest, BUILTIN_LANES[0].definition_digest)
        self.assertEqual(decision.identity.revision, REVISION)
        self.assertEqual(decision.evidence_fingerprint, "sha256:" + EVIDENCE)
        self.assertTrue(decision.decision_key.startswith("lane:"))
        self.assertIn("exact open revision", decision.explanation)
        json.dumps(decision.to_dict())

    def test_each_kill_switch_has_a_specific_rejection(self):
        global_off = dataclasses.replace(enabled_controls(), global_enabled=False)
        self.assertEqual(decide_lane(observation(), global_off).code, "global_disabled")

        project_off = dataclasses.replace(
            enabled_controls(), projects=(ProjectControl("fs/lustre-release", False),)
        )
        self.assertEqual(decide_lane(observation(), project_off).code, "project_disabled")

        no_patch = dataclasses.replace(enabled_controls(), patches=())
        self.assertEqual(decide_lane(observation(), no_patch).code, "patch_not_enrolled")

        patch_off = enabled_controls(patch_enabled=False)
        self.assertEqual(decide_lane(observation(), patch_off).code, "patch_disabled")

    def test_revision_review_and_lifecycle_guards_precede_classification(self):
        stale = observation(current_identity=identity(revision=NEXT_REVISION, patchset=4))
        self.assertEqual(decide_lane(stale, enabled_controls()).code, "stale_revision")

        closed = observation(change_state="merged")
        self.assertEqual(decide_lane(closed, enabled_controls()).code, "change_not_open")

        reviewed = observation(non_maloo_minus_one=True)
        self.assertEqual(decide_lane(reviewed, enabled_controls()).code, "human_review_required")

    def test_lane_enrollment_cannot_grant_primary_authority(self):
        standing_off = observation(standing_policy_authorized=False)
        self.assertEqual(
            decide_lane(standing_off, enabled_controls()).code,
            "standing_policy_not_authorized",
        )
        primary_off = observation(primary_global_enabled=False)
        self.assertEqual(
            decide_lane(primary_off, enabled_controls()).code,
            "primary_global_disabled",
        )

    def test_base_evaluator_and_per_revision_budget_remain_authoritative(self):
        unsafe = observation(base_evaluation_permits=False)
        self.assertEqual(decide_lane(unsafe, enabled_controls()).code, "base_policy_rejected")
        spent = observation(actions_used=1)
        self.assertEqual(decide_lane(spent, enabled_controls()).code, "action_budget_exhausted")

    def test_only_all_deterministic_maloo_failures_are_admitted(self):
        unknown = observation(
            failures=(NormalizedTestFailure("sanity", "101", FAILURE, "unknown"),)
        )
        rejected = decide_lane(unknown, enabled_controls())
        self.assertEqual(rejected.code, "not_deterministic")
        self.assertIn("sanity/101", rejected.explanation)

        no_failures = observation(failures=())
        self.assertEqual(decide_lane(no_failures, enabled_controls()).code, "no_test_failures")

        wrong_kind = observation(evidence_kind="build_failure")
        self.assertEqual(decide_lane(wrong_kind, enabled_controls()).code, "wrong_evidence_kind")

        wrong_source = observation(source="jenkins")
        self.assertEqual(
            decide_lane(wrong_source, enabled_controls()).code, "wrong_evidence_source"
        )

    def test_active_run_and_exact_consumed_key_coalesce_work(self):
        busy = observation(active_run_id="run-123")
        self.assertEqual(decide_lane(busy, enabled_controls()).code, "active_run")

        first = decide_lane(observation(), enabled_controls())
        consumed = observation(consumed_keys=(first.decision_key,))
        self.assertEqual(decide_lane(consumed, enabled_controls()).code, "already_consumed")

        changed_evidence = observation(
            evidence_id="maloo-992", evidence_fingerprint="e" * 64,
            consumed_keys=(first.decision_key,),
        )
        self.assertTrue(decide_lane(changed_evidence, enabled_controls()).eligible)

    def test_unknown_lane_version_is_rejected_without_falling_forward(self):
        controls = enabled_controls(lane=LaneRef(DETERMINISTIC_RETEST_LANE, 2))
        decision = decide_lane(observation(), controls)
        self.assertEqual(decision.code, "lane_unavailable")
        self.assertIn("version 2", decision.explanation)

    def test_normalization_makes_failure_order_irrelevant(self):
        failure_a = NormalizedTestFailure("sanity", "1", "1" * 64, "deterministic")
        failure_b = NormalizedTestFailure("sanity", "2", "2" * 64, "deterministic")
        left = observation(failures=(failure_b, failure_a))
        right = observation(failures=(failure_a, failure_b))
        self.assertEqual(left, right)
        self.assertEqual(
            decide_lane(left, enabled_controls()), decide_lane(right, enabled_controls())
        )

    def test_invalid_normalized_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "full hexadecimal"):
            RevisionIdentity("fs/lustre-release", "68541", 68541, 3, "main")
        with self.assertRaisesRegex(ValueError, "same change"):
            observation(
                current_identity=RevisionIdentity(
                    "other/project", "68541", 68541, 3, REVISION
                )
            )
        with self.assertRaisesRegex(ValueError, "classification"):
            NormalizedTestFailure("sanity", "1", FAILURE, "flaky-ish")


class LaneControlStoreTests(unittest.TestCase):
    def test_round_trip_is_atomic_private_and_restart_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "lanes.json"
            store = LaneControlStore(path)
            self.assertEqual(store.load(), LaneControlSnapshot())

            state = store.set_global_enabled(True, expected_generation=0)
            state = store.set_project_enabled(
                "fs/lustre-release", True, expected_generation=state.generation
            )
            state = store.set_patch_lane(
                "fs/lustre-release",
                "68541",
                LaneRef(DETERMINISTIC_RETEST_LANE, 1),
                True,
                expected_generation=state.generation,
            )
            self.assertEqual(state.generation, 3)
            self.assertEqual(LaneControlStore(path).load(), state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o077, 0)
            self.assertFalse(any(path.parent.glob(".lanes.json.*")))

    def test_optimistic_concurrency_rejects_lost_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LaneControlStore(Path(directory) / "lanes.json")
            stale = store.load()
            store.set_global_enabled(True, expected_generation=stale.generation)
            with self.assertRaises(AutonomousLaneConflict):
                store.save(dataclasses.replace(stale, global_enabled=True))
            with self.assertRaises(AutonomousLaneConflict):
                store.set_project_enabled(
                    "fs/lustre-release", True, expected_generation=stale.generation
                )

    def test_corrupt_duplicate_and_future_documents_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lanes.json"
            path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
            with self.assertRaises(AutonomousLaneError):
                LaneControlStore(path).load()
            path.write_text(
                '{"schema":"patch-watcher-autonomous-lanes/v999",'
                '"generation":0,"global_enabled":false,"projects":[],"patches":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AutonomousLaneError, "unsupported"):
                LaneControlStore(path).load()

            path.write_text(
                '{"schema":"patch-watcher-autonomous-lanes/v1",'
                '"generation":0,"global_enabled":false,"projects":[],"patches":[3]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AutonomousLaneError, "patch control"):
                LaneControlStore(path).load()


class DecisionAuditTests(unittest.TestCase):
    def test_append_is_private_and_replays_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "lane-decisions.jsonl"
            controls = enabled_controls()
            item = observation()
            decision = decide_lane(item, controls)
            history = LaneDecisionHistory(path)
            record = history.append(
                item,
                controls,
                decision,
                recorded_at=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
            )
            self.assertTrue(record.record_id.startswith("lane-audit:"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reloaded = LaneDecisionHistory(path)
            self.assertEqual(reloaded.list(), (record,))
            replay = reloaded.replay()
            self.assertEqual(len(replay), 1)
            self.assertTrue(replay[0].matched)
            self.assertEqual(replay[0].recorded, replay[0].replayed)

    def test_append_rejects_a_decision_from_different_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            history = LaneDecisionHistory(Path(directory) / "history.jsonl")
            controls = enabled_controls()
            with self.assertRaisesRegex(ValueError, "does not match"):
                history.append(
                    observation(),
                    controls,
                    decide_lane(observation(change_state="merged"), controls),
                )

    def test_repeated_identical_decision_is_stored_once(self):
        with tempfile.TemporaryDirectory() as directory:
            history = LaneDecisionHistory(Path(directory) / "history.jsonl")
            controls = enabled_controls()
            item = observation()
            decision = decide_lane(item, controls)
            first = history.append(item, controls, decision)
            second = history.append(item, controls, decision)
            self.assertEqual(first.record_id, second.record_id)
            self.assertEqual(len(history.list()), 1)

    def test_tampered_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            controls = enabled_controls()
            item = observation()
            LaneDecisionHistory(path).append(item, controls, decide_lane(item, controls))
            document = json.loads(path.read_text(encoding="utf-8"))
            document["decision"]["eligible"] = False
            path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AutonomousLaneError, "does not replay"):
                LaneDecisionHistory(path).list()

    def test_dry_run_is_deterministic_and_never_creates_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            controls = enabled_controls()
            items = (observation(), observation(change_state="abandoned"))
            first = dry_run(items, controls)
            second = dry_run(items, controls)
            self.assertEqual(first, second)
            self.assertEqual([item.code for item in first], ["eligible", "change_not_open"])
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
