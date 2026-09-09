// Render a portable illustrated HTML report and A4 PDF from the Markdown source.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import MarkdownIt from '../frontend/node_modules/markdown-it/dist/markdown-it.mjs';
import {chromium} from '../frontend/node_modules/playwright-core/index.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const input=path.join(root,'docs/第三阶段项目与评测报告.md');
const out=process.argv[2] ? path.resolve(process.argv[2]) : path.join(root,'docs');await mkdir(out,{recursive:true});
let content=new MarkdownIt({html:false,linkify:true}).render(await readFile(input,'utf8'));
for(const match of [...content.matchAll(/src="([^"]+)"/g)]){
  const source=path.resolve(path.dirname(input),match[1]);const ext=path.extname(source).slice(1);const bytes=await readFile(source);
  content=content.replace(match[0],`src="data:image/${ext==='svg'?'svg+xml':ext};base64,${bytes.toString('base64')}"`);
}
content=content.replace(/href="([^"#][^"]*)"/g,(whole,href)=>{
 if(/^[a-z]+:/i.test(href))return whole;
 const target=path.relative(root,path.resolve(path.dirname(input),href));return `href="https://github.com/zmuxuny/hy3-learning-agent/blob/main/${encodeURI(target)}"`;
});
const html=`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Learning Agent · Hy3 — 项目与评测报告</title><style>
*{box-sizing:border-box}body{font-family:"Noto Sans CJK SC","Noto Sans CJK JP",sans-serif;color:#22332e;background:#fff;margin:36px auto;max-width:930px;padding:0 30px;font-size:16px;line-height:1.8}h1{font-size:30px;line-height:1.5;color:#174d43;margin:0 0 15px}h2{font-size:23px;color:#174d43;margin:36px 0 12px;border-bottom:1px solid #ccdcd6;padding-bottom:8px;break-after:avoid}h3{font-size:18px;margin-top:24px;break-after:avoid}p{margin:12px 0}a{color:#236b60;text-decoration:none}img{display:block;max-width:100%;max-height:490px;object-fit:contain;margin:20px auto 8px}table{break-inside:avoid;border-collapse:collapse;width:100%;font-size:13px;line-height:1.65;margin:16px 0}th{background:#e9f1ec;color:#244c40;text-align:left}th,td{padding:8px;border-bottom:1px solid #d9e2dc;vertical-align:top}tr{break-inside:avoid}code{font-family:monospace;background:#f1f5f2;padding:0 3px;overflow-wrap:anywhere}em{font-style:normal;color:#56645e;font-size:13px}strong{font-weight:700} @media print{body{margin:0;padding:0;font-size:10.5pt;line-height:1.65}h1{font-size:24pt}h2{font-size:17pt;margin-top:24px}h3{font-size:13pt}table{font-size:9pt}img{max-height:75mm}a{color:inherit}p{orphans:3;widows:3}}
</style><body>${content}</body></html>`;
const htmlPath=path.join(out,'第三阶段项目与评测报告.html');await writeFile(htmlPath,html);
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:1080,height:1400}});await page.goto('file://'+htmlPath);await page.evaluate(()=>document.fonts.ready);await page.pdf({path:path.join(out,'第三阶段项目与评测报告.pdf'),format:'A4',printBackground:true,margin:{top:'14mm',bottom:'17mm',left:'15mm',right:'15mm'},displayHeaderFooter:true,headerTemplate:'<span></span>',footerTemplate:'<div style="font-size:9px;color:#687970;width:100%;text-align:center">Learning Agent · Hy3 · 个人活动作品　<span class="pageNumber"></span> / <span class="totalPages"></span></div>'});
await page.screenshot({path:path.join(out,'report-preview.png'),fullPage:false});await browser.close();console.log('report rendered',out);
