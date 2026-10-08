"""Tests for the ancestry outside the series: _stack_context (drawn
under stacks that stand on nothing, stacks view only) and
_discover_inflight_ancestors (in-flight changes the series sits on,
shown as unrelated parents)."""

from types import SimpleNamespace
from typing import Any

import pytest

from gerrit_cli.graph.build import (
    BuildContext,
    _assemble_payload,
    _discover_inflight_ancestors,
    _make_edge,
    _stack_context,
)
from gerrit_cli.graph.nodes import _make_node


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


class _Gerrit:
    """/commit, commit: and change: lookups over a table of changes,
    each {sha: (patchset, parent sha)}."""

    def __init__(self, changes: dict[int, tuple[str, dict[str, tuple[int, str]]]],
                 branch: str = "master"):
        self.changes, self.branch = changes, branch
        self.calls: list[str] = []

    def _payload(self, cn: int) -> dict[str, Any]:
        status, revs = self.changes[cn]
        current = max(revs, key=lambda h: revs[h][0])
        return {
            "_number": cn, "status": status, "project": "fs/lustre-release",
            "branch": self.branch, "subject": f"LU-2 change {cn}",
            "owner": {"name": "Owner"}, "current_revision": current,
            "revisions": {h: {"_number": ps, "commit": {"parents": [{"commit": p}]}}
                          for h, (ps, p) in revs.items()},
        }

    def get(self, endpoint: str) -> Any:
        self.calls.append(endpoint)
        if endpoint.endswith("/commit"):
            sha = endpoint.split("/revisions/")[1].split("/")[0]
            for _cn, (_st, revs) in self.changes.items():
                if sha in revs:
                    return {"parents": [{"commit": revs[sha][1], "subject": ""}]}
            return {}
        if "?q=commit:" in endpoint:
            sha = endpoint.split("commit:")[1].split("&")[0]
            return [self._payload(cn) for cn, (_st, revs) in self.changes.items()
                    if sha in revs]
        if "?q=change:" in endpoint:
            q = endpoint.split("?q=")[1].split("&")[0]
            cns = [int(x) for x in q.replace("change:", "").replace("OR", " ").split()]
            return [self._payload(cn) for cn in cns]
        raise AssertionError(endpoint)


def _walk_ctx(nodes, gerrit, revision_parents, commit_to_change_ps):
    return SimpleNamespace(
        nodes={n["id"]: n for n in nodes}, revision_parents=dict(revision_parents),
        commit_to_change_ps=dict(commit_to_change_ps),
        client=SimpleNamespace(rest=gerrit), project="fs/lustre-release",
        branch="master", cross_project_branch=False, base_url="https://gerrit.invalid",
        labels_by_cn={}, comment_count_by_cn={}, log=lambda *a, **k: None,
    )


class TestInflightAncestors:
    """61965: 66481 sits on 69506 (found by commit discovery, without
    parents), which sits on 69505, which no search found."""

    def _series(self, parent_of_69505="m", status_69505="NEW"):
        gerrit = _Gerrit({
            69505: (status_69505, {"a1": (1, "m_old"), "a2": (2, parent_of_69505)}),
            100: ("MERGED", {"m": (5, "m0"), "m_old": (4, "m0")}),
        })
        nodes = [_node(66481, commit="c66481"), _node(69506, commit="c69506")]
        ctx = _walk_ctx(nodes, gerrit,
                        {"c66481": "c69506"},
                        {"c66481": (66481, 1), "c69506": (69506, 1)})
        gerrit.changes[69506] = ("NEW", {"c69506": (1, "a1")})
        return ctx

    def test_the_change_a_series_change_sits_on_brings_its_parent(self):
        ctx = self._series()
        assert _discover_inflight_ancestors(ctx) == 1
        node = ctx.nodes[69505]
        assert node["unrelated_parent"] and node["current_commit"] == "a2"
        assert node["current_patchset"] == 2
        assert ctx.revision_parents["c69506"] == "a1"
        assert ctx.commit_to_change_ps["a1"] == (69505, 1)
        # its own base is merged: the walk stops, the trunk hookup stays
        assert 100 not in ctx.nodes

    def test_walks_on_through_in_flight_changes(self):
        ctx = self._series(parent_of_69505="b1")
        ctx.client.rest.changes[200] = ("NEW", {"b1": (1, "m")})
        assert _discover_inflight_ancestors(ctx) == 2
        assert ctx.nodes[200]["unrelated_parent"]
        assert ctx.revision_parents["a2"] == "b1"

    @pytest.mark.parametrize("status", ["MERGED", "ABANDONED"])
    def test_only_in_flight_parents_join(self, status):
        ctx = self._series(status_69505=status)
        assert _discover_inflight_ancestors(ctx) == 0
        assert 69505 not in ctx.nodes and "c69506" not in ctx.revision_parents

    def test_other_branches_stay_out(self):
        ctx = self._series()
        ctx.client.rest.branch = "b2_15"
        assert _discover_inflight_ancestors(ctx) == 0

    def test_a_parent_already_in_the_graph_gets_its_edge(self):
        ctx = self._series()
        ctx.nodes[69505] = _node(69505, commit="a2", ps=2)
        ctx.commit_to_change_ps.update({"a1": (69505, 1), "a2": (69505, 2)})
        assert _discover_inflight_ancestors(ctx) == 0
        assert ctx.revision_parents["c69506"] == "a1"
        assert "unrelated_parent" not in ctx.nodes[69505]

    def test_a_change_only_an_old_patchset_sat_on_is_left_alone(self):
        """49342's 43170: some change's old patchset sat on it; its
        ancestry is for the stacks view only."""
        ctx = self._series()
        ctx.revision_parents = {"c66481": "elsewhere", "c66481_old": "c69506"}
        ctx.commit_to_change_ps["c66481_old"] = (66481, 0)
        assert _discover_inflight_ancestors(ctx) == 0
        assert ctx.client.rest.calls == []

    def test_unrelated_parents_count_nowhere(self):
        ctx = BuildContext(
            client=SimpleNamespace(rest=_Gerrit({})), change_number=2,
            base_url="https://gerrit.invalid", progress=False,
            fetch_details=False, fetch_comments=False, include_topic=False,
            include_hashtag=False, extra_topics=[], extra_hashtags=[],
            extra_tickets=[],
        )
        for cn, subject in ((1, "LU-7 parent"), (2, "LU-1 series")):
            ctx.nodes[cn] = _make_node(cn, subject, "NEW", 1, "A", ctx.base_url)
            ctx.nodes[cn]["current_commit"] = f"c{cn}"
            ctx.nodes[cn]["created"] = "2026-01-01 00:00:00.000000000"
        ctx.nodes[1]["unrelated_parent"] = True
        ctx.edges.append(_make_edge(1, 1, 1, 2, 1, 1))
        stats = _assemble_payload(ctx)["stats"]
        assert stats["status_counts"] == {"NEW": 1}
        assert stats["node_count"] == 1
        assert stats["tickets"] == ["LU-1"]
        assert stats["summary"]["open"] == 1
        assert stats["unrelated_parent_cns"] == [1]
