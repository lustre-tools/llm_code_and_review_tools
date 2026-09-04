import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from standing_policy import (
    ActivePatchRun,
    PatchAutomationPolicy,
    RevisionIdentity,
    StandingPolicyConflict,
    StandingPolicyError,
    StandingPolicyStore,
    TriggerObservation,
    decide_trigger,
    trigger_coalescing_key,
)


REVISION = "a" * 40
NEXT_REVISION = "b" * 40
FINGERPRINT = "c" * 64


def identity(revision=REVISION, patchset=7):
    return RevisionIdentity("68541", 68541, patchset, revision)


def observation(kind="build_failure", revision=REVISION, patchset=7):
    return TriggerObservation(kind, identity(revision, patchset), FINGERPRINT)


class PolicyModelTests(unittest.TestCase):
    def test_defaults_are_inert_and_missing_fields_are_backward_compatible(self):
        policy = PatchAutomationPolicy.from_dict("68541", {})
        self.assertEqual(
            policy.to_dict(),
            {
                "patch_id": "68541",
                "test_failures": "off",
                "build_failures": "off",
                "review_comments": "off",
                "trigger_mode": "manual",
                "version": 0,
            },
        )

    def test_legacy_generic_mode_only_grants_old_retest_capability(self):
        policy = PatchAutomationPolicy.from_dict("68541", {"mode": "automatic"})
        self.assertEqual(policy.test_failures, "deterministic")
        self.assertEqual(policy.trigger_mode, "automatic")
        self.assertEqual(policy.build_failures, "off")
        self.assertEqual(policy.review_comments, "off")

    def test_rejects_unknown_fields_and_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            PatchAutomationPolicy.from_dict("68541", {"surprise": True})
        with self.assertRaisesRegex(ValueError, "unsupported test_failures"):
            PatchAutomationPolicy("68541", test_failures="do-anything")
        with self.assertRaisesRegex(ValueError, "full hexadecimal"):
            RevisionIdentity("68541", 68541, 7, "main")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            TriggerObservation("build_failure", identity(), "short")


class TriggerDecisionTests(unittest.TestCase):
    def test_eligible_decision_is_exact_revision_bound_and_serializable(self):
        policy = PatchAutomationPolicy(
            "68541", build_failures="repair", trigger_mode="automatic", version=3
        )
        decision = decide_trigger(
            policy, observation(), identity(), source="automatic"
        )
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.action, "repair")
        self.assertEqual(decision.policy_version, 3)
        self.assertEqual(decision.to_dict()["identity"]["revision"], REVISION)
        json.dumps(decision.to_dict())

    def test_stale_revision_is_suppressed_before_triggering(self):
        policy = PatchAutomationPolicy(
            "68541", review_comments="all", trigger_mode="automatic"
        )
        decision = decide_trigger(
            policy,
            observation("review_comments", revision=REVISION, patchset=7),
            identity(NEXT_REVISION, 8),
            source="automatic",
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "stale_revision")

    def test_manual_only_off_active_and_duplicate_have_specific_explanations(self):
        automatic_event = observation("test_failure")
        policy = PatchAutomationPolicy("68541", test_failures="investigate")
        manual_only = decide_trigger(
            policy, automatic_event, identity(), source="automatic"
        )
        self.assertEqual(manual_only.code, "manual_only")

        off = decide_trigger(
            PatchAutomationPolicy("68541"), automatic_event, identity(), source="manual"
        )
        self.assertEqual(off.code, "capability_off")

        active = decide_trigger(
            policy,
            automatic_event,
            identity(),
            source="manual",
            active_run=ActivePatchRun("run-1", "68541", "running"),
        )
        self.assertEqual(active.code, "active_run")
        self.assertIn("run-1", active.explanation)

        key = trigger_coalescing_key(policy, automatic_event, source="manual")
        duplicate = decide_trigger(
            policy,
            automatic_event,
            identity(),
            source="manual",
            consumed_keys={key},
        )
        self.assertEqual(duplicate.code, "duplicate")

    def test_coalescing_key_changes_with_revision_event_and_action_not_source(self):
        policy = PatchAutomationPolicy("68541", build_failures="repair")
        base = trigger_coalescing_key(policy, observation(), source="manual")
        self.assertNotEqual(
            base,
            trigger_coalescing_key(
                policy,
                TriggerObservation("build_failure", identity(), "d" * 64),
                source="manual",
            ),
        )
        self.assertNotEqual(
            base,
            trigger_coalescing_key(
                policy, observation(revision=NEXT_REVISION, patchset=8), source="manual"
            ),
        )
        # A manual click and the next automatic observation must coalesce; the
        # trigger source must not cause the exact same event to run twice.
        self.assertEqual(
            base,
            trigger_coalescing_key(policy, observation(), source="automatic"),
        )
        investigate = PatchAutomationPolicy("68541", build_failures="off")
        self.assertNotEqual(
            base,
            trigger_coalescing_key(investigate, observation(), source="manual"),
        )

    def test_different_patch_active_run_does_not_suppress(self):
        policy = PatchAutomationPolicy("68541", build_failures="repair")
        decision = decide_trigger(
            policy,
            observation(),
            identity(),
            source="manual",
            active_run=ActivePatchRun("run-other", "70000", "running"),
        )
        self.assertTrue(decision.eligible)


class PolicyStoreTests(unittest.TestCase):
    def test_round_trip_is_private_atomic_and_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "standing-policies.json"
            store = StandingPolicyStore(path)
            initial = store.get("68541")
            saved = store.save(
                dataclasses.replace(
                    initial,
                    test_failures="deterministic",
                    build_failures="repair",
                    review_comments="simple",
                    trigger_mode="automatic",
                )
            )
            self.assertEqual(saved.version, 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.get("68541"), saved)
            self.assertEqual(store.list(), (saved,))
            self.assertFalse(any(path.parent.glob(".standing-policies.json.*")))

    def test_optimistic_concurrency_prevents_lost_update(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StandingPolicyStore(Path(directory) / "policies.json")
            first_reader = store.get("68541")
            second_reader = store.get("68541")
            store.save(dataclasses.replace(first_reader, build_failures="repair"))
            with self.assertRaises(StandingPolicyConflict):
                store.save(dataclasses.replace(second_reader, review_comments="all"))

    def test_loads_legacy_direct_mapping_and_rewrites_current_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            path.write_text(
                json.dumps({"68541": {"mode": "automatic"}}), encoding="utf-8"
            )
            store = StandingPolicyStore(path)
            legacy = store.get("68541")
            saved = store.save(legacy)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], "patch-watcher-standing-policies/v1")
            self.assertEqual(saved.version, 1)

    def test_rejects_corrupt_duplicate_and_future_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            store = StandingPolicyStore(path)
            path.write_text('{"68541":{},"68541":{}}', encoding="utf-8")
            with self.assertRaises(StandingPolicyError):
                store.list()
            path.write_text(
                '{"schema":"patch-watcher-standing-policies/v999","policies":{}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(StandingPolicyError, "unsupported"):
                store.list()

    def test_remove_uses_expected_version_and_missing_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StandingPolicyStore(Path(directory) / "policies.json")
            saved = store.save(PatchAutomationPolicy("68541"))
            with self.assertRaises(StandingPolicyConflict):
                store.remove("68541", expected_version=0)
            self.assertTrue(store.remove("68541", expected_version=saved.version))
            self.assertFalse(store.remove("68541"))


if __name__ == "__main__":
    unittest.main()
