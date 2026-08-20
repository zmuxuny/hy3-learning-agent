import { mkdir, writeFile } from 'node:fs/promises';

const debugPort = Number(process.argv[2] || process.env.CHROME_DEBUG_PORT || 9223);
const outputDir = process.argv[3] || process.env.H0_BROWSER_OUTDIR || '/tmp/learning-agent-h0-browser';
const appUrlInput = process.argv[4] || process.env.H0_BROWSER_URL;
const fixtureToken = process.argv[5] || process.env.H0_BROWSER_FIXTURE_TOKEN;
if (!appUrlInput) {
  throw new Error('H0 browser target URL is required; no default runtime may be inspected');
}
if (!fixtureToken || !/^[a-zA-Z0-9_-]{16,80}$/.test(fixtureToken)) {
  throw new Error('A 16-80 character synthetic fixture token is required');
}
const appUrl = new URL(appUrlInput);
const viewports = [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 800 },
  { width: 1440, height: 1000 },
  { width: 2560, height: 1440 },
];

if (!Number.isInteger(debugPort) || debugPort < 1 || debugPort > 65535) {
  throw new Error(`Invalid Chrome debugging port: ${process.argv[2] || ''}`);
}
if (!['http:', 'https:'].includes(appUrl.protocol)) {
  throw new Error(`Browser target URL must be HTTP(S), got ${appUrl.protocol}`);
}
if (!['127.0.0.1', 'localhost', '[::1]'].includes(appUrl.hostname)) {
  throw new Error(`H0 browser target must be loopback, got ${appUrl.hostname}`);
}
if (appUrl.username || appUrl.password) {
  throw new Error('H0 browser target URL must not contain credentials');
}

class FixtureError extends Error {}
class ContractFailure extends Error {}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function loadSessionFixture() {
  const endpoint = new URL('/api/v1/agent/sessions', appUrl);
  const response = await fetch(endpoint, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    throw new FixtureError(`Session fixture request failed with HTTP ${response.status}`);
  }
  const payload = await response.json();
  if (!Array.isArray(payload)) {
    throw new FixtureError('Session fixture endpoint did not return an array');
  }
  const expectedTitles = [
    `H0 synthetic alpha ${fixtureToken}`,
    `H0 synthetic beta ${fixtureToken}`,
  ];
  if (payload.length !== expectedTitles.length) {
    throw new FixtureError(
      'H0 browser target must expose exactly the two attested synthetic Sessions',
    );
  }
  const sessionsByTitle = new Map();
  for (const session of payload) {
    const id = String(session?.id || '').trim();
    const title = String(session?.title || '').trim();
    if (id && expectedTitles.includes(title)) sessionsByTitle.set(title, { id, title });
  }
  const fixture = expectedTitles.map((title) => sessionsByTitle.get(title)).filter(Boolean);
  if (fixture.length !== expectedTitles.length) {
    throw new FixtureError(
      'H0 browser target is not the explicitly attested synthetic two-Session fixture',
    );
  }
  return fixture;
}

const versionUrl = `http://127.0.0.1:${debugPort}/json/version`;
const versionResponse = await fetch(versionUrl);
if (!versionResponse.ok) {
  throw new Error(`Chrome debugging endpoint failed with HTTP ${versionResponse.status}`);
}
const browserVersion = await versionResponse.json();
if (!browserVersion.webSocketDebuggerUrl) {
  throw new Error('Chrome debugging endpoint did not expose webSocketDebuggerUrl');
}

await mkdir(outputDir, { recursive: true });
const sessionFixture = await loadSessionFixture();
const socket = new WebSocket(browserVersion.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});

let commandId = 0;
const pending = new Map();
const requestsBySession = new Map();
const pageIssuesBySession = new Map();

socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result || {});
    return;
  }
  if (message.method === 'Network.requestWillBeSent' && requestsBySession.has(message.sessionId)) {
    requestsBySession.get(message.sessionId).push({
      method: message.params.request.method,
      url: message.params.request.url,
    });
    return;
  }
  if (message.method === 'Runtime.exceptionThrown' && pageIssuesBySession.has(message.sessionId)) {
    pageIssuesBySession.get(message.sessionId).push({
      kind: 'exception',
      text: message.params.exceptionDetails?.exception?.description
        || message.params.exceptionDetails?.text
        || 'unknown page exception',
    });
    return;
  }
  if (message.method === 'Log.entryAdded' && pageIssuesBySession.has(message.sessionId)) {
    const entry = message.params.entry || {};
    if (entry.level === 'error') {
      pageIssuesBySession.get(message.sessionId).push({
        kind: 'log',
        text: entry.text || 'unknown error log',
      });
    }
    return;
  }
  if (message.method === 'Runtime.consoleAPICalled' && pageIssuesBySession.has(message.sessionId)) {
    if (message.params.type === 'error' || message.params.type === 'assert') {
      pageIssuesBySession.get(message.sessionId).push({
        kind: `console.${message.params.type}`,
        text: (message.params.args || [])
          .map((argument) => argument.value ?? argument.description ?? '')
          .join(' '),
      });
    }
  }
});

socket.addEventListener('close', () => {
  for (const { reject } of pending.values()) reject(new Error('Chrome debugging socket closed'));
  pending.clear();
});

function send(method, params = {}, sessionId = undefined) {
  const id = ++commandId;
  const message = { id, method, params };
  if (sessionId) message.sessionId = sessionId;
  socket.send(JSON.stringify(message));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

async function evaluate(sessionId, expression) {
  const response = await send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, sessionId);
  if (response.exceptionDetails) {
    const description = response.exceptionDetails.exception?.description
      || response.exceptionDetails.text
      || 'Runtime.evaluate failed';
    throw new Error(description);
  }
  return response.result?.value;
}

async function poll(sessionId, expression, timeoutMilliseconds = 8000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    if (await evaluate(sessionId, expression)) return true;
    await wait(100);
  }
  return false;
}

async function createColdTarget(viewport) {
  const { browserContextId } = await send('Target.createBrowserContext', {
    disposeOnDetach: true,
  });
  let targetId;
  let sessionId;
  try {
    ({ targetId } = await send('Target.createTarget', {
      url: 'about:blank',
      browserContextId,
    }));
    ({ sessionId } = await send('Target.attachToTarget', {
      targetId,
      flatten: true,
    }));
    requestsBySession.set(sessionId, []);
    pageIssuesBySession.set(sessionId, []);
    await send('Page.enable', {}, sessionId);
    await send('Runtime.enable', {}, sessionId);
    await send('Log.enable', {}, sessionId);
    await send('Network.enable', {}, sessionId);
    await send('Network.setCacheDisabled', { cacheDisabled: true }, sessionId);

    // This must happen before the first application navigation. about:blank is
    // only the inert target shell and never renders the Learning Agent app.
    await send('Emulation.setDeviceMetricsOverride', {
      width: viewport.width,
      height: viewport.height,
      screenWidth: viewport.width,
      screenHeight: viewport.height,
      deviceScaleFactor: 1,
      mobile: viewport.width <= 560,
      dontSetVisibleSize: false,
    }, sessionId);
    await send('Emulation.setTouchEmulationEnabled', {
      enabled: viewport.width <= 560,
      maxTouchPoints: viewport.width <= 560 ? 5 : 1,
    }, sessionId);

    const navigation = await send('Page.navigate', { url: appUrl.href }, sessionId);
    if (navigation.errorText) throw new Error(`Page.navigate failed: ${navigation.errorText}`);
    const ready = await poll(
      sessionId,
      `document.readyState === 'complete'
        && Boolean(document.querySelector('.app-shell'))
        && !document.querySelector('.page-loader')`,
      12000,
    );
    if (!ready) throw new Error(`Application did not finish cold boot at ${viewport.width}px`);
    return { browserContextId, targetId, sessionId };
  } catch (error) {
    if (browserContextId) {
      await send('Target.disposeBrowserContext', { browserContextId }).catch(() => {});
    }
    throw error;
  }
}

async function inspectColdLayout(sessionId, viewport) {
  const metrics = await evaluate(sessionId, `(() => {
    const html = document.documentElement;
    const body = document.body;
    return {
      viewport: [innerWidth, innerHeight],
      rootOverflow: html.scrollWidth > html.clientWidth + 1
        || body.scrollWidth > body.clientWidth + 1,
      loaderVisible: Boolean(document.querySelector('.page-loader')),
      appShells: document.querySelectorAll('.app-shell').length,
      errorText: document.querySelector('.app-error-toast')?.textContent?.trim() || '',
    };
  })()`);
  if (metrics.viewport[0] !== viewport.width || metrics.viewport[1] !== viewport.height) {
    throw new Error(
      `Viewport mismatch for ${viewport.width}: browser reported ${metrics.viewport.join('x')}`,
    );
  }
  if (metrics.appShells !== 1 || metrics.loaderVisible) {
    throw new Error(`Cold boot shell is incomplete at ${viewport.width}px`);
  }
  if (metrics.rootOverflow) {
    throw new Error(`Cold boot has horizontal page overflow at ${viewport.width}px`);
  }
  if (metrics.errorText) {
    throw new Error(`Cold boot showed an application error at ${viewport.width}px: ${metrics.errorText}`);
  }
  return metrics;
}

async function capture(sessionId, label) {
  const screenshot = await send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: false,
  }, sessionId);
  await writeFile(`${outputDir}/${label}.png`, Buffer.from(screenshot.data, 'base64'));
}

const OPERABLE_HELPER = String.raw`
  function isOperable(element) {
    if (!(element instanceof Element)) return false;
    let cursor = element;
    while (cursor) {
      const style = getComputedStyle(cursor);
      if (
        style.display === 'none'
        || style.visibility === 'hidden'
        || style.pointerEvents === 'none'
        || Number.parseFloat(style.opacity || '1') <= 0.01
      ) return false;
      cursor = cursor.parentElement;
    }
    const rect = element.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    if (rect.right <= 0 || rect.bottom <= 0 || rect.left >= innerWidth || rect.top >= innerHeight) return false;
    const x = Math.min(innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
    const y = Math.min(innerHeight - 1, Math.max(0, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(x, y);
    return Boolean(hit && (hit === element || element.contains(hit) || hit.contains(element)));
  }
`;

async function activateSessionControl(sessionId, target, allowOpener = true) {
  return evaluate(sessionId, `(() => {
    ${OPERABLE_HELPER}
    const target = ${JSON.stringify(target)};
    const selects = [...document.querySelectorAll('select')].filter(isOperable);
    for (const select of selects) {
      const option = [...select.options].find((item) => (
        String(item.value) === target.id || item.textContent.trim() === target.title
      ));
      if (!option) continue;
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return { activated: true, kind: 'select' };
    }

    const controls = [...document.querySelectorAll('button, a, [role="button"], [data-session-id]')]
      .filter(isOperable);
    const direct = controls.find((element) => (
      String(element.dataset?.sessionId || '') === target.id
      || element.innerText.trim() === target.title
      || element.getAttribute('aria-label') === target.title
      || element.getAttribute('title') === target.title
    ));
    if (direct) {
      direct.click();
      return { activated: true, kind: 'direct' };
    }

    if (${allowOpener ? 'true' : 'false'}) {
      const opener = controls.find((element) => {
        const name = [
          element.getAttribute('aria-label'),
          element.getAttribute('title'),
          element.innerText,
        ].filter(Boolean).join(' ');
        return /(历史|会话|对话)/.test(name);
      });
      if (opener) {
        opener.click();
        return { activated: false, opened: true };
      }
    }
    return { activated: false, opened: false };
  })()`);
}

async function activeSessionTitleIsVisible(sessionId, title) {
  return evaluate(sessionId, `(() => {
    ${OPERABLE_HELPER}
    const title = ${JSON.stringify(title)};
    return [...document.querySelectorAll(
      '.workspace-title strong, [data-active-session-title], [aria-current="page"]',
    )].some((element) => isOperable(element) && element.textContent.trim() === title);
  })()`);
}

async function switchToSession(sessionId, target) {
  let activation = await activateSessionControl(sessionId, target, true);
  if (!activation.activated && activation.opened) {
    await wait(180);
    activation = await activateSessionControl(sessionId, target, false);
  }
  if (!activation.activated) {
    throw new ContractFailure(`no operable Session control can select “${target.title}”`);
  }
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    if (await activeSessionTitleIsVisible(sessionId, target.title)) return;
    await wait(100);
  }
  throw new ContractFailure(`Session control did not make “${target.title}” the active conversation`);
}

async function checkSessionSwitch(sessionId) {
  await switchToSession(sessionId, sessionFixture[0]);
  await switchToSession(sessionId, sessionFixture[1]);
}

async function clickSettingsEntry(sessionId, allowMenu = true) {
  return evaluate(sessionId, `(() => {
    ${OPERABLE_HELPER}
    const controls = [...document.querySelectorAll('button, a, [role="button"]')]
      .filter(isOperable);
    const accessibleName = (element) => (
      element.getAttribute('aria-label')
      || element.getAttribute('title')
      || element.innerText.trim()
    );
    const settings = controls.find((element) => /设置/.test(accessibleName(element)));
    if (settings) {
      settings.click();
      return { activated: true };
    }
    if (${allowMenu ? 'true' : 'false'}) {
      const menu = controls.find((element) => {
        const name = accessibleName(element);
        return /(菜单|更多|对话操作)/.test(name);
      });
      if (menu) {
        menu.click();
        return { activated: false, opened: true };
      }
    }
    return { activated: false, opened: false };
  })()`);
}

async function checkSettingsEntry(sessionId) {
  let activation = await clickSettingsEntry(sessionId, true);
  if (!activation.activated && activation.opened) {
    await wait(180);
    activation = await clickSettingsEntry(sessionId, false);
  }
  if (!activation.activated) {
    throw new ContractFailure('no operable, accessibly named Settings entry exists on cold mobile UI');
  }
  const opened = await poll(
    sessionId,
    `(() => {
      ${OPERABLE_HELPER}
      return [...document.querySelectorAll('h1')]
        .some((heading) => isOperable(heading) && heading.textContent.trim() === '设置');
    })()`,
    5000,
  );
  if (!opened) throw new ContractFailure('Settings entry did not open the Settings view');
}

function assertNoDatabaseWrites(sessionId, viewport) {
  const writes = (requestsBySession.get(sessionId) || []).filter((request) => {
    let url;
    try {
      url = new URL(request.url);
    } catch {
      return false;
    }
    return url.pathname.startsWith('/api/v1/')
      && !['GET', 'HEAD', 'OPTIONS'].includes(request.method.toUpperCase());
  });
  if (writes.length) {
    throw new Error(
      `Read-only H0 browser check issued API writes at ${viewport.width}px: ${JSON.stringify(writes)}`,
    );
  }
}

function assertNoPageIssues(sessionId, viewport) {
  const issues = pageIssuesBySession.get(sessionId) || [];
  if (issues.length) {
    throw new Error(
      `Cold browser check captured page errors at ${viewport.width}px: ${JSON.stringify(issues)}`,
    );
  }
}

async function withColdTarget(viewport, action) {
  const target = await createColdTarget(viewport);
  try {
    const metrics = await inspectColdLayout(target.sessionId, viewport);
    await action(target.sessionId, metrics);
    assertNoDatabaseWrites(target.sessionId, viewport);
    assertNoPageIssues(target.sessionId, viewport);
    return metrics;
  } finally {
    requestsBySession.delete(target.sessionId);
    pageIssuesBySession.delete(target.sessionId);
    await send('Target.disposeBrowserContext', {
      browserContextId: target.browserContextId,
    }).catch(() => {});
  }
}

const report = [];
try {
  for (const viewport of viewports) {
    const result = {
      label: `cold-${viewport.width}`,
      viewport: [viewport.width, viewport.height],
      checks: [],
    };

    result.metrics = await withColdTarget(viewport, async (sessionId) => {
      await capture(sessionId, result.label);
    });

    if (viewport.width === 375 || viewport.width === 768) {
      await withColdTarget(viewport, async (sessionId) => {
        await checkSessionSwitch(sessionId);
        result.checks.push({ id: `H7-UI-001-session-${viewport.width}`, status: 'passed' });
      });
    }
    if (viewport.width === 375) {
      await withColdTarget(viewport, async (sessionId) => {
        await checkSettingsEntry(sessionId);
        result.checks.push({ id: 'H7-UI-001-settings-375', status: 'passed' });
      });
    }
    report.push(result);
  }

  await writeFile(
    `${outputDir}/h0-browser-report.json`,
    `${JSON.stringify({ targetOrigin: appUrl.origin, fixtureAttested: true, report }, null, 2)}\n`,
  );
  console.log(JSON.stringify({ report }, null, 2));
} finally {
  socket.close();
}
