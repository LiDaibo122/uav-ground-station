const $=id=>document.getElementById(id);
const titles={ready:'待开始',running:'巡检中',waiting:'等待继续',cancelling:'等待模拟中断回执',finished:'队列结束',offline:'退出'};
const actions={points_then_areas:'先巡点，再巡区',plan:'分配待办',start:'开始',skip:'跳过当前项',wait_after:'本项完成后等待',resume:'继续',offline:'退出并重新分配',status:'查询'};

export function setupRehearsal(map, onContextChange) {
 const panel=$('exercise'), chat=$('exercise-chat'), info=$('exercise-info');
 const attach=$('attach-sketch');
 const shapeStorage='inspection-pending-sketches';
 function usesSketch() { return attach.checked && (!state || shapes.length>0 || Object.keys(state.tasks).length>0); }
 function saveShapes() { sessionStorage.setItem(shapeStorage,JSON.stringify(shapes)); }
 function clearPreview() { $('exercise-fleet-result').textContent=''; }
 let key, state, proposal, shapes=[], busy=false, pollTimer;
 function reset(id) {
  clearPreview();
  key=id;state=null;proposal=null;shapes=[];
  if(id===sessionStorage.getItem('inspection-exercise')){
   shapes=JSON.parse(sessionStorage.getItem(shapeStorage)||'[]');
   attach.checked=sessionStorage.getItem('inspection-attach-sketch')==='true';
  }else{
   attach.checked=false;saveShapes();
  }
  if(shapes.length){attach.checked=true;}
  if(shapes.length || attach.checked) panel.open=true;
  sessionStorage.setItem('inspection-exercise',key);
  render();
 }
 async function api(op, fields={}) {
  const response=await fetch('/api/inspection-rehearsal',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({session:key, revision:state?.revision??0,vehicle:$('sim-vehicle').value,op,...fields})});
  const result=await response.json();
  if(!response.ok) throw Error(result.error||'演练接口未响应');
  return result;
 }
 function namedShapes() {
  const counts={point:0,area:0};
  return [...Object.values(state?.tasks||{}),...shapes].map(t=>{
   const index=++counts[t.kind];
   return {...t,label:t.label || (t.kind==='area'?'巡逻区':'任务点')+index};
  });
 }
 function taskName(id) { return state?.tasks[id]?.label || namedShapes().find(t=>t.id===id)?.label || id; }
 function render() {
  const tasks=Object.values(state?.tasks||{}), incidents=Object.values(state?.incidents||{});
  const mapAttached=usesSketch();
  $('chat-map-context').hidden=!mapAttached && !tasks.length && !shapes.length;
  $('chat-map-count').textContent=mapAttached && !state && !shapes.length ? '正在读取标绘' : tasks.length+' 项已登记，'+shapes.length+' 项待登记';
  $('map-sketch-legend').hidden=!mapAttached && !panel.open;
  sessionStorage.setItem('inspection-attach-sketch',String(attach.checked));
  map.sketches=(panel.open || mapAttached) ? [...namedShapes(),
   ...incidents.map(t=>({...t,label:t.id,incident:true})),...(state?.zones||[]).map(z=>({...z,label:z.id+' 限制区',zone:true}))] : [];
  onContextChange(mapAttached);
  const lanes=Object.entries(state?.lanes||{}), sel=$('sim-vehicle');
  if(lanes.length && sel.length!==lanes.length) sel.replaceChildren(...lanes.map(([k])=>new Option(k,k)));
  if(!lanes.length) sel.replaceChildren(new Option('sim1','sim1'));
  if(lanes.length) $('sim-count').value=String(lanes.length);
  $('sim-count').disabled=busy||lanes.length>0;
  $('register-sketch').disabled=busy||!shapes.length;
  $('undo-sketch').disabled=busy||!shapes.length;
  const stageText={points:'第一阶段 · 全部任务点完成后转入巡逻区',areas:'第二阶段 · 巡逻区'};
  $('exercise-queue').textContent=(stageText[state?.stage]?stageText[state.stage]+'\n':'')+lanes.map(([k,l])=>k+' · '+titles[l.phase]+(l.current?' · '+taskName(l.current):'')+
   (l.queue.length?' → '+l.queue.map(taskName).join(' → '):'')+(l.wait?' · 完成后等待':'')).join('\n')+
   (tasks.length?'\n'+tasks.map(t=>taskName(t.id)+
    (t.blocked_by?.length?'（限制区 '+t.blocked_by.join('、')+'）':t.status==='skipped'?(state.stage==='points'?'（已跳过，未满足转区条件）':'（已跳过）'):t.status==='completed'?'（已完成）':t.priority==='high'?'（优先复核）':'')).join('，'):'');
  $('exercise-history').replaceChildren(...(state?.events||[]).slice().reverse().map(e=>{
   const li=document.createElement('li');li.textContent=new Date(e.at*1000).toLocaleTimeString()+' '+e.text;return li;
  }));
  $('exercise-incidents').replaceChildren(...incidents.map(i=>{
   const d=document.createElement('details'),s=document.createElement('summary');
   s.textContent=i.id+' 积水 · '+i.reports.length+' 条观测 · '+(i.status==='open'?'待复核':'已复核');d.append(s);
   for(const r of i.reports){const p=document.createElement('p');p.textContent=r.source+' · '+r.text+' · 置信度 '+(r.confidence==null?'未提供':r.confidence);d.append(p);}
   if(i.status==='open'){const b=document.createElement('button');b.textContent='人工标记已复核';b.onclick=()=>change('review',{incident:i.id});d.append(b);}
   // Preserve an expanded incident when periodic progress arrives.
   d.open=[...$('exercise-incidents').children].some(el=>el.dataset.id===i.id && el.open);d.dataset.id=i.id;
   return d;
  }));
  if(proposal && proposal.revision!==state?.revision) proposal=null;
  $('exercise-proposal').hidden=!proposal;
  if(proposal) $('exercise-proposal').firstElementChild.textContent=proposal.uid+' · '+actions[proposal.action]+' · '+proposal.reason;
 }
 async function refresh() {
  if(busy||!panel.open) return;
  const id=key;
  try {const s=await api('read');if(id===key && !busy && (!state||s.revision>state.revision)){state=s;render();}}
  catch(e){if(!state)info.textContent=e.message;}
 }
 async function change(op,fields) {
  if(busy)return false;
  busy=true;render();
  try {
   state=await api(op,fields);proposal=null;
   if(op==='draw'){shapes=[];saveShapes();clearPreview();}
   info.textContent='已更新地图标绘与演练';
   return true;
  }
  catch(e){info.textContent=e.message;return false;}
  finally{busy=false;render();void refresh();}
 }
 map.onIncident=id=>{
  const d=[...$('exercise-incidents').children].find(el=>el.dataset.id===id);
  if(d){d.open=true;d.scrollIntoView({block:'nearest'});}
 };
 map.onSketch=(kind,a,b)=>{
  if(busy)return;
  const east=Math.round(a.x),north=Math.round(a.y);
  if(kind==='water'){
   void change('report',{location:{kind:'point',east,north},source:$('report-source').value,note:$('water-note').value,confidence:$('water-confidence').value===''?null:$('water-confidence').valueAsNumber});return;
  }
  const t=kind==='point'?{kind,east,north}:{kind,west:Math.min(east,Math.round(b.x)),east:Math.max(east,Math.round(b.x)),
   south:Math.min(north,Math.round(b.y)),north:Math.max(north,Math.round(b.y))};
  if(kind!=='point' && (t.west===t.east || t.south===t.north)){info.textContent='请拖出有宽度和高度的矩形';return;}
  if(kind==='restrict'){void change('restrict',{location:{...t,kind:'area'}});return;}
  if(shapes.length+Object.keys(state?.tasks||{}).length>=48){info.textContent='本轮最多 48 项';return;}
  const custom=$('sketch-name').value.trim();
  if(custom)t.label=custom;
  const existing=namedShapes(), proposed=custom || (kind==='area'?'巡逻区':'任务点')+(existing.filter(item=>item.kind===kind).length+1);
  if(existing.some(item=>item.label===proposed)){info.textContent='名称重复，请换一个标绘名称';return;}
  $('sketch-name').value='';
  shapes.push(t);saveShapes();attach.checked=true;clearPreview();info.textContent='待登记 '+shapes.length+' 项 · '+Object.entries(t).filter(([k])=>k!=='kind' && k!=='label').map(([k,v])=>k+' '+v).join('，')+' m';
  render();
 };
 function mode() {
  map.sketchMode=panel.open?$('sketch-mode').value:'';
  map.canvas.style.cursor=map.sketchMode?'crosshair':'grab';
  $('water-fields').hidden=map.sketchMode!=='water';
 }
 $('sketch-mode').onchange=mode;
 $('undo-sketch').onclick=()=>{shapes.pop();if(!shapes.length && !Object.keys(state?.tasks||{}).length)attach.checked=false;saveShapes();info.textContent='待登记 '+shapes.length+' 项';render();};
 async function register() {
  // Load the saved rehearsal before a first write, including after a reload.
  if (!state) {
   try {state=await api('read');}
   catch(e){info.textContent=e.message;return false;}
  }
  if (!shapes.length) return true;
  return change('draw',{shapes,count:Object.keys(state.lanes).length || Number($('sim-count').value)});
 }
 $('register-sketch').onclick=async()=>{
  if(await register()){
   attach.checked=true;render();
   info.textContent='已登记。可以继续标绘，也可以预览分工。';
  }
 };
 $('new-exercise').onclick=()=>{if(!busy && confirm('另开一轮桌面演练？')){reset(crypto.randomUUID());void refresh();}};
 $('dismiss-exercise').onclick=()=>{proposal=null;render();};
 $('apply-exercise').onclick=()=>{if(proposal)void change(proposal.action,{vehicle:proposal.uid,revision:proposal.revision});};
 async function previewFleet() {
  const result=await api('fleet_preview');
  const lines=Object.entries(result.assignments).map(([uid,ids])=>uid+' '+(ids.map(taskName).join(' → ')||(result.fleet?.find(a=>a.id===uid)?.reasons?.join('、') || '本轮未分配 · 待命')));
  lines.push(...Object.entries(result.unassigned).map(([id,why])=>taskName(id)+' 待分配 · '+why));
  $('exercise-fleet-result').textContent='XD 遥测分工预览 · '+new Date(result.created_at*1000).toLocaleTimeString()+' · '+lines.join('；');
  return lines.join('；');
 }
 $('preview-exercise-fleet').onclick=async()=>{
  if(busy || !await register())return;
  busy=true;render();
  try {await previewFleet();}
  catch(e){info.textContent=e.message;}finally{busy=false;render();}
 };
 attach.onchange=render;
 $('sim-vehicle').onchange=()=>{proposal=null;render();};
 panel.ontoggle=()=>{mode();render();clearInterval(pollTimer);if(panel.open){void refresh();pollTimer=setInterval(refresh,1500);}};
 reset(sessionStorage.getItem('inspection-exercise')||crypto.randomUUID());
 info.textContent='桌面演练使用模拟机位和任务时钟。点选或拖框后登记，再向 Qwen 提交。';
 return {
  active:()=>usesSketch() || (panel.open && chat.checked),
  attached:usesSketch,
  async interpret(message, liveFleet) {
   if(busy) throw Error('另一个演练请求处理中');
   if(!await register()) throw Error(info.textContent);
   const selected=Object.keys(state.tasks).map(taskName).join('、');
   if(attach.checked && !selected) throw Error('本轮还没有地图任务，请先选点或框选。');
   const uid=$('sim-vehicle').value;
   busy=true;proposal=null;render();
   try {
    const r=await api('interpret',{message});
    if (liveFleet && usesSketch() && !chat.checked) {
     if (r.proposal.action==='plan')
      return '已按本轮地图标绘 '+selected+' 预览 XD 分工。'+await previewFleet();
     if (r.proposal.action==='points_then_areas') return '这个先后顺序可以在桌面演练中运行。请勾选「总对话用于演练」，再提交两阶段指令。XD 地图分工目前只提供预览。';
     if (r.proposal.action!=='status')
      return '地图标绘支持 XD 分工预览。若要演练开始、等待或跳过，请勾选「总对话用于演练」。';
    }
    if(r.proposal.action!=='status' && uid===$('sim-vehicle').value)
     proposal={...r.proposal,uid,revision:r.revision};
    return r.proposal.reason+(proposal?' 请在地图标绘栏核对并确认「'+actions[proposal.action]+'」。':'');
   } finally {busy=false;render();void refresh();}
  }
 };
}
