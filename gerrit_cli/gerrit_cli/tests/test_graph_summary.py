"""Headline numbers in the graph payload's `stats.summary` (read by the
Stats tab and by the portal) and the per-node lifecycle times they are
built from. Expected values are worked out by hand in the comments."""

import json
import random
import re
import statistics
from datetime import datetime, timezone

from gerrit_cli.graph.render import generate_html
from gerrit_cli.graph.summary import (
    DAY, _quantile, add_timeline, series_summary,
)

AS_OF = int(datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc).timestamp())


def ago(days: float) -> int:
    return int(AS_OF - days * DAY)


def gts(epoch: int) -> str:
    """Gerrit REST timestamp for an epoch."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S.000000000")


def node(cn, status, opened, closed=None, ps=(), rv=(), cp=None,
         updated=None, structural=False, abandon_msg=True):
    """A payload node with lifecycle inputs given in days before AS_OF."""
    n = {
        "id": cn, "status": status, "ticket": f"LU-{cn}",
        "created": gts(ago(opened)) if opened is not None else "",
        "ps_times": [ago(d) for d in ps] or ([ago(opened)] if opened else []),
        "review_times": sorted(ago(d) for d in rv),
        "current_patchset": cp if cp is not None else max(1, len(ps)),
        "updated": gts(ago(updated)) if updated is not None else "",
        "submitted": "", "abandoned_at": "", "current_ps_created": "",
    }
    if status == "MERGED" and closed is not None:
        n["submitted"] = gts(ago(closed))
    if status == "ABANDONED" and closed is not None and abandon_msg:
        n["abandoned_at"] = gts(ago(closed))
    if structural:
        n["trunk_structural"] = True
    add_timeline(n)
    return n


class TestAddTimeline:
    def test_merged_uses_created_and_submitted(self):
        n = node(1, "MERGED", 30, closed=2, ps=(30, 10))
        assert n["opened_at"] == ago(30)
        assert n["closed_at"] == ago(2)
        assert n["closed_approx"] is False

    def test_abandoned_uses_abandon_message_time(self):
        n = node(1, "ABANDONED", 30, closed=4, updated=1)
        assert n["closed_at"] == ago(4)
        assert n["closed_approx"] is False

    def test_abandoned_without_message_falls_back_to_updated(self):
        n = node(1, "ABANDONED", 30, closed=4, updated=1, abandon_msg=False)
        assert n["closed_at"] == ago(1)
        assert n["closed_approx"] is True

    def test_open_change_has_no_close(self):
        n = node(1, "NEW", 30, updated=1)
        assert n["closed_at"] is None
        assert n["closed_approx"] is False

    def test_opened_falls_back_to_first_patchset(self):
        n = node(1, "NEW", None, ps=(20, 5))
        assert n["opened_at"] == ago(20)

    def test_close_before_open_is_clamped(self):
        n = node(1, "MERGED", 10, closed=12)
        assert n["closed_at"] == n["opened_at"] == ago(10)

    def test_last_activity_is_latest_upload_or_review(self):
        assert node(1, "NEW", 30, ps=(30, 9), rv=(20, 4))["last_activity"] == ago(4)
        assert node(1, "NEW", 30, ps=(30, 3), rv=(20,))["last_activity"] == ago(3)

    def test_last_activity_falls_back_to_updated(self):
        n = {"id": 1, "status": "NEW", "created": gts(ago(30)),
             "updated": gts(ago(2))}
        add_timeline(n)
        assert n["last_activity"] == ago(2)

    def test_first_review_ignores_messages_before_the_upload(self):
        n = node(1, "NEW", 10, rv=(12, 8, 3))
        assert n["first_review_at"] == ago(8)
        assert node(1, "NEW", 10)["first_review_at"] is None


def _series():
    return [
        node(1, "MERGED", 100, closed=10, cp=3),          # ttm 90 d
        node(2, "MERGED", 50, closed=40, cp=5),           # ttm 10 d
        node(3, "MERGED", 400, closed=20, cp=20),         # ttm 380 d
        node(4, "NEW", 200, ps=(200, 150), rv=(120,)),    # first rv +80 d
        node(5, "NEW", 5),
        node(6, "ABANDONED", 45, closed=35),
        node(7, "MERGED", 1000, closed=1, structural=True),
        node(8, "NEW", 25, rv=(24,)),                     # first rv +1 d
    ]


class TestSeriesSummary:
    def test_counts_exclude_structural_base(self):
        s = series_summary(_series(), AS_OF, True)
        assert (s["patches"], s["open"], s["merged"], s["abandoned"]) == (7, 3, 3, 1)

    def test_thirty_day_windows(self):
        s = series_summary(_series(), AS_OF, True)
        # last 30 d: opened 5, 8; merged 1 (10 d), 3 (20 d)
        assert s["last_30d"] == {"opened": 2, "merged": 2, "abandoned": 0}
        # 30-60 d: opened 6 (45 d), 2 (50 d); merged 2 (40 d); abandoned 6 (35 d)
        assert s["prev_30d"] == {"opened": 2, "merged": 1, "abandoned": 1}

    def test_window_edges(self):
        # Windows are (as_of-30d, as_of] and (as_of-60d, as_of-30d]: an
        # event exactly 30 days old is in the earlier one, one at the
        # build instant in the later one, one exactly 60 days old in
        # neither.
        nodes = [node(1, "NEW", 30), node(2, "NEW", 30), node(3, "NEW", 0),
                 node(4, "NEW", 60)]
        s = series_summary(nodes, AS_OF, True)
        assert s["last_30d"]["opened"] == 1
        assert s["prev_30d"]["opened"] == 2
        assert s["recent_events"]["opened"] == [ago(30), ago(30), AS_OF]

    def test_open_thirty_days_ago(self):
        # open on day -30: 1 (merged later), 3 (merged later), 4; 2 and 6
        # had closed, 5 and 8 did not exist yet.
        assert series_summary(_series(), AS_OF, True)["open_30d_ago"] == 3

    def test_recent_events_recount_the_windows(self):
        s = series_summary(_series(), AS_OF, True)
        ev = s["recent_events"]
        assert ev["opened"] == [ago(50), ago(45), ago(25), ago(5)]
        assert ev["merged"] == [ago(40), ago(20), ago(10)]
        assert ev["abandoned"] == [ago(35)]
        d30 = AS_OF - 30 * DAY
        recent = lambda k: sum(1 for t in ev[k] if t > d30)
        assert s["open_30d_ago"] == (
            s["open"] - recent("opened") + recent("merged") + recent("abandoned"))

    def test_time_to_merge(self):
        t = series_summary(_series(), AS_OF, True)["time_to_merge"]
        # [10, 90, 380] d: median 90 d; p90 at rank 1.8 = 90 + 0.8 * 290 = 322 d
        assert t == {"count": 3, "median": 90 * DAY, "p90": 322 * DAY}

    def test_time_to_first_review(self):
        t = series_summary(_series(), AS_OF, True)["time_to_first_review"]
        # [1, 80] d: median 40.5 d; p90 = 1 + 0.9 * 79 = 72.1 d
        assert t == {"count": 2, "median": round(40.5 * DAY),
                     "p90": round(72.1 * DAY)}

    def test_first_review_unavailable_without_messages(self):
        assert series_summary(_series(), AS_OF, False)["time_to_first_review"] is None

    def test_patchsets_to_merge(self):
        p = series_summary(_series(), AS_OF, True)["patchsets_to_merge"]
        assert p == {"count": 3, "median": 5, "max": 20}

    def test_oldest_open_and_longest_idle(self):
        s = series_summary(_series(), AS_OF, True)
        assert s["oldest_open"] == {"id": 4, "ticket": "LU-4", "opened_at": ago(200)}
        # 4: last activity 120 d ago; 5: 5 d (upload); 8: 24 d (review)
        assert s["longest_idle"] == {"id": 4, "ticket": "LU-4", "last_activity": ago(120)}

    def test_merged_by_month(self):
        m = series_summary(_series(), AS_OF, True)["merged_by_month"]
        assert [k for k, _ in m][0] == "2025-10" and len(m) == 12
        # 1 merged 2026-09-14, 3 on 2026-09-04, 2 on 2026-08-15; the
        # structural base (2026-09-23) is not counted.
        assert m[-1] == ["2026-09", 2] and m[-2] == ["2026-08", 1]
        assert sum(c for _, c in m) == 3

    def test_empty_series(self):
        s = series_summary([], AS_OF, True)
        assert s["patches"] == 0
        assert s["time_to_merge"] == {"count": 0, "median": None, "p90": None}
        assert s["patchsets_to_merge"]["median"] is None
        assert s["oldest_open"] is None and s["longest_idle"] is None
        assert sum(c for _, c in s["merged_by_month"]) == 0


class TestQuantile:
    def test_matches_statistics_inclusive(self):
        rng = random.Random(4)
        for n in (1, 2, 3, 10, 47, 123):
            vals = sorted(rng.randint(0, 10**8) for _ in range(n))
            if n == 1:
                assert _quantile(vals, 0.9) == vals[0]
                continue
            deciles = statistics.quantiles(vals, n=10, method="inclusive")
            assert abs(_quantile(vals, 0.5) - statistics.median(vals)) < 1e-6
            assert abs(_quantile(vals, 0.9) - deciles[8]) < 1e-6


def test_summary_survives_the_portal_payload_parse():
    """The portal extracts the payload with a non-greedy regex; the
    summary must come back intact."""
    nodes = _series()
    payload = {"nodes": nodes, "edges": [], "stats": {
        "status_counts": {"NEW": 3}, "node_count": len(nodes),
        "summary": series_summary(nodes, AS_OF, True)}}
    html = generate_html(payload)
    m = re.search(r"const\s+G\s*=\s*(\{.*?\});", html, re.S)
    assert json.loads(m.group(1))["stats"] == payload["stats"]
