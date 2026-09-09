import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from patch_watcher.automation_state import AutomationStateStore
from patch_watcher.failure_actions import (
    LINK_ACTION,
    RETEST_ACTION,
    FailureActionController,
    FailureActionError,
    FailurePatchRevision,
    _canonical_digest,
)
from patch_watcher.maloo_adapter import (
    MalooAdapterError,
    MalooBugLink,
    MalooBugLinks,
    MalooErrorCode,
    MalooLinkBugResult,
    MalooRetestReconciliation,
    MalooRetestResult,
)

PATCH_ID = "68160"
REVISION = "7b77eeb0190d6d93880951533c2e1d1145780375"
SESSION = "11111111-2222-3333-4444-555555555555"
SUITE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SUITE_NAME = "sanity"
TICKET = "LU-19487"


class FakeMaloo:
    def __init__(self):
        self.link_state = "absent"
        # The real adapter returns ``linked=False`` when Maloo answers but
        # declines the association (it reads the envelope's ``success`` field),
        # so the fake has to be able to say that too.
        self.link_accepted = True
        # Likewise, ``request_retest`` returns ``requested=False`` when Maloo
        # answers but refuses the retest outright.
        self.retest_accepted = True
        self.retest_observed = False
        self.link_calls = []
        self.retest_calls = []
        self.link_error = None
        self.retest_error = None
        self.link_entered = None
        self.link_release = None
        self.retest_entered = None
        self.retest_release = None
        self.enforced_session_id = SESSION
        self.enforced_test_group = "review-dne-part-1"
        self.enforced_suite_id = SUITE
        self.enforced_suite_name = SUITE_NAME
        self.failure_target_present = True
        self.enforced_failure_calls = []

    def get_enforced_failures(self, change_number, patchset):
        self.enforced_failure_calls.append((change_number, patchset))
        if not self.failure_target_present:
            return ()
        return (
            SimpleNamespace(
                session=SimpleNamespace(
                    session_id=self.enforced_session_id,
                    test_group=self.enforced_test_group,
                ),
                failures=SimpleNamespace(
                    failed_suites=(
                        SimpleNamespace(
                            suite_id=self.enforced_suite_id,
                            suite=self.enforced_suite_name,
                        ),
                    )
                ),
            ),
        )

    def get_bug_links(self, suite_id, related=False):
        links = ()
        if self.link_state != "absent":
            links = (MalooBugLink(TICKET, self.link_state, suite_id),)
        return MalooBugLinks(suite_id, links)

    def link_bug(
        self, suite_id, jira_ticket, *, buggable_class="TestSet", state="accepted"
    ):
        self.link_calls.append(
            (suite_id, jira_ticket, buggable_class, state)
        )
        if self.link_entered is not None:
            self.link_entered.set()
        if self.link_release is not None:
            self.link_release.wait(2)
        if self.link_error is not None:
            raise self.link_error
        return MalooLinkBugResult(
            suite_id,
            jira_ticket,
            buggable_class,
            state,
            self.link_accepted,
            "OK" if self.link_accepted else "refused",
        )

    def reconcile_remote_retest(self, **request):
        return MalooRetestReconciliation(
            request["session_ref"],
            self.retest_observed,
            True if self.retest_observed else None,
            request.get("jira_ticket", ""),
            ("fake",) if self.retest_observed else (),
        )

    def request_retest(self, session_id, jira_ticket, *, option="single"):
        self.retest_calls.append((session_id, jira_ticket, option))
        if self.retest_entered is not None:
            self.retest_entered.set()
        if self.retest_release is not None:
            self.retest_release.wait(2)
        if self.retest_error is not None:
            raise self.retest_error
        return MalooRetestResult(
            session_id,
            jira_ticket,
            option,
            self.retest_accepted,
            "queued" if self.retest_accepted else "refused",
        )


class FailureActionControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "automation.sqlite3"
        self.store = AutomationStateStore(self.database)
        self.store.upsert_patch(
            PATCH_ID,
            gerrit_url="https://review.whamcloud.com/c/fs/lustre-release/+/68160",
            change_number=68160,
            revision=REVISION,
            patchset=13,
        )
        self.store.set_policy(
            PATCH_ID,
            mode="approval",
            action_budget=2,
            delivery_budget=0,
            updated_by="patrick",
        )
        self.maloo = FakeMaloo()
        self.fresh = FailurePatchRevision(
            patch_id=PATCH_ID,
            gerrit_url="https://review.whamcloud.com/c/fs/lustre-release/+/68160",
            change_number=68160,
            patchset_number=13,
            revision_sha=REVISION,
        )
        self.controller = FailureActionController(
            self.store,
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="controller-1",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def plan(self, controller=None):
        return (controller or self.controller).plan_link_existing_bug(
            PATCH_ID,
            expected_revision=REVISION,
            expected_patchset=13,
            session_id=SESSION,
            test_group="review-dne-part-1",
            suite_id=SUITE,
            suite_name=SUITE_NAME,
            jira_ticket=TICKET,
        )

    def approve(self, action_id, controller=None):
        return (controller or self.controller).approve_action(
            action_id,
            approved_by="patrick",
            expected_revision=REVISION,
        )

    def accept_link_and_get_retest(self, plan):
        self.approve(plan.link_action.action_id)
        self.controller.advance(plan.run.run_id)
        self.maloo.link_state = "accepted"
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "waiting_approval")
        return next(
            action
            for action in self.store.list_actions(plan.run.run_id)
            if action.action_type == RETEST_ACTION
        )

    def test_planning_is_idempotent_inert_and_defaults_to_no_execution(self):
        plan = self.plan()
        repeated = self.plan()
        self.assertEqual(repeated.run.run_id, plan.run.run_id)
        self.assertEqual(repeated.link_action.action_id, plan.link_action.action_id)
        self.assertEqual(self.maloo.link_calls, [])
        self.assertEqual(self.maloo.retest_calls, [])
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "waiting_approval")
        self.assertEqual(result.action_id, plan.link_action.action_id)
        self.assertEqual(self.store.get_action(plan.link_action.action_id).status, "planned")

    def test_link_then_separate_retest_approval_and_remote_observation(self):
        plan = self.plan()
        self.assertEqual(plan.link_action.request["suite_name"], SUITE_NAME)
        retest = self.accept_link_and_get_retest(plan)
        self.assertEqual(retest.request["suite_name"], SUITE_NAME)
        self.assertEqual(len(self.maloo.link_calls), 1)
        self.assertEqual(self.maloo.retest_calls, [])
        self.assertIsNone(self.store.get_action_approval(retest.action_id))

        self.approve(retest.action_id)
        requested = self.controller.advance(plan.run.run_id)
        self.assertEqual(requested.stage, "retest_requested")
        self.assertEqual(
            self.maloo.retest_calls,
            [(SESSION, TICKET, "single")],
        )
        self.maloo.retest_observed = True
        complete = self.controller.advance(plan.run.run_id)
        self.assertEqual(complete.stage, "complete")
        self.assertEqual(self.store.get_run(plan.run.run_id).status, "succeeded")
        self.assertEqual(len(self.maloo.retest_calls), 1)
        self.assertEqual(
            self.maloo.enforced_failure_calls,
            [(68160, 13), (68160, 13)],
        )

    def test_link_is_suppressed_when_exact_failure_target_is_stale(self):
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        self.maloo.failure_target_present = False
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "stale")
        self.assertEqual(result.run_status, "stale")
        self.assertEqual(self.maloo.link_calls, [])
        action = self.store.get_action(plan.link_action.action_id)
        self.assertEqual(action.status, "cancelled")
        self.assertEqual(action.failure_code, "maloo_failure_target_changed")

    def test_retest_is_suppressed_when_enforced_failure_tuple_changes(self):
        plan = self.plan()
        retest = self.accept_link_and_get_retest(plan)
        self.approve(retest.action_id)
        self.maloo.enforced_test_group = "review-dne-part-2"
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "stale")
        self.assertEqual(result.run_status, "stale")
        self.assertEqual(self.maloo.retest_calls, [])
        action = self.store.get_action(retest.action_id)
        self.assertEqual(action.status, "cancelled")
        self.assertEqual(action.failure_code, "maloo_failure_target_changed")

    def test_existing_accepted_link_skips_link_write_but_still_requires_retest_approval(self):
        self.maloo.link_state = "accepted"
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "waiting_approval")
        self.assertEqual(self.maloo.link_calls, [])
        self.assertEqual(self.maloo.retest_calls, [])

    def test_maloo_declining_the_link_fails_the_run_rather_than_waiting(self):
        """A declined link must settle as failed, not sit in waiting_external.

        `link_bug` returns `linked=False` when Maloo answers but refuses the
        association -- the adapter reads that straight off the envelope's
        `success` field.  Nothing further will ever arrive for that action, so
        recording it as `waiting_external` parks the whole two-write workflow
        for good: the failure is never bugged, the retest that depends on an
        accepted association is never planned, and no failure is reported to
        anyone.  The retest path already refuses a declined write this way.
        """
        self.maloo.link_accepted = False
        plan = self.plan()
        self.approve(plan.link_action.action_id)

        result = self.controller.advance(plan.run.run_id)

        self.assertEqual(len(self.maloo.link_calls), 1)
        self.assertEqual(result.stage, "failed")
        self.assertEqual(result.run_status, "failed")
        action = self.store.get_action(plan.link_action.action_id)
        self.assertEqual(action.status, "failed")
        self.assertEqual(action.failure_code, "link_rejected")
        self.assertEqual(self.store.get_run(plan.run.run_id).status, "failed")
        self.assertEqual(self.maloo.retest_calls, [])

    def test_pending_link_is_observed_without_duplicate_write(self):
        self.maloo.link_state = "pending"
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "link_pending")
        self.assertEqual(self.maloo.link_calls, [])
        self.assertEqual(
            self.store.get_action(plan.link_action.action_id).status,
            "waiting_external",
        )

    def test_stale_gerrit_revision_cancels_before_any_mutation(self):
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        self.fresh = FailurePatchRevision(
            patch_id=PATCH_ID,
            gerrit_url=self.fresh.gerrit_url,
            change_number=68160,
            patchset_number=14,
            revision_sha="different",
        )
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "stale")
        self.assertEqual(self.maloo.link_calls, [])
        self.assertEqual(self.maloo.retest_calls, [])
        self.assertEqual(self.store.get_run(plan.run.run_id).status, "stale")

    def test_policy_change_after_approval_cancels_before_any_mutation(self):
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        self.store.set_policy(
            PATCH_ID,
            mode="approval",
            action_budget=3,
            delivery_budget=0,
            updated_by="patrick",
        )
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "authority_changed")
        self.assertEqual(result.run_status, "cancelled")
        self.assertEqual(self.maloo.link_calls, [])
        self.assertEqual(self.maloo.retest_calls, [])
        action = self.store.get_action(plan.link_action.action_id)
        self.assertEqual(action.status, "cancelled")
        self.assertEqual(action.failure_code, "authority_changed")

    def test_ambiguous_link_is_never_blindly_retried_after_restart(self):
        self.maloo.link_error = MalooAdapterError(
            MalooErrorCode.AMBIGUOUS_MUTATION,
            "link-bug",
            "timeout",
            ambiguous=True,
        )
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "ambiguous")
        self.assertEqual(len(self.maloo.link_calls), 1)

        restarted = FailureActionController(
            AutomationStateStore(self.database),
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="controller-restarted",
        )
        self.assertEqual(restarted.advance(plan.run.run_id).stage, "terminal")
        self.assertEqual(len(self.maloo.link_calls), 1)

    def test_restart_reconciles_executing_link_without_reissuing(self):
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        self.store.claim_run(plan.run.run_id, "dead-controller")
        claimed = self.store.claim_next_action(plan.run.run_id, "dead-controller")
        self.assertEqual(claimed.action_type, LINK_ACTION)
        self.maloo.link_state = "accepted"

        restarted = FailureActionController(
            AutomationStateStore(self.database),
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="replacement-controller",
            reconcile_orphans=True,
        )
        result = restarted.advance(plan.run.run_id)
        self.assertEqual(result.stage, "waiting_approval")
        self.assertEqual(self.maloo.link_calls, [])

    def test_restart_with_unobservable_executing_link_becomes_ambiguous(self):
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        self.store.claim_run(plan.run.run_id, "dead-controller")
        self.store.claim_next_action(plan.run.run_id, "dead-controller")
        restarted = FailureActionController(
            AutomationStateStore(self.database),
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="replacement-controller",
            reconcile_orphans=True,
        )
        result = restarted.advance(plan.run.run_id)
        self.assertEqual(result.stage, "ambiguous")
        self.assertEqual(self.maloo.link_calls, [])

    def test_restart_reconciles_observed_executing_retest_without_reissuing(self):
        plan = self.plan()
        retest = self.accept_link_and_get_retest(plan)
        self.approve(retest.action_id)
        claimed = self.store.claim_next_action(plan.run.run_id, "dead-controller")
        self.assertEqual(claimed.action_id, retest.action_id)
        self.maloo.retest_observed = True

        restarted = FailureActionController(
            AutomationStateStore(self.database),
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="replacement-controller",
            reconcile_orphans=True,
        )
        result = restarted.advance(plan.run.run_id)
        self.assertEqual(result.stage, "complete")
        self.assertEqual(result.run_status, "succeeded")
        self.assertEqual(self.maloo.retest_calls, [])

    def test_restart_with_unobservable_executing_retest_becomes_ambiguous(self):
        plan = self.plan()
        retest = self.accept_link_and_get_retest(plan)
        self.approve(retest.action_id)
        claimed = self.store.claim_next_action(plan.run.run_id, "dead-controller")
        self.assertEqual(claimed.action_id, retest.action_id)

        restarted = FailureActionController(
            AutomationStateStore(self.database),
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="replacement-controller",
            reconcile_orphans=True,
        )
        result = restarted.advance(plan.run.run_id)
        self.assertEqual(result.stage, "ambiguous")
        self.assertEqual(result.run_status, "ambiguous")
        self.assertEqual(self.maloo.retest_calls, [])

    def test_maloo_declining_the_retest_fails_the_run_rather_than_waiting(self):
        """A declined retest must settle as failed, not sit in waiting_external.

        `request_retest` returns `requested=False` when Maloo answers but
        refuses the request.  This is the second write of the two-write
        workflow, so parking it in `waiting_external` is the worst place to
        stall: the bug link has already been written remotely, the operator has
        already spent both approvals, and nothing will ever arrive to move the
        run on.  `RetestController` has always refused a declined retest this
        way; this path silently did not.
        """
        self.maloo.retest_accepted = False
        plan = self.plan()
        retest = self.accept_link_and_get_retest(plan)
        self.approve(retest.action_id)

        result = self.controller.advance(plan.run.run_id)

        self.assertEqual(len(self.maloo.retest_calls), 1)
        self.assertEqual(result.stage, "failed")
        self.assertEqual(result.run_status, "failed")
        action = self.store.get_action(retest.action_id)
        self.assertEqual(action.status, "failed")
        self.assertEqual(action.failure_code, "retest_rejected")
        self.assertEqual(self.store.get_run(plan.run.run_id).status, "failed")

    def test_retest_is_suppressed_if_association_is_no_longer_accepted(self):
        plan = self.plan()
        retest = self.accept_link_and_get_retest(plan)
        self.approve(retest.action_id)
        self.maloo.link_state = "pending"
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "association_not_accepted")
        self.assertEqual(self.maloo.retest_calls, [])

    def test_ambiguous_retest_is_at_most_once(self):
        plan = self.plan()
        retest = self.accept_link_and_get_retest(plan)
        self.approve(retest.action_id)
        self.maloo.retest_error = MalooAdapterError(
            MalooErrorCode.AMBIGUOUS_MUTATION,
            "retest",
            "timeout",
            ambiguous=True,
        )
        result = self.controller.advance(plan.run.run_id)
        self.assertEqual(result.stage, "ambiguous")
        self.assertEqual(len(self.maloo.retest_calls), 1)
        self.assertEqual(self.controller.advance(plan.run.run_id).stage, "terminal")
        self.assertEqual(len(self.maloo.retest_calls), 1)

    def test_two_controllers_cannot_issue_duplicate_link_write(self):
        plan = self.plan()
        self.approve(plan.link_action.action_id)
        self.maloo.link_entered = threading.Event()
        self.maloo.link_release = threading.Event()
        second = FailureActionController(
            AutomationStateStore(self.database),
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="controller-2",
        )
        results = []

        thread = threading.Thread(
            target=lambda: results.append(self.controller.advance(plan.run.run_id))
        )
        thread.start()
        self.assertTrue(self.maloo.link_entered.wait(2))
        results.append(second.advance(plan.run.run_id))
        self.maloo.link_release.set()
        thread.join()
        self.assertEqual(len(self.maloo.link_calls), 1)
        self.assertIn("claimed_elsewhere", {result.stage for result in results})

    def test_two_controllers_cannot_issue_duplicate_retest_write(self):
        plan = self.plan()
        retest = self.accept_link_and_get_retest(plan)
        self.approve(retest.action_id)
        self.maloo.retest_entered = threading.Event()
        self.maloo.retest_release = threading.Event()
        second = FailureActionController(
            AutomationStateStore(self.database),
            self.maloo,
            revalidate=lambda _url: self.fresh,
            worker_id="controller-2",
        )
        results = []

        thread = threading.Thread(
            target=lambda: results.append(self.controller.advance(plan.run.run_id))
        )
        thread.start()
        self.assertTrue(self.maloo.retest_entered.wait(2))
        results.append(second.advance(plan.run.run_id))
        self.maloo.retest_release.set()
        thread.join()
        self.assertEqual(len(self.maloo.retest_calls), 1)
        self.assertIn("claimed_elsewhere", {result.stage for result in results})

    def test_insufficient_budget_is_rejected_before_planning_or_writing(self):
        self.store.set_policy(
            PATCH_ID,
            mode="approval",
            action_budget=1,
            delivery_budget=0,
            updated_by="patrick",
        )
        with self.assertRaisesRegex(FailureActionError, "at least two"):
            self.plan()
        self.assertEqual(self.store.list_runs(patch_id=PATCH_ID), [])
        self.assertEqual(self.maloo.link_calls, [])
        self.assertEqual(self.maloo.retest_calls, [])


class WorkflowFingerprintTests(unittest.TestCase):
    """The fingerprint that keys a run and an idempotency key must be stable."""

    def payload(self):
        return {
            "patch_id": PATCH_ID,
            "change_number": 68160,
            "revision_sha": REVISION,
            "patchset": 13,
            "session_id": SESSION,
            "test_group": "review-dne-part-1",
            "suite_id": SUITE,
            "suite_name": SUITE_NAME,
            "jira_ticket": TICKET,
        }

    def test_fingerprint_is_independent_of_payload_key_order(self):
        """The same association must fingerprint identically however it is built.

        This digest is both the deterministic run key and the
        `maloo-link-bug:` idempotency key, so it is the only thing stopping a
        replanned action from re-issuing a remote write that already happened.
        It survives reordering today only because the payload literal happens
        to be written in one fixed order; the moment somebody reorders those
        keys -- an entirely content-preserving edit -- the same logical action
        mints a new fingerprint, plans a second run, and writes the bug link to
        Maloo twice.
        """
        forward = self.payload()
        backward = dict(reversed(list(forward.items())))
        self.assertNotEqual(list(forward), list(backward))
        self.assertEqual(forward, backward)

        self.assertEqual(
            _canonical_digest("failure-write/v1", forward),
            _canonical_digest("failure-write/v1", backward),
        )

    def test_fingerprint_still_separates_different_content(self):
        """Order independence must not come from ignoring the payload."""
        base = self.payload()
        other_suite = {**base, "suite_id": "99999999-8888-7777-6666-555555555555"}
        digests = {
            _canonical_digest("failure-write/v1", base),
            _canonical_digest("failure-write/v1", other_suite),
            _canonical_digest("failure-write/v2", base),
        }
        self.assertEqual(len(digests), 3)


if __name__ == "__main__":
    unittest.main()
