import dataclasses
import errno
import fcntl
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from patch_watcher import standing_policy
from patch_watcher.standing_policy import (
    PRESET_LEVELS,
    ActivePatchRun,
    PatchAutomationPolicy,
    RevisionIdentity,
    StandingPolicyConflict,
    StandingPolicyError,
    StandingPolicyStore,
    TriggerObservation,
    decide_trigger,
    is_standing_trigger_key,
    preset_for,
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
                "preset": "watch",
                "test_failures": "off",
                "build_failures": "off",
                "review_comments": "off",
                "trigger_mode": "manual",
                "design_audit": True,
                "version": 0,
            },
        )

    def test_each_level_is_a_strict_superset_of_the_one_below(self):
        """The whole point of the ladder: choosing higher never removes anything."""
        order = {"off": 0, "deterministic": 1, "investigate": 2,
                 "repair": 1, "simple": 1, "bots": 2, "all": 3,
                 "manual": 0, "automatic": 1}
        previous = None
        for level in PRESET_LEVELS:
            current = PatchAutomationPolicy.for_preset("68541", level)
            self.assertEqual(current.preset, level)
            if previous is not None:
                for field in ("test_failures", "build_failures", "review_comments", "trigger_mode"):
                    self.assertGreaterEqual(
                        order[getattr(current, field)], order[getattr(previous, field)],
                        f"{level} lost {field} relative to {previous.preset}",
                    )
                # The audit gate only ever opens on the way up, at the top.
                self.assertTrue(previous.design_audit or not current.design_audit)
            previous = current
        self.assertFalse(PatchAutomationPolicy.for_preset("68541", "own").design_audit)
        self.assertTrue(PatchAutomationPolicy.for_preset("68541", "all").design_audit)

    def test_a_named_level_overrides_a_stale_triple(self):
        policy = PatchAutomationPolicy.from_dict("68541", {
            "preset": "bots", "test_failures": "off", "build_failures": "off",
            "review_comments": "off", "trigger_mode": "manual",
        })
        self.assertEqual(policy.preset, "bots")
        self.assertEqual(policy.test_failures, "investigate")
        self.assertEqual(policy.build_failures, "repair")
        self.assertEqual(policy.review_comments, "bots")
        self.assertEqual(policy.trigger_mode, "automatic")

    def test_a_triple_from_the_old_form_names_its_level_or_custom(self):
        self.assertEqual(preset_for("deterministic", "off", "off", "automatic"), "retest")
        self.assertEqual(preset_for("investigate", "repair", "all", "automatic"), "all")
        self.assertEqual(preset_for("investigate", "repair", "all", "automatic", False), "own")
        # deterministic retests plus build repair matches no rung.
        self.assertEqual(preset_for("deterministic", "repair", "off", "automatic"), "custom")
        custom = PatchAutomationPolicy("68541", test_failures="deterministic",
                                       build_failures="repair", trigger_mode="automatic")
        self.assertEqual(custom.preset, "custom")
        # The store re-derives a policy through dataclasses.replace; "custom"
        # must survive that rather than be rejected as an unknown level.
        bumped = dataclasses.replace(custom, version=3)
        self.assertEqual(bumped.preset, "custom")
        self.assertEqual(bumped.build_failures, "repair")
        with self.assertRaisesRegex(ValueError, "preset must be one of"):
            PatchAutomationPolicy("68541", preset="everything")
        with self.assertRaisesRegex(ValueError, "preset must be one of"):
            PatchAutomationPolicy.from_dict("68541", {"preset": "custom-ish"})

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

    def test_coalescing_key_is_recognisable_as_an_automatic_trigger(self):
        """The key doubles as the run's request identity.

        That is the only durable mark separating a run the standing policy
        caused from one an operator started by hand, so a controller bounding
        automation can read it instead of trusting a second, forgeable flag.
        """

        policy = PatchAutomationPolicy("68541", build_failures="repair")
        key = trigger_coalescing_key(policy, observation(), source="automatic")
        self.assertTrue(is_standing_trigger_key(key))
        self.assertFalse(is_standing_trigger_key("f47ac10b-58cc-4372-a567-0e02b2c3d479"))
        self.assertFalse(is_standing_trigger_key(None))

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


class LockDescriptorTests(unittest.TestCase):
    """A failed lock acquire must not cost a file descriptor.

    ``_Lock.__enter__`` opened the lock file and then did two things that can
    fail -- a chmod and the flock itself.  An exception out of ``__enter__``
    means ``__exit__``, which holds the close, never runs, so each failure
    burned one descriptor.  Every dashboard render and every policy POST takes
    this lock, so the store is the wrong place to lose descriptors one at a
    time.
    """

    class FailingFcntl:
        """Real ``fcntl`` for everything except the call under test."""

        def __getattr__(self, name):
            return getattr(fcntl, name)

        def flock(self, _descriptor, _operation):
            raise OSError(errno.ENOLCK, "no locks available")

    def test_a_failed_acquire_leaks_no_descriptor(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("descriptor accounting needs /proc")
        attempts = 200
        with tempfile.TemporaryDirectory() as directory:
            store = StandingPolicyStore(Path(directory) / "policies.json")
            store.list()  # create the lock file so only the flock can fail
            # Count descriptors naming this store's lock file rather than the
            # whole table: the rest of the suite opens and closes files while
            # this runs, and a total is noise around the number we care about.
            self.assertEqual(_open_count(store.lock_path), 0)
            with unittest.mock.patch.object(
                standing_policy, "fcntl", self.FailingFcntl()
            ):
                for _ in range(attempts):
                    with self.assertRaises(OSError):
                        store.list()
            leaked = _open_count(store.lock_path)
            self.assertEqual(
                leaked, 0, f"leaked {leaked / attempts:.2f} descriptors per failed acquire"
            )

    def test_a_successful_acquire_still_locks_and_releases(self):
        # The fix must not simply stop taking the lock: the descriptor has to
        # survive __enter__ and be released by __exit__.
        with tempfile.TemporaryDirectory() as directory:
            store = StandingPolicyStore(Path(directory) / "policies.json")
            with store._locked(exclusive=True) as lock:
                self.assertGreaterEqual(lock.fd, 0)
                probe = os.open(store.lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(OSError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
            probe = os.open(store.lock_path, os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            finally:
                os.close(probe)


def _open_count(path):
    """How many of this process's descriptors currently name ``path``."""
    total = 0
    for entry in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue  # the descriptor closed while we were walking
        if target == str(path) or target == f"{path} (deleted)":
            total += 1
    return total


if __name__ == "__main__":
    unittest.main()
