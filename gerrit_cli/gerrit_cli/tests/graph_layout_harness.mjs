// Run a gc graph page's script headless and dump what vis.js is fed.
//
//   node graph_layout_harness.mjs PAGE.html [COMBO ...] [--eval EXPR]
//
// COMBO is "a<0|1>m<0|1>h<0|1>": Show abandoned, the layout (m1 =
// Trunk, m0 = Stacks; on pages from before the Trunk | Stacks switch,
// the "Show merged" checkbox) and Show historical parents; default:
// all 8.
// Prints JSON {combos: {COMBO: {nodes: [...], edges: [...]}}, errors,
// eval}: every node's position, label and style and every drawn edge,
// plus the layout phase that placed each node. --eval runs EXPR in
// the page after the combos and returns its value.
//
// The DOM and vis.js are stubs: this checks the data the page hands
// to vis.js, not pixels.
import fs from 'node:fs';
import vm from 'node:vm';

const argv = process.argv.slice(2);
const evalIdx = argv.indexOf('--eval');
const evalExpr = evalIdx >= 0 ? argv.splice(evalIdx, 2)[1] : null;
const [pagePath, ...comboArgs] = argv;
const html = fs.readFileSync(pagePath, 'utf8');
const m = html.match(/<script>\n([\s\S]*)<\/script>\s*<\/body>/);
if (!m) {
    console.error('no inline script found');
    process.exit(2);
}
const script = m[1];

function classList() {
    const s = new Set();
    return {
        add: (...c) => c.forEach(x => s.add(x)),
        remove: (...c) => c.forEach(x => s.delete(x)),
        toggle: (c, force) => {
            const on = force === undefined ? !s.has(c) : force;
            if (on) s.add(c); else s.delete(c);
            return on;
        },
        contains: c => s.has(c),
    };
}

function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function makeElement(id) {
    let text = '';
    const el = {
        id, checked: false, value: '', innerHTML: '', style: {},
        dataset: {}, children: [], classList: classList(),
        addEventListener() {}, removeEventListener() {},
        focus() {}, select() {}, blur() {}, click() {},
        appendChild(c) { this.children.push(c); return c; },
        removeChild() {}, setAttribute() {}, getAttribute() { return null; },
        querySelector() { return makeElement(''); },
        querySelectorAll() { return []; },
        closest() { return null; },
        getBoundingClientRect() {
            return { left: 0, top: 0, right: 800, bottom: 600,
                     width: 800, height: 600, x: 0, y: 0 };
        },
        scrollIntoView() {},
        get clientWidth() { return 800; },
        get clientHeight() { return 600; },
        get offsetWidth() { return 800; },
        get textContent() { return text; },
        set textContent(v) { text = String(v); this.innerHTML = escHtml(v); },
    };
    return el;
}

const elements = {};
const getEl = id => (elements[id] = elements[id] || makeElement(id));
// Checkbox defaults come from the page's own markup.
for (const cb of html.matchAll(/<input type="checkbox" id="([\w-]+)"( checked)?>/g)) {
    getEl(cb[1]).checked = !!cb[2];
}

class DataSet {
    constructor() { this.items = new Map(); }
    add(arr) { for (const it of [].concat(arr)) this.items.set(it.id, it); }
    clear() { this.items.clear(); }
    update(arr) {
        for (const it of [].concat(arr)) {
            this.items.set(it.id, Object.assign(this.items.get(it.id) || {}, it));
        }
    }
    get(id) { return id === undefined ? [...this.items.values()] : this.items.get(id); }
    getIds() { return [...this.items.keys()]; }
    forEach(fn) { this.items.forEach(v => fn(v)); }
    get length() { return this.items.size; }
}
// Like vis.js: selecting a node that isn't in the data set throws,
// focusing one logs an error.
class Network {
    constructor(container, data) { this.data = data || {}; }
    _has(id) { return !!(this.data.nodes && this.data.nodes.get(id)); }
    on() {} fit() {} redraw() {} unselectAll() {}
    selectNodes(ids) {
        for (const id of ids) {
            if (!this._has(id)) throw new RangeError(`Node with id "${id}" not found`);
        }
    }
    focus(id) {
        if (!this._has(id)) sandbox.console.error(`Node: ${id} cannot be found.`);
    }
    moveTo() {} getScale() { return 1; }
    getViewPosition() { return { x: 0, y: 0 }; }
    getPositions() { return {}; } getNodeAt() { return undefined; }
}

const document = {
    body: makeElement('body'),
    documentElement: makeElement('html'),
    getElementById: getEl,
    createElement: () => makeElement(''),
    createElementNS: () => makeElement(''),
    createTextNode: t => ({ textContent: t }),
    addEventListener() {}, removeEventListener() {},
    querySelector: () => makeElement(''),
    querySelectorAll: () => [],
};
const errors = [];
const sandbox = {
    document, console: { log() {}, warn() {}, error: (...a) => errors.push(a.join(' ')) },
    vis: { DataSet, Network },
    location: { hash: '' },
    navigator: { clipboard: { writeText() {} } },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    setTimeout: () => 0, clearTimeout() {}, requestAnimationFrame: () => 0,
    MouseEvent: class {}, Date, Math, JSON, Intl, Set, Map,
};
sandbox.window = sandbox;
sandbox.window.innerWidth = 1440;
sandbox.window.innerHeight = 900;
sandbox.window.addEventListener = () => {};
sandbox.window.matchMedia = () => ({ matches: false, addListener() {}, addEventListener() {} });
sandbox.window.devicePixelRatio = 1;
sandbox.window.getComputedStyle = () => ({ getPropertyValue: () => '' });
vm.createContext(sandbox);

try {
    vm.runInContext(script, sandbox, { filename: 'page.js' });
} catch (e) {
    errors.push('load: ' + (e.stack || e));
}

// Record which layout phase placed each node (diagnostics only; the
// wrapper is transparent to the layout).
try {
    vm.runInContext(`
        globalThis.__phaseOf = {};
        for (const name of ['_layoutAnchorColumn', '_layoutMergedTrunk',
                '_layoutUpwardFromAnchor', '_layoutTrunkSideBranches',
                '_layoutUnplacedMainSeries', '_layoutSeparateGroups',
                '_layoutStacks']) {
            if (typeof globalThis[name] !== 'function') continue;
            const orig = globalThis[name];
            globalThis[name] = function (ctx, ...rest) {
                const before = new Set(Object.keys(ctx.positions));
                const r = orig(ctx, ...rest);
                for (const k of Object.keys(ctx.positions)) {
                    if (!before.has(k)) globalThis.__phaseOf[k] = name;
                }
                return r;
            };
        }`, sandbox);
} catch (e) {
    errors.push('phase wrap: ' + (e.stack || e));
}

const combos = comboArgs.length ? comboArgs
    : ['a0m1h0', 'a1m1h0', 'a0m1h1', 'a1m1h1',
       'a0m0h0', 'a1m0h0', 'a0m0h1', 'a1m0h1'];
const out = {};
for (const c of combos) {
    const mm = /^a([01])m([01])h([01])$/.exec(c);
    if (!mm) { errors.push('bad combo ' + c); continue; }
    getEl('chk-abandoned').checked = mm[1] === '1';
    if (typeof sandbox.setLayout === 'function') {
        vm.runInContext(`setLayout('${mm[2] === '1' ? 'trunk' : 'stacks'}', false)`, sandbox);
    } else {
        getEl('chk-merged').checked = mm[2] === '1';
    }
    getEl('chk-history').checked = mm[3] === '1';
    try {
        vm.runInContext('globalThis.__phaseOf = {}; renderGraph()', sandbox);
        const phaseOf = vm.runInContext('globalThis.__phaseOf', sandbox);
        const nodes = vm.runInContext('nodesDS.get()', sandbox).map(n => ({
            phase: phaseOf[n.id] || null,
            id: n.id, x: n.x, y: n.y, label: n.label,
            bg: n.color && n.color.background, border: n.color && n.color.border,
            borderWidth: n.borderWidth, opacity: n.opacity,
            dashes: n.shapeProperties ? n.shapeProperties.borderDashes : null,
        })).sort((a, b) => a.id - b.id);
        const edges = vm.runInContext('edgesDS.get()', sandbox).map(e => ({
            from: e.from, to: e.to, label: e.label,
            color: e.color && e.color.color, width: e.width, dashes: e.dashes,
        })).sort((a, b) => (a.from - b.from) || (a.to - b.to));
        out[c] = { nodes, edges };
    } catch (e) {
        errors.push(c + ': ' + (e.stack || e));
    }
}
let evalResult = null;
if (evalExpr) {
    try { evalResult = vm.runInContext(evalExpr, sandbox); }
    catch (e) { errors.push('eval: ' + (e.stack || e)); }
}
process.stdout.write(JSON.stringify({ combos: out, errors, eval: evalResult }));
