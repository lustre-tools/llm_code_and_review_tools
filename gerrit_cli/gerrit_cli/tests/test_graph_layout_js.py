"""Layout tests for graph.js, run headless in node.

Each test builds a small payload, renders the real page template and
runs tests/graph_layout_harness.mjs on it, which stubs the DOM and
vis.js and reports every node position and drawn edge per checkbox
combination ("a<abandoned>m<layout>": a1 = abandoned shown, m1 =
Trunk, m0 = Stacks).
"""

import json
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

