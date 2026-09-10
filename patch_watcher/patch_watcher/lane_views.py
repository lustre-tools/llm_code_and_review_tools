"""Compact, side-effect-free HTML views for autonomous-lane controls.

The rendering boundary accepts mappings, dataclasses/attribute objects, and
objects exposing ``to_dict()``.  It deliberately does not import the lane
engine: the controller remains responsible for authentication, CSRF checks,
fresh exact-revision validation, capability enforcement, and all writes.
"""

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


def _hidden(name, value):
    return (
        f"<input type='hidden' name='{escape(name, quote=True)}' "
        f"value='{escape(_text(value, ''), quote=True)}'>"
    )


def _csrf(csrf_token):
    return _hidden("csrf_token", csrf_token)


def _switch_badge(label, value):
    """Render a switch as "&lt;label&gt;: Enabled/Disabled".

    The label must name what the *value* is true of, never its inverse. A badge
    labelled "kill switch" showing "Enabled" for a live system inverts the one
    meaning an operator reads under pressure.
    """
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
        return "<p class='unknown'>Budgets are unknown; no unattended action is permitted.</p>"
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


def _render_replay_control(status, *, csrf_token, action):
    replay = _get(status, "replay", "dry_run", "latest_replay")
    state = _human(_get(replay, "state", "status"))
    explanation = _text(_get(replay, "summary", "explanation", "message"), "No replay has run.")
    return (
        "<div class='lane-replay-status' role='status'>"
        f"<strong>Latest dry run / replay:</strong> {escape(state)} · {escape(explanation)}</div>"
        f"<form class='lane-replay-control' method='post' action='{escape(action, quote=True)}'>"
        # No revision: this control replays the whole recorded history, which
        # is what its label says. `mode` was never read by the replay route --
        # and `dry_run` is not even in the set that route's siblings accept.
        f"{_csrf(csrf_token)}"
        "<button type='submit'>Replay every recorded decision</button></form>"
    )


def render_autonomous_lane_summary(
    status=None, *, csrf_token="",
    replay_action="/autonomous-lanes/replay",
    nested=False,
):
    """Render lane identity, kill switches, budgets, outcomes, and replay.

    `nested` renders it as a stage inside one automation card rather than a
    card of its own, because a separate top-level card made this read as a
    different feature from the saved-policy gate it sits behind.
    """
    status = _project(status)
    lane_name, lane_version = _lane_identity(status)
    enabled = _get(status, "global_enabled", "enabled")
    budgets = _get(status, "budgets", "capability_budgets", "budget")
    outcomes = _get(status, "outcomes", "recent_outcomes", default=[])
    heading = "h3" if nested else "h2"
    return (
        f"<section class='autonomous-lanes{'' if nested else ' card'}' "
        "aria-labelledby='autonomous-lanes-title'>"
        f"<header><{heading} id='autonomous-lanes-title'>Acting without asking"
        f"</{heading}>"
        f"{_switch_badge('Unattended actions', enabled)}</header>"
        f"<p>One rule is configured: <strong>{escape(lane_name)}</strong> "
        f"<span class='lane-version'>version {escape(lane_version)}</span>. It requests "
        "a single retest for one exact failure snapshot whose failures were all "
        "classified deterministic, and nothing else.</p>"
        "<p class='authority-boundary'><strong>What this does not do:</strong> being "
        "eligible grants no credentials and no broader authority. The capability gates "
        "and exact-revision checks still apply, and the global policy gate stops it "
        "like everything else.</p>"
        "<p class='detail'>There is nothing to switch here: a patch takes part when its "
        "level is <strong>Known retests</strong> or higher, and leaves when it is set "
        "back to <strong>Watch only</strong>.</p>"
        "<details><summary>Budgets and recent outcomes</summary><h3>Capability budgets</h3>"
        f"{_render_budgets(budgets)}<h3>Recent outcomes</h3>{_render_outcomes(outcomes)}</details>"
        "<details><summary>Dry run and replay</summary>"
        f"{_render_replay_control(status, csrf_token=csrf_token, action=replay_action)}</details>"
        "</section>"
    )


