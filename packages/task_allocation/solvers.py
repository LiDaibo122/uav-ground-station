"""Interchangeable allocation algorithms with common feasibility and metrics."""
import math
from pathlib import Path
import sys
from .cbba import allocate

NAMES=('cbba','hungarian','auction','greedy')


def eligible(a,t):
    return (a.get('available',True) and a.get('battery_pct') is not None and a['battery_pct']>=25
            and set(t.get('required_capabilities',[])).issubset(a.get('capabilities',[]))
            and (not t.get('allowed_aircraft_ids') or a['id'] in t['allowed_aircraft_ids']))


def metrics(plan,aircraft,tasks):
    lookup={t['id']:t for t in tasks}
    fleet={a['id']:a for a in aircraft}
    distances={}
    durations={}
    for uid,route in plan['assignments'].items():
        a=fleet[uid];x,y=a['east_m'],a['north_m'];distance=0
        for tid in route:
            t=lookup[tid];distance+=math.hypot(t['east_m']-x,t['north_m']-y)
            x,y=t['east_m'],t['north_m']
        distances[uid]=round(distance,2)
        durations[uid]=round(distance/a.get('speed_mps',18),2)
    plan['metrics']={'total_distance_m':round(sum(distances.values()),2),
        'route_distance_m':distances,'estimated_route_time_s':durations,
        'estimated_makespan_s':max(durations.values(),default=0),
        'basis':'straight-line route length; excludes takeoff, bank/turn constraints, terrain and wind'}
    return plan


def solve(aircraft,tasks,algorithm='cbba',capacity=2):
    if algorithm not in NAMES:
        raise ValueError('未知分配算法：'+str(algorithm))
    if algorithm=='cbba':
        result=allocate(aircraft,tasks,capacity)
        result['algorithm_id']='cbba'
        return metrics(result,aircraft,tasks)
    active=[a for a in aircraft if a.get('available') and a.get('battery_pct') is not None and a['battery_pct']>=25]
    assignments={a['id']:[] for a in active}
    result={'algorithm':algorithm,'algorithm_id':algorithm,'assignments':assignments,
            'unassigned':[],'input_fleet':aircraft,'capacity':capacity}
    if algorithm=='greedy':
        positions={a['id']:(a['east_m'],a['north_m']) for a in active}
        for task in sorted(tasks,key=lambda t:{'high':0,'normal':1,'low':2}[t.get('priority','normal')]):
            candidates=[a for a in active if eligible(a,task) and len(assignments[a['id']])<capacity]
            if not candidates:
                result['unassigned'].append(task['id']);continue
            a=min(candidates,key=lambda a:math.hypot(task['east_m']-positions[a['id']][0],task['north_m']-positions[a['id']][1])/(.5+.5*a['battery_pct']/100))
            assignments[a['id']].append(task['id'])
            positions[a['id']]=(task['east_m'],task['north_m'])
    else:
        if len(tasks)>len(active):
            raise ValueError(algorithm+' 当前实现为一对一匹配，任务数多于可用飞机时请使用 CBBA 或 greedy')
        if not tasks:
            return metrics(result,aircraft,tasks)
        import numpy as np
        from scipy.optimize import linear_sum_assignment
        costs=np.array([[math.hypot(t['east_m']-a['east_m'],t['north_m']-a['north_m'])/(a.get('speed_mps',18)*(.5+.5*a['battery_pct']/100))
                         if eligible(a,t) else 1e12 for a in active] for t in tasks],dtype=float)
        rows,cols=linear_sum_assignment(costs)
        if any(costs[i,j]>=1e12 for i,j in zip(rows,cols)):
            result['unassigned']=[t['id'] for t in tasks]
            return metrics(result,aircraft,tasks)
        if algorithm=='auction':
            root=Path(__file__).resolve().parents[2]
            sys.path.insert(0,str(root/'third_party/sslap'))
            try:
                from sslap import auction_solve
            except ImportError as exc:
                raise RuntimeError('基础拍卖扩展未编译，请运行源码准备脚本') from exc
            matrix=np.where(costs>=1e12,-1,np.round(costs*100)+1).astype(float)
            # Upstream's rectangular path can return a suboptimal assignment.
            # Zero-cost dummy tasks reduce it to its verified square problem.
            square=np.zeros((len(active),len(active)),dtype=float)
            square[:len(tasks),:]=matrix
            answer=auction_solve(square,problem='min')
            cols=answer['sol'][:len(tasks)];rows=range(len(tasks))
            result['solver_meta']=answer['meta']
        for i,j in zip(rows,cols):
            if j<0 or j>=len(active) or costs[i,j]>=1e12:
                raise RuntimeError('算法返回不可行匹配')
            assignments[active[j]['id']].append(tasks[i]['id'])
        result['scope']='one-to-one matching, at most one task per aircraft'
    return metrics(result,aircraft,tasks)
