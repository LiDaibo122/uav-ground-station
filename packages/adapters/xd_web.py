"""Qwen tools and receipt-driven workflows for the local XD single-plane simulator."""
from collections import deque
import copy
import json
import math
import queue
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

import rospy
from std_msgs.msg import String
from xd_uav_controller.msg import ControlState
from xd_uav_task_allocate.msg import PlannerStatus
from xd_uav_task_allocate.core.coverage import (
    SearchAreaDefinition, fixedwing_entry_path, fixedwing_lawnmower_path, path_length,
)

from packages.ground_core.agent_tools import definition, validate


SYSTEM = """你是本机 XD 单架固定翼 uav1 的民用巡检助手。以本轮用户指令为准，参考会话资料 latest_plan。
- 本轮明确只规划、只查询或暂不执行时，禁止下发。仅要求空中升降高度时，用一句话说明没有独立的空中改高度功能。
- “巡检点……完成后自动返航降落”是执行任务。直接用 xd_execute 传入航点 points 和 return_home=true。
- 区域 S 形、蛇形或往返扫线巡检使用矩形 coverage（东西范围、南北范围、高度、扫线间距，均为米）。只规划用 xd_plan_coverage，明确执行用 xd_execute 传入 coverage。缺少范围、高度或间距时询问，不编造坐标和航点。
- 指令明确要求按已有巡检点高度执行上述任务时，用 xd_execute({}) 执行 latest_plan 并继承返航设置。XD 沿航线过渡高度。
- 用 xd_plan 保存巡检草案和返航选项，查询状态用 xd_state。询问当前可调用的功能和 ROS 接口时，用 xd_capabilities 核对。注册状态只说明端点存在，飞机能否执行动作还需查询 xd_state。
- 明确要求地面起飞用 xd_takeoff，参数是相对高度。该机起飞后自动盘旋等待。明确立即返航降落用 xd_land。
航点为 world 东、北、高，单位米。使用用户给出的坐标或已有草案；资料不足时询问。任务只提交一次。
工具返回排队时告知用户等待 XD 回执；进度由网页更新。用一两句中文回答。"""
NUMBER = {'type':'number'}
POINT = {'type':'object','properties':{'east':NUMBER,'north':NUMBER,'height':NUMBER},
         'required':['east','north','height'],'additionalProperties':False}
POINTS = {'type': 'array', 'minItems': 2, 'maxItems': 50, 'items': POINT}
COVERAGE_FIELDS = {
    'east_min_m': dict(NUMBER, description='矩形西侧的 world 东坐标'),
    'east_max_m': dict(NUMBER, description='矩形东侧的 world 东坐标'),
    'north_min_m': dict(NUMBER, description='矩形南侧的 world 北坐标'),
    'north_max_m': dict(NUMBER, description='矩形北侧的 world 北坐标'),
    'altitude_m': dict(NUMBER, description='巡检 world 高度'),
    'lane_spacing_m': dict(NUMBER, description='相邻扫描线间距'),
}
COVERAGE = {'type': 'object', 'properties': COVERAGE_FIELDS, 'required': list(COVERAGE_FIELDS),
            'additionalProperties': False, 'description': 'S 形覆盖矩形，world ENU，单位米'}
SITL_COVERAGE_TURN_RADIUS_M = 100.0
COVERAGE_SAMPLE_SPACING_M = 10.0
RETURN_HOME = {'type': 'boolean', 'description': '巡检完成后自动返航降落。执行已有草案时可以省略，沿用草案设置。'}
TOOLS = [definition('xd_capabilities', '查询 XD 接口', '查询当前已接入工具、ROS 端点和在线注册情况。'),
         definition('xd_state','查询 uav1 状态','查询实时坐标、速度、电量、飞行模式、航线进度和下一步。'),
         definition('xd_plan','保存巡检草案','用于仅规划或只提供航点没有执行要求时。保存巡检点和返航选项。',
                    {'points': POINTS, 'return_home': RETURN_HOME}, ['points']),
         definition('xd_plan_coverage', '规划 S 形区域巡检', '使用 XD 固定翼覆盖规划生成矩形巡检草案。只规划，不下发。',
                    {'coverage': COVERAGE, 'return_home': RETURN_HOME}, ['coverage']),
         definition('xd_takeoff','请求起飞','仅用于地面飞机明确要求起飞到指定相对高度。',
                    {'altitude_m':NUMBER},['altitude_m'],write=True),
         definition('xd_execute','执行巡检航线','执行新航线时填 points 或 coverage 二者之一，执行已保存航线时均省略。'
                    '用户说完成后自动返航时，用本工具提交航线并设 return_home=true。'
                    '从空中下降到已保存巡检点的高度去完成任务，也用本工具执行该航线。',
                    {'points': POINTS, 'coverage': COVERAGE, 'return_home': RETURN_HOME}, write=True),
         definition('xd_land','请求返航降落','立即请求 uav1 返航降落。',write=True)]
CATALOG = {d['wire']['function']['name']: d for d in TOOLS}
# Honor explicit requests to withhold dispatch even if the model proposes an action.
HOLD_DISPATCH = re.compile(r'(?:只|仅)(?:查询|规划)|(?:暂不|先不|不要|别)\s*(?:执行|下发)|不执行\s*[。！？!?.，,；;]*\s*$')
ALTITUDE_ONLY = re.compile(
    r'\s*(?:请\s*)?(?:只|仅)?(?:把|让|将)?\s*(?:uav1|无人机|飞机)?\s*'
    r'(?:下降|降低|爬升|升高|调整高度)\s*(?:到|至|为)\s*\d+(?:\.\d+)?\s*'
    r'(?:米|m)(?:\s*(?:高|高度))?\s*[。！？!?.]*\s*', re.IGNORECASE,
)


class MissionError(ValueError):
    retryable = False
    code = 'xd_request_denied'


def coverage_route(spec):
    east_min, east_max = spec['east_min_m'], spec['east_max_m']
    north_min, north_max = spec['north_min_m'], spec['north_max_m']
    altitude, spacing = spec['altitude_m'], spec['lane_spacing_m']
    width, depth = east_max - east_min, north_max - north_min
    if width <= 0 or depth <= 0 or altitude <= 0 or spacing <= 0:
        raise MissionError('区域上下界必须有正跨度，高度和扫线间距须大于零')
    if max(map(abs, (east_min, east_max, north_min, north_max))) > 100000 or altitude > 10000:
        raise MissionError('区域坐标或高度超出 XD 仿真规划范围')
    cross_span = min(width, depth)
    # Each reversal crosses roughly half the region and is sampled every 10 m.
    if (cross_span / spacing) * (cross_span / 2 + SITL_COVERAGE_TURN_RADIUS_M) / COVERAGE_SAMPLE_SPACING_M > 4000:
        raise MissionError('区域扫线过密，预计超出桥接消息上限，请增大间距或缩小区域')
    area = SearchAreaDefinition(
        area_id=1,
        boundary=((east_min, north_min), (east_max, north_min), (east_max, north_max), (east_min, north_max)),
        altitude=altitude,
        lane_spacing=spacing,
    )
    route = fixedwing_lawnmower_path(area, minimum_turn_radius=SITL_COVERAGE_TURN_RADIUS_M,
                                    turn_waypoint_spacing=COVERAGE_SAMPLE_SPACING_M,
                                    straight_lead_distance=SITL_COVERAGE_TURN_RADIUS_M)
    return pack_coverage_route(route)


def pack_coverage_route(route):
    points = []
    for pose in route:
        point = [round(value, 3) for value in pose]
        if not points or math.dist(points[-1], point) >= 0.05:
            points.append(point)
    # Leave room for the envelope and correlation fields under the bridge's 64 KiB limit.
    if len(json.dumps(points)) > 60 * 1024:
        raise MissionError('生成的航线超过桥接消息上限，请增大扫线间距或缩小区域')
    if len(points) < 2:
        raise MissionError('生成的航线没有足够的有效航点')
    return points


def coverage_entry_route(state, points):
    position = state.get('world_position_m') or {}
    origin = tuple(position.get(axis) for axis in 'xyz')
    heading = state.get('world_heading_rad')
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (*origin, heading)):
        raise MissionError('缺少有效 world 位置或航向，无法生成固定翼入场航线')
    return pack_coverage_route(fixedwing_entry_path(
        origin, heading, points, minimum_turn_radius=SITL_COVERAGE_TURN_RADIUS_M,
        turn_waypoint_spacing=COVERAGE_SAMPLE_SPACING_M,
    ))


class TelemetryStation:
    """Read-only view of one vehicle. Bridge topics are relative to the selected namespace."""
    owns_job_records = False
    bridge_modes = ('read_only', 'xd_single', 'xd_dual', 'xd_fleet')

    def __init__(self, vehicle, namespace, bridge):
        self.vehicle = vehicle
        self.lock = threading.RLock()
        self.run_id = rospy.get_param('/run_id')
        self.history = []
        self.packet = {}
        self.control = None
        self.control_time = self.received = 0
        self.track = deque(maxlen=1200)
        self.route_progress = None
        self.point_observation = None
        control_topic = rospy.get_param(bridge + '/bridge/xd_interfaces/control_state/name')
        self.subs = [rospy.Subscriber(bridge + '/telemetry', String, self.telemetry, queue_size=1),
                     rospy.Subscriber(namespace + '/' + control_topic, ControlState, self.control_state, queue_size=1)]

    def telemetry(self,msg):
        pkt = json.loads(msg.data)
        with self.lock:
            self.packet = pkt
            self.received = time.monotonic()
            vehicle = next((v for v in pkt.get('vehicles', []) if v['vehicle_id'] == self.vehicle), {})
            sources = vehicle.get('sources', {})
            odom = sources.get('world_odom', {})
            sample = odom.get('data') or {}
            state = sources.get('state', {})
            pos = sample.get('position_m') or {}
            if (state.get('stale', True) or not (state.get('data') or {}).get('connected')
                    or odom.get('stale', True) or not sample.get('frame_matches_expected')
                    or not all(isinstance(pos.get(k), (float, int)) and math.isfinite(pos[k]) for k in ('x','y','z'))):
                return
            stamp = odom.get('received_at')
            if not isinstance(stamp, (float, int)):
                return
            previous = self.track[-1] if self.track else None
            if previous and stamp - previous['observed_at'] < .9:
                return
            point = {**pos, 'observed_at': stamp, 'break_before': not previous or stamp - previous['observed_at'] > 4}
            self.track.append(point)
            self.observe_inspection_points(point)

    def trail(self, since):
        with self.lock:
            return [point for point in self.track if point['observed_at'] > since]

    def observe_inspection_points(self, point):
        job = self.history[-1] if self.history else None
        if not job or job['phase'] != 'running' or not job.get('assigned_points'):
            return
        last = job['events'][-1] if job['events'] else {}
        if last.get('action') != 'task_path' or last.get('phase') not in ('accepted', 'running'):
            return
        if self.point_observation is None or self.point_observation['job_id'] != job['id']:
            self.point_observation = {'job_id': job['id'], 'passed': 0}
        index = self.point_observation['passed']
        if index < len(job['assigned_points']):
            x, y, z = job['assigned_points'][index]
            if math.hypot(point['x']-x, point['y']-y) <= 25 and abs(point['z']-z) <= 15:
                self.point_observation['passed'] += 1

    def mission_snapshot(self, job, now):
        if not job:
            return None
        events = job.get('events', [])
        last = events[-1] if events else {}
        reached = any(e['action'] == 'task_path' and e['phase'] == 'completed' for e in events)
        points = job.get('assigned_points', [])
        labels = job.get('task_labels', [])
        passed = None
        if reached:
            passed = len(points) if points else None
        elif self.point_observation and self.point_observation['job_id'] == job['id']:
            passed = self.point_observation['passed']
        progress = self.route_progress
        percent = 100.0 if reached else None
        if (percent is None and progress and last.get('action') == 'task_path'
                and progress['goal_id'] == last.get('goal_id') and now - progress['received'] <= 4):
            percent = progress['percent']
        if job['phase'] in ('failed', 'unknown', 'rejected', 'cancelled'):
            stage, following = '需要核对', None
        elif job['phase'] == 'completed':
            stage, following = '任务完成', None
        elif last.get('action') == 'land_home':
            stage, following = '返航降落', '落地并上锁'
        elif reached:
            stage, following = '航线已完成', '返航降落' if job['return_home'] else None
        elif last.get('action') == 'task_path' and last.get('phase') != 'sent':
            stage = '巡检中'
            following = '继续巡检，完成航线后返航降落' if job['return_home'] else '继续巡检'
        elif last.get('action') == 'takeoff' and last.get('phase') != 'completed':
            stage, following = '起飞中', '开始巡检航线' if job['action'] == 'inspection' else '进入盘旋'
        else:
            stage, following = '等待航线确认' if last else '等待执行', '开始巡检航线' if points else None
        next_point = None
        if stage == '巡检中' and points:
            if (passed or 0) < len(points):
                index = passed or 0
                next_point = dict(order=index+1, label=labels[index] if index < len(labels) else None,
                                  world_m=points[index])
            else:
                following = '等待航线完成确认'
        return dict(stage=stage, next_action=following, route_progress_pct=percent,
                    planned_points=len(points), passed_points=passed, next_point_estimate=next_point,
                    point_basis='规划顺序与遥测近点估计（水平 25 米、垂直 15 米）' if points else None,
                    detail=job['detail'])

    def control_state(self,msg):
        with self.lock:
            self.control = msg
            self.control_time = time.monotonic()

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            vehicle = next((v for v in self.packet.get('vehicles',[]) if v['vehicle_id']==self.vehicle),{})
            sources = vehicle.get('sources',{})
            def sample(key):
                row = sources.get(key,{})
                if (now-self.received>3 or row.get('stale',True) or
                        now-self.received+(row.get('age_sec') or 0)>3):
                    return {}
                return row.get('data') or {}
            state, flight, odom, battery = map(sample,('state','extended_state','world_odom','battery'))
            c = self.control
            valid = bool(c and now-self.control_time<1 and c.state_valid and c.localization_valid and c.odometry_fresh)
            connected = bool(state.get('connected')) and self.packet.get('bridge_mode') in self.bridge_modes
            phase = flight.get('flight_phase','unknown')
            pos = odom.get('position_m') if odom.get('frame_matches_expected') else None
            ready = bool(connected and state.get('armed') is False and phase=='on_ground' and valid and pos)
            job = self.history[-1] if self.history else None
            speed = c.groundspeed if valid and math.isfinite(c.groundspeed) else None
            climb = c.climb_rate if valid and math.isfinite(c.climb_rate) else None
            return dict(vehicle=self.vehicle,connected=connected,armed=state.get('armed'),flight_phase=phase,
                        mode=state.get('mode'),world_position_m=pos,localization_valid=valid,can_takeoff=ready,
                        workload_known=self.owns_job_records or (phase=='on_ground' and state.get('armed') is False),
                        world_heading_rad=odom.get('heading_rad') if pos else None,
                        battery_pct=round(battery['percentage']*100,1) if battery.get('percentage') is not None else None,
                        battery_voltage_v=battery.get('voltage_v'), speed_xy_mps=speed, climb_mps=climb,
                        position_observed_at=sources.get('world_odom',{}).get('received_at') if pos else None,
                        observed_at=sources.get('state',{}).get('received_at'),
                        mission=self.mission_snapshot(job, now), job=copy.deepcopy(job))

class Station(TelemetryStation):
    owns_job_records = True
    bridge_modes = ('xd_single', 'xd_dual', 'xd_fleet')

    def __init__(self, directory, vehicle="uav1"):
        vehicles = rospy.get_param('/ground_bridge/bridge/vehicles')
        if not rospy.get_param('/use_sim_time',False) or vehicles.get(vehicle) != '/' + vehicle:
            raise RuntimeError('需要 XD 仿真时钟和已配置的桥接机号 ' + vehicle)
        super().__init__(vehicle, vehicles[vehicle], '/ground_bridge')
        job_dir = Path(directory) if len(vehicles) == 1 else Path(directory) / vehicle
        self.file = job_dir / (self.run_id + '.json')
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.history = json.loads(self.file.read_text()) if self.file.exists() else []
        if self.history and self.history[-1]['phase'] not in ('completed','failed','unknown'):
            self.history[-1].update(phase='unknown', detail='网页后端重启，请核对底座状态')
            self.persist()
        self.active = False
        self.inbox = {}
        self.pub = rospy.Publisher('/ground_bridge/command',String,queue_size=5)
        self.subs.append(rospy.Subscriber('/ground_bridge/result', String, self.receipt, queue_size=30))
        planner = rospy.get_param('/ground_bridge/bridge/xd_interfaces/planner_status/name')
        self.subs.append(rospy.Subscriber(vehicles[vehicle] + '/' + planner, PlannerStatus,
                                          self.planner_progress, queue_size=1))

    def planner_progress(self, msg):
        progress = re.match(r'controller path ([0-9]+(?:\.[0-9]+)?)%', msg.detail)
        if not progress or msg.state not in (PlannerStatus.ACTIVE, PlannerStatus.REACHED):
            return
        with self.lock:
            self.route_progress = dict(goal_id=msg.goal_id, percent=min(100, float(progress[1])),
                                       received=time.monotonic())

    def persist(self):
        tmp = self.file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.history,ensure_ascii=False,indent=2))
        tmp.replace(self.file)

    def receipt(self,msg):
        rec = json.loads(msg.data)
        with self.lock:
            mailbox = self.inbox.get(rec.get('command_id'))
            if mailbox and rec.get('vehicle_id')==self.vehicle and rec.get('run_id',self.run_id)==self.run_id:
                mailbox.put(rec)

    def submit(self, action, payload, plan_id=None, return_home=False):
        with self.lock:
            status = self.snapshot()
            if self.active:
                raise MissionError('已有指令执行中，请查看当前回执')
            if plan_id and any(j.get('plan_id')==plan_id for j in self.history):
                raise MissionError('此方案已提交，请查看原任务记录')
            if action=='takeoff' and not status['can_takeoff']:
                if status['flight_phase'] == 'in_air':
                    raise MissionError(self.vehicle + ' 已在空中，起飞指令仅适用于地面飞机')
                raise MissionError(self.vehicle + ' 尚不满足地面起飞条件')
            needs_takeoff = action == 'inspection' and status['can_takeoff']
            if action!='takeoff' and not needs_takeoff and not (status['connected'] and status['armed'] and status['flight_phase']=='in_air'):
                raise MissionError('需要 ' + self.vehicle + ' 在空中，先完成起飞再提交')
            if action in ('task_path', 'inspection') and not needs_takeoff and not (status['localization_valid'] and status['mode']=='OFFBOARD'):
                raise MissionError('航线执行需要有效定位和 OFFBOARD')
            job = dict(id=uuid4().hex,vehicle=self.vehicle,action=action,phase='queued',plan_id=plan_id,
                       return_home=return_home,events=[],detail='已排队，等待 XD 回执')
            if action == 'inspection':
                assigned_points = [] if payload.get('coverage_entry') else copy.deepcopy(payload['points_m'])
                job.update(needs_takeoff=needs_takeoff, assigned_points=assigned_points,
                           task_labels=list(payload.get('task_labels', [])))
            self.history.append(job)
            self.persist()
            self.active = True
            threading.Thread(target=self.work,args=(job,payload),daemon=True).start()
            return dict(execution_id=job['id'],status='queued',return_home=return_home,vehicle=self.vehicle)

    def leg(self,job,action,payload):
        cid = job['id']+'-'+action
        if action == 'cancel':
            cid += '-' + str(payload['goal_id'])
        q = queue.Queue()
        cmd = dict(schema_version=1,mission_id=job['id'],command_id=cid,vehicle_id=self.vehicle,action=action,payload=payload)
        if action=='task_path':
            cmd['frame_id']='world'
        with self.lock:
            self.inbox[cid]=q
        try:
            if rospy.get_param('/run_id')!=self.run_id or not self.pub.get_num_connections():
                raise RuntimeError('XD 桥接会话已断开')
            now=time.time()
            self.pub.publish(String(data=json.dumps(dict(cmd,issued_at=now,expires_at=now+10),allow_nan=False)))
            deadline=time.monotonic()+320
            while time.monotonic()<deadline and not rospy.is_shutdown():
                try:
                    r=q.get(timeout=1)
                except queue.Empty:
                    continue
                if r.get('mission_id')!=job['id'] or r.get('action')!=action:
                    continue
                with self.lock:
                    job['events'].append(dict(action=action,phase=r['phase'],goal_id=r.get('goal_id'),
                                              message=r['message'],time=time.time(),command_id=cid))
                    job.update(phase='running',detail=r['message'])
                    self.persist()
                if r['phase']=='completed':
                    return r
                if r['phase'] in ('rejected','failed','cancelled'):
                    raise MissionError(r['message'])
                if r['phase']=='unknown':
                    raise RuntimeError(r['message'])
            raise RuntimeError('等待 XD 回执超时，执行结果待核实')
        finally:
            with self.lock:
                self.inbox.pop(cid,None)

    def retained_goal(self):
        goal=None
        for job in self.history:
            for event in job['events']:
                if event['action']=='task_path' and event.get('goal_id'):
                    goal=event['goal_id']
                elif event['action']=='cancel' and event['phase']=='completed':
                    goal=None
        return goal

    def inspection_receipt(self, plan_id):
        with self.lock:
            for job in reversed(self.history):
                if job['action']=='inspection' and job.get('plan_id')==plan_id:
                    return dict(execution_id=job['id'], phase=job['phase'], detail=job['detail'],
                                events=copy.deepcopy(job['events']))
        return None

    def work(self,job,payload):
        try:
            goal=self.retained_goal()
            action = job['action']
            if goal and action in ('task_path','land_home','inspection'):
                self.leg(job,'cancel',{'goal_id':goal})
            if action == 'inspection':
                points = payload['points_m']
                if job['needs_takeoff']:
                    self.leg(job, 'takeoff', {'altitude_m':points[0][2]})
                state = self.snapshot()
                pos = state['world_position_m']
                if not (state['connected'] and state['localization_valid'] and state['armed'] and
                        state['flight_phase']=='in_air' and state['mode']=='OFFBOARD' and pos):
                    raise MissionError('巡检入场前位置或飞行状态发生变化')
                # XD expects a path with at least two distinct vertices, including for a single inspection point.
                if payload.get('coverage_entry'):
                    points = coverage_entry_route(state, points)
                else:
                    points = pack_coverage_route([[pos['x'], pos['y'], pos['z']], *points])
                payload = {'points_m':points}
                action = 'task_path'
            result=self.leg(job,action,payload)
            if action=='task_path' and job['return_home']:
                if type(result.get('goal_id')) is not int or result['goal_id'] <= 0:
                    raise RuntimeError('缺少对应航线的 goal_id，请核对 XD 回执')
                self.leg(job,'cancel',{'goal_id':result['goal_id']})
                self.leg(job,'land_home',{})
            phase,detail='completed',('巡检完成，已返航落地并上锁' if job['return_home'] else job['events'][-1]['message'])
        except Exception as exc:
            phase,detail=('failed' if isinstance(exc,MissionError) else 'unknown'),str(exc)
        finally:
            with self.lock:
                job.update(phase=phase,detail=detail)
                self.active=False
                self.persist()


class Runtime:
    def __init__(self, env, conversation, mode, text):
        self.station=env['station']
        self.interfaces = env.get('interfaces')
        self.vehicle = env.get('vehicle', 'uav1')
        self.conv=conversation
        self.altitude_only = bool(ALTITUDE_ONLY.fullmatch(text))
        self.read_only = mode != 'live' or self.altitude_only or bool(HOLD_DISPATCH.search(text))
        self.execution=self.draft=None

    def context(self):
        state = self.station.snapshot()
        plan = self.conv.get('xd_plan')
        return {
            'vehicle': self.vehicle,
            'flight_phase': state['flight_phase'],
            'latest_plan': self.plan_summary(plan) if plan else None,
            'frame': 'world ENU 米',
        }

    def definitions(self):
        state = self.station.snapshot()
        airborne = state['flight_phase'] in ('in_air', 'taking_off', 'landing')
        return [d['wire'] for name,d in CATALOG.items()
                if (not self.read_only or not d['write']) and (name != 'xd_takeoff' or not airborne)]

    @staticmethod
    def plan_summary(plan):
        if 'coverage' not in plan:
            return plan
        return {key: value for key, value in plan.items() if key != 'points_m'}

    def save_plan(self, args):
        coverage = args.get('coverage')
        if coverage is not None:
            points = coverage_route(coverage)
        else:
            points = [[p[k] for k in ('east', 'north', 'height')] for p in args['points']]
        if any(math.dist(a, b) < 0.05 for a, b in zip(points, points[1:])):
            raise MissionError('相邻航点距离小于 0.05 米')
        prior = self.conv.get('xd_plan')
        same_route = prior and prior['run_id'] == self.station.run_id and prior['points_m'] == points
        draft = dict(prior) if same_route else dict(id=uuid4().hex, points_m=points, frame='world',
                                                    vehicle=self.vehicle, run_id=self.station.run_id)
        if coverage is not None:
            draft.update(coverage=coverage, point_count=len(points), route_length_m=round(path_length(points)),
                         turn_radius_m=SITL_COVERAGE_TURN_RADIUS_M)
        draft['return_home'] = args.get('return_home', draft.get('return_home', False))
        self.conv['xd_plan'] = draft
        self.draft = self.plan_summary(draft)
        return draft

    def call(self,name,args):
        if name not in CATALOG:
            raise MissionError('该工具当前不可用')
        if self.read_only and CATALOG[name]['write']:
            if self.altitude_only:
                raise MissionError('当前没有独立的空中改高度功能')
            raise MissionError('本轮仅允许查询或规划，未下发飞行指令')
        validate(args,CATALOG[name]['wire']['function']['parameters'])
        if name == 'xd_capabilities':
            from packages.adapters.xd_interfaces import describe_interfaces
            return describe_interfaces(self.interfaces)
        if name=='xd_state':
            s=self.station.snapshot()
            if s['job']:
                s['job']={k:s['job'][k] for k in ('action','phase','detail')}
            return s
        if name=='xd_plan':
            return self.save_plan(args)
        if name == 'xd_plan_coverage':
            return self.plan_summary(self.save_plan(args))
        if name=='xd_takeoff':
            if args['altitude_m']<=0:
                raise MissionError('起飞高度应大于零')
            self.execution=self.station.submit('takeoff',args)
        elif name=='xd_execute':
            if 'points' in args and 'coverage' in args:
                raise MissionError('请仅提供区域或航点其中一种')
            plan = self.save_plan(args) if {'points', 'coverage'} & args.keys() else self.conv.get('xd_plan')
            if not plan:
                raise MissionError('当前对话没有巡检草案，请提供航点')
            if plan['run_id']!=self.station.run_id:
                raise MissionError('仿真已经换场，请重新规划')
            return_home = args.get('return_home', plan.get('return_home', False))
            points = plan['points_m']
            if 'coverage' in plan:
                state = self.station.snapshot()
                if state['flight_phase'] != 'in_air':
                    raise MissionError('S 形航线需要飞机在空中，先完成起飞再提交')
                points = coverage_entry_route(state, points)
            self.execution = self.station.submit('task_path', {'points_m': points}, plan['id'], return_home)
            if 'coverage' in plan:
                self.execution['coverage'] = dict(self.plan_summary(plan), return_home=return_home)
        else:
            self.execution=self.station.submit('land_home',{})
        return self.execution
