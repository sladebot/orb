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
        this.changesLog  = document.getElementById('changes-log');
        this.agentCards  = document.getElementById('agent-cards');
        this.ws          = null;
        this.agents      = {};       // id -> agent data object
        this.selectedAgent = null;   // currently selected agent id
        this.reconnectDelay = 1000;

        // Raw data for node detail panel
        this._rawMessages = [];         // all messages, capped at 200
        this._agentActivityLines = {};  // agentId -> recent structured activity events
        this._plan = null;
        this._fileChanges = new Map();
        this._panelWidthKeys = ['changes-panel', 'communications-panel'];
        this._activePanelDrag = null;
        this._mentionTargets = [];
        this._mentionSuggestions = [];
        this._mentionSelection = 0;
        this._traceSessions = [];
        this._traceRuns = [];
        this._selectedTraceSession = '';
        this._selectedTraceRun = '';
        this._demoMode = new URL(window.location.href).searchParams.get('demo') || '';
        this._traceDemoStarted = false;
        this._feedSequence = 0;
        this._thinkingAgents = new Set();

        // Graph node click
        this.graph.onNodeClick = (id, node) => this._selectAgent(id);

        // Query bar
        document.getElementById('query-send').addEventListener('click', () => this._handlePrimaryQueryAction());
        document.getElementById('query-input').addEventListener('keydown', (e) => {
            if (this._handleMentionKeydown(e)) return;
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._submitQuery(); }
        });
        document.getElementById('changes-panel-toggle')?.addEventListener('click', () => this._togglePanel('changes-panel'));
        document.getElementById('communications-panel-toggle')?.addEventListener('click', () => this._togglePanel('communications-panel'));
        this._setupPanelResize();

        // V2 drawer + repo wiring — default open (composer lives in the drawer)
        const storedDrawer = window.localStorage.getItem('orb:drawer:open');
        this._drawerOpen = storedDrawer === null ? true : storedDrawer !== 'false';
        this._applyDrawerState();
        document.getElementById('drawer-toggle')?.addEventListener('click', () => this._toggleDrawer());
        document.getElementById('drawer-close-inline')?.addEventListener('click', () => this._setDrawerOpen(false));
        document.getElementById('drawer-open')?.addEventListener('click', () => this._setDrawerOpen(true));
        document.getElementById('stop-run-button')?.addEventListener('click', () => this._requestStopRun());
        document.getElementById('repo-sync-button')?.addEventListener('click', () => this._renderLiveCodeChanges());
        document.getElementById('repo-pr-button')?.addEventListener('click', () => this._announcePrStub());
        document.querySelectorAll('#repo-view-toggle .seg-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('#repo-view-toggle .seg-btn').forEach((b) => b.classList.toggle('on', b === btn));
                this._repoView = btn.dataset.view || 'unified';
                this._renderLiveCodeChanges();
            });
        });
        this._repoView = 'unified';
        this._repoSelectedPath = null;
        this._throughputSamples = [];

        // Manual session config (set via the Session modal). Persists until
        // a run starts and the server locks in the choice.
        this._sessionConfig = { workdir: '', topology: 'auto', agentModels: {} };
        this._sessionLock = { topology: '', agentModels: {}, modelPin: '', workdir: '' };

        this._setupSessionConfigModal();
        document.getElementById('session-config-button')?.addEventListener('click', () => this._openSessionConfig());

        // Desktop grid column resize
        this._setupGridResize();
        this._restoreGridWidths();

        // Move node detail panel into the left rail (below agents) so it
        // doesn't overlay the conversation drawer on the right.
        this._relocateNodePanel();

        const qi = document.getElementById('query-input');
        qi.addEventListener('input', () => {
            qi.style.height = 'auto';
            qi.style.height = Math.min(qi.scrollHeight, 160) + 'px';
            this._updateMentionSuggestions();
        });
        qi.addEventListener('click', () => this._updateMentionSuggestions());
        qi.addEventListener('keyup', () => this._updateMentionSuggestions());
        qi.addEventListener('blur', () => {
            window.setTimeout(() => this._hideMentionSuggestions(), 120);
        });
        document.querySelectorAll('.query-sample').forEach((button) => {
            button.addEventListener('click', () => {
                const prompt = button.dataset.prompt || '';
                if (!prompt) return;
                qi.value = prompt;
                qi.style.height = 'auto';
                qi.style.height = Math.min(qi.scrollHeight, 160) + 'px';
                qi.focus();
            });
        });

        // Question panel wiring
        document.getElementById('question-dismiss').addEventListener('click', () => {
            document.getElementById('question-panel').classList.add('hidden');
        });
        document.getElementById('question-send').addEventListener('click', () => this._sendQuestionReply());
        document.getElementById('question-reply').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._sendQuestionReply();
        });
        document.getElementById('theme-toggle')?.addEventListener('click', () => this._toggleTheme());
        document.getElementById('settings-button')?.addEventListener('click', () => this._openSettings());
        document.getElementById('new-session-button')?.addEventListener('click', () => this._createNewSession());
        document.getElementById('traces-button')?.addEventListener('click', () => this._openTraceAdmin());
        document.getElementById('settings-close')?.addEventListener('click', () => this._closeSettings());
        document.getElementById('settings-backdrop')?.addEventListener('click', () => this._closeSettings());
        document.getElementById('trace-admin-close')?.addEventListener('click', () => this._closeTraceAdmin());
        document.getElementById('trace-admin-backdrop')?.addEventListener('click', () => this._closeTraceAdmin());

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
        this._providerPrefs = new Set();
        this._selectedTopology = 'auto';
        this._topologyList = [];    // cached topology metadata
        this._isRunActive = false;
        this._loadModelOptions();
        this._loadSettings();
        this._loadTopologyOptions();
        this._setChangesEmptyState();
        this._setMessageEmptyState();
        this._restorePanelState();
        this._restorePanelWidths();
        this.graph.setRunState('idle');

        // Apply saved theme
        const savedTheme = window.localStorage.getItem('orb:theme') || 'dark';
        this._applyTheme(savedTheme);

        // Topology dropdown toggle
        document.getElementById('topology-trigger').addEventListener('click', (e) => {
            e.stopPropagation();
            this._toggleTopologyMenu();
        });
        // Close dropdown on outside click
        document.addEventListener('click', (e) => {
            const dd = document.getElementById('topology-dropdown');
            if (!dd.contains(e.target)) {
                dd.classList.remove('open');
                document.getElementById('topology-menu').classList.add('hidden');
            }
        });
        window.addEventListener('resize', () => {
            this._handleViewportResize();
            this._positionTopologyMenu();
        });

        document.getElementById('graph-zoom-in')?.addEventListener('click', () => this.graph.zoomIn());
        document.getElementById('graph-zoom-out')?.addEventListener('click', () => this.graph.zoomOut());
        document.getElementById('graph-reset-view')?.addEventListener('click', () => this.graph.resetView());

        this._connect();
    }

    // ── WebSocket ─────────────────────────────────────────────

    _connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const sessionId = new URL(window.location.href).searchParams.get('session');
        const wsUrl = `${protocol}//${window.location.host}/ws${sessionId ? `?session=${encodeURIComponent(sessionId)}` : ''}`;
        this._wsSessionId = sessionId || '';

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
        if (label) label.textContent = connected ? 'Daemon Connected' : 'Daemon Offline';
        this._setText('hero-status-label', connected && this._isRunActive ? 'Running' : this._statusText());
        const newSessionButton = document.getElementById('new-session-button');
        if (newSessionButton) newSessionButton.disabled = this._isRunActive;
    }

    _restorePanelState() {
        for (const panelId of ['changes-panel', 'communications-panel']) {
            if (!document.getElementById(panelId)) continue;
            const collapsed = window.localStorage.getItem(`orb:${panelId}:collapsed`) === 'true';
            this._setPanelCollapsed(panelId, collapsed);
        }
    }

    _toggleDrawer() { this._setDrawerOpen(!this._drawerOpen); }

    _setDrawerOpen(open) {
        this._drawerOpen = !!open;
        this._drawerManuallyOpened = this._drawerOpen;
        this._applyDrawerState();
        window.localStorage.setItem('orb:drawer:open', String(this._drawerOpen));
        setTimeout(() => this.graph._resize?.(), 160);
    }

    _applyDrawerState() {
        const grid = document.getElementById('main-layout');
        if (grid) grid.classList.toggle('drawer-closed', !this._drawerOpen);
        const toggle = document.getElementById('drawer-toggle');
        if (toggle) {
            toggle.setAttribute('aria-pressed', String(this._drawerOpen));
            toggle.textContent = this._drawerOpen ? '▸ Chat' : '◂ Chat';
        }
        this._updateDrawerUnread();
    }

    _updateDrawerUnread() {
        const pill = document.getElementById('drawer-unread');
        if (!pill) return;
        const n = this._rawMessages?.length || 0;
        pill.textContent = String(n);
        pill.classList.toggle('empty', n === 0);
    }

    _relocateNodePanel() {
        const panel = document.getElementById('node-detail-panel');
        const leftRail = document.getElementById('left-rail');
        if (!panel || !leftRail || panel.parentElement === leftRail) return;
        leftRail.appendChild(panel);
    }

    _positionNodePanel() {
        const panel = document.getElementById('node-detail-panel');
        const topo = document.getElementById('topology-mini');
        const leftRail = document.getElementById('left-rail');
        if (!panel || !topo || !leftRail) return;
        // Use bounding rects so transforms/scroll are accounted for correctly.
        const railRect = leftRail.getBoundingClientRect();
        const topoRect = topo.getBoundingClientRect();
        const top = Math.max(0, Math.round(topoRect.bottom - railRect.top + 10));
        // Clear any stale inline top so the CSS rule (using --ndp-top) wins.
        panel.style.removeProperty('top');
        panel.style.setProperty('--ndp-top', top + 'px');
    }

    _setupGridResize() {
        const grid = document.getElementById('main-layout');
        if (!grid) return;
        const handles = [
            { id: 'left-resize-handle', varName: '--left-col', side: 'left', min: 260, max: 640 },
            { id: 'right-resize-handle', varName: '--right-col', side: 'right', min: 280, max: 600 },
        ];
        const positionHandle = (h) => {
            const el = document.getElementById(h.id);
            if (!el) return;
            const gridRect = grid.getBoundingClientRect();
            if (h.side === 'left') {
                const leftW = parseFloat(getComputedStyle(grid).getPropertyValue('--left-col')) || 400;
                el.style.left = (leftW + 10) + 'px';
            } else {
                const rightW = parseFloat(getComputedStyle(grid).getPropertyValue('--right-col')) || 380;
                el.style.left = (gridRect.width - rightW - 10) + 'px';
            }
        };
        const reposition = () => {
            handles.forEach(positionHandle);
            this._positionNodePanel?.();
        };
        this._gridResizeReposition = reposition;
        reposition();
        window.addEventListener('resize', reposition);

        handles.forEach((h) => {
            const el = document.getElementById(h.id);
            if (!el) return;
            el.addEventListener('pointerdown', (ev) => {
                if (window.innerWidth <= 720) return;
                ev.preventDefault();
                el.setPointerCapture(ev.pointerId);
                el.classList.add('dragging');
                document.body.style.cursor = 'col-resize';
                document.body.style.userSelect = 'none';

                const startX = ev.clientX;
                const gridRect = grid.getBoundingClientRect();
                const startVal = parseFloat(getComputedStyle(grid).getPropertyValue(h.varName)) || (h.side === 'left' ? 400 : 380);

                const onMove = (e) => {
                    const dx = e.clientX - startX;
                    let next = h.side === 'left' ? startVal + dx : startVal - dx;
                    next = Math.max(h.min, Math.min(h.max, next));
                    grid.style.setProperty(h.varName, next + 'px');
                    positionHandle(h);
                    positionHandle(h.side === 'left' ? handles[1] : handles[0]);
                    this.graph._resize?.();
                };
                const onUp = () => {
                    el.releasePointerCapture(ev.pointerId);
                    el.classList.remove('dragging');
                    document.body.style.cursor = '';
                    document.body.style.userSelect = '';
                    window.removeEventListener('pointermove', onMove);
                    window.removeEventListener('pointerup', onUp);
                    const cs = getComputedStyle(grid);
                    window.localStorage.setItem('orb:v2-left-col', cs.getPropertyValue('--left-col').trim());
                    window.localStorage.setItem('orb:v2-right-col', cs.getPropertyValue('--right-col').trim());
                };
                window.addEventListener('pointermove', onMove);
                window.addEventListener('pointerup', onUp);
            });
        });
    }

    _restoreGridWidths() {
        const grid = document.getElementById('main-layout');
        if (!grid) return;
        const left = window.localStorage.getItem('orb:v2-left-col');
        const right = window.localStorage.getItem('orb:v2-right-col');
        if (left) grid.style.setProperty('--left-col', left);
        if (right) grid.style.setProperty('--right-col', right);
        if (this._gridResizeReposition) this._gridResizeReposition();
    }

    _updateTopologyAdaptiveHeight() {
        const el = document.getElementById('topology-mini');
        if (!el) return;
        const rows = this._plan?.graph_view?.rows;
        let count = Array.isArray(rows) && rows.length ? rows.length : 3;
        // If graphView not present, estimate from agent count (layered layout)
        if (!rows || !rows.length) {
            const n = Object.keys(this.agents || {}).length;
            count = n <= 2 ? 2 : n <= 4 ? 3 : n <= 6 ? 4 : 5;
        }
        count = Math.max(2, Math.min(4, count));
        el.style.setProperty('--topo-rows', String(count));
        // Resize canvas + reposition NDP after DOM reflows
        setTimeout(() => {
            this.graph._resize?.();
            this._positionNodePanel();
        }, 80);
    }

    _setupSessionConfigModal() {
        const modal = document.getElementById('session-config-modal');
        if (!modal) return;
        document.getElementById('session-config-close')?.addEventListener('click', () => this._closeSessionConfig());
        document.getElementById('session-config-cancel')?.addEventListener('click', () => this._closeSessionConfig());
        document.getElementById('session-config-backdrop')?.addEventListener('click', () => this._closeSessionConfig());
        document.getElementById('session-config-apply')?.addEventListener('click', () => this._applySessionConfig());
        document.getElementById('session-config-workdir-cwd')?.addEventListener('click', () => {
            const input = document.getElementById('session-config-workdir');
            if (input) input.value = '';
            const hint = document.getElementById('session-config-footer-hint');
            if (hint) hint.textContent = 'Leaving workspace blank keeps the daemon\'s current directory.';
        });
    }

    async _openSessionConfig() {
        const modal = document.getElementById('session-config-modal');
        if (!modal) return;
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');

        // Seed from current session state
        const workdirInput = document.getElementById('session-config-workdir');
        if (workdirInput) {
            workdirInput.value = this._sessionLock.workdir || this._sessionConfig.workdir || '';
            workdirInput.focus();
        }

        // Ensure topology + model catalogs are cached, then render
        await Promise.all([
            this._ensureTopologyList(),
            this._ensureModelList(),
        ]);
        // Preselect based on what's locked or configured
        const seedTopology =
            this._sessionLock.topology
            || (this._sessionConfig.topology !== 'auto' ? this._sessionConfig.topology : '')
            || this._plan?.topology?.id
            || 'auto';
        this._sessionConfig.topology = seedTopology;
        if (this._sessionLock.topology && !Object.keys(this._sessionConfig.agentModels).length) {
            this._sessionConfig.agentModels = { ...this._sessionLock.agentModels };
        }
        this._renderSessionConfigTopologies();
        this._renderSessionConfigAgentModels();
        this._renderSessionConfigLockBanner();
    }

    _closeSessionConfig() {
        const modal = document.getElementById('session-config-modal');
        if (!modal) return;
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
    }

    async _ensureTopologyList() {
        if (this._topologyList && this._topologyList.length) return;
        try {
            const res = await fetch('/api/topologies');
            const data = await res.json();
            this._topologyList = Array.isArray(data.topologies) ? data.topologies : [];
        } catch {
            this._topologyList = [];
        }
    }

    async _ensureModelList() {
        if (this._modelCatalog && this._modelCatalog.length) return;
        try {
            const res = await fetch('/api/models');
            const data = await res.json();
            this._modelCatalog = Array.isArray(data.models) ? data.models : [];
        } catch {
            this._modelCatalog = [];
        }
    }

    _renderSessionConfigTopologies() {
        const host = document.getElementById('session-config-topology-list');
        if (!host) return;
        const items = [{ id: 'auto', label: 'Auto' }, ...(this._topologyList || [])];
        host.innerHTML = items.map((t) => {
            const selected = this._sessionConfig.topology === t.id ? ' selected' : '';
            return `<button type="button" class="sc-topology-item${selected}" data-topology="${this._escapeAttr(t.id)}">
                <span class="tlabel">${this._escapeHtml(t.label || t.id)}</span>
                ${t.id !== 'auto' ? `<span class="tid">${this._escapeHtml(t.id)}</span>` : ''}
            </button>`;
        }).join('');
        host.querySelectorAll('.sc-topology-item').forEach((btn) => {
            btn.addEventListener('click', () => {
                const tid = btn.dataset.topology || 'auto';
                this._sessionConfig.topology = tid;
                // Reset per-agent models when topology changes — different agent roster
                if (tid === 'auto') {
                    this._sessionConfig.agentModels = {};
                } else {
                    const topo = (this._topologyList || []).find((t) => t.id === tid);
                    const validRoles = new Set(topo?.agents || []);
                    const next = {};
                    for (const [role, model] of Object.entries(this._sessionConfig.agentModels || {})) {
                        if (validRoles.has(role)) next[role] = model;
                    }
                    this._sessionConfig.agentModels = next;
                }
                this._renderSessionConfigTopologies();
                this._renderSessionConfigAgentModels();
            });
        });
    }

    _renderSessionConfigAgentModels() {
        const section = document.getElementById('session-config-agent-models-section');
        const host = document.getElementById('session-config-agent-models');
        if (!section || !host) return;

        const tid = this._sessionConfig.topology;
        if (tid === 'auto') {
            section.classList.add('hidden');
            host.innerHTML = '';
            return;
        }
        section.classList.remove('hidden');
        const topo = (this._topologyList || []).find((t) => t.id === tid);
        const agents = topo?.agents || [];
        if (!agents.length) {
            host.innerHTML = `<div class="sc-empty">No agents defined for this topology.</div>`;
            return;
        }
        const models = this._modelCatalog || [];
        const options = ['<option value="">auto</option>']
            .concat(models.map((m) => {
                const id = String(m.id || '');
                const label = String(m.label || id);
                return `<option value="${this._escapeAttr(id)}">${this._escapeHtml(label)}</option>`;
            }))
            .join('');
        host.innerHTML = agents.map((role) => {
            const selected = this._sessionConfig.agentModels[role] || '';
            const roleClass = (role || 'generic').toLowerCase();
            return `<div class="sc-agent-row v2-role-${this._escapeAttr(roleClass)}">
                <div class="sc-agent-name"><span class="role-dot"></span>${this._escapeHtml(this._roleDisplayName(role, role))}</div>
                <select data-role="${this._escapeAttr(role)}">${options}</select>
            </div>`;
        }).join('');
        host.querySelectorAll('select').forEach((sel) => {
            const role = sel.dataset.role;
            sel.value = this._sessionConfig.agentModels[role] || '';
            sel.addEventListener('change', () => {
                const val = sel.value.trim();
                if (val) this._sessionConfig.agentModels[role] = val;
                else delete this._sessionConfig.agentModels[role];
            });
        });
    }

    _renderSessionConfigLockBanner() {
        const banner = document.getElementById('session-config-lock-banner');
        if (!banner) return;
        if (this._sessionLock.topology) banner.classList.remove('hidden');
        else banner.classList.add('hidden');
    }

    async _applySessionConfig() {
        const workdirInput = document.getElementById('session-config-workdir');
        const workdir = (workdirInput?.value || '').trim();
        this._sessionConfig.workdir = workdir;

        // If workdir differs from the current lock, start a fresh session
        // scoped to that path. Otherwise we just stash the UI config.
        const needsNewSession = workdir && workdir !== this._sessionLock.workdir;
        const applyBtn = document.getElementById('session-config-apply');
        if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = 'Applying…'; }
        try {
            if (needsNewSession) {
                const res = await fetch('/api/session/new', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ workdir }),
                });
                const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
                if (!data.ok) {
                    const hint = document.getElementById('session-config-footer-hint');
                    if (hint) hint.textContent = data.error || 'Failed to create session';
                    return;
                }
                if (data.session_id) this._updateSessionUrl(data.session_id);
                if (data.init) this._handleInit(data.init);
            }
            // Reflect picked topology in the composer trigger + state so the
            // next /api/start call sends it through.
            this._selectedTopology = this._sessionConfig.topology;
            this._userOverrodeTopology = this._sessionConfig.topology !== 'auto';
            this._updateTopologyTrigger?.();
            this._closeSessionConfig();
        } finally {
            if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = 'Apply'; }
        }
    }

    _applySessionLock(sessionBlock) {
        if (!sessionBlock || typeof sessionBlock !== 'object') return;
        this._sessionLock = {
            topology: String(sessionBlock.locked_topology || ''),
            agentModels: { ...(sessionBlock.locked_agent_models || {}) },
            modelPin: String(sessionBlock.locked_model_pin || ''),
            workdir: String(sessionBlock.workdir || ''),
        };
        // Render lock icon
        const icon = document.getElementById('breadcrumbs-lock-icon');
        if (icon) icon.classList.toggle('on', !!this._sessionLock.topology);
        // Disable the inline topology dropdown when pinned
        const trigger = document.getElementById('topology-trigger');
        if (trigger) {
            if (this._sessionLock.topology) {
                trigger.classList.add('locked');
                trigger.title = `Pinned to ${this._sessionLock.topology} for this session`;
            } else {
                trigger.classList.remove('locked');
                trigger.title = 'Switch topology';
            }
        }
    }

    async _requestStopRun() {
        try {
            await fetch('/api/stop', { method: 'POST' });
        } catch (err) { /* ignore */ }
    }

    _announcePrStub() {
        const btn = document.getElementById('repo-pr-button');
        if (!btn) return;
        const original = btn.textContent;
        btn.textContent = 'Not wired yet';
        btn.disabled = true;
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1600);
    }

    _restorePanelWidths() {
        for (const panelId of this._panelWidthKeys) {
            const stored = Number(window.localStorage.getItem(`orb:${panelId}:width`));
            if (Number.isFinite(stored) && stored > 0) {
                this._setPanelWidth(panelId, stored);
            } else {
                this._setPanelWidth(panelId, null);
            }
        }
    }

    _togglePanel(panelId) {
        const panel = document.getElementById(panelId);
        if (!panel) return;
        const collapsed = !panel.classList.contains('is-collapsed');
        this._setPanelCollapsed(panelId, collapsed);
        window.localStorage.setItem(`orb:${panelId}:collapsed`, String(collapsed));
        setTimeout(() => this.graph._resize(), 160);
    }

    _setPanelCollapsed(panelId, collapsed) {
        const panel = document.getElementById(panelId);
        const toggle = document.getElementById(`${panelId}-toggle`);
        if (!panel || !toggle) return;
        panel.classList.toggle('is-collapsed', collapsed);
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        toggle.title = `${collapsed ? 'Expand' : 'Collapse'} ${panelId === 'changes-panel' ? 'code changes' : 'runtime'} panel`;
        const icon = toggle.querySelector('.panel-toggle-icon');
        if (icon) icon.textContent = collapsed ? '+' : '−';
        this._syncResizeHandle(panelId);
        if (!collapsed) this._setPanelWidth(panelId, this._getStoredPanelWidth(panelId));
    }

    _setupPanelResize() {
        const handles = document.querySelectorAll('.panel-resize-handle');
        for (const handle of handles) {
            handle.addEventListener('pointerdown', (event) => this._startPanelResize(event, handle));
        }
    }

    _startPanelResize(event, handle) {
        if (window.innerWidth <= 980) return;
        const panelId = handle.dataset.panelId;
        const side = handle.dataset.side;
        const panel = document.getElementById(panelId);
        if (!panel || panel.classList.contains('is-collapsed')) return;

        this._activePanelDrag = { panelId, side, pointerId: event.pointerId };
        handle.classList.add('is-dragging');
        document.body.classList.add('is-resizing-panels');
        handle.setPointerCapture(event.pointerId);
        event.preventDefault();

        const move = (moveEvent) => this._onPanelResize(moveEvent);
        const stop = (endEvent) => {
            if (endEvent.pointerId !== event.pointerId) return;
            handle.classList.remove('is-dragging');
            document.body.classList.remove('is-resizing-panels');
            this._activePanelDrag = null;
            handle.removeEventListener('pointermove', move);
            handle.removeEventListener('pointerup', stop);
            handle.removeEventListener('pointercancel', stop);
            this.graph._resize();
        };

        handle.addEventListener('pointermove', move);
        handle.addEventListener('pointerup', stop);
        handle.addEventListener('pointercancel', stop);
    }

    _onPanelResize(event) {
        if (!this._activePanelDrag) return;
        const { panelId, side } = this._activePanelDrag;
        const layout = document.getElementById('main-layout');
        const panel = document.getElementById(panelId);
        const oppositePanelId = panelId === 'changes-panel' ? 'communications-panel' : 'changes-panel';
        const oppositePanel = document.getElementById(oppositePanelId);
        const graphPanel = document.getElementById('graph-panel');
        if (!layout || !panel || !graphPanel) return;

        const layoutRect = layout.getBoundingClientRect();
        const graphRect = graphPanel.getBoundingClientRect();
        const oppositeWidth = oppositePanel && !oppositePanel.classList.contains('is-collapsed')
            ? oppositePanel.getBoundingClientRect().width
            : 56;
        const minimumGraphWidth = Math.min(420, Math.max(300, window.innerWidth * 0.28));
        const maxWidth = Math.max(280, layoutRect.width - oppositeWidth - minimumGraphWidth - 24);
        const proposedWidth = side === 'left'
            ? event.clientX - layoutRect.left
            : layoutRect.right - event.clientX;
        const fallbackWidth = side === 'left' ? graphRect.left - layoutRect.left : layoutRect.right - graphRect.right;
        const width = this._clampPanelWidth(Number.isFinite(proposedWidth) ? proposedWidth : fallbackWidth, maxWidth);
        this._setPanelWidth(panelId, width);
    }

    _handleViewportResize() {
        for (const panelId of this._panelWidthKeys) {
            this._setPanelWidth(panelId, this._getStoredPanelWidth(panelId));
            this._syncResizeHandle(panelId);
        }
        this.graph._resize();
    }

    _getStoredPanelWidth(panelId) {
        const stored = Number(window.localStorage.getItem(`orb:${panelId}:width`));
        return Number.isFinite(stored) && stored > 0 ? stored : null;
    }

    _setPanelWidth(panelId, width) {
        const panel = document.getElementById(panelId);
        if (!panel) return;
        if (window.innerWidth <= 980) {
            panel.style.width = '';
            panel.style.flexBasis = '';
            return;
        }
        if (!Number.isFinite(width) || width === null) {
            panel.style.width = '';
            panel.style.flexBasis = '';
            return;
        }
        const clampedWidth = this._clampPanelWidth(width);
        panel.style.width = `${clampedWidth}px`;
        panel.style.flexBasis = `${clampedWidth}px`;
        window.localStorage.setItem(`orb:${panelId}:width`, String(clampedWidth));
        this._syncResizeHandle(panelId);
        this.graph._resize();
    }

    _clampPanelWidth(width, maxWidth = null) {
        const minWidth = 280;
        const boundedMax = Number.isFinite(maxWidth) ? maxWidth : 520;
        return Math.max(minWidth, Math.min(Math.round(width), boundedMax));
    }

    _syncResizeHandle(panelId) {
        const panel = document.getElementById(panelId);
        const handle = document.querySelector(`.panel-resize-handle[data-panel-id="${panelId}"]`);
        if (!panel || !handle) return;
        const hidden = window.innerWidth <= 980 || panel.classList.contains('is-collapsed');
        handle.classList.toggle('hidden', hidden);
    }

    // ── Event dispatch ────────────────────────────────────────

    _handleEvent(data) {
        switch (data.type) {
            case 'init':           this._handleInit(data);           break;
            case 'plan_step':      this._handlePlanStep(data);       break;
            case 'message':        this._handleMessage(data);        break;
            case 'agent_status':   this._handleAgentStatus(data);    break;
            case 'agent_stats':    this._handleAgentStats(data);     break;
            case 'agent_heartbeat': this._handleAgentHeartbeat(data); break;
            case 'complete':       this._handleComplete(data);       break;
            case 'stats':          this._handleStats(data);          break;
            case 'stopped':        this._handleStopped();            break;
            case 'run_complete':   this._handleRunComplete(data);    break;
            case 'agent_activity': this._handleAgentActivity(data);  break;
            case 'file_write':     this._handleFileWrite(data);      break;
            case 'topologies_reloaded': this._loadTopologyOptions();   break;
        }
    }

    _handleInit(data) {
        document.getElementById('result-panel').classList.add('hidden');
        this._rawMessages = [];
        this._agentActivityLines = {};
        this._plan = data.plan || null;
        this._planSteps = Array.isArray(data.plan_steps) ? [...data.plan_steps] : [];
        this._fileChanges = new Map();
        this._feedSequence = 0;
        this._clearThinking();
        this._updateSessionUrl(data.session_id || '');
        if (!document.getElementById('trace-admin-modal')?.classList.contains('hidden')) {
            this._loadTraceSessions();
        }
        if (this.changesLog) this.changesLog.innerHTML = '';
        this._setChangesEmptyState();
        this._updateWorkdir(
            data.workdir || this._plan?.workdir || '',
            data.session_id || '',
            data.session_generation || 1,
            data.session_turn || 0,
        );

        // Update topology stat from server plan
        if (this._plan?.topology?.label) {
            document.getElementById('stat-topology').textContent = this._plan.topology.label;
            this._setText('hero-topology-label', this._plan.topology.label);
            this._setText('breadcrumbs-topology', this._plan.topology.id || this._plan.topology.label);
            this._setText('topology-count', this._plan.topology.id || this._plan.topology.label);
        }

        // Reset panel without side-effects of _hideNodePanel (which clears selectedAgent)
        document.getElementById('node-detail-panel').classList.add('ndp-closed');
        document.getElementById('communications-panel')?.classList.remove('node-panel-active');
        const emptyInspector = document.getElementById('node-detail-empty');
        if (emptyInspector) emptyInspector.classList.remove('hidden');
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
        this._refreshMentionTargets();
        this._renderAgentsList();
        this._renderLiveCodeChanges();
        this._updateTopologyAdaptiveHeight();
        this._applySessionLock(data.session);

        this.graph.setTopology(data.agents, data.edges, this._plan?.graph_view || null);
        this._hideLoader();

        // Render existing communications
        this.messageLog.innerHTML = '';
        for (const step of this._planSteps || []) {
            this._addPlanStepEntry(step);
        }
        for (const activityEvent of data.activity_events || []) {
            this._handleAgentActivity({
                type: 'agent_activity',
                agent: activityEvent.agent,
                activity: activityEvent.activity,
                elapsed: activityEvent.elapsed,
                details: activityEvent.details || {},
                hydrated: true,
            });
        }
        if ((data.messages || []).length === 0 && (data.activity_events || []).length === 0 && !this.messageLog.children.length) {
            this._setMessageEmptyState();
        }
        for (const msg of data.messages) {
            this._addMessageEntry(msg);
        }
        this._renderPlanningState();
        this._updateStatusIndicator();

        if (data.stats) this._handleStats(data.stats);
        this._hydrateLiveCodeChangesFromInit(data);

        // Determine UI state based on run state
        if (data.run_active === true) {
            // Mid-run reconnect
            this._setRunActive(true);
            this.graph.setRunState('running');
            this._hydrateCodeChangesFromInit(data);
        } else if (data.completed === true) {
            // Reconnect after a finished run: keep the result in the left rail only.
            this._setRunActive(false);
            this.graph.setRunState('completed');
            const completedAgent = data.agents.find(a =>
                a.status === 'completed' && a.completed_result && !a.completed_result.startsWith('Consensus:')
            );
            if (completedAgent) {
                this._renderInitialCodeChanges(
                    data.final_result || completedAgent.completed_result,
                    data.final_diff || '',
                    '',
                    data.session_turn || 0,
                );
                this._addRunCompleteActivity({
                    agent: completedAgent.role || completedAgent.id || 'run',
                    elapsed: data.stats?.elapsed || 0,
                    result: data.final_result || completedAgent.completed_result || '',
                    hydrated: true,
                });
            }
            this._hydrateCodeChangesFromInit(data);
        } else {
            // Fresh open or no active run
            this._setRunActive(false);
            this.graph.setRunState('idle');
        }
    }

    _handlePlanStep(data) {
        if (!this._plan) this._plan = { topology: {}, routing: {}, agent_complexity: {}, agent_models: {} };
        if (!this._planSteps) this._planSteps = [];
        const step = {
            stage: data.stage || 'planning',
            title: data.title || 'Planning update',
            detail: data.detail || '',
            elapsed: data.elapsed || 0,
        };
        this._planSteps.push(step);
        if (this._planSteps.length > 20) this._planSteps = this._planSteps.slice(-20);
        this._addPlanStepEntry(step);

        const statusEl = document.getElementById('stat-status');
        if (statusEl && !this._isRunActive) {
            statusEl.textContent = 'Planning';
            statusEl.className = 'stat-value status-running';
            this._setText('hero-status-label', 'Planning');
        }

        if (typeof data.detail === 'string' && data.title === 'Selected topology') {
            const match = data.detail.match(/^([^:]+):?/);
            if (match?.[1]) {
                this._plan.topology = this._plan.topology || {};
                this._plan.topology.label = match[1].trim();
                document.getElementById('stat-topology').textContent = match[1].trim();
                this._setText('hero-topology-label', match[1].trim());
            }
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
        if (data.model) this.graph.updateAgentStatus(data.from, this.agents[data.from]?.status || '', data.model || '');
        // Push last activity preview into sender node
        const preview = (data.content || '').replace(/\s+/g, ' ').trim().slice(0, 60);
        this.graph.updateAgentActivity(data.from, preview);
        this._updateStatusIndicator();

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
        this._updateStatusIndicator();
        if (this.selectedAgent === data.agent) this._refreshNodePanel();
        // Show the actual model being used in the stats bar (first agent to report one wins)
        if (data.model && this._selectedModel === 'auto' && !this._runModelShown) {
            this._runModelShown = true;
            document.getElementById('stat-model').textContent = this._shortModel(data.model);
            this._setText('hero-model-label', this._shortModel(data.model));
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
        if (this.selectedAgent === data.agent) this._refreshNodePanel();
        this._renderAgentsList();
    }

    _handleAgentHeartbeat(data) {
        if (this.agents[data.agent]) {
            this.agents[data.agent].last_heartbeat = data.ts || 0;
            if (data.status && this.agents[data.agent].status !== 'completed' && this.agents[data.agent].status !== 'error') {
                this.agents[data.agent].status = data.status;
            }
        }
        if (this.selectedAgent === data.agent) this._refreshNodePanel();
    }

    _handleComplete(data) {
        this._clearThinking(data.agent);
        if (this.agents[data.agent]) {
            this.agents[data.agent].status = 'completed';
            this.agents[data.agent].result = data.result;
        }
        this.graph.updateAgentStatus(data.agent, 'completed');
        this._updateStatusIndicator();
        if (this.selectedAgent === data.agent) this._refreshNodePanel();

        // Final completion is rendered in the left rail run-output surface.
        const isConsensus = data.is_consensus === true;
        if (isConsensus) return;
    }

    _handleStats(data) {
        document.getElementById('stat-messages').textContent = data.message_count;
        document.getElementById('stat-budget').textContent   = data.budget_remaining;
        document.getElementById('stat-elapsed').textContent  = data.elapsed.toFixed(1) + 's';
        this._lastElapsed = data.elapsed;
        this._sampleThroughput(data.message_count, data.elapsed);
        const sub = document.getElementById('stat-elapsed-sub');
        if (sub) sub.textContent = this._isRunActive ? 'live' : 'idle';
        this._updateDrawerUnread();
    }

    _sampleThroughput(count, elapsed) {
        const samples = this._throughputSamples || (this._throughputSamples = []);
        samples.push({ count, t: elapsed });
        while (samples.length > 60) samples.shift();
        const points = [];
        for (let i = 1; i < samples.length; i++) {
            const a = samples[i - 1], b = samples[i];
            const dt = Math.max(0.0001, b.t - a.t);
            points.push(Math.max(0, (b.count - a.count) / dt));
        }
        const spark = document.getElementById('stat-throughput-spark');
        if (!spark || points.length === 0) return;
        const line = spark.querySelector('.spark-line');
        const fill = spark.querySelector('.spark-fill');
        const W = 100, H = 22;
        const min = 0, max = Math.max(1, ...points);
        const rng = max - min || 1;
        const pts = points.map((p, i) => [
            (i / Math.max(1, points.length - 1)) * W,
            H - ((p - min) / rng) * (H - 2) - 1,
        ]);
        const d = pts.map(([x, y], i) => (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1)).join(' ');
        if (line) line.setAttribute('d', d);
        if (fill) fill.setAttribute('d', d + ` L ${W} ${H} L 0 ${H} Z`);
        const avg = points.reduce((s, v) => s + v, 0) / points.length;
        const peak = Math.max(...points);
        const sub = document.getElementById('stat-throughput-sub');
        if (sub) sub.textContent = `peak ${peak.toFixed(1)} · avg ${avg.toFixed(1)}`;
    }

    _renderAgentsList() {
        const host = document.getElementById('agents-list');
        if (!host) return;
        const entries = Object.values(this.agents || {});
        const count = document.getElementById('agents-count');
        if (count) count.textContent = String(entries.length);
        if (entries.length === 0) {
            host.innerHTML = `<div class="repo-empty" style="padding:18px 14px;">No agents yet. Start a task to spin up the crew.</div>`;
            return;
        }
        host.innerHTML = entries.map((a) => {
            const role = (a.role || 'generic').toLowerCase();
            const selected = this.selectedAgent === a.id ? ' active' : '';
            const statusDim = (a.status === 'completed' || a.status === 'idle') ? 'opacity:.55;' : '';
            return `
                <div class="agent-row v2-role-${this._escapeAttr(role)}${selected}" data-agent-id="${this._escapeAttr(a.id)}">
                    <span class="v2-role-dot" style="${statusDim}"></span>
                    <div class="agent-body">
                        <span class="agent-name">${this._escapeHtml(this._roleDisplayName(a.role, a.id))}</span>
                        <span class="agent-meta">
                            <span>${this._escapeHtml(a.model || '—')}</span>
                            <span class="sep">·</span>
                            <span>${this._escapeHtml(a.status || 'idle')}</span>
                        </span>
                    </div>
                    <span class="msg-count">${a.msg_count || 0}</span>
                </div>
            `;
        }).join('');
        host.querySelectorAll('.agent-row').forEach((row) => {
            row.addEventListener('click', () => {
                const id = row.dataset.agentId;
                if (id) this._selectAgent(id);
            });
        });
    }

    _roleDisplayName(role, fallback) {
        if (!role) return fallback || 'agent';
        const r = String(role);
        return r.charAt(0).toUpperCase() + r.slice(1);
    }

    _escapeAttr(s) {
        return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    _handleFileWrite(data) {
        const path = data.path || '';
        if (!path) return;
        this._fileChanges.set(path, {
            path,
            agent: data.agent || '',
            content: data.content || '',
            oldContent: data.old_content || '',
        });
        this._renderLiveCodeChanges();
    }

    _updateStatusIndicator() {
        const el       = document.getElementById('stat-status');
        const statuses = Object.values(this.agents).map(a => a.status);

        if (statuses.length === 0) return;

        if (statuses.every(s => s === 'completed')) {
            el.textContent = 'Done';
            el.className   = 'stat-value status-done';
            this.graph.setRunState('completed');
            this._setRunActive(false);
        } else if (statuses.some(s => s === 'running')) {
            el.textContent = 'Running';
            el.className   = 'stat-value status-running';
            this.graph.setRunState('running');
        } else {
            el.textContent = 'Waiting';
            el.className   = 'stat-value status-waiting';
            this.graph.setRunState(this._isRunActive ? 'running' : 'idle');
        }
        this._setText('hero-status-label', this._statusText());
    }

    _statusText() {
        const el = document.getElementById('stat-status');
        return el ? el.textContent : 'Waiting';
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
        return this._plan?.topology?.label || (Object.keys(this.agents).length ? 'Active Graph' : 'Uninitialized');
    }

    _setChangesEmptyState(text = 'Final run summaries and diffs will accumulate here.') {
        if (!this.changesLog) return;
        this.changesLog.innerHTML = `
            <div class="empty-state empty-state-changes">
                <div class="empty-state-kicker">Code Changes</div>
                <div class="empty-state-title">No run output yet</div>
                <div class="empty-state-copy">${this._escapeHtml(text)}</div>
            </div>
        `;
    }

    _renderPlanningState() {
        if (!this.messageLog) return;
        const existing = this.messageLog.querySelector('.prediction-card');
        if (existing) existing.remove();
        if (!this._plan?.topology?.id) return;
        const routing = this._plan.routing || {};
        this._addPredictionCard({
            topology: this._plan.topology,
            task_type: routing.task_type || '',
            routing_mode: routing.routing_mode || '',
            reason: routing.reason || '',
            summary: routing.summary || '',
            classifier_model: routing.classifier_model || '',
            classifier_provider: routing.classifier_provider || '',
            escalation_allowed: Boolean(routing.escalation_allowed),
            stop_early_allowed: Boolean(routing.stop_early_allowed),
            escalation_reason: routing.escalation_reason || '',
            stop_early_reason: routing.stop_early_reason || '',
            complexity: Math.max(...Object.values(this._plan.agent_complexity || {}), 0),
            agent_complexity: this._plan.agent_complexity || {},
            agent_models: this._plan.agent_models || {},
        });
    }

    _addPlanStepEntry(step) {
        const stage = String(step.stage || 'planning').toLowerCase();
        const stageClass = {
            planning: 'msg-stage-planning',
            allocator: 'msg-stage-allocator',
            topology: 'msg-stage-topology',
            models: 'msg-stage-models',
            execution: 'msg-stage-execution',
        }[stage] || 'msg-stage-generic';

        const entry = document.createElement('div');
        entry.className = 'msg-entry msg-entry-plan';
        entry.innerHTML = `
            <div class="msg-header">
                <span class="msg-time">${(step.elapsed || 0).toFixed(1)}s</span>
                <span class="msg-type-badge msg-type-system">plan</span>
                <span class="agent-coordinator">runtime</span>
                <span class="msg-stage-pill ${stageClass}">${this._escapeHtml(step.stage || 'planning')}</span>
            </div>
            <div class="msg-preview"><strong>${this._escapeHtml(step.title || 'Planning update')}</strong></div>
            ${step.detail ? `<div class="msg-expanded" style="display:block"><div class="msg-full-content">${this._escapeHtml(step.detail)}</div></div>` : ''}
        `;

        this._insertFeedEntry(entry, step.elapsed || 0);
    }

    _updateWorkdir(workdir, sessionId = '', generation = 1, sessionTurn = 0) {
        const workdirEl = document.getElementById('workdir-banner');
        const composerWorkdirEl = document.getElementById('composer-workdir');
        const sessionEl = document.getElementById('session-id-banner');
        const generationEl = document.getElementById('session-generation-banner');
        const turnEl = document.getElementById('session-turn-banner');
        const pillEl = document.getElementById('session-pill');
        const breadcrumbWorkdir = document.getElementById('breadcrumbs-workdir');
        const breadcrumbSession = document.getElementById('breadcrumbs-session');
        if (!pillEl) return;

        const workspaceName = workdir ? workdir.split('/').filter(Boolean).pop() || workdir : '—';
        const shortSession = sessionId ? sessionId.slice(0, 8) : '—';

        if (workdirEl) { workdirEl.textContent = workspaceName; workdirEl.title = workdir || ''; }
        if (composerWorkdirEl) {
            composerWorkdirEl.textContent = workspaceName;
            composerWorkdirEl.title = workdir || '';
        }
        if (breadcrumbWorkdir) { breadcrumbWorkdir.textContent = workspaceName; breadcrumbWorkdir.title = workdir || ''; }
        if (breadcrumbSession) {
            breadcrumbSession.textContent = sessionId ? `run_${shortSession}` : '—';
            breadcrumbSession.title = sessionId || '';
        }

        if (sessionEl) { sessionEl.textContent = shortSession; sessionEl.title = sessionId || ''; }
        if (generationEl) generationEl.textContent = String(generation || 1);
        if (turnEl) turnEl.textContent = String(sessionTurn || 0);

        if (sessionId) {
            pillEl.textContent = sessionTurn > 0 ? 'Session Active' : 'Session Ready';
            pillEl.className = sessionTurn > 0 ? 'session-pill session-pill-active' : 'session-pill session-pill-ready';
            pillEl.title = `session ${sessionId}`;
        } else {
            pillEl.textContent = 'Session Idle';
            pillEl.className = 'session-pill session-pill-idle';
            pillEl.title = '';
        }
    }

    _setMessageEmptyState(text = 'Runs, agent questions, and handoffs will appear here.') {
        this.messageLog.innerHTML = `
            <div class="empty-state empty-state-messages">
                <div class="empty-state-kicker">Node Communications</div>
                <div class="empty-state-title">No graph traffic yet</div>
                <div class="empty-state-copy">${this._escapeHtml(text)}</div>
            </div>
        `;
    }

    _addRunCompleteActivity(data) {
        if (!this.messageLog || !data?.result) return;
        if (data.hydrated) {
            if (this.messageLog.querySelector('[data-activity-kind="run-complete"]')) return;
        } else {
            const existing = this.messageLog.querySelector('[data-activity-kind="run-complete"]');
            if (existing) existing.remove();
        }

        const empty = this.messageLog.querySelector('.empty-state');
        if (empty) empty.remove();

        const preview = String(data.result || '').replace(/\s+/g, ' ').trim().slice(0, 180);
        const entry = document.createElement('div');
        entry.className = 'msg-entry msg-entry-run-complete';
        entry.dataset.activityKind = 'run-complete';
        entry.innerHTML = `
            <div class="msg-header">
                <span class="msg-time">${(data.elapsed || 0).toFixed(1)}s</span>
                <span class="msg-type-badge msg-type-complete">complete</span>
                <span class="msg-stage-pill msg-stage-complete">done</span>
            </div>
            <div class="msg-preview"><strong>Run complete</strong> by ${this._escapeHtml(data.agent || 'runtime')}</div>
            <div class="msg-expanded" style="display:block">
                <div class="msg-section-label">Summary</div>
                <div class="msg-full-content">${this._escapeHtml(preview)}${preview.length >= 180 ? '…' : ''}</div>
            </div>
        `;
        this._insertFeedEntry(entry, data.elapsed || 0);
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

    _selectAgent(agentId) {
        // Toggle: clicking the currently selected agent closes the panel.
        const panel = document.getElementById('node-detail-panel');
        const isOpen = panel && !panel.classList.contains('ndp-closed');
        if (agentId && this.selectedAgent === agentId && isOpen) {
            this._hideNodePanel();
            this._renderAgentsList();
            return;
        }

        this.selectedAgent = agentId;
        this.graph.selectedNode = agentId;

        if (agentId) {
            this._positionNodePanel();
            this._showNodePanel(agentId);
            this._renderAgentsList();
        } else {
            this._hideNodePanel();
            this._renderAgentsList();
        }
    }

    // ── Node detail panel ─────────────────────────────────

    _showNodePanel(agentId) {
        const emptyInspector = document.getElementById('node-detail-empty');
        if (emptyInspector) emptyInspector.classList.add('hidden');
        document.getElementById('node-detail-panel').classList.remove('ndp-closed');
        document.getElementById('communications-panel')?.classList.add('node-panel-active');
        this._refreshNodePanel();
        const inp = document.getElementById('ndp-chat-input');
        if (inp) inp.focus();
    }

    _hideNodePanel() {
        document.getElementById('node-detail-panel').classList.add('ndp-closed');
        document.getElementById('communications-panel')?.classList.remove('node-panel-active');
        const emptyInspector = document.getElementById('node-detail-empty');
        if (emptyInspector) emptyInspector.classList.remove('hidden');
        if (this.selectedAgent) {
            this.selectedAgent = null;
            this.graph.selectedNode = null;
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
        const compScore = this._plan?.agent_complexity?.[agentId];
        const heartbeat = this._heartbeatState(agent);
        const relevantMessages = this._rawMessages.filter(m => m.from === agentId || m.to === agentId);
        const outgoing = relevantMessages.filter(m => m.from === agentId);
        const incoming = relevantMessages.filter(m => m.to === agentId);
        const peers = [...new Set(relevantMessages.map(m => m.from === agentId ? m.to : m.from).filter(Boolean))];
        const uniqueNeighbors = this._plan?.neighbors?.[agentId]
            || [...new Set((this.graph.edges || [])
                .flatMap(e => e.source === agentId ? [e.target] : (e.target === agentId ? [e.source] : [])))];
        const activePeers = peers.filter(p => uniqueNeighbors.includes(p));
        const edgeList = (this.graph.edges || [])
            .filter(e => e.source === agentId || e.target === agentId)
            .map(e => `${e.source} ↔ ${e.target}`);
        const position = this._plan?.positions?.[agentId] || 'graph participant';
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
            ? `${agent.role || agentId} is active in the graph as the ${position}.`
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
                ${this._escapeHtml(agent.role || agentId)} is the ${this._escapeHtml(position)} and can communicate directly with ${uniqueNeighbors.length ? uniqueNeighbors.join(', ') : 'no current neighbors'}.
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
        if (lines.length > 0) {
            actSection.classList.remove('hidden');
            actLog.innerHTML = lines.map((entry) => {
                const activity = typeof entry === 'string' ? entry : (entry.activity || '');
                const details = entry && typeof entry === 'object' ? (entry.details || {}) : {};
                const elapsed = entry && typeof entry === 'object' && entry.elapsed !== undefined
                    ? `<span class="ndp-activity-elapsed">${Number(entry.elapsed).toFixed(1)}s</span>`
                    : '';
                const destination = typeof details.to === 'string' && details.to
                    ? `<span class="ndp-activity-target">${this._escapeHtml(details.to)}</span>`
                    : '';
                const content = typeof details.content === 'string' && details.content
                    ? `<div class="ndp-activity-payload">${this._escapeHtml(details.content)}</div>`
                    : '';
                const context = Array.isArray(details.context) && details.context.length
                    ? `<div class="ndp-activity-context">${details.context.map((item, index) => `<div class="msg-context-item">[${index}] ${this._escapeHtml(item)}</div>`).join('')}</div>`
                    : '';
                return `
                <div class="ndp-activity-line">
                    <span class="activity-icon">${this._activityIcon(activity)}</span>
                    <div class="ndp-activity-copy">
                        <div class="ndp-activity-header">
                            ${elapsed}
                            <span>${this._escapeHtml(activity)}</span>
                            ${destination}
                        </div>
                        ${content}
                        ${context}
                    </div>
                </div>
            `;
            }).join('');
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

    // ── Query bar ─────────────────────────────────────────────

    async _loadModelOptions() {
        const res = await fetch('/api/models');
        const data = await res.json();
        this._modelLabels = {};
        this._models = data.models || [];
        this._indexModelsByProvider(this._models);
        this._renderProviderSettings();
        this._applyProviderPreferences();
        document.getElementById('stat-model').textContent =
            this._modelLabels[this._selectedModel] || 'Auto';
        this._setText('hero-model-label', this._modelLabels[this._selectedModel] || 'Auto');
    }

    async _loadSettings() {
        const res = await fetch('/api/settings');
        const data = await res.json();
        const providers = data.providers || {};
        const enabledProviders = Object.entries(providers)
            .filter(([, meta]) => meta && meta.enabled)
            .map(([provider]) => provider);
        const fallbackProviders = data.available_providers || [];
        this._providerPrefs = new Set(enabledProviders.length ? enabledProviders : fallbackProviders);
        if (Array.isArray(data.models) && data.models.length) {
            this._models = data.models;
            this._indexModelsByProvider(this._models);
        }
        this._providerSettings = providers;
        this._renderProviderSettings();
        this._applyProviderPreferences();
    }

    _indexModelsByProvider(models) {
        this._providerModels = {};
        this._modelLabels = this._modelLabels || {};
        for (const m of models || []) {
            if (!m || !m.id) continue;
            this._modelLabels[m.id] = m.label;
            const provider = m.provider || this._inferProvider(m.id);
            if (!provider || provider === 'auto') continue;
            if (!this._providerModels[provider]) this._providerModels[provider] = [];
            this._providerModels[provider].push(m);
        }
    }

    _inferProvider(modelId) {
        if (!modelId) return 'unknown';
        if (modelId.startsWith('claude')) return 'anthropic';
        if (modelId.startsWith('gpt') || modelId.startsWith('o1') || modelId.startsWith('o3')) return 'openai-codex';
        if (modelId.includes('/') || modelId.includes('-instruct')) return 'vmlx';
        if (modelId.includes('qwen') || modelId.includes('llama') || modelId.includes(':')) return 'ollama';
        return 'other';
    }

    _providerLabel(provider) {
        if (provider === 'openai-codex') return 'openai';
        return provider;
    }

    _renderProviderSettings() {
        const container = document.getElementById('provider-settings-list');
        if (!container) return;
        const providerSet = new Set([
            ...Object.keys(this._providerModels || {}),
            ...this._providerPrefs,
        ]);
        const providers = [...providerSet].sort();
        if (!providers.length) {
            container.innerHTML = '<div class="settings-empty">No providers discovered from the current daemon.</div>';
            return;
        }
        container.innerHTML = providers.map((provider) => {
            const models = this._providerModels[provider] || [];
            const active = this._providerPrefs.has(provider);
            const settingsMeta = this._providerSettings?.[provider] || {};
            const enabledModels = Array.isArray(settingsMeta.enabled_models) ? settingsMeta.enabled_models : models;
            const enabledModelCopy = enabledModels.length
                ? enabledModels.map((model) => this._escapeHtml(this._shortModel(model.id || model.label || ''))).join(' · ')
                : 'No enabled models';
            return `
                <div class="provider-option provider-option-readonly${active ? ' active' : ''}">
                    <span class="provider-option-copy">
                        <span class="provider-option-name">${this._escapeHtml(this._providerLabel(provider))}</span>
                        <span class="provider-option-meta">${active ? 'enabled' : 'disabled'} · ${models.length} model${models.length === 1 ? '' : 's'}</span>
                        <span class="provider-option-meta">${enabledModelCopy}</span>
                    </span>
                    <span class="provider-state-pill${active ? ' provider-state-pill-active' : ''}">${active ? 'Enabled' : 'Disabled'}</span>
                </div>
            `;
        }).join('');
    }

    _applyProviderPreferences() {
        const activeProviders = [...this._providerPrefs].filter((provider) => (this._providerModels?.[provider] || []).length > 0);
        if (activeProviders.length === 1) {
            this._selectedModel = this._providerModels[activeProviders[0]][0]?.id || 'auto';
        } else {
            this._selectedModel = 'auto';
        }
        document.getElementById('stat-model').textContent =
            this._modelLabels[this._selectedModel] || 'Auto';
        this._setText('hero-model-label', this._modelLabels[this._selectedModel] || 'Auto');
    }

    _applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        const btn = document.getElementById('theme-toggle');
        if (btn) btn.textContent = theme === 'light' ? '🌙' : '☀';
        if (this.graph) this.graph.setTheme(theme);
    }

    _toggleTheme() {
        const current = document.documentElement.getAttribute('data-theme') || 'dark';
        const next = current === 'light' ? 'dark' : 'light';
        window.localStorage.setItem('orb:theme', next);
        this._applyTheme(next);
    }

    _openSettings() {
        const modal = document.getElementById('settings-modal');
        if (!modal) return;
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
    }

    _closeSettings() {
        const modal = document.getElementById('settings-modal');
        if (!modal) return;
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
    }

    async _openTraceAdmin() {
        const modal = document.getElementById('trace-admin-modal');
        if (!modal) return;
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
        await this._loadTraceSessions();
    }

    _maybeStartDemo() {
        if (this._demoMode !== 'traces' || this._traceDemoStarted) return;
        this._traceDemoStarted = true;
        window.setTimeout(() => {
            this._runTraceDemo().catch((err) => console.error('trace demo failed', err));
        }, 900);
    }

    async _runTraceDemo() {
        for (let attempt = 0; attempt < 8; attempt += 1) {
            await this._openTraceAdmin();
            if (this._traceSessions.length) break;
            await this._sleep(400);
        }
        if (!this._traceSessions.length) return;
        await this._sleep(700);

        const sessions = this._traceSessions.slice(0, 3);
        for (const session of sessions) {
            this._selectedTraceSession = session.session_id || '';
            this._selectedTraceRun = '';
            this._renderTraceSessions();
            await this._loadTraceRuns(this._selectedTraceSession);
            await this._sleep(1200);

            if (this._traceRuns.length > 1) {
                this._selectedTraceRun = this._traceRuns[1].run_id || '';
                this._renderTraceRuns();
                await this._loadTraceDetail(this._selectedTraceRun);
                await this._sleep(1000);
            }
        }

        const rawEl = document.getElementById('trace-detail-raw');
        if (rawEl) rawEl.open = true;
        await this._sleep(1400);
        const eventsEl = document.getElementById('trace-detail-events');
        if (eventsEl) eventsEl.scrollTop = Math.min(360, eventsEl.scrollHeight);
        await this._sleep(1000);
    }

    _closeTraceAdmin() {
        const modal = document.getElementById('trace-admin-modal');
        if (!modal) return;
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
    }

    async _loadTraceSessions() {
        const res = await fetch('/api/admin/traces/sessions');
        const data = await res.json();
        this._traceSessions = Array.isArray(data.sessions) ? data.sessions : [];
        const preferredSession = this._selectedTraceSession || data.current_session_id || '';
        const nextSession = this._traceSessions.find((item) => item.session_id === preferredSession)?.session_id
            || this._traceSessions[0]?.session_id
            || '';
        this._selectedTraceSession = nextSession;
        this._renderTraceSessions();
        if (nextSession) {
            await this._loadTraceRuns(nextSession);
        } else {
            this._traceRuns = [];
            this._selectedTraceRun = '';
            this._renderTraceRuns();
            this._renderTraceDetail(null);
        }
    }

    async _loadTraceRuns(sessionId) {
        const res = await fetch(`/api/admin/traces/session/${encodeURIComponent(sessionId)}`);
        const data = await res.json();
        this._traceRuns = Array.isArray(data.runs) ? data.runs : [];
        const nextRun = this._traceRuns.find((item) => item.run_id === this._selectedTraceRun)?.run_id
            || this._traceRuns[0]?.run_id
            || '';
        this._selectedTraceRun = nextRun;
        this._renderTraceRuns();
        if (nextRun) {
            await this._loadTraceDetail(nextRun);
        } else {
            this._renderTraceDetail(null);
        }
    }

    async _loadTraceDetail(runId) {
        const res = await fetch(`/api/admin/traces/run/${encodeURIComponent(runId)}`);
        if (!res.ok) {
            this._renderTraceDetail(null);
            return;
        }
        const data = await res.json();
        this._renderTraceDetail(data);
    }

    _renderTraceSessions() {
        const container = document.getElementById('trace-session-list');
        if (!container) return;
        if (!this._traceSessions.length) {
            container.innerHTML = '<div class="settings-empty">No traced sessions yet.</div>';
            return;
        }
        container.innerHTML = this._traceSessions.map((session) => {
            const active = session.session_id === this._selectedTraceSession;
            const updated = session.updated_at ? new Date(session.updated_at * 1000).toLocaleString() : 'unknown';
            return `
                <button class="trace-admin-item${active ? ' is-active' : ''}" data-trace-session="${this._escapeHtml(session.session_id)}">
                    <div class="trace-admin-item-title">${session.active ? 'Current' : 'Session'} ${this._escapeHtml((session.session_id || '').slice(0, 12) || 'unknown')}</div>
                    <div class="trace-admin-item-copy">
                        generation ${session.generation || 1} · turns ${session.user_turns || 0} · runs ${session.run_count || 0}<br>
                        updated ${this._escapeHtml(updated)}
                    </div>
                </button>
            `;
        }).join('');
        container.querySelectorAll('[data-trace-session]').forEach((button) => {
            button.addEventListener('click', async () => {
                this._selectedTraceSession = button.dataset.traceSession || '';
                this._selectedTraceRun = '';
                this._renderTraceSessions();
                await this._loadTraceRuns(this._selectedTraceSession);
            });
        });
    }

    _renderTraceRuns() {
        const container = document.getElementById('trace-run-list');
        if (!container) return;
        if (!this._traceRuns.length) {
            container.innerHTML = '<div class="settings-empty">No runs recorded for this session.</div>';
            return;
        }
        container.innerHTML = this._traceRuns.map((run) => {
            const active = run.run_id === this._selectedTraceRun;
            const outcome = run.success === true ? 'success' : (run.success === false ? 'failure' : 'unknown');
            return `
                <button class="trace-admin-item${active ? ' is-active' : ''}" data-trace-run="${this._escapeHtml(run.run_id)}">
                    <div class="trace-admin-item-title">${this._escapeHtml((run.topology_id || 'unknown').replaceAll('_', ' '))}</div>
                    <div class="trace-admin-item-copy">
                        run ${(run.run_id || '').slice(0, 12)} · ${outcome}<br>
                        events ${run.event_count || 0} · agents ${run.agent_count || 0}
                    </div>
                </button>
            `;
        }).join('');
        container.querySelectorAll('[data-trace-run]').forEach((button) => {
            button.addEventListener('click', async () => {
                this._selectedTraceRun = button.dataset.traceRun || '';
                this._renderTraceRuns();
                await this._loadTraceDetail(this._selectedTraceRun);
            });
        });
    }

    _renderTraceDetail(payload) {
        const summaryEl = document.getElementById('trace-detail-summary');
        const eventsEl = document.getElementById('trace-detail-events');
        const jsonEl = document.getElementById('trace-detail-json');
        if (!summaryEl || !eventsEl || !jsonEl) return;
        if (!payload || !payload.summary || !payload.trace) {
            summaryEl.innerHTML = '<div class="settings-empty">Select a run to inspect its trace.</div>';
            eventsEl.innerHTML = '';
            jsonEl.textContent = '';
            return;
        }
        const summary = payload.summary;
        const outcome = summary.success === true ? 'success' : (summary.success === false ? 'failure' : 'unknown');
        const agentPills = (summary.agent_ids || []).length
            ? (summary.agent_ids || []).map((id) => `<span class="trace-detail-pill">${this._escapeHtml(id)}</span>`).join('')
            : '<span class="trace-detail-pill">none</span>';
        summaryEl.innerHTML = `
            <div class="trace-detail-grid">
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Session</div>
                    <div class="trace-detail-value"><code>${this._escapeHtml(summary.session_id || 'unknown')}</code></div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Run</div>
                    <div class="trace-detail-value"><code>${this._escapeHtml(summary.run_id || 'unknown')}</code></div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Topology</div>
                    <div class="trace-detail-value">${this._escapeHtml(summary.topology_id || 'unknown')}</div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Outcome</div>
                    <div class="trace-detail-value">${this._escapeHtml(outcome)} · ${summary.event_count || 0} events · ${(summary.duration_s || 0).toFixed(2)}s</div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Task Type</div>
                    <div class="trace-detail-value">${this._escapeHtml(summary.task_type || 'unknown')}</div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Routing</div>
                    <div class="trace-detail-value">${this._escapeHtml(summary.routing_mode || 'unknown')}</div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Classifier Model</div>
                    <div class="trace-detail-value">${this._escapeHtml(this._shortModel(summary.classifier_model || '') || 'unknown')}</div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Escalation</div>
                    <div class="trace-detail-value">${summary.escalation_allowed ? this._escapeHtml(summary.escalation_reason || 'recommended') : 'not recommended'}</div>
                </div>
                <div class="trace-detail-card">
                    <div class="trace-detail-label">Early Stop</div>
                    <div class="trace-detail-value">${summary.stop_early_allowed ? this._escapeHtml(summary.stop_early_reason || 'eligible') : 'not recommended'}</div>
                </div>
                <div class="trace-detail-card trace-detail-card-wide">
                    <div class="trace-detail-label">Agents</div>
                    <div class="trace-detail-value trace-detail-pills">${agentPills}</div>
                </div>
                <div class="trace-detail-card trace-detail-card-wide">
                    <div class="trace-detail-label">Routing Reason</div>
                    <div class="trace-detail-value">${this._escapeHtml(summary.routing_reason || 'n/a')}</div>
                </div>
                <div class="trace-detail-card trace-detail-card-wide">
                    <div class="trace-detail-label">Trace File</div>
                    <div class="trace-detail-value"><code>${this._escapeHtml(payload.path || '')}</code></div>
                </div>
            </div>
        `;
        const stageLatencies = Object.entries(summary.stage_latencies || {});
        const tokenUsage = Object.entries(summary.token_usage_by_agent || {});
        const eventRows = Array.isArray(payload.trace.events) ? payload.trace.events : [];
        eventsEl.innerHTML = `
            <section class="trace-detail-section">
                <div class="trace-detail-section-title">Stage Latencies</div>
                <div class="trace-detail-stat-list">
                    ${stageLatencies.length
                        ? stageLatencies.map(([stage, value]) => `
                            <div class="trace-detail-stat-row">
                                <span class="trace-detail-stat-name">${this._escapeHtml(stage)}</span>
                                <span class="trace-detail-stat-value">${Number(value || 0).toFixed(2)}s</span>
                            </div>
                        `).join('')
                        : '<div class="trace-detail-empty">No stage timings recorded.</div>'}
                </div>
            </section>
            <section class="trace-detail-section">
                <div class="trace-detail-section-title">Token Usage</div>
                <div class="trace-detail-stat-list">
                    ${tokenUsage.length
                        ? tokenUsage.map(([agent, usage]) => `
                            <div class="trace-detail-stat-row">
                                <span class="trace-detail-stat-name">${this._escapeHtml(agent)}</span>
                                <span class="trace-detail-stat-value">${this._escapeHtml(Object.entries(usage || {}).map(([k, v]) => `${k}=${v}`).join(' · ') || 'none')}</span>
                            </div>
                        `).join('')
                        : '<div class="trace-detail-empty">No token usage recorded.</div>'}
                </div>
            </section>
            <section class="trace-detail-section">
                <div class="trace-detail-section-title">Event Timeline</div>
                <div class="trace-detail-event-list">
                    ${eventRows.length
                        ? eventRows.map((event) => this._renderTraceEventRow(event)).join('')
                        : '<div class="trace-detail-empty">No events recorded.</div>'}
                </div>
            </section>
        `;
        jsonEl.textContent = JSON.stringify(payload.trace, null, 2);
    }

    _renderTraceEventRow(event) {
        const kind = event?.kind || 'unknown';
        const actor = event?.actor || '';
        const target = event?.target || '';
        const stage = event?.stage || '';
        const status = event?.status || '';
        const timestamp = event?.timestamp ? new Date(event.timestamp * 1000).toLocaleTimeString() : '';
        const data = event?.data && typeof event.data === 'object' ? event.data : {};
        const meta = [actor ? `actor=${actor}` : '', target ? `target=${target}` : '', stage ? `stage=${stage}` : '', status ? `status=${status}` : '']
            .filter(Boolean)
            .join(' · ');
        const detailParts = [];
        if (event?.message) detailParts.push(String(event.message));
        if (Object.keys(data).length) {
            const compact = Object.entries(data)
                .slice(0, 4)
                .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
                .join(' · ');
            if (compact) detailParts.push(compact);
        }
        return `
            <div class="trace-detail-event">
                <div class="trace-detail-event-main">
                    <div class="trace-detail-event-kind">${this._escapeHtml(kind)}</div>
                    ${meta ? `<div class="trace-detail-event-copy">${this._escapeHtml(meta)}</div>` : ''}
                    ${detailParts.length ? `<div class="trace-detail-event-copy">${this._escapeHtml(detailParts.join(' | '))}</div>` : ''}
                </div>
                <div class="trace-detail-event-meta">${this._escapeHtml(timestamp)}</div>
            </div>
        `;
    }

    async _loadTopologyOptions() {
        const res = await fetch('/api/topologies');
        const data = await res.json();
        this._topologyList = data.topologies || [];
        this._renderTopologyMenu();
        this._updateTopologyTrigger();
    }

    _renderTopologyMenu() {
        const menu = document.getElementById('topology-menu');
        menu.innerHTML = '';

        // Auto option
        const autoItem = this._createTopologyMenuItem({
            id: 'auto',
            label: 'Auto',
            description: 'Let the runtime choose based on task complexity',
            agents: [],
        });
        menu.appendChild(autoItem);

        for (const t of this._topologyList) {
            menu.appendChild(this._createTopologyMenuItem(t));
        }
    }

    _createTopologyMenuItem(t) {
        const item = document.createElement('div');
        item.className = 'topo-menu-item' + (t.id === this._selectedTopology ? ' active' : '');

        const header = document.createElement('div');
        header.className = 'topo-menu-item-header';

        const check = document.createElement('span');
        check.className = 'topo-menu-item-check';
        check.textContent = t.id === this._selectedTopology ? '\u2713' : '';
        header.appendChild(check);

        const label = document.createElement('span');
        label.className = 'topo-menu-item-label';
        label.textContent = t.label;
        header.appendChild(label);

        if (t.agents && t.agents.length > 0) {
            const count = document.createElement('span');
            count.className = 'topo-menu-item-agents';
            count.textContent = `${t.agents.length} agents`;
            header.appendChild(count);
        }

        item.appendChild(header);

        if (t.description) {
            const desc = document.createElement('div');
            desc.className = 'topo-menu-item-desc';
            desc.textContent = t.description;
            item.appendChild(desc);
        }

        item.addEventListener('click', () => {
            if (this._isRunActive) return;
            this._selectedTopology = t.id;
            this._userOverrodeTopology = t.id !== 'auto';
            this._renderTopologyMenu();
            this._updateTopologyTrigger();
            document.getElementById('topology-dropdown').classList.remove('open');
            document.getElementById('topology-menu').classList.add('hidden');
        });

        return item;
    }

    _updateTopologyTrigger() {
        const label = document.getElementById('topology-trigger-label');
        const count = document.getElementById('topology-trigger-count');
        const statEl = document.getElementById('stat-topology');

        if (this._selectedTopology === 'auto') {
            label.textContent = 'Auto';
            count.textContent = '';
            statEl.textContent = 'Auto';
            this._setText('hero-topology-label', 'Auto');
        } else {
            const topo = this._topologyList.find(t => t.id === this._selectedTopology);
            if (topo) {
                label.textContent = topo.label;
                count.textContent = `\u2014 ${topo.agents.length} agents`;
                statEl.textContent = topo.label;
                this._setText('hero-topology-label', topo.label);
            } else {
                label.textContent = this._selectedTopology;
                count.textContent = '';
                statEl.textContent = this._selectedTopology;
                this._setText('hero-topology-label', this._selectedTopology);
            }
        }

        // Disable trigger while running
        const trigger = document.getElementById('topology-trigger');
        trigger.classList.toggle('disabled', this._isRunActive);
        this._refreshMentionTargets();
    }

    _toggleTopologyMenu() {
        if (this._isRunActive) return;
        if (this._sessionLock?.topology) return;
        const dd = document.getElementById('topology-dropdown');
        const menu = document.getElementById('topology-menu');
        if (!dd || !menu) return;

        if (dd.classList.contains('open')) {
            dd.classList.remove('open');
            menu.classList.add('hidden');
        } else {
            // Re-render to pick up any hot-reloaded changes
            this._renderTopologyMenu();

            // Add warning banner if running
            if (this._isRunActive) {
                const warn = document.createElement('div');
                warn.className = 'topo-menu-warning';
                warn.innerHTML = '\u26a0 Cannot switch topology while agents are running';
                menu.prepend(warn);
            }

            dd.classList.add('open');
            menu.classList.remove('hidden');
            this._positionTopologyMenu();
        }
    }

    _positionTopologyMenu() {
        const dd = document.getElementById('topology-dropdown');
        const menu = document.getElementById('topology-menu');
        if (!dd || !menu || menu.classList.contains('hidden')) return;

        dd.classList.remove('open-up');
        menu.style.maxHeight = '';
        menu.style.left = '0px';
        menu.style.right = 'auto';

        const dropdownRect = dd.getBoundingClientRect();
        const menuRect = menu.getBoundingClientRect();
        const viewportHeight = window.innerHeight;
        const spaceBelow = viewportHeight - dropdownRect.bottom - 16;
        const spaceAbove = dropdownRect.top - 16;
        const shouldOpenUp = menuRect.height > spaceBelow && spaceAbove > spaceBelow;
        const available = Math.max(160, shouldOpenUp ? spaceAbove - 10 : spaceBelow - 10);

        dd.classList.toggle('open-up', shouldOpenUp);
        menu.style.maxHeight = `${Math.min(available, Math.max(220, viewportHeight * 0.52))}px`;

        const positionedRect = menu.getBoundingClientRect();
        const overflowRight = positionedRect.right - window.innerWidth + 16;
        const overflowLeft = 16 - positionedRect.left;
        if (overflowRight > 0) {
            menu.style.left = `${-overflowRight}px`;
        } else if (overflowLeft > 0) {
            menu.style.left = `${overflowLeft}px`;
        }
    }

    _setRunActive(active) {
        this._isRunActive = active;
        const send = document.getElementById('query-send');
        const sendLabel = send?.querySelector('.query-action-label');
        const input = document.getElementById('query-input');
        const newSessionButton = document.getElementById('new-session-button');
        if (send) {
            send.classList.toggle('is-stop', active);
            send.title = active ? 'Stop run' : 'Run (Enter)';
            send.disabled = false;
        }
        if (sendLabel) sendLabel.textContent = active ? 'Stop' : 'Execute';
        input.disabled = false;
        if (newSessionButton) newSessionButton.disabled = active;
        input.placeholder = active
            ? 'Send a follow-up or use @node to direct a message…'
            : 'Describe a task for the agents…';
        if (!active) this._hideLoader();
        this._setText('hero-status-label', active ? 'Running' : this._statusText());
        this.graph.setRunState(active ? 'running' : (this._statusText() === 'Done' ? 'completed' : 'idle'));

        // Close and disable/enable topology dropdown
        document.getElementById('topology-trigger').classList.toggle('disabled', active);
        if (active) {
            document.getElementById('topology-dropdown').classList.remove('open');
            document.getElementById('topology-menu').classList.add('hidden');
        }
        this._updateMentionSuggestions();
    }

    _handlePrimaryQueryAction() {
        if (this._isRunActive) {
            this._stopRun();
            return;
        }
        this._submitQuery();
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

        if (this._isRunActive) {
            input.value = '';
            input.style.height = 'auto';
            this._hideMentionSuggestions();
            await fetch('/api/inject', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ to: 'coordinator', message: query }),
            });
            return;
        }

        this._setRunActive(true);

        // Clear previous run state
        this._clearThinking();
        this._runModelShown = false;
        this._rawMessages = [];
        this._agentActivityLines = {};
        this._plan = null;
        this._planSteps = [];
        this.messageLog.innerHTML = '';
        if (this.changesLog) this.changesLog.innerHTML = '';
        this._setChangesEmptyState('Waiting for the run to finish so Orb can summarize the overall code changes.');
        this._setMessageEmptyState('Waiting for the runtime to build the graph and emit activity.');
        this._renderPlanningState();
        this.agents = {};
        this.graph.setTopology([], [], null);
        document.getElementById('node-detail-panel').classList.add('ndp-closed');
        document.getElementById('communications-panel')?.classList.remove('node-panel-active');
        const emptyInspector = document.getElementById('node-detail-empty');
        if (emptyInspector) emptyInspector.classList.remove('hidden');
        this.selectedAgent = null;
        this.graph.selectedNode = null;
        document.getElementById('result-panel').classList.add('hidden');
        document.getElementById('question-panel').classList.add('hidden');
        this._handleStats({ message_count: 0, budget_remaining: 200, elapsed: 0 });
        const statusEl = document.getElementById('stat-status');

        statusEl.textContent = 'Starting…';
        statusEl.className = 'stat-value status-running';
        this._setText('hero-status-label', 'Starting');
        this._showLoader('Starting runtime…');
        this._lastQuery = query;
        this._fileChanges = new Map();
        input.value = '';
        input.style.height = 'auto';
        this._hideMentionSuggestions();
        let res, data;
        try {
            // When the current session already has a topology, reuse it for
            // follow-ups so we don't re-classify and rebuild the graph mid-session.
            const sessionTopology = this._plan?.topology?.id;
            const topologyForStart = this._userOverrodeTopology
                ? this._selectedTopology
                : (sessionTopology && sessionTopology !== 'auto' ? sessionTopology : this._selectedTopology);
            const startBody = {
                query,
                topology: topologyForStart,
                model: this._selectedModel,
            };
            // First-run manual config: pass through workdir + agent_models
            // if the user set them via the Session modal and the session is
            // not yet locked. After the first successful run the server
            // persists the lock on the session.
            const sc = this._sessionConfig || {};
            if (!this._sessionLock?.topology) {
                if (sc.workdir) startBody.workdir = sc.workdir;
                if (sc.topology && sc.topology !== 'auto') {
                    startBody.topology = sc.topology;
                    if (sc.agentModels && Object.keys(sc.agentModels).length) {
                        startBody.agent_models = { ...sc.agentModels };
                    }
                }
            }
            res = await fetch('/api/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(startBody),
            });
            const text = await res.text();
            try { data = JSON.parse(text); }
            catch { data = { ok: false, error: `HTTP ${res.status}: ${text.slice(0, 300)}` }; }
        } catch (err) {
            data = { ok: false, error: `Network error: ${err?.message || err}` };
        }
        if (!data.ok) {
            this._setRunActive(false);
            this._hideLoader();
            document.getElementById('result-agent').textContent = 'Error';
            document.getElementById('result-elapsed').textContent = '';
            document.getElementById('result-body').textContent = data.error || `Failed to start run (HTTP ${res?.status || '?'}).`;
            document.getElementById('result-panel').classList.remove('hidden');
            return;
        }
        if (data.session_id) {
            this._updateSessionUrl(data.session_id);
        }
        if (data.init && !this._shouldIgnoreStartSnapshot(data.init)) {
            this._handleInit(data.init);
        }
    }

    _shouldIgnoreStartSnapshot(snapshot) {
        if (!snapshot || typeof snapshot !== 'object') return false;
        const snapshotSessionId = String(snapshot.session_id || '');
        const currentSessionId = String(this._wsSessionId || '');
        const sameSession = snapshotSessionId && currentSessionId && snapshotSessionId === currentSessionId;
        const hasLiveProgress = Boolean(
            (this._planSteps && this._planSteps.length) ||
            (this._rawMessages && this._rawMessages.length) ||
            (this.messageLog && this.messageLog.querySelector('.msg-entry, .prediction-card')) ||
            Object.keys(this.agents || {}).length
        );
        return sameSession && hasLiveProgress;
    }

    async _createNewSession() {
        if (this._isRunActive) return;
        const button = document.getElementById('new-session-button');
        const previousLabel = button?.textContent || 'New Session';
        if (button) {
            button.disabled = true;
            button.textContent = 'Starting…';
        }
        try {
            const res = await fetch('/api/session/new', { method: 'POST' });
            let data = null;
            try {
                data = await res.json();
            } catch (_err) {
                data = { ok: false, error: `New session endpoint returned ${res.status}. The daemon may need a restart.` };
            }
            if (!data.ok) {
                document.getElementById('result-agent').textContent = 'Session Error';
                document.getElementById('result-elapsed').textContent = '';
                document.getElementById('result-body').textContent = data.error || 'Failed to start a new session.';
                document.getElementById('result-panel').classList.remove('hidden');
                return;
            }
            this._resetForSession(data.init || {});
        } finally {
            if (button) {
                button.textContent = previousLabel;
                button.disabled = this._isRunActive;
            }
        }
    }

    // ── Stop run ──────────────────────────────────────────────

    async _stopRun() {
        const btn = document.getElementById('query-send');
        const label = btn?.querySelector('.query-action-label');
        btn.disabled = true;
        if (label) label.textContent = 'Stopping…';
        await fetch('/api/stop', { method: 'POST' });
    }

    _handleStopped() {
        this._clearThinking();
        this._setRunActive(false);
        this.graph.setRunState('idle');
        const el = document.getElementById('stat-status');
        el.textContent = 'Stopped';
        el.className = 'stat-value status-error';
        this._setText('hero-status-label', 'Stopped');
        const btn = document.getElementById('query-send');
        const label = btn?.querySelector('.query-action-label');
        btn.disabled = false;
        if (label) label.textContent = 'Execute';
    }

    _handleRunComplete(data) {
        this._clearThinking();
        this._setRunActive(false);
        this.graph.setRunState('completed');
        this._updateSessionUrl(data.session_id || '');

        // Force status to Done
        const statusEl = document.getElementById('stat-status');
        statusEl.textContent = 'Done';
        statusEl.className = 'stat-value status-done';
        this._setText('hero-status-label', 'Done');

        const result = data.result || '';
        const elapsed = data.elapsed !== undefined ? data.elapsed.toFixed(1) + 's' : '';
        const sessionTurn = data.session_turn || 0;
        this._updateWorkdir(
            this._plan?.workdir || '',
            data.session_id || '',
            data.session_generation || 1,
            sessionTurn,
        );

        const diff = data.diff || '';
        this._renderInitialCodeChanges(result, diff, elapsed, sessionTurn);
        this._addRunCompleteActivity({
            agent: data.agent || 'run',
            elapsed: data.elapsed || 0,
            result,
        });

        // V2: surface the final result in the drawer's result panel since #changes-log is hidden
        const resAgentEl = document.getElementById('result-agent');
        const resElapsedEl = document.getElementById('result-elapsed');
        const resBodyEl = document.getElementById('result-body');
        const resPanel = document.getElementById('result-panel');
        if (resAgentEl) resAgentEl.textContent = this._roleDisplayName(data.agent, data.agent || 'run');
        if (resElapsedEl) resElapsedEl.textContent = elapsed;
        if (resBodyEl) resBodyEl.innerHTML = this._renderResult(result);
        if (resPanel) resPanel.classList.remove('hidden');
    }

    _renderInitialCodeChanges(result, diff, elapsed = '', sessionTurn = 0) {
        if (!this.changesLog) return;
        const empty = this.changesLog.querySelector('.empty-state');
        if (empty) empty.remove();

        const followUpHint = sessionTurn > 0
            ? `<span class="followup-hint">↩ type a follow-up to continue this session</span>`
            : '';
        const el = document.createElement('div');
        el.className = 'final-result-card final-result-card-complete';
        const diffFiles = diff ? this._parseDiffFiles(diff) : [];
        el.innerHTML = `
            <div class="final-result-header">
                <span class="final-result-title">✓ Run Complete</span>
                ${elapsed ? `<span class="final-result-elapsed">${elapsed}</span>` : ''}
                ${followUpHint}
                <button class="final-result-copy">Copy result</button>
            </div>
            <div class="final-result-body">${this._renderResult(result)}</div>
            ${diffFiles.length ? `
            <div class="diff-section">
                <div class="diff-section-header">
                    <span>Files Changed</span>
                </div>
                <div class="code-change-list">
                    ${diffFiles.map((file) => this._renderCodeChangeCard(file)).join('')}
                </div>
            </div>` : ''}
        `;
        el.querySelector('.final-result-copy').addEventListener('click', () => {
            navigator.clipboard.writeText(result).then(() => {
                const btn = el.querySelector('.final-result-copy');
                btn.textContent = 'Copied!';
                setTimeout(() => { btn.textContent = 'Copy result'; }, 1500);
            });
        });
        this.changesLog.appendChild(el);
        this.changesLog.scrollTop = this.changesLog.scrollHeight;
    }

    _hydrateCodeChangesFromInit(data) {
        if (!this.changesLog) return;
        if (this.changesLog.querySelector('.final-result-card')) return;
        const result = data.final_result || '';
        const diff = data.final_diff || '';
        if (!result && !diff) return;
        this._renderInitialCodeChanges(result, diff, '', data.session_turn || 0);
    }

    _hydrateLiveCodeChangesFromInit(data) {
        const fileChanges = Array.isArray(data.file_changes) ? data.file_changes : [];
        if (!fileChanges.length) return;
        this._fileChanges = new Map(
            fileChanges.map((file) => [file.path, {
                path: file.path,
                agent: file.agent || '',
                content: file.content || '',
                oldContent: file.old_content || '',
            }])
        );
        this._renderLiveCodeChanges();
    }

    _resetForSession(data) {
        this._closeSettings();
        this._hideLoader();
        this._clearThinking();
        this._rawMessages = [];
        this._agentActivityLines = {};
        this._plan = null;
        this._planSteps = [];
        this._fileChanges = new Map();
        this.agents = {};
        this.selectedAgent = null;
        this._isRunActive = false;
        this.messageLog.innerHTML = '';
        if (this.changesLog) this.changesLog.innerHTML = '';
        this._setChangesEmptyState();
        this._setMessageEmptyState();
        this._handleStats({ message_count: 0, budget_remaining: 200, elapsed: 0 });
        const statusEl = document.getElementById('stat-status');
        if (statusEl) {
            statusEl.textContent = 'Waiting';
            statusEl.className = 'stat-value status-waiting';
        }
        document.getElementById('result-panel').classList.add('hidden');
        document.getElementById('question-panel').classList.add('hidden');
        document.getElementById('node-detail-panel').classList.add('ndp-closed');
        document.getElementById('communications-panel')?.classList.remove('node-panel-active');
        const emptyInspector = document.getElementById('node-detail-empty');
        if (emptyInspector) emptyInspector.classList.remove('hidden');
        this.graph.selectedNode = null;
        this.graph.setTopology([], [], null);
        this.graph.setRunState('idle');
        this._selectedTopology = 'auto';
        this._updateTopologyTrigger();
        this._updateWorkdir('', data.session_id || '', data.session_generation || 1, data.session_turn || 0);
        this._setText('hero-status-label', 'Waiting');
        this._setText('hero-topology-label', 'Auto');
        document.getElementById('stat-topology').textContent = 'Auto';
        this._updateSessionUrl(data.session_id || '');
    }

    _renderLiveCodeChanges() {
        const files = Array.from(this._fileChanges.values()).map((f) => {
            const diff = this._buildUnifiedDiff(f.path, f.oldContent, f.content);
            const stats = this._countDiffStats(diff);
            const status = (f.oldContent || '').length === 0 ? 'A' : 'M';
            return { ...f, diff, added: stats.added, removed: stats.removed, status };
        });

        this._renderRepoTree(files);
        this._renderRepoDiff(files);
        this._renderRepoToolbar(files);
        this._updateFilesStat(files);
    }

    _renderRepoTree(files) {
        const host = document.getElementById('repo-tree');
        if (!host) return;
        if (files.length === 0) {
            host.innerHTML = `<div id="repo-tree-empty" class="repo-empty">No file writes yet. Start a task to see diffs here.</div>`;
            return;
        }
        // Group by top-level folder (or "(root)")
        const groups = new Map();
        for (const f of files) {
            const slash = f.path.indexOf('/');
            const folder = slash >= 0 ? f.path.slice(0, slash + 1) : '';
            if (!groups.has(folder)) groups.set(folder, []);
            groups.get(folder).push(f);
        }
        const sortedFolders = Array.from(groups.keys()).sort((a, b) => a.localeCompare(b));
        const selected = this._repoSelectedPath || files[0].path;
        this._repoSelectedPath = selected;

        host.innerHTML = sortedFolders.map((folder) => {
            const items = groups.get(folder) || [];
            items.sort((a, b) => a.path.localeCompare(b.path));
            const label = folder || '(root)';
            return `
                <div class="repo-folder">${this._escapeHtml(label)}</div>
                ${items.map((f) => {
                    const shortName = folder ? f.path.slice(folder.length) || f.path : f.path;
                    const role = (this._roleOf(f.agent) || 'generic').toLowerCase();
                    const sel = f.path === selected ? ' sel' : '';
                    return `
                        <div class="repo-file${sel}" data-path="${this._escapeAttr(f.path)}" title="${this._escapeAttr(f.path)}">
                            <span class="badge ${f.status}">${f.status}</span>
                            <div class="main">
                                <span class="pth">${this._escapeHtml(shortName)}</span>
                                <span class="byline">
                                    <span class="byline-author v2-role-${this._escapeAttr(role)}"><span class="role-dot"></span>${this._escapeHtml(this._roleDisplayName(f.agent, f.agent))}</span>
                                    <span class="sep">·</span>
                                    <span class="stat-add">+${f.added || 0}</span>
                                    ${(f.removed || 0) > 0 ? `<span class="stat-del">−${f.removed}</span>` : ''}
                                </span>
                            </div>
                        </div>
                    `;
                }).join('')}
            `;
        }).join('');

        host.querySelectorAll('.repo-file').forEach((row) => {
            row.addEventListener('click', () => {
                this._repoSelectedPath = row.dataset.path;
                this._renderLiveCodeChanges();
            });
        });
    }

    _renderRepoDiff(files) {
        const filebar = document.getElementById('repo-diff-filebar');
        const pathEl = document.getElementById('repo-diff-path');
        const addEl = document.getElementById('repo-diff-add');
        const delEl = document.getElementById('repo-diff-del');
        const badgeEl = document.getElementById('repo-diff-status');
        const authorEl = document.getElementById('repo-diff-author');
        const diffHost = document.getElementById('repo-diff');
        if (!diffHost) return;

        if (files.length === 0) {
            if (filebar) filebar.classList.add('hidden');
            diffHost.innerHTML = `<div id="repo-diff-empty" class="repo-empty">Select a file from the tree to see its diff.</div>`;
            return;
        }
        const selected = files.find((f) => f.path === this._repoSelectedPath) || files[0];
        this._repoSelectedPath = selected.path;

        if (filebar) filebar.classList.remove('hidden');
        if (pathEl) pathEl.textContent = selected.path;
        if (addEl) addEl.textContent = `+${selected.added || 0}`;
        if (delEl) { delEl.textContent = `−${selected.removed || 0}`; delEl.style.display = (selected.removed || 0) > 0 ? '' : 'none'; }
        if (badgeEl) { badgeEl.textContent = selected.status; badgeEl.className = `repo-diff-badge ${selected.status}`; }
        if (authorEl) {
            const role = (this._roleOf(selected.agent) || 'generic').toLowerCase();
            authorEl.className = `diff-gutter-author v2-role-${role}`;
            authorEl.innerHTML = `<span class="role-dot"></span>${this._escapeHtml(this._roleDisplayName(selected.agent, selected.agent))}`;
        }

        if (this._repoView === 'raw') {
            diffHost.innerHTML = `<pre class="repo-raw" style="padding:12px 16px; margin:0; color:var(--text-secondary);">${this._escapeHtml(selected.content || '')}</pre>`;
            return;
        }
        diffHost.innerHTML = this._renderV2Diff(selected);
    }

    _renderV2Diff(file) {
        const oldLines = (file.oldContent || '').split('\n');
        const newLines = (file.content || '').split('\n');
        const ops = this._diffLineOps(oldLines, newLines);
        const rows = [];
        rows.push(`<div class="diff-hunk-hdr"><span class="at">@@ ${file.status === 'A' ? 'new file' : 'modified'} @@</span><span>${this._escapeHtml(file.path)}</span></div>`);
        let oldLn = 0, newLn = 0;
        for (const op of ops) {
            if (op.type === 'equal') {
                oldLn++; newLn++;
                rows.push(`<div class="diff-line ctx"><span class="ln">${oldLn}</span><span class="ln">${newLn}</span><span class="code">${this._escapeHtml(op.line)}</span></div>`);
            } else if (op.type === 'add') {
                newLn++;
                rows.push(`<div class="diff-line add"><span class="ln"></span><span class="ln">${newLn}</span><span class="code">${this._escapeHtml(op.line)}</span></div>`);
            } else {
                oldLn++;
                rows.push(`<div class="diff-line del"><span class="ln">${oldLn}</span><span class="ln"></span><span class="code">${this._escapeHtml(op.line)}</span></div>`);
            }
        }
        return rows.join('');
    }

    _renderRepoToolbar(files) {
        const countEl = document.getElementById('repo-panel-count');
        const addEl = document.getElementById('repo-stat-add');
        const delEl = document.getElementById('repo-stat-del');
        const contribEl = document.getElementById('repo-contrib');
        const branchEl = document.getElementById('repo-branch');
        const totalAdd = files.reduce((s, f) => s + (f.added || 0), 0);
        const totalDel = files.reduce((s, f) => s + (f.removed || 0), 0);
        const authors = new Set(files.map((f) => f.agent).filter(Boolean));
        if (countEl) countEl.textContent = `${files.length} file${files.length === 1 ? '' : 's'}`;
        if (addEl) addEl.textContent = `+${totalAdd}`;
        if (delEl) delEl.textContent = `−${totalDel}`;
        if (contribEl) contribEl.textContent = `${authors.size} agent${authors.size === 1 ? '' : 's'} contributed`;
        if (branchEl) {
            const short = (this._wsSessionId || '').slice(0, 6);
            branchEl.textContent = short ? `orb/run_${short}` : 'orb/run';
        }
    }

    _updateFilesStat(files) {
        const el = document.getElementById('stat-files');
        const sub = document.getElementById('stat-files-sub');
        if (el) el.textContent = String(files.length);
        if (sub) {
            const add = files.reduce((s, f) => s + (f.added || 0), 0);
            const del = files.reduce((s, f) => s + (f.removed || 0), 0);
            sub.textContent = `+${add} −${del} loc`;
        }
    }

    _roleOf(agentId) {
        if (!agentId) return 'generic';
        const agent = this.agents[agentId];
        if (agent?.role) return agent.role;
        return agentId;
    }

    _renderCodeChangeCard(file) {
        const added = file.added || 0;
        const removed = file.removed || 0;
        const total = added + removed;
        return `
            <details class="code-change-card"${total <= 40 ? ' open' : ''}>
                <summary>
                    <div class="code-change-summary">
                        <div class="code-change-title-row">
                            <span class="diff-file">${this._escapeHtml(file.path)}</span>
                            ${file.agent ? `<span class="live-diff-agent">${this._escapeHtml(file.agent)}</span>` : ''}
                        </div>
                        <div class="code-change-meta">
                            <span class="code-change-stat code-change-stat-add">+${added}</span>
                            <span class="code-change-stat code-change-stat-del">-${removed}</span>
                        </div>
                    </div>
                </summary>
                <pre class="diff-body">${this._renderDiff(file.diff || '')}</pre>
            </details>
        `;
    }

    _buildUnifiedDiff(path, oldContent, newContent) {
        const oldLines = (oldContent || '').split('\n');
        const newLines = (newContent || '').split('\n');
        const ops = this._diffLineOps(oldLines, newLines);
        const body = ops.map((op) => {
            const line = op.line;
            if (op.type === 'equal') return ` ${line}`;
            if (op.type === 'add') return `+${line}`;
            return `-${line}`;
        }).join('\n');
        return `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@\n${body}`.trim();
    }

    _parseDiffFiles(diff) {
        const files = [];
        let current = null;
        for (const line of diff.split('\n')) {
            if (line.startsWith('diff --git ')) {
                if (current) {
                    current.diff = current.lines.join('\n');
                    Object.assign(current, this._countDiffStats(current.diff));
                    delete current.lines;
                    files.push(current);
                }
                const match = line.match(/ b\/(.+)$/);
                current = {
                    path: match?.[1] || 'unknown',
                    agent: '',
                    lines: [line],
                };
                continue;
            }
            if (current) current.lines.push(line);
        }
        if (current) {
            current.diff = current.lines.join('\n');
            Object.assign(current, this._countDiffStats(current.diff));
            delete current.lines;
            files.push(current);
        }
        return files;
    }

    _countDiffStats(diff) {
        let added = 0;
        let removed = 0;
        for (const line of diff.split('\n')) {
            if (line.startsWith('+') && !line.startsWith('+++')) added += 1;
            if (line.startsWith('-') && !line.startsWith('---')) removed += 1;
        }
        return { added, removed };
    }

    _diffLineOps(oldLines, newLines) {
        const m = oldLines.length;
        const n = newLines.length;
        const dp = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0));

        for (let i = m - 1; i >= 0; i--) {
            for (let j = n - 1; j >= 0; j--) {
                dp[i][j] = oldLines[i] === newLines[j]
                    ? dp[i + 1][j + 1] + 1
                    : Math.max(dp[i + 1][j], dp[i][j + 1]);
            }
        }

        const ops = [];
        let i = 0;
        let j = 0;
        while (i < m && j < n) {
            if (oldLines[i] === newLines[j]) {
                ops.push({ type: 'equal', line: oldLines[i] });
                i += 1;
                j += 1;
            } else if (dp[i + 1][j] >= dp[i][j + 1]) {
                ops.push({ type: 'remove', line: oldLines[i] });
                i += 1;
            } else {
                ops.push({ type: 'add', line: newLines[j] });
                j += 1;
            }
        }
        while (i < m) {
            ops.push({ type: 'remove', line: oldLines[i] });
            i += 1;
        }
        while (j < n) {
            ops.push({ type: 'add', line: newLines[j] });
            j += 1;
        }
        return ops;
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

    _topologyAgents(topologyId) {
        if (!topologyId || topologyId === 'auto') return [];
        const topo = this._topologyList.find((item) => item.id === topologyId);
        return [...(topo?.agents || [])].sort();
    }

    _refreshMentionTargets() {
        const liveTargets = Object.keys(this.agents || {});
        if (liveTargets.length) {
            this._mentionTargets = liveTargets.sort();
            return;
        }
        const plannedTopologyTargets = this._topologyAgents(this._plan?.topology?.id);
        if (plannedTopologyTargets.length) {
            this._mentionTargets = plannedTopologyTargets;
            return;
        }
        const selectedTopologyTargets = this._topologyAgents(this._selectedTopology);
        if (selectedTopologyTargets.length) {
            this._mentionTargets = selectedTopologyTargets;
            return;
        }
        this._mentionTargets = ['coordinator'];
    }

    _mentionState() {
        const input = document.getElementById('query-input');
        if (!input) return null;
        const value = input.value || '';
        const caret = input.selectionStart ?? value.length;
        const prefix = value.slice(0, caret);
        const match = prefix.match(/(^|\s)@([\w-]*)$/);
        if (!match) return null;
        return {
            input,
            value,
            caret,
            query: (match[2] || '').toLowerCase(),
            start: caret - (match[2] || '').length - 1,
        };
    }

    _updateMentionSuggestions() {
        this._refreshMentionTargets();
        const menu = document.getElementById('query-mentions');
        const state = this._mentionState();
        if (!menu || !state || !this._mentionTargets.length) {
            this._hideMentionSuggestions();
            return;
        }
        const suggestions = this._mentionTargets
            .filter((target) => target.toLowerCase().includes(state.query))
            .slice(0, 8);
        if (!suggestions.length) {
            this._hideMentionSuggestions();
            return;
        }
        this._mentionSuggestions = suggestions;
        if (this._mentionSelection >= suggestions.length) this._mentionSelection = 0;
        menu.innerHTML = suggestions.map((target, index) => `
            <button
                type="button"
                class="query-mention-item${index === this._mentionSelection ? ' active' : ''}"
                data-target="${this._escapeHtml(target)}"
            >
                <span class="query-mention-handle">@${this._escapeHtml(target)}</span>
                <span class="query-mention-meta">node</span>
            </button>
        `).join('');
        menu.querySelectorAll('.query-mention-item').forEach((button, index) => {
            button.addEventListener('mouseenter', () => {
                this._mentionSelection = index;
                this._updateMentionSuggestions();
            });
            button.addEventListener('mousedown', (event) => {
                event.preventDefault();
                this._applyMentionSuggestion(button.dataset.target || '');
            });
        });
        menu.classList.remove('hidden');
    }

    _hideMentionSuggestions() {
        const menu = document.getElementById('query-mentions');
        if (!menu) return;
        this._mentionSuggestions = [];
        this._mentionSelection = 0;
        menu.classList.add('hidden');
        menu.innerHTML = '';
    }

    _handleMentionKeydown(event) {
        const menu = document.getElementById('query-mentions');
        const isOpen = menu && !menu.classList.contains('hidden') && this._mentionSuggestions.length > 0;
        if (!isOpen) return false;

        if (event.key === 'ArrowDown') {
            event.preventDefault();
            this._mentionSelection = (this._mentionSelection + 1) % this._mentionSuggestions.length;
            this._updateMentionSuggestions();
            return true;
        }
        if (event.key === 'ArrowUp') {
            event.preventDefault();
            this._mentionSelection = (this._mentionSelection - 1 + this._mentionSuggestions.length) % this._mentionSuggestions.length;
            this._updateMentionSuggestions();
            return true;
        }
        if (event.key === 'Escape') {
            event.preventDefault();
            this._hideMentionSuggestions();
            return true;
        }
        if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
            event.preventDefault();
            this._applyMentionSuggestion(this._mentionSuggestions[this._mentionSelection] || '');
            return true;
        }
        return false;
    }

    _applyMentionSuggestion(target) {
        if (!target) return;
        const state = this._mentionState();
        if (!state) return;
        const before = state.value.slice(0, state.start);
        const after = state.value.slice(state.caret);
        const insertion = `@${target} `;
        state.input.value = `${before}${insertion}${after}`;
        const nextCaret = before.length + insertion.length;
        state.input.focus();
        state.input.setSelectionRange(nextCaret, nextCaret);
        state.input.style.height = 'auto';
        state.input.style.height = Math.min(state.input.scrollHeight, 160) + 'px';
        this._hideMentionSuggestions();
    }

    _updateSessionUrl(sessionId) {
        const normalizedSessionId = sessionId || '';
        const url = new URL(window.location.href);
        const previousSessionId = String(this._wsSessionId || '');
        if (normalizedSessionId) {
            url.searchParams.set('session', normalizedSessionId);
        } else {
            url.searchParams.delete('session');
        }
        const next = `${url.pathname}${url.search}${url.hash}`;
        const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        if (next !== current) window.history.replaceState({}, '', next);
        if (!previousSessionId && normalizedSessionId) {
            // The current socket was opened without an explicit session, which already
            // means "attach to the active session". Keep that live socket instead of
            // forcing a reconnect just to mirror the session id into the URL.
            this._wsSessionId = normalizedSessionId;
            return;
        }
        if (previousSessionId !== normalizedSessionId) {
            this._reconnectForSession();
        }
    }

    _reconnectForSession() {
        if (!this.ws) {
            this._connect();
            return;
        }
        try {
            this.ws.onclose = null;
            this.ws.close();
        } catch (_err) {
            // Ignore close errors and establish a fresh socket immediately.
        }
        this._connect();
    }

    _renderResult(text) {
        // Basic markdown-like rendering: bold, code blocks, bullets, paragraphs
        let html = this._escapeHtml(text);
        // Code blocks (``` ... ```)
        html = html.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) =>
            `<pre style="background:rgba(10,15,19,0.78);border:1px solid rgba(65,71,84,0.18);border-radius:14px;padding:12px 14px;font-size:12px;overflow-x:auto;margin:8px 0;color:#dee3e9">${code}</pre>`
        );
        // Inline code
        html = html.replace(/`([^`]+)`/g, '<code style="background:rgba(10,15,19,0.78);border:1px solid rgba(65,71,84,0.18);border-radius:8px;padding:2px 6px;font-size:11px;color:#acc7ff">$1</code>');
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
        this._questionAgent = null;
    }

    _addPredictionCard(pred) {
        const complexity = pred.complexity ?? Math.max(...Object.values(pred.agent_complexity || {}), 0);
        const barColor = complexity >= 75 ? '#cf222e' : complexity >= 50 ? '#9a6700' : '#1a7f37';
        const topology = pred.topology || {};
        const routingMeta = [pred.task_type || '', pred.routing_mode || ''].filter(Boolean).join(' · ');
        const classifierModel = pred.classifier_model || '';
        const classifierProvider = pred.classifier_provider || this._inferProvider(classifierModel);
        const classifierShort = this._shortModel(classifierModel);
        const advisoryRows = [
            pred.escalation_allowed
                ? `<div class="pred-reason">Escalation recommended: ${this._escapeHtml(pred.escalation_reason || 'The task may justify a stronger topology if execution stalls.')}</div>`
                : '',
            pred.stop_early_allowed
                ? `<div class="pred-reason">Early stop eligible: ${this._escapeHtml(pred.stop_early_reason || 'The selected topology may be sufficient without further escalation.')}</div>`
                : '',
        ].filter(Boolean).join('');
        const optionsHtml = topology.id ? `
            <div class="pred-option chosen">
                <span class="pred-option-label">${this._escapeHtml(topology.label || topology.id)}</span>
                <span class="pred-option-desc">${this._escapeHtml(topology.description || '')}</span>
                <span class="pred-chosen-badge">✓ active</span>
            </div>` : '';

        const agentModels     = pred.agent_models     || {};
        const agentComplexity = pred.agent_complexity || {};
        // Show role → model, with complexity score as a small annotation.
        // These are the exact same values _build_agent_model_map will use at run-start.
        const plannerRows = classifierModel ? `
            <div class="pred-agent-model pred-agent-model-classifier">
                <span class="pred-agent-role">classification</span>
                ${classifierProvider ? `<span class="pred-agent-score">${this._escapeHtml(classifierProvider)}</span>` : ''}
                <span class="pred-agent-model-name">${this._escapeHtml(classifierShort || classifierModel)}</span>
            </div>` : '';

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
            <div class="msg-header">
                <span class="msg-time">0.0s</span>
                <span class="msg-type-badge msg-type-system">plan</span>
                <span class="agent-coordinator">runtime</span>
            </div>
            <div class="pred-header">
                <span class="pred-title">Run Plan</span>
                <span class="pred-complexity-label">Complexity</span>
                <span class="pred-complexity-value" style="color:${barColor}">${complexity}</span>
            </div>
            <div class="pred-bar-wrap">
                <div class="pred-bar" style="width:${complexity}%;background:${barColor}"></div>
            </div>
            ${routingMeta ? `<div class="pred-reason">${this._escapeHtml(routingMeta)}</div>` : ''}
            <div class="pred-reason">${this._escapeHtml(topology.description || pred.reason || '')}</div>
            ${advisoryRows}
            <div class="pred-options">${optionsHtml}</div>
            ${(plannerRows || agentRows) ? `<div class="pred-agent-models">${plannerRows}${agentRows}</div>` : ''}
        `;
        this._insertFeedEntry(el, 0);
    }

    _showThinking(agentId) {
        if (!agentId || this._thinkingAgents.has(agentId)) return;
        this._thinkingAgents.add(agentId);
        this.graph.setNodeThinking(agentId, true);
    }

    _handleAgentActivity(data) {
        const { agent, activity } = data;

        // Store per-agent activity lines for node detail panel
        if (!this._agentActivityLines[agent]) this._agentActivityLines[agent] = [];
        const lines = this._agentActivityLines[agent];
        const nextEntry = {
            activity,
            elapsed: data.elapsed ?? this._lastElapsed ?? 0,
            details: data.details || {},
        };
        const previous = lines[lines.length - 1];
        const previousActivity = typeof previous === 'string' ? previous : previous?.activity;
        if (!data.hydrated || previousActivity !== activity) {
            lines.push(nextEntry);
            if (lines.length > 10) lines.shift();
        }

        this._showThinking(agent);
        this._addActivityEntry({
            agent,
            activity,
            elapsed: data.elapsed ?? this._lastElapsed ?? 0,
            details: data.details || {},
            hydrated: Boolean(data.hydrated),
        });

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

    _addActivityEntry(data) {
        if (!data?.agent || !data?.activity) return;
        const entry = document.createElement('div');
        entry.className = 'msg-entry msg-entry-activity';
        const agentClass = AGENT_CSS_CLASS[data.agent] || 'agent-user';
        const elapsed = Number(data.elapsed || 0);
        const icon = this._activityIcon(data.activity);
        const details = data.details && typeof data.details === 'object' ? data.details : {};
        const destination = typeof details.to === 'string' ? details.to : '';
        const content = typeof details.content === 'string' ? details.content : '';
        const context = Array.isArray(details.context) ? details.context : [];
        const detailRows = [];
        if (destination) {
            detailRows.push(`<div class="msg-context-item">to: ${this._escapeHtml(destination)}</div>`);
        }
        if (content) {
            detailRows.push(`
                <div class="msg-section-label">Payload</div>
                <div class="msg-full-content">${this._escapeHtml(content)}</div>
            `);
        }
        if (context.length) {
            detailRows.push(`
                <div class="msg-section-label">Context (${context.length} items)</div>
                ${context.map((item, index) => `<div class="msg-context-item">[${index}] ${this._escapeHtml(item)}</div>`).join('')}
            `);
        }
        entry.innerHTML = `
            <div class="msg-header">
                <span class="msg-time">${elapsed.toFixed(1)}s</span>
                <span class="msg-type-badge msg-type-system">activity</span>
                <span class="${agentClass}">${this._escapeHtml(data.agent)}</span>
                ${destination ? `<span class="msg-arrow">&rarr;</span><span class="${AGENT_CSS_CLASS[destination] || 'agent-user'}">${this._escapeHtml(destination)}</span>` : ''}
                <span class="msg-stage-pill msg-stage-activity">live</span>
            </div>
            <div class="msg-preview"><strong>${this._escapeHtml(data.agent)}</strong> ${this._escapeHtml(data.activity)}</div>
            <div class="msg-expanded" style="display:block">
                <div class="activity-line">
                    <span class="activity-icon">${icon}</span>
                    <span class="activity-text">${this._escapeHtml(data.activity)}</span>
                </div>
                ${detailRows.join('')}
            </div>
        `;
        this._insertFeedEntry(entry, elapsed);
    }

    _clearThinking(agentId) {
        const el = document.getElementById('thinking-indicator');
        if (el) el.remove();
        if (!agentId) {
            for (const id of this._thinkingAgents) this.graph.setNodeThinking(id, false);
            this._thinkingAgents.clear();
            return;
        }
        if (this._thinkingAgents.has(agentId)) {
            this.graph.setNodeThinking(agentId, false);
            this._thinkingAgents.delete(agentId);
        }
    }

    _insertFeedEntry(entry, elapsed = 0) {
        if (!this.messageLog || !entry) return;
        const empty = this.messageLog.querySelector('.empty-state');
        if (empty) empty.remove();

        const sortMs = Math.round(Number(elapsed || 0) * 1000);
        const sequence = this._feedSequence++;
        entry.dataset.feedSortMs = String(sortMs);
        entry.dataset.feedSeq = String(sequence);

        const children = [...this.messageLog.children];
        const insertionPoint = children.find((child) => {
            const childSortMs = Number(child.dataset.feedSortMs);
            const childSeq = Number(child.dataset.feedSeq);
            if (!Number.isFinite(childSortMs) || !Number.isFinite(childSeq)) return false;
            return childSortMs > sortMs || (childSortMs === sortMs && childSeq > sequence);
        });

        if (insertionPoint) this.messageLog.insertBefore(entry, insertionPoint);
        else this.messageLog.appendChild(entry);
        this.messageLog.scrollTop = this.messageLog.scrollHeight;
    }

    _complexityLevel(score) {
        if (score >= 79) return 'high';
        if (score >= 56) return 'mid';
        return 'low';
    }

    _shortModel(modelId) {
        if (!modelId) return '';
        // claude-sonnet-4-6 → sonnet-4-6
        // claude-opus-4-6   → opus-4-6
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

    _setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    _sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }
}

// Initialize on load
window.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new Dashboard();
    window.dashboard?._maybeStartDemo();

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
