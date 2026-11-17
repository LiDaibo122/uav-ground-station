import { chromium } from 'playwright';
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH || '/home/ldb/.cache/ms-playwright/chromium-1140/chrome-linux/chrome',args:['--no-sandbox','--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-dev-shm-usage']});
try {
  const page=await browser.newPage({viewport:{width:1600,height:1000}});
  let n=0;
  page.on('response',r=>{if(r.url().includes('/api/tiles/')&&n++<15)console.log(r.status(),r.url());});
  page.on('pageerror',e=>console.log('ERROR',e.message));
  await page.goto('http://127.0.0.1:11843');
  await page.evaluate(()=>document.fonts.ready);
  await page.waitForTimeout(22000);
  console.log(JSON.stringify(await page.evaluate(()=>{
    const m=fieldlink.map;const g=m.viewer.scene.globe;
    return {loaded:m.terrainLoaded,failures:m.terrainFailures,cache:[...m.tilePromises.keys()].slice(-10),camera:m.viewer.camera.positionCartographic,tiles:g._surface._tilesToRender.map(t=>({l:t.level,x:t.x,y:t.y,render:t.renderable})),queue:[g._surface._tileLoadQueueHigh.length,g._surface._tileLoadQueueMedium.length,g._surface._tileLoadQueueLow.length]};
  })));
  await page.screenshot({path:'runs/map-diagnostic.png'});
}finally{await browser.close();}
