#!/usr/bin/env python3
"""Small, dependency-free Patch Watcher web application."""
import argparse
import platform
import subprocess
import time
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
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
from resource_status import collect_process_tree_rss, collect_resource_snapshot
from resource_views import render_resource_dashboard
from session_state import (
    InvalidSessionOperation,
    SessionNotFound,
    SessionStateStore,
)
from worker_admission_views import render_worker_admission
from worker_contract import load_profile

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
    )


def send_status_email(config=None, *, runner=subprocess.run):
    """Send (or dry-run) the current bounded status summary."""
    return send_daily_summary(
        PATCHES,
        config or GerritConfig.load(),
        runner=runner,
    )


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
<section class='card'><h2>Add a patch</h2><form class='add' method='post' action='/add'><input name='url' required placeholder='https://review.whamcloud.com/c/...'><button>Add patch</button></form>{f"<div class='notice'>{escape(message)}</div>" if message else ''}</section>
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
        path = urlparse(self.path).path
        if path == "/auto-refresh":
            for patch in PATCHES:
                refresh_patch(patch)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path != "/":
            self.send_error(404)
            return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(page().encode())
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0)); data = parse_qs(self.rfile.read(length).decode()); path = urlparse(self.path).path
        if path == "/resources/refresh":
            refresh_resource_status(force=True)
        elif path == "/sessions/guidance":
            session_id = data.get("session_id", [""])[0]
            guidance = data.get("guidance", [""])[0].strip()
            if SESSION_STORE is None:
                self.respond(page("Managed-session storage is not initialized.")); return
            if not guidance:
                self.respond(page("Guidance must not be empty.")); return
            try:
                SESSION_STORE.record_message(session_id, "operator", guidance)
            except (SessionNotFound, InvalidSessionOperation, ValueError) as exc:
                self.respond(page(str(exc))); return
            self.respond(page(
                "Guidance was recorded. Runner delivery will be enabled with the managed Claude runner."
            )); return
        elif path == "/sessions/kill":
            session_id = data.get("session_id", [""])[0]
            if data.get("confirm", [""])[0] != "yes":
                self.respond(page("Kill confirmation was not supplied.")); return
            if SESSION_STORE is None:
                self.respond(page("Managed-session storage is not initialized.")); return
            try:
                intent = SESSION_STORE.request_kill(session_id, "operator")
                SESSION_STORE.confirm_kill(session_id, intent.request_id, "operator")
            except (SessionNotFound, InvalidSessionOperation, ValueError) as exc:
                self.respond(page(str(exc))); return
            self.respond(page(
                "Kill intent was confirmed and recorded. Process signalling will be enabled with the managed Claude runner."
            )); return
        elif path == "/add":
            url = data.get("url", [""])[0]
            patch, error = add_patch(url)
            if error: self.respond(page(error)); return
            refresh_patch(patch)
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
                refresh_patch(patch)
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
    print(f"Patch Watcher listening on http://127.0.0.1:{args.port}")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
