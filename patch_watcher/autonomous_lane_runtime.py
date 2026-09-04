"""Adapter joining autonomous-lane admission to the existing retest engine.

This module never performs a remote write.  It converts the already-normalized
Gerrit/Maloo snapshot and established retest evaluation into a lane decision,
records that decision, and rechecks lane authority immediately before the
existing retest controller crosses its write boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

try:
    from .autonomous_lane import (
        BUILTIN_LANES,
        DETERMINISTIC_RETEST_LANE,
        DETERMINISTIC_RETEST_VERSION,
        LaneControlStore,
        LaneDecision,
        LaneDecisionHistory,
        LaneObservation,
        LaneRef,
        NormalizedTestFailure,
        RevisionIdentity,
        decide_lane,
    )
    from .retest_policy import RetestEvaluation, RevisionSnapshot, failure_fingerprint
except ImportError:  # pragma: no cover
    from autonomous_lane import (  # type: ignore
        BUILTIN_LANES,
        DETERMINISTIC_RETEST_LANE,
        DETERMINISTIC_RETEST_VERSION,
        LaneControlStore,
        LaneDecision,
        LaneDecisionHistory,
        LaneObservation,
        LaneRef,
        NormalizedTestFailure,
        RevisionIdentity,
        decide_lane,
    )
    from retest_policy import RetestEvaluation, RevisionSnapshot, failure_fingerprint  # type: ignore


@dataclass(frozen=True)
class LaneAdmission:
    enrolled: bool
    decision: LaneDecision | None


StandingPolicyLookup = Callable[[str], Any]


class AutonomousLaneRuntime:
    """Fail-closed admission overlay for enrolled patches only.

    Patches without a lane enrollment continue through the pre-existing policy
    path unchanged.  Once enrolled, all three lane switches, the standing
    policy, the primary global gate, and the established retest evaluator must
    agree before the same durable Maloo action can be planned.
    """

    def __init__(
        self,
        controls: LaneControlStore,
        history: LaneDecisionHistory,
        *,
        standing_policy: StandingPolicyLookup,
    ) -> None:
        self.controls = controls
        self.history = history
        self.standing_policy = standing_policy

    def is_enrolled(self, project: str, patch_id: str) -> bool:
        if not project or not patch_id:
            return False
        return self.controls.load().patch_control(project, patch_id) is not None

    def effective_enabled(self, project: str, patch_id: str) -> bool:
        if not project or not patch_id:
            return False
        controls = self.controls.load()
        project_control = controls.project_control(project)
        patch_control = controls.patch_control(project, patch_id)
        return bool(
            controls.global_enabled
            and project_control is not None and project_control.enabled
            and patch_control is not None and patch_control.enabled
            and patch_control.lane
            == LaneRef(DETERMINISTIC_RETEST_LANE, DETERMINISTIC_RETEST_VERSION)
        )

    def evaluate_retest(
        self,
        patch: Any,
        snapshot: RevisionSnapshot,
        evaluation: RetestEvaluation,
        *,
        primary_global_enabled: bool,
        actions_used: int,
        record: bool = True,
    ) -> LaneAdmission:
        project = str(getattr(patch, "project", "") or "")
        patch_id = str(getattr(patch, "patch_id", "") or "")
        controls = self.controls.load()
        if not project or controls.patch_control(project, patch_id) is None:
            return LaneAdmission(False, None)

        identity = RevisionIdentity(
            project,
            patch_id,
            int(snapshot.change_number),
            int(snapshot.patchset_number),
            str(snapshot.revision_sha),
        )
        # The collector binds the snapshot to the exact current PatchRevision.
        # A stale value is still represented by is_current and rejected by the
        # established evaluator before base_evaluation_permits can become true.
        current_identity = identity
        standing = self.standing_policy(patch_id)
        standing_authorized = bool(
            getattr(standing, "trigger_mode", "") == "automatic"
            and getattr(standing, "test_failures", "") == "deterministic"
        )
        ready = tuple(
            item for item in evaluation.decisions
            if item.outcome == "ready" and item.action is not None
            and item.action.execution_allowed
        )
        base_permits = evaluation.status == "ready" and len(ready) == 1
        failures = tuple(
            NormalizedTestFailure(
                item.suite,
                item.test_group,
                failure_fingerprint(snapshot, item),
                "deterministic"
                if item.details_complete
                and item.bug_links_complete
                and any(link.accepted_for_retest for link in item.linked_bugs)
                else "unknown",
            )
            for item in snapshot.maloo_failures
            if item.enforced
        )
        evidence_id = "maloo-" + hashlib.sha256(
            (ready[0].session_id if ready else "evaluation").encode("utf-8")
        ).hexdigest()[:24]
        evidence_fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                evaluation.to_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        observation = LaneObservation(
            identity=identity,
            current_identity=current_identity,
            evidence_kind="test_failure",
            evidence_id=evidence_id,
            evidence_fingerprint=evidence_fingerprint,
            source="maloo",
            change_state=snapshot.lifecycle,
            failures=failures,
            standing_policy_authorized=standing_authorized,
            primary_global_enabled=bool(primary_global_enabled),
            base_evaluation_permits=base_permits,
            actions_used=actions_used,
            non_maloo_minus_one=any(vote.is_non_maloo_veto for vote in snapshot.review_votes),
        )
        decision = decide_lane(observation, controls)
        if record:
            self.history.append(observation, controls, decision)
        return LaneAdmission(True, decision)

    def authorize_request(
        self,
        request: Mapping[str, Any],
        fresh_patch: Any,
        *,
        primary_global_enabled: bool,
        policy_mode: str,
    ) -> bool:
        """Recheck every lane switch and fixed capability before a remote write."""

        metadata = request.get("autonomous_lane")
        if not isinstance(metadata, Mapping):
            return True  # Legacy non-lane action; existing checks still apply.
        if metadata.get("name") != DETERMINISTIC_RETEST_LANE:
            return False
        if metadata.get("version") != DETERMINISTIC_RETEST_VERSION:
            return False
        if metadata.get("definition_digest") != BUILTIN_LANES[0].definition_digest:
            return False
        if metadata.get("capability") != "request_retest":
            return False
        project = str(metadata.get("project") or "")
        patch_id = str(metadata.get("patch_id") or "")
        controls = self.controls.load()
        patch_control = controls.patch_control(project, patch_id)
        project_control = controls.project_control(project)
        standing = self.standing_policy(patch_id)
        return bool(
            primary_global_enabled
            and policy_mode == "automatic"
            and controls.global_enabled
            and project_control is not None and project_control.enabled
            and patch_control is not None and patch_control.enabled
            and patch_control.lane
            == LaneRef(DETERMINISTIC_RETEST_LANE, DETERMINISTIC_RETEST_VERSION)
            and getattr(standing, "trigger_mode", "") == "automatic"
            and getattr(standing, "test_failures", "") == "deterministic"
            and str(getattr(fresh_patch, "project", "") or "") == project
            and str(getattr(fresh_patch, "patch_id", "") or "") == patch_id
        )

    def replay(self):
        return self.history.replay()
