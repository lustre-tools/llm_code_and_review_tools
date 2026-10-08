"""Tests for _stack_context: the ancestry the stacks view draws under
stacks that stand on nothing, down to a merged change."""

from types import SimpleNamespace
from typing import Any

from gerrit_cli.graph.build import _make_edge, _stack_context


def _node(cn: int, status: str = "NEW", commit: str = "", ps: int = 1) -> dict[str, Any]:
    return {"id": cn, "status": status, "current_commit": commit or f"c{cn}",
            "current_patchset": ps}


def _change(cn: int, status: str, commits: dict[str, int],
            subject: str = "") -> dict[str, Any]:
    return {
        "_number": cn, "status": status, "project": "fs/lustre-release",
        "branch": "master", "subject": subject or f"LU-1 change {cn}",
        "owner": {"name": "Owner"},
        "revisions": {sha: {"_number": ps} for sha, ps in commits.items()},
        "current_revision": max(commits, key=commits.get),
    }


class _Rest:
    """Answers /commit and commit: lookups from tables; counts calls."""

    def __init__(self, parents: dict[str, tuple[str, str]],
                 owners: dict[str, dict[str, Any]], fail: bool = False):
        self.parents, self.owners, self.fail = parents, owners, fail
        self.calls: list[str] = []

    def get(self, endpoint: str) -> Any:
        self.calls.append(endpoint)
        if self.fail:
            raise RuntimeError("gerrit down")
        if endpoint.endswith("/commit"):
            sha = endpoint.split("/revisions/")[1].split("/")[0]
            parent = self.parents.get(sha)
            return {"parents": [{"commit": parent[0], "subject": parent[1]}]} if parent else {}
        if "?q=commit:" in endpoint:
            sha = endpoint.split("commit:")[1].split("&")[0]
            return [self.owners[sha]] if sha in self.owners else []
        raise AssertionError(endpoint)


def _ctx(nodes, edges=(), parents=None, owners=None, fail=False,
         revision_parents=None, commit_to_change_ps=None):
    return SimpleNamespace(
        nodes={n["id"]: n for n in nodes}, edges=list(edges),
        revision_parents=dict(revision_parents or {}),
        commit_to_change_ps=dict(commit_to_change_ps or {}),
        client=SimpleNamespace(rest=_Rest(parents or {}, owners or {}, fail)),
        project="fs/lustre-release", base_url="https://gerrit.invalid",
        log=lambda *a, **k: None,
    )


def _pairs(edges):
    return [(e["from"], e["to"]) for e in edges]


class TestStackContext:
    def test_walks_down_to_a_merged_change_outside_the_graph(self):
        ctx = _ctx(
            [_node(100, ps=3)],
            parents={"c100": ("p1", "x"), "p1": ("p2", "y")},
            owners={"p1": _change(50, "NEW", {"p1": 2, "p1b": 4}),
                    "p2": _change(40, "MERGED", {"p2": 7})},
        )
        nodes, edges = _stack_context(ctx)
        by_id = {n["id"]: n for n in nodes}
        assert set(by_id) == {50, 40}
        assert all(n["context"] and n["context_of"] == [100] for n in nodes)
        assert by_id[40]["status"] == "MERGED"
        assert _pairs(edges) == [(50, 100), (40, 50)]
        # 100's ps3 sits on ps2 of 50, which is at ps4 now
        assert (edges[0]["parent_patchset"], edges[0]["parent_latest"],
                edges[0]["child_patchset"]) == (2, 4, 3)
        assert 100 not in by_id

    def test_stops_at_a_change_already_in_the_graph(self):
        ctx = _ctx(
            [_node(100), _node(60, "MERGED", commit="m60")],
            revision_parents={"c100": "m60"},
            commit_to_change_ps={"m60": (60, 5)},
        )
        nodes, edges = _stack_context(ctx)
        assert nodes == []
        assert _pairs(edges) == [(60, 100)]
        assert ctx.client.rest.calls == []

    def test_master_commit_without_a_change(self):
        ctx = _ctx(
            [_node(100), _node(101)],
            parents={"c100": ("rel", "New tag 2.16.59"),
                     "c101": ("rel", "New tag 2.16.59")},
        )
        nodes, edges = _stack_context(ctx)
        assert len(nodes) == 1
        (m,) = nodes
        assert m["id"] < 0 and m["master_commit"] and m["status"] == "MERGED"
        assert m["subject"] == "New tag 2.16.59"
        assert m["context_of"] == [100, 101]
        assert sorted(_pairs(edges)) == [(m["id"], 100), (m["id"], 101)]

    def test_connected_and_hooked_nodes_are_not_walked(self):
        ctx = _ctx(
            [_node(10, "MERGED"), _node(90), _node(100), _node(200)],
            edges=[_make_edge(90, 1, 1, 100, 1, 1),
                   dict(_make_edge(10, 1, 1, 200, 1, 1), inferred=True)],
            parents={"c90": ("p", "s")},
            owners={"p": _change(40, "MERGED", {"p": 1})},
        )
        nodes, edges = _stack_context(ctx)
        assert _pairs(edges) == [(40, 90)]
        assert [n["id"] for n in nodes] == [40]

    def test_long_ancestry_is_cut(self):
        n = 25
        parents = {"c100": ("s0", "")}
        owners = {}
        for i in range(n):
            parents[f"s{i}"] = (f"s{i + 1}", "")
            owners[f"s{i}"] = _change(1000 + i, "NEW", {f"s{i}": 1})
        ctx = _ctx([_node(100)], parents=parents, owners=owners)
        nodes, edges = _stack_context(ctx)
        assert len(nodes) == 20
        assert [x for x in nodes if x.get("context_cut")] == [nodes[-1]]

    def test_gerrit_errors_leave_the_graph_alone(self):
        ctx = _ctx([_node(100)], fail=True)
        assert _stack_context(ctx) == ([], [])
