// ─── STATS TAB ───
// Series statistics. Lifecycle times per change and the headline
// numbers are resolved at build time (summary.py) so the portal reads
// the same values; this file only charts them. Everything renders as
// inline SVG: hosted graphs are served under a CSP that forbids
// fetching anything at view time. Ages are measured against the
// build time, not the viewer's clock.

const ST_DAY = 86400000;
const ST_SUM = G.stats.summary || null;
const ST_NOW = ST_SUM ? ST_SUM.as_of * 1000 : Date.now();
const ST_COL = {
    opened: '#388bfd',
    merged: '#8957e5',
    abandoned: '#8b949e',
    backlog: '#388bfd',
    reviews: '#3fb950',
};
const ST_HEALTH = {
    good:        ['#3fb950', 'ready'],
    pending:     ['#388bfd', 'pending'],
    bad_veto:    ['#a82828', 'CR veto'],
    bad_maloo:   ['#f85149', 'Maloo -1'],
    bad_jenkins: ['#e8a020', 'Jenkins -1'],
    bad_other:   ['#d63384', 'other -1'],
};
const ST_RANGES = [['all', 'All'], ['365', '1y'], ['180', '6m'], ['90', '90d'], ['30', '30d']];

const stState = {
    built: false,
    range: 'all',
    heatMode: 'uploads',
    distMode: 'merge',
    attnAll: false,
    graphNeedsFit: false,
};
let stRecs = null;

// ── tabs ──
function isStatsTab() {
    return document.body.classList.contains('tab-stats');
}

function showTab(name) {
    const stats = name === 'stats';
    document.body.classList.toggle('tab-stats', stats);
    document.getElementById('tab-graph').classList.toggle('active', !stats);
    document.getElementById('tab-stats').classList.toggle('active', stats);
    const want = stats ? '#stats' : '';
    if (location.hash !== want) {
        history.replaceState(null, '', want || (location.pathname + location.search));
    }
    if (stats) {
        closeSearch();
        if (!stState.built) buildStats();
        else stDrawAll();
    } else {
        // The canvas had no size while hidden; any fit done then is void.
        network.redraw();
        if (stState.graphNeedsFit) {
            stState.graphNeedsFit = false;
            network.fit();
        }
    }
}

function stJumpTo(id) {
    const n = nodeMap[id];
    let changed = false;
    if (n && n.status === 'ABANDONED' && !showAbandonedEnabled()) {
        document.getElementById('chk-abandoned').checked = true;
        changed = true;
    }
    if (n && n.status === 'MERGED' && !showMergedEnabled()) {
        document.getElementById('chk-merged').checked = true;
        changed = true;
    }
    showTab('graph');
    if (changed) actions.refresh();
    setTimeout(() => {
        if (nodesDS.get(id)) clickNode(id);
    }, 80);
}

// ── data ──
function stMs(t) {
    return t ? t * 1000 : NaN;
}

function stBuildRecords() {
    const out = [];
    for (const n of G.nodes) {
        // Unrelated merged patches kept only as a branch base.
        if (n.trunk_structural || !n.opened_at) continue;
        const ps = (n.ps_times || []).map(t => t * 1000);
        out.push({
            id: n.id,
            node: n,
            status: n.status,
            created: stMs(n.opened_at),
            closed: stMs(n.closed_at),
            closedApprox: n.closed_approx,
            ps,
            rv: (n.review_times || []).map(t => t * 1000),
            firstReview: stMs(n.first_review_at),
            lastHuman: stMs(n.last_activity),
            psCount: n.current_patchset || ps.length || 1,
            owner: n.owner || n.author || '?',
            ticket: n.ticket || '',
            health: n.status === 'NEW' ? reviewHealth(n) : null,
            unresolved: (n.review || {}).unresolved_count || 0,
        });
    }
    return out;
}

function stSorted(arr) {
    return arr.filter(x => !isNaN(x)).sort((a, b) => a - b);
}

// Number of values <= t in an ascending array.
function stCountLE(sorted, t) {
    let lo = 0, hi = sorted.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (sorted[mid] <= t) lo = mid + 1; else hi = mid;
    }
    return lo;
}

function stQuantile(sorted, q) {
    if (!sorted.length) return NaN;
    const pos = (sorted.length - 1) * q;
    const lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

function stEvents(recs) {
    return {
        opened: stSorted(recs.map(r => r.created)),
        merged: stSorted(recs.filter(r => r.status === 'MERGED').map(r => r.closed)),
        abandoned: stSorted(recs.filter(r => r.status === 'ABANDONED').map(r => r.closed)),
    };
}

// ── formatting ──
const ST_MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function stDur(ms) {
    if (isNaN(ms)) return '—';
    const h = ms / 3600000;
    if (h < 48) return Math.max(0, Math.round(h)) + ' h';
    const d = ms / ST_DAY;
    if (d < 10) return d.toFixed(1) + ' d';
    if (d < 90) return Math.round(d) + ' d';
    if (d < 730) return (d / 30.44).toFixed(1) + ' mo';
    return (d / 365.25).toFixed(1) + ' y';
}

function stDate(ms) {
    if (isNaN(ms)) return '—';
    return new Date(ms).toISOString().slice(0, 10);
}

function stPlural(n, word, plural) {
    return n + ' ' + (n === 1 ? word : (plural || word + 's'));
}

// ── tooltip ──
function stTip(html, e) {
    const tip = document.getElementById('st-tip');
    tip.innerHTML = html;
    tip.classList.remove('hidden');
    const pad = 14;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > window.innerWidth - 4) x = e.clientX - w - pad;
    if (y + h > window.innerHeight - 4) y = e.clientY - h - pad;
    tip.style.left = Math.max(4, x) + 'px';
    tip.style.top = Math.max(4, y) + 'px';
}

function stTipHide() {
    document.getElementById('st-tip').classList.add('hidden');
}

// ── scales & axes ──
// Integer ticks in 1/2/5 steps: every axis here counts patches.
function stYTicks(max, n) {
    const raw = Math.max(1, max) / n;
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    let step = [1, 2, 5, 10].map(m => m * p).find(v => v >= raw);
    step = Math.max(1, Math.round(step));
    const top = Math.ceil(Math.max(1, max) / step) * step;
    const ticks = [];
    for (let v = 0; v <= top; v += step) ticks.push(v);
    return { top, ticks };
}

// Calendar-aligned time ticks (UTC) between t0 and t1, roughly `want` of them.
function stTimeTicks(t0, t1, want) {
    const span = (t1 - t0) / ST_DAY;
    const steps = [
        ['day', 1], ['day', 2], ['day', 7], ['day', 14],
        ['month', 1], ['month', 2], ['month', 3], ['month', 6],
        ['year', 1], ['year', 2], ['year', 5],
    ];
    const approx = { day: 1, month: 30.44, year: 365.25 };
    let unit = 'year', k = 5;
    for (const [u, n] of steps) {
        if (span / (approx[u] * n) <= want) { unit = u; k = n; break; }
    }
    const ticks = [];
    const d = new Date(t0);
    if (unit === 'day') {
        d.setUTCHours(0, 0, 0, 0);
        if (k === 7 || k === 14) {
            // Mondays
            while (d.getUTCDay() !== 1) d.setUTCDate(d.getUTCDate() + 1);
        }
        while (d.getTime() <= t1) {
            if (d.getTime() >= t0) ticks.push(d.getTime());
            d.setUTCDate(d.getUTCDate() + k);
        }
    } else if (unit === 'month') {
        d.setUTCHours(0, 0, 0, 0);
        d.setUTCDate(1);
        while (d.getUTCMonth() % k !== 0) d.setUTCMonth(d.getUTCMonth() + 1);
        while (d.getTime() <= t1) {
            if (d.getTime() >= t0) ticks.push(d.getTime());
            d.setUTCMonth(d.getUTCMonth() + k);
        }
    } else {
        d.setUTCHours(0, 0, 0, 0);
        d.setUTCMonth(0, 1);
        while (d.getUTCFullYear() % k !== 0) d.setUTCFullYear(d.getUTCFullYear() + 1);
        while (d.getTime() <= t1) {
            if (d.getTime() >= t0) ticks.push(d.getTime());
            d.setUTCFullYear(d.getUTCFullYear() + k);
        }
    }
    const fmt = t => {
        const x = new Date(t);
        if (unit === 'year') return String(x.getUTCFullYear());
        if (unit === 'month') {
            return x.getUTCMonth() === 0 || k >= 6
                ? ST_MON[x.getUTCMonth()] + ' ' + x.getUTCFullYear()
                : ST_MON[x.getUTCMonth()];
        }
        return ST_MON[x.getUTCMonth()] + ' ' + x.getUTCDate();
    };
    return ticks.map(t => ({ t, label: fmt(t) }));
}

function stRangeStart(recs) {
    const first = Math.min(...recs.map(r => r.created));
    if (stState.range === 'all') return first;
    return Math.max(first, ST_NOW - parseInt(stState.range, 10) * ST_DAY);
}

function stSeg(id, options, current) {
    return '<span class="st-seg" id="' + id + '">'
        + options.map(([v, label]) =>
            '<button data-v="' + v + '" class="' + (v === current ? 'active' : '') + '">'
            + label + '</button>').join('')
        + '</span>';
}

function stSegBind(id, onPick) {
    const el = document.getElementById(id);
    el.addEventListener('click', e => {
        const b = e.target.closest('button');
        if (!b) return;
        el.querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
        onPick(b.dataset.v);
    });
}

function stWidth(el) {
    return Math.max(320, Math.floor(el.clientWidth));
}

// ── build ──
function buildStats() {
    const view = document.getElementById('stats-view');
    stRecs = ST_SUM ? stBuildRecords() : [];
    if (!stRecs.length) {
        view.innerHTML = '<div class="st-empty">' + (ST_SUM ? 'No timestamp data in this graph.'
            : 'This graph was built without statistics; rebuild it to see them.') + '</div>';
        stState.built = true;
        return;
    }
    const noReviews = G.review_activity === false;
    view.innerHTML = ''
        + '<div class="st-kpis" id="st-kpis"></div>'
        + '<section class="st-card">'
        +   '<div class="st-head"><h3>Patches over time</h3>'
        +     '<span class="st-sub">cumulative opened, merged and abandoned; shaded: open at that moment</span>'
        +     stSeg('st-range', ST_RANGES, stState.range) + '</div>'
        +   '<div class="st-chart" id="st-timeline"></div>'
        +   '<div class="st-legend">'
        +     stLegendItem(ST_COL.opened, 'opened') + stLegendItem(ST_COL.merged, 'merged')
        +     stLegendItem(ST_COL.abandoned, 'abandoned', true) + stLegendItem(ST_COL.backlog, 'open', false, true)
        +   '</div>'
        + '</section>'
        + '<section class="st-card">'
        +   '<div class="st-head"><h3>Throughput</h3><span class="st-sub" id="st-thru-sub"></span></div>'
        +   '<div class="st-chart" id="st-throughput"></div>'
        +   '<div class="st-legend">'
        +     stLegendItem(ST_COL.opened, 'opened') + stLegendItem(ST_COL.merged, 'merged')
        +     stLegendItem(ST_COL.abandoned, 'abandoned')
        +   '</div>'
        + '</section>'
        + '<div class="st-row">'
        +   '<section class="st-card">'
        +     '<div class="st-head"><h3>Activity, last 12 months</h3>'
        +       stSeg('st-heat-mode', [['uploads', 'Patchset uploads'], ['reviews', 'Human reviews']], stState.heatMode)
        +     '</div>'
        +     '<div class="st-chart" id="st-heatmap"></div>'
        +   '</section>'
        + '</div>'
        + '<div class="st-row st-two">'
        +   '<section class="st-card">'
        +     '<div class="st-head"><h3>How long things take</h3>'
        +       stSeg('st-dist-mode', [['merge', 'Upload → merge'], ['review', 'Upload → first review'], ['ps', 'Patchsets to merge']], stState.distMode)
        +     '</div>'
        +     '<div class="st-sub st-dist-sum" id="st-dist-sum"></div>'
        +     '<div class="st-chart" id="st-dist"></div>'
        +   '</section>'
        +   '<section class="st-card">'
        +     '<div class="st-head"><h3>Open patches: age vs. idle</h3>'
        +       '<span class="st-sub">idle = since the last upload or human review; click a dot to open it</span></div>'
        +     '<div class="st-chart" id="st-scatter"></div>'
        +     '<div class="st-legend">'
        +       Object.values(ST_HEALTH).map(([c, l]) => stLegendItem(c, l, false, false, true)).join('')
        +     '</div>'
        +   '</section>'
        + '</div>'
        + '<section class="st-card">'
        +   '<div class="st-head"><h3>Waiting longest</h3>'
        +     '<span class="st-sub">open patches, longest since the last upload or human review first</span></div>'
        +   '<div id="st-attn"></div>'
        + '</section>'
        + '<div class="st-row st-three">'
        +   '<section class="st-card"><div class="st-head"><h3>Tickets</h3></div><div id="st-tickets"></div></section>'
        +   '<section class="st-card"><div class="st-head"><h3>Authors</h3><span class="st-sub">change owners</span></div><div id="st-authors"></div></section>'
        +   '<section class="st-card"><div class="st-head"><h3>Reviewers</h3><span class="st-sub">human review messages, owner and bots excluded</span></div><div id="st-reviewers"></div></section>'
        + '</div>'
        + '<div class="st-notes" id="st-notes"></div>';

    stSegBind('st-range', v => { stState.range = v; stDrawTimeline(); stDrawThroughput(); });
    stSegBind('st-heat-mode', v => { stState.heatMode = v; stDrawHeatmap(); });
    stSegBind('st-dist-mode', v => { stState.distMode = v; stDrawDist(); });
    if (noReviews) {
        document.querySelector('#st-heat-mode button[data-v="reviews"]').disabled = true;
        document.querySelector('#st-dist-mode button[data-v="review"]').disabled = true;
    }

    view.addEventListener('click', e => {
        const row = e.target.closest('[data-jump]');
        if (!row || e.target.closest('a')) return;
        stJumpTo(parseInt(row.dataset.jump, 10));
    });
    view.addEventListener('mouseleave', stTipHide);

    stState.built = true;
    stDrawKpis();
    stDrawAll();
    stDrawTables();
    stDrawNotes();
}

function stLegendItem(color, label, dashed, area, dot) {
    let sw;
    if (dot) sw = '<span class="st-lg-dot" style="background:' + color + '"></span>';
    else if (area) sw = '<span class="st-lg-area" style="background:' + color + '"></span>';
    else sw = '<span class="st-lg-line" style="border-top:2px ' + (dashed ? 'dashed' : 'solid') + ' ' + color + '"></span>';
    return '<span class="st-lg">' + sw + label + '</span>';
}

function stDrawAll() {
    if (!stRecs || !stRecs.length) return;
    stDrawTimeline();
    stDrawThroughput();
    stDrawHeatmap();
    stDrawDist();
    stDrawScatter();
}

// ── KPI tiles ──
function stDrawKpis() {
    const S = ST_SUM;
    const delta = (now, prev) => {
        if (now === prev) return '<span class="st-flat">same as the 30 days before</span>';
        const up = now > prev;
        return '<span class="' + (up ? 'st-up' : 'st-down') + '">' + (up ? '▲ up' : '▼ down')
            + '</span> from ' + prev + ' the 30 days before';
    };
    const tile = (label, value, sub, jump) =>
        '<div class="st-kpi' + (jump ? ' st-click" data-jump="' + jump : '') + '">'
        + '<div class="st-kpi-label">' + label + '</div>'
        + '<div class="st-kpi-value">' + value + '</div>'
        + '<div class="st-kpi-sub">' + sub + '</div></div>';
    const ttm = S.time_to_merge, ttr = S.time_to_first_review, psm = S.patchsets_to_merge;
    const oldest = S.oldest_open;

    const tiles = [
        tile('Patches', S.patches,
            '<span style="color:' + ST_COL.opened + '">' + S.open + ' open</span> · '
            + '<span style="color:' + ST_COL.merged + '">' + S.merged + ' merged</span> · '
            + S.abandoned + ' abandoned'),
        tile('Merged, last 30 days', S.last_30d.merged, delta(S.last_30d.merged, S.prev_30d.merged)),
        tile('Opened, last 30 days', S.last_30d.opened, delta(S.last_30d.opened, S.prev_30d.opened)),
        tile('Open now', S.open,
            S.open === S.open_30d_ago ? 'same as 30 days ago'
            : (S.open > S.open_30d_ago ? 'up' : 'down') + ' from ' + S.open_30d_ago + ' (30 days ago)'),
        tile('Median upload → merge', stDur(stMs(ttm.median)),
            ttm.count ? 'p90 ' + stDur(stMs(ttm.p90)) + ' · ' + stPlural(ttm.count, 'merged patch', 'merged patches')
            : 'nothing merged yet'),
        tile('Median upload → first review', ttr ? stDur(stMs(ttr.median)) : '—',
            !ttr ? 'needs change messages (built with --skip-ci-details)'
            : ttr.count ? 'p90 ' + stDur(stMs(ttr.p90)) + ' · ' + ttr.count + ' reviewed' : 'no human reviews found'),
        tile('Patchsets per merged patch', psm.count ? +psm.median.toFixed(1) : '—',
            psm.count ? 'median · max ' + psm.max : 'nothing merged yet'),
        oldest
            ? tile('Oldest open patch', stDur(ST_NOW - stMs(oldest.opened_at)),
                '#' + oldest.id + ' ' + esc(oldest.ticket || ''), oldest.id)
            : tile('Oldest open patch', '—', 'nothing open'),
    ];
    document.getElementById('st-kpis').innerHTML = tiles.join('');
}

// ── timeline ──
function stDrawTimeline() {
    const box = document.getElementById('st-timeline');
    const W = stWidth(box), H = 260;
    const m = { l: 44, r: 16, t: 12, b: 28 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const ev = stEvents(stRecs);
    const t0 = stRangeStart(stRecs), t1 = ST_NOW;
    const span = Math.max(t1 - t0, ST_DAY);
    const x = t => m.l + (t - t0) / span * iw;
    const total = ev.opened.length;
    const { top, ticks } = stYTicks(total, 5);
    const y = v => m.t + ih - v / top * ih;

    const openCount = t => stCountLE(ev.opened, t) - stCountLE(ev.merged, t) - stCountLE(ev.abandoned, t);
    // Step path through every event inside the window.
    const step = (series, fn) => {
        const pts = [t0, ...series.filter(t => t > t0 && t < t1), t1];
        let d = '';
        let prev = null;
        for (const t of pts) {
            const v = fn(t);
            const X = x(t).toFixed(1), Y = y(v).toFixed(1);
            if (prev === null) d += 'M' + X + ',' + Y;
            else d += 'H' + X + 'V' + Y;
            prev = v;
        }
        return d;
    };
    const allEv = stSorted([...ev.opened, ...ev.merged, ...ev.abandoned]);
    let area = step(allEv, openCount);
    area += 'V' + y(0).toFixed(1) + 'H' + x(t0).toFixed(1) + 'Z';

    let svg = '<svg width="' + W + '" height="' + H + '">';
    for (const v of ticks) {
        svg += '<line class="st-grid" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(v) + '" y2="' + y(v) + '"/>'
            + '<text class="st-axis-text" x="' + (m.l - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + v + '</text>';
    }
    for (const tk of stTimeTicks(t0, t1, Math.max(3, Math.floor(iw / 90)))) {
        svg += '<line class="st-tick" x1="' + x(tk.t) + '" x2="' + x(tk.t) + '" y1="' + (m.t + ih) + '" y2="' + (m.t + ih + 4) + '"/>'
            + '<text class="st-axis-text" x="' + x(tk.t) + '" y="' + (H - 8) + '" text-anchor="middle">' + tk.label + '</text>';
    }
    svg += '<path d="' + area + '" fill="' + ST_COL.backlog + '" fill-opacity="0.18" stroke="none"/>';
    svg += '<path d="' + step(ev.abandoned, t => stCountLE(ev.abandoned, t)) + '" fill="none" stroke="' + ST_COL.abandoned + '" stroke-width="1.5" stroke-dasharray="4 3"/>';
    svg += '<path d="' + step(ev.merged, t => stCountLE(ev.merged, t)) + '" fill="none" stroke="' + ST_COL.merged + '" stroke-width="2"/>';
    svg += '<path d="' + step(ev.opened, t => stCountLE(ev.opened, t)) + '" fill="none" stroke="' + ST_COL.opened + '" stroke-width="2"/>';
    svg += '<line class="st-axis" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + (m.t + ih) + '" y2="' + (m.t + ih) + '"/>';
    svg += '<line id="st-tl-cross" class="st-cross" y1="' + m.t + '" y2="' + (m.t + ih) + '" x1="-10" x2="-10"/>';
    svg += '<rect class="st-hit" x="' + m.l + '" y="' + m.t + '" width="' + iw + '" height="' + ih + '"/>';
    svg += '</svg>';
    box.innerHTML = svg;

    const hit = box.querySelector('.st-hit');
    const cross = box.querySelector('#st-tl-cross');
    hit.addEventListener('mousemove', e => {
        const r = hit.getBoundingClientRect();
        const t = t0 + (e.clientX - r.left) / r.width * span;
        cross.setAttribute('x1', x(t));
        cross.setAttribute('x2', x(t));
        const o = stCountLE(ev.opened, t), mg = stCountLE(ev.merged, t), ab = stCountLE(ev.abandoned, t);
        stTip('<b>' + stDate(t) + '</b>'
            + '<div><span class="st-sw" style="background:' + ST_COL.opened + '"></span>opened ' + o + '</div>'
            + '<div><span class="st-sw" style="background:' + ST_COL.merged + '"></span>merged ' + mg + '</div>'
            + '<div><span class="st-sw" style="background:' + ST_COL.abandoned + '"></span>abandoned ' + ab + '</div>'
            + '<div><span class="st-sw" style="background:' + ST_COL.backlog + ';opacity:.5"></span>open ' + (o - mg - ab) + '</div>', e);
    });
    hit.addEventListener('mouseleave', () => {
        cross.setAttribute('x1', -10);
        cross.setAttribute('x2', -10);
        stTipHide();
    });
}

// ── throughput ──
function stBuckets(t0, t1) {
    const span = (t1 - t0) / ST_DAY;
    const unit = span <= 200 ? 'week' : span <= 1100 ? 'month' : 'quarter';
    const starts = [];
    const d = new Date(t0);
    d.setUTCHours(0, 0, 0, 0);
    if (unit === 'week') {
        while (d.getUTCDay() !== 1) d.setUTCDate(d.getUTCDate() - 1);
    } else {
        d.setUTCDate(1);
        if (unit === 'quarter') d.setUTCMonth(d.getUTCMonth() - d.getUTCMonth() % 3);
    }
    while (d.getTime() <= t1) {
        starts.push(d.getTime());
        if (unit === 'week') d.setUTCDate(d.getUTCDate() + 7);
        else d.setUTCMonth(d.getUTCMonth() + (unit === 'quarter' ? 3 : 1));
    }
    starts.push(d.getTime());
    return { unit, starts };
}

function stBucketLabel(unit, t) {
    const d = new Date(t);
    if (unit === 'week') return 'week of ' + stDate(t);
    if (unit === 'quarter') return 'Q' + (Math.floor(d.getUTCMonth() / 3) + 1) + ' ' + d.getUTCFullYear();
    return ST_MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear();
}

function stDrawThroughput() {
    const box = document.getElementById('st-throughput');
    const W = stWidth(box), H = 200;
    const m = { l: 44, r: 16, t: 10, b: 28 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const ev = stEvents(stRecs);
    const t0 = stRangeStart(stRecs), t1 = ST_NOW;
    const { unit, starts } = stBuckets(t0, t1);
    const nb = starts.length - 1;
    const rows = [];
    for (let i = 0; i < nb; i++) {
        const a = starts[i], b = starts[i + 1];
        const cnt = s => stCountLE(s, b - 1) - stCountLE(s, a - 1);
        rows.push({ a, b, opened: cnt(ev.opened), merged: cnt(ev.merged), abandoned: cnt(ev.abandoned) });
    }
    const max = Math.max(1, ...rows.map(r => Math.max(r.opened, r.merged, r.abandoned)));
    const { top, ticks } = stYTicks(max, 4);
    const y = v => m.t + ih - v / top * ih;
    const bw = iw / nb;
    const gap = Math.min(4, bw * 0.2);
    const barW = Math.max(1, (bw - gap) / 3);
    document.getElementById('st-thru-sub').textContent = 'per ' + unit;

    let svg = '<svg width="' + W + '" height="' + H + '">';
    for (const v of ticks) {
        svg += '<line class="st-grid" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(v) + '" y2="' + y(v) + '"/>'
            + '<text class="st-axis-text" x="' + (m.l - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + v + '</text>';
    }
    rows.forEach((r, i) => {
        const x0 = m.l + i * bw + gap / 2;
        [['opened', ST_COL.opened], ['merged', ST_COL.merged], ['abandoned', ST_COL.abandoned]].forEach(([k, c], j) => {
            if (!r[k]) return;
            svg += '<rect x="' + (x0 + j * barW).toFixed(1) + '" y="' + y(r[k]).toFixed(1) + '" width="' + Math.max(1, barW - 0.5).toFixed(1)
                + '" height="' + (y(0) - y(r[k])).toFixed(1) + '" fill="' + c + '" rx="1"/>';
        });
        svg += '<rect class="st-bucket" data-i="' + i + '" x="' + (m.l + i * bw).toFixed(1) + '" y="' + m.t + '" width="' + bw.toFixed(1) + '" height="' + ih + '"/>';
    });
    const labelEvery = Math.max(1, Math.ceil(nb / Math.max(2, Math.floor(iw / 70))));
    rows.forEach((r, i) => {
        if (i % labelEvery) return;
        const d = new Date(r.a);
        const lab = unit === 'week' ? ST_MON[d.getUTCMonth()] + ' ' + d.getUTCDate()
            : unit === 'quarter' ? 'Q' + (Math.floor(d.getUTCMonth() / 3) + 1) + " '" + String(d.getUTCFullYear()).slice(2)
            : ST_MON[d.getUTCMonth()] + " '" + String(d.getUTCFullYear()).slice(2);
        svg += '<text class="st-axis-text" x="' + (m.l + (i + 0.5) * bw).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle">' + lab + '</text>';
    });
    svg += '<line class="st-axis" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(0) + '" y2="' + y(0) + '"/>';
    svg += '</svg>';
    box.innerHTML = svg;

    box.onmousemove = e => {
        const i = e.target.dataset && e.target.dataset.i;
        if (i === undefined) { stTipHide(); return; }
        box.querySelectorAll('.st-bucket.on').forEach(el => el.classList.remove('on'));
        e.target.classList.add('on');
        const r = rows[+i];
        stTip('<b>' + stBucketLabel(unit, r.a) + '</b>'
            + '<div><span class="st-sw" style="background:' + ST_COL.opened + '"></span>opened ' + r.opened + '</div>'
            + '<div><span class="st-sw" style="background:' + ST_COL.merged + '"></span>merged ' + r.merged + '</div>'
            + '<div><span class="st-sw" style="background:' + ST_COL.abandoned + '"></span>abandoned ' + r.abandoned + '</div>', e);
    };
    box.onmouseleave = () => {
        box.querySelectorAll('.st-bucket.on').forEach(el => el.classList.remove('on'));
        stTipHide();
    };
}

// ── activity heatmap ──
function stDrawHeatmap() {
    const box = document.getElementById('st-heatmap');
    const reviews = stState.heatMode === 'reviews';
    const counts = new Map();
    const perDay = new Map();
    for (const r of stRecs) {
        for (const t of (reviews ? r.rv : r.ps)) {
            const day = Math.floor(t / ST_DAY);
            counts.set(day, (counts.get(day) || 0) + 1);
            if (!perDay.has(day)) perDay.set(day, new Set());
            perDay.get(day).add(r.id);
        }
    }
    const today = Math.floor(ST_NOW / ST_DAY);
    // 53 week columns ending with the week containing the build day; rows Mon..Sun.
    const todayDow = (new Date(today * ST_DAY).getUTCDay() + 6) % 7;
    const firstDay = today - todayDow - 52 * 7;
    const W = stWidth(box);
    const cols = 53, lw = 30;
    const cell = Math.max(8, Math.min(16, Math.floor((W - lw - 8) / cols) - 2));
    const pitch = cell + 2;
    const H = 18 + 7 * pitch + 4;
    const vals = [];
    for (let d = firstDay; d <= today; d++) if (counts.get(d)) vals.push(counts.get(d));
    vals.sort((a, b) => a - b);
    const q = [0.25, 0.5, 0.75].map(p => stQuantile(vals, p));
    const level = v => !v ? 0 : v <= q[0] ? 1 : v <= q[1] ? 2 : v <= q[2] ? 3 : 4;
    const color = reviews ? ST_COL.reviews : ST_COL.opened;
    const alpha = [0, 0.3, 0.5, 0.75, 1];

    let svg = '<svg width="' + (lw + cols * pitch + 4) + '" height="' + H + '">';
    let lastMonth = -1, lastLabelCol = -99;
    for (let c = 0; c < cols; c++) {
        const d0 = firstDay + c * 7;
        const mo = new Date(d0 * ST_DAY).getUTCMonth();
        if (mo !== lastMonth) {
            if (c - lastLabelCol >= 3 && c < cols - 1) {
                svg += '<text class="st-axis-text" x="' + (lw + c * pitch) + '" y="11">' + ST_MON[mo] + '</text>';
                lastLabelCol = c;
            }
            lastMonth = mo;
        }
        for (let r = 0; r < 7; r++) {
            const day = d0 + r;
            if (day > today) continue;
            const v = counts.get(day) || 0;
            const lv = level(v);
            svg += '<rect data-day="' + day + '" x="' + (lw + c * pitch) + '" y="' + (18 + r * pitch) + '" width="' + cell
                + '" height="' + cell + '" rx="2" class="' + (lv ? 'st-heat' : 'st-heat st-heat-empty') + '"'
                + (lv ? ' fill="' + color + '" fill-opacity="' + alpha[lv] + '"' : '') + '/>';
        }
    }
    ['Mon', 'Wed', 'Fri'].forEach((l, i) => {
        svg += '<text class="st-axis-text" x="0" y="' + (18 + (i * 2) * pitch + cell - 1) + '">' + l + '</text>';
    });
    svg += '</svg>';
    const total = vals.reduce((a, b) => a + b, 0);
    const busiest = [...counts.entries()].filter(([d]) => d >= firstDay).sort((a, b) => b[1] - a[1])[0];
    box.innerHTML = svg + '<div class="st-heat-foot">'
        + (reviews ? stPlural(total, 'human review message') : stPlural(total, 'patchset upload'))
        + ' in the last 12 months across ' + stPlural(vals.length, 'day')
        + (busiest ? ' · busiest ' + stDate(busiest[0] * ST_DAY) + ' (' + busiest[1] + ')' : '')
        + '<span class="st-heat-scale">less'
        + alpha.map((a, i) => '<span style="background:' + (i ? color : 'var(--bg-hover)') + ';opacity:' + (i ? a : 1) + '"></span>').join('')
        + 'more</span></div>';

    box.onmousemove = e => {
        const day = e.target.dataset && e.target.dataset.day;
        if (day === undefined) { stTipHide(); return; }
        const v = counts.get(+day) || 0;
        const ids = perDay.get(+day);
        const list = ids ? [...ids].slice(0, 6).map(id => '#' + id).join(' ') + (ids.size > 6 ? ' …' : '') : '';
        stTip('<b>' + stDate(+day * ST_DAY) + '</b><div>' + (reviews ? stPlural(v, 'review message') : stPlural(v, 'upload'))
            + '</div>' + (list ? '<div class="st-tip-dim">' + list + '</div>' : ''), e);
    };
    box.onmouseleave = stTipHide;
}

// ── distributions ──
const ST_DUR_BUCKETS = [
    [0, 'under 1 d'], [1, '1–3 d'], [3, '3–7 d'], [7, '1–2 w'], [14, '2–4 w'],
    [28, '1–2 mo'], [61, '2–3 mo'], [91, '3–6 mo'], [182, '6–12 mo'], [365, 'over 1 y'],
];
const ST_PS_BUCKETS = [[1, '1'], [2, '2'], [3, '3'], [4, '4–5'], [6, '6–10'], [11, '11–20'], [21, '21–40'], [41, 'over 40']];

function stDrawDist() {
    const box = document.getElementById('st-dist');
    const mode = stState.distMode;
    let items, buckets, valueOf, color, what;
    if (mode === 'ps') {
        items = stRecs.filter(r => r.status === 'MERGED');
        valueOf = r => r.psCount;
        buckets = ST_PS_BUCKETS;
        color = ST_COL.merged;
        what = 'merged patches by final patchset number';
    } else if (mode === 'review') {
        items = stRecs.filter(r => !isNaN(r.firstReview));
        valueOf = r => (r.firstReview - r.created) / ST_DAY;
        buckets = ST_DUR_BUCKETS;
        color = ST_COL.reviews;
        what = 'patches by time from first upload to first human review';
    } else {
        items = stRecs.filter(r => r.status === 'MERGED' && !isNaN(r.closed));
        valueOf = r => (r.closed - r.created) / ST_DAY;
        buckets = ST_DUR_BUCKETS;
        color = ST_COL.merged;
        what = 'merged patches by time from first upload to merge';
    }
    const groups = buckets.map(() => []);
    for (const r of items) {
        const v = valueOf(r);
        let k = 0;
        while (k + 1 < buckets.length && v >= buckets[k + 1][0]) k++;
        groups[k].push(r);
    }
    const sorted = stSorted(items.map(valueOf));
    const sum = document.getElementById('st-dist-sum');
    if (!items.length) {
        sum.textContent = mode === 'review' && G.review_activity === false
            ? 'Needs change messages (graph built with --skip-ci-details).' : 'No data.';
        box.innerHTML = '';
        return;
    }
    const f = mode === 'ps' ? v => (Math.round(v * 10) / 10) + ' ps' : v => stDur(v * ST_DAY);
    sum.textContent = items.length + ' ' + what + ' — median ' + f(stQuantile(sorted, 0.5))
        + ' · p75 ' + f(stQuantile(sorted, 0.75)) + ' · p90 ' + f(stQuantile(sorted, 0.9));

    const W = stWidth(box), H = 200;
    const m = { l: 36, r: 8, t: 14, b: 30 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const max = Math.max(1, ...groups.map(g => g.length));
    const { top, ticks } = stYTicks(max, 4);
    const y = v => m.t + ih - v / top * ih;
    const bw = iw / buckets.length;
    let svg = '<svg width="' + W + '" height="' + H + '">';
    for (const v of ticks) {
        svg += '<line class="st-grid" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(v) + '" y2="' + y(v) + '"/>'
            + '<text class="st-axis-text" x="' + (m.l - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + v + '</text>';
    }
    groups.forEach((g, i) => {
        const x0 = m.l + i * bw;
        if (g.length) {
            svg += '<rect x="' + (x0 + 3).toFixed(1) + '" y="' + y(g.length).toFixed(1) + '" width="' + Math.max(1, bw - 6).toFixed(1)
                + '" height="' + (y(0) - y(g.length)).toFixed(1) + '" fill="' + color + '" rx="2"/>'
                + '<text class="st-bar-val" x="' + (x0 + bw / 2).toFixed(1) + '" y="' + (y(g.length) - 3).toFixed(1) + '" text-anchor="middle">' + g.length + '</text>';
        }
        svg += '<text class="st-axis-text" x="' + (x0 + bw / 2).toFixed(1) + '" y="' + (H - 12) + '" text-anchor="middle">' + buckets[i][1] + '</text>';
        svg += '<rect class="st-bucket" data-i="' + i + '" x="' + x0.toFixed(1) + '" y="' + m.t + '" width="' + bw.toFixed(1) + '" height="' + ih + '"/>';
    });
    svg += '<line class="st-axis" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(0) + '" y2="' + y(0) + '"/>';
    svg += '</svg>';
    box.innerHTML = svg;

    box.onmousemove = e => {
        const i = e.target.dataset && e.target.dataset.i;
        if (i === undefined) { stTipHide(); return; }
        const g = groups[+i].slice().sort((a, b) => valueOf(b) - valueOf(a));
        const list = g.slice(0, 8).map(r => '<div class="st-tip-row">#' + r.id + ' <span class="st-tip-dim">'
            + (mode === 'ps' ? r.psCount + ' ps' : stDur(valueOf(r) * ST_DAY)) + '</span> '
            + esc((r.node.subject || '').slice(0, 48)) + '</div>').join('');
        stTip('<b>' + buckets[+i][1] + ': ' + stPlural(g.length, 'patch', 'patches') + '</b>'
            + list + (g.length > 8 ? '<div class="st-tip-dim">+' + (g.length - 8) + ' more</div>' : ''), e);
    };
    box.onmouseleave = stTipHide;
}

// ── scatter: open patches, age vs idle ──
function stDrawScatter() {
    const box = document.getElementById('st-scatter');
    const open = stRecs.filter(r => r.status === 'NEW');
    if (!open.length) {
        box.innerHTML = '<div class="st-empty">No open patches.</div>';
        return;
    }
    const W = stWidth(box), H = 280;
    const m = { l: 48, r: 14, t: 12, b: 34 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const pts = open.map(r => ({
        r,
        age: Math.max(0.1, (ST_NOW - r.created) / ST_DAY),
        idle: Math.max(0.1, (ST_NOW - r.lastHuman) / ST_DAY),
    }));
    const maxV = Math.max(30, ...pts.map(p => Math.max(p.age, p.idle)));
    const lmin = Math.log10(0.5), lmax = Math.log10(maxV * 1.3);
    const sx = v => m.l + (Math.log10(Math.max(0.5, v)) - lmin) / (lmax - lmin) * iw;
    const sy = v => m.t + ih - (Math.log10(Math.max(0.5, v)) - lmin) / (lmax - lmin) * ih;
    const tickVals = [[1, '1 d'], [7, '1 w'], [30, '1 mo'], [90, '3 mo'], [365, '1 y'], [730, '2 y'], [1825, '5 y']]
        .filter(([v]) => v <= maxV * 1.3);

    let svg = '<svg width="' + W + '" height="' + H + '">';
    // "Stuck" quadrant: older than a month AND no human activity for a month.
    svg += '<rect class="st-quad" x="' + sx(30) + '" y="' + m.t + '" width="' + (m.l + iw - sx(30)) + '" height="' + (sy(30) - m.t) + '"/>';
    svg += '<text class="st-quad-label" x="' + (m.l + iw - 6) + '" y="' + (m.t + 14) + '" text-anchor="end">untouched for a month or more</text>';
    for (const [v, l] of tickVals) {
        svg += '<line class="st-grid" x1="' + sx(v) + '" x2="' + sx(v) + '" y1="' + m.t + '" y2="' + (m.t + ih) + '"/>'
            + '<text class="st-axis-text" x="' + sx(v) + '" y="' + (m.t + ih + 14) + '" text-anchor="middle">' + l + '</text>'
            + '<line class="st-grid" x1="' + m.l + '" x2="' + (m.l + iw) + '" y1="' + sy(v) + '" y2="' + sy(v) + '"/>'
            + '<text class="st-axis-text" x="' + (m.l - 6) + '" y="' + (sy(v) + 4) + '" text-anchor="end">' + l + '</text>';
    }
    svg += '<line class="st-diag" x1="' + sx(0.5) + '" y1="' + sy(0.5) + '" x2="' + sx(maxV * 1.3) + '" y2="' + sy(maxV * 1.3) + '"/>';
    svg += '<text class="st-axis-text" x="' + (m.l + iw / 2) + '" y="' + (H - 4) + '" text-anchor="middle">age (since first upload)</text>';
    svg += '<text class="st-axis-text" transform="rotate(-90)" x="' + -(m.t + ih / 2) + '" y="11" text-anchor="middle">idle</text>';
    // Largest first so small dots stay on top.
    pts.sort((a, b) => b.r.psCount - a.r.psCount);
    for (const p of pts) {
        const rad = Math.min(9, 3 + Math.sqrt(p.r.psCount));
        const c = (ST_HEALTH[p.r.health] || ST_HEALTH.pending)[0];
        svg += '<circle data-jump="' + p.r.id + '" data-id="' + p.r.id + '" class="st-dot" cx="' + sx(p.age).toFixed(1) + '" cy="' + sy(p.idle).toFixed(1)
            + '" r="' + rad.toFixed(1) + '" fill="' + c + '"/>';
    }
    svg += '</svg>';
    box.innerHTML = svg;
    const byId = new Map(pts.map(p => [p.r.id, p]));
    box.onmousemove = e => {
        const id = e.target.dataset && e.target.dataset.id;
        if (id === undefined) { stTipHide(); return; }
        const p = byId.get(+id);
        const h = ST_HEALTH[p.r.health] || ST_HEALTH.pending;
        stTip('<b>#' + p.r.id + '</b> ' + esc(p.r.node.subject || '')
            + '<div class="st-tip-dim">' + esc(p.r.owner) + ' · ps' + p.r.psCount + '</div>'
            + '<div>age ' + stDur(p.age * ST_DAY) + ' · idle ' + stDur(p.idle * ST_DAY) + '</div>'
            + '<div><span class="st-sw" style="background:' + h[0] + '"></span>' + h[1] + '</div>', e);
    };
    box.onmouseleave = stTipHide;
}

// ── tables ──
function stHealthCell(h) {
    const v = ST_HEALTH[h] || ST_HEALTH.pending;
    return '<span class="st-sw" style="background:' + v[0] + '"></span>' + v[1];
}

function stDrawTables() {
    // Waiting longest
    const open = stRecs.filter(r => r.status === 'NEW')
        .sort((a, b) => a.lastHuman - b.lastHuman);
    const shown = stState.attnAll ? open : open.slice(0, 12);
    const attn = document.getElementById('st-attn');
    if (!open.length) {
        attn.innerHTML = '<div class="st-empty">No open patches.</div>';
    } else {
        attn.innerHTML = '<div class="st-table-wrap"><table class="st-table"><thead><tr>'
            + '<th>Change</th><th>Ticket</th><th>Owner</th><th>State</th>'
            + '<th class="num">Idle</th><th class="num">Age</th><th class="num">PS</th><th class="num" title="unresolved review comments">Unres.</th>'
            + '</tr></thead><tbody>'
            + shown.map(r => '<tr data-jump="' + r.id + '" class="st-click">'
                + '<td class="st-subj"><a href="' + esc(r.node.url) + '" target="_blank" rel="noopener">#' + r.id + '</a> '
                + esc(r.node.subject || '') + '</td>'
                + '<td>' + esc(r.ticket) + '</td><td>' + esc(r.owner) + '</td>'
                + '<td class="st-nowrap">' + stHealthCell(r.health) + '</td>'
                + '<td class="num">' + stDur(ST_NOW - r.lastHuman) + '</td>'
                + '<td class="num">' + stDur(ST_NOW - r.created) + '</td>'
                + '<td class="num">' + r.psCount + '</td>'
                + '<td class="num">' + (r.unresolved || '') + '</td></tr>').join('')
            + '</tbody></table></div>'
            + (open.length > 12
                ? '<button class="st-more" id="st-attn-more">' + (stState.attnAll ? 'Show fewer' : 'Show all ' + open.length) + '</button>'
                : '');
        const more = document.getElementById('st-attn-more');
        if (more) more.addEventListener('click', () => { stState.attnAll = !stState.attnAll; stDrawTables(); });
    }

    // Tickets
    const tk = new Map();
    for (const r of stRecs) {
        const k = r.ticket || '(none)';
        if (!tk.has(k)) tk.set(k, []);
        tk.get(k).push(r);
    }
    const tickRows = [...tk.entries()].map(([k, rs]) => {
        const ttm = stSorted(rs.filter(r => r.status === 'MERGED').map(r => r.closed - r.created));
        return {
            k,
            n: rs.length,
            open: rs.filter(r => r.status === 'NEW').length,
            merged: rs.filter(r => r.status === 'MERGED').length,
            abandoned: rs.filter(r => r.status === 'ABANDONED').length,
            ttm: stQuantile(ttm, 0.5),
            last: Math.max(...rs.map(r => Math.max(r.lastHuman, isNaN(r.closed) ? 0 : r.closed))),
        };
    }).sort((a, b) => b.n - a.n || b.last - a.last);
    document.getElementById('st-tickets').innerHTML = '<div class="st-table-wrap st-scroll"><table class="st-table"><thead><tr>'
        + '<th>Ticket</th><th class="num">Patches</th><th class="num">Open</th><th class="num">Merged</th>'
        + '<th class="num">Aband.</th><th class="num">Median → merge</th><th class="num">Last activity</th></tr></thead><tbody>'
        + tickRows.map(t => '<tr><td>' + esc(t.k) + '</td><td class="num">' + t.n + '</td><td class="num">' + (t.open || '')
            + '</td><td class="num">' + (t.merged || '') + '</td><td class="num">' + (t.abandoned || '')
            + '</td><td class="num">' + stDur(t.ttm) + '</td><td class="num">' + stDate(t.last) + '</td></tr>').join('')
        + '</tbody></table></div>';

    // People: owners and human reviewers
    const own = new Map();
    for (const r of stRecs) {
        if (!own.has(r.owner)) own.set(r.owner, []);
        own.get(r.owner).push(r);
    }
    const ownRows = [...own.entries()].map(([name, rs]) => ({
        name,
        n: rs.length,
        open: rs.filter(r => r.status === 'NEW').length,
        merged: rs.filter(r => r.status === 'MERGED').length,
        ttm: stQuantile(stSorted(rs.filter(r => r.status === 'MERGED').map(r => r.closed - r.created)), 0.5),
    })).sort((a, b) => b.n - a.n);
    const rev = new Map();
    for (const r of stRecs) {
        for (const [name, c] of Object.entries(r.node.reviewers || {})) {
            if (!rev.has(name)) rev.set(name, { name, msgs: 0, patches: 0 });
            const e = rev.get(name);
            e.msgs += c;
            e.patches += 1;
        }
    }
    const revRows = [...rev.values()].sort((a, b) => b.msgs - a.msgs);
    document.getElementById('st-authors').innerHTML = '<div class="st-table-wrap st-scroll"><table class="st-table"><thead><tr><th>Author</th><th class="num">Patches</th>'
        + '<th class="num">Open</th><th class="num">Merged</th><th class="num">Median → merge</th></tr></thead><tbody>'
        + ownRows.map(o => '<tr><td>' + esc(o.name) + '</td><td class="num">' + o.n + '</td><td class="num">' + (o.open || '')
            + '</td><td class="num">' + (o.merged || '') + '</td><td class="num">' + stDur(o.ttm) + '</td></tr>').join('')
        + '</tbody></table></div>';
    document.getElementById('st-reviewers').innerHTML = '<div class="st-table-wrap st-scroll"><table class="st-table"><thead><tr><th>Reviewer</th>'
        + '<th class="num" title="human review messages">Messages</th><th class="num">Patches</th></tr></thead><tbody>'
        + (revRows.length
            ? revRows.map(v => '<tr><td>' + esc(v.name) + '</td><td class="num">' + v.msgs + '</td><td class="num">' + v.patches + '</td></tr>').join('')
            : '<tr><td colspan="3" class="st-empty">' + (G.review_activity === false
                ? 'Needs change messages (built with --skip-ci-details).' : 'No human reviews found.') + '</td></tr>')
        + '</tbody></table></div>';
}

function stDrawNotes() {
    const approx = stRecs.filter(r => r.closedApprox).length;
    const structural = (G.stats.structural_merged_cns || []).length;
    const pruned = (G.stats.pruned_merged_cns || []).length;
    const notes = [
        'Covers the ' + stRecs.length + ' patches in this graph, including separate groups and abandoned patches.',
        'Opened = first patchset upload. Merged = submit time. Abandoned = the last abandon not followed by a restore.',
        'Human review = a message from someone other than the owner, excluding CI bots and generated messages (uploads, rebases, AI reviews).',
        'Times are relative to the build (' + esc(G.generated_at || stDate(ST_NOW)) + '), not to now.',
    ];
    if (approx) {
        notes.push(stPlural(approx, 'closed patch has', 'closed patches have')
            + ' no close event; the last update time stands in for it.');
    }
    if (structural || pruned) {
        const parts = [];
        if (structural) parts.push(stPlural(structural, 'unrelated merged base patch', 'unrelated merged base patches') + ' shown dimmed in the graph');
        if (pruned) parts.push(stPlural(pruned, 'unrelated merged patch', 'unrelated merged patches') + ' dropped from the trunk');
        notes.push('Not counted: ' + parts.join('; ') + '.');
    }
    if (G.review_activity === false) notes.push('Review activity is missing: this graph was built with --skip-ci-details.');
    document.getElementById('st-notes').innerHTML = notes.map(n => '<div>' + n + '</div>').join('');
}

// ── wiring ──
document.getElementById('tab-graph').addEventListener('click', () => showTab('graph'));
document.getElementById('tab-stats').addEventListener('click', () => showTab('stats'));
window.addEventListener('hashchange', () => showTab(location.hash === '#stats' ? 'stats' : 'graph'));
let stResizeTimer = null;
window.addEventListener('resize', () => {
    if (!isStatsTab() || !stState.built) return;
    clearTimeout(stResizeTimer);
    stResizeTimer = setTimeout(stDrawAll, 150);
});
if (location.hash === '#stats') {
    stState.graphNeedsFit = true;
    showTab('stats');
}
