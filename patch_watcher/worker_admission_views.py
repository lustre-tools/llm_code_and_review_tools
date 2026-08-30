"""Accessible HTML rendering for worker admission and provenance.

The view boundary is deliberately independent of worker-profile and admission
implementations.  Inputs may be mappings, dataclasses/attribute objects, or
objects exposing a mapping-returning ``to_dict()`` method.
"""

from collections.abc import Mapping
from html import escape


UNKNOWN = "unknown"
_MISSING = object()


def _project(record):
    if record is None or isinstance(record, Mapping):
        return record
    try:
        to_dict = getattr(record, "to_dict")
    except AttributeError:
        return record
    if not callable(to_dict):
        return record
    projected = to_dict()
    return projected if isinstance(projected, Mapping) else record


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


def _find(records, *names):
    """Return ``(present, value)`` while preserving an explicit empty value."""
    for record in records:
        value = _get(record, *names, default=_MISSING)
        if value is not _MISSING:
            return True, value
    return False, None


def _plain(value, default=UNKNOWN):
    if value is None or value == "":
        return default
    return str(value)


def _human(value):
    text = _plain(value)
    if text == UNKNOWN:
        return text
    return text.replace("_", " ").replace("-", " ").capitalize()


def _items(value):
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _collect(records, *names):
    """Collect all present list-like fields, preserving absent versus empty."""
    present = False
    collected = []
    for record in records:
        for name in names:
            found, value = _find((record,), name)
            if found:
                present = True
                collected.extend(_items(value))
    return present, collected


def _status(status):
    raw = _plain(status)
    normalized = raw.casefold().replace("-", "_").replace(" ", "_")
    states = {
        "not_checked": ("Not checked", "neutral"),
        "checking": ("Checking", "info"),
        "ready": ("Ready", "good"),
        "degraded": ("Degraded", "warn"),
        "blocked": ("Blocked", "bad"),
    }
    if raw == UNKNOWN:
        label, tone = "Unknown", "neutral"
    elif normalized in states:
        label, tone = states[normalized]
    else:
        label, tone = f"Unknown ({raw})", "neutral"
    return (
        f"<span class='admission-status tone-{tone}'>Admission: "
        f"{escape(label)}</span>"
    )


def _isolation_label(value):
    raw = _plain(value)
    normalized = raw.casefold().replace("_", "-").replace(" ", "-")
    if raw == UNKNOWN:
        return "unknown", "neutral"
    if normalized in {
        "none", "host", "unsandboxed", "host-unsandboxed",
        "trusted-unsandboxed", "unsandboxed-host-worker",
    } or "unsandboxed" in normalized:
        return "Unsandboxed host worker", "bad"
    if "container" in normalized or normalized in {"sandboxed", "isolated"}:
        return _human(raw), "good"
    return _human(raw), "neutral"


def _network_label(value):
    raw = _plain(value)
    normalized = raw.casefold().replace("_", "-").replace(" ", "-")
    if raw == UNKNOWN:
        return "unknown", "neutral"
    if normalized in {
        "general", "network-general", "general-egress", "unrestricted",
        "host", "host-network", "host-ambient", "full",
    }:
        return "General network access", "warn"
    if normalized in {
        "none", "offline", "no-network", "tools-offline",
        "container-offline-tools",
    }:
        # This wording intentionally does not claim the controller/model
        # transport is offline merely because worker tool execution is.
        return "Worker-tool network disabled", "good"
    if "restrict" in normalized or "allowlist" in normalized:
        return _human(raw), "info"
    return _human(raw), "neutral"


def _boundary_badge(kind, value, *, attested):
    if kind == "isolation":
        text, tone = _isolation_label(value)
    else:
        text, tone = _network_label(value)
    source = "Attested" if attested else "Declared only"
    return (
        f"<span class='worker-boundary tone-{tone}'>"
        f"{source} {escape(kind)}: {escape(text)}</span>"
    )


def _profile_field(profile, attestation, admission, *names):
    present, value = _find((profile, attestation, admission), *names)
    return value if present else None


def _actual_or_declared(attestation, admission, profile, *names):
    """Return a value plus whether it came from an attestation/result."""
    actual_present, actual = _find((attestation, admission), *names)
    if actual_present:
        return actual, True
    declared_present, declared = _find((profile,), *names)
    return (declared if declared_present else None), False


def _definition_item(label, value, *, code=False):
    content = escape(_plain(value))
    if code:
        content = f"<code>{content}</code>"
    return f"<div><dt>{escape(label)}</dt><dd>{content}</dd></div>"


def _warning_text(warning):
    if isinstance(warning, bytes):
        return warning.decode("utf-8", errors="replace")
    if isinstance(warning, str):
        return warning
    code = _plain(_get(warning, "code", "warning_code"), "")
    message = _plain(_get(warning, "message", "warning", "detail", "reason"))
    return f"{code}: {message}" if code else message


def _render_warnings(present, warnings):
    if not present:
        return (
            "<section class='admission-warnings' aria-labelledby='worker-warnings-title'>"
            "<h3 id='worker-warnings-title'>Warnings</h3>"
            "<p class='unknown'>Warning collection is unknown.</p></section>"
        )
    warning_items = _items(warnings)
    if not warning_items:
        body = "<p>Warnings: 0 (none reported).</p>"
    else:
        body = "<ul>" + "".join(
            f"<li>{escape(_warning_text(warning))}</li>"
            for warning in warning_items
        ) + "</ul>"
    return (
        "<section class='admission-warnings' aria-labelledby='worker-warnings-title'>"
        f"<h3 id='worker-warnings-title'>Warnings ({len(warning_items)})</h3>"
        f"{body}</section>"
    )


def _failure_fields(failure):
    if isinstance(failure, bytes):
        return failure.decode("utf-8", errors="replace"), "", []
    if isinstance(failure, str):
        return failure, "", []
    code = _plain(_get(failure, "code", "error_code", "failure_code"))
    message = _plain(_get(failure, "message", "reason", "detail", "error"))
    details = []
    for label, names in (
        ("Check", ("check", "check_name", "preflight")),
        ("Tool", ("tool", "tool_name", "command")),
        ("Expected", ("expected", "requirement", "required")),
        ("Actual", ("actual", "observed", "found")),
    ):
        present, value = _find((failure,), *names)
        if present:
            details.append((label, value))
    return code, message, details


def _render_failures(present, failures, normalized_status):
    if not present:
        extra = (
            " Admission is blocked, but precise failure details are unavailable."
            if normalized_status == "blocked" else ""
        )
        return (
            "<section class='preflight-failures' aria-labelledby='preflight-failures-title'>"
            "<h3 id='preflight-failures-title'>Failed preflight checks</h3>"
            f"<p class='unknown'>Preflight failure collection is unknown.{extra}</p>"
            "</section>"
        )
    failure_items = _items(failures)
    if not failure_items:
        if normalized_status == "blocked":
            body = (
                "<p class='unknown'>Failed preflight checks: 0 reported; "
                "the blocked reason is unavailable.</p>"
            )
        else:
            body = "<p>Failed preflight checks: 0 (none reported).</p>"
    else:
        rows = []
        for failure in failure_items:
            code, message, details = _failure_fields(failure)
            detail_html = ""
            if details:
                detail_html = "<dl>" + "".join(
                    _definition_item(label, value, code=label in {"Expected", "Actual"})
                    for label, value in details
                ) + "</dl>"
            rows.append(
                "<li>"
                f"<strong><code>{escape(code)}</code></strong>"
                f"{f': {escape(message)}' if message else ''}"
                f"{detail_html}</li>"
            )
        body = "<ol>" + "".join(rows) + "</ol>"
    return (
        "<section class='preflight-failures' aria-labelledby='preflight-failures-title'>"
        f"<h3 id='preflight-failures-title'>Failed preflight checks ({len(failure_items)})</h3>"
        f"{body}</section>"
    )


def _tool_records(tools):
    if isinstance(tools, Mapping):
        return list(tools.items())
    return [(None, tool) for tool in _items(tools)]


def _tool_field(tool, *names):
    if isinstance(tool, (str, bytes, int, float, bool)):
        return None
    return _get(tool, *names)


def _tool_status(tool):
    status = _tool_field(tool, "status", "state", "quality")
    if status is not None and status != "":
        return _human(status)
    available = _tool_field(tool, "available", "found", "resolved")
    if available is True:
        return "Available"
    if available is False:
        return "Unavailable"
    return UNKNOWN


def _tool_required(tool):
    required = _tool_field(tool, "required", "is_required")
    if required is True:
        return "Required"
    if required is False:
        return "Optional"
    return UNKNOWN


def _render_tools(present, tools):
    if not present:
        return (
            "<section class='resolved-tools' aria-labelledby='resolved-tools-title'>"
            "<h3 id='resolved-tools-title'>Resolved tools</h3>"
            "<p class='unknown'>Tool resolution is unknown.</p></section>"
        )
    records = _tool_records(tools)
    if not records:
        body = "<p>Resolved tools: 0 (none reported).</p>"
    else:
        rows = []
        for supplied_name, tool in records:
            scalar = isinstance(tool, (str, bytes, int, float, bool))
            name = supplied_name
            if name is None:
                name = _tool_field(tool, "name", "tool", "command", "id")
            version = tool if scalar else _tool_field(tool, "version", "resolved_version")
            path = None if scalar else _tool_field(tool, "path", "resolved_path", "executable")
            rows.append(
                "<tr>"
                f"<th scope='row'>{escape(_plain(name))}</th>"
                f"<td>{escape(_tool_status(tool))}</td>"
                f"<td>{escape(_plain(version))}</td>"
                f"<td><code>{escape(_plain(path))}</code></td>"
                f"<td>{escape(_tool_required(tool))}</td></tr>"
            )
        body = (
            "<table><thead><tr><th scope='col'>Tool</th><th scope='col'>Status</th>"
            "<th scope='col'>Version</th><th scope='col'>Resolved path</th>"
            "<th scope='col'>Requirement</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return (
        "<section class='resolved-tools' aria-labelledby='resolved-tools-title'>"
        f"<h3 id='resolved-tools-title'>Resolved tools ({len(records)})</h3>"
        f"{body}</section>"
    )


def render_worker_admission(admission=None, *, profile=None, attestation=None):
    """Render one compact worker-profile admission/provenance section.

    ``admission`` may be a unified view model containing nested ``profile`` and
    ``attestation`` records.  Explicit keyword records take precedence.  The
    renderer never infers a ready state from an empty warning/failure list.
    """
    admission = _project(admission)
    if profile is None:
        profile = _get(admission, "profile", "worker_profile")
    if attestation is None:
        attestation = _get(
            admission,
            "attestation",
            "environment_attestation",
            "admission_attestation",
        )
    profile = _project(profile)
    attestation = _project(attestation)

    profile_id = _profile_field(
        profile,
        attestation,
        admission,
        "worker_profile_id",
        "profile_id",
        "id",
        "name",
    )
    profile_version = _profile_field(
        profile, attestation, admission, "profile_version", "version"
    )
    profile_hash = _profile_field(
        profile,
        attestation,
        admission,
        "worker_profile_hash",
        "profile_hash",
        "content_hash",
        "hash",
    )
    worker_host = _profile_field(
        None, attestation, admission, "worker_host", "host"
    )
    host_id = _profile_field(
        None, attestation, admission, "worker_host_id", "host_id", "hostname"
    )
    if host_id is None:
        host_id = _get(worker_host, "host_id", "identity", "hostname")
    environment_id = _profile_field(
        None,
        attestation,
        admission,
        "environment_id",
        "environment_instance_id",
        "image_digest",
        "host_build_id",
    )
    if environment_id is None:
        environment_id = _get(worker_host, "image_digest") or _get(
            worker_host, "host_build_id"
        )
    attested_at = _profile_field(
        None,
        attestation,
        admission,
        "attested_at",
        "attestation_time",
        "checked_at",
        "created_at",
        "sampled_at",
    )

    isolation, isolation_attested = _actual_or_declared(
        attestation,
        admission,
        profile,
        "active_isolation_profile",
        "isolation_profile",
        "isolation_mode",
        "isolation",
    )
    network, network_attested = _actual_or_declared(
        attestation,
        admission,
        profile,
        "active_network_profile",
        "network_profile",
        "network_mode",
        "network",
    )

    warning_present, warnings = _collect(
        (admission, attestation),
        "warnings",
        "admission_warnings",
        "deviations",
        "unavailable_optional_capabilities",
    )
    failure_present, failures = _collect(
        (admission, attestation),
        "failed_preflights",
        "preflight_failures",
        "failure_reasons",
        "failures",
        "failure_codes",
        "errors",
    )
    stored_failure_code = _get(admission, "failure_code")
    if stored_failure_code not in {None, ""}:
        failure_present = True
        stored_failure_summary = _get(admission, "failure_summary")
        # Prefer the persisted, human-readable summary for its code while
        # retaining any additional attestation failure codes.
        failures = [
            failure for failure in failures
            if not (
                failure == stored_failure_code
                or _get(failure, "code", "error_code", "failure_code")
                == stored_failure_code
            )
        ]
        failures.insert(0, {
            "code": stored_failure_code,
            "message": stored_failure_summary,
        })
    tools_present, tools = _find(
        (attestation, admission),
        "resolved_tools",
        "tools",
        "tool_results",
        "executables",
        "tool_versions",
    )

    status = _get(admission, "admission_status", "status", "state")
    if status is None:
        status = _get(attestation, "admission_status", "status", "state")
    # Canonical attestations carry a status.  ``admitted`` remains a safe
    # compatibility fallback because it is an explicit admission result, not
    # an inference from the absence of failure records.
    if status is None:
        admitted = _get(attestation, "admitted")
        if admitted is None:
            admitted = _get(admission, "admitted")
        if admitted is False:
            status = "blocked"
        elif admitted is True:
            status = "degraded" if warnings else "ready"
    normalized_status = _plain(status).casefold().replace("-", "_").replace(" ", "_")

    # Preserve both sides of provenance mismatches rather than relabeling the
    # attested environment as though it matched the requested profile.
    mismatch_rows = []
    declared_isolation = _get(profile, "isolation_profile", "isolation_mode", "isolation")
    declared_network = _get(profile, "network_profile", "network_mode", "network")
    if isolation_attested and declared_isolation not in {None, ""} and isolation != declared_isolation:
        mismatch_rows.append(
            "<li>Profile-declared isolation: "
            f"{escape(_human(declared_isolation))}</li>"
        )
    if network_attested and declared_network not in {None, ""} and network != declared_network:
        mismatch_rows.append(
            "<li>Profile-declared network: "
            f"{escape(_human(declared_network))}</li>"
        )
    mismatch_html = (
        "<div class='provenance-mismatch' role='status'>"
        "<strong>Declaration/attestation differences</strong><ul>"
        + "".join(mismatch_rows) + "</ul></div>"
        if mismatch_rows else ""
    )

    provenance = "".join((
        _definition_item("Worker profile", profile_id),
        _definition_item("Profile version", profile_version),
        _definition_item("Profile hash", profile_hash, code=True),
        _definition_item("Environment ID", environment_id, code=True),
        _definition_item("Worker host ID", host_id, code=True),
        _definition_item("Attested at", attested_at),
    ))

    return (
        "<section class='worker-admission resource-card' "
        "aria-labelledby='worker-admission-title'>"
        "<div class='worker-admission-heading'>"
        "<h2 id='worker-admission-title'>Worker admission and provenance</h2>"
        f"{_status(status)}</div>"
        "<div class='worker-boundaries' aria-label='Worker execution boundaries'>"
        f"{_boundary_badge('isolation', isolation, attested=isolation_attested)}"
        f"{_boundary_badge('network', network, attested=network_attested)}</div>"
        f"{mismatch_html}<dl class='worker-provenance'>{provenance}</dl>"
        f"{_render_tools(tools_present, tools)}"
        f"{_render_warnings(warning_present, warnings)}"
        f"{_render_failures(failure_present, failures, normalized_status)}"
        "</section>"
    )


render_worker_admission_view = render_worker_admission
