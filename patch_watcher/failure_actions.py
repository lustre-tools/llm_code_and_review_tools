"""Operator-approved, revision-pinned Maloo failure mutations.

This controller supports exactly two writes, in order: associate an existing
Jira key with one failed Maloo test set, then request a session-level retest
after the association is remotely observable as accepted.  Planning is inert;
each write requires its own durable approval in :mod:`automation_state`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Union

try:
    from .automation_state import (
        ActionApproval,
        ActionAttempt,
        AutomationConflict,
        AutomationRun,
        AutomationStateStore,
        BudgetExhausted,
    )
    from .maloo_adapter import (
        JIRA_RE,
        MalooAdapter,
        MalooAdapterError,
        MalooBugLinks,
        MalooRetestReconciliation,
    )
except ImportError:  # pragma: no cover - direct script/test execution
    from automation_state import (  # type: ignore
        ActionApproval,
        ActionAttempt,
        AutomationConflict,
        AutomationRun,
        AutomationStateStore,
        BudgetExhausted,
    )
    from maloo_adapter import (  # type: ignore
        JIRA_RE,
        MalooAdapter,
        MalooAdapterError,
        MalooBugLinks,
        MalooRetestReconciliation,
    )


LINK_ACTION = "associate_existing_jira_to_maloo_failure"
RETEST_ACTION = "request_maloo_retest_after_bug_association"
OBSERVATION_KIND = "operator_failure_write_plan"


class FailureActionError(RuntimeError):
    """Invalid or unsafe failure-write request."""


@dataclass(frozen=True)
class FailurePatchRevision:
    patch_id: str
    gerrit_url: str
    change_number: int
    patchset_number: int
    revision_sha: str
    lifecycle: str = "open"
    is_current: bool = True
    revision_state_complete: bool = True


@dataclass(frozen=True)
class FailureWritePlan:
    run: AutomationRun
    link_action: ActionAttempt
    fingerprint: str


@dataclass(frozen=True)
class FailureAdvanceResult:
    run_id: str
    run_status: str
    stage: str
    action_id: str = ""
    detail: str = ""


Revalidate = Callable[
    [str], Union[FailurePatchRevision, Mapping[str, Any], Any]
]


def _text(name: str, value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _canonical_digest(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, **payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class FailureActionController:
    """Advance a two-write workflow only at explicit operator boundaries."""

    def __init__(
        self,
        store: AutomationStateStore,
        maloo: MalooAdapter,
        *,
        revalidate: Revalidate,
        worker_id: str | None = None,
        reconcile_orphans: bool = False,
    ) -> None:
        self.store = store
        self.maloo = maloo
        self.revalidate = revalidate
        self.worker_id = worker_id or "failure-actions-" + str(uuid.uuid4())
        self.reconcile_orphans = bool(reconcile_orphans)

    def plan_link_existing_bug(
        self,
        patch_id: str,
        *,
        expected_revision: str,
        expected_patchset: int,
        session_id: str,
        test_group: str,
        suite_id: str,
        suite_name: str,
        jira_ticket: str,
        at: datetime | None = None,
    ) -> FailureWritePlan:
        """Plan, but do not execute or approve, one exact association."""
        patch_id = _text("patch_id", patch_id)
        expected_revision = _text("expected_revision", expected_revision).lower()
        session_id = _text("session_id", session_id)
        test_group = _text("test_group", test_group)
        suite_id = _text("suite_id", suite_id)
        suite_name = _text("suite_name", suite_name)
        jira_ticket = _text("jira_ticket", jira_ticket).upper()
        if not isinstance(expected_patchset, int) or expected_patchset <= 0:
            raise ValueError("expected_patchset must be a positive integer")
        if not JIRA_RE.fullmatch(jira_ticket):
            raise ValueError("jira_ticket must be an existing Jira issue key")
        patch = self.store.get_patch(patch_id)
        if (
            patch.current_revision.lower() != expected_revision
            or patch.current_patchset != expected_patchset
            or patch.status.casefold() != "open"
        ):
            raise FailureActionError("stored patch does not match the exact revision")
        policy = self.store.get_policy(patch_id)
        if policy.mode != "approval":
            raise FailureActionError(
                "failure writes require per-action approval policy"
            )
        if policy.action_budget < 2:
            raise FailureActionError(
                "failure-write workflow requires an action budget of at least two"
            )
        payload = {
            "patch_id": patch_id,
            "change_number": patch.change_number,
            "revision_sha": expected_revision,
            "patchset": expected_patchset,
            "session_id": session_id,
            "test_group": test_group,
            "suite_id": suite_id,
            "suite_name": suite_name,
            "jira_ticket": jira_ticket,
        }
        fingerprint = _canonical_digest("failure-write/v1", payload)
        self.store.record_observation(
            patch_id,
            revision=expected_revision,
            source="operator",
            kind=OBSERVATION_KIND,
            fingerprint=fingerprint,
            payload=payload,
            observed_at=at,
        )
        trigger = self.store.create_trigger(
            patch_id,
            revision=expected_revision,
            kind=OBSERVATION_KIND,
            fingerprint=fingerprint,
            payload=payload,
            created_at=at,
        )
        run = self.store.create_run(
            trigger.trigger_id,
            deterministic_key="failure-write-run:" + fingerprint,
            at=at,
        )
        link_action = self.store.plan_action(
            run.run_id,
            action_type=LINK_ACTION,
            request={**payload, "workflow_fingerprint": fingerprint},
            idempotency_key="maloo-link-bug:" + fingerprint,
            at=at,
        )
        self.store.append_timeline(
            run.run_id,
            "failure_link_planned",
            {
                "action_id": link_action.action_id,
                "suite_id": suite_id,
                "suite_name": suite_name,
                "jira_ticket": jira_ticket,
            },
            idempotency_key="failure-link-planned:" + fingerprint,
            at=at,
        )
        return FailureWritePlan(run, link_action, fingerprint)

    def approve_action(
        self,
        action_id: str,
        *,
        approved_by: str,
        expected_revision: str,
        expected_policy_snapshot: dict | None = None,
        at: datetime | None = None,
    ) -> ActionApproval:
        """Persist one exact operator approval; this performs no mutation."""
        return self.store.approve_action(
            action_id,
            approved_by=approved_by,
            expected_revision=expected_revision.lower(),
            expected_policy_mode="approval",
            expected_policy_snapshot=expected_policy_snapshot,
            at=at,
        )

    def advance(self, run_id: str) -> FailureAdvanceResult:
        """Perform at most one approved mutation, or reconcile without writing."""
        run = self.store.get_run(run_id)
        if run.status in {"succeeded", "failed", "ambiguous", "cancelled", "stale"}:
            return FailureAdvanceResult(run_id, run.status, "terminal")
        actions = self.store.list_actions(run_id)
        link = next((item for item in actions if item.action_type == LINK_ACTION), None)
        retest = next((item for item in actions if item.action_type == RETEST_ACTION), None)
        if link is None:
            return self._fail_run(run, "missing_link_action", "Link action is missing")

        if link.status in {"executing", "waiting_external"}:
            if link.status == "executing" and not self.reconcile_orphans:
                return FailureAdvanceResult(
                    run_id, run.status, "claimed_elsewhere", link.action_id
                )
            result = self._reconcile_link(run, link, uncertain=link.status == "executing")
            if result is not None:
                return result
            run = self.store.get_run(run_id)
            actions = self.store.list_actions(run_id)
            link = next(item for item in actions if item.action_type == LINK_ACTION)
            retest = next(
                (item for item in actions if item.action_type == RETEST_ACTION), None
            )

        if link.status == "planned":
            if self.store.get_action_approval(link.action_id) is None:
                return FailureAdvanceResult(
                    run_id, run.status, "waiting_approval", link.action_id
                )
            claimed = self._claim(run, link.action_id)
            if claimed is None:
                return FailureAdvanceResult(run_id, run.status, "claimed_elsewhere")
            return self._execute_link(self.store.get_run(run_id), claimed)

        if link.status != "succeeded":
            return FailureAdvanceResult(run_id, run.status, "link_not_complete", link.action_id)

        if retest is None:
            retest = self._plan_retest(run, link)
        if retest is None:
            return FailureAdvanceResult(
                run_id,
                self.store.get_run(run_id).status,
                "failed",
                detail="Could not plan retest",
            )
        if retest.status in {"executing", "waiting_external"}:
            if retest.status == "executing" and not self.reconcile_orphans:
                return FailureAdvanceResult(
                    run_id, run.status, "claimed_elsewhere", retest.action_id
                )
            return self._reconcile_retest(
                self.store.get_run(run_id),
                retest,
                uncertain=retest.status == "executing",
            )
        if retest.status == "planned":
            if self.store.get_action_approval(retest.action_id) is None:
                return FailureAdvanceResult(
                    run_id,
                    self.store.get_run(run_id).status,
                    "waiting_approval",
                    retest.action_id,
                )
            claimed = self._claim(self.store.get_run(run_id), retest.action_id)
            if claimed is None:
                return FailureAdvanceResult(run_id, run.status, "claimed_elsewhere")
            return self._execute_retest(self.store.get_run(run_id), claimed)
        if retest.status == "succeeded":
            settled = self.store.finish_run(run_id, "succeeded")
            return FailureAdvanceResult(run_id, settled.status, "complete", retest.action_id)
        return FailureAdvanceResult(run_id, self.store.get_run(run_id).status, retest.status)

    def _claim(self, run: AutomationRun, expected_action_id: str) -> ActionAttempt | None:
        try:
            if run.status == "planned":
                self.store.claim_run(run.run_id, self.worker_id)
            claimed = self.store.claim_next_action(run.run_id, self.worker_id)
        except AutomationConflict:
            return None
        if claimed is None:
            return None
        if claimed.action_id != expected_action_id:
            raise FailureActionError("action ordering invariant was violated")
        return claimed

    def _fresh_revision(self, run: AutomationRun) -> FailurePatchRevision:
        patch = self.store.get_patch(run.patch_id)
        raw = self.revalidate(patch.gerrit_url)
        if isinstance(raw, FailurePatchRevision):
            fresh = raw
        elif isinstance(raw, Mapping):
            fresh = FailurePatchRevision(
                patch_id=_text("patch_id", raw.get("patch_id", run.patch_id)),
                gerrit_url=_text("gerrit_url", raw.get("gerrit_url", patch.gerrit_url)),
                change_number=int(raw.get("change_number", patch.change_number)),
                patchset_number=int(raw.get("patchset_number", raw.get("patchset", 0))),
                revision_sha=_text(
                    "revision_sha", raw.get("revision_sha", raw.get("revision", ""))
                ),
                lifecycle=str(raw.get("lifecycle", "open")),
                is_current=bool(raw.get("is_current", False)),
                revision_state_complete=bool(
                    raw.get("revision_state_complete", False)
                ),
            )
        else:
            fresh = FailurePatchRevision(
                patch_id=str(raw.patch_id),
                gerrit_url=str(raw.gerrit_url),
                change_number=int(raw.change_number),
                patchset_number=int(raw.patchset_number),
                revision_sha=str(raw.revision_sha),
                lifecycle=str(getattr(raw, "lifecycle", "open")),
                is_current=bool(getattr(raw, "is_current", False)),
                revision_state_complete=bool(
                    getattr(raw, "revision_state_complete", False)
                ),
            )
        if not (
            fresh.revision_state_complete
            and fresh.is_current
            and fresh.lifecycle.casefold() == "open"
            and fresh.patch_id == run.patch_id
            and fresh.change_number == patch.change_number
            and fresh.patchset_number == run.patchset
            and fresh.revision_sha.lower() == run.revision.lower()
            and patch.current_revision.lower() == run.revision.lower()
            and patch.current_patchset == run.patchset
        ):
            raise FailureActionError("exact Gerrit revision is no longer current")
        return fresh

    def _authority_current(self, run: AutomationRun, action_id: str) -> bool:
        """Recheck the exact durable approval and policy at the write boundary."""
        approval = self.store.get_action_approval(action_id)
        if approval is None or approval.policy_snapshot != run.policy_snapshot:
            return False
        policy = self.store.get_policy(run.patch_id)
        current_snapshot = {
            "mode": policy.mode,
            "action_budget": policy.action_budget,
            "delivery_budget": policy.delivery_budget,
            "updated_by": policy.updated_by,
            "updated_at": policy.updated_at.isoformat(),
        }
        return current_snapshot == run.policy_snapshot and policy.mode == "approval"

    @staticmethod
    def _link_state(links: MalooBugLinks, jira_ticket: str) -> str:
        matches = [item.state for item in links.links if item.ticket == jira_ticket]
        if "accepted" in matches:
            return "accepted"
        if "pending" in matches:
            return "pending"
        return "absent"

    def _failure_target_current(self, request: Mapping[str, Any]) -> bool:
        """Require the exact planned session/group/suite to still be enforced."""
        failures = self.maloo.get_enforced_failures(
            int(request["change_number"]), int(request["patchset"])
        )
        for unit in failures:
            session = unit.session
            if (
                session.session_id != request["session_id"]
                or session.test_group != request["test_group"]
            ):
                continue
            for suite in unit.failures.failed_suites:
                if (
                    suite.suite_id == request["suite_id"]
                    and suite.suite == request["suite_name"]
                ):
                    return True
        return False

    def _execute_link(
        self, run: AutomationRun, action: ActionAttempt
    ) -> FailureAdvanceResult:
        request = action.request
        try:
            if not self._authority_current(run, action.action_id):
                return self._authority_changed(run, action)
            self._fresh_revision(run)
            if not self._failure_target_current(request):
                return self._target_stale(run, action)
            links = self.maloo.get_bug_links(request["suite_id"], related=False)
            state = self._link_state(links, request["jira_ticket"])
            if state == "accepted":
                return self._complete_link(run, action, links)
            if state == "pending":
                self.store.finish_action(
                    action.action_id, "waiting_external", result=links.to_dict()
                )
                self.store.finish_run(run.run_id, "waiting_external")
                return FailureAdvanceResult(
                    run.run_id, "waiting_external", "link_pending", action.action_id
                )
            if not self._authority_current(run, action.action_id):
                return self._authority_changed(run, action)
            result = self.maloo.link_bug(
                request["suite_id"],
                request["jira_ticket"],
                buggable_class="TestSet",
                state="accepted",
            )
            self.store.finish_action(
                action.action_id, "waiting_external", result=result.to_dict()
            )
            self.store.finish_run(run.run_id, "waiting_external")
            self.store.append_timeline(
                run.run_id,
                "failure_link_requested",
                {"action_id": action.action_id, **result.to_dict()},
                idempotency_key="failure-link-requested:" + action.action_id,
            )
            return FailureAdvanceResult(
                run.run_id,
                "waiting_external",
                "link_requested",
                action.action_id,
            )
        except FailureActionError as exc:
            return self._stale(run, action, str(exc))
        except MalooAdapterError as exc:
            return self._mutation_error(run, action, exc)
        except Exception as exc:
            return self._definitive_error(run, action, "precondition_failed", str(exc))

    def _reconcile_link(
        self, run: AutomationRun, action: ActionAttempt, *, uncertain: bool
    ) -> Optional[FailureAdvanceResult]:
        try:
            self._fresh_revision(run)
            links = self.maloo.get_bug_links(action.request["suite_id"], related=False)
            state = self._link_state(links, action.request["jira_ticket"])
            if state == "accepted":
                return self._complete_link(run, action, links)
            if state == "pending":
                if action.status == "executing":
                    self.store.finish_action(
                        action.action_id, "waiting_external", result=links.to_dict()
                    )
                    self.store.finish_run(run.run_id, "waiting_external")
                return FailureAdvanceResult(
                    run.run_id,
                    "waiting_external",
                    "link_pending",
                    action.action_id,
                )
            if uncertain:
                return self._ambiguous(
                    run,
                    action,
                    "link_not_observed_after_restart",
                    "Bug association outcome is not remotely observable",
                )
            return FailureAdvanceResult(
                run.run_id, run.status, "link_not_yet_observed", action.action_id
            )
        except FailureActionError as exc:
            return self._stale(run, action, str(exc))
        except MalooAdapterError as exc:
            if uncertain:
                return self._ambiguous(run, action, exc.code, str(exc))
            return FailureAdvanceResult(
                run.run_id, run.status, "link_reconciliation_failed", action.action_id
            )

    def _complete_link(
        self, run: AutomationRun, action: ActionAttempt, links: MalooBugLinks
    ) -> FailureAdvanceResult:
        if action.status not in {"succeeded"}:
            self.store.finish_action(
                action.action_id, "succeeded", result=links.to_dict()
            )
        retest = self._plan_retest(run, self.store.get_action(action.action_id))
        if retest is None:
            return FailureAdvanceResult(run.run_id, "failed", "failed")
        self.store.append_timeline(
            run.run_id,
            "failure_link_accepted",
            {
                "action_id": action.action_id,
                "retest_action_id": retest.action_id,
                "jira_ticket": action.request["jira_ticket"],
            },
            idempotency_key="failure-link-accepted:" + action.action_id,
        )
        return FailureAdvanceResult(
            run.run_id,
            self.store.get_run(run.run_id).status,
            "waiting_approval",
            retest.action_id,
            "Bug association accepted; retest requires separate approval",
        )

    def _plan_retest(
        self, run: AutomationRun, link_action: ActionAttempt
    ) -> ActionAttempt | None:
        request = link_action.request
        key = "maloo-retest-after-link:" + request["workflow_fingerprint"]
        try:
            retest = self.store.plan_action(
                run.run_id,
                action_type=RETEST_ACTION,
                request={
                    **request,
                    "association_action_id": link_action.action_id,
                    "retest_option": "single",
                },
                idempotency_key=key,
            )
        except BudgetExhausted as exc:
            self.store.finish_run(
                run.run_id,
                "failed",
                failure_code="action_budget_exhausted",
                failure_summary=str(exc),
            )
            return None
        self.store.append_timeline(
            run.run_id,
            "failure_retest_planned",
            {"action_id": retest.action_id, "session_id": request["session_id"]},
            idempotency_key="failure-retest-planned:" + retest.action_id,
        )
        return retest

    def _execute_retest(
        self, run: AutomationRun, action: ActionAttempt
    ) -> FailureAdvanceResult:
        request = action.request
        try:
            if not self._authority_current(run, action.action_id):
                return self._authority_changed(run, action)
            self._fresh_revision(run)
            if not self._failure_target_current(request):
                return self._target_stale(run, action)
            links = self.maloo.get_bug_links(request["suite_id"], related=False)
            if self._link_state(links, request["jira_ticket"]) != "accepted":
                self.store.finish_action(
                    action.action_id, "waiting_external", result=links.to_dict()
                )
                self.store.finish_run(run.run_id, "waiting_external")
                return FailureAdvanceResult(
                    run.run_id,
                    "waiting_external",
                    "association_not_accepted",
                    action.action_id,
                )
            reconciliation = self._remote_retest(request)
            if reconciliation.already_requested:
                return self._complete_retest(run, action, reconciliation)
            if not self._authority_current(run, action.action_id):
                return self._authority_changed(run, action)
            result = self.maloo.request_retest(
                request["session_id"], request["jira_ticket"], option="single"
            )
            self.store.finish_action(
                action.action_id, "waiting_external", result=result.to_dict()
            )
            self.store.finish_run(run.run_id, "waiting_external")
            self.store.append_timeline(
                run.run_id,
                "failure_retest_requested",
                {"action_id": action.action_id, **result.to_dict()},
                idempotency_key="failure-retest-requested:" + action.action_id,
            )
            return FailureAdvanceResult(
                run.run_id,
                "waiting_external",
                "retest_requested",
                action.action_id,
            )
        except FailureActionError as exc:
            return self._stale(run, action, str(exc))
        except MalooAdapterError as exc:
            return self._mutation_error(run, action, exc)
        except Exception as exc:
            return self._definitive_error(run, action, "precondition_failed", str(exc))

    def _remote_retest(self, request: Mapping[str, Any]) -> MalooRetestReconciliation:
        return self.maloo.reconcile_remote_retest(
            change_number=request["change_number"],
            patchset=request["patchset"],
            revision_sha=request["revision_sha"],
            session_ref=request["session_id"],
            test_group=request["test_group"],
            jira_ticket=request["jira_ticket"],
        )

    def _reconcile_retest(
        self, run: AutomationRun, action: ActionAttempt, *, uncertain: bool
    ) -> FailureAdvanceResult:
        try:
            self._fresh_revision(run)
            links = self.maloo.get_bug_links(action.request["suite_id"], related=False)
            if self._link_state(links, action.request["jira_ticket"]) != "accepted":
                if uncertain:
                    return self._ambiguous(
                        run,
                        action,
                        "association_not_accepted",
                        "Retest execution cannot be reconciled without accepted bug link",
                    )
                return FailureAdvanceResult(
                    run.run_id,
                    run.status,
                    "association_not_accepted",
                    action.action_id,
                )
            reconciliation = self._remote_retest(action.request)
            if reconciliation.already_requested:
                return self._complete_retest(run, action, reconciliation)
            if uncertain:
                return self._ambiguous(
                    run,
                    action,
                    "retest_not_observed_after_restart",
                    "Retest outcome is not remotely observable",
                )
            return FailureAdvanceResult(
                run.run_id, run.status, "retest_not_yet_observed", action.action_id
            )
        except (FailureActionError, MalooAdapterError) as exc:
            if uncertain:
                code = getattr(exc, "code", "reconciliation_failed")
                return self._ambiguous(run, action, code, str(exc))
            return FailureAdvanceResult(
                run.run_id, run.status, "retest_reconciliation_failed", action.action_id
            )

    def _complete_retest(
        self,
        run: AutomationRun,
        action: ActionAttempt,
        reconciliation: MalooRetestReconciliation,
    ) -> FailureAdvanceResult:
        self.store.finish_action(
            action.action_id, "succeeded", result=reconciliation.to_dict()
        )
        settled = self.store.finish_run(run.run_id, "succeeded")
        self.store.append_timeline(
            run.run_id,
            "failure_retest_observed",
            {"action_id": action.action_id, **reconciliation.to_dict()},
            idempotency_key="failure-retest-observed:" + action.action_id,
        )
        return FailureAdvanceResult(
            run.run_id, settled.status, "complete", action.action_id
        )

    def _mutation_error(
        self, run: AutomationRun, action: ActionAttempt, exc: MalooAdapterError
    ) -> FailureAdvanceResult:
        if exc.ambiguous:
            return self._ambiguous(run, action, exc.code, str(exc))
        return self._definitive_error(run, action, exc.code, str(exc))

    def _ambiguous(
        self,
        run: AutomationRun,
        action: ActionAttempt,
        code: str,
        summary: str,
    ) -> FailureAdvanceResult:
        self.store.finish_action(
            action.action_id,
            "ambiguous",
            failure_code=code,
            failure_summary=summary,
        )
        settled = self.store.finish_run(
            run.run_id,
            "ambiguous",
            failure_code=code,
            failure_summary=summary,
        )
        return FailureAdvanceResult(
            run.run_id, settled.status, "ambiguous", action.action_id, summary
        )

    def _definitive_error(
        self,
        run: AutomationRun,
        action: ActionAttempt,
        code: str,
        summary: str,
    ) -> FailureAdvanceResult:
        self.store.finish_action(
            action.action_id,
            "failed",
            failure_code=code,
            failure_summary=summary,
        )
        settled = self.store.finish_run(
            run.run_id,
            "failed",
            failure_code=code,
            failure_summary=summary,
        )
        return FailureAdvanceResult(
            run.run_id, settled.status, "failed", action.action_id, summary
        )

    def _stale(
        self, run: AutomationRun, action: ActionAttempt, summary: str
    ) -> FailureAdvanceResult:
        self.store.finish_action(
            action.action_id,
            "cancelled",
            failure_code="stale_revision",
            failure_summary=summary,
        )
        settled = self.store.finish_run(
            run.run_id,
            "stale",
            failure_code="stale_revision",
            failure_summary=summary,
        )
        return FailureAdvanceResult(
            run.run_id, settled.status, "stale", action.action_id, summary
        )

    def _authority_changed(
        self, run: AutomationRun, action: ActionAttempt
    ) -> FailureAdvanceResult:
        summary = "Operator approval or patch policy changed before mutation"
        self.store.finish_action(
            action.action_id,
            "cancelled",
            failure_code="authority_changed",
            failure_summary=summary,
        )
        settled = self.store.finish_run(
            run.run_id,
            "cancelled",
            failure_code="authority_changed",
            failure_summary=summary,
        )
        return FailureAdvanceResult(
            run.run_id,
            settled.status,
            "authority_changed",
            action.action_id,
            summary,
        )

    def _target_stale(
        self, run: AutomationRun, action: ActionAttempt
    ) -> FailureAdvanceResult:
        summary = (
            "The exact Maloo session, test group, and failed suite are no longer "
            "an enforced failure"
        )
        self.store.finish_action(
            action.action_id,
            "cancelled",
            failure_code="maloo_failure_target_changed",
            failure_summary=summary,
        )
        settled = self.store.finish_run(
            run.run_id,
            "stale",
            failure_code="maloo_failure_target_changed",
            failure_summary=summary,
        )
        return FailureAdvanceResult(
            run.run_id, settled.status, "stale", action.action_id, summary
        )

    def _fail_run(
        self, run: AutomationRun, code: str, summary: str
    ) -> FailureAdvanceResult:
        settled = self.store.finish_run(
            run.run_id, "failed", failure_code=code, failure_summary=summary
        )
        return FailureAdvanceResult(run.run_id, settled.status, "failed", detail=summary)


__all__ = [
    "FailureActionController",
    "FailureActionError",
    "FailureAdvanceResult",
    "FailurePatchRevision",
    "FailureWritePlan",
    "LINK_ACTION",
    "RETEST_ACTION",
]
