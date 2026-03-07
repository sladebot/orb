/**
 * Graph renderer — draws agent nodes and edges on a canvas.
 */

const AGENT_COLORS = {
    coder: { fill: '#1a3a5c', stroke: '#79c0ff', text: '#79c0ff' },
    reviewer: { fill: '#3d3219', stroke: '#e3b341', text: '#e3b341' },
    tester: { fill: '#1a3d26', stroke: '#56d364', text: '#56d364' },
    user: { fill: '#2d1f4e', stroke: '#bc8cff', text: '#bc8cff' },
};

const STATUS_COLORS = {
    idle: '#484f58',
    running: '#58a6ff',
    completed: '#3fb950',
    error: '#f85149',
};

class GraphRenderer {
    constructor(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.nodes = {};       // {id: {x, y, role, status, model, radius}}
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
                radius: 40,
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

    _draw() {
        const ctx = this.ctx;
        const now = performance.now();

        ctx.clearRect(0, 0, this.width, this.height);

        // Draw edges
        for (const edge of this.edges) {
            const src = this.nodes[edge.source];
            const tgt = this.nodes[edge.target];
            if (!src || !tgt) continue;

            ctx.beginPath();
            ctx.moveTo(src.x, src.y);
            ctx.lineTo(tgt.x, tgt.y);
            ctx.strokeStyle = '#21262d';
            ctx.lineWidth = 2;
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
                const srcColor = AGENT_COLORS[ae.source]?.stroke || '#58a6ff';
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
                ctx.arc(dotX, dotY, 5, 0, Math.PI * 2);
                ctx.fillStyle = srcColor;
                ctx.shadowColor = srcColor;
                ctx.shadowBlur = 12;
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

            // Outer status ring
            ctx.beginPath();
            ctx.arc(node.x, node.y, r + 4, 0, Math.PI * 2);
            ctx.strokeStyle = statusColor;
            ctx.lineWidth = 3;
            ctx.stroke();

            // Node body
            ctx.beginPath();
            ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
            ctx.fillStyle = colors.fill;
            ctx.fill();
            ctx.strokeStyle = colors.stroke;
            ctx.lineWidth = 2;
            ctx.stroke();

            // Selected highlight
            if (this.selectedNode === id) {
                ctx.beginPath();
                ctx.arc(node.x, node.y, r + 8, 0, Math.PI * 2);
                ctx.strokeStyle = colors.stroke;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 4]);
                ctx.stroke();
                ctx.setLineDash([]);
            }

            // Role label
            ctx.fillStyle = colors.text;
            ctx.font = 'bold 14px "SF Mono", "Fira Code", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(node.role, node.x, node.y - 6);

            // Status text
            ctx.fillStyle = statusColor;
            ctx.font = '10px "SF Mono", "Fira Code", monospace';
            ctx.fillText(node.status, node.x, node.y + 12);

            // Model below node
            if (node.model) {
                ctx.fillStyle = '#484f58';
                ctx.font = '9px "SF Mono", "Fira Code", monospace';
                const shortModel = node.model.length > 20 ? node.model.substring(0, 20) + '...' : node.model;
                ctx.fillText(shortModel, node.x, node.y + r + 20);
            }
        }
    }
}
