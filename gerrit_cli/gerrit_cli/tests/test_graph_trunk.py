"""Tests for the merged-trunk pipeline in graph/build.py.

These cover the pure-data helpers that turn the populated build
context into the chronological merged-trunk layout signal the JS
layer renders. We don't need a real BuildContext (with its
GerritCommentsClient and progress logger) — these helpers only
read a few fields, so a simple namespace satisfies the contract.
"""

from types import SimpleNamespace
from typing import Any

from gerrit_cli.graph.build import (
    _build_merged_trunk,
    _promote_merged_to_main,
    _redirect_inflight_to_recent_merged,
)


def _node(
    cn: int, status: str, submitted: str = "", updated: str = "",
    series_group: int = 0, current_patchset: int = 1,
    current_ps_created: str = "",
) -> dict[str, Any]:
    """Minimal node dict shape the trunk helpers consult."""
    return {
        "id": cn,
        "status": status,
        "submitted": submitted,
        "updated": updated,
        "series_group": series_group,
        "current_patchset": current_patchset,
        "current_ps_created": current_ps_created,
    }


def _ctx(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
    separate_groups: list[dict[str, Any]] | None = None,
    anchor_cn: int = 0,
    commit_to_change_ps: dict[str, tuple[int, int]] | None = None,
    revision_parents: dict[str, str] | None = None,
    external_merged_submitted: dict[int, str] | None = None,
) -> SimpleNamespace:
    """Synthesise a BuildContext-compatible object for the helpers
    under test. We don't construct the real dataclass because it
    requires a network client; the helpers only touch a handful of
    attributes."""
    return SimpleNamespace(
        change_number=anchor_cn,
        nodes={n["id"]: n for n in nodes},
        edges=list(edges or []),
        separate_groups=list(separate_groups or []),
        commit_to_change_ps=dict(commit_to_change_ps or {}),
        revision_parents=dict(revision_parents or {}),
        external_merged_submitted=dict(external_merged_submitted or {}),
    )


# ─── _build_merged_trunk ──────────────────────────────────────────


class TestBuildMergedTrunkOrder:
    """The trunk is the master-landing order — sort by `submitted`
    ASC, oldest first. Everything downstream (JS column layout,
    edge redirect) trusts this ordering, so the test surface here
    is wide."""

    def test_orders_merged_nodes_by_submitted_ascending(self):
        ctx = _ctx([
            _node(1, "MERGED", submitted="2026-03-15 10:00:00"),
            _node(2, "MERGED", submitted="2026-01-05 10:00:00"),
            _node(3, "MERGED", submitted="2026-06-22 10:00:00"),
            _node(4, "MERGED", submitted="2026-04-10 10:00:00"),
        ])
        assert _build_merged_trunk(ctx) == [2, 1, 4, 3]

    def test_excludes_non_merged_nodes(self):
        ctx = _ctx([
            _node(1, "MERGED", submitted="2026-01-01 00:00:00"),
            _node(2, "NEW"),
            _node(3, "ABANDONED"),
            _node(4, "MERGED", submitted="2026-02-01 00:00:00"),
        ])
        assert _build_merged_trunk(ctx) == [1, 4]

    def test_tie_break_on_change_number_ascending(self):
        """Two patches with identical `submitted` (the rare
        batch-merge case) tie-break on cn to keep the order
        deterministic."""
        same = "2026-04-10 12:34:56"
        ctx = _ctx([
            _node(42, "MERGED", submitted=same),
            _node(7, "MERGED", submitted=same),
            _node(13, "MERGED", submitted=same),
        ])
        assert _build_merged_trunk(ctx) == [7, 13, 42]

    def test_falls_back_to_updated_when_submitted_missing(self):
        """Very old merged changes may lack a `submitted` field —
        sort them by `updated` instead so the chronological column
        still reflects the right order."""
        ctx = _ctx([
            _node(1, "MERGED", updated="2026-02-01 00:00:00"),
            _node(2, "MERGED", submitted="2026-03-01 00:00:00"),
            _node(3, "MERGED", updated="2026-01-01 00:00:00"),
        ])
        # Order: 3 (Jan via updated), 1 (Feb via updated), 2 (Mar via submitted)
        assert _build_merged_trunk(ctx) == [3, 1, 2]

    def test_missing_both_timestamps_sorts_last(self):
        ctx = _ctx([
            _node(1, "MERGED"),  # no submitted, no updated
            _node(2, "MERGED", submitted="2026-01-01 00:00:00"),
        ])
        # The unknown-timestamp node goes to the end (treated as
        # "newest unknown" so it doesn't bury the real history).
        assert _build_merged_trunk(ctx) == [2, 1]

    def test_returns_empty_when_no_merged(self):
        ctx = _ctx([_node(1, "NEW"), _node(2, "ABANDONED")])
        assert _build_merged_trunk(ctx) == []


# ─── _promote_merged_to_main ──────────────────────────────────────


class TestPromoteMergedToMain:
    """Merged nodes that landed in topic/hashtag separate groups
    belong in the main trunk. Promote them: set series_group=0 and
    strip them from their group's node_ids. Empty groups disappear."""

    def test_moves_merged_out_of_separate_group(self):
        ctx = _ctx(
            nodes=[
                _node(1, "MERGED", series_group=5),
                _node(2, "NEW", series_group=5),
            ],
            separate_groups=[
                {"id": 5, "label": "topic foo", "node_ids": [1, 2]},
            ],
        )
        promoted = _promote_merged_to_main(ctx)
        assert promoted == 1
        assert ctx.nodes[1]["series_group"] == 0
        assert ctx.nodes[2]["series_group"] == 5
        assert ctx.separate_groups[0]["node_ids"] == [2]

    def test_prunes_group_emptied_by_promotion(self):
        ctx = _ctx(
            nodes=[
                _node(1, "MERGED", series_group=3),
                _node(2, "MERGED", series_group=3),
                _node(3, "NEW", series_group=4),
            ],
            separate_groups=[
                {"id": 3, "label": "all merged", "node_ids": [1, 2]},
                {"id": 4, "label": "in-flight", "node_ids": [3]},
            ],
        )
        _promote_merged_to_main(ctx)
        # Group 3 emptied → removed. Group 4 still has its in-flight node.
        assert len(ctx.separate_groups) == 1
        assert ctx.separate_groups[0]["id"] == 4

    def test_no_promotion_when_no_merged_in_groups(self):
        ctx = _ctx(
            nodes=[
                _node(1, "NEW", series_group=2),
                _node(2, "MERGED", series_group=0),
            ],
            separate_groups=[
                {"id": 2, "label": "x", "node_ids": [1]},
            ],
        )
        promoted = _promote_merged_to_main(ctx)
        assert promoted == 0
        assert ctx.separate_groups[0]["node_ids"] == [1]


# ─── _redirect_inflight_to_recent_merged ──────────────────────────


class TestRedirectInflightToRecentMerged:
    """For an in-flight patch whose only path into the trunk is via
    an OLD merged ancestor, the visual edge gets re-aimed at the
    most recent merged trunk node the patch could plausibly have
    branched off — measured by its current patchset's creation
    time. Newer-than-anchor merged edges are left alone, and edges
    that already point at the right target are no-ops."""

    def _trunk_nodes(self):
        """Three merged trunk nodes, oldest → newest. Anchor is
        the middle merged node."""
        return [
            _node(10, "MERGED", submitted="2026-01-01 00:00:00",
                  current_patchset=5),
            _node(20, "MERGED", submitted="2026-03-01 00:00:00",
                  current_patchset=5),
            _node(30, "MERGED", submitted="2026-05-01 00:00:00",
                  current_patchset=7),
        ]

    def test_redirects_to_most_recent_merged_at_or_before_ps_created(self):
        """In-flight node's PS created 2026-04-01 → most recent
        merged with submitted <= that is the middle trunk node
        (20, Mar 1). NOT the trunk top (30, May 1)."""
        ctx = _ctx(
            nodes=self._trunk_nodes() + [
                _node(100, "NEW", current_ps_created="2026-04-01 00:00:00"),
            ],
            edges=[{
                "from": 10, "to": 100,  # current parent: oldest (Jan)
                "parent_patchset": 3, "parent_latest": 5,
                "is_stale": True,
            }],
            anchor_cn=20,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10, 20, 30])
        assert count == 1
        e = ctx.edges[0]
        # Target is 20, not 30 — that's the recency the in-flight
        # patch's PS-created timestamp allows for.
        assert e["from"] == 20
        assert e["parent_patchset"] == 5
        assert e["is_stale"] is False

    def test_redirects_to_trunk_top_when_ps_created_is_after_top(self):
        """If the in-flight patch was uploaded AFTER every trunk
        node's submit time, the trunk top is the right target."""
        ctx = _ctx(
            nodes=self._trunk_nodes() + [
                _node(100, "NEW", current_ps_created="2026-07-01 00:00:00"),
            ],
            edges=[{
                "from": 10, "to": 100,
                "parent_patchset": 3, "parent_latest": 5,
                "is_stale": True,
            }],
            anchor_cn=20,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10, 20, 30])
        assert count == 1
        assert ctx.edges[0]["from"] == 30

    def test_keeps_edge_when_current_parent_is_already_the_target(self):
        """If 100's current parent is the right trunk node already
        (no recency change), the edge is left alone."""
        ctx = _ctx(
            nodes=self._trunk_nodes() + [
                _node(100, "NEW", current_ps_created="2026-04-01 00:00:00"),
            ],
            edges=[{
                "from": 20, "to": 100,  # already the correct target
                "parent_patchset": 5, "parent_latest": 5,
                "is_stale": False,
            }],
            anchor_cn=10,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10, 20, 30])
        assert count == 0
        assert ctx.edges[0]["from"] == 20

    def test_keeps_edge_from_newer_than_anchor_merged(self):
        """An in-flight node connected to a merged trunk node that
        landed AFTER the anchor is a real downstream chain — don't
        redirect those."""
        ctx = _ctx(
            nodes=self._trunk_nodes() + [
                _node(100, "NEW", current_ps_created="2026-06-01 00:00:00"),
            ],
            edges=[{
                "from": 30, "to": 100,  # 30 is newer than anchor 20
                "parent_patchset": 7, "parent_latest": 7,
                "is_stale": False,
            }],
            anchor_cn=20,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10, 20, 30])
        assert count == 0
        assert ctx.edges[0]["from"] == 30

    def test_in_flight_anchor_with_no_ps_created_falls_back_to_updated(self):
        """If current_ps_created is missing (older Gerrit responses),
        `updated` is the fallback proxy for upload time."""
        ctx = _ctx(
            nodes=self._trunk_nodes() + [
                _node(100, "NEW", updated="2026-02-15 00:00:00"),
            ],
            edges=[{
                "from": 10, "to": 100,  # parent: Jan
                "parent_patchset": 3, "parent_latest": 5,
                "is_stale": True,
            }],
            anchor_cn=20,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10, 20, 30])
        # updated=Feb 15 → most recent merged with submitted <= Feb 15
        # is 10 (Jan 1). Same as current parent → no redirect.
        assert count == 0
        assert ctx.edges[0]["from"] == 10

    def test_multiple_old_merged_links_collapse_to_one_target(self):
        """An in-flight node with two incoming merged edges (e.g.
        one current, one stale) ends up with at most one redirected
        edge — no duplicates."""
        ctx = _ctx(
            nodes=self._trunk_nodes() + [
                _node(100, "NEW", current_ps_created="2026-04-01 00:00:00"),
            ],
            edges=[
                {"from": 10, "to": 100, "parent_patchset": 3,
                 "parent_latest": 5, "is_stale": True},
                {"from": 20, "to": 100, "parent_patchset": 2,
                 "parent_latest": 5, "is_stale": True},
            ],
            anchor_cn=20,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10, 20, 30])
        # 20's edge keeps (anchor-self, newer-or-equal rule).
        # 10's edge redirects to target 20 (most recent at or before
        # Apr 1). Result: just one edge 20→100.
        assert count == 1
        assert len(ctx.edges) == 1
        assert ctx.edges[0]["from"] == 20

    def test_no_redirect_when_no_timestamp_available(self):
        """If neither current_ps_created nor updated is set, we
        can't make a sensible decision — keep the original edge."""
        ctx = _ctx(
            nodes=self._trunk_nodes() + [
                _node(100, "NEW"),  # no timestamps at all
            ],
            edges=[{
                "from": 10, "to": 100,
                "parent_patchset": 3, "parent_latest": 5,
                "is_stale": True,
            }],
            anchor_cn=20,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10, 20, 30])
        assert count == 0

    def test_non_trunk_merged_edges_untouched(self):
        """If a node is MERGED but absent from the trunk list (e.g.
        an outlier), the redirect can't be applied — leave it."""
        ctx = _ctx(
            nodes=[
                _node(10, "MERGED", submitted="2026-01-01 00:00:00"),
                _node(99, "MERGED", submitted="2026-02-01 00:00:00"),
                _node(100, "NEW", current_ps_created="2026-04-01 00:00:00"),
            ],
            edges=[{
                "from": 99, "to": 100,
                "parent_patchset": 1, "parent_latest": 1,
                "is_stale": True,
            }],
            anchor_cn=10,
        )
        count = _redirect_inflight_to_recent_merged(ctx, [10])
        assert count == 0
        assert ctx.edges[0]["from"] == 99

    def test_empty_trunk_is_noop(self):
        ctx = _ctx(nodes=[_node(1, "NEW")], anchor_cn=1)
        count = _redirect_inflight_to_recent_merged(ctx, [])
        assert count == 0

    def test_walks_parent_chain_to_off_tree_merged_ancestor(self):
        """Regression for #64441: the in-flight patch's current PS
        was based on an old non-merged patchset of an older trunk
        node. Walking back one more hop lands on the merged commit
        of an external (filtered-out) change. The redirect target
        must be derived from THAT external change's submitted, not
        from the immediately-named trunk parent or the in-flight
        patch's upload time."""
        ctx = _ctx(
            nodes=[
                # Trunk: old (Jan), middle (Mar), recent (May 28)
                _node(10, "MERGED", submitted="2026-01-01 00:00:00",
                      current_patchset=5),
                _node(20, "MERGED", submitted="2026-03-01 00:00:00",
                      current_patchset=8),
                _node(50, "MERGED", submitted="2026-05-28 00:00:00",
                      current_patchset=3),
                _node(80, "MERGED", submitted="2026-06-10 00:00:00",
                      current_patchset=2),
                # In-flight at ps14, uploaded June 22 (well after
                # everything in the trunk) — so the upload-time
                # fallback would pick the trunk top (80, Jun 10),
                # but the patch's real base on master is May 29.
                _node(100, "NEW", current_patchset=14,
                      current_ps_created="2026-06-22 00:00:00"),
            ],
            edges=[{
                # /related's historical pull-in says the parent is
                # the older trunk node 20.
                "from": 20, "to": 100,
                "parent_patchset": 3, "parent_latest": 8,
                "is_stale": True,
            }],
            # Anchor is the trunk top so older trunk nodes qualify
            # for the "older than anchor" redirect check.
            anchor_cn=80,
            commit_to_change_ps={
                # 100 ps14 commit
                "Cps14": (100, 14),
                # 20's old ps3 commit (where 100 ps14 was based)
                "C20_ps3": (20, 3),
                # External merged change 999 (NOT in ctx.nodes —
                # it was filtered out earlier as an off-tree
                # ancestor). Its merged commit is what 20 ps3 was
                # based on.
                "C999_merged": (999, 1),
            },
            revision_parents={
                "Cps14": "C20_ps3",      # 100 ps14 → 20 ps3
                "C20_ps3": "C999_merged",  # 20 ps3 → 999 merged
            },
            external_merged_submitted={
                # 999 was merged on May 29 — exactly the cutoff we
                # want to use for redirect target lookup.
                999: "2026-05-29 12:00:00",
            },
        )
        count = _redirect_inflight_to_recent_merged(
            ctx, [10, 20, 50, 80],
        )
        assert count == 1
        # Target: the most recent merged trunk with submitted <=
        # 2026-05-29 12:00:00, which is 50 (May 28). NOT 80 (Jun 10).
        assert ctx.edges[0]["from"] == 50
        assert ctx.edges[0]["to"] == 100
