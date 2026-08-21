import { access } from 'node:fs/promises';
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs';

const targetInput = process.argv[2];
if (!targetInput) throw new Error('H8 onboarding browser target is required');
const target = new URL(targetInput);
if (!['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname)) {
  throw new Error('H8 onboarding target must be loopback');
}

async function firstExecutable(candidates) {
  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Continue to the next system browser.
    }
  }
  throw new Error('No supported system Chrome/Chromium executable was found');
}

const executablePath = process.env.CHROME_EXECUTABLE || await firstExecutable([
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
]);
const browser = await chromium.launch({ executablePath, headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const viewports = [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 800 },
  { width: 1440, height: 1000 },
];

try {
  for (const viewport of viewports) {
    const context = await browser.newContext({ viewport, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
    const page = await context.newPage();
    const issues = [];
    page.on('pageerror', (error) => issues.push(`pageerror:${error.message}`));
    page.on('console', (message) => {
      if (['error', 'assert'].includes(message.type())) issues.push(`console:${message.text()}`);
    });
    await page.goto(target.href, { waitUntil: 'domcontentloaded' });
    await page.waitForURL((url) => url.pathname === '/onboarding');
    await page.getByRole('heading', { name: '先连接混元，再开始第一段学习', exact: true }).waitFor();
    await page.evaluate(async () => {
      await document.fonts.ready;
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    const metrics = await page.evaluate(() => {
      const button = document.querySelector('.onboarding-form .primary-button')?.getBoundingClientRect();
      const panel = document.querySelector('.onboarding-panel')?.getBoundingClientRect();
      return {
        viewport: [innerWidth, innerHeight],
        overflow: document.documentElement.scrollWidth > innerWidth + 1 || document.body.scrollWidth > innerWidth + 1,
        sidebarCount: document.querySelectorAll('.sidebar').length,
        passwordInputs: document.querySelectorAll('input[type=password]').length,
        button: button ? [button.width, button.height] : null,
        panel: panel ? [panel.left, panel.right] : null,
      };
    });
    if (metrics.viewport[0] !== viewport.width || metrics.viewport[1] !== viewport.height) throw new Error('viewport drift');
    if (metrics.overflow || metrics.sidebarCount !== 0 || metrics.passwordInputs !== 1) {
      throw new Error(`invalid onboarding layout: ${JSON.stringify(metrics)}`);
    }
    if (!metrics.button || metrics.button[1] < 40 || !metrics.panel || metrics.panel[0] < -1 || metrics.panel[1] > viewport.width + 1) {
      throw new Error(`inoperable onboarding controls: ${JSON.stringify(metrics)}`);
    }
    if (issues.length) throw new Error(`browser issues: ${JSON.stringify(issues)}`);
    await context.close();
  }
  console.log(JSON.stringify({ onboardingViewports: viewports.length }));
} finally {
  await browser.close();
}
