"""Interactive SITL session: wait for Qwen tasks, bid on live state, execute."""
import copy
import json
import math
from pathlib import Path
import queue
import threading
import time
from uuid import uuid4

from packages.task_allocation.cbba import allocate
from packages.task_allocation.solvers import solve
from .mavlink_io import M, Vehicle, to_local
from .observations import flight_observation
from .sitl import ROOT, launch, stop
from .verify_flight import mission, mav_mode, write_json


class Session:
    def __init__(self, receive_telemetry):
        self.id=uuid4().hex[:12]
        self.run_dir=ROOT/'runs/interactive'/self.id
        self.receive_telemetry=receive_telemetry
        self.lock=threading.RLock()
        self.stop_event=threading.Event()
        self.jobs=queue.Queue()
        self.vehicles=[]
        self.fleet=[]
        self.status='starting'
        self.sensor_bad_since={}
        self.report={'kind':'Qwen task -> live-state CBBA -> PX4 fixed-wing',
                     'session_id':self.id,'started_at':time.time(),'tasks':[],
                     'aircraft':{},'success':False,'status':'starting','observations':{},
                     'limits':['flat Gazebo world; real map terrain not imported',
                               'payload capabilities declared in scenario config',
                               'no dynamic collision avoidance or automatic link-loss re-auction']}
        self.thread=threading.Thread(target=self._monitor,daemon=True,name='sitl-session')
        self.worker=threading.Thread(target=self._worker,daemon=True,name='sitl-missions')

    def start(self):
        self.thread.start()
        self.worker.start()

    def snapshot(self):
        with self.lock:
            return {'id':self.id,'status':self.status,'ready':self.status=='ready',
                    'fleet':copy.deepcopy(self.fleet),'report':copy.deepcopy(self.report)}

    def plan(self, tasks, algorithm='cbba'):
        with self.lock:
            if self.status!='ready':
                raise RuntimeError('固定翼仿真尚未就绪，请先启动并等待遥测上线')
            fleet=copy.deepcopy(self.fleet)
            for a in fleet:
                a['available']=a['available'] and time.time()-a['observed_timestamp']<2
            plan=solve(fleet,tasks,algorithm,capacity=2)
            if plan['unassigned']:
                raise ValueError('部分任务没有可用飞机：请检查电量、载荷要求、遥测和每机 2 点容量')
            return plan

    def submit(self, tasks, draft, source_text='', operation='inspection', algorithm='cbba'):
        with self.lock:
            if self.report['status'] in ('queued','uploading','executing','error','timeout','sensor_error'):
                raise RuntimeError('已有任务正在执行。需要改变任务时，先查询进度并令相关飞机盘旋，再提交新方案')
            plan=self.plan(tasks,algorithm)
            execution_id=uuid4().hex[:12]
            self.report.update(tasks=copy.deepcopy(tasks),allocation=plan,status='queued',
                               success=False,execution_id=execution_id,draft=copy.deepcopy(draft),
                               source='qwen_user_request',submitted_at=time.time(),aircraft={},operation=operation,dispatched=False,controls={})
            self.report['command_text']=source_text
            self.report.pop('error',None)
            self.report.pop('completed_at',None)
            self.report.pop('verification_duration_s',None)
            for v in self.vehicles:
                self.report['aircraft'][v.spec['id']]={'mission_accepted':False,'visited':{},
                   'minimum_distance_m':{},'max_relative_alt_m':0,'max_speed_mps':0}
            self.jobs.put((execution_id,plan,tasks))
            return {'id':execution_id,'status':'queued','allocation':copy.deepcopy(plan)}

    def takeoff(self, aircraft_ids=None, source_text=''):
        with self.lock:
            if self.status!='ready':
                raise RuntimeError('请先启动固定翼仿真并等待遥测上线')
            requested=set(aircraft_ids or [])
            known={a['id'] for a in self.fleet}
            if requested-known:
                raise ValueError('起飞指令包含未知飞机编号')
            selected=[a for a in self.fleet if not requested or a['id'] in requested]
            selected=[a for a in selected if a['available'] and time.time()-a['observed_timestamp']<2
                      and a.get('battery_pct') is not None and a['battery_pct']>=25]
            if not selected:
                raise RuntimeError('没有遥测新鲜且电量充足的飞机可以起飞')
            ground=[a for a in selected if a.get('landed') is True or
                    (a.get('armed') is False and abs(a.get('relative_alt_m',999))<5)]
            if not ground:
                raise RuntimeError('所选飞机已在空中或离地状态不明，本次不重复发送起飞')
            tasks=[]
            for a in ground:
                east=a['east_m']+650
                north=a['north_m']
                if math.hypot(east,north)>2500:
                    raise ValueError('起飞后等待位置超出本轮仿真范围')
                tasks.append({'id':'TAKEOFF-'+a['id'],'label':a['id']+' 起飞后等待点',
                    'east_m':east,'north_m':north,'priority':'normal',
                    'allowed_aircraft_ids':[a['id']],'required_capabilities':[],
                    'source':'takeoff_hold_planner'})
            draft={'title':'固定翼起飞后盘旋等待','operation':'takeoff','aircraft_ids':[a['id'] for a in ground]}
            result=self.submit(tasks,draft,source_text,operation='takeoff')
            result['aircraft_ids']=draft['aircraft_ids']
            return result

    def control(self, action, aircraft_ids=None):
        if action not in ('hold','resume','return_home'):
            raise ValueError('不支持的飞控动作')
        with self.lock:
            if self.status!='ready':
                raise RuntimeError('仿真未就绪')
            if self.report['status'] in ('queued','uploading'):
                raise RuntimeError('任务正在上传，请等待飞控回执后再改变模式')
            ids=set(aircraft_ids or [])
            if ids-{v.spec['id'] for v in self.vehicles}:
                raise ValueError('未知飞机编号')
            fleet={a['id']:a for a in self.fleet}
            selected=[v for v in self.vehicles if not ids or v.spec['id'] in ids]
            receipts=[];skipped=[]
            for v in selected:
                a=fleet.get(v.spec['id'],{})
                if not a.get('available') or time.time()-a.get('observed_timestamp',0)>2:
                    raise RuntimeError(v.spec['id']+' 遥测过期')
                taking_off=(self.report.get('status')=='executing' and a.get('relative_alt_m',0)<30)
                if not a.get('armed') or a.get('landed') is not False or taking_off:
                    skipped.append({'id':v.spec['id'],'reason':'在地面或仍处于起飞阶段，不改变模式'});continue
                if action=='return_home':
                    ack=v.command(M.MAV_CMD_NAV_RETURN_TO_LAUNCH)
                    command='MAV_CMD_NAV_RETURN_TO_LAUNCH'
                else:
                    ack=v.command(M.MAV_CMD_DO_SET_MODE,M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,4,3 if action=='hold' else 4)
                    command='MAV_CMD_DO_SET_MODE'
                receipt={'id':v.spec['id'],'action':action,'px4_command':command,'ack':ack,'issued_at':time.time()}
                receipts.append(receipt)
                self.report.setdefault('controls',{})[v.spec['id']]=receipt
            if receipts:
                if action=='resume' and self.report.get('status')=='paused':
                    pause=self.report.pop('paused_at',time.time())
                    self.report['executing_at']+=time.time()-pause
                    self.report['status']='executing'
                elif action in ('hold','return_home'):
                    routes=self.report.get('allocation',{}).get('assignments',{})
                    pending=[uid for uid,route in routes.items() if len(self.report['aircraft'][uid]['visited'])<len(route)]
                    if pending and all(self.report.get('controls',{}).get(uid,{}).get('action') in ('hold','return_home') for uid in pending):
                        self.report.update(status='paused',paused_at=time.time())
            return {'receipts':receipts,'skipped':skipped,'status':'accepted' if receipts else 'no_airborne_aircraft',
                    'note':'回执表示模式指令已接收，实际飞行状态需继续读取遥测'}

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                execution_id,plan,tasks=self.jobs.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                self._execute(execution_id,plan,tasks)
            except Exception as exc:
                with self.lock:
                    self.report.update(status='error',error=str(exc),success=False)

    def _execute(self, execution_id, plan, tasks):
        with self.lock:
            self.report['status']='uploading'
        participating=[]
        for v in self.vehicles:
            route=plan['assignments'].get(v.spec['id'],[])
            if not route:
                continue
            if self.stop_event.is_set():
                return
            pos=v.snapshot().get('GLOBAL_POSITION_INT',{})
            airborne=pos.get('relative_alt',0)>30000
            items=mission(v.spec,route,tasks,self.report['origin'],airborne=airborne)
            with v.condition:
                v.state.pop('MISSION_ITEM_REACHED',None)
            ack=v.upload(items)
            with self.lock:
                self.report['aircraft'][v.spec['id']].update(mission=items,upload=ack,mission_accepted=True)
            participating.append(v)
        time.sleep(1.5)
        for v in participating:
            if self.stop_event.is_set():
                return
            ack=v.command(M.MAV_CMD_DO_SET_MODE,M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,4,4)
            arm=None
            if not v.snapshot().get('HEARTBEAT',{}).get('base_mode',0)&M.MAV_MODE_FLAG_SAFETY_ARMED:
                arm=v.command(M.MAV_CMD_COMPONENT_ARM_DISARM,1)
            with self.lock:
                self.report['aircraft'][v.spec['id']].update(mode_ack=ack,arm_ack=arm)
        with self.lock:
            self.report.update(status='executing',executing_at=time.time(),dispatched=True)

    def _monitor(self):
        launched=False
        telemetry=None
        try:
            meta=launch(self.run_dir)
            launched=True
            (self.run_dir/'missions').mkdir(exist_ok=True)
            with self.lock:
                self.report['origin']=meta['origin']
            payloads=json.loads((ROOT/'config/fleet.json').read_text())['aircraft']
            self.vehicles=[Vehicle(a) for a in meta['aircraft']]
            for v in self.vehicles:
                hb=v.wait('HEARTBEAT',timeout=40)
                if hb.type!=M.MAV_TYPE_FIXED_WING or hb.autopilot!=M.MAV_AUTOPILOT_PX4:
                    raise RuntimeError('This session accepts PX4 fixed-wing SITL only')
                v.wait('GLOBAL_POSITION_INT',timeout=40)
                v.wait('SYS_STATUS',timeout=20)
                v.wait('BATTERY_STATUS',timeout=20)
            with self.lock:
                self.status='ready'
                self.report['status']='waiting_for_task'
            telemetry=(self.run_dir/'telemetry.jsonl').open('w')
            while not self.stop_event.is_set():
                fleet=[]
                now=time.time()
                for v in self.vehicles:
                    state=v.snapshot()
                    pos=state.get('GLOBAL_POSITION_INT')
                    if not pos:
                        continue
                    gps_age=time.monotonic()-state.get('position_received_at',0)
                    hb_age=time.monotonic()-state.get('HEARTBEAT_received_at',0)
                    battery_age=time.monotonic()-state.get('BATTERY_STATUS_received_at',0)
                    battery=state.get('BATTERY_STATUS',{}).get('battery_remaining',-1)
                    battery=battery if battery>=0 and battery_age<3 else None
                    east,north=to_local(pos['lat']/1e7,pos['lon']/1e7,meta['origin'])
                    speed=math.hypot(pos['vx'],pos['vy'])/100
                    hb=state.get('HEARTBEAT',{})
                    landed_code=state.get('EXTENDED_SYS_STATE',{}).get('landed_state')
                    landed=True if landed_code==M.MAV_LANDED_STATE_ON_GROUND else False if landed_code==M.MAV_LANDED_STATE_IN_AIR else None
                    fresh=gps_age<2 and hb_age<3
                    payload=payloads[v.spec['id']]
                    fleet.append({'id':v.spec['id'],'east_m':east,'north_m':north,
                        'battery_pct':battery,'available':fresh and hb.get('system_status') in (3,4),
                        'speed_mps':speed if speed>10 else payload['nominal_speed_mps'],
                        'measured_ground_speed_mps':speed,'capabilities':payload['capabilities'],
                        'capability_source':'scenario_config','state_source':'PX4 SITL GPS / BATTERY_STATUS / HEARTBEAT',
                        'battery_telemetry_age_s':round(battery_age,3),
                        'observed_timestamp':now-gps_age,'telemetry_age_s':round(gps_age,3)})
                    fleet[-1].update(armed=bool(hb.get('base_mode',0)&128),landed=landed,relative_alt_m=pos['relative_alt']/1000)
                    if not fresh:
                        continue
                    sample={'timestamp':now,'id':v.spec['id'],'east_m':east,'north_m':north,
                        'relative_alt_m':pos['relative_alt']/1000,'speed_mps':speed,
                        'lat':pos['lat']/1e7,'lon':pos['lon']/1e7,'altitude_m':pos['alt']/1000,
                        'heading_deg':pos['hdg']/100,'armed':bool(hb.get('base_mode',0)&128),'landed':landed,
                        'mission_current':state.get('MISSION_CURRENT',{}).get('seq'),
                        'last_reached_seq':state.get('MISSION_ITEM_REACHED',{}).get('seq')}
                    telemetry.write(json.dumps(sample)+'\n')
                    with self.lock:
                        self.report['observations'][v.spec['id']]=flight_observation(v.spec['id'],sample,battery)
                        route=self.report.get('allocation',{}).get('assignments',{}).get(v.spec['id'],[])
                        if route and v.spec['id'] in self.report['aircraft']:
                            self.report['aircraft'][v.spec['id']].update(armed=sample['armed'],landed=landed,
                                relative_alt_m=sample['relative_alt_m'],mode=hb.get('custom_mode'),
                                mission_current=sample['mission_current'],last_reached_seq=sample['last_reached_seq'])
                        controlled=self.report.get('controls',{}).get(v.spec['id'],{}).get('action') in ('hold','return_home')
                        if self.report['status']=='executing' and route and not controlled:
                            airspeed=state.get('VFR_HUD',{}).get('airspeed',0)
                            bad=battery is None or (sample['relative_alt_m']>30 and speed>10 and airspeed<5)
                            if bad:
                                self.sensor_bad_since.setdefault(v.spec['id'],now)
                                if now-self.sensor_bad_since[v.spec['id']]>8:
                                    self.report.update(status='sensor_error',error=v.spec['id']+' 电池反馈过期或空速与地速异常，请停止仿真检查；本轮不计通过')
                            else:
                                self.sensor_bad_since.pop(v.spec['id'],None)
                        if self.report['status']=='executing' and route and not controlled:
                            a=self.report['aircraft'][v.spec['id']]
                            a['max_relative_alt_m']=max(a['max_relative_alt_m'],sample['relative_alt_m'])
                            a['max_speed_mps']=max(a['max_speed_mps'],speed)
                            for seq,item in enumerate(a['mission']):
                                task_id=item.get('task_id')
                                if not task_id:
                                    continue
                                task=next(t for t in self.report['tasks'] if t['id']==task_id)
                                distance=math.hypot(east-task['east_m'],north-task['north_m'])
                                a['minimum_distance_m'][task_id]=min(distance,a['minimum_distance_m'].get(task_id,float('inf')))
                                passed=(sample['mission_current'] is not None and sample['mission_current']>seq) or (sample['last_reached_seq'] is not None and sample['last_reached_seq']>=seq)
                                if distance<=85 and sample['relative_alt_m']>50 and speed>10 and passed and task_id not in a['visited']:
                                    a['visited'][task_id]={'timestamp':now,'distance_m':distance,'relative_alt_m':sample['relative_alt_m'],'speed_mps':speed,'mission_seq':seq}
                    self.receive_telemetry({'id':v.spec['id'],'longitude':sample['lon'],'latitude':sample['lat'],
                        'altitude_m':sample['altitude_m'],'heading_deg':sample['heading_deg'],'speed_mps':speed,
                        'battery_pct':battery,'altitude_reference':'AMSL','flight_mode':mav_mode(hb.get('custom_mode',0)),
                        'task':('起飞后盘旋等待' if self.report.get('operation')=='takeoff' else ' / '.join(route)) if route else '等待 Qwen 任务',
                        'observed_at':self.report['observations'][v.spec['id']]['observed_at'], 'session_id':self.id,
                        'armed':sample['armed'],'landed':landed,'relative_alt_m':sample['relative_alt_m']})
                telemetry.flush()
                with self.lock:
                    self.fleet=fleet
                    if self.report['status']=='executing':
                        assigned=self.report['allocation']['assignments']
                        visited=sum(len(a['visited']) for a in self.report['aircraft'].values())
                        if visited==len(self.report['tasks']):
                            self.report.update(status='completed',success=True,completed_at=now,
                                verification_duration_s=round(now-self.report['executing_at'],2))
                        elif all(self.report.get('controls',{}).get(uid,{}).get('action') in ('hold','return_home')
                                 for uid,route in assigned.items() if len(self.report['aircraft'][uid]['visited'])<len(route)):
                            self.report.update(status='paused',paused_at=now)
                        elif now-self.report['executing_at']>600:
                            self.report.update(status='timeout',error='任务执行超过 600 秒，保留现场供检查')
                    self.report['updated_at']=now
                    snapshot=copy.deepcopy(self.report)
                write_json(self.run_dir/'flight-report.json',snapshot)
                if snapshot.get('execution_id'):
                    write_json(self.run_dir/'missions'/(snapshot['execution_id']+'.json'),snapshot)
                write_json(ROOT/'runs/interactive-latest.json',snapshot)
                self.stop_event.wait(.5)
        except Exception as exc:
            with self.lock:
                self.status='error'
                self.report.update(status='error',error=str(exc))
        finally:
            self.stop_event.set()
            for v in self.vehicles:
                v.close()
            if telemetry:
                telemetry.close()
            if launched:
                stop(self.run_dir)
            with self.lock:
                if self.status!='error':
                    self.status='stopped'
                self.report['session_stopped_at']=time.time()
                snapshot=copy.deepcopy(self.report)
            if self.run_dir.exists():
                write_json(self.run_dir/'flight-report.json',snapshot)
                write_json(ROOT/'runs/interactive-latest.json',snapshot)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=8)
        self.worker.join(timeout=1)
