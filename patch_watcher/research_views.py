"""Side-effect-free Phase 2 research and operator-approval HTML views.

The renderers accept mappings, dataclasses, or attribute objects and do not
depend on controllers, persistence, or adapters.  GET links only navigate to
run details or confirmation pages.  The only mutation forms emitted here are
token-protected POST forms on an explicit confirmation page.
"""

from collections.abc import Mapping
from html import escape
import re
from urllib.parse import quote, urlsplit


UNKNOWN = "unknown"
POLICY_MODES = {"disabled", "manual", "automatic"}
MAX_RESEARCH_RUN_BUDGET = 20
LINK_ACTIONS = {"associate_bug", "associate_jira", "link_bug"}
RETEST_ACTIONS = {"request_retest", "retest"}


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
            value = record[name]
            if value is not None and value != "":
                return value
            continue
        try:
            value = getattr(record, name)
        except (AttributeError, TypeError):
            continue
        if value is not None and value != "":
            return value
    return default


def _plain(value, default=UNKNOWN):
    if value is None or value == "":
        return default
    return str(value)


def _state(value):
    return _plain(value).strip().casefold().replace("-", "_").replace(" ", "_")


def _human(value):
    text = _plain(value)
    if text == UNKNOWN:
        return "Unknown"
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


def _hidden(name, value):
    if value is None:
        return ""
    return (
        f"<input type='hidden' name='{escape(str(name), quote=True)}' "
        f"value='{escape(str(value), quote=True)}'>"
    )


def _field(label, value, *, code=False):
    rendered = escape(_plain(value))
    if code:
        rendered = f"<code>{rendered}</code>"
    return f"<div><dt>{escape(label)}</dt><dd>{rendered}</dd></div>"


def _safe_href(value):
    href = _plain(value, "")
    if not href or href.startswith("//"):
        return None
    if href.startswith("/"):
        return href
    parsed = urlsplit(href)
    if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
        return href
    return None


def _run_path(run_id, base_url="/runs"):
    return f"{base_url.rstrip('/')}/{quote(_plain(run_id), safe='')}"


def _action_path(action, base_url="/approvals"):
    action_id = quote(_plain(_get(action, "action_id", "id")), safe="")
    return f"{base_url.rstrip('/')}/{action_id}"


def _policy_mode(policy):
    mode = _state(_get(policy, "unknown_failure_mode", "research_mode", "mode"))
    # Missing, malformed, and future modes are intentionally fail-closed.
    return mode if mode in POLICY_MODES else "disabled"


def _policy_budget(policy):
    value = _get(policy, "per_revision_run_budget", "run_budget", "budget")
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if 0 <= parsed <= MAX_RESEARCH_RUN_BUDGET else 0


def render_research_policy_form(
    patch,
    *,
    policy=None,
    action="/research/policy/prepare",
    csrf_token=None,
    idempotency_token=None,
):
    """Render a revision-pinned policy proposal form.

    The prepare endpoint must not activate automatic mode.  It may validate
    the proposal and redirect with GET to
    :func:`render_research_policy_confirmation`; that view emits the final,
    token-protected POST.
    """
    mode = _policy_mode(policy)
    budget = _policy_budget(policy)
    revision = _get(patch, "revision_sha", "revision", "current_revision")
    patchset = _get(patch, "patchset", "patch_set")
    change = _get(patch, "change_number", "change", "id")
    identity = quote(f"{_plain(change)}-{_plain(patchset)}", safe="")
    select_id = "research-policy-mode-" + identity
    budget_id = "research-policy-budget-" + identity
    available = bool(re_full_revision(_plain(revision, "")))
    options = "".join(
        f"<option value='{candidate}'{' selected' if candidate == mode else ''}>"
        f"{escape(_human(candidate))}</option>"
        for candidate in ("disabled", "manual", "automatic")
    )
    disabled = "" if available else " disabled aria-disabled='true'"
    warning = (
        "Automatic is not enabled by this form alone. Selecting it prepares a "
        "separate confirmation page tied to this exact revision."
    )
    return (
        f"<section class='research-policy' aria-labelledby='research-policy-title-{escape(identity, quote=True)}'>"
        f"<h3 id='research-policy-title-{escape(identity, quote=True)}'>Research trigger policy</h3>"
        f"<p id='research-policy-help-{escape(identity, quote=True)}'>{escape(warning)}</p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        f"<label for='{escape(select_id, quote=True)}'>Unknown-failure trigger</label>"
        f"<select id='{escape(select_id, quote=True)}' name='research_mode' "
        f"aria-describedby='research-policy-help-{escape(identity, quote=True)}'{disabled}>"
        f"{options}</select>"
        f"<label for='{escape(budget_id, quote=True)}'>Maximum research runs per revision</label>"
        f"<input id='{escape(budget_id, quote=True)}' name='per_revision_run_budget' "
        f"type='number' min='0' max='{MAX_RESEARCH_RUN_BUDGET}' step='1' "
        f"value='{budget}'{disabled}>"
        + _hidden("change_number", change)
        + _hidden("patchset", patchset)
        + _hidden("revision_sha", revision)
        + _hidden("expected_policy_version", _get(policy, "version", "policy_version", default=0))
        + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit'{disabled}>Review policy change</button></form>"
        + (
            "<p role='status'>The exact revision is unavailable; policy changes are disabled.</p>"
            if not available else ""
        )
        + "</section>"
    )


def render_research_policy_confirmation(
    patch,
    proposed_policy,
    *,
    confirmation_token,
    csrf_token=None,
    idempotency_token=None,
    action="/research/policy/confirm",
):
    """Render the final confirmation for an automatic research policy.

    This function renders a view; only its POST form may activate the policy.
    It is intentionally limited to automatic proposals. Disabled/manual
    proposals can be persisted by the controller after its prepare validation.
    """
    if not confirmation_token:
        raise ValueError("a confirmation token is required")
    raw_mode = _state(_get(
        proposed_policy, "unknown_failure_mode", "research_mode", "mode"
    ))
    if raw_mode != "automatic":
        raise ValueError("automatic policy confirmation requires automatic mode")
    raw_budget = _get(
        proposed_policy, "per_revision_run_budget", "run_budget", "budget"
    )
    if isinstance(raw_budget, bool):
        raise ValueError("run budget must be an integer")
    try:
        budget = int(raw_budget)
    except (TypeError, ValueError) as exc:
        raise ValueError("run budget must be an integer") from exc
    if budget < 1 or budget > MAX_RESEARCH_RUN_BUDGET:
        raise ValueError("run budget is outside the supported range")
    revision = _plain(_get(patch, "revision_sha", "revision", "current_revision"), "")
    if not re_full_revision(revision):
        raise ValueError("an exact 40-character revision is required")
    change = _get(patch, "change_number", "change", "id")
    patchset = _get(patch, "patchset", "patch_set")
    return (
        "<main class='research-policy-confirmation'>"
        "<h2>Confirm automatic unknown-failure research</h2>"
        "<p role='alert'><strong>Automatic trigger:</strong> eligible unknown "
        "failures may start a read-only Claude research session without another "
        "start confirmation, up to the displayed per-revision budget.</p>"
        "<p><strong>Execution boundary:</strong> Read-only · Unsandboxed host "
        "worker · General network access. No Gerrit, Maloo, Jenkins, or JIRA "
        "write authority is granted.</p>"
        "<dl>"
        + _field("Change", change)
        + _field("Patchset", patchset)
        + _field("Exact pinned revision", revision, code=True)
        + _field("Proposed mode", "Automatic")
        + _field("Maximum research runs per revision", budget)
        + "</dl>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        + _hidden("change_number", change)
        + _hidden("patchset", patchset)
        + _hidden("revision_sha", revision)
        + _hidden("research_mode", "automatic")
        + _hidden("per_revision_run_budget", budget)
        + _hidden("expected_policy_version", _get(
            proposed_policy, "version", "policy_version", default=0
        ))
        + _hidden("confirmation_token", confirmation_token)
        + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
        + "<button type='submit'>Enable automatic research</button> "
        "<a href='/'>Do not change the policy</a></form></main>"
    )


def render_unknown_failure_control(
    patch,
    *,
    policy=None,
    action="/research/investigate",
    csrf_token=None,
    idempotency_token=None,
):
    """Render per-patch unknown-failure research policy and manual control."""
    mode = _policy_mode(policy)
    revision = _get(patch, "revision_sha", "revision", "current_revision")
    patchset = _get(patch, "patchset", "patch_set")
    change = _get(patch, "change_number", "change", "id")
    heading_id = "unknown-research-title-" + quote(
        f"{_plain(change)}-{_plain(patchset)}", safe=""
    )
    reason_id = "unknown-research-reason-" + quote(
        f"{_plain(change)}-{_plain(patchset)}", safe=""
    )
    lifecycle = _state(_get(patch, "lifecycle", "status", "gerrit_status"))
    unknown_failure = bool(_get(patch, "has_unknown_failure", "unknown_failure", default=False))
    active_run = _get(patch, "active_research_run_id", "active_run_id")
    eligible = lifecycle in {"open", "new"} and bool(revision) and unknown_failure and not active_run

    reasons = []
    if mode == "disabled":
        reasons.append("Unknown-failure investigation is disabled for this patch.")
    if lifecycle not in {"open", "new"}:
        reasons.append(f"Change is {_human(lifecycle).casefold()}.")
    if not revision:
        reasons.append("The exact current revision is unavailable.")
    if not unknown_failure:
        reasons.append("No unknown enforced failure is currently recorded.")
    if active_run:
        reasons.append("An active run already owns this patch.")

    policy_text = {
        "disabled": "Disabled — observe only; no research session starts.",
        "manual": "Manual — an operator may start an eligible research session.",
        "automatic": "Automatic — eligible unknown failures may trigger after controller safety gates.",
    }[mode]
    action_html = ""
    if mode == "manual" and eligible:
        action_html = (
            f"<form method='post' action='{escape(action, quote=True)}'>"
            + _hidden("change_number", change)
            + _hidden("patchset", patchset)
            + _hidden("revision_sha", revision)
            + _hidden("csrf_token", csrf_token)
            + _hidden("attempt_id", idempotency_token)
            + "<button type='submit'>Investigate unknown failure</button></form>"
        )
    elif mode == "automatic":
        action_html = (
            "<p role='status'>Automatic observation is configured. This view "
            "does not bypass eligibility, revision, ownership, or budget gates.</p>"
        )
    else:
        reason = " ".join(reasons) or "This investigation cannot start."
        action_html = (
            "<button type='button' disabled aria-disabled='true' "
            f"aria-describedby='{escape(reason_id, quote=True)}'>"
            "Investigate unknown failure</button>"
            f"<p id='{escape(reason_id, quote=True)}' role='status'>{escape(reason)}</p>"
        )

    return (
        f"<section class='unknown-failure-control' aria-labelledby='{escape(heading_id, quote=True)}'>"
        f"<h3 id='{escape(heading_id, quote=True)}'>Unknown-failure investigation</h3>"
        f"<p><strong>Trigger policy:</strong> {escape(policy_text)}</p>"
        "<p class='safety-note' role='note'><strong>Read-only · Unsandboxed host "
        "worker · General network access.</strong> Research cannot write to "
        "Gerrit, Maloo, Jenkins, or JIRA.</p>"
        f"<p>Patchset {escape(_plain(patchset))} · exact revision "
        f"<code>{escape(_plain(revision))}</code></p>{action_html}</section>"
    )


def _render_evidence(evidence):
    items = []
    for item in _items(evidence):
        label = _plain(_get(item, "label", "title", "kind"))
        href = _safe_href(_get(item, "url", "href", "path"))
        detail = _get(item, "detail", "summary")
        if href:
            content = f"<a href='{escape(href, quote=True)}'>{escape(label)}</a>"
        else:
            content = escape(label) + " <span class='muted'>(link unavailable)</span>"
        if detail:
            content += f" — {escape(_plain(detail))}"
        items.append(f"<li>{content}</li>")
    return "<ul class='research-evidence'>" + "".join(items) + "</ul>" if items else "<p>No evidence links recorded.</p>"


def render_research_session(
    research,
    *,
    evidence=(),
    run_base_url="/runs",
):
    """Render latest read-only research state and evidence-linked recommendation."""
    run_id = _get(research, "run_id", "id")
    recommendation = _get(research, "recommendation", "recommended_action")
    rationale = _get(research, "rationale", "reason", "summary")
    state = _get(research, "state", "status")
    question = _get(research, "question", "waiting_question")
    state_text = _human(state)
    tone = "bad" if _state(state) in {"failed", "blocked"} else (
        "warn" if _state(state) in {"waiting_human", "needs_attention"} else "neutral"
    )
    question_html = (
        f"<p role='alert'><strong>Waiting for operator:</strong> {escape(_plain(question))}</p>"
        if question else ""
    )
    run_link = (
        f"<p><a href='{escape(_run_path(run_id, run_base_url), quote=True)}'>Open research run details</a></p>"
        if run_id not in (None, "") else "<p>Run details are unavailable.</p>"
    )
    return (
        "<section class='latest-research' aria-labelledby='latest-research-title'>"
        "<h3 id='latest-research-title'>Latest unknown-failure research</h3>"
        f"<p><span class='research-state tone-{tone}'>State: {escape(state_text)}</span></p>"
        "<p class='safety-note'><strong>Read-only · Unsandboxed host worker · "
        "General network access.</strong></p>"
        "<dl>"
        + _field("Run", run_id, code=True)
        + _field("Exact pinned revision", _get(research, "revision_sha", "revision"), code=True)
        + _field("Recommendation", _human(recommendation))
        + _field("Rationale", rationale)
        + "</dl>"
        + question_html
        + "<h4>Supporting evidence</h4>"
        + _render_evidence(evidence or _get(research, "evidence"))
        + run_link
        + "</section>"
    )


def _action_kind(action):
    raw = _state(_get(action, "action_type", "kind", "type"))
    if raw in LINK_ACTIONS:
        return "associate_bug"
    if raw in RETEST_ACTIONS:
        return "request_retest"
    return "unknown"


def _budget(action):
    value = _get(action, "action_budget_remaining", "budget_remaining")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _approval_readiness(action):
    reasons = []
    kind = _action_kind(action)
    state = _state(_get(action, "state", "status"))
    authority = _state(_get(action, "authority", "authority_mode", "action_mode"))
    budget = _budget(action)
    revision = _plain(_get(action, "revision_sha", "revision"), "")
    session = _plain(_get(action, "session_id", "maloo_session_id"), "")
    suite_id = _plain(_get(action, "suite_id", "test_set_id"), "")
    jira = _plain(_get(action, "jira_key", "jira_ticket", "bug_id"), "")
    action_id = _plain(_get(action, "action_id", "id"), "")
    if not action_id:
        reasons.append("The action identifier is unavailable.")
    if kind == "unknown":
        reasons.append("The action type is not supported.")
    if state not in {"pending_approval", "planned", "awaiting_approval"}:
        reasons.append(f"Action state is {_human(state).casefold()}, not pending approval.")
    if authority != "approval":
        reasons.append("Exact authority is not operator approval.")
    if budget is None or budget <= 0:
        reasons.append("No action budget remains.")
    if not re_full_revision(revision):
        reasons.append("The exact 40-character revision is unavailable.")
    if not session:
        reasons.append("The Maloo session is unavailable.")
    if not suite_id:
        reasons.append("The Maloo suite/test-set ID is unavailable.")
    if not jira_key_valid(jira):
        reasons.append("The JIRA key is invalid or unavailable.")
    if kind == "request_retest":
        dependency = _state(_get(action, "bug_link_state", "dependency_state", "link_action_state"))
        if dependency not in {"succeeded", "linked", "complete", "completed"}:
            reasons.append("The existing JIRA key must be associated successfully before retest approval.")
    return not reasons, reasons


def re_full_revision(value):
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", value or ""))


def jira_key_valid(value):
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", value or ""))


def _action_identity(action):
    return (
        "<dl class='approval-identity'>"
        + _field("Exact pinned revision", _get(action, "revision_sha", "revision"), code=True)
        + _field("Maloo session", _get(action, "session_id", "maloo_session_id"), code=True)
        + _field("Test group", _get(action, "test_group"))
        + _field("Failed suite", _get(action, "suite_name", "suite"))
        + _field("Suite/test-set ID", _get(action, "suite_id", "test_set_id"), code=True)
        + _field("JIRA key", _get(action, "jira_key", "jira_ticket", "bug_id"), code=True)
        + _field("Authority", _human(_get(action, "authority", "authority_mode", "action_mode")))
        + _field("Action budget remaining", _get(action, "action_budget_remaining", "budget_remaining"))
        + "</dl>"
    )


def render_action_approval_card(action, *, base_url="/approvals"):
    """Render a non-mutating card leading to an explicit confirmation page."""
    kind = _action_kind(action)
    ready, reasons = _approval_readiness(action)
    if kind == "associate_bug":
        step, title = "Step 1 of 2", "Associate existing JIRA key"
        explanation = "Associate this existing JIRA key with the named failed Maloo suite."
    elif kind == "request_retest":
        step, title = "Step 2 of 2", "Request one retest"
        explanation = "After the bug association succeeds, request one retest for this session/test group."
    else:
        step, title = "Unsupported step", "Unknown proposed action"
        explanation = "This action cannot be approved from Patch Watcher."
    action_path = _action_path(action, base_url)
    action_fragment = quote(_plain(_get(action, "action_id", "id")), safe="")
    blocker_id = "approval-blockers-" + action_fragment
    control = (
        f"<a class='approval-review-link' href='{escape(action_path + '/confirm', quote=True)}'>Review exact action…</a>"
        if ready else
        f"<button type='button' disabled aria-disabled='true' aria-describedby='{escape(blocker_id, quote=True)}'>Approval unavailable</button>"
    )
    blockers = (
        f"<div id='{escape(blocker_id, quote=True)}' role='status'><strong>Cannot approve:</strong><ul>"
        + "".join(f"<li>{escape(reason)}</li>" for reason in reasons) + "</ul></div>"
        if reasons else ""
    )
    dependency = (
        "<p class='dependency'><strong>Sequential dependency:</strong> Step 1 bug association "
        "must be recorded as succeeded before Step 2 can be approved.</p>"
        if kind == "request_retest" else
        "<p class='dependency'><strong>Sequential dependency:</strong> This association "
        "must succeed before any retest approval is enabled.</p>"
    )
    return (
        "<article class='action-approval-card' aria-labelledby='approval-card-title-"
        f"{escape(action_fragment, quote=True)}'>"
        f"<header><p>{escape(step)}</p><h3 id='approval-card-title-"
        f"{escape(action_fragment, quote=True)}'>"
        f"{escape(title)}</h3></header><p>{escape(explanation)}</p>"
        f"{dependency}{_action_identity(action)}{control}{blockers}</article>"
    )


def render_failure_action_status(action, *, base_url="/approvals"):
    """Render an approved/in-flight/terminal failure action without controls."""
    kind = _action_kind(action)
    state = _state(_get(action, "state", "status"))
    approval = _state(_get(action, "approval_state"))
    stage = _state(_get(action, "stage"))
    labels = {
        "executing": "Executing",
        "waiting_external": "Waiting for Maloo",
        "succeeded": "Succeeded",
        "failed": "Failed",
        "ambiguous": "Ambiguous result — human review required",
        "cancelled": "Cancelled",
        "stale": "Stale",
    }
    label = labels.get(state, _human(stage or state))
    if state == "planned" and approval == "approved":
        label = "Queued for execution"
    if state == "planned" and approval != "approved":
        label = "Waiting for approval"
    title = (
        "JIRA association"
        if kind == "associate_bug"
        else "Maloo retest" if kind == "request_retest" else "Failure action"
    )
    action_path = _action_path(action, base_url)
    detail = _plain(_get(action, "detail", "failure_summary"), "")
    return (
        "<article class='action-approval-card action-status-card'>"
        f"<header><p>Write workflow status</p><h3>{escape(title)}</h3></header>"
        f"<p><strong>{escape(label)}</strong></p>"
        + (f"<p>{escape(detail)}</p>" if detail else "")
        + _action_identity(action)
        + f"<p><a href='{escape(action_path, quote=True)}'>View action details</a></p>"
        "</article>"
    )


def render_action_confirmation(
    action,
    *,
    confirmation_token,
    csrf_token=None,
    idempotency_token=None,
    base_url="/approvals",
):
    """Render the token-protected POST step for one exact proposed action."""
    kind = _action_kind(action)
    ready, reasons = _approval_readiness(action)
    action_path = _action_path(action, base_url)
    if kind == "associate_bug":
        title = "Confirm JIRA association"
        warning = "This writes one existing JIRA association to the named Maloo failed suite. It does not request a retest."
        button = "Associate JIRA key"
    elif kind == "request_retest":
        title = "Confirm one retest"
        warning = "This requests exactly one retest for the displayed Maloo session and test group. The successful bug association is a required prior step."
        button = "Request one retest"
    else:
        title, warning, button = "Unsupported action", "This action cannot be approved.", "Unavailable"
    if not confirmation_token:
        raise ValueError("a confirmation token is required")
    if not ready:
        return (
            "<main class='action-confirmation'><h2>Approval unavailable</h2>"
            "<p role='alert'>The action is not currently safe to approve.</p><ul>"
            + "".join(f"<li>{escape(reason)}</li>" for reason in reasons)
            + f"</ul><p><a href='{escape(action_path, quote=True)}'>Return to action</a></p></main>"
        )
    return (
        "<main class='action-confirmation'>"
        f"<h2>{escape(title)}</h2><p role='alert'>{escape(warning)}</p>"
        f"{_action_identity(action)}"
        f"<form method='post' action='{escape(action_path + '/approve', quote=True)}'>"
        + _hidden("action_id", _get(action, "action_id", "id"))
        + _hidden("action_type", kind)
        + _hidden("expected_version", _get(action, "version", "action_version", default=0))
        + _hidden("revision_sha", _get(action, "revision_sha", "revision"))
        + _hidden("session_id", _get(action, "session_id", "maloo_session_id"))
        + _hidden("test_group", _get(action, "test_group"))
        + _hidden("suite_id", _get(action, "suite_id", "test_set_id"))
        + _hidden("jira_key", _get(action, "jira_key", "jira_ticket", "bug_id"))
        + _hidden("confirmation_token", confirmation_token)
        + _hidden("csrf_token", csrf_token)
        + _hidden("idempotency_token", idempotency_token)
        + f"<button type='submit'>{escape(button)}</button> "
        f"<a href='{escape(action_path, quote=True)}'>Do not perform this action</a>"
        "</form></main>"
    )


render_unknown_failure_investigation = render_unknown_failure_control
render_research_policy = render_research_policy_form
render_research_policy_auto_confirmation = render_research_policy_confirmation
render_latest_research = render_research_session
render_approval_card = render_action_approval_card
render_approval_confirmation = render_action_confirmation
