// Re-record the archived results with the current presentation palette. No API calls.
import {readFile, writeFile, mkdir, rename, rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from '../frontend/node_modules/playwright-core/index.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const target=path.join(root,'assets/demo/stage3');
const archive='evaluation/artifacts/decisionbench-study-20260910';
const original=await readFile(path.join(root,archive,'viewer.html'),'utf8');
const colors={'#f6f8f5':'#f7f8fa','#21352e':'#283343','#cddbd2':'#d8dee7','#52645b':'#647185','#bbcfc1':'#c4cfdd','#246655':'#315f91','#d6e0d9':'#dce2ea','#f1f5f1':'#f3f5f8','#dfe7e0':'#dce2ea','#627269':'#647185'};
let html=original;
for(const [a,b] of Object.entries(colors))html=html.replaceAll(a,b);
html=html.replace('<title>',`<base href="../../../${archive}/"><title>`);
await writeFile(path.join(target,'viewer.html'),html);
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',args:['--no-sandbox']});
const checks=[];
for(const width of [375,768,1280,1440]){
  const page=await browser.newPage({viewport:{width,height:900}});
  await page.goto('file://'+path.join(target,'viewer.html'));
  await page.evaluate(()=>document.fonts.ready);
  for(const tab of ['app-tab','method-tab']){
    await page.locator('#'+tab).click();
    const size=await page.evaluate(()=>({client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}));
    if(size.scroll>size.client)throw Error(`overflow:${width}:${tab}`);
    checks.push({width,tab,...size});
  }
  await page.close();
}
const videoDir=path.join(target,'recording-tmp');await mkdir(videoDir,{recursive:true});
const context=await browser.newContext({viewport:{width:1440,height:900},recordVideo:{dir:videoDir,size:{width:1440,height:900}}});
const page=await context.newPage();
await page.goto('file://'+path.join(target,'viewer.html'));await page.evaluate(()=>document.fonts.ready);
const started=Date.now();const markers=[];
const mark=name=>markers.push({name,seconds:(Date.now()-started)/1000});
mark('case');await page.waitForTimeout(11000);
await page.locator('summary').filter({hasText:'七维等级与原始证据'}).click();
await page.locator('#dimensions').scrollIntoViewIfNeeded();mark('dimensions');await page.waitForTimeout(7000);
await page.locator('#method-tab').click();mark('validation');await page.waitForTimeout(14000);
await page.locator('#tracks').scrollIntoViewIfNeeded();mark('tracks');await page.waitForTimeout(10000);
const video=page.video();await context.close();
await rename(await video.path(),path.join(target,'sources/evaluation.webm'));
await rm(videoDir,{recursive:true});await browser.close();
await writeFile(path.join(target,'sources/evaluation-demo-markers.json'),JSON.stringify(markers,null,2)+'\n');
const manifestPath=path.join(target,'timeline.json');const manifest=JSON.parse(await readFile(manifestPath));
manifest.source_sha256['sources/evaluation.webm']=hash(await readFile(path.join(target,'sources/evaluation.webm')));
manifest.evaluation_presentation={source_archive:archive,source_viewer_sha256:hash(original),viewer_sha256:hash(html),figure_sha256:hash(await readFile(path.join(root,'assets/stage3/discrimination.png'))),palette:'navy / neutral white',recording_script:'scripts/record-stage3-evaluation.mjs',viewport_checks:checks};
await writeFile(manifestPath,JSON.stringify(manifest,null,2)+'\n');
console.log('Evaluation recorded; 8 viewport/tab checks passed.');
