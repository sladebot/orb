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
await page.waitForTimeout(250);
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

// ---- 4. Mobile view ----
await context.close();
const mobileCtx = await browser.newContext({
    viewport: { width: 400, height: 900 },
    deviceScaleFactor: DPI,
});
const mobilePage = await mobileCtx.newPage();
await mobilePage.goto('http://127.0.0.1:1337/');
await mobilePage.waitForSelector('#v2-chrome-actions', { state: 'visible' });
await mobilePage.waitForFunction(
    () => document.getElementById('connection-indicator')?.classList.contains('connected'),
    { timeout: 8000 },
).catch(() => {});
await mobilePage.evaluate(() => document.fonts?.ready);
await mobilePage.waitForTimeout(400);
await mobilePage.evaluate(() => {
    const m = document.getElementById('session-config-modal');
    if (m) { m.classList.add('hidden'); m.setAttribute('aria-hidden', 'true'); }
});
await mobilePage.waitForTimeout(250);
await mobilePage.screenshot({ path: path.join(OUT, 'orb-dashboard-mobile.png'), fullPage: false });
console.log('wrote orb-dashboard-mobile.png');

await browser.close();
