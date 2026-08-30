from __future__ import annotations

import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path

from automation_state import AutomationStateStore
from maloo_adapter import (
    MalooAdapterError,
    MalooBugLink,
    MalooBugLinks,
    MalooEnforcedSessionFailure,
    MalooErrorCode,
    MalooFailedSuite,
    MalooFailures,
    MalooQueueEntry,
    MalooQueueEvidence,
    MalooRetestReconciliation,
    MalooRetestResult,
    MalooSession,
    MalooSuiteBugEvidence,
)
from retest_controller import PatchRevision, RetestController
from retest_policy import ReviewVote


SHA = "a" * 40


def patch(*, sha=SHA, patchset=3, current=True):
    return PatchRevision(
        patch_id="review-101",
        gerrit_url="https://review.whamcloud.com/c/fs/lustre-release/+/101",
        gerrit_server="https://review.whamcloud.com",
        change_number=101,
        patchset_number=patchset,
        revision_sha=sha,
        lifecycle="open",
        is_current=current,
    )


def accepted_group():
    session = MalooSession(
        session_id="session-1",
        test_group="review-dne-selinux",
        test_name="dne",
        test_host="host",
        submission="2026-08-30T10:00:00Z",
        enforcing=True,
        passed=4,
        failed=1,
        aborted=0,
        total=5,
        suites=(),
    )
    suite = MalooFailedSuite(
        suite_id="suite-7",
        suite="sanity",
        status="FAIL",
        failed_count=1,
        total_count=1,
        failed_subtests=(),
    )
    failures = MalooFailures("session-1", session.test_group, "dne", (suite,))
    bugs = MalooBugLinks(
        "suite-7", (MalooBugLink("LU-12345", "accepted", "suite-7"),)
    )
    return MalooEnforcedSessionFailure(
        session, failures, (MalooSuiteBugEvidence("suite-7", "sanity", bugs),)
    )


class FakeMaloo:
    def __init__(self):
        self.groups = (accepted_group(),)
        self.queue_entries = ()
        self.reconcile = MalooRetestReconciliation(
            "session-1", False, None, "LU-12345", ()
        )
        self.requests = []
        self.request_error = None
        self.reconcile_barrier = None
        self.reconcile_hook = None
        self.reads = 0
        self.read_error = None

    def get_enforced_failures(self, change_number, patchset):
        self.reads += 1
        assert (change_number, patchset) == (101, 3)
        if self.read_error is not None:
            raise self.read_error
        return self.groups

    def get_queue(self, revision_sha):
        return MalooQueueEvidence(revision_sha, tuple(self.queue_entries))

    def reconcile_remote_retest(self, **kwargs):
        if self.reconcile_barrier is not None:
            self.reconcile_barrier.wait(timeout=3)
        if self.reconcile_hook is not None:
            self.reconcile_hook()
        return self.reconcile

    def request_retest(self, session_ref, jira_ticket, *, option="single"):
        self.requests.append((session_ref, jira_ticket, option))
        if self.request_error is not None:
            raise self.request_error
        return MalooRetestResult(session_ref, jira_ticket, option, True, "queued")


def configured_store(tmp_path, *, mode="automatic", global_enabled=False):
    store = AutomationStateStore(tmp_path / "automation.sqlite3")
    current = patch()
    store.upsert_patch(
        current.patch_id,
        gerrit_url=current.gerrit_url,
        change_number=current.change_number,
        revision=current.revision_sha,
        patchset=current.patchset_number,
    )
    store.set_policy(
        current.patch_id,
        mode=mode,
        action_budget=1,
        delivery_budget=1,
        updated_by="test",
    )
    if global_enabled:
        store.set_global_automation(True, changed_by="test", reason="test")
    return store


def controller(store, maloo, *, fresh=None, notifications=None, worker=None):
    return RetestController(
        store,
        maloo,
        revalidate=lambda _url: fresh or patch(),
        notify=(notifications if notifications is not None else []).append,
        worker_id=worker,
    )


def all_actions(store):
    return [
        action
        for run in store.list_runs(patch_id=patch().patch_id)
        for action in store.list_actions(run.run_id)
    ]


def test_automatic_global_off_is_preview_only(tmp_path):
    store = configured_store(tmp_path, global_enabled=False)
    maloo = FakeMaloo()

    result = controller(store, maloo).tick_patch(patch())

    assert result.evaluation.reason_code == "global_execution_disabled"
    assert maloo.requests == []
    assert all_actions(store) == []
    assert len(store.list_observations(patch().patch_id)) == 1


def test_repeated_identical_maloo_read_failure_notifies_once(tmp_path):
    store = configured_store(tmp_path, mode="advise")
    maloo = FakeMaloo()
    maloo.read_error = MalooAdapterError(
        MalooErrorCode.CONNECTION,
        "review",
        "Maloo is unavailable",
        retryable=True,
    )
    notifications = []
    service = controller(store, maloo, notifications=notifications)

    service.tick_patch(patch())
    service.tick_patch(patch())

    matching = [
        event for event in notifications
        if event.kind == "maloo_observation_failed"
    ]
    assert len(matching) == 1


def test_automatic_requests_once_across_ticks_and_restart(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()

    first = controller(store, maloo, worker="first")
    first.tick_patch(patch())
    first.tick_patch(patch())
    controller(store, maloo, worker="restart").tick_patch(patch())

    assert maloo.requests == [("session-1", "LU-12345", "single")]
    actions = all_actions(store)
    assert len(actions) == 1
    assert actions[0].status == "waiting_external"
    runs = store.list_runs(patch_id=patch().patch_id)
    assert len(runs) == 1
    assert runs[0].status == "waiting_external"


def test_approval_requires_durable_exact_approval(tmp_path):
    store = configured_store(tmp_path, mode="approval", global_enabled=False)
    maloo = FakeMaloo()
    service = controller(store, maloo)

    service.tick_patch(patch())
    action = all_actions(store)[0]
    run = store.get_run(action.run_id)
    assert action.status == "planned"
    assert maloo.requests == []

    store.approve_action(
        action.action_id,
        approved_by="patrick",
        expected_revision=SHA,
        expected_policy_mode="approval",
        expected_policy_snapshot=run.policy_snapshot,
    )
    service.tick_patch(patch())

    assert maloo.requests == [("session-1", "LU-12345", "single")]
    assert store.get_action_approval(action.action_id).approved_by == "patrick"


def test_exact_revision_is_revalidated_immediately_before_write(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    stale = patch(sha="b" * 40, patchset=4)

    controller(store, maloo, fresh=stale).tick_patch(patch())

    assert maloo.requests == []
    action = all_actions(store)[0]
    run = store.get_run(action.run_id)
    assert action.status == "cancelled"
    assert run.status == "stale"


def test_global_disable_at_final_boundary_prevents_automatic_write(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    maloo.reconcile_hook = lambda: store.set_global_automation(
        False, changed_by="operator", reason="stop now"
    )

    controller(store, maloo).tick_patch(patch())

    assert maloo.requests == []
    action = all_actions(store)[0]
    assert action.status == "cancelled"
    assert store.get_run(action.run_id).status == "cancelled"


def test_ambiguous_mutation_is_reconciled_but_never_retried(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    maloo.request_error = MalooAdapterError(
        MalooErrorCode.AMBIGUOUS_MUTATION,
        "retest",
        "transport outcome unknown",
        ambiguous=True,
    )
    notifications = []
    service = controller(store, maloo, notifications=notifications)

    service.tick_patch(patch())
    action = all_actions(store)[0]
    run = store.get_run(action.run_id)
    assert action.status == "ambiguous"
    assert run.status == "ambiguous"
    assert len(maloo.requests) == 1

    maloo.request_error = None
    maloo.reconcile = MalooRetestReconciliation(
        "session-1", True, True, "LU-12345", ("queue:q-9",)
    )
    controller(store, maloo, notifications=notifications, worker="restart").tick_patch(patch())
    controller(store, maloo, notifications=notifications, worker="restart-2").tick_patch(patch())

    assert len(maloo.requests) == 1
    events = store.list_timeline(run.run_id)
    assert len([e for e in events if e.event_type == "ambiguous_retest_reconciled"]) == 1


def test_waiting_external_completes_when_newer_session_is_observed(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    service = controller(store, maloo)

    service.tick_patch(patch())
    maloo.reconcile = MalooRetestReconciliation(
        "session-1", True, False, "LU-12345", ("newer_session:session-2",)
    )
    service.tick_patch(patch())

    action = all_actions(store)[0]
    assert action.status == "succeeded"
    assert store.get_run(action.run_id).status == "succeeded"
    assert len(maloo.requests) == 1


def test_two_controllers_cannot_duplicate_a_claimed_action(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    # First persist a planned approval action, then switch its policy snapshot
    # is intentionally avoided: use automatic with global off, enable, and let
    # concurrent independent controller instances race the durable claim.
    store.set_global_automation(False, changed_by="test", reason="prepare")
    controller(store, maloo).tick_patch(patch())
    store.set_global_automation(True, changed_by="test", reason="execute")

    services = [
        controller(store, maloo, worker="worker-a"),
        controller(store, maloo, worker="worker-b"),
    ]
    threads = [threading.Thread(target=item.tick_patch, args=(patch(),)) for item in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(maloo.requests) == 1


def test_matching_remote_pending_prevents_mutation(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    maloo.queue_entries = (
        MalooQueueEntry("q-1", SHA, "review-dne-selinux", "queued"),
    )

    result = controller(store, maloo).tick_patch(patch())

    assert result.evaluation.status == "waiting_external"
    assert maloo.requests == []


def test_non_maloo_minus_one_skips_the_test_flow_entirely(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    blocked = dataclasses.replace(
        patch(),
        review_votes=(ReviewVote("Human reviewer", "Code-Review", -1),),
    )

    result = controller(store, maloo).tick_patch(blocked)

    assert result.evaluation.reason_code == "non_maloo_review_veto"
    assert maloo.reads == 0
    assert maloo.requests == []


def test_mapping_dry_run_records_evidence_but_no_trigger_or_action(tmp_path):
    store = configured_store(tmp_path, global_enabled=True)
    maloo = FakeMaloo()
    value = {
        "patch_id": "review-101",
        "url": patch().gerrit_url,
        "change_number": 101,
        "patchset": 3,
        "revision_sha": SHA,
        "lifecycle": "Open",
        "review_votes": [{"name": "Reviewer", "value": 1}],
    }

    result = controller(store, maloo).tick_patch(value, dry_run=True)

    assert result.evaluation.status == "ready"
    assert result.run_ids == ()
    assert store.list_triggers(patch_id=patch().patch_id) == []
    assert all_actions(store) == []
    assert maloo.requests == []


def test_startup_reconciles_executing_action_without_resubmission(tmp_path):
    store = configured_store(tmp_path, mode="approval", global_enabled=False)
    maloo = FakeMaloo()
    service = controller(store, maloo, worker="old-worker")
    service.tick_patch(patch())
    action = all_actions(store)[0]
    run = store.get_run(action.run_id)
    store.approve_action(
        action.action_id,
        approved_by="patrick",
        expected_revision=SHA,
        expected_policy_mode="approval",
        expected_policy_snapshot=run.policy_snapshot,
    )
    store.claim_run(run.run_id, "dead-worker")
    store.claim_next_action(run.run_id, "dead-worker")
    maloo.reconcile = MalooRetestReconciliation(
        "session-1", True, True, "LU-12345", ("queue:q-1",)
    )

    touched = controller(store, maloo, worker="new-worker").reconcile_startup()

    assert touched == (run.run_id,)
    assert store.get_action(action.action_id).status == "waiting_external"
    assert store.get_run(run.run_id).status == "waiting_external"
    assert maloo.requests == []


class RetestControllerUnittestTests(unittest.TestCase):
    """Expose the focused cases to Patch Watcher's unittest quality gate."""

    def run_case(self, case):
        with tempfile.TemporaryDirectory() as directory:
            case(Path(directory))

    def test_automatic_global_off(self):
        self.run_case(test_automatic_global_off_is_preview_only)

    def test_identical_read_failure_notifies_once(self):
        self.run_case(test_repeated_identical_maloo_read_failure_notifies_once)

    def test_automatic_once_across_restart(self):
        self.run_case(test_automatic_requests_once_across_ticks_and_restart)

    def test_durable_approval(self):
        self.run_case(test_approval_requires_durable_exact_approval)

    def test_exact_revision_revalidation(self):
        self.run_case(test_exact_revision_is_revalidated_immediately_before_write)

    def test_final_global_gate(self):
        self.run_case(test_global_disable_at_final_boundary_prevents_automatic_write)

    def test_ambiguous_reconciliation(self):
        self.run_case(test_ambiguous_mutation_is_reconciled_but_never_retried)

    def test_waiting_external_completion(self):
        self.run_case(test_waiting_external_completes_when_newer_session_is_observed)

    def test_concurrent_claim(self):
        self.run_case(test_two_controllers_cannot_duplicate_a_claimed_action)

    def test_remote_pending(self):
        self.run_case(test_matching_remote_pending_prevents_mutation)

    def test_non_maloo_veto(self):
        self.run_case(test_non_maloo_minus_one_skips_the_test_flow_entirely)

    def test_mapping_dry_run(self):
        self.run_case(test_mapping_dry_run_records_evidence_but_no_trigger_or_action)

    def test_startup_reconciliation(self):
        self.run_case(test_startup_reconciles_executing_action_without_resubmission)
