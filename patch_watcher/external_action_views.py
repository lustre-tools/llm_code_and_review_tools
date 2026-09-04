"""Side-effect-free views for explicit controller-owned external writes."""

from collections.abc import Mapping
from html import escape


_MAX_REVIEW_REPLIES = 64
_MAX_REPLY_MESSAGE_BYTES = 4_000
_MAX_REPLY_PATH_BYTES = 1_000


def _state(value):
    return escape(str(value or "unavailable").replace("_", " "))


def render_review_reply_control(
    *, run_id, plan=None, enabled=False, eligible=False, reason="",
    csrf_token="",
):
    if plan is not None:
        action = ""
        if plan.state in {"write_claimed", "ambiguous"}:
            action = (
                f"<form method='post' action='/review-replies/{escape(plan.reply_id, quote=True)}/reconcile'>"
                f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
                "<button type='submit'>Reconcile reply write</button></form>"
            )
        return (
            "<div class='external-write'><strong>Gerrit replies:</strong> "
            f"<span class='run-state'>{_state(plan.state)}</span> "
            + escape(plan.summary or "Exact reply write recorded.") + action + "</div>"
        )
    disabled = " disabled aria-disabled='true'" if not (enabled and eligible) else ""
    explanation = reason or (
        "Posts the immutable reply drafts only after rechecking the exact revision "
        "and unresolved-comment snapshot."
    )
    return (
        "<div class='external-write'><form method='post' action='/review-replies/prepare'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='run_id' value='{escape(run_id, quote=True)}'>"
        f"<button type='submit'{disabled}>Post Gerrit replies…</button></form>"
        f"<p class='detail'>{escape(explanation)}</p></div>"
    )


def render_review_reply_confirmation(plan, *, token, expires_at, csrf_token):
    comments = plan.payload.get("comments", {})
    if not isinstance(comments, Mapping):
        raise ValueError("review reply comments must be a mapping")
    drafts = []
    for path, items in comments.items():
        if (
            not isinstance(path, str) or not path
            or len(path.encode("utf-8")) > _MAX_REPLY_PATH_BYTES
            or not isinstance(items, list)
        ):
            raise ValueError("review reply target is invalid")
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("review reply draft is invalid")
            message = item.get("message")
            if (
                not isinstance(message, str) or not message
                or len(message.encode("utf-8")) > _MAX_REPLY_MESSAGE_BYTES
            ):
                raise ValueError("review reply message is invalid or too large")
            line = item.get("line")
            target = path if not isinstance(line, int) else f"{path}:{line}"
            drafts.append(
                "<li class='reply-draft'>"
                f"<p>Target: <code>{escape(target)}</code></p>"
                f"<pre>{escape(message)}</pre></li>"
            )
    if not drafts or len(drafts) > _MAX_REVIEW_REPLIES:
        raise ValueError("review reply count is outside the display bound")
    count = len(drafts)
    return (
        "<main><h1>Confirm Gerrit review replies</h1>"
        f"<p>Post {count} immutable inline repl{'y' if count == 1 else 'ies'} to change "
        f"{plan.change_number}, PS {plan.patchset}, revision <code>{escape(plan.revision_sha)}</code>.</p>"
        f"<p>Comment snapshot <code>{escape(plan.snapshot_sha256)}</code>; "
        f"draft <code>{escape(plan.draft_sha256)}</code>.</p>"
        "<p>The controller will recheck both before writing. An uncertain POST is never "
        "retried; it becomes reconciliation-only.</p>"
        "<h2>Exact replies to post</h2><ol class='reply-drafts'>"
        + "".join(drafts)
        + "</ol>"
        f"<form method='post' action='/review-replies/{escape(plan.reply_id, quote=True)}/execute'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='binding_digest' value='{escape(plan.binding_digest, quote=True)}'>"
        f"<input type='hidden' name='confirmation_token' value='{escape(token, quote=True)}'>"
        f"<input type='hidden' name='confirmation_expires_at' value='{escape(expires_at, quote=True)}'>"
        "<button type='submit'>Post exact replies</button></form></main>"
    )


def render_jenkins_retrigger_control(
    patch, *, plan=None, enabled=False, csrf_token="",
):
    if plan is not None:
        action = ""
        if plan.state in {"dispatch_claimed", "ambiguous"}:
            action = (
                f"<form method='post' action='/jenkins-retriggers/{escape(plan.action_id, quote=True)}/reconcile'>"
                f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
                "<button type='submit'>Reconcile Jenkins retrigger</button></form>"
            )
        return (
            "<div class='external-write'><strong>Jenkins retrigger:</strong> "
            f"<span class='run-state'>{_state(plan.state)}</span> "
            + escape(plan.summary or "Exact retrigger action recorded.")
            + action + "</div>"
        )
    eligible = bool(
        enabled and str(patch.get("jenkins") or "").upper() in {"FAIL", "FAILURE"}
        and patch.get("jenkins_url") and patch.get("revision_sha")
        and patch.get("revision_ref") and patch.get("project")
        and str(patch.get("lifecycle") or "").lower() == "open"
    )
    disabled = " disabled aria-disabled='true'" if not eligible else ""
    reason = (
        "Retriggers this exact failed Jenkins build after a fresh Gerrit/Jenkins preflight."
        if eligible else
        "Jenkins retrigger is disabled or this is not a complete current failed-build snapshot."
    )
    return (
        "<div class='external-write'><form method='post' action='/jenkins-retriggers/prepare'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(str(patch.get('change_number') or ''), quote=True)}'>"
        f"<input type='hidden' name='patchset' value='{escape(str(patch.get('patchset') or ''), quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(str(patch.get('revision_sha') or ''), quote=True)}'>"
        f"<button type='submit'{disabled}>Retrigger Jenkins…</button></form>"
        f"<p class='detail'>{escape(reason)}</p></div>"
    )


def render_jenkins_retrigger_confirmation(plan, *, token, expires_at, csrf_token):
    return (
        "<main><h1>Confirm Jenkins retrigger</h1>"
        f"<p>Retrigger <strong>{escape(plan.job_name)}</strong> build #{plan.build_number} "
        f"for change {plan.change_number}, PS {plan.patchset}, revision "
        f"<code>{escape(plan.revision_sha)}</code>.</p>"
        f"<p>Immutable failed-build snapshot: <code>{escape(plan.snapshot_sha256)}</code>.</p>"
        "<p>The controller checks for revision changes and an existing equivalent build before "
        "dispatch. A claimed write is never blindly repeated.</p>"
        f"<form method='post' action='/jenkins-retriggers/{escape(plan.action_id, quote=True)}/execute'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='binding_digest' value='{escape(plan.binding_digest, quote=True)}'>"
        f"<input type='hidden' name='confirmation_token' value='{escape(token, quote=True)}'>"
        f"<input type='hidden' name='confirmation_expires_at' value='{escape(expires_at, quote=True)}'>"
        "<button type='submit'>Retrigger exact build</button></form></main>"
    )
