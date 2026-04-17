/**
 * Graph renderer — 2D canvas-based agent graph for the Orb dashboard.
 *
 * Public API:
 *   new GraphRenderer(canvas)
 *   graph.setTopology(agents, edges)
 *   graph.updateAgentStatus(agentId, status, model)
 *   graph.animateEdge(source, target)
 *   graph.onNodeClick = (id, node) => {}
 *   graph.selectedNode   (settable)
 *   graph.startAnimation()
 */

// ── Constants ────────────────────────────────────────────────────────────────

const NODE_W = 156;
const NODE_H = 44;
const CORE_NODE_W = 168;
const CORE_NODE_H = 46;
const NODE_RADIUS = 14;          // pill-chip corner radius
const PARTICLE_DURATION = 900;   // ms

const AGENT_COLORS = {
    coordinator: '#94bfff',
    coder:       '#79a9ff',
    reviewer:    '#f0c982',
    reviewer_a:  '#f0c982',
    reviewer_b:  '#d8a86b',
    tester:      '#86d8ab',
    user:        '#b6d0c2',
};

const FALLBACK_COLORS = ['#b6d0c2', '#94bfff', '#f0c982', '#86d8ab'];

const STATUS_COLORS = {
    idle:      '#8796a7',
    running:   '#94bfff',
    completed: '#86d8ab',
    error:     '#f3afa7',
};

const STATUS_BADGE = {
    idle:      { bg: 'rgba(196,206,217,0.12)', fg: '#c4ced9' },
    running:   { bg: 'rgba(148,191,255,0.14)', fg: '#94bfff' },
    completed: { bg: 'rgba(134,216,171,0.16)', fg: '#86d8ab' },
    error:     { bg: 'rgba(243,175,167,0.16)', fg: '#f3afa7' },
    thinking:  { bg: 'rgba(182,208,194,0.14)', fg: '#b6d0c2' },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function _shortModel(modelId) {
    if (!modelId) return '';
    // claude-sonnet-4-5-20251001 → sonnet-4-5 · gpt-5.4 / qwen3.5:9b pass through
    const m = modelId.match(/^claude-([a-z]+-[\d]+(?:-[\d]+)?)/i);
    if (m) return m[1].toLowerCase();
    return modelId;
}

function agentColor(id) {
    if (AGENT_COLORS[id]) return AGENT_COLORS[id];
    let h = 0;
    for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) & 0xffff;
    return FALLBACK_COLORS[h % FALLBACK_COLORS.length];
}

function statusColor(status, fallback) {
    return STATUS_COLORS[status] || fallback;
}

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** Evaluate a cubic bezier at parameter t, returning {x, y}. */
function bezierPoint(p0, cp1, cp2, p1, t) {
    const mt = 1 - t;
    return {
        x: mt*mt*mt*p0.x + 3*mt*mt*t*cp1.x + 3*mt*t*t*cp2.x + t*t*t*p1.x,
        y: mt*mt*mt*p0.y + 3*mt*mt*t*cp1.y + 3*mt*t*t*cp2.y + t*t*t*p1.y,
    };
}

/** Compute control points for a bezier edge between two node centers. */
function edgeControlPoints(src, tgt) {
    const dx = tgt.x - src.x;
    const dy = tgt.y - src.y;
    const bend = Math.max(Math.abs(dx), Math.abs(dy)) * 0.35;
    // Perpendicular offset for a gentle S-curve
    const nx = -dy / (Math.hypot(dx, dy) || 1);
    const ny =  dx / (Math.hypot(dx, dy) || 1);
    return {
        cp1: { x: src.x + dx * 0.35 + nx * bend * 0.4, y: src.y + dy * 0.35 + ny * bend * 0.4 },
        cp2: { x: src.x + dx * 0.65 - nx * bend * 0.4, y: src.y + dy * 0.65 - ny * bend * 0.4 },
    };
}

/** Find the point on the border of a node in the direction of another point. */
function nodeEdgePoint(node, other) {
    const halfW = (node.w || NODE_W) / 2;
    const halfH = (node.h || NODE_H) / 2;
    const dx = other.x - node.x;
    const dy = other.y - node.y;

    if (Math.abs(dx) > Math.abs(dy)) {
        return {
            x: node.x + Math.sign(dx || 1) * halfW,
            y: node.y + clamp(dy, -halfH * 0.55, halfH * 0.55),
        };
    }

    return {
        x: node.x + clamp(dx, -halfW * 0.55, halfW * 0.55),
        y: node.y + Math.sign(dy || 1) * halfH,
    };
}

/** Draw a rounded rectangle path. */
function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y,     x + w, y + r,     r);
    ctx.lineTo(x + w, y + h - r);
    ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h);
    ctx.arcTo(x,     y + h, x,     y + h - r, r);
    ctx.lineTo(x,     y + r);
    ctx.arcTo(x,     y,     x + r, y,         r);
    ctx.closePath();
}

// Role glyphs drawn line-art style inside the chip.
// Each glyph is drawn in an 18×18 box; caller translates to desired anchor.
const ROLE_GLYPHS = {
    coordinator(ctx) {
        // Capital "A" — triangle silhouette with horizontal crossbar
        ctx.beginPath();
        ctx.moveTo(9, 2.5);
        ctx.lineTo(15.5, 15.5);
        ctx.lineTo(2.5, 15.5);
        ctx.closePath();
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(5.5, 11);
        ctx.lineTo(12.5, 11);
        ctx.stroke();
    },
    coder(ctx) {
        // Curly braces { }
        ctx.beginPath();
        ctx.moveTo(6, 3);
        ctx.bezierCurveTo(4, 3, 4, 6, 4, 7.5);
        ctx.bezierCurveTo(4, 8.5, 3, 9, 3, 9);
        ctx.bezierCurveTo(3, 9, 4, 9.5, 4, 10.5);
        ctx.bezierCurveTo(4, 12, 4, 15, 6, 15);
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(12, 3);
        ctx.bezierCurveTo(14, 3, 14, 6, 14, 7.5);
        ctx.bezierCurveTo(14, 8.5, 15, 9, 15, 9);
        ctx.bezierCurveTo(15, 9, 14, 9.5, 14, 10.5);
        ctx.bezierCurveTo(14, 12, 14, 15, 12, 15);
        ctx.stroke();
    },
    reviewer(ctx) {
        // Eye — lens outline + iris
        ctx.beginPath();
        ctx.moveTo(1.5, 9);
        ctx.bezierCurveTo(4, 4, 14, 4, 16.5, 9);
        ctx.bezierCurveTo(14, 14, 4, 14, 1.5, 9);
        ctx.closePath();
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(9, 9, 2.2, 0, Math.PI * 2);
        ctx.stroke();
    },
    tester(ctx) {
        // Checkbox with tick
        ctx.beginPath();
        ctx.moveTo(4.5, 2.5);
        ctx.lineTo(13.5, 2.5);
        ctx.arcTo(15.5, 2.5, 15.5, 4.5, 2);
        ctx.lineTo(15.5, 13.5);
        ctx.arcTo(15.5, 15.5, 13.5, 15.5, 2);
        ctx.lineTo(4.5, 15.5);
        ctx.arcTo(2.5, 15.5, 2.5, 13.5, 2);
        ctx.lineTo(2.5, 4.5);
        ctx.arcTo(2.5, 2.5, 4.5, 2.5, 2);
        ctx.closePath();
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(5.5, 9.2);
        ctx.lineTo(8, 11.7);
        ctx.lineTo(13, 6.5);
        ctx.stroke();
    },
    researcher(ctx) {
        // Magnifier — circle + handle
        ctx.beginPath();
        ctx.arc(8, 8, 5, 0, Math.PI * 2);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(12, 12);
        ctx.lineTo(15.5, 15.5);
        ctx.stroke();
    },
    generic(ctx) {
        // Lozenge — fallback glyph
        ctx.beginPath();
        ctx.moveTo(9, 2.5);
        ctx.lineTo(15.5, 9);
        ctx.lineTo(9, 15.5);
        ctx.lineTo(2.5, 9);
        ctx.closePath();
        ctx.stroke();
    },
};

/** Classify a node id or role into a glyph key. */
function glyphKeyFor(id, role) {
    const key = `${id} ${role || ''}`.toLowerCase();
    if (key.includes('coordinator') || key.includes('orchestr') || key.includes('planner')) return 'coordinator';
    if (key.includes('coder')   || key.includes('implement') || key.includes('writer'))      return 'coder';
    if (key.includes('review')  || key.includes('critic'))                                   return 'reviewer';
    if (key.includes('test')    || key.includes('valid'))                                    return 'tester';
    if (key.includes('research')|| key.includes('search')  || key.includes('investigat'))    return 'researcher';
    return 'generic';
}

/** Blend `hex` toward `fallback` by `mix` (0–1). Returns rgb() string or `fallback` on parse fail. */
function tintColor(hex, mix, fallback) {
    const m = /^#?([a-f0-9]{6})$/i.exec(hex || '');
    if (!m) return fallback;
    const n = parseInt(m[1], 16);
    const r = (n >> 16) & 0xff, g = (n >> 8) & 0xff, b = n & 0xff;
    const fm = /^#?([a-f0-9]{6})$/i.exec(fallback || '#000000');
    const fn = fm ? parseInt(fm[1], 16) : 0;
    const fr = (fn >> 16) & 0xff, fg = (fn >> 8) & 0xff, fb = fn & 0xff;
    const tr = Math.round(r * mix + fr * (1 - mix));
    const tg = Math.round(g * mix + fg * (1 - mix));
    const tb = Math.round(b * mix + fb * (1 - mix));
    return `rgb(${tr}, ${tg}, ${tb})`;
}

/** Convert a #rrggbb hex to rgba() with the given alpha. */
function alphaHex(hex, alpha) {
    const m = /^#?([a-f0-9]{6})$/i.exec(hex || '');
    if (!m) return `rgba(148, 191, 255, ${alpha})`;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 0xff}, ${(n >> 8) & 0xff}, ${n & 0xff}, ${alpha})`;
}

function drawRoleGlyph(ctx, cx, cy, color, key) {
    const draw = ROLE_GLYPHS[key] || ROLE_GLYPHS.generic;
    ctx.save();
    ctx.translate(cx - 9, cy - 9);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    draw(ctx);
    ctx.restore();
}

// ── GraphRenderer ────────────────────────────────────────────────────────────

class GraphRenderer {
    constructor(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');

        this.nodes = {};        // id -> node descriptor
        this.edges = [];        // [{source, target}]
        this.particles = [];    // active travel animations

        this.selectedNode = null;
        this.onNodeClick = null;
        this.graphView = null;

        this._hoveredId = null;
        this._rafId = null;
        this._dpr = window.devicePixelRatio || 1;
        this._gridCanvas = null;  // offscreen cache for dot grid
        this.zoom = 1;
        this.minZoom = 0.7;
        this.maxZoom = 1.8;
        this.offsetX = 0;
        this.offsetY = 0;
        this.runState = 'idle';
        this.runStateChangedAt = performance.now();
        this._layoutRows = [];
        this._layoutBands = [];
        this.theme = 'dark';

        this._setupEvents();
        this._resize();
    }

    setTheme(theme) {
        this.theme = theme;
        this._gridCanvas = null; // invalidate cached grid
    }

    destroy() {
        if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = null; }
        if (this._resizeObserver) this._resizeObserver.disconnect();
    }

    // ── Public API ───────────────────────────────────────────────────────────

    setTopology(agents, edges, graphView = null) {
        this.nodes = {};
        this.edges = edges || [];
        this.particles = [];
        this.graphView = graphView || null;
        this.selectedNode = null;
        this.zoom = 1;
        this.offsetX = 0;
        this.offsetY = 0;

        for (const agent of agents) {
            const id = agent.id;
            this.nodes[id] = {
                id,
                role:         agent.role || id,
                status:       agent.status || 'idle',
                model:        agent.model || '',
                msgCount:     agent.msg_count || 0,
                lastActivity: '',
                x: 0, y: 0,
                w: id === 'coordinator' ? CORE_NODE_W : NODE_W,
                h: id === 'coordinator' ? CORE_NODE_H : NODE_H,
                pulseStart: null,
                thinking:   false,
                color: agentColor(id),
            };
        }

        this._layoutNodes();
        this.startAnimation();
    }

    updateAgentStatus(agentId, status, model) {
        const n = this.nodes[agentId];
        if (!n) return;
        if (status) n.status = status;
        if (model)  n.model  = model;
    }

    updateAgentActivity(agentId, preview) {
        const n = this.nodes[agentId];
        if (n) n.lastActivity = preview || '';
    }

    setRunState(nextState) {
        const normalized = nextState || 'idle';
        if (this.runState === normalized) return;
        this.runState = normalized;
        this.runStateChangedAt = performance.now();
    }

    setNodeThinking(agentId, thinking) {
        const n = this.nodes[agentId];
        if (n) n.thinking = thinking;
    }

    animateEdge(source, target) {
        const src = this.nodes[source];
        const tgt = this.nodes[target];
        if (!src || !tgt) return;

        if (tgt) tgt.pulseStart = performance.now();
        if (src) src.msgCount = (src.msgCount || 0) + 1;

        // Cap particle queue to prevent unbounded growth if draw loop is throttled
        if (this.particles.length >= 100) this.particles.shift();

        this.particles.push({
            source,
            target,
            startTime: performance.now(),
            duration:  PARTICLE_DURATION,
        });
    }

    startAnimation() {
        if (this._rafId) return;
        const loop = () => {
            this._rafId = requestAnimationFrame(loop);
            this._draw();
        };
        loop();
    }

    // ── Layout ───────────────────────────────────────────────────────────────

    _layoutNodes() {
        const W = this.canvas.width  / this._dpr;
        const H = this.canvas.height / this._dpr;
        const ids = Object.keys(this.nodes);
        const n = ids.length;

        if (n === 0) return;
        if (this._layoutGraphView(W, H)) return;
        this._layoutLayered(W, H);
    }

    _layoutGraphView(W, H) {
        const rows = this.graphView?.rows;
        if (!Array.isArray(rows) || !rows.length) return false;
        const nodeIds = new Set(Object.keys(this.nodes));

        const populatedRows = rows.filter((row) => Array.isArray(row) && row.some((item) => item?.node));
        if (!populatedRows.length) return false;

        const laneTop = 116;
        const laneBottom = H - 92;
        const rowGap = populatedRows.length === 1
            ? 0
            : Math.max(116, Math.min(168, (laneBottom - laneTop) / Math.max(populatedRows.length - 1, 1)));
        const top = populatedRows.length === 1 ? H * 0.46 : laneTop;
        const placed = new Set();
        this._layoutRows = [];
        this._layoutBands = [];
        const left = 84;
        const right = W - 84;
        const usableWidth = Math.max(220, right - left);

        populatedRows.forEach((row, rowIndex) => {
            if (!Array.isArray(row)) return;
            const y = top + rowIndex * rowGap;
            const rowNodes = row
                .map((item) => item?.node)
                .filter(Boolean)
                .filter((id) => this.nodes[id]);
            if (!rowNodes.length) return;
            const count = rowNodes.length;
            const preferredGap = count <= 2 ? 268 : count <= 4 ? 220 : 194;
            const usedWidth = Math.min(usableWidth, Math.max(180, (count - 1) * preferredGap));
            const startX = W / 2 - usedWidth / 2;
            const step = count === 1 ? 0 : usedWidth / Math.max(count - 1, 1);
            this._layoutRows.push(y);
            this._layoutBands.push({ y, top: y - 44, bottom: y + 50 });

            rowNodes.forEach((id, index) => {
                const node = this.nodes[id];
                node.x = count === 1 ? W / 2 : startX + index * step;
                node.y = y;
                node.w = id === 'coordinator' ? CORE_NODE_W : NODE_W;
                node.h = id === 'coordinator' ? CORE_NODE_H : NODE_H;
                placed.add(id);
            });
        });

        const unplaced = [...nodeIds].filter((id) => !placed.has(id));
        if (!unplaced.length) return true;

        const fallbackY = Math.min(H - 72, top + rows.length * rowGap);
        const gap = W / (unplaced.length + 1);
        unplaced.forEach((id, index) => {
            this.nodes[id].x = gap * (index + 1);
            this.nodes[id].y = fallbackY;
            this.nodes[id].w = id === 'coordinator' ? CORE_NODE_W : NODE_W;
            this.nodes[id].h = id === 'coordinator' ? CORE_NODE_H : NODE_H;
        });
        return true;
    }

    _layoutLayered(W, H) {
        const ids = Object.keys(this.nodes);
        const root = this.nodes.coordinator ? 'coordinator' : (ids[0] || null);
        const outgoing = new Map(ids.map((id) => [id, []]));
        const incoming = new Map(ids.map((id) => [id, []]));
        const linked = new Map(ids.map((id) => [id, new Set()]));
        for (const edge of this.edges) {
            if (outgoing.has(edge.source)) outgoing.get(edge.source).push(edge.target);
            if (incoming.has(edge.target)) incoming.get(edge.target).push(edge.source);
            if (linked.has(edge.source) && linked.has(edge.target)) {
                linked.get(edge.source).add(edge.target);
                linked.get(edge.target).add(edge.source);
            }
        }

        const layers = new Map();
        if (root) {
            const queue = [root];
            layers.set(root, 0);
            while (queue.length) {
                const current = queue.shift();
                const depth = layers.get(current) || 0;
                for (const next of outgoing.get(current) || []) {
                    if (!layers.has(next)) {
                        layers.set(next, depth + 1);
                        queue.push(next);
                    }
                }
            }
        }

        const inferLayer = (id, role = '') => {
            const key = `${id} ${role}`.toLowerCase();
            if (key.includes('coordinator')) return 0;
            if (key.includes('research')) return 1;
            if (key.includes('coder') || key.includes('implement')) return 2;
            if (key.includes('review') || key.includes('test') || key.includes('validat')) return 3;
            return 2;
        };

        ids.forEach((id) => {
            if (layers.has(id)) return;
            const neighborLayers = [...(linked.get(id) || [])]
                .map((neighbor) => layers.get(neighbor))
                .filter((value) => Number.isFinite(value));
            if (neighborLayers.length) layers.set(id, Math.min(...neighborLayers) + 1);
            else layers.set(id, inferLayer(id, this.nodes[id].role));
        });

        const uniqueLayers = [...new Set([...layers.values()].sort((a, b) => a - b))];
        const positions = new Map(root ? [[root, 0]] : []);
        const rows = uniqueLayers.map((layer) => {
            const row = ids.filter((id) => layers.get(id) === layer);
            row.sort((a, b) => {
                const aParents = (incoming.get(a) || []).filter((id) => positions.has(id));
                const bParents = (incoming.get(b) || []).filter((id) => positions.has(id));
                const aPos = aParents.length ? aParents.reduce((sum, id) => sum + positions.get(id), 0) / aParents.length : Number.POSITIVE_INFINITY;
                const bPos = bParents.length ? bParents.reduce((sum, id) => sum + positions.get(id), 0) / bParents.length : Number.POSITIVE_INFINITY;
                if (aPos !== bPos) return aPos - bPos;
                return a.localeCompare(b);
            });
            row.forEach((id, index) => positions.set(id, index));
            return row;
        });
        const left = 72;
        const right = W - 72;
        const usableWidth = Math.max(240, right - left);
        const layoutLines = [];

        rows.forEach((row) => {
            const rowMaxPerLine = row.length <= 5
                ? row.length
                : Math.max(3, Math.floor((usableWidth + 44) / 184));
            for (let i = 0; i < row.length; i += rowMaxPerLine) {
                layoutLines.push(row.slice(i, i + rowMaxPerLine));
            }
        });

        const top = 118;
        const bottom = H - 88;
        const usableHeight = Math.max(180, bottom - top);
        const rowGap = layoutLines.length === 1
            ? 0
            : Math.max(124, Math.min(176, usableHeight / Math.max(layoutLines.length - 1, 1)));
        this._layoutRows = [];
        this._layoutBands = [];

        layoutLines.forEach((row, rowIndex) => {
            const y = layoutLines.length === 1 ? H * 0.48 : top + rowIndex * rowGap;
            const count = row.length;
            this._layoutRows.push(y);
            this._layoutBands.push({ y, top: y - 44, bottom: y + 50 });

            row.forEach((id, index) => {
                const node = this.nodes[id];
                node.w = id === 'coordinator' ? CORE_NODE_W : NODE_W;
                node.h = id === 'coordinator' ? CORE_NODE_H : NODE_H;
                node.y = y;
                node.x = count === 1 ? W / 2 : left + (usableWidth * (index + 0.5)) / count;
            });
        });

        const minGap = 186;
        for (let pass = 0; pass < 3; pass++) {
            layoutLines.forEach((row) => {
                const desired = new Map(row.map((id) => {
                    const parents = (incoming.get(id) || []).filter((parentId) => this.nodes[parentId]);
                    if (!parents.length) return [id, this.nodes[id].x];
                    const avgParentX = parents.reduce((sum, parentId) => sum + this.nodes[parentId].x, 0) / parents.length;
                    return [id, avgParentX];
                }));

                row.sort((a, b) => desired.get(a) - desired.get(b));

                let cursor = left + 40;
                row.forEach((id, index) => {
                    const target = clamp(desired.get(id), left + 40, right - 40);
                    const x = Math.max(cursor, target);
                    this.nodes[id].x = x;
                    cursor = x + minGap;
                });

                const overflow = cursor - minGap - (right - 40);
                if (overflow > 0) {
                    for (let i = row.length - 1; i >= 0; i--) {
                        const id = row[i];
                        const nextX = i === row.length - 1 ? right - 40 : this.nodes[row[i + 1]].x - minGap;
                        this.nodes[id].x = Math.min(this.nodes[id].x, nextX);
                    }
                }

                const rowCenter = row.reduce((sum, id) => sum + this.nodes[id].x, 0) / Math.max(1, row.length);
                const delta = W / 2 - rowCenter;
                row.forEach((id) => {
                    this.nodes[id].x = clamp(this.nodes[id].x + delta, left + 40, right - 40);
                });
            });
        }
    }

    // ── Draw ─────────────────────────────────────────────────────────────────

    _draw() {
        const ctx   = this.ctx;
        const W     = this.canvas.width  / this._dpr;
        const H     = this.canvas.height / this._dpr;

        ctx.save();
        ctx.scale(this._dpr, this._dpr);
        ctx.translate(this.offsetX, this.offsetY);
        ctx.scale(this.zoom, this.zoom);

        // 1. Background + dot grid (cached offscreen canvas, repainted only on resize)
        if (this._gridCanvas) {
            ctx.drawImage(this._gridCanvas, -this.offsetX / this.zoom, -this.offsetY / this.zoom, W / this.zoom, H / this.zoom);
        } else {
            ctx.fillStyle = this.theme === 'light' ? '#eef1f6' : '#0a0f13';
            ctx.fillRect(-this.offsetX / this.zoom, -this.offsetY / this.zoom, W / this.zoom, H / this.zoom);
        }

        this._drawCoreBackdrop(ctx, W, H);

        // 3. Edges
        this._drawEdges(ctx);

        // 4. Particles
        this._drawParticles(ctx);

        // 5. Nodes
        this._drawNodes(ctx);

        // 6. Network state
        this._drawNetworkStatus(ctx, W, H);

        // 7. Legend pinned bottom-left — reads like a key for the graph state glyphs.
        this._drawLegend(ctx, W, H);

        ctx.restore();
    }

    _drawLegend(ctx, W, H) {
        const isLight = this.theme === 'light';
        const muted     = isLight ? 'rgba(113,130,149,0.9)'  : 'rgba(135,150,167,0.85)';
        const live      = isLight ? '#2563eb'                : '#94bfff';
        const legendY   = H - 18;
        const worldLeft = -this.offsetX / this.zoom;
        let x = worldLeft + 20;

        const items = [
            { glyph: '●', color: live,   label: 'live' },
            { glyph: '○', color: muted,  label: 'idle' },
            { glyph: '→', color: muted,  label: 'message' },
            { glyph: '—', color: muted,  label: 'edge' },
        ];

        ctx.save();
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'left';
        ctx.font = '10px "JetBrains Mono", monospace';

        for (const item of items) {
            ctx.fillStyle = item.color;
            ctx.fillText(item.glyph, x, legendY);
            x += ctx.measureText(item.glyph).width + 5;
            ctx.fillStyle = muted;
            ctx.fillText(item.label, x, legendY);
            x += ctx.measureText(item.label).width + 18;
        }
        ctx.restore();
    }

    _drawGrid(ctx, W, H) {
        const spacing = 28;
        const r = 1;
        ctx.fillStyle = this.theme === 'light' ? 'rgba(140, 150, 175, 0.45)' : 'rgba(65, 71, 84, 0.34)';
        for (let x = spacing / 2; x < W; x += spacing) {
            for (let y = spacing / 2; y < H; y += spacing) {
                ctx.beginPath();
                ctx.arc(x, y, r, 0, Math.PI * 2);
                ctx.fill();
            }
        }
    }

    _drawEdges(ctx) {
        const isComplete = this.runState === 'completed';
        const isLight = this.theme === 'light';
        const now = performance.now();
        // Animate dash offset for active edges — creates a "flow" direction cue
        const flowOffset = -(now / 28) % 20;

        for (const edge of this.edges) {
            const src = this.nodes[edge.source];
            const tgt = this.nodes[edge.target];
            if (!src || !tgt) continue;

            const srcActive = src.status === 'running' || tgt.status === 'running';

            let edgeColor, arrowColor, glowColor;
            if (isComplete) {
                edgeColor  = isLight ? 'rgba(22,163,74,0.55)'  : 'rgba(125,226,167,0.45)';
                arrowColor = isLight ? '#16a34a'               : '#7de2a7';
                glowColor  = isLight ? 'rgba(22,163,74,0.18)'  : 'rgba(125,226,167,0.18)';
            } else if (srcActive) {
                edgeColor  = isLight ? 'rgba(37,99,235,0.6)'   : 'rgba(172,199,255,0.56)';
                arrowColor = isLight ? '#2563eb'               : '#acc7ff';
                glowColor  = isLight ? 'rgba(37,99,235,0.18)'  : 'rgba(172,199,255,0.18)';
            } else {
                edgeColor  = isLight ? 'rgba(120,132,160,0.52)' : 'rgba(90,98,115,0.52)';
                arrowColor = isLight ? '#8893a8'                : '#7a8499';
                glowColor  = null;
            }

            const route = this._edgeRoute(src, tgt);
            if (!route) continue;

            ctx.save();
            ctx.lineCap  = 'round';
            ctx.lineJoin = 'round';

            // 1. Glow underpass (active / complete only)
            if (glowColor) {
                ctx.save();
                ctx.strokeStyle = glowColor;
                ctx.lineWidth   = srcActive ? 7 : 6;
                ctx.shadowColor = arrowColor;
                ctx.shadowBlur  = srcActive ? 12 : 8;
                ctx.setLineDash([]);
                this._traceRoute(ctx, route);
                ctx.stroke();
                ctx.restore();
            }

            // 2. Main edge line
            const lineWidth = isComplete ? 1.6 : srcActive ? 1.9 : 1.4;
            ctx.strokeStyle = edgeColor;
            ctx.lineWidth   = lineWidth;

            if (isComplete) {
                ctx.setLineDash([5, 9]);
                ctx.lineDashOffset = 0;
            } else if (srcActive) {
                ctx.setLineDash([9, 9]);
                ctx.lineDashOffset = flowOffset;
            } else {
                ctx.setLineDash([6, 11]);
                ctx.lineDashOffset = 0;
            }

            this._traceRoute(ctx, route);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.lineDashOffset = 0;

            // 3. Arrow head with subtle glow when active
            if ((srcActive || isComplete) && glowColor) {
                ctx.save();
                ctx.shadowColor = arrowColor;
                ctx.shadowBlur  = srcActive ? 8 : 5;
                this._drawArrow(ctx, route[route.length - 2], route[route.length - 1], arrowColor, srcActive ? 11 : 9, srcActive ? 6 : 5);
                ctx.restore();
            } else {
                this._drawArrow(ctx, route[route.length - 2], route[route.length - 1], arrowColor, 9, 5);
            }

            ctx.restore();
        }
    }

    _drawCoreBackdrop(ctx, W, H) {
        const cx = W / 2;
        const cy = H / 2;
        const completePulse = this.runState === 'completed'
            ? Math.min(1, (performance.now() - this.runStateChangedAt) / 900)
            : 0;
        ctx.save();

        const outer = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.min(W, H) * 0.42);
        outer.addColorStop(0, completePulse > 0 ? 'rgba(134, 216, 171, 0.14)' : 'rgba(148, 191, 255, 0.08)');
        outer.addColorStop(0.45, completePulse > 0 ? 'rgba(134, 216, 171, 0.06)' : 'rgba(42, 70, 112, 0.08)');
        outer.addColorStop(1, 'rgba(10, 15, 19, 0)');
        ctx.fillStyle = outer;
        ctx.beginPath();
        ctx.arc(cx, cy, Math.min(W, H) * 0.42, 0, Math.PI * 2);
        ctx.fill();

        for (const band of this._layoutBands || []) {
            const laneGlow = ctx.createLinearGradient(56, band.y, W - 56, band.y);
            laneGlow.addColorStop(0, 'rgba(10, 15, 19, 0)');
            laneGlow.addColorStop(0.18, completePulse > 0 ? 'rgba(134, 216, 171, 0.04)' : 'rgba(148, 191, 255, 0.025)');
            laneGlow.addColorStop(0.5, completePulse > 0 ? 'rgba(134, 216, 171, 0.08)' : 'rgba(121, 143, 166, 0.08)');
            laneGlow.addColorStop(0.82, completePulse > 0 ? 'rgba(134, 216, 171, 0.04)' : 'rgba(148, 191, 255, 0.025)');
            laneGlow.addColorStop(1, 'rgba(10, 15, 19, 0)');
            ctx.fillStyle = laneGlow;
            roundRect(ctx, 58, band.top, W - 116, band.bottom - band.top, 24);
            ctx.fill();

            ctx.strokeStyle = completePulse > 0 ? 'rgba(134, 216, 171, 0.08)' : 'rgba(121, 143, 166, 0.08)';
            ctx.lineWidth = 1;
            ctx.setLineDash([10, 14]);
            ctx.beginPath();
            ctx.moveTo(72, band.y);
            ctx.lineTo(W - 72, band.y);
            ctx.stroke();
        }
        ctx.setLineDash([]);

        ctx.restore();
    }

    _drawNetworkStatus(ctx, W, H) {
        let label = 'Idle';
        let accent = this.theme === 'light' ? 'rgba(107,118,148,0.9)' : 'rgba(193, 198, 214, 0.82)';
        let fill = this.theme === 'light' ? 'rgba(255,255,255,0.88)' : 'rgba(27, 32, 37, 0.82)';
        let textColor = this.theme === 'light' ? '#3a4055' : null; // null = use accent

        if (this.runState === 'running') {
            label = 'Network Running';
            accent = this.theme === 'light' ? '#2563eb' : '#acc7ff';
            fill = this.theme === 'light' ? 'rgba(239,246,255,0.92)' : 'rgba(18, 30, 44, 0.86)';
        } else if (this.runState === 'completed') {
            label = 'Network Complete';
            accent = this.theme === 'light' ? '#16a34a' : '#7de2a7';
            fill = this.theme === 'light' ? 'rgba(240,253,244,0.92)' : 'rgba(18, 39, 31, 0.9)';
        } else if (this.runState === 'stopped') {
            label = 'Network Stopped';
            accent = this.theme === 'light' ? '#dc2626' : '#ffb4ab';
            fill = this.theme === 'light' ? 'rgba(254,242,242,0.92)' : 'rgba(42, 23, 23, 0.86)';
        }

        const boxW = 180;
        const boxH = 34;
        const x = W / 2 - boxW / 2;
        const y = 22;

        ctx.save();
        ctx.shadowColor = 'rgba(0, 0, 0, 0.42)';
        ctx.shadowBlur = 18;
        ctx.fillStyle = fill;
        roundRect(ctx, x, y, boxW, boxH, 16);
        ctx.fill();

        ctx.shadowBlur = 0;
        ctx.strokeStyle = this.runState === 'completed'
            ? 'rgba(125, 226, 167, 0.24)'
            : this.runState === 'running'
                ? 'rgba(172, 199, 255, 0.24)'
                : 'rgba(65, 71, 84, 0.18)';
        ctx.lineWidth = 1;
        roundRect(ctx, x, y, boxW, boxH, 16);
        ctx.stroke();

        ctx.fillStyle = accent;
        ctx.beginPath();
        ctx.arc(x + 16, y + boxH / 2, 4.5, 0, Math.PI * 2);
        ctx.fill();

        ctx.font = '700 11px "Space Grotesk", sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = textColor || accent;
        ctx.fillText(label, x + 28, y + boxH / 2 + 0.5);
        ctx.restore();
    }

    _drawArrow(ctx, from, to, color, arrowLen = 9, arrowWid = 5) {
        const dx = to.x - from.x;
        const dy = to.y - from.y;
        const len = Math.hypot(dx, dy);
        if (len < 1) return;
        const ux = dx / len;
        const uy = dy / len;
        const base = { x: to.x - ux * arrowLen, y: to.y - uy * arrowLen };
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(to.x, to.y);
        ctx.lineTo(base.x - uy * arrowWid, base.y + ux * arrowWid);
        ctx.lineTo(base.x + uy * arrowWid, base.y - ux * arrowWid);
        ctx.closePath();
        ctx.fill();
    }

    _drawParticles(ctx) {
        const now = performance.now();
        const alive = [];

        for (const p of this.particles) {
            const src = this.nodes[p.source];
            const tgt = this.nodes[p.target];
            if (!src || !tgt) continue;

            const elapsed = now - p.startTime;
            const raw = Math.min(1, elapsed / p.duration);
            const t = easeInOutCubic(raw);

            const route = this._edgeRoute(src, tgt);
            if (!route) continue;
            const pos = this._pointAlongRoute(route, t);

            const color = src.color;

            ctx.save();
            ctx.shadowColor = color;
            ctx.shadowBlur  = 8;
            ctx.fillStyle   = color;
            ctx.beginPath();
            ctx.arc(pos.x, pos.y, 5, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();

            if (raw < 1) alive.push(p);
        }

        this.particles = alive;
    }

    _drawNodes(ctx) {
        const now = performance.now();
        if (this.nodes.coordinator) {
            this._drawNode(ctx, this.nodes.coordinator, now);
        }
        for (const [id, node] of Object.entries(this.nodes)) {
            if (id === 'coordinator') continue;
            this._drawNode(ctx, node, now);
        }
    }

    _drawNode(ctx, node, now) {
        const roleAccent = node.color;
        const isSelected = this.selectedNode === node.id;
        const isHovered  = this._hoveredId   === node.id;
        const isCore = node.id === 'coordinator';
        const isComplete = this.runState === 'completed';
        const isLight = this.theme === 'light';
        const isLive = node.thinking || node.status === 'running';
        const isDone = isComplete && node.status === 'completed';
        const width = node.w || (isCore ? CORE_NODE_W : NODE_W);
        const height = node.h || (isCore ? CORE_NODE_H : NODE_H);
        const x = node.x - width / 2;
        const y = node.y - height / 2;
        const radius = Math.min(NODE_RADIUS, height / 2);
        node.renderRadius = Math.max(width, height) / 2;

        // Pulse on message receive
        let pulse = 0;
        if (node.pulseStart !== null) {
            const elapsed = now - node.pulseStart;
            if (elapsed < 600) pulse = Math.sin((elapsed / 600) * Math.PI);
            else node.pulseStart = null;
        }

        ctx.save();

        // ── 1. Soft role-colored glow halo — live / selected / hover / done ──
        if (isLive || isSelected || isHovered || pulse > 0.08 || isDone) {
            const haloColor = isDone
                ? (isLight ? '#16a34a' : '#86d8ab')
                : roleAccent;
            const haloAlpha = isSelected ? 0.32 : isHovered ? 0.22 : isLive ? 0.22 + pulse * 0.12 : 0.16;
            ctx.save();
            ctx.shadowColor = haloColor;
            ctx.shadowBlur  = isSelected ? 22 : isLive ? 18 : 14;
            ctx.globalAlpha = haloAlpha;
            ctx.fillStyle   = haloColor;
            roundRect(ctx, x - 4, y - 4, width + 8, height + 8, radius + 4);
            ctx.fill();
            ctx.restore();
        }

        // ── 2. Pill body — very dark fill matching panel tone, crisp 1px border ──
        const borderColor = isSelected
            ? roleAccent
            : isLive
                ? (isLight ? 'rgba(40,50,68,0.92)' : 'rgba(226,234,246,0.95)')
                : isDone
                    ? (isLight ? 'rgba(19,133,77,0.55)' : 'rgba(134,216,171,0.55)')
                    : (isLight ? 'rgba(129,147,166,0.55)' : 'rgba(148,163,184,0.35)');
        const fillColor = isLight
            ? (isLive ? '#ffffff' : isDone ? '#f5fdf9' : '#f7fafc')
            : (isLive ? '#11181f' : isDone ? '#0f1a16' : '#0d1218');

        ctx.save();
        ctx.fillStyle = fillColor;
        roundRect(ctx, x, y, width, height, radius);
        ctx.fill();
        ctx.lineWidth   = isCore ? 1.4 : 1.1;
        ctx.strokeStyle = borderColor;
        roundRect(ctx, x + 0.5, y + 0.5, width - 1, height - 1, radius - 0.5);
        ctx.stroke();
        ctx.restore();

        // ── 3. Inside chip: [glyph] [name] [status dot] ──
        const padX = 14;
        const glyphColor = isLive
            ? (isLight ? '#17202b' : '#e6edf7')
            : (isLight ? '#41505f' : '#c4cbd6');
        const labelColor = isLive
            ? (isLight ? '#0d141d' : '#f0f4fa')
            : (isLight ? '#17202b' : '#c4ced9');

        const glyphKey = glyphKeyFor(node.id, node.role);
        const glyphCx = x + padX + 9;
        const glyphCy = y + height / 2;
        drawRoleGlyph(ctx, glyphCx, glyphCy, glyphColor, glyphKey);

        const textLeft = glyphCx + 14;
        const dotX = x + width - padX;
        const textRight = dotX - 12;
        const maxTextW = Math.max(40, textRight - textLeft);

        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = labelColor;
        ctx.font = isCore
            ? '500 14px "Space Grotesk", sans-serif'
            : '500 13px "Space Grotesk", sans-serif';
        const labelText = this._truncate(ctx, node.role || node.id, maxTextW);
        ctx.fillText(labelText, textLeft, y + height / 2);

        // Status dot: role-colored on live (with glow), muted on idle
        const dotColor = isLive
            ? roleAccent
            : isDone
                ? (isLight ? '#16a34a' : '#86d8ab')
                : (isLight ? '#8796a7' : '#7a8499');
        ctx.save();
        if (isLive) {
            ctx.shadowColor = roleAccent;
            ctx.shadowBlur = 8 + pulse * 6;
        }
        ctx.fillStyle = dotColor;
        ctx.globalAlpha = isLive ? 1 : 0.55;
        ctx.beginPath();
        ctx.arc(dotX, y + height / 2, 3.2 + pulse * 1.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        // ── 4. Caption line below chip: "ROLE_CAPTION · model" ──
        const captionColor = isLive
            ? (isLight ? '#5d6b7c' : '#a5b0bf')
            : (isLight ? '#718295' : '#8796a7');
        const caption = (this._roleCaption(node) || '').toUpperCase();
        const modelLabel = node.model ? _shortModel(node.model) : 'pending';
        ctx.save();
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.font = '9px "JetBrains Mono", monospace';
        ctx.fillStyle = captionColor;
        const metaY = y + height + 8;
        const dotSep = '  ·  ';
        const captionTrack = 0.8;
        const captionWidth = this._trackedWidth(ctx, caption, captionTrack);
        const sepWidth = ctx.measureText(dotSep).width;
        const modelWidth = ctx.measureText(modelLabel).width;
        const totalMetaW = captionWidth + sepWidth + modelWidth;
        let metaX = node.x - totalMetaW / 2;
        this._fillTracked(ctx, caption, metaX, metaY, captionTrack);
        metaX += captionWidth;
        ctx.fillText(dotSep, metaX, metaY);
        metaX += sepWidth;
        ctx.fillStyle = isLive
            ? (isLight ? '#41505f' : '#c4ced9')
            : (isLight ? '#718295' : '#8796a7');
        ctx.fillText(modelLabel, metaX, metaY);
        ctx.restore();

        if (isSelected || isHovered) this._drawNodeLabel(ctx, node, isCore, isSelected, isHovered);

        ctx.restore();
    }

    _roleCaption(node) {
        const map = {
            coordinator: 'Orchestrator',
            coder: 'Writes code',
            reviewer: 'Critiques',
            reviewer_a: 'Quality',
            reviewer_b: 'Security',
            tester: 'Runs tests',
            researcher: 'Investigates',
        };
        if (map[node.id]) return map[node.id];
        if (map[node.role?.toLowerCase?.()]) return map[node.role.toLowerCase()];
        return node.status === 'running' ? 'Working' : node.status === 'completed' ? 'Done' : 'Idle';
    }

    _truncate(ctx, text, maxWidth) {
        if (!text) return '';
        if (ctx.measureText(text).width <= maxWidth) return text;
        let out = text;
        while (out.length && ctx.measureText(out + '…').width > maxWidth) out = out.slice(0, -1);
        return out ? out + '…' : '';
    }

    /** Measure how wide a string renders with manual letter-spacing. */
    _trackedWidth(ctx, text, tracking = 0.5) {
        if (!text) return 0;
        let total = 0;
        for (const ch of text) total += ctx.measureText(ch).width + tracking;
        return total - tracking;
    }

    /** Fill text with manual letter-spacing for the tracked-kicker aesthetic. */
    _fillTracked(ctx, text, x, y, tracking = 0.5, align = 'left') {
        const widths = [];
        let total = 0;
        for (const ch of text) {
            const w = ctx.measureText(ch).width;
            widths.push(w);
            total += w + tracking;
        }
        total -= tracking;
        let cursor = align === 'center' ? x - total / 2
                   : align === 'right'  ? x - total
                   :                       x;
        for (let i = 0; i < text.length; i++) {
            ctx.fillText(text[i], cursor, y);
            cursor += widths[i] + tracking;
        }
    }


    _drawNodeLabel(ctx, node, isCore, isSelected = false, isHovered = false) {
        const title = node.role || node.id;
        const model = node.model ? _shortModel(node.model) : '';
        const subtitle = node.lastActivity || model || `${node.msgCount || 0} messages`;
        const detailVisible = isCore || isSelected || isHovered;
        if (!detailVisible) return;

        ctx.save();
        ctx.font = '700 12px "Space Grotesk", sans-serif';
        const titleW = ctx.measureText(title).width;
        ctx.font = '10px "JetBrains Mono", monospace';
        const subW = ctx.measureText(subtitle).width;
        const contentW = Math.max(titleW, subW);
        const padX = 12;
        const boxW = Math.min(
            Math.max(contentW + padX * 2, isCore ? 132 : 118),
            isCore ? 220 : 184,
        );
        const boxH = isCore ? 56 : 44;
        const viewportW = this.canvas.width / this._dpr;
        const viewportH = this.canvas.height / this._dpr;
        const worldMinX = -this.offsetX / this.zoom;
        const worldMinY = -this.offsetY / this.zoom;
        const worldMaxX = worldMinX + viewportW / this.zoom;
        const worldMaxY = worldMinY + viewportH / this.zoom;
        const boxX = Math.max(worldMinX + 16 / this.zoom, Math.min(worldMaxX - boxW - 16 / this.zoom, node.x - boxW / 2));
        const above = node.y > worldMinY + (viewportH / this.zoom) * 0.55;
        const desiredY = above
            ? node.y - ((node.h || NODE_H) / 2) - boxH - 12
            : node.y + ((node.h || NODE_H) / 2) + 12;
        const boxY = Math.max(worldMinY + 16 / this.zoom, Math.min(worldMaxY - boxH - 16 / this.zoom, desiredY));

        ctx.shadowColor = 'rgba(0, 0, 0, 0.42)';
        ctx.shadowBlur = isCore ? 22 : 16;
        ctx.fillStyle = this.theme === 'light'
            ? (isCore ? 'rgba(255,255,255,0.96)' : 'rgba(248,250,252,0.97)')
            : (isCore ? 'rgba(28, 35, 43, 0.92)' : 'rgba(20, 26, 33, 0.94)');
        roundRect(ctx, boxX, boxY, boxW, boxH, 14);
        ctx.fill();

        ctx.shadowBlur = 0;
        ctx.strokeStyle = isSelected ? 'rgba(148, 191, 255, 0.28)' : 'rgba(121, 143, 166, 0.16)';
        ctx.lineWidth = 1;
        roundRect(ctx, boxX, boxY, boxW, boxH, 14);
        ctx.stroke();

        ctx.fillStyle = this.theme === 'light' ? '#17202b' : '#ecf1f6';
        ctx.font = '700 12px "Space Grotesk", sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText(title, boxX + 12, boxY + 10);

        ctx.font = '10px "JetBrains Mono", monospace';
        ctx.fillStyle = this.theme === 'light' ? '#718295' : '#8796a7';
        let sub = subtitle;
        while (sub.length && ctx.measureText(sub).width > boxW - 24) sub = sub.slice(0, -1);
        if (sub !== subtitle) sub += '…';
        ctx.fillText(sub, boxX + 12, boxY + 30);

        ctx.restore();
    }

    _edgeRoute(src, tgt) {
        const srcHalfW = (src.w || NODE_W) / 2;
        const srcHalfH = (src.h || NODE_H) / 2;
        const tgtHalfW = (tgt.w || NODE_W) / 2;
        const tgtHalfH = (tgt.h || NODE_H) / 2;
        const rowGap = this._layoutRows.length > 1
            ? Math.abs(this._layoutRows[1] - this._layoutRows[0])
            : 120;
        const trackOffset = this._edgeTrackOffset(src.id, tgt.id);

        if (Math.abs(src.y - tgt.y) < 8) {
            const start = {
                x: src.x + Math.sign((tgt.x - src.x) || 1) * srcHalfW,
                y: src.y,
            };
            const end = {
                x: tgt.x - Math.sign((tgt.x - src.x) || 1) * tgtHalfW,
                y: tgt.y,
            };
            const laneY = src.y + (trackOffset === 0 ? -18 : trackOffset);
            return this._compactRoute([
                start,
                { x: start.x + 12 * Math.sign((tgt.x - src.x) || 1), y: laneY },
                { x: end.x - 12 * Math.sign((tgt.x - src.x) || 1), y: laneY },
                end,
            ]);
        }

        const goingDown = tgt.y > src.y;
        const start = { x: src.x, y: src.y + (goingDown ? srcHalfH : -srcHalfH) };
        const end = { x: tgt.x, y: tgt.y + (goingDown ? -tgtHalfH : tgtHalfH) };
        const sourceExitY = start.y + (goingDown ? 18 : -18);
        const targetEntryY = end.y + (goingDown ? -18 : 18);
        const baseTrackX = src.x + (tgt.x - src.x) * 0.5;
        const trackX = clamp(
            baseTrackX + trackOffset,
            Math.min(src.x, tgt.x) - Math.min(120, rowGap * 0.35),
            Math.max(src.x, tgt.x) + Math.min(120, rowGap * 0.35),
        );

        return this._compactRoute([
            start,
            { x: start.x, y: sourceExitY },
            { x: trackX, y: sourceExitY },
            { x: trackX, y: targetEntryY },
            { x: end.x, y: targetEntryY },
            end,
        ]);
    }

    _traceRoute(ctx, route, cornerRadius = 12) {
        if (!route?.length) return;
        ctx.beginPath();
        ctx.moveTo(route[0].x, route[0].y);
        for (let i = 1; i < route.length - 1; i++) {
            ctx.arcTo(route[i].x, route[i].y, route[i + 1].x, route[i + 1].y, cornerRadius);
        }
        ctx.lineTo(route[route.length - 1].x, route[route.length - 1].y);
    }

    _pointAlongRoute(route, t) {
        if (!route?.length) return { x: 0, y: 0 };
        const segments = [];
        let total = 0;

        for (let i = 1; i < route.length; i++) {
            const a = route[i - 1];
            const b = route[i];
            const len = Math.hypot(b.x - a.x, b.y - a.y);
            segments.push({ a, b, len });
            total += len;
        }

        let target = total * t;
        for (const segment of segments) {
            if (target <= segment.len) {
                const ratio = segment.len ? target / segment.len : 0;
                return {
                    x: segment.a.x + (segment.b.x - segment.a.x) * ratio,
                    y: segment.a.y + (segment.b.y - segment.a.y) * ratio,
                };
            }
            target -= segment.len;
        }

        return route[route.length - 1];
    }

    _edgeTrackOffset(sourceId, targetId) {
        const key = `${sourceId}->${targetId}`;
        let hash = 0;
        for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) & 0xffff;
        const slots = [-54, -30, -12, 12, 30, 54];
        return slots[hash % slots.length];
    }

    _compactRoute(points) {
        const compact = [];
        for (const point of points) {
            const prev = compact[compact.length - 1];
            if (!prev || Math.abs(prev.x - point.x) > 0.5 || Math.abs(prev.y - point.y) > 0.5) {
                compact.push(point);
            }
        }
        return compact;
    }

    // ── Events ───────────────────────────────────────────────────────────────

    _setupEvents() {
        const parent = this.canvas.parentElement;
        if (parent) {
            this._resizeObserver = new ResizeObserver(() => this._resize());
            this._resizeObserver.observe(parent);
        }
        window.addEventListener('resize', () => this._resize());
        this.canvas.addEventListener('click',     (e) => this._onClick(e));
        this.canvas.addEventListener('mousemove', (e) => this._onMouseMove(e));
        this.canvas.addEventListener('wheel', (e) => this._onWheel(e), { passive: false });
        this.canvas.addEventListener('mouseleave', () => {
            this._hoveredId = null;
            this.canvas.style.cursor = 'default';
        });
    }

    zoomIn() {
        this._applyZoom(this.zoom * 1.14);
    }

    zoomOut() {
        this._applyZoom(this.zoom / 1.14);
    }

    resetView() {
        this.zoom = 1;
        this.offsetX = 0;
        this.offsetY = 0;
    }

    _applyZoom(nextZoom, clientX = null, clientY = null) {
        const clamped = Math.max(this.minZoom, Math.min(this.maxZoom, nextZoom));
        if (Math.abs(clamped - this.zoom) < 0.001) return;

        const rect = this.canvas.getBoundingClientRect();
        const anchorX = clientX === null ? rect.left + rect.width / 2 : clientX;
        const anchorY = clientY === null ? rect.top + rect.height / 2 : clientY;
        const localX = anchorX - rect.left;
        const localY = anchorY - rect.top;
        const worldX = (localX - this.offsetX) / this.zoom;
        const worldY = (localY - this.offsetY) / this.zoom;

        this.zoom = clamped;
        this.offsetX = localX - worldX * this.zoom;
        this.offsetY = localY - worldY * this.zoom;
        this._clampOffset();
    }

    _clampOffset() {
        const W = this.canvas.width / this._dpr;
        const H = this.canvas.height / this._dpr;
        const maxPanX = Math.max(0, (this.zoom - 1) * W * 0.5);
        const maxPanY = Math.max(0, (this.zoom - 1) * H * 0.5);
        this.offsetX = Math.max(-maxPanX, Math.min(maxPanX, this.offsetX));
        this.offsetY = Math.max(-maxPanY, Math.min(maxPanY, this.offsetY));
    }

    _resize() {
        const el = this.canvas.parentElement || this.canvas;
        const rect = el.getBoundingClientRect();
        const w = Math.max(rect.width,  100);
        const h = Math.max(rect.height, 100);
        this._dpr = window.devicePixelRatio || 1;

        this.canvas.width  = w * this._dpr;
        this.canvas.height = h * this._dpr;
        this.canvas.style.width  = w + 'px';
        this.canvas.style.height = h + 'px';

        this._rebuildGrid(w, h);
        this._layoutNodes();
        this._clampOffset();
    }

    _rebuildGrid(w, h) {
        const offscreen = document.createElement('canvas');
        offscreen.width  = w * this._dpr;
        offscreen.height = h * this._dpr;
        const ctx = offscreen.getContext('2d');
        ctx.scale(this._dpr, this._dpr);
        ctx.fillStyle = this.theme === 'light' ? '#eef2f4' : '#0b1015';
        ctx.fillRect(0, 0, w, h);
        const spacing = 26;
        ctx.fillStyle = this.theme === 'light' ? 'rgba(129,147,166,0.38)' : 'rgba(121,143,166,0.22)';
        for (let x = spacing / 2; x < w; x += spacing) {
            for (let y = spacing / 2; y < h; y += spacing) {
                ctx.beginPath();
                ctx.arc(x, y, 0.9, 0, Math.PI * 2);
                ctx.fill();
            }
        }
        this._gridCanvas = offscreen;
    }

    _nodeAt(clientX, clientY) {
        const rect = this.canvas.getBoundingClientRect();
        const mx   = (clientX - rect.left - this.offsetX) / this.zoom;
        const my   = (clientY - rect.top - this.offsetY) / this.zoom;

        for (const [id, node] of Object.entries(this.nodes)) {
            const halfW = ((node.w || NODE_W) / 2) + 8;
            const halfH = ((node.h || NODE_H) / 2) + 8;
            if (mx >= node.x - halfW && mx <= node.x + halfW && my >= node.y - halfH && my <= node.y + halfH) {
                return id;
            }
        }
        return null;
    }

    _onClick(e) {
        const id = this._nodeAt(e.clientX, e.clientY);
        this.selectedNode = id || null;
        if (this.onNodeClick) this.onNodeClick(id, id ? this.nodes[id] : null);
    }

    _onMouseMove(e) {
        const id = this._nodeAt(e.clientX, e.clientY);
        if (id !== this._hoveredId) {
            this._hoveredId = id;
            this.canvas.style.cursor = id ? 'pointer' : 'default';
        }
    }

    _onWheel(e) {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        this._applyZoom(this.zoom * factor, e.clientX, e.clientY);
    }
}

// Expose helpers for unit tests (Node/Vitest). No-op in the browser.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        GraphRenderer,
        roundRect,
        glyphKeyFor,
        alphaHex,
        tintColor,
        NODE_RADIUS,
    };
}
