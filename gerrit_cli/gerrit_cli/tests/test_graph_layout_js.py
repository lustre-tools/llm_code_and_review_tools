"""Layout tests for graph.js, run headless in node.

Each test builds a small payload, renders the real page template and
runs tests/graph_layout_harness.mjs on it, which stubs the DOM and
vis.js and reports every node position and drawn edge per checkbox
combination ("a<abandoned>m<layout>": a1 = abandoned shown, m1 =
Trunk, m0 = Stacks).
"""

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from gerrit_cli.graph.render import generate_html

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("graph_layout_harness.mjs")
LEVEL_H = 140
NODE_W = 380

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _node(cn: int, status: str = "NEW", **kw: Any) -> dict[str, Any]:
    n = {
        "id": cn, "status": status, "subject": f"LU-1 change {cn}",
        "series_group": 0, "current_patchset": 1, "hashtags": [],
        "topic": "", "ticket": "LU-1", "author": "A", "owner": "A",
        "url": f"https://gerrit.invalid/{cn}", "review": {},
        "submitted": "", "is_wip": False,
    }
    n.update(kw)
    return n


def _edge(frm: int, to: int, pps: int = 1, pl: int = 1, cps: int = 1,
          cl: int = 1, inferred: bool = False) -> dict[str, Any]:
    e = {
        "from": frm, "to": to,
        "parent_patchset": pps, "parent_latest": pl,
        "child_patchset": cps, "child_latest": cl,
        "is_stale": pps < pl or cps < cl or inferred,
    }
    if inferred:
        e["inferred"] = True
    return e


def _payload(anchor: int, nodes: list[dict[str, Any]],
             edges: list[dict[str, Any]]) -> dict[str, Any]:
    merged = sorted(
        (n for n in nodes if n["status"] == "MERGED"),
        key=lambda n: (n["submitted"], n["id"]),
    )
    return {
        "anchor": anchor, "base_url": "https://gerrit.invalid",
        "nodes": nodes, "edges": edges,
        "separate_groups": [], "separate_chains": [],
        "merged_trunk": [n["id"] for n in merged],
        "generated_at": "", "generated_ts": 0, "review_activity": False,
        "stats": {
            "status_counts": dict(Counter(n["status"] for n in nodes)),
            "node_count": len(nodes), "edge_count": len(edges),
            "structural_merged_count": 0,
        },
    }


def _run(payload: dict[str, Any], tmp_path: Path,
         *args: str) -> dict[str, Any]:
    page = tmp_path / "graph.html"
    page.write_text(generate_html(payload))
    out = subprocess.run(
        [NODE, str(HARNESS), str(page), *args],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(out.stdout)
    assert result["errors"] == []
    return result


def _render(payload: dict[str, Any], tmp_path: Path,
            *combos: str) -> dict[str, Any]:
    return _run(payload, tmp_path, *combos)["combos"]


def _eval(payload: dict[str, Any], tmp_path: Path, expr: str) -> Any:
    return _run(payload, tmp_path, "a0m1", "--eval", expr)["eval"]


def _pos(view: dict[str, Any]) -> dict[int, tuple[int, int]]:
    return {n["id"]: (n["x"], n["y"]) for n in view["nodes"]}


def _drawn(view: dict[str, Any]) -> set[tuple[int, int]]:
    return {(e["from"], e["to"]) for e in view["edges"]}


class TestInferredTrunkHookup:
    """A date-inferred trunk hookup holds a node in place only while
    no real parent edge outranks it among the visible parents."""

    def _with_abandoned_parent(self, child_ps: int):
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01", current_patchset=5),
            _node(50, "ABANDONED"),
            _node(100, current_patchset=3),
        ]
        edges = [
            _edge(50, 100, cps=child_ps, cl=3),
            _edge(10, 100, pps=5, pl=5, cps=3, cl=3, inferred=True),
        ]
        return _payload(10, nodes, edges)

    def test_holds_node_while_abandoned_parent_is_hidden(self, tmp_path):
        view = _render(self._with_abandoned_parent(3), tmp_path,
                       "a0m1")["a0m1"]
        pos = _pos(view)
        assert 50 not in pos
        assert pos[100][1] == pos[10][1] - LEVEL_H
        assert abs(pos[100][0] - pos[10][0]) == NODE_W
        assert (10, 100) in _drawn(view)

    def test_real_parent_owns_node_once_shown(self, tmp_path):
        views = _render(self._with_abandoned_parent(3), tmp_path, "a1m1")
        for view in views.values():
            pos = _pos(view)
            assert pos[100][1] == pos[50][1] - LEVEL_H
            assert pos[100][0] == pos[50][0]
            assert (50, 100) in _drawn(view)
            assert (10, 100) not in _drawn(view)

    def test_stand_in_is_not_a_descendant(self, tmp_path):
        """While a real parent outranks the hookup, the trunk node
        must not count the node in its subtree: the inflated count
        re-ranked 54459's main chain and moved 54484 up a row."""
        assert _eval(self._with_abandoned_parent(3), tmp_path,
                     "countDesc(10)") == 0
        assert _eval(self._with_abandoned_parent(1), tmp_path,
                     "countDesc(10)") == 1

    def test_outranks_history_edge_of_shown_parent(self, tmp_path):
        """64616's shape: the abandoned parent only held an old
        patchset of the node."""
        view = _render(self._with_abandoned_parent(1), tmp_path,
                       "a1m1")["a1m1"]
        pos = _pos(view)
        assert pos[100][1] == pos[10][1] - LEVEL_H
        assert _drawn(view) >= {(10, 100)}
        assert (50, 100) not in _drawn(view)


class TestAnchorBaseChain:
    def _anchor_on_abandoned(self, anchor_ps_on_it: int):
        nodes = [
            _node(5, "MERGED", submitted="2026-01-01"),
            _node(50, "ABANDONED"),
            _node(10, "MERGED", submitted="2026-02-01", current_patchset=5),
        ]
        return _payload(10, nodes, [_edge(50, 10, cps=anchor_ps_on_it, cl=5)])

    def test_abandoned_history_parent_stays_hidden(self, tmp_path):
        """61965 ps23 once sat on abandoned 62508."""
        views = _render(self._anchor_on_abandoned(2), tmp_path,
                        "a0m1", "a1m1")
        assert 50 not in _pos(views["a0m1"])
        assert 50 in _pos(views["a1m1"])

    def test_abandoned_current_parent_is_shown(self, tmp_path):
        views = _render(self._anchor_on_abandoned(5), tmp_path, "a0m1")
        assert 50 in _pos(views["a0m1"])


class TestChainIntoTrunkNode:
    def test_chain_ending_in_trunk_node_stays_straight(self, tmp_path):
        """54459's shape: 20's live kid 21 leads through 22 into trunk
        node 30, which has two kids of its own. 21 still continues
        straight up from 20 instead of skipping a row."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01"),
            _node(30, "MERGED", submitted="2026-03-01"),
            _node(20), _node(21), _node(22), _node(23),
            _node(31), _node(32),
        ]
        edges = [
            _edge(10, 20), _edge(20, 21),
            _edge(20, 23, pps=1, pl=2),
            _edge(21, 22), _edge(22, 30),
            _edge(30, 31), _edge(30, 32),
        ]
        pos = _pos(_render(_payload(10, nodes, edges), tmp_path,
                           "a0m1")["a0m1"])
        assert pos[21] == (pos[20][0], pos[20][1] - LEVEL_H)
        assert pos[22] == (pos[20][0], pos[20][1] - 2 * LEVEL_H)


class TestStacksLayout:
    """The Stacks layout: one column per in-flight subtree, standing
    on the merged patch it branches off."""

    def _series(self):
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01"),
            _node(20, "MERGED", submitted="2026-02-01"),
            _node(30, "MERGED", submitted="2026-03-01"),
            _node(101), _node(102),          # chain on 20
            _node(111), _node(112),          # chain on 10
            _node(113),                      # second subtree on 10
            _node(114),                      # fork: second kid of 111
            _node(121), _node(122),          # no merged base
        ]
        edges = [
            _edge(10, 20), _edge(20, 30),
            _edge(20, 101), _edge(101, 102),
            _edge(10, 111), _edge(111, 112), _edge(111, 114),
            _edge(10, 113),
            _edge(121, 122),
        ]
        return _payload(30, nodes, edges)

    def test_columns_stand_on_their_merged_base(self, tmp_path):
        view = _render(self._series(), tmp_path, "a0m0")["a0m0"]
        pos = _pos(view)
        # 30 has nothing in flight on it, so it is left out
        assert 30 not in pos
        assert pos[10][1] == pos[20][1] == pos[121][1] == 0
        # older base first, the subtree without a base last
        assert pos[10][0] < pos[20][0] < pos[121][0]
        assert pos[101] == (pos[20][0], -LEVEL_H)
        assert pos[102] == (pos[20][0], -2 * LEVEL_H)
        assert pos[122] == (pos[121][0], -LEVEL_H)

    def test_narrow_subtree_next_to_base_and_forks_widen_right(self, tmp_path):
        pos = _pos(_render(self._series(), tmp_path, "a0m0")["a0m0"])
        x10 = pos[10][0]
        # base 10 carries 113 (one column) and 111's fork (two)
        assert pos[113] == (x10, -LEVEL_H)
        assert pos[111] == (x10 + NODE_W, -LEVEL_H)
        assert {pos[112], pos[114]} == {
            (x10 + NODE_W, -2 * LEVEL_H), (x10 + 2 * NODE_W, -2 * LEVEL_H)}
        # 10's three columns end before 20's starts
        assert pos[20][0] >= x10 + 3 * NODE_W

    def _chain_with_branches(self, branch_len: int):
        """Chain 201..208 (no merged base); a branch of `branch_len`
        nodes off 202 and a leaf off 205."""
        nodes = [_node(c) for c in range(201, 209)]
        edges = [_edge(c, c + 1) for c in range(201, 208)]
        branch = list(range(301, 301 + branch_len))
        nodes += [_node(c) for c in branch] + [_node(400)]
        edges += [_edge(202, branch[0]), _edge(205, 400)]
        edges += [_edge(a, b) for a, b in zip(branch, branch[1:])]
        return _payload(201, nodes, edges)

    def test_branches_at_different_heights_share_a_column(self, tmp_path):
        pos = _pos(_render(self._chain_with_branches(1), tmp_path,
                           "a0m0")["a0m0"])
        x = pos[201][0]
        assert pos[301] == (x + NODE_W, pos[202][1] - LEVEL_H)
        assert pos[400] == (x + NODE_W, pos[205][1] - LEVEL_H)

    def test_branches_never_touch_in_a_column(self, tmp_path):
        """The higher branch (400, part of the main subtree) is placed
        first; 301..303 would end right under it in the same column,
        so they move one column further out."""
        pos = _pos(_render(self._chain_with_branches(3), tmp_path,
                           "a0m0")["a0m0"])
        x = pos[201][0]
        assert pos[400] == (x + NODE_W, pos[205][1] - LEVEL_H)
        assert pos[301] == (x + 2 * NODE_W, pos[202][1] - LEVEL_H)
        assert pos[303] == (x + 2 * NODE_W, pos[205][1])

    def _lone_stack_with_context(self):
        """100 stands on nothing in the graph; the build walked its
        ancestry: 50 (in flight, outside the graph) on merged 40."""
        nodes = [_node(10, "MERGED", submitted="2026-01-01"), _node(100)]
        payload = _payload(100, nodes, [])
        payload["context_nodes"] = [
            dict(_node(50), context=True, context_of=[100]),
            dict(_node(40, "MERGED"), context=True, context_of=[100]),
        ]
        payload["context_edges"] = [_edge(50, 100), _edge(40, 50)]
        return payload

    def test_lone_stack_stands_on_its_faded_ancestry(self, tmp_path):
        views = _render(self._lone_stack_with_context(), tmp_path,
                        "a0m0", "a0m1")
        pos = _pos(views["a0m0"])
        assert pos[40][1] == 0
        assert pos[50] == (pos[40][0], -LEVEL_H)
        assert pos[100] == (pos[40][0], -2 * LEVEL_H)
        assert {(40, 50), (50, 100)} <= _drawn(views["a0m0"])
        faded = {n["id"]: n for n in views["a0m0"]["nodes"]}
        assert faded[50]["opacity"] == faded[40]["opacity"] == 0.6
        assert "CR:" not in faded[50]["label"]
        # the trunk view never shows them
        assert not {40, 50} & set(_pos(views["a0m1"]))

    def test_context_counts_nowhere(self, tmp_path):
        result = _run(self._lone_stack_with_context(), tmp_path, "a0m0",
                      "--eval", "document.getElementById('stats').innerHTML"
                      " + '|' + (showNodeInfo(50),"
                      " document.getElementById('info').innerHTML)")
        stats, info = result["eval"].split("|", 1)
        assert "In-flight: 1<" in stats
        assert "Not part of this graph" in info and "#100" in info

    def test_no_edges_into_merged_nodes(self, tmp_path):
        view = _render(self._series(), tmp_path, "a0m0")["a0m0"]
        assert (10, 20) not in _drawn(view)
        assert {(20, 101), (10, 111), (10, 113)} <= _drawn(view)

    def test_hidden_parent_starts_its_own_column(self, tmp_path):
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01"),
            _node(50, "ABANDONED"), _node(100),
        ]
        payload = _payload(10, nodes, [_edge(50, 100)])
        views = _render(payload, tmp_path, "a0m0", "a1m0")
        assert _pos(views["a0m0"])[100][1] == 0
        pos = _pos(views["a1m0"])
        assert pos[100] == (pos[50][0], pos[50][1] - LEVEL_H)

    def test_merged_shown_keeps_the_trunk(self, tmp_path):
        pos = _pos(_render(self._series(), tmp_path, "a0m1")["a0m1"])
        assert pos[10][0] == pos[20][0] == pos[30][0] == 0

    def test_undrawn_node_shows_details_without_error(self, tmp_path):
        """The panel's chain lists merged patches the stacks layout
        leaves out; clicking one or focusing an undrawn anchor used to
        throw in vis.js."""
        payload = self._series()
        result = _run(payload, tmp_path, "a0m0", "--eval",
                      "clickNode(30); actions.focusSelection();"
                      " document.getElementById('info').innerHTML")
        assert "#30" in result["eval"]



class TestTrunkEdges:
    def test_no_edges_between_merged_patches(self, tmp_path):
        """The trunk column already shows landing order; a stale
        series edge between two merged patches only drew over it."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01", current_patchset=3),
            _node(11, "MERGED", submitted="2026-02-01", current_patchset=5),
            _node(12, "MERGED", submitted="2026-03-01"),
            _node(100),
        ]
        edges = [
            _edge(10, 11, pps=1, pl=3, cps=2, cl=5),
            _edge(10, 12, pps=3, pl=3),
            _edge(11, 100, pps=5, pl=5),
        ]
        view = _render(_payload(11, nodes, edges), tmp_path,
                       "a0m1")["a0m1"]
        assert _drawn(view) == {(11, 100)}
        pos = _pos(view)
        assert pos[10][0] == pos[11][0] == pos[12][0] == 0

    def _history_into_merged(self):
        """100 (the anchor) and 200 are in flight; an old patchset of
        merged 11 once sat on each of them."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01"),
            _node(11, "MERGED", submitted="2026-02-01", current_patchset=5),
            _node(100), _node(200),
        ]
        edges = [
            _edge(10, 100),
            _edge(100, 11, cps=1, cl=5),
            _edge(200, 11, cps=2, cl=5),
        ]
        return _payload(100, nodes, edges)

    def test_history_into_merged_drawn_only_where_it_places(self, tmp_path):
        view = _render(self._history_into_merged(), tmp_path, "a0m1")["a0m1"]
        pos = _pos(view)
        # 200 has nothing else: it is placed under 11, and the edge says why
        assert pos[200] == (pos[11][0] + NODE_W, pos[11][1] + LEVEL_H)
        assert (200, 11) in _drawn(view)
        # the anchor sits elsewhere; its history edge to 11 is not drawn
        assert (100, 11) not in _drawn(view)

    def test_change_with_own_parent_stays_with_it(self, tmp_path):
        """LU-17916's 66902: 300 sits on trunk node 11 and once carried
        the older trunk node 10, which the trunk walk reaches first.
        300 goes above 11, not under 10, and its history edge to 10
        isn't drawn."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01", current_patchset=4),
            _node(11, "MERGED", submitted="2026-02-01", current_patchset=3),
            _node(100), _node(300),
        ]
        edges = [
            _edge(10, 100),
            _edge(11, 300, pps=1, pl=3),
            _edge(300, 10, cps=1, cl=4),
        ]
        view = _render(_payload(100, nodes, edges), tmp_path, "a0m1")["a0m1"]
        pos = _pos(view)
        assert pos[300][1] == pos[11][1] - LEVEL_H
        assert (11, 300) in _drawn(view)
        assert (300, 10) not in _drawn(view)

    def test_unhooked_chains_keep_their_own_columns(self, tmp_path):
        """61977's shape: 300 and the chain 310 -> 311 -> 312 are based
        on master history older than any merged patch. Each keeps its
        own column on the right; the chain must not stand on 300."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01"),
            _node(100), _node(300), _node(310), _node(311), _node(312),
        ]
        edges = [_edge(10, 100), _edge(310, 311), _edge(311, 312)]
        pos = _pos(_render(_payload(100, nodes, edges), tmp_path,
                           "a0m1")["a0m1"])
        assert pos[300][1] == pos[310][1] == 0
        assert pos[300][0] != pos[310][0]
        assert pos[311] == (pos[310][0], -LEVEL_H)
        assert pos[312] == (pos[310][0], -2 * LEVEL_H)


class TestNoFalseChains:
    """Two nodes right above each other in one column read as parent
    and child; unrelated nodes must not end up that way."""

    def _false_chains(self, view):
        pos = _pos(view)
        at = {p: cn for cn, p in pos.items()}
        drawn = _drawn(view)
        return [(cn, at[(x, y - LEVEL_H)]) for cn, (x, y) in pos.items()
                if (x, y - LEVEL_H) in at and x != 0
                and (cn, at[(x, y - LEVEL_H)]) not in drawn
                and (at[(x, y - LEVEL_H)], cn) not in drawn]

    def test_side_kid_off_trunk_column_avoids_unrelated(self, tmp_path):
        """61965's shape: 100 stands right of trunk node 20, so its side
        kid 102 is laid out on the trunk column and bumped right; it
        must not land right above the unrelated 120."""
        nodes = [
            _node(5, "MERGED", submitted="2026-01-01"),
            _node(20, "MERGED", submitted="2026-02-01"),
            _node(30, "MERGED", submitted="2026-03-01"),
        ] + [_node(c) for c in (100, 101, 102, 103, 110, 120)]
        edges = [_edge(20, k) for k in (100, 110, 120)] + [
            _edge(100, 101), _edge(101, 103), _edge(100, 102)]
        view = _render(_payload(5, nodes, edges), tmp_path, "a0m1")["a0m1"]
        assert self._false_chains(view) == []

    def test_trunk_parent_takes_the_nearest_free_slot(self, tmp_path):
        """49342's 50621: 301 can't take the right slot (it would stack
        under 300) and goes left with a fork; 302 then takes the free
        right slot two columns out, not the left one three out."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01", current_patchset=2),
            _node(11, "MERGED", submitted="2026-02-01", current_patchset=2),
        ] + [_node(c) for c in (300, 301, 302, 310, 311, 400)]
        edges = [_edge(300, 10, cps=1, cl=2), _edge(301, 11, cps=1, cl=2),
                 _edge(302, 11, cps=1, cl=2), _edge(301, 310), _edge(301, 311),
                 _edge(11, 400, pps=2, pl=2)]
        view = _render(_payload(400, nodes, edges), tmp_path, "a0m1")["a0m1"]
        pos = _pos(view)
        assert pos[301][0] == -NODE_W
        assert pos[302] == (2 * NODE_W, pos[11][1] + LEVEL_H)
        assert self._false_chains(view) == []

    def test_trunk_parents_of_neighbouring_rows(self, tmp_path):
        """49342's shape: 300 and 301 each once carried one of two
        neighbouring trunk nodes and have nothing else; they must not
        stack in one column."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01", current_patchset=2),
            _node(11, "MERGED", submitted="2026-02-01", current_patchset=2),
            _node(300), _node(301), _node(400),
        ]
        edges = [_edge(300, 10, cps=1, cl=2), _edge(301, 11, cps=1, cl=2),
                 _edge(11, 400, pps=2, pl=2)]
        view = _render(_payload(400, nodes, edges), tmp_path, "a0m1")["a0m1"]
        pos = _pos(view)
        assert pos[300][1] == pos[10][1] + LEVEL_H
        assert pos[301][1] == pos[11][1] + LEVEL_H
        assert self._false_chains(view) == []

    def test_edge_through_a_node_arcs_around_it(self, tmp_path):
        """LU-18222's shape: the anchor's parent 101 is based on merged
        10, below the later merged 11 and 12 in the trunk column. Its
        edge from 10 would run straight through them."""
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01"),
            _node(11, "MERGED", submitted="2026-02-01"),
            _node(12, "MERGED", submitted="2026-03-01"),
            _node(100), _node(101),
        ]
        edges = [_edge(10, 101, inferred=True), _edge(101, 100)]
        view = _render(_payload(100, nodes, edges), tmp_path, "a0m1")["a0m1"]
        pos = _pos(view)
        assert pos[10][0] == pos[11][0] == pos[101][0] == 0
        smooth = {(e["from"], e["to"]): e["smooth"] for e in view["edges"]}
        assert smooth[(10, 101)] == "curvedCW"
        assert smooth[(101, 100)] == "cubicBezier"


class TestTrunkSpacing:
    def test_trunk_gap_stops_at_the_next_trunk_node(self, tmp_path):
        """54459's shape: trunk node 11's side branch 100 -> 101 is two
        rows tall, but an old patchset of the newer trunk node 12 sat
        on 101, and 12 has a long chain of its own. The rows reserved
        above 11 must not count 12's chain."""
        chain = list(range(200, 210))
        nodes = [
            _node(10, "MERGED", submitted="2026-01-01"),
            _node(11, "MERGED", submitted="2026-02-01"),
            _node(12, "MERGED", submitted="2026-03-01", current_patchset=2),
            _node(100), _node(101),
        ] + [_node(c) for c in chain]
        edges = [_edge(11, 100), _edge(100, 101), _edge(101, 12, cps=1, cl=2),
                 _edge(12, chain[0], pps=2, pl=2)]
        edges += [_edge(a, b) for a, b in zip(chain, chain[1:])]
        pos = _pos(_render(_payload(10, nodes, edges), tmp_path,
                           "a0m1")["a0m1"])
        assert pos[11][1] - pos[12][1] == 3 * LEVEL_H



class TestConflicts:
    """'Show conflicts' and the panel section of a --conflicts build."""

    def _payload(self, with_conflicts: bool = True):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(101), _node(102), _node(103), _node(104)]
        edges = [_edge(10, 101), _edge(101, 102), _edge(10, 103), _edge(10, 104)]
        payload = _payload(101, nodes, edges)
        if with_conflicts:
            payload["conflicts"] = {
                "branch": "master", "tip": "4a7bddaf15ceb6d4fbe9e54cef4e2cf23a98263f",
                "results": {
                    "101": {"status": "clean"},
                    "102": {"status": "conflict", "files": ["x.c"], "under": 101},
                    "103": {"status": "clean"},
                    "104": {"status": "clean"},
                },
                "pairs": [{"a": 101, "b": 103, "files": ["a.c"]}],
                "pairs_tried": 2, "pair_errors": 0,
            }
        return payload

    _VIEWS = """JSON.stringify(['trunk', 'stacks'].flatMap(mode => [false, true].map(on => {
        setLayout(mode, false);
        document.getElementById('chk-conflicts').checked = on;
        renderGraph();
        const n = nodesDS.get(102);
        return {mode, on,
                edges: edgesDS.get().filter(e => String(e.id).startsWith('x'))
                    .map(e => [e.from, e.to, e.width, e.color.color, e.arrows.to.enabled, e.title]),
                label: n.label, border: n.borderWidth};
    })))"""

    def _views(self, payload, tmp_path):
        return json.loads(_eval(payload, tmp_path, self._VIEWS))

    def _panel(self, payload, tmp_path, cn):
        return _eval(payload, tmp_path,
                     f"showNodeInfo({cn}), document.getElementById('info').innerHTML")

    def test_checkbox_only_with_trial_merges(self, tmp_path):
        expr = "document.getElementById('lbl-conflicts').style.display || ''"
        assert _eval(self._payload(False), tmp_path, expr) == "none"
        assert _eval(self._payload(), tmp_path, expr) == ""

    def test_conflict_edges_only_when_checked(self, tmp_path):
        for view in self._views(self._payload(), tmp_path):
            if view["on"]:
                assert view["edges"] == [
                    [101, 103, 4, "#ff4d4f", False, "Conflict in a.c"]], view["mode"]
            else:
                assert view["edges"] == [], view["mode"]

    def test_ticking_it_keeps_the_camera_and_the_selection(self, tmp_path):
        """Only the overlay changes, so the view is not fitted again;
        "Show abandoned" moves nodes and still fits."""
        expr = """(() => {
            globalThis.setTimeout = f => f();
            clickNode(103);
            network.fits = 0;
            const chk = document.getElementById('chk-conflicts');
            const seen = [];
            for (const on of [true, false]) {
                chk.checked = on;
                chk.fire('change');
                seen.push([network.fits, network.getSelectedNodes(),
                           edgesDS.get().filter(e => String(e.id).startsWith('x')).length]);
            }
            document.getElementById('chk-abandoned').fire('change');
            seen.push([network.fits]);
            return JSON.stringify(seen);
        })()"""
        assert json.loads(_eval(self._payload(), tmp_path, expr)) == [
            [0, [103], 1], [0, [103], 0], [1]]

    def test_a_change_that_does_not_apply_is_marked(self, tmp_path):
        for view in self._views(self._payload(), tmp_path):
            marked = "✗ conflicts with master" in view["label"].split("\n")[0]
            assert marked == view["on"], view["mode"]
            assert (view["border"] == 4) == view["on"], view["mode"]

    def test_without_trial_merges_nothing_is_drawn(self, tmp_path):
        for view in self._views(self._payload(False), tmp_path):
            assert view["edges"] == [] and view["border"] != 4
        assert "Conflicts" not in self._panel(self._payload(False), tmp_path, 101)

    def test_panel_has_a_review_and_a_conflicts_card(self, tmp_path):
        """No Dependents/Dependencies lists: the graph shows the stack."""
        info = self._panel(self._payload(), tmp_path, 101)
        assert info.index("<span>Review</span>") < info.index("<span>Conflicts</span>")
        assert "Applies" in info and "4a7bddaf15" in info
        section = info[info.index("<span>Conflicts</span>"):]
        assert "clickNode(103)" in section and "<code>a.c</code>" in section
        assert "\u2717 1 conflict</span>" in section
        assert "Dependents" not in info and "Dependencies" not in info and "Tip (no" not in info

    def test_panel_lists_conflicts_through_the_changes_below(self, tmp_path):
        info = self._panel(self._payload(), tmp_path, 102)
        assert "Does not apply" in info and "after the 1 change below it" in info
        assert "<code>x.c</code>" in info
        assert "clickNode(103)" in info
        assert "Through the changes below it (1)" in info
        assert '<div class="pitem-files"><a href="#" onclick="clickNode(101);return false">#101</a>: ' in info

    def test_its_own_conflicts_come_first(self, tmp_path):
        """102's own conflict (with 104) heads the list; 101's (with 103)
        follows in a group of its own. No status badge: all are in flight."""
        payload = self._payload()
        payload["conflicts"]["results"]["102"] = {"status": "clean", "under": 101}
        payload["conflicts"]["pairs"] = [{"a": 101, "b": 103, "files": ["a.c"]},
                                         {"a": 102, "b": 104, "files": ["b.c"]}]
        info = self._panel(payload, tmp_path, 102)
        sec = info[info.index("With other in-flight changes (1)"):]
        assert sec.index("clickNode(104)") < sec.index("Through the changes below it (1)") < sec.index("clickNode(103)")
        assert "sbadge" not in sec
        assert "\u2717 2 conflicts</span>" in info

    def test_a_change_whose_base_is_not_on_the_branch(self, tmp_path):
        """105 sits on an abandoned change: only its own diff was tried,
        so the label and panel blame the base, not master."""
        payload = self._payload()
        payload["nodes"].append(_node(105))
        payload["edges"].append(_edge(10, 105))
        payload["conflicts"]["results"]["105"] = {
            "status": "conflict", "files": ["y.c"],
            "base": {"cn": 30, "status": "ABANDONED"}}
        expr = """(() => {
            document.getElementById('chk-conflicts').checked = true;
            renderGraph();
            const n = nodesDS.get(105);
            showNodeInfo(105);
            return JSON.stringify([n.label.split('\\n')[0], n.color.border,
                                   document.getElementById('info').innerHTML]);
        })()"""
        first, border, info = json.loads(_eval(payload, tmp_path, expr))
        assert first.endswith("\u2717 base not on master")
        assert border == "#d29922"
        assert "Does not apply without its base" in info
        assert "abandoned <a href=\"https://gerrit.invalid/c/" in info and "/+/30\"" in info

    def test_a_change_that_conflicts_with_its_parents_new_patch_set(self, tmp_path):
        payload = self._payload()
        payload["edges"] = [e for e in payload["edges"] if (e["from"], e["to"]) != (101, 102)]
        payload["edges"].append(_edge(101, 102, 1, 2))
        payload["conflicts"]["results"]["102"]["parent"] = {"cn": 101, "files": ["x.c"]}
        expr = """(() => {
            document.getElementById('chk-conflicts').checked = true;
            renderGraph();
            const n = nodesDS.get(102);
            showNodeInfo(102);
            return JSON.stringify([n.label.split('\\n')[0], n.color.border,
                                   document.getElementById('info').innerHTML]);
        })()"""
        first, border, info = json.loads(_eval(payload, tmp_path, expr))
        assert first.endswith("\u2717 conflicts with #101") and border == "#d29922"
        assert "it sits on ps1 of" in info and "the current patch set (ps2)" in info
        assert "needs a rebase onto" in info

    def _with_queue(self):
        payload = self._payload()
        payload["conflicts"]["next"] = {
            "branch": "master-next", "tip": "0c93230bde933914cf0a8d325ed36a25205d2f05",
            "ahead": 77,
            "results": {
                "101": {"status": "conflict", "files": ["a.c"],
                        "with": {"cn": 103, "subject": "LU-1 change 103",
                                 "commit": "c" * 40, "position": 9}},
                "103": {"status": "conflict", "files": ["b.c"],
                        "with": {"cn": 104, "subject": "LU-1 change 104",
                                 "commit": "d" * 40, "position": 3}},
                "104": {"status": "queued"},
            },
        }
        return payload

    def test_a_collision_with_a_queued_patch(self, tmp_path):
        expr = """(() => {
            document.getElementById('chk-conflicts').checked = true;
            renderGraph();
            const q = edgesDS.get().filter(e => String(e.id).startsWith('q'))
                .map(e => [e.from, e.to, e.dashes !== false]);
            return JSON.stringify([q, nodesDS.get(103).label.split('\\n')[0],
                                   nodesDS.get(101).label.split('\\n')[0]]);
        })()"""
        edges, label103, label101 = json.loads(_eval(self._with_queue(), tmp_path, expr))
        # 101-103 is drawn as an in-graph pair already
        assert edges == [[103, 104, True]]
        assert label103.endswith("\u2717 conflicts with master-next #104")
        assert label101.endswith("\u2717 conflicts with master-next #103")

    def test_a_stale_queue_is_not_used(self, tmp_path):
        payload = self._payload()
        payload["conflicts"]["next"] = {
            "branch": "master-next", "tip": "0" * 40,
            "skipped": "master-next is stale, master has moved on since it was built"}
        info = self._panel(payload, tmp_path, 101)
        assert "Not used: master-next is stale, master has moved on since it was built." in info

    def test_panel_says_where_in_the_queue(self, tmp_path):
        payload = self._with_queue()
        info = self._panel(payload, tmp_path, 103)
        assert "master-next<small>0c93230bde, 77 queued</small>" in info
        assert "Conflicts with</span> number 3 in the queue: <code>b.c</code>" in info
        nxt = info[info.index("master-next<small>"):info.index("With other in-flight changes")]
        assert 'class="pitem-main" onclick="clickNode(104)"' in nxt
        assert "Queued: this change is on master-next" in self._panel(payload, tmp_path, 104)
        # a queued patch outside the graph opens in Gerrit
        payload["conflicts"]["next"]["results"]["103"]["with"]["cn"] = 58097
        info = self._panel(payload, tmp_path, 103)
        assert '<a href="https://gerrit.invalid/c/' in info and '/+/58097" target="_blank">' in info
        assert "Not tried: it does not land on master" in self._panel(payload, tmp_path, 102)

    def test_the_conflicting_changes_show_their_hashtags(self, tmp_path):
        """Which hashtags a conflicting change has, master-next (queued)
        in the queued border color."""
        payload = self._with_queue()
        by_id = {n["id"]: n for n in payload["nodes"]}
        by_id[103]["hashtags"] = ["master-next", "<b>ec</b>"]
        by_id[104]["hashtags"] = ["218_review_priority"]
        info = self._panel(payload, tmp_path, 101)
        row = info[info.index("With other in-flight changes"):]
        assert row.index("clickNode(103)") < row.index(">master-next</span>") < row.index("<code>a.c</code>")
        assert "style=\"color:#8957e5;box-shadow:inset 0 0 0 1px #8957e5\">master-next</span>" in row
        assert "&lt;b&gt;ec&lt;/b&gt;" in row and "<b>ec</b>" not in info
        # the queued patch master-next conflicts with
        info = self._panel(payload, tmp_path, 103)
        nxt = info[info.index("master-next<small>"):info.index("With other in-flight changes")]
        assert "number 3 in the queue" in nxt and ">218_review_priority</span>" in nxt
        # no hashtags: only its state
        by_id[103]["hashtags"] = []
        info = self._panel(payload, tmp_path, 101)
        row = info[info.index("With other in-flight changes"):]
        assert 'CI pending</span></div>' in row and 'class="ptag"' not in row

    def test_the_conflicting_changes_say_what_they_need(self, tmp_path):
        """Ready, a CI failure or a CR veto, +1s still missing, or CI
        still to finish: what the other side needs before it lands."""
        ci = lambda j, m: {"verified_votes": [{"name": "Jenkins", "value": j}, {"name": "Maloo", "value": m}],
                           "verified_pass": j > 0 and m >= 0 and not (j < 0 or m < 0),
                           "verified_fail": j < 0 or m < 0}
        cr = lambda *v: {"cr_votes": [{"name": f"R{i}", "value": x} for i, x in enumerate(v)],
                         "cr_veto": any(x < 0 for x in v)}
        cases = [
            ({**ci(1, 1), **cr(1, 1)}, {}, "\u2713 Ready"),
            ({**ci(1, -1), **cr(1, 1)}, {}, "\u2717 Maloo -1"),
            ({**ci(-1, 0), **cr()}, {}, "\u2717 Jenkins -1"),
            ({**ci(1, 1), **cr(1, 1, 1, -1)}, {}, "\u2717 CR veto"),
            ({**ci(1, 1), **cr(1)}, {}, "Reviews 1 of 2 +1s"),
            ({**ci(1, 1), **cr(1)}, {"is_backport": True}, "\u2713 Ready"),
            ({**ci(1, 0), **cr(1, 1)}, {}, "CI pending"),
            ({**ci(1, 1), **cr(1, 1)}, {"is_wip": True}, "WIP</span>"),
        ]
        expr = "JSON.stringify(G.nodes.filter(n => n.id > 200).map(n => landingState(n)))"
        payload = self._payload()
        for i, (review, extra, _) in enumerate(cases):
            payload["nodes"].append(_node(201 + i, review=review, **extra))
            payload["edges"].append(_edge(10, 201 + i))
        got = json.loads(_eval(payload, tmp_path, expr))
        assert all(want in g for (_, _, want), g in zip(cases, got)), list(zip([c[2] for c in cases], got))
        assert "#3fb950" in got[0] and "#f85149" in got[1] and "#a82828" in got[3] and "#388bfd" in got[4]

    def test_panel_without_conflicts_and_for_merged_changes(self, tmp_path):
        info = self._panel(self._payload(), tmp_path, 104)
        section = info[info.index("<span>Conflicts</span>"):]
        assert '<div class="pnone">None</div>' in section and "\u2713 None</span>" in section
        assert "Conflicts" not in self._panel(self._payload(), tmp_path, 10)


class TestRender:
    def test_a_subject_with_a_script_end_tag(self, tmp_path):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(101, subject="LU-1 fix </script><b>x</b> in docs")]
        payload = _payload(101, nodes, [_edge(10, 101)])
        html = generate_html(payload)
        assert "</script><b>" not in html
        G = json.loads(re.search(r"const G = (\{.*?\});\n", html, re.S).group(1))
        assert G["nodes"][1]["subject"] == "LU-1 fix </script><b>x</b> in docs"
        assert _eval(payload, tmp_path, "nodeMap[101].subject") == G["nodes"][1]["subject"]


class TestTrunkSideKidLean:
    """A trunk side kid whose subtree leans back toward the trunk (61965:
    69346 with 69694 left of it) must not land on its sibling's column:
    _resolveCollisions then pushed a whole row of side chains one column
    out, bending each of them."""

    def _payload(self):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(11, "MERGED", submitted="2026-02-01"),
                 _node(12, "MERGED", submitted="2026-03-01")]
        edges = [_edge(10, 11), _edge(11, 12)]

        def chain(*cns):
            nodes.extend(_node(c) for c in cns)
            edges.extend(_edge(a, b) for a, b in zip(cns, cns[1:]))
        chain(201)
        chain(202, 212, 222, 232)
        chain(203)
        chain(204, 214, 224)        # 204's left side kid: inward, toward 202
        chain(215, 225, 235, 245)   # 204's main kid
        chain(216, 226)             # 204's right side kid
        edges += [_edge(11, c) for c in (201, 202, 203, 204)]
        edges += [_edge(204, 215), _edge(204, 216)]
        return _payload(10, nodes, edges)

    def test_it_moves_out_by_what_it_leans(self, tmp_path):
        pos = _pos(_render(self._payload(), tmp_path, "a0m1")["a0m1"])
        assert {pos[c][0] for c in (202, 212, 222, 232)} == {NODE_W}
        x = pos[204][0]
        assert x == 3 * NODE_W
        assert pos[214][0] == pos[224][0] == x - NODE_W
        assert pos[216][0] == pos[226][0] == x + NODE_W
        assert {pos[c][0] for c in (215, 225, 235, 245)} == {x}

    def test_it_stays_when_what_it_leans_over_is_free(self, tmp_path):
        """54050: the anchor is the trunk top; its kid on an old patchset
        branches left and reaches back over the free column above it."""
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(11, "MERGED", submitted="2026-02-01", current_patchset=2)]
        edges = [_edge(10, 11)]
        for cn in (401, 411, 421, 431, 441, 451, 461):
            nodes.append(_node(cn))
        # 421: main kid 431 (it has a kid), side kids 441 left, 451 right
        edges += [_edge(11, 401, 1, 2), _edge(401, 411), _edge(411, 421),
                  _edge(421, 431), _edge(421, 441), _edge(421, 451), _edge(431, 461)]
        pos = _pos(_render(_payload(11, nodes, edges), tmp_path, "a0m1")["a0m1"])
        assert pos[401][0] == pos[431][0] == -NODE_W
        assert pos[451][0] == 0


class TestLonePieces:
    """Chains tied to nothing placed (based on master older than the
    trunk) sit next to the anchor, not past the widest row."""

    def _payload(self):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(11, "MERGED", submitted="2026-02-01"),
                 _node(101)]
        edges = [_edge(10, 11), _edge(11, 101)]
        # a wide row of side branches under the anchor's row
        for cn in range(201, 207):
            nodes.append(_node(cn))
            edges.append(_edge(10, cn))
        # 104 and 105 beside 103, one row above where a two-row tree
        # next to the anchor would end
        nodes += [_node(102), _node(103), _node(104), _node(105)]
        edges += [_edge(101, 102), _edge(102, 103), _edge(102, 104), _edge(102, 105)]
        # an unplaced main-series tree and a lone separate-group change
        nodes += [_node(401), _node(402), _node(301, series_group=1)]
        edges.append(_edge(401, 402))
        payload = _payload(101, nodes, edges)
        payload["separate_groups"] = [{"id": 1, "label": "x", "node_ids": [301]}]
        payload["separate_chains"] = [[301]]
        return payload

    def test_next_to_the_anchor(self, tmp_path):
        pos = _pos(_render(self._payload(), tmp_path, "a0m1")["a0m1"])
        assert pos[101] == (0, 0)
        assert max(pos[cn][0] for cn in range(201, 207)) >= 2 * NODE_W
        assert pos[105] == (NODE_W, -2 * LEVEL_H)
        # a free half column (STACK_GAP) to the sides, a free row above
        # and below: one step further right than 105 alone needs
        assert (pos[401], pos[402]) == ((NODE_W * 5 // 2, 0), (NODE_W * 5 // 2, -LEVEL_H))
        assert pos[301] == (NODE_W * 4, 0)


class TestUnrelatedParents:
    """69505: in flight, not in the series, but 69506 sits on it."""

    def _payload(self):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(69505, unrelated_parent=True, opened_at=1767225600),
                 _node(69506, opened_at=1767225600), _node(66481, opened_at=1767225600)]
        edges = [_edge(10, 69505, inferred=True), _edge(69505, 69506, 1, 2),
                 _edge(69506, 66481)]
        payload = _payload(66481, nodes, edges)
        payload["stats"]["status_counts"] = {"MERGED": 1, "NEW": 2}
        payload["stats"]["unrelated_parent_cns"] = [69505]
        return payload

    def test_it_holds_the_change_on_it_in_both_views(self, tmp_path):
        for combo, view in _render(self._payload(), tmp_path, "a0m1", "a0m0").items():
            pos = _pos(view)
            assert pos[69506] == (pos[69505][0], pos[69505][1] - LEVEL_H), combo
            assert (69505, 69506) in _drawn(view), combo

    def test_it_is_dimmed(self, tmp_path):
        view = _render(self._payload(), tmp_path, "a0m1")["a0m1"]
        nodes = {n["id"]: n for n in view["nodes"]}
        assert nodes[69505]["bg"] == "#161b22" and nodes[69505]["border"] == "#388bfd"
        assert nodes[69506]["bg"] != "#161b22"

    def test_it_counts_nowhere_and_says_why_it_is_there(self, tmp_path):
        expr = """JSON.stringify([
            health.ready + health.pending + health.veto + health.maloo
                + health.jenkins + health.other,
            stBuildRecords().map(r => r.id).sort(),
            document.getElementById('legend').innerHTML.includes('Unrelated in-flight parent'),
            (showNodeInfo(69505), document.getElementById('info').innerHTML)])"""
        chips, records, legend, info = json.loads(_eval(self._payload(), tmp_path, expr))
        assert chips == 2
        assert records == [66481, 69506]
        assert legend
        assert "Not part of this series" in info and "clickNode(69506)" in info


class TestFocus:
    """The "Hashtags/topics" picker: any hashtags/topics of the graph
    ticked together; the asked ones (stats.focus) first."""

    FADED = 0.25

    def _payload(self, asked=("prio",), on=False, tag="prio"):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(101, hashtags=[tag], opened_at=1767225600),
                 _node(102, hashtags=["master-next"], opened_at=1767225600),
                 _node(103, hashtags=[tag], opened_at=1767225600),
                 _node(104, topic="ec2", opened_at=1767225600)]
        payload = _payload(101, nodes, [_edge(10, 101), _edge(101, 102), _edge(10, 103),
                                        _edge(10, 104)])
        payload["stats"]["status_counts"] = {"MERGED": 1, "NEW": 4}
        payload["stats"]["summary"] = {"as_of": 1767225600}
        if asked:
            members = [101, 103] + ([104] if "ec2" in asked else [])
            payload["stats"]["focus"] = {
                "hashtags": [a for a in asked if a != "ec2"],
                "topics": [a for a in asked if a == "ec2"], "on_by_default": on,
                "node_count": len(members), "status_counts": {"NEW": len(members)},
                "summary": {"patches": len(members)}}
        return payload

    # header, opacity and label color of 101..104, the picker's label
    # and ticks, and the legend, as the page opens and after each pick
    def _looks(self, payload, tmp_path, *picks):
        expr = """(() => {
            const look = () => [document.getElementById('stats').innerHTML,
                Object.fromEntries([101, 102, 103, 104].map(id => [id,
                    [nodesDS.get(id).opacity, nodesDS.get(id).font.color]])),
                document.getElementById('tags-label').textContent, [...tagSel],
                document.getElementById('legend').innerHTML];
            const out = [look()];
            for (const keys of PICKS) { setTagSelection(keys); out.push(look()); }
            return JSON.stringify(out);
        })()""".replace("PICKS", json.dumps(list(picks)))
        return json.loads(_eval(payload, tmp_path, expr))

    def _faded(self, styles):
        return sorted(int(i) for i, (opacity, font) in styles.items()
                      if opacity == self.FADED and font.startswith("rgba("))

    def test_with_an_anchor_nothing_is_ticked(self, tmp_path):
        (stats, styles, label, sel, legend), (stats2, styles2, label2, sel2, legend2) = \
            self._looks(self._payload(), tmp_path, ["hashtag:prio"])
        assert sel == [] and label == "all changes"
        assert "In-flight: 4<" in stats and "5 changes" in stats and " of " not in stats
        assert self._faded(styles) == []
        assert sel2 == ["hashtag:prio"] and label2 == "#prio"
        assert "In-flight: 2<" in stats2 and "2 of 5 changes" in stats2
        assert self._faded(styles2) == [102, 104]
        # 102/104 keep their own colors, only fainter; no legend entry
        assert styles2["101"] == styles["101"]
        assert legend2 == legend

    def test_from_one_hashtag_alone_it_opens_ticked(self, tmp_path):
        (stats, styles, label, sel, _l), (stats2, styles2, _lb, sel2, _l2) = \
            self._looks(self._payload(on=True), tmp_path, [])
        assert sel == ["hashtag:prio"] and label == "#prio" and "2 of 5 changes" in stats
        assert self._faded(styles) == [102, 104]
        assert sel2 == [] and self._faded(styles2) == []

    def test_several_together(self, tmp_path):
        (stats, styles, label, sel, _l), (stats2, styles2, label2, sel2, _l2) = self._looks(
            self._payload(asked=("prio", "ec2"), on=True), tmp_path,
            ["topic:ec2", "hashtag:master-next"])
        assert sorted(sel) == ["hashtag:prio", "topic:ec2"] and label == "#prio, topic ec2"
        assert "3 of 5 changes" in stats and self._faded(styles) == [102]
        assert label2 == "topic ec2, #master-next" and "2 of 5 changes" in stats2
        assert self._faded(styles2) == [101, 103]

    def test_the_list(self, tmp_path):
        expr = "document.getElementById('tag-menu').innerHTML"
        one = _eval(self._payload(), tmp_path, expr)
        assert one.index('data-act="all"') < one.index("Asked for") \
            < one.index('data-key="hashtag:prio"') < one.index("Also in this graph") \
            < one.index('data-key="hashtag:master-next"') < one.index('data-key="topic:ec2"')
        assert '#prio</span><span class="tag-count">2<' in one and " checked" not in one
        ticked = _eval(self._payload(on=True), tmp_path, expr)
        assert 'data-key="hashtag:prio" checked' in ticked and ticked.count(" checked") == 1
        nothing = _eval(self._payload(asked=()), tmp_path, expr)
        assert "tag-group" not in nothing and 'data-key="hashtag:prio"' in nothing
        absent = _eval(self._payload(asked=("nobody",)), tmp_path, expr)
        assert '#nobody</span><span class="tag-count">0<' in absent

    def test_most_common_first(self, tmp_path):
        payload = self._payload(asked=())
        for cn, tags in ((101, ["zz"]), (102, ["zz"]), (103, ["aa"])):
            payload["nodes"][[n["id"] for n in payload["nodes"]].index(cn)]["hashtags"] = tags
        menu = _eval(payload, tmp_path, "document.getElementById('tag-menu').innerHTML")
        assert menu.index('data-key="hashtag:zz"') < menu.index('data-key="hashtag:aa"') \
            < menu.index('data-key="topic:ec2"')

    def test_an_unrelated_parent_counts_nowhere(self, tmp_path):
        payload = self._payload(on=True)
        payload["nodes"].append(_node(105, hashtags=["prio"], unrelated_parent=True,
                                      opened_at=1767225600))
        payload["edges"].append(_edge(105, 104))
        payload["stats"]["unrelated_parent_cns"] = [105]
        expr = """JSON.stringify([document.getElementById('tag-menu').innerHTML,
            document.getElementById('stats').innerHTML, nodesDS.get(105).opacity])"""
        menu, stats, opacity = json.loads(_eval(payload, tmp_path, expr))
        assert '#prio</span><span class="tag-count">2<' in menu
        assert "2 of 5 changes" in stats and opacity == self.FADED

    def test_the_menu_ticks_and_clears(self, tmp_path):
        expr = """(() => {
            const menu = document.getElementById('tag-menu');
            const out = [];
            for (const [key, checked] of [['topic:ec2', true], ['hashtag:prio', true], ['topic:ec2', false]]) {
                menu.listeners.change[0]({ target: { dataset: { key }, checked } });
                out.push([...tagSel].sort());
            }
            menu.listeners.click[0]({ target: { dataset: { act: 'all' } } });
            out.push([...tagSel], document.getElementById('tag-pick').classList.contains('on'));
            return JSON.stringify(out);
        })()"""
        assert json.loads(_eval(self._payload(), tmp_path, expr)) == [
            ["topic:ec2"], ["hashtag:prio", "topic:ec2"], ["hashtag:prio"], [], False]

    def test_no_hashtags_no_picker(self, tmp_path):
        payload = self._payload(asked=())
        for n in payload["nodes"]:
            n["hashtags"], n["topic"] = [], ""
        expr = "document.getElementById('tag-pick').style.display || ''"
        assert _eval(payload, tmp_path, expr) == "none"

    def test_nothing_ticked_looks_like_nothing_asked(self, tmp_path):
        payload, plain = self._payload(), self._payload(asked=())
        a, b = (_render(p, tmp_path, "a0m1", "a0m0", "a1m1") for p in (payload, plain))
        assert a == b
        assert self._looks(payload, tmp_path)[0] == self._looks(plain, tmp_path)[0]

    def test_edges_fade_with_their_changes(self, tmp_path):
        payload = self._payload(on=True)
        payload["conflicts"] = {
            "branch": "master", "tip": "4a7bddaf15ceb6d4fbe9e54cef4e2cf23a98263f",
            "results": {str(c): {"status": "clean"} for c in (101, 102, 103, 104)},
            "pairs": [{"a": 101, "b": 104, "files": ["a.c"]},
                      {"a": 102, "b": 104, "files": ["b.c"]}],
            "pairs_tried": 3, "pair_errors": 0,
        }
        expr = """(() => {
            document.getElementById('chk-conflicts').checked = true;
            renderGraph();
            return JSON.stringify(edgesDS.get().map(e => [e.from, e.to, e.color.opacity]));
        })()"""
        edges = {(a, b): o for a, b, o in json.loads(_eval(payload, tmp_path, expr))}
        assert edges[(10, 101)] == self.FADED and edges[(101, 102)] == self.FADED
        # a conflict stays while either side is ticked
        assert edges[(101, 104)] == 1 and edges[(102, 104)] == self.FADED

    def test_panel(self, tmp_path):
        expr = """JSON.stringify([(showNodeInfo(102), document.getElementById('info').innerHTML),
            (showNodeInfo(103), document.getElementById('info').innerHTML)])"""
        untracked, tracked = json.loads(_eval(self._payload(), tmp_path, expr))
        assert "No #prio: in the graph next to" in untracked and "clickNode(101)" in untracked
        assert "which has it." in untracked and "Faded" not in untracked
        assert "box-shadow:inset 0 0 0 1px var(--accent)" in tracked and "No #prio" not in tracked
        untracked, _ = json.loads(_eval(self._payload(on=True), tmp_path, expr))
        assert "Faded, and not counted." in untracked
        untracked, _ = json.loads(_eval(self._payload(asked=("prio", "ec2"), on=True), tmp_path, expr))
        assert "None of #prio, topic ec2: in the graph next to" in untracked and "which has one" in untracked
        untracked, _ = json.loads(_eval(self._payload(asked=()), tmp_path, expr))
        assert "No #" not in untracked and "None of" not in untracked

    def test_stats_tab_follows_the_picker(self, tmp_path):
        expr = """(() => {
            const out = [];
            for (const keys of [[], ['hashtag:prio'], ['topic:ec2', 'hashtag:master-next']]) {
                setTagSelection(keys);
                stRecs = stBuildRecords();
                out.push([stRecs.map(r => r.id).sort(), focusOnly() ? stSummary().patches : null]);
            }
            return JSON.stringify(out);
        })()"""
        assert json.loads(_eval(self._payload(), tmp_path, expr)) == [
            [[101, 102, 103, 104], None], [[101, 103], 2], [[102, 104], 2]]

    def test_a_tag_is_escaped(self, tmp_path):
        expr = ("JSON.stringify([document.getElementById('stats').innerHTML,"
                " document.getElementById('tags-label').innerHTML,"
                " document.getElementById('tag-menu').innerHTML,"
                " (showNodeInfo(102), document.getElementById('info').innerHTML)])")
        out = "".join(json.loads(_eval(self._payload(asked=("<b>x</b>",), on=True, tag="<b>x</b>"),
                                       tmp_path, expr)))
        assert "<b>x</b>" not in out and "#&lt;b&gt;x&lt;/b&gt;" in out


class TestCodeReviewVeto:
    """Any negative Code-Review vote is a veto, the owner's included;
    otherwise Jenkins and Maloo +1 and two reviewer +1s (one for a
    backport) is ready, the owner's own +1 not counted."""

    CI = [{"name": "Jenkins", "value": 1}, {"name": "Maloo", "value": 1}]

    @staticmethod
    def _review(votes, ci=None):
        return {"verified_votes": ci or TestCodeReviewVeto.CI, "verified_pass": True,
                "cr_votes": [{"name": n, "value": v} for n, v in votes],
                "cr_veto": any(v < 0 for _, v in votes),
                "cr_approved": any(v == 2 for _, v in votes)}

    def _verdicts(self, tmp_path, cases):
        nodes = [_node(10, "MERGED", submitted="2026-01-01")]
        for i, (votes, backport) in enumerate(cases):
            nodes.append(_node(101 + i, is_backport=backport, review=self._review(votes)))
        payload = _payload(101, nodes, [_edge(10, n["id"]) for n in nodes[1:]])
        expr = """JSON.stringify(G.nodes.filter(n => n.id > 100).map(n =>
            [reviewHealth(n), nodeLabel(n).split('| CR: ')[1], landingInfo(n).text]))"""
        return json.loads(_eval(payload, tmp_path, expr))

    def test_a_minus_one_is_a_veto(self, tmp_path):
        r = [("R1", 1), ("R2", 1), ("R3", 1)]
        cases = [
            (r + [("R4", -1)], False),            # three +1s do not outweigh a -1
            ([("A", -1)], False),                  # the owner's own -1 too
            ([("R1", -2)], False),
            (r[:2], False),                        # two +1s: ready
            ([("R1", 1)], True),                   # a backport needs one
            ([("R1", 1)], False),
            ([("R1", 1), ("A", 1)], False),        # the owner's +1 does not count
            ([("G", 2)], False),                   # a +2 is one person
        ]
        assert self._verdicts(tmp_path, cases) == [
            ["bad_veto", "\u2717 VETO", "\u2717 CR veto"],
            ["bad_veto", "\u2717 VETO", "\u2717 CR veto"],
            ["bad_veto", "\u2717 VETO", "\u2717 CR veto"],
            ["good", "2\u00d7(+1)", "\u2713 Ready"],
            ["good", "1\u00d7(+1)", "\u2713 Ready"],
            ["pending", "1\u00d7(+1)", "Reviews 1 of 2 +1s"],
            ["pending", "2\u00d7(+1)", "Reviews 1 of 2 +1s"],
            ["pending", "\u2713 +2", "Reviews 1 of 2 +1s"],
        ]

    def test_the_panel_says_who_vetoed(self, tmp_path):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(101, review=self._review([("R1", 1), ("R2", 1), ("R3", 1), ("R4", -1)])),
                 _node(102, review=self._review([("R1", 1)]))]
        expr = """JSON.stringify([101, 102].map(id => (showNodeInfo(id), document.getElementById('info').innerHTML)))"""
        vetoed, short = json.loads(_eval(_payload(101, nodes, [_edge(10, 101), _edge(10, 102)]), tmp_path, expr))
        assert "\u2717 CR veto</span>" in vetoed and "\u2717 Vetoed: -1 by R4" in vetoed
        assert "reviewer +1" not in vetoed and "Total" not in vetoed
        assert "1 of 2 reviewer +1s" in short and "Reviews 1 of 2 +1s</span>" in short

    def test_the_chips_and_legend_say_veto(self, tmp_path):
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(101, review=self._review([("R1", 1), ("R4", -1)]))]
        expr = "document.getElementById('stats').innerHTML + document.getElementById('legend').innerHTML"
        out = _eval(_payload(101, nodes, [_edge(10, 101)]), tmp_path, expr)
        assert "CR veto" in out and "CR Veto" in out and "blocked" not in out

    def test_the_panel_shows_earlier_votes(self, tmp_path):
        """Votes on older patch sets, unfolded: each reviewer's latest, a
        retracted one (0) left out; no row without any."""
        history = [{"name": "R1", "value": 1, "ps": 2}, {"name": "R2", "value": -1, "ps": 3},
                   {"name": "R1", "value": 2, "ps": 4}, {"name": "R3", "value": 1, "ps": 1},
                   {"name": "R3", "value": 0, "ps": 2}]
        nodes = [_node(10, "MERGED", submitted="2026-01-01"),
                 _node(101, review={"verified_votes": TestCodeReviewVeto.CI, "verified_pass": True, "cr_history": history}),
                 _node(102, review={"verified_votes": TestCodeReviewVeto.CI, "verified_pass": True})]
        expr = """JSON.stringify([101, 102].map(id => (showNodeInfo(id), document.getElementById('info').innerHTML)))"""
        with_history, without = json.loads(_eval(_payload(101, nodes, [_edge(10, 101), _edge(10, 102)]), tmp_path, expr))
        assert "Earlier votes" in with_history and "<details" not in with_history
        assert "+2</span>" in with_history and "patch set 4" in with_history and "patch set 2" not in with_history
        assert "R2" in with_history and "R3" not in with_history
        assert "Earlier votes" not in without


class TestPickInTheUrl:
    """The ticked hashtags/topics live in the URL's #, so a reload keeps
    them: "tags=" and the keys when they differ from the page's default."""

    _STATE = """JSON.stringify([[...tagSel].sort(), location.hash,
        document.getElementById('stats').innerHTML.match(/\\d+ of \\d+ changes|\\d+ changes/)[0]])"""

    def _open(self, payload, tmp_path, hash_, expr=None):
        page = tmp_path / "graph.html"
        page.write_text(generate_html(payload))
        out = subprocess.run([NODE, str(HARNESS), str(page), "a0m1", "--hash", hash_,
                              "--eval", expr or self._STATE],
                             capture_output=True, text=True, check=True)
        result = json.loads(out.stdout)
        assert result["errors"] == []
        return json.loads(result["eval"])

    def test_a_reload_keeps_the_pick(self, tmp_path):
        anchored = TestFocus()._payload()
        assert self._open(anchored, tmp_path, "") == [[], "", "5 changes"]
        assert self._open(anchored, tmp_path, "#tags=topic:ec2") == [
            ["topic:ec2"], "#tags=topic:ec2", "1 of 5 changes"]
        # opened filtered, "all changes" picked: stays all
        tag_only = TestFocus()._payload(on=True)
        assert self._open(tag_only, tmp_path, "") == [["hashtag:prio"], "", "2 of 5 changes"]
        assert self._open(tag_only, tmp_path, "#tags=") == [[], "#tags=", "5 changes"]

    @staticmethod
    def _with_summary(payload):
        """The Stats tab draws stats.summary in full."""
        from gerrit_cli.graph.summary import series_summary
        for n in payload["nodes"]:
            n.setdefault("closed_at", None)
            n.setdefault("first_review_at", None)
            n.setdefault("last_activity", None)
        payload["stats"]["summary"] = series_summary(payload["nodes"], 1767225600, False)
        return payload

    def test_picking_writes_it_and_the_default_clears_it(self, tmp_path):
        expr = """JSON.stringify([
            (setTagSelection(['hashtag:prio', 'topic:ec2']), location.hash),
            (showTab('stats'), location.hash),
            (setTagSelection([]), location.hash),
            (showTab('graph'), location.hash)])"""
        assert self._open(self._with_summary(TestFocus()._payload()), tmp_path, "", expr) == [
            "#tags=hashtag:prio,topic:ec2", "#stats&tags=hashtag:prio,topic:ec2",
            "#stats", ""]
        tag_only = self._with_summary(TestFocus()._payload(on=True))
        assert self._open(tag_only, tmp_path, "", expr)[2:] == ["#stats&tags=", "#tags="]

    def test_with_the_stats_tab(self, tmp_path):
        expr = "JSON.stringify([isStatsTab(), [...tagSel]])"
        assert self._open(self._with_summary(TestFocus()._payload()), tmp_path,
                          "#stats&tags=topic:ec2", expr) == [True, ["topic:ec2"]]
        assert self._open(self._with_summary(TestFocus()._payload(on=True)), tmp_path,
                          "#stats", expr) == [True, ["hashtag:prio"]]

    def test_a_tag_the_graph_no_longer_has_is_dropped(self, tmp_path):
        state = self._open(TestFocus()._payload(), tmp_path, "#tags=hashtag:gone,hashtag:prio")
        assert state[0] == ["hashtag:prio"]

    def test_odd_characters_round_trip(self, tmp_path):
        payload = TestFocus()._payload(asked=(), tag="a,b&c#d")
        expr = """(() => { setTagSelection(['hashtag:a,b&c#d']); const h = location.hash;
            return JSON.stringify([h, readHash().tags]); })()"""
        h, tags = self._open(payload, tmp_path, "", expr)
        assert h == "#tags=hashtag:a%2Cb%26c%23d" and tags == ["hashtag:a,b&c#d"]


class TestPickedSummary:
    """The page's KPI numbers for a ticked set are summary.py's."""

    DAY = 86400
    AS_OF = 1790000000

    def _nodes(self):
        d, t = self.DAY, self.AS_OF
        specs = [
            # cn, status, tags, opened days ago, closed days ago, review after (s), patchsets
            (201, "MERGED", ["a"], 90, 10, 3600, 5),
            (202, "MERGED", ["a"], 40, 35, 7200, 2),
            (203, "MERGED", ["b"], 20, 5, None, 3),
            (204, "NEW", ["a"], 70, None, 1801, 4),
            (205, "NEW", ["b"], 25, None, 1800, 1),
            (206, "ABANDONED", ["a"], 50, 45, None, 2),
            (207, "NEW", ["a", "b"], 5, None, None, 1),
            (208, "MERGED", ["a"], 31, 29, 2, 7),
            # opened with 204: the oldest open is the first listed
            (211, "NEW", ["a"], 70, None, None, 1),
            # on the 30-day boundary: in the 30 days before, not the last
            (212, "NEW", ["a"], 30, None, None, 1),
        ]
        # #a's median upload -> first review is 2700.5 s: Python rounds
        # it to even
        nodes = []
        for cn, status, tags, opened, closed, review, ps in specs:
            n = _node(cn, status, hashtags=tags, current_patchset=ps)
            n["opened_at"] = t - opened * d
            n["closed_at"] = t - closed * d if closed is not None else None
            n["first_review_at"] = n["opened_at"] + review if review is not None else None
            n["ps_times"] = [n["opened_at"]]
            nodes.append(n)
        nodes.append(_node(209, hashtags=["a"]))  # no timeline: counted nowhere
        nodes.append(_node(210, hashtags=["a"], unrelated_parent=True,
                           opened_at=t - 3 * d))
        return nodes

    @pytest.mark.parametrize("keys", [["hashtag:a"], ["hashtag:b"], ["hashtag:a", "hashtag:b"]])
    def test_same_as_python(self, tmp_path, keys):
        from gerrit_cli.graph.summary import series_summary
        nodes = self._nodes()
        payload = _payload(201, nodes, [])
        payload["review_activity"] = True
        payload["stats"]["summary"] = series_summary(nodes, self.AS_OF, True)
        tags = {k.split(":")[1] for k in keys}
        want = series_summary([n for n in nodes if tags & set(n["hashtags"])], self.AS_OF, True)
        expr = f"JSON.stringify(stSeriesSummary(G.nodes.filter(n => inSelection(n, new Set({json.dumps(keys)})))))"
        got = json.loads(_eval(payload, tmp_path, expr))
        assert got == {k: want[k] for k in got}
        assert set(got) >= {"patches", "last_30d", "prev_30d", "open_30d_ago", "time_to_merge",
                            "time_to_first_review", "patchsets_to_merge", "oldest_open"}
