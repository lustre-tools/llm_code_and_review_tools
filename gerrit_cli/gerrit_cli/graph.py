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
    }


def _extract_ci_links(
    messages: list[dict[str, Any]], patchset: int
) -> dict[str, str]:
    """Extract Jenkins build URL and Maloo results URL from change messages.

    Only looks at messages for the given patchset number.
    """
    import re

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


def build_graph(
    client: GerritCommentsClient,
    change_number: int,
    base_url: str,
    progress: bool = True,
    fetch_details: bool = True,
) -> dict[str, Any]:
    """Build the full series graph with stale branch information.

    Args:
        fetch_details: If True, fetch CI links and comments from change
            messages (slower, requires extra API calls). If False, skip
            message fetching for faster graph generation.

    Returns a dict ready to be embedded as JSON in the HTML template.
    """
    # 1. Fetch related changes
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

        # Extract ticket from subject
        import re
        subject = ci.get("subject", "")
        ticket_match = re.match(r"(LU-\d+)", subject)
        ticket = ticket_match.group(1) if ticket_match else ""

        nodes[cn] = {
            "id": cn,
            "subject": subject,
            "status": status,
            "current_patchset": latest,
            "author": author_info.get("name", "Unknown"),
            "url": f"{base_url}/c/fs/lustre-release/+/{cn}",
            "ticket": ticket,
        }
        commit_to_cn[commit_hash] = cn
        raw_entries.append({
            "cn": cn,
            "commit": commit_hash,
            "parent_commit": parent_hash,
            "ps": ps,
            "latest": latest,
        })

    # 3. Fetch ALL_REVISIONS in batches to build commit -> (change, patchset) map
    all_cns = sorted(nodes.keys())
    commit_to_change_ps: dict[str, tuple[int, int]] = {}
    batch_size = 50
    batches = [all_cns[i:i + batch_size] for i in range(0, len(all_cns), batch_size)]

    if progress:
        print(f"Fetching revision history ({len(all_cns)} changes)...",
              end="", file=sys.stderr, flush=True)

    labels_by_cn: dict[int, dict[str, Any]] = {}  # change_number -> review info

    for batch_idx, batch in enumerate(batches):
        query = " OR ".join(f"change:{cn}" for cn in batch)
        try:
            result = client.rest.get(
                f"/changes/?q={quote(query, safe=':+ ')}"
                f"&o=ALL_REVISIONS&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS&n=500"
            )
            for change in result:
                cn = change.get("_number", 0)
                for rev_hash, rev_info in change.get("revisions", {}).items():
                    ps = rev_info.get("_number", 0)
                    commit_to_change_ps[rev_hash] = (cn, ps)
                # Parse labels into compact review info
                labels_by_cn[cn] = _parse_labels(change.get("labels", {}))
        except Exception as e:
            if progress:
                print(f" (batch {batch_idx} error: {e})", end="",
                      file=sys.stderr, flush=True)

    if progress:
        print(f" {len(commit_to_change_ps)} commits mapped.", file=sys.stderr)

    # 3b. Attach review info to nodes
    for cn, node in nodes.items():
        node["review"] = labels_by_cn.get(cn, _empty_review())

    # 3c. Fetch messages for non-abandoned changes to extract Jenkins/Maloo links
    active_cns = sorted(
        cn for cn, node in nodes.items() if node["status"] != "ABANDONED"
    )
    if fetch_details and active_cns:
        if progress:
            print(f"Fetching CI links ({len(active_cns)} active changes)...",
                  end="", file=sys.stderr, flush=True)
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
        if progress:
            print(" done.", file=sys.stderr)

    # 4. Build edges by resolving parent commits
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[int, int]] = set()

    for entry in raw_entries:
        child_cn = entry["cn"]
        parent_commit = entry["parent_commit"]
        if not parent_commit:
            continue

        # Look up which change/patchset the parent commit belongs to
        if parent_commit in commit_to_change_ps:
            parent_cn, parent_ps = commit_to_change_ps[parent_commit]
            if parent_cn not in nodes:
                continue  # Parent is outside our related set
            if parent_cn == child_cn:
                continue  # Self-reference

            edge_key = (parent_cn, child_cn)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)

            parent_latest = nodes[parent_cn]["current_patchset"]
            edges.append({
                "from": parent_cn,
                "to": child_cn,
                "parent_patchset": parent_ps,
                "parent_latest": parent_latest,
                "is_stale": parent_ps < parent_latest,
            })

    # 5. Stats
    status_counts: dict[str, int] = {}
    for n in nodes.values():
        s = n["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    stale_edges = sum(1 for e in edges if e["is_stale"])
    tickets = sorted(set(n["ticket"] for n in nodes.values() if n["ticket"]))

    return {
        "anchor": change_number,
        "base_url": base_url,
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "status_counts": status_counts,
            "stale_edge_count": stale_edges,
            "tickets": tickets,
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
}
.topbar h1 { font-size: 15px; color: var(--accent); white-space: nowrap; }
.stats { display: flex; gap: 10px; font-size: 12px; }
.badge {
    padding: 2px 8px; border-radius: 10px; font-weight: 600; font-size: 11px;
}
.badge-new { background: #1f6feb; color: #fff; }
.badge-merged { background: #238636; color: #fff; }
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
.controls button:hover { background: var(--bg-hover); border-color: var(--accent); }
.controls button.primary {
    background: #1f6feb; color: #fff; border-color: #1f6feb;
}
.controls button.primary:hover { background: #388bfd; }

.legend {
    display: flex; gap: 10px; font-size: 11px; color: var(--text-muted);
    margin-left: auto;
}
.legend-item { display: flex; align-items: center; gap: 3px; }
.legend-dot {
    width: 10px; height: 10px; border-radius: 2px; display: inline-block;
}

.main { display: flex; flex: 1; overflow: hidden; }

#graph { flex: 1; background: var(--bg); }

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
        <div class="legend-item"><span class="legend-dot" style="background:#f85149"></span> Anchor</div>
        <div class="legend-item"><span class="legend-dot" style="background:#1f6feb"></span> NEW</div>
        <div class="legend-item"><span class="legend-dot" style="background:#238636"></span> MERGED</div>
        <div class="legend-item"><span class="legend-dot" style="background:#484f58"></span> ABANDONED</div>
        <div class="legend-item"><span class="legend-dot" style="background:#d29922"></span> Stale edge</div>
    </div>
</div>

<div class="controls">
    <label><input type="checkbox" id="chk-abandoned"> Show abandoned</label>
    <label><input type="checkbox" id="chk-stale" checked> Show stale branches</label>
    <button class="primary" id="btn-reset">Reset</button>
    <button id="btn-fit">Fit</button>
    <button id="btn-focus">Focus</button>
    <button id="btn-panel">Panel</button>
    <button id="btn-theme">Light</button>
</div>

<div class="main">
    <div id="graph"></div>
    <div class="panel" id="panel">
        <div class="panel-drag" id="panel-drag"></div>
        <h2>Node Details</h2>
        <div id="info">
            <p style="color:#8b949e">Click a node to see details and its chain.<br><br>
            Double-click to open in Gerrit.<br><br>
            <b>Keyboard:</b> F = fit, Z = focus, R = reset</p>
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

// ─── STATE ───
let currentAnchor = ANCHOR_INIT;
let mainChain = new Set();
let selectedNodeId = null;

// ─── MAIN CHAIN COMPUTATION ───
function computeMainChain(anchorId) {
    const chain = new Set();
    chain.add(anchorId);

    // Walk upward: pick best child at each step
    let cursor = anchorId;
    while (true) {
        const kids = (childrenOf[cursor] || []).filter(id => {
            const n = nodeMap[id];
            return n && n.status !== 'ABANDONED';
        });
        if (kids.length === 0) break;
        // Prefer non-stale edge, then most descendants
        kids.sort((a, b) => {
            const ea = edgeMap[cursor + '->' + a];
            const eb = edgeMap[cursor + '->' + b];
            const sa = ea ? (ea.is_stale ? 1 : 0) : 0;
            const sb = eb ? (eb.is_stale ? 1 : 0) : 0;
            if (sa !== sb) return sa - sb;
            return countDesc(b) - countDesc(a);
        });
        chain.add(kids[0]);
        cursor = kids[0];
    }

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
    (childrenOf[id] || []).forEach(c => {
        if (nodeMap[c]) { count += 1 + countDesc(c); }
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
    const showStale = document.getElementById('chk-stale').checked;

    function isVisible(id) {
        const n = nodeMap[id];
        if (!n) return false;
        if (n.status === 'ABANDONED' && !showAbandoned) return false;
        return true;
    }

    // Should this node be shown? It's visible if:
    // - it passes the filter
    // - OR it's the anchor
    // - OR it's on the main chain
    // For stale branches: show if showStale OR on main chain
    function shouldShow(id) {
        if (id == anchorId) return true;
        if (!isVisible(id)) return false;
        // Check if the edge TO this node is stale
        const eTo = edgesTo[id];
        if (eTo && eTo.length > 0 && eTo[0].is_stale && !showStale) {
            // This node is reached via a stale edge
            // Only show if it's on the main chain
            return mainChain.has(id);
        }
        return true;
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

        // Separate main chain child from side branches
        const mainKid = kids.find(k => mainChain.has(k));
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
            MERGED:    { bg: '#238636', border: '#3fb950', font: '#fff' },
            ABANDONED: light
                ? { bg: '#afb8c1', border: '#8b949e', font: '#24292f' }
                : { bg: '#30363d', border: '#484f58', font: '#8b949e' },
        },
        // Review health: overrides STATUS.NEW color for active patches
        REVIEW_GOOD: { bg: '#238636', border: '#3fb950', font: '#fff' },
        REVIEW_BAD:  { bg: '#b62324', border: '#f85149', font: '#fff' },
        ANCHOR: { bg: '#f85149', border: '#ff7b72', font: '#fff' },
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
// Returns 'good', 'bad', or 'pending' based on review state.
// Good: all verified +1 (no -1s) AND at least 2 non-author CR +1s
// Bad: any verified -1 OR any CR vote <= -1 (veto)
// Pending: everything else (undecided)
function reviewHealth(node) {
    if (node.status !== 'NEW') return 'pending';
    const rv = node.review || {};

    // Bad: any verified failure or CR veto
    if (rv.verified_fail) return 'bad';
    if (rv.cr_veto) return 'bad';

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
    const showStale = document.getElementById('chk-stale').checked;

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
        const isBase = !isAbove && !isAnchor;

        const C = getColors();
        let colors;
        if (isAnchor) {
            // Anchor gets review health coloring too
            const health = reviewHealth(node);
            if (health === 'bad') colors = C.REVIEW_BAD;
            else if (health === 'good') colors = C.REVIEW_GOOD;
            else colors = C.ANCHOR;
        } else if (isBase) {
            colors = C.DIM;
        } else if (node.status === 'NEW') {
            const health = reviewHealth(node);
            if (health === 'bad') colors = C.REVIEW_BAD;
            else if (health === 'good') colors = C.REVIEW_GOOD;
            else colors = C.STATUS.NEW;
        } else {
            colors = C.STATUS[node.status] || C.STATUS.NEW;
        }

        // If not on main chain and above anchor, slightly dim
        const opacity = (isAbove && !isMain && !isAnchor) ? 0.7 : 1.0;

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
            reviewLine = `\n${vStr}| CR: ${crStr}`;
        }

        let label = `#${node.id}\n${shortSubject}${reviewLine}`;

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
            borderWidth: isAnchor ? 4 : (isMain ? 2 : 1),
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
    const healthBadge = health === 'good'
        ? '<span style="color:#3fb950;font-weight:700">\u2713 Ready</span>'
        : health === 'bad'
        ? '<span style="color:#f85149;font-weight:700">\u2717 Issues</span>'
        : '<span style="color:#8b949e">Pending</span>';

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
    </div>`;
}

// ─── INFO PANEL ───
function showNodeInfo(id) {
    const node = nodeMap[id];
    if (!node) return;
    const panel = document.getElementById('info');

    // Respect current filter state
    const showAbandoned = document.getElementById('chk-abandoned').checked;
    const showStale = document.getElementById('chk-stale').checked;
    function isListVisible(nid) {
        const n = nodeMap[nid];
        if (!n) return false;
        if (n.status === 'ABANDONED' && !showAbandoned) return false;
        const eTo = edgesTo[nid];
        if (eTo && eTo.length > 0 && eTo[0].is_stale && !showStale && !mainChain.has(nid)) return false;
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
                ${staleTag}
                &nbsp; ps${node.current_patchset}
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
        <div class="field">
            <div class="fl">Ticket</div>
            <div class="fv">${node.ticket || '—'}</div>
        </div>
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
        <b>Keyboard:</b> F = fit, Z = focus, R = reset</p>`;
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
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

// ─── CONTROLS ───
function onFilterChange() {
    renderGraph();
    if (selectedNodeId !== null) { showNodeInfo(selectedNodeId); }
}
document.getElementById('chk-abandoned').addEventListener('change', onFilterChange);
document.getElementById('chk-stale').addEventListener('change', onFilterChange);
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
document.getElementById('btn-panel').addEventListener('click', function() {
    document.getElementById('panel').classList.toggle('hidden');
    setTimeout(() => network.redraw(), 100);
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
    } else if (e.key === 'Escape') {
        network.unselectAll();
        showDefaultInfo();
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

// ─── INITIAL RENDER ───
renderGraph();
</script>
</body>
</html>
"""
