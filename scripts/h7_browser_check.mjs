import { access, mkdir, writeFile } from 'node:fs/promises';
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs';

const appUrlInput = process.argv[2] || process.env.H7_BROWSER_URL;
const fixtureToken = process.argv[3] || process.env.H7_BROWSER_FIXTURE_TOKEN;
const outputDir = process.argv[4] || process.env.H7_BROWSER_OUTDIR || '/tmp/learning-agent-h7-browser';
if (!appUrlInput) throw new Error('H7 browser target URL is required');
if (!fixtureToken || !/^[A-Za-z0-9_-]{16,80}$/.test(fixtureToken)) {
  throw new Error('A 16-80 character synthetic fixture token is required');
}
const appUrl = new URL(appUrlInput);
if (!['http:', 'https:'].includes(appUrl.protocol)) throw new Error('H7 browser target must be HTTP(S)');
if (!['127.0.0.1', 'localhost', '[::1]'].includes(appUrl.hostname)) {
  throw new Error(`H7 browser target must be loopback, got ${appUrl.hostname}`);
}
if (appUrl.username || appUrl.password) throw new Error('H7 browser URL must not contain credentials');

const viewports = [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 800 },
  { width: 1440, height: 1000 },
  { width: 2560, height: 1440 },
];
const screenshotWidths = new Set([375, 1440]);

async function firstExecutable(candidates) {
  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next system browser path.
    }
  }
  throw new Error('No supported system Chrome/Chromium executable was found');
}

async function fetchJson(path) {
  const response = await fetch(new URL(path, appUrl), { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`Fixture GET ${path} failed with HTTP ${response.status}`);
  return response.json();
}

async function loadFixture() {
  const [sessions, plans, notifications] = await Promise.all([
    fetchJson('/api/v1/agent/sessions'),
    fetchJson('/api/v1/plans'),
    fetchJson('/api/v1/notifications'),
  ]);
  const expectedTitles = [
    `H7 synthetic alpha ${fixtureToken}`,
    `H7 synthetic beta ${fixtureToken}`,
  ];
  if (!Array.isArray(sessions) || sessions.length !== 2) {
    throw new Error('H7 browser fixture must expose exactly two synthetic Sessions');
  }
  const orderedSessions = expectedTitles.map((title) => sessions.find((item) => item.title === title));
  if (orderedSessions.some((item) => !item?.id)) throw new Error('Synthetic Session fixture attestation failed');
  const plan = plans.find((item) => item.title === `H7 可解释学习计划 ${fixtureToken}`);
  if (!plan?.id || plans.length !== 1) throw new Error('Synthetic Plan fixture attestation failed');
  const notification = notifications.find((item) => item.title === 'H7 合成复习提醒');
  if (!notification?.id || !notification?.intervention_id || notifications.length !== 1) {
    throw new Error('Synthetic Intervention fixture attestation failed');
  }
  return { alpha: orderedSessions[0], beta: orderedSessions[1], plan, notification };
}

function durationSeconds(value) {
  return String(value || '0s').split(',').reduce((maximum, part) => {
    const token = part.trim();
    const numeric = Number.parseFloat(token) || 0;
    return Math.max(maximum, token.endsWith('ms') ? numeric / 1000 : numeric);
  }, 0);
}

const executablePath = process.env.CHROME_EXECUTABLE || await firstExecutable([
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
]);
const fixture = await loadFixture();
await mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});

const report = [];
const routeCases = [
  { label: 'home', path: '/', locator: '.conversation-page' },
  { label: 'session', path: `/sessions/${fixture.alpha.id}`, locator: '.thread' },
  { label: 'plan', path: `/plans/${fixture.plan.id}`, locator: '.learning-map-section' },
  { label: 'inbox', path: '/inbox', heading: '收件箱' },
  { label: 'intervention', path: `/inbox/${fixture.notification.intervention_id}`, locator: '.composer-reply-target' },
  { label: 'memory', path: '/memory', heading: 'AI 眼中的我' },
  { label: 'settings', path: '/settings', heading: '设置' },
  { label: 'archives', path: '/archives', heading: '学习计划' },
];

async function openPage(viewport, routeCase, { optionalFailure = false, reducedMotion = 'no-preference' } = {}) {
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 1,
    colorScheme: 'light',
    reducedMotion,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
  });
  const page = await context.newPage();
  const issues = [];
  const failedRequests = [];
  page.on('pageerror', (error) => issues.push(`pageerror:${error.message}`));
  page.on('console', (message) => {
    const expectedOptional503 = optionalFailure
      && message.type() === 'error'
      && message.text().includes('503 (Service Unavailable)');
    if (!expectedOptional503 && ['error', 'assert'].includes(message.type())) {
      issues.push(`console:${message.text()}`);
    }
  });
  page.on('requestfailed', (request) => failedRequests.push(request.url()));
  if (optionalFailure) {
    await page.route('**/api/v1/{dashboard,settings/email}', async (route) => route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'synthetic optional endpoint outage' }),
    }));
  }
  await page.goto(new URL(routeCase.path, appUrl).href, { waitUntil: 'domcontentloaded' });
  await page.locator('.app-shell').waitFor({ state: 'visible' });
  await page.locator('.page-loader').waitFor({ state: 'detached', timeout: 15_000 }).catch(async () => {
    if (await page.locator('.page-loader').isVisible()) throw new Error('Core loader did not finish');
  });
  if (routeCase.locator) await page.locator(routeCase.locator).first().waitFor({ state: 'visible' });
  if (routeCase.heading) await page.getByRole('heading', { name: routeCase.heading, exact: true }).waitFor();
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await page.waitForTimeout(120);
  const metrics = await page.evaluate(() => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const horizontalEscapes = [...document.querySelectorAll('body *')]
      .filter(visible)
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.left < -1 || rect.right > innerWidth + 1;
      })
      .slice(0, 8)
      .map((element) => element.className || element.tagName);
    return {
      viewport: [innerWidth, innerHeight],
      rootOverflow: document.documentElement.scrollWidth > innerWidth + 1
        || document.body.scrollWidth > innerWidth + 1,
      horizontalEscapes,
      shells: document.querySelectorAll('.app-shell').length,
      workspaceHeaders: document.querySelectorAll('.workspace > .workspace-header').length,
      errorVisible: Boolean(document.querySelector('.app-error-toast')),
    };
  });
  if (metrics.viewport[0] !== viewport.width || metrics.viewport[1] !== viewport.height) {
    throw new Error(`Viewport drift at ${viewport.width}/${routeCase.label}`);
  }
  if (metrics.rootOverflow || metrics.horizontalEscapes.length) {
    throw new Error(`Horizontal overflow at ${viewport.width}/${routeCase.label}: ${JSON.stringify(metrics.horizontalEscapes)}`);
  }
  if (metrics.shells !== 1 || metrics.workspaceHeaders > 1 || metrics.errorVisible) {
    throw new Error(`Unstable shell at ${viewport.width}/${routeCase.label}: ${JSON.stringify(metrics)}`);
  }
  const unexpectedFailures = failedRequests.filter((url) => (
    !optionalFailure || (!url.includes('/api/v1/dashboard') && !url.includes('/api/v1/settings/email'))
  ));
  if (issues.length || unexpectedFailures.length) {
    throw new Error(`Browser issues at ${viewport.width}/${routeCase.label}: ${JSON.stringify({ issues, unexpectedFailures })}`);
  }
  return { context, page, metrics };
}

async function closePage(target) {
  await target.context.close();
}

try {
  for (const viewport of viewports) {
    const viewportResult = { viewport: [viewport.width, viewport.height], routes: [] };
    for (const routeCase of routeCases) {
      const target = await openPage(viewport, routeCase);
      try {
        if (routeCase.label === 'session') {
          const messageCount = await target.page.locator('.user-turn, .thread-run').count();
          if (messageCount < 90) throw new Error(`100-message projection was truncated to ${messageCount}`);
          const text = await target.page.locator('.thread').innerText();
          for (const required of ['合成消息 1', '合成消息 100', '实验通过']) {
            if (!text.includes(required)) throw new Error(`Session projection omitted ${required}`);
          }
        }
        if (routeCase.label === 'plan') {
          const text = await target.page.locator('.learning-map-section').innerText();
          for (const required of ['事务原子性', '训练', '证明', '最近学习证据', '不把完成进度推断成掌握度']) {
            if (!text.includes(required)) throw new Error(`Learning map omitted ${required}`);
          }
          if (viewport.width > 560) {
            const lastTask = target.page.locator('.timeline-task').last();
            await lastTask.scrollIntoViewIfNeeded();
            const [taskBox, composerBox] = await Promise.all([
              lastTask.boundingBox(),
              target.page.locator('.plan-workspace > .composer-wrap').boundingBox(),
            ]);
            if (!taskBox || !composerBox || taskBox.y + taskBox.height > composerBox.y - 4) {
              throw new Error('Plan composer prevents the last task from scrolling fully into view');
            }
          }
        }
        if (routeCase.label === 'intervention') {
          const text = await target.page.locator('.composer-reply-target').innerText();
          if (!text.includes('H7 合成复习提醒')) throw new Error('Intervention deep link lost its reply target');
        }
        if (screenshotWidths.has(viewport.width)) {
          if (viewport.width === 375) {
            const mobileNav = await target.page.locator('.nav-list .nav-item:visible').count();
            if (mobileNav !== 5) throw new Error(`Mobile navigation painted ${mobileNav}/5 destinations`);
          }
          await target.page.screenshot({
            path: `${outputDir}/${viewport.width}-${routeCase.label}.png`,
            fullPage: false,
          });
          if (routeCase.label === 'plan') {
            await target.page.locator('.learning-map-section').evaluate((element) => {
              element.scrollIntoView({ block: 'start' });
              const scroller = element.closest('.view');
              if (scroller) scroller.scrollTop = Math.max(0, scroller.scrollTop - 64);
            });
            await target.page.screenshot({
              path: `${outputDir}/${viewport.width}-plan-evidence.png`,
              fullPage: false,
            });
          }
        }
        viewportResult.routes.push({ label: routeCase.label, passed: true });
      } finally {
        await closePage(target);
      }
    }

    if (viewport.width === 375 || viewport.width === 768) {
      const target = await openPage(viewport, routeCases[1]);
      try {
        const select = target.page.locator('.mobile-session-switch select');
        await select.waitFor({ state: 'visible' });
        const box = await select.boundingBox();
        if (!box || box.height < 34 || box.width < 120) throw new Error('Compact Session switch is not operable');
        await select.selectOption(fixture.beta.id);
        await target.page.waitForURL((url) => url.pathname === `/sessions/${fixture.beta.id}`);
        viewportResult.routes.push({ label: 'session-switch', passed: true });
      } finally {
        await closePage(target);
      }
    }

    if (viewport.width === 375) {
      const target = await openPage(viewport, routeCases[0]);
      try {
        const settings = target.page.getByRole('button', { name: '设置', exact: true });
        await settings.waitFor({ state: 'visible' });
        const box = await settings.boundingBox();
        if (!box || box.width < 44 || box.height < 44) throw new Error('Mobile Settings target is smaller than 44px');
        await settings.focus();
        await target.page.keyboard.press('Enter');
        await target.page.waitForURL((url) => url.pathname === '/settings');
        await target.page.getByRole('heading', { name: '设置', exact: true }).waitFor();
        viewportResult.routes.push({ label: 'keyboard-settings', passed: true });
      } finally {
        await closePage(target);
      }
    }

    report.push(viewportResult);
  }

  const degraded = await openPage(viewports[0], routeCases[0], { optionalFailure: true });
  try {
    const sessionOptions = await degraded.page.locator('.mobile-session-switch select option').allTextContents();
    if (!sessionOptions.includes(fixture.alpha.title)) throw new Error('Optional outage hid core Sessions');
    if (await degraded.page.locator('.page-loader').isVisible()) throw new Error('Optional failure kept core loader active');
  } finally {
    await closePage(degraded);
  }

  const reduced = await openPage(viewports[0], routeCases[0], { reducedMotion: 'reduce' });
  try {
    const motion = await reduced.page.evaluate(() => {
      const candidates = [...document.querySelectorAll('button, .thread-run, .dialog-surface')]
        .filter((element) => getComputedStyle(element).display !== 'none');
      return candidates.reduce((maximum, element) => {
        const style = getComputedStyle(element);
        const parse = (value) => String(value).split(',').reduce((result, part) => {
          const token = part.trim();
          const numeric = Number.parseFloat(token) || 0;
          return Math.max(result, token.endsWith('ms') ? numeric / 1000 : numeric);
        }, 0);
        return Math.max(maximum, parse(style.animationDuration), parse(style.transitionDuration));
      }, 0);
    });
    if (durationSeconds(`${motion}s`) > 0.001) throw new Error(`Reduced motion left ${motion}s duration`);
  } finally {
    await closePage(reduced);
  }

  const archiveTarget = await openPage(viewports[3], routeCases[2]);
  try {
    const archive = archiveTarget.page.locator('.plan-detail-actions .secondary-button');
    await archive.focus();
    await archiveTarget.page.keyboard.press('Enter');
    await archiveTarget.page.waitForURL((url) => url.pathname === '/plans');
    await archiveTarget.page.goto(new URL('/archives', appUrl).href);
    const restore = archiveTarget.page.locator('.plan-lifecycle-button');
    await restore.waitFor();
    await restore.focus();
    await archiveTarget.page.keyboard.press('Enter');
    await archiveTarget.page.waitForTimeout(250);
    const plans = await fetchJson('/api/v1/plans');
    if (plans.length !== 1 || plans[0].status === 'archived') throw new Error('Keyboard archive/restore did not converge');
  } finally {
    await closePage(archiveTarget);
  }

  await writeFile(
    `${outputDir}/h7-browser-report.json`,
    `${JSON.stringify({ fixtureAttested: true, browser: 'system-chromium', report }, null, 2)}\n`,
  );
  console.log(JSON.stringify({ viewports: report.length, routeChecks: report.reduce((sum, item) => sum + item.routes.length, 0) }));
} finally {
  await browser.close();
}
