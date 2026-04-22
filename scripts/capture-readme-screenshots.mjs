// Capture fresh README screenshots of the flat-UI dashboard.
// Requires the Orb daemon running at http://127.0.0.1:1337.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import path from 'node:path';

const OUT = '/Users/souranil/projects/orb/docs';
mkdirSync(OUT, { recursive: true });

const DPI = 2; // retina; matches the existing PNGs
const HERO_W = 1440;
const HERO_H = 900;

// Synthetic init event so the screenshots show the node/edge palette
// under realistic state (one agent running, one waiting, one done, one
// idle) without needing to run a real LLM. The init event shape matches
// what the server would broadcast at run-start.
const DEMO_INIT = {
    type: 'init',
    session_id: 'demo',
    session_generation: 1,
    session_turn: 2,
    run_state: 'running',
    workdir: '/Users/souranil/projects/orb',
    plan: {
        topology: { id: 'triad', label: 'Triad' },
        routing: { task_type: 'coding', reason: 'compact implementation task' },
        agent_complexity: { coordinator: 20, coder: 50, reviewer: 35, tester: 30 },
        agent_models: { coordinator: 'sonnet', coder: 'opus', reviewer: 'sonnet', tester: 'haiku' },
    },
    stats: { message_count: 7, budget_remaining: 193, elapsed: 42.6 },
    agents: [
        { id: 'coordinator', role: 'Coordinator', status: 'completed', model: 'sonnet', msg_count: 2, completed_result: 'Routed task to coder.' },
        { id: 'coder',       role: 'Coder',       status: 'running',   model: 'opus',    msg_count: 3 },
        { id: 'reviewer',    role: 'Reviewer',    status: 'waiting',   model: 'sonnet',  msg_count: 1 },
        { id: 'tester',      role: 'Tester',      status: 'idle',      model: 'haiku',   msg_count: 1 },
    ],
    edges: [
        { source: 'coordinator', target: 'coder' },
        { source: 'coder',       target: 'reviewer' },
        { source: 'coder',       target: 'tester' },
        { source: 'reviewer',    target: 'tester' },
    ],
    messages: [],
    activity_events: [],
    plan_steps: [],
};

const browser = await chromium.launch();
const context = await browser.newContext({
    viewport: { width: HERO_W, height: HERO_H },
    deviceScaleFactor: DPI,
});
const page = await context.newPage();

// ---- 1. Hero shot: full dashboard ----
await page.goto('http://127.0.0.1:1337/');
// Wait for the chrome + graph canvas.
await page.waitForSelector('#v2-chrome-actions', { state: 'visible' });
await page.waitForFunction(
    () => document.getElementById('connection-indicator')?.classList.contains('connected'),
    { timeout: 8000 },
).catch(() => { /* still capture even if WS slow */ });
await page.evaluate(() => document.fonts?.ready);
// Dismiss the auto-opening Session modal so the chrome is visible.
await page.waitForTimeout(400);
await page.evaluate(() => {
    const m = document.getElementById('session-config-modal');
    if (m) { m.classList.add('hidden'); m.setAttribute('aria-hidden', 'true'); }
});
// Seed a demo init event so topology nodes render with all the status
// colors (running / waiting / completed / idle).
await page.evaluate((init) => {
    window.dashboard?._handleInit(init);
}, DEMO_INIT);
await page.waitForTimeout(400);
// Force the connection pill into the "Connected" state for the screenshot
// AFTER _handleInit — the demo init's session_id doesn't match the real
// daemon's so the WS may flap, but the UI we're demoing is independent
// of that.
await page.evaluate(() => {
    const ind = document.getElementById('connection-indicator');
    if (ind) { ind.className = 'connected'; ind.title = 'Connected'; }
    const lbl = document.getElementById('connection-label');
    if (lbl) lbl.textContent = 'Daemon Connected';
});
await page.waitForTimeout(200);
await page.screenshot({ path: path.join(OUT, 'orb-dashboard.png'), fullPage: false });
console.log('wrote orb-dashboard.png');

// ---- 2. Repo panel close-up ----
const repoPanel = await page.locator('#repo-panel').first();
if (await repoPanel.count()) {
    await repoPanel.screenshot({ path: path.join(OUT, 'orb-dashboard-repo.png') });
    console.log('wrote orb-dashboard-repo.png');
}

// ---- 3. Session modal ----
await page.evaluate(() => {
    const m = document.getElementById('session-config-modal');
    if (m) { m.classList.remove('hidden'); m.setAttribute('aria-hidden', 'false'); }
});
await page.waitForTimeout(250);
await page.screenshot({ path: path.join(OUT, 'orb-session-modal.png'), fullPage: false });
console.log('wrote orb-session-modal.png');

await browser.close();
