"""Edge staleness is two-sided.

An edge is stale when the parent uploaded a newer patchset than the
one the child sits on (the child needs a rebase) OR when the edge
was derived from an old patchset of the child that has since been
rebased elsewhere (pure history). The second case is what put 58229
under 62459 in the 61965 graph: 58229 ps16 sat on 62459's merged
commit, ps26 sits on 68752, and the parent-side-only check rendered
the 62459 edge as live.
"""

from types import SimpleNamespace
from typing import Any

from gerrit_cli.graph.build import (
    _build_main_edges,
    _current_parent_cn,
    _group_cross_edges,
    _make_edge,
    _redirect_inflight_to_recent_merged,
)


def _node(cn, status, current_patchset, **kw):
    n = {
        "id": cn, "status": status, "current_patchset": current_patchset,
        "submitted": kw.get("submitted", ""), "updated": "",
        "series_group": kw.get("series_group", 0),
        "current_ps_created": kw.get("current_ps_created", ""),
        "ticket": "", "topic": "", "hashtags": [],
    }
    return n


def _ctx(nodes, raw_entries, ctps, rev_parents, edges=None):
    return SimpleNamespace(
        change_number=nodes[0]["id"],
        nodes={n["id"]: n for n in nodes},
        raw_entries=list(raw_entries),
        commit_to_change_ps=dict(ctps),
        revision_parents=dict(rev_parents),
        external_merged_submitted={},
        edges=list(edges or []),
        seen_edges=set((e["from"], e["to"]) for e in (edges or [])),
        extra_topics=[], extra_hashtags=[], extra_tickets=[],
    )


class TestMakeEdge:
    def test_parent_moved_is_stale(self):
        assert _make_edge(1, 3, 5, 2, 4, 4)["is_stale"] is True

    def test_child_moved_is_stale(self):
        e = _make_edge(1, 5, 5, 2, 2, 4)
        assert e["is_stale"] is True
        assert e["child_patchset"] == 2 and e["child_latest"] == 4

    def test_both_current_is_live(self):
        assert _make_edge(1, 5, 5, 2, 4, 4)["is_stale"] is False


class TestChildSideStaleness:
    """58229 shape: old child patchset on a merged parent, current
    child patchset on an in-flight parent that is in the pool."""

    def _build(self):
        nodes = [
            _node(100, "NEW", 3),          # anchor, in-flight child
            _node(50, "MERGED", 7),        # old base, now merged
            _node(60, "NEW", 1),           # current base, in-flight
        ]
        ctps = {
            "C100_1": (100, 1), "C100_3": (100, 3),
            "C50_7": (50, 7), "C60_1": (60, 1),
        }
        rev_parents = {
            "C100_1": "C50_7",   # ps1 sat on 50's merged commit
            "C100_3": "C60_1",   # ps3 sits on 60
            "C60_1": "master",
        }
        # 50 is a discovered node (not in /related): the old-patchset
        # edge to it comes from the revision-parents pass.
        raw = [
            {"cn": 100, "commit": "C100_3", "parent_commit": "C60_1",
             "ps": 3, "latest": 3},
        ]
        ctx = _ctx(nodes, raw, ctps, rev_parents)
        _build_main_edges(ctx)
        return ctx

    def test_old_patchset_edge_is_history(self):
        ctx = self._build()
        by = {(e["from"], e["to"]): e for e in ctx.edges}
        assert by[(60, 100)]["is_stale"] is False
        assert by[(60, 100)]["child_patchset"] == 3
        old = by[(50, 100)]
        assert old["is_stale"] is True
        # parent side did NOT move — this is child-side history
        assert old["parent_patchset"] == old["parent_latest"] == 7
        assert old["child_patchset"] == 1 and old["child_latest"] == 3

    def test_live_derivation_wins_dedupe_over_old_patchset(self):
        """Same (parent, child) pair from an old AND the current child
        patchset: the edge must be recorded as live regardless of
        dict iteration order."""
        nodes = [_node(100, "NEW", 2), _node(60, "NEW", 1)]
        ctps = {"C100_1": (100, 1), "C100_2": (100, 2), "C60_1": (60, 1)}
        # Old patchset first in insertion order.
        rev_parents = {"C100_1": "C60_1", "C100_2": "C60_1"}
        raw = [{"cn": 100, "commit": "C100_2", "parent_commit": "C60_1",
                "ps": 2, "latest": 2}]
        ctx = _ctx(nodes, raw, ctps, rev_parents)
        _build_main_edges(ctx)
        e = next(e for e in ctx.edges if (e["from"], e["to"]) == (60, 100))
        assert e["is_stale"] is False and e["child_patchset"] == 2

    def test_group_cross_edges_are_child_aware(self):
        main = _node(50, "MERGED", 7)
        grp = _node(100, "NEW", 3, series_group=2)
        ctx = _ctx([main, grp], [], {"C50_7": (50, 7)}, {})
        group_ctps = {"C100_1": (100, 1), "C100_3": (100, 3)}
        group_rev_parents = {"C100_3": "elsewhere", "C100_1": "C50_7"}
        out = _group_cross_edges(ctx, group_ctps, group_rev_parents,
                                 {100: grp})
        assert [(e["from"], e["to"], e["is_stale"]) for e in out] \
            == [(50, 100, True)]


class TestRedirectSkipsChildWithLiveInPoolParent:
    def test_current_parent_cn(self):
        nodes = [_node(100, "NEW", 3), _node(60, "NEW", 1)]
        ctx = _ctx(nodes, [], {"C100_3": (100, 3), "C60_1": (60, 1)},
                   {"C100_3": "C60_1"})
        assert _current_parent_cn(ctx, 100) == 60
        ctx.revision_parents["C100_3"] = "master"
        assert _current_parent_cn(ctx, 100) is None

    def test_no_redirect_when_child_sits_on_inflight_change(self):
        """58229 shape: the merged->child edge is history; the child's
        real parent 68752 is in the graph. The redirect must not
        manufacture a trunk attachment."""
        nodes = [
            _node(100, "NEW", 3, current_ps_created="2026-09-01 00:00:00"),
            _node(50, "MERGED", 7, submitted="2026-01-01 00:00:00"),
            _node(55, "MERGED", 2, submitted="2026-08-01 00:00:00"),
            _node(60, "NEW", 1),
        ]
        ctps = {"C100_3": (100, 3), "C50_7": (50, 7), "C55_2": (55, 2),
                "C60_1": (60, 1)}
        rev_parents = {"C100_3": "C60_1", "C60_1": "C55_2"}
        edges = [
            _make_edge(50, 7, 7, 100, 1, 3),   # history
            _make_edge(60, 1, 1, 100, 3, 3),   # live
        ]
        ctx = _ctx(nodes, [], ctps, rev_parents, edges)
        count = _redirect_inflight_to_recent_merged(ctx, [50, 55])
        assert count == 0
        assert sorted((e["from"], e["to"]) for e in ctx.edges) \
            == [(50, 100), (60, 100)]
        assert not any(e["from"] == 55 for e in ctx.edges)


class TestRedirectGateAbandonedParent:
    def test_abandoned_current_parent_still_redirects(self):
        """A child sitting on an ABANDONED in-pool change has no
        visible base by default, so the date-based trunk attachment
        must still be produced."""
        nodes = [
            _node(100, "NEW", 3, current_ps_created="2026-09-01 00:00:00"),
            _node(50, "MERGED", 7, submitted="2026-01-01 00:00:00"),
            _node(55, "MERGED", 2, submitted="2026-08-01 00:00:00"),
            _node(60, "ABANDONED", 1),
        ]
        ctps = {"C100_3": (100, 3), "C50_7": (50, 7), "C55_2": (55, 2),
                "C60_1": (60, 1)}
        rev_parents = {"C100_3": "C60_1", "C60_1": "C55_2"}
        edges = [_make_edge(50, 7, 7, 100, 1, 3), _make_edge(60, 1, 1, 100, 3, 3)]
        ctx = _ctx(nodes, [], ctps, rev_parents, edges)
        count = _redirect_inflight_to_recent_merged(ctx, [50, 55])
        assert count == 1
        assert any((e["from"], e["to"]) == (55, 100) and not e["is_stale"]
                   for e in ctx.edges)


class TestGroupInternalEdges:
    """Group members are listed by /related at whatever revision sits
    in the SEED's chain; the edge must be derived from the member's
    current patchset when that parent is in the group."""

    def _group(self):
        # Stack A(10) -> B(11) -> C(12); seed is C at ps1 on B ps1,
        # B re-uploaded as ps2 still on A ps1.
        nodes = {
            10: _node(10, "NEW", 1), 11: _node(11, "NEW", 2),
            12: _node(12, "NEW", 1),
        }
        ctps = {"A1": (10, 1), "B1": (11, 1), "B2": (11, 2), "C1": (12, 1)}
        rev_parents = {"B1": "A1", "B2": "A1", "C1": "B1", "A1": "master"}
        raw = [
            {"cn": 12, "commit": "C1", "parent_commit": "B1"},
            {"cn": 11, "commit": "B1", "parent_commit": "A1"},  # old ps
            {"cn": 10, "commit": "A1", "parent_commit": "master"},
        ]
        ctx = _ctx(list(nodes.values()), [], ctps, rev_parents)
        return ctx, raw, ctps, nodes, rev_parents

    def test_live_edge_from_current_patchset(self):
        from gerrit_cli.graph.build import _group_internal_edges
        ctx, raw, ctps, nodes, rp = self._group()
        out = _group_internal_edges(ctx, raw, ctps, nodes, rp)
        by = {(e["from"], e["to"]): e for e in out}
        assert by[(10, 11)]["is_stale"] is False
        assert by[(10, 11)]["child_patchset"] == 2
        # C sits on B ps1 while B is at ps2: parent-side stale,
        # child-side current — a genuine NEEDS REBASE edge.
        assert by[(11, 12)]["is_stale"] is True
        assert by[(11, 12)]["child_patchset"] == by[(11, 12)]["child_latest"]

    def test_fallback_when_current_parent_outside_group(self):
        from gerrit_cli.graph.build import _group_internal_edges
        ctx, raw, ctps, nodes, rp = self._group()
        # B's current ps now sits on a commit outside the group.
        rp["B2"] = "elsewhere"
        out = _group_internal_edges(ctx, raw, ctps, nodes, rp)
        e = next(e for e in out if (e["from"], e["to"]) == (10, 11))
        assert e["is_stale"] is True and e["child_patchset"] == 1


class TestBreakLateCycles:
    def test_removes_history_edge_of_two_cycle(self):
        from gerrit_cli.graph.build import _break_late_cycles
        edges = [
            _make_edge(1, 5, 5, 2, 2, 4),   # 1->2 from old child ps: history
            _make_edge(2, 3, 4, 1, 5, 5),   # 2->1 child current, parent moved
            _make_edge(2, 4, 4, 9, 1, 1),   # unrelated live edge
        ]
        removed = _break_late_cycles(edges)
        assert removed == 1
        assert [(e["from"], e["to"]) for e in edges] == [(2, 1), (2, 9)]

    def test_noop_on_dag(self):
        from gerrit_cli.graph.build import _break_late_cycles
        edges = [_make_edge(1, 1, 1, 2, 1, 1), _make_edge(2, 1, 1, 3, 1, 1)]
        assert _break_late_cycles(edges) == 0 and len(edges) == 2
