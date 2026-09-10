#!/usr/bin/env python3
"""Small, dependency-free Patch Watcher web application."""
import argparse
import contextlib
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from patch_watcher.automation_state import (
    AutomationConflict,
    AutomationNotFound,
    AutomationStateStore,
)
from patch_watcher.autonomous_lane import (
    DETERMINISTIC_RETEST_LANE,
    DETERMINISTIC_RETEST_VERSION,
    AutonomousLaneConflict,
    AutonomousLaneError,
    LaneControlStore,
    LaneDecisionHistory,
    LaneRef,
)
from patch_watcher.autonomous_lane_runtime import AutonomousLaneRuntime
from patch_watcher.build_views import (
    render_build_result,
    render_build_start_confirmation,
    render_build_start_control,
)
from patch_watcher.engineering_views import (
    render_capability_status,
    render_engineering_confirmation,
    render_engineering_run,
    render_engineering_start_confirmation,
    render_engineering_start_control,
    render_unmatched_resources,
)
from patch_watcher.failure_actions import (
    LINK_ACTION as FAILURE_LINK_ACTION,
)
from patch_watcher.failure_actions import (
    RETEST_ACTION as FAILURE_RETEST_ACTION,
)
from patch_watcher.failure_actions import (
    FailureActionController,
    FailureActionError,
)
from patch_watcher.gerrit_status import (
    GerritConfig,
    GerritConfigError,
    GerritRequestError,
    GerritStatusClient,
    parse_change_number,
    refresh_patch,
)
from patch_watcher.jenkins_adapter import JenkinsSnapshotClient, JenkinsSnapshotError
from patch_watcher.lane_views import render_autonomous_lane_summary
from patch_watcher.ltvm_resources import (
    LTVMAdapter,
    LTVMCommandError,
    checkout_vm_prefix,
    owner_id_for_session,
)
from patch_watcher.maloo_adapter import MalooAdapter
from patch_watcher.observer import BackgroundObserver
from patch_watcher.reporting import (
    compose_gerrit_help_message,
    log_structured_error,
    send_automation_alert,
    send_daily_summary,
    send_human_notice,
    send_session_alert,
)
from patch_watcher.research_views import (
    render_action_approval_card as render_failure_approval_card,
)
from patch_watcher.research_views import (
    render_action_confirmation as render_failure_action_confirmation,
)
from patch_watcher.research_views import (
    render_failure_action_status,
    render_research_policy_confirmation,
    render_research_policy_form,
    render_research_session,
    render_unknown_failure_control,
)
from patch_watcher.resource_status import collect_process_tree_rss, collect_resource_snapshot
from patch_watcher.resource_views import render_resource_dashboard
from patch_watcher.retest_controller import ControllerNotification, RetestController
from patch_watcher.retest_views import (
    render_action_confirmation,
    render_enable_confirmation,
    render_global_retest_status,
    render_policy_confirmation,
    render_retest_control,
)
from patch_watcher.review_views import (
    render_review_result,
    render_review_start_confirmation,
    render_review_start_control,
)
from patch_watcher.run_controller import (
    BUILD_FAILURE_REQUEST_EVENT,
    CHECKOUT_ALLOCATED_EVENT,
    ENGINEERING_REQUEST_EVENT,
    RESEARCH_REQUEST_EVENT,
    REVIEW_REQUEST_EVENT,
    RUNNER_HANDLE_EVENT,
    NoReviewTargets,
    RunController,
    RunControllerError,
    normalize_unknown_failure_evidence,
    unknown_failure_research_run_id,
)
from patch_watcher.run_views import (
    render_destructive_confirmation,
    render_investigate_control,
    render_run_detail,
    render_run_summary,
)
from patch_watcher.session_state import (
    ABSOLUTE_RUNTIME_CAP,
    ENGINEERING_INACTIVITY_LIMIT,
    TRIAGE_WALL_LIMIT,
    InvalidSessionOperation,
    SessionAlreadyExists,
    SessionNotFound,
    SessionStateStore,
)
from patch_watcher.standing_policy import (
    PRESET_LABELS,
    PRESET_LEVELS,
    PRESET_SUMMARIES,
    ActivePatchRun,
    PatchAutomationPolicy,
    RevisionIdentity,
    StandingPolicyConflict,
    StandingPolicyError,
    StandingPolicyStore,
    TriggerObservation,
    decide_trigger,
)
from patch_watcher.workspace import CheckoutPool, CheckoutPoolError

PATCHES = []
DEFAULT_SEED_FILE = Path.home() / ".config" / "patch-watcher" / "patches.txt"
DEFAULT_SESSION_DATABASE = (
    Path.home() / ".local" / "state" / "patch-watcher" / "sessions.sqlite3"
)
DEFAULT_AUTOMATION_DATABASE = (
    Path.home() / ".local" / "state" / "patch-watcher" / "automation.sqlite3"
)
DEFAULT_STANDING_POLICY_FILE = (
    Path.home() / ".config" / "patch-watcher" / "standing-policies.json"
)
DEFAULT_AUTONOMOUS_LANE_FILE = (
    Path.home() / ".config" / "patch-watcher" / "autonomous-lanes.json"
)
DEFAULT_AUTONOMOUS_LANE_HISTORY = (
    Path.home() / ".local" / "state" / "patch-watcher" / "autonomous-lanes.jsonl"
)
ACTIVE_WATCH_FILE = DEFAULT_SEED_FILE
ACTIVE_SESSION_DATABASE = DEFAULT_SESSION_DATABASE
ACTIVE_AUTOMATION_DATABASE = DEFAULT_AUTOMATION_DATABASE
JIRA_BASE_URL = "https://jira.whamcloud.com/browse"
SESSION_STORE = None
RUN_CONTROLLER = None
AUTOMATION_STORE = None
RETEST_CONTROLLER = None
FAILURE_ACTION_CONTROLLER = None
STANDING_POLICY_STORE = None
AUTONOMOUS_LANE_STORE = None
AUTONOMOUS_LANE_HISTORY = None
AUTONOMOUS_LANE_RUNTIME = None
JENKINS_SNAPSHOT_CLIENT = JenkinsSnapshotClient()
AUTOMATION_OBSERVER = None
PATCHES_LOCK = threading.RLock()
CSRF_TOKEN = secrets.token_urlsafe(32)
# The confirmation-signing key is deliberately SEPARATE from CSRF_TOKEN and is
# never rendered. They were the same value, and CSRF_TOKEN is emitted as a
# hidden field in every page -- so anyone who could read one page body held the
# signing key and could mint a valid confirmation for any purpose, with
# attacker-chosen values and expiry. That reduced the whole prepare -> confirm
# -> start flow to the CSRF check it already had.
_CONFIRMATION_KEY = secrets.token_bytes(32)
MAX_FORM_BODY_BYTES = 64 * 1024
MAX_FORM_FIELDS = 128
MALOO_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,199}")
# Host memory and LTVM inventory collection. This was False and nothing in
# production ever set it True, so the entire resource surface rendered
# "unknown" forever: no VM inventory, a "Refresh resource status" button that
# did nothing, and -- worse -- the cleanup/orphan warning paths derive from the
# inventory, so an abandoned VM reported "none reported" rather than a problem.
# The collector is cheap (measured ~0.1s, cached for RESOURCE_CACHE_SECONDS)
# and degrades to recorded collection errors when ltvm is absent.
RESOURCE_COLLECTION_ENABLED = True
RESOURCE_CACHE_SECONDS = 15
_RESOURCE_SNAPSHOT = None
_RESOURCE_SNAPSHOT_MONOTONIC = 0.0
ENGINEERING_CONFIRMATION_TTL_SECONDS = 60 * 60
ENGINEERING_CONFIRMATION_MAX_ENTRIES = 4096
_ENGINEERING_CONFIRMATION_LOCK = threading.Lock()
_ENGINEERING_USED_CONFIRMATIONS = {}
ENGINEERING_RETRYABLE_STATES = {
    "succeeded", "failed", "cancelled", "stale", "resource_exhausted",
}
# (title, what confirming will do, back-link label) for the FIRST review page
# of each run-control intent. "Keep session running" is wrong for a retry on a
# terminal run, where nothing is running to keep.
RUN_INTENT_REVIEW = {
    "cancel": (
        "Review stop and cancel",
        "Confirming on the next page requests an orderly stop, marks the run "
        "cancelled, collects available evidence, and begins cleanup of the "
        "resources this run owns.",
        "← Keep session running",
    ),
    "kill": (
        "Review kill session",
        "Confirming on the next page forcibly stops the Claude process, "
        "cancels the run, collects whatever evidence exists, and begins "
        "cleanup of the resources this run owns.",
        "← Keep session running",
    ),
    "retry": (
        "Review retry as a new run",
        "Confirming on the next page starts a NEW isolated engineering run at "
        "the patch's exact current revision. It does not revive this finished "
        "run's checkout, session, or its VMs.",
        "← Back to this finished run",
    ),
}


def initialize_automation_store(database=DEFAULT_AUTOMATION_DATABASE):
    """Open the private durable deterministic-automation ledger."""
    global AUTOMATION_STORE, ACTIVE_AUTOMATION_DATABASE
    ACTIVE_AUTOMATION_DATABASE = Path(database)
    AUTOMATION_STORE = AutomationStateStore(ACTIVE_AUTOMATION_DATABASE)
    return AUTOMATION_STORE


def initialize_standing_policy_store(path=DEFAULT_STANDING_POLICY_FILE):
    """Open the private per-patch standing-policy document."""

    global STANDING_POLICY_STORE
    STANDING_POLICY_STORE = StandingPolicyStore(path)
    return STANDING_POLICY_STORE


def initialize_autonomous_lanes(
    path=DEFAULT_AUTONOMOUS_LANE_FILE,
    history_path=DEFAULT_AUTONOMOUS_LANE_HISTORY,
):
    """Open the disabled-by-default lane controls and append-only audit."""

    global AUTONOMOUS_LANE_STORE, AUTONOMOUS_LANE_HISTORY, AUTONOMOUS_LANE_RUNTIME
    AUTONOMOUS_LANE_STORE = LaneControlStore(path)
    AUTONOMOUS_LANE_HISTORY = LaneDecisionHistory(history_path)
    AUTONOMOUS_LANE_RUNTIME = AutonomousLaneRuntime(
        AUTONOMOUS_LANE_STORE,
        AUTONOMOUS_LANE_HISTORY,
        standing_policy=lambda patch_id: (
            STANDING_POLICY_STORE.get(patch_id)
            if STANDING_POLICY_STORE is not None
            else PatchAutomationPolicy(patch_id)
        ),
    )
    return AUTONOMOUS_LANE_RUNTIME


def _fresh_patch_revision(gerrit_url):
    """Fetch the exact current revision immediately before an external write."""
    status = GerritStatusClient.configured().fetch(gerrit_url)
    return RetestController._coerce_patch({
        **status,
        "patch_id": str(status.get("change_number") or ""),
        "gerrit_url": gerrit_url,
        "url": gerrit_url,
        "is_current": True,
        "revision_state_complete": bool(
            status.get("revision_sha") and status.get("patchset")
        ),
    })


def _signed_confirmation(purpose, *values):
    """Bind a confirmation to one exact, currently displayed proposal."""
    payload = json.dumps(
        [str(purpose), *(str(value) for value in values)],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(_CONFIRMATION_KEY, payload, hashlib.sha256).hexdigest()


def _verify_confirmation(token, purpose, *values):
    expected = _signed_confirmation(purpose, *values)
    return bool(token) and hmac.compare_digest(str(token), expected)


def _claim_engineering_confirmation(token, idempotency_token, *, now=None):
    """Atomically consume one exact engineering-start confirmation.

    The durable session-store uniqueness constraint remains the authoritative
    concurrency boundary.  This bounded process-local ledger prevents an
    ordinary browser replay from invoking the controller twice with the same
    signed start proposal.
    """
    token = str(token or "")
    idempotency_token = str(idempotency_token or "")
    if not token or not idempotency_token:
        return False
    observed_at = time.monotonic() if now is None else float(now)
    key = (token, idempotency_token)
    with _ENGINEERING_CONFIRMATION_LOCK:
        expired_before = observed_at - ENGINEERING_CONFIRMATION_TTL_SECONDS
        stale = [
            item for item, claimed_at in _ENGINEERING_USED_CONFIRMATIONS.items()
            if claimed_at < expired_before
        ]
        for item in stale:
            _ENGINEERING_USED_CONFIRMATIONS.pop(item, None)
        if key in _ENGINEERING_USED_CONFIRMATIONS:
            return False
        if len(_ENGINEERING_USED_CONFIRMATIONS) >= ENGINEERING_CONFIRMATION_MAX_ENTRIES:
            # Do not evict a still-valid one-time token and make it replayable.
            # Capacity pressure fails closed until an older signed proposal has
            # expired and its consumed marker is pruned.
            return False
        _ENGINEERING_USED_CONFIRMATIONS[key] = observed_at
        return True


def _engineering_confirmation_unexpired(value):
    try:
        expires_at = int(value)
    except (TypeError, ValueError):
        return False
    return int(time.time()) <= expires_at


def _global_gate_proposal(setting):
    """Describe the exact gate state a global-automation proposal was made on.

    Binding the confirmation to this makes the token stale as soon as anyone
    else moves the primary automation gate, so a confirmation can only ever
    apply the change the operator was actually shown.
    """

    return ("enabled" if setting.enabled else "disabled") + "@" + setting.changed_at.isoformat()


def _bind_confirmation_form(html, form_action, **fields):
    """Add the signed-proposal inputs to one rendered confirmation form.

    The confirmation pages are rendered by view modules that only know about
    the CSRF token.  Minting the signed proposal, its expiry, and its one-time
    idempotency token belongs here, next to the code that verifies them, so the
    extra hidden inputs are injected into the exact form that posts the
    escalation rather than being threaded through every view signature.
    """

    anchor = html.find("action='" + escape(str(form_action), quote=True) + "'")
    if anchor < 0:
        raise ValueError(f"no confirmation form posts to {form_action}")
    close = html.find("</form>", anchor)
    if close < 0:
        raise ValueError(f"the {form_action} confirmation form is unterminated")
    return html[:close] + "".join(
        f"<input type='hidden' name='{escape(str(name), quote=True)}'"
        f" value='{escape(str(value), quote=True)}'>"
        for name, value in fields.items()
    ) + html[close:]


def _engineering_retry_patch(session):
    """Return the exact still-current patch eligible for a new engineering run."""
    if (
        session.profile != "engineering"
        or not session.run_id.startswith("pw-engineer-")
        or session.state not in ENGINEERING_RETRYABLE_STATES
    ):
        return None
    try:
        return _find_exact_patch(
            int(session.patch_id), int(session.patchset), session.revision
        )
    except (TypeError, ValueError):
        return None


def _send_retest_notification(event: ControllerNotification):
    """Record every notice and optionally deliver it through host sendmail."""
    log_structured_error(
        f"retest_{event.kind}",
        event.summary,
        next((
            patch.get("url", "") for patch in PATCHES
            if str(patch.get("change_number")) == event.patch_id
        ), ""),
    )
    try:
        config = GerritConfig.load()
    except GerritConfigError:
        return False
    timeline = []
    revision = str(event.details.get("revision") or "")
    if AUTOMATION_STORE is not None and event.run_id:
        try:
            timeline = AUTOMATION_STORE.list_timeline(event.run_id)
            revision = revision or AUTOMATION_STORE.get_run(event.run_id).revision
        except AutomationNotFound:
            timeline = []
    return send_automation_alert(
        config,
        patch_id=event.patch_id,
        revision=revision,
        state=event.kind,
        summary=event.summary,
        timeline=timeline,
    ).sent


def _automation_error(patch, error):
    log_structured_error(
        "retest_observer",
        str(error),
        str(patch.get("url") or ""),
    )


def initialize_retest_controller(*, start_observer=True, maloo=None):
    """Start browser-independent deterministic retest observation."""
    global RETEST_CONTROLLER, FAILURE_ACTION_CONTROLLER, AUTOMATION_OBSERVER
    if AUTOMATION_STORE is None:
        initialize_automation_store()
    maloo_adapter = maloo or MalooAdapter()
    RETEST_CONTROLLER = RetestController(
        AUTOMATION_STORE,
        maloo_adapter,
        revalidate=_fresh_patch_revision,
        notify=_send_retest_notification,
        lane_runtime=AUTONOMOUS_LANE_RUNTIME,
    )
    RETEST_CONTROLLER.reconcile_startup()
    FAILURE_ACTION_CONTROLLER = FailureActionController(
        AUTOMATION_STORE,
        maloo_adapter,
        revalidate=_fresh_patch_revision,
        reconcile_orphans=True,
    )
    _advance_failure_action_runs()
    FAILURE_ACTION_CONTROLLER.reconcile_orphans = False
    AUTOMATION_OBSERVER = BackgroundObserver(
        _patch_snapshot,
        refresh_watched_patch,
        _observe_patch_automation,
        interval_seconds=configured_refresh_interval(),
        error_handler=_automation_error,
    )
    if start_observer:
        AUTOMATION_OBSERVER.start()
    return RETEST_CONTROLLER


def _is_failure_action_run(run):
    if AUTOMATION_STORE is None:
        return False
    return any(
        action.action_type in {FAILURE_LINK_ACTION, FAILURE_RETEST_ACTION}
        for action in AUTOMATION_STORE.list_actions(run.run_id)
    )


def _advance_failure_action_runs(patch_id=None):
    """Reconcile approved failure writes without touching deterministic retests."""
    if AUTOMATION_STORE is None or FAILURE_ACTION_CONTROLLER is None:
        return []
    results = []
    for run in AUTOMATION_STORE.list_runs(
        patch_id=str(patch_id) if patch_id is not None else None,
        include_terminal=False,
    ):
        if _is_failure_action_run(run):
            results.append(FAILURE_ACTION_CONTROLLER.advance(run.run_id))
    return results


def _observe_patch_automation(patch):
    """Collect one snapshot, reconcile writes, and apply the research trigger."""
    patch_record = sync_automation_patch(patch)
    # Persist the canonical standing-policy projection before any legacy
    # controller gets a chance to act.  This closes the restart/crash window
    # where an older automatic retest policy could otherwise survive a newly
    # saved standing-policy change.
    standing_session = None
    if _has_explicit_standing_policy(patch):
        standing_policy = _standing_policy(patch)
        _sync_standing_test_policy(patch, standing_policy)
        standing_session = _apply_standing_policy(patch, policy=standing_policy)
    research_mode = "disabled"
    if patch_record is not None:
        research_mode = AUTOMATION_STORE.get_research_policy(
            patch_record.patch_id
        ).mode
    result = None
    # A selected engineering handler owns this observation cycle.  Do not also
    # dispatch a Maloo write from the same poll.
    active_session = _active_session_for_patch(patch.get("change_number"))
    if standing_session is None and active_session is None:
        result = RETEST_CONTROLLER.tick_patch(
            patch,
            collect_research_evidence=research_mode != "disabled",
        )
        _advance_failure_action_runs(result.patch_id)
    if (
        standing_session is None
        and active_session is None
        and
        research_mode == "automatic"
        and AUTOMATION_STORE.get_global_automation().enabled
    ):
        try:
            request = _start_unknown_failure_research(patch, automatic=True)
            _record_research_trigger_decision(
                patch,
                "started" if request.created else "already_exists",
                (
                    f"Started {request.run_id}"
                    if request.created
                    else f"Research attempt already registered as {request.run_id}"
                ),
            )
        except (
            AutomationConflict, RunControllerError, InvalidSessionOperation,
            SessionAlreadyExists,
        ) as exc:
            # Ineligible, duplicate, active-owner, and exhausted-budget states
            # are normal polling outcomes, but remain durably inspectable.
            _record_research_trigger_decision(patch, "not_started", str(exc))
    return result


def _standing_identity(patch):
    return RevisionIdentity(
        patch_id=str(patch.get("change_number") or ""),
        change_number=int(patch.get("change_number") or 0),
        patchset=int(patch.get("patchset") or 0),
        revision=str(patch.get("revision_sha") or ""),
    )


def _standing_policy(patch):
    patch_id = str(patch.get("change_number") or "")
    if STANDING_POLICY_STORE is None or not patch_id:
        return PatchAutomationPolicy(patch_id or "unknown")
    return STANDING_POLICY_STORE.get(patch_id)


def _has_explicit_standing_policy(patch):
    if STANDING_POLICY_STORE is None:
        return False
    patch_id = str(patch.get("change_number") or "")
    return bool(patch_id) and any(
        item.patch_id == patch_id for item in STANDING_POLICY_STORE.list()
    )


def _enroll_lane_for_level(patch, policy):
    """Make the retest lane's own switches follow the patch's level.

    The lane keeps three switches of its own -- global, per project, per patch
    -- and every one had to be on before a level-1 policy did anything, which
    is the four-switch lattice the ladder replaces.  A level at or above
    "retest" now enrols the patch (and its project, and the lane) itself;
    "watch" withdraws the patch.  The one switch the operator keeps is the
    global policy gate, which every automatic action still requires.
    """
    if AUTONOMOUS_LANE_STORE is None:
        return
    project = str(patch.get("project") or "")
    patch_id = str(patch.get("change_number") or "")
    if not project or not patch_id:
        return
    lane = LaneRef(DETERMINISTIC_RETEST_LANE, DETERMINISTIC_RETEST_VERSION)
    want = policy.rank >= 1
    try:
        controls = AUTONOMOUS_LANE_STORE.load()
        if want and not controls.global_enabled:
            controls = AUTONOMOUS_LANE_STORE.set_global_enabled(
                True, expected_generation=controls.generation,
            )
        project_control = controls.project_control(project)
        if want and (project_control is None or not project_control.enabled):
            controls = AUTONOMOUS_LANE_STORE.set_project_enabled(
                project, True, expected_generation=controls.generation,
            )
        patch_control = controls.patch_control(project, patch_id)
        enrolled = (
            patch_control is not None and patch_control.enabled
            and patch_control.lane == lane
        )
        if want and not enrolled:
            AUTONOMOUS_LANE_STORE.set_patch_lane(
                project, patch_id, lane, True, expected_generation=controls.generation,
            )
        elif not want and patch_control is not None and patch_control.enabled:
            AUTONOMOUS_LANE_STORE.set_patch_lane(
                project, patch_id, lane, False, expected_generation=controls.generation,
            )
    except (AutonomousLaneConflict, AutonomousLaneError, ValueError) as exc:
        log_structured_error("lane_enrolment", str(exc), str(patch.get("url") or ""))


def _sync_standing_test_policy(patch, policy):
    """Keep the existing deterministic/research controllers under one policy."""

    if AUTOMATION_STORE is None:
        return
    patch_id = str(patch.get("change_number") or "")
    if not patch_id:
        return
    _enroll_lane_for_level(patch, policy)
    retest_mode = "disabled"
    research_mode = "disabled"
    if policy.test_failures != "off":
        retest_mode = "automatic" if policy.trigger_mode == "automatic" else "approval"
    if policy.test_failures == "investigate":
        research_mode = "automatic" if policy.trigger_mode == "automatic" else "manual"
    retest_budget = 4 if retest_mode != "disabled" else 0
    project = str(patch.get("project") or "")
    if (
        AUTONOMOUS_LANE_RUNTIME is not None
        and AUTONOMOUS_LANE_RUNTIME.is_enrolled(project, patch_id)
    ):
        retest_budget = 1
        if not AUTONOMOUS_LANE_RUNTIME.effective_enabled(project, patch_id):
            retest_mode = "disabled"
            retest_budget = 0
    current = AUTOMATION_STORE.get_policy(patch_id)
    if (
        current.mode != retest_mode
        or current.action_budget != retest_budget
        or current.delivery_budget != retest_budget
    ):
        AUTOMATION_STORE.set_policy(
            patch_id, mode=retest_mode,
            action_budget=retest_budget,
            delivery_budget=retest_budget,
            updated_by="standing-policy-sync",
        )
    research = AUTOMATION_STORE.get_research_policy(patch_id)
    if research.mode != research_mode:
        AUTOMATION_STORE.set_research_policy(
            patch_id, mode=research_mode,
            run_budget=2 if research_mode != "disabled" else 0,
            updated_by="standing-policy-sync",
        )


def _record_standing_decision(patch, decision, *, outcome=""):
    if AUTOMATION_STORE is None:
        return
    payload = decision.to_dict()
    if outcome:
        payload["outcome"] = str(outcome)[:500]
    AUTOMATION_STORE.record_observation(
        str(patch.get("change_number") or ""),
        revision=str(patch.get("revision_sha") or ""),
        source="standing_policy",
        kind="standing_policy_trigger_decision",
        fingerprint=hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        payload=payload,
    )


def _consumed_standing_keys(patch):
    """Return exact standing events that already reserved a managed run."""

    if AUTOMATION_STORE is None:
        return frozenset()
    patch_id = str(patch.get("change_number") or "")
    revision = str(patch.get("revision_sha") or "")
    if not patch_id:
        return frozenset()
    return frozenset(
        str(item.payload.get("coalescing_key"))
        for item in AUTOMATION_STORE.list_observations(patch_id)
        if item.source == "standing_policy"
        and item.kind == "standing_policy_trigger_decision"
        and item.revision == revision
        and item.payload.get("eligible") is True
        and item.payload.get("outcome")
        and item.payload.get("coalescing_key")
    )


def _automatic_standing_decision(patch, policy, kind, fingerprint):
    identity = _standing_identity(patch)
    active = _revision_owner_session(patch)
    active_run = None
    if active is not None:
        active_run = ActivePatchRun(
            run_id=active.run_id, patch_id=identity.patch_id, state="running"
        )
    observation = TriggerObservation(kind, identity, fingerprint)
    return decide_trigger(
        policy, observation, identity, source="automatic", active_run=active_run,
        consumed_keys=_consumed_standing_keys(patch),
    )


def _apply_standing_policy(patch, *, policy=None, attended=False):
    """Start at most one exact-revision handler from an explicit standing policy.

    ``attended`` is the Run now button: the operator asked, here, for the level
    to be applied once.  The global kill switch governs what happens without
    anyone asking, so an attended run does not need it -- the same way the
    manual Investigate and Engineering run buttons never did.
    """

    if (
        STANDING_POLICY_STORE is None or RUN_CONTROLLER is None
        or AUTOMATION_STORE is None
    ):
        return None
    try:
        policy = policy or _standing_policy(patch)
        _sync_standing_test_policy(patch, policy)
        if policy.trigger_mode != "automatic":
            return None
        if not attended and not AUTOMATION_STORE.get_global_automation().enabled:
            return None
        # A patchset checkpatch cannot cherry-pick has to be rebased before
        # anything else is worth doing to it: a build repair or a review reply
        # on a revision that will never land is wasted, and a rebase makes a
        # new patchset that resets every other signal anyway.
        if (
            policy.configured_action("rebase_needed") == "rebase"
            and bool(patch.get("rebase_needed"))
        ):
            seed = hashlib.sha256(json.dumps({
                "revision": patch.get("revision_sha"),
                "blockers": patch.get("review_blockers") or [],
            }, sort_keys=True).encode("utf-8")).hexdigest()
            decision = _automatic_standing_decision(
                patch, policy, "rebase_needed", seed,
            )
            if decision.eligible:
                session = RUN_CONTROLLER.request_engineering(
                    patch, request_id=decision.coalescing_key, task="rebase",
                )
                SESSION_STORE.append_event(
                    session.session_id, "standing_policy_triggered", decision.to_dict(),
                    idempotency_key="standing-trigger:" + decision.coalescing_key,
                )
                _record_standing_decision(patch, decision, outcome=session.run_id)
                return session
            _record_standing_decision(patch, decision)
            # Whatever the reason the rebase did not start now -- already
            # started for this revision, at its ceiling, or not yet eligible --
            # the revision still cannot land, so nothing below is worth doing
            # to it.  The next patchset is where the other handlers resume.
            return None
        # Review work wins when both review and build signals arrive together;
        # the one-run owner invariant defers the build event to a later poll.
        if (
            policy.review_comments != "off"
            and int(patch.get("unresolved") or 0) > 0
        ):
            snapshot = GerritStatusClient.configured().fetch_review_snapshot(
                patch["url"], expected_revision=str(patch.get("revision_sha") or "")
            )
            decision = _automatic_standing_decision(
                patch, policy, "review_comments", snapshot["snapshot_sha256"],
            )
            if decision.eligible:
                try:
                    session = RUN_CONTROLLER.request_review_comments(
                        patch, snapshot, mode=policy.review_comments,
                        request_id=decision.coalescing_key,
                        design_audit=policy.design_audit,
                    )
                except NoReviewTargets as exc:
                    _record_standing_decision(patch, decision, outcome=str(exc))
                    return None
                SESSION_STORE.append_event(
                    session.session_id, "standing_policy_triggered", decision.to_dict(),
                    idempotency_key="standing-trigger:" + decision.coalescing_key,
                )
                _record_standing_decision(patch, decision, outcome=session.run_id)
                return session
            _record_standing_decision(patch, decision)
        if (
            policy.build_failures == "repair"
            and str(patch.get("jenkins") or "").upper() == "FAIL"
            and patch.get("jenkins_url")
        ):
            seed = hashlib.sha256(json.dumps({
                "revision": patch.get("revision_sha"),
                "jenkins_url": patch.get("jenkins_url"),
            }, sort_keys=True).encode("utf-8")).hexdigest()
            preliminary = _automatic_standing_decision(
                patch, policy, "build_failure", seed,
            )
            if not preliminary.eligible:
                _record_standing_decision(patch, preliminary)
                if preliminary.code != "duplicate":
                    return None
            snapshot = _capture_build_failure_snapshot(patch)
            decision = _automatic_standing_decision(
                patch, policy, "build_failure", snapshot["snapshot_sha256"],
            )
            if decision.eligible:
                session = RUN_CONTROLLER.request_build_failure(
                    patch, snapshot, request_id=decision.coalescing_key,
                )
                SESSION_STORE.append_event(
                    session.session_id, "standing_policy_triggered", decision.to_dict(),
                    idempotency_key="standing-trigger:" + decision.coalescing_key,
                )
                _record_standing_decision(patch, decision, outcome=session.run_id)
                return session
            _record_standing_decision(patch, decision)
    except (
        AutomationConflict, GerritConfigError, GerritRequestError,
        JenkinsSnapshotError, RunControllerError, SessionAlreadyExists,
        StandingPolicyError, ValueError,
    ) as exc:
        log_structured_error(
            "standing_policy", str(exc), str(patch.get("url") or ""),
        )
    return None


def _patch_snapshot():
    with PATCHES_LOCK:
        return list(PATCHES)


def _find_exact_patch(change_number, patchset, revision):
    with PATCHES_LOCK:
        return next((
            item for item in PATCHES
            if int(item.get("change_number", 0) or 0) == int(change_number)
            and int(item.get("patchset", 0) or 0) == int(patchset)
            and str(item.get("revision_sha") or "").lower()
            == str(revision or "").lower()
        ), None)


def _capture_build_failure_snapshot(patch):
    """Bracket one exact Jenkins failure with current Gerrit observations."""

    client = GerritStatusClient.configured()
    before = client.fetch(patch["url"])

    def identity(value):
        return (
            int(value.get("change_number") or 0),
            int(value.get("patchset") or 0),
            str(value.get("revision_sha") or "").lower(),
            str(value.get("revision_ref") or ""),
            str(value.get("project") or ""),
            str(value.get("branch") or ""),
            str(value.get("jenkins") or "").upper(),
            str(value.get("jenkins_url") or ""),
        )

    expected = identity(patch)
    if identity(before) != expected or expected[6] != "FAIL" or not expected[7]:
        raise JenkinsSnapshotError(
            "The watched patch no longer identifies this exact Jenkins failure"
        )
    snapshot = JENKINS_SNAPSHOT_CLIENT.fetch_failure_snapshot(
        before["jenkins_url"],
        change_number=before["change_number"], patchset=before["patchset"],
        revision_sha=before["revision_sha"], revision_ref=before["revision_ref"],
        project=before["project"], branch=before.get("branch", ""),
    )
    after = client.fetch(patch["url"])
    if identity(after) != expected:
        raise JenkinsSnapshotError(
            "Gerrit or its current Jenkins build changed while capturing evidence"
        )
    return snapshot


def sync_automation_patch(patch):
    """Persist one exact Gerrit revision without changing its safe policy."""
    if AUTOMATION_STORE is None:
        return None
    revision = str(patch.get("revision_sha") or "")
    patchset = int(patch.get("patchset") or 0)
    change_number = int(patch.get("change_number") or 0)
    if not revision or not patchset or not change_number:
        return None
    return AUTOMATION_STORE.upsert_patch(
        str(change_number),
        gerrit_url=patch["url"],
        change_number=change_number,
        revision=revision,
        patchset=patchset,
        status=str(patch.get("lifecycle") or patch.get("status") or "open").lower(),
    )


def configured_refresh_interval():
    """Return the browser polling interval without exposing credentials."""
    try:
        return GerritConfig.load().refresh_interval
    except GerritConfigError:
        return 300


def initialize_session_store(database=DEFAULT_SESSION_DATABASE):
    """Open the private durable managed-session store."""
    global SESSION_STORE, ACTIVE_SESSION_DATABASE
    ACTIVE_SESSION_DATABASE = Path(database)
    SESSION_STORE = SessionStateStore(ACTIVE_SESSION_DATABASE)
    return SESSION_STORE


def load_checkout_pool(path=None):
    """Load the operator's declared checkout pool, or None when unusable.

    A missing or malformed declaration is not fatal: engineering runs fall back
    to a private per-run clone.  The pool is an optimisation (reuse a warm
    Lustre tree) and an ownership model (the checkout index names the run's
    VMs), not a prerequisite for the tool to run.
    """

    try:
        pool = (
            CheckoutPool.from_config(path) if path is not None
            else CheckoutPool.from_config()
        )
    except (CheckoutPoolError, OSError, sqlite3.Error) as exc:
        _automation_error({"url": "checkout-pool"}, exc)
        return None
    return pool


def initialize_run_controller(
    *, runs_directory=None, start=True, checkout_pool=None,
    model=None, effort=None,
):
    """Create the background dispatcher after session-state initialization."""
    global RUN_CONTROLLER
    if SESSION_STORE is None:
        initialize_session_store()

    def alert(session, reason, messages, confirmation_url):
        try:
            config = GerritConfig.load()
        except GerritConfigError:
            return False
        return send_session_alert(
            config,
            session_id=session.session_id,
            patch_id=session.patch_id,
            state=SESSION_STORE.get_session(session.session_id).state,
            reason=reason,
            messages=messages,
            confirmation_url=confirmation_url,
        ).sent

    def notify_human(session, question, run_url):
        """Reach the operator on every channel the host has: email and Gerrit.

        Returns {channel: (sent, detail)}; the controller records each outcome
        in the delivery ledger and shows it on the run page.
        """
        outcomes = {}
        try:
            config = GerritConfig.load()
        except GerritConfigError as exc:
            return {
                "email": (False, f"Gerrit config unavailable: {exc}"[:500]),
                "gerrit": (False, f"Gerrit config unavailable: {exc}"[:500]),
            }
        mail = send_human_notice(
            config,
            session_id=session.session_id,
            patch_id=session.patch_id,
            run_id=session.run_id,
            question=question.question,
            run_url=run_url,
        )
        outcomes["email"] = (mail.sent, mail.message)
        try:
            GerritStatusClient(config).post_message(
                int(session.patch_id),
                compose_gerrit_help_message(run_id=session.run_id, question=question.question),
            )
            outcomes["gerrit"] = (True, "change message posted")
        except (GerritRequestError, ValueError) as exc:
            outcomes["gerrit"] = (False, str(exc)[:500])
        return outcomes

    options = {
        "alert_sender": alert,
        "human_notifier": notify_human,
        "ltvm_adapter": LTVMAdapter(),
    }
    # The fallback for any run started without an explicit choice.
    if model is not None:
        options["model"] = model
    if effort is not None:
        options["effort"] = effort
    if runs_directory is not None:
        options["runs_directory"] = Path(runs_directory)
    pool = checkout_pool if checkout_pool is not None else load_checkout_pool()
    if pool is not None and pool.indices:
        # Only hand over a pool that actually declares checkouts. An empty pool
        # would make every engineering run fail to allocate, where the per-run
        # clone path still works.
        options["checkout_pool"] = pool
    RUN_CONTROLLER = RunController(
        SESSION_STORE,
        **options,
    )
    if start:
        RUN_CONTROLLER.start()
    return RUN_CONTROLLER


def refresh_resource_status(*, force=False):
    """Return a short-lived host/LTVM snapshot without polling per render."""
    global _RESOURCE_SNAPSHOT, _RESOURCE_SNAPSHOT_MONOTONIC
    now = time.monotonic()
    if (
        not force
        and _RESOURCE_SNAPSHOT is not None
        and now - _RESOURCE_SNAPSHOT_MONOTONIC < RESOURCE_CACHE_SECONDS
    ):
        return _RESOURCE_SNAPSHOT
    if not RESOURCE_COLLECTION_ENABLED:
        return {
            "host_memory": {
                "name": platform.node(),
                "quality": "unavailable",
                "errors": [{"message": "Resource collection starts with the web service."}],
            },
            "ltvm": {"vms": []},
        }
    try:
        _RESOURCE_SNAPSHOT = collect_resource_snapshot()
    except Exception as exc:
        # This is the first thing `main()` does, before the socket is bound, so
        # an exception here used to stop the server from ever starting -- and
        # on the render path it turned the dashboard into a 500. Degrade to the
        # same "unavailable" snapshot the disabled path returns, which the host
        # panel already renders as an error the operator can read.
        log_structured_error("resource_snapshot_failed", str(exc), "")
        _RESOURCE_SNAPSHOT = {
            "host_memory": {
                "name": platform.node(),
                "quality": "unavailable",
                "errors": [{
                    "message": "Resource collection failed: "
                               f"{type(exc).__name__}: {exc}",
                }],
            },
            "ltvm": {"vms": []},
        }
    _RESOURCE_SNAPSHOT_MONOTONIC = now
    return _RESOURCE_SNAPSHOT


def _session_dashboard_records(now=None):
    """Project durable active sessions and bounded messages for the view."""
    if SESSION_STORE is None:
        return [], {}
    observed_at = now or datetime.now(UTC)
    sessions = []
    messages_by_session = {}
    for session in SESSION_STORE.list_sessions(include_terminal=False):
        agent_pid = _agent_process_pid(session)
        record = {
            "session_id": session.session_id,
            "owner_id": f"patch-watcher:{session.session_id}",
            "patch_id": session.patch_id,
            "run_id": session.run_id,
            "profile": session.profile,
            "state": session.state,
            "elapsed_seconds": max(
                0, (observed_at - session.started_at).total_seconds()
            ),
            "last_qualifying_activity": session.last_qualifying_activity_at.isoformat(),
            "process_id": agent_pid,
            "current_step": "Managed session",
        }
        if agent_pid:
            process_memory = collect_process_tree_rss(agent_pid)
            record["process_tree_rss_bytes"] = (
                process_memory.total_rss_bytes
                if process_memory.total_rss_bytes is not None
                else process_memory.known_rss_bytes
            )
            record["resource_sample_age_seconds"] = 0
            record["resource_quality"] = process_memory.quality
        sessions.append(record)
        messages_by_session[session.session_id] = [
            {
                "author": message.author,
                "body": message.body,
                "created_at": message.created_at.isoformat(),
            }
            for message in SESSION_STORE.recent_messages(session.session_id, limit=10)
        ]
    return sessions, messages_by_session


def controller_failures_html():
    """Surface controller faults that belong to no session.

    These are the failures that stop dispatch entirely -- a locked database, an
    unreadable LTVM inventory -- so they cannot be recorded in the session store
    the controller may be unable to read. They live in a durable JSON document
    instead, which means that without this panel "somewhere the operator can
    see it" would mean "on disk".
    """

    if RUN_CONTROLLER is None:
        return ""
    try:
        failures = RUN_CONTROLLER.controller_failures()
    except Exception:  # never let the error view be the thing that errors
        return (
            "<section class='card controller-failures' role='alert'>"
            "<h2>Controller failures</h2>"
            "<p class='error'>The controller failure record could not be read.</p>"
            "</section>"
        )
    if not failures:
        return ""
    rows = []
    for failure in reversed(failures[-20:]):
        count = failure.get("count") or 1
        repeated = f" &middot; seen {escape(str(count))}&times;" if int(count) > 1 else ""
        detail = failure.get("detail") or ""
        rows.append(
            "<li><strong>" + escape(str(failure.get("scope") or "controller"))
            + "</strong>: <code>" + escape(str(failure.get("error_type") or "error"))
            + "</code> " + escape(str(failure.get("summary") or ""))
            + repeated
            + "<div class='detail'>last seen "
            + escape(str(failure.get("last_seen") or "unknown"))
            + (" &middot; " + escape(str(detail)) if detail else "")
            + "</div></li>"
        )
    return (
        "<section class='card controller-failures' role='alert' "
        "aria-labelledby='controller-failures-title'>"
        "<h2 id='controller-failures-title'>Controller failures</h2>"
        "<p class='detail'>Faults that belong to no single run. While these "
        "recur, runs may not start and finished runs may not be cleaned up.</p>"
        "<ol>" + "".join(rows) + "</ol></section>"
    )


def resource_dashboard_html(*, force=False):
    """Render current host, managed-session, and LTVM resource status."""
    snapshot = refresh_resource_status(force=force)
    sessions, messages = _session_dashboard_records()
    if hasattr(snapshot, "to_dict"):
        snapshot = snapshot.to_dict()
    if isinstance(snapshot, dict):
        snapshot = dict(snapshot)
        host_memory = dict(snapshot.get("host_memory") or {})
        host_memory.setdefault("name", platform.node())
        measured_session_memory = [
            session.get("process_tree_rss_bytes")
            for session in sessions
            if session.get("process_tree_rss_bytes") is not None
        ]
        host_memory["session_process_rss_bytes"] = sum(measured_session_memory)
        snapshot["host_memory"] = host_memory
    return render_resource_dashboard(
        snapshot,
        sessions,
        messages_by_session=messages,
        csrf_token=CSRF_TOKEN,
    )


def send_status_email(config=None, *, runner=subprocess.run):
    """Send (or dry-run) the current bounded status summary."""
    with PATCHES_LOCK:
        patches = [dict(patch) for patch in PATCHES]
    return send_daily_summary(
        patches,
        config or GerritConfig.load(),
        runner=runner,
        automation_events=automation_daily_events(),
    )


def automation_daily_events(limit=25):
    """Project recent deterministic-run events for reports without secrets."""
    events = []
    if AUTOMATION_STORE is not None:
        for run in AUTOMATION_STORE.list_runs():
            for event in AUTOMATION_STORE.list_timeline(run.run_id):
                events.append({
                    "created_at": event.created_at.isoformat(),
                    "patch_id": run.patch_id,
                    "event_type": event.event_type,
                    "summary": str(event.payload.get("summary") or "Recorded")[:500],
                })
    for record in _lane_records():
        events.append({
            "created_at": record.recorded_at,
            "patch_id": record.decision.identity.patch_id,
            "event_type": "autonomous_lane_" + (
                "admitted" if record.decision.eligible else "rejected"
            ),
            "summary": (
                f"{record.decision.lane.name if record.decision.lane else 'unavailable'} "
                f"v{record.decision.lane.version if record.decision.lane else '—'}: "
                f"{record.decision.code} — {record.decision.explanation}"
            )[:500],
        })
    return sorted(events, key=lambda item: item["created_at"])[-limit:]


def refresh_watched_patch(patch):
    """Refresh once and stale any run no longer pinned to the current revision."""
    result = refresh_patch(patch)
    if result is None:
        sync_automation_patch(patch)
    if RUN_CONTROLLER is not None and patch.get("revision_sha"):
        RUN_CONTROLLER.reconcile_patch_revision(patch)
    return result


def valid_url(value):
    """Return true for canonical Whamcloud Gerrit change URLs only."""
    try:
        parse_change_number(value)
    except ValueError:
        return False
    return True


def add_patch(url, title=""):
    """Add a patch, returning (patch, error). Keeps the web handler testable."""
    url = url.strip().rstrip("/")
    if not valid_url(url):
        return None, "Use an HTTPS Whamcloud Gerrit URL containing /c/."
    patch = {
        "url": url,
        "title": title.strip() or str(parse_change_number(url)),
        "status": "Pending",
        "last_updated": datetime.now(UTC).isoformat(timespec="seconds"),
        "lifecycle": "Open", "patchset": "—", "wip": False,
        "change_number": parse_change_number(url),
        "project": "", "revision_sha": "", "revision_ref": "",
        "review": "—", "unresolved": 0, "jenkins": "—", "maloo": "—",
        "watch_state": "uninitialized",
        "recommendation": "Refresh to retrieve Gerrit status",
        "last_checked": "—", "last_changed": "—", "change_summary": "—",
        "history": [],
        "errors": [], "check_count": 0,
    }
    # Check and append under ONE lock: the server is threaded, so splitting
    # them let two concurrent adds of the same URL both pass the check.
    with PATCHES_LOCK:
        if any(p["url"] == url for p in PATCHES):
            return None, "That patch is already being watched."
        PATCHES.append(patch)
    return patch, None


def ticket_from_title(title):
    """Return the leading Jira issue key used by Lustre patch subjects."""
    match = re.match(r"([A-Z][A-Z0-9]*-[0-9]+)(?:\b|:)", title or "")
    return match.group(1) if match else ""


def _chip(text, tone, *, title=""):
    """Render a text-labelled status chip; color is only reinforcement."""
    title_attr = f" title='{escape(title, quote=True)}'" if title else ""
    return (
        f"<span class='status-chip tone-{tone}'{title_attr}>"
        f"{escape(str(text))}</span>"
    )


def _review_chip(patch):
    """Map Mark-style review health to accessible display categories."""
    review = str(patch.get("review", "—"))
    if review == "Ready":
        return _chip("✓ Ready", "good", title="All landing criteria satisfied")
    if review == "Veto":
        return _chip("✕ Veto", "bad", title="A reviewer voted Code-Review -1 or -2")
    if "failed" in review.casefold():
        return _chip(f"✕ Needs · {review}", "bad", title="Review or CI needs attention")
    clean = (
        review == "Pending"
        and patch.get("jenkins") == "PASS"
        and patch.get("maloo") == "PASS"
        and not patch.get("unresolved")
    )
    if clean:
        return _chip("✓ Clean", "info", title="No failures; still awaiting review criteria")
    if review == "Pending":
        return _chip("! Needs", "warn", title="Still awaiting review or CI criteria")
    return _chip("— Not applicable", "neutral", title="No active review state")


def _ci_chip(service, value, url=""):
    value = str(value or "—")
    labels = {
        "PASS": ("good", f"✓ {service} pass"),
        "FAIL": ("bad", f"✕ {service} fail"),
        "RUNNING": ("warn", f"… {service} running"),
        "—": ("neutral", f"— {service} no result"),
    }
    tone, label = labels.get(value, ("neutral", f"{service} {value}"))
    chip = _chip(label, tone, title=f"{service} status: {value}")
    if not url:
        return chip
    return (
        f"<a class='status-link' href='{escape(url, quote=True)}' "
        f"target='_blank' rel='noreferrer'>{chip}</a>"
    )


def _watch_chip(value):
    tones = {
        "ready": "good",
        "merged": "good",
        "abandoned": "bad",
        "terminal": "neutral",
        "ci-failed": "bad",
        "needs-attention": "bad",
        "rebase-needed": "warn",
        "needs-review": "warn",
        "awaiting-ci": "warn",
        "work-in-progress": "info",
        "uninitialized": "neutral",
    }
    # .title() mangles initialisms -- "ci-failed" rendered as "Ci Failed" in
    # the browser. Spell the user-facing labels out instead of deriving them.
    labels = {
        "ready": "Ready",
        "merged": "Merged",
        "abandoned": "Abandoned",
        "terminal": "Terminal",
        "ci-failed": "CI failed",
        "needs-attention": "Needs attention",
        "rebase-needed": "Rebase needed",
        "needs-review": "Needs review",
        "awaiting-ci": "Awaiting CI",
        "work-in-progress": "Work in progress",
        "uninitialized": "Uninitialized",
    }
    text = labels.get(value, str(value or "unknown").replace("-", " ").capitalize())
    prefix = "✕ " if value in {"ci-failed", "needs-attention"} else ""
    if value in {"needs-review", "awaiting-ci"}:
        prefix = "! "
    if value == "ready":
        prefix = "✓ "
    return _chip(prefix + text, tones.get(value, "neutral"), title="Watch state")


def _vote_summary(patch):
    votes = patch.get("review_votes") or []
    if not votes:
        return "No CR votes"
    return ", ".join(
        f"{vote.get('name', '?')} {vote.get('value', 0):+d}"
        for vote in votes
    )


def _history_html(patch):
    history = patch.get("history") or []
    if not history:
        return ""
    items = "".join(
        "<li>"
        f"<time>{escape(event.get('changed_at', '') or event.get('checked_at', ''))}</time> "
        f"{escape(event.get('summary', 'Status changed'))} "
        f"<span class='history-state'>[{escape(event.get('watch_state', ''))}]</span>"
        "</li>"
        for event in reversed(history)
    )
    return f"<details><summary>History ({len(history)})</summary><ol>{items}</ol></details>"


def overall_last_checked():
    """Return the newest check ATTEMPT shown on the page.

    ``refresh_patch`` stamps ``last_checked`` even when the fetch failed, so
    this is only ever an attempt time. Use :func:`overall_last_successful_check`
    for the value an operator reads as "the data is this fresh".
    """
    with PATCHES_LOCK:
        checked = [
            str(patch.get("last_checked", ""))
            for patch in PATCHES
            if patch.get("last_checked") not in {None, "", "—"}
        ]
    return max(checked) if checked else "Never"


def overall_last_successful_check():
    """Return the newest check that actually succeeded.

    Only a successful fetch writes ``refreshed_at``; a failed one leaves the
    previous value in place. Reporting max(last_checked) as the check time made
    a host on which every refresh failed look freshly checked.
    """
    with PATCHES_LOCK:
        checked = [
            str(patch.get("refreshed_at", ""))
            for patch in PATCHES
            if patch.get("refreshed_at")
        ]
    return max(checked) if checked else "Never"


def refresh_failure_summary():
    """Summarise how many watched patches failed their most recent refresh."""
    with PATCHES_LOCK:
        errors = [bool(patch.get("status_error")) for patch in PATCHES]
    if not errors:
        return ""
    failing = sum(errors)
    if not failing:
        return f"All {len(errors)} patches refreshed successfully."
    return f"{failing} of {len(errors)} patches failed to refresh."


def _refresh_errors_html(patch):
    """Render the stored per-patch check count and recorded refresh errors."""
    checks = int(patch.get("check_count", 0) or 0)
    errors = list(patch.get("errors") or [])
    if not errors:
        return f"<div class='detail'>Checks: {checks}</div>" if checks else ""
    items = "".join(
        "<li><time>" + escape(str(item.get("checked_at", "") or "")) + "</time> "
        + escape(str(item.get("message", "") or "")) + "</li>"
        for item in reversed(errors)
    )
    return (
        f"<details><summary>Refresh errors ({len(errors)} of {checks} checks)"
        f"</summary><ol>{items}</ol></details>"
    )


def _active_session_for_patch(change_number):
    if SESSION_STORE is None:
        return None
    for session in SESSION_STORE.list_sessions(include_terminal=False):
        if session.patch_id == str(change_number):
            return session
    return None


def _revision_owner_session(patch):
    """Return the session that owns this patch's exact revision, if one does.

    A non-terminal session owns its patch outright.  A session that already
    succeeded keeps owning the revision it ran against for a bounded window:
    the agent uploads its own patchset, so between "the run finished" and "we
    next polled Gerrit" we cannot yet tell whether a new patchset exists.
    Starting a second agent in that window means two agents pushing to one
    change.

    The window closes by itself.  Once Gerrit has been polled *after* the run
    finished and the revision is still the same, the run published nothing and
    the patch is free again -- otherwise a run that legitimately produced no
    patchset (a review run that only posted replies, say) would block its
    change from all further automatic work forever.
    """

    if SESSION_STORE is None:
        return None
    change_number = patch.get("change_number")
    active = _active_session_for_patch(change_number)
    if active is not None:
        return active
    revision = str(patch.get("revision_sha") or "").lower()
    if not revision:
        return None
    checked_at = _parse_timestamp(patch.get("last_checked"))
    for session in SESSION_STORE.list_sessions(include_terminal=True):
        if (
            session.patch_id != str(change_number)
            or session.state != "succeeded"
            or str(session.revision or "").lower() != revision
        ):
            continue
        finished_at = session.state_changed_at
        if (
            checked_at is not None
            and finished_at is not None
            and checked_at > finished_at
        ):
            continue  # polled since it finished; the revision really is unchanged
        return session
    return None


def _parse_timestamp(value):
    """Parse a stored ISO timestamp into an aware datetime, or None."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _latest_session_for_patch_kind(change_number, request_kind):
    if SESSION_STORE is None or RUN_CONTROLLER is None:
        return None
    for session in SESSION_STORE.list_sessions(include_terminal=True):
        if session.patch_id != str(change_number):
            continue
        try:
            request = RUN_CONTROLLER._request_payload(session)
        except RunControllerError:
            continue
        if request.get("request_kind") == request_kind:
            return session
    return None


def _latest_maloo_observation(patch):
    if AUTOMATION_STORE is None:
        return None
    patch_id = str(patch.get("change_number") or "")
    revision = str(patch.get("revision_sha") or "").lower()
    if not patch_id or not revision:
        return None
    try:
        observations = AUTOMATION_STORE.list_observations(patch_id)
    except AutomationNotFound:
        return None
    return next((
        item for item in reversed(observations)
        if item.revision.lower() == revision
        and item.kind == "maloo_retest_evaluation"
    ), None)


def _unknown_failures(patch):
    """Return enforced failures lacking an accepted, complete Jira link."""
    observation = _latest_maloo_observation(patch)
    if observation is None:
        return observation, []
    snapshot = observation.payload.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("maloo_state_complete"):
        return observation, []
    unknown = []
    for failure in snapshot.get("maloo_failures") or []:
        if not isinstance(failure, dict) or failure.get("enforced") is not True:
            continue
        accepted = any(
            isinstance(link, dict) and link.get("accepted_for_retest") is True
            for link in failure.get("linked_bugs") or []
        )
        if not accepted:
            unknown.append(dict(failure))
    return observation, unknown


def _match_unknown_failure(patch, session_id, test_group, suite_id):
    """Prove submitted identifiers name a currently observed unknown failure."""
    submitted = tuple(str(value or "") for value in (
        session_id, test_group, suite_id
    ))
    if not all(MALOO_ID_RE.fullmatch(value) for value in submitted):
        return None
    _observation, failures = _unknown_failures(patch)
    for failure in failures:
        identity = tuple(str(failure.get(key) or "") for key in (
            "session_id", "test_group", "remote_failure_id"
        ))
        if not all(MALOO_ID_RE.fullmatch(value) for value in identity):
            continue
        if identity == submitted:
            return failure
    return None


def _research_evidence(patch):
    observation, failures = _unknown_failures(patch)
    if observation is None or not failures:
        return None, observation
    records = []
    for index, failure in enumerate(failures, 1):
        remote_id = str(failure.get("remote_failure_id") or index)
        safe_id = "".join(
            char if char.isalnum() or char in "._-:" else "-"
            for char in remote_id
        )[:150]
        records.append({
            "record_id": f"maloo-failure-{index}-{safe_id}",
            "source": "maloo",
            "kind": "enforced_test_failure_without_accepted_bug",
            "payload": failure,
        })
    artifacts = []
    maloo_url = str(patch.get("maloo_url") or "")
    if maloo_url:
        artifacts.append({
            "artifact_id": "maloo-related-results",
            "kind": "maloo_results_url",
            "locator": maloo_url,
            "description": "Related Maloo results captured by Patch Watcher.",
        })
    return {
        "schema": "patch-watcher-unknown-failure-evidence/v1",
        "change_number": int(patch["change_number"]),
        "project": str(patch["project"]),
        "patchset": int(patch["patchset"]),
        "revision_sha": str(patch["revision_sha"]).lower(),
        "revision_ref": str(patch["revision_ref"]),
        "records": records,
        "artifacts": artifacts,
    }, observation


def _research_sessions(patch, *, include_terminal=True):
    if SESSION_STORE is None:
        return []
    patch_id = str(patch.get("change_number") or "")
    revision = str(patch.get("revision_sha") or "").lower()
    return [
        session for session in SESSION_STORE.list_sessions(
            include_terminal=include_terminal
        )
        if session.patch_id == patch_id
        and (session.revision or "").lower() == revision
        and session.run_id.startswith("pw-research-")
    ]


def _start_unknown_failure_research(
    patch, *, automatic=False, attempt_id=None
):
    if AUTOMATION_STORE is None or RUN_CONTROLLER is None:
        raise RunControllerError("research controller is not initialized")
    synced = sync_automation_patch(patch)
    if synced is None:
        raise RunControllerError("refresh the exact Gerrit revision before research")
    policy = AUTOMATION_STORE.get_research_policy(synced.patch_id)
    required_mode = "automatic" if automatic else "manual"
    if policy.mode != required_mode:
        raise RunControllerError(
            f"unknown-failure research policy is {policy.mode}, not {required_mode}"
        )
    evidence, observation = _research_evidence(patch)
    if evidence is None or observation is None:
        raise RunControllerError("no complete unknown enforced Maloo failure is recorded")
    normalized = normalize_unknown_failure_evidence(evidence)
    evidence_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    evidence_fingerprint = hashlib.sha256(evidence_json.encode()).hexdigest()
    attempt_id = str(attempt_id or (
        ("automatic:" if automatic else "manual:") + observation.observation_id
    ))
    admission, _claim_created = AUTOMATION_STORE.claim_research_admission(
        synced.patch_id,
        revision=synced.current_revision,
        patchset=synced.current_patchset,
        expected_policy_version=policy.version,
        mode=required_mode,
        attempt_id=attempt_id,
        evidence_fingerprint=evidence_fingerprint,
    )
    if admission.state == "released":
        raise RunControllerError(
            "this research attempt previously failed admission; use a new retry attempt"
        )
    try:
        request = RUN_CONTROLLER.request_unknown_failure_investigation(
            normalized,
            attempt_id=attempt_id,
            trigger={
                "kind": "automatic" if automatic else "manual",
                "observation_id": observation.observation_id,
                "observation_fingerprint": observation.fingerprint,
                "policy_version": policy.version,
                "admission_id": admission.admission_id,
                "admission_slot": admission.slot,
            },
        )
    except Exception as exc:
        expected_run_id = unknown_failure_research_run_id(
            normalized, attempt_id
        )
        session_was_registered = bool(
            SESSION_STORE is not None
            and any(
                item.run_id == expected_run_id
                for item in SESSION_STORE.list_sessions(include_terminal=True)
            )
        )
        if admission.state == "reserved" and not session_was_registered:
            AUTOMATION_STORE.release_research_admission(
                admission.admission_id,
                reason="session registration failed: " + type(exc).__name__,
            )
        raise
    AUTOMATION_STORE.register_research_admission(
        admission.admission_id, request.session_id
    )
    return request


def _record_research_trigger_decision(patch, status, reason):
    if AUTOMATION_STORE is None or not patch.get("revision_sha"):
        return None
    payload = {
        "status": str(status),
        "reason": str(reason)[:500],
        "patchset": int(patch.get("patchset") or 0),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = "sha256:" + hashlib.sha256(encoded).hexdigest()
    try:
        return AUTOMATION_STORE.record_observation(
            str(patch.get("change_number") or ""),
            revision=str(patch.get("revision_sha") or "").lower(),
            source="patch-watcher",
            kind="unknown_failure_research_trigger_decision",
            fingerprint=fingerprint,
            payload=payload,
        )[0]
    except (AutomationConflict, AutomationNotFound, ValueError):
        return None


def _research_context(patch):
    default = {"mode": "disabled", "run_budget": 0, "version": "0"}
    _observation, unknown = _unknown_failures(patch)
    if AUTOMATION_STORE is None or not patch.get("revision_sha"):
        return default, unknown, None, None
    patch_id = str(patch.get("change_number") or "")
    try:
        policy = AUTOMATION_STORE.get_research_policy(patch_id)
    except AutomationNotFound:
        return default, unknown, None, None
    sessions = _research_sessions(patch)
    latest = sessions[0] if sessions else None
    report = None
    if latest is not None and SESSION_STORE is not None:
        terminal = SESSION_STORE.get_terminal_result(latest.session_id)
        if terminal is not None:
            report = dict(terminal.result)
            report.update({
                "run_id": latest.run_id,
                "state": terminal.state,
                "revision_sha": latest.revision,
            })
    return policy, unknown, latest, report


def _retest_context(patch):
    """Return the persisted policy, latest decision, and bounded timeline."""
    default_policy = {"mode": "disabled", "action_budget": 0}
    if AUTOMATION_STORE is None or not patch.get("revision_sha"):
        return default_policy, None, [], None
    patch_id = str(patch.get("change_number") or "")
    try:
        policy = AUTOMATION_STORE.get_policy(patch_id)
        runs = AUTOMATION_STORE.list_runs(patch_id=patch_id)
    except (AutomationNotFound, ValueError):
        return default_policy, None, [], None
    if not runs:
        return policy, None, [], None
    run = runs[-1]
    timeline = AUTOMATION_STORE.list_timeline(run.run_id)
    evaluation = {
        "status": run.status,
        "reason_code": run.failure_code or "",
        "reason": run.failure_summary or "",
    }
    for event in reversed(timeline):
        candidate = event.payload.get("evaluation")
        if isinstance(candidate, dict):
            evaluation = candidate
            break
        if event.event_type in {
            "decision_recorded", "evaluation_recorded", "retest_evaluated"
        }:
            evaluation = dict(event.payload)
            break
    timeline_view = [
        {
            "created_at": event.created_at.isoformat(),
            "event_type": event.event_type,
            "summary": event.payload.get("summary")
            or event.payload.get("reason")
            or "Recorded",
        }
        for event in timeline
    ]
    approval_action = None
    if run.policy_snapshot.get("mode") == "approval":
        for action in AUTOMATION_STORE.list_actions(run.run_id):
            if (
                action.status == "planned"
                and AUTOMATION_STORE.get_action_approval(action.action_id) is None
            ):
                approval_action = {
                    "action_id": action.action_id,
                    "session_id": action.request.get("session_id", ""),
                    "jira_ticket": action.request.get("jira_ticket", ""),
                }
                break
    return policy, evaluation, timeline_view, approval_action


def _failure_action_projection(run, action):
    request = dict(action.request)
    approval = AUTOMATION_STORE.get_action_approval(action.action_id)
    kind = (
        "associate_bug"
        if action.action_type == FAILURE_LINK_ACTION
        else "request_retest"
    )
    link_state = "pending"
    if kind == "request_retest":
        association_id = str(request.get("association_action_id") or "")
        try:
            link_state = AUTOMATION_STORE.get_action(association_id).status
        except AutomationNotFound:
            link_state = "missing"
    suite_name = str(request.get("suite") or "")
    if not suite_name:
        with PATCHES_LOCK:
            patch = next((
                item for item in PATCHES
                if str(item.get("change_number") or "") == run.patch_id
                and str(item.get("revision_sha") or "").lower()
                == run.revision.lower()
            ), None)
        if patch is not None:
            _observation, failures = _unknown_failures(patch)
            match = next((
                item for item in failures
                if str(item.get("remote_failure_id") or "")
                == str(request.get("suite_id") or "")
            ), None)
            suite_name = str((match or {}).get("suite") or "")
    return {
        "action_id": action.action_id,
        "action_type": kind,
        "state": action.status,
        "approval_state": "approved" if approval is not None else "pending",
        "run_id": run.run_id,
        "stage": action.status,
        "detail": action.failure_summary or "",
        "authority": "approval",
        "revision_sha": run.revision,
        "session_id": request.get("session_id", ""),
        "test_group": request.get("test_group", ""),
        "suite_name": suite_name,
        "suite_id": request.get("suite_id", ""),
        "jira_key": request.get("jira_ticket", ""),
        # The action budget is consumed when this action is planned.  Include
        # the already-reserved slot so a valid final planned action does not
        # render as if its own budget had disappeared.
        "action_budget_remaining": max(
            0, run.action_budget - run.action_count + (action.status == "planned")
        ),
        "bug_link_state": link_state,
        "version": action.created_at.isoformat() + ":" + action.status,
    }


def _pending_failure_actions(patch):
    if AUTOMATION_STORE is None:
        return []
    patch_id = str(patch.get("change_number") or "")
    result = []
    for run in reversed(AUTOMATION_STORE.list_runs(patch_id=patch_id)):
        for action in AUTOMATION_STORE.list_actions(run.run_id):
            if action.action_type not in {
                FAILURE_LINK_ACTION, FAILURE_RETEST_ACTION
            }:
                continue
            result.append((run, action, _failure_action_projection(run, action)))
    return result[:6]


def _research_and_failure_html(patch, *, show_policy_form=True):
    policy, failures, session, report = _research_context(patch)
    active = _revision_owner_session(patch)
    research_patch = dict(patch)
    research_patch.update({
        "has_unknown_failure": bool(failures),
        "active_run_id": active.run_id if active is not None else "",
        "active_research_run_id": (
            active.run_id
            if active is not None and active.run_id.startswith("pw-research-")
            else ""
        ),
    })
    sections = []
    if show_policy_form:
        sections.append(render_research_policy_form(
            research_patch,
            policy=policy,
            csrf_token=CSRF_TOKEN,
            idempotency_token=secrets.token_urlsafe(18),
        ))
    sections.append(render_unknown_failure_control(
            research_patch,
            policy=policy,
            csrf_token=CSRF_TOKEN,
            idempotency_token=secrets.token_urlsafe(18),
        ))
    if AUTOMATION_STORE is not None:
        try:
            decision = next((
                item for item in reversed(AUTOMATION_STORE.list_observations(
                    str(patch.get("change_number") or "")
                ))
                if item.revision.lower()
                == str(patch.get("revision_sha") or "").lower()
                and item.kind == "unknown_failure_research_trigger_decision"
            ), None)
        except AutomationNotFound:
            decision = None
        if decision is not None:
            sections.append(
                "<p class='research-trigger-decision'><strong>Latest trigger decision:</strong> "
                + escape(str(decision.payload.get("status") or "unknown"))
                + " — " + escape(str(decision.payload.get("reason") or ""))
                + "</p>"
            )
    if session is not None:
        if report is None:
            report = {
                "run_id": session.run_id,
                "state": session.state,
                "revision_sha": session.revision,
                "recommendation": "pending",
                "summary": "Research is still in progress.",
            }
        evidence_links = [
            {
                "label": item.get("evidence_ref", "Evidence"),
                "detail": item.get("supports", ""),
                "path": item.get("locator", ""),
            }
            for item in report.get("evidence_references") or []
            if isinstance(item, dict)
        ]
        sections.append(render_research_session(report, evidence=evidence_links))
    policy_mode = getattr(_retest_context(patch)[0], "mode", "disabled")
    if failures:
        proposal_rows = []
        for index, failure in enumerate(failures, 1):
            session_id = str(failure.get("session_id") or "")
            test_group = str(failure.get("test_group") or "")
            suite_name = str(failure.get("suite") or "")
            suite_id = str(failure.get("remote_failure_id") or "")
            identity_complete = all(
                MALOO_ID_RE.fullmatch(value)
                for value in (session_id, test_group, suite_name, suite_id)
            )
            disabled = policy_mode != "approval" or not identity_complete
            disabled_attr = " disabled aria-disabled='true'" if disabled else ""
            reason = (
                # Name the control that exists. The per-feature retest policy
                # form this text referred to is not rendered: the standing
                # policy is the single control, and _sync_standing_test_policy
                # derives the retest mode from it -- "approval" is exactly
                # Trigger=Manual with Tests set to anything but Off.
                "Set the standing policy Trigger to Manual and Tests to "
                "Deterministic or Investigate before planning writes."
                if policy_mode != "approval"
                else "The exact Maloo session, test group, suite name, or suite ID is unavailable."
            )
            proposal_rows.append(
                "<li><strong>" + escape(suite_name or f"Failure {index}")
                + "</strong> · session <code>" + escape(session_id)
                + "</code><form method='post' action='/failure-actions/plan'>"
                + f"<input type='hidden' name='csrf_token' value='{escape(CSRF_TOKEN, quote=True)}'>"
                + f"<input type='hidden' name='change_number' value='{escape(str(patch.get('change_number') or ''), quote=True)}'>"
                + f"<input type='hidden' name='patchset' value='{escape(str(patch.get('patchset') or ''), quote=True)}'>"
                + f"<input type='hidden' name='revision_sha' value='{escape(str(patch.get('revision_sha') or ''), quote=True)}'>"
                + f"<input type='hidden' name='session_id' value='{escape(session_id, quote=True)}'>"
                + f"<input type='hidden' name='test_group' value='{escape(test_group, quote=True)}'>"
                + f"<input type='hidden' name='suite_id' value='{escape(suite_id, quote=True)}'>"
                + "<label>Existing Jira key <input name='jira_ticket' required pattern='[A-Z][A-Z0-9_]*-[1-9][0-9]*' placeholder='LU-12345'></label>"
                + f"<button type='submit'{disabled_attr}>Plan association</button></form>"
                + (f"<p role='status'>{escape(reason)}</p>" if disabled else "")
                + "</li>"
            )
        sections.append(
            "<section class='failure-write-proposals'><h3>Operator-approved failure actions</h3>"
            "<p>Planning is inert. Associating the Jira key and requesting the retest "
            "are two separate, revision-pinned approvals.</p><ul>"
            + "".join(proposal_rows) + "</ul></section>"
        )
    for _run, _action, projection in _pending_failure_actions(patch):
        if (
            projection["state"] == "planned"
            and projection["approval_state"] == "pending"
        ):
            sections.append(render_failure_approval_card(projection))
        else:
            sections.append(render_failure_action_status(projection))
    return (
        "<details class='research-controls'><summary>Research and approved actions</summary>"
        + "".join(sections) + "</details>"
    )


def automation_html():
    """One card for every gate that lets Patch Watcher act without being asked.

    These were two top-level cards, "Automatic actions" and "Autonomous
    lanes", which read as two unrelated features. They are two gates over the
    same question, and both must be on before anything runs unattended: the
    first enables policies saved on individual patches, the second enables the
    one narrow rule that may then fire without a per-action approval.
    """

    return (
        "<section class='card automation'>"
        "<h2>Acting without being asked</h2>"
        "<p class='sub'>Both gates start off, and both must be on before Patch "
        "Watcher does anything to a patch without you approving it first. "
        "Per-patch settings live under each patch's own Actions.</p>"
        f"{global_retest_html(nested=True)}"
        f"{autonomous_lane_summary_html(nested=True)}"
        "</section>"
    )


def global_retest_html(nested=False):
    if AUTOMATION_STORE is None:
        return render_global_retest_status(
            execution_enabled=False,
            csrf_token=CSRF_TOKEN,
            recent_summary="Automation state is not initialized.",
            nested=nested,
        )
    setting = AUTOMATION_STORE.get_global_automation()
    events = automation_daily_events(limit=1)
    summary = ""
    if events:
        latest = events[-1]
        summary = (
            f"Latest: change {latest['patch_id']} · "
            f"{latest['event_type'].replace('_', ' ')} · {latest['summary']}"
        )
    return render_global_retest_status(
        execution_enabled=setting.enabled,
        csrf_token=CSRF_TOKEN,
        recent_summary=summary,
        nested=nested,
    )


RUN_KIND_LABELS = {
    "pw-engineer-": "engineering",
    "pw-review-": "review comment",
    "pw-build-": "build repair",
    "pw-research-": "failure research",
}
SESSION_TERMINAL_STATES = {
    "succeeded", "failed", "cancelled", "stale", "resource_exhausted",
}


# The controller starts the agent with capability_profile="full" only for
# these request kinds (`RunController._prepare_and_start`); everything else
# gets "read_only" -- tools Read/Glob/Grep, `--safe-mode --restricted`, and
# service credentials scrubbed from the environment.  The SESSION profile is a
# separate axis: `request_investigation` mints a read-only run whose session
# profile is still "engineering", so nothing about capability may be inferred
# from it.
FULL_CAPABILITY_REQUEST_KINDS = {"engineering", "review_comments", "build_failure"}
REQUEST_EVENT_TYPES = (
    "investigation_requested",
    RESEARCH_REQUEST_EVENT,
    ENGINEERING_REQUEST_EVENT,
    REVIEW_REQUEST_EVENT,
    BUILD_FAILURE_REQUEST_EVENT,
)


def _session_events(session_id, event_types):
    """Read one narrow slice of a session's durable events, tolerating absence."""
    if SESSION_STORE is None:
        return []
    try:
        return SESSION_STORE.list_events(session_id, event_types=event_types)
    except (AttributeError, TypeError, SessionNotFound):
        return []


def _capability_profile(session):
    """Return the capability profile the agent was actually started with.

    Derived from the immutable request event, exactly as the controller derives
    it. This is the only honest source for a safety statement: the session
    profile says "engineering" for a manual investigation that is granted no
    write capability at all.
    """
    for event in reversed(_session_events(session.session_id, REQUEST_EVENT_TYPES)):
        kind = event.payload.get("request_kind") if isinstance(
            event.payload, Mapping
        ) else None
        if kind in FULL_CAPABILITY_REQUEST_KINDS:
            return "full"
        return "read_only"
    return "read_only"


def _agent_process_pid(session):
    """Return the PID of this run's own agent process, not its wrapper's.

    `pw_runner_transport`, and the session row it updates, record
    `handle.host_identity.pid`. That is per-run -- `ClaudeHost.start` runs
    inside the spawned host process, so its `os.getpid()` is that wrapper, not
    Patch Watcher -- but it is the wrapper, whose process tree includes the
    agent plus the supervision around it. `claude_identity` on the same
    durable handle is the Claude process itself, which is what an operator
    asking "what is this run costing" actually wants, and it degrades to the
    session row when a run predates the handle event.
    """
    for event in reversed(_session_events(session.session_id, (RUNNER_HANDLE_EVENT,))):
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        handle = payload.get("handle")
        identity = handle.get("claude_identity") if isinstance(handle, Mapping) else None
        pid = identity.get("pid") if isinstance(identity, Mapping) else None
        if isinstance(pid, int) and pid > 0:
            return pid
        break
    return getattr(session, "pid", None)


def _checkout_index(session):
    """Return the pool checkout index this run holds, if it has one."""
    for event in _session_events(session.session_id, (CHECKOUT_ALLOCATED_EVENT,)):
        try:
            return int(event.payload["checkout_index"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _run_kind(run_id):
    """Name the kind of run a run_id encodes.

    The follow-up control starts a read-only investigation whatever kind the
    original run was, so the page has to be able to say which kind it is
    leaving behind.
    """
    for prefix, label in RUN_KIND_LABELS.items():
        if str(run_id or "").startswith(prefix):
            return label
    return "read-only investigation"


def _run_failure(session):
    """Return the recorded failure code and summary for a finished run.

    Both are stored on the terminal result and were rendered by no view at
    all, so a failed run's page explained nothing.
    """
    if SESSION_STORE is None or session.state not in SESSION_TERMINAL_STATES:
        return None, None
    try:
        terminal = SESSION_STORE.get_terminal_result(session.session_id)
    except SessionNotFound:
        return None, None
    if terminal is None:
        return None, None
    return terminal.failure_code, terminal.failure_summary


def _run_projection(session, *, now=None):
    """Project durable state plus bounded live telemetry for run views."""
    observed_at = now or datetime.now(UTC)
    messages = SESSION_STORE.recent_messages(session.session_id, limit=10)
    latest = messages[-1] if messages else None
    elapsed = max(0, (observed_at - session.started_at).total_seconds())
    absolute_remaining = max(
        0, (session.started_at + ABSOLUTE_RUNTIME_CAP - observed_at).total_seconds()
    )
    runtime_remaining = None
    inactivity_remaining = None
    if session.profile == "triage":
        runtime_remaining = max(
            0, (session.started_at + TRIAGE_WALL_LIMIT - observed_at).total_seconds()
        )
    elif session.state in {"preparing", "running"}:
        anchor = max(
            session.last_qualifying_activity_at,
            session.active_interval_started_at or session.last_qualifying_activity_at,
        )
        inactivity_remaining = max(
            0, (anchor + ENGINEERING_INACTIVITY_LIMIT - observed_at).total_seconds()
        )
    agent_pid = _agent_process_pid(session)
    memory = None
    if agent_pid:
        measured = collect_process_tree_rss(agent_pid)
        memory = measured.total_rss_bytes or measured.known_rss_bytes
    failure_code, failure_summary = _run_failure(session)
    return {
        "run_id": session.run_id,
        "run_kind": _run_kind(session.run_id),
        "failure_code": failure_code,
        "failure_summary": failure_summary,
        "session_id": session.session_id,
        "change_number": session.patch_id,
        "subject": session.patch_id,
        "patchset": session.patchset,
        "revision_sha": session.revision,
        "state": session.state,
        "profile": session.profile,
        "execution_profile": session.profile,
        # What the agent may actually do. The session profile is not it.
        "capability_profile": _capability_profile(session),
        # The run's own recorded choice, not whatever the controller default
        # happens to be now; a run started last week did not necessarily use
        # today's default.
        "model": (
            session.model
            or getattr(RUN_CONTROLLER, "model", "")
            or "Configured default"
        ),
        "effort": (
            session.effort
            or getattr(RUN_CONTROLLER, "effort", "")
            or "Configured default"
        ),
        "pid": agent_pid,
        "process_pid": agent_pid,
        "process_memory_bytes": memory,
        "started_at": session.started_at.isoformat(),
        "last_activity_at": session.last_qualifying_activity_at.isoformat(),
        "elapsed_seconds": elapsed,
        "runtime_remaining_seconds": runtime_remaining,
        "inactivity_remaining_seconds": inactivity_remaining,
        "absolute_remaining_seconds": absolute_remaining,
        "current_step": session.state.replace("_", " ").title(),
        "latest_message": latest,
        # The five run-control forms have always submitted expected_version,
        # and nothing supplied it or read it: it was hardcoded 0, so the
        # optimistic-concurrency guard those forms advertise could never have
        # discriminated anything. state_changed_at is the natural token --
        # microseconds so two transitions in the same second still differ.
        "version": _run_version(session),
    }


def _engineering_projection(session):
    """Join session, checkout, manifest, and captured evidence for the run views."""
    projection = _run_projection(session)
    projection["owner_id"] = owner_id_for_session(session.session_id)
    # Nothing stamps an LTVM owner id on a guest, so the reserved `co<N>-` name
    # prefix of the run's pool checkout is what actually establishes ownership
    # (see `RunController._register_ltvm_observations`). The view cannot
    # recompute it -- the index lives only in the immutable allocation event.
    checkout_index = _checkout_index(session)
    projection["checkout_index"] = checkout_index
    projection["vm_prefix"] = (
        checkout_vm_prefix(checkout_index) if checkout_index is not None else ""
    )
    # The durable resource rows, not the live LTVM inventory, are what record
    # a failed or abandoned cleanup: a guest whose destroy failed and then
    # vanished is invisible to `ltvm list` but still needs an operator.
    projection["owned_resources"] = [
        {
            "resource_id": item.resource_id,
            "resource_type": item.resource_type,
            "external_id": item.external_id,
            "owner_id": item.owner_id,
            "state": item.state,
            "cleanup_failure": item.cleanup_failure,
        }
        for item in SESSION_STORE.list_owned_resources(
            session_id=session.session_id
        )
    ]
    if RUN_CONTROLLER is None:
        return projection
    allocation = RUN_CONTROLLER.engineering_store.get_allocation_by_run(session.run_id)
    if allocation is not None:
        projection["checkout"] = {
            "state": allocation.state,
            "checkout_index": checkout_index,
            "revision_sha": allocation.revision_sha,
            "remote": allocation.repository_url,
            "base_branch": allocation.base_branch,
            "logical_path": "/work/source",
            "dedicated": allocation.checkout_kind == "full_clone",
            "initial_dirty": allocation.initial_dirty,
            "cleanup_state": allocation.state,
        }
    manifest = RUN_CONTROLLER.engineering_store.get_manifest(session.run_id)
    if manifest is not None:
        projection["manifest"] = {
            "schema_version": manifest.schema_version,
            "digest": manifest.digest,
            "ltvm_owner_id": projection["owner_id"],
            "build_steps": [],
            "test_steps": [
                {"name": item.step_id, "state": "requested", "target": "LTVM"}
                for item in manifest.commands
            ],
        }
    artifacts = RUN_CONTROLLER.engineering_store.list_artifacts(session.run_id)
    projection["artifacts"] = [
        {
            "artifact_id": item.artifact_id,
            "name": item.relative_path,
            "state": "captured",
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in artifacts if item.kind != "diff"
    ]
    projection["diffs"] = [
        {
            "artifact_id": item.artifact_id,
            "name": item.relative_path,
            "state": "captured",
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in artifacts if item.kind == "diff"
    ]
    validation_lookup = getattr(
        RUN_CONTROLLER.engineering_store,
        "get_validation_execution_by_run",
        None,
    )
    execution = (
        validation_lookup(session.run_id) if callable(validation_lookup) else None
    )
    if execution is not None:
        attempts = RUN_CONTROLLER.engineering_store.list_validation_attempts(
            execution.execution_id
        )
        attempt = attempts[0] if attempts else None
        cooldown = RUN_CONTROLLER.engineering_store.get_capacity_cooldown(
            session.patch_id
        )
        resources = SESSION_STORE.list_owned_resources(
            session_id=session.session_id
        )
        remaining = sum(
            resource.resource_type in {"ltvm_vm", "ltvm_cluster"}
            and resource.state not in {"cleaned", "destroyed"}
            for resource in resources
        )
        resource_exhaustion = None
        if attempt is not None and attempt.state == "resource_exhausted":
            resource_exhaustion = {
                "error_code": attempt.failure_code or "ltvm_resource_exhausted",
                "operation": "session-owned guest validation",
                "requested_resources": "exact-owner LTVM guest capacity",
                "evidence": attempt.summary or "LTVM capacity was exhausted",
            }
        cooldown_projection = None
        if cooldown is not None:
            observed_at = datetime.now(UTC)
            active_cooldown = cooldown.active_at(observed_at)
            cooldown_projection = {
                "state": "active" if active_cooldown else "expired",
                "retry_not_before": cooldown.not_before.isoformat(),
                "remaining_seconds": max(
                    0, int((cooldown.not_before - observed_at).total_seconds())
                ),
                "automation_suppressed": active_cooldown,
                "exhaustion_count": cooldown.consecutive_exhaustions,
            }
        projection["validation"] = {
            "execution_id": execution.execution_id,
            "attempt_id": attempt.attempt_id if attempt else None,
            "state": attempt.state if attempt else execution.state,
            "approval_state": execution.admission_state,
            "approved_by": execution.approved_by,
            "approved_at": (
                execution.approved_at.isoformat() if execution.approved_at else None
            ),
            "revision_sha": execution.revision_sha,
            "owner_id": execution.owner_id,
            "target": "any exact-owner session LTVM guest",
            "manifest_id": execution.manifest_id,
            "manifest_digest": execution.manifest_sha256,
            "resource_exhaustion": resource_exhaustion,
            "cooldown": cooldown_projection,
            "cleanup": {
                "state": "pending" if remaining else "clean",
                "owned_resources_remaining": remaining,
            },
        }
    return projection


def _snapshot_ltvm_vms():
    """Return the sampled LTVM guests from the shared resource snapshot.

    `refresh_resource_status` returns a `ResourceSnapshot` dataclass on the
    success path and a plain dict only when collection is disabled or has
    failed. Callers that tested `isinstance(snapshot, Mapping)` therefore saw
    an empty VM list exactly when sampling WORKED: every session-owned guest
    table and every orphan warning rendered empty on a healthy host. No test
    caught it because they all patch this function to return a dict.
    """
    snapshot = refresh_resource_status()
    if hasattr(snapshot, "to_dict"):
        snapshot = snapshot.to_dict()
    if not isinstance(snapshot, Mapping):
        return []
    ltvm = snapshot.get("ltvm") or {}
    if hasattr(ltvm, "to_dict"):
        ltvm = ltvm.to_dict()
    if not isinstance(ltvm, Mapping):
        return []
    return list(ltvm.get("vms") or ())


def _finished_run_row(item):
    failure_code, failure_summary = _run_failure(item)
    reason = failure_summary or failure_code or ""
    tone = "bad" if item.state != "succeeded" else "good"
    return (
        "<tr><td><a href='/runs/" + escape(item.run_id, quote=True) + "'>"
        + escape(item.run_id) + "</a>"
        + f"<div class='detail'>{escape(_run_kind(item.run_id))} run</div></td>"
        + "<td>" + escape(str(item.patch_id)) + "</td>"
        + "<td>" + _chip(item.state.replace("_", " ").capitalize(), tone,
                         title="Final run state")
        + "</td><td>" + escape(item.state_changed_at.isoformat(timespec="seconds"))
        + "</td><td>"
        + (f"<div class='error'>{escape(str(reason))}</div>" if reason else "\u2014")
        + "</td></tr>"
    )


def runs_html(limit=25):
    """List every run in one card: what is running now, and what has finished.

    This replaced three panels that overlapped rather than divided the runs
    between them.  "Agent runs" listed non-terminal runs of every kind,
    "Controlled engineering runs" listed ``pw-engineer-`` runs whether terminal
    or not, and "Finished runs" listed every terminal run -- so an active
    engineering run appeared in two panels at once, a finished one appeared in
    two others, and there was no single place that answered "what has this
    thing been doing".

    The engineering panel's per-run detail is not lost: it moved to
    ``/runs/<id>``, which every row here links to.  Its two statements that
    were never about one run -- the standing capability boundary and the
    unmatched-resource warning -- stay on this card.
    """
    if SESSION_STORE is None:
        return ""
    sessions = SESSION_STORE.list_sessions(include_terminal=True)
    active = [item for item in sessions if item.state not in SESSION_TERMINAL_STATES]
    finished = sorted(
        (item for item in sessions if item.state in SESSION_TERMINAL_STATES),
        key=lambda item: item.state_changed_at,
        reverse=True,
    )
    # Ownership warnings are an engineering-run concern: only those runs claim
    # a checkout, and only a checkout's reserved `co<N>-` prefix claims guests.
    engineering = [
        item for item in sessions
        if item.profile == "engineering" and item.run_id.startswith("pw-engineer-")
    ][:20]
    # Same timestamped sample the host-resource dashboard uses; `refresh_resource_status`
    # caches, so asking again here does not re-poll the host.
    vms = _snapshot_ltvm_vms()
    warnings = render_unmatched_resources(
        [_engineering_projection(item) for item in engineering],
        vms,
        heading_tag="h3",
    )
    if active:
        active_html = (
            "<div class='run-grid'>"
            + "".join(
                render_run_summary(
                    _run_projection(item), kind=_run_kind(item.run_id)
                )
                for item in active
            )
            + "</div>"
        )
    else:
        active_html = "<p class='empty'>Nothing is running.</p>"
    if finished:
        more = ""
        if len(finished) > limit:
            more = (
                f"<p class='detail'>Showing the {limit} most recent of "
                f"{len(finished)} finished runs.</p>"
            )
        finished_html = (
            "<details class='finished-runs'><summary>Finished runs "
            f"({len(finished)})</summary>{more}"
            "<table><thead><tr><th>Run</th><th>Patch</th><th>State</th>"
            "<th>Finished</th><th>Why it ended</th></tr></thead><tbody>"
            + "".join(_finished_run_row(item) for item in finished[:limit])
            + "</tbody></table></details>"
        )
    else:
        finished_html = "<p class='detail'>No run has finished yet.</p>"
    return (
        "<section class='card runs'>"
        f"<h2>Runs <small>({len(active)} running, {len(finished)} finished)</small></h2>"
        f"{render_capability_status('runs', standing=True)}{warnings}"
        f"{active_html}{finished_html}</section>"
    )


def _find_session_by_run_id(run_id):
    if SESSION_STORE is None:
        raise SessionNotFound(run_id)
    for session in SESSION_STORE.list_sessions(include_terminal=True):
        if session.run_id == run_id:
            return session
    raise SessionNotFound(run_id)


def _run_messages(session):
    messages = [
        {
            "author": item.author,
            "body": item.body,
            "created_at": item.created_at.isoformat(),
            "delivery_state": "recorded",
        }
        for item in SESSION_STORE.recent_messages(session.session_id, limit=20)
    ]
    for item in SESSION_STORE.list_guidance(session.session_id):
        messages.append({
            "author": "operator",
            "body": item.body,
            "created_at": item.created_at.isoformat(),
            "delivery_state": item.status,
        })
    return sorted(messages, key=lambda item: item["created_at"])


# Keys a controller event payload uses to say what happened, most specific
# first.  ``name`` is last because it identifies the resource rather than
# explaining it.
EVENT_SUMMARY_KEYS = (
    "summary", "detail", "reason", "failure_summary", "error_type",
    "runner_type", "name",
)
# Companion facts, and the labels they read as.  The destroy ladder
# (`ltvm_cleanup_failed`, `ltvm_cleanup_stuck`) and the runner stop ladder
# (`runner_stop_attempt`) record the SINGULAR ``attempt``; only their give-up
# events record the plural ``attempts``.  A summary that looked for the plural
# alone told an operator nothing about how far a failing guest had climbed,
# and `runner_stop_attempt` and `engineering_diff_salvaged` -- which carry no
# explanatory key at all -- rendered as the bare word "Recorded".
EVENT_SUMMARY_EXTRA_LABELS = (
    ("failure_type", "failure"),
    ("attempt", "attempt"),
    ("attempts", "attempts"),
    ("count", "count"),
    ("size_bytes", "bytes"),
)


def _event_summary(payload):
    """Describe a timeline event using whatever the writer actually recorded.

    Almost no controller event writes a "summary" key, so the three events that
    matter most -- controller_error, runner_stop_abandoned,
    ltvm_cleanup_abandoned -- all rendered as the word "Recorded" while their
    payloads carried the real explanation.
    """

    if not isinstance(payload, Mapping):
        return "Recorded"
    head = ""
    for key in EVENT_SUMMARY_KEYS:
        value = payload.get(key)
        if value:
            head = str(value)
            break
    extras = [
        f"{label}: {payload[key]}"
        for key, label in EVENT_SUMMARY_EXTRA_LABELS
        if payload.get(key) is not None
    ]
    if head and extras:
        return f"{head} ({', '.join(extras)})"[:500]
    if head:
        return head[:500]
    if extras:
        text = ", ".join(extras)
        return (text[0].upper() + text[1:])[:500]
    return "Recorded"


def _run_events(session):
    result = []
    for item in SESSION_STORE.list_events(session.session_id):
        payload = item.payload
        result.append({
            "event_type": item.event_type,
            "created_at": item.created_at.isoformat(),
            "summary": _event_summary(payload),
        })
    return result


def run_detail_html(session):
    questions = SESSION_STORE.list_human_questions(session.session_id)
    question = next((item for item in reversed(questions) if item.status == "open"), None)
    review_html = ""
    build_html = ""
    if RUN_CONTROLLER is not None:
        try:
            request = RUN_CONTROLLER._request_payload(session)
        except RunControllerError:
            request = {}
        if request.get("request_kind") == "review_comments":
            terminal = SESSION_STORE.get_terminal_result(session.session_id)
            report = terminal.result if terminal is not None else {}
            review_html = render_review_result(request, report)
        elif request.get("request_kind") == "build_failure":
            terminal = SESSION_STORE.get_terminal_result(session.session_id)
            report = terminal.result if terminal is not None else {}
            build_html = render_build_result(request, report)
    return render_run_detail(
        _run_projection(session),
        messages=_run_messages(session),
        events=_run_events(session),
        question=question,
        notices=_human_notices(session, question),
        csrf_token=CSRF_TOKEN,
        idempotency_token=secrets.token_urlsafe(18),
    ) + review_html + build_html + _engineering_detail_html(session)


def _human_notices(session, question):
    """Project the delivery ledger for this question onto the run page."""
    if question is None:
        return []
    return [
        {
            "channel": str(item.payload.get("channel", "")),
            "status": item.status,
            "detail": item.failure_summary or "",
            "at": (item.delivered_at or item.failed_at or item.created_at).isoformat(timespec="seconds"),
        }
        for item in SESSION_STORE.list_deliveries(session.session_id, kind="human_notice")
        if item.payload.get("question_id") == question.question_id
    ]


def _engineering_detail_html(session):
    """Render the checkout, guests, artifacts and manifest an engineering run owns.

    This is the detail the index page's engineering card used to show for every
    run at once.  Folding the three run panels into one list moved it here, to
    the page for the one run it describes -- without this the consolidation
    would have deleted the only view of a run's checkout state, owned guests,
    captured artifacts and prompt manifest.
    """
    if session.profile != "engineering" or not session.run_id.startswith("pw-engineer-"):
        return ""
    return render_engineering_run(
        _engineering_projection(session),
        vms=_snapshot_ltvm_vms(),
        messages=_run_messages(session),
        base_url="/runs",
        csrf_token=CSRF_TOKEN,
        idempotency_token=secrets.token_urlsafe(18),
    )


def _standalone_document(title, body):
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{escape(title)}</title><style>
body{{margin:0;
background:#f5f7fb;color:#172033;font:15px system-ui,sans-serif}}main{{max-width:1100px;margin:42px auto;padding:24px;background:white;border:1px solid #e4e7ec;border-radius:14px}}section{{border-top:1px solid #eaecf0;padding-top:18px;margin-top:18px}}dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}}dt{{font-size:12px;color:#667085}}dd{{margin:4px 0;word-break:break-word}}textarea{{width:min(720px,95%);min-height:90px;display:block;margin:8px 0}}button{{border:0;border-radius:8px;padding:10px 14px;background:#315efb;color:white;font-weight:600}}form.inline-control{{display:inline-block;margin:5px}}.danger-link,.danger{{color:#b42318}}.run-state{{display:inline-block;border-radius:999px;padding:4px 8px;background:#f2f4f7;margin:3px}}.tone-good{{background:#dcfce7}}.tone-warn{{background:#fef3c7}}.tone-bad{{background:#fee2e2}}.run-conversation,.run-timeline{{max-height:400px;overflow:auto}}.safety-note{{padding:10px;background:#eff8ff;border-radius:8px}}.notice{{background:#fffaeb;color:#b54708;border:1px solid #fedf89;padding:10px 12px;border-radius:8px;margin:0 0 16px}}.run-failure{{border-top:0;background:#fef3f2;border:1px solid #fecdca;border-radius:10px;padding:14px 16px;margin-top:16px}}.run-failure h3{{margin-top:0;color:#b42318}}.failure-summary{{font-weight:600}}.run-failure-line{{color:#b42318}}.control-note,.controls-unavailable{{color:#667085;font-size:13px}}code{{word-break:break-all}}</style></head><body>{body}</body></html>"""


def _standing_policy_html(patch):
    """One choice per patch: how far up the ladder Patch Watcher may go.

    This replaced four independent selects (trigger, tests, builds, reviews)
    plus a separate lane override.  Each level is a strict superset of the one
    below it, and the per-kind modes are derived from it, so there is nothing
    else to keep consistent.
    """
    try:
        policy = _standing_policy(patch)
    except (StandingPolicyError, ValueError) as exc:
        return "<p class='error'>Standing policy unavailable: " + escape(str(exc)) + "</p>"

    levels = list(PRESET_LEVELS)
    if policy.preset == "custom":
        levels.insert(0, "custom")
    options = "".join(
        "<option value='" + escape(level, quote=True) + "'"
        + (" selected" if level == policy.preset else "")
        + (" disabled" if level == "custom" else "")
        + ">" + escape(PRESET_LABELS[level]) + "</option>"
        for level in levels
    )
    global_enabled = bool(
        AUTOMATION_STORE is not None
        and AUTOMATION_STORE.get_global_automation().enabled
    )
    if policy.rank == 0 and policy.preset != "custom":
        gate_note = ""
    elif global_enabled:
        gate_note = " The global kill switch is <strong>on</strong>, so this level is live."
    else:
        gate_note = (
            " The global kill switch is <strong>off</strong>, so nothing runs unattended "
            "until it is <a href='/automation/global/confirm-enable'>turned on</a>; the "
            "level is saved and waiting. <strong>Run now</strong> applies it once regardless."
        )
    return (
        "<section class='standing-policy'><div class='policy-heading'>"
        "<strong>What Patch Watcher may do</strong><span class='availability'>"
        + escape(policy.label) + "</span></div>"
        "<form method='post' action='/standing-policy'>"
        f"<input type='hidden' name='csrf_token' value='{escape(CSRF_TOKEN, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(str(patch.get('change_number') or ''), quote=True)}'>"
        f"<input type='hidden' name='patchset' value='{escape(str(patch.get('patchset') or ''), quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(str(patch.get('revision_sha') or ''), quote=True)}'>"
        f"<input type='hidden' name='expected_version' value='{policy.version}'>"
        "<label>Level<select name='preset'>" + options + "</select></label>"
        "<button class='secondary' type='submit'>Save</button>"
        "</form>"
        # Run now applies the saved level once, because the operator asked:
        # the unattended gate does not apply to a button press.
        "<form class='run-now' method='post' action='/standing-policy/run-now'>"
        f"<input type='hidden' name='csrf_token' value='{escape(CSRF_TOKEN, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(str(patch.get('change_number') or ''), quote=True)}'>"
        f"<input type='hidden' name='patchset' value='{escape(str(patch.get('patchset') or ''), quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(str(patch.get('revision_sha') or ''), quote=True)}'>"
        "<button type='submit'"
        + (" disabled title='Watch only has nothing to run'" if policy.rank == 0 else
           " title='Apply the saved level to this patch once, now, kill switch or not'")
        + ">Run now</button></form>"
        "<p class='detail'>" + escape(policy.summary) + gate_note + "</p>"
        # Every rung, always visible: the difference between two neighbours
        # is the thing a person choosing between them needs, and it was only
        # shown for the one already saved.
        "<ul class='level-ladder'>"
        + "".join(
            "<li" + (" class='current'" if level == policy.preset else "") + ">"
            "<strong>" + escape(PRESET_LABELS[level]) + "</strong> — "
            + escape(PRESET_SUMMARIES[level]) + "</li>"
            for level in PRESET_LEVELS
        )
        + "</ul>"
        "<p class='detail'>Each level includes everything below it. Whatever the level, a "
        "run that needs a decision only you can make stops, tells you, and waits. Every "
        "write is real and made with your own service credentials: the agent posts its own "
        "Gerrit replies and uploads its own patchsets, and never votes, abandons, writes "
        "JIRA, or touches Jenkins.</p>"
        "</section>"
    )


def _lane_records():
    if AUTONOMOUS_LANE_HISTORY is None:
        return ()
    try:
        return AUTONOMOUS_LANE_HISTORY.list()
    except AutonomousLaneError as exc:
        log_structured_error("autonomous_lane_history", str(exc), "")
        return ()


def _lane_decision_projection(record):
    decision = record.decision
    identity = decision.identity
    return {
        **decision.to_dict(),
        "lane_name": decision.lane.name if decision.lane else "unavailable",
        "lane_version": decision.lane.version if decision.lane else "—",
        "change_number": identity.change_number,
        "patchset": identity.patchset,
        "revision_sha": identity.revision,
        "occurred_at": record.recorded_at,
        "state": "admitted" if decision.eligible else "rejected",
        "summary": decision.explanation,
    }


def autonomous_lane_summary_html(nested=False):
    if AUTONOMOUS_LANE_STORE is None:
        return render_autonomous_lane_summary(None, csrf_token=CSRF_TOKEN, nested=nested)
    try:
        controls = AUTONOMOUS_LANE_STORE.load()
        recent = [_lane_decision_projection(item) for item in _lane_records()[-8:]]
        replay = AUTONOMOUS_LANE_HISTORY.replay() if AUTONOMOUS_LANE_HISTORY else ()
        with PATCHES_LOCK:
            watched_projects = {
                str(item.get("project") or "") for item in PATCHES
                if item.get("project")
            }
        project_controls = {item.project: item for item in controls.projects}
        status = {
            "global_enabled": controls.global_enabled,
            "lane_name": DETERMINISTIC_RETEST_LANE,
            "lane_version": DETERMINISTIC_RETEST_VERSION,
            "expected_generation": controls.generation,
            "projects": [
                {
                    "project": project,
                    "mode": (
                        "inherit" if project not in project_controls
                        else "enabled" if project_controls[project].enabled else "disabled"
                    ),
                    "effective_enabled": bool(
                        controls.global_enabled
                        and project in project_controls
                        and project_controls[project].enabled
                    ),
                    "expected_generation": controls.generation,
                }
                for project in sorted(watched_projects | set(project_controls))
            ],
            "budgets": {
                "actions per exact revision": 1,
                "remote writes per exact revision": 1,
                "agent runs": 0,
            },
            "outcomes": recent,
            "replay": {
                "state": "complete" if replay else "not_run",
                "summary": (
                    f"{sum(item.matched for item in replay)}/{len(replay)} decisions match"
                    if replay else "No decisions recorded yet."
                ),
            },
        }
        return render_autonomous_lane_summary(status, csrf_token=CSRF_TOKEN, nested=nested)
    except AutonomousLaneError as exc:
        return "<section class='card'><h2>Unattended actions</h2><p class='error'>" + escape(str(exc)) + "</p></section>"


def _last_finished_session_for_patch(patch):
    """The most recently finished run on this patch, if any."""
    if SESSION_STORE is None:
        return None
    change_number = patch.get("change_number")
    if change_number is None:
        return None
    finished = [
        session for session in SESSION_STORE.list_sessions(include_terminal=True)
        if session.patch_id == str(change_number)
        and session.state in SESSION_TERMINAL_STATES
    ]
    return max(finished, key=lambda item: item.state_changed_at, default=None)


def _patch_now_html(patch):
    """Answer, first, the questions the panel used to leave open: is anything
    running on this patch, what will run without asking, and when was it last
    looked at.  A run's absence is stated, not left to be inferred from a
    missing chip three cards away.
    """
    change_number = patch.get("change_number")
    active = _active_session_for_patch(change_number) if change_number is not None else None
    if active is not None:
        href = "/runs/" + escape(active.run_id, quote=True)
        state = active.state.replace("_", " ")
        run_html = (
            f"<strong>A run is {escape(state)}:</strong> "
            f"<a href='{href}'>{escape(active.run_id)}</a> ({escape(_run_kind(active.run_id))})."
        )
        if active.state == "waiting_human":
            run_html += " <strong>It is waiting for you.</strong>"
    else:
        run_html = "<strong>No run</strong> on this patch."
    try:
        policy = _standing_policy(patch)
        if policy.rank == 0:
            level_html = f"Level <strong>{escape(policy.label)}</strong>: nothing runs unattended."
        else:
            gate = bool(AUTOMATION_STORE is not None and AUTOMATION_STORE.get_global_automation().enabled)
            level_html = (
                f"Level <strong>{escape(policy.label)}</strong>"
                + (": acts unattended when there is something to do, at the next check."
                   if gate else
                   ", but the global kill switch is off, so nothing runs unattended until it "
                   "is <a href='/automation/global/confirm-enable'>turned on</a> -- or you "
                   "press Run now.")
            )
    except (StandingPolicyError, ValueError):
        level_html = "Level unavailable."
    last = _last_finished_session_for_patch(patch)
    last_html = ""
    if last is not None:
        href = "/runs/" + escape(last.run_id, quote=True)
        last_html = (
            f" Last run: <a href='{href}'>{escape(last.run_id)}</a> "
            f"{escape(last.state.replace('_', ' '))} "
            f"{escape(last.state_changed_at.isoformat(timespec='minutes'))}."
        )
    try:
        interval = int(configured_refresh_interval())
    except Exception:
        interval = 300
    checked = str(patch.get("last_checked") or "—")
    return (
        "<section class='patch-now' aria-label='What is happening now'>"
        f"<p>{run_html} {level_html}{last_html}</p>"
        f"<p class='detail'>Gerrit last checked {escape(checked)}; checks every {interval} s.</p>"
        "</section>"
    )


def _waiting_session_for_patch(patch):
    """Return this patch's run that is paused on a human question, if any."""
    if SESSION_STORE is None:
        return None
    change_number = patch.get("change_number")
    if change_number is None:
        return None
    for session in SESSION_STORE.list_sessions(include_terminal=False):
        if session.patch_id == str(change_number) and session.state == "waiting_human":
            return session
    return None


def _needs_you_chip(patch):
    """The in-console "label": a run on this patch is waiting for a decision.

    Nothing is stored for it -- an open question on a ``waiting_human`` run IS
    the state -- so it can never go stale or disagree with the run page.
    """
    session = _waiting_session_for_patch(patch)
    if session is None:
        return ""
    href = "/runs/" + escape(session.run_id, quote=True)
    return (
        f"<a class='status-link' href='{href}' title='A run is paused on a question for you'>"
        + _chip("Needs you", "warn", title="A run is paused on a question for you")
        + "</a>"
    )


def _patches_needing_you():
    """How many watched patches have a run paused on a human question."""
    if SESSION_STORE is None:
        return 0
    return sum(
        1 for session in SESSION_STORE.list_sessions(include_terminal=False)
        if session.state == "waiting_human"
    )


def _patch_row(patch, jira_base=JIRA_BASE_URL):
    title = patch.get("title", "")
    ticket = ticket_from_title(title)
    ticket_html = ""
    if ticket:
        ticket_html = (
            f"<a class='ticket' href='{escape(jira_base.rstrip('/') + '/' + ticket, quote=True)}' "
            f"target='_blank' rel='noreferrer'>{escape(ticket)}</a>"
        )
    error_html = ""
    if patch.get("status_error"):
        error_html = f"<div class='error'>{escape(patch['status_error'])}</div>"
    patchset = escape(str(patch.get("patchset", "—")))
    patchset_html = f"<span>PS {patchset}</span>" if patchset != "—" else ""
    if patch.get("wip"):
        patchset_html += _chip("! WIP", "warn", title="Work in progress")
    active = _revision_owner_session(patch)
    investigation_patch = dict(patch)
    investigation_patch["active_run_id"] = active.run_id if active else ""
    investigation_patch["investigation_eligible"] = bool(
        patch.get("revision_sha") and patch.get("revision_ref") and patch.get("project")
    )
    if not investigation_patch["investigation_eligible"]:
        investigation_patch["investigation_disabled_reason"] = (
            "Refresh this patch to load its exact project, patchset, revision, and ref."
        )
    investigate_html = render_investigate_control(
        investigation_patch,
        csrf_token=CSRF_TOKEN,
        idempotency_token=secrets.token_urlsafe(18),
        compact=True,
    )
    engineering_patch = dict(investigation_patch)
    engineering_patch["engineering_eligible"] = bool(
        investigation_patch["investigation_eligible"] and active is None
    )
    if active is not None:
        engineering_patch["engineering_disabled_reason"] = (
            "A managed run already owns this patch."
        )
    elif not engineering_patch["engineering_eligible"]:
        engineering_patch["engineering_disabled_reason"] = (
            investigation_patch.get("investigation_disabled_reason")
            or "Refresh the exact Gerrit revision first."
        )
    engineering_html = render_engineering_start_control(
        engineering_patch,
        csrf_token=CSRF_TOKEN,
        idempotency_token=secrets.token_urlsafe(18),
        compact=True,
    )
    review_html = render_review_start_control(
        investigation_patch,
        csrf_token=CSRF_TOKEN,
        idempotency_token=secrets.token_urlsafe(18),
    )
    build_html = render_build_start_control(
        investigation_patch,
        None,
        csrf_token=CSRF_TOKEN,
        idempotency_token=secrets.token_urlsafe(18),
        build_eligible=bool(
            active is None
            and str(patch.get("jenkins") or "").upper() == "FAIL"
            and patch.get("jenkins_url")
            and investigation_patch["investigation_eligible"]
        ),
    )
    latest_build_run = _latest_session_for_patch_kind(
        patch.get("change_number"), "build_failure"
    )
    if latest_build_run is not None:
        build_html += (
            "<p class='detail'>Latest build run: <a href='/runs/"
            + escape(latest_build_run.run_id, quote=True) + "'>"
            + escape(latest_build_run.state.replace("_", " "))
            + "</a> · <code>" + escape(latest_build_run.run_id) + "</code></p>"
        )
    (
        retest_policy,
        retest_evaluation,
        retest_timeline,
        retest_approval,
    ) = _retest_context(patch)
    retest_html = render_retest_control(
        patch,
        retest_policy,
        evaluation=retest_evaluation,
        timeline=retest_timeline,
        approval_action=retest_approval,
        csrf_token=CSRF_TOKEN,
        show_policy_form=False,
    )
    research_html = _research_and_failure_html(patch, show_policy_form=False)
    standing_policy_html = _standing_policy_html(patch)
    identity = "patch-actions-" + escape(
        f"{patch.get('change_number', 'unknown')}-{patch.get('patchset', 'unknown')}",
        quote=True,
    )
    action_policy_html = (
        f"<details class='patch-actions' id='{identity}'>"
        "<summary>Actions for this patch</summary>"
        f"{_patch_now_html(patch)}"
        f"{standing_policy_html}"
        "<section class='manual-runs' aria-label='Start a run by hand'>"
        "<div class='policy-heading'><strong>Start a run by hand</strong></div>"
        "<p class='detail'>One-off runs on the exact pinned revision, whatever the level. "
        "A greyed button says why it cannot start.</p>"
        "<div class='action-policy-grid'>"
        # Generic runs: look at anything, or fix anything.
        "<section class='action-policy-item' aria-label='Investigate'>"
        "<div class='policy-heading'><strong>Investigate</strong></div>"
        "<p class='detail'>Reads the pinned source and reports, with file references. "
        "Cannot change files, run commands, or reach Gerrit or CI.</p>"
        f"{investigate_html}</section>"
        "<section class='action-policy-item' aria-label='Engineering run'>"
        "<div class='policy-heading'><strong>Engineering run</strong></div>"
        "<p class='detail'>A writable checkout, a shell and VMs; builds and tests, and "
        "leaves a diff for your review without uploading it.</p>"
        f"{engineering_html}</section>"
        # Specific runs: each bound to one signal on the patch.
        "<section class='action-policy-item' aria-label='Build failure handling'>"
        "<div class='policy-heading'><strong>Build failures</strong></div>"
        f"{build_html}</section>"
        "<section class='action-policy-item' aria-label='Test failure handling'>"
        "<div class='policy-heading'><strong>Test failures</strong></div>"
        "<p class='detail'>Retest a failure already classified as known, or research one "
        "that is not.</p>"
        f"{retest_html}{research_html}</section>"
        "<section class='action-policy-item' aria-label='Review comment handling'>"
        "<div class='policy-heading'><strong>Review comments</strong></div>"
        f"{review_html}</section>"
        "</div></section>"
        f"<form class='quick-action remove-patch' method='post' action='/remove'>"
        f"<input type='hidden' name='csrf_token' value='{CSRF_TOKEN}'>"
        f"<input type='hidden' name='url' value='{escape(patch['url'], quote=True)}'>"
        "<button class='danger' type='submit'>Remove…</button></form>"
        "</details>"
    )
    return (
        "<tr><td>"
        f"<a href='{escape(patch['url'], quote=True)}' target='_blank' rel='noreferrer'>"
        f"{escape(title)}</a>{ticket_html}"
        f"<div class='url'>{escape(patch['url'])}</div>"
        f"<div class='patch-meta'>{patchset_html}</div>{error_html}"
        f"{_refresh_errors_html(patch)}</td>"
        f"<td>{_watch_chip(patch.get('watch_state', 'uninitialized'))}{_needs_you_chip(patch)}"
        f"<div class='detail'>{escape(str(patch.get('recommendation', '')))}</div>"
        f"<div class='ci-stack'>{_ci_chip('Jenkins', patch.get('jenkins', '—'), patch.get('jenkins_url', ''))}"
        f"{_ci_chip('Maloo', patch.get('maloo', '—'), patch.get('maloo_url', ''))}</div></td>"
        f"<td>{_review_chip(patch)}"
        f"<div class='detail'>{escape(_vote_summary(patch))} · "
        f"{escape(str(patch.get('unresolved', 0)))} unresolved</div></td>"
        f"<td>{escape(patch.get('change_summary', '—') or '—')}"
        f"<div class='detail'>Changed: {escape(patch.get('last_changed', '—') or '—')}</div>"
        f"{_history_html(patch)}</td>"
        f"<td>{action_policy_html}</td></tr>"
    )


def _forget_patch_automation(patch):
    """Drop the standing policy of a patch that is no longer watched.

    Removing a patch dropped it from the watch list and left its standing
    policy on disk, keyed by change number. Re-adding the same change -- the
    normal way to resume after a rebase or a mistaken removal -- silently
    reactivated whatever it had been set to, up to and including
    trigger_mode=automatic, with no confirmation and nothing on screen saying
    so. The operator's removal withdraws that consent, so the policy goes with
    it.

    The standing policy is the source of truth for the derived retest and
    research modes, so with it gone the next refresh syncs those back to
    disabled: this one deletion is enough.
    """

    patch_id = str(patch.get("change_number") or "")
    if not patch_id or STANDING_POLICY_STORE is None:
        return
    try:
        STANDING_POLICY_STORE.remove(patch_id)
    except (StandingPolicyError, OSError) as exc:
        # Never fail the removal the operator asked for: the patch is already
        # out of the watch list by the time we get here.
        log_structured_error(
            "standing_policy_remove_failed", f"{patch_id}: {exc}", ""
        )


def ltvm_guest_name(value):
    """Validate a guest name that arrived from a form and becomes an argv entry.

    The name is displayed from an `ltvm list --json` this tool did not write,
    so it is untrusted on the way back in; the adapter validates again before
    the subprocess, and this gives the operator a readable refusal rather than
    a 500.
    """

    name = str(value or "").strip()
    if not name or not LTVM_GUEST_NAME_RE.fullmatch(name):
        raise ValueError("that is not a valid LTVM guest name")
    return name


def _destroy_vm_confirmation_html(name):
    """Ask before an irreversible destroy of a guest Patch Watcher does not own."""

    expires_at = str(int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS)
    confirmation = _signed_confirmation("destroy-vm", name, expires_at)
    return (
        "<main class='remove-confirmation'>"
        "<p><a href='/'>&#8592; Leave this guest alone</a></p>"
        "<h2>Destroy this LTVM guest?</h2>"
        "<p role='alert'>This runs <code>sudo ltvm destroy</code> immediately "
        "and cannot be undone. Any work inside the guest, and any test run "
        "using it, is lost. Patch Watcher does not own this guest and has no "
        "way to tell whether something else on this host is using it.</p>"
        f"<p><strong>{escape(name)}</strong></p>"
        "<form method='post' action='/vms/destroy'>"
        f"<input type='hidden' name='csrf_token' value='{escape(CSRF_TOKEN, quote=True)}'>"
        f"<input type='hidden' name='name' value='{escape(name, quote=True)}'>"
        f"<input type='hidden' name='confirmation_token' value='{escape(confirmation, quote=True)}'>"
        f"<input type='hidden' name='confirmation_expires_at' value='{escape(expires_at, quote=True)}'>"
        "<button type='submit' class='danger'>Destroy this guest</button></form></main>"
    )


def _remove_confirmation_html(patch):
    """Describe what removing a watched patch does to any run on it.

    Remove was the only one-click mutation on the dashboard: styled exactly
    like "Kill session", which has two confirmations, but with none of its own.
    It also silently detaches a live run -- and because revision reconciliation
    is driven over the watched-patch list, once the patch is gone that run can
    never be marked stale and keeps its real Gerrit-write credentials.
    """
    url = str(patch.get("url") or "")
    expires_at = str(int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS)
    confirmation = _signed_confirmation("remove-patch", url, expires_at)
    active = _active_session_for_patch(patch.get("change_number"))
    if active is None:
        run_html = "<p>No managed run is currently active on this patch.</p>"
    else:
        run_path = "/runs/" + escape(active.run_id, quote=True)
        run_html = (
            "<div class='notice' role='alert'><strong>Run <code>"
            + escape(active.run_id) + "</code> is still active on this patch ("
            + escape(active.state.replace("_", " "))
            + ").</strong> Removing the patch does NOT stop it. It keeps "
            "running with real Gerrit and CI credentials against pinned "
            "revision <code>" + escape(str(active.revision or "unknown"))
            + "</code>, and because revision reconciliation walks the "
            "watched-patch list, once this patch is gone the run can never be "
            "marked stale.<br>Stop the run first: <a href='" + run_path
            + "'>open the run</a> · <a href='" + run_path
            + "/confirm?intent=cancel'>review stop and cancel</a> · <a href='"
            + run_path + "/confirm?intent=kill'>review kill session</a>.</div>"
        )
    return (
        "<main class='remove-confirmation'>"
        "<p><a href='/'>← Keep watching this patch</a></p>"
        "<h2>Confirm removing a watched patch</h2>"
        "<p role='alert'>Removing stops Patch Watcher polling this change, "
        "drops its recorded status history from the watch list, and clears "
        "its standing automation policy -- so re-adding this change later "
        "starts from the defaults rather than silently resuming whatever it "
        "was set to. It does not change anything in Gerrit, and it does not "
        "stop anything already running.</p>"
        f"<p><strong>{escape(str(patch.get('title') or url))}</strong><br>"
        f"<code>{escape(url)}</code></p>" + run_html
        + "<form method='post' action='/remove'>"
        f"<input type='hidden' name='csrf_token' value='{escape(CSRF_TOKEN, quote=True)}'>"
        f"<input type='hidden' name='url' value='{escape(url, quote=True)}'>"
        f"<input type='hidden' name='confirmation_token' value='{escape(confirmation, quote=True)}'>"
        f"<input type='hidden' name='confirmation_expires_at' value='{escape(expires_at, quote=True)}'>"
        "<button type='submit' class='danger'>Remove this patch from the "
        "watch list</button></form></main>"
    )


def _degraded_patch_row(patch, exc):
    """Render one unrenderable patch as a removable row, not a blank page."""

    url = str(patch.get("url") or "")
    return (
        "<tr><td>"
        + (
            f"<a href='{escape(url, quote=True)}' target='_blank' rel='noreferrer'>"
            f"{escape(str(patch.get('title') or url or 'Unknown patch'))}</a>"
            f"<div class='url'>{escape(url)}</div>"
        )
        + "<div class='error'>This patch could not be rendered: "
        + escape(f"{type(exc).__name__}: {exc}")
        + "</div>"
        + (
            "<form class='quick-action' method='post' action='/remove'>"
            f"<input type='hidden' name='csrf_token' value='{escape(CSRF_TOKEN, quote=True)}'>"
            f"<input type='hidden' name='url' value='{escape(url, quote=True)}'>"
            "<button class='danger' type='submit'>Remove…</button></form>"
            if url else ""
        )
        + "</td><td colspan='4' class='detail'>Controls are unavailable for this row."
        "</td></tr>"
    )


def _safe_patch_row(patch, jira_base=JIRA_BASE_URL):
    """Contain a rendering failure to the one patch that caused it.

    A single malformed Gerrit value used to raise out of page() and, because
    the status line had already been sent, hand the operator a blank HTTP 200
    with no error and no CSRF token to act on.
    """

    try:
        return _patch_row(patch, jira_base)
    except Exception as exc:
        # Deliberately broad: one bad row must not cost the whole dashboard.
        log_structured_error(
            "patch_row_render_failed", str(exc), str(patch.get("url") or "")
        )
        return _degraded_patch_row(patch, exc)


def page(message="", jira_base=JIRA_BASE_URL):
    refresh_interval = configured_refresh_interval()
    resources = controller_failures_html() + resource_dashboard_html()
    needing = _patches_needing_you()
    needs_you_html = f" · <strong>{needing} need{'s' if needing == 1 else ''} you</strong>" if needing else ""
    with PATCHES_LOCK:
        patches = [dict(patch) for patch in PATCHES]
    rows = "".join(
        _safe_patch_row(patch, jira_base) for patch in patches
    ) or "<tr><td colspan='5' class='empty'>No patches yet. Add a Gerrit change to start watching.</td></tr>"
    refresh_health = refresh_failure_summary()
    refresh_health_html = (
        f"<div class='detail refresh-health'>{escape(refresh_health)}</div>"
        if refresh_health else ""
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Patch Watcher</title><style>
body{{margin:0;
background:#f5f7fb;color:#172033;font:15px system-ui,sans-serif}}main{{max-width:1450px;margin:48px auto;padding:0 24px}}h1{{margin-bottom:6px}}.sub{{color:#667085;margin-top:0}}.card,.resource-card{{background:white;border:1px solid #e4e7ec;border-radius:14px;padding:22px;margin-top:28px;box-shadow:0 4px 16px #1018280a;overflow-x:auto}}form.add{{display:flex;gap:10px;flex-wrap:wrap}}input,textarea,select{{border:1px solid #d0d5dd;border-radius:8px;padding:11px 12px;font-size:14px}}input,textarea{{flex:1;min-width:240px}}textarea{{display:block;width:min(620px,95%);min-height:70px;margin:7px 0 10px}}button,.button-link{{border:0;border-radius:8px;padding:11px 16px;background:#315efb;color:white;font-weight:600;cursor:pointer}}.button-link{{display:inline-block;text-decoration:none}}button:disabled{{cursor:not-allowed;opacity:.68}}button.danger,button.secondary,.button-link.danger-link{{background:#fff;padding:7px 11px}}button.danger,.button-link.danger-link{{color:#b42318;border:1px solid #fecdca}}button.secondary{{color:#344054;border:1px solid #d0d5dd}}table{{width:100%;border-collapse:collapse;margin-top:18px;min-width:1050px}}th,td{{text-align:left;padding:14px 10px;border-top:1px solid #eaecf0;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:#667085}}.url,.detail{{color:#667085;font-size:12px;margin-top:4px;word-break:break-word}}.patch-meta{{display:flex;align-items:center;gap:6px;color:#667085;font-size:12px;margin-top:7px}}.ticket{{display:inline-block;margin-left:8px;font-size:12px}}.error{{color:#b42318;font-size:12px;margin-top:5px;max-width:340px}}.empty{{text-align:center;color:#667085;padding:35px}}.notice{{background:#fffaeb;color:#b54708;padding:10px 12px;border-radius:8px;margin-top:16px}}.section-title{{display:flex;justify-content:space-between;align-items:center;gap:16px}}small{{display:block;color:#667085;margin-top:4px}}details{{margin-top:7px;font-size:12px;color:#475467}}details ol{{padding-left:18px;max-height:140px;overflow:auto}}details li{{margin:5px 0}}details time{{font-variant-numeric:tabular-nums}}.history-state{{color:#667085}}.status-chip,.resource-status{{display:inline-block;border:1px solid transparent;border-radius:999px;padding:3px 8px;font-size:12px;font-weight:700;line-height:1.35;white-space:nowrap}}.tone-good{{background:#dcfce7;border-color:#86efac;color:#166534}}.tone-bad{{background:#fee2e2;border-color:#fca5a5;color:#991b1b}}.tone-warn{{background:#fef3c7;border-color:#fcd34d;color:#78350f}}.tone-info{{background:#dbeafe;border-color:#93c5fd;color:#1e3a8a}}.tone-neutral{{background:#f2f4f7;border-color:#d0d5dd;color:#344054}}.status-link{{text-decoration:none}}.status-link:focus-visible .status-chip{{outline:3px solid #315efb;outline-offset:2px}}.ci-stack{{display:flex;align-items:flex-start;gap:5px;flex-wrap:wrap;margin-top:8px}}.patch-actions{{width:min(720px,80vw)}}.patch-actions>summary{{cursor:pointer;display:inline-flex;align-items:center;border:1px solid #d0d5dd;border-radius:8px;padding:7px 11px;background:white;color:#344054;font-weight:700}}.quick-actions{{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}}.level-ladder{{margin:10px 0 4px;padding-left:18px;font-size:12px;color:#475467}}.level-ladder li{{margin:3px 0}}.level-ladder li.current{{color:#172033}}.level-ladder li.current strong{{background:#dbeafe;border-radius:4px;padding:0 4px}}.patch-now{{border:1px solid #d0d5dd;border-radius:10px;padding:10px 12px;margin:12px 0;background:#fff;font-size:13px}}.patch-now p{{margin:0}}.patch-now .detail{{margin-top:4px}}.manual-runs{{margin:12px 0}}.manual-runs>.policy-heading{{margin-bottom:2px}}.remove-patch{{margin-top:10px}}.run-now{{display:inline-block;margin:6px 0 0}}.run-now button{{padding:7px 12px}}.standing-policy form.run-now{{display:inline-block;grid-template-columns:none}}.action-policy-item .quick-action{{margin-top:8px}}.quick-action{{margin:0}}.standing-policy{{border:1px solid #b2ccff;border-radius:10px;padding:10px;background:#f5f8ff;margin:10px 0}}.standing-policy form{{display:grid;grid-template-columns:repeat(4,minmax(110px,1fr)) auto;gap:7px;align-items:end;margin-top:8px}}.standing-policy label{{display:grid;gap:3px}}.standing-policy select{{min-width:0;width:100%;padding:7px}}.action-policy-grid{{display:grid;grid-template-columns:repeat(3,minmax(190px,1fr));gap:10px}}.action-policy-item{{border:1px solid #d0d5dd;border-radius:10px;padding:10px;background:#fff}}.action-policy-item.unavailable{{background:#f8fafc;color:#667085}}.policy-heading{{display:flex;justify-content:space-between;gap:8px;align-items:center}}.availability{{border:1px solid #d0d5dd;border-radius:999px;padding:2px 6px;font-size:10px;text-transform:uppercase;font-weight:700}}.available .availability{{background:#dcfce7;border-color:#86efac;color:#166534}}.retest-control,.research-controls{{width:auto;padding:6px 8px;border:1px solid #d0d5dd;border-radius:8px}}.agent-choice{{display:grid;grid-template-columns:repeat(2,minmax(140px,1fr));gap:7px;margin:10px 0;align-items:end}}.agent-choice label{{font-size:12px;color:#667085}}.agent-choice input,.agent-choice select{{box-sizing:border-box;min-width:0;width:100%;padding:7px 8px}}@media(max-width:600px){{.agent-choice{{grid-template-columns:1fr}}}}.retest-control form{{display:grid;gap:7px;margin-top:9px}}.retest-control label{{display:grid;gap:4px}}.retest-control input,.retest-control select{{box-sizing:border-box;min-width:0;width:100%;padding:7px 8px}}.retest-decision,.retest-approval{{display:grid;gap:6px;margin-top:9px;padding:8px;background:#f8fafc;border-radius:7px}}.retest-approval{{background:#fffaeb}}.retest-timeline{{padding-left:18px}}.retest-global form{{margin-top:12px}}.runs .empty{{padding:18px}}.run-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin-top:16px}}.run-summary{{border:1px solid #eaecf0;border-radius:10px;padding:14px;background:#f8fafc}}.run-summary header{{display:flex;justify-content:space-between;align-items:center;gap:10px}}.run-summary h3{{margin:0;font-size:15px}}.run-summary p{{font-size:13px;word-break:break-word}}.run-metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:12px 0}}.run-metrics dt{{font-size:11px;color:#667085}}.run-metrics dd{{margin:2px 0;font-size:13px;word-break:break-word}}.finished-runs{{margin-top:20px;font-size:15px;color:inherit}}.finished-runs>summary{{cursor:pointer;font-size:13px;color:#475467;font-weight:600}}.finished-runs table{{min-width:0}}.engineering-capabilities{{background:#f8fafc;border:1px solid #eaecf0;border-radius:10px;padding:10px 14px;margin-top:14px;font-size:13px}}.engineering-capabilities h3{{margin:0 0 6px;font-size:13px;text-transform:uppercase;color:#667085}}.engineering-capabilities ul{{margin:0;padding-left:18px}}.engineering-capabilities p{{color:#667085;margin-bottom:0}}.orphan-vms{{border:1px solid #fecdca;background:#fef3f2;border-radius:10px;padding:12px 14px;margin-top:14px}}.orphan-vms h3{{margin:0 0 6px;color:#b42318;font-size:15px}}.orphan-vms ul{{margin:0;padding-left:18px;font-size:13px}}.orphan-ok{{color:#027a48;font-size:13px;margin-top:12px}}.resource-toolbar{{display:flex;justify-content:flex-end;margin-top:20px}}.resource-dashboard{{display:grid;gap:18px}}.resource-card{{margin-top:0}}.resource-metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.other-vms-headline{{font-size:15px;margin:6px 0 0;color:#344054}}.other-vms-detail{{margin-top:12px;font-size:15px;color:inherit}}.other-vms-detail>summary{{cursor:pointer;font-size:13px;color:#475467;font-weight:600}}.vm-controls{{white-space:nowrap}}.vm-controls form{{display:inline-block;margin:0 4px 0 0}}.vm-controls button{{font-size:12px;padding:5px 9px}}.host-memory-headline{{font-size:17px;margin:6px 0 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}.host-memory-detail{{margin-top:14px;font-size:15px;color:inherit}}.host-memory-detail>summary{{cursor:pointer;font-size:13px;color:#475467;font-weight:600}}.resource-metric{{background:#f8fafc;border:1px solid #eaecf0;border-radius:10px;padding:12px}}.resource-metric dt{{font-size:12px;color:#667085}}.resource-metric dd{{margin:5px 0 0;font-size:18px;font-weight:700}}.resource-errors{{color:#b42318}}.resource-ok{{color:#027a48}}.session-controls{{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-top:16px}}fieldset{{border:1px solid #fecdca;border-radius:8px}}.message-content{{white-space:pre-wrap;margin-top:3px}}.run-failure{{background:#fef3f2;border:1px solid #fecdca;border-radius:10px;padding:12px 14px}}.run-failure h3{{margin-top:0;color:#b42318}}.run-failure-line{{color:#b42318}}.control-note,.controls-unavailable{{color:#667085;font-size:12px}}.refresh-health{{color:#b42318}}@media(max-width:980px){{.action-policy-grid{{grid-template-columns:1fr}}.standing-policy form{{grid-template-columns:repeat(2,minmax(140px,1fr))}}}}@media(max-width:760px){{.session-controls{{grid-template-columns:1fr}}}}</style></head>
<body><style>.research-controls{{width:min(430px,88vw);
border:1px solid #d0d5dd;border-radius:8px;padding:8px}}.research-controls>summary{{cursor:pointer;font-weight:700}}.research-controls section{{border-top:1px solid #eaecf0;margin-top:10px;padding-top:10px}}.research-controls form{{display:grid;gap:7px;margin-top:8px}}.research-controls input,.research-controls select{{box-sizing:border-box;min-width:0;width:100%;padding:7px 8px}}.research-controls dl{{display:grid;gap:6px}}.research-controls dd{{margin:2px 0 6px;word-break:break-word}}.action-approval-card{{background:#fffaeb;border:1px solid #fedf89;border-radius:8px;padding:10px}}</style><main><h1>Patch Watcher</h1><p class='sub'>Track Gerrit patches, managed sessions, and worker resources.</p>
<section class='card'><div class='section-title'><div><h2>Watched patches <small>({len(patches)} · checks every {refresh_interval}s{needs_you_html})</small></h2><div class='detail'>Last successful check: {escape(overall_last_successful_check())}</div><div class='detail'>Last check attempt: {escape(overall_last_checked())}</div>{refresh_health_html}</div><div class='actions'><form method='post' action='/refresh-all'><input type='hidden' name='csrf_token' value='{CSRF_TOKEN}'><button class='secondary'>Refresh all</button></form><form method='post' action='/email'><input type='hidden' name='csrf_token' value='{CSRF_TOKEN}'><button class='secondary'>Send status email</button></form></div></div><table><thead><tr><th>Patch</th><th>Watch state / CI</th><th>Review</th><th>Latest change</th><th></th></tr></thead><tbody>{rows}</tbody></table></section>
<section class='card'><h2>Add a patch</h2><form class='add' method='post' action='/add'><input type='hidden' name='csrf_token' value='{CSRF_TOKEN}'><input name='url' required placeholder='https://review.whamcloud.com/c/...'><button>Add patch</button></form>{f"<div class='notice'>{escape(message)}</div>" if message else ''}</section>
<div class='resource-toolbar'><form method='post' action='/resources/refresh'><input type='hidden' name='csrf_token' value='{CSRF_TOKEN}'><button class='secondary'>Refresh resource status</button></form></div>{resources}
{runs_html()}{automation_html()}

</main></body></html>"""


def load_seed_file(path=DEFAULT_SEED_FILE):
    """Load ``URL<TAB>optional title`` lines into the in-memory watch list."""
    seed_path = Path(path)
    if not seed_path.exists():
        return []
    loaded = []
    for raw_line in seed_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, title = line.partition("\t")
        patch, error = add_patch(url, title)
        if error and "already" not in error:
            raise ValueError(f"Invalid seed entry {url!r}: {error}")
        if patch:
            refresh_watched_patch(patch)
            loaded.append(patch)
    return loaded


def save_watch_file(path=DEFAULT_SEED_FILE):
    """Atomically persist the current watch list as private URL-only config.

    Both callers are HTTP handlers, so two of them run concurrently the moment
    an operator has two tabs open or double-clicks. A shared fixed temp name
    made that lose the whole list: the second writer's ``open(..., "w")``
    truncates the file the first is about to publish, and the first then
    renames those zero bytes over the real watch list. Snapshot and publish
    are serialized under one lock for the same reason -- otherwise the older
    of two snapshots can win the race to ``replace`` and silently undo an add.
    """

    watch_path = Path(path)
    watch_path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    with WATCH_FILE_LOCK:
        with PATCHES_LOCK:
            contents = "".join(f"{patch['url']}\n" for patch in PATCHES)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=watch_path.parent,
                prefix=f".{watch_path.name}.",
                delete=False,
            ) as stream:
                pending = Path(stream.name)
                os.chmod(pending, 0o600)
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, watch_path)
            pending = None
        finally:
            if pending is not None:
                with contextlib.suppress(OSError):
                    pending.unlink()


# Serializes watch-file publication; always taken before PATCHES_LOCK.
WATCH_FILE_LOCK = threading.Lock()


ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def _allowed_host(host_header, port):
    """True when the Host header names this loopback service.

    Without this the server answers to any name. A page on evil.com whose DNS
    rebinds to 127.0.0.1 is then same-origin to the browser, so it can READ the
    dashboard body -- and with it the CSRF token -- rather than merely posting
    to it blind.
    """

    host = str(host_header or "").strip()
    if not host:
        return False
    if host.startswith("["):                      # [::1]:8080
        name, _, rest = host.partition("]")
        name, given = name + "]", rest.lstrip(":")
    else:
        name, _, given = host.partition(":")
    if name not in ALLOWED_HOSTS:
        return False
    return not given or given == str(port)


_REQUEST_LINE_RE = re.compile(r"^([A-Za-z]+) (\S+?)(\?\S*)?( HTTP/[0-9.]+)?$")


def _redact_request_line(value):
    """Drop the query string from a logged HTTP request line.

    The confirm-start URLs carry ``confirmation_token`` in their query string
    and BaseHTTPRequestHandler writes the whole request line to stderr, so the
    default log leaks a live confirmation token to anything that can read the
    server's output.  Only the path is worth logging.
    """

    # Return non-str arguments untouched: log_error() passes an int status
    # code through a "%d" format, so stringifying everything here would raise
    # inside the logger and kill the connection.
    if not isinstance(value, str):
        return value
    match = _REQUEST_LINE_RE.match(value)
    if match is None or match.group(3) is None:
        return value
    return match.group(1) + " " + match.group(2) + "?<redacted>" + (match.group(4) or "")


ERROR_PAGE_FORMAT = """<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Patch Watcher error %(code)d</title><style>
body{margin:0;background:#f5f7fb;color:#172033;font:15px system-ui,sans-serif}
main{max-width:640px;margin:64px auto;padding:24px;background:#fff;border:1px solid #e4e7ec;border-radius:14px}
h1{margin:0 0 6px;font-size:20px;color:#b42318}
.code{color:#667085;font-size:13px;margin:0 0 18px}
.explain{background:#fffaeb;color:#b54708;border:1px solid #fedf89;padding:10px 12px;border-radius:8px}
a.back{display:inline-block;margin-top:20px;border-radius:8px;padding:11px 16px;background:#315efb;color:#fff;font-weight:600;text-decoration:none}
</style></head><body><main><h1>%(message)s</h1>
<p class='code'>Patch Watcher refused this request (HTTP %(code)d).</p>
<p class='explain'>%(explain)s</p>
<a class='back' href='/'>Return to Patch Watcher</a></main></body></html>
"""
# Any dashboard tab left open across a server restart posts a stale CSRF token,
# and the bare http.server error page said only "Invalid request token" with no
# way back and no hint about what to do.
# The same refusal covers a token that is absent and one that is stale, so the
# explanation must fit both. It used to assert the page "was rendered before the
# current process started", which is a confident and wrong diagnosis for a form
# that simply carried no token.
CSRF_RECOVERY_EXPLANATION = (
    "This request carried no valid one-time request token -- usually because "
    "the page was rendered before the current Patch Watcher process started. "
    "Reload the dashboard and repeat the action from the freshly rendered "
    "page. Nothing was changed."
)


# Reasoning-effort levels the Claude CLI accepts; kept in step with
# claude_runner.ReadOnlyRunSpec's own validation.
AGENT_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# A model name is passed straight to `claude --model`, so it is restricted to
# what a model identifier can look like rather than merely escaped.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _run_version(session):
    """An opaque token that changes whenever the run's state changes."""

    return int(session.state_changed_at.timestamp() * 1_000_000)


def _agent_choice(data):
    """Read the operator's model and effort choice from a submitted form.

    An empty value means "whatever this Patch Watcher is configured to use",
    which is what every existing form sends, so adding the control does not
    change the meaning of a form that omits it. Raises ValueError on anything
    that was not offered -- an unvalidated value here would reach a subprocess
    argument list.
    """

    model = data.get("model", [""])[0].strip()
    effort = data.get("effort", [""])[0].strip()
    if model and not MODEL_NAME_RE.match(model):
        raise ValueError(f"unsupported model: {model}")
    if effort and effort not in AGENT_EFFORTS:
        raise ValueError(f"unsupported effort: {effort}")
    return model, effort


# What the guidance composer in run_views can post. `follow_up` is absent
# deliberately: that button targets /follow-up, a different route.
# Mirrors ltvm_resources._SAFE_NAME: what an LTVM guest may be called.
LTVM_GUEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


GUIDANCE_DELIVERY_MODES = frozenset({
    "answer", "interrupt_and_send", "queue", "resume_with_message",
    "safe_boundary",
})


def _status_line_text(message):
    """Reduce a message to something an HTTP status line can carry.

    `send_error` puts its message in the status line, which http.server
    encodes latin-1/strict. Exception text is not latin-1: a single non-Latin
    character -- and these messages quote Gerrit values and operator input --
    raised UnicodeEncodeError inside send_response_only, which left the client
    with a dropped connection and no error at all.
    """

    text = " ".join(str(message).split())
    return text.encode("latin-1", "replace").decode("latin-1")[:200]


class Handler(BaseHTTPRequestHandler):
    error_message_format = ERROR_PAGE_FORMAT
    error_content_type = "text/html; charset=utf-8"

    def send_error(self, code, message=None, explain=None):
        """Answer every refusal with a readable page that offers a way back."""

        if explain is None and message and "request token" in str(message):
            explain = CSRF_RECOVERY_EXPLANATION
        if message is not None:
            message = _status_line_text(message)
        super().send_error(code, message, explain)

    def send_response_only(self, code, message=None):
        # Every response -- send_response, send_error, and our own respond() --
        # funnels through here, so this is the one place that can tell the
        # guard below whether it is still safe to write a status line.
        self.response_started = True
        super().send_response_only(code, message)

    def do_HEAD(self):
        # Without this, BaseHTTPRequestHandler's 501 declares a Content-Length
        # and then sends no body, so link checkers and monitoring see a
        # truncated response instead of a clean refusal. This app has no
        # body-free rendering path -- every route builds its HTML before it
        # knows the length -- so it refuses HEAD properly rather than
        # pretending to support it.
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._guarded(self._dispatch_get)

    def do_POST(self):
        self._guarded(self._dispatch_post)

    def _guarded(self, dispatch):
        """Turn any unhandled handler fault into a page instead of silence.

        Only `GET /` used to render inside a try. Every other route -- and
        nearly every POST, which re-renders the whole dashboard after mutating
        -- answered an unreadable store, a failing `ltvm`, or an unexpected
        exception type by unwinding into socketserver and closing the
        connection. The operator saw ERR_EMPTY_RESPONSE, with no way to tell
        "the click did nothing" from "the click did everything and then the
        render failed".
        """

        self.response_started = False
        try:
            dispatch()
        except Exception as exc:
            log_structured_error("request_failed", f"{self.command} {self.path}: {exc}", "")
            if self.response_started:
                # A status line is already on the wire; anything further would
                # corrupt the response. Drop the connection instead.
                self.close_connection = True
                return
            self.send_error(
                500,
                "The request could not be completed",
                "Patch Watcher could not complete "
                f"{escape(self.command)} {escape(self.path)}: "
                f"{escape(type(exc).__name__)}: {escape(str(exc))}",
            )

    def log_message(self, format, *args):
        """Log requests without their query strings; they carry secrets."""

        super().log_message(format, *(_redact_request_line(arg) for arg in args))

    def _reject_foreign_origin(self):
        """Refuse anything not addressed to this loopback service by name."""

        port = self.server.server_address[1]
        if not _allowed_host(self.headers.get("Host"), port):
            self.send_error(421, "This service answers only on loopback")
            return True
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if not _allowed_host(parsed.netloc, port):
                self.send_error(403, "Cross-origin requests are refused")
                return True
        return False

    def _dispatch_get(self):
        if self._reject_foreign_origin():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/favicon.ico":
            # Browsers request this unprompted; answering 404 puts a console
            # error on every page load and buries real ones.
            self.send_response(204)
            self.end_headers()
            return
        parts = [item for item in path.split("/") if item]
        if path == "/build-runs/confirm-start":
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                change = int(query.get("change_number", ["0"])[0])
                patchset = int(query.get("patchset", ["0"])[0])
                build_number = int(query.get("build_number", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid build-failure identity")
                return
            revision = query.get("revision_sha", [""])[0].lower()
            build_job = query.get("build_job", [""])[0]
            digest = query.get("snapshot_sha256", [""])[0]
            confirmation = query.get("confirmation_token", [""])[0]
            request_id = query.get("idempotency_token", [""])[0]
            expires_at = query.get("confirmation_expires_at", [""])[0]
            patch = _find_exact_patch(change, patchset, revision)
            if patch is None:
                self.send_error(409, "The patch changed; prepare build handling again")
                return
            # Verify the signed proposal BEFORE touching Gerrit or Jenkins.
            # GET carries no CSRF requirement, and every value the signature
            # covers arrives in the query string, so nothing here needs the
            # snapshot. Capturing first let any page a browser visited spend
            # the operator's Gerrit and Jenkins credentials at will.
            if (
                not _engineering_confirmation_unexpired(expires_at)
                or not _verify_confirmation(
                    confirmation, "build-start", change, patchset, revision,
                    build_job, build_number, digest, True, request_id, expires_at,
                )
            ):
                self.send_error(403, "Invalid or stale build-failure confirmation")
                return
            try:
                snapshot = _capture_build_failure_snapshot(patch)
            except (
                GerritConfigError, GerritRequestError, JenkinsSnapshotError, ValueError,
            ) as exc:
                self.respond(page("Could not capture the exact Jenkins failure: " + str(exc)))
                return
            build = snapshot["build"]
            if (
                snapshot.get("snapshot_sha256") != digest
                or build.get("job_name") != build_job
                or int(build.get("build_number") or 0) != build_number
            ):
                self.send_error(403, "Invalid or stale build-failure confirmation")
                return
            body = render_build_start_confirmation(
                patch, snapshot, confirmation_token=confirmation,
                idempotency_token=request_id,
                confirmation_expires_at=expires_at, csrf_token=CSRF_TOKEN,
            )
            self.respond(_standalone_document("Confirm build-failure handling", body))
            return
        if path == "/review-runs/confirm-start":
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                change = int(query.get("change_number", ["0"])[0])
                patchset = int(query.get("patchset", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid review revision identity")
                return
            revision = query.get("revision_sha", [""])[0].lower()
            mode = query.get("review_mode", [""])[0]
            digest = query.get("snapshot_sha256", [""])[0]
            confirmation = query.get("confirmation_token", [""])[0]
            request_id = query.get("idempotency_token", [""])[0]
            expires_at = query.get("confirmation_expires_at", [""])[0]
            patch = _find_exact_patch(change, patchset, revision)
            if patch is None or mode not in {"simple", "bots", "all"}:
                self.send_error(409, "The patch changed; prepare review handling again")
                return
            # Verify the signed proposal BEFORE fetching from Gerrit; see the
            # matching note on /build-runs/confirm-start above.
            if (
                not _engineering_confirmation_unexpired(expires_at)
                or not _verify_confirmation(
                    confirmation, "review-start", change, patchset, revision,
                    mode, digest, True, request_id, expires_at,
                )
            ):
                self.send_error(403, "Invalid or stale review confirmation")
                return
            try:
                snapshot = GerritStatusClient.configured().fetch_review_snapshot(
                    patch["url"], expected_revision=revision
                )
            except (GerritConfigError, GerritRequestError, ValueError) as exc:
                self.respond(page("Could not capture exact review comments: " + str(exc)))
                return
            if (
                not snapshot.get("complete")
                or snapshot.get("snapshot_sha256") != digest
            ):
                self.send_error(403, "Invalid or stale review confirmation")
                return
            body = render_review_start_confirmation(
                patch, snapshot, mode=mode, confirmation_token=confirmation,
                idempotency_token=request_id,
                confirmation_expires_at=expires_at, csrf_token=CSRF_TOKEN,
            )
            self.respond(_standalone_document("Confirm review handling", body))
            return
        if path == "/engineering-runs/confirm-start":
            query = parse_qs(parsed.query)
            try:
                change = int(query.get("change_number", ["0"])[0])
                patchset = int(query.get("patchset", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid engineering revision identity")
                return
            revision = query.get("revision_sha", [""])[0].lower()
            confirmation = query.get("confirmation_token", [""])[0]
            idempotency_token = query.get("idempotency_token", [""])[0]
            confirmation_expires_at = query.get(
                "confirmation_expires_at", [""]
            )[0]
            model = query.get("model", [""])[0]
            effort = query.get("effort", [""])[0]
            patch = _find_exact_patch(change, patchset, revision)
            if (
                patch is None
                or not _engineering_confirmation_unexpired(
                    confirmation_expires_at
                )
                or not _verify_confirmation(
                    confirmation, "engineering-start", change, patchset,
                    revision, idempotency_token, confirmation_expires_at,
                    model, effort,
                )
            ):
                self.send_error(403, "Invalid or stale engineering confirmation")
                return
            body = render_engineering_start_confirmation(
                patch,
                confirmation_token=confirmation,
                confirmation_expires_at=confirmation_expires_at,
                csrf_token=CSRF_TOKEN,
                idempotency_token=idempotency_token,
                model=model,
                effort=effort,
            )
            self.respond(_standalone_document("Confirm engineering run", body))
            return
        if path == "/research/policy/confirm":
            query = parse_qs(parsed.query)
            try:
                change = int(query.get("change_number", ["0"])[0])
                patchset = int(query.get("patchset", ["0"])[0])
                budget = int(query.get("per_revision_run_budget", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid research confirmation")
                return
            revision = query.get("revision_sha", [""])[0].lower()
            expected_version = query.get("expected_policy_version", ["0"])[0]
            confirmation = query.get("confirmation_token", [""])[0]
            expires_at = query.get("confirmation_expires_at", [""])[0]
            patch = _find_exact_patch(change, patchset, revision)
            if (
                patch is None
                or AUTOMATION_STORE is None
                or not _engineering_confirmation_unexpired(expires_at)
                or not _verify_confirmation(
                    confirmation,
                    "research-policy", change, patchset, revision,
                    budget, expected_version, expires_at,
                )
            ):
                self.send_error(403, "Invalid or stale research confirmation")
                return
            try:
                policy = AUTOMATION_STORE.get_research_policy(str(change))
            except AutomationNotFound:
                self.send_error(404)
                return
            if policy.version != expected_version:
                self.send_error(409, "Research policy changed; prepare it again")
                return
            body = _bind_confirmation_form(
                render_research_policy_confirmation(
                    patch,
                    {
                        "mode": "automatic",
                        "run_budget": budget,
                        "version": expected_version,
                    },
                    confirmation_token=confirmation,
                    csrf_token=CSRF_TOKEN,
                    idempotency_token=query.get(
                        "idempotency_token", [secrets.token_urlsafe(18)]
                    )[0],
                ),
                "/research/policy/confirm",
                confirmation_expires_at=expires_at,
            )
            self.respond(_standalone_document("Confirm automatic research", body))
            return
        if (
            len(parts) == 3
            and parts[0] == "approvals"
            and parts[2] == "confirm"
        ):
            if AUTOMATION_STORE is None:
                self.send_error(503, "Automation state is not initialized")
                return
            try:
                action = AUTOMATION_STORE.get_action(parts[1])
                run = AUTOMATION_STORE.get_run(action.run_id)
            except AutomationNotFound:
                self.send_error(404)
                return
            if (
                action.action_type not in {
                    FAILURE_LINK_ACTION, FAILURE_RETEST_ACTION
                }
                or action.status != "planned"
                or AUTOMATION_STORE.get_action_approval(action.action_id) is not None
            ):
                self.respond(_standalone_document(
                    "Approval unavailable",
                    "<main><h1>This exact failure action is no longer awaiting approval.</h1>"
                    "<p><a href='/'>Return to Patch Watcher</a></p></main>",
                ))
                return
            projection = _failure_action_projection(run, action)
            expires_at = str(int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS)
            token = _signed_confirmation(
                "failure-action", action.action_id, run.revision, action.status,
                expires_at,
            )
            body = render_failure_action_confirmation(
                projection,
                confirmation_token=token,
                csrf_token=CSRF_TOKEN,
                idempotency_token=action.idempotency_key,
            )
            if "</form>" in body:
                # The renderer omits the form when the action is not safe to
                # approve; there is then nothing to bind.
                body = _bind_confirmation_form(
                    body,
                    f"/approvals/{action.action_id}/approve",
                    confirmation_expires_at=expires_at,
                )
            self.respond(_standalone_document("Confirm failure action", body))
            return
        if len(parts) == 2 and parts[0] == "approvals":
            if AUTOMATION_STORE is None:
                self.send_error(503, "Automation state is not initialized")
                return
            try:
                action = AUTOMATION_STORE.get_action(parts[1])
                run = AUTOMATION_STORE.get_run(action.run_id)
            except AutomationNotFound:
                self.send_error(404)
                return
            if action.action_type not in {
                FAILURE_LINK_ACTION, FAILURE_RETEST_ACTION
            }:
                self.send_error(404)
                return
            projection = _failure_action_projection(run, action)
            renderer = (
                render_failure_approval_card
                if projection["state"] == "planned"
                and projection["approval_state"] == "pending"
                else render_failure_action_status
            )
            body = (
                "<main><p><a href='/'>← Patch Watcher</a></p>"
                + renderer(projection)
                + "</main>"
            )
            self.respond(_standalone_document("Failure action", body))
            return
        if (
            len(parts) == 4
            and parts[:2] == ["automation", "actions"]
            and parts[3] == "confirm"
        ):
            if AUTOMATION_STORE is None:
                self.send_error(503, "Deterministic retest state is not initialized")
                return
            try:
                action = AUTOMATION_STORE.get_action(parts[2])
                run = AUTOMATION_STORE.get_run(action.run_id)
            except AutomationNotFound:
                self.send_error(404)
                return
            if (
                action.status != "planned"
                or run.policy_snapshot.get("mode") != "approval"
                or AUTOMATION_STORE.get_action_approval(action.action_id) is not None
            ):
                self.respond(_standalone_document(
                    "Retest approval unavailable",
                    "<main><h1>This action is no longer awaiting approval.</h1>"
                    "<p><a href='/'>Return to Patch Watcher</a></p></main>",
                ))
                return
            expires_at = str(int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS)
            confirmation = _signed_confirmation(
                "retest-action", action.action_id, run.revision, action.status,
                expires_at,
            )
            body = _bind_confirmation_form(
                render_action_confirmation(
                    action_id=action.action_id,
                    change_number=run.patch_id,
                    revision_sha=run.revision,
                    session_id=str(action.request.get("session_id") or ""),
                    jira_ticket=str(action.request.get("jira_ticket") or ""),
                    csrf_token=CSRF_TOKEN,
                ),
                f"/automation/actions/{action.action_id}/approve",
                confirmation_expires_at=expires_at,
                confirmation_token=confirmation,
            )
            self.respond(_standalone_document("Approve Maloo retest", body))
            return
        if path == "/automation/global/confirm-enable":
            if AUTOMATION_STORE is None:
                self.send_error(503, "Deterministic retest state is not initialized")
                return
            setting = AUTOMATION_STORE.get_global_automation()
            gate_state = _global_gate_proposal(setting)
            expires_at = str(int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS)
            request_id = secrets.token_urlsafe(18)
            confirmation = _signed_confirmation(
                "automation-global-enable", gate_state, request_id, expires_at,
            )
            self.respond(_standalone_document(
                "Enable automatic retests",
                _bind_confirmation_form(
                    render_enable_confirmation(csrf_token=CSRF_TOKEN),
                    "/automation/global/enable",
                    expected_gate_state=gate_state,
                    idempotency_token=request_id,
                    confirmation_expires_at=expires_at,
                    confirmation_token=confirmation,
                ),
            ))
            return
        if path == "/auto-refresh":
            # GET is display-only. Refreshing is an explicit POST below.
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if len(parts) >= 2 and parts[0] == "runs":
            try:
                session = _find_session_by_run_id(parts[1])
            except SessionNotFound:
                self.send_error(404)
                return
            if len(parts) == 4 and parts[2] == "artifacts":
                if RUN_CONTROLLER is None or not session.run_id.startswith(
                    ("pw-engineer-", "pw-review-", "pw-build-")
                ):
                    self.send_error(404)
                    return
                artifact = next((
                    item for item in RUN_CONTROLLER.engineering_store.list_artifacts(
                        session.run_id
                    )
                    if item.artifact_id == parts[3]
                ), None)
                if artifact is None:
                    self.send_error(404)
                    return
                artifact_root = (
                    RUN_CONTROLLER.runs_directory / "engineering-artifacts" / session.run_id
                ).resolve()
                target = (artifact_root / artifact.relative_path).resolve()
                if target.parent != artifact_root or not target.is_file():
                    self.send_error(404)
                    return
                # Check the size the cheap way before reading. An artifact may
                # be up to MAX_ARTIFACT_BYTES (2 GiB), and reading it whole to
                # then discover it is the wrong size meant K concurrent
                # fetches could hold K x 2 GiB resident -- on a server with no
                # concurrency limit.
                if target.stat().st_size != artifact.size_bytes:
                    self.send_error(409, "Captured artifact failed integrity verification")
                    return
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    while True:
                        chunk = stream.read(1 << 20)
                        if not chunk:
                            break
                        digest.update(chunk)
                if digest.hexdigest() != artifact.sha256:
                    self.send_error(409, "Captured artifact failed integrity verification")
                    return
                self.send_response(200)
                self.send_header("Content-Type", artifact.media_type)
                self.send_header("Content-Length", str(artifact.size_bytes))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                # Verified above, then streamed: the file is re-read rather
                # than held, so peak memory is one chunk rather than the whole
                # artifact.
                with target.open("rb") as stream:
                    sent = 0
                    while sent < artifact.size_bytes:
                        chunk = stream.read(min(1 << 20, artifact.size_bytes - sent))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        sent += len(chunk)
                return
            if len(parts) == 2:
                self.respond(_standalone_document("Patch Watcher run", run_detail_html(session)))
                return
            if len(parts) == 3 and parts[2] == "confirm":
                query = parse_qs(parsed.query)
                intent = query.get("intent", [""])[0]
                if intent not in {"cancel", "kill", "retry"}:
                    self.send_error(400, "Unknown destructive intent")
                    return
                if intent == "retry" and _engineering_retry_patch(session) is None:
                    self.send_error(
                        409,
                        "Engineering retry requires a terminal run at the exact current revision",
                    )
                    return
                # GET is deliberately display-only. A first POST creates the
                # short-lived one-use token, followed by the explicit final POST.
                # The description of what the intent DOES belongs on this page:
                # it used to appear only on the second one, so step 1 asked the
                # operator to continue towards an unnamed consequence.
                title, description, back_label = RUN_INTENT_REVIEW[intent]
                body = (
                    "<main><p><a href='/runs/" + escape(session.run_id, quote=True)
                    + f"'>{escape(back_label)}</a></p>"
                    f"<h2>{escape(title)}</h2>"
                    f"<p role='alert'>{escape(description)}</p>"
                    f"<p>Run <code>{escape(session.run_id)}</code> · state "
                    f"<strong>{escape(session.state.replace('_', ' '))}</strong> · "
                    f"exact pinned revision <code>{escape(str(session.revision or 'unknown'))}</code></p>"
                    "<p>No action has been taken. Continue only to open the final confirmation.</p>"
                    f"<form method='post' action='/runs/{escape(session.run_id, quote=True)}/confirm'>"
                    f"<input type='hidden' name='intent' value='{escape(intent, quote=True)}'>"
                    f"<input type='hidden' name='csrf_token' value='{escape(CSRF_TOKEN, quote=True)}'>"
                    "<button type='submit'>Continue to confirmation</button></form></main>"
                )
                self.respond(_standalone_document("Confirm session control", body))
                return
            self.send_error(404)
            return
        if path != "/":
            self.send_error(404)
            return
        # Render the whole body BEFORE committing to a status line. Sending
        # "200 OK" first turned any rendering failure into a blank successful
        # page: no error, and no CSRF token left on screen to recover with.
        try:
            body = page().encode("utf-8", "backslashreplace")
        except Exception as exc:
            # Deliberately broad: a visible 500 beats a blank 200.
            log_structured_error("dashboard_render_failed", str(exc), "")
            self.send_error(
                500,
                "The dashboard could not be rendered",
                "Patch Watcher could not render the dashboard: "
                f"{type(exc).__name__}: {exc}",
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _dispatch_post(self):
        if self._reject_foreign_origin():
            return
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self.send_error(400, "Invalid Content-Length")
            return
        if length < 0:
            self.send_error(400, "Invalid Content-Length")
            return
        if length > MAX_FORM_BODY_BYTES:
            self.send_error(413, "Form body is too large")
            return
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.send_error(415, "Expected a URL-encoded form body")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400, "Incomplete form body")
            return
        try:
            data = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                max_num_fields=MAX_FORM_FIELDS,
            )
        except (UnicodeDecodeError, ValueError):
            self.send_error(400, "Malformed form body")
            return
        path = urlparse(self.path).path
        parts = [item for item in path.split("/") if item]
        if parts and parts[0] == "autonomous-lanes":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if AUTONOMOUS_LANE_STORE is None:
                self.send_error(503, "Unattended-action controls are unavailable")
                return
            if path == "/autonomous-lanes/replay":
                # The per-patch control is labelled "Replay this exact
                # revision" and posts one; the global control posts none and
                # means the whole history. Honour whichever arrived, rather
                # than replaying everything and reporting a count about a
                # different question.
                revision = data.get("revision_sha", [""])[0].strip().lower()
                try:
                    results = AUTONOMOUS_LANE_HISTORY.replay(
                        revision=revision or None
                    )
                except AutonomousLaneError as exc:
                    self.send_error(409, str(exc))
                    return
                matched = sum(item.matched for item in results)
                scope_text = (
                    f"revision {revision}" if revision
                    else "the whole recorded decision history"
                )
                if not results:
                    self.respond(page(
                        f"No lane decisions are recorded for {scope_text}."
                    ))
                    return
                self.respond(page(
                    f"Replay complete for {scope_text}: {matched}/{len(results)} "
                    "recorded lane decisions verify against their original "
                    "observation and control snapshots."
                ))
                return
            # The lane used to have its own global, project, and patch switches
            # here, each behind a signed confirmation.  A patch's level now
            # enrols or withdraws it -- and re-applies that on every poll --
            # so a switch set here was silently undone within seconds.  The
            # two things an operator sets are the level on each patch and the
            # global policy gate; nothing else is accepted.
            self.send_error(404)
            return
        if path == "/standing-policy":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if STANDING_POLICY_STORE is None or AUTOMATION_STORE is None:
                self.send_error(503, "Standing policy state is not initialized")
                return
            try:
                change = int(data.get("change_number", ["0"])[0])
                patchset = int(data.get("patchset", ["0"])[0])
                expected_version = int(data.get("expected_version", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid standing-policy identity")
                return
            revision = data.get("revision_sha", [""])[0].lower()
            patch = _find_exact_patch(change, patchset, revision)
            if patch is None:
                self.send_error(409, "The patch changed; refresh before saving its policy")
                return
            try:
                preset = data.get("preset", [""])[0].strip()
                if preset:
                    proposed = PatchAutomationPolicy.for_preset(
                        str(change), preset, version=expected_version,
                    )
                else:
                    # The old four-field form, or a caller that still speaks it.
                    proposed = PatchAutomationPolicy(
                        patch_id=str(change),
                        test_failures=data.get("test_failures", ["off"])[0],
                        build_failures=data.get("build_failures", ["off"])[0],
                        review_comments=data.get("review_comments", ["off"])[0],
                        trigger_mode=data.get("trigger_mode", ["manual"])[0],
                        version=expected_version,
                    )
                # No interstitial: the level list on the panel says what each
                # rung does, and the global kill switch is the gate.  Saving
                # a level is saving a level.
                saved = STANDING_POLICY_STORE.save(
                    proposed, expected_version=expected_version,
                )
                sync_automation_patch(patch)
                _sync_standing_test_policy(patch, saved)
                if saved.trigger_mode == "automatic":
                    _apply_standing_policy(patch)
            except (
                AutomationConflict, StandingPolicyConflict,
                StandingPolicyError, ValueError,
            ) as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path == "/standing-policy/run-now":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if STANDING_POLICY_STORE is None or AUTOMATION_STORE is None or RUN_CONTROLLER is None:
                self.send_error(503, "Standing policy state is not initialized")
                return
            try:
                change = int(data.get("change_number", ["0"])[0])
                patchset = int(data.get("patchset", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid standing-policy identity")
                return
            revision = data.get("revision_sha", [""])[0].lower()
            patch = _find_exact_patch(change, patchset, revision)
            if patch is None:
                self.send_error(409, "The patch changed; refresh before running its level")
                return
            try:
                policy = _standing_policy(patch)
            except (StandingPolicyError, ValueError) as exc:
                self.send_error(409, str(exc))
                return
            if policy.rank == 0:
                self.send_error(409, "Watch only has nothing to run; choose a level first")
                return
            session = _apply_standing_policy(patch, policy=policy, attended=True)
            if session is not None:
                self.send_response(303)
                self.send_header("Location", "/runs/" + quote(session.run_id, safe=""))
                self.end_headers()
                return
            self.respond(page(
                f"Run now: nothing to do for {policy.label} on patchset {patchset} -- no "
                "failed build, no cherry-pick veto, and no unresolved comment on this "
                "revision, or a run already owns it."
            ))
            return
        if parts and parts[0] == "build-runs":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if path not in {"/build-runs/prepare", "/build-runs/start"}:
                self.send_error(404)
                return
            if RUN_CONTROLLER is None:
                self.send_error(503, "Build-failure handling is disabled")
                return
            try:
                change = int(data.get("change_number", ["0"])[0])
                patchset = int(data.get("patchset", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid build-failure revision identity")
                return
            revision = data.get("revision_sha", [""])[0].lower()
            patch = _find_exact_patch(change, patchset, revision)
            if patch is None or _revision_owner_session(patch) is not None:
                self.send_error(409, "The patch changed or already has an active run")
                return
            try:
                snapshot = _capture_build_failure_snapshot(patch)
            except (
                GerritConfigError, GerritRequestError, JenkinsSnapshotError, ValueError,
            ) as exc:
                self.respond(page("Could not capture the exact Jenkins failure: " + str(exc)))
                return
            build = snapshot["build"]
            digest = str(snapshot["snapshot_sha256"])
            build_job = str(build["job_name"])
            build_number = int(build["build_number"])
            request_id = data.get("idempotency_token", [""])[0].strip()
            if not request_id:
                request_id = secrets.token_urlsafe(18)
            if path == "/build-runs/prepare":
                expires_at = str(int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS)
                confirmation = _signed_confirmation(
                    "build-start", change, patchset, revision, build_job,
                    build_number, digest, True, request_id, expires_at,
                )
                query = urlencode({
                    "change_number": change, "patchset": patchset,
                    "revision_sha": revision, "build_job": build_job,
                    "build_number": build_number, "snapshot_sha256": digest,
                    "confirmation_token": confirmation,
                    "idempotency_token": request_id,
                    "confirmation_expires_at": expires_at,
                })
                self.send_response(303)
                self.send_header("Location", "/build-runs/confirm-start?" + query)
                self.end_headers()
                return
            confirmation = data.get("confirmation_token", [""])[0]
            expires_at = data.get("confirmation_expires_at", [""])[0]
            submitted_digest = data.get("build_snapshot_sha256", [""])[0]
            submitted_job = data.get("build_job", [""])[0]
            try:
                submitted_number = int(data.get("build_number", ["0"])[0])
            except ValueError:
                submitted_number = 0
            if (
                submitted_digest != digest or submitted_job != build_job
                or submitted_number != build_number
                or not _engineering_confirmation_unexpired(expires_at)
                or not _verify_confirmation(
                    confirmation, "build-start", change, patchset, revision,
                    build_job, build_number, digest, True, request_id, expires_at,
                )
            ):
                self.send_error(403, "Invalid or stale build-failure confirmation")
                return
            if not _claim_engineering_confirmation(confirmation, request_id):
                self.send_error(409, "Build-failure confirmation was already used")
                return
            try:
                session = RUN_CONTROLLER.request_build_failure(
                    patch, snapshot, request_id=request_id
                )
            except (
                RunControllerError, InvalidSessionOperation,
                SessionAlreadyExists, ValueError,
            ) as exc:
                self.respond(page(str(exc)))
                return
            self.send_response(303)
            self.send_header("Location", f"/runs/{session.run_id}")
            self.end_headers()
            return
        if parts and parts[0] == "review-runs":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if path not in {"/review-runs/prepare", "/review-runs/start"}:
                self.send_error(404)
                return
            if RUN_CONTROLLER is None:
                self.send_error(503, "Review-comment handling is disabled")
                return
            try:
                change = int(data.get("change_number", ["0"])[0])
                patchset = int(data.get("patchset", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid review revision identity")
                return
            revision = data.get("revision_sha", [""])[0].lower()
            mode = data.get("review_mode", [""])[0]
            if mode not in {"simple", "bots", "all"}:
                self.send_error(400, "Invalid review mode")
                return
            patch = _find_exact_patch(change, patchset, revision)
            if patch is None or _revision_owner_session(patch) is not None:
                self.send_error(409, "The patch changed or already has an active run")
                return
            try:
                snapshot = GerritStatusClient.configured().fetch_review_snapshot(
                    patch["url"], expected_revision=revision
                )
            except (GerritConfigError, GerritRequestError, ValueError) as exc:
                self.respond(page("Could not capture exact review comments: " + str(exc)))
                return
            if not snapshot.get("complete") or not snapshot.get("threads"):
                self.respond(page(
                    "Review comments are incomplete or no unresolved comments remain."
                ))
                return
            digest = str(snapshot["snapshot_sha256"])
            request_id = data.get("idempotency_token", [""])[0].strip()
            if not request_id:
                request_id = secrets.token_urlsafe(18)
            if path == "/review-runs/prepare":
                expires_at = str(int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS)
                confirmation = _signed_confirmation(
                    "review-start", change, patchset, revision, mode, digest,
                    True, request_id, expires_at,
                )
                query = urlencode({
                    "change_number": change, "patchset": patchset,
                    "revision_sha": revision, "review_mode": mode,
                    "snapshot_sha256": digest,
                    "confirmation_token": confirmation,
                    "idempotency_token": request_id,
                    "confirmation_expires_at": expires_at,
                })
                self.send_response(303)
                self.send_header("Location", "/review-runs/confirm-start?" + query)
                self.end_headers()
                return
            confirmation = data.get("confirmation_token", [""])[0]
            expires_at = data.get("confirmation_expires_at", [""])[0]
            submitted_digest = data.get("snapshot_sha256", [""])[0]
            if (
                submitted_digest != digest
                or not _engineering_confirmation_unexpired(expires_at)
                or not _verify_confirmation(
                    confirmation, "review-start", change, patchset, revision,
                    mode, digest, True, request_id, expires_at,
                )
            ):
                self.send_error(403, "Invalid or stale review confirmation")
                return
            if not _claim_engineering_confirmation(confirmation, request_id):
                self.send_error(409, "Review confirmation was already used")
                return
            try:
                session = RUN_CONTROLLER.request_review_comments(
                    patch, snapshot, mode=mode, request_id=request_id
                )
            except (
                RunControllerError, InvalidSessionOperation,
                SessionAlreadyExists, ValueError,
            ) as exc:
                self.respond(page(str(exc)))
                return
            self.send_response(303)
            self.send_header("Location", f"/runs/{session.run_id}")
            self.end_headers()
            return
        if parts and parts[0] == "engineering-runs":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if path not in {"/engineering-runs/prepare", "/engineering-runs/start"}:
                self.send_error(404)
                return
            try:
                change = int(data.get("change_number", ["0"])[0])
                patchset = int(data.get("patchset", ["0"])[0])
            except ValueError:
                self.send_error(400, "Invalid engineering revision identity")
                return
            revision = data.get("revision_sha", [""])[0].lower()
            patch = _find_exact_patch(change, patchset, revision)
            if patch is None:
                self.send_error(
                    409, "The patch changed; refresh before starting engineering"
                )
                return
            if _revision_owner_session(patch) is not None:
                self.send_error(
                    409, "Another run already owns this patch revision"
                )
                return
            if path == "/engineering-runs/prepare":
                idempotency_token = data.get("idempotency_token", [""])[0].strip()
                if not idempotency_token:
                    idempotency_token = secrets.token_urlsafe(18)
                confirmation_expires_at = str(
                    int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS
                )
                try:
                    model, effort = _agent_choice(data)
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                # The choice is part of what is signed. Without that, the
                # confirmation page could display one model and the POST that
                # follows could start another.
                confirmation = _signed_confirmation(
                    "engineering-start", change, patchset, revision,
                    idempotency_token, confirmation_expires_at, model, effort,
                )
                query = urlencode({
                    "change_number": change,
                    "patchset": patchset,
                    "revision_sha": revision,
                    "confirmation_token": confirmation,
                    "idempotency_token": idempotency_token,
                    "confirmation_expires_at": confirmation_expires_at,
                    "model": model,
                    "effort": effort,
                })
                self.send_response(303)
                self.send_header("Location", "/engineering-runs/confirm-start?" + query)
                self.end_headers()
                return
            confirmation = data.get("confirmation_token", [""])[0]
            idempotency_token = data.get("idempotency_token", [""])[0]
            confirmation_expires_at = data.get(
                "confirmation_expires_at", [""]
            )[0]
            try:
                model, effort = _agent_choice(data)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            if (
                not _engineering_confirmation_unexpired(
                    confirmation_expires_at
                )
                or not _verify_confirmation(
                    confirmation, "engineering-start", change, patchset,
                    revision, idempotency_token, confirmation_expires_at,
                    model, effort,
                )
            ):
                self.send_error(403, "Invalid or stale engineering confirmation")
                return
            if RUN_CONTROLLER is None:
                self.respond(page("The run controller is not initialized."))
                return
            if not _claim_engineering_confirmation(
                confirmation, idempotency_token
            ):
                self.send_error(409, "Engineering confirmation was already used")
                return
            try:
                session = RUN_CONTROLLER.request_engineering(
                    patch, request_id=idempotency_token,
                    model=model, effort=effort,
                )
            except (
                RunControllerError, InvalidSessionOperation,
                SessionAlreadyExists, ValueError,
            ) as exc:
                self.respond(page(str(exc)))
                return
            self.send_response(303)
            self.send_header("Location", f"/runs/{session.run_id}")
            self.end_headers()
            return
        if parts and parts[0] in {"research", "failure-actions", "approvals"}:
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if AUTOMATION_STORE is None:
                self.respond(page("Automation state is not initialized."))
                return
            if path in {"/research/policy/prepare", "/research/policy/confirm"}:
                try:
                    change = int(data.get("change_number", ["0"])[0])
                    patchset = int(data.get("patchset", ["0"])[0])
                    budget = int(data.get("per_revision_run_budget", ["0"])[0])
                except ValueError:
                    self.send_error(400, "Invalid research policy values")
                    return
                revision = data.get("revision_sha", [""])[0].lower()
                mode = data.get("research_mode", ["disabled"])[0]
                expected_version = data.get("expected_policy_version", ["0"])[0]
                patch = _find_exact_patch(change, patchset, revision)
                if patch is None:
                    self.respond(page(
                        "The patch changed; refresh before changing research policy."
                    ))
                    return
                sync_automation_patch(patch)
                current = AUTOMATION_STORE.get_research_policy(str(change))
                if str(current.version) != str(expected_version):
                    self.respond(page(
                        "Research policy changed; refresh before replacing it."
                    ))
                    return
                if (
                    mode not in {"disabled", "manual", "automatic"}
                    or not 0 <= budget <= 20
                    or (mode != "disabled" and budget < 1)
                ):
                    self.send_error(
                        400,
                        "Research mode is invalid or its run budget is outside 1-20",
                    )
                    return
                if mode == "automatic" and path == "/research/policy/prepare":
                    expires_at = str(
                        int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS
                    )
                    confirmation = _signed_confirmation(
                        "research-policy", change, patchset, revision,
                        budget, expected_version, expires_at,
                    )
                    query = urlencode({
                        "change_number": change,
                        "patchset": patchset,
                        "revision_sha": revision,
                        "per_revision_run_budget": budget,
                        "expected_policy_version": expected_version,
                        "confirmation_token": confirmation,
                        "confirmation_expires_at": expires_at,
                        "idempotency_token": data.get(
                            "idempotency_token", [secrets.token_urlsafe(18)]
                        )[0],
                    })
                    self.send_response(303)
                    self.send_header(
                        "Location", "/research/policy/confirm?" + query
                    )
                    self.end_headers()
                    return
                if mode == "automatic":
                    confirmation = data.get("confirmation_token", [""])[0]
                    expires_at = data.get("confirmation_expires_at", [""])[0]
                    if not _engineering_confirmation_unexpired(
                        expires_at
                    ) or not _verify_confirmation(
                        confirmation,
                        "research-policy", change, patchset, revision,
                        budget, expected_version, expires_at,
                    ):
                        self.send_error(403, "Invalid or stale confirmation")
                        return
                try:
                    AUTOMATION_STORE.set_research_policy(
                        str(change),
                        mode=mode,
                        run_budget=budget,
                        updated_by="operator",
                        expected_version=expected_version,
                    )
                except AutomationConflict as exc:
                    self.respond(page(str(exc)))
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if path == "/research/investigate":
                if RUN_CONTROLLER is None:
                    self.respond(page("The research controller is not initialized."))
                    return
                try:
                    change = int(data.get("change_number", ["0"])[0])
                    patchset = int(data.get("patchset", ["0"])[0])
                except ValueError:
                    self.send_error(400, "Invalid research revision identity")
                    return
                revision = data.get("revision_sha", [""])[0].lower()
                patch = _find_exact_patch(change, patchset, revision)
                if patch is None:
                    self.respond(page("The patch changed; refresh before research."))
                    return
                try:
                    request = _start_unknown_failure_research(
                        patch,
                        automatic=False,
                        attempt_id=data.get("attempt_id", [None])[0],
                    )
                except (
                    AutomationConflict, RunControllerError, InvalidSessionOperation,
                    SessionAlreadyExists, ValueError,
                ) as exc:
                    self.respond(page(str(exc)))
                    return
                self.send_response(303)
                self.send_header("Location", f"/runs/{request.run_id}")
                self.end_headers()
                return
            if path == "/failure-actions/plan":
                if FAILURE_ACTION_CONTROLLER is None:
                    self.respond(page("Failure action controller is not initialized."))
                    return
                try:
                    change = int(data.get("change_number", ["0"])[0])
                    patchset = int(data.get("patchset", ["0"])[0])
                except ValueError:
                    self.send_error(400, "Invalid failure action identity")
                    return
                revision = data.get("revision_sha", [""])[0].lower()
                patch = _find_exact_patch(change, patchset, revision)
                if patch is None:
                    self.respond(page(
                        "The patch changed; refresh before planning failure actions."
                    ))
                    return
                submitted_session = data.get("session_id", [""])[0]
                submitted_group = data.get("test_group", [""])[0]
                submitted_suite = data.get("suite_id", [""])[0]
                failure = _match_unknown_failure(
                    patch, submitted_session, submitted_group, submitted_suite
                )
                if failure is None:
                    self.respond(page(
                        "That failure is not present in the latest complete, exact-revision "
                        "Maloo observation. Refresh before planning an association."
                    ))
                    return
                failure_session = str(failure.get("session_id") or "")
                failure_group = str(failure.get("test_group") or "")
                failure_suite_name = str(failure.get("suite") or "")
                failure_suite = str(failure.get("remote_failure_id") or "")
                if not all(MALOO_ID_RE.fullmatch(value) for value in (
                    failure_session, failure_group, failure_suite_name,
                    failure_suite,
                )):
                    self.send_error(409, "Observed Maloo failure identity is incomplete")
                    return
                try:
                    plan = FAILURE_ACTION_CONTROLLER.plan_link_existing_bug(
                        str(change),
                        expected_revision=revision,
                        expected_patchset=patchset,
                        session_id=failure_session,
                        test_group=failure_group,
                        suite_name=failure_suite_name,
                        suite_id=failure_suite,
                        jira_ticket=data.get("jira_ticket", [""])[0],
                    )
                except (
                    FailureActionError, AutomationConflict,
                    AutomationNotFound, ValueError,
                ) as exc:
                    self.respond(page(f"Failure action was not planned: {exc}"))
                    return
                self.send_response(303)
                self.send_header(
                    "Location", f"/approvals/{plan.link_action.action_id}/confirm"
                )
                self.end_headers()
                return
            if (
                len(parts) == 3
                and parts[0] == "approvals"
                and parts[2] == "approve"
            ):
                if FAILURE_ACTION_CONTROLLER is None:
                    self.respond(page("Failure action controller is not initialized."))
                    return
                try:
                    action = AUTOMATION_STORE.get_action(parts[1])
                    run = AUTOMATION_STORE.get_run(action.run_id)
                    if action.action_type not in {
                        FAILURE_LINK_ACTION, FAILURE_RETEST_ACTION
                    } or action.status != "planned":
                        raise AutomationConflict(
                            "this exact action is no longer awaiting approval"
                        )
                    confirmation = data.get("confirmation_token", [""])[0]
                    expires_at = data.get("confirmation_expires_at", [""])[0]
                    if not _engineering_confirmation_unexpired(
                        expires_at
                    ) or not _verify_confirmation(
                        confirmation,
                        "failure-action", action.action_id,
                        run.revision, action.status, expires_at,
                    ):
                        raise AutomationConflict("confirmation is invalid or stale")
                    FAILURE_ACTION_CONTROLLER.approve_action(
                        action.action_id,
                        approved_by="operator",
                        expected_revision=data.get("revision_sha", [""])[0],
                    )
                except (
                    FailureActionError, AutomationConflict,
                    AutomationNotFound, ValueError,
                ) as exc:
                    self.respond(page(f"Failure action was not approved: {exc}"))
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self.send_error(404)
            return
        if parts and parts[0] == "automation":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if AUTOMATION_STORE is None:
                self.respond(page("Deterministic retest state is not initialized."))
                return
            if path == "/automation/global/disable":
                AUTOMATION_STORE.set_global_automation(
                    False,
                    changed_by="operator",
                    reason="Disabled from the dashboard",
                )
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if path == "/automation/global/enable":
                # The CSRF token alone proves only that the POST came from this
                # origin -- not that the operator was ever shown, and agreed
                # to, this exact escalation. Without the signed proposal below
                # the durable audit row claimed a confirmation that the code
                # had never verified.
                gate_state = _global_gate_proposal(
                    AUTOMATION_STORE.get_global_automation()
                )
                submitted_state = data.get("expected_gate_state", [""])[0]
                confirmation = data.get("confirmation_token", [""])[0]
                request_id = data.get("idempotency_token", [""])[0]
                expires_at = data.get("confirmation_expires_at", [""])[0]
                if (
                    submitted_state != gate_state
                    or not _engineering_confirmation_unexpired(expires_at)
                    or not _verify_confirmation(
                        confirmation, "automation-global-enable",
                        gate_state, request_id, expires_at,
                    )
                    or not _claim_engineering_confirmation(confirmation, request_id)
                ):
                    self.send_error(
                        403,
                        "Invalid, expired, or used global automation confirmation",
                    )
                    return
                AUTOMATION_STORE.set_global_automation(
                    True,
                    changed_by="operator",
                    reason="Explicitly confirmed from the dashboard",
                )
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if (
                len(parts) == 4
                and parts[:2] == ["automation", "actions"]
                and parts[3] == "approve"
            ):
                try:
                    action = AUTOMATION_STORE.get_action(parts[2])
                    run = AUTOMATION_STORE.get_run(action.run_id)
                    expected_revision = data.get("revision_sha", [""])[0]
                    # Bind the approval to the exact planned action the
                    # confirmation page displayed, expire it, and spend it once.
                    confirmation = data.get("confirmation_token", [""])[0]
                    expires_at = data.get("confirmation_expires_at", [""])[0]
                    if (
                        not _engineering_confirmation_unexpired(expires_at)
                        or not _verify_confirmation(
                            confirmation, "retest-action", action.action_id,
                            run.revision, action.status, expires_at,
                        )
                        or not _claim_engineering_confirmation(
                            confirmation, action.idempotency_key,
                        )
                    ):
                        raise AutomationConflict(
                            "confirmation is invalid, expired, or already used"
                        )
                    AUTOMATION_STORE.approve_action(
                        action.action_id,
                        approved_by="operator",
                        expected_revision=expected_revision,
                        expected_policy_mode="approval",
                    )
                except (AutomationConflict, AutomationNotFound, ValueError) as exc:
                    self.respond(page(f"Retest approval was not recorded: {exc}"))
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if path in {"/automation/policy", "/automation/policy/confirm"}:
                try:
                    change = int(data.get("change_number", ["0"])[0])
                    max_actions = int(data.get("max_actions", ["1"])[0])
                except ValueError:
                    self.send_error(400, "Invalid policy values")
                    return
                revision = data.get("revision_sha", [""])[0]
                with PATCHES_LOCK:
                    patch = next((
                        item for item in PATCHES
                        if int(item.get("change_number", 0) or 0) == change
                        and item.get("revision_sha") == revision
                    ), None)
                if patch is None:
                    self.respond(page("The patch changed; refresh before changing its policy."))
                    return
                if not 1 <= max_actions <= 20:
                    self.send_error(400, "Action budget must be between 1 and 20")
                    return
                mode = (
                    "automatic"
                    if path == "/automation/policy/confirm"
                    else data.get("mode", ["disabled"])[0]
                )
                if mode not in {"disabled", "advise", "approval", "automatic"}:
                    self.send_error(400, "Unknown policy mode")
                    return
                if mode == "automatic" and path != "/automation/policy/confirm":
                    expires_at = str(
                        int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS
                    )
                    request_id = secrets.token_urlsafe(18)
                    confirmation = _signed_confirmation(
                        "automation-policy", change, revision, max_actions,
                        request_id, expires_at,
                    )
                    body = _bind_confirmation_form(
                        render_policy_confirmation(
                            change_number=str(change),
                            revision_sha=revision,
                            max_actions=max_actions,
                            csrf_token=CSRF_TOKEN,
                        ),
                        "/automation/policy/confirm",
                        idempotency_token=request_id,
                        confirmation_expires_at=expires_at,
                        confirmation_token=confirmation,
                    )
                    self.respond(_standalone_document("Confirm automatic policy", body))
                    return
                if path == "/automation/policy/confirm":
                    # Same reasoning as /automation/global/enable: bind the
                    # escalation to the exact displayed proposal, expire it,
                    # and let it be spent only once.
                    confirmation = data.get("confirmation_token", [""])[0]
                    request_id = data.get("idempotency_token", [""])[0]
                    expires_at = data.get("confirmation_expires_at", [""])[0]
                    if (
                        not _engineering_confirmation_unexpired(expires_at)
                        or not _verify_confirmation(
                            confirmation, "automation-policy", change, revision,
                            max_actions, request_id, expires_at,
                        )
                        or not _claim_engineering_confirmation(
                            confirmation, request_id
                        )
                    ):
                        self.send_error(
                            403, "Invalid, expired, or used policy confirmation"
                        )
                        return
                sync_automation_patch(patch)
                AUTOMATION_STORE.set_policy(
                    str(change),
                    mode=mode,
                    action_budget=max_actions,
                    delivery_budget=4,
                    updated_by="operator",
                )
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if path == "/automation/dry-run":
                try:
                    change = int(data.get("change_number", ["0"])[0])
                except ValueError:
                    self.send_error(400, "Invalid change number")
                    return
                revision = data.get("revision_sha", [""])[0]
                with PATCHES_LOCK:
                    patch = next((
                        item for item in PATCHES
                        if int(item.get("change_number", 0) or 0) == change
                        and item.get("revision_sha") == revision
                    ), None)
                if patch is None:
                    self.respond(page("The patch changed; refresh before evaluating it."))
                    return
                if RETEST_CONTROLLER is None:
                    self.respond(page("The deterministic retest controller is not initialized."))
                    return
                result = RETEST_CONTROLLER.tick_patch(patch, dry_run=True)
                self.respond(page(
                    "Dry run: " + result.evaluation.status.replace("_", " ")
                    + " — " + result.evaluation.reason
                ))
                return
            self.send_error(404)
            return
        if parts and parts[0] == "runs":
            token = data.get("csrf_token", [""])[0]
            if not hmac.compare_digest(token, CSRF_TOKEN):
                self.send_error(403, "Invalid request token")
                return
            if path == "/runs/investigate":
                if RUN_CONTROLLER is None:
                    self.respond(page("The run controller is not initialized."))
                    return
                try:
                    change = int(data.get("change_number", ["0"])[0])
                    patchset = int(data.get("patchset", ["0"])[0])
                    revision = data.get("revision_sha", [""])[0]
                except ValueError:
                    self.send_error(400, "Invalid revision identity")
                    return
                patch = next((
                    item for item in PATCHES
                    if int(item.get("change_number", 0) or 0) == change
                    and int(item.get("patchset", 0) or 0) == patchset
                    and item.get("revision_sha") == revision
                ), None)
                if patch is None:
                    self.respond(page("The patch changed; refresh before investigating."))
                    return
                try:
                    model, effort = _agent_choice(data)
                    session = RUN_CONTROLLER.request_investigation(
                        patch, model=model, effort=effort
                    )
                except (
                    RunControllerError, InvalidSessionOperation,
                    SessionAlreadyExists, ValueError,
                ) as exc:
                    # SessionAlreadyExists is a sibling of
                    # InvalidSessionOperation, not a subclass, so it used to
                    # escape into socketserver and drop the connection -- which
                    # a double-click, or the observer claiming the same patch
                    # between render and POST, reaches routinely.
                    self.respond(page(str(exc)))
                    return
                self.send_response(303)
                self.send_header("Location", f"/runs/{session.run_id}")
                self.end_headers()
                return
            if len(parts) != 3:
                self.send_error(404)
                return
            try:
                session = _find_session_by_run_id(parts[1])
            except SessionNotFound:
                self.send_error(404)
                return
            action = parts[2]
            submitted_version = data.get("expected_version", [""])[0].strip()
            # The operator is acting on what the page showed them. If the run
            # has moved since it was rendered, their decision was made about a
            # different situation -- most sharply when answering a question the
            # run is no longer asking. "0" and "" mean the form did not carry a
            # version, which is how every form behaved before this existed.
            if (
                submitted_version not in {"", "0"}
                and submitted_version != str(_run_version(session))
            ):
                self.send_error(
                    409,
                    "This run changed after the page was rendered; "
                    "reload it and repeat the action",
                )
                return
            try:
                if action == "confirm":
                    intent_name = data.get("intent", [""])[0]
                    if intent_name == "retry":
                        if _engineering_retry_patch(session) is None:
                            self.send_error(
                                409,
                                "Engineering retry requires a terminal run at the exact current revision",
                            )
                            return
                        idempotency_token = secrets.token_urlsafe(18)
                        confirmation_expires_at = str(
                            int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS
                        )
                        confirmation = _signed_confirmation(
                            "engineering-retry", session.run_id,
                            session.patchset, session.revision,
                            idempotency_token, confirmation_expires_at,
                        )
                        body = render_engineering_confirmation(
                            _engineering_projection(session),
                            "retry",
                            confirmation_token=confirmation,
                            confirmation_expires_at=confirmation_expires_at,
                            csrf_token=CSRF_TOKEN,
                            idempotency_token=idempotency_token,
                            base_url="/runs",
                        )
                        self.respond(_standalone_document(
                            "Final engineering retry confirmation", body
                        ))
                        return
                    intent, confirmation = SESSION_STORE.request_destructive_control(
                        session.session_id, intent_name, "operator"
                    )
                    body = render_destructive_confirmation(
                        _run_projection(session),
                        intent_name,
                        confirmation_token=confirmation,
                        csrf_token=CSRF_TOKEN,
                        idempotency_token=intent.request_id,
                    )
                    self.respond(_standalone_document("Final confirmation", body))
                    return
                if action == "retry":
                    patch = _engineering_retry_patch(session)
                    if patch is None:
                        self.send_error(
                            409,
                            "Engineering retry requires a terminal run at the exact current revision",
                        )
                        return
                    idempotency_token = data.get(
                        "idempotency_token", [""]
                    )[0]
                    confirmation_expires_at = data.get(
                        "confirmation_expires_at", [""]
                    )[0]
                    confirmation = data.get(
                        "confirmation_token", [""]
                    )[0]
                    if (
                        not _engineering_confirmation_unexpired(
                            confirmation_expires_at
                        )
                        or not _verify_confirmation(
                            confirmation, "engineering-retry", session.run_id,
                            session.patchset, session.revision,
                            idempotency_token, confirmation_expires_at,
                        )
                    ):
                        self.send_error(
                            403, "Invalid or stale engineering retry confirmation"
                        )
                        return
                    if RUN_CONTROLLER is None:
                        self.send_error(503, "The run controller is not initialized")
                        return
                    if not _claim_engineering_confirmation(
                        confirmation, idempotency_token
                    ):
                        self.send_error(
                            409, "Engineering confirmation was already used"
                        )
                        return
                    new_session = RUN_CONTROLLER.request_engineering(
                        patch, request_id=idempotency_token
                    )
                    self.send_response(303)
                    self.send_header(
                        "Location", f"/runs/{new_session.run_id}"
                    )
                    self.end_headers()
                    return
                if action in {"cancel", "kill"}:
                    request_id = data.get("idempotency_token", [""])[0]
                    SESSION_STORE.confirm_control_with_token(
                        session.session_id,
                        request_id,
                        data.get("confirmation_token", [""])[0],
                        "operator",
                    )
                elif action == "pause":
                    SESSION_STORE.request_pause(session.session_id, "operator")
                elif action == "interrupt":
                    SESSION_STORE.request_interrupt(session.session_id, "operator")
                elif action == "resume":
                    if session.state not in {"paused", "waiting_external", "blocked"}:
                        raise InvalidSessionOperation("this run cannot be resumed from its current state")
                    SESSION_STORE.set_state(session.session_id, "running")
                    SESSION_STORE.enqueue_guidance(
                        session.session_id,
                        "Continue from the previous safe boundary.",
                        idempotency_key="resume:" + secrets.token_urlsafe(18),
                    )
                elif action == "guidance":
                    message = data.get("message", [""])[0].strip()
                    mode = data.get("delivery_mode", ["safe_boundary"])[0]
                    # Validate everything BEFORE touching the run. Resuming or
                    # interrupting first and only then letting enqueue_guidance
                    # reject the message left the run resumed or interrupted
                    # while the error page told the operator, in those words,
                    # that "the run itself is unchanged" -- and the textarea's
                    # `required` does not stop whitespace, so a stray space bar
                    # was enough to silently resume a paused agent with no new
                    # instruction.
                    if mode not in GUIDANCE_DELIVERY_MODES:
                        self.send_error(400, f"unsupported delivery mode: {mode}")
                        return
                    if not message:
                        raise InvalidSessionOperation(
                            "guidance needs a message"
                        )
                    if mode == "resume_with_message" and session.state != "paused":
                        raise InvalidSessionOperation(
                            "only a paused run can resume with guidance"
                        )
                    if mode == "answer":
                        SESSION_STORE.answer_human_question(
                            session.session_id,
                            data.get("question_id", [""])[0],
                            answered_by="operator",
                            answer=message,
                        )
                    else:
                        if mode == "resume_with_message":
                            SESSION_STORE.set_state(session.session_id, "running")
                        if mode == "interrupt_and_send":
                            SESSION_STORE.request_interrupt(session.session_id, "operator")
                        SESSION_STORE.enqueue_guidance(
                            session.session_id,
                            message,
                            idempotency_key=(
                                data.get("idempotency_token", [""])[0]
                                or "guidance:" + secrets.token_urlsafe(18)
                            ),
                        )
                elif action == "follow-up":
                    source_patch = next((
                        item for item in PATCHES
                        if str(item.get("change_number")) == session.patch_id
                    ), None)
                    if source_patch is None:
                        raise InvalidSessionOperation("the watched patch no longer exists")
                    follow_up = RUN_CONTROLLER.request_investigation(source_patch)
                    message = data.get("message", [""])[0].strip()
                    if message:
                        SESSION_STORE.enqueue_guidance(
                            follow_up.session_id,
                            message,
                            idempotency_key="follow-up:" + secrets.token_urlsafe(18),
                        )
                    self.send_response(303)
                    self.send_header("Location", f"/runs/{follow_up.run_id}")
                    self.end_headers()
                    return
                else:
                    self.send_error(404)
                    return
            except (
                InvalidSessionOperation, RunControllerError,
                SessionAlreadyExists, SessionNotFound, ValueError,
            ) as exc:
                # The notice used to be appended AFTER </main>, so the only
                # explanation the operator got rendered outside the document
                # and unstyled. Put it inside the run detail's own <main>.
                detail = run_detail_html(
                    SESSION_STORE.get_session(session.session_id)
                )
                notice = (
                    "<p class='notice' role='alert'><strong>That control could "
                    "not be applied to this run:</strong> " + escape(str(exc))
                    + " The run itself is unchanged; its current state is "
                    "below.</p>"
                )
                opening = "<main class='run-detail'>"
                body = (
                    detail.replace(opening, opening + notice, 1)
                    if opening in detail else notice + detail
                )
                self.respond(_standalone_document("Run control error", body))
                return
            self.send_response(303)
            self.send_header("Location", f"/runs/{session.run_id}")
            self.end_headers()
            return
        # These mutate watch-list and side-effecting state (adding/removing a
        # patch, forcing a poll, sending mail). They are same-origin form posts
        # from the dashboard and must carry the token, exactly like every
        # branch above; without this a page the operator merely visits can
        # drive the tool.
        if not hmac.compare_digest(data.get("csrf_token", [""])[0], CSRF_TOKEN):
            self.send_error(403, "Invalid request token")
            return
        if path == "/resources/refresh":
            refresh_resource_status(force=True)
        elif path in {"/vms/stop", "/vms/destroy"}:
            # Operator-initiated guest control. These act on guests Patch
            # Watcher does NOT own -- that is the whole point of the panel
            # they live in -- so they deliberately bypass the controller's
            # ownership proof, which exists to stop the CONTROLLER acting on
            # its own initiative. The authority here is the human at the
            # dashboard, so the only gates are the CSRF token above and, for
            # the irreversible one, an explicit confirmation.
            name = data.get("name", [""])[0]
            try:
                target = ltvm_guest_name(name)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            if path == "/vms/destroy":
                confirmation = data.get("confirmation_token", [""])[0]
                expires_at = data.get("confirmation_expires_at", [""])[0]
                if not (
                    _engineering_confirmation_unexpired(expires_at)
                    and _verify_confirmation(
                        confirmation, "destroy-vm", target, expires_at
                    )
                    and _claim_engineering_confirmation(confirmation, target)
                ):
                    self.respond(_standalone_document(
                        "Confirm destroying an LTVM guest",
                        _destroy_vm_confirmation_html(target),
                    ))
                    return
            try:
                adapter = LTVMAdapter()
                if path == "/vms/stop":
                    adapter.operator_stop(target)
                    outcome = f"Shut down {target}."
                else:
                    adapter.operator_destroy(target)
                    outcome = f"Destroyed {target}."
            except (LTVMCommandError, ValueError) as exc:
                log_structured_error("ltvm_operator_control", f"{target}: {exc}", "")
                self.respond(page(f"Could not act on {target}: {exc}"))
                return
            refresh_resource_status(force=True)
            self.respond(page(outcome))
            return
        elif path == "/add":
            url = data.get("url", [""])[0]
            patch, error = add_patch(url)
            if error:
                self.respond(page(error))
                return
            refresh_watched_patch(patch)
            if RETEST_CONTROLLER is not None and patch.get("revision_sha"):
                RETEST_CONTROLLER.tick_patch(patch)
            try:
                save_watch_file(ACTIVE_WATCH_FILE)
            except OSError as exc:
                self.respond(page(f"Could not save the watch list: {exc}"))
                return
        elif path == "/remove":
            url = data.get("url", [""])[0]
            with PATCHES_LOCK:
                target = next((p for p in PATCHES if p["url"] == url), None)
            if target is None:
                self.respond(page("That patch is not being watched."))
                return
            # Remove used to be the only one-click mutation on the page. Show
            # what it does -- above all to any run still active on the patch --
            # before doing it.
            if not (
                _engineering_confirmation_unexpired(
                    data.get("confirmation_expires_at", [""])[0]
                )
                and _verify_confirmation(
                    data.get("confirmation_token", [""])[0], "remove-patch",
                    url, data.get("confirmation_expires_at", [""])[0],
                )
            ):
                self.respond(_standalone_document(
                    "Confirm removing a watched patch",
                    _remove_confirmation_html(target),
                ))
                return
            with PATCHES_LOCK:
                PATCHES[:] = [p for p in PATCHES if p["url"] != url]
            try:
                save_watch_file(ACTIVE_WATCH_FILE)
            except OSError as exc:
                self.respond(page(f"Could not save the watch list: {exc}"))
                return
            _forget_patch_automation(target)
        elif path in {"/refresh-all", "/auto-refresh"}:
            if AUTOMATION_OBSERVER is not None:
                AUTOMATION_OBSERVER.tick()
            else:
                for patch in _patch_snapshot():
                    refresh_watched_patch(patch)
        elif path == "/email":
            try:
                config = GerritConfig.load()
                result = send_status_email(config)
                self.respond(page(result.message))
            except GerritConfigError as exc:
                self.respond(page(str(exc)))
            return
        else:
            self.send_error(404)
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()
    def respond(self, body):
        # Content-Length matters more than usual here: without it a truncated
        # write is indistinguishable from a complete response.
        # `backslashreplace`, not a bare encode: a single lone surrogate --
        # in an operator-typed patch title, an agent message, a guest name --
        # otherwise raises UnicodeEncodeError here, and because the value is
        # persisted every later request raises it too. The dashboard is then
        # dead rather than degraded. One mangled glyph is the better failure.
        encoded = body.encode("utf-8", "backslashreplace")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main(argv=None):
    """Console entry point for the local Patch Watcher web app."""

    parser = argparse.ArgumentParser(description="Run the local Patch Watcher web app")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed-file", type=Path, default=DEFAULT_SEED_FILE)
    parser.add_argument(
        "--session-database",
        type=Path,
        default=DEFAULT_SESSION_DATABASE,
        help="private SQLite database for managed-session state",
    )
    parser.add_argument(
        "--automation-database",
        type=Path,
        default=DEFAULT_AUTOMATION_DATABASE,
        help="private SQLite database for deterministic retest state",
    )
    parser.add_argument(
        "--standing-policy-file",
        type=Path,
        default=DEFAULT_STANDING_POLICY_FILE,
        help="private JSON file for per-patch standing automation policies",
    )
    parser.add_argument(
        "--autonomous-lane-file", type=Path,
        default=DEFAULT_AUTONOMOUS_LANE_FILE,
        help="private JSON file for autonomous-lane kill switches",
    )
    parser.add_argument(
        "--autonomous-lane-history", type=Path,
        default=DEFAULT_AUTONOMOUS_LANE_HISTORY,
        help="append-only autonomous-lane decision audit",
    )
    parser.add_argument(
        "--model",
        default="",
        help="default Claude model for runs started without an explicit choice",
    )
    parser.add_argument(
        "--effort",
        default="",
        choices=("", *AGENT_EFFORTS),
        help="default reasoning effort for runs started without an explicit choice",
    )
    parser.add_argument(
        "--daily-summary",
        action="store_true",
        help="refresh seeds, then send/dry-run the configured daily email",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    initialize_session_store(args.session_database)
    initialize_automation_store(args.automation_database)
    initialize_standing_policy_store(args.standing_policy_file)
    initialize_autonomous_lanes(
        args.autonomous_lane_file, args.autonomous_lane_history,
    )
    snapshot = refresh_resource_status(force=True)
    host = snapshot.get("host_memory", {}) if isinstance(snapshot, Mapping) else {}
    if host.get("quality") == "unavailable":
        for error in host.get("errors") or ():
            print("Resource collection is degraded: "
                  f"{error.get('message') if isinstance(error, Mapping) else error}")
    load_seed_file(args.seed_file)
    if args.daily_summary:
        try:
            config = GerritConfig.load()
        except GerritConfigError as exc:
            # This runs from cron. An uncaught traceback here is mailed to the
            # operator every night, and says nothing about what to do; the
            # server path already reports the same condition as a page.
            print(f"Cannot send the daily summary: {exc}")
            print("Run pw-configure to set up Gerrit access, then pw-doctor to check it.")
            raise SystemExit(1) from None
        result = send_daily_summary(
            PATCHES,
            config,
            automation_events=automation_daily_events(),
        )
        print(result.message)
        raise SystemExit(0 if result.sent or not config.email_enabled else 1)
    initialize_run_controller(
        model=args.model or None, effort=args.effort or None,
    )
    initialize_retest_controller()
    print(f"Patch Watcher listening on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
