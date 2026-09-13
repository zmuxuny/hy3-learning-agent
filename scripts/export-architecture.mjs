// Export the editable overview and detailed architecture with a fixed light theme.
// Requires network access to diagrams.net and the installed Playwright Chromium.
import {readFile, writeFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from '../frontend/node_modules/playwright-core/index.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const files = process.argv.slice(2);
if (!files.length) files.push(
  'assets/stage3/application-evaluation-architecture.drawio',
  'assets/proposal/architecture/learning-agent-system-architecture.drawio',
);
let xml;
const server = createServer((_req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(`<!doctype html><style>html,body{margin:0}iframe{border:0;width:100vw;height:100vh}</style><iframe id="diagram" src="https://embed.diagrams.net/?embed=1&proto=json&ui=atlas&dark=0"></iframe><script>
  const frame=document.getElementById('diagram');
  window.addEventListener('message',e=>{
    if(e.source!==frame.contentWindow||e.origin!=='https://embed.diagrams.net')return;
    let m;try{m=JSON.parse(e.data)}catch{return;}
    if(m.event==='init')frame.contentWindow.postMessage(JSON.stringify({action:'load',xml:${JSON.stringify(xml).replaceAll('<', '\\u003c')}}),e.origin);
    if(m.event==='load')setTimeout(()=>frame.contentWindow.postMessage(JSON.stringify({action:'export',format:'svg',border:${xml.includes('id="hy3_core"') ? 12 : 25},scale:1}),e.origin),2500);
    if(m.event==='export')window.exported=m.data;
  });</script>`);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let browser;
try {
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || '/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',
    args: ['--no-sandbox'],
  });
  const page = await browser.newPage({viewport: {width: 1920, height: 1900}, colorScheme: 'light'});
  for (const file of files) {
    const input = path.resolve(root, file);
    xml = await readFile(input, 'utf8');
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => window.exported, {timeout: 60000});
    const data = await page.evaluate(() => window.exported);
    let svg = Buffer.from(data.split(',')[1], 'base64').toString();
    // SVG exports otherwise adapt to the reader's OS theme, including in reports.
    svg = svg.replace(/light-dark\(((?:rgb\([^)]*\)|[^,()])+),\s*(?:rgb\([^)]*\)|[^()])+\)/g, '$1')
      .replace(/color-scheme:\s*light dark/g, 'color-scheme: light');
    const stem = input.replace(/\.drawio$/, '');
    await writeFile(`${stem}.svg`, svg);
    await page.goto(`file://${stem}.svg`);
    await page.evaluate(() => document.fonts.ready);
    await page.locator('svg').screenshot({path: `${stem}.png`});
    console.log(`Exported ${path.relative(root, stem)}.{svg,png}`);
  }
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
