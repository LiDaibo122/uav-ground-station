import {setupTaskLibrary} from './xd-task-library.js';
import {setupRehearsal} from './xd-rehearsal.js';
import {requestChat} from './chat-stream.js';
import {FlightMap, colors, loadLayer} from './xd-map.js';
import {mapSources} from './xd-basemap.js';

const $ = id => document.getElementById(id);
function node(tag, text, className) {
 const item = document.createElement(tag);
 if (text != null) item.textContent = text;
 if (className) item.className = className;
 return item;
}
const n = (value, digits=1) => Number.isFinite(value) ? value.toFixed(digits) : '—';
const unit = (value, suffix) => Number.isFinite(value) ? n(value) + suffix : '—';
const clock = stamp => Number.isFinite(stamp) ? new Date(stamp * 1000).toLocaleTimeString('zh-CN', {hour12:false}) : '—';
const phases = {on_ground:'地面', in_air:'空中', taking_off:'起飞中', landing:'降落中'};
const outcomes = {running:'执行中', queued:'已排队', completed:'已完成', idle:'待命', failed:'失败',
 unknown:'待核对', partial:'部分完成', rejected:'未接单', preview:'待确认'};
const drawer = $('drawer');
const scopedChats = new Map();
const latestPlans = new Map();
const mainChat = {form:$('chat-form'), input:$('chat-input'), log:$('messages'), send:$('send'), mode:'xd_plan_only'};
const vehicleChat = {form:$('vehicle-form'), input:$('vehicle-input'), log:$('vehicle-messages'), send:$('vehicle-send'), mode:'xd_vehicle'};
const newId = () => crypto.randomUUID();
const welcome = mainChat.log.firstElementChild.cloneNode(true);
const exerciseOnly=new URLSearchParams(location.search).get('exercise')==='1';
let mainId;
let session;
let selectedVehicle;
let drawerTrigger;
let openPanel;
let draft;
let executedPlan;
let dispatchAvailable = false;
let busy = false;
let polling = false;
let since = 0;
let datum;
let vehicles = [];
let trails = {};
let chosenGeoJSON;
let layerCaption;
let sourceConfigError;
let providers = mapSources({});
let pollingError = false;
const basemapSelect = $('basemap-source');
function showBasemapStatus(text) {
 if ($('basemap-status').textContent !== text) $('basemap-status').textContent = text;
}
const flightMap = new FlightMap($('flight-map'), $('map-coordinate'), openVehicle, showBasemapStatus,
 active=>showMapCredit(active ? providers.osm : providers[basemapSelect.value]));

let riverState, riverBusy=false;
const riverPanel=$('river-trial');
let sketchAttached=false;
const exercise=exerciseOnly ? setupRehearsal(flightMap,attached=>{
 sketchAttached=attached;
 updateMap();
 queueMicrotask(refreshButtons);
}) : null;
const library=exerciseOnly ? null : setupTaskLibrary(flightMap,()=>{
 sketchAttached=library.active();
 updateMap();
 refreshButtons();
});


function renderRiver(s) {
 riverState=s;
 const primary=$('river-primary');
 primary.disabled=riverBusy || s.phase!=='ready';
 if(s.phase!=='ready')primary.value=s.primary||'uav2';
 $('river-recall').textContent='召回 '+primary.value+' 并自动接替';
 $('river-start').disabled=riverBusy || s.phase!=='ready';
 $('river-recall').disabled=riverBusy || s.phase!=='survey' || s.recall;
 $('river-events').textContent=s.events.at(-1)?.text || '待开始 · 需要两架新启动且可起飞的飞机';
 $('river-samples').textContent=s.samples.map(t=>t.name+' · '+(t.receipt ? t.receipt.vehicle+' 已采样' : (s.phase==='ready'?primary.value:t.owner)+' 待采样')).join('\n');
}

async function riverRequest(op,message) {
 const response=await fetch('/api/xd/river-trial',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({op,message,primary:$('river-primary').value,run_id:riverState?.run_id})});
 const data=await response.json();
 if(!response.ok)throw Error(data.error || '双机验收接口异常');
 return data;
}

async function riverCommand(op) {
 if(riverBusy)return;
 riverBusy=true;renderRiver(riverState);
 try{renderRiver(await riverRequest(op));}
 catch(e){message(mainChat.log,'双机验收',e.message);}
 finally{riverBusy=false;renderRiver(riverState);void poll();}
}
$('river-primary').onchange=()=>{if(riverState)renderRiver(riverState);};
$('river-start').onclick=()=>riverCommand('start');
$('river-recall').onclick=()=>riverCommand('recall');
riverPanel.addEventListener('toggle',()=>{
 if(riverPanel.open){$('task-library').open=false;void poll();}
});
$('task-library').addEventListener('toggle',()=>{if($('task-library').open)riverPanel.open=false;});
$('exercise').addEventListener('toggle',()=>{if($('exercise').open)riverPanel.open=false;});

async function api(path) {
 const response = await fetch(path, {signal: AbortSignal.timeout(4500)});
 const result = await response.json();
 if (!response.ok) throw new Error(result.error || '地面站接口暂不可用');
 return result;
}

function table(headers, rows) {
 const element = node('table'), head = element.createTHead().insertRow(), body = element.createTBody();
 for (const name of headers) head.append(node('th', name));
 for (const data of rows) {
  const row = body.insertRow();
  for (const value of data) row.insertCell().append(value instanceof Node ? value : document.createTextNode(String(value ?? '—')));
 }
 return element;
}

function position(vehicle) {
 const pos = vehicle.world_position_m;
 return pos ? [pos.x,pos.y,pos.z].map(value => n(value)).join(' / ') : '—';
}

function nextStep(mission) {
 if (!mission) return '—';
 const estimated = mission.next_point_estimate;
 if (estimated) return `估计下一点 · ${estimated.label || '航点 '+estimated.order}`;
 return mission.next_action || '—';
}

function missionLine(mission) {
 if (!mission) return '待命';
 const pct = mission.route_progress_pct;
 return mission.stage + (Number.isFinite(pct) ? ` · 航线 ${n(pct)}%` : '');
}

function flightPhase(vehicle) {
 if (!vehicle.connected) return '遥测待更新';
 return phases[vehicle.flight_phase] || '状态待核对';
}

function renderVehicle() {
 const v = vehicles.find(item => item.vehicle === selectedVehicle);
 if (!v) return;
 $('vehicle-title').textContent = v.vehicle + ' · 飞机详情';
 const mission = v.mission;
 const rows = [
  ['东 / 北 / 高（m）', position(v)],
  ['飞行阶段', flightPhase(v) + (v.armed == null ? '' : v.armed ? ' · 已解锁' : ' · 已上锁')],
  ['模式 / 定位', (v.mode || '—') + ' / ' + (v.localization_valid ? '有效' : '待确认')],
  ['电量 / 电压', unit(v.battery_pct,'%') + ' / ' + unit(v.battery_voltage_v,' V')],
  ['地速 / 垂直速度', unit(v.speed_xy_mps,' m/s') + ' / ' + unit(v.climb_mps,' m/s')],
  ['航向', Number.isFinite(v.world_heading_rad) ? n((450-v.world_heading_rad*180/Math.PI)%360,0) + '°' : '—'],
  ['任务', missionLine(mission)],
  ['下一步', nextStep(mission)],
 ];
 if (mission?.next_point_estimate) rows.push(['下一点（估计）', mission.next_point_estimate.world_m.join(' / ') + ' m']);
 if (mission?.detail) rows.push(['最新回执', mission.detail]);
 rows.push(['位置更新时间', clock(v.position_observed_at)]);
 const list = node('dl', undefined, 'facts');
 for (const [label,value] of rows) list.append(node('dt',label), node('dd',value));
 $('vehicle-facts').replaceChildren(list);
}

function renderFleet() {
 const body = $('fleet');
 const active = document.activeElement?.dataset.vehicle;
 const fragment = document.createDocumentFragment();
 vehicles.forEach((v,i) => {
  const row = node('tr');
  const trigger = node('button',v.vehicle,'vehicle-button');
  trigger.type = 'button';
  trigger.dataset.vehicle = v.vehicle;
  trigger.style.setProperty('--vehicle-color',colors[i%colors.length]);
  trigger.setAttribute('aria-controls', 'vehicle-panel');
  trigger.setAttribute('aria-expanded', String(openPanel === 'vehicle-panel' && selectedVehicle === v.vehicle));
  trigger.addEventListener('click', () => openVehicle(v.vehicle, trigger));
  row.append(node('td'), node('td',flightPhase(v)), node('td',position(v)), node('td',unit(v.battery_pct,'%')),
   node('td',missionLine(v.mission)));
  row.cells[0].append(trigger);
  row.cells[1].append(node('span',v.mode || '—','secondary-line'));
  const next = nextStep(v.mission);
  if (next !== '—') row.cells[4].append(node('span',next,'secondary-line'));
  fragment.append(row);
 });
 if (vehicles.length) body.replaceChildren(fragment);
 if (active) {
  [...body.querySelectorAll('button')].find(b => b.dataset.vehicle === active)?.focus({preventScroll:true});
 }
 renderVehicle();
}

function showPanel(name, trigger) {
 drawerTrigger?.setAttribute('aria-expanded','false');
 openPanel = name;
 drawerTrigger = trigger;
 drawerTrigger?.setAttribute('aria-expanded','true');
 drawer.hidden = false;
 $('vehicle-panel').hidden = name !== 'vehicle-panel';
 if (name === 'vehicle-panel') vehicleChat.input.focus({preventScroll:true});
}

function closePanel() {
 if (!openPanel) return;
 const trigger = drawerTrigger;
 const wasVehicle = openPanel === 'vehicle-panel';
 openPanel = null;
 drawer.hidden = true;
 drawerTrigger = null;
 trigger?.setAttribute('aria-expanded','false');
 if (trigger?.isConnected) trigger.focus({preventScroll:true});
 else if (wasVehicle) [...$('fleet').querySelectorAll('button')].find(b => b.dataset.vehicle === selectedVehicle)?.focus();
 renderFleet();
}

function openVehicle(id, trigger) {
 if (selectedVehicle !== id && busy) return;
 selectedVehicle = id;
 if (!scopedChats.has(id)) scopedChats.set(id,{id: newId(), log: node('div'), input: ''});
 const chat = scopedChats.get(id);
 vehicleChat.log.replaceChildren(chat.log);
 vehicleChat.input.value = chat.input;
 renderVehicle();
 showPanel('vehicle-panel',trigger);
 renderFleet();
}

function draftRows(plan, coordinates=false) {
 const tasks = new Map(plan.tasks.map(task=>[task.id, task]));
 const rows = Object.entries(plan.assignments).map(([name,ids])=>[
  name, ids.map(id=>{
   const task=tasks.get(id);
   if (!task) return id;
   if (coordinates && task.area) {
    const a=task.area;
    return `${task.label}（东 ${a.west}～${a.east}，北 ${a.south}～${a.north}）`;
   }
   return coordinates && Number.isFinite(task.east_m) && Number.isFinite(task.north_m)
    ? `${task.label} (${task.east_m}, ${task.north_m})` : task.label;
  }).join(' → ') || (plan.fleet?.find(aircraft=>aircraft.id===name)?.reasons?.join('、') || '本轮未分配 · 待命'),
 ]);
 for (const [id, reason] of Object.entries(plan.unassigned || {})) rows.push(['待分配', `${tasks.get(id)?.label || id} · ${reason}`]);
 return rows;
}

function replayProgress(state) {
 return Object.entries(state.lanes).map(([uid,lane])=>{
  const task=state.tasks[lane.current];
  return uid+' · '+(task?'演练中 '+task.label:'演练完成');
 }).join('　');
}

// This inset depicts a clock-driven exercise in its own frame, separate from the telemetry map.
function drawReplay(canvas,state) {
 const lines=[];
 for(const [uid,lane] of Object.entries(state.lanes)){
  if(!lane.origin || !lane.route)continue;
  const path=[lane.origin];
  let travelled=0;
  for(const id of lane.route){
   const t=state.tasks[id], a=t.area, section=[path.at(-1)];
   if(a){
    for(let i=0;i<4;i++){
     const y=a.south+(a.north-a.south)*(i+.5)/4;
     section.push([i%2?a.east:a.west,y],[i%2?a.west:a.east,y]);
    }
   }else section.push(t.center);
   const lengths=section.slice(1).map((p,i)=>Math.hypot(p[0]-section[i][0],p[1]-section[i][1]));
   const length=lengths.reduce((x,y)=>x+y,0);
   if(t.status==='completed')travelled+=length;
   if(lane.current===id){
    const fraction=Math.max(0,Math.min(1,1-(lane.due-Date.now()/1000)/(a?90:45)));
    travelled+=length*fraction;
   }
   path.push(...section.slice(1));
  }
  lines.push({uid,path,travelled});
 }
 if(!lines.length)return;
 const ctx=canvas.getContext('2d'), all=lines.flatMap(l=>l.path);
 const xs=all.map(p=>p[0]),ys=all.map(p=>p[1]),xmin=Math.min(...xs),ymin=Math.min(...ys);
 const dx=Math.max(...xs)-xmin,dy=Math.max(...ys)-ymin,w=canvas.width,h=canvas.height;
 const scale=Math.min((w-85)/Math.max(1,dx),(h-40)/Math.max(1,dy));
 const project=([x,y])=>[20+(x-xmin)*scale,h-18-(y-ymin)*scale];
 ctx.clearRect(0,0,w,h);
 lines.forEach(({uid,path,travelled},i)=>{
  ctx.strokeStyle=ctx.fillStyle=colors[i%colors.length];
  ctx.setLineDash([4,4]);ctx.beginPath();
  let marker=path[0], left=travelled;
  path.forEach((p,j)=>{
   const [x,y]=project(p);
   if(j===0){ctx.moveTo(x,y);return;}
   ctx.lineTo(x,y);
   const a=path[j-1],len=Math.hypot(p[0]-a[0],p[1]-a[1]);
   if(left>0 && len){
    const f=Math.min(1,left/len);
    marker=[a[0]+(p[0]-a[0])*f,a[1]+(p[1]-a[1])*f];left-=len;
   }
  });
  ctx.stroke();ctx.setLineDash([]);
  const [x,y]=project(marker);
  ctx.beginPath();ctx.arc(x,y,4,0,2*Math.PI);ctx.fill();ctx.font='13px sans-serif';ctx.fillText(uid,x+7,y-4);
 });
}

function flightReceipt(element,receipt) {
 element.textContent='飞行回执 · '+(outcomes[receipt.status]||receipt.status);
 const rows=table(['飞机','执行状态'],Object.entries(receipt.vehicles).map(([uid,row])=>
  [uid,row.detail || outcomes[row.phase] || row.phase]));
 element.append(rows);
 if(receipt.status==='running')element.dataset.flight=receipt.plan_id;
 else delete element.dataset.flight;
}

function draftCard(plan, conversation=mainId) {
 const card=node('div',undefined,'draft-card');
 const replay=plan.kind==='library_replay', action=plan.kind==='local_action';
 card.dataset.execution = replay ? 'replay' : 'flight';
 const title=action ? plan.vehicle+' · '+(plan.action==='takeoff'?'起飞盘旋（相对高度 40 m）':'返航降落') :
  plan.library_revision!=null ? '任务库分工 · 版本 '+plan.library_revision : 'CBBA 分配 · '+plan.tasks.length+' 个巡检任务';
 card.append(node('h3',title));
 if(!action) card.append(table(['飞机','任务顺序'],draftRows(plan,true)));
 const replayReason=plan.tasks?.some(t=>t.area)?'包含巡逻区':'任务点在本机点巡检试验区外，或当前仅接入遥测';
 const receipt=node('div',replay?replayReason+'。桌面演练按任务时钟推进，Gazebo 飞机保持原状态。': '本机 Gazebo / PX4 · 等待确认');
 card.append(receipt);
 if (plan.flight_blocked) {
  card.dataset.blocked = plan.flight_blocked;
  receipt.textContent=plan.flight_blocked;
  return card;
 }
 if (!plan.id || (!replay && !dispatchAvailable)) return card;
 const button=node('button',replay?'确认任务演练':'确认飞行','primary');
 button.type='button';
 button.dataset.planId=plan.id;
 button.dataset.conversation=conversation;
 if(plan.library_revision!=null)button.dataset.revision=String(plan.library_revision);
 button.disabled=Boolean(Object.keys(plan.unassigned || {}).length);
 if(button.disabled){
  card.dataset.blocked = '有任务未分配，请调整分组后重新规划';
  receipt.textContent = card.dataset.blocked;
  return card;
 }
 const run=session;
 button.onclick=async()=>{
  if(button.disabled || busy)return;
  busy=true;refreshButtons();
  try{
   const response=await fetch('/api/xd/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({conversation_id:conversation,run_id:run,plan_id:plan.id,window:plan.window || null})});
   const value=await response.json();
   if(!response.ok)throw Error(value.error || '未取得确认回执');
   if(run!==session)return;
   if(value.kind==='library_replay'){
    receipt.dataset.replay=plan.id;
    receipt.textContent=replayProgress(value.state);
    const figure=node('figure'), canvas=node('canvas');
    canvas.width=480;canvas.height=190;canvas.style.width='100%';
    figure.append(node('figcaption','二维演练示意 · 虚线为巡检路线'),canvas);
    card.append(figure);
    drawReplay(canvas,value.state);
   }else if(value.kind==='local_action'){
    receipt.textContent=value.vehicle+' · '+(outcomes[value.status]||value.status)+' · 执行编号 '+value.execution_id;
   }else{
    flightReceipt(receipt,value.receipt);
   }
   button.remove();
  }catch(error){receipt.textContent=error.message;}
  finally{busy=false;refreshButtons();void poll();}
 };
 card.append(button);
 return card;
}

function targetPoints() {
 if (draft?.tasks) return draft.tasks.filter(t=>Number.isFinite(t.east_m)&&Number.isFinite(t.north_m))
  .map(t=>({x:t.east_m,y:t.north_m,label:t.label}));
 return (executedPlan?.task_specs || []).map(t=>({x:t.east,y:t.north,label:t.label}))
  .filter(t=>Number.isFinite(t.x)&&Number.isFinite(t.y));
}

function updateMap() {
 let targets=sketchAttached ? [] : targetPoints();
 if(riverPanel.open && !riverPanel.hidden && riverState?.run_id===session){
  targets=riverState.samples.filter(s=>s.position).map(s=>({...s.position,label:s.name}));
 }
 flightMap.setData({vehicles,trails,targets});
}

function sessionChanged(run) {
 session = run;
 mainId = sessionStorage.getItem('xd-console-id:'+run) || newId();
 sessionStorage.setItem('xd-console-id:'+run,mainId);
 try {draft=JSON.parse(sessionStorage.getItem('xd-console-draft:'+mainId));}
 catch {draft=null;}
 if (draft?.run_id !== run) draft=null;
 executedPlan=null;
 trails={};
 since=0;
 closePanel();
 scopedChats.clear();
 selectedVehicle=null;
 mainChat.log.replaceChildren(welcome.cloneNode(true));
 latestPlans.clear();
 if (draft) {
  const conversation=draft.conversation_id || mainId;
  latestPlans.set(conversation,draft.id);
  mainChat.log.append(draftCard(draft,conversation));
 }
 void library?.load(run);
 refreshButtons();
}

async function poll() {
 if (exerciseOnly || polling || document.hidden) return;
 polling=true;
 try {
  let snapshot = await api('/api/xd/console?since='+Math.max(0,since-3));
  dispatchAvailable = snapshot.dispatch_available;
  if (session !== snapshot.run_id) {
   const previousCursor=since;
   sessionChanged(snapshot.run_id);
   if (previousCursor) snapshot=await api('/api/xd/console');
  }
  vehicles = snapshot.vehicles;
  riverPanel.hidden=!snapshot.river_trial;
  if(riverPanel.open && snapshot.river_trial && !riverBusy){
   try{renderRiver(await api('/api/xd/river-trial'));}
   catch(e){$('river-events').textContent=e.message;}
  }
  $('preview-exercise-fleet').hidden=!vehicles.length;
  const datumChanged = datum?.lat !== snapshot.datum?.lat || datum?.lon !== snapshot.datum?.lon ||
   datum?.altitude_m !== snapshot.datum?.altitude_m;
  datum = snapshot.datum;
  $('map-coordinates').options[1].disabled = !datum;
  if (datumChanged) {
   if ($('map-coordinates').value === 'wgs84') drawLayer();
   activateBasemap();
  }
  for (const [id,list] of Object.entries(snapshot.trails)) {
   const items = trails[id] || [];
   const last = items.at(-1)?.observed_at || 0;
   trails[id] = items.concat(list.filter(t=>t.observed_at>last)).slice(-1200);
   since=Math.max(since,trails[id].at(-1)?.observed_at || 0);
  }
  pollingError=false;
  const online=vehicles.filter(v=>v.connected).length;
  $('flight-state').textContent=`遥测 ${online}/${vehicles.length} 在线`;
  $('flight-state').classList.toggle('online',online>0);
  const latest = Math.max(0,...vehicles.map(v=>v.position_observed_at||0));
  $('last-update').textContent=latest ? '更新时间 ' + clock(latest) : '等待定位';
  renderFleet();
  const planId=vehicles.find(v=>v.job?.action==='inspection' && v.job.plan_id)?.job.plan_id;
  if (!draft && dispatchAvailable && planId && executedPlan?.plan_id!==planId) {
   try {executedPlan=await api('/api/xd/executions/'+encodeURIComponent(planId));}
   catch { /* Live position display is independent of mission history. */ }
  }
  updateMap();
  for(const element of document.querySelectorAll('[data-flight]')){
   try{flightReceipt(element,await api('/api/xd/executions/'+element.dataset.flight));}
   catch { /* Keep the last confirmed receipt while the connection recovers. */ }
  }
  for(const element of document.querySelectorAll('[data-replay]')){
   try{
    const state=await api('/api/xd/replays/'+element.dataset.replay);
    element.textContent=replayProgress(state);
    const canvas=element.parentElement.querySelector('figure canvas');
    if(canvas)drawReplay(canvas,state);
    if(Object.values(state.lanes).every(l=>l.phase==='finished'))delete element.dataset.replay;
   }catch {element.textContent='演练进度暂不可用';}
  }
 } catch {
  pollingError=true;
  $('flight-state').textContent='实时遥测暂不可用';
  $('flight-state').classList.remove('online');
  $('last-update').textContent='保留上次数据';
 } finally {
  polling=false;
  refreshButtons();
 }
}

async function health() {
 try {
  const state=(await api('/api/health')).model;
  $('model-state').textContent=state.available?state.model+' 在线':'Qwen 未连接';
  $('model-state').classList.toggle('online',state.available);
 } catch {
  $('model-state').textContent='Qwen 未连接';
  $('model-state').classList.remove('online');
 }
}

function refreshButtons() {
 [$('new-conversation'),mainChat.send,vehicleChat.send].forEach(button=>button.disabled=busy || !session);
 if (exercise?.active()) mainChat.send.disabled=busy;
 for(const b of document.querySelectorAll('[data-plan-id]')){
  b.disabled=busy || pollingError || latestPlans.get(b.dataset.conversation)!==b.dataset.planId ||
   (b.dataset.revision!=null && (!library?.active() || library.pending() ||
       library.context().library_revision!==Number(b.dataset.revision)));
 }

}

function scroll(log) {log.scrollTop=log.scrollHeight;}
function message(log,label,body,kind) {
 const item=node('article',undefined,'message'+(kind ? ' '+kind : ''));
 item.append(node('span',label,'message-label'),node('p',body));
 log.append(item);
 return item;
}

function showResult(item, result, chat) {
 const plan=result.draft;
 if (plan?.id && plan.run_id===session) {
  const conversation=result.conversation_id;
  for (const old of item.parentElement.querySelectorAll('[data-plan-id]')) latestPlans.delete(old.dataset.conversation);
  if (plan.library_revision!=null && !library?.matches(plan)){
   item.append(node('p','任务库已变化，请用最新版本重新分配。'));
  } else {
   latestPlans.set(conversation,plan.id);
   if (chat===mainChat){
    draft={...plan,conversation_id:conversation};
    sessionStorage.setItem('xd-console-draft:'+mainId,JSON.stringify(draft));
    updateMap();
   }
   item.append(draftCard(plan,conversation));
  }
 }
 for (const call of result.tool_calls || []) {
  const value=call.result?.result;
  if (call.status !== 'success' || !value) continue;
  if (call.tool==='xd_library_tasks') {
   item.append(table(['名称','类型'],value.tasks.map(t=>[t.label,t.kind==='area'?'巡逻区':'任务点'])));
  } else if (call.tool==='xd_state') {
   const list=value.vehicles || [value];
   item.append(table(['飞机','东 / 北 / 高 m','电量','任务 / 下一步'],list.map(v=>[
    v.vehicle,position(v),unit(v.battery_pct,'%'),missionLine(v.mission)+'\n'+nextStep(v.mission)])));
  } else if (call.tool==='xd_allocation_status' || call.tool==='xd_confirm_allocation') {
   item.append(node('small','集群回执 · '+(outcomes[value.status]||value.status)),
    table(['飞机','状态','最新回执'],Object.entries(value.vehicles).map(([id,row])=>[
     id,outcomes[row.phase]||row.phase,row.detail || (row.phase==='idle'?'本轮未分配任务':'—')])));
  }
 }
 if (result.tool_calls?.length) {
  const record=node('details');
  record.append(node('summary',`工具记录 · ${result.tool_calls.length} 次`),
   node('pre',JSON.stringify(result.tool_calls,null,2).slice(0,16000)));
  item.append(record);
 }
}

async function send(chat, override) {
 if (busy) return;
 const text=(override || chat.input.value).trim();
 if (!text) return;
 if(chat===mainChat && riverPanel.open && !riverPanel.hidden && /双机验收|河堤验收|接替/.test(text)){
  if(!riverState)return;
  busy=true;refreshButtons();
  message(chat.log,'你 · 双机河堤验收',text,'user');
  try{
   const result=await riverRequest('interpret',text);
   const reply={start:'请核对场景，点击「开始双机验收」。',recall:'请点击「'+$('river-recall').textContent+'」确认。',status:
    $('river-events').textContent+'。'+$('river-samples').textContent};
   message(chat.log,'双机验收',reply[result.proposal]);
   chat.input.value='';
  }catch(e){message(chat.log,'双机验收',e.message);}
  finally{busy=false;refreshButtons();scroll(chat.log);}
  return;
 }
 if (chat===mainChat && exercise?.active()) {
  busy=true;refreshButtons();
  message(chat.log,exercise.attached()?'你 · 地图标绘':'你 · 桌面演练',text,'user');
  const pending=message(chat.log,'Qwen','正在读取本轮地图标绘和任务进度…');
  try {pending.lastElementChild.textContent=await exercise.interpret(text,vehicles.length>0);chat.input.value='';}
  catch(e){pending.lastElementChild.textContent=e.message;}
  finally{busy=false;refreshButtons();scroll(chat.log);}
  return;
 }
 if (!session) return;
 const scope = chat === vehicleChat ? scopedChats.get(selectedVehicle) : null;
 if (chat===vehicleChat && !scope) return;
 const log=scope ? scope.log : chat.log;
 if (/^(?:确认飞行|确认执行|确认任务演练|立即执行|立即开始执行|开始执行|执行任务)[。！! ]*$/.test(text)) {
  const card=Array.from(log.querySelectorAll('.draft-card')).at(-1);
  const button=card?.querySelector('[data-plan-id]');
  const wantedReplay=text.startsWith('确认任务演练');
  const isReplay=card?.dataset.execution==='replay';
  chat.input.value='';
  if (scope) scope.input='';
  message(log,'你',text,'user');
  if (button && !button.disabled && isReplay===wantedReplay) {
   button.click();
  } else {
   const reason=card?.dataset.blocked || (isReplay && !wantedReplay
    ? '这份草案只支持桌面演练，无法下发 Gazebo 飞行。请用当前程序重新生成飞行草案。'
    : '当前没有可确认的草案。请核对最新草案、任务库版本或已有执行回执。');
   message(log,'地面站',reason);
  }
  scroll(chat.log);
  return;
 }
 const useLibrary=library?.active();
 if(useLibrary && library.pending()){
  message(chat.log,'地面站','请先点击「提交任务库」。Qwen 使用提交后的任务。');
  scroll(chat.log);
  return;
 }
 const run=session;
 const request={message:text,mode:chat.mode,conversation_id:scope?.id || mainId};
 if(useLibrary){
  const context=library.context();
  const key='xd-library-chat:'+(scope?.id || mainId)+':'+context.library_revision;
  const id=sessionStorage.getItem(key) || newId();
  sessionStorage.setItem(key,id);
  Object.assign(request,context,{mode:scope?'xd_vehicle':'xd_task_library',conversation_id:id});
 }
 if (scope) request.vehicle_id=selectedVehicle;

 busy=true;
 refreshButtons();
 chat.input.value='';
 if (scope) scope.input='';
 if (chat===mainChat) chat.log.querySelector('.welcome')?.remove();
 message(log,'你',text,'user');
 const pending=node('p','Qwen 正在处理…','pending');
 log.append(pending);
 scroll(chat.log);
 let preview;
 try {
  const result=await requestChat(request,event=>{
   if (event.type==='tool_start') pending.textContent=event.label || '正在查询…';
   if (event.type==='text_delta') {
    if (!preview) preview=message(log,'Qwen · 生成中','');
    preview.lastElementChild.textContent+=event.text;
    scroll(chat.log);
   }
  });
  preview?.remove();
  if (session!==run) return;
  const hasDraft=Boolean(result.draft?.id);
  const answer=hasDraft
   ? (result.draft.flight_blocked ? '已完成分工，飞行校验未通过。' : '已生成草案，请核对下方分工或单机动作。')
   : result.reply;
  const item=message(log,hasDraft?'地面站':result.source==='controller'?'执行回执':'Qwen',answer);
  const paragraph = item.querySelector('p');
  const pieces = paragraph.textContent.split(/\*\*([^*]+)\*\*/g);
  paragraph.replaceChildren(...pieces.map((text,index)=>index%2 ? node('strong',text) : document.createTextNode(text)));
  showResult(item,{...result,conversation_id:request.conversation_id},chat);
 } catch (error) {
  preview?.remove();
  message(log,'地面站',error.message).lastElementChild.classList.add('error');
 } finally {
  pending.remove();
  busy=false;
  refreshButtons();
  scroll(chat.log);
  void poll();
 }
}

for (const chat of [mainChat,vehicleChat]) {
 chat.form.addEventListener('submit',event=>{event.preventDefault();void send(chat);});
 chat.input.addEventListener('keydown',event=>{
  if (event.key==='Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault();void send(chat);}
 });
}
vehicleChat.input.addEventListener('input',()=>{
 const scope=scopedChats.get(selectedVehicle);
 if (scope) scope.input=vehicleChat.input.value;
});
$('close-vehicle').addEventListener('click',closePanel);
drawer.addEventListener('keydown',e=>{if (e.key==='Escape' && !e.isComposing) closePanel();});
$('messages').addEventListener('click',e=>{
 const prompt=e.target.closest('[data-prompt]');
 if (prompt) {mainChat.input.value=prompt.dataset.prompt;mainChat.input.focus();}
});
$('new-conversation').addEventListener('click',()=>{
 mainId=newId();
 sessionStorage.setItem('xd-console-id:'+session,mainId);
 draft=null;
 closePanel();
 mainChat.log.replaceChildren(welcome.cloneNode(true));
 mainChat.input.value='';
 updateMap();
 mainChat.input.focus();
});

function refreshMapCaption() {
 const basemapName = providers[basemapSelect.value]?.name || '仿真 World';
 $('map-layer').textContent = (layerCaption ? basemapName + ' · ' + layerCaption : basemapName) +
  ' · 东 / 北坐标（m）';
}

function showMapCredit(provider) {
 const credit = $('map-credit');
 credit.replaceChildren();
 credit.hidden = true;
 if (provider?.credits.length) {
  if (provider.logo) {
   const logo = node('img');
   logo.alt = 'MapTiler';
   logo.src = provider.logo;
   credit.append(logo);
  }
  for (const [name,url] of provider.credits) {
   const link = node('a',name);
   link.href=url;
   link.target='_blank';
   link.rel='noopener';
   credit.append(link);
  }
  credit.hidden = false;
 }
}

function activateBasemap() {
 const id = basemapSelect.value;
 const provider = providers[id];
 const configured = provider?.url || provider?.styleUrl;
 const available = configured && datum;
 showBasemapStatus('');
 flightMap.setBasemap(available ? provider : null,datum,providers.osm);
 showMapCredit(flightMap.usingFallback ? providers.osm : available ? provider : null);
 if (id !== 'grid' && !available) {
  showBasemapStatus(!configured ?
   (sourceConfigError || '请在 config/map_sources.local.json 填写 API Key，保存后刷新。') :
   '等待仿真地理原点，当前显示离线网格。');
 }
 refreshMapCaption();
}

async function configureBasemaps() {
 try {
  providers = mapSources(await api('/api/xd/map-sources'));
  if (providers.custom) {
   const option = node('option',providers.custom.name);
   option.value='custom';
   basemapSelect.append(option);
  }
 } catch (error) {
  sourceConfigError=error.message;
 }
 const previous=sessionStorage.getItem('xd-basemap-source');
 if (Array.from(basemapSelect.options).some(option=>option.value===previous)) basemapSelect.value=previous;
 activateBasemap();
}

basemapSelect.addEventListener('change',()=>{
 sessionStorage.setItem('xd-basemap-source',basemapSelect.value);
 activateBasemap();
});
void configureBasemaps();

function drawLayer() {
 if (!chosenGeoJSON) return;
 const {name, data}=chosenGeoJSON;
 try {
  const geographic=$('map-coordinates').value==='wgs84';
  if (geographic && !datum) {
   flightMap.setLayer([]);
   layerCaption=null;
   refreshMapCaption();
   throw new Error('等待 World 地理原点。');
  }
  flightMap.setLayer(loadLayer(data,geographic?datum:null));
  layerCaption=name;
  refreshMapCaption();
  $('map-notice').textContent='';
 } catch (error) {$('map-notice').textContent=error.message;}
}
$('map-file').addEventListener('change',async(event)=>{
 const file=event.target.files[0];
 if (!file) return;
 try {
  if (file.size>2*1024*1024) throw new Error('请选择小于 2 MB 的 GeoJSON。');
  chosenGeoJSON={name:file.name,data:JSON.parse(await file.text())};
  drawLayer();
 } catch (error) {$('map-notice').textContent=error.message;}
 finally {event.target.value='';}
});
$('map-coordinates').addEventListener('change',drawLayer);
$('remove-layer').addEventListener('click',()=>{
 chosenGeoJSON=null;
 flightMap.setLayer([]);
 layerCaption=null;
 refreshMapCaption();
 $('map-notice').textContent='';
});
$('zoom-in').addEventListener('click',()=>flightMap.zoom(1.3));
$('zoom-out').addEventListener('click',()=>flightMap.zoom(1/1.3));
$('fit-map').addEventListener('click',()=>flightMap.fit());
$('exercise').addEventListener('toggle',refreshButtons);
$('exercise').addEventListener('change',refreshButtons);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){void poll();void health();}});
if (exerciseOnly) {
 $('exercise').hidden=false;
 $('exercise').open=true;
 $('exercise-chat').checked=true;
 $('flight-state').textContent='桌面演练';
 $('fleet').rows[0].cells[0].textContent='模拟机队进度见地图标绘栏';
 $('map-layer').textContent='演练 world 东 / 北坐标（米）';
}
else {$('task-library').hidden=false;}
refreshButtons();
void poll();
void health();
setInterval(poll,1250);
setInterval(health,15000);
