"""Series statistics computed at build time.

`add_timeline()` resolves each node's lifecycle times from the raw
Gerrit timestamps; `series_summary()` reduces them to the headline
numbers in the payload's `stats.summary`. The Stats tab renders
both as-is, and anything that reads the payload without running the
page (the portal's graph list) gets the same numbers.

All times are epoch seconds (UTC); a missing time is None. "Recent"
figures are relative to the build (`as_of`); `recent_events` keeps
the raw timestamps so a reader can recount against its own clock.
"""

from datetime import datetime, timezone
from typing import Any

from .nodes import gerrit_epoch

DAY = 86400
RECENT_DAYS = 60


def add_timeline(node: dict[str, Any]) -> None:
    """Set opened_at, closed_at, closed_approx, last_activity and
    first_review_at on a node.

    opened_at falls back from the change's creation to its first
    patchset, current patchset, then last update. A closed change
    with no submit or abandon time (abandon messages not fetched)
    uses its last update as closed_at and is flagged closed_approx.
    last_activity is the latest patchset upload or human review.
    """
    ps = node.get("ps_times") or []
    rv = node.get("review_times") or []
    updated = gerrit_epoch(node.get("updated", ""))
    opened = (
        gerrit_epoch(node.get("created", ""))
        or (ps[0] if ps else 0)
        or gerrit_epoch(node.get("current_ps_created", ""))
        or updated
    )
    closed = 0
    approx = False
    status = node.get("status")
    if status == "MERGED":
        closed = gerrit_epoch(node.get("submitted", ""))
    elif status == "ABANDONED":
        closed = gerrit_epoch(node.get("abandoned_at", ""))
    if status != "NEW" and not closed and updated:
        closed = updated
        approx = True
    if closed and opened and closed < opened:
        closed = opened
    last = max(ps[-1] if ps else 0, rv[-1] if rv else 0) or updated
    first_review = next((t for t in rv if t >= opened), 0) if opened else 0
    node["opened_at"] = opened or None
    node["closed_at"] = closed or None
    node["closed_approx"] = approx
    node["last_activity"] = last or None
    node["first_review_at"] = first_review or None


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _duration_stats(values: list[int]) -> dict[str, Any]:
    vals = sorted(values)
    if not vals:
        return {"count": 0, "median": None, "p90": None}
    return {
        "count": len(vals),
        "median": round(_quantile(vals, 0.5)),
        "p90": round(_quantile(vals, 0.9)),
    }


def _months_back(as_of: int, count: int) -> list[str]:
    """"YYYY-MM" for the `count` calendar months ending with as_of's."""
    d = datetime.fromtimestamp(as_of, tz=timezone.utc)
    y, m = d.year, d.month
    months = []
    for _ in range(count):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return months[::-1]


def _month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def series_summary(
    nodes: list[dict[str, Any]], as_of: int, review_activity: bool,
) -> dict[str, Any]:
    """Headline numbers over the series' own patches: nodes kept only
    as a branch base (trunk_structural) are not counted. Nodes must
    already carry add_timeline()'s fields."""
    recs = [
        n for n in nodes
        if not n.get("trunk_structural") and n.get("opened_at")
    ]
    open_ = [n for n in recs if n["status"] == "NEW"]
    merged = [n for n in recs if n["status"] == "MERGED"]
    abandoned = [n for n in recs if n["status"] == "ABANDONED"]

    def window(lo: int, hi: int, times: list[int | None]) -> int:
        return sum(1 for t in times if t is not None and lo < t <= hi)

    d30, d60 = as_of - 30 * DAY, as_of - RECENT_DAYS * DAY
    opened_t = [n["opened_at"] for n in recs]
    merged_t = [n["closed_at"] for n in merged]
    abandoned_t = [n["closed_at"] for n in abandoned]

    ps_merged = sorted(
        n.get("current_patchset") or len(n.get("ps_times") or []) or 1
        for n in merged
    )
    oldest = min(open_, key=lambda n: n["opened_at"], default=None)
    idle = min(
        (n for n in open_ if n.get("last_activity")),
        key=lambda n: n["last_activity"], default=None,
    )
    months = _months_back(as_of, 12)
    per_month = dict.fromkeys(months, 0)
    for t in merged_t:
        if t is not None and _month_key(t) in per_month:
            per_month[_month_key(t)] += 1

    return {
        "as_of": as_of,
        "patches": len(recs),
        "open": len(open_),
        "merged": len(merged),
        "abandoned": len(abandoned),
        "last_30d": {
            "opened": window(d30, as_of, opened_t),
            "merged": window(d30, as_of, merged_t),
            "abandoned": window(d30, as_of, abandoned_t),
        },
        "prev_30d": {
            "opened": window(d60, d30, opened_t),
            "merged": window(d60, d30, merged_t),
            "abandoned": window(d60, d30, abandoned_t),
        },
        "open_30d_ago": sum(
            1 for n in recs
            if n["opened_at"] <= d30
            and not (n["closed_at"] is not None and n["closed_at"] <= d30)
        ),
        "recent_events": {
            key: sorted(t for t in times if t is not None and d60 < t <= as_of)
            for key, times in (
                ("opened", opened_t), ("merged", merged_t),
                ("abandoned", abandoned_t),
            )
        },
        "time_to_merge": _duration_stats([
            n["closed_at"] - n["opened_at"]
            for n in merged if n["closed_at"] is not None
        ]),
        "time_to_first_review": _duration_stats([
            n["first_review_at"] - n["opened_at"]
            for n in recs if n["first_review_at"] is not None
        ]) if review_activity else None,
        "patchsets_to_merge": {
            "count": len(ps_merged),
            "median": _quantile(ps_merged, 0.5) if ps_merged else None,
            "max": ps_merged[-1] if ps_merged else None,
        },
        "oldest_open": {
            "id": oldest["id"], "ticket": oldest.get("ticket", ""),
            "opened_at": oldest["opened_at"],
        } if oldest else None,
        "longest_idle": {
            "id": idle["id"], "ticket": idle.get("ticket", ""),
            "last_activity": idle["last_activity"],
        } if idle else None,
        "merged_by_month": [[m, per_month[m]] for m in months],
    }
