"""Dry-run actuator fixture; real algorithm implementations remain in use."""
import copy
import time


class FixtureSession:
    def __init__(self):
        self.calls=[]
        self.report={'status':'waiting_for_task','tasks':[],'aircraft':{},'origin':{'lon':103.565,'lat':31.045}}
        self.fleet=[dict(id='FW-%02d'%(i+1),east_m=0,north_m=i*350,battery_pct=90-i*10,
            available=True,speed_mps=18,capabilities=['rgb','thermal'] if i==2 else ['rgb'],
            armed=False,landed=True,relative_alt_m=0,observed_timestamp=time.time(),telemetry_age_s=0,
            state_source='dry-run fixture') for i in range(3)]

    def snapshot(self):
        fleet=copy.deepcopy(self.fleet)
        for a in fleet:a['observed_timestamp']=time.time()
        return {'status':'ready','ready':True,'fleet':fleet,'report':copy.deepcopy(self.report)}

    def submit(self,tasks,draft,**kwargs):
        from packages.swarm_sim.algorithms import solve
        self.calls.append(('execute_plan',copy.deepcopy(tasks),kwargs))
        return {'id':'fixture-execution-%d'%len(self.calls),'status':'queued','allocation':solve(self.snapshot()['fleet'],tasks,kwargs.get('algorithm','cbba'))}

    def takeoff(self,ids,**kwargs):
        self.calls.append(('takeoff',ids,kwargs))
        return {'id':'fixture-takeoff','status':'queued','aircraft_ids':ids or [a['id'] for a in self.fleet]}

    def control(self,action,ids):
        self.calls.append((action,ids))
        return {'status':'accepted','receipts':[{'id':uid,'action':action,'px4_command':'MAV_CMD_DO_SET_MODE' if action!='return_home' else 'MAV_CMD_NAV_RETURN_TO_LAUNCH','ack':{'result':0}} for uid in ids], 'skipped':[], 'execution_backend':'dry-run fixture'}


def environment(session):
    def state():
        return {'mode':'live','px4_connected':True,'aircraft':[dict(id=a['id'],longitude=103.565,latitude=31.045+a['north_m']/111195,
            altitude_m=1000,relative_alt_m=a['relative_alt_m'],armed=a['armed'],landed=a['landed'],speed_mps=0,
            battery_pct=a['battery_pct'],flight_mode='AUTO.LOITER',stale=False,source='fixture') for a in session.fleet]}
    return {'scene':{'zones':[{'id':'S','name':'S 区 · 平地仿真巡检','coordinates':[[103.5734,31.0442],[103.585,31.0442],[103.585,31.0525],[103.5734,31.0525]]}]},
            'live_state':state,'demo_state':state,'simulation_state':session.snapshot,'session':lambda:session}


def target(x=1100,y=50,priority='normal'):
    return dict(label='巡检点',frame='local',x=x,y=y,zone_id='',priority=priority,sensor='rgb')
