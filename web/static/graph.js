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

const NODE_W = 188;
const NODE_H = 82;
const NODE_RADIUS = 10;
const PARTICLE_DURATION = 900; // ms

const AGENT_COLORS = {
    coordinator: '#6e40c9',  // purple
    coder:       '#0550ae',  // blue
    reviewer:    '#7d4e00',  // amber
    reviewer_a:  '#7d4e00',
    reviewer_b:  '#953800',
    tester:      '#1a7f37',  // green
    user:        '#8250df',
};

const FALLBACK_COLORS = ['#9e1239', '#0369a1', '#92400e', '#6b21a8'];

const STATUS_COLORS = {
    idle:      '#9198a1',
    running:   '#0969da',
    completed: '#1a7f37',
    error:     '#cf222e',
};

const STATUS_BADGE = {
    idle:      { bg: '#f0f2f5', fg: '#8b949e' },
    running:   { bg: '#dbeafe', fg: '#0969da' },
    completed: { bg: '#dcfce7', fg: '#1a7f37' },
    error:     { bg: '#fee2e2', fg: '#cf222e' },
    thinking:  { bg: '#fef3c7', fg: '#9a6700' },
};

// Fractional canvas positions for known topologies
const LAYOUT_TRIAD = {
    coordinator: [0.5,  0.15],
    coder:       [0.5,  0.47],
    reviewer:    [0.24, 0.81],
    tester:      [0.76, 0.81],
};

const LAYOUT_DUAL_REVIEW = {
    coordinator: [0.5,  0.10],
    coder:       [0.5,  0.35],
    reviewer_a:  [0.24, 0.65],
    reviewer_b:  [0.76, 0.65],
    tester:      [0.5,  0.88],
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

/** Find the point on the border of a node rect in the direction of another point. */
function nodeEdgePoint(node, other) {
    const cx = node.x;
    const cy = node.y;
    const dx = other.x - cx;
    const dy = other.y - cy;
    const hw = NODE_W / 2;
    const hh = NODE_H / 2;
    if (Math.abs(dx) < 0.001 && Math.abs(dy) < 0.001) return { x: cx, y: cy };
    const sx = dx === 0 ? Infinity : hw / Math.abs(dx);
    const sy = dy === 0 ? Infinity : hh / Math.abs(dy);
    const s = Math.min(sx, sy);
    return { x: cx + dx * s, y: cy + dy * s };
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

        this._hoveredId = null;
        this._rafId = null;
        this._dpr = window.devicePixelRatio || 1;
        this._gridCanvas = null;  // offscreen cache for dot grid

        this._setupEvents();
        this._resize();
    }

    destroy() {
        if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = null; }
        if (this._resizeObserver) this._resizeObserver.disconnect();
    }

    // ── Public API ───────────────────────────────────────────────────────────

    setTopology(agents, edges) {
        this.nodes = {};
        this.edges = edges || [];
        this.particles = [];

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

        // Choose layout map
        let layoutMap = null;
        if (n === 4 && ids.every(id => id in LAYOUT_TRIAD)) {
            layoutMap = LAYOUT_TRIAD;
        } else if (n === 5 && ids.every(id => id in LAYOUT_DUAL_REVIEW)) {
            layoutMap = LAYOUT_DUAL_REVIEW;
        }

        if (layoutMap) {
            for (const id of ids) {
                const [fx, fy] = layoutMap[id];
                this.nodes[id].x = fx * W;
                this.nodes[id].y = fy * H;
            }
        } else {
            // Circular fallback
            const padding = 80;
            const cx = W / 2;
            const cy = H / 2;
            const rx = W / 2 - padding - NODE_W / 2;
            const ry = H / 2 - padding - NODE_H / 2;
            ids.forEach((id, i) => {
                const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
                this.nodes[id].x = cx + rx * Math.cos(angle);
                this.nodes[id].y = cy + ry * Math.sin(angle);
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

        // 1. Background + dot grid (cached offscreen canvas, repainted only on resize)
        if (this._gridCanvas) {
            ctx.drawImage(this._gridCanvas, 0, 0, W, H);
        } else {
            ctx.fillStyle = '#f5f7fa';
            ctx.fillRect(0, 0, W, H);
        }

        // 3. Edges
        this._drawEdges(ctx);

        // 4. Particles
        this._drawParticles(ctx);

        // 5. Nodes
        this._drawNodes(ctx);

        ctx.restore();
    }

    _drawGrid(ctx, W, H) {
        const spacing = 28;
        const r = 1;
        ctx.fillStyle = '#d0d7de';
        for (let x = spacing / 2; x < W; x += spacing) {
            for (let y = spacing / 2; y < H; y += spacing) {
                ctx.beginPath();
                ctx.arc(x, y, r, 0, Math.PI * 2);
                ctx.fill();
            }
        }
    }

    _drawEdges(ctx) {
        for (const edge of this.edges) {
            const src = this.nodes[edge.source];
            const tgt = this.nodes[edge.target];
            if (!src || !tgt) continue;

            const srcActive = src.status === 'running' || tgt.status === 'running';
            const edgeColor = srcActive ? '#c8d8f4' : '#d8dde3';
            const arrowColor = srcActive ? '#9ab5e8' : '#b8c0ca';

            const p0 = nodeEdgePoint(src, tgt);
            const p1 = nodeEdgePoint(tgt, src);
            const { cp1, cp2 } = edgeControlPoints(p0, p1);

            ctx.save();
            ctx.strokeStyle = edgeColor;
            ctx.lineWidth   = srcActive ? 2 : 1.5;
            ctx.lineCap     = 'round';
            ctx.beginPath();
            ctx.moveTo(p0.x, p0.y);
            ctx.bezierCurveTo(cp1.x, cp1.y, cp2.x, cp2.y, p1.x, p1.y);
            ctx.stroke();

            // Arrowhead at target
            this._drawArrow(ctx, cp2, p1, arrowColor);
            ctx.restore();
        }
    }

    _drawArrow(ctx, from, to, color) {
        const dx = to.x - from.x;
        const dy = to.y - from.y;
        const len = Math.hypot(dx, dy);
        if (len < 1) return;
        const ux = dx / len;
        const uy = dy / len;
        const arrowLen = 9;
        const arrowWid = 5;
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

            const p0 = nodeEdgePoint(src, tgt);
            const p1 = nodeEdgePoint(tgt, src);
            const { cp1, cp2 } = edgeControlPoints(p0, p1);
            const pos = bezierPoint(p0, cp1, cp2, p1, t);

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
        for (const [id, node] of Object.entries(this.nodes)) {
            this._drawNode(ctx, node, now);
        }
    }

    _drawNode(ctx, node, now) {
        const x   = node.x - NODE_W / 2;
        const y   = node.y - NODE_H / 2;
        const w   = NODE_W;
        const h   = NODE_H;
        const r   = NODE_RADIUS;
        const col = node.color;

        const isSelected = this.selectedNode === node.id;
        const isHovered  = this._hoveredId   === node.id;

        // Pulse on message receive
        let pulse = 0;
        if (node.pulseStart !== null) {
            const elapsed = now - node.pulseStart;
            if (elapsed < 600) pulse = Math.sin((elapsed / 600) * Math.PI);
            else node.pulseStart = null;
        }

        const runPhase = (now % 2000) / 2000;
        const STRIPE   = 5;   // top accent stripe height
        const DIVIDER  = 52;  // y-offset for content/stats divider
        const PAD      = 13;  // horizontal padding

        ctx.save();

        // ── Selected glow ring ───────────────────────────────────
        if (isSelected || pulse > 0.15) {
            ctx.save();
            ctx.shadowColor = col;
            ctx.shadowBlur  = isSelected ? 20 : 10;
            const ga = isSelected ? 0.6 : pulse * 0.4;
            ctx.strokeStyle = col + Math.round(ga * 255).toString(16).padStart(2, '0');
            ctx.lineWidth   = isSelected ? 2.5 : 1.5;
            roundRect(ctx, x - 4, y - 4, w + 8, h + 8, r + 3);
            ctx.stroke();
            ctx.restore();
        }

        // ── Drop shadow ──────────────────────────────────────────
        ctx.save();
        ctx.shadowColor   = isSelected ? col + '28' : 'rgba(0,0,0,0.08)';
        ctx.shadowBlur    = isHovered ? 18 : isSelected ? 24 : 7;
        ctx.shadowOffsetY = isHovered ? 5 : 3;
        ctx.fillStyle = '#fff';
        roundRect(ctx, x, y, w, h, r);
        ctx.fill();
        ctx.restore();

        // ── White card body ──────────────────────────────────────
        ctx.fillStyle = '#ffffff';
        roundRect(ctx, x, y, w, h, r);
        ctx.fill();

        // ── Stats tinted footer ──────────────────────────────────
        ctx.save();
        roundRect(ctx, x, y, w, h, r);
        ctx.clip();
        ctx.fillStyle = '#f6f8fa';
        ctx.fillRect(x, y + DIVIDER, w, h - DIVIDER);
        ctx.restore();

        // ── Card border ──────────────────────────────────────────
        ctx.strokeStyle = isSelected ? col + '80'
            : isHovered  ? '#c0c8d2'
            : '#dde1e7';
        ctx.lineWidth = isSelected ? 1.5 : 1;
        roundRect(ctx, x, y, w, h, r);
        ctx.stroke();

        // ── Top accent stripe ────────────────────────────────────
        ctx.save();
        roundRect(ctx, x, y, w, h, r);
        ctx.clip();

        if (node.status === 'running' || node.thinking) {
            // Sweeping shimmer across the stripe
            const sx = x + (runPhase * w * 2) - w * 0.5;
            const g  = ctx.createLinearGradient(sx, y, sx + w * 0.7, y);
            g.addColorStop(0,   col + 'cc');
            g.addColorStop(0.3, col + 'ff');
            g.addColorStop(0.7, col + 'ff');
            g.addColorStop(1,   col + 'cc');
            ctx.fillStyle = g;
        } else if (node.status === 'completed') {
            ctx.fillStyle = '#1a7f37';
        } else if (node.status === 'error') {
            ctx.fillStyle = '#cf222e';
        } else {
            ctx.fillStyle = col;
            ctx.globalAlpha = pulse > 0 ? 0.75 + 0.25 * pulse : 0.9;
        }
        ctx.fillRect(x, y, w, STRIPE);
        ctx.globalAlpha = 1;
        ctx.restore();

        // ── Divider line ─────────────────────────────────────────
        ctx.save();
        ctx.strokeStyle = '#e4e8ec';
        ctx.lineWidth   = 0.5;
        ctx.beginPath();
        ctx.moveTo(x + PAD, y + DIVIDER);
        ctx.lineTo(x + w - PAD, y + DIVIDER);
        ctx.stroke();
        ctx.restore();

        // ── Role name ────────────────────────────────────────────
        ctx.textAlign    = 'left';
        ctx.textBaseline = 'top';
        ctx.font         = '700 13px Inter, -apple-system, sans-serif';
        ctx.fillStyle    = '#1c2128';
        ctx.fillText(node.role, x + PAD, y + STRIPE + 8, w - PAD * 2 - 58);

        // ── Status badge ─────────────────────────────────────────
        const statusKey  = node.thinking ? 'thinking' : (node.status || 'idle');
        const badge      = STATUS_BADGE[statusKey] || STATUS_BADGE.idle;
        const statusText = statusKey;

        ctx.font = '600 9px Inter, -apple-system, sans-serif';
        const btw = ctx.measureText(statusText).width;
        const bW  = btw + 18;   // dot (5px) + gap + text + h-padding
        const bH  = 15;
        const bX  = x + w - bW - PAD + 4;
        const bY  = y + STRIPE + 7;

        ctx.fillStyle = badge.bg;
        ctx.beginPath();
        ctx.roundRect(bX, bY, bW, bH, 7);
        ctx.fill();

        // Dot
        const dCx = bX + 7;
        const dCy = bY + bH / 2;
        if (statusKey === 'running' || statusKey === 'thinking') {
            ctx.save();
            ctx.globalAlpha = 0.5 + 0.5 * Math.abs(Math.sin(runPhase * Math.PI * 2));
            ctx.fillStyle   = badge.fg;
            ctx.beginPath();
            ctx.arc(dCx, dCy, 3, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();
        } else {
            ctx.fillStyle = badge.fg;
            ctx.beginPath();
            ctx.arc(dCx, dCy, 2.5, 0, Math.PI * 2);
            ctx.fill();
        }

        ctx.fillStyle    = badge.fg;
        ctx.textAlign    = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(statusText, bX + 13, bY + bH / 2);

        // ── Model name ───────────────────────────────────────────
        ctx.textAlign    = 'left';
        ctx.textBaseline = 'top';
        if (node.model) {
            ctx.font      = '10px "JetBrains Mono", monospace';
            ctx.fillStyle = '#6b7280';
            ctx.fillText(_shortModel(node.model), x + PAD, y + STRIPE + 26, w - PAD * 2);
        } else {
            ctx.font      = '10px Inter, -apple-system, sans-serif';
            ctx.fillStyle = '#9198a1';
            ctx.fillText('selecting model…', x + PAD, y + STRIPE + 26, w - PAD * 2);
        }

        // ── Footer: msg count + activity ─────────────────────────
        const fY  = y + DIVIDER + (h - DIVIDER) / 2;  // vertical center of footer
        ctx.textAlign    = 'left';
        ctx.textBaseline = 'middle';

        const msgStr = `↑${node.msgCount}`;
        ctx.font      = '600 9px Inter, -apple-system, sans-serif';
        ctx.fillStyle = '#8b949e';
        ctx.fillText(msgStr, x + PAD, fY);

        if (node.lastActivity) {
            const mW  = ctx.measureText(msgStr).width + 7;
            const avW = w - PAD * 2 - mW;
            let act   = node.lastActivity;
            ctx.font  = '9px Inter, -apple-system, sans-serif';
            ctx.fillStyle = '#57606a';
            if (ctx.measureText(act).width > avW) {
                while (act.length && ctx.measureText(act + '…').width > avW) act = act.slice(0, -1);
                act += '…';
            }
            ctx.fillText(act, x + PAD + mW, fY);
        }

        ctx.restore();
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
        this.canvas.addEventListener('mouseleave', () => {
            this._hoveredId = null;
            this.canvas.style.cursor = 'default';
        });
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
    }

    _rebuildGrid(w, h) {
        const offscreen = document.createElement('canvas');
        offscreen.width  = w * this._dpr;
        offscreen.height = h * this._dpr;
        const ctx = offscreen.getContext('2d');
        ctx.scale(this._dpr, this._dpr);
        ctx.fillStyle = '#f5f7fa';
        ctx.fillRect(0, 0, w, h);
        const spacing = 26;
        ctx.fillStyle = '#cdd1d6';
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
        const mx   = clientX - rect.left;
        const my   = clientY - rect.top;

        for (const [id, node] of Object.entries(this.nodes)) {
            if (
                mx >= node.x - NODE_W / 2 &&
                mx <= node.x + NODE_W / 2 &&
                my >= node.y - NODE_H / 2 &&
                my <= node.y + NODE_H / 2
            ) {
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
}
