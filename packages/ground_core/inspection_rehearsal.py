"""Local disaster-inspection exercise. The executor is a clock and has no vehicle I/O."""
import copy
import json
import math
from pathlib import Path
import re
import threading
import time



def geometry(value):
    if not isinstance(value, dict):
        raise ValueError('缺少地图标绘')
    kind = value.get('kind')
    fields = {'point': ('east','north'), 'area': ('west','south','east','north')}.get(kind)
    if not fields or any(type(value.get(k)) not in (int,float) or not math.isfinite(value[k])
                         or abs(value[k]) > 100000 for k in fields):
        raise ValueError('标绘须使用有效的 world 米坐标，范围 ±100 km')
    if kind == 'area' and (value['west'] >= value['east'] or value['south'] >= value['north']):
        raise ValueError('矩形须有正面积')
    return {'kind': kind, **{k:value[k] for k in fields}}


def name_tasks(tasks):
    counts = {'area': 0, 'point': 0}
    for task in tasks.values():
        kind = task['kind']
        counts[kind] += 1
        prefix = '巡逻区' if kind == 'area' else '任务点'
        task.setdefault('label', prefix + str(counts[kind]))


def center(g):
    if g['kind'] == 'point':
        return g['east'], g['north']
    return (g['east'] + g['west']) / 2, (g['north'] + g['south']) / 2


def event(s, now, msg):
    s['events'].append({'at':now, 'text':msg})
    s['events'] = s['events'][-250:]


def advance(s, uid, now):
    lane = s['lanes'][uid]
    lane['current'] = lane['queue'].pop(0) if lane['queue'] else None
    if lane['current']:
        task = s['tasks'][lane['current']]
        lane.update(phase='running', due=now+(90 if task['kind']=='area' else 45))
        task['status'] = 'running'
        event(s, now, uid+' 开始演练 '+task['label'])
    else:
        lane.update(phase='finished', due=None)


def tick(s, now):
    for uid, lane in s['lanes'].items():
        while lane.get('due') is not None and lane['due'] <= now:
            at = lane['due']
            tid = lane['current']
            cancelled = lane['phase'] == 'cancelling'
            s['tasks'][tid]['status'] = 'skipped' if cancelled else 'completed'
            event(s, at, uid+' '+s['tasks'][tid]['label']+(' 模拟中断已回执' if cancelled else ' 模拟巡检已完成'))
            requested_wait = lane['wait']
            wait_here = requested_wait and lane['queue'] and not cancelled
            lane.update(due=None, current=None, wait=False)
            if lane['phase']=='running' and s.get('stage')=='points' and requested_wait and not lane['queue']:
                s['wait_at_transition']=True
            if wait_here:
                lane['phase'] = 'waiting'
            else:
                advance(s, uid, at)
            s['revision'] += 1
    if s.get('stage') == 'points':
        points = [t for t in s['tasks'].values() if t['kind']=='point']
        if points and all(t['status']=='completed' for t in points):
            s['stage'] = 'areas'
            allocate(s,now)
            event(s,now,'全部任务点已完成，进入巡逻区阶段')
            hold = s.pop('wait_at_transition',False)
            for uid,lane in s['lanes'].items():
                if lane['queue']:
                    if hold:
                        lane['phase']='waiting'
                    else:
                        advance(s,uid,now)
            s['revision'] += 1


def allocate(s, now):
    from packages.task_allocation.solvers import solve
    remaining = [t for t in s['tasks'].values() if t['status']=='pending']
    fleet = [dict(id=uid, east_m=0, north_m=i*120, battery_pct=90, speed_mps=18,
                  capabilities=['rgb'], available=lane['phase']!='offline')
             for i,(uid,lane) in enumerate(s['lanes'].items())]
    items = []
    for t in remaining:
        if s.get('stage')=='points' and t['kind']=='area':
            continue
        x,y = center(t)
        west,south = t.get('west',x),t.get('south',y)
        east,north = t.get('east',x),t.get('north',y)
        blocked = [zone['id'] for zone in s.get('zones',[]) if
                   west<=zone['east'] and east>=zone['west'] and south<=zone['north'] and north>=zone['south']]
        t['blocked_by'] = blocked
        if blocked:
            t.pop('priority',None)
            continue
        priority = 'high' if any(i['status']=='open' and (
            (t['west'] <= i['east'] <= t['east'] and t['south'] <= i['north'] <= t['north'])
            if t['kind']=='area' else math.hypot(i['east']-x,i['north']-y)<=25)
            for i in s['incidents'].values()) else 'normal'
        t['priority'] = priority
        items.append(dict(id=t['id'],east_m=x,north_m=y,priority=priority))
    result = solve(fleet,items,'cbba',max(1,len(items))) if items and any(a['available'] for a in fleet) else {}
    for uid, lane in s['lanes'].items():
        lane['queue'] = result.get('assignments',{}).get(uid,[])
        if lane['phase'] == 'finished' and lane['queue']:
            lane['phase'] = 'waiting'
    event(s, now, 'CBBA 已按模拟机位和当前灾情分配待办任务')


ACTIONS = ('start','skip','wait_after','resume','status','offline','plan','points_then_areas')


def interpret(s, uid, message, model, ask):
    if not isinstance(message,str) or not 0 < len(message.strip()) <= 1500:
        raise ValueError('请输入不超过 1500 字的演练指令')
    schema = {'type':'object','properties':{'action':{'type':'string','enum':list(ACTIONS)},
              'reason':{'type':'string'}}, 'required':['action','reason']}
    # Send current objects and a bounded evidence summary. The full log stays on disk.
    context = {k:s[k] for k in ('lanes','tasks')}
    context['stage'] = s.get('stage','free')
    context['restricted_areas'] = s.get('zones',[])
    context['incidents'] = [dict(id=i['id'],status=i['status'],east=i['east'],north=i['north'],
                                  count=len(i['reports']),sources=sorted({r['source'] for r in i['reports']}),
                                  latest=i['reports'][-1]['text'][:100],confidence=i['reports'][-1].get('confidence'))
                            for i in list(s['incidents'].values())[-32:]]
    payload = dict(model=model,stream=False,think=False,format=schema,options={'temperature':0,'num_predict':420},
        messages=[dict(role='system',content='你是民用灾情巡检桌面演练助手。只输出 JSON。根据用户意图选一个操作并用中文解释。'
          '明确要求先完成所有点、再派一架去矩形巡逻时选 points_then_areas，是有效的两阶段计划。'
          'plan 只提交标绘做CBBA分配，start 开始已规划队列，skip 中断当前项转下一项，wait_after 做完当前项等待，'
          'resume 从等待开始下一项，offline 模拟所选机退出并重分未完项。查询、假设、否定操作、指代不明、'
          '指定了与选中机不同的飞机或无法表达的组合均返回status。不要声称动作已执行。回答灾情问题引用来源。'),
          dict(role='user',content=json.dumps(dict(selected=uid,exercise=context,instruction=message),ensure_ascii=False))])
    answer = ask(payload)
    if answer.get('done_reason')=='length':
        raise ValueError('模型生成截断，请缩短请求重试')
    proposal = json.loads(answer['message']['content'])
    if not isinstance(proposal,dict) or proposal.get('action') not in ACTIONS or not isinstance(proposal.get('reason'),str):
        raise ValueError('模型未给出有效的演练意图')
    action = proposal['action']
    labels = dict(points_then_areas='先分配所有任务点，全部点完成后再分配并开始巡逻区演练',plan='按地图登记的几何与灾情优先级重新计算待办分工',
                  start='启动所选模拟机已分配的队列', skip='请求模拟中断，收到回执后进入下一项',
                  wait_after='当前项继续，完成后等待', resume='结束等待，继续下一项',
                  offline='所选模拟机退出，其未完成项目重新分配')
    return dict(action=action,reason=labels.get(action,proposal['reason']))


class Rehearsal:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()

    def request(self, data, model, ask, preview_fleet=None):
        key = data.get('session','')
        if not isinstance(key,str) or not re.fullmatch('[0-9a-f-]{36}',key):
            raise ValueError('演练会话编号无效')
        path = self.root/(key+'.json')
        op, uid = data.get('op'), data.get('vehicle','sim1')
        with self.lock:
            now = time.time()
            s = json.loads(path.read_text()) if path.exists() else dict(revision=0,tasks={},lanes={},incidents={},events=[])
            name_tasks(s['tasks'])
            old = s['revision']
            tick(s,now)
            if s['revision'] != old:
                self.save(path,s)
            if op=='read':
                return s
            if s['revision'] != data.get('revision'):
                raise ValueError('演练进度已变化，请刷新后重试')
            if op=='fleet_preview':
                if not preview_fleet:
                    raise ValueError('请在已接入 XD 遥测的工作台预览')
                if not s['tasks']:
                    raise ValueError('请先登记地图标绘')
                if s.get('zones'):
                    raise ValueError('本轮含桌面演练限制区，XD 遥测预览不读取该图层。请在演练中分配。')
                return preview_fleet(list(s['tasks'].values()))
            if op=='interpret':
                view = copy.deepcopy(s)
            else:
                self.change(s,op,uid,data,now)
                s['revision'] += 1
                self.save(path,s)
                return s
        return dict(proposal=interpret(view,uid,data.get('message'),model,ask),revision=view['revision'])

    def save(self,path,s):
        self.root.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(s,ensure_ascii=False))
        tmp.replace(path)

    def change(self,s,op,uid,data,now):
        if op=='draw':
            shapes=data.get('shapes')
            count=data.get('count',1)
            if type(count) is not int or not 1<=count<=4 or not isinstance(shapes,list) or not 1<=len(shapes)<=48-len(s['tasks']):
                raise ValueError('每轮最多 48 个标绘、1 至 4 架模拟机')
            if s['lanes'] and count!=len(s['lanes']):
                raise ValueError('本轮机数已确定')
            for i in range(count):
                s['lanes'].setdefault('sim'+str(i+1),dict(phase='ready',current=None,queue=[],wait=False,due=None))
            if s.get('stage')=='areas' and any(t.get('kind')=='point' for t in shapes if isinstance(t,dict)):
                raise ValueError('巡逻区阶段已开始；新增点请另开一轮演练')
            for shape in shapes:
                tid='T'+str(len(s['tasks'])+1)
                task=dict(geometry(shape),id=tid,status='pending')
                if 'label' in shape:
                    name=shape['label']
                    if not isinstance(name,str) or not 1<=len(name.strip())<=40:
                        raise ValueError('标绘名称需要 1 至 40 个字符')
                    task['label']=name.strip()
                s['tasks'][tid]=task
                name_tasks(s['tasks'])
                if any(t['label']==task['label'] for t in s['tasks'].values() if t is not task):
                    raise ValueError('标绘名称重复，请换一个名称')

            event(s,now,'已登记地图标绘 '+str(len(shapes))+' 项')
            return
        if op=='restrict':
            zone = geometry(data.get('location'))
            zones = s.setdefault('zones',[])
            if zone['kind']!='area' or len(zones)>=24:
                raise ValueError('限制区需要矩形，每轮最多 24 个')
            if any(l['current'] for l in s['lanes'].values()):
                raise ValueError('请在模拟机开始前或全部等待时设置限制区')
            zones.append(dict(zone,id='Z'+str(len(zones)+1)))
            event(s,now,'已登记演练限制区 '+zones[-1]['id'])
            if s['tasks']:
                allocate(s,now)
            return
        if op=='report':
            g=geometry(data.get('location'))
            source, note = data.get('source'),data.get('note')
            if g['kind']!='point' or source not in ('sim1','sim2','sim3','sim4') or not isinstance(note,str) or not 0<len(note.strip())<=300:
                raise ValueError('观测需要模拟机号、位置及文字')
            incidents=s['incidents']
            matches=[(math.hypot(g['east']-i['east'],g['north']-i['north']),i['id']) for i in incidents.values() if i['status']=='open']
            distance,iid=min(matches,default=(math.inf,''))
            if distance>25:
                if len(incidents)>=64:
                    raise ValueError('本场演练灾情已满')
                iid='W'+str(len(incidents)+1)
                incidents[iid]=dict(g,id=iid,status='open',reports=[])
            if len(incidents[iid]['reports'])>=32:
                raise ValueError('此事件观测数量已满')
            confidence = data.get('confidence')
            if confidence is not None and (type(confidence) not in (float,int) or not 0<=confidence<=1):
                raise ValueError('置信度取 0 至 1，未知时留空')
            incidents[iid]['reports'].append(dict(at=now,source=source,text=note,location=g,confidence=confidence))
            event(s,now,source+' 模拟积水观测归入 '+iid)
            return
        if op=='review':
            incident=s['incidents'].get(data.get('incident'))
            if not incident or incident['status']!='open':
                raise ValueError('请选择一个待复核事件')
            incident['status']='reviewed'
            event(s,now,'人工标记已复核 '+incident['id'])
            return
        if op=='points_then_areas':
            if {t['kind'] for t in s['tasks'].values()} != {'point','area'}:
                raise ValueError('两阶段演练至少需要一个任务点和一个巡逻区')
            if any(t['status']!='pending' for t in s['tasks'].values()):
                raise ValueError('请在本轮开始前设置两阶段演练')
            s['stage']='points'
            event(s,now,'已设置先完成所有点，再巡逻区域')
            allocate(s,now)
            return
        if op=='plan' and s['tasks']:
            s.pop('stage',None)
            s.pop('wait_at_transition',None)
            allocate(s,now)
            return
        lane=s['lanes'].get(uid)
        if not lane:
            raise ValueError('请先登记地图标绘并选择模拟机')
        if op in ('start','resume') and lane['phase'] in ('ready','waiting','finished') and lane['queue']:
            advance(s,uid,now)
        elif op=='wait_after' and lane['phase']=='running':
            lane['wait']=True
            event(s,now,uid+' 本项完成后等待')
        elif op=='skip' and lane['phase']=='running':
            lane.update(phase='cancelling',due=now+2)
            event(s,now,uid+' 请求中断 '+s['tasks'][lane['current']]['label'])
        elif op=='offline' and lane['phase'] not in ('offline','cancelling'):
            if lane['current']:
                s['tasks'][lane['current']]['status']='pending'
            lane.update(phase='offline',due=None,current=None,queue=[])
            event(s,now,uid+' 模拟退出，未完成工作返回待办')
            allocate(s,now)
        else:
            raise ValueError('当前演练阶段无法执行此操作，请核对队列及阶段')
        if op=='resume':
            lane['wait']=False
