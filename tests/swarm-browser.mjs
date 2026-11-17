import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {writeFile} from 'node:fs/promises';
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH||'/home/ldb/.cache/ms-playwright/chromium-1140/chrome-linux/chrome',args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-dev-shm-usage']});
const result={checks:[],errors:[]};
const check=(label,value)=>{assert.ok(value,label);result.checks.push(label);console.log('PASS',label);};
try{
  const page=await browser.newPage({viewport:{width:1600,height:1000},deviceScaleFactor:1});
  page.on('pageerror',e=>result.errors.push(e.message));
  await page.goto('http://127.0.0.1:11843/?mode=live&swarm=1',{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>window.fieldlink?.swarm?.report?.allocation,{},{timeout:30000});
  check('Actual CBBA assignments rendered',await page.locator('.swarm-route').count()===3);
  check('Six allocated task points and three route lines',await page.evaluate(()=>fieldlink.map.missionEntities.length===9));
  await page.waitForFunction(()=>fieldlink.state?.aircraft.filter(a=>!a.stale).length===3,{},{timeout:30000});
  check('Three fresh PX4 SITL vehicles received',await page.locator('.aircraft-row').count()===3);
  check('No demonstration aircraft in live view',await page.evaluate(()=>fieldlink.state.aircraft.every(a=>a.source==='px4'&&a.id.startsWith('FW-'))));
  await page.waitForFunction(()=>fieldlink.map.highestTerrainLevel>=10,{},{timeout:90000});
  await page.evaluate(()=>{const C=window.Cesium;fieldlink.map.viewer.camera.flyToBoundingSphere(new C.BoundingSphere(C.Cartesian3.fromDegrees(103.576,31.048,1400),1),{duration:0,offset:new C.HeadingPitchRange(0,-.9,6500)});});
  await page.waitForTimeout(5000);
  check('Physical simulation limitation visible',(await page.locator('#swarm-panel').textContent()).includes('平地物理仿真'));
  check('No horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  check('No unhandled browser errors',result.errors.length===0);
  await page.screenshot({path:'reports/swarm-live.png'});
  result.summary=await page.locator('#swarm-summary').textContent();
}finally{
  await writeFile('reports/swarm-browser.json',JSON.stringify(result,null,2));
  await browser.close();
}
