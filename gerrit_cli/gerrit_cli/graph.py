"""Gerrit patch series DAG visualizer.

Builds a full DAG of all related changes for a Gerrit patch, resolving
stale patchset dependencies to show which patches need rebasing. Generates
an interactive HTML visualization with:

- Vertical tree layout growing upward from the anchor change
- Edge labels showing which patchset each dependency goes through
- Stale edges highlighted (child depends on old patchset of parent)
- Click-to-re-anchor: click any node to make it the new starting point
- Filter controls for abandoned/stale changes

The key insight: Gerrit's /related endpoint shows one patchset per change
in the commit chain. When a change is rebased, its old patchset's children
become "orphans" — their parent commit no longer matches anything in the
current chain. By fetching ALL_REVISIONS for each change, we can reconnect
these orphans to the correct parent change at the correct (stale) patchset.
"""

import json
import os
import re
import sys
import tempfile
from typing import Any
from urllib.parse import quote

from .client import GerritCommentsClient


def _empty_review() -> dict[str, Any]:
    """Return an empty review info structure."""
    return {
        "verified_votes": [],    # [{name, value}] — all non-zero Verified votes
        "verified_pass": False,  # at least one +1, no -1s
        "verified_fail": False,  # any -1
        "cr_votes": [],          # [{name, value}] — all non-zero Code-Review votes
        "cr_approved": False,    # has +2
        "cr_rejected": False,    # has -2
        "cr_rejected_by": "",
        "cr_veto": False,        # any CR vote <= -1
        "jenkins_url": "",       # link to Jenkins build
        "maloo_url": "",         # link to Maloo test results
        "unresolved_count": 0,   # number of unresolved inline comments
        "unresolved_comments": [],  # [{file, line, author, message, patch_set}]
    }


def _extract_ci_links(
    messages: list[dict[str, Any]], patchset: int
) -> dict[str, str]:
    """Extract Jenkins build URL and Maloo results URL from change messages.

    Only looks at messages for the given patchset number.
    """
    jenkins_url = ""
    maloo_url = ""

    for msg in messages:
        if msg.get("_revision_number", 0) != patchset:
            continue
        text = msg.get("message", "")

        # Jenkins: look for build.whamcloud.com URL
        if not jenkins_url:
            m = re.search(
                r"(https?://build\.whamcloud\.com/job/[^/]+/\d+/?)", text
            )
            if m:
                jenkins_url = m.group(1)

        # Maloo: look for "sessions will be run for Build NNNNN"
        # to construct the results overview link
        if not maloo_url:
            m = re.search(
                r"sessions will be run for Build (\d+)", text
            )
            if m:
                build_num = m.group(1)
                maloo_url = (
                    f"https://testing.whamcloud.com/test_sessions/related"
                    f"?jobs=lustre-reviews&builds={build_num}#redirect"
                )

    return {"jenkins_url": jenkins_url, "maloo_url": maloo_url}


def _extract_unresolved_comments(
    client: Any,
    cn: int,
    expected_count: int = -1,
) -> list[dict[str, Any]]:
    """Extract unresolved comments using multi-source heuristics.

    Gerrit's unresolved_comment_count is authoritative but opaque — its
    resolution logic (code-change-based, porting) isn't fully exposed via
    any single API, and the per-comment `unresolved` field is unreliable
    (especially for PATCHSET_LEVEL comments posted with votes).

    Strategy:
    1. Raw thread analysis: threads where last comment has unresolved=True
    2. Subtract threads that ported_comments confirms as resolved
    3. If still short of expected_count, supplement with recent human
       comments on the current patchset (Gerrit may track these as
       unresolved despite the API field saying False)

    Results are capped at expected_count (from unresolved_comment_count).
    """
    try:
        raw = client.rest.get(f"/changes/{cn}/comments")
    except Exception:
        return []

    # Get current patchset number
    current_ps = 0
    try:
        detail = client.rest.get(f"/changes/{cn}?o=CURRENT_REVISION")
        for rev_info in detail.get("revisions", {}).values():
            current_ps = rev_info.get("_number", 0)
    except Exception:
        pass

    # Flatten all comments with file path
    all_comments: list[dict[str, Any]] = []
    for filepath, file_comments in raw.items():
        for c in file_comments:
            c["_file"] = filepath
            all_comments.append(c)

    by_id = {c.get("id", ""): c for c in all_comments}

    # Build threads: group by root comment
    threads: dict[str, list[dict[str, Any]]] = {}
    for c in all_comments:
        root = c
        visited: set[str] = set()
        while root.get("in_reply_to") and root["in_reply_to"] in by_id:
            if root["in_reply_to"] in visited:
                break
            visited.add(root.get("id", ""))
            root = by_id[root["in_reply_to"]]
        threads.setdefault(root.get("id", ""), []).append(c)

    bot_names = {"wc-checkpatch", "Lustre Gerrit Janitor", "jenkins",
                 "Maloo", "Autotest",
                 "Misc Code Checks Robot (Gatekeeper helper)"}

    def _make_item(root: dict[str, Any]) -> dict[str, Any]:
        return {
            "file": root.get("_file", ""),
            "line": root.get("line", 0),
            "author": root.get("author", {}).get("name", "?"),
            "message": root.get("message", "")[:200],
            "patch_set": root.get("patch_set", 0),
            "id": root.get("id", ""),
        }

    # Primary: raw thread analysis — threads where last comment has
    # unresolved=True. Ranked: current-patchset first, then older.
    primary: list[tuple[int, dict[str, Any]]] = []
    seen_root_ids: set[str] = set()

    for root_id, thread_comments in threads.items():
        thread_comments.sort(key=lambda x: x.get("updated", ""))
        last = thread_comments[-1]
        if not last.get("unresolved", False):
            continue

        root = by_id.get(root_id, thread_comments[0])
        seen_root_ids.add(root_id)
        max_ps = max(c.get("patch_set", 0) for c in thread_comments)
        rank = 0 if max_ps == current_ps else 1
        primary.append((rank, _make_item(root)))

    primary.sort(key=lambda x: (x[0], x[1]["file"], x[1]["line"]))
    items = [p[1] for p in primary]

    # When raw analysis finds MORE candidates than expected_count,
    # use ported_comments to identify which old-patchset threads
    # Gerrit considers resolved (via code changes). Remove those
    # to get closer to the true set. Only applied when we have
    # excess — when raw matches or undershoots expected_count,
    # ported is too unreliable (it sometimes resolves threads
    # that Gerrit still counts as unresolved).
    if expected_count >= 0 and len(items) > expected_count:
        ported_resolved_ids: set[str] = set()
        try:
            ported = client.rest.get(
                f"/changes/{cn}/revisions/current/ported_comments"
            )
            ported_flat: list[dict[str, Any]] = []
            for filepath, file_comments in ported.items():
                for c in file_comments:
                    c["_file"] = filepath
                    ported_flat.append(c)

            ported_by_id = {c.get("id", ""): c for c in ported_flat}
            ported_threads: dict[str, list[dict[str, Any]]] = {}
            for c in ported_flat:
                root = c
                visited: set[str] = set()
                while (root.get("in_reply_to")
                       and root["in_reply_to"] in ported_by_id):
                    if root["in_reply_to"] in visited:
                        break
                    visited.add(root.get("id", ""))
                    root = ported_by_id[root["in_reply_to"]]
                ported_threads.setdefault(
                    root.get("id", ""), []
                ).append(c)

            for root_id, thread in ported_threads.items():
                thread.sort(key=lambda x: x.get("updated", ""))
                if not thread[-1].get("unresolved", False):
                    ported_resolved_ids.add(root_id)
        except Exception:
            pass

        if ported_resolved_ids:
            items = [it for it in items
                     if it["id"] not in ported_resolved_ids]

    # Fallback: when raw analysis (after optional ported filtering)
    # finds ZERO candidates but expected_count > 0, supplement with
    # recent current-patchset human comments. Handles a Gerrit API
    # bug where PATCHSET_LEVEL comments posted with votes have
    # unresolved=False in the API but are counted as unresolved.
    if expected_count > 0 and len(items) == 0:
        for root_id, thread_comments in threads.items():
            if root_id in seen_root_ids:
                continue
            root = by_id.get(root_id, thread_comments[0])
            if root.get("patch_set", 0) != current_ps:
                continue
            author = root.get("author", {}).get("name", "")
            if author in bot_names:
                continue
            items.append(_make_item(root))
        items.sort(key=lambda x: x.get("id", ""), reverse=True)

    # Cap at expected_count if provided
    if expected_count >= 0:
        items = items[:expected_count]

    return items


def _parse_labels(labels: dict[str, Any]) -> dict[str, Any]:
    """Parse Gerrit DETAILED_LABELS into compact review info."""
    result = _empty_review()

    # Verified label — track ALL voters, not just Jenkins/Maloo
    verified = labels.get("Verified", {})
    has_plus = False
    has_minus = False
    for vote in verified.get("all", []):
        val = vote.get("value", 0)
        if val == 0:
            continue
        name = vote.get("name", f"account:{vote.get('_account_id', '?')}")
        result["verified_votes"].append({"name": name, "value": val})
        if val > 0:
            has_plus = True
        if val < 0:
            has_minus = True

    result["verified_pass"] = has_plus and not has_minus
    result["verified_fail"] = has_minus

    # Code-Review label
    cr = labels.get("Code-Review", {})
    for vote in cr.get("all", []):
        val = vote.get("value", 0)
        if val == 0:
            continue
        name = vote.get("name", f"account:{vote.get('_account_id', '?')}")
        result["cr_votes"].append({"name": name, "value": val})
        if val <= -1:
            result["cr_veto"] = True

    if cr.get("approved"):
        result["cr_approved"] = True
    if cr.get("rejected"):
        result["cr_rejected"] = True
        result["cr_rejected_by"] = cr["rejected"].get("name", "")

    # Sort CR votes: negative first (most concerning), then positive
    result["cr_votes"].sort(key=lambda v: (v["value"] > 0, abs(v["value"])))

    return result


def _make_node(
    cn: int, subject: str, status: str, latest: int,
    author: str, base_url: str, ticket: str = "",
    topic: str = "", hashtags: list[str] | None = None,
    updated: str = "", is_wip: bool = False,
    project: str = "fs/lustre-release",
) -> dict[str, Any]:
    """Create a node dict for the graph."""
    if not ticket:
        m = re.match(r"(LU-\d+)", subject)
        ticket = m.group(1) if m else ""
    ref = f"refs/changes/{cn % 100:02d}/{cn}/{latest}"
    fetch_cmd = f"git fetch {base_url}/{project} {ref}"
    return {
        "id": cn,
        "subject": subject,
        "status": status,
        "current_patchset": latest,
        "author": author,
        "url": f"{base_url}/c/{project}/+/{cn}",
        "ticket": ticket,
        "topic": topic,
        "hashtags": hashtags or [],
        "checkout_cmd": f"{fetch_cmd} && git checkout FETCH_HEAD",
        "cherrypick_cmd": f"{fetch_cmd} && git cherry-pick FETCH_HEAD",
        "updated": updated,
        "is_wip": is_wip,
    }


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


def _update_node_meta(node: dict[str, Any], change: dict[str, Any]) -> None:
    """Copy topic/hashtags/updated/wip-flag from a change payload onto a node."""
    node["topic"] = change.get("topic", "")
    node["hashtags"] = change.get("hashtags", [])
    node["updated"] = change.get("updated", "")
    node["is_wip"] = bool(change.get("work_in_progress", False))


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


def generate_html(graph_data: dict[str, Any]) -> str:
    """Generate a self-contained interactive HTML visualization."""
    data_json = json.dumps(graph_data)
    return _HTML_TEMPLATE.replace("__GRAPH_DATA__", data_json)


def save_and_open(html_content: str, output_path: str | None = None) -> str:
    """Save HTML to a file. Returns the path."""
    if output_path:
        path = output_path
    else:
        fd, path = tempfile.mkstemp(suffix=".html", prefix="gerrit-graph-")
        os.close(fd)
    with open(path, "w") as f:
        f.write(html_content)
    return path


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gerrit Series Graph</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
/* ─── THEME VARIABLES ─── */
:root {
    --bg: #0d1117; --bg-surface: #161b22; --bg-inset: #0d1117;
    --bg-hover: #21262d; --border: #30363d;
    --text: #c9d1d9; --text-muted: #8b949e; --text-dim: #484f58;
    --accent: #58a6ff; --accent-hover: #388bfd;
    --btn-bg: #21262d; --btn-border: #30363d;
    --edge-stroke: #0d1117;
}
body.light {
    --bg: #ffffff; --bg-surface: #f6f8fa; --bg-inset: #ffffff;
    --bg-hover: #eaeef2; --border: #d0d7de;
    --text: #1f2328; --text-muted: #656d76; --text-dim: #8b949e;
    --accent: #0969da; --accent-hover: #0550ae;
    --btn-bg: #f6f8fa; --btn-border: #d0d7de;
    --edge-stroke: #ffffff;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}

.topbar {
    background: var(--bg-surface); padding: 8px 16px;
    display: flex; align-items: center; gap: 16px;
    border-bottom: 1px solid var(--border); flex-shrink: 0; flex-wrap: wrap;
    position: relative;
}
.topbar h1 { font-size: 15px; color: var(--accent); white-space: nowrap; }
.stats { display: flex; gap: 10px; font-size: 12px; }
.badge {
    padding: 2px 8px; border-radius: 10px; font-weight: 600; font-size: 11px;
}
.badge-new { background: #1f6feb; color: #fff; }
.badge-merged { background: #6e40c9; color: #fff; }
.badge-abandoned { background: #484f58; color: #c9d1d9; }
body.light .badge-abandoned { background: #8b949e; color: #fff; }
.badge-stale { background: #d29922; color: #000; }

.controls {
    background: var(--bg-surface); padding: 6px 16px;
    display: flex; align-items: center; gap: 12px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0; flex-wrap: wrap; font-size: 13px;
}
.controls label {
    cursor: pointer; display: flex; align-items: center; gap: 4px;
}
.controls input[type="checkbox"] { accent-color: var(--accent); cursor: pointer; }
.controls button {
    background: var(--btn-bg); color: var(--text); border: 1px solid var(--btn-border);
    padding: 3px 10px; border-radius: 6px; cursor: pointer; font-size: 12px;
}
.controls button:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
.controls button.primary {
    background: #1f6feb; color: #fff; border-color: #1f6feb;
}
.controls button.primary:hover { background: #388bfd; }

/* Help overlay */
.help-overlay {
    position: fixed; inset: 0; z-index: 200;
    background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center;
}
.help-overlay.hidden { display: none; }
.help-box {
    background: var(--bg-surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 28px; max-width: 420px; width: 90%;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.help-box h3 { font-size: 15px; color: var(--accent); margin-bottom: 12px; }
.help-box table { width: 100%; border-collapse: collapse; }
.help-box td { padding: 3px 0; font-size: 13px; }
.help-box td:first-child {
    font-family: monospace; font-weight: 600; color: var(--accent);
    white-space: nowrap; padding-right: 16px; width: 1%;
}
.help-box td:last-child { color: var(--text); }

.legend {
    display: flex; gap: 10px; font-size: 11px; color: var(--text-muted);
    margin-left: auto;
}
.legend-item { display: flex; align-items: center; gap: 3px; }
.legend-dot {
    width: 10px; height: 10px; border-radius: 2px; display: inline-block;
}

/* vis-network navigation button overrides — only re-tint, keep default sprites */
div.vis-network div.vis-navigation div.vis-button {
    filter: saturate(0) brightness(1.6);
    opacity: 0.7;
}
div.vis-network div.vis-navigation div.vis-button:hover {
    filter: saturate(0) brightness(2);
    opacity: 1;
}
body.light div.vis-network div.vis-navigation div.vis-button {
    filter: saturate(0) brightness(0.8);
    opacity: 0.6;
}
body.light div.vis-network div.vis-navigation div.vis-button:hover {
    filter: saturate(0) brightness(0.4);
    opacity: 1;
}

.main { display: flex; flex: 1; overflow: hidden; position: relative; }

#graph { flex: 1; background: var(--bg); }

/* Search bar */
.search-bar {
    position: absolute; top: 8px; left: 50%; transform: translateX(-50%);
    z-index: 100; display: flex; align-items: center; gap: 6px;
    background: var(--bg-surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 4px 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}
.search-bar.hidden { display: none; }
.search-bar input {
    background: var(--bg-inset); color: var(--text); border: 1px solid var(--border);
    border-radius: 4px; padding: 4px 8px; font-size: 13px; width: 280px;
    outline: none; font-family: inherit;
}
.search-bar input:focus { border-color: var(--accent); }
.search-bar .search-info {
    font-size: 12px; color: var(--text-muted); white-space: nowrap; min-width: 70px;
    text-align: center;
}
.search-bar button {
    background: none; border: none; color: var(--text-muted); cursor: pointer;
    font-size: 16px; padding: 2px 4px; line-height: 1;
}
.search-bar button:hover { color: var(--text); }

.panel {
    width: 480px; min-width: 280px; max-width: 80vw;
    background: var(--bg-surface); border-left: 1px solid var(--border);
    overflow-y: auto; padding: 14px; flex-shrink: 0; position: relative;
}
.panel.hidden { display: none; }
.panel-drag {
    position: absolute; left: 0; top: 0; bottom: 0; width: 5px;
    cursor: col-resize; background: transparent; z-index: 10;
}
.panel-drag:hover, .panel-drag.active { background: var(--accent); }
.panel h2 {
    font-size: 14px; color: var(--accent); margin-bottom: 10px;
    padding-bottom: 6px; border-bottom: 1px solid var(--border);
}
.panel .field { margin-bottom: 6px; }
.panel .fl { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.panel .fv { color: var(--text); font-size: 14px; word-break: break-word; }
.panel a { color: var(--accent); text-decoration: none; }
.panel a:hover { text-decoration: underline; }
.panel .chain { margin-top: 8px; font-size: 13px; }
.panel .ci {
    padding: 5px 8px; margin: 3px 0; background: var(--bg-inset);
    border-radius: 4px; cursor: pointer; border-left: 3px solid var(--border);
    display: flex; align-items: center; gap: 8px;
}
.panel .ci:hover { background: var(--bg-hover); }
.panel .ci.anchor { border-left-color: #f85149; }
.panel .ci.main-chain { border-left-color: var(--accent); }
.panel .ci .snum { color: var(--accent); font-weight: 600; white-space: nowrap; font-size: 13px; }
.panel .ci .sbadge {
    font-size: 10px; padding: 2px 5px; border-radius: 4px; white-space: nowrap;
}
.panel .ci .ssub { color: var(--text); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }

.sbadge-NEW { background: #1f6feb; color: #fff; }
.sbadge-MERGED { background: #238636; color: #fff; }
.sbadge-ABANDONED { background: #484f58; color: #c9d1d9; }
body.light .sbadge-ABANDONED { background: #8b949e; color: #fff; }

.stale-tag {
    background: #d29922; color: #000; font-size: 9px;
    padding: 1px 4px; border-radius: 4px; font-weight: 700;
}
</style>
</head>
<body>

<div class="topbar">
    <h1 id="title">Gerrit Series Graph</h1>
    <div class="stats" id="stats"></div>
    <div class="legend">
        <span style="color:var(--text-muted);font-weight:600">Nodes:</span>
        <div class="legend-item"><span class="legend-dot" style="background:#238636"></span> Ready</div>
        <div class="legend-item"><span class="legend-dot" style="background:#1f6feb"></span> Pending</div>
        <div class="legend-item"><span class="legend-dot" style="background:#7a1a1a"></span> CR Veto</div>
        <div class="legend-item"><span class="legend-dot" style="background:#d32f2f"></span> Maloo</div>
        <div class="legend-item"><span class="legend-dot" style="background:#c47f17"></span> Jenkins</div>
        <div class="legend-item"><span class="legend-dot" style="background:#9b2d6e"></span> Other -1</div>
        <div class="legend-item"><span class="legend-dot" style="background:#6e40c9"></span> Merged</div>
        <div class="legend-item"><span class="legend-dot" style="background:#484f58"></span> Abandoned</div>
        <div class="legend-item"><span class="legend-dot" style="background:transparent;border:2px solid #c9d1d9"></span> Topic/hashtag series</div>
        <div class="legend-item"><span class="legend-dot" style="background:transparent;border:2px dashed #c9d1d9"></span> 🚧 WIP</div>
        <span style="color:var(--text-muted);font-weight:600;margin-left:8px">Edges:</span>
        <div class="legend-item"><span class="legend-dot" style="background:#d29922"></span> Stale</div>
    </div>
</div>

<div class="controls">
    <label><input type="checkbox" id="chk-abandoned"> Show abandoned</label>
    <button class="primary" id="btn-reset">Reset</button>
    <button id="btn-fit">Fit</button>
    <button id="btn-focus">Focus</button>
    <button id="btn-search">Search</button>
    <button id="btn-panel">Panel</button>
    <button id="btn-theme">Light</button>
    <button id="btn-help">?</button>
    <div id="generated-at" style="margin-left:auto;font-size:12px;font-weight:500;color:var(--text-muted)"></div>
</div>

<div class="help-overlay hidden" id="help-overlay" onclick="if(event.target===this)this.classList.add('hidden')">
    <div class="help-box">
        <h3>Keyboard Shortcuts</h3>
        <table>
            <tr><td>Ctrl+F</td><td>Search nodes</td></tr>
            <tr><td>F</td><td>Fit graph to view</td></tr>
            <tr><td>Z</td><td>Focus selected node / anchor</td></tr>
            <tr><td>R</td><td>Reset to original anchor</td></tr>
            <tr><td>+ / -</td><td>Zoom in / out</td></tr>
            <tr><td>Esc</td><td>Deselect / close search</td></tr>
            <tr><td>?</td><td>Toggle this help</td></tr>
        </table>
        <div style="margin-top:12px;font-size:12px;color:var(--text-muted)">
            Click a node to see details. Double-click to open in Gerrit.
        </div>
        <div style="text-align:right;margin-top:10px">
            <button onclick="document.getElementById('help-overlay').classList.add('hidden')"
                style="background:var(--btn-bg);color:var(--text);border:1px solid var(--btn-border);
                padding:4px 14px;border-radius:6px;cursor:pointer;font-size:12px">Close</button>
        </div>
    </div>
</div>

<div class="main">
    <div class="search-bar hidden" id="search-bar">
        <input type="text" id="search-input" placeholder="Search nodes...">
        <span class="search-info" id="search-info"></span>
        <button id="search-prev" title="Previous (Shift+Enter)">&#x25B2;</button>
        <button id="search-next" title="Next (Enter)">&#x25BC;</button>
        <button id="search-close" title="Close (Esc)">&#x2715;</button>
    </div>
    <div id="graph"></div>
    <div class="panel" id="panel">
        <div class="panel-drag" id="panel-drag"></div>
        <h2>Node Details</h2>
        <div id="info">
            <p style="color:#8b949e">Click a node to see details and its chain.<br><br>
            Double-click to open in Gerrit.<br><br>
            <b>Ctrl+F</b> to search &nbsp; <b>?</b> for all shortcuts</p>
        </div>
    </div>
</div>

<script>
// ─── DATA ───
const G = __GRAPH_DATA__;
const ANCHOR_INIT = G.anchor;

// ─── DERIVED STRUCTURES ───
const nodeMap = {};     // id -> node
const childrenOf = {};  // id -> [child ids]
const parentOf = {};    // id -> parent id
const edgeMap = {};     // "from->to" -> edge
const edgesFrom = {};   // id -> [edge objects from this id]
const edgesTo = {};     // id -> [edge objects to this id]

G.nodes.forEach(n => { nodeMap[n.id] = n; });
G.edges.forEach(e => {
    const key = e.from + '->' + e.to;
    edgeMap[key] = e;
    childrenOf[e.from] = childrenOf[e.from] || [];
    childrenOf[e.from].push(e.to);
    parentOf[e.to] = e.from;
    edgesFrom[e.from] = edgesFrom[e.from] || [];
    edgesFrom[e.from].push(e);
    edgesTo[e.to] = edgesTo[e.to] || [];
    edgesTo[e.to].push(e);
});

// ─── STATS BAR ───
const sc = G.stats.status_counts;
document.getElementById('stats').innerHTML = [
    ['NEW', sc.NEW || 0, 'badge-new'],
    ['MERGED', sc.MERGED || 0, 'badge-merged'],
    ['ABANDONED', sc.ABANDONED || 0, 'badge-abandoned'],
    ['Stale edges', G.stats.stale_edge_count || 0, 'badge-stale'],
].map(([l, c, cls]) => `<span class="badge ${cls}">${l}: ${c}</span>`).join('')
    + `<span style="color:#8b949e;font-size:11px">${G.stats.node_count} changes</span>`;

if (G.generated_at) {
    document.getElementById('generated-at').textContent = 'Generated: ' + G.generated_at;
}

// ─── STATE ───
let currentAnchor = ANCHOR_INIT;
let mainChain = new Set();
let selectedNodeId = null;

// ─── MAIN CHAIN COMPUTATION ───
//
// Cross-group edges (stale edges from the main series into separate
// topic/hashtag groups, and vice versa) exist as visual hints but must
// not participate in main-chain selection. If we follow them, a node
// in the main series can inherit an inflated descendant count from a
// separate series — making the walker pick an abandoned side branch
// over the real chain. All descendant/active computations below stay
// within the starting node's series_group.
function _groupOf(id) {
    const n = nodeMap[id];
    return n ? (n.series_group || 0) : 0;
}

const activeDescCache = {};
function hasActiveDescendant(id) {
    if (activeDescCache[id] !== undefined) return activeDescCache[id];
    const n = nodeMap[id];
    if (!n) { activeDescCache[id] = false; return false; }
    if (n.status !== 'ABANDONED') { activeDescCache[id] = true; return true; }
    const myGroup = n.series_group || 0;
    const kids = childrenOf[id] || [];
    for (const k of kids) {
        const kn = nodeMap[k];
        if (!kn) continue;
        if ((kn.series_group || 0) !== myGroup) continue; // don't cross groups
        if (hasActiveDescendant(k)) {
            activeDescCache[id] = true;
            return true;
        }
    }
    activeDescCache[id] = false;
    return false;
}

function computeMainChain(anchorId) {
    const chain = new Set();
    chain.add(anchorId);

    const anchorGroup = _groupOf(anchorId);

    // Walk upward: pick best child at each step. Do NOT filter out abandoned
    // children — we want to walk through abandoned patches if there are still
    // active patches above them. Trailing abandoned tails are trimmed below.
    // Stay within the anchor's series group so cross-group stale edges don't
    // derail the walk.
    let cursor = anchorId;
    const upward = [];
    const seen = new Set([anchorId]);
    while (true) {
        const kids = (childrenOf[cursor] || []).filter(k => {
            if (!nodeMap[k] || seen.has(k)) return false;
            return _groupOf(k) === anchorGroup;
        });
        if (kids.length === 0) break;
        // Prefer: branch that leads to an active patch, then the branch
        // with the most descendants (the dominant real chain), and finally
        // non-stale edge as tiebreaker. Descendant count must beat
        // staleness — otherwise a non-stale dead-end child wins over the
        // long "real" chain whose entry edge happens to be stale.
        kids.sort((a, b) => {
            const ha = hasActiveDescendant(a) ? 0 : 1;
            const hb = hasActiveDescendant(b) ? 0 : 1;
            if (ha !== hb) return ha - hb;
            const dd = countDesc(b) - countDesc(a);
            if (dd !== 0) return dd;
            const ea = edgeMap[cursor + '->' + a];
            const eb = edgeMap[cursor + '->' + b];
            const sa = ea ? (ea.is_stale ? 1 : 0) : 0;
            const sb = eb ? (eb.is_stale ? 1 : 0) : 0;
            return sa - sb;
        });
        upward.push(kids[0]);
        seen.add(kids[0]);
        cursor = kids[0];
    }

    // Trim trailing abandoned: keep the walk up to (and including) the
    // highest non-abandoned node. Everything above that last active node
    // is a purely-abandoned tail and stays hidden unless "Show abandoned".
    let lastActive = -1;
    for (let i = 0; i < upward.length; i++) {
        const n = nodeMap[upward[i]];
        if (n && n.status !== 'ABANDONED') lastActive = i;
    }
    for (let i = 0; i <= lastActive; i++) chain.add(upward[i]);

    // Walk downward: follow parent chain
    cursor = parentOf[anchorId];
    while (cursor && nodeMap[cursor]) {
        chain.add(cursor);
        cursor = parentOf[cursor];
    }

    return chain;
}

const descCache = {};
function countDesc(id) {
    if (descCache[id] !== undefined) return descCache[id];
    let count = 0;
    const myGroup = _groupOf(id);
    (childrenOf[id] || []).forEach(c => {
        const cn = nodeMap[c];
        if (!cn) return;
        // Don't count descendants that belong to a different series
        // group — cross-group stale edges would otherwise inflate the
        // count with unrelated history and skew main-chain selection.
        if ((cn.series_group || 0) !== myGroup) return;
        count += 1 + countDesc(c);
    });
    descCache[id] = count;
    return count;
}

// ─── TREE LAYOUT ───
const LEVEL_H = 140;
const NODE_W = 380;

function computeLayout(anchorId) {
    mainChain = computeMainChain(anchorId);
    const positions = {};
    const showAbandoned = document.getElementById('chk-abandoned').checked;

    function isVisible(id) {
        const n = nodeMap[id];
        if (!n) return false;
        // Abandoned nodes that bridge active patches in the main chain stay
        // visible even when "Show abandoned" is off.
        if (n.status === 'ABANDONED' && !showAbandoned && !mainChain.has(id)) return false;
        return true;
    }

    function shouldShow(id) {
        if (id == anchorId) return true;
        return isVisible(id);
    }

    // Compute subtree width for each node (number of leaf descendants)
    const widthCache = {};
    function subtreeWidth(id) {
        if (widthCache[id] !== undefined) return widthCache[id];
        const kids = (childrenOf[id] || []).filter(shouldShow);
        if (kids.length === 0) { widthCache[id] = 1; return 1; }
        let w = 0;
        kids.forEach(k => { w += subtreeWidth(k); });
        widthCache[id] = w;
        return w;
    }

    // Compute subtree height (max depth) for a node
    const heightCache = {};
    function subtreeHeight(id) {
        if (heightCache[id] !== undefined) return heightCache[id];
        const kids = (childrenOf[id] || []).filter(shouldShow);
        if (kids.length === 0) { heightCache[id] = 1; return 1; }
        let maxH = 0;
        kids.forEach(k => { maxH = Math.max(maxH, subtreeHeight(k)); });
        heightCache[id] = maxH + 1;
        return maxH + 1;
    }

    // Layout a tree from a node.
    // dir=1: children grow upward (negative y). dir=-1: children grow downward.
    // Returns the outermost level used by this subtree.
    function layoutTree(id, x, level, dir) {
        if (positions[id]) return level;
        positions[id] = { x, y: -level * LEVEL_H };

        const kids = (childrenOf[id] || [])
            .filter(shouldShow)
            .filter(k => !positions[k]);

        if (kids.length === 0) return level;

        // Separate main chain child from side branches.
        // If no child is on the global main chain, elect the best local
        // candidate (prefer non-stale edge, then most descendants) so the
        // dominant branch continues straight up instead of going sideways.
        let mainKid = kids.find(k => mainChain.has(k));
        if (!mainKid && kids.length > 1) {
            const sorted = kids.slice().sort((a, b) => {
                const ea = edgeMap[id + '->' + a];
                const eb = edgeMap[id + '->' + b];
                const sa = ea && ea.is_stale ? 1 : 0;
                const sb = eb && eb.is_stale ? 1 : 0;
                if (sa !== sb) return sa - sb;
                return countDesc(b) - countDesc(a);
            });
            mainKid = sorted[0];
        }
        const sideKids = kids.filter(k => k !== mainKid).sort((a, b) => a - b);

        if (kids.length === 1) {
            return layoutTree(kids[0], x, level + dir, dir);
        }

        // Place side branches first, alternating left and right
        const leftKids = [];
        const rightKids = [];
        for (let i = 0; i < sideKids.length; i++) {
            if (i % 2 === 0) {
                leftKids.push(sideKids[i]);
            } else {
                rightKids.push(sideKids[i]);
            }
        }

        // Track the outermost level used by side branches
        let extremeSideLevel = level;
        function updateExtreme(l) {
            if (dir > 0) { extremeSideLevel = Math.max(extremeSideLevel, l); }
            else { extremeSideLevel = Math.min(extremeSideLevel, l); }
        }

        // Place left branches (negative x offsets)
        let leftX = x - NODE_W;
        for (const kid of leftKids) {
            const w = subtreeWidth(kid);
            const top = layoutTree(kid, leftX - (w - 1) * NODE_W / 2, level + dir, dir);
            updateExtreme(top);
            leftX -= w * NODE_W;
        }

        // Place right branches (positive x offsets)
        let rightX = x + NODE_W;
        for (const kid of rightKids) {
            const w = subtreeWidth(kid);
            const top = layoutTree(kid, rightX + (w - 1) * NODE_W / 2, level + dir, dir);
            updateExtreme(top);
            rightX += w * NODE_W;
        }

        // Place main chain child past the outermost side branch so nothing overlaps
        if (mainKid) {
            const mainLevel = extremeSideLevel + dir;
            return layoutTree(mainKid, x, mainLevel, dir);
        }

        return extremeSideLevel;
    }

    // Convenience wrapper: layout growing upward (default direction)
    function layoutUp(id, x, level) {
        return layoutTree(id, x, level, 1);
    }

    // Place anchor
    positions[anchorId] = { x: 0, y: 0 };

    // Layout upward tree from anchor — delegate to layoutUp which
    // handles main-chain centering and left/right branch spreading
    // We already placed the anchor at (0,0), so layoutUp will handle children
    {
        const upKids = (childrenOf[anchorId] || [])
            .filter(shouldShow)
            .filter(k => !positions[k]);
        if (upKids.length > 0) {
            // Temporarily remove anchor from positions so layoutUp re-processes children
            const saved = positions[anchorId];
            delete positions[anchorId];
            layoutUp(anchorId, 0, 0);
            // Restore anchor position (layoutUp would have set it to same coords)
        }
    }

    // Layout base chain (below anchor) — straight down
    // Side branches grow UPWARD from each base node. Each base node must
    // be placed far enough below the previous one so that its upward
    // branches don't overlap with the previous node's branches.
    // This mirrors the upward tree logic: above the anchor, the main chain
    // child is pushed UP past side branches. Below the anchor, each base
    // node is pushed DOWN by its own branch height so branches grow into
    // the space ABOVE it.
    let cursor = parentOf[anchorId];
    let prevNodeLevel = 0; // anchor at level 0

    while (cursor && nodeMap[cursor] && !positions[cursor]) {
        if (!shouldShow(cursor) && cursor != anchorId) {
            cursor = parentOf[cursor];
            continue;
        }

        // Pre-compute how tall this node's side branches will be
        const sideKids = (childrenOf[cursor] || [])
            .filter(shouldShow)
            .filter(k => k != anchorId && !mainChain.has(k));
        let branchH = 0;
        for (const sk of sideKids) {
            branchH = Math.max(branchH, subtreeHeight(sk));
        }

        // Place this node far enough below prevNodeLevel so its branches
        // (which grow UP branchH levels) don't overlap with the previous
        // node's area. Branches reach from (nodeLevel+1) to (nodeLevel+branchH).
        // Constraint: nodeLevel + branchH < prevNodeLevel
        // So: nodeLevel = prevNodeLevel - branchH - 1 (at least 1 gap)
        const nodeLevel = prevNodeLevel - Math.max(1, branchH + 1);
        positions[cursor] = { x: 0, y: -nodeLevel * LEVEL_H };

        // Now actually layout the side branches growing upward
        if (sideKids.length > 0) {
            // Re-filter to exclude already positioned nodes
            const kids = sideKids.filter(k => !positions[k]);
            kids.sort((a, b) => a - b);
            let leftX = -NODE_W;
            let rightX = NODE_W;
            for (let bi = 0; bi < kids.length; bi++) {
                const bk = kids[bi];
                const w = subtreeWidth(bk);
                if (bi % 2 === 0) {
                    layoutTree(bk, rightX + (w - 1) * NODE_W / 2, nodeLevel + 1, 1);
                    rightX += w * NODE_W;
                } else {
                    layoutTree(bk, leftX - (w - 1) * NODE_W / 2, nodeLevel + 1, 1);
                    leftX -= w * NODE_W;
                }
            }
        }

        prevNodeLevel = nodeLevel;
        cursor = parentOf[cursor];
    }

    // Layout separate series: if a group has cross-group edges to
    // main-series nodes, its members are already reachable via
    // childrenOf and layoutTree has placed them as stale side
    // branches. For groups with NO connection to main (truly
    // disconnected), place them in columns to the far right.
    const groups = G.separate_groups || [];
    if (groups.length > 0) {
        let mainMaxX = 0;
        for (const pos of Object.values(positions)) {
            mainMaxX = Math.max(mainMaxX, pos.x);
        }
        let fallbackX = mainMaxX + NODE_W * 2;

        for (const group of groups) {
            const visibleIds = group.node_ids.filter(
                id => nodeMap[id] && isVisible(id)
            );
            if (visibleIds.length === 0) continue;

            // Check if ANY node in this group was already placed
            // via layoutTree (reachable through cross-group edges).
            const alreadyPlaced = visibleIds.some(
                id => positions[id] !== undefined
            );
            if (alreadyPlaced) {
                // Let layoutTree handle the rest via childrenOf.
                // Any unplaced nodes fall through to the fixup below.
                continue;
            }

            // Truly disconnected group: place as a vertical column
            // to the far right.
            const groupSet = new Set(group.node_ids);
            const groupParent = {};
            const groupChildren = {};
            for (const id of visibleIds) groupChildren[id] = [];
            for (const e of G.edges) {
                if (groupSet.has(e.from) && groupSet.has(e.to)) {
                    groupParent[e.to] = e.from;
                    groupChildren[e.from].push(e.to);
                }
            }
            const roots = visibleIds.filter(id => !groupParent[id]);
            const levels = {};
            const bfsQ = [];
            for (const r of roots) {
                levels[r] = 0;
                bfsQ.push(r);
            }
            while (bfsQ.length > 0) {
                const n = bfsQ.shift();
                for (const c of (groupChildren[n] || [])) {
                    if (!(c in levels)) {
                        levels[c] = levels[n] + 1;
                        bfsQ.push(c);
                    }
                }
            }
            for (const id of visibleIds) {
                if (!(id in levels)) levels[id] = 0;
            }
            for (const id of visibleIds) {
                positions[id] = {
                    x: fallbackX,
                    y: -levels[id] * LEVEL_H,
                };
            }
            fallbackX += NODE_W * 1.5;
        }

        // Fixup: any group nodes whose group had SOME reachable
        // nodes but this particular one wasn't placed (e.g. the
        // cross-group edge went to a different node in the group)
        // — place them near their parent/child if reachable.
        for (const group of groups) {
            for (const id of group.node_ids) {
                if (positions[id] !== undefined) continue;
                if (!nodeMap[id] || !isVisible(id)) continue;
                // Find a placed neighbor in same group
                let neighborPos = null;
                let neighborDir = 0;
                for (const e of G.edges) {
                    if (e.to === id && positions[e.from]) {
                        neighborPos = positions[e.from];
                        neighborDir = 1;  // child goes up
                        break;
                    }
                    if (e.from === id && positions[e.to]) {
                        neighborPos = positions[e.to];
                        neighborDir = -1;  // parent goes down
                        break;
                    }
                }
                if (neighborPos) {
                    let px = neighborPos.x;
                    let py = neighborPos.y + neighborDir * LEVEL_H;
                    // Shift right if occupied
                    let tries = 0;
                    while (tries < 20) {
                        let occupied = false;
                        for (const p of Object.values(positions)) {
                            if (Math.abs(p.x - px) < NODE_W * 0.9
                                    && Math.abs(p.y - py) < LEVEL_H * 0.6) {
                                occupied = true; break;
                            }
                        }
                        if (!occupied) break;
                        px += NODE_W;
                        tries++;
                    }
                    positions[id] = { x: px, y: py };
                }
            }
        }
    }

    // Final collision avoidance: shift any nodes that ended up at
    // exactly the same (x, y) as another node.
    const occupied = new Map();
    for (const [idStr, pos] of Object.entries(positions)) {
        const key = pos.x + ',' + pos.y;
        if (occupied.has(key)) {
            // Shift this one right
            let px = pos.x + NODE_W;
            let tries = 0;
            while (tries < 30) {
                const k2 = px + ',' + pos.y;
                if (!occupied.has(k2)) break;
                px += NODE_W;
                tries++;
            }
            positions[idStr] = { x: px, y: pos.y };
            occupied.set(px + ',' + pos.y, idStr);
        } else {
            occupied.set(key, idStr);
        }
    }

    return positions;
}

// ─── VIS.JS SETUP ───
const nodesDS = new vis.DataSet();
const edgesDS = new vis.DataSet();

const container = document.getElementById('graph');
const network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, {
    layout: { hierarchical: false },
    physics: { enabled: false },
    interaction: {
        hover: true, tooltipDelay: 150, zoomSpeed: 0.5,
        navigationButtons: true, keyboard: false,
    },
    nodes: {
        shape: 'box',
        margin: { top: 6, right: 10, bottom: 6, left: 10 },
        font: { face: 'monospace', size: 12, color: '#fff', multi: false },
        borderWidth: 2,
        shadow: false,
    },
    edges: {
        arrows: { to: { enabled: true, scaleFactor: 0.6 } },
        font: { face: 'monospace', size: 13, color: '#8b949e', strokeWidth: 4, strokeColor: 'transparent', align: 'middle' },
        smooth: { type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 },
    },
});


// ─── COLORS (theme-aware) ───
function isLight() { return document.body.classList.contains('light'); }
function getColors() {
    const light = isLight();
    return {
        STATUS: {
            NEW:       { bg: '#1f6feb', border: '#388bfd', font: '#fff' },
            MERGED:    { bg: '#6e40c9', border: '#8957e5', font: '#fff' },
            ABANDONED: light
                ? { bg: '#afb8c1', border: '#8b949e', font: '#24292f' }
                : { bg: '#30363d', border: '#484f58', font: '#8b949e' },
        },
        // Review health: overrides STATUS.NEW color for active patches
        REVIEW_GOOD:       { bg: '#238636', border: '#3fb950', font: '#fff' },
        REVIEW_BAD_VETO:   { bg: '#7a1a1a', border: '#a82828', font: '#fff' },  // CR veto — dark red
        REVIEW_BAD_MALOO:  { bg: '#d32f2f', border: '#f85149', font: '#fff' },  // Maloo -1 — bright red
        REVIEW_BAD_JENKINS:{ bg: '#c47f17', border: '#e8a020', font: '#fff' },  // Jenkins -1 — orange
        REVIEW_BAD_OTHER:  { bg: '#9b2d6e', border: '#d63384', font: '#fff' },  // Other -1 — pink
        DIM: light
            ? { bg: '#eaeef2', border: '#d0d7de', font: '#8b949e' }
            : { bg: '#161b22', border: '#21262d', font: '#484f58' },
        HIGHLIGHT: light
            ? { bg: '#bf8700', border: '#9a6700' }
            : { bg: '#ffa657', border: '#f0883e' },
        edgeMain: light ? '#0969da' : '#58a6ff',
        edgeStale: '#d29922',
        edgeDim: light ? '#d0d7de' : '#21262d',
        edgeSide: light ? '#8b949e' : '#30363d',
        edgeFontNormal: light ? '#57606a' : '#6e7681',
        edgeFontStale: '#d29922',
        edgeStroke: light ? '#ffffff' : '#0d1117',
    };
}

// ─── REVIEW HEALTH ───
// Returns: 'good', 'pending', or a specific failure type:
//   'bad_veto'    — CR -1/-2 (highest priority)
//   'bad_maloo'   — Maloo verified -1
//   'bad_jenkins' — Jenkins verified -1
//   'bad_other'   — other verified -1
function reviewHealth(node) {
    if (node.status !== 'NEW') return 'pending';
    const rv = node.review || {};

    // CR veto is highest priority (overrides everything)
    if (rv.cr_veto) return 'bad_veto';

    // Verified failures: classify by voter name
    if (rv.verified_fail) {
        const failVoters = (rv.verified_votes || [])
            .filter(v => v.value < 0)
            .map(v => v.name.toLowerCase());
        if (failVoters.some(n => n === 'maloo')) return 'bad_maloo';
        if (failVoters.some(n => n === 'jenkins')) return 'bad_jenkins';
        return 'bad_other';
    }

    // Good: verified passed AND >= 2 non-author CR +1s
    if (rv.verified_pass) {
        const author = node.author || '';
        const nonAuthorPlus = (rv.cr_votes || []).filter(
            v => v.value > 0 && v.name !== author
        ).length;
        if (nonAuthorPlus >= 2) return 'good';
    }

    return 'pending';
}

// ─── RENDER ───
function renderGraph() {
    const positions = computeLayout(currentAnchor);
    const showAbandoned = document.getElementById('chk-abandoned').checked;

    // Determine which nodes are in the "active subtree" (reachable from anchor going up)
    const activeUp = new Set();
    function markActiveUp(id) {
        activeUp.add(id);
        (childrenOf[id] || []).forEach(c => {
            if (positions[c]) markActiveUp(c);
        });
    }
    markActiveUp(currentAnchor);

    // Build vis.js nodes
    const visNodes = [];
    const visEdges = [];

    for (const [idStr, pos] of Object.entries(positions)) {
        const id = parseInt(idStr);
        const node = nodeMap[id];
        if (!node) continue;

        const isAnchor = id === currentAnchor;
        const isMain = mainChain.has(id);
        const isAbove = activeUp.has(id);
        // Any node in a non-zero series_group is a separate-series
        // member: it gets its own distinctive border and is never
        // dimmed or treated as base-chain context. Cross-group edges
        // are informational only — they don't make a separate series
        // "part of" the main chain.
        const isSeparate = (node.series_group || 0) > 0;
        // Separate-group nodes aren't part of the base chain — color by status.
        const isBase = !isAbove && !isAnchor && !isSeparate;

        const C = getColors();
        let colors;
        if (isBase) {
            colors = C.DIM;
        } else if (node.status === 'NEW') {
            const health = reviewHealth(node);
            if (health === 'bad_veto') colors = C.REVIEW_BAD_VETO;
            else if (health === 'bad_maloo') colors = C.REVIEW_BAD_MALOO;
            else if (health === 'bad_jenkins') colors = C.REVIEW_BAD_JENKINS;
            else if (health === 'bad_other') colors = C.REVIEW_BAD_OTHER;
            else if (health === 'good') colors = C.REVIEW_GOOD;
            else colors = C.STATUS.NEW;
        } else {
            colors = C.STATUS[node.status] || C.STATUS.NEW;
        }

        // Separate-series nodes: override border to a distinct purple
        // so they're visually obvious as a different series.
        if (isSeparate) {
            colors = Object.assign({}, colors, { border: '#c9d1d9' });
        }

        // If not on main chain and above anchor, slightly dim.
        // Separate-series nodes are never dimmed — they render at full
        // intensity with their own distinctive border.
        const opacity = (isAbove && !isMain && !isAnchor && !isSeparate) ? 0.7 : 1.0;

        const shortSubject = node.subject.length > 50
            ? node.subject.substring(0, 47) + '...'
            : node.subject;

        // Build review status line
        let reviewLine = '';
        if (node.status !== 'ABANDONED' && node.status !== 'MERGED') {
            const rv = node.review || {};

            // Verified summary: show each voter's status
            const vVotes = rv.verified_votes || [];
            let vStr = '';
            if (vVotes.length === 0) {
                vStr = 'V:- ';
            } else {
                vStr = vVotes.map(v => {
                    // Abbreviate known names
                    let n = v.name;
                    if (/jenkins/i.test(n)) n = 'J';
                    else if (/maloo/i.test(n)) n = 'M';
                    else n = n.split(' ')[0].substring(0, 6);
                    return n + ':' + (v.value > 0 ? '\u2713' : '\u2717');
                }).join(' ') + ' ';
            }

            // CR summary
            const crPlus = (rv.cr_votes || []).filter(v => v.value > 0).length;
            const crMinus = (rv.cr_votes || []).filter(v => v.value < 0).length;
            let crStr = '';
            if (rv.cr_veto) {
                crStr = '\u2717 VETO';
            } else if (rv.cr_approved) {
                crStr = '\u2713 +2';
            } else if (crPlus > 0 || crMinus > 0) {
                const parts = [];
                if (crPlus > 0) parts.push(crPlus + '\u00d7(+1)');
                if (crMinus > 0) parts.push(crMinus + '\u00d7(-1)');
                crStr = parts.join(' ');
            } else {
                crStr = 'none';
            }
            // Comment count
            const cc = rv.unresolved_count || 0;
            const ccStr = cc > 0 ? ` | \u{1f4ac}${cc}` : '';
            reviewLine = `\n${vStr}| CR: ${crStr}${ccStr}`;
        }

        // WIP marker: prefix the label with 🚧 so it's unmistakable
        // even at low zoom. Combined with the dashed border below.
        const wipPrefix = node.is_wip ? '\u{1f6a7} ' : '';
        let label = `${wipPrefix}#${node.id}\n${shortSubject}${reviewLine}`;

        visNodes.push({
            id: id,
            label: label,
            x: pos.x,
            y: pos.y,
            fixed: { x: true, y: true },
            color: {
                background: colors.bg,
                border: colors.border,
                highlight: { background: C.HIGHLIGHT.bg, border: C.HIGHLIGHT.border },
            },
            font: {
                color: colors.font,
                size: 12,
                face: 'monospace',
            },
            // WIP nodes get a dashed border (vis.js native) — pattern
            // reads as "non-final" and never collides with the solid
            // colored borders that encode review state. Only attach
            // shapeProperties when WIP so non-WIP nodes use defaults.
            ...(node.is_wip ? { shapeProperties: { borderDashes: [6, 4] } } : {}),
            borderWidth: isAnchor ? 4 : (node.is_wip ? 3 : (isSeparate ? 3 : (isMain ? 2 : 1))),
            opacity: opacity,
            // Custom data for click handler
            _isAnchor: isAnchor,
            _isMain: isMain,
        });
    }

    // Build vis.js edges
    let edgeIdx = 0;
    for (const edge of G.edges) {
        if (!positions[edge.from] || !positions[edge.to]) continue;

        const isMainEdge = mainChain.has(edge.from) && mainChain.has(edge.to);
        const isBase = !activeUp.has(edge.to);

        const C = getColors();
        let color, width, dashes;
        if (edge.is_stale) {
            color = C.edgeStale;
            width = 2;
            dashes = [8, 4];
        } else if (isMainEdge) {
            color = C.edgeMain;
            width = 3;
            dashes = false;
        } else if (isBase) {
            color = C.edgeDim;
            width = 1;
            dashes = false;
        } else {
            color = C.edgeSide;
            width = 1.5;
            dashes = false;
        }

        // Edge label: patchset number
        let label;
        if (edge.is_stale) {
            label = `ps${edge.parent_patchset}→${edge.parent_latest}`;
        } else {
            label = `ps${edge.parent_patchset}`;
        }

        visEdges.push({
            id: 'e' + edgeIdx,
            from: edge.from,
            to: edge.to,
            label: label,
            color: { color: color, highlight: C.HIGHLIGHT.bg },
            width: width,
            dashes: dashes,
            font: {
                color: edge.is_stale ? C.edgeFontStale : C.edgeFontNormal,
                size: edge.is_stale ? 14 : 12,
                strokeWidth: 4,
                strokeColor: C.edgeStroke,
            },
            smooth: {
                type: 'cubicBezier',
                forceDirection: 'vertical',
                roundness: 0.4,
            },
        });
        edgeIdx++;
    }

    // Update datasets
    nodesDS.clear();
    edgesDS.clear();
    nodesDS.add(visNodes);
    edgesDS.add(visEdges);

    // Update title
    document.getElementById('title').textContent =
        `Series Graph — #${currentAnchor}`;

    // Fit after render
    setTimeout(() => {
        network.fit({ animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
    }, 50);
}

// ─── REVIEW STATUS RENDERING ───
function reviewIcon(val) {
    if (val > 0) return '<span style="color:#3fb950;font-weight:700">\u2713</span>';
    if (val < 0) return '<span style="color:#f85149;font-weight:700">\u2717</span>';
    return '<span style="color:#8b949e">—</span>';
}

function renderReviewPanel(node) {
    const rv = node.review || {};
    if (node.status === 'ABANDONED') return '';
    if (node.status === 'MERGED') {
        return `<div class="field">
            <div class="fl">Review</div>
            <div class="fv" style="color:#3fb950">Merged</div>
        </div>`;
    }

    // Health summary
    const health = reviewHealth(node);
    let healthBadge;
    if (health === 'good') {
        healthBadge = '<span style="color:#3fb950;font-weight:700">\u2713 Ready</span>';
    } else if (health === 'bad_veto') {
        healthBadge = '<span style="color:#a82828;font-weight:700">\u2717 CR Veto</span>';
    } else if (health === 'bad_maloo') {
        healthBadge = '<span style="color:#f85149;font-weight:700">\u2717 Maloo Failed</span>';
    } else if (health === 'bad_jenkins') {
        healthBadge = '<span style="color:#e8a020;font-weight:700">\u2717 Jenkins Failed</span>';
    } else if (health === 'bad_other') {
        healthBadge = '<span style="color:#d63384;font-weight:700">\u2717 Verified Failed</span>';
    } else {
        healthBadge = '<span style="color:#8b949e">Pending</span>';
    }

    // Verified section — show ALL voters with CI links
    const vVotes = rv.verified_votes || [];
    const jenkinsUrl = rv.jenkins_url || '';
    const malooUrl = rv.maloo_url || '';

    let verifiedHtml = '';
    if (vVotes.length === 0) {
        verifiedHtml = '<div style="color:#8b949e;font-size:13px">No verified votes</div>';
    } else {
        verifiedHtml = '<div style="margin:2px 0">';
        for (const v of vVotes) {
            // Add link for Jenkins/Maloo if available, with descriptive label
            let nameHtml = esc(v.name);
            const nl = v.name.toLowerCase();
            if (/jenkins/i.test(nl) && jenkinsUrl) {
                nameHtml = `<a href="${jenkinsUrl}" target="_blank">Jenkins Build</a>`;
            } else if (/maloo/i.test(nl) && malooUrl) {
                nameHtml = `<a href="${malooUrl}" target="_blank">Maloo Test Results</a>`;
            }
            verifiedHtml += `<div style="font-size:13px;margin:1px 0">
                ${reviewIcon(v.value)}
                <span style="color:var(--text)">${nameHtml}</span>
            </div>`;
        }
        verifiedHtml += '</div>';
    }


    // Code-Review section
    const crVotes = rv.cr_votes || [];
    const author = node.author || '';
    let crHtml = '';
    if (rv.cr_rejected) {
        crHtml = `<div style="color:#f85149;font-weight:700;margin:2px 0">\u2717 VETOED by ${esc(rv.cr_rejected_by)}</div>`;
    } else if (rv.cr_approved) {
        crHtml = `<div style="color:#3fb950;font-weight:700;margin:2px 0">\u2713 Approved (+2)</div>`;
    }

    if (crVotes.length > 0) {
        crHtml += '<div style="margin-top:4px">';
        for (const v of crVotes) {
            const color = v.value > 0 ? '#3fb950' : '#f85149';
            const sign = v.value > 0 ? '+' : '';
            const isAuthor = v.name === author;
            const authorTag = isAuthor ? ' <span style="color:var(--text-muted);font-size:11px">(author)</span>' : '';
            crHtml += `<div style="font-size:13px;margin:1px 0${isAuthor ? ';opacity:0.6' : ''}">
                <span style="color:${color};font-weight:600">${sign}${v.value}</span>
                <span style="color:var(--text)">${esc(v.name)}</span>${authorTag}
            </div>`;
        }
        crHtml += '</div>';
    } else if (!rv.cr_approved && !rv.cr_rejected) {
        crHtml = '<div style="color:#8b949e;font-size:13px">No reviews yet</div>';
    }

    return `<div class="field">
        <div class="fl">Review Health</div>
        <div class="fv">${healthBadge}</div>
    </div>
    <div class="field">
        <div class="fl">Verified</div>
        <div class="fv">${verifiedHtml}</div>
    </div>
    <div class="field">
        <div class="fl">Code Review</div>
        <div class="fv">${crHtml}</div>
    </div>
    ${renderCommentsPanel(node)}`;
}

function renderCommentsPanel(node) {
    const rv = node.review || {};
    const count = rv.unresolved_count || 0;
    const comments = rv.unresolved_comments || [];
    if (count === 0 && comments.length === 0) return '';

    let html = `<div class="field">
        <div class="fl">Unresolved Comments (${count})</div>
        <div class="fv">`;

    if (comments.length === 0) {
        html += `<div style="color:#8b949e;font-size:13px">${count} unresolved (details not fetched)</div>`;
    } else {
        const currentPs = node.current_patchset;
        html += '<div style="max-height:300px;overflow-y:auto">';
        for (const c of comments) {
            const stale = c.patch_set < currentPs
                ? `<span style="color:#d29922;font-size:10px"> ps${c.patch_set}</span>`
                : '';
            const file = c.file === '/COMMIT_MSG' ? 'Commit Message' : c.file;
            // Link to the comment on Gerrit
            const commentUrl = `${node.url}/comment/${c.id}/`;
            html += `<div style="margin:4px 0;padding:5px 8px;background:var(--bg-inset);border-radius:4px;border-left:2px solid var(--accent);font-size:12px">
                <div>
                    <a href="${commentUrl}" target="_blank" style="color:var(--accent);font-weight:600;font-size:11px">${esc(file)}:${c.line}</a>${stale}
                    <span style="color:var(--text-muted);font-size:11px"> — ${esc(c.author)}</span>
                </div>
                <div style="color:var(--text);margin-top:2px;white-space:pre-wrap;word-break:break-word">${esc(c.message)}</div>
            </div>`;
        }
        html += '</div>';
    }

    if (comments.length > 0) {
        html += '<div style="color:var(--text-muted);font-size:10px;font-style:italic;margin-top:4px">Note: Gerrit API comment resolution tracking is unreliable; listed comments may not match exactly.</div>';
    }

    html += '</div></div>';
    return html;
}

// ─── INFO PANEL ───
function showNodeInfo(id) {
    const node = nodeMap[id];
    if (!node) return;
    const panel = document.getElementById('info');

    // Respect current filter state
    const showAbandoned = document.getElementById('chk-abandoned').checked;
    function isListVisible(nid) {
        const n = nodeMap[nid];
        if (!n) return false;
        if (n.status === 'ABANDONED' && !showAbandoned && !mainChain.has(nid)) return false;
        return true;
    }

    // Find chain above (walk up from this node)
    const above = [];
    function walkUp(nid, depth) {
        if (depth > 50) return;
        const kids = (childrenOf[nid] || []).filter(k => nodeMap[k] && isListVisible(k));
        // Sort: main chain first
        kids.sort((a, b) => {
            if (mainChain.has(a) && !mainChain.has(b)) return -1;
            if (!mainChain.has(a) && mainChain.has(b)) return 1;
            return a - b;
        });
        kids.forEach(k => {
            const edge = edgeMap[nid + '->' + k];
            above.push({ node: nodeMap[k], edge: edge });
            walkUp(k, depth + 1);
        });
    }
    walkUp(id, 0);

    // Find chain below (walk down)
    const below = [];
    let cursor = parentOf[id];
    while (cursor && nodeMap[cursor] && below.length < 30) {
        if (!isListVisible(cursor)) { id = cursor; cursor = parentOf[cursor]; continue; }
        const edge = edgeMap[cursor + '->' + id];
        below.push({ node: nodeMap[cursor], edge: edge });
        id = cursor;
        cursor = parentOf[cursor];
    }

    const staleIncoming = (edgesTo[node.id] || []).filter(e => e.is_stale);
    const staleTag = staleIncoming.length > 0
        ? `<span class="stale-tag">NEEDS REBASE</span>` : '';

    panel.innerHTML = `
        <div class="field">
            <div class="fl">Change</div>
            <div class="fv">
                <a href="${node.url}" target="_blank">#${node.id}</a>
                <span class="sbadge sbadge-${node.status}">${node.status}</span>
                ${node.is_wip ? '<span class="stale-tag" style="background:#7a1a1a;color:#f85149;border-color:#f85149">WIP</span>' : ''}
                ${staleTag}
                &nbsp; ps${node.current_patchset}
                ${node.checkout_cmd ? `<button onclick="navigator.clipboard.writeText('${node.checkout_cmd.replace(/'/g, "\\'")}');this.textContent='\u2713';setTimeout(()=>this.textContent='Checkout',1500)" style="cursor:pointer;font-size:11px;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 8px;color:var(--accent);margin-left:6px" title="Copy checkout command to clipboard">Checkout</button>` : ''}
                ${node.cherrypick_cmd ? `<button onclick="navigator.clipboard.writeText('${node.cherrypick_cmd.replace(/'/g, "\\'")}');this.textContent='\u2713';setTimeout(()=>this.textContent='Cherry-pick',1500)" style="cursor:pointer;font-size:11px;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 8px;color:var(--accent);margin-left:4px" title="Copy cherry-pick command to clipboard">Cherry-pick</button>` : ''}
            </div>
        </div>
        <div class="field">
            <div class="fl">Subject</div>
            <div class="fv">${esc(node.subject)}</div>
        </div>
        <div class="field">
            <div class="fl">Author</div>
            <div class="fv">${esc(node.author)}</div>
        </div>
        ${node.updated ? `<div class="field">
            <div class="fl">Updated</div>
            <div class="fv">${formatGerritDate(node.updated)}</div>
        </div>` : ''}
        <div class="field">
            <div class="fl">Ticket</div>
            <div class="fv">${node.ticket || '\u2014'}</div>
        </div>
        ${node.topic ? `<div class="field">
            <div class="fl">Topic</div>
            <div class="fv">${esc(node.topic)}</div>
        </div>` : ''}
        ${(node.hashtags && node.hashtags.length > 0) ? `<div class="field">
            <div class="fl">Hashtags</div>
            <div class="fv">${node.hashtags.map(h => '<span style="background:var(--bg-inset);padding:1px 6px;border-radius:3px;font-size:12px;margin-right:4px">' + esc(h) + '</span>').join('')}</div>
        </div>` : ''}
        ${renderReviewPanel(node)}
        ${staleIncoming.length > 0 ? `
        <div class="field">
            <div class="fl">Stale dependency</div>
            <div class="fv" style="color:#d29922">
                Based on ps${staleIncoming[0].parent_patchset} of #${staleIncoming[0].from},
                now at ps${staleIncoming[0].parent_latest}
            </div>
        </div>` : ''}
        <div class="field" style="margin-top:8px">
            <button class="primary" onclick="reanchor(${node.id})" style="font-size:12px;padding:4px 12px;border-radius:6px;cursor:pointer">
                Re-anchor here
            </button>
        </div>

        ${above.length > 0 ? `
        <h2>Dependents (${above.length})</h2>
        <div class="chain">
            ${above.slice().reverse().map(a => chainItem(a.node, a.edge, node.id)).join('')}
        </div>` : '<h2>Tip (no dependents)</h2>'}

        ${below.length > 0 ? `
        <h2>Dependencies (${below.length})</h2>
        <div class="chain">
            ${below.map(b => chainItem(b.node, b.edge, node.id, true)).join('')}
        </div>` : ''}
    `;
}

function chainItem(node, edge, selectedId, isBelow) {
    const isAnc = node.id === currentAnchor;
    const isMain = mainChain.has(node.id);
    const cls = isAnc ? 'anchor' : (isMain ? 'main-chain' : '');
    const stale = edge && edge.is_stale
        ? `<span class="stale-tag">ps${edge.parent_patchset}→${edge.parent_latest}</span>`
        : (edge ? `<span style="color:#484f58;font-size:10px">ps${edge.parent_patchset}</span>` : '');

    return `<div class="ci ${cls}" onclick="clickNode(${node.id})" title="${esc(node.subject)}">
        <span class="snum">#${node.id}</span>
        <span class="sbadge sbadge-${node.status}" style="font-size:9px">${node.status.substring(0, 3)}</span>
        ${stale}
        <span class="ssub">${esc(node.subject)}</span>
    </div>`;
}

function showDefaultInfo() {
    document.getElementById('info').innerHTML = `
        <p style="color:#8b949e">Click a node to see details.<br><br>
        Double-click to open in Gerrit.<br><br>
        <b>Ctrl+F</b> to search &nbsp; <b>?</b> for all shortcuts</p>`;
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function formatGerritDate(s) {
    // Gerrit format: "2025-06-15 14:30:00.000000000"
    if (!s) return '';
    const iso = s.replace(' ', 'T').replace(/\..*$/, '') + 'Z';
    const d = new Date(iso);
    if (isNaN(d)) return esc(s);
    return d.toLocaleDateString(undefined, { year:'numeric', month:'short', day:'numeric' })
        + ' ' + d.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' });
}

// ─── INTERACTION ───
function clickNode(id) {
    network.selectNodes([id]);
    network.focus(id, { scale: 1.0, animation: { duration: 300, easingFunction: 'easeInOutQuad' } });
    showNodeInfo(id);
}

function reanchor(id) {
    currentAnchor = id;
    renderGraph();
    showNodeInfo(id);
}

network.on('click', function(params) {
    if (params.nodes.length > 0) {
        selectedNodeId = params.nodes[0];
        showNodeInfo(selectedNodeId);
    } else {
        selectedNodeId = null;
        showDefaultInfo();
    }
});

network.on('doubleClick', function(params) {
    if (params.nodes.length > 0) {
        const node = nodeMap[params.nodes[0]];
        if (node) window.open(node.url, '_blank');
    }
});

// Single middle-click opens the patch in a background tab
container.addEventListener('auxclick', function(e) {
    if (e.button !== 1) return; // middle button only
    e.preventDefault();
    const nodeId = network.getNodeAt({ x: e.offsetX, y: e.offsetY });
    const node = nodeId != null ? nodeMap[nodeId] : null;
    if (!node) return;
    // Open in background: create a link and dispatch a Ctrl/Meta click
    // so the browser treats it as a background-tab open.
    const a = document.createElement('a');
    a.href = node.url;
    a.target = '_blank';
    a.rel = 'noopener';
    const evt = new MouseEvent('click', { ctrlKey: true, metaKey: true, bubbles: true });
    a.dispatchEvent(evt);
});
// Prevent middle-click auto-scroll
container.addEventListener('mousedown', function(e) {
    if (e.button === 1) e.preventDefault();
});

// ─── CONTROLS ───
function onFilterChange() {
    renderGraph();
    if (selectedNodeId !== null) { showNodeInfo(selectedNodeId); }
}
document.getElementById('chk-abandoned').addEventListener('change', onFilterChange);
document.getElementById('btn-reset').addEventListener('click', function() {
    currentAnchor = ANCHOR_INIT;
    renderGraph();
    showDefaultInfo();
});
document.getElementById('btn-fit').addEventListener('click', function() {
    network.fit({ animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
});
document.getElementById('btn-focus').addEventListener('click', function() {
    const target = selectedNodeId !== null ? selectedNodeId : currentAnchor;
    network.focus(target, {
        scale: 1.5,
        animation: { duration: 400, easingFunction: 'easeInOutQuad' },
    });
});
document.getElementById('btn-search').addEventListener('click', openSearch);
document.getElementById('btn-panel').addEventListener('click', function() {
    document.getElementById('panel').classList.toggle('hidden');
    setTimeout(() => network.redraw(), 100);
});
document.getElementById('btn-help').addEventListener('click', function() {
    document.getElementById('help-overlay').classList.toggle('hidden');
});
document.getElementById('btn-theme').addEventListener('click', function() {
    document.body.classList.toggle('light');
    const btn = document.getElementById('btn-theme');
    btn.textContent = isLight() ? 'Dark' : 'Light';
    renderGraph();
    if (selectedNodeId !== null) { showNodeInfo(selectedNodeId); }
});

// Keyboard
document.addEventListener('keydown', function(e) {
    // Ctrl/Cmd+F opens search
    if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        openSearch();
        return;
    }
    if (e.target.tagName === 'INPUT') return;
    if (e.key === 'f' || e.key === 'F') {
        network.fit({ animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
    } else if (e.key === 'r' || e.key === 'R') {
        currentAnchor = ANCHOR_INIT;
        renderGraph();
        showDefaultInfo();
    } else if (e.key === 'z' || e.key === 'Z') {
        const target = selectedNodeId !== null ? selectedNodeId : currentAnchor;
        network.focus(target, {
            scale: 1.5,
            animation: { duration: 400, easingFunction: 'easeInOutQuad' },
        });
    } else if (e.key === '+' || e.key === '=') {
        const scale = network.getScale();
        network.moveTo({ scale: scale * 1.3, animation: { duration: 200, easingFunction: 'easeInOutQuad' } });
    } else if (e.key === '-') {
        const scale = network.getScale();
        network.moveTo({ scale: scale / 1.3, animation: { duration: 200, easingFunction: 'easeInOutQuad' } });
    } else if (e.key === '?') {
        document.getElementById('help-overlay').classList.toggle('hidden');
    } else if (e.key === 'Escape') {
        const help = document.getElementById('help-overlay');
        if (!help.classList.contains('hidden')) {
            help.classList.add('hidden');
        } else {
            network.unselectAll();
            showDefaultInfo();
        }
    }
});

// ─── PANEL RESIZE DRAG ───
(function() {
    const panel = document.getElementById('panel');
    const drag = document.getElementById('panel-drag');
    let dragging = false;
    drag.addEventListener('mousedown', function(e) {
        dragging = true;
        drag.classList.add('active');
        e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
        if (!dragging) return;
        const newWidth = window.innerWidth - e.clientX;
        panel.style.width = Math.max(280, Math.min(newWidth, window.innerWidth * 0.8)) + 'px';
    });
    document.addEventListener('mouseup', function() {
        if (dragging) {
            dragging = false;
            drag.classList.remove('active');
            setTimeout(() => network.redraw(), 50);
        }
    });
})();

// ─── SEARCH ───
function getNodeSearchText(node) {
    const parts = [
        '#' + node.id,
        node.subject,
        node.author,
        node.status,
        node.ticket || '',
        node.topic || '',
        (node.hashtags || []).join(' '),
        'ps' + node.current_patchset,
    ];
    const rv = node.review || {};
    (rv.cr_votes || []).forEach(v => parts.push(v.name));
    (rv.verified_votes || []).forEach(v => parts.push(v.name));
    if (rv.cr_rejected_by) parts.push(rv.cr_rejected_by);
    (rv.unresolved_comments || []).forEach(c => {
        parts.push(c.file || '', c.author || '', c.message || '');
    });
    return parts.join('\n').toLowerCase();
}

// Pre-build search index
const searchIndex = {};
G.nodes.forEach(n => { searchIndex[n.id] = getNodeSearchText(n); });

let searchMatches = [];
let searchIdx = -1;

function searchNodes(query) {
    if (!query) { searchMatches = []; searchIdx = -1; return; }
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    // Only match nodes currently rendered in the graph
    const rendered = new Set(nodesDS.getIds());
    searchMatches = G.nodes
        .filter(n => {
            if (!rendered.has(n.id)) return false;
            const text = searchIndex[n.id];
            return terms.every(t => text.includes(t));
        })
        .map(n => n.id);
    searchIdx = searchMatches.length > 0 ? 0 : -1;
}

function updateSearchHighlight() {
    const info = document.getElementById('search-info');
    if (searchMatches.length === 0) {
        info.textContent = searchIdx === -1 && !document.getElementById('search-input').value
            ? '' : 'No matches';
        // Reset any previous highlight
        const updates = [];
        nodesDS.forEach(n => {
            if (n._searchMatch !== undefined) updates.push({ id: n.id, borderWidth: n._origBorder, color: n._origColor, _searchMatch: undefined });
        });
        if (updates.length) nodesDS.update(updates);
        return;
    }
    info.textContent = (searchIdx + 1) + ' / ' + searchMatches.length;

    const matchSet = new Set(searchMatches);
    const updates = [];
    nodesDS.forEach(n => {
        const isMatch = matchSet.has(n.id);
        if (isMatch && !n._searchMatch) {
            updates.push({ id: n.id, _origBorder: n.borderWidth, _origColor: n.color,
                _searchMatch: true, borderWidth: 4,
                color: Object.assign({}, n.color, { border: '#f0e040' }) });
        } else if (!isMatch && n._searchMatch) {
            updates.push({ id: n.id, borderWidth: n._origBorder, color: n._origColor,
                _searchMatch: undefined });
        }
    });
    if (updates.length) nodesDS.update(updates);

    // Focus the current match
    if (searchIdx >= 0) {
        const focusId = searchMatches[searchIdx];
        network.selectNodes([focusId]);
        network.focus(focusId, { animation: { duration: 300, easingFunction: 'easeInOutQuad' } });
        showNodeInfo(focusId);
        selectedNodeId = focusId;
    }
}

function openSearch() {
    const bar = document.getElementById('search-bar');
    bar.classList.remove('hidden');
    const input = document.getElementById('search-input');
    input.focus();
    input.select();
}

function closeSearch() {
    document.getElementById('search-bar').classList.add('hidden');
    searchMatches = [];
    searchIdx = -1;
    updateSearchHighlight();
    document.getElementById('search-input').value = '';
    document.getElementById('search-info').textContent = '';
}

document.getElementById('search-input').addEventListener('input', function() {
    searchNodes(this.value);
    updateSearchHighlight();
});

document.getElementById('search-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        if (searchMatches.length === 0) return;
        if (e.shiftKey) {
            searchIdx = (searchIdx - 1 + searchMatches.length) % searchMatches.length;
        } else {
            searchIdx = (searchIdx + 1) % searchMatches.length;
        }
        updateSearchHighlight();
    } else if (e.key === 'Escape') {
        closeSearch();
    }
});

document.getElementById('search-prev').addEventListener('click', function() {
    if (searchMatches.length === 0) return;
    searchIdx = (searchIdx - 1 + searchMatches.length) % searchMatches.length;
    updateSearchHighlight();
});
document.getElementById('search-next').addEventListener('click', function() {
    if (searchMatches.length === 0) return;
    searchIdx = (searchIdx + 1) % searchMatches.length;
    updateSearchHighlight();
});
document.getElementById('search-close').addEventListener('click', closeSearch);

// ─── INITIAL RENDER ───
renderGraph();
</script>
</body>
</html>
"""
