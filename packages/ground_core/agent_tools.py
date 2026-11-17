"""Tool contracts and domain adapters for the Qwen ground-station agent."""
import copy
import math
import time
from uuid import uuid4
from .tasks import TARGET_SCHEMA,normalize_targets
from .commands import status_reply


def object_schema(properties=None,required=None):
    return {'type':'object','properties':properties or {},'required':required or [],'additionalProperties':False}


IDS={'type':'array','items':{'type':'string'},'maxItems':3}
ALGORITHM={'type':'string','enum':['cbba','hungarian','auction','greedy'],
           'description':'cbba/greedy支持一机多任务；hungarian/auction当前仅一对一，任务多于可用飞机不可选。'}
AGENT_TARGET=copy.deepcopy(TARGET_SCHEMA)
AGENT_TARGET['required']=['frame']
AGENT_TARGET['description']='按frame填写：local/wgs84必须提供数值x、y；zone必须提供zone_id，x/y可省略或为null。'
AGENT_TARGET['properties']['sensor']['description']='默认 rgb；仅用户明确要求热成像或热源时设 thermal，紧急优先级不改变传感器。'
AGENT_TARGET['properties']['priority']['description']='默认normal；用户说紧急/优先/尽快时可设high。'


def definition(name,label,description,properties=None,required=None,write=False):
    return {'label':label,'write':write,'wire':{'type':'function','function':{
        'name':name,'description':description,'parameters':object_schema(properties,required)}}}


REGISTRY=[
 definition('get_fleet_state','读取集群状态','读取实时位置、电量、解锁/离地状态、模式、载荷和当前任务。需要当前事实或选择飞机时调用。'),
 definition('get_mission_status','读取任务进度','读取当前执行状态、各机进度和剩余目标，用于跟进、暂停或重新规划。'),
 definition('get_plan','读取已有方案','读取本会话刚才的方案及完整目标，理解“刚才方案/第二个点/那架飞机”等指代。空 plan_id 表示最新方案。',{'plan_id':{'type':'string'}},['plan_id']),
 definition('plan_inspection','规划巡检任务','创建或修改方案，不执行。用户提供东/西/北/南距离就足够：用local，x是向东米数，y是向北米数，西/南取负；公里转米，不必询问经纬度。local/wgs84需要数值x/y；zone仅已有区域，只需zone_id，x/y可省略。无旧方案时直接创建，不先找旧方案。targets=[]仅复用旧目标；改优先级需给出修改后的完整目标。算法默认CBBA，飞机默认全部可用，RGB默认，不要替用户增加热成像要求。范围或容量错误需要用户调整任务，不擅自改区域或丢弃目标。',
    {'targets':{'type':'array','items':AGENT_TARGET,'maxItems':6},'algorithm':ALGORITHM,'aircraft_ids':IDS},['targets']),
 definition('compare_allocations','比较分配算法','对已有方案按相同遥测快照运行多种算法，返回真实分配和估计航程/时间；不下发、不起飞。空 plan_id 表示最新方案。',
    {'plan_id':{'type':'string'},'algorithms':{'type':'array','items':ALGORITHM,'minItems':1,'maxItems':4}},['plan_id','algorithms']),
 definition('execute_plan','执行任务方案','按最新状态重新计算并执行本会话已有方案。地面飞机会自动起飞，空中飞机接续航点，不重复起飞。仅在用户要求实际执行时使用；仅比较/讨论/草案时不要使用。已有任务执行中应先查询进度，再按用户意图盘旋暂停后换方案。返回排队/接收回执，不等于任务已完成。',
    {'plan_id':{'type':'string'},'algorithm':ALGORITHM},['plan_id'],write=True),
 definition('takeoff_aircraft','起飞并盘旋等待','没有巡检目标时的专用飞行动作：让固定翼起飞并进入等待航段，不需要地点，不创建巡检任务。只有已有巡检目标方案需要执行时才使用execute_plan。未指定飞机时用空 aircraft_ids，表示全部可用地面飞机。不是悬停，回执不等于已离地。',
    {'aircraft_ids':IDS},['aircraft_ids'],write=True),
 definition('control_aircraft','改变飞行任务状态','对空中飞机执行 hold=在当前位置附近盘旋等待，resume=继续原任务，return_home=交由PX4返航。空 aircraft_ids 表示全部。固定翼不悬停，不强制解锁，不打断起飞过程。回执只表示指令接收，需要查询遥测确认实际状态。',
    {'aircraft_ids':IDS,'action':{'type':'string','enum':['hold','resume','return_home']}},['aircraft_ids','action'],write=True),
]
BY_NAME={d['wire']['function']['name']:d for d in REGISTRY}


def validate(value,schema,path='arguments'):
    kind=schema.get('type')
    if isinstance(kind,list):
        if value is None and 'null' in kind:return
        kind=next((t for t in kind if t!='null'),None)
    checks={'object':lambda:isinstance(value,dict),'array':lambda:isinstance(value,list),
            'string':lambda:isinstance(value,str),'number':lambda:isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value),
            'boolean':lambda:isinstance(value,bool),'null':lambda:value is None}
    if kind in checks and not checks[kind]():raise ValueError(path+' 类型错误')
    if 'enum' in schema and value not in schema['enum']:raise ValueError(path+' 不在可选范围')
    if kind=='object':
        props=schema.get('properties',{})
        missing=[k for k in schema.get('required',[]) if k not in value]
        if missing:raise ValueError(path+' 缺少必需参数：'+', '.join(missing))
        if schema.get('additionalProperties') is False and set(value)-set(props):raise ValueError(path+' 存在未知参数')
        for k,v in value.items():
            if k in props:validate(v,props[k],path+'.'+k)
    if kind=='array':
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',100):raise ValueError(path+' 数量超出范围')
        for i,v in enumerate(value):validate(v,schema['items'],path+'[%d]'%i)


def prepare_targets(targets):
    """Validate each coordinate frame without interpreting the user's wording."""
    result=[]
    for i,t in enumerate(targets):
        prefix='targets[%d]'%i
        if t['frame']=='zone':
            if not t.get('zone_id'):
                raise ValueError(prefix+' 为区域任务，需要 zone_id（例如地图已有的 S），无需 x/y')
            if t.get('x') is not None or t.get('y') is not None:
                raise ValueError(prefix+' 为区域任务，x/y应省略或为null；明确点坐标请使用local/wgs84')
        else:
            missing=[k for k in ('x','y') if t.get(k) is None]
            if missing:
                raise ValueError(prefix+' 的 '+t['frame']+' 坐标需要数值参数：'+', '.join(missing))
        label=t.get('label') or (t['zone_id']+'区' if t['frame']=='zone' else '巡检点%d'%(i+1))
        result.append({'label':label,'frame':t['frame'],
            'x':t.get('x'),'y':t.get('y'),'zone_id':t.get('zone_id',''),
            'priority':t.get('priority','normal'),'sensor':t.get('sensor','rgb')})
    return result


class ToolRuntime:
    def __init__(self,environment,conversation,mode,user_text):
        self.env=environment;self.conversation=conversation;self.mode=mode;self.user_text=user_text
        self.execution=None;self.draft=None

    def definitions(self):
        has_plan=bool(self.conversation.get('last_plan_id'))
        state=self.env['simulation_state']()
        airborne=any(a.get('armed') and a.get('landed') is False for a in state.get('fleet',[]))
        return [d['wire'] for d in REGISTRY if (self.mode=='live' or not d['write'])
                and (has_plan or d['wire']['function']['name'] not in ('get_plan','compare_allocations','execute_plan'))
                and (airborne or d['wire']['function']['name']!='control_aircraft')]

    def context(self):
        plans=self.conversation.get('plans',{})
        latest=plans.get(self.conversation.get('last_plan_id'))
        return {'mode':self.mode,'origin':{'longitude':103.565,'latitude':31.045},
            'zones':[{'id':z['id'],'name':z['name']} for z in self.env['scene']['zones']],
            'latest_plan':None if latest is None else {'id':latest['id'],'targets':latest['draft']['targets'],
                'algorithm':latest['algorithm'],'aircraft_ids':latest['draft']['aircraft_ids']},
            'last_execution_id':self.conversation.get('last_execution_id'),
            'last_selected_aircraft':self.conversation.get('last_selected_aircraft',[]),
            'previous_turn_status':self.conversation.get('last_turn_status'),
            'task_capabilities':{'waypoints':'absolute local ENU metres from simulation origin or WGS84',
                'hold':'unlimited loiter, no finite orbit count',
                'return_home':'separate PX4 RTL command, not a verified return-to-base sequence',
                'unsupported':['formation-relative displacement','finite orbit count','automatic return after orbit']},
            'aircraft_names':{'一号机':'FW-01','二号机':'FW-02','三号机':'FW-03'}}

    def state(self):
        return self.env['live_state']() if self.mode=='live' else self.env['demo_state']()

    def session(self):
        session=self.env['session']()
        if self.mode!='live' or not session or not session.snapshot()['ready']:
            raise RuntimeError('仿真执行器未就绪，请先切换PX4模式并启动固定翼仿真')
        return session

    def fleet(self):
        session=self.env['session']()
        if self.mode=='live' and session and session.snapshot()['ready']:
            fleet=session.snapshot()['fleet']
            for a in fleet:
                a['available']=a['available'] and time.time()-a['observed_timestamp']<2
            return fleet
        from .tasks import local_xy
        return [dict(id=a['id'],east_m=local_xy(a['longitude'],a['latitude'],{'lon':103.565,'lat':31.045})[0],
             north_m=local_xy(a['longitude'],a['latitude'],{'lon':103.565,'lat':31.045})[1],
             battery_pct=a['battery_pct'],available=not a['stale'],speed_mps=a['speed_mps'] or 18,capabilities=['rgb'],
             state_source=a['source']) for a in self.state()['aircraft']]

    def get_plan(self,plan_id=''):
        key=plan_id or self.conversation.get('last_plan_id')
        plan=self.conversation.get('plans',{}).get(key)
        if not plan:raise ValueError('本会话尚无该方案，请先规划任务或询问用户目标')
        return plan

    def call(self,name,args):
        definition=BY_NAME.get(name)
        if not definition:raise ValueError('未知工具：'+name)
        if definition['write'] and self.mode!='live':raise ValueError('演示模式没有飞控执行工具')
        validate(args,definition['wire']['function']['parameters'])
        from packages.task_allocation.solvers import solve
        if name=='get_fleet_state':
            state=self.state();snap=self.env['simulation_state']();report=snap.get('report',{})
            return {'observed_at':time.time(),'aircraft':state['aircraft'],'planning_fleet':self.fleet(),
                'sensor_text_observations':report.get('observations',{}),
                'simulation':{'status':snap['status'],'ready':snap['ready']},
                'summary':status_reply(state,report),'mission_status':report.get('status')}
        if name=='get_mission_status':
            report=self.env['simulation_state']().get('report',{})
            visited={tid for a in report.get('aircraft',{}).values() for tid in a.get('visited',{})}
            return {'status':report.get('status','offline'),'execution_id':report.get('execution_id'),
                'assignments':report.get('allocation',{}).get('assignments',{}),
                'visited_task_ids':sorted(visited),'remaining_targets':[t['request_target'] for t in report.get('tasks',[]) if t['id'] not in visited and 'request_target' in t],
                'controls':report.get('controls',{}),'aircraft':self.state()['aircraft'],'error':report.get('error')}
        if name=='get_plan':return copy.deepcopy(self.get_plan(**args))
        if name=='plan_inspection':
            targets=args['targets']
            if not targets:targets=self.get_plan()['draft']['targets']
            targets=prepare_targets(targets)
            fleet=self.fleet()
            allowed=args.get('aircraft_ids',[])
            if set(allowed)-{a['id'] for a in fleet}:raise ValueError('方案包含未知飞机编号，请读取集群状态')
            priority=max((t['priority'] for t in targets),key=lambda p:{'low':0,'normal':1,'high':2}[p])
            draft={'title':'自然语言巡检方案','area':'用户指定目标','task_type':'inspect',
                   'priority':priority,'notes':'由 Qwen 自主选择工具与算法生成，尚未执行。',
                   'aircraft_ids':allowed,'targets':copy.deepcopy(targets)}
            tasks=normalize_targets(draft,{'lon':103.565,'lat':31.045},self.env['scene']['zones'])
            for t in tasks:t['source']='qwen_native_tool_call'
            algorithm=args.get('algorithm','cbba')
            allocation=solve(fleet,tasks,algorithm,capacity=2)
            if allocation['unassigned']:raise ValueError('任务存在无法分配项，请检查载荷、电量、容量或飞机限制')
            plan={'id':uuid4().hex[:12],'created_at':time.time(),'draft':draft,'tasks':tasks,
                  'algorithm':algorithm,'allocation':allocation,'mode':self.mode,'executed':False,
                  'available_next_tools':['compare_allocations','execute_plan','get_plan']}
            self.conversation.setdefault('plans',{})[plan['id']]=plan
            self.conversation['last_plan_id']=plan['id']
            self.draft=draft
            return copy.deepcopy(plan)
        if name=='compare_allocations':
            plan=self.get_plan(args['plan_id']);fleet=self.fleet();results=[]
            for algorithm in args['algorithms']:
                try:
                    count=sum(a.get('available',False) and a.get('battery_pct') is not None and a['battery_pct']>=25 for a in fleet)
                    capacity=1 if len(plan['tasks'])<=count else 2
                    result=solve(fleet,plan['tasks'],algorithm,capacity=capacity)
                    results.append({'algorithm':algorithm,'feasible':not result['unassigned'],
                        'assignments':result['assignments'],'metrics':result['metrics'],'capacity':capacity})
                except (ValueError,RuntimeError) as exc:results.append({'algorithm':algorithm,'feasible':False,'reason':str(exc)})
            return {'plan_id':plan['id'],'results':results,'input_fleet':fleet,
                    'note':'不同算法目标函数不同；这里只比较同一快照下的直线航程估计，不保证固定翼最优航迹，也没有下发'}
        if name=='execute_plan':
            plan=self.get_plan(args['plan_id'])
            if plan.get('execution'):self.execution=plan['execution'];return {'already_submitted':True,**copy.deepcopy(self.execution)}
            algorithm=args.get('algorithm',plan['algorithm'])
            result=self.session().submit(plan['tasks'],plan['draft'],source_text=self.user_text,algorithm=algorithm)
            plan.update(execution=result,executed=True,algorithm=algorithm,allocation=result['allocation'])
            self.conversation['last_execution_id']=result['id'];self.execution=result
            self.conversation['last_selected_aircraft']=[uid for uid,route in result['allocation']['assignments'].items() if route]
            return {'execution_id':result['id'],'status':result['status'],'allocation':result['allocation'],
                    'current_flight_facts':status_reply(self.state()),
                    'note':'只确认已按最新遥测分配并排队；没有确认起飞或完成，不得向用户声称已起飞或完成'}
        if name=='takeoff_aircraft':
            result=self.session().takeoff(args['aircraft_ids'],source_text=self.user_text)
            self.execution=result;self.conversation['last_execution_id']=result['id']
            self.conversation['last_selected_aircraft']=result['aircraft_ids']
            return {**result,'current_aircraft':self.state()['aircraft'],
                'current_flight_facts':status_reply(self.state()),
                'postcondition_airborne_verified':False,
                'note':'这里只提交起飞任务，尚未确认任何飞机起飞。请明确告诉用户等待回执和离地反馈，不能说已起飞。'}
        if name=='control_aircraft':
            result=self.session().control(args['action'],args['aircraft_ids'])
            self.conversation['last_selected_aircraft']=[r['id'] for r in result['receipts']]
            return result
        raise ValueError('工具尚未实现')
