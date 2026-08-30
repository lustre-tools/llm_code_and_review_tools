"""Accessible, dependency-free HTML views for worker and session resources.

The rendering boundary deliberately accepts dictionaries, other mappings, or
objects with attributes (including dataclasses).  It does not import the
resource collector or persistence layer, so those implementations can evolve
without coupling the dashboard to their concrete record types.
"""

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from html import escape


UNKNOWN = "unknown"
DEFAULT_MESSAGE_LIMIT = 10
MAX_MESSAGE_LIMIT = 100


def _get(record, *names, default=None):
    """Return the first present mapping key or object attribute in *names*."""
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
    """Return display text without ever converting absence into zero."""
    if value is None or value == "":
        return default
    return str(value)


def _first(records, *names, default=None):
    """Return a non-None field from the first record that supplies it."""
    for record in records:
        value = _get(record, *names)
        if value is not None:
            return value
    return default


def _project(record):
    """Use an object's mapping projection when it explicitly provides one."""
    if isinstance(record, Mapping) or record is None:
        return record
    try:
        to_dict = getattr(record, "to_dict")
    except AttributeError:
        return record
    if not callable(to_dict):
        return record
    projected = to_dict()
    return projected if isinstance(projected, Mapping) else record


def _human_state(value):
    text = _plain(value)
    if text == UNKNOWN:
        return text
    return text.replace("_", " ").replace("-", " ").capitalize()


def format_bytes(value):
    """Format a non-negative byte value with IEC units, or return ``unknown``.

    Booleans, negative numbers, non-finite floats, numeric-looking strings, and
    other unmeasured values are intentionally not coerced into a measurement.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    if not math.isfinite(value) or value < 0:
        return UNKNOWN
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    rounded = f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{rounded} {unit}"


def _format_duration_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    if not math.isfinite(value) or value < 0:
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


def _parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _seconds_since(value, *, now=None):
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return None
    elapsed = (current - parsed).total_seconds()
    return elapsed if elapsed >= 0 else None


def _duration(record, text_names, seconds_names, started_names=()):
    explicit = _get(record, *text_names)
    if explicit is not None and explicit != "":
        return str(explicit)
    seconds = _get(record, *seconds_names)
    if seconds is None and started_names:
        seconds = _seconds_since(_get(record, *started_names))
    return _format_duration_seconds(seconds)


def _format_percent(value):
    if isinstance(value, bool):
        return UNKNOWN
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return f"{value:.1f}%".replace(".0%", "%")
    if isinstance(value, str) and value:
        return value
    return UNKNOWN


def _bytes_pair(used, total):
    used_text = format_bytes(used)
    total_text = format_bytes(total)
    if used_text == UNKNOWN and total_text == UNKNOWN:
        return UNKNOWN
    return f"{used_text} used / {total_text} total"


def _sample_freshness(record, *, age_names=("sample_age_seconds", "freshness_seconds")):
    age = _get(record, *age_names)
    stale = bool(_get(record, "stale", "sample_stale", default=False))
    quality = _plain(_get(record, "quality", "sample_quality"), "")
    if quality.casefold() in {"stale", "failed", "error"}:
        stale = True
    if age is None:
        age = _seconds_since(_get(record, "sampled_at", "sample_time", "measured_at"))
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        age_text = _format_duration_seconds(age)
        if age_text != UNKNOWN:
            return ("Stale sample" if stale else "Sample") + f" · {age_text} old"
    return "Stale sample · age unknown" if stale else "Sample age unknown"


def _status_badge(state):
    raw = _plain(state)
    normalized = raw.casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"running", "active", "succeeded", "complete", "completed"}:
        tone = "good"
    elif normalized in {
        "failed", "blocked", "resource_exhausted", "orphaned", "cleanup_failed",
    }:
        tone = "bad"
    elif normalized in {
        "waiting_human", "needs_attention", "stale", "cleanup_pending", "stopping",
    }:
        tone = "warn"
    else:
        tone = "neutral"
    return (
        f"<span class='resource-status tone-{tone}'>State: "
        f"{escape(_human_state(raw))}</span>"
    )


def _metric(label, value, *, detail=""):
    detail_html = f"<small>{escape(detail)}</small>" if detail else ""
    return (
        "<div class='resource-metric'>"
        f"<dt>{escape(label)}</dt><dd>{escape(value)}{detail_html}</dd></div>"
    )


def _error_text(error):
    if isinstance(error, Mapping) or not isinstance(error, (str, bytes)):
        message = _get(error, "message", "error", "detail")
        if message is not None:
            return _plain(message)
    if isinstance(error, bytes):
        return error.decode("utf-8", errors="replace")
    return _plain(error)


def render_host_memory_summary(host):
    """Render the authoritative host memory sample and its collection health."""
    host = _project(host)
    memory = _get(host, "host_memory") or host
    inventory = _get(host, "ltvm")
    name = _plain(_first((host, memory), "name", "host_name", "hostname"))
    sampled_at = _plain(
        _first((host, memory), "sampled_at", "sample_time", "measured_at")
    )
    quality = _plain(_first((host, memory), "quality", "sample_quality"))
    pressure = _plain(_first((memory, host), "pressure", "memory_pressure"))
    freshness_record = host if _get(host, "sampled_at") is not None else memory
    freshness = _sample_freshness(freshness_record)

    metrics = "".join((
        _metric("Total physical memory", format_bytes(_get(memory, "total_bytes", "memory_total_bytes"))),
        _metric("Used physical memory", format_bytes(_get(memory, "used_bytes", "memory_used_bytes"))),
        _metric("Available physical memory", format_bytes(_get(memory, "available_bytes", "memory_available_bytes"))),
        _metric(
            "Swap",
            _bytes_pair(
                _get(memory, "swap_used_bytes"),
                _get(memory, "swap_total_bytes"),
            ),
        ),
        _metric("Cache / reclaimable", format_bytes(_get(memory, "cache_bytes", "reclaimable_bytes"))),
        _metric(
            "Managed-session process-tree RSS",
            format_bytes(_first((host, memory), "session_process_rss_bytes", "claude_process_rss_bytes")),
            detail="Measured host use; not added to the host used-memory value.",
        ),
        _metric(
            "LTVM process RSS",
            format_bytes(
                _first(
                    (host, inventory, memory),
                    "vm_process_rss_bytes",
                    "ltvm_process_rss_bytes",
                    "measured_host_rss_bytes",
                )
            ),
            detail="Measured host use; not added to the host used-memory value.",
        ),
        _metric(
            "Configured LTVM guest memory",
            format_bytes(
                _first(
                    (host, inventory, memory),
                    "configured_guest_memory_bytes",
                    "guest_memory_bytes",
                )
            ),
            detail="Guest capacity only; not physical host usage.",
        ),
    ))

    errors = _items(_get(host, "errors", "collection_errors", default=[]))
    if not errors and memory is not host:
        errors.extend(_items(_get(memory, "errors", "collection_errors", default=[])))
        errors.extend(_items(_get(inventory, "errors", "collection_errors", default=[])))
    if not errors:
        single_error = _first((host, memory, inventory), "error", "collection_error")
        errors = [] if single_error is None or single_error == "" else [single_error]
    if errors:
        error_html = (
            "<div class='resource-errors' role='status'><strong>Collection errors</strong><ul>"
            + "".join(f"<li>{escape(_error_text(error))}</li>" for error in errors)
            + "</ul></div>"
        )
    else:
        error_html = "<p class='resource-ok'>Collection errors: none reported</p>"

    return (
        "<section class='host-memory resource-card' aria-labelledby='host-memory-title'>"
        "<h2 id='host-memory-title'>Worker host memory</h2>"
        f"<p><strong>Host:</strong> {escape(name)}</p>"
        "<p class='resource-sample'>"
        f"<strong>Sample time:</strong> {escape(sampled_at)} · "
        f"<strong>Freshness:</strong> {escape(freshness)} · "
        f"<strong>Pressure:</strong> {escape(pressure)} · "
        f"<strong>Quality:</strong> {escape(quality)}</p>"
        f"<dl class='resource-metrics'>{metrics}</dl>{error_html}</section>"
    )


def _record_label(record, names, nested_names=()):
    value = _get(record, *names)
    if value is None:
        return UNKNOWN
    if isinstance(value, Mapping) or not isinstance(value, (str, int, float, bool)):
        nested = _get(value, *nested_names) if nested_names else None
        return _plain(nested)
    return _plain(value)


def _message_content(message):
    if isinstance(message, bytes):
        return message.decode("utf-8", errors="replace")
    if isinstance(message, str):
        return message
    return _plain(_get(message, "content", "message", "text", "summary", "body"))


def _message_summary(session, messages):
    value = _get(session, "last_message", "last_message_summary", "recent_message")
    if value is not None and value != "":
        return _message_content(value)
    if messages:
        return _message_content(messages[-1])
    return UNKNOWN


def _render_messages(messages, limit):
    bounded = messages[-limit:] if limit else []
    omitted = len(messages) - len(bounded)
    if not bounded:
        return "<p class='empty'>No recent messages available.</p>"
    rows = []
    for message in bounded:
        role = _plain(_get(message, "role", "author", "kind", "type"), "Message")
        timestamp = _plain(_get(message, "created_at", "timestamp", "time"), "")
        time_html = f" <time>{escape(timestamp)}</time>" if timestamp else ""
        rows.append(
            "<li>"
            f"<strong>{escape(role)}</strong>{time_html}"
            f"<div class='message-content'>{escape(_message_content(message))}</div>"
            "</li>"
        )
    omitted_html = (
        f"<p class='bounded-note'>{omitted} older message(s) omitted.</p>"
        if omitted else ""
    )
    return omitted_html + "<ol class='session-messages'>" + "".join(rows) + "</ol>"


def _csrf_field(csrf_token):
    if csrf_token is None:
        return ""
    return (
        "<input type='hidden' name='csrf_token' "
        f"value='{escape(str(csrf_token), quote=True)}'>"
    )


def _render_controls(session_id, control_index, guidance_action, kill_action, csrf_token):
    if session_id == UNKNOWN:
        return (
            "<p class='controls-unavailable' role='status'>"
            "Session controls unavailable: session identifier is unknown.</p>"
        )
    escaped_id = escape(session_id, quote=True)
    guidance_id = f"session-guidance-{control_index}"
    confirm_id = f"session-kill-confirm-{control_index}"
    csrf = _csrf_field(csrf_token)
    return (
        "<div class='session-controls' aria-label='Session controls'>"
        f"<form method='post' action='{escape(guidance_action, quote=True)}'>"
        f"<input type='hidden' name='session_id' value='{escaped_id}'>{csrf}"
        f"<label for='{guidance_id}'>Send guidance to this session</label>"
        f"<textarea id='{guidance_id}' name='guidance' required></textarea>"
        "<button type='submit'>Send guidance</button></form>"
        f"<form class='kill-session' method='post' action='{escape(kill_action, quote=True)}'>"
        f"<input type='hidden' name='session_id' value='{escaped_id}'>{csrf}"
        "<fieldset><legend>Kill session</legend>"
        f"<label for='{confirm_id}'><input id='{confirm_id}' type='checkbox' "
        "name='confirm' value='yes' required> I confirm this session should be killed.</label>"
        "<button class='danger' type='submit'>Kill session</button>"
        "</fieldset></form></div>"
    )


def _vm_name(vm):
    return _plain(_get(vm, "name", "vm_name", "id", "vm_id"))


def _render_vm_table(vms, *, show_owner):
    owner_header = "<th scope='col'>Owner</th>" if show_owner else ""
    rows = []
    for vm in vms:
        topology = _plain(_get(vm, "topology", "cluster"))
        role = _plain(_get(vm, "role", "vm_role"))
        topology_role = (
            UNKNOWN if topology == UNKNOWN and role == UNKNOWN
            else f"{topology} / {role}"
        )
        age = _duration(vm, ("age",), ("age_seconds",))
        sample = _sample_freshness(
            vm,
            age_names=("sample_age_seconds", "memory_sample_age_seconds"),
        )
        owner_cell = ""
        if show_owner:
            owner_cell = f"<td>{escape(_plain(_get(vm, 'owner_id', 'owner')))}</td>"
        rows.append(
            "<tr>"
            f"<th scope='row'>{escape(_vm_name(vm))}</th>"
            f"<td>{escape(topology_role)}</td>"
            f"<td>{_status_badge(_get(vm, 'state', 'status'))}</td>"
            f"<td>{escape(age)}</td>"
            f"<td>{escape(format_bytes(_get(vm, 'configured_guest_memory_bytes', 'guest_memory_bytes', 'memory_bytes')))}</td>"
            f"<td>{escape(format_bytes(_get(vm, 'host_rss_bytes', 'process_rss_bytes', 'actual_host_rss_bytes')))}"
            f"<small>{escape(sample)}</small></td>"
            f"<td>{escape(_format_percent(_get(vm, 'cpu_percent', 'cpu_use_percent')))}</td>"
            f"<td>{escape(_human_state(_get(vm, 'cleanup_state')))}</td>"
            f"{owner_cell}</tr>"
        )
    colspan = 9 if show_owner else 8
    if not rows:
        rows.append(
            f"<tr><td class='empty' colspan='{colspan}'>No VMs in this group.</td></tr>"
        )
    return (
        "<table class='vm-table'><thead><tr>"
        "<th scope='col'>VM</th><th scope='col'>Topology / role</th>"
        "<th scope='col'>State</th><th scope='col'>Age</th>"
        "<th scope='col'>Configured guest memory</th>"
        "<th scope='col'>Actual host RSS</th><th scope='col'>CPU</th>"
        f"<th scope='col'>Cleanup</th>{owner_header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _session_id(session):
    return _plain(_get(session, "id", "session_id", "runner_session_id"))


def _owner_aliases(session):
    """Return exact durable owner values capable of naming this session."""
    aliases = []
    explicit = _get(session, "owner_id", "ltvm_owner_id")
    if explicit not in {None, ""}:
        aliases.append(str(explicit))
    identifier = _get(session, "id", "session_id", "runner_session_id")
    if identifier not in {None, ""}:
        identifier = str(identifier)
        aliases.extend((identifier, f"patch-watcher:{identifier}"))
    return set(aliases)


def _associate_vms(sessions, vms):
    """Associate a VM once, and only for one unambiguous exact owner match."""
    owner_indexes = {}
    for index, session in enumerate(sessions):
        for owner in _owner_aliases(session):
            owner_indexes.setdefault(owner, set()).add(index)
    owned = [[] for _ in sessions]
    other = []
    for vm in vms:
        owner = _get(vm, "owner_id", "owner")
        matches = owner_indexes.get(str(owner), set()) if owner not in {None, ""} else set()
        if len(matches) == 1:
            owned[next(iter(matches))].append(vm)
        else:
            other.append(vm)
    return owned, other


def render_active_sessions(
    sessions,
    vms=(),
    *,
    max_messages=DEFAULT_MESSAGE_LIMIT,
    guidance_action="/sessions/guidance",
    kill_action="/sessions/kill",
    csrf_token=None,
    messages_by_session=None,
):
    """Render active managed-session rows and return ``(html, other_vms)``.

    ``sessions`` should already be the caller's active-session selection.  VM
    ownership is resolved only from exact durable owner identifiers; name
    similarity is never used.  Ambiguous and unmatched VMs are returned in the
    second tuple item for the separate Other LTVM VMs group.
    """
    session_items = _items(sessions)
    vm_items = _items(vms)
    try:
        limit = int(max_messages)
    except (TypeError, ValueError):
        limit = DEFAULT_MESSAGE_LIMIT
    limit = max(0, min(limit, MAX_MESSAGE_LIMIT))
    owned, other = _associate_vms(session_items, vm_items)

    rows = []
    for index, session in enumerate(session_items):
        attached_messages = _get(session, "messages", "recent_messages")
        if attached_messages is None and messages_by_session is not None:
            session_key = _session_id(session)
            if isinstance(messages_by_session, Mapping):
                attached_messages = messages_by_session.get(session_key)
        messages = _items(attached_messages)
        patch = _record_label(
            session,
            ("patch", "patch_title", "patch_id", "change"),
            ("title", "subject", "change_id", "url"),
        )
        run = _record_label(session, ("run", "run_id"), ("id", "run_id", "name"))
        profile = _plain(_get(session, "profile", "run_profile"))
        elapsed = _duration(
            session,
            ("elapsed",),
            ("elapsed_seconds", "runtime_seconds"),
            ("started_at", "started"),
        )
        current_step = _plain(_get(session, "current_step", "step"))
        last_message = _message_summary(session, messages)
        process_memory = format_bytes(
            _get(
                session,
                "process_tree_rss_bytes",
                "process_tree_memory_bytes",
                "process_rss_bytes",
            )
        )
        memory_freshness = _sample_freshness(
            session,
            age_names=("memory_sample_age_seconds", "resource_sample_age_seconds"),
        )
        activity = _plain(
            _get(
                session,
                "last_qualifying_activity",
                "last_qualifying_activity_at",
                "last_activity_at",
            ),
        )
        session_id = _session_id(session)
        detail_id = f"session-detail-{index}"
        rows.append(
            "<tr class='session-row'>"
            f"<th scope='row'>{escape(patch)}</th><td>{escape(run)}</td>"
            f"<td>{escape(profile)}</td><td>{_status_badge(_get(session, 'state', 'status'))}</td>"
            f"<td>{escape(elapsed)}</td><td>{escape(current_step)}</td>"
            f"<td>{escape(last_message)}</td>"
            f"<td>{escape(process_memory)}<small>{escape(memory_freshness)}</small></td></tr>"
            "<tr class='session-detail-row'><td colspan='8'>"
            f"<details id='{detail_id}'><summary>Session details · "
            f"{len(owned[index])} owned VM(s) · recent messages</summary>"
            f"<p><strong>Session:</strong> {escape(session_id)} · "
            f"<strong>Last qualifying activity:</strong> {escape(activity)}</p>"
            "<section class='recent-messages' aria-label='Recent session messages'>"
            f"<h4>Recent messages (showing at most {limit})</h4>"
            f"{_render_messages(messages, limit)}</section>"
            "<section class='owned-vms' aria-label='Owned LTVM VMs'>"
            f"<h4>Owned LTVM VMs ({len(owned[index])})</h4>"
            f"{_render_vm_table(owned[index], show_owner=False)}</section>"
            f"{_render_controls(session_id, index, guidance_action, kill_action, csrf_token)}"
            "</details></td></tr>"
        )

    if not rows:
        rows.append(
            "<tr><td class='empty' colspan='8'>No active managed sessions.</td></tr>"
        )
    html = (
        "<section class='active-sessions resource-card' aria-labelledby='active-sessions-title'>"
        f"<h2 id='active-sessions-title'>Active managed sessions ({len(session_items)})</h2>"
        "<table class='session-table'><thead><tr>"
        "<th scope='col'>Patch</th><th scope='col'>Run</th>"
        "<th scope='col'>Profile</th><th scope='col'>State</th>"
        "<th scope='col'>Elapsed</th><th scope='col'>Current step</th>"
        "<th scope='col'>Last message</th><th scope='col'>Process-tree memory</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></section>"
    )
    return html, other


def render_other_vms(vms):
    """Render inventoried LTVM VMs that are not owned by a shown session."""
    vm_items = _items(vms)
    return (
        "<section class='other-vms resource-card' aria-labelledby='other-vms-title'>"
        f"<h2 id='other-vms-title'>Other LTVM VMs ({len(vm_items)})</h2>"
        "<p>These VMs have no unambiguous owner match among the managed sessions "
        "shown above. They are observable only and are not adopted for automatic cleanup.</p>"
        f"{_render_vm_table(vm_items, show_owner=True)}</section>"
    )


def render_resource_dashboard(
    host,
    sessions=(),
    vms=None,
    *,
    max_messages=DEFAULT_MESSAGE_LIMIT,
    guidance_action="/sessions/guidance",
    kill_action="/sessions/kill",
    csrf_token=None,
    messages_by_session=None,
):
    """Render host memory, active sessions with owned VMs, and other LTVM VMs."""
    host = _project(host)
    if vms is None:
        inventory = _get(host, "ltvm")
        vms = _get(inventory, "vms", default=[])
    sessions_html, other = render_active_sessions(
        sessions,
        vms,
        max_messages=max_messages,
        guidance_action=guidance_action,
        kill_action=kill_action,
        csrf_token=csrf_token,
        messages_by_session=messages_by_session,
    )
    return (
        "<div class='resource-dashboard'>"
        f"{render_host_memory_summary(host)}{sessions_html}{render_other_vms(other)}"
        "</div>"
    )
