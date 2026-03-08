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

const NODE_W = 120;
const NODE_H = 56;
const NODE_RADIUS = 8;       // border-radius for rounded rect
const NODE_BORDER = 4;       // left accent border width
const PARTICLE_DURATION = 900; // ms

const AGENT_COLORS = {
    coder:      '#0550ae',
    reviewer:   '#7d4e00',
    reviewer_a: '#7d4e00',
    reviewer_b: '#953800',
    tester:     '#1a7f37',
    user:       '#8250df',
};

const FALLBACK_COLORS = ['#9e1239', '#0369a1', '#92400e', '#6b21a8'];

const STATUS_COLORS = {
    idle:      '#9198a1',
    running:   '#0969da',
    completed: '#1a7f37',
    error:     '#cf222e',
};

const STATUS_BG = {
    idle:      '#f0f2f5',
    running:   '#dbeafe',
    completed: '#dcfce7',
    error:     '#fee2e2',
};

// Fractional canvas positions for known topologies
const LAYOUT_TRIANGLE = {
    coder:    [0.5,  0.2 ],
    reviewer: [0.2,  0.75],
    tester:   [0.8,  0.75],
};

const LAYOUT_4NODE = {
    coder:      [0.5,  0.15],
    reviewer_a: [0.2,  0.5 ],
    reviewer_b: [0.8,  0.5 ],
    tester:     [0.5,  0.85],
};

// ── Helpers ──────────────────────────────────────────────────────────────────

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
                role:     agent.role || id,
                status:   agent.status || 'idle',
                model:    agent.model || '',
                msgCount: agent.msg_count || 0,
                // Layout positions are set by _layoutNodes()
                x: 0, y: 0,
                pulseStart: null,
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
        if (n === 3 && ids.every(id => id in LAYOUT_TRIANGLE)) {
            layoutMap = LAYOUT_TRIANGLE;
        } else if (n === 4 && ids.every(id => id in LAYOUT_4NODE)) {
            layoutMap = LAYOUT_4NODE;
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
            ctx.fillStyle = '#f6f8fa';
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

            const p0 = nodeEdgePoint(src, tgt);
            const p1 = nodeEdgePoint(tgt, src);
            const { cp1, cp2 } = edgeControlPoints(p0, p1);

            ctx.save();
            ctx.strokeStyle = '#d0d7de';
            ctx.lineWidth = 1.5;
            ctx.lineCap = 'round';
            ctx.beginPath();
            ctx.moveTo(p0.x, p0.y);
            ctx.bezierCurveTo(cp1.x, cp1.y, cp2.x, cp2.y, p1.x, p1.y);
            ctx.stroke();

            // Arrowhead at target
            this._drawArrow(ctx, cp2, p1, '#d0d7de');
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

        // Pulse animation
        let pulse = 0;
        if (node.pulseStart !== null) {
            const elapsed = now - node.pulseStart;
            const dur = 600;
            if (elapsed < dur) {
                pulse = Math.sin((elapsed / dur) * Math.PI);
            } else {
                node.pulseStart = null;
            }
        }

        ctx.save();

        // Outer blue ring for selected node
        if (isSelected) {
            ctx.save();
            ctx.strokeStyle = 'rgba(9, 105, 218, 0.45)';
            ctx.lineWidth   = 3;
            roundRect(ctx, x - 5, y - 5, w + 10, h + 10, r + 4);
            ctx.stroke();
            ctx.restore();
        }

        // Drop shadow
        ctx.save();
        ctx.shadowColor  = 'rgba(0,0,0,0.10)';
        ctx.shadowBlur   = isHovered ? 14 : 8;
        ctx.shadowOffsetX = 0;
        ctx.shadowOffsetY = 2;
        ctx.fillStyle = '#ffffff';
        roundRect(ctx, x, y, w, h, r);
        ctx.fill();
        ctx.restore();

        // White body (no shadow)
        ctx.fillStyle = '#ffffff';
        roundRect(ctx, x, y, w, h, r);
        ctx.fill();

        // Border
        const borderAlpha = isSelected ? 1 : (isHovered ? 0.8 : 0.5);
        ctx.strokeStyle = `rgba(208,215,222,${borderAlpha})`;
        ctx.lineWidth   = 1;
        roundRect(ctx, x, y, w, h, r);
        ctx.stroke();

        // Left accent border
        const accentIntensity = isSelected ? 1 : (pulse > 0 ? 0.6 + 0.4 * pulse : 0.85);
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x + r,   y);
        ctx.arcTo(x, y,     x, y + r,     r);
        ctx.lineTo(x, y + h - r);
        ctx.arcTo(x, y + h, x + r, y + h, r);
        ctx.lineTo(x + NODE_BORDER, y + h - r);
        ctx.arcTo(x + NODE_BORDER, y + h, x + NODE_BORDER, y + h - r, 0);
        ctx.lineTo(x + NODE_BORDER, y + r);
        ctx.arcTo(x + NODE_BORDER, y, x + r, y, 0);
        ctx.closePath();
        ctx.fillStyle = col + Math.round(accentIntensity * 255).toString(16).padStart(2, '0');
        ctx.fill();
        ctx.restore();

        // Clean left border using clip + fillRect approach
        ctx.save();
        roundRect(ctx, x, y, w, h, r);
        ctx.clip();
        ctx.fillStyle = col;
        ctx.globalAlpha = accentIntensity;
        ctx.fillRect(x, y, NODE_BORDER, h);
        ctx.globalAlpha = 1;
        ctx.restore();

        // Text content
        const textX = x + NODE_BORDER + 10;
        const lineH  = 14;

        // Agent name (bold, colored)
        ctx.font      = 'bold 13px Inter, -apple-system, sans-serif';
        ctx.fillStyle = col;
        ctx.textBaseline = 'top';
        ctx.fillText(node.role, textX, y + 8, w - NODE_BORDER - 14);

        // Role label / subtitle (gray, smaller)
        ctx.font      = '10px Inter, -apple-system, sans-serif';
        ctx.fillStyle = '#656d76';
        ctx.fillText(node.id !== node.role ? node.id : '', textX, y + 8 + lineH, w - NODE_BORDER - 14);

        // Status badge
        const badgeText  = node.status;
        const badgeColor = STATUS_COLORS[node.status] || STATUS_COLORS.idle;
        const badgeBg    = STATUS_BG[node.status]     || STATUS_BG.idle;
        ctx.font = 'bold 8px JetBrains Mono, monospace';
        const badgeW   = ctx.measureText(badgeText).width + 8;
        const badgeH   = 12;
        const badgeX   = x + w - badgeW - 6;
        const badgeY   = y + 6;

        ctx.fillStyle = badgeBg;
        ctx.beginPath();
        ctx.roundRect(badgeX, badgeY, badgeW, badgeH, 6);
        ctx.fill();
        ctx.fillStyle = badgeColor;
        ctx.textAlign    = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(badgeText, badgeX + badgeW / 2, badgeY + badgeH / 2);

        // Model name (muted, bottom row)
        if (node.model) {
            const modelStr = node.model.length > 18 ? node.model.slice(0, 18) + '…' : node.model;
            ctx.font      = '9px JetBrains Mono, monospace';
            ctx.fillStyle = '#9198a1';
            ctx.textAlign    = 'left';
            ctx.textBaseline = 'bottom';
            ctx.fillText(modelStr, textX, y + h - 7, w - NODE_BORDER - 14);
        }

        // Running pulse: animated border glow
        if (node.status === 'running') {
            const t   = (now % 1400) / 1400;
            const glow = 0.25 + 0.25 * Math.sin(t * Math.PI * 2);
            ctx.save();
            ctx.strokeStyle = STATUS_COLORS.running;
            ctx.lineWidth   = 1.5;
            ctx.globalAlpha = glow;
            roundRect(ctx, x, y, w, h, r);
            ctx.stroke();
            ctx.restore();
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
        ctx.fillStyle = '#f6f8fa';
        ctx.fillRect(0, 0, w, h);
        const spacing = 28;
        ctx.fillStyle = '#d0d7de';
        for (let x = spacing / 2; x < w; x += spacing) {
            for (let y = spacing / 2; y < h; y += spacing) {
                ctx.beginPath();
                ctx.arc(x, y, 1, 0, Math.PI * 2);
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
