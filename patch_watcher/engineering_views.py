"""Side-effect-free HTML views for Phase 3 engineering runs.

The views accept mappings, dataclasses, or other attribute objects and have no
controller or persistence dependencies.  The Phase 3A summary deliberately
renders only a small manifest allowlist.  Phase 3B separately renders planned
and executed argv as escaped, tokenized audit data; environment values and
arbitrary artifact URLs never cross this display boundary.

Route contract used by the renderers:

* ``POST /engineering-runs/prepare`` validates a requested start and redirects
  to a display-only confirmation page owned by the controller.
* that confirmation page uses :func:`render_engineering_start_confirmation`
  and submits ``POST /engineering-runs/start`` with controller-issued tokens;
* run detail controls link to ``.../confirm?intent=...`` display pages; and
* only confirmation pages submit cancel, kill, or retry mutations.

Phase 3B guest execution is granted by the existing engineering-run start
confirmation.  The immutable manifest records optional intent and evidence;
it is not a per-command allowlist.  Every actual guest command remains visible
in the audit history.
"""

import math
from collections.abc import Mapping
from html import escape
from urllib.parse import quote, urlencode, urlsplit


UNKNOWN = "unknown"
ACTIVE_STATES = {
    "queued", "preparing", "running", "waiting_external", "waiting_human",
    "paused", "blocked", "recovering", "cleanup_pending", "cleaning",
}
TERMINAL_STATES = {
    "succeeded", "failed", "cancelled", "stale", "resource_exhausted",
    "cleanup_failed", "quarantined",
}
CONTROL_INTENTS = {"cancel", "kill", "retry"}


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
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _state(value):
    return _plain(value).casefold().replace("-", "_").replace(" ", "_")


def _human(value):
    text = _plain(value)
    if text == UNKNOWN:
        return "Unknown"
    return text.replace("_", " ").replace("-", " ").capitalize()


def _format_bytes(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    if value < 0 or not math.isfinite(value):
        return UNKNOWN
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
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


def _field(label, value, *, code=False, detail=None):
    content = escape(_plain(value))
    if code:
        content = f"<code>{content}</code>"
    detail_html = f"<small>{escape(detail)}</small>" if detail else ""
    return (
        f"<div><dt>{escape(label)}</dt><dd>{content}{detail_html}</dd></div>"
    )


def _hidden(name, value):
    if value is None:
        return ""
    return (
        f"<input type='hidden' name='{escape(str(name), quote=True)}' "
        f"value='{escape(str(value), quote=True)}'>"
    )


def _safe_base_url(value):
    """Return a local route prefix, never a user-controlled external URL."""
    text = _plain(value, "")
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "/engineering-runs"
    if (
        not text.startswith("/") or text.startswith("//") or parsed.scheme
        or parsed.netloc or parsed.query or parsed.fragment or "\\" in text
        or any(ord(character) < 32 for character in text)
    ):
        return "/engineering-runs"
    return parsed.path.rstrip("/") or "/engineering-runs"


def _run_id(run):
    return _plain(_get(run, "run_id", "id"))


def _run_path(run, base_url):
    return _safe_base_url(base_url) + "/" + quote(_run_id(run), safe="")


def _revision(record):
    checkout = _get(record, "checkout", "source_checkout")
    return _get(
        record, "revision_sha", "pinned_revision", "revision", "commit_sha",
        default=_get(checkout, "revision_sha", "revision", "commit_sha"),
    )


def _status_badge(value, *, noun="Run"):
    normalized = _state(value)
    if normalized in {"running", "active", "succeeded", "destroyed", "clean"}:
        tone = "good"
    elif normalized in {
        "failed", "resource_exhausted", "cleanup_failed", "orphaned",
        "quarantined", "cancelled",
    }:
        tone = "bad"
    elif normalized in {
        "waiting_human", "paused", "cleanup_pending", "destroying", "stale",
        "retained", "cooldown",
    }:
        tone = "warn"
    else:
        tone = "neutral"
    return (
        f"<span class='engineering-status tone-{tone}'>"
        f"{escape(noun)}: {escape(_human(value))}</span>"
    )


def _capability_status(suffix="default", run=None):
    """Render the Phase 3 boundary without overstating an active grant."""
    title_id = "engineering-capabilities-title-" + quote(str(suffix), safe="")
    if run is None:
        source_status = "declared; activated only for a confirmed, active engineering run"
        guest_status = (
            "declared; activated only after exact revision and owner binding are verified"
        )
    else:
        run_state = _state(_get(run, "state", "status"))
        validation = _get(
            run, "validation", "validation_execution", "validation_status"
        )
        if run_state in TERMINAL_STATES:
            source_status = "expired; the engineering run is terminal"
            guest_status = "expired; no guest command capability is active"
        elif validation is None:
            source_status = (
                "active inside the isolated checkout" if run_state == "running"
                else "pending isolated-checkout activation"
            )
            guest_status = "not verified active; no validation grant is displayed"
        else:
            identity = _validation_identity(run, validation)
            issues = _validation_identity_issues(run, identity)
            approval = _state(_get(
                validation, "approval_state", "authorization_state"
            ))
            validation_state = _state(_get(
                validation, "state", "status", "outcome"
            ))
            active = (
                not issues and approval == "approved"
                and validation_state in {"claimed", "running", "active"}
            )
            source_status = (
                "active inside the isolated checkout" if run_state == "running"
                else "approved for this non-terminal engineering run"
            )
            guest_status = (
                "active as one open-ended audited capability, restricted to exact-owner session LTVM guests"
                if active else
                "inactive; an exact approved running validation grant is not verified"
            )
    return (
        "<section class='engineering-capabilities' "
        f"aria-labelledby='{escape(title_id, quote=True)}'>"
        f"<h3 id='{escape(title_id, quote=True)}'>Capability status</h3><ul>"
        "<li class='capability-enabled'><strong>Source editing:</strong> "
        + escape(source_status) + "</li>"
        "<li class='capability-gated'><strong>Guest build/test execution:</strong> "
        + escape(guest_status) + "</li>"
        "<li class='capability-disabled'><strong>Host command execution:</strong> disabled; commands are brokered only into approved session-owned guests</li>"
        "<li class='capability-disabled'><strong>Gerrit upload:</strong> disabled for this subphase</li>"
        "</ul><p>The engineering-run approval grants an open-ended, audited guest-execution capability "
        "inside the exact owner boundary; it is not approval of each individual "
        "command. The run cannot execute patch code on the host, upload a patchset, "
        "or otherwise write to Gerrit.</p></section>"
    )


def _safe_remote(value):
    """Remove credentials, query text, and fragments from a display-only remote."""
    text = _plain(value)
    if text == UNKNOWN:
        return text
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "redacted invalid remote"
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or "redacted host"
        try:
            port = parsed.port
        except ValueError:
            return "redacted invalid remote"
        if port:
            host += f":{port}"
        return f"{parsed.scheme}://{host}{parsed.path}"
    # SCP-style remotes contain no URL password/query component.  Keep only a
    # bounded human-readable value and redact anything resembling userinfo.
    if "@" in text:
        return text.split("@", 1)[1]
    return text.split("?", 1)[0].split("#", 1)[0]


def _render_checkout(run, suffix):
    checkout = _get(run, "checkout", "source_checkout", default={})
    state = _get(checkout, "state", "status", default=_get(run, "checkout_state"))
    dedicated = _get(checkout, "dedicated", "isolated", "independent")
    dirty = _get(checkout, "initial_dirty", "dirty_at_start", "initially_dirty")
    dedicated_text = "yes" if dedicated is True else "no" if dedicated is False else UNKNOWN
    dirty_text = "dirty" if dirty is True else "clean" if dirty is False else UNKNOWN
    warnings = []
    if dedicated is False:
        warnings.append("Checkout is not recorded as a dedicated independent checkout.")
    if dirty is True:
        warnings.append("Checkout was dirty before this run and must not be reused.")
    manifest_revision = _get(checkout, "revision_sha", "revision", "commit_sha")
    if manifest_revision and _revision(run) and manifest_revision != _revision(run):
        warnings.append("Checkout revision does not match the run's exact pinned revision.")
    warning_html = ""
    if warnings:
        warning_html = (
            "<div class='checkout-warning' role='alert'><strong>Checkout warning</strong><ul>"
            + "".join(f"<li>{escape(item)}</li>" for item in warnings) + "</ul></div>"
        )
    return (
        f"<section class='engineering-checkout' aria-labelledby='checkout-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='checkout-title-{escape(suffix, quote=True)}'>Isolated full checkout</h3>"
        f"{_status_badge(state, noun='Checkout')}<dl>"
        + _field("Exact pinned revision", _revision(run), code=True)
        + _field("Repository remote", _safe_remote(_get(checkout, "remote", "repository_remote")))
        + _field("Base branch", _get(checkout, "base_branch", "branch"), code=True)
        + _field("Logical source path", _get(checkout, "logical_path", "source_path"), code=True)
        + _field("Dedicated independent checkout", dedicated_text)
        + _field("Initial checkout state", dirty_text)
        + _field("Cleanup state", _human(_get(checkout, "cleanup_state")))
        + "</dl>" + warning_html + "</section>"
    )


def _manifest_step_label(step):
    return _get(step, "label", "name", "step_id", "id", default="Unnamed step")


def _render_manifest(run, suffix):
    manifest = _get(run, "manifest", "resource_manifest", "execution_manifest", default={})
    build_steps = _items(_get(manifest, "build_steps", "builds"))
    test_steps = _items(_get(manifest, "test_steps", "tests"))

    def step_list(steps, empty):
        if not steps:
            return f"<p class='empty'>{escape(empty)}</p>"
        return "<ul>" + "".join(
            "<li><strong>" + escape(_plain(_manifest_step_label(step))) + "</strong> · "
            + escape(_human(_get(step, "state", "status", default="planned")))
            + " · target " + escape(_plain(_get(step, "target", "environment", "scope")))
            + "</li>" for step in steps
        ) + "</ul>"

    return (
        f"<section class='safe-manifest' aria-labelledby='safe-manifest-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='safe-manifest-title-{escape(suffix, quote=True)}'>Safe execution manifest summary</h3>"
        "<p>Only approved metadata is shown. Raw commands, arguments, environment "
        "values, and secrets are intentionally omitted.</p><dl>"
        + _field("Manifest schema", _get(manifest, "schema_version", "version"), code=True)
        + _field("Manifest digest", _get(manifest, "digest", "hash", "manifest_hash"), code=True)
        + _field("Isolation profile", _get(manifest, "isolation_profile", "isolation"))
        + _field("Network profile", _get(manifest, "network_profile", "network"))
        + _field("LTVM owner", _get(run, "owner_id", "ltvm_owner_id", default=_get(manifest, "ltvm_owner_id")), code=True)
        + "</dl><h4>Build steps</h4>" + step_list(build_steps, "No build steps recorded.")
        + "<h4>Test steps</h4>" + step_list(test_steps, "No test steps recorded.")
        + "</section>"
    )


def _artifact_link(run, artifact, *, base_url):
    artifact_id = _get(artifact, "artifact_id", "id")
    if artifact_id is None or artifact_id == "":
        return ""
    path = _run_path(run, base_url) + "/artifacts/" + quote(str(artifact_id), safe="")
    return f" <a href='{escape(path, quote=True)}'>Open captured artifact</a>"


def _artifact_row(run, artifact, *, base_url, kind):
    label = _get(artifact, "label", "name", "filename", default=f"{kind} artifact")
    digest = _get(artifact, "digest", "sha256", "hash")
    size = _format_bytes(_get(artifact, "size_bytes", "bytes"))
    state = _human(_get(artifact, "state", "status"))
    return (
        "<li><strong>" + escape(_plain(label)) + "</strong> · " + escape(state)
        + " · " + escape(size) + " · digest <code>" + escape(_plain(digest))
        + "</code>" + _artifact_link(run, artifact, base_url=base_url) + "</li>"
    )


def _render_artifacts(run, *, base_url, suffix):
    artifacts = _items(_get(run, "artifacts", "collected_artifacts"))
    diffs = _items(_get(run, "diffs", "patches", "proposed_diffs"))
    tests = _items(_get(run, "test_results", "tests", "test_evidence"))

    artifact_html = (
        "<p class='empty'>No captured artifacts.</p>" if not artifacts else
        "<ul>" + "".join(
            _artifact_row(run, item, base_url=base_url, kind="Captured")
            for item in artifacts
        ) + "</ul>"
    )
    diff_html = (
        "<p class='empty'>No proposed diff artifact.</p>" if not diffs else
        "<ul>" + "".join(
            _artifact_row(run, item, base_url=base_url, kind="Diff")
            for item in diffs
        ) + "</ul>"
    )
    if tests:
        test_html = "<ul>" + "".join(
            "<li><strong>" + escape(_plain(_get(test, "name", "label", "suite")))
            + "</strong> · " + escape(_human(_get(test, "outcome", "state", "status")))
            + " · exit status " + escape(_plain(_get(test, "exit_status", "exit_code")))
            + " · " + escape(_format_duration(_get(test, "duration_seconds", "elapsed_seconds")))
            + _artifact_link(run, test, base_url=base_url) + "</li>" for test in tests
        ) + "</ul>"
    else:
        test_html = "<p class='empty'>No test evidence recorded.</p>"
    return (
        f"<section class='engineering-evidence' aria-labelledby='engineering-evidence-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='engineering-evidence-title-{escape(suffix, quote=True)}'>Artifacts, proposed diffs, and test evidence</h3>"
        "<h4>Captured artifacts</h4>" + artifact_html
        + "<h4>Proposed diffs</h4>" + diff_html
        + "<h4>Test evidence</h4>" + test_html + "</section>"
    )


def _owner_id(run):
    owner = _get(run, "owner_id", "ltvm_owner_id", "session_owner_id")
    if owner:
        return str(owner)
    session = _get(run, "session", "worker_session")
    return _plain(_get(session, "owner_id", "ltvm_owner_id"), "")


def _vm_key(vm):
    return (_plain(_get(vm, "name", "vm_name", "id")), _plain(_get(vm, "owner_id", "owner"), ""))


def _owned_vms(run, supplied_vms):
    owner = _owner_id(run)
    if not owner:
        return []
    candidates = _items(supplied_vms) + _items(_get(run, "vms", "virtual_machines"))
    result = []
    seen = set()
    for vm in candidates:
        if _plain(_get(vm, "owner_id", "owner"), "") != owner:
            continue
        key = _vm_key(vm)
        if key not in seen:
            seen.add(key)
            result.append(vm)
    return result


def _render_vms(run, *, supplied_vms=(), suffix):
    vms = _owned_vms(run, supplied_vms)
    rows = []
    for vm in vms:
        rows.append(
            "<tr><th scope='row'>" + escape(_plain(_get(vm, "name", "vm_name", "id")))
            + "</th><td>" + escape(_human(_get(vm, "state", "status")))
            + "</td><td>" + escape(_plain(_get(vm, "topology", "role")))
            + "</td><td>" + escape(_format_bytes(_get(vm, "configured_guest_memory_bytes", "guest_memory_bytes", "memory_bytes")))
            + "<small>Guest capacity; not host usage.</small></td><td>"
            + escape(_format_bytes(_get(vm, "host_rss_bytes", "process_rss_bytes", "actual_host_rss_bytes")))
            + "<small>Measured host QEMU RSS.</small></td><td>"
            + escape(_human(_get(vm, "cleanup_state"))) + "</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6' class='empty'>No exactly owner-matched session VMs.</td></tr>")
    return (
        f"<section class='engineering-vms' aria-labelledby='engineering-vms-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='engineering-vms-title-{escape(suffix, quote=True)}'>Session-owned LTVM guests ({len(vms)})</h3>"
        "<p>Only VMs with the run's exact durable owner identifier are nested here.</p>"
        "<table><thead><tr><th scope='col'>VM</th><th scope='col'>State</th>"
        "<th scope='col'>Topology / role</th><th scope='col'>Configured guest memory</th>"
        "<th scope='col'>Actual host RSS</th><th scope='col'>Cleanup</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>"
    )


def _render_resource_state(run, suffix):
    exhaustion = _get(run, "resource_exhaustion", "exhaustion_report", default={})
    cooldown = _get(run, "cooldown", "vm_cooldown", default={})
    active = _state(_get(run, "state", "status")) == "resource_exhausted" or bool(exhaustion)
    if active:
        exhaustion_html = (
            "<div class='resource-exhaustion' role='alert'><h4>Resource exhaustion</h4><dl>"
            + _field("Error code", _get(exhaustion, "error_code", "code"), code=True)
            + _field("Failed LTVM operation", _get(exhaustion, "operation", "failed_operation"))
            + _field("Requested resources", _get(exhaustion, "requested_resources", "request_summary"))
            + _field("Bounded evidence", _get(exhaustion, "evidence", "summary", "message"))
            + "</dl><p>No automatic retry is performed.</p></div>"
        )
    else:
        exhaustion_html = "<p>No LTVM resource exhaustion reported.</p>"
    suppressed = _get(cooldown, "automation_suppressed", "suppressed")
    suppressed_text = "yes" if suppressed is True else "no" if suppressed is False else UNKNOWN
    return (
        f"<section class='resource-cooldown' aria-labelledby='resource-cooldown-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='resource-cooldown-title-{escape(suffix, quote=True)}'>Resource exhaustion and VM cooldown</h3>"
        + exhaustion_html + "<dl>"
        + _field("Cooldown state", _human(_get(cooldown, "state", "status")))
        + _field("Retry not before", _get(cooldown, "retry_not_before", "vm_retry_not_before"))
        + _field("Cooldown remaining", _format_duration(_get(cooldown, "remaining_seconds", "seconds_remaining")))
        + _field("Automatic VM-backed runs suppressed", suppressed_text)
        + _field("Consecutive exhaustion count", _get(cooldown, "attempt_count", "exhaustion_count"))
        + "</dl></section>"
    )


def _render_lifecycle_warnings(run, *, supplied_vms=(), suffix):
    warnings = []
    checkout = _get(run, "checkout", "source_checkout", default={})
    cleanup_state = _state(_get(checkout, "cleanup_state", default=_get(run, "cleanup_state")))
    if cleanup_state in {"cleanup_failed", "failed", "orphaned"}:
        warnings.append("Checkout cleanup requires operator attention.")
    quarantine = _get(run, "quarantine", "quarantine_state")
    quarantine_state = _state(_get(quarantine, "state", "status", default=quarantine))
    if quarantine_state not in {"unknown", "none", "not_quarantined", "released"}:
        reason = _get(quarantine, "reason", "message")
        warnings.append("Quarantined run resource: " + _plain(reason, quarantine_state))
    for vm in _owned_vms(run, supplied_vms):
        state = _state(_get(vm, "cleanup_state"))
        if state in {"cleanup_failed", "orphaned"}:
            warnings.append(
                f"VM {_plain(_get(vm, 'name', 'vm_name', 'id'))} is {_human(state).casefold()}."
            )
    warnings.extend(_plain(item) for item in _items(_get(run, "warnings", "lifecycle_warnings")))
    if not warnings:
        return "<p class='lifecycle-ok'>Cleanup, quarantine, and orphan warnings: none reported.</p>"
    return (
        f"<section class='lifecycle-warnings' role='alert' aria-labelledby='lifecycle-warnings-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='lifecycle-warnings-title-{escape(suffix, quote=True)}'>Cleanup, quarantine, or orphan warning</h3><ul>"
        + "".join(f"<li>{escape(item)}</li>" for item in warnings) + "</ul></section>"
    )


def _render_messages(run, messages, suffix):
    rows = []
    for message in _items(messages):
        rows.append(
            "<li><strong>" + escape(_human(_get(message, "author", "role", "sender")))
            + "</strong> · <time>" + escape(_plain(_get(message, "created_at", "timestamp", "time")))
            + "</time><p>" + escape(_plain(_get(message, "body", "message", "text")))
            + "</p><small>Delivery: " + escape(_human(_get(message, "delivery_state", "state", "status")))
            + "</small></li>"
        )
    return (
        f"<section class='engineering-messages' aria-labelledby='engineering-messages-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='engineering-messages-title-{escape(suffix, quote=True)}'>Operator conversation</h3>"
        + ("<ol>" + "".join(rows) + "</ol>" if rows else "<p class='empty'>No messages recorded.</p>")
        + "</section>"
    )


def _render_operator_form(run, *, base_url, csrf_token, idempotency_token, suffix):
    path = _run_path(run, base_url)
    field_id = "engineering-message-" + quote(_run_id(run), safe="")
    return (
        f"<section class='engineering-operator' aria-labelledby='engineering-operator-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='engineering-operator-title-{escape(suffix, quote=True)}'>Message or prod the worker</h3>"
        f"<form method='post' action='{escape(path + '/guidance', quote=True)}'>"
        + _hidden("run_id", _run_id(run)) + _hidden("expected_version", _get(run, "version", "run_version", default=0))
        + _hidden("csrf_token", csrf_token) + _hidden("idempotency_token", idempotency_token)
        + f"<label for='{escape(field_id, quote=True)}'>Operator message</label>"
        + f"<textarea id='{escape(field_id, quote=True)}' name='message' required></textarea>"
        + "<p id='engineering-delivery-help'>Send waits for the next safe turn boundary. "
        "Prod interrupts the current turn and delivers immediately.</p>"
        + "<button type='submit' name='delivery_mode' value='safe_boundary'>Send message</button> "
        + "<button type='submit' name='delivery_mode' value='interrupt_and_send'>Prod now</button>"
        + "</form></section>"
    )


def _validation_manifest(run, record):
    return _get(
        record, "manifest", "execution_manifest", "validation_manifest",
        default=_get(run, "manifest", "execution_manifest"),
    ) or {}


def _validation_commands(manifest):
    commands = _items(_get(manifest, "commands", "steps"))
    if commands:
        return commands
    return (
        _items(_get(manifest, "build_steps", "builds"))
        + _items(_get(manifest, "test_steps", "tests"))
    )


def _validation_target(record, manifest):
    projected = _project(record)
    target = None
    explicit = False
    for name in (
        "target", "guest_target", "execution_target", "target_id",
        "target_name",
    ):
        if isinstance(projected, Mapping) and name in projected:
            target = projected[name]
            explicit = True
            break
        try:
            target = getattr(projected, name)
        except (AttributeError, TypeError):
            continue
        explicit = True
        break
    if isinstance(target, Mapping) or (
        target is not None and not isinstance(target, (str, bytes, int, float, bool))
    ):
        return _plain(_get(target, "name", "target_id", "id", "label"))
    if target is not None and target != "":
        return _plain(target)
    # An explicitly supplied empty target is invalid.  Only an absent proposal
    # target may be inferred from a manifest whose steps unanimously name one.
    if explicit:
        return UNKNOWN
    targets = {
        _plain(_get(command, "execution_target", "target", "environment"), "")
        for command in _validation_commands(manifest)
    }
    targets.discard("")
    return next(iter(targets)) if len(targets) == 1 else UNKNOWN


def _validation_identity(run, record):
    manifest = _validation_manifest(run, record)
    return {
        "manifest": manifest,
        "manifest_id": _get(
            record, "manifest_id",
            default=_get(manifest, "manifest_id", "id"),
        ),
        "manifest_digest": _get(
            record, "manifest_digest", "digest",
            default=_get(manifest, "digest", "manifest_digest", "hash"),
        ),
        "revision_sha": _get(
            record, "revision_sha", "revision",
            default=_get(manifest, "revision_sha", "revision", default=_revision(run)),
        ),
        "owner_id": _get(
            record, "owner_id", "ltvm_owner_id", "session_owner_id",
            default=_get(manifest, "owner_id", "ltvm_owner_id", default=_owner_id(run)),
        ),
        "target": _validation_target(record, manifest),
    }


def _validation_identity_issues(run, identity):
    """Return reasons an identity cannot be trusted as this run's capability."""
    issues = []
    for label, key in (
        ("manifest ID", "manifest_id"),
        ("manifest digest", "manifest_digest"),
        ("exact revision", "revision_sha"),
        ("durable LTVM owner", "owner_id"),
        ("guest target", "target"),
    ):
        value = identity[key]
        if value is None or value == "" or value == UNKNOWN:
            issues.append(label)
    expected_revision = _revision(run)
    if expected_revision is None or expected_revision == "" or expected_revision == UNKNOWN:
        issues.append("engineering run exact revision")
    elif (
        identity["revision_sha"] is not None
        and identity["revision_sha"] != ""
        and identity["revision_sha"] != UNKNOWN
        and str(identity["revision_sha"]).casefold()
        != str(expected_revision).casefold()
    ):
        issues.append("revision matching the engineering run")
    expected_owner = _owner_id(run)
    if not expected_owner:
        issues.append("engineering run durable owner")
    elif (
        identity["owner_id"] is not None
        and identity["owner_id"] != ""
        and identity["owner_id"] != UNKNOWN
        and str(identity["owner_id"]) != expected_owner
    ):
        issues.append("owner matching the engineering session")
    return issues


def _argv_html(command):
    argv = _get(command, "argv", "arguments")
    text = _get(command, "text", "guest_text")
    if argv is None and text not in (None, ""):
        return (
            "<span class='guest-command-kind'>guest shell text:</span> "
            "<code class='guest-command-text'>" + escape(_plain(text)) + "</code>"
        )
    if isinstance(argv, (str, bytes, Mapping)):
        return "<span class='invalid-argv'>invalid argv representation</span>"
    try:
        values = list(argv)
    except TypeError:
        return "<span class='invalid-argv'>argv unavailable</span>"
    if not values:
        return "<span class='invalid-argv'>argv unavailable</span>"
    # Each argument is a separate escaped token.  This is audit data, never a
    # shell command string and never copied into a GET action.
    bounded = values[:128]
    rendered = " ".join(
        f"<code class='argv-token'>{escape(_plain(value))}</code>"
        for value in bounded
    )
    if len(values) > len(bounded):
        rendered += f" <span>{len(values) - len(bounded)} argument(s) omitted</span>"
    return rendered


def _render_validation_plan(identity, *, heading="Immutable validation manifest"):
    manifest = identity["manifest"]
    commands = _validation_commands(manifest)
    if commands:
        rows = []
        for command in commands[:64]:
            label = _get(command, "label", "name", "step_id", "id", default="Validation step")
            rows.append(
                "<li><strong>" + escape(_plain(label)) + "</strong><dl>"
                + _field("Step ID", _get(command, "step_id", "id"), code=True)
                + _field("Guest target", _get(command, "execution_target", "target", "environment"))
                + _field("Guest working directory", _get(command, "cwd", "working_directory"), code=True)
                + _field("Timeout", _format_duration(_get(command, "timeout_seconds")))
                + "</dl><p><strong>Planned exec argv:</strong> "
                + _argv_html(command) + "</p></li>"
            )
        omitted = len(commands) - len(rows)
        command_html = "<ol class='validation-plan-steps'>" + "".join(rows) + "</ol>"
        if omitted:
            command_html += f"<p>{omitted} step(s) omitted from this bounded view.</p>"
    else:
        command_html = "<p class='empty'>No validation steps are recorded.</p>"
    return (
        f"<h4>{escape(heading)}</h4><p>The frozen manifest records the proposed "
        "plan and later evidence. It does not constrain the approved session to "
        "only these commands; every actual guest exec remains audited below.</p><dl>"
        + _field("Manifest ID", identity["manifest_id"], code=True)
        + _field("Manifest digest", identity["manifest_digest"], code=True)
        + _field("Exact pinned revision", identity["revision_sha"], code=True)
        + _field("Exact LTVM owner", identity["owner_id"], code=True)
        + _field("Requested guest target", identity["target"], code=True)
        + "</dl>" + command_html
    )


def _validation_missing(run, record, identity):
    missing = _validation_identity_issues(run, identity)
    if not _validation_commands(identity["manifest"]):
        missing.append("at least one manifest plan step")
    if _get(record, "validation_eligible", "eligible") is False:
        missing.append(_plain(_get(record, "disabled_reason", "ineligible_reason"), "controller eligibility"))
    return missing


def _validation_route(run, base_url, suffix):
    return _run_path(run, base_url) + "/validation/" + suffix


def render_validation_proposal(
    run, proposal=None, *, base_url="/engineering-runs", action=None,
    csrf_token=None, idempotency_token=None,
):
    """Render the inert Phase 3B proposal and its prepare-only POST.

    The prepare endpoint may freeze/sign the proposal and redirect to a GET
    confirmation.  It must not execute guest commands.
    """
    proposal = proposal or {}
    identity = _validation_identity(run, proposal)
    missing = _validation_missing(run, proposal, identity)
    disabled = " disabled aria-disabled='true'" if missing else ""
    target_action = _safe_base_url(
        action or _validation_route(run, base_url, "prepare")
    )
    reason_html = ""
    title_id = "validation-proposal-title-" + quote(_run_id(run), safe="")
    if missing:
        reason_html = (
            "<div class='validation-unavailable' role='status'><strong>Validation "
            "approval unavailable:</strong><ul>"
            + "".join(f"<li>{escape(item)}</li>" for item in missing)
            + "</ul></div>"
        )
    return (
        "<section class='validation-proposal' aria-labelledby='"
        + escape(title_id, quote=True) + "'>"
        + "<h3 id='" + escape(title_id, quote=True)
        + "'>Proposed session-owned LTVM validation</h3>"
        "<p>No build or test has started. Preparing this proposal only opens a "
        "display-only confirmation page. Explicit operator approval is required "
        "before the controller grants guest execution.</p>"
        + _render_validation_plan(identity)
        + "<p><strong>Capability boundary:</strong> approval permits open-ended, "
        "brokered command execution only inside LTVM guests carrying the exact "
        "owner shown above. Host execution and Gerrit upload remain disabled.</p>"
        + f"<form method='post' action='{escape(target_action, quote=True)}'>"
        + _hidden("run_id", _run_id(run))
        + _hidden("manifest_id", identity["manifest_id"])
        + _hidden("manifest_digest", identity["manifest_digest"])
        + _hidden("revision_sha", identity["revision_sha"])
        + _hidden("owner_id", identity["owner_id"])
        + _hidden("target", identity["target"])
        + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit'{disabled}>Review validation capability</button></form>"
        + reason_html + "</section>"
    )


def render_validation_confirmation(
    run, proposal, *, confirmation_token, intent="execute",
    confirmation_expires_at=None, csrf_token=None, idempotency_token=None,
    base_url="/engineering-runs", action=None,
):
    """Render the display-only GET confirmation whose final action is POST."""
    normalized = _state(intent)
    if normalized not in {"execute", "retry"}:
        raise ValueError("validation intent must be execute or retry")
    if not confirmation_token:
        raise ValueError("a confirmation token is required")
    identity = _validation_identity(run, proposal)
    missing = _validation_missing(run, proposal, identity)
    if missing:
        raise ValueError("validation proposal lacks an exact immutable identity")
    route = action or _validation_route(
        run, base_url, "execute" if normalized == "execute" else "retry"
    )
    target_action = _safe_base_url(route)
    if normalized == "retry":
        title = "Confirm a new validation attempt"
        button = "Approve new validation attempt"
        warning = (
            "This starts a new attempt against the same immutable proposal; it "
            "does not resume or rewrite the previous attempt."
        )
    else:
        title = "Confirm session-owned LTVM validation"
        button = "Approve validation capability"
        warning = (
            "This grants the engineering run an open-ended, brokered guest-command "
            "capability inside resources carrying the exact owner below."
        )
    return (
        "<main class='validation-confirmation'>"
        f"<h2>{escape(title)}</h2><p role='alert'>{escape(warning)}</p>"
        + _render_validation_plan(identity, heading="Immutable proposal being approved")
        + "<p><strong>Not granted:</strong> host command execution, unrelated or "
        "unowned VM access, and Gerrit upload remain disabled.</p>"
        + f"<form method='post' action='{escape(target_action, quote=True)}'>"
        + _hidden("intent", normalized)
        + _hidden("run_id", _run_id(run))
        + _hidden("manifest_id", identity["manifest_id"])
        + _hidden("manifest_digest", identity["manifest_digest"])
        + _hidden("revision_sha", identity["revision_sha"])
        + _hidden("owner_id", identity["owner_id"])
        + _hidden("target", identity["target"])
        + _hidden("confirmation_token", confirmation_token)
        + _hidden("confirmation_expires_at", confirmation_expires_at)
        + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit'>{escape(button)}</button> "
        + f"<a href='{escape(_run_path(run, base_url), quote=True)}'>Do not grant validation</a>"
        + "</form></main>"
    )


def _render_validation_audit(validation, *, expected_owner, identity_trusted):
    audits = _items(
        _get(validation, "command_audits", "audit_history", "command_history")
    )
    if not audits:
        return "<p class='empty'>No guest command audit records.</p>"
    rows = []
    for audit in audits[:100]:
        observed_owner = _get(audit, "owner_id", "observed_owner_id")
        owner_matches = (
            identity_trusted and observed_owner is not None
            and observed_owner != "" and observed_owner != UNKNOWN
            and str(observed_owner) == str(expected_owner)
        )
        audit_warning = ""
        if not owner_matches:
            audit_warning = (
                "<p class='validation-audit-warning' role='alert'>This audit "
                "record is unverified because its owner binding is missing or "
                "does not match the engineering run.</p>"
            )
        rows.append(
            "<li><header><strong>" + escape(_plain(_get(audit, "label", "step_id", "audit_id")))
            + "</strong> · " + escape(_human(_get(audit, "state", "status", "outcome")))
            + "</header><dl>"
            + _field("Guest", _get(audit, "guest", "vm_name", "target"), code=True)
            + _field("Owner observed", observed_owner, code=True)
            + _field("Started", _get(audit, "started_at", "created_at"))
            + _field("Finished", _get(audit, "finished_at", "completed_at"))
            + _field("Exit status", _get(audit, "exit_status", "exit_code"))
            + _field("Duration", _format_duration(_get(audit, "duration_seconds", "elapsed_seconds")))
            + "</dl>" + audit_warning
            + "<p><strong>Executed argv:</strong> " + _argv_html(audit) + "</p></li>"
        )
    omitted = len(audits) - len(rows)
    note = f"<p>{omitted} older audit record(s) omitted.</p>" if omitted else ""
    return note + "<ol class='validation-command-audit'>" + "".join(rows) + "</ol>"


def render_validation_status(
    run, validation, *, base_url="/engineering-runs",
):
    """Render immutable identity, guest command audit, results, and cleanup state."""
    validation = validation or {}
    identity = _validation_identity(run, validation)
    identity_issues = _validation_identity_issues(run, identity)
    identity_trusted = not identity_issues
    state = _get(validation, "state", "status", "outcome")
    results = _items(_get(validation, "results", "test_results", "step_results"))
    artifacts = _items(_get(validation, "artifacts", "result_artifacts"))
    result_rows = []
    for result in results[:100]:
        result_rows.append(
            "<li><strong>" + escape(_plain(_get(result, "label", "name", "step_id")))
            + "</strong> · " + escape(_human(_get(result, "state", "status", "outcome")))
            + " · exit status " + escape(_plain(_get(result, "exit_status", "exit_code")))
            + " · " + escape(_format_duration(_get(result, "duration_seconds", "elapsed_seconds")))
            + _artifact_link(run, result, base_url=base_url) + "</li>"
        )
    result_html = (
        "<ul>" + "".join(result_rows) + "</ul>" if result_rows
        else "<p class='empty'>No validation results recorded.</p>"
    )
    artifact_html = (
        "<ul>" + "".join(
            _artifact_row(run, artifact, base_url=base_url, kind="Validation")
            for artifact in artifacts[:100]
        ) + "</ul>" if artifacts
        else "<p class='empty'>No validation artifacts recorded.</p>"
    )
    cleanup = _get(validation, "cleanup", "cleanup_status", default={})
    cleanup_state = _get(
        cleanup, "state", "status", default=_get(validation, "cleanup_state")
    )
    cleanup_error = _get(
        cleanup, "error", "failure_summary", "message",
        default=_get(validation, "cleanup_error"),
    )
    normalized = _state(state)
    run_state = _state(_get(run, "state", "status"))
    reported_approval = _human(_get(
        validation, "approval_state", "authorization_state"
    ))
    effective_active = (
        identity_trusted and run_state in ACTIVE_STATES
        and _state(reported_approval) == "approved"
        and normalized in {"claimed", "running", "active"}
    )
    retry_html = ""
    if normalized in {
        "failed", "cancelled", "resource_exhausted", "cleanup_failed",
    }:
        retry_html = (
            "<p>Use the engineering run controls to review and confirm a new "
            "isolated run. This guest capability cannot silently retry itself.</p>"
        )
    suffix = "validation-status-" + quote(_run_id(run), safe="")
    if effective_active:
        identity_warning = ""
        approval_label = "Approval state"
        approval_value = reported_approval
        boundary = (
            "<p><strong>Execution boundary:</strong> one open-ended guest-command "
            "capability is brokered and audited inside exact-owner LTVM resources. "
            "It is not per-command approval. Host execution and Gerrit upload are "
            "disabled.</p>"
        )
    elif not identity_trusted:
        identity_warning = (
            "<div class='validation-identity-warning' role='alert'><strong>Validation "
            "identity not verified; capability treated as inactive.</strong><ul>"
            + "".join(f"<li>{escape(item)}</li>" for item in identity_issues)
            + "</ul></div>"
        )
        approval_label = "Reported approval state"
        approval_value = reported_approval
        boundary = (
            "<p><strong>Execution boundary:</strong> this status does not establish "
            "an active guest capability because its exact run identity is unverified. "
            "Host execution and Gerrit upload remain disabled.</p>"
        )
    else:
        identity_warning = ""
        approval_label = "Approval state"
        approval_value = reported_approval
        boundary = (
            "<p><strong>Execution boundary:</strong> the exact run identity is "
            "verified, but no active approved guest capability is reported. Host "
            "execution and Gerrit upload are disabled.</p>"
        )
    return (
        f"<section class='validation-status' aria-labelledby='{escape(suffix, quote=True)}'>"
        f"<h3 id='{escape(suffix, quote=True)}'>Session-owned LTVM validation</h3>"
        + _status_badge(state, noun="Validation") + identity_warning + "<dl>"
        + _field("Validation attempt", _get(validation, "validation_id", "attempt_id", "execution_id"), code=True)
        + _field(approval_label, approval_value)
        + _field("Effective guest capability", "active" if effective_active else "inactive")
        + _field("Approved by", _get(validation, "approved_by", "authorized_by"))
        + _field("Approved at", _get(validation, "approved_at", "authorized_at"))
        + _field("Manifest ID", identity["manifest_id"], code=True)
        + _field("Manifest digest", identity["manifest_digest"], code=True)
        + _field("Exact pinned revision", identity["revision_sha"], code=True)
        + _field("Exact LTVM owner", identity["owner_id"], code=True)
        + _field("Guest target", identity["target"], code=True)
        + "</dl>" + boundary + "<h4>Guest command audit</h4>"
        + _render_validation_audit(
            validation, expected_owner=identity["owner_id"],
            identity_trusted=identity_trusted,
        )
        + "<h4>Results</h4>" + result_html
        + "<h4>Artifacts</h4>" + artifact_html
        + _render_resource_state(validation, suffix)
        + "<section class='validation-cleanup'><h4>Validation cleanup</h4><dl>"
        + _field("Cleanup state", _human(cleanup_state))
        + _field("Cleanup error", cleanup_error)
        + _field("Quarantine state", _human(_get(validation, "quarantine_state", default=_get(cleanup, "quarantine_state"))))
        + _field("Owned resources remaining", _get(cleanup, "owned_resources_remaining", "remaining_count"))
        + "</dl></section>" + retry_html + "</section>"
    )


def _render_control_links(run, *, base_url):
    state = _state(_get(run, "state", "status"))
    path = _run_path(run, base_url)
    links = []
    if state in ACTIVE_STATES:
        links.extend((("cancel", "Stop and cancel"), ("kill", "Kill session")))
    if state in TERMINAL_STATES:
        label = "Retry now as a new run" if state == "resource_exhausted" else "Retry as a new run"
        links.append(("retry", label))
    if not links:
        return "<p class='controls-unavailable'>No run controls are available in this state.</p>"
    return (
        "<nav class='engineering-controls' aria-label='Engineering run controls'><ul>"
        + "".join(
            "<li><a href='" + escape(path + "/confirm?" + urlencode({"intent": intent}), quote=True)
            + "'>" + escape(label) + "</a></li>" for intent, label in links
        ) + "</ul><p>Each link opens a display-only confirmation page. It does not mutate the run.</p></nav>"
    )


def render_engineering_run(
    run, *, vms=(), messages=None, base_url="/engineering-runs",
    csrf_token=None, idempotency_token=None,
):
    """Render one Phase 3 run and all resources owned by its session."""
    path = _run_path(run, base_url)
    title_id = "engineering-run-" + quote(_run_id(run), safe="")
    suffix = quote(_run_id(run), safe="")
    messages = _get(run, "messages", default=[]) if messages is None else messages
    validation = _get(
        run, "validation", "validation_execution", "validation_status"
    )
    validation_proposal = _get(
        run, "validation_proposal", "pending_validation", "validation_request"
    )
    if validation is not None:
        validation_html = render_validation_status(
            run, validation, base_url=base_url
        )
    elif validation_proposal is not None:
        validation_html = render_validation_proposal(
            run,
            validation_proposal,
            base_url=base_url,
            csrf_token=csrf_token,
            idempotency_token=idempotency_token,
        )
    else:
        validation_html = ""
    return (
        f"<article class='engineering-run' aria-labelledby='{escape(title_id, quote=True)}'>"
        f"<header><h2 id='{escape(title_id, quote=True)}'>Engineering run "
        f"<code>{escape(_run_id(run))}</code></h2>{_status_badge(_get(run, 'state', 'status'))}</header>"
        "<dl class='engineering-run-summary'>"
        + _field("Patch", _get(run, "subject", "patch_subject", "patch_id", "change_number"))
        + _field("Session", _get(run, "session_id", "worker_session_id"), code=True)
        + _field("Exact pinned revision", _revision(run), code=True)
        + _field("Current step", _get(run, "current_step", "step"))
        + _field("Started", _get(run, "started_at", "created_at"))
        + "</dl><p><a href='" + escape(path, quote=True) + "'>Permalink to this run</a></p>"
        + _capability_status(suffix, run) + _render_checkout(run, suffix) + _render_manifest(run, suffix)
        + _render_artifacts(run, base_url=base_url, suffix=suffix)
        + validation_html
        + _render_vms(run, supplied_vms=vms, suffix=suffix) + _render_resource_state(run, suffix)
        + _render_lifecycle_warnings(run, supplied_vms=vms, suffix=suffix)
        + _render_messages(run, messages, suffix)
        + _render_operator_form(run, base_url=base_url, csrf_token=csrf_token, idempotency_token=idempotency_token, suffix=suffix)
        + _render_control_links(run, base_url=base_url) + "</article>"
    )


def _unmatched_vms(runs, vms):
    owners = {_owner_id(run) for run in runs if _owner_id(run)}
    return [vm for vm in vms if _plain(_get(vm, "owner_id", "owner"), "") not in owners]


def render_engineering_dashboard(
    runs, *, vms=(), messages_by_run=None, base_url="/engineering-runs",
    csrf_token=None, idempotency_token=None,
):
    """Render all engineering runs plus unmatched/orphan inventory warnings."""
    run_items = _items(runs)
    vm_items = _items(vms)
    messages_by_run = messages_by_run or {}
    cards = []
    for run in run_items:
        run_messages = (
            messages_by_run.get(_run_id(run))
            if isinstance(messages_by_run, Mapping) else None
        )
        cards.append(render_engineering_run(
            run, vms=vm_items, messages=run_messages, base_url=base_url,
            csrf_token=csrf_token, idempotency_token=idempotency_token,
        ))
    unmatched = _unmatched_vms(run_items, vm_items)
    if unmatched:
        orphan_html = (
            "<section class='orphan-vms' role='alert' aria-labelledby='orphan-vms-title'>"
            f"<h2 id='orphan-vms-title'>Unmatched or orphan LTVM resources ({len(unmatched)})</h2>"
            "<p>These VMs are not adopted or made mutable because their durable owner "
            "does not exactly match a displayed healthy run.</p><ul>"
            + "".join(
                "<li><strong>" + escape(_plain(_get(vm, "name", "vm_name", "id")))
                + "</strong> · owner <code>" + escape(_plain(_get(vm, "owner_id", "owner")))
                + "</code> · cleanup " + escape(_human(_get(vm, "cleanup_state")))
                + "</li>" for vm in unmatched
            ) + "</ul></section>"
        )
    else:
        orphan_html = "<p class='orphan-ok'>Unmatched or orphan LTVM resources: none reported.</p>"
    empty = "<p class='empty'>No engineering runs.</p>" if not cards else ""
    return (
        "<main class='engineering-dashboard'><h1>Controlled engineering runs</h1>"
        + _capability_status("dashboard") + orphan_html + empty + "".join(cards) + "</main>"
    )


def render_engineering_start_control(
    patch, *, action="/engineering-runs/prepare", csrf_token=None,
    idempotency_token=None,
):
    """Render the first POST in the controller-owned start confirmation flow."""
    revision = _revision(patch)
    eligible = bool(_get(patch, "engineering_eligible", "eligible", default=False))
    reason = _get(patch, "engineering_disabled_reason", "disabled_reason")
    if not revision:
        eligible = False
        reason = reason or "The exact revision is unavailable."
    disabled = "" if eligible else " disabled aria-disabled='true'"
    return (
        "<section class='engineering-start' aria-labelledby='engineering-start-title'>"
        "<h3 id='engineering-start-title'>Start controlled engineering run</h3>"
        "<p>This prepares a dedicated full checkout at the exact revision and "
        "opens a display-only confirmation page before any worker starts.</p>"
        + _capability_status("start")
        + f"<p><strong>Exact pinned revision:</strong> <code>{escape(_plain(revision))}</code></p>"
        + f"<form method='post' action='{escape(_safe_base_url(action), quote=True)}'>"
        + _hidden("change_number", _get(patch, "change_number", "change", "id"))
        + _hidden("patchset", _get(patch, "patchset", "patch_set", "patchset_number"))
        + _hidden("revision_sha", revision) + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit'{disabled}>Prepare engineering run</button></form>"
        + (f"<p role='status'>{escape(_plain(reason))}</p>" if not eligible else "")
        + "</section>"
    )


def render_engineering_start_confirmation(
    request, *, confirmation_token, csrf_token=None, idempotency_token=None,
    confirmation_expires_at=None, action="/engineering-runs/start",
):
    """Render the GET confirmation page whose only mutation is a final POST."""
    if not confirmation_token:
        raise ValueError("a confirmation token is required")
    revision = _revision(request)
    if not revision:
        raise ValueError("an exact pinned revision is required")
    return (
        "<main class='engineering-start-confirmation'>"
        "<h2>Confirm controlled engineering run</h2>"
        "<p role='alert'>Starting creates a dedicated writable checkout and permits "
        "isolated builds and tests. Session-owned VMs may be created on demand.</p>"
        + _capability_status("start-confirmation")
        + f"<p>Patch <strong>{escape(_plain(_get(request, 'subject', 'patch_id', 'change_number')))}</strong> "
        + f"at exact pinned revision <code>{escape(_plain(revision))}</code>.</p>"
        + f"<form method='post' action='{escape(_safe_base_url(action), quote=True)}'>"
        + _hidden("change_number", _get(request, "change_number", "change", "id"))
        + _hidden("patchset", _get(request, "patchset", "patch_set", "patchset_number"))
        + _hidden("revision_sha", revision)
        + _hidden("confirmation_token", confirmation_token)
        + _hidden("confirmation_expires_at", confirmation_expires_at)
        + _hidden("csrf_token", csrf_token) + _hidden("idempotency_token", idempotency_token)
        + "<button type='submit'>Start engineering run</button></form></main>"
    )


def render_engineering_confirmation(
    run, intent, *, confirmation_token, csrf_token=None,
    idempotency_token=None, confirmation_expires_at=None,
    base_url="/engineering-runs",
):
    """Render the token-bound final POST for cancel, kill, or retry."""
    normalized = _state(intent)
    if normalized not in CONTROL_INTENTS:
        raise ValueError("intent must be cancel, kill, or retry")
    if not confirmation_token:
        raise ValueError("a confirmation token is required")
    path = _run_path(run, base_url)
    copy = {
        "cancel": (
            "Confirm stop and cancel",
            "Requests an orderly stop, captures evidence, then begins owner-scoped cleanup.",
            "Stop and cancel",
        ),
        "kill": (
            "Confirm kill session",
            "Forcibly stops the worker, captures available evidence, then begins owner-scoped cleanup.",
            "Kill session",
        ),
        "retry": (
            "Confirm retry as a new run",
            "Starts a new isolated run; it does not revive this checkout, session, or its VMs.",
            "Retry as a new run",
        ),
    }[normalized]
    title, warning, button = copy
    return (
        "<main class='engineering-confirmation'>"
        f"<h2>{escape(title)}</h2><p role='alert'>{escape(warning)}</p>"
        f"<p>Run <code>{escape(_run_id(run))}</code> · exact pinned revision "
        f"<code>{escape(_plain(_revision(run)))}</code></p>"
        + (_capability_status("retry-confirmation") if normalized == "retry" else "")
        + f"<form method='post' action='{escape(path + '/' + normalized, quote=True)}'>"
        + _hidden("intent", normalized)
        + _hidden("expected_version", _get(run, "version", "run_version", default=0))
        + _hidden("confirmation_token", confirmation_token)
        + _hidden("confirmation_expires_at", confirmation_expires_at)
        + _hidden("csrf_token", csrf_token) + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit' class='danger'>{escape(button)}</button> "
        + f"<a href='{escape(path, quote=True)}'>Keep current state</a></form></main>"
    )


__all__ = [
    "render_engineering_confirmation",
    "render_engineering_dashboard",
    "render_engineering_run",
    "render_engineering_start_confirmation",
    "render_engineering_start_control",
    "render_validation_confirmation",
    "render_validation_proposal",
    "render_validation_status",
]
