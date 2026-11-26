"""Fixed riverbank survey acceptance fixture for the local two-plane Gazebo world."""
import copy
import json
import threading
import time

import rospy
from gazebo_msgs.srv import GetWorldProperties

from packages.adapters.xd_dispatch import check_local_sitl
from packages.adapters.xd_web import MissionError

# Fixed stations along the riverbank of this test fixture, in Gazebo world metres.
SAMPLES = [('河堤西段', 650), ('河堤中段', 1020), ('河堤东段', 1400)]
LEAD = 'uav2'
SPARE = 'uav1'


def interpret_trial(data,state,model,ask):
    message=data.get('message')
    primary = data.get('primary',LEAD) if state.get('phase')=='ready' else state.get('primary',LEAD)
    if primary not in (LEAD,SPARE):
        raise MissionError('未知巡检机')
    spare = SPARE if primary==LEAD else LEAD
    if data.get('run_id')!=state['run_id'] or not isinstance(message,str) or not 0<len(message.strip())<=1500:
        raise MissionError('请核对本场双机验收指令')
    schema=dict(type='object',properties={'action':dict(type='string',enum=['status','start','recall'])},
                required=['action'])
    result=ask(dict(model=model,stream=False,think=False,format=schema,options=dict(temperature=0,num_predict=100),
                    messages=[dict(role='system',content='你是固定双机河堤仿真验收助手。明确命令开始场景用start。'
                                   f'明确要求主机{primary}中途返航或把它未完成项交给备用机{spare}时选recall。'
                                   '提问、否定、假设、其他机号或其他操作选status。'),
                              dict(role='user',content=message)]))
    action=json.loads(result['message']['content'])['action']
    if action not in ('status','start','recall') or result.get('done_reason')=='length':
        raise MissionError('模型未能识别本次验收操作')
    return dict(proposal=action,run_id=state['run_id'])


class RiverTrial:
    def __init__(self, stations, directory):
        self.stations = stations
        self.run_id = stations[LEAD].run_id
        self.path = directory/'riverbank-trial.json'
        self.lock = threading.RLock()
        self.state = dict(run_id=self.run_id, phase='ready', primary=LEAD, recall=False, events=[], samples=[
            dict(name=name, owner=LEAD, receipt=None) for name, _ in SAMPLES])
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            self.state.setdefault('primary',LEAD)
            if self.state['phase'] not in ('complete','stopped','unknown'):
                self.record('unknown','工作台重启，需核对最后的仿真回执')

    def read(self):
        with self.lock:
            snapshot=copy.deepcopy(self.state)
            for sample,(_,east) in zip(snapshot['samples'],SAMPLES):
                sample['position']={'x':east,'y':120}
            return snapshot

    def record(self, phase, text):
        with self.lock:
            self.state['phase'] = phase
            self.state['events'].append(dict(at=time.time(),text=text))
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2))
            tmp.replace(self.path)

    @property
    def primary(self):
        return self.state['primary']

    @property
    def spare(self):
        return SPARE if self.primary==LEAD else LEAD

    def command(self, op, run_id, primary=None):
        with self.lock:
            if run_id != self.run_id:
                raise MissionError('仿真场次已变更')
            check_local_sitl(self.run_id,self.stations)
            phase = self.state['phase']
            primary=primary or self.primary
            if primary not in (LEAD,SPARE) or (phase!='ready' and primary!=self.primary):
                raise MissionError('巡检机与本次验收不符')
            if op=='start' and phase=='ready':
                try:
                    rospy.wait_for_service('/gazebo/get_world_properties',timeout=2)
                    world=rospy.ServiceProxy('/gazebo/get_world_properties',GetWorldProperties)()
                except rospy.ROSException as exc:
                    raise MissionError('无法核对双机 Gazebo 世界') from exc
                except rospy.ServiceException as exc:
                    raise MissionError('Gazebo 世界查询失败') from exc
                if not world.success or not {'plane0','plane1','asphalt_plane'}.issubset(world.model_names):
                    raise MissionError('双机河堤验收需要本机固定翼 Gazebo 场景')
                if set(self.stations)!={LEAD,SPARE} or any(not s.snapshot()['can_takeoff'] or s.active or s.history
                                                           for s in self.stations.values()):
                    raise MissionError('本场景需要新启动的两架空闲、定位有效且已在地面上锁的仿真飞机')
                self.state['primary']=primary
                for sample in self.state['samples']:
                    sample['owner']=primary
                self.record('survey',f'开始河堤验收，{self.primary} 先巡检，{self.spare} 待命')
                threading.Thread(target=self.run,daemon=True).start()
            elif op=='recall' and phase=='survey' and not self.state['recall']:
                self.state['recall']=True
                self.record(phase,f'收到 {self.primary} 返航请求，当前 XD 航段结束后释放原航线')
            elif op not in ('start','recall'):
                raise MissionError('未知验收操作')
            elif op=='recall' and not self.state['recall']:
                raise MissionError('此时没有可交接的 uav2 巡检任务')
            return self.read()

    def fly(self, uid, action, index=None):
        station=self.stations[uid]
        # Only the fixed fixture geometry is accepted. No endpoint supplies a route.
        payload={}
        if index is not None:
            name,x=SAMPLES[index]
            payload=dict(points_m=[[x,120,40]],task_labels=[name])
        check_local_sitl(self.run_id,self.stations)
        with self.lock, station.lock:
            if index is not None and uid==self.primary and self.state['recall']:
                return None
            if station.history and not str(station.history[-1].get('plan_id')).startswith('river-trial-'):
                raise MissionError(uid+' 已有其他任务')
            job=station.submit(action,payload,'river-trial-'+uid+'-'+str(index))
        deadline=time.monotonic()+720
        while time.monotonic()<deadline:
            snapshot=station.snapshot()
            latest=snapshot['job']
            if not latest or latest['id']!=job['execution_id']:
                raise MissionError('执行编号变化，请核对 '+uid)
            if latest['phase']=='completed':
                expected='land_home' if action=='land_home' else 'task_path'
                if not any(e['action']==expected and e['phase']=='completed' for e in latest['events']):
                    raise MissionError('缺少 '+expected+' 完成回执')
                if action=='land_home' and snapshot['armed'] is False and snapshot['flight_phase']=='on_ground' and not snapshot['connected']:
                    raise MissionError('落地时遥测已断开，请核对')
                if action!='land_home' or (snapshot['connected'] and snapshot['flight_phase']=='on_ground'
                                            and snapshot['armed'] is False):
                    return job['execution_id']
            elif latest['phase'] in ('failed','unknown','rejected','cancelled'):
                raise MissionError(latest['detail'])
            time.sleep(.25)
        raise MissionError(uid+' 等待回执超时')

    def survey(self, uid):
        for index,sample in enumerate(self.state['samples']):
            with self.lock:
                if sample['receipt']:
                    continue
                if uid==self.primary and self.state['recall']:
                    break
                self.record('survey' if uid==self.primary else 'relief',uid+' 前往'+sample['name'])
            job_id=self.fly(uid,'inspection',index)
            with self.lock:
                if uid==self.primary and self.state['recall']:
                    break
                # The fixture issues one synthetic collection receipt per completed leg.
                sample['receipt']=dict(vehicle=uid,execution_id=job_id,kind='simulated_sample')
                self.record(self.state['phase'],uid+' 完成模拟采样：'+sample['name'])
        status=self.stations[uid].snapshot()
        if status['flight_phase']=='on_ground' and status['armed'] is False and status['connected']:
            if self.stations[uid].history:
                raise MissionError('出现计划外落地，请先核对任务')
        else:
            self.record('returning',uid+' 正在返航降落')
            receipt=self.fly(uid,'land_home')
            with self.lock:
                self.state.setdefault('landing_receipts',{})[uid]=receipt
        self.record('landed',uid+' 已确认地面上锁')

    def run(self):
        try:
            self.survey(self.primary)
            with self.lock:
                remaining=[s for s in self.state['samples'] if not s['receipt']]
                if remaining:
                    spare=self.stations[self.spare]
                    snapshot=spare.snapshot()
                    if not self.state['recall'] or spare.history or not snapshot['can_takeoff'] or (snapshot.get('battery_pct') or 0)<25:
                        raise MissionError('备用机不可用，未完成项保留在原记录中')
                    for sample in remaining:
                        sample['owner']=self.spare
                    self.record('relief',self.spare+' 接替：'+'、'.join(s['name'] for s in remaining))
            if remaining:
                self.survey(self.spare)
            self.record('complete','全部模拟采样已完成，参与飞行的飞机已返航上锁')
        except Exception as exc:
            self.record('stopped','交接停止，保留已有采样回执：'+str(exc))
