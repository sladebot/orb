/**
 * Dashboard app — WebSocket client, message log, agent cards, chat panel.
 */

const AGENT_CSS_CLASS = {
    coder:    'agent-coder',
    reviewer: 'agent-reviewer',
    tester:   'agent-tester',
    user:     'agent-user',
};

const AGENT_CARD_CLASS = {
    coder:    'agent-card-coder',
    reviewer: 'agent-card-reviewer',
    tester:   'agent-card-tester',
};

const MSG_TYPE_BADGE_CLASS = {
    task:     'msg-type-task',
    response: 'msg-type-response',
    feedback: 'msg-type-feedback',
    complete: 'msg-type-complete',
    system:   'msg-type-system',
};

class Dashboard {
    constructor() {
        this.canvas      = document.getElementById('graph-canvas');
        this.graph       = new GraphRenderer(this.canvas);
        this.messageLog  = document.getElementById('message-log');
        this.agentCards  = document.getElementById('agent-cards');
        this.ws          = null;
        this.agents      = {};       // id -> agent data object
        this.selectedAgent = null;   // currently selected agent id
        this.reconnectDelay = 1000;

        // Tab switching
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => this._switchTab(btn.dataset.tab));
        });

        // Graph node click
        this.graph.onNodeClick = (id, node) => this._selectAgent(id);

        // Chat panel controls
        document.getElementById('chat-close').addEventListener('click', () => this._selectAgent(null));
        document.getElementById('chat-send').addEventListener('click', () => this._sendChatMessage());
        document.getElementById('chat-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._sendChatMessage();
        });

        this._connect();
    }

    // ── WebSocket ─────────────────────────────────────────────

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

        this.ws.onerror = () => { this.ws.close(); };

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

    // ── Event dispatch ────────────────────────────────────────

    _handleEvent(data) {
        switch (data.type) {
            case 'init':          this._handleInit(data);        break;
            case 'message':       this._handleMessage(data);     break;
            case 'agent_status':  this._handleAgentStatus(data); break;
            case 'agent_stats':   this._handleAgentStats(data);  break;
            case 'complete':      this._handleComplete(data);    break;
            case 'stats':         this._handleStats(data);       break;
        }
    }

    _handleInit(data) {
        this.agents = {};
        for (const agent of data.agents) {
            this.agents[agent.id] = {
                id: agent.id,
                role: agent.role,
                status: agent.status || 'idle',
                model: agent.model || '',
                msg_count: agent.msg_count || 0,
                result: agent.completed_result || '',
            };
        }

        this.graph.setTopology(data.agents, data.edges);

        // Render existing messages
        this.messageLog.innerHTML = '';
        for (const msg of data.messages) {
            this._addMessageEntry(msg);
        }

        // Render agent cards
        this._rebuildAgentCards();
        this._updateStatusIndicator();

        if (data.stats) this._handleStats(data.stats);
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
            if (data.model) this.agents[data.agent].model = data.model;
        }
        this.graph.updateAgentStatus(data.agent, data.status, data.model);
        this._updateAgentCard(data.agent);
        this._updateStatusIndicator();
    }

    _handleAgentStats(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].msg_count = data.msg_count;
            if (data.status) this.agents[data.agent].status = data.status;
            if (data.model)  this.agents[data.agent].model  = data.model;
        }
        this._updateAgentCard(data.agent);
    }

    _handleComplete(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].status = 'completed';
            this.agents[data.agent].result = data.result;
        }
        this.graph.updateAgentStatus(data.agent, 'completed');
        this._updateAgentCard(data.agent);
        this._updateStatusIndicator();
    }

    _handleStats(data) {
        document.getElementById('stat-messages').textContent = data.message_count;
        document.getElementById('stat-budget').textContent   = data.budget_remaining;
        document.getElementById('stat-elapsed').textContent  = data.elapsed.toFixed(1) + 's';
    }

    _updateStatusIndicator() {
        const el       = document.getElementById('stat-status');
        const statuses = Object.values(this.agents).map(a => a.status);

        if (statuses.length === 0) return;

        if (statuses.every(s => s === 'completed')) {
            el.textContent = 'Done';
            el.className   = 'stat-value status-done';
        } else if (statuses.some(s => s === 'running')) {
            el.textContent = 'Running';
            el.className   = 'stat-value status-running';
        } else {
            el.textContent = 'Waiting';
            el.className   = 'stat-value status-waiting';
        }
    }

    // ── Tab switching ─────────────────────────────────────────

    _switchTab(tabName) {
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });
        document.querySelectorAll('.tab-pane').forEach(pane => {
            pane.classList.toggle('active', pane.id === `tab-${tabName}`);
        });
    }

    // ── Message log ───────────────────────────────────────────

    _addMessageEntry(msg) {
        const entry = document.createElement('div');
        entry.className = 'msg-entry';

        const fromClass = AGENT_CSS_CLASS[msg.from] || 'agent-user';
        const toClass   = AGENT_CSS_CLASS[msg.to]   || 'agent-user';
        const elapsed   = msg.elapsed !== undefined ? msg.elapsed.toFixed(1) + 's' : '';
        const msgType   = msg.msg_type || msg.type || 'system';
        const badgeCls  = MSG_TYPE_BADGE_CLASS[msgType] || 'msg-type-system';
        const depth     = msg.depth !== undefined ? msg.depth : '';
        const preview   = (msg.content || '').split('\n')[0].slice(0, 120);

        // Build context section HTML
        let contextHtml = '';
        const slices = msg.context_slice || [];
        if (slices.length > 0) {
            const items = slices.map((s, i) =>
                `<div class="msg-context-item">[${i}] ${this._escapeHtml(s)}</div>`
            ).join('');
            contextHtml = `
                <div class="msg-section-label">Context (${slices.length} items)</div>
                ${items}
            `;
        }

        entry.innerHTML = `
            <div class="msg-header">
                <span class="msg-time">${elapsed}</span>
                <span class="${fromClass}">${msg.from}</span>
                <span class="msg-arrow">&rarr;</span>
                <span class="${toClass}">${msg.to}</span>
                <span class="msg-type-badge ${badgeCls}">${msgType}</span>
                ${depth !== '' ? `<span class="msg-depth-badge">${depth}</span>` : ''}
                <span class="msg-model">${msg.model || ''}</span>
            </div>
            <div class="msg-preview">${this._escapeHtml(preview)}</div>
            <div class="msg-expanded">
                <div class="msg-section-label">Payload</div>
                <div class="msg-full-content">${this._escapeHtml(msg.content || '')}</div>
                ${contextHtml}
            </div>
        `;

        entry.addEventListener('click', () => entry.classList.toggle('expanded'));

        this.messageLog.appendChild(entry);
        this.messageLog.scrollTop = this.messageLog.scrollHeight;
    }

    // ── Agent cards ───────────────────────────────────────────

    _rebuildAgentCards() {
        this.agentCards.innerHTML = '';
        for (const agent of Object.values(this.agents)) {
            this._createAgentCard(agent);
        }
    }

    _createAgentCard(agent) {
        const card = document.createElement('div');
        card.id = `agent-card-${agent.id}`;
        this._renderAgentCard(card, agent);
        card.addEventListener('click', () => this._selectAgent(agent.id));
        this.agentCards.appendChild(card);
    }

    _renderAgentCard(card, agent) {
        const agentClass = AGENT_CSS_CLASS[agent.id]   || 'agent-user';
        const borderCls  = AGENT_CARD_CLASS[agent.id]  || '';
        const statusCls  = `status-badge-${agent.status}`;
        const isSelected = this.selectedAgent === agent.id;

        card.className = `agent-card ${borderCls}${isSelected ? ' selected' : ''}`;

        // Result expand toggle
        const hasResult = agent.status === 'completed' && agent.result;
        const resultHtml = hasResult ? `
            <div class="agent-result-section">
                <div class="msg-section-label">Result</div>
                <div class="agent-result-content">${this._escapeHtml(agent.result)}</div>
            </div>
        ` : '';

        card.innerHTML = `
            <div class="agent-card-header">
                <span class="agent-card-name ${agentClass}">${agent.id}</span>
                <span class="agent-card-role">${agent.role}</span>
                <span class="agent-status-badge ${statusCls}">${agent.status}</span>
            </div>
            <div class="agent-card-meta">
                <span>model: ${agent.model || '—'}</span>
                <span>msgs: ${agent.msg_count}</span>
            </div>
            ${resultHtml}
        `;

        if (hasResult) {
            card.classList.add('result-expanded');
        }
    }

    _updateAgentCard(agentId) {
        const agent = this.agents[agentId];
        if (!agent) return;

        let card = document.getElementById(`agent-card-${agentId}`);
        if (!card) {
            this._createAgentCard(agent);
        } else {
            this._renderAgentCard(card, agent);
            // Re-attach click listener (innerHTML wipe removed it)
            card.addEventListener('click', () => this._selectAgent(agentId));
        }
    }

    // ── Agent selection & chat panel ─────────────────────────

    _selectAgent(agentId) {
        this.selectedAgent = agentId;

        // Update graph selection
        this.graph.selectedNode = agentId;

        // Update card selection styling
        document.querySelectorAll('.agent-card').forEach(c => c.classList.remove('selected'));
        if (agentId) {
            const card = document.getElementById(`agent-card-${agentId}`);
            if (card) card.classList.add('selected');
        }

        // Show/hide chat panel
        const chatPanel = document.getElementById('chat-panel');
        if (agentId) {
            const agent = this.agents[agentId] || {};
            document.getElementById('chat-title').textContent = `Chat with ${agentId}`;
            chatPanel.classList.remove('hidden');
            document.getElementById('chat-input').focus();
        } else {
            chatPanel.classList.add('hidden');
        }
    }

    async _sendChatMessage() {
        if (!this.selectedAgent) return;

        const input = document.getElementById('chat-input');
        const text  = input.value.trim();
        if (!text) return;

        const btn = document.getElementById('chat-send');
        btn.disabled = true;
        input.value  = '';

        // Show sent message locally
        const chatMessages = document.getElementById('chat-messages');
        const sentEl = document.createElement('div');
        sentEl.className = 'chat-msg-sent';
        sentEl.textContent = text;
        chatMessages.appendChild(sentEl);
        chatMessages.scrollTop = chatMessages.scrollHeight;

        // Also add to the message log immediately
        this._addMessageEntry({
            from:     'user',
            to:       this.selectedAgent,
            content:  text,
            elapsed:  0,
            model:    '',
            depth:    0,
            msg_type: 'task',
            context_slice: [],
        });

        try {
            const res = await fetch('/api/inject', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ to: this.selectedAgent, message: text }),
            });
            const json = await res.json();
            if (!json.ok) {
                const errEl = document.createElement('div');
                errEl.style.cssText = 'color:#f85149;font-size:10px;margin-bottom:3px';
                errEl.textContent = `Error: ${json.error}`;
                chatMessages.appendChild(errEl);
                chatMessages.scrollTop = chatMessages.scrollHeight;
            }
        } catch (err) {
            const errEl = document.createElement('div');
            errEl.style.cssText = 'color:#f85149;font-size:10px;margin-bottom:3px';
            errEl.textContent = `Network error: ${err.message}`;
            chatMessages.appendChild(errEl);
            chatMessages.scrollTop = chatMessages.scrollHeight;
        } finally {
            btn.disabled = false;
            input.focus();
        }
    }

    // ── Utilities ─────────────────────────────────────────────

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }
}

// Initialize on load
window.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new Dashboard();
});
