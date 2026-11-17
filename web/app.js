import { GroundMap } from './map.js';
import { requestChat } from './chat-stream.js';
const $=s=>document.querySelector(s);
const $$=s=>[...document.querySelectorAll(s)];
let map,scene,state,selectedId='UAV-01',mode=new URLSearchParams(location.search).get('mode')==='live'?'live':'demo',paused=false,busy=false;
let swarm=null;
let history=[],drafts=[],tasks=[];
let conversationId=sessionStorage.getItem('fieldlink-conversation-id')||crypto.randomUUID();
sessionStorage.setItem('fieldlink-conversation-id',conversationId);
let toastTimer,health=null;
const icon=name=>`<svg><use href="#i-${name}"/></svg>`;
const el=(tag,className,text)=>{const e=document.createElement(tag);if(className)e.className=className;if(text!==undefined)e.textContent=text;return e;};

async function api(path,body){
  const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw new Error(data.error || '服务暂时不可用');return data;
}
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,5000);}
function mapNotice(text){$('#map-notice span').textContent=text;$('#map-notice').hidden=false;}
function updateMapStatus(text,ok){$('#terrain-status').textContent=text;$('#connection-map-state').textContent=ok?'已连接':'待检查';}
function tab(name){$$('[data-tab]').forEach(b=>{b.classList.toggle('active',b.dataset.tab===name);b.setAttribute('aria-selected',String(b.dataset.tab===name));});$$('.tab-content').forEach(p=>{const active=p.id==='tab-'+name;p.classList.toggle('active',active);p.hidden=!active;});}
function selectAircraft(id,focus=false){selectedId=id;renderFleet();if(state)map?.updateAircraft(state.aircraft,id);if(focus)map?.focus(id);}

function renderFleet(){
  if(!state)return;
  const list=$('#fleet-list');list.replaceChildren();
  $('#fleet-count').textContent=String(state.aircraft.length).padStart(2,'0');
  if(!state.aircraft.length){const p=el('div','empty-state','等待 PX4 遥测\n启动集群仿真或 ROS 接收适配器后显示飞机');list.append(p);}
  state.aircraft.forEach((a,index)=>{
    const row=el('button','aircraft-row'+(a.id===selectedId?' selected':''));row.dataset.id=a.id;row.setAttribute('aria-label',`选择 ${a.id}`);
    row.style.setProperty('--aircraft-color',a.color||['#99cfb0','#e2b777','#9dbbe0'][index%3]);
    const mark=el('span','aircraft-icon');mark.innerHTML=icon('plane');
    const phase=a.armed===false&&a.landed===true?'地面待命 · ':a.armed===true&&a.landed===false?'飞行中 · ':a.armed===true&&(a.relative_alt_m??0)>3?'起飞/爬升 · ':a.armed===true?'已解锁 · ':'';
    const info=el('span','aircraft-info');info.append(el('strong','',a.id),el('small','',a.stale?'遥测已过期 · '+a.age_s+'s':phase+a.task));
    const battery=el('span','aircraft-battery',a.battery_pct===null?'—':Math.round(a.battery_pct)+'%');
    const track=el('span','battery-track');const bar=el('i');bar.style.width=(a.battery_pct??0)+'%';track.append(bar);battery.append(track);
    row.append(mark,info,battery);row.onclick=()=>selectAircraft(a.id,true);list.append(row);
  });
  const selected=state.aircraft.find(a=>a.id===selectedId)||state.aircraft[0];
  $('#selected-card').hidden=!selected;
  if(selected){
    selectedId=selected.id;$('#selected-id').textContent=selected.id;
    $('#selected-altitude').replaceChildren(document.createTextNode(Math.round(selected.altitude_m).toLocaleString()+' '),el('small','','m'));
    $('#selected-speed').replaceChildren(document.createTextNode(selected.speed_mps===null?'— ':Math.round(selected.speed_mps)+' '),el('small','','m/s'));
    $('#altitude-label').textContent=mode==='demo'?'演示高度':selected.altitude_reference==='WGS84_ellipsoid'?'椭球高':'上报高度';
  }
  $('#fleet-source').textContent=mode==='demo'?'数据源：演示飞行器':'数据源：PX4 / ROS';
  $('.initial-bubble p').textContent=mode==='demo'?'当前是演示模式，可讨论巡检需求和方案。进入 PX4 模式并启动固定翼仿真后，可通过对话操作飞机。':'直接描述你的想法。可以查询状态、修改方案、比较算法，再决定是否执行；Qwen 会选择相应工具。';
  $('.initial-bubble .bubble-note').textContent='例如：先让一号上天转一会儿，另外两架先别动。';
  $('#mode-switch').innerHTML=(mode==='demo'?'切换 PX4':'查看演示')+icon('chevron');
  $('#data-mode-badge').textContent=mode==='demo'?'演示航迹 · 非 PX4':state.px4_connected?'PX4 遥测':'PX4 遥测 · 等待接入';
  $('#data-mode-badge').classList.toggle('demo-tag',mode==='demo');
  $('#footer-source').textContent=mode==='demo'?'演示模式 · 无飞控下发':health?.flight_command_transport?'PX4 仿真 · 对话调度':'PX4 遥测模式 · 只读';
  if(mode==='demo'){
    const seconds=Math.floor(state.simulation_time_s);$('#demo-time').textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
  }else $('#demo-time').textContent=state.px4_connected?'已接入':'等待中';
  $('#playback-label').textContent=mode==='demo'?'航迹演示':'实时遥测';
  $('#pause-demo').disabled=mode!=='demo';
}

let stateRequest=false;
async function refreshState(){
  if(stateRequest || (paused&&mode==='demo'))return;
  stateRequest=true;
  try{state=await api('/api/state?mode='+mode);renderFleet();map?.updateAircraft(state.aircraft,selectedId);}
  catch{ $('#footer-source').textContent='后端暂时断开 · 显示最后状态'; }
  finally{stateRequest=false;}
}
async function refreshHealth(){
  try{
    health=await api('/api/health');const available=health.model.available;
    $('#model-status').textContent=available?health.model.model+' · 本地连接':health.model.reason;
    $('#model-dot').className='dot '+(available?'green':'dim');$('#composer-dot').className='dot '+(available?'green':'dim');
    $('#composer-model').textContent=health.model.model.split(':')[1] || '';
    $('#connection-model').textContent='Ollama · '+health.model.model;
    $('#connection-model-state').textContent=available?'已连接':'未连接';
    $('#px4-status').textContent=health.px4?'PX4 遥测已连接':'PX4 未连接';$('#px4-dot').className='dot '+(health.px4?'green':'dim');
    $('#connection-px4-state').textContent=health.px4?'已连接':'未连接';
  }catch{$('#model-status').textContent='地面后端未连接';$('#px4-status').textContent='连接状态未知';}
}
async function refreshSwarm(){
  try{
    const sim=await api('/api/simulation');
    $('#sim-state').textContent=({offline:'仿真未启动',starting:'正在启动固定翼…',ready:'固定翼在线 · 可接收任务',stopped:'仿真已停止',error:'仿真启动失败'})[sim.status]||sim.status;
    $('#start-sim').disabled=['starting','ready'].includes(sim.status);$('#stop-sim').disabled=!['starting','ready'].includes(sim.status);
    const data=await api('/api/swarm');swarm=data.available?data:null;
    $('#swarm-panel').hidden=!swarm;
    if(!swarm){map?.updateMission(null);return;}
    const report=swarm.report,allocation=report.allocation;
    const visited=Object.values(report.aircraft||{}).reduce((n,a)=>n+Object.keys(a.visited||{}).length,0);
    $('#swarm-summary').textContent=report.error?'任务中断：'+report.error:!report.tasks.length?'等待 Qwen 对话任务':report.success?`验证通过 · ${visited}/${report.tasks.length} 个巡检点已到达`:`${({queued:'已分配，等待上传',uploading:'上传 PX4 任务中',executing:'正在执行',paused:'已暂停原任务'})[report.status]||'巡检中'} · 已到达 ${visited}/${report.tasks.length} 点 · 算法 ${allocation?.algorithm_id||'CBBA'}`;
    if(report.operation==='takeoff'&&!report.error)$('#swarm-summary').textContent=report.success?`${visited} 架已起飞并进入盘旋等待航段`:`起飞任务${({queued:'排队中',uploading:'上传中',executing:'执行中',paused:'已暂停'})[report.status]||''} · ${visited}/${report.tasks.length} 架进入等待航段`;
    if(report.session_stopped_at)$('#swarm-summary').textContent='历史记录 · '+$('#swarm-summary').textContent;
    const list=$('#swarm-routes');list.replaceChildren();
    Object.entries(allocation?.assignments||{}).forEach(([id,route])=>{
      const row=el('div','swarm-route'),a=report.aircraft[id];
      const labels=route.map(tid=>report.tasks.find(t=>t.id===tid)?.label||tid);
      const action=report.controls?.[id]?.action;
      row.append(el('strong','',id),el('span','',labels.join(' → ')||'未分配任务'),el('small','',action?({hold:'已接收盘旋等待',resume:'已接收继续任务',return_home:'已接收返航'})[action]:route.length?`任务上传${a?.mission_accepted?'已确认':'待确认'} · 到点 ${Object.keys(a?.visited||{}).length}/${route.length}`:'待命'));
      list.append(row);
    });
    map?.updateMission(mode==='live'?report:null);
  }catch{/* Existing map and last report remain usable during a transient read failure. */}
}
function scrollChat(){const box=$('#chat-scroll');box.scrollTop=box.scrollHeight;}
function addMessage(role,text,source){
  const box=el('div',role==='user'?'user-bubble':'assistant-bubble');
  if(role==='assistant')box.append(el('span','bubble-label',source==='controller'?'地面指控 · 飞控反馈':'✧ QWEN'),el('p','',text));else box.textContent=text;
  $('#messages').append(box);scrollChat();
}
function draftCard(draft,saved=false){
  const card=el('div','draft-card');card.append(el('span','eyebrow',saved?'SAVED DRAFT · 未下发':'MISSION DRAFT · 待检查'),el('h4','',draft.title));
  const meta=el('div','draft-meta');[draft.area,({high:'高优先级',normal:'常规优先级',low:'低优先级'})[draft.priority],draft.aircraft_ids.length?draft.aircraft_ids.join(' / '):'待分配飞机'].forEach(t=>meta.append(el('span','',t)));
  card.append(meta,el('p','',draft.notes));
  (draft.targets||[]).forEach(t=>card.append(el('p','target-detail',`${t.label} · ${t.frame==='zone'?t.zone_id+' 区':t.frame==='local'?`东 ${t.x}m / 北 ${t.y}m`:`${t.x}, ${t.y}`} · ${{high:'高',normal:'常规',low:'低'}[t.priority]}优先级 · ${t.sensor}`)));
  if(saved){card.append(el('p','bubble-note',new Date(draft.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})+' · 仅保存草案'));}
  else{
    const btn=el('button','','检查后保存草案');btn.onclick=async()=>{
      btn.disabled=true;
      try{const task=await api('/api/tasks',{draft,mode});tasks.push(task);renderTasks();btn.textContent='已保存草案 · 未下发';toast('任务草案已保存；需要执行时，在对话中明确要求下发。');}
      catch(e){btn.disabled=false;toast(e.message);}
    };card.append(btn);
  }
  return card;
}
function renderTasks(){
  $('#task-count').textContent=tasks.length;
  const list=$('#tasks-list');list.replaceChildren();
  if(!tasks.length){const empty=el('div','empty-state');empty.innerHTML=icon('file');empty.append(el('p','','暂无任务草案'),el('p','','在对话中描述巡检需求即可开始。'));list.append(empty);}
  [...tasks].reverse().forEach(task=>list.append(draftCard(task,true)));
}
function toolTrace(calls){
  const details=el('details','tool-trace');
  const summary=el('summary','',`Qwen 实际调用 ${calls.length} 次工具`);details.append(summary);
  calls.forEach(call=>{const row=el('div','tool-trace-row');row.append(el('strong','',call.label+' · '+(call.status==='success'?'成功':'失败')));
    row.append(el('pre','',JSON.stringify({tool:call.tool,selected_by:call.selected_by,arguments:call.arguments,result:call.result},null,2).slice(0,9000)));details.append(row);});
  return details;
}
function toolResults(calls,plan){
  const fragment=document.createDocumentFragment();
  const labels=new Map((plan?.tasks||[]).map(t=>[t.id,t.label]));
  const routes=assignments=>Object.entries(assignments||{}).map(([id,route])=>`${id}：${route.map(t=>labels.get(t)||t).join(' → ')||'待命'}`).join('\n');
  const table=(title,head,rows)=>{
    const card=el('div','tool-result');card.append(el('strong','',title));
    const grid=el('table');const header=el('tr');head.forEach(t=>header.append(el('th','',t)));grid.append(header);
    rows.forEach(values=>{const row=el('tr');values.forEach(t=>row.append(el('td','',String(t))));grid.append(row);});card.append(grid);return card;
  };
  calls.filter(c=>c.status==='success').forEach(c=>{
    const r=c.result?.result;if(!r)return;
    if(c.tool==='get_fleet_state')fragment.append(table('实时飞行状态',['飞机','阶段','模式'],r.aircraft.map(a=>[a.id,a.stale?'遥测过期':a.landed===true?(a.armed?'已解锁 · 在地面':'未解锁 · 在地面'):a.landed===false?'已离地':'离地状态待确认',a.flight_mode||'未知'])));
    if(c.tool==='compare_allocations'){
      const card=table('算法实算 · 同一遥测快照',['算法','直线航程','结果'],r.results.map(x=>[x.algorithm,x.feasible?`${Math.round(x.metrics.total_distance_m)} m`:'—',x.feasible?routes(x.assignments):x.reason||'存在未分配任务']));
      card.append(el('small','','航程未计入起飞、转弯、风和地形；此比较没有下发。'));fragment.append(card);
    }
    if(c.tool==='plan_inspection'||c.tool==='execute_plan'){
      const a=r.allocation;if(!a)return;
      const card=table(c.tool==='execute_plan'?'已提交 · 等待 PX4 执行反馈':'规划结果 · 尚未执行',['飞机','任务顺序'],Object.entries(a.assignments).map(([id,route])=>[id,route.map(t=>labels.get(t)||t).join(' → ')||'待命']));
      card.append(el('small','',`算法：${a.algorithm_id||a.algorithm} · ${c.tool==='execute_plan'?'排队不表示已离地':'仅方案，不会自动起飞'}`));fragment.append(card);
    }
    if(c.tool==='control_aircraft')fragment.append(table('PX4 指令回执',['飞机','动作','ACK'],r.receipts.map(x=>[x.id,x.action,JSON.stringify(x.ack)])));
  });return fragment;
}
async function sendChat(event){
  event.preventDefault();if(busy)return;
  const text=$('#chat-input').value.trim();if(!text)return;
  busy=true;$('#chat-send').disabled=true;$('#chat-input').value='';
  addMessage('user',text);const thinking=el('div','thinking','Qwen 正在结合地图态势整理…');$('#messages').append(thinking);scrollChat();
  let preview=null;
  const onStream=e=>{
    if(e.type==='model_start'){thinking.textContent='Qwen 正在准备第 '+e.step+' 步推理…';if(preview)preview.querySelector('p').textContent='';}
    if(e.type==='model_progress')thinking.textContent=e.phase==='answering'?'Qwen 正在生成正文…':`Qwen 正在推理 · 内部生成 ${e.thinking_characters} 字符`;
    if(e.type==='tool_start')thinking.textContent='Qwen 正在'+e.label+'…';
    if(e.type==='tool_result')thinking.textContent=e.call.label+'已返回，正在处理后续结果…';
    if(e.type==='text_delta'){if(!preview){preview=el('div','assistant-bubble');preview.append(el('span','bubble-label','QWEN · 正文生成中，以最终回执为准'),el('p','',''));$('#messages').append(preview);}preview.querySelector('p').textContent+=e.text;scrollChat();}
  };
  try{
    const result=await requestChat({message:text,history,mode,conversation_id:conversationId},onStream);
    if(result.conversation_id){conversationId=result.conversation_id;sessionStorage.setItem('fieldlink-conversation-id',conversationId);}
    thinking.remove();preview?.remove();addMessage('assistant',result.reply,result.source);
    if(result.partial_reply){const partial=el('details','tool-trace');partial.append(el('summary','','未完成正文（不代表动作已执行）'),el('pre','',result.partial_reply));$('#messages').append(partial);}
    history.push({role:'user',content:text},{role:'assistant',content:JSON.stringify({reply:result.reply,draft:result.draft})});history=history.slice(-8);
    if(result.tool_calls?.length){$('#messages').append(toolResults(result.tool_calls,result.plan),toolTrace(result.tool_calls));refreshSwarm();scrollChat();}
    if(result.draft){drafts.push(result.draft);const card=draftCard(result.draft);if(result.execution){card.querySelector('button')?.remove();card.append(el('p','bubble-note','已提交分配方案 · 等待 PX4 回执'));const view=el('button','','查看执行进度');view.onclick=()=>tab('tasks');card.append(view);refreshSwarm();}$('#messages').append(card);scrollChat();}
    if(result.execution&&!result.draft){refreshSwarm();const view=el('button','outline-button','查看起飞进度');view.onclick=()=>tab('tasks');$('#messages').append(view);scrollChat();}
  }catch(error){thinking.remove();if(preview)preview.querySelector('.bubble-label').textContent='QWEN · 未完成正文';$('#messages').append(el('div','message-error',error.message+'；请先核对任务状态，避免重复下发。'));$('#chat-input').value=text;scrollChat();}
  finally{busy=false;$('#chat-send').disabled=false;refreshHealth();}
}

function setupInteractions(){
  $('#start-sim').onclick=async()=>{const btn=$('#start-sim');btn.disabled=true;try{await api('/api/simulation/start',{});mode='live';refreshState();refreshSwarm();toast('固定翼正在上线，尚未加载任务。上线后请在对话中下发任务。');}catch(e){toast(e.message);btn.disabled=false;}};
  $('#stop-sim').onclick=async()=>{const btn=$('#stop-sim');btn.disabled=true;try{await api('/api/simulation/stop',{});refreshSwarm();refreshHealth();}catch(e){toast(e.message);btn.disabled=false;}};
  $$('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
  $('#nav-tasks').onclick=()=>tab('tasks');$('#nav-help').onclick=()=>tab('connection');
  $('#nav-map').onclick=()=>map.home();$('#nav-fleet').onclick=()=>{$('#fleet-list').hidden=false;$('#fleet-panel').animate([{borderColor:'#c5e2b4'},{borderColor:'#d8e7cc26'}],{duration:1000});};
  const openSettings=()=>$('#settings-dialog').showModal();$('#settings-button').onclick=openSettings;$('#nav-layers').onclick=openSettings;$('#source-button').onclick=openSettings;
  $('#search-button').onclick=()=>{$('#search-dialog').showModal();$('#location-input').focus();};
  document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)&&!$('dialog[open]')){event.preventDefault();$('#search-dialog').showModal();$('#location-input').focus();}});
  $$('input[name="map-source"]').forEach(input=>input.onchange=()=>$('#google-config').hidden=input.value!=='google');
  $('#apply-settings').onclick=async()=>{
    const btn=$('#apply-settings');btn.disabled=true;$('#settings-error').textContent='';
    const source=$('input[name="map-source"]:checked').value;
    try{
      await map.setSource(source,$('#google-key').value);
      map.setLayers($('#toggle-zones').checked,$('#toggle-trails').checked);
      $('#map-source-label').textContent=source==='google'?'Google 实景三维':'卫星影像 + 真实地形';
      $('#connection-map').textContent=source==='google'?'Google Photorealistic 3D Tiles':'真实高程 + 卫星影像';
      $('#settings-dialog').close();toast('地图设置已更新');
    }catch(e){$('#settings-error').textContent=e.message;}
    finally{btn.disabled=false;}
  };
  ['zones','trails'].forEach(name=>$('#toggle-'+name).onchange=()=>map.setLayers($('#toggle-zones').checked,$('#toggle-trails').checked));
  $('#map-notice button').onclick=()=>$('#map-notice').hidden=true;
  $('#north-button').onclick=()=>map.north();$('#zoom-in').onclick=()=>map.zoom(1);$('#zoom-out').onclick=()=>map.zoom(-1);$('#home-view').onclick=()=>{map.home();$('#follow-button').classList.remove('active');};
  $('#tilt-view').onclick=()=>$('#tilt-view').textContent=map.tilt()?'3D':'俯视';
  $('#full-view').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('.map-workspace').requestFullscreen();}catch{toast('当前浏览器不支持全屏');}};
  $('#follow-button').onclick=()=>$('#follow-button').classList.toggle('active',map.follow(selectedId));
  $('#fleet-collapse').onclick=()=>{$('#fleet-list').hidden=!$('#fleet-list').hidden;$('#fleet-collapse').innerHTML=icon($('#fleet-list').hidden?'plus':'minus');};
  $('#pause-demo').onclick=()=>{paused=!paused;$('#pause-demo').innerHTML=icon(paused?'play':'pause');$('#pause-demo').setAttribute('aria-label',paused?'继续演示航迹':'暂停演示航迹');$('#playback-label').textContent=paused?'画面已暂停':'航迹演示';if(!paused)refreshState();toast(paused?'已冻结演示画面；真实遥测不受影响':'已恢复当前演示位置');};
  $('#mode-switch').onclick=()=>{mode=mode==='demo'?'live':'demo';paused=false;$('#pause-demo').innerHTML=icon('pause');map.followId=null;$('#follow-button').classList.remove('active');refreshState();refreshSwarm();toast(mode==='live'?'已切换 PX4 遥测。未接入时不显示演示飞机。':'已切换演示航迹。数据不来自 PX4。');};
  $('#location-form').onsubmit=event=>{
    event.preventDefault();const parts=$('#location-input').value.trim().split(/[,，\s]+/).map(Number);
    if(parts.length!==2||!parts.every(Number.isFinite)||Math.abs(parts[0])>180||Math.abs(parts[1])>85){$('#location-error').textContent='请输入有效的经度, 纬度，例如 103.565, 31.045。';return;}
    $('#location-error').textContent='';map.flyTo(...parts);$('#search-dialog').close();toast('地图视角已定位；巡检任务区域保持原配置。');
  };
  $$('[data-lon]').forEach(b=>b.onclick=()=>{map.flyTo(Number(b.dataset.lon),Number(b.dataset.lat),b.dataset.name==='旧金山'?5000:15000);$('#search-dialog').close();toast('已定位到'+b.dataset.name+'；巡检区域仍为龙池山区。');});
  $('#chat-form').onsubmit=sendChat;
  $('#chat-input').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('#chat-form').requestSubmit();}};
  $$('[data-prompt]').forEach(b=>b.onclick=()=>{tab('chat');$('#chat-input').value=b.dataset.prompt;$('#chat-input').focus();});
  $('#clear-chat').onclick=()=>{if(busy){toast('请等待当前模型回复完成');return;}history=[];drafts=[];conversationId=crypto.randomUUID();sessionStorage.setItem('fieldlink-conversation-id',conversationId);$('#messages').replaceChildren();$('#chat-input').value='';tab('chat');toast('已开始新对话，已保存草案保留。');};
  $('#export-scene').onclick=async()=>{
    const manifest=await api('/api/scene/manifest');const url=URL.createObjectURL(new Blob([JSON.stringify(manifest,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='dujiangyan-longchi-scene.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已导出场景对齐说明，不包含 Google 瓦片或传感器图像。');
  };
  $$('dialog').forEach(d=>d.addEventListener('click',event=>{if(event.target===d){const r=d.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)d.close();}}));
}

function clock(){const d=new Date();$('#clock').textContent=d.toLocaleTimeString('en-GB',{timeZone:'Asia/Shanghai',hour12:false})+' CST';}
async function main(){
  clock();setInterval(clock,1000);
  scene=await api('/api/scene');
  await document.fonts.load('12px FieldLinkSC');
  try{map=new GroundMap(scene,{onSelect:id=>selectAircraft(id),onStatus:updateMapStatus,onNotice:mapNotice,onPosition:(lon,lat)=>$('#cursor-position').textContent=`${Math.abs(lon).toFixed(5)}° ${lon>=0?'E':'W'}   ${Math.abs(lat).toFixed(5)}° ${lat>=0?'N':'S'}`});}
  catch(error){$('#map-loading').hidden=true;mapNotice('三维地图初始化失败，请启用浏览器 WebGL：'+error.message);}
  setupInteractions();
  if(new URLSearchParams(location.search).get('swarm')==='1')tab('tasks');
  await Promise.allSettled([refreshState(),refreshHealth(),refreshSwarm(),api('/api/tasks').then(value=>{tasks=value;renderTasks();})]);
  setInterval(refreshState,1000);setInterval(refreshHealth,12000);setInterval(refreshSwarm,2000);
  // Read-only diagnostics for reproducible browser validation.
  window.fieldlink={get map(){return map;},get state(){return state;},get mode(){return mode;},get health(){return health;},get swarm(){return swarm;}};
}
main().catch(error=>{$('#map-loading').hidden=true;mapNotice('地面站启动失败：'+error.message);});
