import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';

const origin = 'https://fieldlink.test';
const runId = 'sim-workbench';
const allocation = {
 id: '9ed49aa6aa5e4ab89e21f51460bf896c', run_id: runId, height_m: 40,
 tasks: [{id:'T1', label:'道路', east_m:650,north_m:0,priority:'normal'}],
 assignments: {uav1: ['T1'], uav2: []}, unassigned: {},
};
const receipt = {plan_id:allocation.id,status:'running',vehicles:{uav1:{phase:'running'}}};
const html = await readFile(new URL('../web/xd-console.html',import.meta.url));
const browser = await chromium.launch({args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
try {
 const page = await browser.newPage({viewport:{width:1500,height:910}});
 const requests = [], errors=[], tiles=[], libraryRequests=[];
 let currentRun=runId, libraryState={run_id:runId,revision:0,tasks:[]};
 let mapTilerKey = '', customStyle = '';
 let riverAvailable=false;
 let river={run_id:runId,phase:'ready',primary:'uav2',recall:false,
  samples:[{name:'河堤西段',owner:'uav2',receipt:null,position:{x:650,y:120}}],events:[]};
 const riverOps=[];
 const replayState={lanes:{uav2:{route:['T1'],origin:[0,0],current:'T1',due:Date.now()/1000+90,phase:'running'}},
  tasks:{T1:{kind:'area',label:'河堤东段',center:[20,20],area:{west:10,east:30,south:15,north:25},status:'running'}}};

 page.on('pageerror',error=>errors.push(error.message));
 const states=['uav1','uav2','uav3'].map((vehicle,i)=>({
  vehicle,connected:true,localization_valid:true,armed:i===0,flight_phase:i===0?'in_air':'on_ground',
  battery_pct:85,mode:'OFFBOARD',world_position_m:{x:123,y:i*120,z:i===0?40:0},
  mission:i===0?{stage:'巡检中',route_progress_pct:22.7,next_point_estimate:{label:'桥面',world_m:[720,10,40]}}:null,
 }));
 await page.route(origin+'/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/chat.html')return route.fulfill({contentType:'text/html',body:html});
  if(['/xd-console.js','/xd-map.js','/xd-rehearsal.js','/xd-task-library.js','/xd-basemap.js','/xd-maplibre.js','/xd-console.css','/chat-stream.js'].includes(path)){
   const contentType=path.endsWith('.css')?'text/css':'application/javascript';
   return route.fulfill({contentType,body:await readFile(new URL('../web'+path,import.meta.url))});
  }
  if(path.startsWith('/vendor/maplibre/')) {
   const file=path.split('/').at(-1);
   return route.fulfill({contentType:file.endsWith('.css')?'text/css':'application/javascript',
    body:await readFile(new URL('../node_modules/maplibre-gl/dist/'+file,import.meta.url))});
  }
  if(path==='/api/health')return route.fulfill({json:{model:{available:true,model:'Qwen'}}});
  if(path==='/api/xd/map-sources')return route.fulfill({json:{maptiler_api_key:mapTilerKey,custom_xyz:null,maplibre_style_url:customStyle}});
  if(path==='/api/xd/console')return route.fulfill({json:{run_id:currentRun,vehicles:states,dispatch_available:true,river_trial:riverAvailable,
   datum:{lat:47.397742,lon:8.545594,altitude_m:535.7},
   trails:{uav1:[{x:0,y:0,observed_at:1},{x:123,y:0,observed_at:2}]}}});
  if(path.startsWith('/api/xd/replays/'))return route.fulfill({json:replayState});
  if(path==='/api/xd/river-trial'){
   if(route.request().method()==='POST'){
    const data=route.request().postDataJSON();
    riverOps.push(data);
    if(data.op==='interpret')return route.fulfill({json:{proposal:'recall',run_id:runId}});
    if(data.op==='start')river={...river,phase:'survey',primary:data.primary};
    if(data.op==='recall')river={...river,recall:true,events:[{text:'已收到返航请求'}]};
   }
   return route.fulfill({json:river});
  }
  if(path==='/api/xd/task-library'){
   if(route.request().method()==='POST'){
    const data=route.request().postDataJSON();
    libraryRequests.push(data);
    assert.equal(data.run_id,currentRun);
    assert.equal(data.revision,libraryState.revision);
    libraryState={...data,revision:data.revision+1};
   }
   return route.fulfill({json:libraryState});
  }
  if(path==='/api/xd/confirm'){
   const data=route.request().postDataJSON();
   requests.push({...data,op:'confirm'});
   if(data.plan_id==='b'.repeat(32))return route.fulfill({json:{kind:'library_replay',state:replayState}});
   return route.fulfill({json:{kind:'flight',receipt}});
  }
  if(path!=='/api/chat/stream')return route.fulfill({status:404});
  const request=route.request().postDataJSON();
  requests.push(request);
  if(request.mode==='xd_task_library'){
   assert.equal(request.library_run_id,currentRun);
   assert.equal(request.library_revision,libraryState.revision);
   const value={id:'b'.repeat(32),kind:'library_replay',run_id:currentRun,library_revision:libraryState.revision,tasks:libraryState.tasks,
    assignments:{uav1:[],uav2:libraryState.tasks.map(t=>t.id)},unassigned:{}};
   if(request.message==='校验试验区外的任务'){
    Object.assign(value,{id:'c'.repeat(32),kind:'library_flight',flight_blocked:'河堤东段的短边须超过 315 米，才有两条固定翼巡检扫线'});
   }
   return route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'result',result:{
    reply:'已按提交的任务库分工。',draft:value,
    tool_calls:[{tool:'xd_library_allocate',status:'success',result:{result:{...value,id:'a'.repeat(32)}}},
     {tool:'xd_library_allocate',status:'success',result:{result:value}}]
   }})+'\n'});
  }
  const single=request.mode==='xd_vehicle';
  const planning=request.mode==='xd_plan_only';
  const updated={...allocation,id:'5b389490eb4f4d858f8dde7e46aaf35f'};
  const value=single?states.find(v=>v.vehicle===request.vehicle_id):planning?(request.message==='重新规划。'?updated:allocation):receipt;
  const tool=single?'xd_state':planning?'xd_allocate':'xd_confirm_allocation';
  const event={type:'result',result:{reply:single?`${value.vehicle} 状态已查询。`:planning?'已生成草案。':'开始执行草案。',
   draft:planning?value:null,tool_calls:[{tool,status:'success',result:{result:value}}]}};
  return route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify(event)+'\n'});
 });

 const tile = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNI7VkFAAL1AZzjnPYDAAAAAElFTkSuQmCC','base64');
 await page.route('https://tile.openstreetmap.org/**', route=>{
  tiles.push(new URL(route.request().url()));
  return route.fulfill({contentType:'image/png',body:tile,headers:{'Access-Control-Allow-Origin':'*'}});
 });
 await page.route('https://tiles.openfreemap.org/styles/liberty', route=>route.fulfill({
  headers:{'Access-Control-Allow-Origin':'*'},
  json:{version:8, sources:{local:{type:'geojson',attribution:'MapLibre Test Attribution',data:{
   type:'FeatureCollection', features:[{type:'Feature',properties:{},geometry:{
    type:'Point',coordinates:[8.545594,47.397742]}}]
  }}}, layers:[{id:'bg',type:'background',paint:{'background-color':'#bcdad2'}},
   {id:'point',type:'circle',source:'local',paint:{'circle-radius':12,'circle-color':'#7766dd'}}]},
 }));
 await page.route('https://styles.example.test/unavailable.json', route=>route.fulfill({
  status:403,headers:{'Access-Control-Allow-Origin':'*'},
 }));
 await page.route('https://api.maptiler.com/**', route=>{
  tiles.push(new URL(route.request().url()));
  return route.fulfill({status:403,headers:{'Access-Control-Allow-Origin':'*'}});
 });

 const url=origin+'/chat.html?mode=plan';
 const dispatch=page.locator('#dispatch-panel'), aircraft=page.locator('#vehicle-panel');
 await page.goto(url);
 await page.getByText('遥测 3/3 在线').waitFor();
 await page.locator('#use-task-library').uncheck();
 assert.equal(await dispatch.isVisible(),false);
 assert.equal(await aircraft.isVisible(),false);
 await page.getByText('巡检中 · 航线 22.7%').waitFor();
 await page.getByText('估计下一点 · 桥面').waitFor();
 await page.getByRole('button',{name:'uav1',exact:true}).click();
 await aircraft.getByText('123.0 / 0.0 / 40.0').waitFor();
 await page.locator('#vehicle-input').fill('现在坐标？');
 await page.locator('#vehicle-input').press('Enter');
 await aircraft.getByText('uav1 状态已查询。').waitFor();
 assert.equal(requests[0].mode,'xd_vehicle');
 assert.equal(requests[0].vehicle_id,'uav1');
 await page.getByRole('button',{name:'uav2',exact:true}).click();
 await aircraft.getByText('123.0 / 120.0 / 0.0').waitFor();
 assert.equal(await aircraft.getByText('uav1 状态已查询。').count(),0);
 await page.getByRole('button',{name:'uav1',exact:true}).click();
 await aircraft.getByText('uav1 状态已查询。').waitFor();
 await page.locator('#vehicle-input').press('Escape');
 await aircraft.waitFor({state:'hidden'});
 await page.getByText('底图',{exact:true}).click();
 await page.locator('#basemap-source').selectOption('maplibre');
 await page.locator('#vector-map[data-ready="true"] canvas').waitFor({timeout:20000});
 assert.equal(await page.locator('#vector-map').getByText('MapLibre Test Attribution').count(),1);
 await page.waitForFunction(()=>document.querySelector('#basemap-status').textContent==='');
 const projectedOverlay = await page.locator('#flight-map').evaluate(canvas=>{
  const data=canvas.getContext('2d').getImageData(0,0,Math.min(canvas.width,40),Math.min(canvas.height,40)).data;
  return data.some((value,index)=>index%4===3&&value===0);
 });
 assert.ok(projectedOverlay,'Canvas must allow the MapLibre base to show through');
 const originalMap=await page.locator('.map-card').boundingBox();
 const conversation=await page.locator('.conversation').boundingBox();
 const fleet=await page.locator('.fleet-card').boundingBox();
 assert.ok(conversation.x >= originalMap.x + originalMap.width);
 assert.equal(fleet.x,conversation.x);
 assert.ok(fleet.y >= conversation.y + conversation.height && fleet.height < 190);
 assert.ok(Math.abs(fleet.y + fleet.height - originalMap.y - originalMap.height) <= 1);
 await page.locator('#zoom-out').evaluate(button=>{for(let i=0;i<20;i++)button.click();});
 const vectorCanvas=page.locator('#vector-map[data-ready="true"] canvas');
 await vectorCanvas.waitFor();
 await page.evaluate(()=>{document.documentElement.style.zoom='0.55';});
 await page.setViewportSize({width:1240,height:780});
 await vectorCanvas.waitFor();
 await page.evaluate(()=>{document.documentElement.style.zoom='';});
 await page.setViewportSize({width:420,height:860});
 await vectorCanvas.waitFor();
 await page.reload();
 await vectorCanvas.waitFor();
 assert.equal(await page.locator('#basemap-source').inputValue(),'maplibre');
 await page.setViewportSize({width:1500,height:910});
 await page.getByText('底图',{exact:true}).click();
 const loseContextSupported=await vectorCanvas.evaluate(canvas=>{
  const extension=canvas.getContext('webgl2')?.getExtension('WEBGL_lose_context');
  if (!extension) return false;
  canvas.addEventListener('webglcontextlost',()=>{
   setTimeout(()=>extension.restoreContext(),2000);
  },{once:true});
  extension.loseContext();
  return true;
 });
 if (loseContextSupported) {
  await page.getByText('已切换 OpenStreetMap 在线道路底图。',{exact:false}).waitFor();
  await vectorCanvas.waitFor({timeout:20000});
  await page.waitForFunction(()=>document.querySelector('#map-credit').hidden);
 }
 await page.locator('#basemap-source').selectOption('grid');
 await page.locator('#vector-map').waitFor({state:'hidden'});
 await page.locator('#basemap-source').selectOption('maptiler-streets');
 await page.getByText('请在 config/map_sources.local.json 填写 API Key，保存后刷新。').waitFor();
 assert.ok(tiles.every(tile=>tile.host==='tile.openstreetmap.org'));
 await page.locator('#basemap-source').selectOption('osm');
 await page.getByRole('link',{name:'© OpenStreetMap contributors'}).waitFor();
 await page.waitForFunction(()=>document.querySelector('#basemap-status').textContent==='');
 assert.ok(tiles.some(tile=>tile.host==='tile.openstreetmap.org' && /\/\d+\/\d+\/\d+\.png$/.test(tile.pathname)));
 await page.locator('#basemap-source').selectOption('grid');
 assert.equal(await page.locator('#map-credit').isVisible(),false);
 mapTilerKey = 'fake+browser/key';
 customStyle = 'https://styles.example.test/unavailable.json';
 await page.reload();
 await page.getByText('底图',{exact:true}).click();
 await page.locator('#basemap-source').selectOption('maplibre');
 await page.getByText('MapLibre 样式加载失败。已切换 OpenStreetMap 在线道路底图。').waitFor();
 await page.locator('#map-credit').getByRole('link',{name:'© OpenStreetMap contributors'}).waitFor();
 assert.equal(await page.locator('#vector-map').getAttribute('data-ready'),'false');
 await page.locator('#basemap-source').selectOption('maptiler-satellite');
 await page.getByText('在线底图加载失败，当前显示离线网格。请检查网络或 API Key。').waitFor();
 assert.ok(tiles.some(tile=>tile.pathname.startsWith('/maps/satellite-v4/256/')&&tile.searchParams.get('key')===mapTilerKey));
 await page.locator('#basemap-source').selectOption('grid');
 await page.getByText('底图',{exact:true}).click();
 await page.getByText('底图',{exact:true}).click();
 await page.locator('#map-file').setInputFiles({name:'sample.geojson',mimeType:'application/geo+json',
  buffer:Buffer.from(JSON.stringify({type:'LineString',coordinates:[[0,0],[80,80]]}))});
 await page.getByText('仿真 World · sample.geojson · 东 / 北坐标（m）').waitFor();
 await page.getByText('底图',{exact:true}).click();
 assert.equal(await page.locator('#flight-map').evaluate(c=>c.width>300 && c.height>200),true);

 await page.locator('#chat-input').fill('先规划道路巡检。');
 await page.locator('#chat-input').press('Enter');
 const trigger=page.getByRole('button',{name:'确认飞行',exact:true});
 await trigger.waitFor();
 await page.locator('#chat-input').fill('立即执行');
 await page.locator('#chat-input').press('Enter');
 await page.getByText('飞行回执 · 执行中').waitFor();
 assert.equal(await dispatch.count(),0);
 assert.equal(page.url(),url);
 assert.equal(page.context().pages().length,1);
 assert.equal(requests[2].conversation_id,requests[1].conversation_id);
 assert.equal(requests[2].plan_id,allocation.id);
 assert.equal(requests[2].op,'confirm');
 await page.reload();
 await page.locator('#chat-input').fill('重新规划。');
 await page.locator('#chat-input').press('Enter');
 await page.waitForFunction(()=>document.querySelectorAll('[data-plan-id]').length===2);
 assert.equal(await trigger.first().isDisabled(),true);
 await page.setViewportSize({width:390,height:790});
 assert.equal(await page.locator('#drawer').isVisible(),false);
 await page.setViewportSize({width:1500,height:910});
 assert.equal(await page.locator('#exercise').isVisible(),false);
 await page.locator('#task-library > summary').click();
 await page.locator('#library-mode').selectOption('point');
 let canvas=await page.locator('#flight-map').boundingBox();
 const X=canvas.x+canvas.width*.49, Y=canvas.y+canvas.height*.60;
 await page.mouse.move(X,Y);
 const initialCoordinate=await page.locator('#map-coordinate').textContent();
 await page.mouse.down({button:'middle'});
 await page.mouse.move(X+75,Y+42,{steps:4});
 await page.mouse.up({button:'middle'});
 await page.mouse.move(X,Y);
 assert.notEqual(await page.locator('#map-coordinate').textContent(),initialCoordinate);
 assert.equal(await page.locator('#library-mode').inputValue(),'point');
 assert.equal(await page.locator('#library-items li').count(),0);
 await page.mouse.click(X,Y);
 await page.locator('#library-mode').selectOption('area');
 await page.locator('#library-name').fill('河堤东段');
 canvas=await page.locator('#flight-map').boundingBox();
 await page.mouse.move(canvas.x+canvas.width*.63,canvas.y+canvas.height*.5);
 await page.mouse.down();
 await page.mouse.move(canvas.x+canvas.width*.70,canvas.y+canvas.height*.7,{steps:3});
 await page.mouse.up();
 await page.locator('#library-items').getByText('河堤东段 · 巡逻区').waitFor();
 await page.locator('#chat-input').fill('按地图点分配');
 await page.locator('#chat-input').press('Enter');
 await page.getByText('请先点击「提交任务库」。Qwen 使用提交后的任务。').waitFor();
 assert.equal(requests.length,4);
 await page.locator('#library-submit').click();
 await page.locator('#library-status').getByText('已提交 2 项',{exact:true}).waitFor();
 assert.equal(await page.locator('#library-mode').inputValue(),'area');
 assert.deepEqual(libraryRequests[0].tasks.map(t=>t.kind),['point','area']);
 await page.reload();
 await page.locator('#task-library > summary').click();
 await page.locator('#library-items').getByText('任务点1 · 任务点').click();
 await page.locator('#library-delete').click();
 await page.locator('#library-submit').click();
 await page.locator('#library-status').getByText('已提交 1 项',{exact:true}).waitFor();
 assert.deepEqual(libraryRequests[1].tasks.map(t=>t.label),['河堤东段']);
 await page.locator('#chat-input').fill('按已提交的任务库分配');
 await page.locator('#chat-input').press('Enter');
 await page.getByText('任务库分工 · 版本 2').waitFor();
 assert.equal(requests[4].mode,'xd_task_library');
 assert.equal(await page.getByRole('button',{name:'确认任务演练',exact:true}).count(),1);
 await page.locator('#chat-input').fill('立即执行');
 await page.locator('#chat-input').press('Enter');
 await page.getByText('这份草案只支持桌面演练，无法下发 Gazebo 飞行。请用当前程序重新生成飞行草案。').waitFor();
 assert.equal(requests.length,5);
 await page.getByRole('button',{name:'确认任务演练',exact:true}).click();
 await page.getByText('uav2 · 演练中 河堤东段').waitFor();
 assert.ok(await page.locator('.draft-card figure canvas').isVisible());
 await page.locator('#chat-input').fill('校验试验区外的任务');
 await page.locator('#chat-input').press('Enter');
 await page.getByText('已完成分工，飞行校验未通过。').waitFor();
 await page.locator('#chat-input').fill('立即执行');
 await page.locator('#chat-input').press('Enter');
 await page.getByText('河堤东段的短边须超过 315 米，才有两条固定翼巡检扫线',{exact:true}).last().waitFor();
 assert.equal(await page.getByText('河堤东段的短边须超过 315 米，才有两条固定翼巡检扫线',{exact:true}).count(),2);
 assert.equal(requests.at(-1).message,'校验试验区外的任务');
 assert.equal(await dispatch.count(),0);
 riverAvailable=true;
 await page.locator('#river-trial > summary').click();
 await page.locator('#river-primary').selectOption('uav1');
 await page.locator('#river-start').click();
 await page.waitForFunction(()=>!document.querySelector('#river-recall').disabled);
 await page.locator('#chat-input').fill('召回 uav1，由备用机接替');
 await page.locator('#chat-input').press('Enter');
 await page.getByText('请点击「召回 uav1 并自动接替」确认。',{exact:true}).waitFor();
 assert.deepEqual(riverOps.map(r=>r.op),['start','interpret']);
 await page.locator('#river-recall').click();
 await page.getByText('已收到返航请求',{exact:true}).waitFor();
 assert.deepEqual(riverOps.map(r=>r.op),['start','interpret','recall']);
 assert.ok(riverOps.every(r=>r.primary==='uav1'&&r.run_id===runId));
 currentRun='new-simulation';
 libraryState={run_id:currentRun,revision:0,tasks:[]};
 await page.locator('#task-library > summary').click();
 await page.getByText('任务库为空',{exact:true}).waitFor();
 assert.equal(await page.locator('#library-items li').count(),0);
 assert.deepEqual(errors,[]);
 console.log('PASS 总对话内确认、单机窗口、任务库绑定、旧草案拦截、地图与回执');
} finally {
 await browser.close();
}
