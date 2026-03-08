/**
 * Graph renderer — draws agent nodes and edges on a canvas.
 */

const AGENT_COLORS = {
    coder:    { fill: '#e8f0fd', stroke: '#0550ae', text: '#0550ae' },
    reviewer: { fill: '#fdf6e3', stroke: '#7d4e00', text: '#7d4e00' },
    tester:   { fill: '#e6f4ea', stroke: '#1a7f37', text: '#1a7f37' },
    user:     { fill: '#f3effe', stroke: '#8250df', text: '#8250df' },
};

const STATUS_COLORS = {
    idle:      '#9198a1',
    running:   '#0969da',
    completed: '#1a7f37',
    error:     '#cf222e',
};

class GraphRenderer {
    constructor(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.nodes = {};       // {id: {x, y, role, status, model, radius, msgCount, spinAngle}}
        this.edges = [];       // [{source, target}]
        this.activeEdges = []; // [{source, target, progress, startTime}]
        this.pulsingNodes = {}; // {id: startTime}
        this.selectedNode = null;
        this.onNodeClick = null;
        this._resize();
        this._setupEvents();
        this._animationFrame = null;
    }

    _resize() {
        const rect = this.canvas.parentElement.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        this.canvas.width = rect.width * dpr;
        this.canvas.height = rect.height * dpr;
        this.canvas.style.width = rect.width + 'px';
        this.canvas.style.height = rect.height + 'px';
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        this.width = rect.width;
        this.height = rect.height;
    }

    _setupEvents() {
        window.addEventListener('resize', () => {
            this._resize();
            this._layoutNodes();
        });

        this.canvas.addEventListener('click', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            for (const [id, node] of Object.entries(this.nodes)) {
                const dx = x - node.x;
                const dy = y - node.y;
                if (dx * dx + dy * dy < node.radius * node.radius) {
                    this.selectedNode = id;
                    if (this.onNodeClick) this.onNodeClick(id, node);
                    return;
                }
            }
            this.selectedNode = null;
            if (this.onNodeClick) this.onNodeClick(null, null);
        });
    }

    setTopology(agents, edges) {
        for (const agent of agents) {
            this.nodes[agent.id] = {
                x: 0, y: 0,
                role: agent.role,
                status: agent.status || 'idle',
                model: agent.model || '',
                radius: 48,
                msgCount: agent.msg_count || 0,
                spinAngle: 0,
            };
        }
        this.edges = edges;
        this._layoutNodes();
        this.startAnimation();
    }

    _layoutNodes() {
        const ids = Object.keys(this.nodes);
        const cx = this.width / 2;
        const cy = this.height / 2;
        const radius = Math.min(this.width, this.height) * 0.28;

        // Arrange in a circle, with coder at top
        const order = ['coder', 'reviewer', 'tester'];
        const sortedIds = ids.sort((a, b) => {
            const ai = order.indexOf(a);
            const bi = order.indexOf(b);
            return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
        });

        for (let i = 0; i < sortedIds.length; i++) {
            const angle = -Math.PI / 2 + (2 * Math.PI * i) / sortedIds.length;
            this.nodes[sortedIds[i]].x = cx + radius * Math.cos(angle);
            this.nodes[sortedIds[i]].y = cy + radius * Math.sin(angle);
        }
    }

    updateAgentStatus(agentId, status, model) {
        if (this.nodes[agentId]) {
            this.nodes[agentId].status = status;
            if (model) this.nodes[agentId].model = model;
        }
    }

    animateEdge(source, target) {
        this.activeEdges.push({
            source,
            target,
            startTime: performance.now(),
            duration: 800,
        });
        // Pulse the receiving node
        this.pulsingNodes[target] = performance.now();
        // Increment msg count on source node
        if (this.nodes[source]) {
            this.nodes[source].msgCount = (this.nodes[source].msgCount || 0) + 1;
        }
    }

    startAnimation() {
        const animate = () => {
            this._draw();
            this._animationFrame = requestAnimationFrame(animate);
        };
        if (!this._animationFrame) {
            animate();
        }
    }

    _drawDotGrid() {
        const ctx = this.ctx;
        const spacing = 28;
        const dotRadius = 0.8;

        // Light background
        ctx.fillStyle = '#f6f8fa';
        ctx.fillRect(0, 0, this.width, this.height);

        ctx.globalAlpha = 0.6;
        ctx.fillStyle = '#d0d7de';

        const startX = spacing / 2;
        const startY = spacing / 2;

        for (let x = startX; x < this.width; x += spacing) {
            for (let y = startY; y < this.height; y += spacing) {
                ctx.beginPath();
                ctx.arc(x, y, dotRadius, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        ctx.globalAlpha = 1;
    }

    _draw() {
        const ctx = this.ctx;
        const now = performance.now();

        ctx.clearRect(0, 0, this.width, this.height);

        // Subtle dot grid background
        this._drawDotGrid();

        // Draw edges
        for (const edge of this.edges) {
            const src = this.nodes[edge.source];
            const tgt = this.nodes[edge.target];
            if (!src || !tgt) continue;

            const srcColors = AGENT_COLORS[edge.source] || AGENT_COLORS.user;
            const tgtColors = AGENT_COLORS[edge.target] || AGENT_COLORS.user;

            ctx.beginPath();
            ctx.moveTo(src.x, src.y);
            ctx.lineTo(tgt.x, tgt.y);
            ctx.strokeStyle = '#d0d7de';
            ctx.lineWidth = 3;
            ctx.stroke();
        }

        // Draw active edge animations (traveling dot)
        const stillActive = [];
        for (const ae of this.activeEdges) {
            const elapsed = now - ae.startTime;
            const progress = Math.min(1, elapsed / ae.duration);

            const src = this.nodes[ae.source];
            const tgt = this.nodes[ae.target];
            if (!src || !tgt) continue;

            if (progress < 1) {
                stillActive.push(ae);

                // Glowing line
                const grad = ctx.createLinearGradient(src.x, src.y, tgt.x, tgt.y);
                const srcColor = AGENT_COLORS[ae.source]?.stroke || '#0969da';
                grad.addColorStop(Math.max(0, progress - 0.15), 'transparent');
                grad.addColorStop(progress, srcColor);
                grad.addColorStop(Math.min(1, progress + 0.05), 'transparent');

                ctx.beginPath();
                ctx.moveTo(src.x, src.y);
                ctx.lineTo(tgt.x, tgt.y);
                ctx.strokeStyle = grad;
                ctx.lineWidth = 3;
                ctx.stroke();

                // Traveling dot
                const dx = tgt.x - src.x;
                const dy = tgt.y - src.y;
                const dotX = src.x + dx * progress;
                const dotY = src.y + dy * progress;

                ctx.beginPath();
                ctx.arc(dotX, dotY, 7, 0, Math.PI * 2);
                ctx.fillStyle = srcColor;
                ctx.shadowColor = srcColor;
                ctx.shadowBlur = 20;
                ctx.fill();
                ctx.shadowBlur = 0;
            }
        }
        this.activeEdges = stillActive;

        // Draw nodes
        for (const [id, node] of Object.entries(this.nodes)) {
            const colors = AGENT_COLORS[id] || AGENT_COLORS.user;
            const statusColor = STATUS_COLORS[node.status] || STATUS_COLORS.idle;

            // Pulse effect
            let pulseScale = 1;
            if (this.pulsingNodes[id]) {
                const elapsed = now - this.pulsingNodes[id];
                if (elapsed < 600) {
                    pulseScale = 1 + 0.15 * Math.sin((elapsed / 600) * Math.PI);
                } else {
                    delete this.pulsingNodes[id];
                }
            }

            const r = node.radius * pulseScale;

            // Node shadow
            ctx.shadowColor = colors.stroke + '4d'; // ~30% opacity
            ctx.shadowBlur = 10;

            // Node body with radial gradient
            const radGrad = ctx.createRadialGradient(
                node.x - r * 0.25, node.y - r * 0.25, r * 0.05,
                node.x, node.y, r
            );
            radGrad.addColorStop(0, colors.fill + 'ff');
            radGrad.addColorStop(0.6, colors.fill + 'ee');
            radGrad.addColorStop(1, colors.fill + '88');

            ctx.beginPath();
            ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
            ctx.fillStyle = radGrad;
            ctx.fill();
            ctx.shadowBlur = 0;

            ctx.strokeStyle = colors.stroke;
            ctx.lineWidth = 2;
            ctx.stroke();

            // Outer status ring — animated arc for "running", full ring otherwise
            if (node.status === 'running') {
                node.spinAngle = (node.spinAngle || 0) + 0.04;
                const arcStart = node.spinAngle;
                const arcEnd   = arcStart + Math.PI * 1.4;

                ctx.beginPath();
                ctx.arc(node.x, node.y, r + 5, arcStart, arcEnd);
                ctx.strokeStyle = statusColor;
                ctx.lineWidth = 2.5;
                ctx.shadowColor = statusColor;
                ctx.shadowBlur = 8;
                ctx.stroke();
                ctx.shadowBlur = 0;
            } else {
                ctx.beginPath();
                ctx.arc(node.x, node.y, r + 5, 0, Math.PI * 2);
                ctx.strokeStyle = statusColor;
                ctx.lineWidth = 2.5;
                ctx.stroke();
            }

            // Selected highlight
            if (this.selectedNode === id) {
                ctx.beginPath();
                ctx.arc(node.x, node.y, r + 10, 0, Math.PI * 2);
                ctx.strokeStyle = colors.stroke;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 4]);
                ctx.stroke();
                ctx.setLineDash([]);
            }

            // Role label
            ctx.fillStyle = colors.text;
            ctx.font = 'bold 15px "JetBrains Mono", "SF Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(node.role, node.x, node.y - 8);

            // Status text
            ctx.fillStyle = statusColor;
            ctx.font = '11px "JetBrains Mono", "SF Mono", monospace';
            ctx.fillText(node.status, node.x, node.y + 10);

            // Message count at bottom of circle
            if (node.msgCount > 0) {
                ctx.fillStyle = 'rgba(139, 148, 158, 0.7)';
                ctx.font = '9px "JetBrains Mono", monospace';
                ctx.fillText(node.msgCount + ' msg' + (node.msgCount !== 1 ? 's' : ''), node.x, node.y + r - 9);
            }

            // Model below node
            if (node.model) {
                ctx.fillStyle = '#9198a1';
                ctx.font = '9px "JetBrains Mono", "SF Mono", monospace';
                const shortModel = node.model.length > 20 ? node.model.substring(0, 20) + '...' : node.model;
                ctx.fillText(shortModel, node.x, node.y + r + 20);
            }
        }
    }
}
