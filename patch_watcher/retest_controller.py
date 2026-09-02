"""Deterministic Phase 1 orchestration for Maloo retests.

The controller is deliberately independent of HTTP and Claude.  It observes
one exact Gerrit revision, records a pure-policy decision, and executes only
durably claimed Maloo actions.  Every write is preceded by exact Gerrit
revalidation and read-only Maloo reconciliation.  An executing action found
after a restart is *only* reconciled; it is never submitted again.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

try:  # Support both package and direct-module test imports.
    from .automation_state import (
        AutomationConflict,
        AutomationRun,
        AutomationStateStore,
        GlobalAutomationDisabled,
    )
    from .maloo_adapter import (
        MalooAdapter,
        MalooAdapterError,
        MalooEnforcedSessionFailure,
        MalooRetestReconciliation,
    )
    from .retest_policy import (
        JiraBugLink,
        MalooFailure,
        PendingRetest,
        RetestBudget,
        RetestEvaluation,
        RetestPolicy,
        ReviewVote,
        RevisionSnapshot,
        evaluate_retests,
    )
except ImportError:  # pragma: no cover - direct execution convenience
    from automation_state import (  # type: ignore
        AutomationConflict,
        AutomationRun,
        AutomationStateStore,
        GlobalAutomationDisabled,
    )
    from maloo_adapter import (  # type: ignore
        MalooAdapter,
        MalooAdapterError,
        MalooEnforcedSessionFailure,
        MalooRetestReconciliation,
    )
    from retest_policy import (  # type: ignore
        JiraBugLink,
        MalooFailure,
        PendingRetest,
        RetestBudget,
        RetestEvaluation,
        RetestPolicy,
        ReviewVote,
        RevisionSnapshot,
        evaluate_retests,
    )


ACTION_TYPE = "request_maloo_retest"
OBSERVATION_KIND = "maloo_retest_evaluation"


@dataclass(frozen=True)
class PatchRevision:
    """Gerrit evidence required before Maloo is consulted.

    ``revalidate`` returns this same type immediately before any mutation.
    Callers should set ``is_current`` and ``revision_state_complete`` only from
    a fresh Gerrit response, not from the watch-list cache.
    """

    patch_id: str
    gerrit_url: str
    gerrit_server: str
    change_number: int
    patchset_number: int
    revision_sha: str
    lifecycle: str = "open"
    is_current: bool = True
    revision_state_complete: bool = True
    review_votes: Tuple[ReviewVote, ...] = ()


@dataclass(frozen=True)
class ControllerNotification:
    kind: str
    patch_id: str
    summary: str
    run_id: str = ""
    action_id: str = ""
    details: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class TickResult:
    patch_id: str
    evaluation: RetestEvaluation
    observation_created: bool
    run_ids: Tuple[str, ...]


Revalidate = Callable[[str], Union[PatchRevision, Mapping[str, Any]]]
Notify = Callable[[ControllerNotification], None]


def _canonical_digest(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, **payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _snapshot_payload(snapshot: RevisionSnapshot) -> dict:
    return dataclasses.asdict(snapshot)


def _policy_snapshot(policy: Any) -> dict:
    return {
        "mode": policy.mode,
        "action_budget": policy.action_budget,
        "delivery_budget": policy.delivery_budget,
        "updated_by": policy.updated_by,
        "updated_at": policy.updated_at.isoformat(),
    }


class RetestController:
    """Observe patches and advance a crash-safe Maloo retest state machine."""

    def __init__(
        self,
        store: AutomationStateStore,
        maloo: MalooAdapter,
        *,
        revalidate: Revalidate,
        notify: Optional[Notify] = None,
        worker_id: str | None = None,
    ) -> None:
        self.store = store
        self.maloo = maloo
        self.revalidate = revalidate
        self.notify = notify or (lambda _event: None)
        self.worker_id = worker_id or "retest-controller-" + str(uuid.uuid4())
        # Protect one process from re-entering the same controller.  Different
        # processes remain serialized by SQLite claims and have distinct IDs.
        self._tick_lock = threading.Lock()

    def tick(
        self, patches: Iterable[PatchRevision | Mapping[str, Any]], *, dry_run: bool = False
    ) -> Tuple[TickResult, ...]:
        """Observe and advance all supplied patches without any UI dependency."""

        with self._tick_lock:
            results = []
            for patch in patches:
                results.append(self._tick_patch_locked(self._coerce_patch(patch), dry_run=dry_run))
            if not dry_run:
                self._reconcile_ambiguous_history()
            return tuple(results)

    def tick_patch(
        self,
        patch: PatchRevision | Mapping[str, Any],
        *,
        dry_run: bool = False,
        collect_research_evidence: bool = False,
    ) -> TickResult:
        """Observe one patch mapping; ``dry_run`` persists no trigger or action."""

        revision = self._coerce_patch(patch)
        with self._tick_lock:
            result = self._tick_patch_locked(
                revision,
                dry_run=dry_run,
                collect_research_evidence=collect_research_evidence,
            )
            if not dry_run:
                self._reconcile_ambiguous_history(revision.patch_id)
            return result

    def reconcile_startup(self) -> Tuple[str, ...]:
        """Reconcile in-flight effects after startup without starting new work."""

        with self._tick_lock:
            touched = []
            for run in self.store.list_runs(include_terminal=False):
                changed = False
                for action in self.store.list_actions(run.run_id):
                    if action.status == "executing":
                        self._reconcile_uncertain(run, action)
                        changed = True
                    elif action.status == "waiting_external":
                        self._poll_waiting(run, action)
                        changed = True
                if changed:
                    self._settle_run(run.run_id)
                    touched.append(run.run_id)
            self._reconcile_ambiguous_history()
            return tuple(touched)

    @staticmethod
    def _coerce_patch(value: PatchRevision | Mapping[str, Any]) -> PatchRevision:
        if isinstance(value, PatchRevision):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("patch must be PatchRevision or a mapping")
        url = str(value.get("gerrit_url") or value.get("url") or "")
        server = str(value.get("gerrit_server") or "")
        if not server and url:
            parsed = urlsplit(url)
            server = "%s://%s" % (parsed.scheme, parsed.netloc)
        votes = []
        for item in value.get("review_votes") or ():
            if isinstance(item, ReviewVote):
                votes.append(item)
                continue
            if not isinstance(item, Mapping):
                continue
            reviewer = str(item.get("reviewer") or item.get("name") or "unknown")
            source = str(item.get("source") or "")
            if not source:
                source = "maloo" if reviewer.casefold() == "maloo" else "human"
            votes.append(
                ReviewVote(
                    reviewer=reviewer,
                    label=str(item.get("label") or "Code-Review"),
                    value=int(item.get("value") or 0),
                    source=source,
                    message=str(item.get("message") or ""),
                )
            )
        lifecycle = str(value.get("lifecycle") or value.get("status") or "open").lower()
        lifecycle = {"new": "open"}.get(lifecycle, lifecycle)
        change = int(value.get("change_number") or 0)
        return PatchRevision(
            patch_id=str(value.get("patch_id") or change),
            gerrit_url=url,
            gerrit_server=server,
            change_number=change,
            patchset_number=int(value.get("patchset_number") or value.get("patchset") or 0),
            revision_sha=str(value.get("revision_sha") or value.get("revision") or ""),
            lifecycle=lifecycle,
            is_current=bool(value.get("is_current", True)),
            revision_state_complete=bool(value.get("revision_state_complete", True)),
            review_votes=tuple(votes),
        )

    def _tick_patch_locked(
        self,
        patch: PatchRevision,
        *,
        dry_run: bool = False,
        collect_research_evidence: bool = False,
    ) -> TickResult:
        self._validate_patch_input(patch)
        self.store.upsert_patch(
            patch.patch_id,
            gerrit_url=patch.gerrit_url,
            change_number=patch.change_number,
            revision=patch.revision_sha.lower(),
            patchset=patch.patchset_number,
            status=patch.lifecycle,
        )
        if not dry_run:
            self._cancel_policy_drift(patch.patch_id)
            self._advance_active_runs(patch.patch_id)

        policy_record = self.store.get_policy(patch.patch_id)
        global_setting = self.store.get_global_automation()
        observation_notice = None
        if self._should_read_maloo(
            patch,
            policy_record.mode,
            collect_research_evidence=collect_research_evidence,
        ):
            snapshot, session_submissions, observation_notice = self._collect_snapshot(patch)
        else:
            snapshot = RevisionSnapshot(
                patch.gerrit_server,
                patch.change_number,
                patch.patchset_number,
                patch.revision_sha,
                patch.lifecycle,
                patch.is_current,
                patch.revision_state_complete,
                False,
                patch.review_votes,
            )
            session_submissions = {}
        existing_keys = frozenset(self._existing_action_keys(patch.patch_id))
        evaluation = evaluate_retests(
            snapshot,
            RetestPolicy(
                mode=policy_record.mode,
                global_execution_enabled=global_setting.enabled,
                policy_version=policy_record.updated_at.isoformat(),
                max_new_actions=policy_record.action_budget,
            ),
            RetestBudget(
                max_actions=policy_record.action_budget,
                actions_used=min(len(existing_keys), policy_record.action_budget),
                existing_action_keys=existing_keys,
            ),
        )
        observation_payload = {
            "snapshot": _snapshot_payload(snapshot),
            "policy": {
                **_policy_snapshot(policy_record),
                "global_execution_enabled": global_setting.enabled,
            },
            "evaluation": evaluation.to_dict(),
        }
        fingerprint = _canonical_digest("maloo_observation/v1", observation_payload)
        _observation, created = self.store.record_observation(
            patch.patch_id,
            revision=patch.revision_sha.lower(),
            source="gerrit+maloo",
            kind=OBSERVATION_KIND,
            fingerprint=fingerprint,
            payload=observation_payload,
        )
        if created and observation_notice is not None:
            self._notify(observation_notice)

        run_ids = ()
        if not dry_run:
            run_ids = self._persist_evaluation(
                patch,
                evaluation,
                fingerprint,
                session_submissions,
            )
            self._advance_active_runs(patch.patch_id)
        return TickResult(patch.patch_id, evaluation, created, tuple(run_ids))

    @staticmethod
    def _should_read_maloo(
        patch: PatchRevision,
        policy_mode: str,
        *,
        collect_research_evidence: bool = False,
    ) -> bool:
        """Apply Gerrit and operator gates before entering the test flow."""

        return (
            patch.revision_state_complete
            and patch.is_current
            and patch.lifecycle == "open"
            and (policy_mode != "disabled" or collect_research_evidence)
            and not any(vote.is_non_maloo_veto for vote in patch.review_votes)
        )

    @staticmethod
    def _validate_patch_input(patch: PatchRevision) -> None:
        if not isinstance(patch, PatchRevision):
            raise TypeError("patch must be PatchRevision")
        # RevisionSnapshot performs the remaining strict validation.
        RevisionSnapshot(
            patch.gerrit_server,
            patch.change_number,
            patch.patchset_number,
            patch.revision_sha,
            patch.lifecycle,
            patch.is_current,
            patch.revision_state_complete,
            True,
            patch.review_votes,
        )

    def _collect_snapshot(
        self, patch: PatchRevision
    ) -> tuple[
        RevisionSnapshot,
        dict[str, str],
        Optional[ControllerNotification],
    ]:
        failures: list[MalooFailure] = []
        pending: list[PendingRetest] = []
        submissions: dict[str, str] = {}
        complete = True
        notice = None
        try:
            groups = self.maloo.get_enforced_failures(
                patch.change_number, patch.patchset_number
            )
            queue = self.maloo.get_queue(patch.revision_sha)
            for group in groups:
                submissions[group.session.session_id] = group.session.submission
                self._append_group_failures(failures, group)
                for entry in queue.pending:
                    if entry.test_group == group.session.test_group:
                        pending.append(
                            PendingRetest(
                                group.session.session_id,
                                group.session.test_group,
                                entry.queue_id or entry.status,
                            )
                        )
        except MalooAdapterError as exc:
            complete = False
            notice = ControllerNotification(
                "maloo_observation_failed",
                patch.patch_id,
                str(exc),
                details=exc.to_dict(),
            )
        return (
            RevisionSnapshot(
                patch.gerrit_server,
                patch.change_number,
                patch.patchset_number,
                patch.revision_sha,
                patch.lifecycle,
                patch.is_current,
                patch.revision_state_complete,
                complete,
                patch.review_votes,
                tuple(failures),
                tuple(pending),
            ),
            submissions,
            notice,
        )

    @staticmethod
    def _append_group_failures(
        output: list[MalooFailure], group: MalooEnforcedSessionFailure
    ) -> None:
        bug_evidence = {item.suite_id: item for item in group.suite_bugs}
        for suite in group.failures.failed_suites:
            evidence = bug_evidence.get(suite.suite_id)
            links = ()
            if evidence is not None:
                links = tuple(
                    JiraBugLink(
                        issue_key=link.ticket,
                        accepted_for_retest=link.accepted,
                        evidence="maloo:%s" % link.state,
                    )
                    for link in evidence.bugs.links
                )
            output.append(
                MalooFailure(
                    session_id=group.session.session_id,
                    test_group=group.session.test_group,
                    suite=suite.suite,
                    enforced=True,
                    linked_bugs=links,
                    failing_subtests=tuple(item.name for item in suite.failed_subtests),
                    details_complete=True,
                    bug_links_complete=evidence is not None,
                    remote_failure_id=suite.suite_id,
                )
            )

    def _existing_action_keys(self, patch_id: str) -> Sequence[str]:
        keys = []
        for run in self.store.list_runs(patch_id=patch_id):
            keys.extend(action.idempotency_key for action in self.store.list_actions(run.run_id))
        return keys

    def _persist_evaluation(
        self,
        patch: PatchRevision,
        evaluation: RetestEvaluation,
        observation_fingerprint: str,
        session_submissions: Mapping[str, str],
    ) -> Sequence[str]:
        actionable = tuple(
            decision
            for decision in evaluation.decisions
            if decision.outcome in {"advice", "investigate", "waiting_approval", "ready"}
        )
        if not actionable:
            return ()
        trigger = self.store.create_trigger(
            patch.patch_id,
            revision=patch.revision_sha.lower(),
            kind=OBSERVATION_KIND,
            fingerprint=observation_fingerprint,
            payload=evaluation.to_dict(),
        )
        deterministic_key = "retest-run:" + observation_fingerprint
        try:
            run = self.store.create_run(trigger.trigger_id, deterministic_key=deterministic_key)
        except AutomationConflict:
            existing = next(
                (
                    value
                    for value in self.store.list_runs(patch_id=patch.patch_id)
                    if value.deterministic_key == deterministic_key
                ),
                None,
            )
            if existing is None:
                # A different active run owns this patch.  Its next tick will
                # finish before this durable observation can be considered.
                return ()
            run = existing
        self.store.append_timeline(
            run.run_id,
            "retest_evaluated",
            evaluation.to_dict(),
            idempotency_key="evaluation:" + observation_fingerprint,
        )

        planned = 0
        for decision in actionable:
            action = decision.action
            if action is None or decision.outcome not in {"waiting_approval", "ready"}:
                continue
            request = {
                "change_number": action.change_number,
                "patchset": patch.patchset_number,
                "revision_sha": action.revision_sha,
                "session_id": action.session_id,
                "test_group": action.test_groups[0],
                "test_groups": list(action.test_groups),
                "jira_ticket": action.jira_justification,
                "original_submission": session_submissions.get(action.session_id, ""),
                "action_fingerprint": action.action_fingerprint,
            }
            try:
                attempt = self.store.plan_action(
                    run.run_id,
                    action_type=ACTION_TYPE,
                    request=request,
                    idempotency_key=action.action_key,
                )
            except AutomationConflict:
                continue
            planned += 1
            self.store.append_timeline(
                run.run_id,
                "retest_action_planned",
                {
                    "action_id": attempt.action_id,
                    "mode": run.policy_snapshot["mode"],
                    "session_id": action.session_id,
                    "test_group": request["test_group"],
                    "jira_ticket": action.jira_justification,
                },
                idempotency_key="action-planned:" + attempt.action_id,
            )

        if planned == 0 and not self.store.list_actions(run.run_id):
            self.store.finish_run(run.run_id, "succeeded")
        return (run.run_id,)

    def _cancel_policy_drift(self, patch_id: str) -> None:
        current = self.store.get_policy(patch_id)
        current_snapshot = _policy_snapshot(current)
        for run in self.store.list_runs(patch_id=patch_id, include_terminal=False):
            if run.policy_snapshot == current_snapshot:
                continue
            for action in self.store.list_actions(run.run_id):
                if action.status == "planned":
                    self.store.finish_action(
                        action.action_id,
                        "cancelled",
                        failure_code="policy_changed",
                        failure_summary="Automation policy changed before completion",
                    )
                elif action.status in {"executing", "waiting_external"}:
                    # Never declare an executing mutation absent.  It remains
                    # for read-only recovery instead of being cancelled.  A
                    # waiting action likewise already crossed the write
                    # boundary and must be observed to completion.
                    continue
            if any(
                action.status in {"executing", "waiting_external"}
                for action in self.store.list_actions(run.run_id)
            ):
                continue
            self.store.finish_run(
                run.run_id,
                "cancelled",
                failure_code="policy_changed",
                failure_summary="Automation policy changed",
            )

    def _advance_active_runs(self, patch_id: str) -> None:
        for run in self.store.list_runs(patch_id=patch_id, include_terminal=False):
            actions = self.store.list_actions(run.run_id)
            for action in actions:
                if action.status == "executing" and action.claimed_by == self.worker_id:
                    self._reconcile_uncertain(run, action)
                elif action.status == "waiting_external":
                    self._poll_waiting(run, action)
            run = self.store.get_run(run.run_id)
            if run.status in {"planned", "executing", "waiting_external"}:
                self._execute_next_planned(run)
                self._settle_run(run.run_id)

    def _execute_next_planned(self, run: AutomationRun) -> None:
        policy_mode = run.policy_snapshot.get("mode")
        if policy_mode == "advise":
            return
        planned = next(
            (item for item in self.store.list_actions(run.run_id) if item.status == "planned"),
            None,
        )
        if planned is None:
            return
        if policy_mode == "approval" and self.store.get_action_approval(planned.action_id) is None:
            return
        try:
            current_run = self.store.get_run(run.run_id)
            if current_run.status == "planned":
                self.store.claim_run(run.run_id, self.worker_id)
            claimed = self.store.claim_next_action(run.run_id, self.worker_id)
        except (AutomationConflict, GlobalAutomationDisabled):
            return
        if claimed is None:
            return
        self._execute_claimed(self.store.get_run(run.run_id), claimed)

    def _execute_claimed(self, run: AutomationRun, action: Any) -> None:
        request = action.request
        try:
            patch = self.store.get_patch(run.patch_id)
            fresh = self._coerce_patch(self.revalidate(patch.gerrit_url))
            if not self._same_open_revision(run, patch.change_number, fresh):
                self.store.finish_action(
                    action.action_id,
                    "cancelled",
                    failure_code="stale_revision",
                    failure_summary="Exact Gerrit revision changed before retest",
                )
                self.store.finish_run(
                    run.run_id,
                    "stale",
                    failure_code="stale_revision",
                    failure_summary="Exact Gerrit revision changed before retest",
                )
                self.store.append_timeline(
                    run.run_id,
                    "retest_suppressed_stale_revision",
                    {"action_id": action.action_id},
                    idempotency_key="stale:" + action.action_id,
                )
                return

            reconciliation = self._remote_reconcile(request)
            if reconciliation.already_requested:
                self._record_reconciled(run, action, reconciliation)
                return
            if not self._execution_authorized(run, action.action_id):
                self.store.finish_action(
                    action.action_id,
                    "cancelled",
                    failure_code="authority_changed",
                    failure_summary="Automation authority changed before the Maloo write",
                )
                self.store.finish_run(
                    run.run_id,
                    "cancelled",
                    failure_code="authority_changed",
                    failure_summary="Automation authority changed before the Maloo write",
                )
                self.store.append_timeline(
                    run.run_id,
                    "retest_suppressed_authority_changed",
                    {"action_id": action.action_id},
                    idempotency_key="authority-changed:" + action.action_id,
                )
                return

            result = self.maloo.request_retest(
                request["session_id"], request["jira_ticket"], option="single"
            )
            if not result.requested:
                self._fail_action(run, action, "retest_rejected", "Maloo rejected the retest")
                return
            self.store.finish_action(
                action.action_id,
                "waiting_external",
                result=result.to_dict(),
            )
            self.store.finish_run(run.run_id, "waiting_external")
            self.store.append_timeline(
                run.run_id,
                "retest_requested",
                {"action_id": action.action_id, **result.to_dict()},
                idempotency_key="retest-requested:" + action.action_id,
            )
            self._notify(
                ControllerNotification(
                    "retest_requested",
                    run.patch_id,
                    "Maloo retest requested for %s" % request["session_id"],
                    run.run_id,
                    action.action_id,
                    result.to_dict(),
                )
            )
        except MalooAdapterError as exc:
            if exc.ambiguous:
                self.store.finish_action(
                    action.action_id,
                    "ambiguous",
                    failure_code=exc.code,
                    failure_summary=str(exc),
                )
                self.store.finish_run(
                    run.run_id,
                    "ambiguous",
                    failure_code=exc.code,
                    failure_summary=str(exc),
                )
                self.store.append_timeline(
                    run.run_id,
                    "retest_outcome_ambiguous",
                    {"action_id": action.action_id, "error": exc.to_dict()},
                    idempotency_key="ambiguous:" + action.action_id,
                )
                self._notify(
                    ControllerNotification(
                        "retest_ambiguous",
                        run.patch_id,
                        str(exc),
                        run.run_id,
                        action.action_id,
                        exc.to_dict(),
                    )
                )
            else:
                self._fail_action(run, action, exc.code, str(exc), exc.to_dict())
        except AutomationConflict:
            # Another controller may have reconciled the same durable claim
            # while this process was finishing its read.  Never turn that
            # harmless state race into a second action or a false failure.
            return
        except Exception as exc:
            # Gerrit revalidation failures happen before the Maloo mutation and
            # are therefore definitive local failures, not ambiguous writes.
            self._fail_action(run, action, "precondition_failed", str(exc))

    @staticmethod
    def _same_open_revision(
        run: AutomationRun, expected_change: int, fresh: PatchRevision
    ) -> bool:
        return (
            fresh.revision_state_complete
            and fresh.is_current
            and fresh.lifecycle == "open"
            and fresh.patch_id == run.patch_id
            and fresh.change_number == expected_change
            and fresh.patchset_number == run.patchset
            and fresh.revision_sha.lower() == run.revision.lower()
        )

    def _remote_reconcile(self, request: Mapping[str, Any]) -> MalooRetestReconciliation:
        return self.maloo.reconcile_remote_retest(
            change_number=request["change_number"],
            patchset=request["patchset"],
            revision_sha=request["revision_sha"],
            session_ref=request["session_id"],
            test_group=request["test_group"],
            original_submission=request.get("original_submission", ""),
            jira_ticket=request["jira_ticket"],
        )

    def _execution_authorized(self, run: AutomationRun, action_id: str) -> bool:
        """Recheck mutable operator authority at the final write boundary."""

        if _policy_snapshot(self.store.get_policy(run.patch_id)) != run.policy_snapshot:
            return False
        mode = run.policy_snapshot.get("mode")
        if mode == "automatic":
            return self.store.get_global_automation().enabled
        if mode == "approval":
            approval = self.store.get_action_approval(action_id)
            return (
                approval is not None
                and approval.expected_revision == run.revision
                and approval.policy_snapshot == run.policy_snapshot
            )
        return False

    def _reconcile_uncertain(self, run: AutomationRun, action: Any) -> None:
        """Recover an executing action after restart without ever resubmitting."""

        try:
            reconciliation = self._remote_reconcile(action.request)
        except MalooAdapterError as exc:
            self._notify(
                ControllerNotification(
                    "retest_reconciliation_failed",
                    run.patch_id,
                    str(exc),
                    run.run_id,
                    action.action_id,
                    exc.to_dict(),
                )
            )
            return
        if reconciliation.already_requested:
            self._record_reconciled(run, action, reconciliation)
            return
        self.store.finish_action(
            action.action_id,
            "ambiguous",
            result=reconciliation.to_dict(),
            failure_code="outcome_not_observed",
            failure_summary="Restart reconciliation could not prove whether the retest was sent",
        )
        self.store.finish_run(
            run.run_id,
            "ambiguous",
            failure_code="outcome_not_observed",
            failure_summary="Restart reconciliation could not prove whether the retest was sent",
        )
        self.store.append_timeline(
            run.run_id,
            "retest_reconciled_not_observed",
            {"action_id": action.action_id, **reconciliation.to_dict()},
            idempotency_key="reconcile-not-observed:" + action.action_id,
        )

    def _poll_waiting(self, run: AutomationRun, action: Any) -> None:
        try:
            reconciliation = self._remote_reconcile(action.request)
        except MalooAdapterError as exc:
            self._notify(
                ControllerNotification(
                    "retest_poll_failed",
                    run.patch_id,
                    str(exc),
                    run.run_id,
                    action.action_id,
                    exc.to_dict(),
                )
            )
            return
        if reconciliation.pending is True:
            return
        if reconciliation.already_requested:
            self.store.finish_action(
                action.action_id, "succeeded", result=reconciliation.to_dict()
            )
            self.store.append_timeline(
                run.run_id,
                "retest_result_observed",
                {"action_id": action.action_id, **reconciliation.to_dict()},
                idempotency_key="result-observed:" + action.action_id,
            )
        # not_observed is not authority to retry; retain waiting_external.

    def _record_reconciled(
        self, run: AutomationRun, action: Any, reconciliation: MalooRetestReconciliation
    ) -> None:
        status = "waiting_external" if reconciliation.pending is True else "succeeded"
        self.store.finish_action(action.action_id, status, result=reconciliation.to_dict())
        if status == "waiting_external":
            self.store.finish_run(run.run_id, "waiting_external")
        self.store.append_timeline(
            run.run_id,
            "retest_already_requested",
            {"action_id": action.action_id, **reconciliation.to_dict()},
            idempotency_key="already-requested:%s:%s" % (action.action_id, reconciliation.outcome),
        )
        self._settle_run(run.run_id)

    def _settle_run(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run.status not in {"planned", "executing", "waiting_external"}:
            return
        actions = self.store.list_actions(run_id)
        if not actions:
            # A different controller may see the run in the narrow durable
            # window between create_run() and plan_action().  Only the creator
            # explicitly closes a genuine preview-only run; observers must
            # not mistake this construction window for completed work.
            return
        statuses = {item.status for item in actions}
        if "ambiguous" in statuses:
            self.store.finish_run(run_id, "ambiguous")
        elif "failed" in statuses:
            self.store.finish_run(run_id, "failed")
        elif statuses <= {"succeeded"}:
            self.store.finish_run(run_id, "succeeded")
        elif "waiting_external" in statuses and "executing" not in statuses:
            self.store.finish_run(run_id, "waiting_external")

    def _fail_action(
        self,
        run: AutomationRun,
        action: Any,
        code: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.store.finish_action(
            action.action_id,
            "failed",
            failure_code=code,
            failure_summary=summary,
        )
        self.store.finish_run(
            run.run_id,
            "failed",
            failure_code=code,
            failure_summary=summary,
        )
        self.store.append_timeline(
            run.run_id,
            "retest_failed",
            {"action_id": action.action_id, "code": code, "summary": summary},
            idempotency_key="failed:" + action.action_id,
        )
        self._notify(
            ControllerNotification(
                "retest_failed",
                run.patch_id,
                summary,
                run.run_id,
                action.action_id,
                details or {"code": code},
            )
        )

    def _reconcile_ambiguous_history(self, patch_id: str | None = None) -> None:
        for run in self.store.list_runs(patch_id=patch_id):
            for action in self.store.list_actions(run.run_id):
                if action.status != "ambiguous" or action.action_type != ACTION_TYPE:
                    continue
                try:
                    reconciliation = self._remote_reconcile(action.request)
                except MalooAdapterError:
                    continue
                if not reconciliation.already_requested:
                    continue
                key = "ambiguous-reconciled:%s:%s" % (
                    action.action_id,
                    reconciliation.outcome,
                )
                if any(
                    event.idempotency_key == key
                    for event in self.store.list_timeline(run.run_id)
                ):
                    continue
                self.store.append_timeline(
                    run.run_id,
                    "ambiguous_retest_reconciled",
                    {"action_id": action.action_id, **reconciliation.to_dict()},
                    idempotency_key=key,
                )
                self._notify(
                    ControllerNotification(
                        "ambiguous_retest_reconciled",
                        run.patch_id,
                        "An ambiguous Maloo retest was found remotely",
                        run.run_id,
                        action.action_id,
                        reconciliation.to_dict(),
                    )
                )

    def _notify(self, event: ControllerNotification) -> None:
        try:
            self.notify(event)
        except Exception:
            # Notification delivery must never change action semantics.  The
            # run timeline remains the source of truth for later reporting.
            return
