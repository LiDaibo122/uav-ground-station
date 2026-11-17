import {chromium} from 'playwright';
import {writeFile} from 'node:fs/promises';
import {deflateSync} from 'node:zlib';
const label=process.argv[2]||'after';
const base=process.env.FRONTEND_PROBE_URL||'http://127.0.0.1:11973';
function crc32(bytes){let crc=-1;for(const byte of bytes){crc^=byte;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^-1)>>>0;}
function chunk(kind,data){const key=Buffer.from(kind);const size=Buffer.alloc(4);size.writeUInt32BE(data.length);const crc=Buffer.alloc(4);crc.writeUInt32BE(crc32(Buffer.concat([key,data])));return Buffer.concat([size,key,data,crc]);}
function tile(r,g,b){const header=Buffer.alloc(13);header.writeUInt32BE(256);header.writeUInt32BE(256,4);header[8]=8;header[9]=2;const raw=Buffer.alloc((256*3+1)*256);for(let y=0;y<256;y++)for(let x=0;x<256;x++){const i=y*769+1+x*3;raw[i]=r;raw[i+1]=g;raw[i+2]=b;}return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateSync(raw)),chunk('IEND',Buffer.alloc(0))]);}
const terrain=tile(128,0,0),imagery=tile(70,90,70);
const browser=await chromium.launch({headless:true,executablePath:'/home/ldb/.cache/ms-playwright/chromium-1140/chrome-linux/chrome',args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-dev-shm-usage']});
const report={label,test:'controlled static map; fixture flat PNG tiles; forced SwiftShader software rendering; not real map accuracy or user-browser FPS',pageErrors:[]};
try{
 const page=await browser.newPage({viewport:{width:1600,height:1000}});
 page.on('pageerror',error=>report.pageErrors.push(error.message));
 await page.route('**/api/tiles/**',route=>route.fulfill({body:route.request().url().includes('/terrain/')?terrain:imagery,contentType:'image/png'}));
 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>window.fieldlink?.map?.viewer&&window.fieldlink.state.aircraft.length===3);
 await page.waitForTimeout(3000);
 await page.locator('#pause-demo').click();
 await page.waitForTimeout(1200);
 const cdp=await page.context().newCDPSession(page);await cdp.send('Performance.enable');
 const metric=async()=>Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m=>[m.name,m.value]));
 const before=await metric();
 report.staticMap=await page.evaluate(async()=>{
  const map=fieldlink.map,scene=map.viewer.scene;let frames=0;
  const remove=scene.postRender.addEventListener(()=>frames++);
  const start=performance.now();await new Promise(resolve=>setTimeout(resolve,3000));remove();
  return {renderedFrames:frames,elapsedMs:performance.now()-start,requestRenderMode:scene.requestRenderMode,entityCount:map.viewer.entities.values.length};
 });
 const after=await metric();report.staticMap.mainThreadTaskSeconds=after.TaskDuration-before.TaskDuration;
 report.updateChecks=await page.evaluate(async()=>{
  const map=fieldlink.map;
  const frameAfter=action=>new Promise(resolve=>{let finished=false;const remove=map.viewer.scene.postRender.addEventListener(()=>{if(finished)return;finished=true;remove();clearTimeout(timer);resolve(true);});const timer=setTimeout(()=>{if(finished)return;finished=true;remove();resolve(false);},4000);action();});
  const aircraft=fieldlink.state.aircraft.map((a,i)=>i===0?{...a,longitude:a.longitude+.0001}:a);
  const telemetryTriggeredFrame=await frameAfter(()=>map.updateAircraft(aircraft,aircraft[0].id));
  const cameraTriggeredFrame=await frameAfter(()=>map.zoom(1));
  return {telemetryTriggeredFrame,cameraTriggeredFrame};
 });
 report.resources=await page.evaluate(()=>performance.getEntriesByType('resource').map(r=>({name:new URL(r.name).pathname,durationMs:r.duration,transferSize:r.transferSize})).filter(r=>r.name.includes('Cesium.js')||r.name.includes('app.js')));
}finally{await browser.close();}
await writeFile(`reports/frontend-performance-${label}.json`,JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
if(report.pageErrors.length||!report.updateChecks.telemetryTriggeredFrame||!report.updateChecks.cameraTriggeredFrame)process.exitCode=1;
