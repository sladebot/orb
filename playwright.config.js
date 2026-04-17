import { defineConfig, devices } from '@playwright/test';

// The Orb daemon is expected to be already running on the default port
// (1337 locally; overridable via ORB_DASHBOARD_URL). We do *not* spawn one
// from Playwright — see `CLAUDE.md` rule #4 and memory/feedback_daemon_lifecycle.
const BASE_URL = process.env.ORB_DASHBOARD_URL || 'http://127.0.0.1:1337';

export default defineConfig({
    testDir: './web/tests/e2e',
    fullyParallel: false,
    forbidOnly: !!process.env.CI,
    retries: 0,
    reporter: 'list',
    use: {
        baseURL: BASE_URL,
        viewport: { width: 1440, height: 980 },
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
    },
    expect: {
        toHaveScreenshot: {
            // Canvas rendering varies a tiny bit across OS/GPU; allow a small pixel budget.
            maxDiffPixelRatio: 0.012,
        },
    },
    projects: [
        {
            name: 'chromium',
            use: { ...devices['Desktop Chrome'] },
        },
    ],
});
