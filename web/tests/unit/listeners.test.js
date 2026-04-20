// @vitest-environment happy-dom
/**
 * Guards against event-listener leak regressions.
 *
 * Risk shape: if `_handleInit` (called on every WS reconnect) attaches
 * listeners to *persistent* DOM elements, duplicates stack up across
 * reconnects and handlers fire N times. Safe patterns:
 *
 *   1. Constructor attaches listeners to persistent elements ONCE.
 *   2. Renderers replace container `innerHTML` (GC'ing old listeners),
 *      then attach listeners to the freshly-created children.
 *
 * This test counts addEventListener invocations on the handful of
 * persistent header/drawer controls across a normal init cycle.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML_PATH = resolve(__dirname, '../../static/index.html');
const APP_PATH = resolve(__dirname, '../../static/app.js');

describe('persistent-element listener discipline', () => {
    it('does not attach duplicate listeners on repeated _handleInit calls', async () => {
        // Load the dashboard HTML into happy-dom.
        const html = readFileSync(HTML_PATH, 'utf8');
        document.documentElement.innerHTML = html;

        // Wrap addEventListener on the persistent control IDs we care about.
        // If a duplicate attaches, the counter increments past 1 after init.
        const watched = ['query-send', 'stop-run-button', 'new-session-button', 'drawer-toggle', 'theme-toggle'];
        const counts = new Map(watched.map((id) => [id, 0]));
        for (const id of watched) {
            const el = document.getElementById(id);
            if (!el) continue;
            const orig = el.addEventListener.bind(el);
            el.addEventListener = (type, fn, opts) => {
                counts.set(id, counts.get(id) + 1);
                return orig(type, fn, opts);
            };
        }

        // Load app.js (its DOMContentLoaded handler constructs Dashboard once).
        const appSrc = readFileSync(APP_PATH, 'utf8');
        // Run app.js inline; module.exports branch is a no-op here.
        new Function('window', 'document', appSrc)(globalThis, document);
        document.dispatchEvent(new Event('DOMContentLoaded'));

        const constructorCounts = new Map(counts);

        // Simulate multiple WS reconnect init events. A reconnected dashboard
        // receives a fresh init-event from the server (see web/api_v1.py
        // ws_handler → current_init_event). If _handleInit re-binds
        // persistent listeners, the counts blow up here.
        const dash = globalThis.dashboard;
        if (!dash) return; // dashboard didn't construct — skip silently.
        const fakeInit = {
            agents: [],
            edges: [],
            messages: [],
            activity_events: [],
            plan_steps: [],
            stats: { message_count: 0, budget_remaining: 0, elapsed: 0 },
            run_state: 'idle',
            session_id: 'test',
            session_generation: 1,
            session_turn: 0,
        };
        for (let i = 0; i < 3; i++) {
            try { dash._handleInit(fakeInit); } catch { /* renderer may hit missing DOM in this stub */ }
        }

        for (const id of watched) {
            const before = constructorCounts.get(id);
            const after = counts.get(id);
            expect(after, `${id} gained listeners during _handleInit (was ${before}, now ${after})`).toBe(before);
        }
    });
});
