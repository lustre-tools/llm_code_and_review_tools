"""Edge-related helpers: revision-to-commit mapping and cycle breaking.

The commit->(change, patchset) map is what lets us turn a raw
first-parent commit hash from any historical revision into an edge in
the graph. Cycle-breaking cleans up the rare case where old-patchset
dependencies create A→B→A loops."""

from typing import Any


def _collect_revisions(
    change: dict[str, Any],
    ctps_out: dict[str, tuple[int, int]],
    rev_parents_out: dict[str, str] | None = None,
) -> None:
    """Walk a change's revisions, populating commit->(cn,ps) and optionally
    commit->parent_commit maps."""
    cn = change.get("_number", 0)
    for rev_hash, rev_info in change.get("revisions", {}).items():
        ps = rev_info.get("_number", 0)
        ctps_out[rev_hash] = (cn, ps)
        if rev_parents_out is not None:
            ci = rev_info.get("commit", {})
            parents = ci.get("parents", [])
            if parents:
                rev_parents_out[rev_hash] = parents[0].get("commit", "")


def _break_cycles(edges: list[dict[str, Any]]) -> int:
    """Remove edges participating in cycles. Returns count removed.

    Old patchset dependencies can create circular references (A depended
    on B in ps3, B depended on A in ps5). Uses Kahn's algorithm to find
    nodes not in any cycle; edges between remaining (cycle) nodes are
    removed, preferring stale edges.
    """
    removed = 0
    for _ in range(50):
        adj: dict[int, set[int]] = {}
        for e in edges:
            adj.setdefault(e["from"], set()).add(e["to"])
            adj.setdefault(e["to"], set())

        in_degree: dict[int, int] = {n: 0 for n in adj}
        for u in adj:
            for v in adj[u]:
                in_degree[v] = in_degree.get(v, 0) + 1

        queue = [n for n, d in in_degree.items() if d == 0]
        visited: set[int] = set()
        while queue:
            n = queue.pop()
            visited.add(n)
            for v in adj.get(n, ()):
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

        cycle_nodes = set(adj.keys()) - visited
        if not cycle_nodes:
            break

        removed_one = False
        for i, e in enumerate(edges):
            if (e["from"] in cycle_nodes and e["to"] in cycle_nodes
                    and e["is_stale"]):
                edges.pop(i)
                removed += 1
                removed_one = True
                break
        if not removed_one:
            for i, e in enumerate(edges):
                if e["from"] in cycle_nodes and e["to"] in cycle_nodes:
                    edges.pop(i)
                    removed += 1
                    removed_one = True
                    break
        if not removed_one:
            break
    return removed
