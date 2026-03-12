/**
 * Dashboard app — WebSocket client, message log, agent cards, chat panel.
 */

const AGENT_CSS_CLASS = {
    coordinator: 'agent-coordinator',
    coder:       'agent-coder',
    reviewer:    'agent-reviewer',
    reviewer_a:  'agent-reviewer',
    reviewer_b:  'agent-reviewer-b',
    tester:      'agent-tester',
    user:        'agent-user',
};

const AGENT_CARD_CLASS = {
    coordinator: 'agent-card-coordinator',
    coder:       'agent-card-coder',
    reviewer:    'agent-card-reviewer',
    reviewer_a:  'agent-card-reviewer',
    reviewer_b:  'agent-card-reviewer-b',
    tester:      'agent-card-tester',
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

        // Raw data for node detail panel
        this._rawMessages = [];         // all messages, capped at 200
        this._agentActivityLines = {};  // agentId -> string[] (last 10 lines)

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

        // Query bar
        document.getElementById('query-send').addEventListener('click', () => this._submitQuery());
        document.getElementById('query-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._submitQuery(); }
        });
        document.getElementById('query-stop').addEventListener('click', () => this._stopRun());

        const qi = document.getElementById('query-input');
        qi.addEventListener('input', () => {
            qi.style.height = 'auto';
            qi.style.height = Math.min(qi.scrollHeight, 160) + 'px';
        });

        // Question panel wiring
        document.getElementById('question-dismiss').addEventListener('click', () => {
            document.getElementById('question-panel').classList.add('hidden');
        });
        document.getElementById('question-send').addEventListener('click', () => this._sendQuestionReply());
        document.getElementById('question-reply').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._sendQuestionReply();
        });

        // Node detail panel
        document.getElementById('ndp-close').addEventListener('click', () => this._hideNodePanel());
        document.getElementById('ndp-chat-send').addEventListener('click', () => this._sendNdpMessage());
        document.getElementById('ndp-chat-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._sendNdpMessage();
        });
        // Result panel close
        document.getElementById('result-close').addEventListener('click', () => {
            document.getElementById('result-panel').classList.add('hidden');
        });

        // Result panel copy
        document.getElementById('result-copy').addEventListener('click', () => {
            const text = document.getElementById('result-body').innerText;
            navigator.clipboard.writeText(text).then(() => {
                const btn = document.getElementById('result-copy');
                btn.textContent = 'Copied!';
                btn.classList.add('copied');
                setTimeout(() => {
                    btn.textContent = 'Copy';
                    btn.classList.remove('copied');
                }, 1500);
            });
        });

        this._selectedModel = 'auto';
        this._loadModelOptions();
        this._setMessageEmptyState();
        this._setAgentEmptyState();

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
        const label = document.getElementById('connection-label');
        el.className = connected ? 'connected' : 'disconnected';
        el.title = connected ? 'Connected' : 'Disconnected';
        if (label) label.textContent = connected ? 'Live' : 'Offline';
    }

    // ── Event dispatch ────────────────────────────────────────

    _handleEvent(data) {
        switch (data.type) {
            case 'init':           this._handleInit(data);           break;
            case 'message':        this._handleMessage(data);        break;
            case 'agent_status':   this._handleAgentStatus(data);    break;
            case 'agent_stats':    this._handleAgentStats(data);     break;
            case 'agent_heartbeat': this._handleAgentHeartbeat(data); break;
            case 'complete':       this._handleComplete(data);       break;
            case 'stats':          this._handleStats(data);          break;
            case 'stopped':        this._handleStopped();            break;
            case 'run_complete':   this._handleRunComplete(data);    break;
            case 'agent_activity': this._handleAgentActivity(data);  break;
        }
    }

    _handleInit(data) {
        document.getElementById('result-panel').classList.add('hidden');
        this._rawMessages = [];
        this._agentActivityLines = {};
        // Reset panel without side-effects of _hideNodePanel (which clears selectedAgent)
        document.getElementById('node-detail-panel').classList.add('ndp-closed');
        this.selectedAgent = null;
        this.graph.selectedNode = null;
        this.agents = {};
        for (const agent of data.agents) {
            this.agents[agent.id] = {
                id: agent.id,
                role: agent.role,
                status: agent.status || 'idle',
                model: agent.model || '',
                msg_count: agent.msg_count || 0,
                complexity: agent.complexity || 0,
                result: agent.completed_result || '',
                last_heartbeat: agent.last_heartbeat || 0,
            };
        }

        this.graph.setTopology(data.agents, data.edges);
        this._hideLoader();

        // Render existing messages (prediction card first if we have one)
        this.messageLog.innerHTML = '';
        if (this._pendingPrediction) {
            this._addPredictionCard(this._pendingPrediction);
            this._pendingPrediction = null;
        }
        if ((data.messages || []).length === 0 && !this.messageLog.children.length) {
            this._setMessageEmptyState();
        }
        for (const msg of data.messages) {
            this._addMessageEntry(msg);
        }

        // Render agent cards
        this._rebuildAgentCards();
        this._updateStatusIndicator();

        if (data.stats) this._handleStats(data.stats);

        // Determine UI state based on run state
        if (data.run_active === true) {
            // Mid-run reconnect
            this._setRunActive(true);
        } else if (data.completed === true) {
            // Reconnect after a finished run: show result panel with first completed agent's result
            this._setRunActive(false);
            const completedAgent = data.agents.find(a =>
                a.status === 'completed' && a.completed_result && !a.completed_result.startsWith('Consensus:')
            );
            if (completedAgent) {
                document.getElementById('result-agent').textContent = completedAgent.role;
                document.getElementById('result-elapsed').textContent = '';
                document.getElementById('result-body').innerHTML = this._renderResult(completedAgent.completed_result);
                document.getElementById('result-panel').classList.remove('hidden');
                const bodyEl = document.getElementById('result-body');
                const wrap = document.getElementById('result-body-wrap');
                requestAnimationFrame(() => {
                    wrap.classList.toggle('no-overflow', bodyEl.scrollHeight <= bodyEl.clientHeight);
                });
            }
        } else {
            // Fresh open or no active run
            this._setRunActive(false);
        }
    }

    _handleMessage(data) {
        // Store raw message for node detail panel
        this._rawMessages.push(data);
        if (this._rawMessages.length > 200) this._rawMessages.shift();

        // Clear thinking for the sender (they just responded)
        this._clearThinking(data.from);
        this._addMessageEntry(data);
        this.graph.animateEdge(data.from, data.to);
        this.graph.updateAgentStatus(data.from, 'running', data.model || '');
        // Push last activity preview into sender node
        const preview = (data.content || '').replace(/\s+/g, ' ').trim().slice(0, 60);
        this.graph.updateAgentActivity(data.from, preview);
        this._updateStatusIndicator();

        if (data.to && this.agents[data.to] && this.agents[data.to].status !== 'completed') {
            // Show thinking indicator for the recipient
            this._showThinking(data.to);
        }

        // Refresh node detail panel if the involved agent is selected
        if (this.selectedAgent && (data.from === this.selectedAgent || data.to === this.selectedAgent)) {
            this._refreshNodePanel();
        }
    }

    _handleAgentStatus(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].status = data.status;
            if (data.model) this.agents[data.agent].model = data.model;
        }
        // Pass full model id — graph.js will shorten it for display
        this.graph.updateAgentStatus(data.agent, data.status, data.model || '');
        this._updateAgentCard(data.agent);
        this._updateStatusIndicator();
        if (this.selectedAgent === data.agent) this._refreshNodePanel();
        // Show the actual model being used in the stats bar (first agent to report one wins)
        if (data.model && this._selectedModel === 'auto' && !this._runModelShown) {
            this._runModelShown = true;
            document.getElementById('stat-model').textContent = this._shortModel(data.model);
        }
    }

    _handleAgentStats(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].msg_count = data.msg_count;
            if (data.status)     this.agents[data.agent].status     = data.status;
            if (data.model)      this.agents[data.agent].model      = data.model;
            if (data.complexity) this.agents[data.agent].complexity = data.complexity;
        }
        // Keep graph node model in sync (agent_stats fires for both sender and receiver)
        if (data.model) this.graph.updateAgentStatus(data.agent, data.status || '', data.model);
        this._updateAgentCard(data.agent);
        if (this.selectedAgent === data.agent) this._refreshNodePanel();
    }

    _handleAgentHeartbeat(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].last_heartbeat = data.ts || 0;
            if (data.status && this.agents[data.agent].status !== 'completed' && this.agents[data.agent].status !== 'error') {
                this.agents[data.agent].status = data.status;
            }
        }
        this._updateAgentCard(data.agent);
        if (this.selectedAgent === data.agent) this._refreshNodePanel();
    }

    _handleComplete(data) {
        this._clearThinking(data.agent);
        if (this.agents[data.agent]) {
            this.agents[data.agent].status = 'completed';
            this.agents[data.agent].result = data.result;
        }
        this.graph.updateAgentStatus(data.agent, 'completed');
        this._updateAgentCard(data.agent);
        this._updateStatusIndicator();
        if (this.selectedAgent === data.agent) this._refreshNodePanel();

        // Show result panel for the first real completion (not a consensus relay)
        const isConsensus = data.is_consensus === true;
        const panel = document.getElementById('result-panel');
        if (!isConsensus && data.result && data.result.trim() && panel.classList.contains('hidden')) {
            const agent = this.agents[data.agent];
            document.getElementById('result-agent').textContent =
                (agent ? agent.role : data.agent);

            // Show elapsed time from last stats update
            const elapsedEl = document.getElementById('result-elapsed');
            if (this._lastElapsed !== undefined) {
                elapsedEl.textContent = this._lastElapsed.toFixed(1) + 's';
            } else {
                elapsedEl.textContent = '';
            }

            // Render markdown-like formatting
            const bodyEl = document.getElementById('result-body');
            bodyEl.innerHTML = this._renderResult(data.result || '');

            panel.classList.remove('hidden');

            // Check overflow to control the fade gradient
            const wrap = document.getElementById('result-body-wrap');
            // Use rAF so the DOM has painted and scrollHeight is accurate
            requestAnimationFrame(() => {
                const overflows = bodyEl.scrollHeight > bodyEl.clientHeight;
                wrap.classList.toggle('no-overflow', !overflows);
                // Also hide fade when user scrolls to the bottom
                bodyEl.addEventListener('scroll', () => {
                    const atBottom = bodyEl.scrollHeight - bodyEl.scrollTop <= bodyEl.clientHeight + 2;
                    wrap.classList.toggle('no-overflow', atBottom);
                }, { passive: true });
            });
        }
    }

    _handleStats(data) {
        document.getElementById('stat-messages').textContent = data.message_count;
        document.getElementById('stat-budget').textContent   = data.budget_remaining;
        document.getElementById('stat-elapsed').textContent  = data.elapsed.toFixed(1) + 's';
        this._lastElapsed = data.elapsed;
    }

    _updateStatusIndicator() {
        const el       = document.getElementById('stat-status');
        const statuses = Object.values(this.agents).map(a => a.status);

        if (statuses.length === 0) return;

        if (statuses.every(s => s === 'completed')) {
            el.textContent = 'Done';
            el.className   = 'stat-value status-done';
            this._setRunActive(false);
        } else if (statuses.some(s => s === 'running')) {
            el.textContent = 'Running';
            el.className   = 'stat-value status-running';
        } else {
            el.textContent = 'Waiting';
            el.className   = 'stat-value status-waiting';
        }
    }

    _heartbeatAge(agent) {
        if (!agent || !agent.last_heartbeat) return null;
        return Math.max(0, Date.now() / 1000 - agent.last_heartbeat);
    }

    _heartbeatState(agent) {
        const age = this._heartbeatAge(agent);
        if (age === null) return { label: 'no hb', age: null, live: false };
        return {
            label: age <= 6 ? 'live' : 'stale',
            age,
            live: age <= 6,
        };
    }

    _formatRelativeAge(age) {
        if (age === null || age === undefined) return '—';
        if (age < 1) return '<1s';
        if (age < 60) return `${age.toFixed(1)}s`;
        return `${Math.round(age / 60)}m`;
    }

    _topologyLabel() {
        const ids = Object.keys(this.agents);
        return ids.includes('reviewer_a') ? 'Dual Review' : (ids.length ? 'Triad' : 'Uninitialized');
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

    _setMessageEmptyState(text = 'Runs, agent questions, and handoffs will appear here.') {
        this.messageLog.innerHTML = `
            <div class="empty-state empty-state-messages">
                <div class="empty-state-kicker">Message Feed</div>
                <div class="empty-state-title">No activity yet</div>
                <div class="empty-state-copy">${this._escapeHtml(text)}</div>
            </div>
        `;
    }

    _setAgentEmptyState(text = 'Agents will appear after topology prediction and run startup.') {
        this.agentCards.innerHTML = `
            <div class="empty-state empty-state-agents">
                <div class="empty-state-kicker">Agents</div>
                <div class="empty-state-title">Graph is idle</div>
                <div class="empty-state-copy">${this._escapeHtml(text)}</div>
            </div>
        `;
    }

    // ── Message log ───────────────────────────────────────────

    _addMessageEntry(msg) {
        const empty = this.messageLog.querySelector('.empty-state');
        if (empty) empty.remove();
        const entry = document.createElement('div');
        entry.className = 'msg-entry';

        const fromClass = AGENT_CSS_CLASS[msg.from] || 'agent-user';
        const toClass   = AGENT_CSS_CLASS[msg.to]   || 'agent-user';
        const elapsed   = msg.elapsed !== undefined ? msg.elapsed.toFixed(1) + 's' : '';
        const msgType   = msg.msg_type || msg.type || 'system';
        const badgeCls  = MSG_TYPE_BADGE_CLASS[msgType] || 'msg-type-system';
        const depth     = msg.depth !== undefined ? msg.depth : '';
        const preview   = (msg.content || '').split('\n')[0].slice(0, 120);
        const modelLabel = this._shortModel(msg.model);

        // Build context section HTML
        let contextHtml = '';
        const slices = Array.isArray(msg.context_slice) ? msg.context_slice :
                       (msg.context_slice ? [String(msg.context_slice)] : []);
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
                ${modelLabel ? `<span class="msg-model-pill">${this._escapeHtml(modelLabel)}</span>` : ''}
                <span class="msg-arrow">&rarr;</span>
                <span class="${toClass}">${msg.to}</span>
                <span class="msg-type-badge ${badgeCls}">${msgType}</span>
                ${depth !== '' ? `<span class="msg-depth-badge">${depth}</span>` : ''}
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
        if (Object.keys(this.agents).length === 0) {
            this._setAgentEmptyState();
            return;
        }
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

        const complexity = agent.complexity || 0;
        const complexityHtml = complexity > 0
            ? `<span class="complexity-badge complexity-${this._complexityLevel(complexity)}" title="Complexity score: ${complexity}">${complexity}</span>`
            : '';
        const heartbeat = this._heartbeatState(agent);
        const heartbeatHtml = heartbeat.age !== null
            ? `<span class="agent-heartbeat ${heartbeat.live ? 'live' : 'stale'}" title="Last heartbeat ${heartbeat.age.toFixed(1)}s ago">hb ${heartbeat.age.toFixed(1)}s</span>`
            : `<span class="agent-heartbeat stale" title="No heartbeat received">hb —</span>`;
        const recentActivity = (this._agentActivityLines[agent.id] || []).slice(-1)[0]
            || this._rawMessages.filter(m => m.from === agent.id || m.to === agent.id).slice(-1)[0]?.content
            || '';
        const activityHtml = recentActivity
            ? `<div class="agent-card-activity">${this._escapeHtml(recentActivity.replace(/\s+/g, ' ').trim().slice(0, 120))}</div>`
            : `<div class="agent-card-activity empty">No recent activity yet</div>`;

        card.innerHTML = `
            <div class="agent-card-header">
                <span class="agent-card-name ${agentClass}">${agent.id}</span>
                <span class="agent-card-role">${agent.role}</span>
                <span class="agent-status-badge ${statusCls}">${agent.status}</span>
            </div>
            <div class="agent-card-meta">
                <span>model: ${this._shortModel(agent.model) || '—'}</span>
                <span>msgs: ${agent.msg_count}</span>
                ${heartbeatHtml}
                ${complexityHtml}
            </div>
            ${activityHtml}
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

        // Show node detail panel (replaces old chat panel for graph clicks)
        if (agentId) {
            this._showNodePanel(agentId);
        } else {
            this._hideNodePanel();
        }
    }

    // ── Node detail panel ─────────────────────────────────

    _showNodePanel(agentId) {
        document.getElementById('node-detail-panel').classList.remove('ndp-closed');
        this._refreshNodePanel();
        const inp = document.getElementById('ndp-chat-input');
        if (inp) inp.focus();
    }

    _hideNodePanel() {
        document.getElementById('node-detail-panel').classList.add('ndp-closed');
        if (this.selectedAgent) {
            this.selectedAgent = null;
            this.graph.selectedNode = null;
            document.querySelectorAll('.agent-card').forEach(c => c.classList.remove('selected'));
        }
    }

    _refreshNodePanel() {
        const agentId = this.selectedAgent;
        if (!agentId) return;
        const agent = this.agents[agentId] || {};

        // ── Accent color ──────────────────────────────────
        const color = {
            coordinator: '#6e40c9', coder: '#0550ae',
            reviewer: '#7d4e00', reviewer_a: '#7d4e00', reviewer_b: '#953800',
            tester: '#1a7f37',
        }[agentId] || '#9198a1';
        document.getElementById('ndp-accent').style.background = color;

        // ── Role + status badge ───────────────────────────
        const roleEl = document.getElementById('ndp-role');
        roleEl.textContent = agent.role || agentId;
        roleEl.style.color = color;

        const status = agent.status || 'idle';
        const badgeEl = document.getElementById('ndp-status-badge');
        badgeEl.textContent = status;
        badgeEl.className = `ndp-badge-${status}`;

        // ── Meta row: model · msgs · complexity ───────────
        const meta = document.getElementById('ndp-meta');
        const summary = document.getElementById('ndp-summary');
        const overview = document.getElementById('ndp-overview-grid');
        const topologyMap = document.getElementById('ndp-topology-map');
        const commGrid = document.getElementById('ndp-comm-grid');
        const modelShort = agent.model ? this._shortModel(agent.model) : '—';
        const compScore = this._lastPrediction?.agent_complexity?.[agentId];
        const heartbeat = this._heartbeatState(agent);
        const relevantMessages = this._rawMessages.filter(m => m.from === agentId || m.to === agentId);
        const outgoing = relevantMessages.filter(m => m.from === agentId);
        const incoming = relevantMessages.filter(m => m.to === agentId);
        const peers = [...new Set(relevantMessages.map(m => m.from === agentId ? m.to : m.from).filter(Boolean))];
        const neighbors = (this.graph.edges || [])
            .flatMap(e => e.source === agentId ? [e.target] : (e.target === agentId ? [e.source] : []));
        const uniqueNeighbors = [...new Set(neighbors)];
        const activePeers = peers.filter(p => uniqueNeighbors.includes(p));
        const edgeList = (this.graph.edges || [])
            .filter(e => e.source === agentId || e.target === agentId)
            .map(e => `${e.source} ↔ ${e.target}`);
        const compHtml = compScore !== undefined
            ? `<span class="ndp-meta-pill">complexity&nbsp;${compScore}</span>`
            : '';
        meta.innerHTML = `
            <span class="ndp-meta-pill">${this._escapeHtml(modelShort)}</span>
            <span class="ndp-meta-pill accent" style="color:var(--text-muted)">
                ${agent.msg_count || 0} msg${(agent.msg_count || 0) !== 1 ? 's' : ''}
            </span>
            <span class="ndp-meta-pill" style="color:${heartbeat.live ? 'var(--green)' : 'var(--red)'}">
                ${heartbeat.age !== null ? `heartbeat ${heartbeat.age.toFixed(1)}s` : 'heartbeat —'}
            </span>
            ${compHtml}
        `;

        summary.textContent = heartbeat.live
            ? `${agent.role || agentId} is active in the graph and ready to exchange work.`
            : `${agent.role || agentId} has not emitted a recent heartbeat. Inspect activity and message flow before trusting the state.`;

        overview.innerHTML = `
            <div class="ndp-overview-card">
                <span class="ndp-overview-label">State</span>
                <span class="ndp-overview-value">${this._escapeHtml(status)}</span>
                <span class="ndp-overview-note">${heartbeat.label}</span>
            </div>
            <div class="ndp-overview-card">
                <span class="ndp-overview-label">Messages</span>
                <span class="ndp-overview-value">${relevantMessages.length}</span>
                <span class="ndp-overview-note">${outgoing.length} out · ${incoming.length} in</span>
            </div>
            <div class="ndp-overview-card">
                <span class="ndp-overview-label">Heartbeat</span>
                <span class="ndp-overview-value">${this._formatRelativeAge(heartbeat.age)}</span>
                <span class="ndp-overview-note">${heartbeat.live ? 'recent' : 'stale'}</span>
            </div>
            <div class="ndp-overview-card">
                <span class="ndp-overview-label">Peers</span>
                <span class="ndp-overview-value">${peers.length}</span>
                <span class="ndp-overview-note">${peers.slice(0, 3).join(', ') || 'none yet'}</span>
            </div>
        `;

        topologyMap.innerHTML = `
            <div class="ndp-topology-header">
                <span class="ndp-topology-badge">${this._topologyLabel()}</span>
                <span class="ndp-topology-node">${this._escapeHtml(agentId)}</span>
            </div>
            <div class="ndp-topology-copy">
                ${this._escapeHtml(agent.role || agentId)} sits on ${uniqueNeighbors.length} graph edge${uniqueNeighbors.length === 1 ? '' : 's'} and can communicate directly with its neighbors.
            </div>
            <div class="ndp-neighbor-row">
                ${uniqueNeighbors.length
                    ? uniqueNeighbors.map(n => `<span class="ndp-neighbor-chip${activePeers.includes(n) ? ' active' : ''}">${this._escapeHtml(n)}</span>`).join('')
                    : '<span class="ndp-neighbor-empty">No connected neighbors</span>'}
            </div>
            <div class="ndp-edge-list">
                ${edgeList.length
                    ? edgeList.map(edge => `<div class="ndp-edge-item">${this._escapeHtml(edge)}</div>`).join('')
                    : '<div class="ndp-edge-item empty">No active edges</div>'}
            </div>
        `;

        commGrid.innerHTML = `
            <div class="ndp-comm-card">
                <span class="ndp-comm-kicker">Outgoing</span>
                <span class="ndp-comm-value">${outgoing.length}</span>
                <span class="ndp-comm-note">messages sent to collaborators</span>
            </div>
            <div class="ndp-comm-card">
                <span class="ndp-comm-kicker">Incoming</span>
                <span class="ndp-comm-value">${incoming.length}</span>
                <span class="ndp-comm-note">messages received from the graph</span>
            </div>
            <div class="ndp-comm-card ndp-comm-wide">
                <span class="ndp-comm-kicker">Connected peers</span>
                <span class="ndp-comm-note">${peers.length ? peers.map(p => this._escapeHtml(p)).join(' · ') : 'No graph communication yet'}</span>
            </div>
        `;

        // ── Activity section ──────────────────────────────
        const lines = this._agentActivityLines[agentId] || [];
        const actSection = document.getElementById('ndp-activity-section');
        const actLog = document.getElementById('ndp-activity-log');
        if (lines.length > 0 && status !== 'completed') {
            actSection.classList.remove('hidden');
            actLog.innerHTML = lines.map(a => `
                <div class="ndp-activity-line">
                    <span class="activity-icon">${this._activityIcon(a)}</span>
                    <span>${this._escapeHtml(a)}</span>
                </div>
            `).join('');
        } else {
            actSection.classList.add('hidden');
        }

        // ── Messages section ──────────────────────────────
        const msgList = document.getElementById('ndp-message-list');
        const relevant = relevantMessages.slice(-8);

        if (relevant.length === 0) {
            msgList.innerHTML = `<div style="font-size:11px;color:var(--text-muted);font-style:italic">No messages yet.</div>`;
        } else {
            const BADGE_STYLES = {
                task:     'background:#dbeafe;color:#0969da',
                response: 'background:#dcfce7;color:#1a7f37',
                feedback: 'background:#fef3c7;color:#9a6700',
                complete: 'background:#f3e8ff;color:#8250df',
            };
            msgList.innerHTML = relevant.map(m => {
                const fromCls = AGENT_CSS_CLASS[m.from] || 'agent-user';
                const toCls   = AGENT_CSS_CLASS[m.to]   || 'agent-user';
                const mtype   = m.msg_type || m.type || 'system';
                const bstyle  = BADGE_STYLES[mtype] || 'background:var(--bg-overlay);color:var(--text-muted)';
                const elapsed = m.elapsed !== undefined ? m.elapsed.toFixed(1) + 's' : '';
                const preview = (m.content || '').replace(/\s+/g, ' ').trim().slice(0, 100);
                const contextCount = Array.isArray(m.context_slice) ? m.context_slice.length : 0;
                const chainId = m.chain_id ? String(m.chain_id).slice(0, 8) : '';
                return `<div class="ndp-msg-entry">
                    <div class="ndp-msg-header">
                        <span class="ndp-msg-from ${fromCls}">${this._escapeHtml(m.from)}</span>
                        <span class="ndp-msg-arrow">→</span>
                        <span class="ndp-msg-to ${toCls}">${this._escapeHtml(m.to)}</span>
                        <span class="ndp-msg-type" style="${bstyle}">${mtype}</span>
                        ${elapsed ? `<span class="ndp-msg-elapsed">${elapsed}</span>` : ''}
                    </div>
                    <div class="ndp-msg-meta">
                        ${chainId ? `<span>chain ${this._escapeHtml(chainId)}</span>` : ''}
                        ${contextCount ? `<span>${contextCount} ctx</span>` : '<span>0 ctx</span>'}
                    </div>
                    <div class="ndp-msg-preview">${this._escapeHtml(preview)}${preview.length >= 100 ? '…' : ''}</div>
                    <details class="ndp-msg-details">
                        <summary>Full payload</summary>
                        <pre class="ndp-msg-full">${this._escapeHtml(m.content || '')}</pre>
                    </details>
                </div>`;
            }).join('');
        }

        // ── Result section ────────────────────────────────
        const resultSection = document.getElementById('ndp-result-section');
        const resultBody    = document.getElementById('ndp-result-body');
        if (status === 'completed' && agent.result && !agent.result.startsWith('Consensus:') && agent.result !== '[shutdown]') {
            resultSection.classList.remove('hidden');
            resultBody.textContent = agent.result;
        } else {
            resultSection.classList.add('hidden');
        }
    }

    async _sendNdpMessage() {
        if (!this.selectedAgent) return;
        const input = document.getElementById('ndp-chat-input');
        const text  = input.value.trim();
        if (!text) return;

        input.value = '';
        // Store and display it
        const msgData = { from: 'user', to: this.selectedAgent, content: text,
                          elapsed: 0, model: '', depth: 0, msg_type: 'task', context_slice: [] };
        this._rawMessages.push(msgData);
        this._addMessageEntry(msgData);
        this._refreshNodePanel();

        try {
            await fetch('/api/inject', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ to: this.selectedAgent, message: text }),
            });
        } catch (e) { /* ignore */ }
        document.getElementById('ndp-chat-input').focus();
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

    // ── Query bar ─────────────────────────────────────────────

    async _loadModelOptions() {
        const res = await fetch('/api/models');
        const data = await res.json();
        this._modelLabels = {};
        const picker = document.getElementById('model-picker');
        picker.innerHTML = '';
        for (const m of data.models || []) {
            this._modelLabels[m.id] = m.label;
            const pill = document.createElement('button');
            pill.className = 'model-pill' + (m.local ? ' local' : '') + (m.id === this._selectedModel ? ' selected' : '');
            pill.textContent = m.label;
            pill.title = m.id;
            pill.addEventListener('click', () => {
                this._selectedModel = m.id;
                picker.querySelectorAll('.model-pill').forEach(p => p.classList.remove('selected'));
                pill.classList.add('selected');
                document.getElementById('stat-model').textContent = m.label;
            });
            picker.appendChild(pill);
        }
        // Set initial stat label
        document.getElementById('stat-model').textContent =
            this._modelLabels[this._selectedModel] || 'Auto';
    }

    _setRunActive(active) {
        const send = document.getElementById('query-send');
        const stop = document.getElementById('query-stop');
        const input = document.getElementById('query-input');
        send.classList.toggle('hidden', active);
        stop.classList.toggle('hidden', !active);
        input.disabled = active;
        if (!active) this._hideLoader();
    }

    _showLoader(text = 'Starting agents…') {
        const loader = document.getElementById('graph-loader');
        document.getElementById('loader-text').textContent = text;
        loader.classList.remove('hidden');
    }

    _hideLoader() {
        document.getElementById('graph-loader').classList.add('hidden');
    }

    async _submitQuery() {
        const input = document.getElementById('query-input');
        const query = input.value.trim();
        if (!query) return;

        this._setRunActive(true);

        // Clear previous run state
        this._clearThinking();
        this._thinkingAgent = null;
        this._pendingPrediction = null;
        this._runModelShown = false;
        this._rawMessages = [];
        this._agentActivityLines = {};
        this.messageLog.innerHTML = '';
        this._setMessageEmptyState('Analyzing the task and waiting for the runtime to emit events.');
        this.agentCards.innerHTML = '';
        this._setAgentEmptyState('Waiting for the runtime to construct the predicted topology.');
        this.agents = {};
        this.graph.setTopology([], []);
        document.getElementById('node-detail-panel').classList.add('ndp-closed');
        this.selectedAgent = null;
        this.graph.selectedNode = null;
        document.getElementById('result-panel').classList.add('hidden');
        document.getElementById('question-panel').classList.add('hidden');
        this._handleStats({ message_count: 0, budget_remaining: 200, elapsed: 0 });
        const statusEl = document.getElementById('stat-status');

        // Step 1: Predict topology (LLM call)
        statusEl.textContent = 'Predicting…';
        statusEl.className = 'stat-value status-running';
        this._showLoader('Choosing topology…');

        const pred = await fetch(`/api/predict-topology?q=${encodeURIComponent(query)}&model=${encodeURIComponent(this._selectedModel)}`);
        const predData = await pred.json();
        const topology = predData.topology || 'triangle';
        this._showLoader(`Using ${predData.label || topology}…`);

        // Store prediction — will be shown when _handleInit fires
        this._pendingPrediction = predData;
        this._lastPrediction = predData;   // persists for node detail panel
        this._lastQuery = query;


        // Step 2: Start the run
        statusEl.textContent = 'Starting…';
        const res = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                query,
                topology,
                model: this._selectedModel,
                complexity: predData.complexity || 50,
                agent_complexity: predData.agent_complexity || {},
            }),
        });
        const data = await res.json();
        if (!data.ok) {
            this._setRunActive(false);
            this._hideLoader();
            document.getElementById('result-agent').textContent = 'Error';
            document.getElementById('result-elapsed').textContent = '';
            document.getElementById('result-body').textContent = data.error || 'Failed to start run.';
            document.getElementById('result-panel').classList.remove('hidden');
        }
    }

    // ── Stop run ──────────────────────────────────────────────

    async _stopRun() {
        const btn = document.getElementById('query-stop');
        btn.disabled = true;
        btn.textContent = 'Stopping…';
        await fetch('/api/stop', { method: 'POST' });
    }

    _handleStopped() {
        this._clearThinking();
        this._setRunActive(false);
        const el = document.getElementById('stat-status');
        el.textContent = 'Stopped';
        el.className = 'stat-value status-error';
        const btn = document.getElementById('query-stop');
        btn.disabled = false;
        btn.textContent = 'Stop';
    }

    _handleRunComplete(data) {
        this._clearThinking();
        this._setRunActive(false);

        // Mark any agents still in running/waiting as completed
        for (const agent of Object.values(this.agents)) {
            if (agent.status !== 'completed') {
                agent.status = 'completed';
                this.graph.updateAgentStatus(agent.id, 'completed');
                this._updateAgentCard(agent.id);
            }
        }

        // Force status to Done
        const statusEl = document.getElementById('stat-status');
        statusEl.textContent = 'Done';
        statusEl.className = 'stat-value status-done';

        const result = data.result || '';
        const elapsed = data.elapsed !== undefined ? data.elapsed.toFixed(1) + 's' : '';
        const sessionTurn = data.session_turn || 0;

        // Render final result card in the message log
        const diff = data.diff || '';
        const followUpHint = sessionTurn > 0
            ? `<span class="followup-hint">↩ type a follow-up to continue this session</span>`
            : '';
        const el = document.createElement('div');
        el.className = 'final-result-card';
        el.innerHTML = `
            <div class="final-result-header">
                <span class="final-result-title">✓ Run Complete</span>
                ${elapsed ? `<span class="final-result-elapsed">${elapsed}</span>` : ''}
                ${followUpHint}
                <button class="final-result-copy">Copy result</button>
            </div>
            <div class="final-result-body">${this._renderResult(result)}</div>
            ${diff ? `
            <div class="diff-section">
                <div class="diff-section-header">
                    <span>Files Changed</span>
                    <button class="diff-toggle">Show diff ▾</button>
                </div>
                <div class="diff-stat">${this._renderDiffStat(diff)}</div>
                <pre class="diff-body hidden">${this._renderDiff(diff)}</pre>
            </div>` : ''}
        `;
        el.querySelector('.final-result-copy').addEventListener('click', () => {
            navigator.clipboard.writeText(result).then(() => {
                const btn = el.querySelector('.final-result-copy');
                btn.textContent = 'Copied!';
                setTimeout(() => { btn.textContent = 'Copy result'; }, 1500);
            });
        });
        if (diff) {
            el.querySelector('.diff-toggle').addEventListener('click', (e) => {
                const pre = el.querySelector('.diff-body');
                const btn = e.target;
                if (pre.classList.contains('hidden')) {
                    pre.classList.remove('hidden');
                    btn.textContent = 'Hide diff ▴';
                } else {
                    pre.classList.add('hidden');
                    btn.textContent = 'Show diff ▾';
                }
            });
        }
        this.messageLog.appendChild(el);
        this.messageLog.scrollTop = this.messageLog.scrollHeight;

        // Also update the result panel
        document.getElementById('result-agent').textContent = 'Final Result';
        document.getElementById('result-elapsed').textContent = elapsed;
        document.getElementById('result-body').innerHTML = this._renderResult(result);
        document.getElementById('result-panel').classList.remove('hidden');
    }

    _renderDiffStat(diff) {
        const files = [];
        let added = 0, removed = 0;
        for (const line of diff.split('\n')) {
            if (line.startsWith('diff --git ')) {
                const m = line.match(/ b\/(.+)$/);
                if (m) files.push(m[1]);
                added = 0; removed = 0;
            } else if (line.startsWith('+') && !line.startsWith('+++')) {
                added++;
            } else if (line.startsWith('-') && !line.startsWith('---')) {
                removed++;
            }
        }
        return files.map(f => `<span class="diff-file">${this._escapeHtml(f)}</span>`).join('');
    }

    _renderDiff(diff) {
        return diff.split('\n').map(line => {
            const esc = this._escapeHtml(line);
            if (line.startsWith('diff --git') || line.startsWith('index '))
                return `<span class="diff-meta">${esc}</span>`;
            if (line.startsWith('--- ') || line.startsWith('+++ '))
                return `<span class="diff-file-hdr">${esc}</span>`;
            if (line.startsWith('@@'))
                return `<span class="diff-hunk">${esc}</span>`;
            if (line.startsWith('+'))
                return `<span class="diff-add">${esc}</span>`;
            if (line.startsWith('-'))
                return `<span class="diff-del">${esc}</span>`;
            return `<span class="diff-ctx">${esc}</span>`;
        }).join('\n');
    }

    // ── Utilities ─────────────────────────────────────────────

    _renderResult(text) {
        // Basic markdown-like rendering: bold, code blocks, bullets, paragraphs
        let html = this._escapeHtml(text);
        // Code blocks (``` ... ```)
        html = html.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) =>
            `<pre style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:8px 10px;font-size:11px;overflow-x:auto;margin:6px 0">${code}</pre>`
        );
        // Inline code
        html = html.replace(/`([^`]+)`/g, '<code style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:4px;padding:1px 5px;font-size:11px">$1</code>');
        // Bold
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        // Bullet lists
        html = html.replace(/^[ \t]*[-*] (.+)$/gm, '<li>$1</li>');
        html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
        // Paragraphs (double newline)
        html = html.replace(/\n{2,}/g, '</p><p>');
        html = `<p>${html}</p>`;
        html = html.replace(/<p>\s*<\/p>/g, '');
        return html;
    }

    _isQuestion(text) {
        if (!text) return false;
        const q = text.trim();
        // Multiple question marks, or ends with a question, or starts with clarifying phrases
        const questionCount = (q.match(/\?/g) || []).length;
        if (questionCount >= 2) return true;
        if (/\?\s*$/.test(q) && questionCount >= 1 && q.length < 600) return true;
        return /^(could you|can you|what do you|please clarify|i need (to understand|more info|clarification)|to (better|properly) (help|assist)|before i (can|proceed))/i.test(q);
    }

    _showQuestion(agentId, text) {
        this._questionAgent = agentId;
        const agent = this.agents[agentId] || {};
        document.getElementById('question-from').textContent =
            `${agent.role || agentId} is asking:`;
        document.getElementById('question-body').textContent = text;
        document.getElementById('question-reply').value = '';
        document.getElementById('question-panel').classList.remove('hidden');
        setTimeout(() => document.getElementById('question-reply').focus(), 50);
    }

    async _sendQuestionReply() {
        const input = document.getElementById('question-reply');
        const text = input.value.trim();
        if (!text || !this._questionAgent) return;

        document.getElementById('question-panel').classList.add('hidden');
        input.value = '';

        // Inject the reply directly to the agent that asked
        await fetch('/api/inject', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to: this._questionAgent, message: text }),
        });
        this._addMessageEntry({
            from: 'user', to: this._questionAgent,
            content: text, elapsed: 0, model: '',
            depth: 0, msg_type: 'task', context_slice: [],
        });
        this._showThinking(this._questionAgent);
        this._questionAgent = null;
    }

    _addPredictionCard(pred) {
        const complexity = pred.complexity ?? 50;
        const barColor = complexity >= 75 ? '#cf222e' : complexity >= 50 ? '#9a6700' : '#1a7f37';
        const optionsHtml = (pred.options || []).map(o => `
            <div class="pred-option${o.chosen ? ' chosen' : ''}">
                <span class="pred-option-label">${this._escapeHtml(o.label)}</span>
                <span class="pred-option-desc">${this._escapeHtml(o.description)}</span>
                ${o.chosen ? '<span class="pred-chosen-badge">✓ chosen</span>' : ''}
            </div>`).join('');

        const agentModels     = pred.agent_models     || {};
        const agentComplexity = pred.agent_complexity || {};
        // Show role → model, with complexity score as a small annotation.
        // These are the exact same values _build_agent_model_map will use at run-start.
        const agentRows = Object.entries(agentModels).map(([role, model]) => {
            const short   = this._shortModel(model);
            const isLocal = model.includes('qwen') || model.includes('llama');
            const score   = agentComplexity[role];
            return `<div class="pred-agent-model">
                <span class="pred-agent-role">${role}</span>
                ${score !== undefined ? `<span class="pred-agent-score">${score}</span>` : ''}
                <span class="pred-agent-model-name${isLocal ? ' local' : ''}">${this._escapeHtml(short)}</span>
            </div>`;
        }).join('');

        const el = document.createElement('div');
        el.className = 'prediction-card';
        el.innerHTML = `
            <div class="pred-header">
                <span class="pred-title">Task Analysis</span>
                <span class="pred-complexity-label">Complexity</span>
                <span class="pred-complexity-value" style="color:${barColor}">${complexity}</span>
            </div>
            <div class="pred-bar-wrap">
                <div class="pred-bar" style="width:${complexity}%;background:${barColor}"></div>
            </div>
            <div class="pred-reason">${this._escapeHtml(pred.reason || '')}</div>
            <div class="pred-options">${optionsHtml}</div>
            ${agentRows ? `<div class="pred-agent-models">${agentRows}</div>` : ''}
        `;
        this.messageLog.appendChild(el);
        this.messageLog.scrollTop = this.messageLog.scrollHeight;
    }

    _showThinking(agentId) {
        this._clearThinking();
        const agentClass = AGENT_CSS_CLASS[agentId] || 'agent-user';
        const el = document.createElement('div');
        el.id = 'thinking-indicator';
        el.className = 'thinking-indicator';
        el.innerHTML = `
            <div class="thinking-header">
                <span class="${agentClass}">${agentId}</span>
                <span class="thinking-dots"><span></span><span></span><span></span></span>
            </div>
            <div class="activity-log" id="activity-log"></div>
        `;
        this.messageLog.appendChild(el);
        this.messageLog.scrollTop = this.messageLog.scrollHeight;
        this.graph.setNodeThinking(agentId, true);
        this._thinkingAgent = agentId;
    }

    _handleAgentActivity(data) {
        const { agent, activity } = data;

        // Store per-agent activity lines for node detail panel
        if (!this._agentActivityLines[agent]) this._agentActivityLines[agent] = [];
        this._agentActivityLines[agent].push(activity);
        if (this._agentActivityLines[agent].length > 10) this._agentActivityLines[agent].shift();

        // If this agent isn't currently showing the thinking indicator, show it
        if (this._thinkingAgent !== agent) {
            this._showThinking(agent);
        }
        const log = document.getElementById('activity-log');
        if (log) {
            const line = document.createElement('div');
            line.className = 'activity-line';
            const icon = this._activityIcon(activity);
            line.innerHTML = `<span class="activity-icon">${icon}</span><span class="activity-text">${this._escapeHtml(activity)}</span>`;
            log.appendChild(line);
            // Keep only last 12 lines
            while (log.children.length > 12) log.removeChild(log.firstChild);
            // Scroll the whole message log to keep the indicator in view
            this.messageLog.scrollTop = this.messageLog.scrollHeight;
        }

        if (typeof activity === 'string' && activity.startsWith('⏳ Waiting for user')) {
            this._addMessageEntry({
                from: agent,
                to: 'user',
                content: activity,
                elapsed: 0,
                model: '',
                depth: 0,
                msg_type: 'question',
                context_slice: [],
            });
            this._showQuestion(agent, activity);
        } else if (!activity && this._questionAgent === agent) {
            document.getElementById('question-panel').classList.add('hidden');
            this._questionAgent = null;
        }

        // Update node detail panel if this agent is selected
        if (this.selectedAgent === agent) this._refreshNodePanel();
    }

    _activityIcon(activity) {
        if (activity.startsWith('$'))            return '▶';
        if (activity.startsWith('Writing'))      return '✎';
        if (activity.startsWith('Reading'))      return '◎';
        if (activity.startsWith('Listing'))      return '≡';
        if (activity.startsWith('Sending'))      return '→';
        if (activity.startsWith('Calling'))      return '◈';
        if (activity.startsWith('Complet'))      return '✓';
        return '⚙';
    }

    _clearThinking(agentId) {
        const el = document.getElementById('thinking-indicator');
        if (el) el.remove();
        if (this._thinkingAgent) {
            this.graph.setNodeThinking(this._thinkingAgent, false);
        }
        if (!agentId || agentId === this._thinkingAgent) {
            this._thinkingAgent = null;
        }
    }

    _complexityLevel(score) {
        if (score >= 79) return 'high';
        if (score >= 56) return 'mid';
        return 'low';
    }

    _shortModel(modelId) {
        if (!modelId) return '';
        // claude-sonnet-4-5-20251001 → sonnet-4-5
        // claude-opus-4-20250514     → opus-4
        // gpt-5.4, gpt-4o, qwen3.5:9b pass through unchanged
        const m = modelId.match(/^claude-([a-z]+-[\d]+(?:-[\d]+)?)/i);
        if (m) return m[1].toLowerCase();
        return modelId;
    }

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }
}

// Initialize on load
window.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new Dashboard();

    // Mobile: graph toggle button
    const graphToggleBtn = document.getElementById('graph-toggle-btn');
    const graphPanel = document.getElementById('graph-panel');
    const graphToggleLabel = document.getElementById('graph-toggle-label');
    if (graphToggleBtn && graphPanel) {
        graphToggleBtn.addEventListener('click', () => {
            const visible = graphPanel.classList.toggle('mobile-visible');
            graphToggleLabel.textContent = visible ? 'Hide Agent Graph' : 'Show Agent Graph';
            if (visible && window.dashboard && window.dashboard.graph) {
                // Wait for CSS height transition to finish, then resize canvas
                setTimeout(() => window.dashboard.graph._resize(), 300);
            }
        });
    }
});
