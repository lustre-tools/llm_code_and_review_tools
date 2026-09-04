"""Small, side-effect-free Phase 1 dashboard renderers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from html import escape
from typing import Any, Mapping


RETEST_MODES = ("disabled", "advise", "approval", "automatic")


def _project(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        return result if isinstance(result, Mapping) else {}
    return getattr(value, "__dict__", {})


def _text(value: Any, default: str = "") -> str:
    return str(default if value is None else value)


def render_global_retest_status(
    *, execution_enabled: bool, csrf_token: str, recent_summary: str = ""
) -> str:
    """Render the global safety gate; GET never changes it."""
    state = "Enabled" if execution_enabled else "Disabled"
    tone = "tone-bad" if execution_enabled else "tone-neutral"
    if execution_enabled:
        action = (
            "<form method='post' action='/automation/global/disable'>"
            f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
            "<button class='secondary' type='submit'>Disable automatic actions</button></form>"
        )
    else:
        action = (
            "<a class='button-link danger-link' href='/automation/global/confirm-enable'>"
            "Review enabling automatic actions</a>"
        )
    summary = (
        f"<p class='detail'>{escape(recent_summary)}</p>" if recent_summary else ""
    )
    return (
        "<section class='card retest-global' aria-labelledby='retest-global-title'>"
        "<div class='section-title'><div><h2 id='retest-global-title'>Automatic actions</h2>"
        "<p class='sub'>Master gate for saved automatic test, build, and review policies.</p></div>"
        f"<span class='status-chip {tone}'>Global execution: {state}</span></div>"
        "<p>Patch policies may observe, advise, or prepare approval while this gate is off. "
        "Only explicitly approved actions can run; automatic actions require this global gate.</p>"
        f"{summary}{action}</section>"
    )


def render_retest_control(
    patch: Any,
    policy: Any,
    *,
    evaluation: Any = None,
    timeline: list[Any] | None = None,
    approval_action: Any = None,
    csrf_token: str,
    show_policy_form: bool = True,
) -> str:
    """Render one revision-aware policy control and bounded decision history."""
    patch_data = _project(patch)
    policy_data = _project(policy)
    evaluation_data = _project(evaluation)
    change = _text(patch_data.get("change_number"))
    revision = _text(patch_data.get("revision_sha"))
    mode = _text(policy_data.get("mode"), "disabled").casefold()
    if mode not in RETEST_MODES:
        mode = "disabled"
    max_actions = policy_data.get("max_actions", policy_data.get("action_budget", 1))
    try:
        max_actions = max(1, min(20, int(max_actions)))
    except (TypeError, ValueError):
        max_actions = 1
    options = "".join(
        f"<option value='{item}'{' selected' if item == mode else ''}>"
        f"{escape(item.title())}</option>"
        for item in RETEST_MODES
    )
    decision = ""
    if evaluation_data:
        status = _text(evaluation_data.get("status"), "observed")
        reason = _text(
            evaluation_data.get("reason") or evaluation_data.get("reason_code"),
            "No decision recorded",
        )
        decision = (
            "<div class='retest-decision' aria-label='Latest retest decision'>"
            f"<strong>{escape(status.replace('_', ' ').title())}</strong>"
            f"<span>{escape(reason)}</span></div>"
        )
    history = render_retest_timeline(timeline or [])
    approval_data = _project(approval_action)
    approval = ""
    if approval_data:
        action_id = _text(approval_data.get("action_id"))
        session_id = _text(approval_data.get("session_id"), "unknown session")
        jira_ticket = _text(approval_data.get("jira_ticket"), "linked Jira bug")
        approval = (
            "<div class='retest-approval'><strong>Approval required</strong>"
            f"<span>Retest {escape(session_id)} using {escape(jira_ticket)}.</span>"
            f"<a class='button-link danger-link' href='/automation/actions/"
            f"{escape(action_id, quote=True)}/confirm'>Review exact action</a></div>"
        )
    disabled = "" if change and revision else " disabled"
    explanation = {
        "disabled": "Observe only; create no retest action.",
        "advise": "Show the exact action that would be taken, without requesting it.",
        "approval": "Prepare an action and wait for explicit operator approval.",
        "automatic": "Request one eligible retest only when the global gate and all safety checks pass.",
    }[mode]
    policy_form = (
        "<form method='post' action='/automation/policy'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(change, quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(revision, quote=True)}'>"
        "<label>Mode <select name='mode' aria-label='Test failure handling mode'>"
        f"{options}</select></label>"
        "<label>Per-revision action budget "
        f"<input name='max_actions' type='number' min='1' max='20' value='{max_actions}'></label>"
        f"<button class='secondary' type='submit'{disabled}>Save policy</button></form>"
        if show_policy_form else ""
    )
    return (
        "<details class='retest-control'><summary>Test failure handling: "
        f"<strong>{escape(mode.title())}</strong></summary>"
        f"<p class='detail'>{escape(explanation)}</p>"
        f"{policy_form}"
        "<form method='post' action='/automation/dry-run'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(change, quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(revision, quote=True)}'>"
        f"<button class='secondary' type='submit'{disabled}>Evaluate now (dry run)</button></form>"
        f"{decision}{approval}{history}</details>"
    )


def render_retest_timeline(entries: list[Any], *, limit: int = 8) -> str:
    projected = [_project(item) for item in entries][-max(0, limit):]
    if not projected:
        return "<p class='detail'>No deterministic retest decisions recorded.</p>"
    rows = []
    for item in projected:
        at = _text(item.get("created_at") or item.get("at"), "unknown time")
        kind = _text(item.get("event_type") or item.get("kind"), "event")
        summary = _text(item.get("summary") or item.get("reason"), "Recorded")
        rows.append(
            f"<li><time>{escape(at)}</time> <strong>{escape(kind.replace('_', ' '))}</strong>"
            f" — {escape(summary)}</li>"
        )
    return "<ol class='retest-timeline'>" + "".join(rows) + "</ol>"


def render_enable_confirmation(*, csrf_token: str) -> str:
    return (
        "<main><h1>Enable automatic patch actions?</h1>"
        "<p>This permits every watched patch whose standing trigger is Automatic to run "
        "its selected test, build-repair, or review-comment handler after exact-revision, "
        "idempotency, ownership, and budget checks pass. Build and review handlers start "
        "Claude and may create LTVM guests. Separate kill switches still control Gerrit "
        "uploads, Gerrit replies, and Jenkins retriggers.</p>"
        "<form method='post' action='/automation/global/enable'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        "<button class='danger' type='submit'>Enable automatic actions</button></form>"
        "<p><a href='/'>Cancel and keep execution disabled</a></p></main>"
    )


def render_policy_confirmation(
    *, change_number: str, revision_sha: str, max_actions: int, csrf_token: str
) -> str:
    return (
        "<main><h1>Set this patch to Automatic?</h1>"
        f"<p>Change {escape(change_number)}, revision <code>{escape(revision_sha)}</code>, "
        f"with a maximum of {int(max_actions)} retest action(s) for this revision.</p>"
        "<p>The global execution gate is independent and may still prevent execution.</p>"
        "<form method='post' action='/automation/policy/confirm'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(change_number, quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(revision_sha, quote=True)}'>"
        f"<input type='hidden' name='max_actions' value='{int(max_actions)}'>"
        "<button class='danger' type='submit'>Confirm Automatic policy</button></form>"
        "<p><a href='/'>Cancel without changing policy</a></p></main>"
    )


def render_action_confirmation(
    *, action_id: str, change_number: str, revision_sha: str,
    session_id: str, jira_ticket: str, csrf_token: str
) -> str:
    """Render a non-mutating review page for one exact approval action."""
    return (
        "<main><h1>Approve this exact Maloo retest?</h1>"
        f"<p>Change {escape(change_number)}, revision <code>{escape(revision_sha)}</code>.</p>"
        f"<p>Maloo session <code>{escape(session_id)}</code> will be retested using "
        f"<strong>{escape(jira_ticket)}</strong> as its justification.</p>"
        "<p>Approval is bound to this revision and policy snapshot. A fresh Gerrit and "
        "Maloo reconciliation still runs before the request.</p>"
        f"<form method='post' action='/automation/actions/{escape(action_id, quote=True)}/approve'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(revision_sha, quote=True)}'>"
        "<button class='danger' type='submit'>Approve one retest</button></form>"
        "<p><a href='/'>Cancel without approving</a></p></main>"
    )


__all__ = [
    "RETEST_MODES",
    "render_enable_confirmation",
    "render_action_confirmation",
    "render_global_retest_status",
    "render_policy_confirmation",
    "render_retest_control",
    "render_retest_timeline",
]
