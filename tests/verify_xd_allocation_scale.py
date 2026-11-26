"""Repeatable civilian inspection planning check with synthetic ENU telemetry."""
import argparse
import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.adapters.xd_allocation import POLICY, plan_inspections


def evaluate(count, task_count):
    now = time.time()
    fleet = [dict(vehicle='uav%d' % (i+1), connected=True, armed=False, flight_phase='on_ground',
                  can_takeoff=True, localization_valid=True, battery_pct=90, observed_at=now,
                  world_position_m={'x': 0, 'y': 20*i}, job=None) for i in range(count)]
    tasks = [dict(label='堤段检查点%d' % (i+1), east=300+150*(i//count), north=20*(i%count))
             for i in range(task_count)]
    reports = {}
    for name in ('all_available', 'degraded'):
        states = copy.deepcopy(fleet)
        if name == 'degraded':
            states[0]['battery_pct'] = 12
            states[1]['observed_at'] -= POLICY['state_ttl_s'] + 1
        start = time.monotonic()
        plan = plan_inspections(dict(tasks=tasks), None, states, {}, '', now=now)
        routes = plan['assignments']
        assigned = [t for route in routes.values() for t in route]
        pending = set(plan['unassigned'])
        assert len(assigned) == len(set(assigned)) and not pending.intersection(assigned)
        assert set(assigned) | pending == {t['id'] for t in plan['tasks']}
        assert all(len(route) <= plan['capacity'] for route in routes.values())
        unavailable = {a['id']: a['reasons'] for a in plan['fleet'] if not a['available']}
        assert set(unavailable) == ({'uav1','uav2'} if name == 'degraded' else set())
        assert all(not routes[u] for u in unavailable)
        assert not pending
        reports[name] = dict(seconds=round(time.monotonic()-start, 3), rounds=plan['rounds'],
                             assigned=len(assigned), assignments=routes, unavailable=unavailable,
                             unassigned=plan['unassigned'])
    return dict(source='synthetic telemetry, allocation only', aircraft=count, tasks=len(tasks), scenarios=reports)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vehicles', type=int, choices=range(3, 33), default=16, metavar='3..32')
    parser.add_argument('--tasks', type=int, choices=range(1, 101), default=90, metavar='1..100')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = evaluate(args.vehicles, args.tasks)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    for name, row in report['scenarios'].items():
        print('%s: %d/%d 个任务已分配，%d 个待分配，%d 轮，%.3f 秒' %
              (name, row['assigned'], report['tasks'], len(row['unassigned']), row['rounds'], row['seconds']))
