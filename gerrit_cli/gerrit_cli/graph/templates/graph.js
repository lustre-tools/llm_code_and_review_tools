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

        // Separate-series border: applied per-node. A separate-group
        // node that the main upward walk actually reached (activeUp)
        // is a "bridge" visually stitched into the main tree and
        // renders without the border. All other separate-group nodes
        // — those positioned by the separate-groups fallback layout —
        // keep the distinctive grey border so viewers can tell them
        // apart from the main series at a glance.
        const isStandaloneSeparate = isSeparate && !isAbove;
        if (isStandaloneSeparate) {
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
            borderWidth: isAnchor ? 4 : (node.is_wip ? 3 : (isStandaloneSeparate ? 3 : (isMain ? 2 : 1))),
            opacity: opacity,
            // Custom data for click handler
            _isAnchor: isAnchor,
            _isMain: isMain,
        });
    }

    // Historical-parent suppression: a patch can have multiple
    // incoming edges because its first-parent changed across rebases.
    // Only the "current" dependency is usually meaningful, so by
    // default we keep one best incoming edge per child:
    //   - prefer a non-stale edge (the dependency still current)
    //   - otherwise the edge with the highest parent_patchset
    // Edges whose endpoints aren't visible in the current layout are
    // excluded from the ranking — if the truly-current parent isn't
    // rendered in this graph, we fall back to the most recent
    // historical parent that IS visible so the child doesn't end up
    // visually orphaned. A toggle restores the rest.
    // Build a per-child set of "kept" from-node ids (not a set of
    // edges, because G.edges can contain duplicate entries with the
    // same from→to pair — suppressing by string key would then match
    // the edge we meant to keep too). We then drop any edge whose
    // from-node is not in keptSources for that child.
    const showHistory = document.getElementById('chk-history').checked;
    const keptSources = {};  // child id -> Set<from id>
    if (!showHistory) {
        const byChild = {};
        for (const e of G.edges) {
            if (!positions[e.from] || !positions[e.to]) continue;
            // Dedupe by from-node so we rank each parent once.
            (byChild[e.to] = byChild[e.to] || {})[e.from] = e;
        }
        for (const child in byChild) {
            const uniq = Object.values(byChild[child]);
            uniq.sort((a, b) => {
                const sa = a.is_stale ? 1 : 0;
                const sb = b.is_stale ? 1 : 0;
                if (sa !== sb) return sa - sb;
                if (b.parent_patchset !== a.parent_patchset) {
                    return b.parent_patchset - a.parent_patchset;
                }
                if (b.parent_latest !== a.parent_latest) {
                    return b.parent_latest - a.parent_latest;
                }
                return a.from - b.from;
            });
            keptSources[child] = new Set([uniq[0].from]);
        }
    }

    // Build vis.js edges. Dedupe duplicate (from,to) pairs so a
    // single edge is drawn even if G.edges lists it twice.
    const drawnEdgeKeys = new Set();
    let edgeIdx = 0;
    for (const edge of G.edges) {
        if (!positions[edge.from] || !positions[edge.to]) continue;
        if (!showHistory) {
            const ks = keptSources[edge.to];
            if (ks && !ks.has(edge.from)) continue;
        }
        const key = edge.from + '->' + edge.to;
        if (drawnEdgeKeys.has(key)) continue;
        drawnEdgeKeys.add(key);

        const isMainEdge = mainChain.has(edge.from) && mainChain.has(edge.to);
        // "Base" = edge leads into historical chain below the anchor.
        // Separate-group nodes aren't part of the base chain even if
        // they weren't reached by the upward walk — they're positioned
        // via the separate-groups layout and should render in the
        // normal palette, not the dim one.
        const toNode = nodeMap[edge.to];
        const toSeparate = toNode && (toNode.series_group || 0) > 0;
        const isBase = !activeUp.has(edge.to) && !toSeparate;

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
            // Non-stale edges that aren't on the main chain (separate
            // series, side branches, cross-group links) still
            // represent a real current dependency — render them in
            // the main edge color, just thinner so the dominant chain
            // still stands out.
            color = C.edgeMain;
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
document.getElementById('chk-history').addEventListener('change', onFilterChange);
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
