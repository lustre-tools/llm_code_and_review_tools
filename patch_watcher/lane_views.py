"""Compact, side-effect-free HTML views for autonomous-lane controls.

The rendering boundary accepts mappings, dataclasses/attribute objects, and
objects exposing ``to_dict()``.  It deliberately does not import the lane
engine: the controller remains responsible for authentication, CSRF checks,
fresh exact-revision validation, capability enforcement, and all writes.
"""

import hashlib
from collections.abc import Mapping
from html import escape


UNKNOWN = "unknown"
MAX_OUTCOMES = 8


def _project(record):
    if record is None or isinstance(record, Mapping):
        return record
    method = getattr(record, "to_dict", None)
    if callable(method):
        value = method()
        if isinstance(value, Mapping):
            return value
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


def _text(value, default=UNKNOWN):
    if value is None or value == "":
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _human(value):
    value = _text(value)
    if value == UNKNOWN:
        return value
    return value.replace("_", " ").replace("-", " ").capitalize()


def _truth(value):
    """Only explicit booleans enable a safety-sensitive display state."""
    return value is True


def _hidden(name, value):
    return (
        f"<input type='hidden' name='{escape(name, quote=True)}' "
        f"value='{escape(_text(value, ''), quote=True)}'>"
    )


def _csrf(csrf_token):
    return _hidden("csrf_token", csrf_token)


def _switch_badge(label, value):
    if value is True:
        state, tone = "Enabled", "good"
    elif value is False:
        state, tone = "Disabled", "bad"
    else:
        state, tone = "Unknown (treated as disabled)", "neutral"
    return (
        f"<span class='lane-switch tone-{tone}'>"
        f"{escape(label)}: {state}</span>"
    )


def _identity(record):
    change = _get(record, "change_number", "change", "patch_id")
    patchset = _get(record, "patchset", "patch_set")
    revision = _get(record, "revision_sha", "revision", "commit_sha")
    return change, patchset, revision


def _identity_html(record):
    change, patchset, revision = _identity(record)
    return (
        f"change {escape(_text(change))}, PS {escape(_text(patchset))}, "
        f"revision <code>{escape(_text(revision))}</code>"
    )


def _lane_identity(*records):
    lane = None
    version = None
    for record in records:
        if lane is None:
            lane = _get(record, "lane_name", "name", "configured_lane")
            nested = _get(record, "lane")
            if lane is None and nested is not None:
                lane = _get(nested, "lane_name", "name", "id")
        if version is None:
            version = _get(record, "lane_version", "version", "configured_version")
            nested = _get(record, "lane")
            if version is None and nested is not None:
                version = _get(nested, "lane_version", "version")
    return _text(lane), _text(version)


def _definition(label, value, *, code=False):
    rendered = escape(_text(value))
    if code:
        rendered = f"<code>{rendered}</code>"
    return f"<div><dt>{escape(label)}</dt><dd>{rendered}</dd></div>"


def _budget_items(budgets):
    budgets = _project(budgets)
    if budgets is None:
        return []
    if isinstance(budgets, Mapping):
        return [(str(key), value) for key, value in budgets.items()]
    result = []
    for item in _items(budgets):
        label = _get(item, "label", "name", "kind", default="Budget")
        value = _get(item, "display", "value", "limit")
        used = _get(item, "used")
        if used is not None:
            value = f"{_text(used)} used / {_text(value)} limit"
        result.append((_text(label), value))
    return result


def _render_budgets(budgets):
    items = _budget_items(budgets)
    if not items:
        return "<p class='unknown'>Budgets are unknown; no autonomous action is permitted.</p>"
    return "<dl class='lane-metrics'>" + "".join(
        _definition(_human(label), value) for label, value in items
    ) + "</dl>"


def _outcome_fields(outcome):
    if isinstance(outcome, (str, bytes)):
        return _text(outcome), UNKNOWN, UNKNOWN
    return (
        _text(_get(outcome, "summary", "explanation", "message", "outcome")),
        _text(_get(outcome, "state", "status", "result")),
        _text(_get(outcome, "occurred_at", "created_at", "finished_at", "timestamp")),
    )


def _render_outcomes(outcomes):
    values = _items(outcomes)[-MAX_OUTCOMES:]
    if not values:
        return "<p>No lane outcomes recorded.</p>"
    return "<ol class='lane-outcomes'>" + "".join(
        "<li>"
        f"<span class='lane-outcome-state'>{escape(_human(_outcome_fields(item)[1]))}</span> "
        f"{escape(_outcome_fields(item)[0])} "
        f"<small>{escape(_outcome_fields(item)[2])}</small></li>"
        for item in values
    ) + "</ol>"


def _mode(record, default="inherit"):
    value = _text(_get(record, "mode", "override", "state"), default).casefold()
    return value if value in {"inherit", "enabled", "disabled"} else default


def _option(value, current, label):
    selected = " selected" if value == current else ""
    return f"<option value='{value}'{selected}>{escape(label)}</option>"


def _override_form(*, scope, identity, current, csrf_token, action, record=None):
    digest = hashlib.sha256(f"{scope}\0{_text(identity)}".encode()).hexdigest()[:12]
    field_id = f"lane-{scope}-mode-{digest}"
    return (
        f"<form class='lane-override compact-form' method='post' action='{escape(action, quote=True)}'>"
        f"{_csrf(csrf_token)}{_hidden('scope', scope)}{_hidden(f'{scope}_id', identity)}"
        f"{_hidden('expected_generation', _get(record, 'expected_generation', 'generation', default=0))}"
        f"{_hidden('project', _get(record, 'project', 'project_id', default=''))}"
        f"{_hidden('lane_name', _get(record, 'lane_name', default='deterministic-test-retest'))}"
        f"{_hidden('lane_version', _get(record, 'lane_version', default=1))}"
        f"<label for='{field_id}'>{escape(_human(scope))} override</label>"
        f"<select id='{field_id}' name='mode'>"
        f"{_option('inherit', current, 'Inherit')}{_option('enabled', current, 'Enable')}"
        f"{_option('disabled', current, 'Disable / kill switch')}</select>"
        "<button type='submit'>Save</button></form>"
    )


def _render_projects(projects, *, csrf_token, action):
    rows = []
    for project in _items(projects):
        project_id = _get(project, "project_id", "project", "name")
        if project_id is None:
            continue
        current = _mode(project)
        rows.append(
            "<li><strong>" + escape(_text(project_id)) + "</strong> "
            + _switch_badge("Effective", _get(project, "effective_enabled", "enabled"))
            + _override_form(
                scope="project", identity=project_id, current=current,
                csrf_token=csrf_token, action=action, record=project,
            ) + "</li>"
        )
    if not rows:
        return "<p>No project overrides configured.</p>"
    return "<ul class='lane-project-overrides'>" + "".join(rows) + "</ul>"


def _render_global_switch(status, *, csrf_token, action):
    enabled = _truth(_get(status, "global_enabled", "enabled"))
    next_value = "disabled" if enabled else "enabled"
    label = "Disable all autonomous lanes" if enabled else "Enable autonomous lanes…"
    return (
        f"<form class='lane-global-switch' method='post' action='{escape(action, quote=True)}'>"
        f"{_csrf(csrf_token)}{_hidden('mode', next_value)}"
        f"{_hidden('expected_generation', _get(status, 'expected_generation', 'generation', default=0))}"
        f"<button type='submit'>{escape(label)}</button></form>"
    )


def _render_replay_control(status, *, csrf_token, action):
    replay = _get(status, "replay", "dry_run", "latest_replay")
    state = _human(_get(replay, "state", "status"))
    explanation = _text(_get(replay, "summary", "explanation", "message"), "No replay has run.")
    return (
        "<div class='lane-replay-status' role='status'>"
        f"<strong>Latest dry run / replay:</strong> {escape(state)} · {escape(explanation)}</div>"
        f"<form class='lane-replay-control' method='post' action='{escape(action, quote=True)}'>"
        f"{_csrf(csrf_token)}{_hidden('mode', 'dry_run')}"
        "<button type='submit'>Dry-run historical observations</button></form>"
    )


def render_autonomous_lane_summary(
    status=None, *, csrf_token="",
    global_action="/autonomous-lanes/global",
    project_action="/autonomous-lanes/project",
    replay_action="/autonomous-lanes/replay",
):
    """Render lane identity, kill switches, budgets, outcomes, and replay."""
    status = _project(status)
    lane_name, lane_version = _lane_identity(status)
    enabled = _get(status, "global_enabled", "enabled")
    projects = _get(status, "project_overrides", "projects", default=[])
    budgets = _get(status, "budgets", "capability_budgets", "budget")
    outcomes = _get(status, "outcomes", "recent_outcomes", default=[])
    return (
        "<section class='autonomous-lanes card' aria-labelledby='autonomous-lanes-title'>"
        "<header><h2 id='autonomous-lanes-title'>Autonomous lanes</h2>"
        f"{_switch_badge('Global kill switch', enabled)}</header>"
        f"<p>Configured lane: <strong>{escape(lane_name)}</strong> "
        f"<span class='lane-version'>version {escape(lane_version)}</span></p>"
        "<p class='authority-boundary'><strong>Authority boundary:</strong> Lane eligibility "
        "does not grant credentials or broader worker authority. Existing controller "
        "capability gates, exact-revision checks, and project/patch kill switches still apply.</p>"
        f"{_render_global_switch(status, csrf_token=csrf_token, action=global_action)}"
        "<details><summary>Project overrides</summary>"
        f"{_render_projects(projects, csrf_token=csrf_token, action=project_action)}</details>"
        "<details><summary>Budgets and recent outcomes</summary><h3>Capability budgets</h3>"
        f"{_render_budgets(budgets)}<h3>Recent outcomes</h3>{_render_outcomes(outcomes)}</details>"
        "<details><summary>Dry run and replay</summary>"
        f"{_render_replay_control(status, csrf_token=csrf_token, action=replay_action)}</details>"
        "</section>"
    )


def _eligibility_badge(evaluation, *, stale=False):
    eligible = _get(evaluation, "eligible")
    if stale:
        label, tone = "Stale decision", "warn"
    elif eligible is True:
        label, tone = "Eligible", "good"
    elif eligible is False:
        label, tone = "Rejected", "bad"
    else:
        label, tone = "Not evaluated", "neutral"
    return f"<span class='lane-eligibility tone-{tone}'>{label}</span>"


def _complete_identity(record):
    return all(value is not None and value != "" for value in _identity(record))


def _stale_evaluation(evaluation, patch):
    if not (_complete_identity(evaluation) and _complete_identity(patch)):
        return False
    return tuple(_text(value) for value in _identity(evaluation)) != tuple(
        _text(value) for value in _identity(patch)
    )


def _render_patch_replay(patch, replay, *, csrf_token, action):
    change, patchset, revision = _identity(patch)
    state = _human(_get(replay, "state", "status"))
    explanation = _text(_get(replay, "summary", "explanation"), "No exact-revision replay recorded.")
    return (
        f"<p class='lane-patch-replay' role='status'>Replay: {escape(state)} · "
        f"{escape(explanation)}</p>"
        f"<form method='post' action='{escape(action, quote=True)}'>"
        f"{_csrf(csrf_token)}{_hidden('change_number', change)}"
        f"{_hidden('patchset', patchset)}{_hidden('revision_sha', revision)}"
        f"{_hidden('mode', 'dry_run')}"
        "<button type='submit'>Replay this exact revision</button></form>"
    )


def render_patch_lane_controls(
    patch, *, policy=None, evaluation=None, outcome=None, replay=None,
    csrf_token="", patch_action="/autonomous-lanes/patch",
    replay_action="/autonomous-lanes/replay",
):
    """Render one patch's override and latest exact-revision lane evidence."""
    patch = _project(patch)
    policy = _project(policy)
    evaluation = _project(evaluation)
    change, _, _ = _identity(patch)
    patch_id = _get(patch, "patch_id", default=change)
    lane_name, lane_version = _lane_identity(policy, evaluation)
    current = _mode(policy)
    explanation = _text(
        _get(evaluation, "explanation", "reason", "message"),
        "No exact-revision eligibility decision has been recorded.",
    )
    code = _text(_get(evaluation, "code", "reason_code"))
    effective = _get(policy, "effective_enabled", "enabled")
    stale = _stale_evaluation(evaluation, patch)
    title_digest = hashlib.sha256(_text(patch_id).encode()).hexdigest()[:12]
    title_id = f"patch-lane-title-{title_digest}"
    if _complete_identity(evaluation):
        decision_identity = _identity_html(evaluation)
    else:
        decision_identity = "Exact revision identity unavailable"
    stale_warning = (
        "<p class='lane-stale-warning' role='alert'>This decision is for a different "
        "revision and cannot authorize the current patch revision.</p>"
        if stale else ""
    )
    return (
        f"<section class='patch-lane-controls' aria-labelledby='{title_id}'>"
        f"<h3 id='{title_id}'>Autonomous lane</h3>"
        f"<p>{_switch_badge('Patch', effective)} "
        f"Lane <strong>{escape(lane_name)}</strong> version {escape(lane_version)}</p>"
        f"{_override_form(scope='patch', identity=patch_id, current=current, csrf_token=csrf_token, action=patch_action, record=policy)}"
        "<p class='authority-boundary'>Enabling this patch only opts it into the named lane. "
        "It does not grant credentials, expand the lane budget, or bypass any broader kill switch.</p>"
        "<div class='lane-latest-evaluation'>"
        f"<h4>Latest exact-revision decision {_eligibility_badge(evaluation, stale=stale)}</h4>"
        f"<p>{decision_identity}</p>{stale_warning}"
        f"<p><code>{escape(code)}</code>: {escape(explanation)}</p></div>"
        "<details><summary>Budget use, outcome, and replay</summary>"
        f"{_render_budgets(_get(evaluation, 'budgets', 'budget_use', 'capability_budgets'))}"
        f"<h4>Latest outcome</h4>{_render_outcomes([] if outcome is None else [outcome])}"
        f"{_render_patch_replay(patch, replay, csrf_token=csrf_token, action=replay_action)}"
        "</details></section>"
    )
