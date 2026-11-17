"""CBBA adapter: upstream consensus/bundle code, civilian fixed-wing scoring."""
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'third_party/cbba_python/lib'))
from CBBA import CBBA
from Agent import Agent
from Task import Task
from WorldInfo import WorldInfo


class FixedWingCBBA(CBBA):
    # The original constructor hard-codes quad/car compatibility. Keep the
    # upstream auction unchanged and replace only domain labels and bid scoring.
    def __init__(self, compatibility=None):
        self.agent_types = ['fixedwing']
        self.task_types = ['inspection']
        self.time_interval_list = [0, 3600]
        self.duration_flag = False
        self.compatibility_mat = compatibility or [[1]]
        self.rounds = 0

    def communicate(self, time_mat, iter_idx):
        self.rounds = iter_idx
        if iter_idx > 500:
            raise RuntimeError('CBBA exceeded 500 synchronous rounds')
        return super().communicate(time_mat, iter_idx)

    def scoring_compute_score(self, idx_agent, task_current, task_prev,
                              time_prev, task_next, time_next):
        agent = self.AgentList[idx_agent]
        prev = task_prev if task_prev else agent
        dt = math.sqrt((prev.x-task_current.x)**2 + (prev.y-task_current.y)**2) / agent.nom_velocity
        earliest = max(task_current.start_time,
                       (time_prev + task_prev.duration if task_prev else agent.availability) + dt)
        latest = task_current.end_time
        if task_next:
            transit = math.hypot(task_next.x-task_current.x, task_next.y-task_current.y)/agent.nom_velocity
            latest = min(latest, time_next-task_current.duration-transit)
        # Priority multiplies task value; decay penalizes later arrival. This
        # first adapter estimates straight-line travel, not a flyable Dubins path.
        return (task_current.task_value*math.exp(-task_current.discount*(earliest-task_current.start_time))*getattr(agent,'bid_factor',1),
                earliest, latest)


def allocate(aircraft, tasks, capacity=2):
    if capacity < 1:
        raise ValueError('capacity must be positive')
    # Legacy algorithm-only fixtures omit battery; runtime fleet snapshots always
    # supply the actual value, including None when it is unknown.
    active=[a for a in aircraft if a.get('available',True) and
            ('battery_pct' not in a or (a['battery_pct'] is not None and a['battery_pct']>=25))]
    if len({a['id'] for a in aircraft}) != len(aircraft) or len({t['id'] for t in tasks}) != len(tasks):
        raise ValueError('duplicate aircraft/task IDs')
    if not active or not tasks:
        return {'algorithm': 'CBBA', 'assignments': {}, 'unassigned': [t['id'] for t in tasks], 'rounds': 0}
    agents = [Agent(agent_id=i, agent_type=i, x=a['east_m'], y=a['north_m'], nom_velocity=a.get('speed_mps', 20))
              for i, a in enumerate(active)]
    for agent,a in zip(agents,active):
        agent.bid_factor=0.5+0.5*a.get('battery_pct',100)/100
    if any(a.nom_velocity <= 0 for a in agents):
        raise ValueError('nominal speed must be positive')
    source_tasks = [Task(task_id=i, task_type=i,
                         task_value={'low': 500, 'normal': 1000, 'high': 2000}[t.get('priority', 'normal')],
                         start_time=0, end_time=t.get('deadline_s', 3600), duration=0,
                         discount=0.003, x=t['east_m'], y=t['north_m'])
                    for i, t in enumerate(tasks)]
    compatibility=[[int(set(t.get('required_capabilities',[])).issubset(a.get('capabilities',['rgb']))
                        and (not t.get('allowed_aircraft_ids') or a['id'] in t['allowed_aircraft_ids']))
                    for t in tasks] for a in active]
    solver = FixedWingCBBA(compatibility)
    solver.agent_types=[a['id'] for a in active]
    solver.task_types=[t['id'] for t in tasks]
    started = time.perf_counter()
    paths, times = solver.solve(agents, source_tasks, WorldInfo([-3000, 3000], [-3000, 3000], [0, 500]),
                                capacity, True)
    assignments = {a['id']: [tasks[j]['id'] for j in path] for a, path in zip(active, paths)}
    flat = [t for route in assignments.values() for t in route]
    if len(flat) != len(set(flat)):
        raise RuntimeError('CBBA returned duplicate ownership')
    if any(row != solver.winners_list[0] for row in solver.winners_list):
        raise RuntimeError('CBBA winner views did not converge')
    return {'algorithm': 'CBBA', 'transport': 'in-process synchronous fully connected consensus',
            'assignments': assignments, 'unassigned': [t['id'] for t in tasks if t['id'] not in flat],
            'arrival_estimates_s': {a['id']: route for a, route in zip(active, times)},
            'rounds': solver.rounds, 'elapsed_ms': (time.perf_counter()-started)*1000,
            'winner_views': solver.winners_list, 'capacity': capacity,
            'input_fleet':aircraft,
            'bid_factors':{a['id']:agent.bid_factor for a,agent in zip(active,agents)}}


def inspection_tasks():
    return [{'id': 'SURVEY-%02d' % (lane*2+leg+1),
             'label': '灾害巡检点 %d' % (lane*2+leg+1),
             'east_m': east, 'north_m': lane*350, 'priority': 'normal'}
            for lane in range(3) for leg, east in enumerate([900, 1600])]
