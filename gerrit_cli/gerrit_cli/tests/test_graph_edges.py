"""Tests for the graph/edges.py helpers.

`_collect_revisions` turns a Gerrit change payload into commit ->
(cn, ps) and commit -> parent-commit maps. Both feed the chain
edge derivation in build.py; a missing parent map silently
produces empty edges.

`_break_cycles` removes edges that participate in cycles created
by stale old-patchset dependencies (A based on B in ps3, B based
on A in ps5). The cycle breaker preserves stale edges over fresh
ones so the displayed chain reflects current relationships.
"""

from gerrit_cli.graph.edges import _break_cycles, _collect_revisions


def _change_with_revisions(cn, revs):
    """revs: list of (rev_hash, ps, parent_hash). Returns a Gerrit
    change payload shaped like the one ALL_REVISIONS+ALL_COMMITS
    returns."""
    revisions = {}
    for rev_hash, ps, parent_hash in revs:
        revisions[rev_hash] = {
            "_number": ps,
            "commit": {
                "commit": rev_hash,
                "parents": [{"commit": parent_hash}] if parent_hash else [],
            },
        }
    return {"_number": cn, "revisions": revisions}


# ─── _collect_revisions ───────────────────────────────────────────


class TestCollectRevisions:
    def test_populates_commit_to_change_ps_map(self):
        ctps: dict = {}
        change = _change_with_revisions(42, [
            ("aaa111", 1, "parent1"),
            ("bbb222", 2, "parent2"),
        ])
        _collect_revisions(change, ctps)
        assert ctps == {"aaa111": (42, 1), "bbb222": (42, 2)}

    def test_optional_parent_map_populated_when_requested(self):
        ctps: dict = {}
        parents: dict = {}
        change = _change_with_revisions(7, [
            ("ch1", 1, "par1"),
            ("ch2", 2, "par2"),
        ])
        _collect_revisions(change, ctps, parents)
        assert parents == {"ch1": "par1", "ch2": "par2"}

    def test_parent_map_skipped_when_not_requested(self):
        """build.py only requests parent collection when it actually
        needs the chain edges — most calls pass None and shouldn't
        pay the lookup cost. Confirm the function respects that."""
        ctps: dict = {}
        change = _change_with_revisions(7, [("ch1", 1, "par1")])
        # Should not raise even though we pass None for the parent
        # map and the revision has parents.
        _collect_revisions(change, ctps, None)
        assert ctps == {"ch1": (7, 1)}

    def test_revision_without_parents_skipped_in_parent_map(self):
        ctps: dict = {}
        parents: dict = {}
        change = _change_with_revisions(7, [
            ("rootrev", 1, ""),  # initial commit, no parents
        ])
        _collect_revisions(change, ctps, parents)
        assert ctps == {"rootrev": (7, 1)}
        assert "rootrev" not in parents


# ─── _break_cycles ────────────────────────────────────────────────


def _edge(frm, to, *, stale=False):
    return {"from": frm, "to": to, "is_stale": stale}


class TestBreakCyclesAcyclic:
    def test_no_cycle_returns_zero(self):
        edges = [_edge(1, 2), _edge(2, 3), _edge(3, 4)]
        removed = _break_cycles(edges)
        assert removed == 0
        assert len(edges) == 3

    def test_dag_with_branch_returns_zero(self):
        """A → B → D, A → C → D is a DAG; no edge should be removed."""
        edges = [
            _edge(1, 2), _edge(2, 4),
            _edge(1, 3), _edge(3, 4),
        ]
        removed = _break_cycles(edges)
        assert removed == 0
        assert len(edges) == 4


class TestBreakCyclesSimple:
    def test_two_node_cycle_removed(self):
        """A → B and B → A: one edge must be removed to break it."""
        edges = [_edge(1, 2), _edge(2, 1)]
        removed = _break_cycles(edges)
        assert removed == 1
        assert len(edges) == 1

    def test_stale_edge_preferred_for_removal(self):
        """When breaking a cycle, the stale (old-patchset) edge
        should be removed first — that's the relationship the user
        thinks of as 'gone'. The fresh current-ps edge survives."""
        edges = [
            _edge(1, 2, stale=False),  # current
            _edge(2, 1, stale=True),   # historical
        ]
        _break_cycles(edges)
        # The surviving edge should be the fresh one.
        assert len(edges) == 1
        assert edges[0]["from"] == 1 and edges[0]["to"] == 2
        assert edges[0]["is_stale"] is False

    def test_three_node_cycle_resolved(self):
        """A → B → C → A is a 3-cycle. One edge gets removed."""
        edges = [_edge(1, 2), _edge(2, 3), _edge(3, 1)]
        removed = _break_cycles(edges)
        assert removed >= 1
        # Graph is now acyclic.
        adj: dict = {}
        for e in edges:
            adj.setdefault(e["from"], set()).add(e["to"])
        # Quick acyclicity check: Kahn's algorithm completes.
        in_deg = {n: 0 for n in adj}
        for u, vs in adj.items():
            for v in vs:
                in_deg[v] = in_deg.get(v, 0) + 1
                in_deg.setdefault(u, in_deg.get(u, 0))
        queue = [n for n, d in in_deg.items() if d == 0]
        seen = set()
        while queue:
            n = queue.pop()
            seen.add(n)
            for v in adj.get(n, ()):
                in_deg[v] -= 1
                if in_deg[v] == 0:
                    queue.append(v)
        # Every node visited → no remaining cycle.
        assert set(in_deg.keys()) == seen


class TestBreakCyclesIsolatedFromAcyclic:
    """An acyclic neighbour of a cycle should be left alone — the
    cycle breaker should not collateral-damage unrelated edges."""

    def test_acyclic_neighbour_untouched(self):
        edges = [
            _edge(1, 2, stale=True),
            _edge(2, 1, stale=False),   # cycle between 1 and 2
            _edge(3, 4),                 # acyclic, separate
            _edge(4, 5),
        ]
        _break_cycles(edges)
        # 3→4 and 4→5 must still be present.
        survivors = {(e["from"], e["to"]) for e in edges}
        assert (3, 4) in survivors
        assert (4, 5) in survivors
