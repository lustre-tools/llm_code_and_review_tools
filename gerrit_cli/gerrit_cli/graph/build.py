"""Main build_graph orchestrator.

Pulls together:
- /related for the anchor's main series
- ALL_REVISIONS + ALL_COMMITS for edge reconstruction (incl. stale)
- commit-parent-based discovery of changes dropped from /related
- topic/hashtag expansion into separate-series trees
- cycle breaking on the final edge set

The returned dict is the shape expected by `render.generate_html`."""

import sys
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..client import GerritCommentsClient
from .edges import _break_cycles, _collect_revisions
from .nodes import _make_node, _update_node_meta
from .review import (
    _empty_review,
    _extract_ci_links,
    _extract_unresolved_comments,
    _parse_labels,
)


def build_graph(
    client: GerritCommentsClient,
    change_number: int,
    base_url: str,
    progress: bool = True,
    fetch_details: bool = True,
    fetch_comments: bool = False,
    include_topic: bool = True,
    include_hashtag: bool = True,
    extra_topics: list[str] | None = None,
    extra_hashtags: list[str] | None = None,
) -> dict[str, Any]:
    """Build the full series graph with stale branch information.

    Args:
        fetch_details: If True, fetch CI links from change messages
            (slower, requires extra API calls). If False, skip message
            fetching for faster graph generation.
        fetch_comments: If True, fetch detailed inline comments per
            change (requires individual API calls, can be slow for
            large series). Implies fetch_details.
        include_topic: If True (default), include series sharing the
            anchor's topic as SEPARATE trees alongside the main one.
        include_hashtag: Same as include_topic but for hashtags.
        extra_topics: Additional topic names to search for and include.
        extra_hashtags: Additional hashtag names to search for and include.

    Returns a dict ready to be embedded as JSON in the HTML template.
    """
    if fetch_comments:
        fetch_details = True
    # 1. Resolve the project for this change so we can build correct
    #    URLs and fetch refs (different repos like fs/lustre-release vs
    #    ex/lustre-release live on the same Gerrit host).
    try:
        anchor_change = client.rest.get(f"/changes/{change_number}")
        project = anchor_change.get("project", "fs/lustre-release")
    except Exception:
        project = "fs/lustre-release"

    # 2. Fetch related changes
    if progress:
        print("Fetching related changes...", end="", file=sys.stderr, flush=True)
    response = client.rest.get(
        f"/changes/{change_number}/revisions/current/related"
    )
    entries = response.get("changes", [])
    if progress:
        print(f" {len(entries)} found.", file=sys.stderr)

    # 2. Parse entries into nodes
    nodes: dict[int, dict[str, Any]] = {}  # change_number -> node
    commit_to_cn: dict[str, int] = {}  # commit_hash -> change_number (from related)
    raw_entries: list[dict[str, Any]] = []

    for entry in entries:
        ci = entry.get("commit", {})
        commit_hash = ci.get("commit", "")
        parents = ci.get("parents", [])
        parent_hash = parents[0].get("commit", "") if parents else ""
        author_info = ci.get("author", {})
        cn = entry.get("_change_number", 0)
        ps = entry.get("_revision_number", 0)
        latest = entry.get("_current_revision_number", 0)
        status = entry.get("status", "UNKNOWN")

        subject = ci.get("subject", "")
        nodes[cn] = _make_node(
            cn, subject, status, latest,
            author_info.get("name", "Unknown"), base_url,
            project=project,
        )
        commit_to_cn[commit_hash] = cn
        raw_entries.append({
            "cn": cn,
            "commit": commit_hash,
            "parent_commit": parent_hash,
            "ps": ps,
            "latest": latest,
        })

    # 3. Fetch ALL_REVISIONS + ALL_COMMITS in batches to build
    #    commit -> (change, patchset) map AND collect parent commits
    #    for every revision (needed to discover stale branches that
    #    are no longer in the /related chain).
    commit_to_change_ps: dict[str, tuple[int, int]] = {}
    revision_parents: dict[str, str] = {}  # commit_hash -> parent_commit_hash
    batch_size = 50
    labels_by_cn: dict[int, dict[str, Any]] = {}
    comment_count_by_cn: dict[int, int] = {}

    def _fetch_revisions_batch(
        cns: list[int], *, collect_parents: bool = False,
    ) -> None:
        """Fetch ALL_REVISIONS for a batch of changes.

        If collect_parents is True, also request ALL_COMMITS and
        record parent commit hashes in revision_parents. Only use
        this for the initial /related set to avoid unbounded discovery.
        """
        opts = "&o=ALL_REVISIONS&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS"
        if collect_parents:
            opts += "&o=ALL_COMMITS"
        # ALL_COMMITS returns much more data per change, use
        # smaller batches to avoid connection errors.
        bs = 10 if collect_parents else batch_size
        batches = [cns[i:i + bs]
                   for i in range(0, len(cns), bs)]
        for batch_idx, batch in enumerate(batches):
            query = " OR ".join(f"change:{cn}" for cn in batch)
            try:
                result = client.rest.get(
                    f"/changes/?q={quote(query, safe=':+ ')}"
                    f"{opts}&n=500"
                )
                for change in result:
                    cn = change.get("_number", 0)
                    _collect_revisions(
                        change, commit_to_change_ps,
                        revision_parents if collect_parents else None,
                    )
                    labels_by_cn[cn] = _parse_labels(
                        change.get("labels", {})
                    )
                    comment_count_by_cn[cn] = change.get(
                        "unresolved_comment_count", 0
                    )
                    if cn in nodes:
                        _update_node_meta(nodes[cn], change)
            except Exception as e:
                if progress:
                    print(f" (batch {batch_idx} error: {e})", end="",
                          file=sys.stderr, flush=True)

    all_cns = sorted(nodes.keys())
    if progress:
        print(f"Fetching revision history ({len(all_cns)} changes)...",
              end="", file=sys.stderr, flush=True)
    _fetch_revisions_batch(all_cns, collect_parents=True)
    if progress:
        print(f" {len(commit_to_change_ps)} commits mapped.",
              file=sys.stderr, flush=True)

    # 3a. Discover changes reachable via old patchset parent commits
    #     that weren't returned by /related. Only uses parents from
    #     the initial /related set (collect_parents=True above) to
    #     avoid unbounded expansion into unrelated history.
    unresolved: set[str] = set()
    for child_hash, parent_hash in revision_parents.items():
        if parent_hash and parent_hash not in commit_to_change_ps:
            unresolved.add(parent_hash)

    if unresolved:
        # Search Gerrit for changes containing these commits
        unresolved_list = sorted(unresolved)
        discovered_cns: set[int] = set()
        search_batches = [
            unresolved_list[i:i + 30]
            for i in range(0, len(unresolved_list), 30)
        ]
        for sb in search_batches:
            query = " OR ".join(f"commit:{h}" for h in sb)
            try:
                result = client.rest.get(
                    f"/changes/?q={quote(query, safe=':+ ')}&n=500"
                )
                for change in result:
                    cn = change.get("_number", 0)
                    if cn and cn not in nodes:
                        discovered_cns.add(cn)
                        nodes[cn] = _make_node(
                            cn, change.get("subject", ""),
                            change.get("status", "UNKNOWN"),
                            change.get("_current_revision_number", 1),
                            change.get("owner", {}).get("name", "Unknown"),
                            base_url,
                            topic=change.get("topic", ""),
                            hashtags=change.get("hashtags", []),
                            updated=change.get("updated", ""),
                            is_wip=bool(change.get("work_in_progress", False)),
                            project=change.get("project", project),
                        )
            except Exception:
                pass

        if discovered_cns:
            if progress:
                print(
                    f"Discovered {len(discovered_cns)} additional changes"
                    " via old patchset parents...",
                    end="", file=sys.stderr, flush=True,
                )
            # Also collect parents for discovered changes so we can
            # find one more level of connections.
            _fetch_revisions_batch(sorted(discovered_cns))
            if progress:
                print(
                    f" {len(commit_to_change_ps)} total commits mapped.",
                    file=sys.stderr, flush=True,
                )

    # 3a-ii. Remove discovered changes that are MERGED — these are
    #        git ancestors (commits on lustre-master the series is
    #        based on), not part of the actual patch series.
    related_set = set(e["cn"] for e in raw_entries)
    merged_discovered = [
        cn for cn in nodes
        if cn not in related_set and nodes[cn]["status"] == "MERGED"
    ]
    for cn in merged_discovered:
        del nodes[cn]
    if merged_discovered and progress:
        print(
            f"  (filtered {len(merged_discovered)} merged ancestors)",
            file=sys.stderr,
        )

    # 3b. Attach review info to nodes (with comment count from batch query)
    for cn, node in nodes.items():
        review = labels_by_cn.get(cn, _empty_review())
        review["unresolved_count"] = comment_count_by_cn.get(cn, 0)
        node["review"] = review

    # 3c. Fetch details (CI links + comments) for non-abandoned changes
    active_cns = sorted(
        cn for cn, node in nodes.items() if node["status"] != "ABANDONED"
    )
    if fetch_details and active_cns:
        if progress:
            print(
                f"Fetching details ({len(active_cns)} active changes)...",
                end="", file=sys.stderr, flush=True,
            )

        # 3c-i. Batch-fetch messages for CI links
        msg_batches = [
            active_cns[i:i + 20] for i in range(0, len(active_cns), 20)
        ]
        for batch in msg_batches:
            query = " OR ".join(f"change:{cn}" for cn in batch)
            try:
                result = client.rest.get(
                    f"/changes/?q={quote(query, safe=':+ ')}&o=MESSAGES&n=500"
                )
                for change in result:
                    cn = change.get("_number", 0)
                    if cn not in nodes:
                        continue
                    latest_ps = nodes[cn]["current_patchset"]
                    links = _extract_ci_links(
                        change.get("messages", []), latest_ps
                    )
                    nodes[cn]["review"]["jenkins_url"] = links.get(
                        "jenkins_url", ""
                    )
                    nodes[cn]["review"]["maloo_url"] = links.get(
                        "maloo_url", ""
                    )
            except Exception:
                pass

        # 3c-ii. Fetch comments per change (opt-in, slow)
        # Fetch detailed comments per change — uses confidence-ranked
        # thread analysis capped at unresolved_comment_count.
        if fetch_comments:
            if progress:
                print(
                    f"\nFetching comments ({len(active_cns)} changes)...",
                    end="", file=sys.stderr, flush=True,
                )
            for cn in active_cns:
                try:
                    expected = nodes[cn]["review"].get("unresolved_count", -1)
                    nodes[cn]["review"]["unresolved_comments"] = (
                        _extract_unresolved_comments(client, cn, expected)
                    )
                except Exception:
                    pass

        if progress:
            print(" done.", file=sys.stderr)

    # 4. Build edges.
    #    First from /related raw_entries (guaranteed chain edges),
    #    then from revision_parents (stale branches from old patchsets).
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[int, int]] = set()

    def _add_edge(parent_cn: int, child_cn: int, parent_ps: int) -> None:
        if parent_cn == child_cn:
            return
        if parent_cn not in nodes or child_cn not in nodes:
            return
        edge_key = (parent_cn, child_cn)
        if edge_key in seen_edges:
            return
        seen_edges.add(edge_key)
        parent_latest = nodes[parent_cn]["current_patchset"]
        edges.append({
            "from": parent_cn,
            "to": child_cn,
            "parent_patchset": parent_ps,
            "parent_latest": parent_latest,
            "is_stale": parent_ps < parent_latest,
        })

    # 4a. Edges from /related entries (core chain)
    for entry in raw_entries:
        parent_commit = entry["parent_commit"]
        if not parent_commit or parent_commit not in commit_to_change_ps:
            continue
        parent_cn, parent_ps = commit_to_change_ps[parent_commit]
        _add_edge(parent_cn, entry["cn"], parent_ps)

    # 4b. Edges from revision parents — only where at least one end is
    #     a discovered change (not in the original /related set). This
    #     connects discovered nodes to the graph without adding cross-
    #     connections between /related changes from old patchset history.
    related_cns = set(e["cn"] for e in raw_entries)
    for child_hash, parent_hash in revision_parents.items():
        if not parent_hash:
            continue
        if child_hash not in commit_to_change_ps:
            continue
        if parent_hash not in commit_to_change_ps:
            continue
        child_cn, _child_ps = commit_to_change_ps[child_hash]
        parent_cn, parent_ps = commit_to_change_ps[parent_hash]
        # Skip if both ends are from /related (already connected in 4a)
        if child_cn in related_cns and parent_cn in related_cns:
            continue
        _add_edge(parent_cn, child_cn, parent_ps)

    # 4c. Remove edges that form cycles (from old patchset dependencies).
    removed_cycle_edges = _break_cycles(edges)
    if removed_cycle_edges and progress:
        print(
            f"  (removed {removed_cycle_edges} edges to break cycles)",
            file=sys.stderr,
        )

    # Tag all main series nodes with group 0
    for n in nodes.values():
        n["series_group"] = 0

    # 5. Separate series from topic/hashtag search (opt-in).
    #    For each matching change not already in the main series,
    #    fetch its own /related chain and build a SEPARATE series
    #    (no edges crossing into the main graph).
    separate_groups: list[dict[str, Any]] = []  # [{label, node_ids}]

    def _build_separate_series(
        seed_cns: list[int], label: str,
    ) -> None:
        main_cns = set(nodes.keys())
        # Track which seeds are already placed in some group
        placed: set[int] = set()
        # Also, seeds that are already in the main series should be skipped
        seeds_new = [cn for cn in seed_cns if cn not in main_cns]
        if not seeds_new:
            return

        for seed in seeds_new:
            if seed in placed:
                continue
            # Fetch /related for this seed
            try:
                resp = client.rest.get(
                    f"/changes/{seed}/revisions/current/related"
                )
                rel_entries = resp.get("changes", [])
            except Exception:
                rel_entries = []

            # Parse entries into nodes for this group
            group_nodes: dict[int, dict[str, Any]] = {}
            group_raw: list[dict[str, Any]] = []
            for entry in rel_entries:
                ci = entry.get("commit", {})
                commit_hash = ci.get("commit", "")
                parents = ci.get("parents", [])
                parent_hash = (
                    parents[0].get("commit", "") if parents else ""
                )
                cn = entry.get("_change_number", 0)
                if not cn or cn in main_cns:
                    # Skip entries that are in the main series
                    continue
                latest = entry.get("_current_revision_number", 0) or 1
                status = entry.get("status", "UNKNOWN")
                subject = ci.get("subject", "")
                author = ci.get("author", {}).get("name", "Unknown")
                group_nodes[cn] = _make_node(
                    cn, subject, status, latest, author, base_url,
                    project=project,
                )
                group_raw.append({
                    "cn": cn,
                    "parent_commit": parent_hash,
                    "commit": commit_hash,
                })

            if not group_nodes:
                # Seed had no related or all were in main — make a
                # single-node group for just this seed
                if seed not in nodes:
                    try:
                        single = client.rest.get(
                            f"/changes/?q=change:{seed}"
                            "&o=CURRENT_REVISION&o=CURRENT_COMMIT"
                        )
                    except Exception:
                        continue
                    if not single:
                        continue
                    ch = single[0]
                    latest = ch.get("_current_revision_number", 1)
                    subject = ch.get("subject", "")
                    status = ch.get("status", "UNKNOWN")
                    author = ch.get("owner", {}).get("name", "Unknown")
                    group_nodes[seed] = _make_node(
                        seed, subject, status, latest, author, base_url,
                        topic=ch.get("topic", ""),
                        hashtags=ch.get("hashtags", []),
                        updated=ch.get("updated", ""),
                        is_wip=bool(ch.get("work_in_progress", False)),
                        project=ch.get("project", project),
                    )
                else:
                    continue

            # Fetch revisions + commits for all group nodes (to build
            # commit_to_change_ps and collect parent commits for
            # cross-group stale edge detection)
            group_ctps: dict[str, tuple[int, int]] = {}
            group_rev_parents: dict[str, str] = {}
            try:
                q = " OR ".join(
                    f"change:{c}" for c in group_nodes
                )
                result = client.rest.get(
                    f"/changes/?q={quote(q, safe=':+ ')}"
                    "&o=ALL_REVISIONS&o=ALL_COMMITS"
                    "&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS&n=500"
                )
                for change in result:
                    cn = change.get("_number", 0)
                    _collect_revisions(
                        change, group_ctps, group_rev_parents,
                    )
                    if cn in group_nodes:
                        _update_node_meta(group_nodes[cn], change)
                        lbl = _parse_labels(change.get("labels", {}))
                        lbl["unresolved_count"] = change.get(
                            "unresolved_comment_count", 0
                        )
                        group_nodes[cn]["review"] = lbl
            except Exception:
                pass

            # Build edges for this group from raw_entries
            group_edges: list[dict[str, Any]] = []
            group_seen: set[tuple[int, int]] = set()
            for entry in group_raw:
                pc = entry["parent_commit"]
                child_cn = entry["cn"]
                if not pc or pc not in group_ctps:
                    continue
                parent_cn, parent_ps = group_ctps[pc]
                if parent_cn not in group_nodes:
                    continue
                if parent_cn == child_cn:
                    continue
                key = (parent_cn, child_cn)
                if key in group_seen:
                    continue
                group_seen.add(key)
                parent_latest = group_nodes[parent_cn][
                    "current_patchset"
                ]
                group_edges.append({
                    "from": parent_cn,
                    "to": child_cn,
                    "parent_patchset": parent_ps,
                    "parent_latest": parent_latest,
                    "is_stale": parent_ps < parent_latest,
                })

            # Build cross-group stale edges: for each group node's
            # revisions, if the parent commit resolves to a main-
            # series node, add a stale edge main → group_node.
            # This hooks separate series back into the main tree.
            group_commit_set = set(group_ctps.keys())
            for child_hash, parent_hash in group_rev_parents.items():
                if not parent_hash:
                    continue
                if child_hash not in group_ctps:
                    continue
                child_cn, _ = group_ctps[child_hash]
                if child_cn not in group_nodes:
                    continue
                # Is parent in main series?
                if parent_hash not in commit_to_change_ps:
                    continue
                parent_cn, parent_ps = commit_to_change_ps[parent_hash]
                if parent_cn not in nodes:
                    continue
                if nodes[parent_cn].get("series_group", 0) != 0:
                    continue  # parent must be in main series
                key = (parent_cn, child_cn)
                if key in group_seen:
                    continue
                group_seen.add(key)
                parent_latest = nodes[parent_cn]["current_patchset"]
                group_edges.append({
                    "from": parent_cn,
                    "to": child_cn,
                    "parent_patchset": parent_ps,
                    "parent_latest": parent_latest,
                    "is_stale": parent_ps < parent_latest,
                })

            # Assign group id and add to main collections
            group_id = len(separate_groups) + 1
            group_label = f"{label}: {min(group_nodes.keys())}"
            for cn, node in group_nodes.items():
                node["series_group"] = group_id
                node["review"] = node.get("review") or _empty_review()
                nodes[cn] = node
                placed.add(cn)
            edges.extend(group_edges)
            separate_groups.append({
                "id": group_id,
                "label": group_label,
                "node_ids": sorted(group_nodes.keys()),
            })

    search_labels: list[tuple[str, str]] = []
    anchor_topic = nodes.get(change_number, {}).get("topic", "")
    anchor_hashtags = nodes.get(change_number, {}).get("hashtags", []) or []
    topics_to_search: list[str] = []
    if include_topic and anchor_topic:
        topics_to_search.append(anchor_topic)
    topics_to_search.extend(extra_topics or [])
    hashtags_to_search: list[str] = []
    if include_hashtag:
        hashtags_to_search.extend(anchor_hashtags)
    hashtags_to_search.extend(extra_hashtags or [])
    # Dedup while preserving order
    seen_t: set[str] = set()
    for t in topics_to_search:
        if t and t not in seen_t:
            seen_t.add(t)
            search_labels.append((f"topic:{t}", f"topic {t}"))
    seen_h: set[str] = set()
    for h in hashtags_to_search:
        if h and h not in seen_h:
            seen_h.add(h)
            search_labels.append((f"hashtag:{h}", f"hashtag {h}"))

    for query, label in search_labels:
        try:
            result = client.rest.get(
                f"/changes/?q={quote(query, safe=':+ ')}&n=500"
            )
            seed_cns = [
                ch.get("_number", 0) for ch in result
                if ch.get("_number")
            ]
        except Exception:
            seed_cns = []
        if progress and seed_cns:
            n_new = sum(1 for c in seed_cns if c not in nodes)
            print(
                f"Searching {label}: {len(seed_cns)} matches"
                f" ({n_new} outside main series)...",
                file=sys.stderr,
            )
        _build_separate_series(seed_cns, label)

    if separate_groups and progress:
        total = sum(len(g["node_ids"]) for g in separate_groups)
        print(
            f"Built {len(separate_groups)} separate series"
            f" ({total} nodes total).",
            file=sys.stderr,
        )

    # 6. Stats
    status_counts: dict[str, int] = {}
    for n in nodes.values():
        s = n["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    stale_edges = sum(1 for e in edges if e["is_stale"])
    tickets = sorted(set(n["ticket"] for n in nodes.values() if n["ticket"]))
    from datetime import datetime
    generated_at = datetime.now().astimezone().strftime(
        "%Y-%m-%d %I:%M:%S %p %Z"
    )

    return {
        "anchor": change_number,
        "base_url": base_url,
        "nodes": list(nodes.values()),
        "edges": edges,
        "separate_groups": separate_groups,
        "generated_at": generated_at,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "status_counts": status_counts,
            "stale_edge_count": stale_edges,
            "tickets": tickets,
            "separate_group_count": len(separate_groups),
            "generated_at": generated_at,
        },
    }
