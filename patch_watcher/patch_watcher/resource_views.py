"""Accessible, dependency-free HTML views for worker and session resources.

The rendering boundary deliberately accepts dictionaries, other mappings, or
objects with attributes (including dataclasses).  It does not import the
resource collector or persistence layer, so those implementations can evolve
without coupling the dashboard to their concrete record types.
"""

import math
from collections.abc import Mapping
from datetime import UTC, datetime
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
        to_dict = record.to_dict
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
    current = now or datetime.now(UTC)
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

    # The headline is the question an operator actually opens this page with:
    # is there room to start another VM, and can I trust the number? The other
    # nine metrics are diagnostic, so they go behind a disclosure -- but the
    # collection errors stay outside it, because a panel that hides the reason
    # its own numbers are wrong is worse than one that shows nothing.
    available = format_bytes(
        _get(memory, "available_bytes", "memory_available_bytes")
    )
    total = format_bytes(_get(memory, "total_bytes", "memory_total_bytes"))
    guest_rss = format_bytes(
        _first(
            (host, inventory, memory),
            "vm_process_rss_bytes",
            "ltvm_process_rss_bytes",
            "measured_host_rss_bytes",
        )
    )
    healthy = quality.casefold() == "good" and not errors
    headline = (
        "<p class='host-memory-headline'>"
        f"<strong>{escape(available)}</strong> available of {escape(total)} · "
        f"LTVM guests using {escape(guest_rss)} · "
        f"<span class='resource-status tone-{'good' if healthy else 'warn'}'>"
        f"{escape(quality)}</span></p>"
    )
    return (
        "<section class='host-memory resource-card' aria-labelledby='host-memory-title'>"
        "<h2 id='host-memory-title'>Worker host memory</h2>"
        f"<p class='detail'><strong>Host:</strong> {escape(name)}</p>"
        f"{headline}{error_html}"
        # Opens itself when the sample is not clean, so a degraded reading is
        # never one click away from an operator who has no reason to click.
        f"<details class='host-memory-detail'{'' if healthy else ' open'}>"
        "<summary>Full memory breakdown</summary>"
        "<p class='resource-sample'>"
        f"<strong>Sample time:</strong> {escape(sampled_at)} · "
        f"<strong>Freshness:</strong> {escape(freshness)} · "
        f"<strong>Pressure:</strong> {escape(pressure)} · "
        f"<strong>Quality:</strong> {escape(quality)}</p>"
        f"<dl class='resource-metrics'>{metrics}</dl></details></section>"
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


def _vm_name(vm):
    return _plain(_get(vm, "name"))


def _vm_controls(vm, csrf_token):
    """Stop and destroy for one guest, addressed by exact name.

    Destroy posts to a confirmation page rather than acting: it is
    irreversible and these are the operator's own guests, several of which
    are long-lived by the look of the inventory.
    """

    name = _vm_name(vm)
    if not name or name == UNKNOWN:
        return "<td class='vm-controls'></td>"
    token = escape(str(csrf_token or ""), quote=True)
    safe = escape(name, quote=True)
    stopped = _human_state(_get(vm, "state")).casefold() == "stopped"
    stop_button = (
        "<form method='post' action='/vms/stop'>"
        f"<input type='hidden' name='csrf_token' value='{token}'>"
        f"<input type='hidden' name='name' value='{safe}'>"
        "<button class='secondary' type='submit'"
        + (" disabled aria-disabled='true'" if stopped else "")
        + ">Shut down</button></form>"
    )
    destroy_button = (
        "<form method='post' action='/vms/destroy'>"
        f"<input type='hidden' name='csrf_token' value='{token}'>"
        f"<input type='hidden' name='name' value='{safe}'>"
        "<button class='danger' type='submit'>Destroy…</button></form>"
    )
    return f"<td class='vm-controls'>{stop_button}{destroy_button}</td>"


def _vm_totals(vms):
    """Sum what this group of guests is actually costing the host."""

    running = sum(
        1 for vm in vms
        if _human_state(_get(vm, "state")).casefold() == "running"
    )
    configured = sum(
        value for vm in vms
        if isinstance(value := _get(vm, "configured_guest_memory_bytes"), int)
    )
    measured = sum(
        value for vm in vms
        if isinstance(value := _get(vm, "host_rss_bytes"), int)
    )
    return running, configured, measured


def _render_vm_table(vms, *, show_owner, csrf_token=None, owner_labels=None):
    """Render one LTVM guest table from the fields the sampler really writes.

    ``LTVMVMStatus.to_dict`` supplies name, state, owner_id, vcpus, ip,
    configured guest memory, measured host RSS and the source it was measured
    from, the QEMU PID, and a per-VM sample quality. It supplies no topology,
    role, age, CPU share, or cleanup state, and columns for those could only
    ever print "unknown" for every guest that has ever existed.
    """
    owner_header = "<th scope='col'>Owner</th>" if show_owner else ""
    rows = []
    for vm in vms:
        measurement = _plain(_get(vm, "host_memory_source"), "")
        quality = _plain(_get(vm, "quality"), "")
        detail = measurement or (f"Sample quality: {quality}" if quality else "Not measured")
        owner_cell = ""
        if show_owner:
            # The run that owns it, when one does.  A raw owner id names a
            # session nobody can look up from here; a run id is the thing the
            # rest of the console is keyed on, and "no owner" is the word for
            # a guest that outlived whatever made it.
            named = (owner_labels or {}).get(_vm_name(vm))
            owner_cell = (
                f"<td>{escape(named)}</td>" if named
                else "<td class='vm-unowned'>no owner</td>"
            )
        control_cell = _vm_controls(vm, csrf_token) if csrf_token else ""
        rows.append(
            "<tr>"
            f"<th scope='row'>{escape(_vm_name(vm))}</th>"
            f"<td>{_status_badge(_get(vm, 'state'))}</td>"
            f"<td>{escape(_plain(_get(vm, 'vcpus')))}</td>"
            f"<td>{escape(_plain(_get(vm, 'ip')))}</td>"
            f"<td>{escape(format_bytes(_get(vm, 'configured_guest_memory_bytes')))}</td>"
            f"<td>{escape(format_bytes(_get(vm, 'host_rss_bytes')))}"
            f"<small>{escape(detail)}</small></td>"
            f"<td>{escape(_plain(_get(vm, 'process_id')))}</td>"
            f"{owner_cell}{control_cell}</tr>"
        )
    colspan = (8 if show_owner else 7) + (1 if csrf_token else 0)
    if not rows:
        rows.append(
            f"<tr><td class='empty' colspan='{colspan}'>No VMs in this group.</td></tr>"
        )
    return (
        "<table class='vm-table'><thead><tr>"
        "<th scope='col'>VM</th><th scope='col'>State</th>"
        "<th scope='col'>vCPUs</th><th scope='col'>IP address</th>"
        "<th scope='col'>Configured guest memory</th>"
        "<th scope='col'>Actual host RSS</th>"
        f"<th scope='col'>QEMU PID</th>{owner_header}"
        + ("<th scope='col'>Controls</th>" if csrf_token else "")
        + "</tr></thead>"
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


def render_ltvm_guests(sessions, vms=(), *, csrf_token=None):
    """Every LTVM guest on this host, in one table, with the run that owns it.

    This replaced two cards.  "Active managed sessions" listed the runs that
    were going, in the Runs card's own columns, with each run's guests folded
    inside it; "Other LTVM VMs" listed the guests that matched no run, under a
    heading that only made sense if you had read the first card.  Between them
    a guest appeared in one place or the other depending on a matching rule,
    which is exactly the thing a reader cannot see.

    One table, every guest, and a column saying which run owns it.  Ownership
    is still resolved only from exact durable owner identifiers -- never name
    similarity -- and a guest that matches no live run says "no owner", which
    is the thing actually worth noticing: it outlived whatever made it.
    """
    session_items = _items(sessions)
    vm_items = _items(vms)
    owned, _other = _associate_vms(session_items, vm_items)
    labels = {}
    for index, session in enumerate(session_items):
        raw = _get(session, "run", "run_id")
        run = str(raw) if raw not in (None, "") else _session_id(session)
        for vm in owned[index]:
            labels[_vm_name(vm)] = run or "a running run"

    running, configured, measured = _vm_totals(vm_items)
    unowned = [vm for vm in vm_items if _vm_name(vm) not in labels]
    summary = (
        f"{len(vm_items)} guest(s) · {running} running · "
        f"{format_bytes(measured)} measured host RSS · "
        f"{format_bytes(configured)} configured guest memory"
    )
    if unowned:
        summary += f" · {len(unowned)} with no owner"
    return (
        "<section class='other-vms resource-card' aria-labelledby='other-vms-title'>"
        f"<h2 id='other-vms-title'>LTVM guests ({len(vm_items)})</h2>"
        f"<p class='other-vms-headline'>{escape(summary)}</p>"
        "<details class='other-vms-detail'><summary>Show each guest</summary>"
        "<p>A guest with no owner matched no running run. Patch Watcher never "
        "adopts one for automatic cleanup, so the controls here are yours and "
        "act immediately on this host.</p>"
        + _render_vm_table(
            vm_items, show_owner=True, csrf_token=csrf_token, owner_labels=labels
        )
        + "</details></section>"
    )


def render_resource_dashboard(host, sessions=(), vms=None, *, csrf_token=None):
    """Render host memory and every LTVM guest, with the run that owns it.

    ``sessions`` is still taken, and still only to resolve guest ownership.
    It no longer produces a card of its own: what a run is doing and what it
    last said is the Runs card's job, and the two message parameters this used
    to accept were feeding a display that has moved to the run's own page.
    """
    host = _project(host)
    if vms is None:
        inventory = _get(host, "ltvm")
        vms = _get(inventory, "vms", default=[])
    return (
        "<div class='resource-dashboard'>"
        f"{render_host_memory_summary(host)}"
        f"{render_ltvm_guests(sessions, vms, csrf_token=csrf_token)}"
        "</div>"
    )
