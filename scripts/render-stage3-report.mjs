// Render a portable illustrated HTML report and A4 PDF from the Markdown source.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import MarkdownIt from '../frontend/node_modules/markdown-it/dist/markdown-it.mjs';
import {chromium} from '../frontend/node_modules/playwright-core/index.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const input=path.join(root,'第三阶段项目与评测报告.md');
const out=process.argv[2] ? path.resolve(process.argv[2]) : root;await mkdir(out,{recursive:true});
let content=new MarkdownIt({html:false,linkify:true}).render(await readFile(input,'utf8'));
// Keep headings addressable and give dense case tables room for readable filenames.
content=content.replace(/<h([1-6])>([\s\S]*?)<\/h\1>/g,(_,level,label)=>{
 const id=label.replace(/<[^>]*>/g,'').toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu,'').replace(/\s/g,'-');
 return `<h${level} id="${id}">${label}</h${level}>`;
});
content=content.replace(/<table>([\s\S]*?)<\/table>/g,(_,body)=>{
 const headers=[...body.matchAll(/<th[^>]*>(.*?)<\/th>/g)].map(m=>m[1]);
 let widths;
 if(headers[0]==='编号与任务')widths=[18,55,7,20];
 else if(headers[0]==='类别与用例')widths=[22,28,28,22];
 else if(headers.at(-1)?.startsWith('文件名'))widths=[23,29,29,19];
 else if(headers[1]?.startsWith('正确处理：'))widths=[25,25,25,25];
 else if(headers[0]==='顺序')widths=[13,45,42];
 else if(headers[0]==='用例编号')widths=[14,40,46];
 else if(headers[0]==='预设质量')widths=[14,49,37];
 const small=[...body.matchAll(/<tr>/g)].length<=9&&body.replace(/<[^>]*>/g,'').length<1100;
 const cols=widths?'<colgroup>'+widths.map(w=>`<col style="width:${w}%">`).join('')+'</colgroup>':'';
 return `<table${small?' class="keep-table"':''}>${cols}${body}</table>`;
});
content=content.replace(/<p>(<img[^>]+>)<\/p>\s*<p><em>(图[\s\S]*?)<\/em><\/p>/g,
  (_,img,caption)=>`<figure${/evaluation-(method-flow|experiment-map)/.test(img)?' class="flow-figure"':''}>${img}<figcaption>${caption}</figcaption></figure>`);
for(const match of [...content.matchAll(/src="([^"]+)"/g)]){
  const source=path.resolve(path.dirname(input),match[1]);const ext=path.extname(source).slice(1);const bytes=await readFile(source);
  content=content.replace(match[0],`src="data:image/${ext==='svg'?'svg+xml':ext};base64,${bytes.toString('base64')}"`);
}
content=content.replace(/href="([^"#][^"]*)"/g,(whole,href)=>{
 if(/^[a-z]+:/i.test(href))return whole;
 const decoded=decodeURI(href);
 if(decoded.startsWith(path.basename(input)+'#'))return `href="#${decoded.split('#')[1]}"`;
 const target=path.relative(root,path.resolve(path.dirname(input),decoded));return `href="https://github.com/zmuxuny/hy3-learning-agent/blob/main/${encodeURI(target)}"`;
});
const html=`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Learning Agent · Hy3 — 项目与评测报告</title><style>
*{box-sizing:border-box}body{font-family:"Noto Sans CJK SC","Noto Sans CJK JP",sans-serif;color:#283343;background:#fff;margin:36px auto;max-width:930px;padding:0 30px;font-size:16px;line-height:1.8}h1{font-size:30px;line-height:1.5;color:#203d63;margin:0 0 15px}h2{font-size:23px;color:#203d63;margin:36px 0 12px;border-bottom:1px solid #d8dee7;padding-bottom:8px;break-after:avoid}h3{font-size:18px;margin-top:24px;break-after:avoid}h4{break-after:avoid}figure{margin:18px 0;break-inside:avoid}figcaption{color:#5d6878;font-size:13px;line-height:1.65}p{margin:12px 0}a{color:#315f91;text-decoration:underline;text-decoration-color:#8ba3bf;text-underline-offset:3px}img{display:block;max-width:100%;max-height:490px;object-fit:contain;margin:20px auto 8px}table{break-inside:auto;border-collapse:collapse;width:100%;font-size:13px;line-height:1.65;margin:16px 0}th{background:#eef2f7;color:#203d63;text-align:left}th,td{padding:8px;border-bottom:1px solid #dce2ea;vertical-align:top}thead{display:table-header-group}tr{break-inside:avoid}td,th{overflow-wrap:anywhere}table.keep-table{break-inside:avoid}code{font-family:monospace;background:#f3f5f8;padding:0 3px;overflow-wrap:anywhere}em{font-style:normal;color:#5d6878;font-size:13px}strong{font-weight:700} @media print{body{margin:0;padding:0;font-size:10.5pt;line-height:1.65}h1{font-size:24pt}h2{font-size:17pt;margin-top:24px}h3{font-size:13pt}table{font-size:9pt}img{max-height:95mm}figure:first-of-type img{max-height:110mm}figure.flow-figure img{max-height:135mm}figcaption{font-size:9pt}a{color:#315f91}p{orphans:3;widows:3}}
</style><body>${content}</body></html>`;
const htmlPath=path.join(out,'第三阶段项目与评测报告.html');await writeFile(htmlPath,html);
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:1080,height:1400}});await page.goto('file://'+htmlPath);await page.evaluate(()=>document.fonts.ready);
// PDF destination names have tighter limits than HTML IDs. Keep the portable
// HTML anchors, and use short names for the otherwise identical PDF rendering.
await page.evaluate(()=>{
 const ids=new Map();
 document.querySelectorAll('[id]').forEach((el,i)=>{const id=`section-${i+1}`;ids.set(el.id,id);el.id=id;});
 document.querySelectorAll('a[href^="#"]').forEach(a=>{
  const target=decodeURIComponent(a.getAttribute('href').slice(1));
  if(ids.has(target))a.setAttribute('href','#'+ids.get(target));
 });
});
await page.pdf({path:path.join(out,'第三阶段项目与评测报告.pdf'),format:'A4',printBackground:true,margin:{top:'14mm',bottom:'17mm',left:'15mm',right:'15mm'},displayHeaderFooter:true,headerTemplate:'<span></span>',footerTemplate:'<div style="font-size:9px;color:#687386;width:100%;text-align:center">Learning Agent · Hy3 · 个人活动作品　<span class="pageNumber"></span> / <span class="totalPages"></span></div>'});
await page.screenshot({path:path.join(out,'report-preview.png'),fullPage:false});await browser.close();console.log('report rendered',out);
