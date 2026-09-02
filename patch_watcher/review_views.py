"""Compact, side-effect-free views for Phase 4 review-comment handling."""

from html import escape


def render_review_start_control(
    patch, *, csrf_token: str, idempotency_token: str,
    action: str = "/review-runs/prepare", upload_enabled: bool = False,
):
    eligible = bool(
        upload_enabled
        and patch.get("revision_sha") and patch.get("revision_ref")
        and patch.get("project") and int(patch.get("unresolved") or 0) > 0
        and not patch.get("active_run_id")
        and str(patch.get("lifecycle") or "").lower() == "open"
    )
    if not upload_enabled:
        reason = "Automatic patchset upload is disabled by the controller kill switch."
    elif patch.get("active_run_id"):
        reason = "A managed run already owns this patch."
    elif not int(patch.get("unresolved") or 0):
        reason = "There are no unresolved review comments."
    else:
        reason = "Refresh the exact open revision before handling comments."
    disabled = " disabled" if not eligible else ""
    return (
        "<div class='review-start'>"
        "<p class='detail'>Simple handles only clearly trivial comments; All attempts broadly. "
        "Both bail to human when judgment is required.</p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(str(patch.get('change_number') or ''), quote=True)}'>"
        f"<input type='hidden' name='patchset' value='{escape(str(patch.get('patchset') or ''), quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(str(patch.get('revision_sha') or ''), quote=True)}'>"
        f"<input type='hidden' name='idempotency_token' value='{escape(idempotency_token, quote=True)}'>"
        "<label>Mode <select name='review_mode'>"
        "<option value='simple'>Handle simple comments</option>"
        "<option value='all'>Handle all comments</option>"
        "</select></label>"
        f"<button type='submit'{disabled}>Review and start…</button></form>"
        + (f"<p class='detail'>{escape(reason)}</p>" if not eligible else "")
        + "<p class='detail'><strong>Successful validated runs upload one new patchset automatically.</strong> "
          "Reply text remains a draft and is never posted.</p></div>"
    )


def render_review_start_confirmation(
    patch, snapshot, *, mode: str, confirmation_token: str,
    idempotency_token: str, confirmation_expires_at: str, csrf_token: str,
    action: str = "/review-runs/start",
):
    label = "Handle simple comments" if mode == "simple" else "Handle all comments"
    return (
        "<main class='review-start-confirmation'><h1>Confirm review-comment run</h1>"
        f"<p><strong>{escape(label)}</strong> on change {escape(str(patch['change_number']))}, "
        f"PS {escape(str(patch['patchset']))} at <code>{escape(str(patch['revision_sha']))}</code>.</p>"
        f"<p>Exact unresolved snapshot: {len(snapshot.get('threads') or [])} thread(s), "
        f"<code>{escape(str(snapshot.get('snapshot_sha256') or ''))}</code>.</p>"
        "<p>This approval starts an isolated writable run and preauthorizes one controller-owned "
        "patchset upload only if the exact revision and comment snapshot remain unchanged, the "
        "result has a nonempty diff, and all guest validation passes with explicit test evidence. "
        "There is no later upload confirmation. Review replies remain drafts and are never posted.</p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(str(patch['change_number']), quote=True)}'>"
        f"<input type='hidden' name='patchset' value='{escape(str(patch['patchset']), quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(str(patch['revision_sha']), quote=True)}'>"
        f"<input type='hidden' name='review_mode' value='{escape(mode, quote=True)}'>"
        f"<input type='hidden' name='snapshot_sha256' value='{escape(str(snapshot['snapshot_sha256']), quote=True)}'>"
        f"<input type='hidden' name='confirmation_token' value='{escape(confirmation_token, quote=True)}'>"
        f"<input type='hidden' name='idempotency_token' value='{escape(idempotency_token, quote=True)}'>"
        f"<input type='hidden' name='confirmation_expires_at' value='{escape(confirmation_expires_at, quote=True)}'>"
        "<button type='submit'>Start review handling</button></form></main>"
    )


def render_review_result(request, report, upload=None):
    if not request or request.get("request_kind") != "review_comments":
        return ""
    rows = []
    for item in (report or {}).get("comment_results", ()):
        draft = item.get("reply_draft")
        rows.append(
            "<li><code>" + escape(str(item.get("comment_id") or "")) + "</code> — "
            + escape(str(item.get("assessment") or "")) + " / "
            + escape(str(item.get("disposition") or "")) + ": "
            + escape(str(item.get("summary") or ""))
            + ("<blockquote>Draft reply: " + escape(str(draft)) + "</blockquote>" if draft else "")
            + "</li>"
        )
    upload_state = getattr(upload, "state", None) if upload is not None else None
    return (
        "<section class='review-result'><h2>Review-comment handling</h2>"
        f"<p>Mode: <strong>{escape(str(request.get('review_mode') or ''))}</strong> · "
        f"snapshot <code>{escape(str(request.get('review_snapshot_sha256') or ''))}</code></p>"
        "<p>Patchset publication: <strong>"
        + escape(upload_state or "not ready")
        + "</strong>. Reply drafts are not posted.</p><ul>" + "".join(rows) + "</ul></section>"
    )


__all__ = [
    "render_review_result", "render_review_start_confirmation",
    "render_review_start_control",
]
