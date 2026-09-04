"""Compact, side-effect-free views for Jenkins build-failure handling."""

from __future__ import annotations

import re
from collections.abc import Mapping
from html import escape
from urllib.parse import urlsplit


_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}")


def _get(value, key, default=""):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _plain(value, default=""):
    if value is None or value == "" or isinstance(value, (Mapping, list, tuple, set)):
        return default
    return str(value)


def _build(snapshot):
    nested = _get(snapshot, "build")
    return nested if isinstance(nested, Mapping) else snapshot


def _change(snapshot):
    nested = _get(snapshot, "change")
    return nested if isinstance(nested, Mapping) else snapshot


def _job(snapshot):
    build = _build(snapshot)
    return _plain(_get(build, "job_name") or _get(build, "job"))


def _build_number(snapshot):
    return _plain(_get(_build(snapshot), "build_number") or _get(_build(snapshot), "number"))


def _build_url(snapshot):
    return _plain(_get(_build(snapshot), "build_url") or _get(_build(snapshot), "url"))


def _result(snapshot):
    return _plain(_get(_build(snapshot), "result")).upper()


def _digest(snapshot):
    return _plain(_get(snapshot, "snapshot_sha256"))


def _safe_jenkins_url(value):
    """Return an allowlisted Jenkins HTTPS URL, or an empty string."""

    text = _plain(value).strip()
    if not text or any(ord(character) < 32 for character in text):
        return ""
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != "build.whamcloud.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/job/")
    ):
        return ""
    return text


def _snapshot_matches(patch, snapshot):
    """Fail closed unless the snapshot is one exact, completed failed build."""

    if not isinstance(snapshot, Mapping) or _get(snapshot, "complete") is not True:
        return False
    change = _change(snapshot)
    try:
        same_change = int(_get(change, "change_number")) == int(_get(patch, "change_number"))
        same_patchset = int(_get(change, "patchset")) == int(_get(patch, "patchset"))
        positive_build = int(_build_number(snapshot)) > 0
    except (TypeError, ValueError):
        return False
    patch_revision = _plain(_get(patch, "revision_sha")).lower()
    snapshot_revision = _plain(_get(change, "revision_sha")).lower()
    return bool(
        same_change
        and same_patchset
        and patch_revision == snapshot_revision
        and _SHA_RE.fullmatch(patch_revision)
        and positive_build
        and _job(snapshot).strip()
        and _result(snapshot) in {"FAIL", "FAILURE"}
        and _DIGEST_RE.fullmatch(_digest(snapshot))
    )


def _build_identity(snapshot):
    job = escape(_job(snapshot))
    number = escape(_build_number(snapshot))
    url = _safe_jenkins_url(_build_url(snapshot))
    label = f"<strong>{job}</strong> build <code>#{number}</code>"
    if url:
        label = (
            f"<a href='{escape(url, quote=True)}' target='_blank' rel='noreferrer'>"
            + label + "</a>"
        )
    return label


def render_build_start_control(
    patch,
    snapshot,
    *,
    csrf_token: str,
    idempotency_token: str,
    build_eligible: bool = False,
    upload_enabled: bool = False,
    action: str = "/build-runs/prepare",
):
    """Render one inert POST control for an explicitly eligible failed build."""

    snapshot_valid = _snapshot_matches(patch, snapshot)
    discovery_url = _safe_jenkins_url(_get(patch, "jenkins_url"))
    discoverable = bool(
        str(_get(patch, "jenkins") or "").upper() in {"FAIL", "FAILURE"}
        and discovery_url
    )
    eligible = bool(
        build_eligible
        and upload_enabled
        and (snapshot_valid or discoverable)
        and not _get(patch, "active_run_id")
        and _plain(_get(patch, "lifecycle")).casefold() == "open"
    )
    if not upload_enabled:
        reason = "Automatic patchset upload is disabled by the controller kill switch."
    elif _get(patch, "active_run_id"):
        reason = "A managed run already owns this patch."
    elif not build_eligible:
        reason = "Build-failure handling is not explicitly eligible for this patch."
    elif not snapshot_valid and not discoverable:
        reason = "Refresh the complete failed Jenkins build snapshot for this exact revision."
    else:
        reason = "Only an exact open revision can start build-failure handling."
    disabled = " disabled aria-disabled='true'" if not eligible else ""
    if snapshot_valid:
        binding = (
            "Handle " + _build_identity(snapshot) + " on exact revision <code>"
            + escape(_plain(_get(patch, "revision_sha"))) + "</code> · snapshot <code>"
            + escape(_digest(snapshot)) + "</code>."
        )
    else:
        failure_label = "current Jenkins failure"
        if discovery_url:
            failure_label = (
                "<a href='" + escape(discovery_url, quote=True)
                + "' target='_blank' rel='noreferrer'>" + failure_label + "</a>"
            )
        binding = (
            "Handle the " + failure_label + ". Its exact job, build, revision, logs, "
            "and digest will be captured before confirmation."
        )
    return (
        "<div class='build-start'>"
        "<p class='build-binding'>" + binding + "</p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(_plain(_get(patch, 'change_number')), quote=True)}'>"
        f"<input type='hidden' name='patchset' value='{escape(_plain(_get(patch, 'patchset')), quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(_plain(_get(patch, 'revision_sha')), quote=True)}'>"
        f"<input type='hidden' name='build_job' value='{escape(_job(snapshot), quote=True)}'>"
        f"<input type='hidden' name='build_number' value='{escape(_build_number(snapshot), quote=True)}'>"
        f"<input type='hidden' name='build_snapshot_sha256' value='{escape(_digest(snapshot), quote=True)}'>"
        f"<input type='hidden' name='idempotency_token' value='{escape(idempotency_token, quote=True)}'>"
        f"<button type='submit'{disabled}>Handle build failure…</button></form>"
        + (f"<p class='detail' role='status'>{escape(reason)}</p>" if not eligible else "")
        + "<p class='detail'><strong>A successful validated run uploads one new patchset "
          "automatically; there is no later upload confirmation.</strong> Commands are open-ended "
          "only inside exact-owner LTVM guests. The worker has no host shell or Gerrit credentials.</p>"
          "</div>"
    )


def render_build_start_confirmation(
    patch,
    snapshot,
    *,
    confirmation_token: str,
    idempotency_token: str,
    confirmation_expires_at: str,
    csrf_token: str,
    action: str = "/build-runs/start",
):
    """Render the single run-start approval for one exact failed build."""

    if not _snapshot_matches(patch, snapshot):
        raise ValueError("build snapshot does not match the exact failed revision")
    return (
        "<main class='build-start-confirmation'><h1>Confirm build-failure run</h1>"
        "<p>Handle " + _build_identity(snapshot) + " for change "
        + escape(_plain(_get(patch, "change_number"))) + ", PS "
        + escape(_plain(_get(patch, "patchset"))) + " at <code>"
        + escape(_plain(_get(patch, "revision_sha"))) + "</code>.</p>"
        "<p>Immutable failed-build snapshot: <code>" + escape(_digest(snapshot))
        + "</code>.</p>"
        "<p>This single approval starts an isolated writable run and grants open-ended, audited "
          "commands only inside LTVM guests owned by this exact session. It grants no host command "
          "execution and gives the worker no Gerrit credentials.</p>"
        "<p>It also preauthorizes one controller-owned patchset upload only if the Gerrit revision "
          "and failed-build snapshot remain exact, the result has a nonempty diff, and guest "
          "validation passes with explicit evidence. <strong>There is no later upload "
          "confirmation.</strong> Any uncertainty escalates to a human without widening authority.</p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='change_number' value='{escape(_plain(_get(patch, 'change_number')), quote=True)}'>"
        f"<input type='hidden' name='patchset' value='{escape(_plain(_get(patch, 'patchset')), quote=True)}'>"
        f"<input type='hidden' name='revision_sha' value='{escape(_plain(_get(patch, 'revision_sha')), quote=True)}'>"
        f"<input type='hidden' name='build_job' value='{escape(_job(snapshot), quote=True)}'>"
        f"<input type='hidden' name='build_number' value='{escape(_build_number(snapshot), quote=True)}'>"
        f"<input type='hidden' name='build_snapshot_sha256' value='{escape(_digest(snapshot), quote=True)}'>"
        f"<input type='hidden' name='confirmation_token' value='{escape(confirmation_token, quote=True)}'>"
        f"<input type='hidden' name='idempotency_token' value='{escape(idempotency_token, quote=True)}'>"
        f"<input type='hidden' name='confirmation_expires_at' value='{escape(confirmation_expires_at, quote=True)}'>"
        "<button type='submit'>Start build handling</button></form></main>"
    )


def _validation_html(report):
    validation = _get(report, "validation")
    if not isinstance(validation, Mapping):
        validation = {}
    state = _plain(_get(validation, "state") or _get(report, "validation_state"), "not ready")
    summary = _plain(_get(validation, "summary") or _get(report, "validation_summary"))
    evidence = _get(validation, "evidence") or _get(report, "test_evidence") or ()
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, (list, tuple)):
        evidence = ()
    rows = []
    for item in evidence[:100]:
        if isinstance(item, Mapping):
            label = _plain(_get(item, "label") or _get(item, "name") or _get(item, "kind"), "Evidence")
            detail = _plain(_get(item, "summary") or _get(item, "detail") or _get(item, "result"))
        else:
            label, detail = "Evidence", _plain(item)
        rows.append("<li><strong>" + escape(label) + "</strong> — " + escape(detail) + "</li>")
    return (
        "<section class='build-validation'><h3>Validation</h3><p>Status: <strong>"
        + escape(state) + "</strong>" + (" — " + escape(summary) if summary else "") + ".</p>"
        + ("<ul>" + "".join(rows) + "</ul>" if rows else "<p>No validation evidence recorded.</p>")
        + "</section>"
    )


def _escalation_html(report):
    escalation = _get(report, "human_escalation") or _get(report, "escalation")
    state = _plain(_get(report, "state")).casefold()
    if not escalation and state not in {"needs_human", "needs_input", "waiting_human"}:
        return "<section class='build-escalation'><h3>Human escalation</h3><p>None recorded.</p></section>"
    if isinstance(escalation, Mapping):
        reason = _plain(_get(escalation, "reason") or _get(escalation, "summary"))
        question = _plain(_get(escalation, "question"))
        recommended = _plain(_get(escalation, "recommended") or _get(escalation, "recommended_default"))
    else:
        reason, question, recommended = _plain(escalation), "", ""
    reason = reason or "The run requires human judgment before it can continue safely."
    return (
        "<section class='build-escalation' role='alert'><h3>Human escalation required</h3>"
        "<p><strong>Reason:</strong> " + escape(reason) + "</p>"
        + ("<p><strong>Question:</strong> " + escape(question) + "</p>" if question else "")
        + ("<p><strong>Recommended safe default:</strong> " + escape(recommended) + "</p>" if recommended else "")
        + "</section>"
    )


def render_build_result(request, report, upload=None):
    """Render diagnosis, validation, publication, and escalation for a build run."""

    if not request or _get(request, "request_kind") != "build_failure":
        return ""
    snapshot = _get(request, "build_snapshot")
    if not isinstance(snapshot, Mapping):
        snapshot = request
    diagnosis = _plain(
        _get(report, "diagnosis") or _get(report, "diagnosis_summary") or _get(report, "summary"),
        "No diagnosis recorded.",
    )
    upload_state = _plain(_get(upload, "state"), "not ready")
    new_patchset = _plain(_get(upload, "new_patchset"))
    new_revision = _plain(_get(upload, "new_revision_sha"))
    upload_summary = _plain(_get(upload, "summary"))
    publication_detail = ""
    if new_patchset:
        publication_detail += " · new patchset " + escape(new_patchset)
    if new_revision:
        publication_detail += " at <code>" + escape(new_revision) + "</code>"
    if upload_summary:
        publication_detail += " — " + escape(upload_summary)
    return (
        "<section class='build-result'><h2>Build-failure handling</h2>"
        "<p>Bound to " + _build_identity(snapshot) + " · exact revision <code>"
        + escape(_plain(_get(_change(snapshot), "revision_sha") or _get(request, "revision")))
        + "</code> · snapshot <code>" + escape(_digest(snapshot) or _plain(_get(request, "build_snapshot_sha256")))
        + "</code>.</p>"
        "<section class='build-diagnosis'><h3>Diagnosis</h3><p>" + escape(diagnosis) + "</p></section>"
        + _validation_html(report or {})
        + "<section class='build-publication'><h3>Patchset publication</h3><p>Status: <strong>"
        + escape(upload_state) + "</strong>" + publication_detail
        + ".</p><p>A qualifying result uploads automatically under the run-start authorization; "
          "there is no later upload confirmation.</p></section>"
        + _escalation_html(report or {}) + "</section>"
    )


__all__ = [
    "render_build_result",
    "render_build_start_confirmation",
    "render_build_start_control",
]
