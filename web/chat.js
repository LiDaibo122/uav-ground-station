import {requestChat} from './chat-stream.js';
const $=selector=>document.querySelector(selector);
const create=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node;};
const xd=document.documentElement.dataset.backend==='xd';
const monitor=document.documentElement.dataset.monitor==='true';
const fleetPreview=xd && (monitor || new URLSearchParams(location.search).get('mode')==='plan');
const fleetConsole=xd && !monitor && new URLSearchParams(location.search).get('mode')==='fleet';
const key=monitor?'fieldlink-xd-monitor-id':fleetPreview||fleetConsole?'fieldlink-xd-allocation-id':xd?'fieldlink-xd-conversation-id':'fieldlink-conversation-id';
if(xd){
 document.title='XD 固定翼巡检';
 $('.intro h1').textContent='XD 固定翼巡检';
 $('.intro p').textContent='查询飞机状态，保存 world 坐标航点，再下发巡检任务。';
 $('nav a').hidden=true;
 $('aside h2').textContent='飞机实时状态';
 $('#chat-input').placeholder='输入状态查询、巡检点或飞行请求…';
 if(fleetPreview){
  $('.intro h1').textContent='集群巡检分配';
  $('.intro p').textContent='输入任务位置，查看各机的分工、优先级依据和未分配原因。本页仅规划。';
 }
}
let conversationId=sessionStorage.getItem(key)||crypto.randomUUID();sessionStorage.setItem(key,conversationId);
let busy=false,polling=false,healthPolling=false;
const dispatchPanel = $('#dispatch-panel');
const mainChat = {
 form: $('#chat-form'), input: $('#chat-input'), messages: $('#messages'), send: $('#send'),
};
const dispatchChat = {
 form: $('#dispatch-form'), input: $('#dispatch-input'), messages: $('#dispatch-messages'), send: $('#dispatch-send'),
};
let activeDraft = null;
let dispatchTrigger = null;

function openDispatch(button, draft) {
 if (activeDraft?.id !== draft.id) {
  if (busy) return;
  activeDraft = draft;
  $('#dispatch-summary').textContent = draft.summary;
  dispatchChat.messages.replaceChildren();
  dispatchChat.input.value = '';
 }
 dispatchTrigger?.setAttribute('aria-expanded', 'false');
 dispatchTrigger = button;
 button.setAttribute('aria-expanded', 'true');
 dispatchPanel.hidden = false;
 $('main').classList.add('has-dispatch');
 dispatchChat.input.focus();
}

function closeDispatch() {
 if (dispatchPanel.hidden) return;
 dispatchPanel.hidden = true;
 $('main').classList.remove('has-dispatch');
 dispatchTrigger?.setAttribute('aria-expanded', 'false');
 if (dispatchTrigger?.isConnected) dispatchTrigger.focus();
}

function dispatchButton(id, summary) {
 const button = create('button', 'open-dispatch', '在调度页打开这份草案');
 button.type = 'button';
 button.setAttribute('aria-controls', dispatchPanel.id);
 button.setAttribute('aria-expanded', 'false');
 button.addEventListener('click', () => openDispatch(button, {id, summary}));
 return button;
}

$('#close-dispatch').addEventListener('click', closeDispatch);
dispatchPanel.addEventListener('keydown', event => {
 if (event.key === 'Escape' && !event.isComposing) closeDispatch();
});

async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error||'服务暂时不可用');return data;}
function scrollChat(messages) {
 messages.scrollTop = messages.scrollHeight;
}
function message(messages, role, text, source) {
 const box = create('article', 'message '+role);
 box.append(create('span','message-label',role==='user'?'你':source==='controller'?'地面指控反馈':'QWEN'), create('p','message-body',text));
 messages.append(box);
 scrollChat(messages);
 return box;
}
const stage=a=>a.stale?'遥测过期':a.landed===true?(a.armed?'在地面 · 已解锁':'在地面 · 未解锁'):a.landed===false?'已离地':'起飞/降落状态待确认';
const numeric=(value,digits=1)=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(digits):'—';
function table(title,head,rows){const card=create('div','result');card.append(create('strong','',title));const grid=create('table');const header=create('tr');head.forEach(text=>header.append(create('th','',text)));grid.append(header);rows.forEach(values=>{const row=create('tr');values.forEach(value=>row.append(create('td','',value)));grid.append(row);});card.append(grid);return card;}
function groupReceipt(r){
 const labels={queued:'已排队',running:'执行中',completed:'已完成',partial:'部分完成',idle:'待命',failed:'失败',unknown:'待核实',rejected:'已拒绝',preview:'尚未确认'};
 const rows=Object.entries(r.vehicles).map(([id,v])=>[id,v.task_ids.map(t=>r.task_labels[t]).join(' → ')||'—',labels[v.phase]||v.phase,v.detail||'—']);
 const card=table('集群草案 '+r.plan_id.slice(0,8)+' · '+(labels[r.status]||r.status),['飞机','任务顺序','阶段','回执'],rows);
 if(r.repeated)card.append(create('p','','重复确认，已返回原执行记录。'));
 for(const [id,why] of Object.entries(r.unassigned))card.append(create('p','',r.task_labels[id]+' · 未分配 · '+why));
 return card;
}
function results(box,result){
 const names=new Map((result.plan?.tasks||[]).map(t=>[t.id,t.label]));
 const route=assignments=>Object.entries(assignments||{}).map(([id,list])=>`${id}：${list.map(t=>names.get(t)||t).join(' → ')||'待命'}`).join('\n');
 for(const call of result.tool_calls||[]){
  if(call.status!=='success')continue;const value=call.result?.result;if(!value)continue;
  if(call.tool==='xd_allocate'){
   const owners=new Map();
   for(const [vehicle,ids] of Object.entries(value.assignments))
    ids.forEach((id,i)=>{
     const seconds=value.arrival_estimates_s?.[vehicle]?.[i];
     owners.set(id,vehicle+' · 第 '+(i+1)+' 项'+(Number.isFinite(seconds)?' · 约 '+Math.round(seconds)+' 秒':''));
    });
   const levels={normal:'常规',high:'高',low:'低'};
   const card=table('CBBA 集群分配 · 仅规划',['任务','飞机及顺序','优先级','依据'],value.tasks.map(t=>[
    t.label,owners.get(t.id)||value.unassigned[t.id]||'待分配',levels[t.priority],
    [t.basis,...t.evidence.map(e=>e.source+' · '+e.text)].join(' / ')]));
   for(const a of value.fleet.filter(a=>!a.available))card.append(create('p','',a.id+' · '+a.reasons.join('，')));
   card.append(create('small','','到达时间按标称速度和直线距离估算，未计起飞、转弯和巡检耗时。'));
   const summary='草案 '+value.id+' · world 高 '+value.height_m+' 米 · 完成后返航。'+Object.entries(value.assignments).map(([u,ids])=>u+'：'+(ids.map(id=>value.tasks.find(t=>t.id===id).label).join(' → ')||'待命')).join('；');
   card.append(create('p','',summary));
   sessionStorage.setItem('xd-summary-'+conversationId,summary);
   sessionStorage.setItem('xd-plan-'+conversationId,value.id);
   if(fleetPreview && !monitor){
    $('#saved-draft')?.remove();
    card.append(dispatchButton(value.id, summary));
   }
   box.append(card);
  }
  if(call.tool==='xd_confirm_allocation'||call.tool==='xd_allocation_status'){
   box.append(groupReceipt(value));
   if(value.status!=='preview')sessionStorage.setItem('xd-execution-'+conversationId,value.plan_id);
  }
  if(call.tool==='xd_plan'){
   const card=table('巡检草案 · 尚未下发',['点','东','北','高'],value.points_m.map((p,i)=>[i+1,...p]));
   card.append(create('small','',value.return_home?'执行后自动返航降落 · world 坐标，单位米':'world 坐标，单位米'));
   box.append(card);
  }
  if(call.tool==='xd_execute'){
   const card=create('div','result');
   card.append(create('strong','','巡检航线已排队 · 等待 XD 回执'));
   if(value.return_home)card.append(create('p','','巡检完成后自动返航降落'));
   box.append(card);
  }
  if(call.tool==='xd_plan_coverage'||(call.tool==='xd_execute'&&value.coverage)){
   const plan=call.tool==='xd_plan_coverage'?value:value.coverage;
   const a=plan.coverage;
   const card=table(call.tool==='xd_plan_coverage'?'S 形巡检草案 · 尚未下发':'S 形巡检范围（world，米）',
    ['东坐标','北坐标','高度','扫线间距'],[[`${a.east_min_m} 至 ${a.east_max_m}`,`${a.north_min_m} 至 ${a.north_max_m}`,a.altitude_m,a.lane_spacing_m]]);
   card.append(create('p','',`扫线及掉头约 ${plan.route_length_m} 米 · ${plan.point_count} 个航点 · 入场段另计`));
   if(call.tool==='xd_plan_coverage'&&plan.return_home)card.append(create('small','','执行后自动返航降落'));
   box.append(card);
  }
  if(call.tool==='xd_state'){
   const aircraft=value.vehicles||[value];
   box.append(table('起飞条件',['飞机','连接','定位','地面状态','观测时间'],aircraft.map(a=>[a.vehicle,a.connected?'已连接':'未连接',a.localization_valid?'有效':'待确认',a.flight_phase,a.observed_at?new Date(a.observed_at*1000).toLocaleTimeString():'—'])));
  }
  if(call.tool==='get_fleet_state')box.append(table('工具返回的飞行状态',['飞机','阶段','模式'],value.aircraft.map(a=>[a.id,stage(a),a.flight_mode||'未知'])));
  if(call.tool==='plan_inspection'||call.tool==='execute_plan'){
   const allocation=value.allocation;if(!allocation)continue;
   const card=table(call.tool==='execute_plan'?'已提交 · 等待 PX4 后续反馈':'本轮规划结果 · 尚未执行',['飞机','任务顺序'],Object.entries(allocation.assignments||{}).map(([id,list])=>[id,list.map(t=>names.get(t)||t).join(' → ')||'待命']));
   for(const target of value.draft?.targets||[]){const location=target.frame==='zone'?`${target.zone_id} 区`:target.frame==='local'?`东 ${target.x}m / 北 ${target.y}m`:`经度 ${target.x} / 纬度 ${target.y}`;card.append(create('p','',`${target.label} · ${location} · ${target.priority} · ${target.sensor}`));}
   card.append(create('small','',`算法：${allocation.algorithm_id||allocation.algorithm}。${call.tool==='execute_plan'?'排队/接收回执不等于离地或完成。':'仅生成方案，不会自动起飞。'}`));box.append(card);
  }
  if(call.tool==='compare_allocations'){
   const card=table('算法实算比较',['算法','直线航程','分配结果'],value.results.map(x=>[x.algorithm,x.feasible?`${numeric(x.metrics.total_distance_m,0)}m`:'—',x.feasible?route(x.assignments):x.reason||'存在未分配任务']));card.append(create('small','','直线估计未计入起飞、固定翼转弯、风和地形；比较没有下发。'));box.append(card);
  }
  if(call.tool==='control_aircraft')box.append(table('PX4 指令回执',['飞机','动作','ACK'],(value.receipts||[]).map(r=>[r.id,r.action,JSON.stringify(r.ack)])));
  if(call.tool==='takeoff_aircraft'){
   const card=create('div','result');card.append(create('strong','','起飞任务已提交'),create('p','',`飞机：${(value.aircraft_ids||[]).join('、')}\n状态：${value.status||'等待回执'}`),create('small','','提交不表示已经离地，请结合遥测和 QGC 查看。'));box.append(card);
  }
 }
 if(result.tool_calls?.length){const details=create('details');details.append(create('summary','',`实际工具调用 ${result.tool_calls.length} 次`));for(const call of result.tool_calls){details.append(create('strong','',`${call.label||call.tool} · ${call.status}`),create('pre','',JSON.stringify({tool:call.tool,arguments:call.arguments,result:call.result},null,2).slice(0,16000)));}box.append(details);}
 if(result.partial_reply){const details=create('details','partial-reply');details.append(create('summary','','未完成正文（不代表动作已执行）'),create('p','message-body',result.partial_reply));box.append(details);}
 const usage=result.model_usage||[];const inputs=usage.map(u=>u.prompt_eval_count).filter(Number.isFinite),generated=usage.reduce((n,u)=>n+(u.eval_count||0),0),unknown=usage.some(u=>!Number.isFinite(u.eval_count));
 box.append(create('small','usage',`用时 ${numeric(result.duration_s)} 秒${inputs.length?` · 最大输入 ${Math.max(...inputs)} / ${usage.at(-1).num_ctx} token · ${unknown?'已完成调用生成':'生成'} ${generated} token${unknown?'（中断部分未返回计数）':''}`:''}`));
}
async function state(){
 if(polling||document.hidden)return;polling=true;
 try{
  if(xd){
   const state=await api('/api/xd/status'), aircraft=state.vehicles||[state];
   if(aircraft.length>1 && !fleetPreview){
    const link=$('nav a');link.hidden=false;link.href='/chat.html?mode=plan';link.textContent='集群只规划 ↗';
   }
   const count=aircraft.filter(a=>a.connected).length;
   $('#flight-state').textContent=`XD · ${count}/${aircraft.length} 架在线`;
   $('#flight-state').classList.toggle('online',count>0);
   const phases={on_ground:'地面',in_air:'空中',taking_off:'起飞中',landing:'降落中'};
   const actions={takeoff:'起飞',task_path:'巡检航线',cancel:'释放航路',land_home:'返航降落'};
   const stages={sent:'已发送',accepted:'XD 已接受',running:'执行中',completed:'已完成',failed:'失败',unknown:'结果待核实',rejected:'已拒绝'};
   $('#fleet').replaceChildren();
   $('#mission').replaceChildren();
   const groupId=sessionStorage.getItem('xd-execution-'+conversationId)||aircraft.find(a=>a.job?.action==='inspection')?.job?.plan_id;
   if(groupId){
    const group=await api('/api/xd/executions/'+encodeURIComponent(groupId));
    if(group.status!=='preview')$('#mission').append(groupReceipt(group));
   }
   for(const a of aircraft){
    const card=table(a.vehicle,['阶段','锁定状态','模式','定位','电量'],[[phases[a.flight_phase]||'未知',a.armed==null?'未知':a.armed?'已解锁':'已上锁',a.mode||'—',a.localization_valid?'有效':'待确认',a.battery_pct==null?'—':a.battery_pct+'%']]);
    const p=a.world_position_m;
    if(p)card.append(create('p','',`world（米） 东 ${numeric(p.x)} / 北 ${numeric(p.y)} / 高 ${numeric(p.z)}`));
    $('#fleet').append(card);
    const job=a.job;
    if(!job){$('#mission').append(create('p','',a.vehicle+' · 等待任务'));continue;}
    $('#mission').append(create('strong','',aircraft.length>1?a.vehicle+' · '+job.detail:job.detail));
    for(const e of job.events)$('#mission').append(create('p','',`${actions[e.action]} · ${stages[e.phase]||e.phase}${e.goal_id==null?'':' · goal '+e.goal_id} · ${e.message}`));
   }
   $('#last-update').textContent=aircraft.map(a=>a.vehicle+' '+(a.observed_at?new Date(a.observed_at*1000).toLocaleTimeString():'等待遥测')).join(' / ');
   return;
  }
  const reads=await Promise.allSettled([api('/api/state?mode=live'),api('/api/simulation')]);
  const fleet=reads[0].status==='fulfilled'?reads[0].value:null,sim=reads[1].status==='fulfilled'?reads[1].value:null;
  if(fleet){const fragment=document.createDocumentFragment();for(const a of fleet.aircraft){const card=create('div','aircraft'+(a.stale?' stale':''));card.append(create('strong','',a.id),create('p','',`${stage(a)} · ${a.flight_mode||'未知模式'}\n电量 ${a.battery_pct==null?'未知':numeric(a.battery_pct,0)+'%'} · 相对高度 ${numeric(a.relative_alt_m)}m\n地速 ${numeric(a.speed_mps)}m/s · 遥测年龄 ${numeric(a.age_s)}s`));fragment.append(card);}if(!fleet.aircraft.length)fragment.append(create('p','muted','等待 PX4 遥测。'));$('#fleet').replaceChildren(fragment);$('#last-update').textContent='最近获取：'+new Date().toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（UTC+8）';}
  else $('#last-update').textContent='状态接口暂时不可达；以上为最后获取值。';
  if(sim){$('#flight-state').textContent=({offline:'仿真未启动',starting:'固定翼正在上线',ready:'固定翼在线',stopped:'仿真已停止',error:'仿真异常'})[sim.status]||sim.status;$('#flight-state').classList.toggle('online',sim.ready===true);const r=sim.report||{},total=(r.tasks||[]).length,visited=Object.values(r.aircraft||{}).reduce((sum,a)=>sum+Object.keys(a.visited||{}).length,0);$('#mission').textContent=r.error?`任务反馈：${r.error}`:total?`巡检到点 ${visited}/${total} · ${r.status}\n执行编号：${r.execution_id||'—'}`:'等待对话任务。';}
 }catch{ $('#last-update').textContent='连接中断，当前显示上次反馈'; $('#flight-state').textContent='遥测待更新'; }finally{polling=false;}
}
async function health(){if(healthPolling||document.hidden)return;healthPolling=true;try{const value=await api('/api/health');$('#model-state').textContent=value.model.available?`${value.model.model} · 本地连接`:value.model.reason;$('#model-state').classList.toggle('online',value.model.available);}catch{$('#model-state').textContent='地面后端未连接';}finally{healthPolling=false;}}
async function sendChat(chat) {
 const {input, messages} = chat;
 const text = input.value.trim();
 const dispatch = chat === dispatchChat;
 if (busy || !text || (dispatch && (dispatchPanel.hidden || !activeDraft))) return;
 const latestDraftId = sessionStorage.getItem('xd-plan-'+conversationId);
 if (dispatch && activeDraft.id !== latestDraftId) {
  messages.append(create('p', 'pending', '已有更新的集群草案。请从最新的草案卡片打开调度对话。'));
  scrollChat(messages);
  return;
 }
 const body = {
  message: text,
  mode: fleetPreview && !dispatch ? 'xd_plan_only' : 'live',
  conversation_id: conversationId,
 };
 if (xd) body.displayed_plan_id = dispatch ? activeDraft.id : latestDraftId;

 busy = true;
 mainChat.send.disabled = dispatchChat.send.disabled = $('#new-conversation').disabled = true;
 input.value = '';
 message(messages, 'user', text);
 const pending = create('div', 'pending', 'Qwen 正在理解需求…');
 const liveTools = create('div', 'live-tools');
 messages.append(pending, liveTools);
 scrollChat(messages);
 const began = performance.now();
 let lastActivity = 'Qwen 正在理解需求';
 let preview = null;
 const progress = setInterval(() => {
  const seconds = Math.floor((performance.now() - began) / 1000);
  pending.textContent = lastActivity + ' · 已等待 ' + seconds + ' 秒';
 }, 500);
 const onStream = event => {
  if (event.type === 'start' && event.conversation_id) {
   conversationId = event.conversation_id;
   sessionStorage.setItem(key, conversationId);
  }
  if (event.type === 'model_start') {
   lastActivity = 'Qwen 正在准备第 ' + event.step + ' 步推理';
   if (preview) preview.querySelector('.message-body').textContent = '';
  }
  if (event.type === 'model_progress') {
   lastActivity = event.phase === 'answering' ? 'Qwen 正在生成正文' : 'Qwen 正在推理（已生成 ' + event.thinking_characters + ' 个内部字符）';
  }
  if (event.type === 'text_delta') {
   if (!preview) {
    preview = message(messages, 'assistant', '', 'qwen');
    preview.classList.add('provisional');
    preview.querySelector('.message-label').textContent = 'QWEN · 正文生成中，以最终回执为准';
   }
   preview.querySelector('.message-body').textContent += event.text;
   scrollChat(messages);
  }
  if (event.type === 'tool_start') {
   lastActivity = '正在' + event.label;
   liveTools.append(create('p','',event.label + ' · 开始'));
  }
  if (event.type === 'tool_result') {
   const call = event.call;
   lastActivity = call.label + '已返回';
   const outcome = call.status === 'success' ? '工具返回成功' : call.result?.error || '失败';
   liveTools.append(create('p', '', call.label + ' · ' + outcome));
   scrollChat(messages);
  }
 };
 try {
  const result = await requestChat(body, onStream);
  if (result.conversation_id) {
   conversationId = result.conversation_id;
   sessionStorage.setItem(key, conversationId);
  }
  pending.remove();
  preview?.remove();
  liveTools.remove();
  const box = message(messages, 'assistant', result.reply, result.source);
  results(box, result);
  scrollChat(messages);
  state();
 } catch (error) {
  pending.remove();
  if (preview) preview.querySelector('.message-label').textContent = 'QWEN · 未完成正文，不代表动作已执行';
  const box = create('div', 'message error');
  box.append(create('p', '', error.message), create('small', 'usage', '连接中断不会撤销已执行动作。请先查看 QGC、任务记录和实际状态，再决定是否重试。'));
  messages.append(box);
  input.value = text;
  scrollChat(messages);
 } finally {
  clearInterval(progress);
  busy = false;
  mainChat.send.disabled = dispatchChat.send.disabled = $('#new-conversation').disabled = false;
  health();
 }
}

for (const chat of [mainChat, dispatchChat]) {
 chat.form.addEventListener('submit', event => {
  event.preventDefault();
  sendChat(chat);
 });
 chat.input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
   event.preventDefault();
   chat.form.requestSubmit();
  }
 });
}

$('#new-conversation').addEventListener('click', () => {
 if (busy) return;
 closeDispatch();
 activeDraft = dispatchTrigger = null;
 dispatchChat.messages.replaceChildren();
 dispatchChat.input.value = '';
 conversationId = crypto.randomUUID();
 sessionStorage.setItem(key, conversationId);
 mainChat.messages.replaceChildren(create('p','muted','已开始新对话。飞机当前任务不受清空对话影响。'));
});
if (fleetPreview && !monitor) {
 const draftId = sessionStorage.getItem('xd-plan-'+conversationId);
 const summary = sessionStorage.getItem('xd-summary-'+conversationId);
 if (draftId && summary) {
  const savedDraft = create('div');
  savedDraft.id = 'saved-draft';
  savedDraft.append(create('p', '', summary), dispatchButton(draftId, summary));
  $('.intro').append(savedDraft);
 }
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden){state();health();}});
if(fleetConsole){
 $('.intro h1').textContent='集群巡检调度';
 $('.intro p').textContent=sessionStorage.getItem('xd-summary-'+conversationId)||'描述巡检任务生成草案，核对后再确认执行。';
}
state();health();setInterval(state,3000);setInterval(health,15000);
