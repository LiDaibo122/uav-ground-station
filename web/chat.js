import {requestChat} from './chat-stream.js';
const $=selector=>document.querySelector(selector);
const create=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node;};
const key='fieldlink-conversation-id';
let conversationId=sessionStorage.getItem(key)||crypto.randomUUID();sessionStorage.setItem(key,conversationId);
let busy=false,polling=false,healthPolling=false;
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error||'服务暂时不可用');return data;}
const scroll=()=>{$('#messages').scrollTop=$('#messages').scrollHeight;};
function message(role,text,source){const box=create('article','message '+role);box.append(create('span','message-label',role==='user'?'你':source==='controller'?'地面指控反馈':'QWEN'),create('p','message-body',text));$('#messages').append(box);scroll();return box;}
const stage=a=>a.stale?'遥测过期':a.landed===true?(a.armed?'在地面 · 已解锁':'在地面 · 未解锁'):a.landed===false?'已离地':'起飞/降落状态待确认';
const numeric=(value,digits=1)=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(digits):'—';
function table(title,head,rows){const card=create('div','result');card.append(create('strong','',title));const grid=create('table');const header=create('tr');head.forEach(text=>header.append(create('th','',text)));grid.append(header);rows.forEach(values=>{const row=create('tr');values.forEach(value=>row.append(create('td','',value)));grid.append(row);});card.append(grid);return card;}
function results(box,result){
 const names=new Map((result.plan?.tasks||[]).map(t=>[t.id,t.label]));
 const route=assignments=>Object.entries(assignments||{}).map(([id,list])=>`${id}：${list.map(t=>names.get(t)||t).join(' → ')||'待命'}`).join('\n');
 for(const call of result.tool_calls||[]){
  if(call.status!=='success')continue;const value=call.result?.result;if(!value)continue;
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
  const reads=await Promise.allSettled([api('/api/state?mode=live'),api('/api/simulation')]);
  const fleet=reads[0].status==='fulfilled'?reads[0].value:null,sim=reads[1].status==='fulfilled'?reads[1].value:null;
  if(fleet){const fragment=document.createDocumentFragment();for(const a of fleet.aircraft){const card=create('div','aircraft'+(a.stale?' stale':''));card.append(create('strong','',a.id),create('p','',`${stage(a)} · ${a.flight_mode||'未知模式'}\n电量 ${a.battery_pct==null?'未知':numeric(a.battery_pct,0)+'%'} · 相对高度 ${numeric(a.relative_alt_m)}m\n地速 ${numeric(a.speed_mps)}m/s · 遥测年龄 ${numeric(a.age_s)}s`));fragment.append(card);}if(!fleet.aircraft.length)fragment.append(create('p','muted','等待 PX4 遥测。'));$('#fleet').replaceChildren(fragment);$('#last-update').textContent='最近获取：'+new Date().toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（UTC+8）';}
  else $('#last-update').textContent='状态接口暂时不可达；以上为最后获取值。';
  if(sim){$('#flight-state').textContent=({offline:'仿真未启动',starting:'固定翼正在上线',ready:'固定翼在线',stopped:'仿真已停止',error:'仿真异常'})[sim.status]||sim.status;$('#flight-state').classList.toggle('online',sim.ready===true);const r=sim.report||{},total=(r.tasks||[]).length,visited=Object.values(r.aircraft||{}).reduce((sum,a)=>sum+Object.keys(a.visited||{}).length,0);$('#mission').textContent=r.error?`任务反馈：${r.error}`:total?`巡检到点 ${visited}/${total} · ${r.status}\n执行编号：${r.execution_id||'—'}`:'等待对话任务。';}
 }finally{polling=false;}
}
async function health(){if(healthPolling||document.hidden)return;healthPolling=true;try{const value=await api('/api/health');$('#model-state').textContent=value.model.available?`${value.model.model} · 本地连接`:value.model.reason;$('#model-state').classList.toggle('online',value.model.available);}catch{$('#model-state').textContent='地面后端未连接';}finally{healthPolling=false;}}
$('#chat-form').addEventListener('submit',async event=>{
 event.preventDefault();if(busy)return;const text=$('#chat-input').value.trim();if(!text)return;
 busy=true;$('#send').disabled=true;$('#new-conversation').disabled=true;$('#chat-input').value='';message('user',text);
 const pending=create('div','pending','Qwen 正在理解需求…');$('#messages').append(pending);scroll();
 const began=performance.now();let lastActivity='Qwen 正在理解需求',preview=null;
 const liveTools=create('div','live-tools');$('#messages').append(liveTools);
 const progress=setInterval(()=>{pending.textContent=`${lastActivity} · 已等待 ${Math.floor((performance.now()-began)/1000)} 秒`;},500);
 const onStream=e=>{
  if(e.type==='start'&&e.conversation_id){conversationId=e.conversation_id;sessionStorage.setItem(key,conversationId);}
  if(e.type==='model_start'){lastActivity='Qwen 正在准备第 '+e.step+' 步推理';if(preview)preview.querySelector('.message-body').textContent='';}
  if(e.type==='model_progress')lastActivity=e.phase==='answering'?'Qwen 正在生成正文':`Qwen 正在推理（已生成 ${e.thinking_characters} 个内部字符）`;
  if(e.type==='text_delta'){if(!preview){preview=message('assistant','','qwen');preview.classList.add('provisional');preview.querySelector('.message-label').textContent='QWEN · 正文生成中，以最终回执为准';}preview.querySelector('.message-body').textContent+=e.text;scroll();}
  if(e.type==='tool_start'){lastActivity='正在'+e.label;liveTools.append(create('p','',e.label+' · 开始'));}
  if(e.type==='tool_result'){const c=e.call;lastActivity=c.label+'已返回';liveTools.append(create('p','',c.label+' · '+(c.status==='success'?'工具返回成功':c.result?.error||'失败')));scroll();}
 };
 try{const result=await requestChat({message:text,mode:'live',conversation_id:conversationId},onStream);if(result.conversation_id){conversationId=result.conversation_id;sessionStorage.setItem(key,conversationId);}pending.remove();preview?.remove();liveTools.remove();const box=message('assistant',result.reply,result.source);results(box,result);scroll();state();}
 catch(error){pending.remove();if(preview)preview.querySelector('.message-label').textContent='QWEN · 未完成正文，不代表动作已执行';const box=create('div','message error');box.append(create('p','',error.message),create('small','usage','连接中断不会撤销已执行动作。请先查看 QGC、任务记录和实际状态，再决定是否重试。'));$('#messages').append(box);$('#chat-input').value=text;scroll();}
 finally{clearInterval(progress);busy=false;$('#send').disabled=false;$('#new-conversation').disabled=false;health();}
});
$('#chat-input').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('#chat-form').requestSubmit();}});
$('#new-conversation').addEventListener('click',()=>{if(busy)return;conversationId=crypto.randomUUID();sessionStorage.setItem(key,conversationId);$('#messages').replaceChildren(create('p','muted','已开始新对话。飞机当前任务不受清空对话影响。'));});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){state();health();}});
state();health();setInterval(state,3000);setInterval(health,15000);
