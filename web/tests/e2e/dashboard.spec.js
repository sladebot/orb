import { test, expect } from '@playwright/test';

test.describe('Orb dashboard — graph panel', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/');
        // Wait for the graph canvas to mount and for fonts + first render to land.
        await page.waitForSelector('#graph-panel canvas', { state: 'attached' });
        await page.evaluate(() => document.fonts?.ready);
        // Pause canvas animation at a known frame so screenshots are deterministic.
        await page.evaluate(() => {
            const raf = window.requestAnimationFrame;
            // eslint-disable-next-line no-global-assign
            window.requestAnimationFrame = () => 0;
            window.__pausedRaf = raf;
        });
        await page.waitForTimeout(150);
    });

    test('renders with the live stats bar visible', async ({ page }) => {
        await expect(page.locator('#stats-bar')).toBeVisible();
        await expect(page.locator('#stat-messages')).toBeVisible();
        await expect(page.locator('#stat-status')).toBeVisible();
    });

    test('graph panel renders a canvas sized to its container', async ({ page }) => {
        const canvas = page.locator('#graph-panel canvas');
        await expect(canvas).toBeVisible();
        const box = await canvas.boundingBox();
        expect(box?.width || 0).toBeGreaterThan(400);
        expect(box?.height || 0).toBeGreaterThan(300);
    });

    test('graph panel matches visual baseline', async ({ page }) => {
        const panel = page.locator('#graph-panel');
        await expect(panel).toHaveScreenshot('graph-panel.png');
    });
});
