import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';

// Replay an actual model/solver response to isolate UI rendering from flight actuation.
const evaluation=JSON.parse(await readFile('reports/qwen-agent-evaluation-4b.json','utf8'));
const response=evaluation.cases.find(c=>c.case==='compare').result;
const browser=await chromium.launch({headless:true,executablePath:'/home/ldb/.cache/ms-playwright/chromium-1140/chrome-linux/chrome',args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-dev-shm-usage']});
const report={mode:'UI response replay; live telemetry; no flight actuation',checks:[],pageErrors:[]};
const check=(name,value)=>{assert.ok(value,name);report.checks.push(name);};
try{
  const page=await browser.newPage({viewport:{width:1600,height:1000}});
  page.on('pageerror',e=>report.pageErrors.push(e.message));
  await page.route('**/api/chat',route=>route.fulfill({json:response}));
  await page.route('**/api/chat/stream',route=>route.fulfill({status:404,json:{error:'isolated legacy replay'}}));
  await page.goto('http://127.0.0.1:11843/?mode=live&swarm=1',{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>window.fieldlink?.state?.aircraft.length===3);
  check('Three live PX4 aircraft rendered',await page.locator('.aircraft-row').count()===3);
  await page.locator('[data-tab="chat"]').click();
  await page.locator('#chat-input').fill('再比较一下，先不要执行');
  await page.locator('#chat-send').click();
  await page.waitForSelector('.tool-result');
  check('Algorithm results rendered from tool data',(await page.locator('.tool-result').innerText()).includes('hungarian'));
  check('Real call trace available',await page.locator('.tool-trace').count()===1);
  await page.locator('.tool-trace summary').click();
  check('Trace identifies selected tool',(await page.locator('.tool-trace').innerText()).includes('compare_allocations'));
  check('Desktop layout fits',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.locator('.tool-trace summary').click();
  await page.screenshot({path:'reports/qwen-agent-ui.png'});
  await page.setViewportSize({width:390,height:844});
  check('Mobile layout fits',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  check('No unhandled browser errors',report.pageErrors.length===0);
}finally{
  await writeFile('reports/qwen-agent-ui.json',JSON.stringify(report,null,2));
  await browser.close();
}
console.log(JSON.stringify(report,null,2));
