/**
 * Dashboard app — WebSocket client, message log, stats bar.
 */

const AGENT_CSS_CLASS = {
    coder: 'agent-coder',
    reviewer: 'agent-reviewer',
    tester: 'agent-tester',
    user: 'agent-user',
};

class Dashboard {
    constructor() {
        this.canvas = document.getElementById('graph-canvas');
        this.graph = new GraphRenderer(this.canvas);
        this.messageLog = document.getElementById('message-log');
        this.agentDetails = document.getElementById('agent-details');
        this.ws = null;
        this.agents = {};
        this.reconnectDelay = 1000;

        this.graph.onNodeClick = (id, node) => this._showAgentDetail(id, node);

        this._connect();
    }

    _connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        this.ws = new WebSocket(wsUrl);
        this._setConnectionStatus(false);

        this.ws.onopen = () => {
            this._setConnectionStatus(true);
            this.reconnectDelay = 1000;
        };

        this.ws.onclose = () => {
            this._setConnectionStatus(false);
            setTimeout(() => this._connect(), this.reconnectDelay);
            this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10000);
        };

        this.ws.onerror = () => {
            this.ws.close();
        };

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            this._handleEvent(data);
        };
    }

    _setConnectionStatus(connected) {
        const el = document.getElementById('connection-indicator');
        el.className = connected ? 'connected' : 'disconnected';
        el.title = connected ? 'Connected' : 'Disconnected';
    }

    _handleEvent(data) {
        switch (data.type) {
            case 'init':
                this._handleInit(data);
                break;
            case 'message':
                this._handleMessage(data);
                break;
            case 'agent_status':
                this._handleAgentStatus(data);
                break;
            case 'complete':
                this._handleComplete(data);
                break;
            case 'stats':
                this._handleStats(data);
                break;
        }
    }

    _handleInit(data) {
        // Store agent info
        this.agents = {};
        for (const agent of data.agents) {
            this.agents[agent.id] = agent;
        }

        // Set up graph
        this.graph.setTopology(data.agents, data.edges);

        // Render existing messages
        this.messageLog.innerHTML = '';
        for (const msg of data.messages) {
            this._addMessageEntry(msg);
        }

        // Update stats
        if (data.stats) {
            this._handleStats(data.stats);
        }

        this._updateStatusIndicator();
    }

    _handleMessage(data) {
        this._addMessageEntry(data);
        this.graph.animateEdge(data.from, data.to);
        this.graph.updateAgentStatus(data.from, 'running', data.model);
        this._updateStatusIndicator();
    }

    _handleAgentStatus(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].status = data.status;
            this.agents[data.agent].model = data.model || this.agents[data.agent].model;
        }
        this.graph.updateAgentStatus(data.agent, data.status, data.model);
        this._updateStatusIndicator();
    }

    _handleComplete(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].status = 'completed';
            this.agents[data.agent].result = data.result;
        }
        this.graph.updateAgentStatus(data.agent, 'completed');
        this._addCompletionEntry(data);
        this._updateStatusIndicator();
    }

    _handleStats(data) {
        document.getElementById('stat-messages').textContent = data.message_count;
        document.getElementById('stat-budget').textContent = data.budget_remaining;
        document.getElementById('stat-elapsed').textContent = data.elapsed.toFixed(1) + 's';
    }

    _updateStatusIndicator() {
        const el = document.getElementById('stat-status');
        const statuses = Object.values(this.agents).map(a => a.status);

        if (statuses.every(s => s === 'completed')) {
            el.textContent = 'Done';
            el.className = 'stat-value status-done';
        } else if (statuses.some(s => s === 'running')) {
            el.textContent = 'Running';
            el.className = 'stat-value status-running';
        } else {
            el.textContent = 'Waiting';
            el.className = 'stat-value status-waiting';
        }
    }

    _addMessageEntry(msg) {
        const entry = document.createElement('div');
        entry.className = 'msg-entry';

        const fromClass = AGENT_CSS_CLASS[msg.from] || 'agent-user';
        const toClass = AGENT_CSS_CLASS[msg.to] || 'agent-user';
        const elapsed = msg.elapsed !== undefined ? msg.elapsed.toFixed(1) + 's' : '';

        entry.innerHTML = `
            <div class="msg-header">
                <span class="msg-time">${elapsed}</span>
                <span class="${fromClass}">${msg.from}</span>
                <span class="msg-arrow">&rarr;</span>
                <span class="${toClass}">${msg.to}</span>
                <span class="msg-model">${msg.model || ''}</span>
            </div>
            <div class="msg-content">${this._escapeHtml(msg.content || '')}</div>
        `;

        entry.addEventListener('click', () => {
            entry.classList.toggle('expanded');
        });

        this.messageLog.appendChild(entry);
        this.messageLog.scrollTop = this.messageLog.scrollHeight;
    }

    _addCompletionEntry(data) {
        const entry = document.createElement('div');
        const agentClass = AGENT_CSS_CLASS[data.agent] || 'agent-user';
        entry.className = 'msg-entry';
        entry.style.borderColor = '#3fb950';

        entry.innerHTML = `
            <div class="msg-header">
                <span class="${agentClass}">${data.agent}</span>
                <span style="color: #3fb950; margin-left: 8px;">COMPLETED</span>
            </div>
            <div class="msg-content">${this._escapeHtml(data.result || '')}</div>
        `;

        entry.addEventListener('click', () => {
            entry.classList.toggle('expanded');
        });

        this.messageLog.appendChild(entry);
        this.messageLog.scrollTop = this.messageLog.scrollHeight;
    }

    _showAgentDetail(id, node) {
        if (!id || !node) {
            this.agentDetails.innerHTML = '<span class="detail-placeholder">Click an agent node to see details</span>';
            return;
        }

        const agent = this.agents[id] || {};
        const agentClass = AGENT_CSS_CLASS[id] || '';

        this.agentDetails.innerHTML = `
            <div class="detail-grid">
                <span class="detail-label">Agent</span>
                <span class="detail-value ${agentClass}">${node.role} (${id})</span>
                <span class="detail-label">Status</span>
                <span class="detail-value">${node.status}</span>
                <span class="detail-label">Model</span>
                <span class="detail-value">${node.model || 'none'}</span>
                ${agent.result ? `<span class="detail-label">Result</span><span class="detail-value">${this._escapeHtml(agent.result).substring(0, 200)}</span>` : ''}
            </div>
        `;
    }

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize on load
window.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new Dashboard();
});
