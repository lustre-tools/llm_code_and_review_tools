// ─── DATA ───
const G = __GRAPH_DATA__;

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
// In-flight badge breaks down NEW changes by review health so a
// single glance tells you "X ready to land, Y blocked on Maloo,
// …" without having to scan the graph node-by-node.
const sc = G.stats.status_counts;
const inflight = sc.NEW || 0;
const health = { ready: 0, pending: 0, veto: 0, maloo: 0, jenkins: 0, other: 0 };
for (const n of G.nodes) {
    if (n.status !== 'NEW') continue;
    const h = reviewHealth(n);
    if      (h === 'good')        health.ready++;
    else if (h === 'bad_veto')    health.veto++;
    else if (h === 'bad_maloo')   health.maloo++;
    else if (h === 'bad_jenkins') health.jenkins++;
    else if (h === 'bad_other')   health.other++;
    else                          health.pending++;
}
// Render the in-flight breakdown as a row of small "chips" — a
// colored bullet (matching the legend dot for that health) plus
// count + label. Sits OUTSIDE the blue In-flight badge so the
// badge stays visually self-contained.
const healthChips = [];
const chip = (n, label, color) => {
    if (!n) return;
    healthChips.push(
        '<span class="health-chip" title="' + n + ' ' + label + '">'
      + '<span class="health-dot" style="background:' + color + '"></span>'
      + '<b>' + n + '</b> ' + label
      + '</span>'
    );
};
chip(health.ready,   'ready',   '#3fb950');
chip(health.pending, 'pending', '#58a6ff');
chip(health.veto,    'CR veto', '#f85149');
chip(health.maloo,   'Maloo',   '#f85149');
chip(health.jenkins, 'Jenkins', '#e8a020');
chip(health.other,   'other -1','#d63384');
const breakdown = healthChips.length
    ? '<span class="health-breakdown">' + healthChips.join('') + '</span>'
    : '';

document.getElementById('stats').innerHTML =
    `<span class="badge badge-new">In-flight: ${inflight}</span>`
  + breakdown
  + `<span class="badge badge-merged">MERGED: ${sc.MERGED || 0}</span>`
  + `<span class="badge badge-abandoned">ABANDONED: ${sc.ABANDONED || 0}</span>`
  + `<span style="color:#8b949e;font-size:11px">${G.stats.node_count} changes</span>`;

if (G.generated_at) {
    document.getElementById('generated-at').textContent = 'Generated: ' + G.generated_at;
}

// Set the browser tab title and the topbar h1 from G.name when the
// build was invoked with --name; otherwise fall back to the change
// number. Both are updated again in renderGraph(); we set them here
// too so they're correct before the first render.
if (G.name) {
    document.title = G.name;
    document.getElementById('title').textContent = G.name;
} else {
    document.title = `Series Graph — #${G.anchor}`;
    document.getElementById('title').textContent = `Series Graph — #${G.anchor}`;
}

// Build the legend from the palette. Re-rendered on theme toggle.
renderLegend();

// ─── STATE ───
// The anchor is fixed for the lifetime of the page. The underlying
// data was fetched against G.anchor, so re-centering the layout on
// a different node would show a partial view of a different series
// — confusing more than helpful. Focus (Z) handles the "just center
// this node on screen" case without touching layout.
const currentAnchor = G.anchor;
let mainChain = new Set();
// Nodes placed as historical base-chain context (below the anchor
// in the linear parentOf walk). renderGraph dims these.
let baseChainSet = new Set();
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

// ─── VISIBILITY / TRAVERSAL PRIMITIVES ───
// Single source of truth for "is this node visible right now?".
// Abandoned nodes are hidden unless the "Show abandoned" toggle is on
// OR the node bridges active patches in the main chain. Every
// consumer — layout, info panel, traversal filters — goes through
// here so changing the rule is a one-line edit.
function showAbandonedEnabled() {
    return document.getElementById('chk-abandoned').checked;
}

// "Show merged" is checked by default. The toggle is applied at
// render time only — layout, edges, and traversal all keep merged
// nodes in scope so an in-flight node sitting on top of a merged
// predecessor stays correctly positioned and connected when the
// merged node is just visually elided.
function showMergedEnabled() {
    return document.getElementById('chk-merged').checked;
}

function nodeVisible(id) {
    const n = nodeMap[id];
    if (!n) return false;
    if (n.status === 'ABANDONED' && !showAbandonedEnabled() && !mainChain.has(id)) {
        return false;
    }
    return true;
}

// Return the in-series-group children of `id`. Every traversal that
// computes "descendants" for main-chain selection must stay inside
// the starting node's group — cross-group stale edges exist as
// visual hints only and shouldn't inflate descendant counts.
function childrenInGroup(id) {
    const myGroup = _groupOf(id);
    const out = [];
    for (const k of (childrenOf[id] || [])) {
        const kn = nodeMap[k];
        if (!kn) continue;
        if ((kn.series_group || 0) !== myGroup) continue;
        out.push(k);
    }
    return out;
}

const activeDescCache = {};
function hasActiveDescendant(id) {
    if (activeDescCache[id] !== undefined) return activeDescCache[id];
    const n = nodeMap[id];
    if (!n) { activeDescCache[id] = false; return false; }
    if (n.status !== 'ABANDONED') { activeDescCache[id] = true; return true; }
    for (const k of childrenInGroup(id)) {
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

    // Walk upward: pick best child at each step. Do NOT filter out abandoned
    // children — we want to walk through abandoned patches if there are still
    // active patches above them. Trailing abandoned tails are trimmed below.
    // childrenInGroup naturally bounds the walk to the anchor's series
    // group so cross-group stale edges don't derail it.
    let cursor = anchorId;
    const upward = [];
    const seen = new Set([anchorId]);
    while (true) {
        const kids = childrenInGroup(cursor).filter(k => !seen.has(k));
        if (kids.length === 0) break;
        // Ranking for the main-chain walker:
        //   1. Has an active descendant (we never climb into dead
        //      abandoned subtrees).
        //   2. Quality class: a branch whose entry edge is non-stale
        //      AND has at least one descendant is the "current live"
        //      chain (class 0). A stale entry edge, or any branch
        //      with no descendants, is class 1 (historical / dead
        //      end). Class 0 always wins over class 1.
        //   3. Within the same class, prefer more descendants.
        //   4. Final tiebreaker: non-stale edge.
        // This lets the walker follow the current commit graph
        // whenever a live branch exists, but still reach a long
        // "all stale" chain when every current option is a dead end
        // (e.g. the 38305 case where every entry edge is stale).
        kids.sort((a, b) => {
            const ha = hasActiveDescendant(a) ? 0 : 1;
            const hb = hasActiveDescendant(b) ? 0 : 1;
            if (ha !== hb) return ha - hb;

            const ea = edgeMap[cursor + '->' + a];
            const eb = edgeMap[cursor + '->' + b];
            const staleA = ea && ea.is_stale ? 1 : 0;
            const staleB = eb && eb.is_stale ? 1 : 0;
            const descA = countDesc(a);
            const descB = countDesc(b);

            const classA = (staleA === 0 && descA > 0) ? 0 : 1;
            const classB = (staleB === 0 && descB > 0) ? 0 : 1;
            if (classA !== classB) return classA - classB;

            if (descA !== descB) return descB - descA;
            return staleA - staleB;
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
    for (const c of childrenInGroup(id)) {
        count += 1 + countDesc(c);
    }
    descCache[id] = count;
    return count;
}

// ─── TREE LAYOUT ───
//
// The layout pipeline is split across several small top-level helpers
// that operate on a shared "layout context" object. Each helper does
// one geometric job: caches, recursive tree placement, the upward
// walk from the anchor, the downward base chain, separate-series
// fallback, and the final collision pass. `computeLayout` is the
// orchestrator that creates the context and runs the phases in
// order.
//
// The layout context shape:
//   ctx = {
//     anchorId,     // the current anchor
//     positions,    // id -> { x, y }, mutated by each phase
//     widthCache,   // memoized subtree width
//     heightCache,  // memoized subtree height
//   }
// `mainChain` is module-level so it's available to styling too.

const LEVEL_H = 140;
const NODE_W = 380;

function _layoutShouldShow(ctx, id) {
    if (id == ctx.anchorId) return true;
    return nodeVisible(id);
}

// Subtree width = number of visible leaf descendants. Memoized per
// layout context so repeated queries from the tree-placement phases
// don't re-walk the same subtrees.
function _subtreeWidth(ctx, id) {
    if (ctx.widthCache[id] !== undefined) return ctx.widthCache[id];
    const kids = (childrenOf[id] || []).filter(k => _layoutShouldShow(ctx, k));
    if (kids.length === 0) { ctx.widthCache[id] = 1; return 1; }
    let w = 0;
    for (const k of kids) w += _subtreeWidth(ctx, k);
    ctx.widthCache[id] = w;
    return w;
}

// Subtree height = max depth from `id` to any visible leaf.
function _subtreeHeight(ctx, id) {
    if (ctx.heightCache[id] !== undefined) return ctx.heightCache[id];
    const kids = (childrenOf[id] || []).filter(k => _layoutShouldShow(ctx, k));
    if (kids.length === 0) { ctx.heightCache[id] = 1; return 1; }
    let maxH = 0;
    for (const k of kids) maxH = Math.max(maxH, _subtreeHeight(ctx, k));
    ctx.heightCache[id] = maxH + 1;
    return maxH + 1;
}

// Recursively place a subtree rooted at `id`. `dir` is +1 when
// children grow up (negative y) and -1 when they grow down. Returns
// the outermost level used by the subtree so callers can chain
// placements without overlap.
function _layoutTree(ctx, id, x, level, dir) {
    const positions = ctx.positions;
    if (positions[id]) return level;
    positions[id] = { x, y: -level * LEVEL_H };

    const kids = (childrenOf[id] || [])
        .filter(k => _layoutShouldShow(ctx, k))
        .filter(k => !positions[k]);

    if (kids.length === 0) return level;

    // Pick the child that continues straight up. Same quality-
    // class rule as computeMainChain: a non-stale child with any
    // descendants (class 0) always beats a stale or dead-end one
    // (class 1); within a class, prefer more descendants. The
    // global main chain is used as a tie-break so coordinated main-
    // chain nodes line up exactly, but the ranking is applied to
    // all children so that off-main subtrees still grow up along
    // their own current branch.
    let mainKid = null;
    if (kids.length > 0) {
        const sorted = kids.slice().sort((a, b) => {
            const ma = mainChain.has(a) ? 0 : 1;
            const mb = mainChain.has(b) ? 0 : 1;

            const ea = edgeMap[id + '->' + a];
            const eb = edgeMap[id + '->' + b];
            const staleA = ea && ea.is_stale ? 1 : 0;
            const staleB = eb && eb.is_stale ? 1 : 0;
            const descA = countDesc(a);
            const descB = countDesc(b);

            const classA = (staleA === 0 && descA > 0) ? 0 : 1;
            const classB = (staleB === 0 && descB > 0) ? 0 : 1;
            if (classA !== classB) return classA - classB;

            if (ma !== mb) return ma - mb;
            if (descA !== descB) return descB - descA;
            return staleA - staleB;
        });
        mainKid = sorted[0];
    }
    const sideKids = kids.filter(k => k !== mainKid).sort((a, b) => a - b);

    if (kids.length === 1) {
        return _layoutTree(ctx, kids[0], x, level + dir, dir);
    }

    // Place side branches first, alternating left and right.
    const leftKids = [];
    const rightKids = [];
    for (let i = 0; i < sideKids.length; i++) {
        (i % 2 === 0 ? leftKids : rightKids).push(sideKids[i]);
    }

    let extremeSideLevel = level;
    const updateExtreme = (l) => {
        extremeSideLevel = dir > 0
            ? Math.max(extremeSideLevel, l)
            : Math.min(extremeSideLevel, l);
    };

    let leftX = x - NODE_W;
    for (const kid of leftKids) {
        const w = _subtreeWidth(ctx, kid);
        const top = _layoutTree(
            ctx, kid, leftX - (w - 1) * NODE_W / 2, level + dir, dir
        );
        updateExtreme(top);
        leftX -= w * NODE_W;
    }

    let rightX = x + NODE_W;
    for (const kid of rightKids) {
        const w = _subtreeWidth(ctx, kid);
        const top = _layoutTree(
            ctx, kid, rightX + (w - 1) * NODE_W / 2, level + dir, dir
        );
        updateExtreme(top);
        rightX += w * NODE_W;
    }

    // Place main-chain child past the outermost side branch so
    // nothing overlaps with it.
    if (mainKid) {
        const mainLevel = extremeSideLevel + dir;
        return _layoutTree(ctx, mainKid, x, mainLevel, dir);
    }
    return extremeSideLevel;
}

// Step 1: place the anchor and everything reachable from it via
// childrenOf (growing upward, negative y).
function _layoutUpwardFromAnchor(ctx) {
    const positions = ctx.positions;
    positions[ctx.anchorId] = { x: 0, y: 0 };

    const upKids = (childrenOf[ctx.anchorId] || [])
        .filter(k => _layoutShouldShow(ctx, k))
        .filter(k => !positions[k]);
    if (upKids.length === 0) return;

    // layoutTree re-positions the anchor at the same coords when
    // called with positions cleared, so clear → call → it's back.
    delete positions[ctx.anchorId];
    _layoutTree(ctx, ctx.anchorId, 0, 0, 1);
}

// Step 2: place the base chain below the anchor. Each base node is
// pushed far enough down that its upward side branches don't
// overlap the previous node's area. Mirrors the upward layout logic
// where the main-chain child is pushed past side branches. Every
// node placed by this helper is recorded in the module-level
// baseChainSet so renderGraph can dim only real base-chain history,
// not e.g. unreachable ancestors glued in by the fallback layout.
function _layoutBaseChain(ctx) {
    const positions = ctx.positions;
    let cursor = parentOf[ctx.anchorId];
    let prevNodeLevel = 0; // anchor is at level 0

    while (cursor && nodeMap[cursor] && !positions[cursor]) {
        if (!_layoutShouldShow(ctx, cursor) && cursor != ctx.anchorId) {
            cursor = parentOf[cursor];
            continue;
        }
        baseChainSet.add(cursor);

        // Pre-compute how tall this node's side branches will be so
        // the node can be placed far enough below the previous one
        // that the branches (which grow UP branchH levels) don't
        // overlap.
        const sideKids = (childrenOf[cursor] || [])
            .filter(k => _layoutShouldShow(ctx, k))
            .filter(k => k != ctx.anchorId && !mainChain.has(k));
        let branchH = 0;
        for (const sk of sideKids) {
            branchH = Math.max(branchH, _subtreeHeight(ctx, sk));
        }

        const nodeLevel = prevNodeLevel - Math.max(1, branchH + 1);
        positions[cursor] = { x: 0, y: -nodeLevel * LEVEL_H };

        // Lay out the side branches growing upward from this base node.
        if (sideKids.length > 0) {
            const kids = sideKids.filter(k => !positions[k]);
            kids.sort((a, b) => a - b);
            let leftX = -NODE_W;
            let rightX = NODE_W;
            for (let bi = 0; bi < kids.length; bi++) {
                const bk = kids[bi];
                const w = _subtreeWidth(ctx, bk);
                if (bi % 2 === 0) {
                    _layoutTree(
                        ctx, bk, rightX + (w - 1) * NODE_W / 2,
                        nodeLevel + 1, 1
                    );
                    rightX += w * NODE_W;
                } else {
                    _layoutTree(
                        ctx, bk, leftX - (w - 1) * NODE_W / 2,
                        nodeLevel + 1, 1
                    );
                    leftX -= w * NODE_W;
                }
            }
        }

        prevNodeLevel = nodeLevel;
        cursor = parentOf[cursor];
    }
}

// Step 3: lay out separate-series groups as forward-walked chains.
//
// Python provides G.separate_chains: a list of chains, each chain
// being a list of cns ordered OLDEST → NEWEST. The chain is built
// by starting at the oldest unused separate-group node and walking
// forward: for each candidate, look at its NEWEST patchset's
// parent commit hash and ask "is that hash any patchset of the
// current cursor?" If yes, that candidate follows the cursor.
// Continue until nothing follows. New chain starts from the next
// oldest unused node.
//
// Layout: each chain is a single vertical column, oldest at the
// bottom, newest at the top. If the chain has a cross-group edge
// to a placed main node, the column is anchored DIRECTLY ABOVE
// that node so the dependency relationship is visually obvious.
// Otherwise the column sits in a far-right shelf.
//
// Single-element chains (no following patches found) are placed
// the same way — each gets its own column.
function _layoutSeparateGroups(ctx) {
    const positions = ctx.positions;
    const chains = G.separate_chains || [];
    if (chains.length === 0) {
        _layoutSeparateFixup(ctx);
        return;
    }

    // Classify each chain: anchored (has a placed main neighbor)
    // vs disconnected (no cross-group edge to anything placed).
    const anchored = [];     // { chain, anchor }
    const disconnected = []; // chain
    for (const rawChain of chains) {
        const chain = rawChain.filter(id => nodeVisible(id));
        if (chain.length === 0) continue;
        // Skip if any member was already placed by a previous phase.
        if (chain.some(id => positions[id] !== undefined)) continue;
        const anchor = _findChainAnchor(chain, positions);
        if (anchor) anchored.push({ chain, anchor });
        else disconnected.push(chain);
    }

    // Anchored chains: sort by anchor x so they place left-to-right
    // in spatial order. Monotonic column allocator avoids overlap.
    anchored.sort((a, b) => {
        if (a.anchor.x !== b.anchor.x) return a.anchor.x - b.anchor.x;
        return a.chain[0] - b.chain[0];
    });
    let lastRight = -Infinity;
    for (const item of anchored) {
        const baseX = Math.max(item.anchor.x, lastRight + NODE_W);
        const baseY = item.anchor.y - LEVEL_H;
        _placeChainColumn(ctx, item.chain, baseX, baseY);
        lastRight = Math.max(lastRight, baseX);
    }

    // Disconnected chains: far-right shelf starting past the main
    // tree and any anchored columns we just placed.
    let mainMaxX = 0;
    for (const pos of Object.values(positions)) {
        mainMaxX = Math.max(mainMaxX, pos.x);
    }
    let shelfX = Math.max(mainMaxX, lastRight) + NODE_W * 2;
    for (const chain of disconnected) {
        _placeChainColumn(ctx, chain, shelfX, 0);
        shelfX += NODE_W;
    }

    _layoutSeparateFixup(ctx);
}

// Find a placed main-tree neighbor for any node in the chain.
// Prefers non-stale edges (live dependencies are stronger context)
// and the rightmost x among ties (so this chain lands past
// leftward-anchored chains naturally). Returns null if no chain
// member touches any placed node.
function _findChainAnchor(chain, positions) {
    const chainSet = new Set(chain);
    let best = null;
    for (const e of G.edges) {
        const fromIs = chainSet.has(e.from);
        const toIs = chainSet.has(e.to);
        if (fromIs === toIs) continue;
        const anchorId = fromIs ? e.to : e.from;
        const anchorPos = positions[anchorId];
        if (!anchorPos) continue;
        const cand = {
            x: anchorPos.x, y: anchorPos.y,
            isStale: !!e.is_stale,
        };
        if (!best) { best = cand; continue; }
        if (best.isStale !== cand.isStale) {
            if (!cand.isStale) best = cand;
            continue;
        }
        if (cand.x > best.x) best = cand;
    }
    return best;
}

// Place a chain (oldest → newest) as a single vertical column
// starting at (baseX, baseY) and growing upward.
function _placeChainColumn(ctx, chain, baseX, baseY) {
    const positions = ctx.positions;
    for (let i = 0; i < chain.length; i++) {
        positions[chain[i]] = { x: baseX, y: baseY - i * LEVEL_H };
    }
}

// Stragglers: any separate-group member not placed by the chain
// pass (e.g. cross-group edge already pulled it into the main
// tree, or it was filtered as hidden then revealed). Glue them
// near any placed neighbor.
function _layoutSeparateFixup(ctx) {
    const positions = ctx.positions;
    for (const node of G.nodes) {
        const id = node.id;
        if ((node.series_group || 0) === 0) continue;
        if (positions[id] !== undefined) continue;
        if (!nodeVisible(id)) continue;

        let neighborPos = null;
        let neighborDir = 0;
        for (const e of G.edges) {
            if (e.to === id && positions[e.from]) {
                neighborPos = positions[e.from];
                neighborDir = -1;  // child sits above parent
                break;
            }
            if (e.from === id && positions[e.to]) {
                neighborPos = positions[e.to];
                neighborDir = 1;  // parent sits below child
                break;
            }
        }
        if (!neighborPos) continue;
        let px = neighborPos.x;
        const py = neighborPos.y + neighborDir * LEVEL_H;
        let tries = 0;
        while (tries < 20) {
            let occupied = false;
            for (const p of Object.values(positions)) {
                if (Math.abs(p.x - px) < NODE_W * 0.9
                        && Math.abs(p.y - py) < LEVEL_H * 0.6) {
                    occupied = true;
                    break;
                }
            }
            if (!occupied) break;
            px += NODE_W;
            tries++;
        }
        positions[id] = { x: px, y: py };
    }
}

// Place main-series nodes that the upward walk + base chain never
// reached. These are typically ancestors that live on a
// non-direct parent branch or side nodes that only connect back to
// main via a stale edge from some non-anchor node. Treat them like
// a synthetic disconnected group: BFS from the set's own roots so
// oldest nodes sit at level 0 and children grow upward from there,
// matching how both the main tree and disconnected separate groups
// are laid out. The column starts at the far right of everything
// already placed.
function _layoutUnplacedMainSeries(ctx) {
    const positions = ctx.positions;
    const unplaced = new Set();
    for (const n of G.nodes) {
        if (positions[n.id] !== undefined) continue;
        if (!nodeVisible(n.id)) continue;
        if ((n.series_group || 0) !== 0) continue;
        unplaced.add(n.id);
    }
    if (unplaced.size === 0) return;

    // Build a child map restricted to the unplaced set so BFS stays
    // within it. Non-unplaced edges are ignored for the layout, but
    // the edges themselves still render normally (they'll fly over
    // from the main tree into the column).
    const parentIn = new Set();
    const childrenInSet = {};
    for (const id of unplaced) childrenInSet[id] = [];
    for (const e of G.edges) {
        if (!unplaced.has(e.from) || !unplaced.has(e.to)) continue;
        parentIn.add(e.to);
        childrenInSet[e.from].push(e.to);
    }

    // Roots = unplaced nodes with no parent *within the set*.
    const roots = [...unplaced].filter(id => !parentIn.has(id));
    const levels = {};
    const queue = [];
    for (const r of roots) {
        levels[r] = 0;
        queue.push(r);
    }
    while (queue.length > 0) {
        const n = queue.shift();
        for (const c of (childrenInSet[n] || [])) {
            if (!(c in levels)) {
                levels[c] = levels[n] + 1;
                queue.push(c);
            }
        }
    }
    // Any leftover (cycle remnant or fully-disconnected member)
    // gets level 0 so it still lands on the baseline.
    for (const id of unplaced) {
        if (!(id in levels)) levels[id] = 0;
    }

    // Arrange the column at the far right of everything placed so
    // far. Members at the same BFS level are spaced horizontally
    // instead of stacking so they don't overlap.
    let mainMaxX = 0;
    for (const pos of Object.values(positions)) {
        mainMaxX = Math.max(mainMaxX, pos.x);
    }
    const columnX = mainMaxX + NODE_W * 2;

    const levelBuckets = {};
    for (const id of unplaced) {
        (levelBuckets[levels[id]] = levelBuckets[levels[id]] || []).push(id);
    }
    for (const lv in levelBuckets) {
        const ids = levelBuckets[lv].sort((a, b) => a - b);
        for (let i = 0; i < ids.length; i++) {
            positions[ids[i]] = {
                x: columnX + i * NODE_W,
                y: -parseInt(lv) * LEVEL_H,
            };
        }
    }
}

// Step 4: any nodes that ended up at exactly the same (x, y) — e.g.
// because two fixup passes chose the same slot — get shifted right
// until they find an empty coordinate.
function _resolveCollisions(ctx) {
    const positions = ctx.positions;
    const occupied = new Map();
    for (const [idStr, pos] of Object.entries(positions)) {
        const key = pos.x + ',' + pos.y;
        if (!occupied.has(key)) {
            occupied.set(key, idStr);
            continue;
        }
        let px = pos.x + NODE_W;
        let tries = 0;
        while (tries < 30) {
            if (!occupied.has(px + ',' + pos.y)) break;
            px += NODE_W;
            tries++;
        }
        positions[idStr] = { x: px, y: pos.y };
        occupied.set(px + ',' + pos.y, idStr);
    }
}

// Orchestrator: build the context, compute the main chain, run each
// layout phase, and return the positions dict that renderGraph feeds
// into vis.js.
function computeLayout(anchorId) {
    mainChain = computeMainChain(anchorId);
    baseChainSet = new Set();
    const ctx = {
        anchorId,
        positions: {},
        widthCache: {},
        heightCache: {},
    };
    _layoutUpwardFromAnchor(ctx);
    _layoutBaseChain(ctx);
    // Unplaced-main BEFORE separate groups so the chain anchor
    // lookup can find every placed main neighbor (including main
    // ancestors that fell off the linear base chain).
    _layoutUnplacedMainSeries(ctx);
    _layoutSeparateGroups(ctx);
    _resolveCollisions(ctx);
    return ctx.positions;
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

// ─── LEGEND ───
// Rendered from the same palette as the graph so a color tweak in
// getColors() updates the legend automatically. Must be called
// whenever the palette might have changed (init + theme toggle).
function legendItems() {
    const C = getColors();
    return [
        { kind: 'group', label: 'Nodes' },
        { kind: 'fill', label: 'Ready',       color: C.REVIEW_GOOD.bg },
        { kind: 'fill', label: 'Pending',     color: C.STATUS.NEW.bg },
        { kind: 'fill', label: 'CR Veto',     color: C.REVIEW_BAD_VETO.bg },
        { kind: 'fill', label: 'Maloo',       color: C.REVIEW_BAD_MALOO.bg },
        { kind: 'fill', label: 'Jenkins',     color: C.REVIEW_BAD_JENKINS.bg },
        { kind: 'fill', label: 'Other -1',    color: C.REVIEW_BAD_OTHER.bg },
        { kind: 'fill', label: 'Merged',      color: C.STATUS.MERGED.bg },
        { kind: 'fill', label: 'Abandoned',   color: C.STATUS.ABANDONED.bg },
        { kind: 'border', label: '🚧 WIP',   color: '#c9d1d9', dashed: true },
        { kind: 'border', label: 'master-next (queued)', color: C.STATUS.MERGED.border },
        { kind: 'group', label: 'Edges', marginLeft: '8px' },
        { kind: 'fill', label: 'Stale',       color: C.edgeStale },
    ];
}

function renderLegend() {
    const container = document.getElementById('legend');
    if (!container) return;
    container.innerHTML = legendItems().map(item => {
        if (item.kind === 'group') {
            const ml = item.marginLeft ? `margin-left:${item.marginLeft}` : '';
            return `<span style="color:var(--text-muted);font-weight:600;${ml}">${item.label}:</span>`;
        }
        if (item.kind === 'border') {
            const style = item.dashed ? 'dashed' : 'solid';
            return `<div class="legend-item"><span class="legend-dot"`
                + ` style="background:transparent;border:2px ${style} ${item.color}"></span>`
                + ` ${item.label}</div>`;
        }
        // kind === 'fill'
        return `<div class="legend-item"><span class="legend-dot"`
            + ` style="background:${item.color}"></span> ${item.label}</div>`;
    }).join('');
}

// Automated Gerrit voters — their CR votes (typically -1 from CI
// or style checks) are tagged "(bot)" in the panel rather than
// "(reviewer)".
const BOT_VOTERS = new Set(['Lustre Gerrit Janitor', 'wc-checkpatch']);

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

    // Good: BOTH Jenkins +1 and Maloo +1 AND >= 2 CR +1s that
    // don't come from the change owner.
    //
    // verified_pass alone ("at least one +1, no -1") is not
    // sufficient: when a CI run simply doesn't fire, it leaves
    // no vote at all (not a -1), so a single Jenkins +1 with
    // Maloo silent would silently pass that check. A patch can
    // only land when both CIs have voted +1, so both must be
    // present before we paint the node green.
    //
    // CR side: only the OWNER's self-vote is excluded —
    // Gerrit's self-approval rule keys off the owner, not the
    // git author. A reviewer who happens to be the commit
    // author still counts. Fall back to author only if owner is
    // missing (node never enriched).
    if (rv.verified_pass) {
        const passers = new Set(
            (rv.verified_votes || [])
                .filter(v => v.value > 0)
                .map(v => v.name.toLowerCase())
        );
        if (!passers.has('jenkins') || !passers.has('maloo')) {
            return 'pending';
        }
        const owner = node.owner || node.author || '';
        const nonOwnerPlus = (rv.cr_votes || []).filter(
            v => v.value > 0 && v.name !== owner
        ).length;
        if (nonOwnerPlus >= 2) return 'good';
    }

    return 'pending';
}

// ─── STYLING ───
// Pure helpers that turn a node/edge + computed flags into the
// vis.js options object. Kept separate from renderGraph so visual
// tweaks live in one place.

// Node label: WIP prefix + #id + truncated subject + review line.
function nodeLabel(node) {
    const shortSubject = node.subject.length > 50
        ? node.subject.substring(0, 47) + '...'
        : node.subject;

    let reviewLine = '';
    if (node.status !== 'ABANDONED' && node.status !== 'MERGED') {
        const rv = node.review || {};

        // Verified summary: one token per voter.
        const vVotes = rv.verified_votes || [];
        let vStr = '';
        if (vVotes.length === 0) {
            vStr = 'V:- ';
        } else {
            vStr = vVotes.map(v => {
                let n = v.name;
                if (/jenkins/i.test(n)) n = 'J';
                else if (/maloo/i.test(n)) n = 'M';
                else n = n.split(' ')[0].substring(0, 6);
                return n + ':' + (v.value > 0 ? '\u2713' : '\u2717');
            }).join(' ') + ' ';
        }

        // CR summary.
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

        const cc = rv.unresolved_count || 0;
        const ccStr = cc > 0 ? ` | \u{1f4ac}${cc}` : '';
        reviewLine = `\n${vStr}| CR: ${crStr}${ccStr}`;
    }

    const wipPrefix = node.is_wip ? '\u{1f6a7} ' : '';
    return `${wipPrefix}#${node.id}\n${shortSubject}${reviewLine}`;
}

// Pick the base color palette for a node. Returns { bg, border, font }.
function nodeBaseColors(node, flags, C) {
    if (flags.isBase) return C.DIM;
    if (node.status === 'NEW') {
        const health = reviewHealth(node);
        if (health === 'bad_veto') return C.REVIEW_BAD_VETO;
        if (health === 'bad_maloo') return C.REVIEW_BAD_MALOO;
        if (health === 'bad_jenkins') return C.REVIEW_BAD_JENKINS;
        if (health === 'bad_other') return C.REVIEW_BAD_OTHER;
        if (health === 'good') return C.REVIEW_GOOD;
        return C.STATUS.NEW;
    }
    return C.STATUS[node.status] || C.STATUS.NEW;
}

// Full vis.js node options for a rendered node.
function styleForNode(node, flags, position, C) {
    let colors = nodeBaseColors(node, flags, C);

    // master-next override: a patch tagged "master-next" is queued
    // for the next master merge and is treated as effectively
    // merged. Apply only the MERGED border color (not the fill) so
    // its actual review state still shows through. Doesn't change
    // borderWidth or other geometry.
    if ((node.hashtags || []).includes('master-next')) {
        colors = Object.assign({}, colors, {
            border: C.STATUS.MERGED.border,
        });
    }

    // Non-main nodes above the anchor dim slightly. Separate-series
    // nodes are never dimmed — they render at full intensity.
    const opacity = (
        flags.isAbove && !flags.isMain && !flags.isAnchor && !flags.isSeparate
    ) ? 0.7 : 1.0;

    const borderWidth = flags.isAnchor
        ? 4
        : (node.is_wip
            ? 3
            : (flags.isMain ? 2 : 1));

    return {
        id: node.id,
        label: nodeLabel(node),
        x: position.x,
        y: position.y,
        fixed: { x: true, y: true },
        color: {
            background: colors.bg,
            border: colors.border,
            highlight: { background: C.HIGHLIGHT.bg, border: C.HIGHLIGHT.border },
        },
        font: { color: colors.font, size: 12, face: 'monospace' },
        // WIP nodes get a dashed border (vis.js native). Only attach
        // shapeProperties when WIP so non-WIP nodes use defaults.
        ...(node.is_wip ? { shapeProperties: { borderDashes: [6, 4] } } : {}),
        borderWidth: borderWidth,
        opacity: opacity,
        _isAnchor: flags.isAnchor,
        _isMain: flags.isMain,
    };
}

// Full vis.js edge options for a rendered edge.
function styleForEdge(edge, edgeId, flags, C) {
    let color;
    let width;
    let dashes;
    if (edge.is_stale) {
        color = C.edgeStale;
        width = 2;
        dashes = [8, 4];
    } else if (flags.isMainEdge) {
        color = C.edgeMain;
        width = 3;
        dashes = false;
    } else if (flags.isBase) {
        color = C.edgeDim;
        width = 1;
        dashes = false;
    } else {
        // Non-stale edges that aren't on the main chain (separate
        // series, side branches, cross-group links) still represent a
        // real current dependency — same color as main, just thinner
        // so the dominant chain still stands out.
        color = C.edgeMain;
        width = 1.5;
        dashes = false;
    }

    const label = edge.is_stale
        ? `ps${edge.parent_patchset}→${edge.parent_latest}`
        : `ps${edge.parent_patchset}`;

    return {
        id: 'e' + edgeId,
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
    };
}

// ─── RENDER HELPERS ───

// Nodes reachable from `anchor` by walking children that are also
// in `positions`. "Active subtree" — used by render to decide whether
// an edge points into base-chain history or into live descendants.
function computeActiveUp(positions, anchor) {
    const activeUp = new Set();
    const stack = [anchor];
    while (stack.length > 0) {
        const id = stack.pop();
        if (activeUp.has(id)) continue;
        activeUp.add(id);
        for (const c of (childrenOf[id] || [])) {
            if (positions[c]) stack.push(c);
        }
    }
    return activeUp;
}

// Historical-parent suppression: a patch can have multiple incoming
// edges because its first-parent changed across rebases. By default
// we keep one best incoming edge per child — non-stale first,
// otherwise highest parent_patchset. Edges whose endpoints aren't
// visible in the current layout are excluded from the ranking so the
// child doesn't become orphaned if the truly-current parent isn't
// rendered in this graph. Returns a {child id -> Set<from id>} map.
// Empty when "Show historical parents" is on — everything passes
// through unchanged.
function computeHistoricalSuppression(positions) {
    const keptSources = {};
    if (document.getElementById('chk-history').checked) return keptSources;

    const byChild = {};
    for (const e of G.edges) {
        if (!positions[e.from] || !positions[e.to]) continue;
        // Dedupe by from-node so each distinct parent is ranked once.
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
    return keptSources;
}

// ─── RENDER ───
function renderGraph() {
    const positions = computeLayout(currentAnchor);
    const activeUp = computeActiveUp(positions, currentAnchor);
    const keptSources = computeHistoricalSuppression(positions);
    const C = getColors();
    // Final render-time filter for the "Show merged" toggle. Layout
    // and edges are already computed against the full node set; we
    // just drop merged nodes and any edge touching them from the
    // datasets the user sees. In-flight chains that traversed a
    // merged predecessor keep their positions, so unhiding restores
    // the previous view exactly.
    const hideMerged = !showMergedEnabled();

    // Build vis.js nodes
    const visNodes = [];
    const visEdges = [];

    for (const [idStr, pos] of Object.entries(positions)) {
        const id = parseInt(idStr);
        const node = nodeMap[id];
        if (!node) continue;
        if (hideMerged && node.status === 'MERGED') continue;

        const isAnchor = id === currentAnchor;
        const isMain = mainChain.has(id);
        const isAbove = activeUp.has(id);
        // Any node in a non-zero series_group is a separate-series
        // member. Cross-group edges are informational only — they
        // don't make a separate series "part of" the main chain.
        const isSeparate = (node.series_group || 0) > 0;
        // Base chain = nodes actually placed by _layoutBaseChain
        // (the linear parentOf walk below the anchor). Any other
        // node outside activeUp — e.g. an unreachable ancestor glued
        // in by the fallback layout — is NOT base chain and should
        // render with its real status color, not dimmed.
        const isBase = baseChainSet.has(id);

        visNodes.push(styleForNode(node, {
            isAnchor, isMain, isAbove, isSeparate, isBase,
        }, pos, C));
    }

    // Build vis.js edges. G.edges is already deduped in the Python
    // builder, so each (from, to) pair appears at most once.
    let edgeIdx = 0;
    for (const edge of G.edges) {
        if (!positions[edge.from] || !positions[edge.to]) continue;
        const ks = keptSources[edge.to];
        if (ks && !ks.has(edge.from)) continue;
        if (hideMerged) {
            const fn = nodeMap[edge.from];
            const tn = nodeMap[edge.to];
            if ((fn && fn.status === 'MERGED')
                    || (tn && tn.status === 'MERGED')) continue;
        }

        const isMainEdge = mainChain.has(edge.from) && mainChain.has(edge.to);
        // "Base" = edge points INTO a historical base-chain node —
        // matches the node-side isBase check so edge and endpoint
        // colors agree.
        const isBase = baseChainSet.has(edge.to);

        visEdges.push(styleForEdge(edge, edgeIdx, { isMainEdge, isBase }, C));
        edgeIdx++;
    }

    // Update datasets
    nodesDS.clear();
    edgesDS.clear();
    nodesDS.add(visNodes);
    edgesDS.add(visEdges);

    // Update title. If the build was given an explicit --name, it
    // becomes the headline; otherwise fall back to the anchor change
    // number. Either way the anchor is still in the URL on every
    // node so it's never lost.
    document.getElementById('title').textContent = G.name
        ? G.name
        : `Series Graph — #${currentAnchor}`;

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
    const owner = node.owner || '';
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
            // Role tag: the owner's own vote is the only one that
            // doesn't count toward approval, so it's the only one
            // dimmed. The git author (when not the owner) and plain
            // reviewers are labelled too, but their votes count.
            // Automated voters (CI/style bots) are tagged "(bot)".
            const isOwner = owner && v.name === owner;
            const isAuthor = !isOwner && author && v.name === author;
            let role = 'reviewer';
            if (isOwner) role = 'owner';
            else if (isAuthor) role = 'author';
            else if (BOT_VOTERS.has(v.name)) role = 'bot';
            const roleTag = ` <span style="color:var(--text-muted);font-size:11px">(${role})</span>`;
            crHtml += `<div style="font-size:13px;margin:1px 0${isOwner ? ';opacity:0.6' : ''}">
                <span style="color:${color};font-weight:600">${sign}${v.value}</span>
                <span style="color:var(--text)">${esc(v.name)}</span>${roleTag}
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
    ${renderCommentsPanel(node)}
    ${renderPreviousReviewsPanel(node)}`;
}

// Pre-current-patchset Code-Review actions by human reviewers.
// We get the raw chronological event list from Python; here we
// reduce it to the latest action per voter and discard anyone
// whose latest action was a reset (value === 0) — they retracted
// their opinion, so they have no "previous review" still standing.
//
// Useful when an author keeps uploading new patchsets: shows what
// each reviewer thought of an earlier revision, even when the
// reviewer hasn't (yet) voted again on the current patchset.
//
// Always rendered (with a "None" placeholder when empty) so the
// section provides predictable visual separation from the Code
// Review block above. Vote rows render at lower opacity to make
// it obvious at a glance that these are NOT the current votes.
function renderPreviousReviewsPanel(node) {
    const rv = node.review || {};
    const history = rv.cr_history || [];

    // history is in chronological order; the LAST entry per voter
    // is their most recent action. Reset actions (value === 0)
    // mean the voter retracted, so they drop out.
    const lastByName = new Map();
    for (const h of history) lastByName.set(h.name, h);
    const entries = [...lastByName.values()].filter(h => h.value !== 0);

    let body;
    if (entries.length === 0) {
        body = '<div style="color:var(--text-muted);font-size:13px">None</div>';
    } else {
        entries.sort((a, b) => (b.ps - a.ps) || a.name.localeCompare(b.name));
        const owner = node.owner || '';
        const author = node.author || '';
        body = '';
        for (const e of entries) {
            const isOwner = owner && e.name === owner;
            const isAuthor = !isOwner && author && e.name === author;
            let role = 'reviewer';
            if (isOwner) role = 'owner';
            else if (isAuthor) role = 'author';
            const color = e.value > 0 ? '#3fb950' : '#f85149';
            const sign = e.value > 0 ? '+' : '';
            body += `<div style="font-size:13px;margin:1px 0;opacity:0.6">
                <span style="color:${color};font-weight:600">${sign}${e.value}</span>
                <span style="color:var(--text)">${esc(e.name)}</span>
                <span style="color:var(--text-muted);font-size:11px"> (${role}) — patch set ${e.ps}</span>
            </div>`;
        }
    }
    return `<div class="field">
        <div class="fl">Review History (older patchsets)</div>
        <div class="fv">${body}</div>
    </div>`;
}

function renderCommentsPanel(node) {
    const rv = node.review || {};
    const count = rv.unresolved_count || 0;
    const comments = rv.unresolved_comments || [];

    let html = `<div class="field">
        <div class="fl">Unresolved Comments (${count})</div>
        <div class="fv">`;

    if (count === 0 && comments.length === 0) {
        html += '<div style="color:var(--text-muted);font-size:13px">None</div>';
    } else if (comments.length === 0) {
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

    // Find chain above (walk up from this node). Visibility is
    // routed through the shared nodeVisible helper so this view
    // stays in sync with the main graph filter state.
    const above = [];
    function walkUp(nid, depth) {
        if (depth > 50) return;
        const kids = (childrenOf[nid] || []).filter(k => nodeVisible(k));
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
        if (!nodeVisible(cursor)) { id = cursor; cursor = parentOf[cursor]; continue; }
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
                ${node.current_commit ? `<button onclick="navigator.clipboard.writeText('${node.current_commit}');this.textContent='\u2713';setTimeout(()=>this.textContent='SHA',1500)" style="cursor:pointer;font-size:11px;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 8px;color:var(--accent);margin-left:4px" title="Copy current patchset commit hash to clipboard">SHA</button>` : ''}
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
        ${(node.project || node.branch) ? `<div class="field">
            <div class="fl">Repo &middot; Branch</div>
            <div class="fv">
                <code style="font-size:12px">${esc(node.project || '?')}</code>
                <span style="color:var(--text-muted)"> &middot; </span>
                <code style="font-size:12px">${esc(node.branch || '?')}</code>
            </div>
        </div>` : ''}
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
        Double-click (or middle-click) to open in Gerrit.<br><br>
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
// Every user-initiated action routes through this `actions` object
// so button clicks and keyboard shortcuts share the same
// implementation. Adding a new entry point (command palette,
// programmatic control, etc.) becomes a one-line call.
const actions = {
    refresh() {
        renderGraph();
        if (selectedNodeId !== null) showNodeInfo(selectedNodeId);
    },
    fit() {
        network.fit({
            animation: { duration: 400, easingFunction: 'easeInOutQuad' },
        });
    },
    focusSelection() {
        const target = selectedNodeId !== null ? selectedNodeId : currentAnchor;
        network.focus(target, {
            scale: 1.5,
            animation: { duration: 400, easingFunction: 'easeInOutQuad' },
        });
    },
    zoom(factor) {
        const scale = network.getScale();
        network.moveTo({
            scale: scale * factor,
            animation: { duration: 200, easingFunction: 'easeInOutQuad' },
        });
    },
    pan(dx, dy) {
        // Step size scales inverse to the zoom level so each key
        // press moves roughly the same distance on screen.
        const scale = network.getScale();
        const step = 80 / scale;
        const v = network.getViewPosition();
        network.moveTo({
            position: { x: v.x + dx * step, y: v.y + dy * step },
            animation: { duration: 150, easingFunction: 'easeInOutQuad' },
        });
    },
    togglePanel() {
        document.getElementById('panel').classList.toggle('hidden');
        setTimeout(() => network.redraw(), 100);
    },
    toggleHelp() {
        document.getElementById('help-overlay').classList.toggle('hidden');
    },
    toggleTheme() {
        document.body.classList.toggle('light');
        document.getElementById('btn-theme').textContent =
            isLight() ? 'Dark Mode' : 'Light Mode';
        renderLegend();
        this.refresh();
    },
    closeOverlaysOrSelection() {
        const help = document.getElementById('help-overlay');
        if (!help.classList.contains('hidden')) {
            help.classList.add('hidden');
        } else {
            network.unselectAll();
            showDefaultInfo();
        }
    },
};

document.getElementById('chk-abandoned').addEventListener('change', () => actions.refresh());
document.getElementById('chk-merged').addEventListener('change', () => actions.refresh());
document.getElementById('chk-history').addEventListener('change', () => actions.refresh());
document.getElementById('btn-fit').addEventListener('click', () => actions.fit());
document.getElementById('btn-focus').addEventListener('click', () => actions.focusSelection());
document.getElementById('btn-search').addEventListener('click', openSearch);
document.getElementById('btn-panel').addEventListener('click', () => actions.togglePanel());
document.getElementById('btn-help').addEventListener('click', () => actions.toggleHelp());
document.getElementById('btn-theme').addEventListener('click', () => actions.toggleTheme());

// Keyboard
document.addEventListener('keydown', function(e) {
    // Ctrl/Cmd+F opens search
    if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        openSearch();
        return;
    }
    if (e.target.tagName === 'INPUT') return;
    const k = e.key;
    if (k === 'f' || k === 'F') actions.fit();
    else if (k === 'z' || k === 'Z') actions.focusSelection();
    else if (k === '+' || k === '=') actions.zoom(1.3);
    else if (k === '-') actions.zoom(1 / 1.3);
    else if (k === '?') actions.toggleHelp();
    else if (k === 'Escape') actions.closeOverlaysOrSelection();
    else if (k === 'ArrowLeft')  { e.preventDefault(); actions.pan(-1,  0); }
    else if (k === 'ArrowRight') { e.preventDefault(); actions.pan( 1,  0); }
    else if (k === 'ArrowUp')    { e.preventDefault(); actions.pan( 0, -1); }
    else if (k === 'ArrowDown')  { e.preventDefault(); actions.pan( 0,  1); }
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
