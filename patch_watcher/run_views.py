"""Accessible HTML views for Phase 0C read-only investigation runs.

Inputs may be mappings, dataclasses, or other attribute objects.  The module
only renders controls: the controller owns CSRF, optimistic concurrency,
idempotency, authorization, and confirmation-token validation.  GET links
only open detail/confirmation views; every mutation uses POST.
"""

import math
from collections.abc import Mapping
from html import escape
from urllib.parse import quote, urlencode


UNKNOWN = "unknown"
ACTIVE_STATES = {
    "queued", "preparing", "running", "waiting_external", "waiting_human",
    "paused", "blocked", "recovering",
}
TERMINAL_STATES = {
    "succeeded", "failed", "cancelled", "stale", "resource_exhausted",
}
DESTRUCTIVE_ACTIONS = {"cancel", "kill"}


def _project(record):
    if record is None or isinstance(record, Mapping):
        return record
    method = getattr(record, "to_dict", None)
    if callable(method):
        projected = method()
        if isinstance(projected, Mapping):
            return projected
    return record


def _get(record, *names, default=None):
    record = _project(record)
    if record is None:
        return default
    for name in names:
        if isinstance(record, Mapping) and name in record:
            return record[name]
        try:
            return getattr(record, name)
        except (AttributeError, TypeError):
            pass
    return default


def _items(value):
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _plain(value, default=UNKNOWN):
    if value is None or value == "":
        return default
    return str(value)


def _human(value):
    text = _plain(value)
    if text == UNKNOWN:
        return "Unknown"
    return text.replace("_", " ").replace("-", " ").capitalize()


def _state(value):
    return _plain(value).casefold().replace("-", "_").replace(" ", "_")


def _format_bytes(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    if value < 0 or not math.isfinite(value):
        return UNKNOWN
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.1f}".rstrip("0").rstrip(".") + f" {unit}"


def _format_duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    if value < 0 or not math.isfinite(value):
        return UNKNOWN
    seconds = int(value)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _field(label, value, *, code=False):
    content = escape(_plain(value))
    if code:
        content = f"<code>{content}</code>"
    return f"<div><dt>{escape(label)}</dt><dd>{content}</dd></div>"


def _hidden(name, value):
    if value is None:
        return ""
    return (
        f"<input type='hidden' name='{escape(str(name), quote=True)}' "
        f"value='{escape(str(value), quote=True)}'>"
    )


def _run_path(run, base_url="/runs"):
    run_id = quote(_plain(_get(run, "run_id", "id")), safe="")
    return f"{base_url.rstrip('/')}/{run_id}"


def _revision(run):
    return _get(run, "revision_sha", "revision", "commit_sha", "pinned_revision")


def _version(run):
    return _get(run, "version", "run_version", default=0)


def _status_badge(state):
    normalized = _state(state)
    if normalized in {"running", "succeeded"}:
        tone = "good"
    elif normalized in {"failed", "blocked", "resource_exhausted", "cancelled"}:
        tone = "bad"
    elif normalized in {"waiting_human", "waiting_external", "paused", "stale"}:
        tone = "warn"
    else:
        tone = "neutral"
    return f"<span class='run-state tone-{tone}'>Run: {escape(_human(state))}</span>"


def render_investigate_control(
    patch, *, action="/runs/investigate", csrf_token=None,
    idempotency_token=None,
):
    """Render the per-patch manual read-only Investigate control."""
    status = _state(_get(patch, "lifecycle", "status", "gerrit_status"))
    eligible = bool(_get(patch, "investigation_eligible", "eligible", default=False))
    active_run = _get(patch, "active_run_id", "active_run")
    revision = _get(patch, "revision_sha", "revision", "current_revision")
    patchset = _get(patch, "patchset", "patch_set", "patchset_number")
    change = _get(patch, "change_number", "change", "id")
    reason = _get(patch, "investigation_disabled_reason", "disabled_reason")
    if status not in {"open", "new"}:
        eligible = False
        reason = reason or f"Change is {_human(status).casefold()}."
    elif active_run:
        eligible = False
        reason = reason or "This patch already has an active run."
    elif not revision:
        eligible = False
        reason = reason or "The exact current revision is unavailable."
    fields = (
        _hidden("change_number", change) + _hidden("patchset", patchset)
        + _hidden("revision_sha", revision) + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
    )
    disabled = "" if eligible else " disabled aria-disabled='true'"
    reason_html = (
        f"<p class='control-reason' role='status'>{escape(_plain(reason))}</p>"
        if not eligible else ""
    )
    return (
        "<section class='investigate-control' aria-label='Manual read-only investigation'>"
        "<h3>Manual investigation</h3>"
        "<p>Starts one manually requested Claude investigation pinned to this "
        "exact revision. The worker may read only the pinned local source and "
        "controller-provided evidence; it cannot write to Gerrit or CI.</p>"
        "<p class='safety-note'><strong>Read-only:</strong> no Gerrit or CI "
        "write capability is granted.</p>"
        f"<p>Patchset {escape(_plain(patchset))} · pinned revision "
        f"<code>{escape(_plain(revision))}</code></p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        f"{fields}<button type='submit'{disabled}>Investigate</button></form>"
        f"{reason_html}</section>"
    )


def _countdowns(run):
    definitions = []
    for label, names in (
        ("Runtime remaining", ("runtime_remaining_seconds", "runtime_seconds_remaining")),
        ("Inactivity remaining", ("inactivity_remaining_seconds", "inactivity_seconds_remaining")),
        ("Absolute-cap remaining", ("absolute_remaining_seconds", "absolute_seconds_remaining")),
    ):
        definitions.append(_field(label, _format_duration(_get(run, *names))))
    return "<dl class='run-countdowns'>" + "".join(definitions) + "</dl>"


def render_run_summary(run, *, base_url="/runs"):
    """Render a bounded overview of an active or historical run."""
    path = _run_path(run, base_url)
    title_id = "agent-run-" + quote(_plain(_get(run, "run_id", "id")), safe="")
    message = _get(run, "latest_message", "recent_message", "message_summary")
    message_text = _get(message, "body", "text", "summary", default=message)
    pid = _get(run, "process_pid", "pid", "claude_pid")
    memory = _get(run, "process_memory_bytes", "memory_bytes", "rss_bytes")
    return (
        f"<article class='run-summary' aria-labelledby='{escape(title_id, quote=True)}'>"
        f"<header><h3 id='{escape(title_id, quote=True)}'>Agent run</h3>"
        f"{_status_badge(_get(run, 'state', 'status'))}</header>"
        "<dl class='run-metrics'>"
        + _field("Run", _get(run, "run_id", "id"), code=True)
        + _field("Patch", _get(run, "subject", "patch_subject", "change_number"))
        + _field("Current step", _get(run, "current_step", "step"))
        + _field("Process", f"PID {_plain(pid)} · {_format_bytes(memory)}")
        + _field("Last qualifying activity", _get(run, "last_activity_at", "last_qualifying_activity"))
        + "</dl>" + _countdowns(run)
        + "<p class='pinned-revision'><strong>Exact pinned revision:</strong> "
        f"<code>{escape(_plain(_revision(run)))}</code></p>"
        + "<p class='latest-message'><strong>Latest message:</strong> "
        f"{escape(_plain(message_text))}</p>"
        + f"<p><a href='{escape(path, quote=True)}'>Open run details</a></p></article>"
    )


def _boundary_label(kind, value):
    normalized = _state(value)
    if kind == "isolation" and ("unsandbox" in normalized or normalized == "host"):
        return "Unsandboxed host worker", "bad"
    if kind == "network" and normalized in {
        "general", "general_network", "network_general", "host_ambient",
        "host", "full", "unrestricted",
    }:
        return "General network access", "warn"
    return _human(value), "neutral"


def _render_admission(run, admission):
    evidence = admission if admission is not None else _get(run, "admission", "worker_admission")
    isolation = _get(evidence, "isolation_profile", "isolation")
    network = _get(evidence, "network_profile", "network")
    isolation_label, isolation_tone = _boundary_label("isolation", isolation)
    network_label, network_tone = _boundary_label("network", network)
    return (
        "<section class='run-admission' aria-labelledby='run-admission-title'>"
        "<h3 id='run-admission-title'>Worker admission evidence</h3>"
        f"<p><span class='worker-boundary tone-{isolation_tone}'>Isolation: "
        f"{escape(isolation_label)}</span> <span class='worker-boundary "
        f"tone-{network_tone}'>Network: {escape(network_label)}</span></p><dl>"
        + _field("Admission", _human(_get(evidence, "status", "admission_status")))
        + _field("Worker profile", _get(evidence, "profile_id", "worker_profile"), code=True)
        + _field("Profile hash", _get(evidence, "profile_hash"), code=True)
        + _field("Environment", _get(evidence, "environment_instance_id", "environment_id"), code=True)
        + _field("Instruction hash", _get(evidence, "instruction_hash"), code=True)
        + "</dl></section>"
    )


def _render_question(run, question):
    question = question if question is not None else _get(run, "question", "waiting_question")
    if question is None:
        return ""
    choices = _items(_get(question, "choices", "suggested_choices"))
    choices_html = ""
    if choices:
        choices_html = "<h4>Suggested choices</h4><ul>" + "".join(
            f"<li>{escape(_plain(_get(choice, 'label', 'text', default=choice)))}</li>"
            for choice in choices
        ) + "</ul>"
    return (
        "<section class='waiting-question' role='alert' aria-labelledby='waiting-question-title'>"
        "<h3 id='waiting-question-title'>Waiting for your decision</h3>"
        f"<p><strong>Question:</strong> {escape(_plain(_get(question, 'question', 'text', 'body')))}</p>"
        f"<p><strong>Why:</strong> {escape(_plain(_get(question, 'why', 'reason')))}</p>"
        f"<p><strong>Already tried:</strong> {escape(_plain(_get(question, 'tried', 'already_tried')))}</p>"
        f"<p><strong>Recommended safe default:</strong> {escape(_plain(_get(question, 'recommended', 'recommended_default')))}</p>"
        f"{choices_html}</section>"
    )


def _message_row(message):
    question = _get(message, "question_id", "target_question_id")
    question_html = f" · question <code>{escape(_plain(question))}</code>" if question else ""
    return (
        "<li class='run-message'><header><strong>"
        f"{escape(_human(_get(message, 'author', 'role', 'sender')))}</strong> · "
        f"<time>{escape(_plain(_get(message, 'created_at', 'timestamp', 'time')))}</time>"
        f"</header><p>{escape(_plain(_get(message, 'body', 'text', 'message')))}</p>"
        f"<footer>Delivery: <strong>{escape(_human(_get(message, 'delivery_state', 'state', 'status')))}</strong>"
        f"{question_html}</footer></li>"
    )


def _event_row(event):
    return (
        "<li class='run-event'>"
        f"<time>{escape(_plain(_get(event, 'created_at', 'timestamp', 'time')))}</time> "
        f"<strong>{escape(_human(_get(event, 'event_type', 'type', 'kind')))}</strong>: "
        f"{escape(_plain(_get(event, 'summary', 'message', 'detail')))}</li>"
    )


def _render_history(messages, events):
    message_items, event_items = _items(messages), _items(events)
    message_html = (
        "<ol class='run-conversation'>" + "".join(_message_row(i) for i in message_items) + "</ol>"
        if message_items else "<p>No messages recorded.</p>"
    )
    event_html = (
        "<ol class='run-timeline'>" + "".join(_event_row(i) for i in event_items) + "</ol>"
        if event_items else "<p>No events recorded.</p>"
    )
    return (
        "<section aria-labelledby='conversation-title'><h3 id='conversation-title'>"
        f"Conversation ({len(message_items)})</h3>{message_html}</section>"
        "<section aria-labelledby='timeline-title'><h3 id='timeline-title'>"
        f"Timeline ({len(event_items)})</h3>{event_html}</section>"
    )


def _simple_post_form(path, label, intent, run, csrf_token, idempotency_token=None):
    return (
        f"<form method='post' action='{escape(path, quote=True)}' class='inline-control'>"
        + _hidden("intent", intent) + _hidden("expected_version", _version(run))
        + _hidden("csrf_token", csrf_token) + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit'>{escape(label)}</button></form>"
    )


def _render_controls(run, *, base_url, csrf_token, idempotency_token):
    state = _state(_get(run, "state", "status"))
    path = _run_path(run, base_url)
    forms = []
    if state in {"queued", "preparing", "running", "waiting_external", "blocked"}:
        forms.append(_simple_post_form(f"{path}/pause", "Pause after current step", "pause", run, csrf_token, idempotency_token))
    if state in {"preparing", "running"}:
        forms.append(_simple_post_form(f"{path}/interrupt", "Interrupt turn", "interrupt", run, csrf_token, idempotency_token))
    # Waiting-human resumes only through a question-bound answer submitted by
    # the guidance composer; a generic resume would bypass that contract.
    if state in {"paused", "waiting_external", "blocked"}:
        forms.append(_simple_post_form(f"{path}/resume", "Resume", "resume", run, csrf_token, idempotency_token))
    if state in ACTIVE_STATES:
        cancel_query = urlencode({"intent": "cancel", "version": _version(run)})
        kill_query = urlencode({"intent": "kill", "version": _version(run)})
        forms.append(f"<a class='danger-link' href='{escape(path + '/confirm?' + cancel_query, quote=True)}'>Review stop and cancel…</a>")
        forms.append(f"<a class='danger-link' href='{escape(path + '/confirm?' + kill_query, quote=True)}'>Review kill session…</a>")
    if state in TERMINAL_STATES:
        forms.append(_simple_post_form(f"{path}/follow-up", "Start follow-up run", "follow_up", run, csrf_token, idempotency_token))
    return (
        "<section class='run-controls' aria-labelledby='run-controls-title'>"
        "<h3 id='run-controls-title'>Run controls</h3>"
        f"{''.join(forms) if forms else '<p>No controls are available for this state.</p>'}</section>"
    )


def _render_guidance(run, *, base_url, csrf_token, idempotency_token):
    state = _state(_get(run, "state", "status"))
    path = _run_path(run, base_url)
    question = _get(run, "question", "waiting_question")
    question_id = _get(question, "question_id", "id")
    if state in TERMINAL_STATES:
        help_text = "This run is terminal. Guidance can only start a new follow-up run."
        action = f"{path}/follow-up"
        modes = "<button type='submit' name='delivery_mode' value='follow_up'>Start follow-up run</button>"
    elif state == "paused":
        help_text = "The message is stored unless you explicitly resume with it."
        action = f"{path}/guidance"
        modes = ("<button type='submit' name='delivery_mode' value='queue'>Queue guidance</button>"
                 "<button type='submit' name='delivery_mode' value='resume_with_message'>Resume with this message</button>")
    elif state == "waiting_human":
        help_text = "Your answer targets the displayed question and resumes only after revision and policy checks."
        action = f"{path}/guidance"
        modes = "<button type='submit' name='delivery_mode' value='answer'>Answer and resume</button>"
    else:
        help_text = "Guidance queues for the next safe turn boundary unless you explicitly interrupt."
        action = f"{path}/guidance"
        modes = ("<button type='submit' name='delivery_mode' value='safe_boundary'>Send guidance</button>"
                 "<button type='submit' name='delivery_mode' value='interrupt_and_send'>Interrupt and send</button>")
    return (
        "<section class='guidance-composer' aria-labelledby='guidance-title'>"
        "<h3 id='guidance-title'>Send guidance</h3>"
        f"<p id='guidance-help'>{escape(help_text)}</p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        "<label for='guidance-message'>Message to the agent</label>"
        "<textarea id='guidance-message' name='message' rows='5' required aria-describedby='guidance-help'></textarea>"
        + _hidden("expected_version", _version(run)) + _hidden("question_id", question_id)
        + _hidden("csrf_token", csrf_token) + _hidden("idempotency_token", idempotency_token)
        + f"<div class='guidance-actions'>{modes}</div></form></section>"
    )


def render_run_detail(
    run, *, messages=(), events=(), admission=None, question=None,
    csrf_token=None, idempotency_token=None, base_url="/runs",
):
    """Render complete Phase 0C run detail without unsafe mutations."""
    return (
        "<main class='run-detail'><header><p><a href='/'>← Patch Watcher</a></p>"
        f"<h2>Read-only investigation · {escape(_plain(_get(run, 'subject', 'patch_subject', 'change_number')))}</h2>"
        f"{_status_badge(_get(run, 'state', 'status'))}"
        "<p class='safety-note'><strong>Read-only run:</strong> no Gerrit or CI "
        "write capability is available.</p></header>"
        "<section aria-labelledby='run-identity-title'><h3 id='run-identity-title'>Run identity</h3><dl>"
        + _field("Run", _get(run, "run_id", "id"), code=True)
        + _field("Change", _get(run, "change_number", "change"))
        + _field("Patchset", _get(run, "patchset", "patch_set"))
        + _field("Exact pinned revision", _revision(run), code=True)
        + _field("Current step", _get(run, "current_step", "step"))
        + _field("Execution profile", _get(run, "execution_profile", "profile"))
        + _field("Model", _get(run, "model", "model_name"))
        + _field("Process", f"PID {_plain(_get(run, 'process_pid', 'pid'))} · {_format_bytes(_get(run, 'process_memory_bytes', 'memory_bytes', 'rss_bytes'))}")
        + _field("Started", _get(run, "started_at", "created_at"))
        + _field("Last qualifying activity", _get(run, "last_activity_at", "last_qualifying_activity"))
        + "</dl>" + _countdowns(run) + "</section>"
        + _render_admission(run, admission) + _render_question(run, question)
        + _render_history(messages or _get(run, "messages"), events or _get(run, "events"))
        + _render_guidance(run, base_url=base_url, csrf_token=csrf_token, idempotency_token=idempotency_token)
        + _render_controls(run, base_url=base_url, csrf_token=csrf_token, idempotency_token=idempotency_token)
        + "</main>"
    )


def render_destructive_confirmation(
    run, intent, *, confirmation_token, csrf_token=None,
    idempotency_token=None, base_url="/runs",
):
    """Render a run-bound, token-protected second confirmation step."""
    normalized = _state(intent)
    if normalized not in DESTRUCTIVE_ACTIONS:
        raise ValueError("intent must be cancel or kill")
    if not confirmation_token:
        raise ValueError("a confirmation token is required")
    path = _run_path(run, base_url)
    if normalized == "kill":
        title, button = "Confirm kill session", "Kill session"
        warning = ("This forcibly stops the Claude process, cancels the run, collects "
                   "available evidence, and begins cleanup of resources owned by this run.")
    else:
        title, button = "Confirm stop and cancel", "Stop and cancel"
        warning = ("This requests an orderly stop, marks the run cancelled, collects "
                   "available evidence, and begins cleanup of resources owned by this run.")
    return (
        "<main class='destructive-confirmation'>"
        f"<h2>{escape(title)}</h2><p role='alert'>{escape(warning)}</p>"
        f"<p>Run <code>{escape(_plain(_get(run, 'run_id', 'id')))}</code> · exact "
        f"pinned revision <code>{escape(_plain(_revision(run)))}</code></p>"
        f"<form method='post' action='{escape(path + '/' + normalized, quote=True)}'>"
        + _hidden("intent", normalized) + _hidden("expected_version", _version(run))
        + _hidden("confirmation_token", confirmation_token) + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit' class='danger'>{escape(button)}</button> "
        f"<a href='{escape(path, quote=True)}'>Keep the session running</a></form></main>"
    )


render_patch_investigation_view = render_investigate_control
render_active_run_view = render_run_summary
render_run_detail_view = render_run_detail
render_run_confirmation_view = render_destructive_confirmation
