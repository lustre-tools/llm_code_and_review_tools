#!/usr/bin/env python3
"""Small, dependency-free Patch Watcher web application."""
import argparse
import hashlib
import hmac
import json
import platform
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Mapping
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from gerrit_status import (
    GerritConfig,
    GerritConfigError,
    GerritStatusClient,
    parse_change_number,
    refresh_patch,
)
from automation_state import (
    AutomationConflict,
    AutomationNotFound,
    AutomationStateStore,
)
from reporting import log_structured_error
from reporting import send_daily_summary
from reporting import send_automation_alert
from reporting import send_session_alert
from resource_status import collect_process_tree_rss, collect_resource_snapshot
from resource_views import render_resource_dashboard
from engineering_views import (
    render_engineering_confirmation,
    render_engineering_dashboard,
    render_engineering_start_confirmation,
    render_engineering_start_control,
)
from ltvm_resources import LTVMAdapter, owner_id_for_session
from session_state import (
    ABSOLUTE_RUNTIME_CAP,
    ENGINEERING_INACTIVITY_LIMIT,
    TRIAGE_WALL_LIMIT,
    InvalidSessionOperation,
    SessionAlreadyExists,
    SessionNotFound,
    SessionStateStore,
)
from worker_admission_views import render_worker_admission
from worker_contract import load_profile
from run_controller import (
    RunController,
    RunControllerError,
    normalize_unknown_failure_evidence,
    unknown_failure_research_run_id,
)
from run_views import (
    render_destructive_confirmation,
    render_investigate_control,
    render_run_detail,
    render_run_summary,
)
from maloo_adapter import MalooAdapter
from observer import BackgroundObserver
from retest_controller import ControllerNotification, PatchRevision, RetestController
from retest_views import (
    render_action_confirmation,
    render_enable_confirmation,
    render_global_retest_status,
    render_policy_confirmation,
    render_retest_control,
)
from failure_actions import (
    FailureActionController,
    FailureActionError,
    LINK_ACTION as FAILURE_LINK_ACTION,
    RETEST_ACTION as FAILURE_RETEST_ACTION,
)
from research_views import (
    render_action_approval_card as render_failure_approval_card,
    render_action_confirmation as render_failure_action_confirmation,
    render_failure_action_status,
    render_research_policy_confirmation,
    render_research_policy_form,
    render_research_session,
    render_unknown_failure_control,
)

PATCHES = []
DEFAULT_SEED_FILE = Path.home() / ".config" / "patch-watcher" / "patches.txt"
DEFAULT_SESSION_DATABASE = (
    Path.home() / ".local" / "state" / "patch-watcher" / "sessions.sqlite3"
)
DEFAULT_AUTOMATION_DATABASE = (
    Path.home() / ".local" / "state" / "patch-watcher" / "automation.sqlite3"
)
DEFAULT_WORKER_PROFILE_ID = "host-unsandboxed-mac-v1"
DEFAULT_ENGINEERING_WORKER_PROFILE_ID = "host-unsandboxed-mac-engineering-v1"
ACTIVE_WATCH_FILE = DEFAULT_SEED_FILE
ACTIVE_SESSION_DATABASE = DEFAULT_SESSION_DATABASE
ACTIVE_AUTOMATION_DATABASE = DEFAULT_AUTOMATION_DATABASE
JIRA_BASE_URL = "https://jira.whamcloud.com/browse"
SESSION_STORE = None
WORKER_PROFILE = None
ENGINEERING_WORKER_PROFILE = None
RUN_CONTROLLER = None
AUTOMATION_STORE = None
RETEST_CONTROLLER = None
FAILURE_ACTION_CONTROLLER = None
AUTOMATION_OBSERVER = None
PATCHES_LOCK = threading.RLock()
CSRF_TOKEN = secrets.token_urlsafe(32)
MAX_FORM_BODY_BYTES = 64 * 1024
MAX_FORM_FIELDS = 128
MALOO_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,199}")
RESOURCE_COLLECTION_ENABLED = False
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


def initialize_automation_store(database=DEFAULT_AUTOMATION_DATABASE):
    """Open the private durable deterministic-automation ledger."""
    global AUTOMATION_STORE, ACTIVE_AUTOMATION_DATABASE
    ACTIVE_AUTOMATION_DATABASE = Path(database)
    AUTOMATION_STORE = AutomationStateStore(ACTIVE_AUTOMATION_DATABASE)
    return AUTOMATION_STORE


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
    return hmac.new(CSRF_TOKEN.encode("utf-8"), payload, hashlib.sha256).hexdigest()


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
        lambda: _patch_snapshot(),
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
    """Reconcile approved failure writes without touching Phase 1 runs."""
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
    research_mode = "disabled"
    if patch_record is not None:
        research_mode = AUTOMATION_STORE.get_research_policy(
            patch_record.patch_id
        ).mode
    result = RETEST_CONTROLLER.tick_patch(
        patch,
        collect_research_evidence=research_mode != "disabled",
    )
    _advance_failure_action_runs(result.patch_id)
    if (
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


def initialize_worker_profile(profile_id=DEFAULT_WORKER_PROFILE_ID):
    """Load and validate the declared worker profile used for new runs."""
    global WORKER_PROFILE
    WORKER_PROFILE = load_profile(profile_id)
    return WORKER_PROFILE


def initialize_engineering_worker_profile(
    profile_id=DEFAULT_ENGINEERING_WORKER_PROFILE_ID,
):
    """Load the separately declared source-edit capability boundary."""
    global ENGINEERING_WORKER_PROFILE
    ENGINEERING_WORKER_PROFILE = load_profile(profile_id)
    return ENGINEERING_WORKER_PROFILE


def initialize_run_controller(*, runs_directory=None, start=True):
    """Create the background dispatcher after state/profile initialization."""
    global RUN_CONTROLLER
    if SESSION_STORE is None:
        initialize_session_store()
    if WORKER_PROFILE is None:
        initialize_worker_profile()
    if ENGINEERING_WORKER_PROFILE is None:
        initialize_engineering_worker_profile()

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

    options = {"alert_sender": alert, "ltvm_adapter": LTVMAdapter()}
    if runs_directory is not None:
        options["runs_directory"] = Path(runs_directory)
    RUN_CONTROLLER = RunController(
        SESSION_STORE,
        WORKER_PROFILE,
        engineering_profile=ENGINEERING_WORKER_PROFILE,
        **options,
    )
    if start:
        RUN_CONTROLLER.start()
    return RUN_CONTROLLER


def worker_admission_html():
    """Render the newest persisted admission or the declared host profile."""
    try:
        profile = WORKER_PROFILE or initialize_worker_profile()
    except (OSError, ValueError) as exc:
        return render_worker_admission({
            "status": "blocked",
            "failure_code": "profile_unknown",
            "failure_summary": str(exc)[:500],
        })

    profile_view = profile.to_dict()
    if profile.isolation_profiles:
        profile_view["isolation_profile"] = profile.isolation_profiles[0]
    if profile.network_profiles:
        profile_view["network_profile"] = profile.network_profiles[0]

    admission = None
    attestation = None
    if SESSION_STORE is not None:
        admissions = SESSION_STORE.list_worker_admissions()
        if admissions:
            admission = admissions[0]
            attestation = admission.attestation
    if admission is None:
        admission = {"status": "not_checked"}
    return render_worker_admission(
        admission,
        profile=profile_view,
        attestation=attestation,
    )


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
    _RESOURCE_SNAPSHOT = collect_resource_snapshot()
    _RESOURCE_SNAPSHOT_MONOTONIC = now
    return _RESOURCE_SNAPSHOT


def _session_dashboard_records(now=None):
    """Project durable active sessions and bounded messages for the view."""
    if SESSION_STORE is None:
        return [], {}
    observed_at = now or datetime.now(timezone.utc)
    sessions = []
    messages_by_session = {}
    for session in SESSION_STORE.list_sessions(include_terminal=False):
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
            "process_id": session.pid,
            "current_step": "Managed session",
        }
        if session.pid:
            process_memory = collect_process_tree_rss(session.pid)
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
        show_controls=False,
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
    if AUTOMATION_STORE is None:
        return []
    events = []
    for run in AUTOMATION_STORE.list_runs():
        for event in AUTOMATION_STORE.list_timeline(run.run_id):
            events.append({
                "created_at": event.created_at.isoformat(),
                "patch_id": run.patch_id,
                "event_type": event.event_type,
                "summary": str(event.payload.get("summary") or "Recorded")[:500],
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
    with PATCHES_LOCK:
        if any(p["url"] == url for p in PATCHES):
            return None, "That patch is already being watched."
    patch = {
        "url": url,
        "title": title.strip() or str(parse_change_number(url)),
        "status": "Pending",
        "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    with PATCHES_LOCK:
        PATCHES.append(patch)
    return patch, None


def ticket_from_title(title):
    """Return the leading Jira issue key used by Lustre patch subjects."""
    import re

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
        "needs-review": "warn",
        "awaiting-ci": "warn",
        "work-in-progress": "info",
        "uninitialized": "neutral",
    }
    text = str(value or "unknown").replace("-", " ").title()
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
    """Return the newest successful or attempted check shown on the page."""
    with PATCHES_LOCK:
        checked = [
            str(patch.get("last_checked", ""))
            for patch in PATCHES
            if patch.get("last_checked") not in {None, "", "—"}
        ]
    return max(checked) if checked else "Never"


def _active_session_for_patch(change_number):
    if SESSION_STORE is None:
        return None
    for session in SESSION_STORE.list_sessions(include_terminal=False):
        if session.patch_id == str(change_number):
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
    observation, unknown = _unknown_failures(patch)
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


def _research_and_failure_html(patch):
    policy, failures, session, report = _research_context(patch)
    active = _active_session_for_patch(patch.get("change_number"))
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
    sections = [
        render_research_policy_form(
            research_patch,
            policy=policy,
            csrf_token=CSRF_TOKEN,
            idempotency_token=secrets.token_urlsafe(18),
        ),
        render_unknown_failure_control(
            research_patch,
            policy=policy,
            csrf_token=CSRF_TOKEN,
            idempotency_token=secrets.token_urlsafe(18),
        ),
    ]
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
                "Set Test failure handling to Approval before planning writes."
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


def global_retest_html():
    if AUTOMATION_STORE is None:
        return render_global_retest_status(
            execution_enabled=False,
            csrf_token=CSRF_TOKEN,
            recent_summary="Automation state is not initialized.",
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
    )


def _run_projection(session, *, now=None):
    """Project durable state plus bounded live telemetry for run views."""
    observed_at = now or datetime.now(timezone.utc)
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
    memory = None
    if session.pid:
        measured = collect_process_tree_rss(session.pid)
        memory = measured.total_rss_bytes or measured.known_rss_bytes
    return {
        "run_id": session.run_id,
        "session_id": session.session_id,
        "change_number": session.patch_id,
        "subject": session.patch_id,
        "patchset": session.patchset,
        "revision_sha": session.revision,
        "state": session.state,
        "profile": session.profile,
        "execution_profile": session.profile,
        "model": getattr(RUN_CONTROLLER, "model", "") or "Configured default",
        "pid": session.pid,
        "process_pid": session.pid,
        "process_memory_bytes": memory,
        "started_at": session.started_at.isoformat(),
        "last_activity_at": session.last_qualifying_activity_at.isoformat(),
        "elapsed_seconds": elapsed,
        "runtime_remaining_seconds": runtime_remaining,
        "inactivity_remaining_seconds": inactivity_remaining,
        "absolute_remaining_seconds": absolute_remaining,
        "current_step": session.state.replace("_", " ").title(),
        "latest_message": latest,
        "version": 0,
    }


def active_runs_html():
    if SESSION_STORE is None:
        return ""
    sessions = SESSION_STORE.list_sessions(include_terminal=False)
    if not sessions:
        return "<section class='card'><h2>Agent runs</h2><p class='empty'>No active agent runs.</p></section>"
    return (
        "<section class='card'><h2>Agent runs</h2><div class='run-grid'>"
        + "".join(render_run_summary(_run_projection(item)) for item in sessions)
        + "</div></section>"
    )


def _engineering_projection(session):
    """Join session, checkout, manifest, and captured evidence for Phase 3 views."""
    projection = _run_projection(session)
    projection["owner_id"] = owner_id_for_session(session.session_id)
    if RUN_CONTROLLER is None:
        return projection
    allocation = RUN_CONTROLLER.engineering_store.get_allocation_by_run(session.run_id)
    if allocation is not None:
        projection["checkout"] = {
            "state": allocation.state,
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
            "isolation_profile": "session-owned-ltvm",
            "network_profile": "controller-mediated",
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
    return projection


def engineering_runs_html():
    """Render Phase 3 sessions without granting any action by rendering alone."""
    if SESSION_STORE is None:
        return ""
    sessions = [
        item for item in SESSION_STORE.list_sessions(include_terminal=True)
        if item.profile == "engineering" and item.run_id.startswith("pw-engineer-")
    ][:20]
    runs = [_engineering_projection(item) for item in sessions]
    messages = {item.run_id: _run_messages(item) for item in sessions}
    # Use the same timestamped sample as the host-resource dashboard.  The
    # sampler augments LTVM's configured guest memory with verified QEMU RSS;
    # raw ``ltvm list --json`` deliberately cannot supply that host measure.
    snapshot = refresh_resource_status()
    ltvm = snapshot.get("ltvm", {}) if isinstance(snapshot, Mapping) else {}
    vms = list(ltvm.get("vms", ())) if isinstance(ltvm, Mapping) else []
    return (
        "<section class='card'>"
        + render_engineering_dashboard(
            runs,
            vms=vms,
            messages_by_run=messages,
            base_url="/runs",
            csrf_token=CSRF_TOKEN,
            idempotency_token=secrets.token_urlsafe(18),
        )
        + "</section>"
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


def _run_events(session):
    result = []
    for item in SESSION_STORE.list_events(session.session_id):
        payload = item.payload
        result.append({
            "event_type": item.event_type,
            "created_at": item.created_at.isoformat(),
            "summary": payload.get("summary") or payload.get("runner_type") or "Recorded",
        })
    return result


def run_detail_html(session):
    questions = SESSION_STORE.list_human_questions(session.session_id)
    question = next((item for item in reversed(questions) if item.status == "open"), None)
    admission = SESSION_STORE.get_worker_admission(session.session_id)
    return render_run_detail(
        _run_projection(session),
        messages=_run_messages(session),
        events=_run_events(session),
        admission=admission,
        question=question,
        csrf_token=CSRF_TOKEN,
        idempotency_token=secrets.token_urlsafe(18),
    )


def _standalone_document(title, body):
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{escape(title)}</title><style>
body{{margin:0;background:#f5f7fb;color:#172033;font:15px system-ui,sans-serif}}main{{max-width:1100px;margin:42px auto;padding:24px;background:white;border:1px solid #e4e7ec;border-radius:14px}}section{{border-top:1px solid #eaecf0;padding-top:18px;margin-top:18px}}dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}}dt{{font-size:12px;color:#667085}}dd{{margin:4px 0;word-break:break-word}}textarea{{width:min(720px,95%);min-height:90px;display:block;margin:8px 0}}button{{border:0;border-radius:8px;padding:10px 14px;background:#315efb;color:white;font-weight:600}}form.inline-control{{display:inline-block;margin:5px}}.danger-link,.danger{{color:#b42318}}.run-state,.worker-boundary{{display:inline-block;border-radius:999px;padding:4px 8px;background:#f2f4f7;margin:3px}}.tone-good{{background:#dcfce7}}.tone-warn{{background:#fef3c7}}.tone-bad{{background:#fee2e2}}.run-conversation,.run-timeline{{max-height:400px;overflow:auto}}.safety-note{{padding:10px;background:#eff8ff;border-radius:8px}}code{{word-break:break-all}}</style></head><body>{body}</body></html>"""


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
    active = _active_session_for_patch(patch.get("change_number"))
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
    )
    research_html = _research_and_failure_html(patch)
    return (
        "<tr><td>"
        f"<a href='{escape(patch['url'], quote=True)}' target='_blank' rel='noreferrer'>"
        f"{escape(title)}</a>{ticket_html}"
        f"<div class='url'>{escape(patch['url'])}</div>"
        f"<div class='patch-meta'>{patchset_html}</div>{error_html}</td>"
        f"<td>{_watch_chip(patch.get('watch_state', 'uninitialized'))}"
        f"<div class='detail'>{escape(str(patch.get('recommendation', '')))}</div>"
        f"<div class='ci-stack'>{_ci_chip('Jenkins', patch.get('jenkins', '—'), patch.get('jenkins_url', ''))}"
        f"{_ci_chip('Maloo', patch.get('maloo', '—'), patch.get('maloo_url', ''))}</div></td>"
        f"<td>{_review_chip(patch)}"
        f"<div class='detail'>{escape(_vote_summary(patch))} · "
        f"{escape(str(patch.get('unresolved', 0)))} unresolved</div></td>"
        f"<td>{escape(patch.get('change_summary', '—') or '—')}"
        f"<div class='detail'>Changed: {escape(patch.get('last_changed', '—') or '—')}</div>"
        f"{_history_html(patch)}</td>"
        "<td><div class='actions'>"
        f"<form method='post' action='/remove'><input type='hidden' name='url' "
        f"value='{escape(patch['url'], quote=True)}'><button class='danger'>Remove</button></form>"
        f"{investigate_html}"
        f"{engineering_html}"
        f"{retest_html}"
        f"{research_html}"
        "</div></td></tr>"
    )


def page(message="", jira_base=JIRA_BASE_URL):
    refresh_interval = configured_refresh_interval()
    resources = resource_dashboard_html()
    worker_admission = worker_admission_html()
    with PATCHES_LOCK:
        patches = [dict(patch) for patch in PATCHES]
    rows = "".join(
        _patch_row(patch, jira_base) for patch in patches
    ) or "<tr><td colspan='5' class='empty'>No patches yet. Add a Gerrit change to start watching.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Patch Watcher</title><style>
body{{margin:0;background:#f5f7fb;color:#172033;font:15px system-ui,sans-serif}}main{{max-width:1450px;margin:48px auto;padding:0 24px}}h1{{margin-bottom:6px}}.sub{{color:#667085;margin-top:0}}.card,.resource-card{{background:white;border:1px solid #e4e7ec;border-radius:14px;padding:22px;margin-top:28px;box-shadow:0 4px 16px #1018280a;overflow-x:auto}}form.add{{display:flex;gap:10px;flex-wrap:wrap}}input,textarea,select{{border:1px solid #d0d5dd;border-radius:8px;padding:11px 12px;font-size:14px}}input,textarea{{flex:1;min-width:240px}}textarea{{display:block;width:min(620px,95%);min-height:70px;margin:7px 0 10px}}button,.button-link{{border:0;border-radius:8px;padding:11px 16px;background:#315efb;color:white;font-weight:600;cursor:pointer}}.button-link{{display:inline-block;text-decoration:none}}button:disabled{{cursor:not-allowed;opacity:.68}}button.danger,button.secondary,.button-link.danger-link{{background:#fff;padding:7px 11px}}button.danger,.button-link.danger-link{{color:#b42318;border:1px solid #fecdca}}button.secondary{{color:#344054;border:1px solid #d0d5dd}}table{{width:100%;border-collapse:collapse;margin-top:18px;min-width:1050px}}th,td{{text-align:left;padding:14px 10px;border-top:1px solid #eaecf0;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:#667085}}.url,.detail{{color:#667085;font-size:12px;margin-top:4px;word-break:break-word}}.patch-meta{{display:flex;align-items:center;gap:6px;color:#667085;font-size:12px;margin-top:7px}}.ticket{{display:inline-block;margin-left:8px;font-size:12px}}.actions{{display:flex;gap:6px;flex-wrap:wrap}}.actions form{{margin:0}}.error{{color:#b42318;font-size:12px;margin-top:5px;max-width:340px}}.empty{{text-align:center;color:#667085;padding:35px}}.notice{{background:#fffaeb;color:#b54708;padding:10px 12px;border-radius:8px;margin-top:16px}}.section-title{{display:flex;justify-content:space-between;align-items:center;gap:16px}}small{{display:block;color:#667085;margin-top:4px}}details{{margin-top:7px;font-size:12px;color:#475467}}details ol{{padding-left:18px;max-height:140px;overflow:auto}}details li{{margin:5px 0}}details time{{font-variant-numeric:tabular-nums}}.history-state{{color:#667085}}.status-chip,.resource-status,.admission-status,.worker-boundary{{display:inline-block;border:1px solid transparent;border-radius:999px;padding:3px 8px;font-size:12px;font-weight:700;line-height:1.35;white-space:nowrap}}.tone-good{{background:#dcfce7;border-color:#86efac;color:#166534}}.tone-bad{{background:#fee2e2;border-color:#fca5a5;color:#991b1b}}.tone-warn{{background:#fef3c7;border-color:#fcd34d;color:#78350f}}.tone-info{{background:#dbeafe;border-color:#93c5fd;color:#1e3a8a}}.tone-neutral{{background:#f2f4f7;border-color:#d0d5dd;color:#344054}}.status-link{{text-decoration:none}}.status-link:focus-visible .status-chip{{outline:3px solid #315efb;outline-offset:2px}}.ci-stack{{display:flex;align-items:flex-start;gap:5px;flex-wrap:wrap;margin-top:8px}}.retest-control{{width:min(390px,85vw);padding:6px 8px;border:1px solid #d0d5dd;border-radius:8px}}.retest-control form{{display:grid;gap:7px;margin-top:9px}}.retest-control label{{display:grid;gap:4px}}.retest-control input,.retest-control select{{box-sizing:border-box;min-width:0;width:100%;padding:7px 8px}}.retest-decision,.retest-approval{{display:grid;gap:6px;margin-top:9px;padding:8px;background:#f8fafc;border-radius:7px}}.retest-approval{{background:#fffaeb}}.retest-timeline{{padding-left:18px}}.retest-global form{{margin-top:12px}}.stub-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}.stub-option{{display:flex;align-items:flex-start;gap:10px;text-align:left;background:#f8fafc;color:#344054;border:1px solid #d0d5dd;padding:14px}}.stub-label{{display:block;color:#667085;font-size:12px;font-weight:500;margin-top:4px}}.stub-tag{{display:inline-block;margin-left:6px;border:1px solid #d0d5dd;border-radius:999px;padding:1px 6px;font-size:10px;text-transform:uppercase}}.resource-toolbar{{display:flex;justify-content:flex-end;margin-top:20px}}.resource-dashboard{{display:grid;gap:18px}}.resource-card{{margin-top:0}}.resource-metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.resource-metric{{background:#f8fafc;border:1px solid #eaecf0;border-radius:10px;padding:12px}}.resource-metric dt{{font-size:12px;color:#667085}}.resource-metric dd{{margin:5px 0 0;font-size:18px;font-weight:700}}.resource-errors{{color:#b42318}}.resource-ok{{color:#027a48}}.session-controls{{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-top:16px}}fieldset{{border:1px solid #fecdca;border-radius:8px}}.message-content{{white-space:pre-wrap;margin-top:3px}}.worker-admission{{margin-top:18px}}.worker-admission-heading{{display:flex;justify-content:space-between;gap:12px;align-items:center}}.worker-boundaries{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}}.worker-provenance{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}.worker-provenance div{{background:#f8fafc;border:1px solid #eaecf0;border-radius:8px;padding:10px}}.worker-provenance dt{{font-size:12px;color:#667085}}.worker-provenance dd{{margin:4px 0 0;word-break:break-word}}.admission-failures{{color:#b42318}}@media(max-width:760px){{.session-controls{{grid-template-columns:1fr}}}}</style></head>
<body><style>.research-controls{{width:min(430px,88vw);border:1px solid #d0d5dd;border-radius:8px;padding:8px}}.research-controls>summary{{cursor:pointer;font-weight:700}}.research-controls section{{border-top:1px solid #eaecf0;margin-top:10px;padding-top:10px}}.research-controls form{{display:grid;gap:7px;margin-top:8px}}.research-controls input,.research-controls select{{box-sizing:border-box;min-width:0;width:100%;padding:7px 8px}}.research-controls dl{{display:grid;gap:6px}}.research-controls dd{{margin:2px 0 6px;word-break:break-word}}.action-approval-card{{background:#fffaeb;border:1px solid #fedf89;border-radius:8px;padding:10px}}</style><main><h1>Patch Watcher</h1><p class='sub'>Track Gerrit patches, managed sessions, and worker resources.</p>
<div class='resource-toolbar'><form method='post' action='/resources/refresh'><button class='secondary'>Refresh resource status</button></form></div>{resources}{worker_admission}{global_retest_html()}
{active_runs_html()}{engineering_runs_html()}<section class='card'><h2>Add a patch</h2><form class='add' method='post' action='/add'><input name='url' required placeholder='https://review.whamcloud.com/c/...'><button>Add patch</button></form>{f"<div class='notice'>{escape(message)}</div>" if message else ''}</section>
<section class='card'><div class='section-title'><div><h2>Watched patches <small>({len(patches)} · checks every {refresh_interval}s)</small></h2><div class='detail'>Overall last checked: {escape(overall_last_checked())}</div></div><div class='actions'><form method='post' action='/refresh-all'><button class='secondary'>Refresh all</button></form><form method='post' action='/email'><button class='secondary'>Send status email</button></form></div></div><table><thead><tr><th>Patch</th><th>Watch state / CI</th><th>Review</th><th>Latest change</th><th></th></tr></thead><tbody>{rows}</tbody></table></section>
<section class='card' aria-labelledby='handle-reviews-title'><h2 id='handle-reviews-title'>Handle reviews <span class='stub-tag'>Stub · disabled</span></h2><p class='sub'>Planned Claude Code workflows are visible for design review only. They cannot be selected and perform no Gerrit writes or Claude invocation.</p><div class='stub-grid'><button class='stub-option' type='button' disabled aria-disabled='true'><span>Handle simple comments<span class='stub-label'>Future: ask Claude to fix only clearly trivial comments; report and email-escalate everything complex or ambiguous.</span></span></button><button class='stub-option' type='button' disabled aria-disabled='true'><span>Handle all comments<span class='stub-label'>Future: ask Claude to attempt every comment; escalate whenever it cannot resolve one safely or requests human judgment.</span></span></button></div></section></main></body></html>"""


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
    """Atomically persist the current watch list as private URL-only config."""
    watch_path = Path(path)
    watch_path.parent.mkdir(parents=True, exist_ok=True)
    pending = watch_path.with_name(f".{watch_path.name}.tmp")
    with PATCHES_LOCK:
        contents = "".join(f"{patch['url']}\n" for patch in PATCHES)
    pending.write_text(contents, encoding="utf-8")
    pending.chmod(0o600)
    pending.replace(watch_path)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = [item for item in path.split("/") if item]
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
            patch = _find_exact_patch(change, patchset, revision)
            if (
                patch is None
                or not _engineering_confirmation_unexpired(
                    confirmation_expires_at
                )
                or not _verify_confirmation(
                    confirmation, "engineering-start", change, patchset,
                    revision, idempotency_token, confirmation_expires_at,
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
            patch = _find_exact_patch(change, patchset, revision)
            if (
                patch is None
                or AUTOMATION_STORE is None
                or not _verify_confirmation(
                    confirmation,
                    "research-policy", change, patchset, revision,
                    budget, expected_version,
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
            body = render_research_policy_confirmation(
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
            token = _signed_confirmation(
                "failure-action", action.action_id, run.revision, action.status
            )
            body = render_failure_action_confirmation(
                projection,
                confirmation_token=token,
                csrf_token=CSRF_TOKEN,
                idempotency_token=action.idempotency_key,
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
            body = render_action_confirmation(
                action_id=action.action_id,
                change_number=run.patch_id,
                revision_sha=run.revision,
                session_id=str(action.request.get("session_id") or ""),
                jira_ticket=str(action.request.get("jira_ticket") or ""),
                csrf_token=CSRF_TOKEN,
            )
            self.respond(_standalone_document("Approve Maloo retest", body))
            return
        if path == "/automation/global/confirm-enable":
            self.respond(_standalone_document(
                "Enable automatic retests",
                render_enable_confirmation(csrf_token=CSRF_TOKEN),
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
                if RUN_CONTROLLER is None or not session.run_id.startswith("pw-engineer-"):
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
                content = target.read_bytes()
                if (
                    len(content) != artifact.size_bytes
                    or hashlib.sha256(content).hexdigest() != artifact.sha256
                ):
                    self.send_error(409, "Captured artifact failed integrity verification")
                    return
                self.send_response(200)
                self.send_header("Content-Type", artifact.media_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
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
                body = (
                    "<main><p><a href='/runs/" + escape(session.run_id, quote=True) + "'>← Keep session running</a></p>"
                    f"<h2>Review {escape(intent)} request</h2>"
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
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(page().encode())
    def do_POST(self):
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
            if path == "/engineering-runs/prepare":
                idempotency_token = data.get("idempotency_token", [""])[0].strip()
                if not idempotency_token:
                    idempotency_token = secrets.token_urlsafe(18)
                confirmation_expires_at = str(
                    int(time.time()) + ENGINEERING_CONFIRMATION_TTL_SECONDS
                )
                confirmation = _signed_confirmation(
                    "engineering-start", change, patchset, revision,
                    idempotency_token, confirmation_expires_at,
                )
                query = urlencode({
                    "change_number": change,
                    "patchset": patchset,
                    "revision_sha": revision,
                    "confirmation_token": confirmation,
                    "idempotency_token": idempotency_token,
                    "confirmation_expires_at": confirmation_expires_at,
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
            if (
                not _engineering_confirmation_unexpired(
                    confirmation_expires_at
                )
                or not _verify_confirmation(
                    confirmation, "engineering-start", change, patchset,
                    revision, idempotency_token, confirmation_expires_at,
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
                    patch, request_id=idempotency_token
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
                        "Research mode is invalid or its run budget is outside 1–20",
                    )
                    return
                if mode == "automatic" and path == "/research/policy/prepare":
                    confirmation = _signed_confirmation(
                        "research-policy", change, patchset, revision,
                        budget, expected_version,
                    )
                    query = urlencode({
                        "change_number": change,
                        "patchset": patchset,
                        "revision_sha": revision,
                        "per_revision_run_budget": budget,
                        "expected_policy_version": expected_version,
                        "confirmation_token": confirmation,
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
                    if not _verify_confirmation(
                        confirmation,
                        "research-policy", change, patchset, revision,
                        budget, expected_version,
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
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
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
                    if not _verify_confirmation(
                        confirmation,
                        "failure-action", action.action_id,
                        run.revision, action.status,
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
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
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
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
                return
            if path == "/automation/global/enable":
                AUTOMATION_STORE.set_global_automation(
                    True,
                    changed_by="operator",
                    reason="Explicitly confirmed from the dashboard",
                )
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
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
                    AUTOMATION_STORE.approve_action(
                        action.action_id,
                        approved_by="operator",
                        expected_revision=expected_revision,
                        expected_policy_mode="approval",
                    )
                except (AutomationConflict, AutomationNotFound, ValueError) as exc:
                    self.respond(page(f"Retest approval was not recorded: {exc}"))
                    return
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
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
                    body = render_policy_confirmation(
                        change_number=str(change),
                        revision_sha=revision,
                        max_actions=max_actions,
                        csrf_token=CSRF_TOKEN,
                    )
                    self.respond(_standalone_document("Confirm automatic policy", body))
                    return
                sync_automation_patch(patch)
                AUTOMATION_STORE.set_policy(
                    str(change),
                    mode=mode,
                    action_budget=max_actions,
                    delivery_budget=4,
                    updated_by="operator",
                )
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
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
                    self.respond(page("The run controller is not initialized.")); return
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
                    self.respond(page("The patch changed; refresh before investigating.")); return
                try:
                    session = RUN_CONTROLLER.request_investigation(patch)
                except (RunControllerError, InvalidSessionOperation, ValueError) as exc:
                    self.respond(page(str(exc))); return
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
                    self.respond(_standalone_document("Final confirmation", body)); return
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
                    if mode == "answer":
                        SESSION_STORE.answer_human_question(
                            session.session_id,
                            data.get("question_id", [""])[0],
                            answered_by="operator",
                            answer=message,
                        )
                    else:
                        if mode == "resume_with_message":
                            if session.state != "paused":
                                raise InvalidSessionOperation("only a paused run can resume with guidance")
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
                self.respond(_standalone_document(
                    "Run control error", run_detail_html(SESSION_STORE.get_session(session.session_id))
                    + f"<p class='notice'>{escape(str(exc))}</p>"
                )); return
            self.send_response(303)
            self.send_header("Location", f"/runs/{session.run_id}")
            self.end_headers()
            return
        if path == "/resources/refresh":
            refresh_resource_status(force=True)
        elif path == "/add":
            url = data.get("url", [""])[0]
            patch, error = add_patch(url)
            if error: self.respond(page(error)); return
            refresh_watched_patch(patch)
            if RETEST_CONTROLLER is not None and patch.get("revision_sha"):
                RETEST_CONTROLLER.tick_patch(patch)
            try:
                save_watch_file(ACTIVE_WATCH_FILE)
            except OSError as exc:
                self.respond(page(f"Could not save the watch list: {exc}")); return
        elif path == "/remove":
            with PATCHES_LOCK:
                PATCHES[:] = [p for p in PATCHES if p["url"] != data.get("url", [""])[0]]
            try:
                save_watch_file(ACTIVE_WATCH_FILE)
            except OSError as exc:
                self.respond(page(f"Could not save the watch list: {exc}")); return
        elif path == "/refresh-all":
            if AUTOMATION_OBSERVER is not None:
                AUTOMATION_OBSERVER.tick()
            else:
                for patch in _patch_snapshot():
                    refresh_watched_patch(patch)
        elif path == "/auto-refresh":
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
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()
    def respond(self, body):
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body.encode())


if __name__ == "__main__":
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
        "--worker-profile",
        default=DEFAULT_WORKER_PROFILE_ID,
        help="checked-in worker profile ID for newly admitted runs",
    )
    parser.add_argument(
        "--daily-summary",
        action="store_true",
        help="refresh seeds, then send/dry-run the configured daily email",
    )
    args = parser.parse_args()
    ACTIVE_WATCH_FILE = args.seed_file
    initialize_session_store(args.session_database)
    initialize_automation_store(args.automation_database)
    initialize_worker_profile(args.worker_profile)
    RESOURCE_COLLECTION_ENABLED = True
    refresh_resource_status(force=True)
    load_seed_file(args.seed_file)
    if args.daily_summary:
        config = GerritConfig.load()
        result = send_daily_summary(
            PATCHES,
            config,
            automation_events=automation_daily_events(),
        )
        print(result.message)
        raise SystemExit(0 if result.sent or not config.email_enabled else 1)
    initialize_run_controller()
    initialize_retest_controller()
    print(f"Patch Watcher listening on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
