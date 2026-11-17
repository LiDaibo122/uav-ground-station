import {chromium} from 'playwright';
import {writeFile} from 'node:fs/promises';
const base=process.env.FRONTEND_PROBE_URL||'http://127.0.0.1:11843';
const browser=await chromium.launch({headless:true,executablePath:'/home/ldb/.cache/ms-playwright/chromium-1140/chrome-linux/chrome',args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-dev-shm-usage']});
const report={test:'actual map HTTP traffic under forced SwiftShader; read-only GET requests; not user-browser measurement',requests:[],responses:[],pageErrors:[]};
try{
 const page=await browser.newPage({viewport:{width:1600,height:1000}});const start=performance.now();const pending=new Map();
 page.on('pageerror',error=>report.pageErrors.push(error.message));
 page.on('request',request=>{const path=new URL(request.url()).pathname;if(!path.startsWith('/api/'))return;const item={path,requestedMs:performance.now()-start};pending.set(request,item);report.requests.push(item);});
 page.on('response',response=>{const item=pending.get(response.request());if(item){report.responses.push({...item,status:response.status(),totalMs:performance.now()-start-item.requestedMs,timing:response.request().timing()});pending.delete(response.request());}});
 await page.goto(base+'/?mode=live',{waitUntil:'domcontentloaded'});await page.waitForTimeout(12000);
 report.elapsedMs=performance.now()-start;report.pending=[...pending.values()];report.apiResponses=report.responses.filter(r=>!r.path.includes('/tiles/'));report.tileResponses=report.responses.filter(r=>r.path.includes('/tiles/'));
 report.initializationFinished=await page.evaluate(()=>Boolean(window.fieldlink));
}finally{await browser.close();}
await writeFile('reports/frontend-network-probe.json',JSON.stringify(report,null,2));console.log(JSON.stringify({elapsedMs:report.elapsedMs,initializationFinished:report.initializationFinished,apiResponses:report.apiResponses,pendingTiles:report.pending.filter(r=>r.path.includes('/tiles/')).length,tileResponses:report.tileResponses.length,pageErrors:report.pageErrors},null,2));
