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
const NODE_RADIUS = 16;
const PARTICLE_DURATION = 900; // ms

const AGENT_COLORS = {
    coordinator: '#acc7ff',
    coder:       '#7cb7ff',
    reviewer:    '#ffd37f',
    reviewer_a:  '#ffd37f',
    reviewer_b:  '#ffb47d',
    tester:      '#7de2a7',
    user:        '#cdbdff',
};

const FALLBACK_COLORS = ['#cdbdff', '#acc7ff', '#ffd37f', '#7de2a7'];

const STATUS_COLORS = {
    idle:      '#8e97a8',
    running:   '#acc7ff',
    completed: '#7de2a7',
    error:     '#ffb4ab',
};

const STATUS_BADGE = {
    idle:      { bg: 'rgba(193,198,214,0.12)', fg: '#c1c6d6' },
    running:   { bg: 'rgba(172,199,255,0.14)', fg: '#acc7ff' },
    completed: { bg: 'rgba(125,226,167,0.16)', fg: '#7de2a7' },
    error:     { bg: 'rgba(255,180,171,0.16)', fg: '#ffb4ab' },
    thinking:  { bg: 'rgba(205,189,255,0.14)', fg: '#cdbdff' },
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
    const cx = node.x;
    const cy = node.y;
    const dx = other.x - cx;
    const dy = other.y - cy;
    const dist = Math.hypot(dx, dy) || 1;
    const r = node.renderRadius || NODE_RADIUS;
    return { x: cx + (dx / dist) * r, y: cy + (dy / dist) * r };
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
        this.zoom = 1;
        this.minZoom = 0.7;
        this.maxZoom = 1.8;
        this.offsetX = 0;
        this.offsetY = 0;

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
        const cx = W / 2;
        const cy = H / 2;
        const hasCoordinator = Boolean(this.nodes.coordinator);
        const ringIds = hasCoordinator ? ids.filter((id) => id !== 'coordinator') : ids;
        const ringCount = ringIds.length;
        const radius = Math.max(140, Math.min(W, H) * 0.29);

        if (hasCoordinator) {
            this.nodes.coordinator.x = cx;
            this.nodes.coordinator.y = cy;
        }

        ringIds.forEach((id, i) => {
            const angleOffset = hasCoordinator ? -Math.PI / 2 : -Math.PI / 2 - Math.PI / 8;
            const angle = angleOffset + (2 * Math.PI * i) / Math.max(ringCount, 1);
            const nodeRadius = radius * (i % 2 === 0 ? 1 : 0.9);
            this.nodes[id].x = cx + nodeRadius * Math.cos(angle);
            this.nodes[id].y = cy + nodeRadius * Math.sin(angle);
        });
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
            ctx.fillStyle = '#0a0f13';
            ctx.fillRect(-this.offsetX / this.zoom, -this.offsetY / this.zoom, W / this.zoom, H / this.zoom);
        }

        this._drawCoreBackdrop(ctx, W, H);

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
        ctx.fillStyle = 'rgba(65, 71, 84, 0.34)';
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
            const edgeColor = srcActive ? 'rgba(172,199,255,0.45)' : 'rgba(65,71,84,0.55)';
            const arrowColor = srcActive ? '#acc7ff' : '#6d7788';

            const p0 = nodeEdgePoint(src, tgt);
            const p1 = nodeEdgePoint(tgt, src);
            const { cp1, cp2 } = edgeControlPoints(p0, p1);

            ctx.save();
            ctx.strokeStyle = edgeColor;
            ctx.lineWidth   = srcActive ? 1.8 : 1.2;
            ctx.lineCap     = 'round';
            ctx.setLineDash(srcActive ? [8, 8] : [5, 10]);
            ctx.beginPath();
            ctx.moveTo(p0.x, p0.y);
            ctx.bezierCurveTo(cp1.x, cp1.y, cp2.x, cp2.y, p1.x, p1.y);
            ctx.stroke();
            ctx.setLineDash([]);

            // Arrowhead at target
            this._drawArrow(ctx, cp2, p1, arrowColor);
            ctx.restore();
        }
    }

    _drawCoreBackdrop(ctx, W, H) {
        const cx = W / 2;
        const cy = H / 2;
        ctx.save();

        const outer = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.min(W, H) * 0.34);
        outer.addColorStop(0, 'rgba(172, 199, 255, 0.09)');
        outer.addColorStop(0.45, 'rgba(82, 3, 213, 0.08)');
        outer.addColorStop(1, 'rgba(10, 15, 19, 0)');
        ctx.fillStyle = outer;
        ctx.beginPath();
        ctx.arc(cx, cy, Math.min(W, H) * 0.34, 0, Math.PI * 2);
        ctx.fill();

        ctx.strokeStyle = 'rgba(65, 71, 84, 0.2)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 10]);
        ctx.beginPath();
        ctx.arc(cx, cy, Math.min(W, H) * 0.18, 0, Math.PI * 2);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(cx, cy, Math.min(W, H) * 0.29, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.restore();
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
        if (this.nodes.coordinator) {
            this._drawNode(ctx, this.nodes.coordinator, now);
        }
        for (const [id, node] of Object.entries(this.nodes)) {
            if (id === 'coordinator') continue;
            this._drawNode(ctx, node, now);
        }
    }

    _drawNode(ctx, node, now) {
        const accent = statusColor(node.thinking ? 'running' : node.status, node.color);
        const isSelected = this.selectedNode === node.id;
        const isHovered  = this._hoveredId   === node.id;
        const isCore = node.id === 'coordinator';
        const ringRadius = isCore ? 30 : 12;
        node.renderRadius = ringRadius + 8;

        // Pulse on message receive
        let pulse = 0;
        if (node.pulseStart !== null) {
            const elapsed = now - node.pulseStart;
            if (elapsed < 600) pulse = Math.sin((elapsed / 600) * Math.PI);
            else node.pulseStart = null;
        }

        const runPhase = (now % 2000) / 2000;

        ctx.save();

        const glowStrength = isSelected ? 34 : isHovered ? 22 : isCore ? 20 : 14;
        const glowAlpha = isSelected ? 0.7 : isHovered ? 0.45 : 0.28 + pulse * 0.2;
        if (isSelected || isHovered || pulse > 0.08 || isCore) {
            ctx.save();
            ctx.shadowColor = accent;
            ctx.shadowBlur = glowStrength;
            ctx.fillStyle = accent;
            ctx.globalAlpha = glowAlpha;
            ctx.beginPath();
            ctx.arc(node.x, node.y, ringRadius + 10 + pulse * 6, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();
        }

        ctx.lineWidth = isCore ? 1.6 : 1.2;
        ctx.strokeStyle = isSelected ? accent : 'rgba(65,71,84,0.44)';
        ctx.fillStyle = isCore ? 'rgba(27,32,37,0.86)' : 'rgba(27,32,37,0.96)';
        ctx.beginPath();
        ctx.arc(node.x, node.y, ringRadius, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        const statusKey  = node.thinking ? 'thinking' : (node.status || 'idle');
        const innerRadius = isCore ? 12 : 4.5;
        const innerGlow = accent;

        ctx.save();
        ctx.shadowColor = innerGlow;
        ctx.shadowBlur = isCore ? 22 : 12;
        ctx.fillStyle = innerGlow;
        ctx.beginPath();
        ctx.arc(node.x, node.y, innerRadius + pulse * (isCore ? 4 : 1.6), 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        if (node.thinking || node.status === 'running') {
            ctx.save();
            ctx.strokeStyle = accent;
            ctx.globalAlpha = 0.4 + 0.25 * Math.sin(runPhase * Math.PI * 2);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.arc(node.x, node.y, ringRadius + 6 + Math.sin(runPhase * Math.PI * 2) * 2, 0, Math.PI * 2);
            ctx.stroke();
            ctx.restore();
        }

        const showLabel = true;
        if (showLabel) {
            this._drawNodeLabel(ctx, node, isCore);
        }

        ctx.restore();
    }

    _drawNodeLabel(ctx, node, isCore) {
        const title = node.role || node.id;
        const model = node.model ? _shortModel(node.model) : '';
        const subtitle = node.lastActivity || model || `${node.msgCount || 0} messages`;

        ctx.save();
        ctx.font = '700 12px Inter, -apple-system, sans-serif';
        const titleW = ctx.measureText(title).width;
        ctx.font = '10px "JetBrains Mono", monospace';
        const subW = ctx.measureText(subtitle).width;
        const contentW = Math.max(titleW, subW);
        const padX = 12;
        const boxW = Math.min(Math.max(contentW + padX * 2, isCore ? 124 : 110), isCore ? 220 : 190);
        const boxH = isCore ? 52 : 42;
        const viewportW = this.canvas.width / this._dpr;
        const viewportH = this.canvas.height / this._dpr;
        const worldMinX = -this.offsetX / this.zoom;
        const worldMinY = -this.offsetY / this.zoom;
        const worldMaxX = worldMinX + viewportW / this.zoom;
        const worldMaxY = worldMinY + viewportH / this.zoom;
        const boxX = Math.max(worldMinX + 16 / this.zoom, Math.min(worldMaxX - boxW - 16 / this.zoom, node.x - boxW / 2));
        const desiredY = isCore ? node.y + 44 : node.y + 20;
        const boxY = Math.max(worldMinY + 16 / this.zoom, Math.min(worldMaxY - boxH - 16 / this.zoom, desiredY));

        ctx.shadowColor = 'rgba(0, 0, 0, 0.42)';
        ctx.shadowBlur = isCore ? 24 : 14;
        ctx.fillStyle = isCore ? 'rgba(48, 53, 58, 0.72)' : 'rgba(27, 32, 37, 0.82)';
        roundRect(ctx, boxX, boxY, boxW, boxH, 14);
        ctx.fill();

        ctx.shadowBlur = 0;
        ctx.strokeStyle = isCore ? 'rgba(65, 71, 84, 0.18)' : 'rgba(65, 71, 84, 0.12)';
        ctx.lineWidth = 1;
        roundRect(ctx, boxX, boxY, boxW, boxH, 14);
        ctx.stroke();

        ctx.fillStyle = '#dee3e9';
        ctx.font = '700 12px Inter, -apple-system, sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText(title, boxX + 12, boxY + 10);

        if (isCore) {
            ctx.font = '10px "JetBrains Mono", monospace';
            ctx.fillStyle = '#8e97a8';
            let sub = subtitle;
            while (sub.length && ctx.measureText(sub).width > boxW - 24) sub = sub.slice(0, -1);
            if (sub !== subtitle) sub += '…';
            ctx.fillText(sub, boxX + 12, boxY + 28);
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
        ctx.fillStyle = '#0a0f13';
        ctx.fillRect(0, 0, w, h);
        const spacing = 26;
        ctx.fillStyle = 'rgba(65,71,84,0.32)';
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
            const r = (node.renderRadius || NODE_RADIUS) + 10;
            if (Math.hypot(mx - node.x, my - node.y) <= r) {
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
