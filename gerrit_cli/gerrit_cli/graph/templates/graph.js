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
  + `<span class="badge badge-merged" title="${sc.MERGED || 0} series member(s) merged${(G.stats.structural_merged_count || 0) ? `; ${G.stats.structural_merged_count} unrelated merged patch(es) shown dimmed only as branch base` : ''}">MERGED: ${sc.MERGED || 0}${(G.stats.structural_merged_count || 0) ? ` +${G.stats.structural_merged_count} base` : ''}</span>`
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
// Set of every merged-trunk node id. Used by _layoutTree to reserve
// the x=0 column for trunk-only "continuation" steps: when a trunk
// node's only continuation forward is another trunk node, that
// stays at x=0; any in-flight kid of a trunk node always branches
// off to the side instead of trying to inherit the trunk column.
const trunkSet = new Set(G.merged_trunk || []);
// The newest trunk node (last in chronological merge order). The
// trunk column above it is empty by definition, so an in-flight
// "current live" kid can take that column instead of branching
// off to the side — only ones above an OLDER trunk node have to
// branch (the column above is reserved for the next trunk patch).
const trunkTopId = (G.merged_trunk && G.merged_trunk.length > 0)
    ? G.merged_trunk[G.merged_trunk.length - 1]
    : null;
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

// Subtree extents: how many NODE_W columns the subtree under `id`
// occupies on the left and right of `id`'s own column when placed
// by _layoutTree. Mirrors the alternating left/right side-kid
// assignment (default startRight=false: i=0 → LEFT, i=1 → RIGHT).
// mainKid sits in the parent column and contributes its own
// extents directly.
//
// Used in place of the (w-1)*NODE_W/2 centered allocation so an
// asymmetric subtree (e.g. 45051 which extends only leftward)
// doesn't reserve the unused half-allocation and visually drift
// away from its parent. For symmetric subtrees the result equals
// (w-1)/2 on both sides — same as the old centered formula.
// Assumes non-trunk-parent alternation; the parentInTrunk branch
// in _layoutTree uses leftX/rightX directly and never consults
// this helper.
function _subtreeExtents(ctx, id) {
    if (ctx.extentsCache[id] !== undefined) return ctx.extentsCache[id];
    const kidsAll = (childrenOf[id] || [])
        .filter(k => _layoutShouldShow(ctx, k));
    if (kidsAll.length === 0) {
        ctx.extentsCache[id] = { left: 0, right: 0 };
        return ctx.extentsCache[id];
    }
    const mainKid = _pickMainKid(ctx, id, kidsAll);
    const sideKids = kidsAll
        .filter(k => k !== mainKid)
        .sort((a, b) => a - b);
    let leftCols = 0, rightCols = 0;
    let leftOffset = 1, rightOffset = 1;
    for (let i = 0; i < sideKids.length; i++) {
        const goRight = (i % 2 === 1);
        const k = sideKids[i];
        const w = _subtreeWidth(ctx, k);
        const ext = _subtreeExtents(ctx, k);
        if (goRight) {
            const rootOffset = rightOffset + ext.left;
            rightCols = Math.max(rightCols, rootOffset + ext.right);
            rightOffset += w;
        } else {
            const rootOffset = leftOffset + ext.right;
            leftCols = Math.max(leftCols, rootOffset + ext.left);
            leftOffset += w;
        }
    }
    if (mainKid) {
        const ext = _subtreeExtents(ctx, mainKid);
        leftCols = Math.max(leftCols, ext.left);
        rightCols = Math.max(rightCols, ext.right);
    }
    ctx.extentsCache[id] = { left: leftCols, right: rightCols };
    return ctx.extentsCache[id];
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

// Pick the child of `parentId` that should continue straight up
// the current column. Selection is a multi-key sort:
//   1. Already-placed children win — when a merged trunk node sits
//      directly above a parent, the trunk path stays at x=0 and
//      in-flight siblings get routed to the sides.
//   2. Quality class 0 (current edge + has descendants) beats
//      class 1 (stale or leaf).
//   3. Membership in the global main chain.
//   4. More descendants wins.
//   5. Non-stale edge wins.
// All other kids end up as side-kids (sorted by cn for stable
// alternation in _layoutTree).
//
// Trunk-parent rule: when the parent is itself a merged trunk
// node, only ANOTHER trunk kid is allowed to continue the column
// upward — in-flight kids of a trunk node always branch off to
// the side. Returns null when the parent is a trunk node with no
// trunk-kid candidates: the caller routes every kid as a side.
function _pickMainKid(ctx, parentId, kids) {
    if (kids.length === 0) return null;
    const positions = ctx.positions;
    const parentInTrunk = trunkSet.has(parentId);
    // Trunk top exception: when the parent is the newest trunk
    // node, the column directly above is free (no later merged
    // patch). A "current live" in-flight kid (non-stale edge,
    // has at least one descendant) is allowed to take that
    // column. Without this exception, even a single non-stale
    // continuation off the trunk top branches sideways and
    // leaves the visual column above the trunk top empty.
    let candidates;
    if (parentInTrunk && parentId === trunkTopId) {
        const trunkKids = kids.filter(k => trunkSet.has(k));
        if (trunkKids.length > 0) {
            candidates = trunkKids;
        } else {
            candidates = kids.filter(k => {
                const e = edgeMap[parentId + '->' + k];
                if (e && e.is_stale) return false;
                return countDesc(k) > 0;
            });
        }
    } else if (parentInTrunk) {
        candidates = kids.filter(k => trunkSet.has(k));
    } else {
        candidates = kids;
    }
    if (candidates.length === 0) return null;
    const sorted = candidates.slice().sort((a, b) => {
        const placedA = positions[a] ? 0 : 1;
        const placedB = positions[b] ? 0 : 1;
        if (placedA !== placedB) return placedA - placedB;

        const ma = mainChain.has(a) ? 0 : 1;
        const mb = mainChain.has(b) ? 0 : 1;

        const ea = edgeMap[parentId + '->' + a];
        const eb = edgeMap[parentId + '->' + b];
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
    return sorted[0];
}

// Either record (x, level) for `id` if it isn't placed yet, or
// reuse its existing position. Returns the (x, level) the rest of
// _layoutTree should use for child placement. Pulled out so the
// "skip if placed" path is one obvious line in the caller.
function _enterLayoutFrame(ctx, id, x, level) {
    const placed = ctx.positions[id];
    if (placed) {
        return { x: placed.x, level: -placed.y / LEVEL_H };
    }
    _placeNode(ctx, id, x, -level * LEVEL_H);
    return { x, level };
}

// Single point through which all node positions are recorded, so we
// can preserve INSERTION order alongside ctx.positions. JS object
// iteration walks numeric keys in ascending number order regardless
// of when they were added, so _resolveCollisions could never tell
// which node "claimed" a coordinate first. ctx.placementOrder gives
// us the right tie-break: when two nodes land on the same (x, y),
// the one placed first wins.
function _placeNode(ctx, id, x, y) {
    if (ctx.positions[id] === undefined) {
        (ctx.placementOrder = ctx.placementOrder || []).push(id);
    }
    ctx.positions[id] = { x, y };
}

// True iff any already-placed node sits at column `colX` within
// the level band [startLevel, startLevel + heightSigned]. Used by
// trunk side-kid placement to detect when a sibling chain has
// already claimed the column we'd otherwise default to.
function _columnBlocked(ctx, colX, startLevel, heightSigned) {
    const endLevel = startLevel + heightSigned;
    const yLo = -Math.max(startLevel, endLevel) * LEVEL_H;
    const yHi = -Math.min(startLevel, endLevel) * LEVEL_H;
    for (const pid of (ctx.placementOrder || [])) {
        const p = ctx.positions[pid];
        if (!p || p.x !== colX) continue;
        if (p.y >= yLo && p.y <= yHi) return true;
    }
    return false;
}

// Recursively place a subtree rooted at `id`. `dir` is +1 when
// children grow up (negative y) and -1 when they grow down. Returns
// the outermost level used by the subtree so callers can chain
// placements without overlap.
//
// When `id` is already placed by an earlier phase (typically a
// merged trunk node from _layoutMergedTrunk), we keep its position
// and continue walking its children — that's how in-flight
// descendants of trunk nodes get reached.
function _layoutTree(ctx, id, x, level, dir) {
    const frame = _enterLayoutFrame(ctx, id, x, level);
    x = frame.x; level = frame.level;

    const kidsAll = (childrenOf[id] || [])
        .filter(k => _layoutShouldShow(ctx, k));
    if (kidsAll.length === 0) return level;

    const mainKid = _pickMainKid(ctx, id, kidsAll);

    // Single child: usually continue straight up via that kid.
    // Exception: when the parent is a trunk node and its only kid
    // is in-flight, _pickMainKid returns null — the in-flight kid
    // must branch OFF the trunk column instead of taking it over.
    // Fall through to the side-branch path below.
    if (kidsAll.length === 1 && mainKid !== null) {
        return _layoutTree(ctx, kidsAll[0], x, level + dir, dir);
    }
    // Side kids exclude mainKid AND any already-placed kids.
    // Already-placed non-main kids (e.g. a second merged trunk
    // node on a fork) are walked into afterwards purely to
    // descend into THEIR children — they don't consume a side
    // slot of this parent.
    const sideKids = kidsAll
        .filter(k => k !== mainKid && !ctx.positions[k])
        .sort((a, b) => a - b);

    // Place side branches first, alternating left and right.
    //
    // Column-occupancy steer (trunk parents only): if the column
    // immediately left of the trunk node is already occupied in
    // the y-range the first side kid's subtree would span, flip
    // the alternation seed so the kid goes RIGHT. Prevents two
    // trunk-attached chains from both defaulting to LEFT and
    // colliding — hit on 54459, where chain A (54469..54485) was
    // placed at x=-380 by an earlier _layoutTree call rooted at
    // trunk 54463, then chain B (54487..54496) off trunk 54486
    // also wanted x=-380 and _resolveCollisions snaked its nodes
    // between -380 and +380.
    let startRight = false;
    if (trunkSet.has(id) && sideKids.length >= 1 && dir > 0
            && _columnBlocked(ctx, x - NODE_W, level + dir,
                              _subtreeHeight(ctx, sideKids[0]) * dir)
            && !_columnBlocked(ctx, x + NODE_W, level + dir,
                               _subtreeHeight(ctx, sideKids[0]) * dir)) {
        startRight = true;
    }
    const leftKids = [];
    const rightKids = [];
    for (let i = 0; i < sideKids.length; i++) {
        const goRight = startRight ? (i % 2 === 0) : (i % 2 === 1);
        (goRight ? rightKids : leftKids).push(sideKids[i]);
    }

    let extremeSideLevel = level;
    const updateExtreme = (l) => {
        extremeSideLevel = dir > 0
            ? Math.max(extremeSideLevel, l)
            : Math.min(extremeSideLevel, l);
    };

    // Side branches start one row above the parent. _layoutMergedTrunk
    // packs trunk rows with a `branchH + 1` gap, so a side subtree
    // tall enough to need extra space already has it — the side kid
    // can step in full LEVEL_H units without colliding with the next
    // trunk row.
    //
    // When the parent is a trunk node, skip the (w-1)*NODE_W/2
    // centering: the trunk column is reserved at x=0, and a tall
    // chain-like side kid (e.g. 62852 with 15 leaves spread across
    // 22 levels) shouldn't get shoved 7 node widths away just
    // because its leaf-count is large. Place it at parent.x ±
    // NODE_W and let its subtree extend further out from there.
    // For non-trunk parents we keep the centered allocation so
    // siblings under a normal in-flight node don't overlap.
    const parentInTrunk = trunkSet.has(id);

    // Pre-place a CHAIN mainKid in the parent column BEFORE side
    // branches. Chain mainLevel is level+dir and doesn't depend on
    // extremeSideLevel from the side loops, so committing it early
    // is safe. Once mainKid sits at parent.x at the level+dir row,
    // a w=1 sideKid that the trunk-column rule in _resolveCollisions
    // would otherwise bump from x=0 onto parent.x now finds parent.x
    // taken and is bumped one more column out instead — chain stays
    // straight, stale leaf branches off. Hit on 54489 (54491 chain
    // mainKid vs 54490 stale leaf sideKid both at level+dir; the
    // unconditional first bump in _resolveCollisions used to land
    // 54490 on top of 54491's column at parent.x=380, pushing the
    // chain to x=760).
    // Skipped when mainKid is already placed (a fork's second trunk
    // kid descended into below) since its position is fixed and the
    // collision-avoidance is moot.
    const mainKidIsChain = mainKid !== null
            && !ctx.positions[mainKid]
            && _isChainSubtree(ctx, mainKid);
    if (mainKidIsChain) {
        const top = _layoutTree(ctx, mainKid, x, level + dir, dir);
        updateExtreme(top);
    }

    let leftX = x - NODE_W;
    for (const kid of leftKids) {
        // Extent-aware placement (non-trunk parents): position root
        // so its rightward extent just touches leftX. For a purely
        // left-leaning subtree (ext.right = 0, e.g. 45051) root sits
        // AT leftX = parent.x - NODE_W instead of the centered
        // parent.x - NODE_W - (w-1)*NODE_W/2. For symmetric subtrees
        // ext.right = (w-1)/2, matching the old centered formula.
        const ext = _subtreeExtents(ctx, kid);
        const kidX = parentInTrunk
            ? leftX
            : leftX - ext.right * NODE_W;
        const top = _layoutTree(ctx, kid, kidX, level + dir, dir);
        updateExtreme(top);
        // Advance past the columns the subtree ACTUALLY occupies
        // (root at kidX, extending ext.left further left), not its
        // leaf count. Leaf-count advances over-reserve for deep
        // unbalanced trees — hit on 66898's left kids in 61965,
        // where 63166 (leaf count 10, actual extent 1-2 columns)
        // pushed the next left kid 67067 out to x=-4180.
        leftX = kidX - (ext.left + 1) * NODE_W;
    }

    let rightX = x + NODE_W;
    for (const kid of rightKids) {
        const ext = _subtreeExtents(ctx, kid);
        const kidX = parentInTrunk
            ? rightX
            : rightX + ext.left * NODE_W;
        const top = _layoutTree(ctx, kid, kidX, level + dir, dir);
        updateExtreme(top);
        rightX = kidX + (ext.right + 1) * NODE_W;
    }

    // Non-chain mainKid: pushed past side branches so its own left/
    // right sub-branches don't collide with this parent's side
    // branches at the same row. Chain mainKid was already placed
    // above; we only handle the non-chain case here.
    if (mainKid && !mainKidIsChain) {
        const mainLevel = extremeSideLevel + dir;
        const top = _layoutTree(ctx, mainKid, x, mainLevel, dir);
        updateExtreme(top);
    }

    // Any kids that were already placed but didn't become mainKid
    // (e.g., a second merged trunk node attached to the same
    // parent — unusual, but possible on a fork) still need to be
    // descended into so their in-flight descendants get reached.
    // The recursion uses the kid's existing (x, level) and skips
    // re-positioning.
    for (const k of kidsAll) {
        if (k === mainKid) continue;
        if (sideKids.includes(k)) continue;
        if (!ctx.positions[k]) continue;
        const top = _layoutTree(ctx, k, 0, 0, dir);
        updateExtreme(top);
    }
    return extremeSideLevel;
}

// True when every visible descendant of `id` has at most one
// visible child — i.e., the subtree is one vertical line with no
// horizontal branching. Used by _layoutTree to decide whether the
// main-chain push above side branches is actually needed.
function _isChainSubtree(ctx, id) {
    let cur = id;
    // Safety bound to prevent runaway in case of an unexpected
    // cycle that wasn't caught by _break_cycles.
    for (let i = 0; i < 500; i++) {
        const kids = (childrenOf[cur] || [])
            .filter(k => _layoutShouldShow(ctx, k));
        if (kids.length === 0) return true;
        if (kids.length > 1) return false;
        cur = kids[0];
    }
    return false;
}

// Step 1: place in-flight descendants of the anchor by walking
// _layoutTree from the anchor upward. The anchor itself is
// expected to already be positioned by _layoutAnchorColumn, and
// _layoutTree's "already placed → keep position, walk children"
// path means we naturally descend into the trunk too — any
// newer-merged trunk node above the anchor has its in-flight
// kids placed by the same recursive walk.
function _layoutUpwardFromAnchor(ctx) {
    _layoutTree(ctx, ctx.anchorId, 0, 0, 1);
}

// _layoutBaseChain was the pre-trunk way of placing ancestors
// (walked parentOf, spaced via subtree-height). Its job is split
// between _layoutAnchorColumn (in-flight ancestors), _layoutMergedTrunk
// (merged ancestors in chronological order), and
// _layoutTrunkSideBranches (their side branches). The old helper
// has no callers left.

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
    for (let i = 0; i < chain.length; i++) {
        _placeNode(ctx, chain[i], baseX, baseY - i * LEVEL_H);
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
        _placeNode(ctx, id, px, py);
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
            _placeNode(
                ctx, ids[i],
                columnX + i * NODE_W,
                -parseInt(lv) * LEVEL_H,
            );
        }
    }
}

// Step 4: any nodes that ended up at exactly the same (x, y) — e.g.
// because two fixup passes chose the same slot — get shifted right
// until they find an empty coordinate.
//
// Iteration uses ctx.placementOrder (insertion order) rather than
// Object.entries — JS objects with numeric keys iterate by ASCENDING
// NUMBER regardless of insertion order, which previously let a
// later-placed but lower-cn node steal the slot from an earlier-
// placed one. The earliest placement wins.
//
// Non-trunk nodes also can never be shifted INTO the x=0 column —
// that column belongs to the merged trunk, and dumping an in-flight
// side branch there visually merges it with the trunk row.
function _resolveCollisions(ctx) {
    const positions = ctx.positions;
    const order = ctx.placementOrder || [];
    // Reserve x=0 only across the y-range where trunk nodes actually
    // sit. Above the topmost trunk node and below the bottommost the
    // column is fair game for in-flight side branches — otherwise a
    // node at parent.x = -NODE_W gets shoved to +NODE_W via the trunk
    // reservation even though the row in question is free of any
    // trunk patch, producing a visible 2*NODE_W gap (hit in 61965 at
    // 66691/66481/66567 vs their parents at x=-NODE_W).
    let trunkYMin = Infinity, trunkYMax = -Infinity;
    for (const id of order) {
        if (!trunkSet.has(id)) continue;
        const p = positions[id];
        if (!p) continue;
        if (p.y < trunkYMin) trunkYMin = p.y;
        if (p.y > trunkYMax) trunkYMax = p.y;
    }
    const xZeroReserved = (y) => y >= trunkYMin && y <= trunkYMax;

    const occupied = new Map();
    for (const id of order) {
        const pos = positions[id];
        if (!pos) continue;
        const isTrunk = trunkSet.has(id);
        const key = pos.x + ',' + pos.y;
        // Treat "non-trunk landed on x=0 inside the trunk span" as a
        // collision even when nothing else is at that slot, so the
        // bump loop's blockedByTrunkColumn rule fires. Without this,
        // a w=1 sideKid whose centered formula collapses to x=0
        // would sit on the trunk column whenever no trunk node
        // happens to share its exact row.
        if (!occupied.has(key)
                && (isTrunk || pos.x !== 0 || !xZeroReserved(pos.y))) {
            occupied.set(key, id);
            continue;
        }
        let px = pos.x + NODE_W;
        let tries = 0;
        while (tries < 30) {
            const blockedByTrunkColumn = !isTrunk && px === 0
                    && xZeroReserved(pos.y);
            if (!occupied.has(px + ',' + pos.y) && !blockedByTrunkColumn) {
                break;
            }
            px += NODE_W;
            tries++;
        }
        positions[id] = { x: px, y: pos.y };
        occupied.set(px + ',' + pos.y, id);
    }
}

// Step 0a: place the anchor (y=0) and any in-flight ancestors of
// the anchor (walked via parentOf) on the central x=0 column.
// Stops at the first merged ancestor — those belong to the trunk
// and get placed in _layoutMergedTrunk.
//
// Returns the bottom-most level used (a negative or zero number
// in our convention: y=0 is the anchor, y=N*LEVEL_H below is
// level=-N). The trunk uses that level to know where to start
// stacking older merged nodes BELOW the in-flight ancestors.
function _layoutAnchorColumn(ctx) {
    const positions = ctx.positions;
    const anchor = ctx.anchorId;
    _placeNode(ctx, anchor, 0, 0);
    baseChainSet.add(anchor);

    let belowLevel = 0;
    let cur = parentOf[anchor];
    const seen = new Set();
    while (cur && nodeMap[cur] && !seen.has(cur)) {
        seen.add(cur);
        if (nodeMap[cur].status === 'MERGED') break;
        if (!_layoutShouldShow(ctx, cur)) {
            cur = parentOf[cur];
            continue;
        }
        belowLevel -= 1;
        _placeNode(ctx, cur, 0, -belowLevel * LEVEL_H);
        baseChainSet.add(cur);
        cur = parentOf[cur];
    }
    return belowLevel;
}

// Step 0b: lay out the merged trunk. Every MERGED node sits at
// x=0 in chronological order (submitted ASC). The anchor is
// pinned at y=0 even when itself merged; newer merged sit above
// (negative y), older merged sit below the in-flight ancestor
// chain (positive y, starting one level below `belowAnchorLevel`).
//
// Spacing between adjacent trunk rows is NOT uniform: each trunk
// node X gets a gap above it equal to `1 + max(_subtreeHeight of
// X's in-flight side branches)`. That makes room for X's side
// subtree to grow up without colliding y with the next merged
// patch — mirrors what the retired _layoutBaseChain did via
// `Math.max(1, branchH + 1)`. Trunk nodes with no in-flight side
// branches still pack tight (gap = 1 row).
//
// G.merged_trunk is the server-built list (oldest first).
function _layoutMergedTrunk(ctx, belowAnchorLevel) {
    const anchor = ctx.anchorId;
    const trunk = (G.merged_trunk || []).filter(id => nodeVisible(id));
    if (trunk.length === 0) return;
    const anchorIdx = trunk.indexOf(anchor);

    // Number of vertical slots a trunk node needs ABOVE itself for
    // its in-flight side branches. Trunk-kid edges don't consume
    // slots (those run along the trunk column). Stale and non-stale
    // edges both count: a stale side kid still gets rendered in
    // its own column at level+1, so the trunk row above needs to
    // skip past it for the side kid to be visually attached to its
    // parent trunk node rather than colliding with the next trunk
    // row's side kid. The same gap that gives the non-stale 62732
    // → 64441 edge its own row should apply to the stale 62063 →
    // 62389/62064 edges.
    function trunkSpacing(id) {
        const sideKids = (childrenOf[id] || [])
            .filter(k => _layoutShouldShow(ctx, k))
            .filter(k => !trunkSet.has(k));
        let h = 0;
        for (const sk of sideKids) h = Math.max(h, _subtreeHeight(ctx, sk));
        return Math.max(1, h + 1);
    }

    if (anchorIdx >= 0) {
        baseChainSet.add(anchor);
        // Above anchor: each newer trunk node sits a "spacing" gap
        // above the trunk node just below it (or above the anchor
        // for trunk[anchorIdx+1]).
        let y = 0;
        for (let i = anchorIdx + 1; i < trunk.length; i++) {
            const below = (i === anchorIdx + 1) ? anchor : trunk[i - 1];
            y -= trunkSpacing(below) * LEVEL_H;
            _placeNode(ctx, trunk[i], 0, y);
            baseChainSet.add(trunk[i]);
        }
        // Below anchor: the in-flight ancestor chain (if any) ends
        // at y = -belowAnchorLevel * LEVEL_H. The first older trunk
        // sits a gap further down to leave room for ITS side branches.
        const inflightBottomY = -belowAnchorLevel * LEVEL_H;
        y = inflightBottomY;
        for (let i = anchorIdx - 1; i >= 0; i--) {
            y += trunkSpacing(trunk[i]) * LEVEL_H;
            _placeNode(ctx, trunk[i], 0, y);
            baseChainSet.add(trunk[i]);
        }
    } else {
        // Anchor not in trunk: entire trunk below the in-flight chain.
        const inflightBottomY = -belowAnchorLevel * LEVEL_H;
        let y = inflightBottomY;
        for (let i = trunk.length - 1; i >= 0; i--) {
            y += trunkSpacing(trunk[i]) * LEVEL_H;
            _placeNode(ctx, trunk[i], 0, y);
            baseChainSet.add(trunk[i]);
        }
    }
}

// Step 0c: surface in-flight side branches that hang off any trunk
// node. _layoutUpwardFromAnchor reaches most of them by descending
// through anchor's child chain, but the walk stops at any hidden
// node (e.g., an abandoned change in the middle of the chain) —
// every trunk node beyond that point and its in-flight kids would
// otherwise be left for the generic fallback. This pass walks the
// trunk explicitly and places each trunk node's unplaced visible
// kids as side branches, alternating left and right of the trunk
// column with one level of upward offset.
function _layoutTrunkSideBranches(ctx) {
    const positions = ctx.positions;
    const trunk = (G.merged_trunk || []).filter(id => nodeVisible(id));
    // Reverse index: which unplaced in-flight parents (git-ancestors)
    // does each trunk node have? Hit on 62887 in the 61965 graph —
    // 62887 is NEW, its git child 61962 is merged and on trunk. The
    // /related edge 62887 -> 61962 exists, but nothing walks it
    // upward from 61962. Without a placement pass here, 62887 falls
    // into _layoutUnplacedMainSeries's floating column.
    const trunkParents = {};
    for (const t of trunk) {
        for (const e of (G.edges || [])) {
            if (e.to !== t) continue;
            if (trunkSet.has(e.from)) continue;
            if (!_layoutShouldShow(ctx, e.from)) continue;
            (trunkParents[t] = trunkParents[t] || []).push(e.from);
        }
    }
    for (const id of trunk) {
        const pos = positions[id];
        if (!pos) continue;
        const level = Math.round(-pos.y / LEVEL_H);
        // In-flight parents of the trunk node — placed one row
        // BELOW (older direction). Their outgoing edge to the
        // trunk node reads upward toward it. Place the parent
        // node itself with _placeNode (so it isn't recursed into
        // as a subtree with dir=-1, which would push the parent's
        // own in-flight kids further DOWN into the older-trunk
        // row) and then recurse into the parent's own children
        // upward with dir=+1 — their column stays alongside p and
        // grows toward the trunk row.
        const parentSides = (trunkParents[id] || [])
            .filter(p => !positions[p])
            .sort((a, b) => a - b);
        if (parentSides.length > 0) {
            let leftX = pos.x - NODE_W;
            let rightX = pos.x + NODE_W;
            for (let i = 0; i < parentSides.length; i++) {
                const p = parentSides[i];
                const pX = (i % 2 === 0) ? rightX : leftX;
                _placeNode(ctx, p, pX, -(level - 1) * LEVEL_H);
                for (const gk of (childrenOf[p] || [])) {
                    if (!_layoutShouldShow(ctx, gk)) continue;
                    if (positions[gk]) continue;
                    _layoutTree(ctx, gk, pX, level, 1);
                }
                const ext = _subtreeExtents(ctx, p);
                if (i % 2 === 0) rightX = pX + (ext.right + 1) * NODE_W;
                else leftX = pX - (ext.left + 1) * NODE_W;
            }
        }
        const unplaced = (childrenOf[id] || [])
            .filter(k => _layoutShouldShow(ctx, k))
            .filter(k => !positions[k])
            .sort((a, b) => a - b);
        if (unplaced.length === 0) continue;
        // One row above the trunk node — _layoutMergedTrunk already
        // reserved enough rows above this trunk node for the kid's
        // subtree height. Skip the (w-1)*NODE_W/2 centering: trunk
        // side kids hug the column at parent.x ± NODE_W regardless
        // of how leaf-heavy their subtrees are (same logic as
        // _layoutTree's trunk-parent branch).
        const sideStart = level + 1;
        let leftX = pos.x - NODE_W;
        let rightX = pos.x + NODE_W;
        // Symmetric column-occupancy steer: this function defaults
        // i=0 to RIGHT, so flip to LEFT when right is blocked but
        // left is free.
        const firstKidH = _subtreeHeight(ctx, unplaced[0]);
        const startLeft = _columnBlocked(ctx, pos.x + NODE_W,
                                         sideStart, firstKidH)
                && !_columnBlocked(ctx, pos.x - NODE_W,
                                   sideStart, firstKidH);
        for (let i = 0; i < unplaced.length; i++) {
            const kid = unplaced[i];
            // Advance by the subtree's actual column extent, not
            // leaf count — leaf-count advances over-reserve for
            // deep unbalanced trees and shove the next sibling
            // several empty columns out.
            const ext = _subtreeExtents(ctx, kid);
            const goRight = startLeft ? (i % 2 === 1) : (i % 2 === 0);
            if (goRight) {
                _layoutTree(ctx, kid, rightX, sideStart, 1);
                rightX += (ext.right + 1) * NODE_W;
            } else {
                _layoutTree(ctx, kid, leftX, sideStart, 1);
                leftX -= (ext.left + 1) * NODE_W;
            }
        }
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
        // Insertion-order array of node ids, parallel to `positions`.
        // _resolveCollisions consults this so the earliest placement
        // wins a contested slot (JS object iteration sorts numeric
        // keys by ascending number — useless for "who got there
        // first" decisions).
        placementOrder: [],
        widthCache: {},
        heightCache: {},
        extentsCache: {},
    };
    // Layout phases in dependency order. Each phase owns one
    // placement task and is safe to read in isolation.
    //
    //  1. _layoutAnchorColumn  — anchor at (0,0) plus any
    //                            in-flight ancestors stacked
    //                            below it on the x=0 column.
    //  2. _layoutMergedTrunk   — every MERGED node at x=0 in
    //                            chronological (submitted) order;
    //                            newer above anchor, older below
    //                            the in-flight ancestor chain.
    //                            Anchor if merged keeps y=0.
    //  3. _layoutUpwardFromAnchor — in-flight descendants of the
    //                            anchor. _layoutTree walks past
    //                            already-placed trunk nodes so
    //                            their in-flight side branches
    //                            get reached on the same walk.
    //  4. _layoutTrunkSideBranches — side branches of trunk nodes
    //                            BELOW the anchor (not reachable
    //                            from the descendant walk).
    //  5. _layoutUnplacedMainSeries — stragglers that no phase
    //                            placed (defensive).
    //  6. _layoutSeparateGroups — only IN-FLIGHT groups remain;
    //                            merged members were promoted to
    //                            the trunk on the Python side.
    //  7. _resolveCollisions   — final pass to nudge exact-coord
    //                            overlaps right.
    const belowLevel = _layoutAnchorColumn(ctx);
    _layoutMergedTrunk(ctx, belowLevel);
    _layoutUpwardFromAnchor(ctx);
    _layoutTrunkSideBranches(ctx);
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
        { kind: 'border', label: 'Base (unrelated parent)', color: C.STATUS.MERGED.border },
        { kind: 'fill', label: 'Abandoned',   color: C.STATUS.ABANDONED.bg },
        { kind: 'border', label: '🚧 WIP',   color: '#c9d1d9', dashed: true },
        { kind: 'border', label: 'Anchor',   color: C.HIGHLIGHT.border, thick: true },
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
            const width = item.thick ? 3 : 2;
            return `<div class="legend-item"><span class="legend-dot"`
                + ` style="background:transparent;border:${width}px ${style} ${item.color}"></span>`
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

    // Good: BOTH Jenkins +1 and Maloo +1 AND enough non-owner
    // CR +1s. The CR threshold depends on whether the patch is a
    // backport — backports of a master change only need 1
    // reviewer +1, native patches need 2 (matches patch_status's
    // rule; see patch_status/classify.py).
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
        const required = node.is_backport ? 1 : 2;
        if (nonOwnerPlus >= required) return 'good';
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
//
// Status colors are used for every node — merged nodes stay
// purple wherever they sit (including down in the trunk), in-flight
// nodes follow their review health, and abandoned use the muted
// grey. The pre-trunk layout used to override this with a dim
// palette for "below the anchor" nodes, but the trunk's
// chronological column already conveys "history" through position;
// dimming on top of that just hid the merged → in-flight color
// distinction the user relies on.
function nodeBaseColors(node, flags, C) {
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

    // Structural trunk members: unrelated merged patches kept in
    // the trunk only because an in-flight branch hangs off them.
    // Dim fill + merged border marks them as context rather than
    // series members; the stats bar counts them separately as
    // "+N base" so the Merged number matches the full-color boxes.
    if (node.trunk_structural) {
        colors = Object.assign({}, C.DIM, {
            border: C.STATUS.MERGED.border,
        });
    }

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

    // Anchor highlight: the user's focal node always carries the
    // selection-highlight border color so it stands out from the
    // rest of the trunk and chain. Combined with the thicker
    // borderWidth (set below) the anchor is unmistakable even when
    // it sits mid-trunk among other merged patches.
    if (flags.isAnchor) {
        colors = Object.assign({}, colors, {
            border: C.HIGHLIGHT.border,
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
    } else {
        // Non-stale edges that aren't on the main chain (separate
        // series, side branches, cross-group links, trunk connections)
        // still represent a real current dependency — same color as
        // main, just thinner so the dominant chain stands out.
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

    const C = getColors();
    const anchorBanner = (node.id === currentAnchor)
        ? `<div class="field" style="background:rgba(255,166,87,0.12);border-left:3px solid ${C.HIGHLIGHT.border};padding:6px 10px;border-radius:4px;margin-bottom:8px">
            <span style="color:${C.HIGHLIGHT.border};font-weight:700">★ Anchor</span>
            <span style="color:var(--text-muted);font-size:11px;margin-left:6px">The change this graph is centred on.</span>
        </div>`
        : '';

    panel.innerHTML = `
        ${anchorBanner}
        <div class="field">
            <div class="fl">Change</div>
            <div class="fv">
                <a href="${node.url}" target="_blank">#${node.id}</a>
                <span class="sbadge sbadge-${node.status}">${node.status}</span>
                ${node.is_wip ? '<span class="stale-tag" style="background:#7a1a1a;color:#f85149;border-color:#f85149">WIP</span>' : ''}
                ${staleTag}
                &nbsp; ps${node.current_patchset}
                ${node.checkout_cmd ? `<button onclick="navigator.clipboard.writeText('${node.checkout_cmd.replace(/'/g, "\\'")}');this.textContent='\u2713';setTimeout(()=>this.textContent='Checkout',1500)" style="cursor:pointer;font-size:11px;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 8px;color:var(--accent);margin-left:6px;display:inline-block;min-width:65px;text-align:center" title="Copy checkout command to clipboard">Checkout</button>` : ''}
                ${node.cherrypick_cmd ? `<button onclick="navigator.clipboard.writeText('${node.cherrypick_cmd.replace(/'/g, "\\'")}');this.textContent='\u2713';setTimeout(()=>this.textContent='Cherry-pick',1500)" style="cursor:pointer;font-size:11px;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 8px;color:var(--accent);margin-left:4px;display:inline-block;min-width:80px;text-align:center" title="Copy cherry-pick command to clipboard">Cherry-pick</button>` : ''}
                ${node.current_commit ? `<button onclick="navigator.clipboard.writeText('${node.current_commit}');const o=this.textContent;this.innerHTML='<span style=\\'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;font-size:11px\\'>\u2713</span>';setTimeout(()=>this.textContent=o,1500)" style="cursor:pointer;font-family:monospace;font-size:12px;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 6px;color:var(--accent);margin-left:6px;display:inline-block;min-width:65px;text-align:center" title="Click to copy full SHA: ${node.current_commit}">${node.current_commit.substring(0, 7)}</button>` : ''}
            </div>
        </div>
        ${node.is_backport ? `<div class="field">
            <div class="fl">Type</div>
            <div class="fv">
                <span class="stale-tag" style="background:#1f6f6f;color:#fff">BACKPORT</span>
                <span style="color:var(--text-muted);font-size:11px;margin-left:6px">only needs 1 reviewer +1 to be Ready</span>
            </div>
        </div>` : ''}
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

// Draw a thin dashed vertical line at canvas x=0 spanning the
// merged trunk's y range. The line is purely a visual guide —
// "everything in this column is a merged patch in landing order"
// — and is drawn underneath the nodes/edges so it never obscures
// them. Skipped when the trunk has fewer than 2 nodes (nothing
// to guide). The callback fires every redraw, so we re-derive
// the span from the live node positions in case the user drags
// the network around.
network.on('beforeDrawing', function (canvasCtx) {
    const trunk = G.merged_trunk || [];
    if (trunk.length < 2) return;
    const trunkPositions = network.getPositions(trunk);
    let minY = Infinity, maxY = -Infinity, lineX = 0;
    let count = 0;
    for (const cn of trunk) {
        const p = trunkPositions[cn];
        if (!p) continue;
        minY = Math.min(minY, p.y);
        maxY = Math.max(maxY, p.y);
        lineX = p.x;
        count++;
    }
    if (count < 2) return;
    canvasCtx.save();
    canvasCtx.strokeStyle = isLight()
        ? 'rgba(0, 0, 0, 0.16)' : 'rgba(255, 255, 255, 0.18)';
    canvasCtx.lineWidth = 2;
    canvasCtx.setLineDash([5, 6]);
    canvasCtx.beginPath();
    canvasCtx.moveTo(lineX, minY - 40);
    canvasCtx.lineTo(lineX, maxY + 40);
    canvasCtx.stroke();
    canvasCtx.restore();
});

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
