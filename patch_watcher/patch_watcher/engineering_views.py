"""Side-effect-free HTML views for engineering runs.

The views accept mappings, dataclasses, or other attribute objects and have no
controller or persistence dependencies.  The run summary deliberately
renders only a small manifest allowlist.  The guest-capability section separately renders
executed argv as escaped, tokenized audit data; environment values and
arbitrary artifact URLs never cross this display boundary.

Route contract used by the renderers:

* ``POST /engineering-runs/prepare`` validates a requested start and redirects
  to a display-only confirmation page owned by the controller.
* that confirmation page uses :func:`render_engineering_start_confirmation`
  and submits ``POST /engineering-runs/start`` with controller-issued tokens;
* run detail controls link to ``.../confirm?intent=...`` display pages; and
* only confirmation pages submit cancel, kill, or retry mutations.

Guest execution is granted by the existing engineering-run start
confirmation.  The immutable manifest records optional intent and evidence;
it is not a per-command allowlist.
"""

import math
from collections.abc import Mapping
from html import escape
from urllib.parse import quote, urlencode, urlsplit

# `FULL_CAPABILITY_PROFILES` is shared with `run_views`, which writes the
# boundary statement at the top of the same page: one definition, so the two
# can never disagree about what a run is allowed to do.
try:  # Support both package and direct-module test imports.
    from .ltvm_resources import (
        PATCH_WATCHER_OWNER_PREFIX,
        checkout_vm_prefix,
    )
    from .run_views import FULL_CAPABILITY_PROFILES
except ImportError:  # pragma: no cover - direct execution convenience
    from patch_watcher.ltvm_resources import (  # type: ignore
        PATCH_WATCHER_OWNER_PREFIX,
        checkout_vm_prefix,
    )
    from patch_watcher.run_views import FULL_CAPABILITY_PROFILES  # type: ignore

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
# States a recorded ``pw_owned_resource`` row can be left in that mean an
# operator still has to do something.  Warnings used to be derived only from
# the live LTVM inventory, so a resource whose cleanup failed -- or whose guest
# is already gone -- reported a clean bill of health.
UNCLEAN_RESOURCE_STATES = {
    "cleanup_failed", "failed", "orphaned", "abandoned", "quarantined",
}
# The only ``pw_checkout_allocation`` state that means a human has to act.  Its
# CHECK constraint allows exactly planned, allocated, active, cleanup_pending,
# released and quarantined; the warning used to test for cleanup_failed,
# failed and orphaned, which intersect that set nowhere, so it could never
# fire -- while `quarantined`, the state that pins a pool checkout out of
# circulation until someone reviews it, went unmentioned.
ATTENTION_CHECKOUT_STATES = {"quarantined"}


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


def render_capability_status(suffix="dashboard", run=None, *, standing=False):
    """State the execution boundary, for one run or as a standing declaration.

    With no run this is the declaration the index page carries: capabilities
    exist but nothing has activated them.  It used to head the engineering-run
    card; that card is gone and the statement moved to the Runs card, which is
    the honest place for it -- it was never about any one run.
    """
    return _capability_status(suffix, run, standing=standing)


def _capability_status(suffix="default", run=None, *, standing=False):
    """Render the execution boundary without overstating an active grant."""
    title_id = "engineering-capabilities-title-" + quote(str(suffix), safe="")
    # A run minted with the `read_only` capability profile gets Read/Glob/Grep,
    # `--safe-mode --restricted`, and scrubbed credentials. Claiming a host
    # shell and real Gerrit credentials for it would contradict the boundary
    # statement `render_run_detail` puts at the top of the very same page.
    # An absent field is not a claim either way, so it leaves the wording
    # alone: `_state` turns a missing value into "unknown", which would
    # otherwise have read as "not full" and downgraded every caller that does
    # not project a capability profile.
    declared = _get(run, "capability_profile", "capabilities") if run else None
    read_only = bool(declared) and _state(declared) not in FULL_CAPABILITY_PROFILES
    if read_only:
        host_status = (
            "restricted; this run was started read-only with Read, Glob and "
            "Grep, and no host shell"
        )
        upload_status = "unavailable; service credentials are scrubbed from this run"
    else:
        host_status = (
            "available; the run has a host shell, the installed LLM tools, and ltvm"
        )
        upload_status = (
            "available with real credentials; this run is asked to produce a diff "
            "for review, not to upload"
        )
    if standing:
        # On a page that merely lists runs, "available" reads as a grant that
        # is live right now.  A confirmation page keeps the unhedged wording:
        # there the operator is about to authorise exactly these capabilities,
        # and hedging what they are consenting to would be the worse error.
        host_status = (
            "declared; an engineering run gets a host shell, the installed LLM "
            "tools, and ltvm"
        )
        upload_status = (
            "declared; an engineering run carries real service credentials"
        )
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
                "active as one open-ended capability, recorded for exact-owner session LTVM guests"
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
        f"<li class='{'capability-gated' if read_only else 'capability-enabled'}'>"
        "<strong>Host command execution:</strong> " + escape(host_status) + "</li>"
        f"<li class='{'capability-gated' if read_only else 'capability-enabled'}'>"
        "<strong>Gerrit upload:</strong> " + escape(upload_status) + "</li>"
        "</ul><p>The engineering-run approval records an open-ended guest-execution "
        "capability inside the exact owner boundary; it is not approval of each individual "
        "command. What the run does beyond that is set by its prompt and its own checkout, "
        "not by a capability grant: any write it makes is real.</p></section>"
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
        # "Isolation profile: session-owned-ltvm" and "Network profile:
        # controller-mediated" were hardcoded constants describing a
        # mediation layer that was removed in the carve-down. An engineering
        # run has no isolation profile and no network mediation: it runs on
        # this host with bypassPermissions, the ambient environment and real
        # credentials. Two rows asserting otherwise on the operator's page
        # were a safety claim, not a label.
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


def _vm_name(vm):
    """Return a VM's name. ``name`` is the only key the sampler writes."""
    return _plain(_get(vm, "name"), "")


def _vm_prefix(run):
    """Return the ``co<N>-`` guest name prefix this run's checkout reserves.

    Ownership is established by this prefix, not by a stamped owner id:
    ``ltvm list --json`` has no patch-watcher owner field, so every sampled
    guest carries ``owner_id`` of ``None`` or LTVM's own ``pid:<n>``, and
    `RunController._register_ltvm_observations` finds a run's guests by the
    reserved prefix of the pool checkout it holds.
    """
    explicit = _get(run, "vm_prefix", "checkout_vm_prefix")
    if explicit:
        return str(explicit)
    index = _get(run, "checkout_index")
    if index is None:
        checkout = _get(run, "checkout", "source_checkout", default={})
        index = _get(checkout, "checkout_index", "index")
    try:
        return checkout_vm_prefix(int(index))
    except (TypeError, ValueError):
        return ""


def _recorded_vm_names(run):
    """Return the guest names this run durably recorded as its own."""
    return {
        _resource_label(resource)
        for resource in _owned_resources(run)
        if _state(_get(resource, "resource_type", "type")).startswith("ltvm")
    }


def _vm_is_owned(vm, *, owner, prefix, recorded):
    """Mirror `ltvm_resources.reconcile_session_resources`'s claim rule.

    An exact owner id always wins. Failing that a prefix claim is allowed only
    when the row declares no other owner: the name says the guest is ours while
    a foreign owner id says it is not, and that ambiguity must not be resolved
    by guessing.
    """
    vm_owner = _plain(_get(vm, "owner_id", "owner"), "")
    if owner and vm_owner == owner:
        return True
    if vm_owner:
        return False
    name = _vm_name(vm)
    if not name:
        return False
    return bool(prefix and name.startswith(prefix)) or name in recorded


def _owned_vms(run, supplied_vms):
    owner = _owner_id(run)
    prefix = _vm_prefix(run)
    recorded = _recorded_vm_names(run)
    candidates = _items(supplied_vms) + _items(_get(run, "vms", "virtual_machines"))
    result = []
    seen = set()
    for vm in candidates:
        if not _vm_is_owned(vm, owner=owner, prefix=prefix, recorded=recorded):
            continue
        name = _vm_name(vm)
        if name not in seen:
            seen.add(name)
            result.append(vm)
    return result


def _recorded_vm_states(run):
    """Map guest name to the durable cleanup row the controller keeps for it.

    Cleanup state is recorded in ``pw_owned_resource``; the LTVM sample carries
    no such field, so reading ``cleanup_state`` off an inventory row could only
    ever print "Unknown".
    """
    return {
        _resource_label(resource): resource
        for resource in _owned_resources(run)
        if _state(_get(resource, "resource_type", "type")).startswith("ltvm")
    }


def _render_vms(run, *, supplied_vms=(), suffix):
    vms = _owned_vms(run, supplied_vms)
    recorded = _recorded_vm_states(run)
    prefix = _vm_prefix(run)
    rows = []
    for vm in vms:
        resource = recorded.get(_vm_name(vm))
        cleanup = (
            _human(_get(resource, "state", "cleanup_state")) if resource is not None
            else "Not recorded yet"
        )
        rows.append(
            "<tr><th scope='row'>" + escape(_vm_name(vm) or UNKNOWN)
            + "</th><td>" + escape(_human(_get(vm, "state")))
            + "</td><td>" + escape(_plain(_get(vm, "vcpus")))
            + "</td><td>" + escape(_plain(_get(vm, "ip")))
            + "</td><td>" + escape(_format_bytes(_get(vm, "configured_guest_memory_bytes")))
            + "<small>Guest capacity; not host usage.</small></td><td>"
            + escape(_format_bytes(_get(vm, "host_rss_bytes")))
            + "<small>" + escape(_plain(_get(vm, "host_memory_source"), "Not measured"))
            + "</small></td><td>" + escape(cleanup) + "</td></tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='7' class='empty'>No LTVM guests in this run's "
            "name prefix.</td></tr>"
        )
    scope = (
        f"Guests named <code>{escape(prefix)}&lt;role&gt;</code>, the prefix this "
        "run's pool checkout reserves, plus any guest carrying its exact owner "
        "identifier." if prefix else
        "This run holds no pool checkout, so it reserves no guest name prefix "
        "and owns no guests."
    )
    return (
        f"<section class='engineering-vms' aria-labelledby='engineering-vms-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='engineering-vms-title-{escape(suffix, quote=True)}'>Session-owned LTVM guests ({len(vms)})</h3>"
        f"<p>{scope}</p>"
        "<table><thead><tr><th scope='col'>VM</th><th scope='col'>State</th>"
        "<th scope='col'>vCPUs</th><th scope='col'>IP address</th>"
        "<th scope='col'>Configured guest memory</th>"
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
        + "</dl>"
        # Say what the operator can do, because the answer is "nothing here".
        # The panel reports a deadline and a countdown, which reads as though
        # there were a control to shorten it; there is not, and an operator
        # looking for one finds nothing and cannot tell whether they are
        # missing it. The cooldown lifts itself; what shortens it is freeing
        # LTVM capacity, which happens outside this page.
        "<p class='detail'>This cooldown lifts on its own at the time above. "
        "There is no override: it exists because the host could not give a "
        "run the guest capacity it asked for, so the way to shorten it is to "
        "free VM slots, disk, or memory.</p>"
        + "</section>"
    )


def _owned_resources(run):
    """Return the durable ``pw_owned_resource`` rows recorded for this run."""
    return _items(_get(run, "owned_resources", "resources"))


def _resource_label(resource):
    return _plain(_get(resource, "external_id", "name", "resource_id"))


def _unclean_resources(run):
    """Return recorded resources whose cleanup did not complete."""
    return [
        resource for resource in _owned_resources(run)
        if _state(_get(resource, "state", "cleanup_state"))
        in UNCLEAN_RESOURCE_STATES
    ]


def _resource_warning(resource):
    state = _state(_get(resource, "state", "cleanup_state"))
    kind = _plain(_get(resource, "resource_type", "type"), "resource")
    reason = _get(
        resource, "cleanup_failure", "failure_summary", "reason", "error",
    )
    return (
        f"Recorded resource {_resource_label(resource)} ({kind}) is in state "
        f"{state}: " + _plain(reason, "no reason was recorded")
    )


def _render_lifecycle_warnings(run, *, supplied_vms=(), suffix):
    warnings = [
        _resource_warning(resource) for resource in _unclean_resources(run)
    ]
    checkout = _get(run, "checkout", "source_checkout", default={})
    cleanup_state = _state(_get(checkout, "cleanup_state", default=_get(run, "cleanup_state")))
    if cleanup_state in ATTENTION_CHECKOUT_STATES:
        warnings.append(
            "Checkout is quarantined: it is not released back to the pool and "
            "needs operator review before it can be reused."
        )
    quarantine = _get(run, "quarantine", "quarantine_state")
    quarantine_state = _state(_get(quarantine, "state", "status", default=quarantine))
    if quarantine_state not in {"unknown", "none", "not_quarantined", "released"}:
        reason = _get(quarantine, "reason", "message")
        warnings.append("Quarantined run resource: " + _plain(reason, quarantine_state))
    # The LTVM sample has no cleanup field, so the guest's cleanup state comes
    # from its durable resource row. That a guest whose cleanup did not finish
    # is STILL in the inventory is the part the resource warning cannot say,
    # and the part that tells an operator there is something left to destroy.
    recorded = _recorded_vm_states(run)
    for vm in _owned_vms(run, supplied_vms):
        name = _vm_name(vm)
        resource = recorded.get(name)
        state = _state(_get(vm, "cleanup_state", default=_get(resource, "state")))
        if state in UNCLEAN_RESOURCE_STATES:
            warnings.append(
                f"VM {name} is still in the LTVM inventory with recorded "
                f"cleanup state {_human(state).casefold()}."
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
    help_id = "engineering-delivery-help-" + quote(_run_id(run), safe="")
    state = _state(_get(run, "state", "status"))
    header = (
        f"<section class='engineering-operator' aria-labelledby='engineering-operator-title-{escape(suffix, quote=True)}'>"
        f"<h3 id='engineering-operator-title-{escape(suffix, quote=True)}'>Message or prod the worker</h3>"
    )
    if state in TERMINAL_STATES:
        # Offering Send/Prod here only produced a controller error page: there
        # is no worker left to deliver to.
        return (
            header + "<p class='controls-unavailable'>This run is "
            + escape(_human(state).casefold())
            + ". There is no live worker to message or prod; guidance is only "
            "delivered to a running session. Use the run controls below to "
            "review a retry as a new run.</p></section>"
        )
    return (
        header
        + f"<form method='post' action='{escape(path + '/guidance', quote=True)}'>"
        + _hidden("run_id", _run_id(run)) + _hidden("expected_version", _get(run, "version", "run_version", default=0))
        + _hidden("csrf_token", csrf_token) + _hidden("idempotency_token", idempotency_token)
        + f"<label for='{escape(field_id, quote=True)}'>Operator message</label>"
        + f"<textarea id='{escape(field_id, quote=True)}' name='message' required "
        + f"aria-describedby='{escape(help_id, quote=True)}'></textarea>"
        # Every other id in this renderer is suffixed with the run id. This one
        # was not, so two engineering runs on one dashboard emitted the same
        # id twice -- invalid HTML, and ambiguous for getElementById and for
        # the aria-describedby above.
        + f"<p id='{escape(help_id, quote=True)}'>Send waits for the next safe turn boundary. "
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
        if value is None or value in ("", UNKNOWN):
            issues.append(label)
    expected_revision = _revision(run)
    if expected_revision is None or expected_revision in ("", UNKNOWN):
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


def render_validation_status(
    run, validation, *, base_url="/engineering-runs",
):
    """Render immutable identity, guest command audit, results, and cleanup state."""
    validation = validation or {}
    identity = _validation_identity(run, validation)
    identity_issues = _validation_identity_issues(run, identity)
    identity_trusted = not identity_issues
    state = _get(validation, "state", "status", "outcome")
    artifacts = _items(_get(validation, "artifacts", "result_artifacts"))
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
            "capability is recorded for exact-owner LTVM resources. "
            "It is not per-command approval. The run also has a host shell and real "
            "service credentials.</p>"
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
            "The run's host shell and service credentials are unaffected by that.</p>"
        )
    else:
        identity_warning = ""
        approval_label = "Approval state"
        approval_value = reported_approval
        boundary = (
            "<p><strong>Execution boundary:</strong> the exact run identity is "
            "verified, but no active approved guest capability is reported. The run's "
            "host shell and service credentials are unaffected by that.</p>"
        )
    return (
        f"<section class='validation-status' aria-labelledby='{escape(suffix, quote=True)}'>"
        f"<h3 id='{escape(suffix, quote=True)}'>Session-owned LTVM validation</h3>"
        + _status_badge(state, noun="Validation") + identity_warning + "<dl>"
        + _field("Validation attempt", _get(validation, "validation_id", "attempt_id", "execution_id"), code=True)
        + _field(approval_label, approval_value)
        + _field("Effective guest capability", "active" if effective_active else "inactive")
        # Deliberately not rendered: `approved_by` is the fixed literal
        # "local-dashboard-user", not a recorded identity. Showing it as
        # "Approved by" implies an accountability trail that does not exist --
        # this tool has no authentication and no per-operator identity.
        + _field("Approved at", _get(validation, "approved_at", "authorized_at"))
        + _field("Manifest ID", identity["manifest_id"], code=True)
        + _field("Manifest digest", identity["manifest_digest"], code=True)
        + _field("Exact pinned revision", identity["revision_sha"], code=True)
        + _field("Exact LTVM owner", identity["owner_id"], code=True)
        + _field("Guest target", identity["target"], code=True)
        + "</dl>" + boundary
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
    validation_html = (
        "" if validation is None
        else render_validation_status(run, validation, base_url=base_url)
    )
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


def _declares_patch_watcher_owner(vm):
    """Say whether a sampled guest claims a Patch Watcher owner id.

    LTVM stamps `pid:<n>` for guests a human created, and nothing at all for
    older ones; neither is a claim on us.
    """
    return _plain(
        _get(vm, "owner_id", "owner"), ""
    ).startswith(PATCH_WATCHER_OWNER_PREFIX)


def _unmatched_vms(runs, vms):
    """Return live inventory orphans plus recorded resources nothing owns.

    The LTVM inventory only lists guests that still exist, so an abandoned
    cleanup -- the exact case an operator has to chase -- disappeared from this
    list entirely.  Recorded resources in an unclean state are added here even
    when ``ltvm list`` no longer knows about them.
    """
    # Ownership is the same question `_owned_vms` answers per run: the guests
    # a displayed run claims are not orphans. Testing the sampled ``owner_id``
    # against the run's own owner id made EVERY guest an orphan, because
    # `ltvm list --json` never carries a patch-watcher owner id.
    claimed = {
        _vm_name(vm) for run in runs for vm in _owned_vms(run, vms)
    }
    # Only guests that declare a Patch Watcher owner can be OUR orphans. The
    # rest of `ltvm list` is other people's work: reporting all of it here as
    # an alert duplicated the resource card's "Other LTVM VMs", which lists the
    # same guests calmly, with controls, and is the honest place for them.
    unmatched = [
        vm for vm in vms
        if _vm_name(vm) not in claimed and _declares_patch_watcher_owner(vm)
    ]
    seen = {_vm_name(vm) for vm in vms}
    for run in runs:
        for resource in _unclean_resources(run):
            name = _resource_label(resource)
            if name in seen:
                continue
            seen.add(name)
            unmatched.append({
                "name": name,
                "owner_id": _get(resource, "owner_id", "owner"),
                "cleanup_state": _get(resource, "state", "cleanup_state"),
                "cleanup_failure": _get(
                    resource, "cleanup_failure", "failure_summary", "reason",
                    "error",
                ),
            })
    return unmatched


def _orphan_row(vm):
    """Render one unmatched guest from the fields its source actually has.

    A live inventory row has an owner id, a state, and a name; only a recorded
    resource row carries a cleanup state or a cleanup failure. Printing
    "cleanup Unknown" against every live guest said nothing at all.
    """
    parts = [
        "<li><strong>" + escape(_vm_name(vm) or UNKNOWN) + "</strong>",
        "owner <code>" + escape(_plain(_get(vm, "owner_id", "owner"))) + "</code>",
    ]
    state = _get(vm, "state")
    if state:
        parts.append("state " + escape(_human(state).casefold()))
    cleanup = _get(vm, "cleanup_state")
    if cleanup:
        parts.append("cleanup " + escape(_human(cleanup).casefold()))
    failure = _get(vm, "cleanup_failure", "failure_summary", "reason")
    if failure:
        parts.append(escape(_plain(failure)))
    return " · ".join(parts) + "</li>"


def render_unmatched_resources(runs, vms, *, heading_tag="h2"):
    """Name every guest and recorded resource no displayed run accounts for.

    Split out of the engineering dashboard so it can outlive it. The card that
    used to carry this was one of three overlapping run panels and was folded
    into a single "Runs" card, but this warning is the only place an abandoned
    cleanup -- a resource still recorded as unclean after `ltvm list` has
    forgotten the guest -- is ever reported to an operator.
    """
    unmatched = _unmatched_vms(_items(runs), _items(vms))
    if not unmatched:
        return "<p class='orphan-ok'>Unmatched or orphan LTVM resources: none reported.</p>"
    tag = escape(str(heading_tag), quote=True)
    return (
        "<section class='orphan-vms' role='alert' aria-labelledby='orphan-vms-title'>"
        f"<{tag} id='orphan-vms-title'>Unmatched or orphan LTVM resources "
        f"({len(unmatched)})</{tag}>"
        "<p>These VMs are not adopted or made mutable because their durable owner "
        "does not exactly match a displayed healthy run.</p><ul>"
        + "".join(_orphan_row(vm) for vm in unmatched)
        + "</ul></section>"
    )


# Effort levels the Claude CLI accepts, mirrored from app.AGENT_EFFORTS. The
# model is a free-text field with suggestions rather than a closed list:
# model identifiers change faster than this file does, and a hardcoded menu
# would quietly stop offering the current one.
AGENT_EFFORTS = ("low", "medium", "high", "xhigh", "max")
SUGGESTED_MODELS = (
    "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001",
)


def _agent_choice_fields(suffix, *, model="", effort=""):
    """Render the per-run model and reasoning-effort controls.

    An empty value means "use whatever this Patch Watcher is configured with",
    which is what the blank first option selects.
    """

    model_id = f"agent-model-{suffix}"
    effort_id = f"agent-effort-{suffix}"
    options = "".join(
        f"<option value='{escape(str(level), quote=True)}'"
        + (" selected" if str(effort) == level else "")
        + f">{escape(level)}</option>"
        for level in AGENT_EFFORTS
    )
    suggestions = "".join(
        f"<option value='{escape(name, quote=True)}'></option>"
        for name in SUGGESTED_MODELS
    )
    return (
        "<div class='agent-choice'>"
        f"<label for='{model_id}'>Model</label>"
        f"<input id='{model_id}' name='model' list='{model_id}-options' "
        f"value='{escape(str(model or ''), quote=True)}' "
        "placeholder='Configured default' maxlength='64'>"
        f"<datalist id='{model_id}-options'>{suggestions}</datalist>"
        f"<label for='{effort_id}'>Reasoning effort</label>"
        f"<select id='{effort_id}' name='effort'>"
        + "<option value=''"
        + ("" if effort else " selected")
        + ">Configured default</option>"
        + options
        + "</select></div>"
    )


def render_engineering_start_control(
    patch, *, action="/engineering-runs/prepare", csrf_token=None,
    idempotency_token=None, compact=False,
):
    """Render the first POST in the controller-owned start confirmation flow."""
    revision = _revision(patch)
    eligible = bool(_get(patch, "engineering_eligible", "eligible", default=False))
    reason = _get(patch, "engineering_disabled_reason", "disabled_reason")
    if not revision:
        eligible = False
        reason = reason or "The exact revision is unavailable."
    disabled = "" if eligible else " disabled aria-disabled='true'"
    fields = (
        _hidden("change_number", _get(patch, "change_number", "change", "id"))
        + _hidden("patchset", _get(patch, "patchset", "patch_set", "patchset_number"))
        + _hidden("revision_sha", revision)
        + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
    )
    if compact:
        title = reason or (
            "Prepare a confirmed source-editing run with owned LTVM validation"
        )
        return (
            "<form class='quick-action' method='post' "
            f"action='{escape(_safe_base_url(action), quote=True)}'>"
            + fields
            + "<button class='secondary' type='submit' title='"
            + f"{escape(_plain(title), quote=True)}'{disabled}>Engineering run</button>"
            + "</form>"
        )
    return (
        "<section class='engineering-start' aria-labelledby='engineering-start-title'>"
        "<h3 id='engineering-start-title'>Start controlled engineering run</h3>"
        "<p>This prepares a dedicated full checkout at the exact revision and "
        "opens a display-only confirmation page before any worker starts.</p>"
        + _capability_status("start")
        + f"<p><strong>Exact pinned revision:</strong> <code>{escape(_plain(revision))}</code></p>"
        + f"<form method='post' action='{escape(_safe_base_url(action), quote=True)}'>"
        + fields
        + _agent_choice_fields("engineering-start")
        + f"<button type='submit'{disabled}>Prepare engineering run</button></form>"
        + (f"<p role='status'>{escape(_plain(reason))}</p>" if not eligible else "")
        + "</section>"
    )


def render_engineering_start_confirmation(
    request, *, confirmation_token, csrf_token=None, idempotency_token=None,
    confirmation_expires_at=None, action="/engineering-runs/start",
    model="", effort="",
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
        # Shown AND carried: the operator confirms the model and effort they
        # are about to spend, and the same values are what the final POST
        # sends. Both are covered by the confirmation signature, so a page
        # that displays one model cannot start a run with another.
        + "<p>Model <strong>"
        + escape(_plain(model) or "configured default")
        + "</strong> at <strong>"
        + escape(_plain(effort) or "configured default")
        + "</strong> reasoning effort.</p>"
        + _hidden("model", model) + _hidden("effort", effort)
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
    "render_capability_status",
    "render_engineering_confirmation",
    "render_engineering_run",
    "render_engineering_start_confirmation",
    "render_engineering_start_control",
    "render_unmatched_resources",
    "render_validation_status",
]
