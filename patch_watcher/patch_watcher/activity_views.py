"""Activity over time, as charts that need nothing to render them.

Inline SVG, no script and no library.  The console is reached through an SSH
tunnel on a workstation that is sometimes offline, so anything fetched from a
CDN is a chart that is blank exactly when someone is trying to find out what
happened.

Every chart is also a table.  The table is not a fallback nobody sees: a bar
answers "was yesterday unusual" at a glance and cannot answer "how much did
Thursday cost", and both questions get asked of the same page.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

CHART_WIDTH = 720
CHART_HEIGHT = 180
BAR_GAP = 3
# Enough to show a fortnight without the bars becoming threads.
DEFAULT_DAYS = 21


@dataclass(frozen=True)
class DayBucket:
    """One day's activity, in the reader's own timezone."""

    day: str
    runs: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    turns: int = 0
    failures: int = 0


def bucket_by_day(usage, sessions=(), *, days: int = DEFAULT_DAYS, now=None):
    """Group runs and their cost into consecutive local days, oldest first.

    Consecutive, including days with nothing on them: a bar chart that omits
    empty days draws a busy fortnight and a quiet one identically, which is
    the one comparison the chart exists to make.
    """
    observed = now or datetime.now().astimezone()
    span = max(1, int(days))
    order = [
        (observed - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(span - 1, -1, -1)
    ]
    buckets = {day: {"runs": 0, "cost": 0.0, "tokens": 0, "turns": 0, "failures": 0}
               for day in order}

    for record in usage or ():
        stamp = getattr(record, "recorded_at", None)
        if stamp is None:
            continue
        day = stamp.astimezone().strftime("%Y-%m-%d")
        if day not in buckets:
            continue
        buckets[day]["runs"] += 1
        buckets[day]["cost"] += float(getattr(record, "cost_usd", 0.0) or 0.0)
        buckets[day]["tokens"] += int(getattr(record, "total_tokens", 0) or 0)
        buckets[day]["turns"] += int(getattr(record, "turns", 0) or 0)

    for session in sessions or ():
        state = str(getattr(session, "state", ""))
        if state in {"", "succeeded", "running", "queued", "preparing", "paused"}:
            continue
        stamp = getattr(session, "state_changed_at", None)
        if stamp is None:
            continue
        day = stamp.astimezone().strftime("%Y-%m-%d")
        if day in buckets:
            buckets[day]["failures"] += 1

    return [
        DayBucket(
            day=day,
            runs=values["runs"],
            cost_usd=values["cost"],
            tokens=values["tokens"],
            turns=values["turns"],
            failures=values["failures"],
        )
        for day, values in ((day, buckets[day]) for day in order)
    ]


def _bar_chart(buckets: Sequence[DayBucket], value, *, label: str, fmt,
               tone: str = "normal") -> str:
    """One labelled SVG bar chart, or a sentence when there is nothing to plot."""
    values = [float(value(bucket)) for bucket in buckets]
    peak = max(values, default=0.0)
    if peak <= 0:
        return f"<p class='detail'>No {escape(label.lower())} in this period.</p>"
    count = len(buckets)
    slot = CHART_WIDTH / count
    width = max(1.0, slot - BAR_GAP)
    bars = []
    for index, (bucket, amount) in enumerate(zip(buckets, values, strict=False)):
        if not amount:
            continue  # a day with nothing on it is the gap, not a flat bar
        height = (amount / peak) * (CHART_HEIGHT - 20)
        # A day too small to see still gets a visible mark: a bar of zero
        # height reads as "nothing happened", which is a different fact from
        # "a little happened".
        height = max(height, 2.0)
        x = index * slot
        y = CHART_HEIGHT - height
        bars.append(
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{width:.1f}' "
            f"height='{height:.1f}' rx='1'><title>{escape(bucket.day)}: "
            f"{escape(fmt(amount))}</title></rect>"
        )
    first, last = buckets[0].day, buckets[-1].day
    return (
        f"<figure class='chart chart-{escape(tone)}'>"
        f"<figcaption>{escape(label)} "
        f"<span class='detail'>peak {escape(fmt(peak))}</span></figcaption>"
        f"<svg viewBox='0 0 {CHART_WIDTH} {CHART_HEIGHT}' role='img' "
        f"aria-label='{escape(label)} per day from {escape(first)} to {escape(last)}' "
        "preserveAspectRatio='none'>"
        + "".join(bars)
        + "</svg>"
        f"<p class='detail chart-axis'><span>{escape(first)}</span>"
        f"<span>{escape(last)}</span></p></figure>"
    )


def _format_cost(amount: float) -> str:
    return f"${amount:,.2f}"


def _format_tokens(amount: float) -> str:
    count = int(amount)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def render_activity(
    buckets: Collection[DayBucket], *, totals: Mapping | None = None, divisor: float = 37.0
) -> str:
    """The activity page: what ran, what it cost, and when."""
    items = list(buckets)
    if not items:
        return (
            "<section class='card'><h2>Activity</h2>"
            "<p class='detail'>No runs have been recorded yet.</p></section>"
        )
    charts = (
        _bar_chart(items, lambda b: b.runs, label="Runs", fmt=lambda v: f"{int(v)}")
        + _bar_chart(
            items, lambda b: b.cost_usd / divisor if divisor else b.cost_usd,
            label="Spend on subscription", fmt=_format_cost,
        )
        + _bar_chart(items, lambda b: b.cost_usd, label="Spend at list price",
                     fmt=_format_cost)
        + _bar_chart(items, lambda b: b.tokens, label="Tokens", fmt=_format_tokens)
        + _bar_chart(items, lambda b: b.failures, label="Runs that did not succeed",
                     fmt=lambda v: f"{int(v)}", tone="bad")
    )
    rows = []
    for bucket in reversed(items):
        if not (bucket.runs or bucket.failures):
            continue
        rows.append(
            "<tr><th scope='row'>" + escape(bucket.day) + "</th>"
            f"<td>{bucket.runs}</td>"
            f"<td>{bucket.failures}</td>"
            f"<td>{escape(_format_cost(bucket.cost_usd / divisor if divisor else bucket.cost_usd))}</td>"
            f"<td>{escape(_format_cost(bucket.cost_usd))}</td>"
            f"<td>{escape(_format_tokens(bucket.tokens))}</td>"
            f"<td>{bucket.turns}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td class='empty' colspan='7'>Nothing in this period.</td></tr>")
    return (
        "<section class='card activity'><h2>Activity</h2>"
        "<p class='detail'>Per day, in your timezone. Days with nothing on them "
        "are kept, because a chart that drops them draws a busy fortnight and a "
        "quiet one the same way.</p>"
        + charts
        + "<table><thead><tr><th scope='col'>Day</th><th scope='col'>Runs</th>"
        "<th scope='col'>Not succeeded</th>"
        f"<th scope='col'>On subscription (÷{divisor:g})</th>"
        "<th scope='col'>List</th><th scope='col'>Tokens</th>"
        "<th scope='col'>Turns</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )
