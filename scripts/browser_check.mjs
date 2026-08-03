import { mkdir, writeFile } from 'node:fs/promises';

const port = Number(process.argv[2] || 9223);
const outputDir = process.argv[3] || '/tmp/learning-agent-browser-check';
const appUrl = process.argv[4] || 'http://127.0.0.1:8000';
const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
const target = targets.find((item) => item.type === 'page' && item.url.startsWith(appUrl));
if (!target) throw new Error('Learning Agent browser target not found');

await mkdir(outputDir, { recursive: true });
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});

let commandId = 0;
const pending = new Map();
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (!message.id || !pending.has(message.id)) return;
  const { resolve, reject } = pending.get(message.id);
  pending.delete(message.id);
  if (message.error) reject(new Error(message.error.message));
  else resolve(message.result);
});

function send(method, params = {}) {
  const id = ++commandId;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  return result.result.value;
}

async function viewport(width, height) {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width <= 560 });
  await wait(250);
}

async function inspect(label) {
  const metrics = await evaluate(`(() => {
    const clipped = [...document.querySelectorAll('button')].filter((node) => {
      const rect = node.getBoundingClientRect();
      return rect.right > innerWidth + 1 || rect.left < -1;
    }).map((node) => node.textContent.trim().slice(0, 40));
    return {
      label: ${JSON.stringify(label)},
      viewport: [innerWidth, innerHeight],
      h1: [...document.querySelectorAll('h1')].map((node) => node.textContent.trim()),
      workspaceBars: document.querySelectorAll('.workspace-bar').length,
      timelineStages: document.querySelectorAll('.timeline-stage').length,
      messages: document.querySelectorAll('.message-bubble').length,
      runTurns: document.querySelectorAll('.run-turn, .conversation-agent').length,
      sessions: document.querySelectorAll('.session-row').length,
      sessionActions: document.querySelectorAll('.session-actions').length,
      planCards: document.querySelectorAll('.plan-list-card').length,
      planFilters: document.querySelectorAll('.plan-filter-tabs button').length,
      resourceRows: document.querySelectorAll('.resource-row').length,
      resourceSections: document.querySelectorAll('.plan-resources-section').length,
      emailSetup: document.querySelectorAll('.email-setup').length,
      proactiveStatus: document.querySelectorAll('.proactive-status').length,
      inboxTabs: document.querySelectorAll('.inbox-tabs button').length,
      inboxArchiveActions: document.querySelectorAll('.inbox-actions button').length,
      archivedNotificationCards: document.querySelector('.inbox-tabs button.active')?.textContent.includes('已归档')
        ? document.querySelectorAll('.inbox-card').length
        : 0,
      runActivity: document.querySelectorAll('.run-activity').length,
      runActivityExpanded: document.querySelectorAll('.run-activity.expanded').length,
      achievementStrip: document.querySelectorAll('.achievement-strip').length,
      achievementBadges: document.querySelectorAll('.achievement-badge').length,
      composerStop: document.querySelectorAll('.composer-stop').length,
      contextUsage: document.querySelectorAll('.context-usage').length,
      planningAnswersCards: document.querySelectorAll('.planning-answers-card').length,
      subagentRows: document.querySelectorAll('.subagent-row').length,
      memoryChips: document.querySelectorAll('.memory-chip').length,
      memoryToolbar: document.querySelectorAll('.memory-toolbar').length,
      markdownTables: document.querySelectorAll('.agent-markdown table').length,
      sidebarScrollOwner: getComputedStyle(document.querySelector('.sidebar')).overflowY,
      recentScrollOwner: getComputedStyle(document.querySelector('.recent-group') || document.body).overflowY,
      pinnedPlanArchiveActions: document.querySelectorAll('.side-plan-archive').length,
      contextTransitions: document.querySelectorAll('.context-transition').length,
      planningPanels: document.querySelectorAll('.planning-panel').length,
      planningQuestions: document.querySelectorAll('.planning-question').length,
      proposalStages: document.querySelectorAll('.proposal-stages article').length,
      messageActions: document.querySelectorAll('.message-actions').length,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      clipped,
    };
  })()`);
  const screenshot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(`${outputDir}/${label}.png`, Buffer.from(screenshot.data, 'base64'));
  return metrics;
}

await send('Page.enable');
await send('Page.reload', { ignoreCache: true });
await wait(1500);
const report = [];

await viewport(2560, 1440);
await evaluate(`(() => { const brand = document.querySelector('.brand'); if (brand) brand.click(); return Boolean(brand); })()`);
await wait(350);
report.push(await inspect('home-wide'));

await evaluate(`(() => { const row = document.querySelector('.session-row'); if (row) row.click(); return Boolean(row); })()`);
await wait(1200);
report.push(await inspect('conversation-wide'));
const expandedRunActivity = await evaluate(`(() => {
  const button = document.querySelector('.run-activity-summary');
  if (button) button.click();
  return Boolean(button);
})()`);
if (expandedRunActivity) {
  await wait(250);
  report.push(await inspect('conversation-activity-expanded'));
}

for (const [width, height] of [[1440, 1000], [768, 1024], [375, 812]]) {
  await viewport(width, height);
  report.push(await inspect(`conversation-${width}`));
}

await viewport(1440, 1000);
const openedCurrentEditor = await evaluate(`(() => {
  const button = document.querySelector('.message-actions button[title="编辑并重新运行"]');
  if (button) button.click();
  return Boolean(button);
})()`);
if (openedCurrentEditor) {
  await wait(500);
  report.push(await inspect('message-editor-current'));
  await evaluate(`(() => {
    const button = [...document.querySelectorAll('.message-editor button')].find((node) => node.textContent.includes('取消'));
    if (button) button.click();
    return Boolean(button);
  })()`);
}

await viewport(1440, 1000);
const openedProposalFixture = await evaluate(`(() => {
  const row = [...document.querySelectorAll('.session-row')].find((node) => node.textContent.includes('计划提案'));
  if (row) row.click();
  return Boolean(row);
})()`);
if (openedProposalFixture) {
  await wait(700);
  report.push(await inspect('planning-proposal-1440'));
  const openedEditor = await evaluate(`(() => {
    const button = document.querySelector('.message-actions button[title="编辑并重新运行"]');
    if (button) button.click();
    return Boolean(button);
  })()`);
  if (openedEditor) {
    await wait(500);
    report.push(await inspect('message-editor-1440'));
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.message-editor button')].find((node) => node.textContent.includes('取消'));
      if (button) button.click();
      return Boolean(button);
    })()`);
  }
  await viewport(375, 812);
  report.push(await inspect('planning-proposal-mobile'));
}

await viewport(2560, 1440);
await evaluate(`(() => { const item = [...document.querySelectorAll('.nav-item')].find((node) => node.textContent.includes('学习计划')); if (item) item.click(); return Boolean(item); })()`);
await wait(700);
report.push(await inspect('plan-list-wide'));
await evaluate(`(() => { const card = document.querySelector('.plan-list-open'); if (card) card.click(); return Boolean(card); })()`);
await wait(900);
report.push(await inspect('plan-wide'));

for (const [width, height] of [[1440, 1000], [1280, 800], [768, 1024]]) {
  await viewport(width, height);
  report.push(await inspect(`plan-${width}`));
}

await viewport(375, 812);
report.push(await inspect('plan-mobile'));

await viewport(1440, 1000);
await evaluate(`(() => { const item = [...document.querySelectorAll('.nav-item')].find((node) => node.textContent.includes('学习计划')); if (item) item.click(); return Boolean(item); })()`);
await wait(350);
await evaluate(`(() => { const cards = [...document.querySelectorAll('.plan-list-open')]; const card = cards.find((node) => node.textContent.includes('FastAPI')) || cards[0]; if (card) card.click(); return Boolean(card); })()`);
await wait(700);
report.push(await inspect('plan-resources-1440'));

await viewport(1440, 1000);
await evaluate(`(() => { const item = [...document.querySelectorAll('.nav-item')].find((node) => node.textContent.includes('AI 记忆')); if (item) item.click(); return Boolean(item); })()`);
await wait(450);
report.push(await inspect('memory-1440'));

await viewport(1440, 1000);
await evaluate(`(() => { const item = [...document.querySelectorAll('.nav-item')].find((node) => node.textContent.includes('收件箱')); if (item) item.click(); return Boolean(item); })()`);
await wait(500);
report.push(await inspect('inbox-1440'));

const archivedNotificationTitle = await evaluate(`(() => {
  const button = document.querySelector('.inbox-actions button[title="归档消息"]');
  const title = button?.closest('.inbox-card')?.querySelector('strong')?.textContent || '';
  if (button) button.click();
  return title;
})()`);
if (archivedNotificationTitle) {
  await wait(500);
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll('.inbox-tabs button')].find((node) => node.textContent.includes('已归档'));
    if (tab) tab.click();
    return Boolean(tab);
  })()`);
  await wait(250);
  report.push(await inspect('inbox-archived-1440'));
  await evaluate(`(() => {
    const title = ${JSON.stringify(archivedNotificationTitle)};
    const card = [...document.querySelectorAll('.inbox-card')].find((node) => node.querySelector('strong')?.textContent === title);
    const button = card?.querySelector('.inbox-actions button[title="恢复消息"]');
    if (button) button.click();
    return Boolean(button);
  })()`);
  await wait(500);
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll('.inbox-tabs button')].find((node) => node.textContent.includes('收件箱'));
    if (tab) tab.click();
    return Boolean(tab);
  })()`);
}

await viewport(375, 812);
report.push(await inspect('inbox-mobile'));

console.log(JSON.stringify(report, null, 2));
socket.close();
