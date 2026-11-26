const $ = id => document.getElementById(id);
const title = kind => kind==='area' ? '巡逻区' : '任务点';
const url = '/api/xd/task-library';

async function request(method='GET', body) {
 const response=await fetch(url,{method,body:body && JSON.stringify(body),
  headers:{'Content-Type':'application/json'},signal:AbortSignal.timeout(5000)});
 const value=await response.json();
 if (!response.ok) {
  const error=new Error(value.error || '任务库暂不可用');
  error.conflict=response.status===409;
  throw error;
 }
 return value;
}

export function setupTaskLibrary(map,onChange) {
 const panel=$('task-library'), mode=$('library-mode'), attach=$('use-task-library');
 const list=$('library-items'), info=$('library-status'), name=$('library-name');
 const submit=$('library-submit'), reload=$('library-reload'), remove=$('library-delete');
 let run, snapshot, selected, items=[], dirty=false, saving=false, retry=false;

 function applyMode() {
  map.sketchMode=panel.open && snapshot && !saving ? mode.value : '';
  map.canvas.style.cursor=map.sketchMode?'crosshair':'grab';
 }
 function render() {
  const selection=items.find(t=>t.id===selected);
  map.sketches=items.map(t=>({...t,selected:t.id===selected}));
  applyMode();
  submit.disabled=!snapshot || !dirty || saving || retry;
  remove.disabled=!selection || saving;
  mode.disabled=!snapshot || saving;
  name.disabled=mode.disabled;
  reload.hidden=!retry;
  attach.disabled=!snapshot;
  $('map-sketch-legend').hidden=!items.length;
  $('library-chat-context').hidden=!snapshot;
  $('library-text').hidden=!snapshot?.tasks.length;
  $('library-text').querySelector('pre').textContent=(snapshot?.tasks||[]).map(t=>t.kind==='point'
    ? t.label+' · 东 '+t.east+'、北 '+t.north+' m'
    : t.label+' · 西 '+t.west+'、东 '+t.east+'、南 '+t.south+'、北 '+t.north+' m').join('\n');
  $('library-chat-count').textContent=snapshot ? '已提交 '+snapshot.tasks.length+' 项'+(dirty?' · 有未提交修改':'') : '';
  list.replaceChildren(...items.map(t=>{
   const row=document.createElement('li');
   const button=document.createElement('button');
   button.type='button';
   button.textContent=t.label+' · '+title(t.kind);
   button.setAttribute('aria-pressed',String(t.id===selected));
   button.onclick=()=>select(t.id);
   row.append(button);
   return row;
  }));
  onChange();
 }
 function select(id) {
  selected=id;
  render();
  return true;
 }
 function edit(next) {
  items=next;
  dirty=JSON.stringify(items)!==JSON.stringify(snapshot.tasks);
  attach.checked=true;sessionStorage.setItem('xd-task-library-mode:'+run,'on');
  info.textContent=dirty?'有未提交修改。提交后 Qwen 会使用这一版。':'与已提交版本一致';
  render();
 }
 function accept(value) {
  snapshot=value;
  items=value.tasks;
  selected=undefined;
  dirty=false;
  retry=false;
  info.textContent=items.length?'已提交 '+items.length+' 项':'任务库为空';
 }
 async function load(id) {
  run=id;
  items=[];snapshot=null;selected=undefined;
  saving=false;dirty=false;retry=false;attach.checked=false;
  mode.value='';name.value='';
  info.textContent='正在读取本轮任务库…';
  render();
  try {
   const value=await request();
   if(run!==id)return;
   if(value.run_id!==id)throw Error('仿真场次已变化，正在等待最新遥测');
   accept(value);
   attach.checked=sessionStorage.getItem('xd-task-library-mode:'+id)!=='off';
  } catch(e) {
   if(run!==id)return;
   info.textContent=e.message;
   retry=true;
  }
  render();
 }
 submit.onclick=async()=>{
  if(submit.disabled)return;
  const id=run;
  saving=true;
  info.textContent='正在提交任务库…';
  render();
  try {
   const value=await request('POST',{run_id:id,revision:snapshot.revision,tasks:items});
   if(run!==id)return;
   accept(value);
   attach.checked=true;
   sessionStorage.setItem('xd-task-library-mode:'+run,'on');
  }catch(e){
   if(run!==id)return;
   retry=Boolean(e.conflict);
   info.textContent=e.message;
  }finally{
   if(run===id){saving=false;render();}
  }
 };
 reload.onclick=()=>{
  if(!dirty || confirm('重新读取会放弃当前未提交的修改。继续吗？'))void load(run);
 };
 remove.onclick=()=>{
  if(!selected || saving)return;
  const id=selected;
  selected=undefined;
  edit(items.filter(t=>t.id!==id));
 };
 map.onSketch=(kind,a,b)=>{
  if(!snapshot || saving)return;
  if(items.length>=snapshot.max_items){info.textContent='本轮最多保存 '+snapshot.max_items+' 项';return;}
  const east=Math.round(a.x), north=Math.round(a.y);
  let shape={kind,east,north};
  if(kind==='area'){
   const x=Math.round(b.x), y=Math.round(b.y);
   if(east===x || north===y){info.textContent='请拖出有宽度和高度的矩形';return;}
   shape={kind,west:Math.min(east,x),east:Math.max(east,x),south:Math.min(north,y),north:Math.max(north,y)};
  }
  const names=new Set(items.map(t=>t.label));
  let label=name.value.trim();
  if(label && names.has(label)){info.textContent='这个名称已使用，请换一个';return;}
  if(!label){
   let n=1;
   while(names.has(title(kind)+n))n++;
   label=title(kind)+n;
  }
  const id=crypto.randomUUID();
  selected=id;
  name.value='';
  edit([...items,{...shape,id,label}]);
 };
 map.onSelectSketch=(x,y)=>{
  if(!panel.open)return false;
  const world=map.world(x,y);
  const point=items.find(t=>{
   if(t.kind!=='point')return false;
   const p=map.screen({x:t.east,y:t.north});
   return Math.hypot(p.x-x,p.y-y)<=12;
  });
  const area=point || items.slice().reverse().find(t=>t.kind==='area' &&
    world.x>=t.west && world.x<=t.east && world.y>=t.south && world.y<=t.north);
  return area ? select(area.id) : false;
 };
 panel.ontoggle=applyMode;
 mode.onchange=applyMode;
 attach.onchange=()=>{
  sessionStorage.setItem('xd-task-library-mode:'+run,attach.checked?'on':'off');
  onChange();
 };
 return {
  load,
  active:()=>Boolean(snapshot && attach.checked),
  pending:()=>dirty || saving,
  context:()=>({library_run_id:run,library_revision:snapshot.revision}),
  matches:plan=>!dirty && snapshot?.revision===plan.library_revision && run===plan.run_id
 };
}
