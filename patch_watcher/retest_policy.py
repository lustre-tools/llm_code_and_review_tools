"""Pure deterministic policy for Phase 1 Maloo retest decisions.

This module performs no I/O and grants no authority.  It consumes a normalized
snapshot and returns a dry-run decision that the Patch Watcher controller may
persist and, after revalidation, execute through its action outbox.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote


ACTION_MODES = frozenset({"disabled", "advise", "approval", "automatic"})
LIFECYCLE_STATES = frozenset({"open", "merged", "abandoned", "unknown"})
ACTION_TYPE = "request_maloo_retest"
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _require_text(value: str, field: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("%s must be a non-empty bounded string" % field)
    return value.strip()


def _canonical_hash(kind: str, value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"kind": kind, **value}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclasses.dataclass(frozen=True)
class ReviewVote:
    reviewer: str
    label: str
    value: int
    source: str = "human"
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewer", _require_text(self.reviewer, "reviewer", 256))
        object.__setattr__(self, "label", _require_text(self.label, "label", 128))
        source = _require_text(self.source, "source", 64).lower()
        if source not in {"human", "bot", "maloo"}:
            raise ValueError("source must be human, bot, or maloo")
        object.__setattr__(self, "source", source)
        if not isinstance(self.value, int) or isinstance(self.value, bool) or not -2 <= self.value <= 2:
            raise ValueError("review value must be an integer from -2 through 2")
        if not isinstance(self.message, str) or len(self.message) > 2000:
            raise ValueError("review message is too large")

    @property
    def is_non_maloo_veto(self) -> bool:
        return self.label.casefold() == "code-review" and self.value < 0 and self.source != "maloo"


@dataclasses.dataclass(frozen=True)
class JiraBugLink:
    issue_key: str
    accepted_for_retest: bool
    evidence: str = ""

    def __post_init__(self) -> None:
        key = _require_text(self.issue_key, "issue_key", 64).upper()
        if not JIRA_KEY_RE.fullmatch(key):
            raise ValueError("issue_key must be a Jira issue key")
        object.__setattr__(self, "issue_key", key)
        if not isinstance(self.accepted_for_retest, bool):
            raise ValueError("accepted_for_retest must be boolean")
        if not isinstance(self.evidence, str) or len(self.evidence) > 2000:
            raise ValueError("bug-link evidence is too large")


@dataclasses.dataclass(frozen=True)
class MalooFailure:
    session_id: str
    test_group: str
    suite: str
    enforced: bool
    linked_bugs: Tuple[JiraBugLink, ...] = ()
    failing_subtests: Tuple[str, ...] = ()
    details_complete: bool = True
    bug_links_complete: bool = True
    remote_failure_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _require_text(self.session_id, "session_id", 256))
        object.__setattr__(self, "test_group", _require_text(self.test_group, "test_group", 256))
        object.__setattr__(self, "suite", _require_text(self.suite, "suite", 512))
        if not isinstance(self.enforced, bool):
            raise ValueError("enforced must be boolean")
        if not isinstance(self.details_complete, bool) or not isinstance(self.bug_links_complete, bool):
            raise ValueError("failure completeness fields must be boolean")
        links = tuple(self.linked_bugs)
        if len(links) > 100 or any(not isinstance(link, JiraBugLink) for link in links):
            raise ValueError("linked_bugs must contain at most 100 JiraBugLink values")
        object.__setattr__(self, "linked_bugs", links)
        subtests = tuple(sorted({_require_text(item, "failing_subtest", 512) for item in self.failing_subtests}))
        if len(subtests) > 500:
            raise ValueError("too many failing_subtests")
        object.__setattr__(self, "failing_subtests", subtests)
        if not isinstance(self.remote_failure_id, str) or len(self.remote_failure_id) > 256:
            raise ValueError("remote_failure_id is too large")


@dataclasses.dataclass(frozen=True)
class PendingRetest:
    session_id: str
    test_group: str
    remote_request_id: str
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _require_text(self.session_id, "session_id", 256))
        object.__setattr__(self, "test_group", _require_text(self.test_group, "test_group", 256))
        object.__setattr__(
            self, "remote_request_id", _require_text(self.remote_request_id, "remote_request_id", 256)
        )
        if not isinstance(self.active, bool):
            raise ValueError("active must be boolean")


@dataclasses.dataclass(frozen=True)
class RevisionSnapshot:
    gerrit_server: str
    change_number: int
    patchset_number: int
    revision_sha: str
    lifecycle: str
    is_current: bool
    revision_state_complete: bool
    maloo_state_complete: bool
    review_votes: Tuple[ReviewVote, ...] = ()
    maloo_failures: Tuple[MalooFailure, ...] = ()
    pending_retests: Tuple[PendingRetest, ...] = ()

    def __post_init__(self) -> None:
        server = _require_text(self.gerrit_server, "gerrit_server", 2048).rstrip("/").lower()
        if not (server.startswith("https://") or server.startswith("http://")):
            raise ValueError("gerrit_server must be an HTTP(S) URL")
        object.__setattr__(self, "gerrit_server", server)
        if not isinstance(self.change_number, int) or isinstance(self.change_number, bool) or self.change_number <= 0:
            raise ValueError("change_number must be positive")
        if not isinstance(self.patchset_number, int) or isinstance(self.patchset_number, bool) or self.patchset_number <= 0:
            raise ValueError("patchset_number must be positive")
        sha = _require_text(self.revision_sha, "revision_sha", 64).lower()
        if not SHA_RE.fullmatch(sha):
            raise ValueError("revision_sha must be a full hexadecimal revision")
        object.__setattr__(self, "revision_sha", sha)
        lifecycle = _require_text(self.lifecycle, "lifecycle", 32).lower()
        if lifecycle not in LIFECYCLE_STATES:
            raise ValueError("unsupported lifecycle")
        object.__setattr__(self, "lifecycle", lifecycle)
        for field in ("is_current", "revision_state_complete", "maloo_state_complete"):
            if not isinstance(getattr(self, field), bool):
                raise ValueError("%s must be boolean" % field)
        votes = tuple(self.review_votes)
        failures = tuple(self.maloo_failures)
        pending = tuple(self.pending_retests)
        if any(not isinstance(value, ReviewVote) for value in votes):
            raise ValueError("review_votes contains an invalid value")
        if any(not isinstance(value, MalooFailure) for value in failures):
            raise ValueError("maloo_failures contains an invalid value")
        if any(not isinstance(value, PendingRetest) for value in pending):
            raise ValueError("pending_retests contains an invalid value")
        object.__setattr__(self, "review_votes", votes)
        object.__setattr__(self, "maloo_failures", failures)
        object.__setattr__(self, "pending_retests", pending)


@dataclasses.dataclass(frozen=True)
class RetestPolicy:
    mode: str
    global_execution_enabled: bool
    policy_version: str
    max_new_actions: int = 1

    def __post_init__(self) -> None:
        mode = _require_text(self.mode, "mode", 32).lower()
        if mode not in ACTION_MODES:
            raise ValueError("unsupported retest policy mode")
        object.__setattr__(self, "mode", mode)
        if not isinstance(self.global_execution_enabled, bool):
            raise ValueError("global_execution_enabled must be boolean")
        object.__setattr__(self, "policy_version", _require_text(self.policy_version, "policy_version", 128))
        if (
            not isinstance(self.max_new_actions, int)
            or isinstance(self.max_new_actions, bool)
            or self.max_new_actions < 0
            or self.max_new_actions > 100
        ):
            raise ValueError("max_new_actions must be between 0 and 100")


@dataclasses.dataclass(frozen=True)
class RetestBudget:
    max_actions: int
    actions_used: int
    existing_action_keys: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_actions, int)
            or isinstance(self.max_actions, bool)
            or self.max_actions < 0
        ):
            raise ValueError("max_actions must not be negative")
        if (
            not isinstance(self.actions_used, int)
            or isinstance(self.actions_used, bool)
            or not 0 <= self.actions_used <= self.max_actions
        ):
            raise ValueError("actions_used must be within the action budget")
        keys = frozenset(_require_text(key, "existing_action_key", 2048) for key in self.existing_action_keys)
        object.__setattr__(self, "existing_action_keys", keys)

    @property
    def remaining(self) -> int:
        return self.max_actions - self.actions_used


@dataclasses.dataclass(frozen=True)
class RetestActionPreview:
    action_type: str
    action_key: str
    action_fingerprint: str
    change_number: int
    revision_sha: str
    session_id: str
    test_groups: Tuple[str, ...]
    jira_justification: str
    all_linked_bug_keys: Tuple[str, ...]
    linked_bug_evidence: Tuple[JiraBugLink, ...]
    execution_allowed: bool


@dataclasses.dataclass(frozen=True)
class RetestDecision:
    session_id: str
    test_groups: Tuple[str, ...]
    suites: Tuple[str, ...]
    failure_fingerprints: Tuple[str, ...]
    trigger_fingerprint: str
    linked_bug_keys: Tuple[str, ...]
    outcome: str
    reason_code: str
    reason: str
    action: Optional[RetestActionPreview] = None


@dataclasses.dataclass(frozen=True)
class RetestEvaluation:
    change_number: int
    patchset_number: int
    revision_sha: str
    status: str
    reason_code: str
    reason: str
    decisions: Tuple[RetestDecision, ...] = ()
    dry_run: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def failure_fingerprint(snapshot: RevisionSnapshot, failure: MalooFailure) -> str:
    """Return the stable identity of one normalized failed suite."""

    return _canonical_hash(
        "maloo_failure/v1",
        {
            "gerrit_server": snapshot.gerrit_server,
            "change_number": snapshot.change_number,
            "revision_sha": snapshot.revision_sha,
            "session_id": failure.session_id,
            "test_group": failure.test_group,
            "suite": failure.suite,
            "remote_failure_id": failure.remote_failure_id,
            "failing_subtests": list(failure.failing_subtests),
        },
    )


def _group_trigger_fingerprint(
    snapshot: RevisionSnapshot, session_id: str
) -> str:
    return _canonical_hash(
        "maloo_retest_trigger/v1",
        {
            "gerrit_server": snapshot.gerrit_server,
            "change_number": snapshot.change_number,
            "revision_sha": snapshot.revision_sha,
            "session_id": session_id,
        },
    )


def _action_key(snapshot: RevisionSnapshot, session_id: str) -> str:
    return "maloo-retest:%d:%s:%s" % (
        snapshot.change_number,
        snapshot.revision_sha,
        quote(session_id, safe=""),
    )


def _action_preview(
    snapshot: RevisionSnapshot,
    session_id: str,
    test_groups: Tuple[str, ...],
    accepted_bug_keys: Tuple[str, ...],
    linked_bug_evidence: Tuple[JiraBugLink, ...],
    execution_allowed: bool,
) -> RetestActionPreview:
    action_key = _action_key(snapshot, session_id)
    justification = accepted_bug_keys[0]
    all_linked_bug_keys = tuple(sorted({link.issue_key for link in linked_bug_evidence}))
    fingerprint = _canonical_hash(
        "maloo_retest_action/v1",
        {
            "action_key": action_key,
            "jira_justification": justification,
            "linked_bug_evidence": [dataclasses.asdict(link) for link in linked_bug_evidence],
        },
    )
    return RetestActionPreview(
        action_type=ACTION_TYPE,
        action_key=action_key,
        action_fingerprint=fingerprint,
        change_number=snapshot.change_number,
        revision_sha=snapshot.revision_sha,
        session_id=session_id,
        test_groups=test_groups,
        jira_justification=justification,
        all_linked_bug_keys=all_linked_bug_keys,
        linked_bug_evidence=linked_bug_evidence,
        execution_allowed=execution_allowed,
    )


def _global_result(snapshot: RevisionSnapshot, code: str, reason: str) -> RetestEvaluation:
    return RetestEvaluation(
        snapshot.change_number,
        snapshot.patchset_number,
        snapshot.revision_sha,
        "suppressed",
        code,
        reason,
    )


def _group_failures(failures: Iterable[MalooFailure]) -> Tuple[Tuple[str, Tuple[MalooFailure, ...]], ...]:
    grouped: Dict[str, list] = {}
    for failure in failures:
        if not failure.enforced:
            continue
        grouped.setdefault(failure.session_id, []).append(failure)
    values = []
    for session_id, group in grouped.items():
        ordered = tuple(
            sorted(
                group,
                key=lambda item: (
                    item.test_group,
                    item.suite,
                    item.remote_failure_id,
                    item.failing_subtests,
                ),
            )
        )
        values.append((session_id, ordered))
    return tuple(sorted(values, key=lambda item: item[0]))


def _top_status(decisions: Sequence[RetestDecision]) -> Tuple[str, str, str]:
    if len(decisions) == 1:
        decision = decisions[0]
        return decision.outcome, decision.reason_code, decision.reason
    priorities = (
        ("ready", "action_ready", "At least one deterministic retest action is ready."),
        ("waiting_approval", "approval_required", "At least one retest is waiting for explicit approval."),
        ("advice", "advice_only", "Retest recommendations are available in advice-only mode."),
        ("investigate", "investigate_phase_2", "Unknown failures were preserved for Phase 2 investigation."),
        ("waiting_external", "pending_retest", "At least one matching retest is already pending."),
        ("suppressed", "action_suppressed", "Retest candidates were suppressed by safety policy."),
    )
    outcomes = {decision.outcome for decision in decisions}
    for outcome, code, reason in priorities:
        if outcome in outcomes:
            return outcome, code, reason
    return "no_action", "no_action", "No deterministic retest action is needed."


def evaluate_retests(
    snapshot: RevisionSnapshot, policy: RetestPolicy, budget: RetestBudget
) -> RetestEvaluation:
    """Evaluate the current revision and return a deterministic dry-run plan."""

    if not isinstance(snapshot, RevisionSnapshot):
        raise TypeError("snapshot must be RevisionSnapshot")
    if not isinstance(policy, RetestPolicy):
        raise TypeError("policy must be RetestPolicy")
    if not isinstance(budget, RetestBudget):
        raise TypeError("budget must be RetestBudget")

    if not snapshot.revision_state_complete or snapshot.lifecycle == "unknown":
        return _global_result(
            snapshot,
            "revision_unknown",
            "Current Gerrit revision state is incomplete; no Maloo action can be planned.",
        )
    if not snapshot.is_current:
        return _global_result(
            snapshot,
            "stale_revision",
            "Patchset %d revision %s is not current; no retest is allowed."
            % (snapshot.patchset_number, snapshot.revision_sha),
        )
    if snapshot.lifecycle in {"merged", "abandoned"}:
        return _global_result(
            snapshot,
            "terminal_change",
            "Change %d is %s; terminal changes cannot be retested."
            % (snapshot.change_number, snapshot.lifecycle),
        )

    vetoes = tuple(
        sorted(
            (vote for vote in snapshot.review_votes if vote.is_non_maloo_veto),
            key=lambda vote: (vote.reviewer.casefold(), vote.value, vote.message),
        )
    )
    if vetoes:
        visible = ", ".join("%s (%+d)" % (vote.reviewer, vote.value) for vote in vetoes)
        return _global_result(
            snapshot,
            "non_maloo_review_veto",
            "Current patchset %d has a non-Maloo Code-Review veto from %s; the Maloo flow was not evaluated."
            % (snapshot.patchset_number, visible),
        )
    if policy.mode == "disabled":
        return _global_result(
            snapshot,
            "policy_disabled",
            "Test-error handling is disabled for this patch; no retest preview was created.",
        )
    if not snapshot.maloo_state_complete:
        return _global_result(
            snapshot,
            "maloo_state_unknown",
            "Current Maloo failure or pending-retest state is incomplete; no retest can be planned.",
        )

    grouped = _group_failures(snapshot.maloo_failures)
    if not grouped:
        return RetestEvaluation(
            snapshot.change_number,
            snapshot.patchset_number,
            snapshot.revision_sha,
            "no_action",
            "no_enforced_failures",
            "No enforced Maloo failures exist on the current revision.",
        )

    active_pending: Dict[str, PendingRetest] = {}
    for pending in sorted(
        (value for value in snapshot.pending_retests if value.active),
        key=lambda value: (value.session_id, value.remote_request_id, value.test_group),
    ):
        active_pending.setdefault(pending.session_id, pending)
    decisions = []
    planned_count = 0
    for session_id, failures in grouped:
        fingerprints = tuple(sorted(failure_fingerprint(snapshot, failure) for failure in failures))
        suites = tuple(sorted({failure.suite for failure in failures}))
        test_groups = tuple(sorted({failure.test_group for failure in failures}))
        trigger = _group_trigger_fingerprint(snapshot, session_id)
        all_bug_keys = tuple(
            sorted({link.issue_key for failure in failures for link in failure.linked_bugs})
        )
        base = {
            "session_id": session_id,
            "test_groups": test_groups,
            "suites": suites,
            "failure_fingerprints": fingerprints,
            "trigger_fingerprint": trigger,
            "linked_bug_keys": all_bug_keys,
        }

        pending = active_pending.get(session_id)
        if pending is not None:
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="waiting_external",
                    reason_code="pending_retest",
                    reason="Maloo retest %s is already pending for session %s."
                    % (pending.remote_request_id, session_id),
                )
            )
            continue

        incomplete = [
            failure.suite
            for failure in failures
            if not failure.details_complete or not failure.bug_links_complete
        ]
        missing_accepted = [
            failure.suite
            for failure in failures
            if not any(link.accepted_for_retest for link in failure.linked_bugs)
        ]
        if incomplete or missing_accepted:
            affected = tuple(sorted(set(incomplete + missing_accepted)))
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="investigate",
                    reason_code="investigate_phase_2",
                    reason=(
                        "Session %s is not safe to retest because enforced suite(s) %s "
                        "lack complete accepted Jira-bug evidence; preserve the failure for Phase 2 investigation."
                        % (session_id, ", ".join(affected))
                    ),
                )
            )
            continue

        accepted_bug_keys = tuple(
            sorted(
                {
                    link.issue_key
                    for failure in failures
                    for link in failure.linked_bugs
                    if link.accepted_for_retest
                }
            )
        )
        linked_bug_evidence = tuple(
            JiraBugLink(key, accepted, evidence)
            for key, accepted, evidence in sorted(
                {
                    (link.issue_key, link.accepted_for_retest, link.evidence)
                    for failure in failures
                    for link in failure.linked_bugs
                }
            )
        )
        # At least one accepted key per suite above guarantees a non-empty union.
        action_key = _action_key(snapshot, session_id)
        if action_key in budget.existing_action_keys:
            action = _action_preview(
                snapshot, session_id, test_groups, accepted_bug_keys, linked_bug_evidence,
                execution_allowed=False
            )
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="waiting_external",
                    reason_code="action_already_recorded",
                    reason="The idempotent retest action is already recorded; repeated observation created no duplicate.",
                    action=action,
                )
            )
            continue
        if budget.remaining <= planned_count:
            action = _action_preview(
                snapshot, session_id, test_groups, accepted_bug_keys, linked_bug_evidence,
                execution_allowed=False
            )
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="suppressed",
                    reason_code="action_budget_exhausted",
                    reason="The external-action budget has no remaining slot for this retest.",
                    action=action,
                )
            )
            continue
        if planned_count >= policy.max_new_actions:
            action = _action_preview(
                snapshot, session_id, test_groups, accepted_bug_keys, linked_bug_evidence,
                execution_allowed=False
            )
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="suppressed",
                    reason_code="evaluation_action_limit",
                    reason="This evaluation already reached its deterministic new-action limit.",
                    action=action,
                )
            )
            continue

        if policy.mode == "advise":
            action = _action_preview(
                snapshot, session_id, test_groups, accepted_bug_keys, linked_bug_evidence,
                execution_allowed=False
            )
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="advice",
                    reason_code="advice_only",
                    reason="A session-level retest is recommended, but advice mode cannot execute it.",
                    action=action,
                )
            )
            planned_count += 1
            continue
        if policy.mode == "approval":
            action = _action_preview(
                snapshot, session_id, test_groups, accepted_bug_keys, linked_bug_evidence,
                execution_allowed=False
            )
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="waiting_approval",
                    reason_code="approval_required",
                    reason=(
                        "The exact session-level retest is prepared but requires explicit "
                        "operator approval. The global gate applies only to automatic actions."
                    ),
                    action=action,
                )
            )
            planned_count += 1
            continue
        if not policy.global_execution_enabled:
            action = _action_preview(
                snapshot, session_id, test_groups, accepted_bug_keys, linked_bug_evidence,
                execution_allowed=False
            )
            decisions.append(
                RetestDecision(
                    **base,
                    outcome="suppressed",
                    reason_code="global_execution_disabled",
                    reason="Automatic mode selected this retest, but global execution is disabled; preview only.",
                    action=action,
                )
            )
            planned_count += 1
            continue

        action = _action_preview(
            snapshot, session_id, test_groups, accepted_bug_keys, linked_bug_evidence,
            execution_allowed=True
        )
        decisions.append(
            RetestDecision(
                **base,
                outcome="ready",
                reason_code="automatic_retest_ready",
                reason=(
                    "Every enforced failed suite in session %s has accepted Jira evidence; "
                    "one idempotent session-level retest is ready."
                    % session_id
                ),
                action=action,
            )
        )
        planned_count += 1

    decisions_tuple = tuple(decisions)
    status, code, reason = _top_status(decisions_tuple)
    return RetestEvaluation(
        snapshot.change_number,
        snapshot.patchset_number,
        snapshot.revision_sha,
        status,
        code,
        reason,
        decisions_tuple,
    )
