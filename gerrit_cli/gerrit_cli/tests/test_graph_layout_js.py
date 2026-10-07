"""Layout tests for graph.js, run headless in node.

Each test builds a small payload, renders the real page template and
runs tests/graph_layout_harness.mjs on it, which stubs the DOM and
vis.js and reports every node position and drawn edge per checkbox
combination ("a<abandoned>m<merged>h<history>", 1 = shown).
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
    return _run(payload, tmp_path, "a0m1h0", "--eval", expr)["eval"]


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
                       "a0m1h0")["a0m1h0"]
        pos = _pos(view)
        assert 50 not in pos
        assert pos[100][1] == pos[10][1] - LEVEL_H
        assert abs(pos[100][0] - pos[10][0]) == NODE_W
        assert (10, 100) in _drawn(view)

    def test_real_parent_owns_node_once_shown(self, tmp_path):
        views = _render(self._with_abandoned_parent(3), tmp_path,
                        "a1m1h0", "a1m1h1")
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
                       "a1m1h0")["a1m1h0"]
        pos = _pos(view)
        assert pos[100][1] == pos[10][1] - LEVEL_H
        assert _drawn(view) >= {(10, 100)}
        assert (50, 100) not in _drawn(view)
