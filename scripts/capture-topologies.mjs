#!/usr/bin/env node
/**
 * capture-topologies.mjs — one-off Playwright script that opens the dashboard,
 * forces each topology (triad / dual-review / hierarchy) onto the graph
 * renderer by calling window.dashboard.graph.setTopology(), and writes a PNG
 * screenshot of the graph panel per topology into docs/.
 *
 * Usage:
 *   ORB_DASHBOARD_URL=http://127.0.0.1:1337 node scripts/capture-topologies.mjs
 */
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const BASE_URL = process.env.ORB_DASHBOARD_URL || 'http://127.0.0.1:1337';
const OUT_DIR  = path.resolve(process.cwd(), 'docs');

const TOPOLOGIES = [
    {
        id: 'triad',
        label: 'Triad',
        description: 'A quiet view of the active network.',
        agents: [
            { id: 'coordinator', role: 'Coordinator', status: 'running', model: 'sonnet-4-6' },
            { id: 'coder',       role: 'Coder',       status: 'running', model: 'haiku-4-5' },
            { id: 'reviewer',    role: 'Reviewer',    status: 'idle',    model: 'sonnet-4-6' },
            { id: 'tester',      role: 'Tester',      status: 'running', model: 'haiku-4-5' },
        ],
        edges: [
            { source: 'coordinator', target: 'coder' },
            { source: 'coder',       target: 'reviewer' },
            { source: 'coder',       target: 'tester' },
        ],
    },
    {
        id: 'dual-review',
        label: 'Dual Review',
        description: 'Coder fans out to two reviewer branches and a tester.',
        agents: [
            { id: 'coordinator', role: 'Coordinator', status: 'running',   model: 'sonnet-4-6' },
            { id: 'coder',       role: 'Coder',       status: 'running',   model: 'haiku-4-5' },
            { id: 'reviewer_a',  role: 'Reviewer A',  status: 'running',   model: 'sonnet-4-6' },
            { id: 'tester',      role: 'Tester',      status: 'idle',      model: 'haiku-4-5' },
            { id: 'reviewer_b',  role: 'Reviewer B',  status: 'running',   model: 'sonnet-4-6' },
        ],
        edges: [
            { source: 'coordinator', target: 'coder' },
            { source: 'coder',       target: 'reviewer_a' },
            { source: 'coder',       target: 'tester' },
            { source: 'coder',       target: 'reviewer_b' },
        ],
    },
    {
        id: 'hierarchy',
        label: 'Hierarchy',
        description: 'Coordinator routes through a research layer before implementation.',
        agents: [
            { id: 'coordinator', role: 'Coordinator', status: 'running', model: 'sonnet-4-6' },
            { id: 'researcher',  role: 'Researcher',  status: 'running', model: 'opus-4-6' },
            { id: 'coder',       role: 'Coder',       status: 'running', model: 'haiku-4-5' },
            { id: 'reviewer',    role: 'Reviewer',    status: 'idle',    model: 'sonnet-4-6' },
            { id: 'tester',      role: 'Tester',      status: 'running', model: 'haiku-4-5' },
        ],
        edges: [
            { source: 'coordinator', target: 'researcher' },
            { source: 'researcher',  target: 'coder' },
            { source: 'coder',       target: 'reviewer' },
            { source: 'coder',       target: 'tester' },
        ],
    },
];

async function main() {
    await mkdir(OUT_DIR, { recursive: true });

    const browser = await chromium.launch();
    const ctx = await browser.newContext({
        viewport: { width: 1440, height: 980 },
        deviceScaleFactor: 2,
        recordVideo: {
            dir: OUT_DIR,
            size: { width: 1440, height: 980 },
        },
    });
    const page = await ctx.newPage();

    await page.goto(BASE_URL);
    await page.waitForSelector('#graph-panel canvas', { state: 'attached' });
    await page.evaluate(() => document.fonts?.ready);

    // Also capture the composer with the topology dropdown open — shows users
    // where they pick a topology before hitting Execute.
    await page.evaluate(() => document.getElementById('topology-trigger')?.click());
    await page.waitForTimeout(350);
    await page.screenshot({
        path: path.join(OUT_DIR, 'orb-dashboard-composer.png'),
        clip: { x: 0, y: 0, width: 560, height: 900 },
    });
    // Close the menu before looping.
    await page.evaluate(() => document.getElementById('topology-trigger')?.click());
    await page.waitForTimeout(150);

    for (const topology of TOPOLOGIES) {
        // Force the renderer into the desired topology and update the HUD pills
        // so the header row reflects what we're showing.
        await page.evaluate((t) => {
            window.dashboard.graph.setRunState('running');
            window.dashboard.graph.setTopology(t.agents, t.edges, null);
            const topoLabel = document.querySelector('#hero-topology-label');
            const statLabel = document.querySelector('#stat-topology');
            const runLabel  = document.querySelector('#hero-status-label');
            if (topoLabel) topoLabel.textContent = t.label;
            if (statLabel) statLabel.textContent = t.label;
            if (runLabel)  runLabel.textContent  = 'Running';
        }, topology);

        // Hold on each topology long enough for viewers to read it + for the
        // particle edges + arrow flow to animate in the video capture.
        await page.waitForTimeout(2600);

        const outPath = path.join(OUT_DIR, `topology-${topology.id}.png`);
        const panel = page.locator('#graph-panel');
        await panel.screenshot({ path: outPath });
        console.log(`wrote ${outPath}`);
    }

    // Final "complete" beat
    await page.evaluate(() => window.dashboard.graph.setRunState('completed'));
    await page.waitForTimeout(1400);

    const videoHandle = page.video();
    await page.close();
    await ctx.close();

    if (videoHandle) {
        const finalPath = path.join(OUT_DIR, 'orb-dashboard-workflow.webm');
        await videoHandle.saveAs(finalPath);
        console.log(`wrote ${finalPath}`);
    }

    await browser.close();
}

main().catch((err) => {
    console.error(err);
    process.exitCode = 1;
});
