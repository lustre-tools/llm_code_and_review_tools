#!/usr/bin/env python3
"""Small, dependency-free Patch Watcher web application."""
import argparse
import hmac
import platform
import secrets
import subprocess
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from gerrit_status import (
    GerritConfig,
    GerritConfigError,
    parse_change_number,
    refresh_patch,
)
from reporting import send_daily_summary
from reporting import send_session_alert
from resource_status import collect_process_tree_rss, collect_resource_snapshot
from resource_views import render_resource_dashboard
from session_state import (
    ABSOLUTE_RUNTIME_CAP,
    ENGINEERING_INACTIVITY_LIMIT,
    TRIAGE_WALL_LIMIT,
    InvalidSessionOperation,
    SessionNotFound,
    SessionStateStore,
)
from worker_admission_views import render_worker_admission
from worker_contract import load_profile
from run_controller import RunController, RunControllerError
from run_views import (
    render_destructive_confirmation,
    render_investigate_control,
    render_run_detail,
    render_run_summary,
)

PATCHES = []
DEFAULT_SEED_FILE = Path.home() / ".config" / "patch-watcher" / "patches.txt"
DEFAULT_SESSION_DATABASE = (
    Path.home() / ".local" / "state" / "patch-watcher" / "sessions.sqlite3"
)
DEFAULT_WORKER_PROFILE_ID = "host-unsandboxed-mac-v1"
ACTIVE_WATCH_FILE = DEFAULT_SEED_FILE
ACTIVE_SESSION_DATABASE = DEFAULT_SESSION_DATABASE
JIRA_BASE_URL = "https://jira.whamcloud.com/browse"
SESSION_STORE = None
WORKER_PROFILE = None
RUN_CONTROLLER = None
CSRF_TOKEN = secrets.token_urlsafe(32)
RESOURCE_COLLECTION_ENABLED = False
RESOURCE_CACHE_SECONDS = 15
_RESOURCE_SNAPSHOT = None
_RESOURCE_SNAPSHOT_MONOTONIC = 0.0


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


def initialize_run_controller(*, runs_directory=None, start=True):
    """Create the background dispatcher after state/profile initialization."""
    global RUN_CONTROLLER
    if SESSION_STORE is None:
        initialize_session_store()
    if WORKER_PROFILE is None:
        initialize_worker_profile()

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

    options = {"alert_sender": alert}
    if runs_directory is not None:
        options["runs_directory"] = Path(runs_directory)
    RUN_CONTROLLER = RunController(SESSION_STORE, WORKER_PROFILE, **options)
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
    return send_daily_summary(
        PATCHES,
        config or GerritConfig.load(),
        runner=runner,
    )


def refresh_watched_patch(patch):
    """Refresh once and stale any run no longer pinned to the current revision."""
    result = refresh_patch(patch)
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
        "</div></td></tr>"
    )


def page(message="", jira_base=JIRA_BASE_URL):
    refresh_interval = configured_refresh_interval()
    resources = resource_dashboard_html()
    worker_admission = worker_admission_html()
    rows = "".join(
        _patch_row(patch, jira_base) for patch in PATCHES
    ) or "<tr><td colspan='5' class='empty'>No patches yet. Add a Gerrit change to start watching.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta http-equiv='refresh' content='{refresh_interval};url=/auto-refresh'>
<title>Patch Watcher</title><style>
body{{margin:0;background:#f5f7fb;color:#172033;font:15px system-ui,sans-serif}}main{{max-width:1450px;margin:48px auto;padding:0 24px}}h1{{margin-bottom:6px}}.sub{{color:#667085;margin-top:0}}.card,.resource-card{{background:white;border:1px solid #e4e7ec;border-radius:14px;padding:22px;margin-top:28px;box-shadow:0 4px 16px #1018280a;overflow-x:auto}}form.add{{display:flex;gap:10px;flex-wrap:wrap}}input,textarea{{border:1px solid #d0d5dd;border-radius:8px;padding:11px 12px;font-size:14px;flex:1;min-width:240px}}textarea{{display:block;width:min(620px,95%);min-height:70px;margin:7px 0 10px}}button{{border:0;border-radius:8px;padding:11px 16px;background:#315efb;color:white;font-weight:600;cursor:pointer}}button:disabled{{cursor:not-allowed;opacity:.68}}button.danger,button.secondary{{background:#fff;padding:7px 11px}}button.danger{{color:#b42318;border:1px solid #fecdca}}button.secondary{{color:#344054;border:1px solid #d0d5dd}}table{{width:100%;border-collapse:collapse;margin-top:18px;min-width:1050px}}th,td{{text-align:left;padding:14px 10px;border-top:1px solid #eaecf0;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:#667085}}.url,.detail{{color:#667085;font-size:12px;margin-top:4px;word-break:break-word}}.patch-meta{{display:flex;align-items:center;gap:6px;color:#667085;font-size:12px;margin-top:7px}}.ticket{{display:inline-block;margin-left:8px;font-size:12px}}.actions{{display:flex;gap:6px;flex-wrap:wrap}}.actions form{{margin:0}}.error{{color:#b42318;font-size:12px;margin-top:5px;max-width:340px}}.empty{{text-align:center;color:#667085;padding:35px}}.notice{{background:#fffaeb;color:#b54708;padding:10px 12px;border-radius:8px;margin-top:16px}}.section-title{{display:flex;justify-content:space-between;align-items:center;gap:16px}}small{{display:block;color:#667085;margin-top:4px}}details{{margin-top:7px;font-size:12px;color:#475467}}details ol{{padding-left:18px;max-height:140px;overflow:auto}}details li{{margin:5px 0}}details time{{font-variant-numeric:tabular-nums}}.history-state{{color:#667085}}.status-chip,.resource-status,.admission-status,.worker-boundary{{display:inline-block;border:1px solid transparent;border-radius:999px;padding:3px 8px;font-size:12px;font-weight:700;line-height:1.35;white-space:nowrap}}.tone-good{{background:#dcfce7;border-color:#86efac;color:#166534}}.tone-bad{{background:#fee2e2;border-color:#fca5a5;color:#991b1b}}.tone-warn{{background:#fef3c7;border-color:#fcd34d;color:#78350f}}.tone-info{{background:#dbeafe;border-color:#93c5fd;color:#1e3a8a}}.tone-neutral{{background:#f2f4f7;border-color:#d0d5dd;color:#344054}}.status-link{{text-decoration:none}}.status-link:focus-visible .status-chip{{outline:3px solid #315efb;outline-offset:2px}}.ci-stack{{display:flex;align-items:flex-start;gap:5px;flex-wrap:wrap;margin-top:8px}}.stub-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}.stub-option{{display:flex;align-items:flex-start;gap:10px;text-align:left;background:#f8fafc;color:#344054;border:1px solid #d0d5dd;padding:14px}}.stub-label{{display:block;color:#667085;font-size:12px;font-weight:500;margin-top:4px}}.stub-tag{{display:inline-block;margin-left:6px;border:1px solid #d0d5dd;border-radius:999px;padding:1px 6px;font-size:10px;text-transform:uppercase}}.resource-toolbar{{display:flex;justify-content:flex-end;margin-top:20px}}.resource-dashboard{{display:grid;gap:18px}}.resource-card{{margin-top:0}}.resource-metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.resource-metric{{background:#f8fafc;border:1px solid #eaecf0;border-radius:10px;padding:12px}}.resource-metric dt{{font-size:12px;color:#667085}}.resource-metric dd{{margin:5px 0 0;font-size:18px;font-weight:700}}.resource-errors{{color:#b42318}}.resource-ok{{color:#027a48}}.session-controls{{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-top:16px}}fieldset{{border:1px solid #fecdca;border-radius:8px}}.message-content{{white-space:pre-wrap;margin-top:3px}}.worker-admission{{margin-top:18px}}.worker-admission-heading{{display:flex;justify-content:space-between;gap:12px;align-items:center}}.worker-boundaries{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}}.worker-provenance{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}.worker-provenance div{{background:#f8fafc;border:1px solid #eaecf0;border-radius:8px;padding:10px}}.worker-provenance dt{{font-size:12px;color:#667085}}.worker-provenance dd{{margin:4px 0 0;word-break:break-word}}.admission-failures{{color:#b42318}}@media(max-width:760px){{.session-controls{{grid-template-columns:1fr}}}}</style></head>
<body><main><h1>Patch Watcher</h1><p class='sub'>Track Gerrit patches, managed sessions, and worker resources.</p>
<div class='resource-toolbar'><form method='post' action='/resources/refresh'><button class='secondary'>Refresh resource status</button></form></div>{resources}{worker_admission}
{active_runs_html()}<section class='card'><h2>Add a patch</h2><form class='add' method='post' action='/add'><input name='url' required placeholder='https://review.whamcloud.com/c/...'><button>Add patch</button></form>{f"<div class='notice'>{escape(message)}</div>" if message else ''}</section>
<section class='card'><div class='section-title'><div><h2>Watched patches <small>({len(PATCHES)} · checks every {refresh_interval}s)</small></h2><div class='detail'>Overall last checked: {escape(overall_last_checked())}</div></div><div class='actions'><form method='post' action='/refresh-all'><button class='secondary'>Refresh all</button></form><form method='post' action='/email'><button class='secondary'>Send status email</button></form></div></div><table><thead><tr><th>Patch</th><th>Watch state / CI</th><th>Review</th><th>Latest change</th><th></th></tr></thead><tbody>{rows}</tbody></table></section>
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
            refresh_patch(patch)
            loaded.append(patch)
    return loaded


def save_watch_file(path=DEFAULT_SEED_FILE):
    """Atomically persist the current watch list as private URL-only config."""
    watch_path = Path(path)
    watch_path.parent.mkdir(parents=True, exist_ok=True)
    pending = watch_path.with_name(f".{watch_path.name}.tmp")
    contents = "".join(f"{patch['url']}\n" for patch in PATCHES)
    pending.write_text(contents, encoding="utf-8")
    pending.chmod(0o600)
    pending.replace(watch_path)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/auto-refresh":
            for patch in PATCHES:
                refresh_watched_patch(patch)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        parts = [item for item in path.split("/") if item]
        if len(parts) >= 2 and parts[0] == "runs":
            try:
                session = _find_session_by_run_id(parts[1])
            except SessionNotFound:
                self.send_error(404)
                return
            if len(parts) == 2:
                self.respond(_standalone_document("Patch Watcher run", run_detail_html(session)))
                return
            if len(parts) == 3 and parts[2] == "confirm":
                query = parse_qs(parsed.query)
                intent = query.get("intent", [""])[0]
                if intent not in {"cancel", "kill"}:
                    self.send_error(400, "Unknown destructive intent")
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
        length = int(self.headers.get("Content-Length", 0)); data = parse_qs(self.rfile.read(length).decode()); path = urlparse(self.path).path
        parts = [item for item in path.split("/") if item]
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
            except (InvalidSessionOperation, SessionNotFound, ValueError) as exc:
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
            try:
                save_watch_file(ACTIVE_WATCH_FILE)
            except OSError as exc:
                self.respond(page(f"Could not save the watch list: {exc}")); return
        elif path == "/remove":
            PATCHES[:] = [p for p in PATCHES if p["url"] != data.get("url", [""])[0]]
            try:
                save_watch_file(ACTIVE_WATCH_FILE)
            except OSError as exc:
                self.respond(page(f"Could not save the watch list: {exc}")); return
        elif path == "/refresh-all":
            for patch in PATCHES:
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
    initialize_worker_profile(args.worker_profile)
    RESOURCE_COLLECTION_ENABLED = True
    refresh_resource_status(force=True)
    load_seed_file(args.seed_file)
    if args.daily_summary:
        config = GerritConfig.load()
        result = send_daily_summary(PATCHES, config)
        print(result.message)
        raise SystemExit(0 if result.sent or not config.email_enabled else 1)
    initialize_run_controller()
    print(f"Patch Watcher listening on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
