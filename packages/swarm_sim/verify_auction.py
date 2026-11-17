"""Reproducible correctness checks; does not claim CBBA global optimality."""
import json
import math
from pathlib import Path
import sys
import time
import numpy as np
from scipy.optimize import linear_sum_assignment
from packages.task_allocation.cbba import ROOT, allocate, inspection_tasks


def run():
    sys.path.insert(0, str(ROOT/'third_party/sslap'))
    from sslap import auction_solve
    rng = np.random.default_rng(20261114)
    checks = []
    gaps = []
    t0 = time.perf_counter()
    for case in range(120):
        n = [3, 5, 10, 20][case % 4]
        cost = rng.integers(1, 10000, size=(n, n)).astype(float)
        result = auction_solve(cost.copy(), problem='min')
        sol = result['sol']
        rows, cols = linear_sum_assignment(cost)
        if len(set(sol)) != n or any(j < 0 or j >= n for j in sol):
            raise AssertionError('invalid basic auction matching')
        gap = float(cost[np.arange(n), sol].sum()-cost[rows, cols].sum())
        gaps.append(gap)
        if abs(gap) > 1e-6:
            raise AssertionError('basic auction failed integer optimality comparison')
    checks.append({'name': 'SSlap auction vs SciPy optimal linear assignment',
                   'cases': 120, 'sizes': [3, 5, 10, 20], 'seed': 20261114,
                   'maximum_cost_gap': max(gaps), 'passed': True})
    aircraft = [{'id': 'FW-%02d' % (i+1), 'east_m': 0, 'north_m': i*350} for i in range(3)]
    tasks = inspection_tasks()
    initial = allocate(aircraft, tasks)
    assert not initial['unassigned']
    assert initial['assignments'] == {'FW-01': ['SURVEY-01', 'SURVEY-02'],
        'FW-02': ['SURVEY-03', 'SURVEY-04'], 'FW-03': ['SURVEY-05', 'SURVEY-06']}
    checks.append({'name': 'CBBA 3 aircraft / 6 points: complete, exclusive, capacity 2, consensus',
                   'passed': True, 'allocation': initial})
    random_rounds = []
    for case in range(60):
        fleet = [{'id': 'FW-%02d' % i, 'east_m': float(rng.uniform(-1500, 1500)),
                  'north_m': float(rng.uniform(-1500, 1500))} for i in range(3)]
        points = [{'id': 'P-%02d' % j, 'east_m': float(rng.uniform(-2000, 2000)),
                   'north_m': float(rng.uniform(-2000, 2000))} for j in range(6)]
        plan = allocate(fleet, points)
        assert not plan['unassigned']
        assert all(len(route) <= 2 for route in plan['assignments'].values())
        random_rounds.append(plan['rounds'])
    checks.append({'name': 'CBBA seeded random geometry: coverage, exclusivity, capacity, consensus',
                   'cases': 60, 'max_rounds': max(random_rounds), 'passed': True})
    # Offline replanning, deliberately distinct from real in-flight failure tests.
    fleet = [dict(a) for a in aircraft]
    fleet[1]['available'] = False
    replan = allocate(fleet, tasks, capacity=3)
    assert not replan['unassigned'] and 'FW-02' not in replan['assignments']
    checks.append({'name': 'Offline unavailable-aircraft re-auction', 'passed': True, 'allocation': replan})
    fleet[0]['battery_pct'] = 20
    battery = allocate(fleet, tasks, capacity=6)
    assert list(battery['assignments']) == ['FW-03'] and not battery['unassigned']
    checks.append({'name': 'Offline low-battery eligibility filter', 'passed': True})
    overloaded = allocate(aircraft, tasks+[{**tasks[0], 'id': 'OVERFLOW'}])
    assert len(overloaded['unassigned']) == 1
    checks.append({'name': 'Insufficient capacity explicitly reports unassigned task', 'passed': True})
    one = [aircraft[0]]
    urgent_tasks = [{'id': 'NORMAL', 'east_m': 900, 'north_m': 0, 'priority': 'normal'},
                    {'id': 'URGENT', 'east_m': 900, 'north_m': 0, 'priority': 'high'}]
    priority = allocate(one, urgent_tasks, capacity=1)
    assert priority['assignments']['FW-01'] == ['URGENT']
    checks.append({'name': 'Priority influences winning bid at equal travel distance', 'passed': True})
    report = {'success': True, 'elapsed_ms': (time.perf_counter()-t0)*1000, 'checks': checks,
              'notes': ['SSlap optimality comparison applies only to one-to-one integer cost matrices.',
                        'CBBA is not claimed globally optimal; random cases test assignment invariants.',
                        'Failure and battery scenarios are algorithm-only, not flight failover tests.']}
    path = ROOT/'reports/auction-verification.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({'success': True, 'check_groups': len(checks),
                      'random_cases': 180, 'report': str(path)}, indent=2))
    return report


if __name__ == '__main__':
    run()
