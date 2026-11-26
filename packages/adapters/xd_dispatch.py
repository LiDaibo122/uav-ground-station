"""Human-confirmed inspection plan journal for the local fixed-wing XD SITL session."""
import json
import os
import re
import threading
import time
from pathlib import Path

import rospy

from packages.adapters.xd_allocation import aircraft_input, finite
from packages.adapters.xd_web import MissionError, coverage_route
from packages.ground_core.agent_tools import definition


CONFIRM = definition('xd_confirm_allocation', '确认集群仿真草案',
    '明确要求执行上一轮集群草案时调用。plan_id 原样复制 latest_cluster_plan.id；重试返回原回执。',
    {'plan_id': {'type': 'string'}}, ['plan_id'], write=True)
STATUS = definition('xd_allocation_status', '查询集群执行回执', '查询最近集群草案逐机执行情况，只读。')


def check_local_sitl(run_id, stations):
    names = ['uav%d' % i for i in range(1, len(stations)+1)]
    if (not 1 <= len(names) <= 4 or set(names) != set(stations)
            or os.environ.get('ROS_MASTER_URI') != 'http://127.0.0.1:11321'
            or not rospy.get_param('/use_sim_time', False) or rospy.get_param('/run_id') != run_id
            or rospy.get_param('/ground_bridge/bridge/vehicles') != {name: '/' + name for name in names}):
        raise MissionError('当前连接与创建草案的本机集群仿真不符')
    for i, name in enumerate(names):
        if rospy.get_param('/' + name + '/mavros/fcu_url', '') != 'udp://:%d@127.0.0.1:%d' % (14540+i, 14580+i):
            raise MissionError(name + ' 未连接本机仿真')


COVERAGE_LANE_SPACING = 210


def check_gazebo_world(stations):
    from gazebo_msgs.srv import GetWorldProperties
    try:
        rospy.wait_for_service('/gazebo/get_world_properties', timeout=2)
        world = rospy.ServiceProxy('/gazebo/get_world_properties', GetWorldProperties)()
    except (rospy.ROSException, rospy.ServiceException) as exc:
        raise MissionError('无法核对当前 Gazebo 世界，请检查本机仿真') from exc
    models = {'plane'+str(i) for i in range(len(stations))} | {'asphalt_plane'}
    if not world.success or not models.issubset(world.model_names):
        raise MissionError('未找到本机 Gazebo 仿真飞机')


def map_flight_payloads(plan):
    """Compile the saved map allocation for the local airfield exercise."""
    tasks = {t['id']: t for t in plan['tasks']}
    if plan['unassigned']:
        raise MissionError('还有未分配的任务，请调整分工')
    payloads = {}
    height = plan['height_m']
    for uid, ids in plan['assignments'].items():
        if not ids:
            continue
        selected = [tasks[key] for key in ids]
        areas = [task for task in selected if task.get('area')]
        if areas:
            if len(selected) != 1:
                raise MissionError(uid+' 的巡逻区须独立分配给该机，请将它的其他任务改派给别的飞机')
            area = areas[0]['area']
            if not all(finite(area.get(k)) for k in ('west', 'east', 'south', 'north')):
                raise MissionError('巡逻区需要有效的 world 坐标')
            short_side = min(area['east']-area['west'], area['north']-area['south'])
            min_short_side = 1.5 * COVERAGE_LANE_SPACING
            if short_side <= min_short_side:
                raise MissionError(f"{areas[0]['label']} 的短边须超过 {min_short_side:g} 米，才有两条固定翼巡检扫线")
            points = coverage_route(dict(east_min_m=area['west'], east_max_m=area['east'],
                                         north_min_m=area['south'], north_max_m=area['north'],
                                         altitude_m=height, lane_spacing_m=COVERAGE_LANE_SPACING))
        else:
            if any(not finite(task.get(k)) for task in selected for k in ('east_m', 'north_m')):
                raise MissionError('任务点需要有效的 world 坐标')
            points = [[task['east_m'], task['north_m'], height] for task in selected]
        payloads[uid] = dict(points_m=points, task_labels=[task['label'] for task in selected])
        if areas:
            payloads[uid]['coverage_entry'] = True
    return payloads


class Dispatch:
    def __init__(self, directory, stations, check_session=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stations = stations
        self.run_id = stations['uav1'].run_id
        self.lock = threading.RLock()
        self.check_session = check_session or (lambda run_id: check_local_sitl(run_id, self.stations))

    def path(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch('[0-9a-f]{32}', plan_id):
            raise MissionError('集群草案编号无效')
        return self.directory / (plan_id + '.json')

    def save(self, record):
        path = self.path(record['plan_id'])
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False))
        tmp.replace(path)

    def status(self, plan_id):
        with self.lock:
            path = self.path(plan_id)
            if not path.exists():
                return dict(plan_id=plan_id, status='preview', vehicles={}, unassigned={})
            record = json.loads(path.read_text())
            for uid, row in record['vehicles'].items():
                if row.get('phase') in ('rejected', 'idle'):
                    continue
                job = self.stations[uid].inspection_receipt(plan_id)
                if job:
                    row.update(job)
                else:
                    row.update(phase='unknown', detail='保留了确认记录，尚无可关联的本机任务记录')
            phases = {row['phase'] for row in record['vehicles'].values()} - {'idle'}
            if phases & {'queued','running'}:
                phase = 'running'
            elif 'unknown' in phases:
                phase = 'unknown'
            elif phases == {'completed'} and not record['unassigned']:
                phase = 'completed'
            elif 'completed' in phases:
                phase = 'partial'
            else:
                phase = 'failed'
            return dict(record, status=phase)

    def confirm(self, plan, plan_id):
        if plan.get('id') != plan_id or plan.get('run_id') != self.run_id:
            raise MissionError('草案版本或仿真场次已变更，请核对最新草案')
        with self.lock:
            if self.path(plan_id).exists():
                return dict(self.status(plan_id), repeated=True)
            self.check_session(self.run_id)
            library_payloads = None
            if plan.get('kind') == 'library_flight':
                check_gazebo_world(self.stations)
                library_payloads = map_flight_payloads(plan)
            tasks = {t['id']: t for t in plan['tasks']}
            rows = {uid: dict(task_ids=list(ids), phase='reserved' if ids else 'idle')
                    for uid,ids in plan['assignments'].items()}
            record = dict(plan_id=plan_id, run_id=self.run_id, confirmed_at=time.time(), vehicles=rows,
                          unassigned=plan['unassigned'], task_labels={k:v['label'] for k,v in tasks.items()},
                          height_m=plan['height_m'], frame=plan['frame'],
                          task_specs=plan['task_specs'])
            self.save(record)
            for uid, row in rows.items():
                if row['phase'] == 'idle':
                    continue
                try:
                    station = self.stations[uid]
                    with station.lock:
                        state = aircraft_input(station.snapshot(), plan['excluded_vehicles'], time.time(), plan['policy'])
                        if not state['available']:
                            raise MissionError('；'.join(state['reasons']))
                        if library_payloads is not None:
                            payload = library_payloads[uid]
                        else:
                            selected = [tasks[key] for key in row['task_ids']]
                            if any(t['pattern'] != 'point' for t in selected):
                                raise MissionError('区域任务须从地图任务库分配后下发')
                            points = [[t['east_m'], t['north_m'], plan['height_m']] for t in selected]
                            payload = {'points_m':points, 'task_labels':[t['label'] for t in selected]}
                        station.submit('inspection', payload, plan_id, return_home=True)
                    row['phase'] = 'queued'
                except MissionError as exc:
                    row.update(phase='rejected', detail=str(exc))
                except Exception as exc:
                    row.update(phase='unknown', detail=str(exc))
                self.save(record)
            return dict(self.status(plan_id), repeated=False)
