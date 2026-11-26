"""Civil inspection allocation previews. All positions use the XD simulation world (ENU metres)."""
import json
import math
import re
import time
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from packages.ground_core.agent_tools import definition, object_schema, validate
from packages.ground_core.task_library import MAX_LIBRARY_TASKS
from packages.task_allocation.solvers import eligible, solve


NUMBER = {'type': 'number'}
RECT = object_schema({k: NUMBER for k in ('west', 'east', 'south', 'north')},
                     ['west', 'east', 'south', 'north'])
TASK = object_schema({
    'label': {'type': 'string'},
    'east': dict(NUMBER, description='点任务的东坐标（米），一对坐标直接使用 east 和 north'),
    'north': dict(NUMBER, description='点任务的北坐标（米）'), 'area': RECT,
    'bounds_quote': {'type': 'string', 'description': '仅矩形使用，逐字引用用户给出四个边界的原文'},
    'pattern': {'type': 'string', 'enum': ['sweep'], 'description': '仅用户明确要求矩形 S 形扫线时填写'},
    'sensor': {'type': 'string', 'enum': ['rgb', 'thermal']},
    'priority_override': {'type': 'string', 'enum': ['high', 'normal', 'low'],
                          'description': '仅用户明确指定等级时填写，并引用原话'},
    'priority_quote': {'type': 'string'},
}, ['label'])
PLAN_TOOL = definition('xd_allocate', '集群巡检分配草案',
    '用实时仿真遥测、已登记观测和 CBBA 给集群分配巡检任务，只规划。点用 east/north；矩形用 area。'
    '单个位置如东300米北0米，参数为 {"label":"道路","east":300,"north":0}。切勿把点扩大为区域。'
    '新规划传全部 tasks。重算上次任务时省略 tasks；仅排除用户明确要求不参与的飞机。',
    {'tasks': {'type': 'array', 'items': TASK, 'minItems': 1, 'maxItems': MAX_LIBRARY_TASKS},
     'exclude_vehicles': {'type': 'array', 'items': {'type': 'string'}},
     'height_m': dict(NUMBER, description='用户明确指定的全组巡检 world 高度（米）。未指定则省略'),
     'height_quote': {'type': 'string', 'description': '指定高度时，逐字引用用户含高度和数值的短语'}})

# Simulation planning policy, independent of the model. The payload is a scenario declaration.
POLICY = json.loads((Path(__file__).resolve().parents[2] / 'config/xd_planning.json').read_text())
LEVEL_WORDS = {'high': ('高优先','优先处理','优先巡检','最优先'), 'normal': ('常规','普通','正常','中优先'),
               'low': ('低优先','最后处理')}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def aircraft_input(state, excluded, now, policy):
    uid = state['vehicle']
    pos = state.get('world_position_m') or {}
    stamp = state.get('observed_at')
    battery = state.get('battery_pct')
    phase = state.get('flight_phase')
    reasons = []
    if uid in excluded:
        reasons.append('本轮规划排除')
    if not state.get('connected') or not finite(stamp) or not 0 <= now-stamp <= policy['state_ttl_s']:
        reasons.append('连接或遥测时效不满足')
    if not state.get('localization_valid') or not all(finite(pos.get(k)) for k in ('x', 'y')):
        reasons.append('缺少有效 world 定位')
    if not finite(battery) or not 25 <= battery <= 100:
        reasons.append('电量未知或不满足分配门槛')
    if not (phase == 'on_ground' and state.get('can_takeoff') or
            phase == 'in_air' and state.get('armed') and state.get('mode') == 'OFFBOARD'):
        reasons.append('当前飞行阶段不可接单')
    if state.get('workload_known') is False:
        reasons.append('底座尚未报告空中任务占用状态')
    job_phase = (state.get('job') or {}).get('phase')
    if job_phase and job_phase not in ('completed', 'failed', 'rejected', 'cancelled'):
        reasons.append('已有任务或结果待核实')
    profile = policy.get('aircraft_profiles', {}).get(uid, {})
    speed = profile.get('speed_mps', policy['speed_mps'])
    capabilities = profile.get('capabilities', policy['capabilities'])
    if not finite(speed) or speed <= 0 or not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise ValueError('仿真机型配置无效：' + uid)
    return dict(id=uid, east_m=pos.get('x'), north_m=pos.get('y'), battery_pct=battery,
                observed_at=stamp, phase=phase, job_phase=job_phase, available=not reasons,
                reasons=reasons, speed_mps=speed, capabilities=capabilities)


def inspect_observations(records):
    if not isinstance(records, dict):
        raise ValueError('观测接口应为按观测编号索引的对象')
    for oid, row in records.items():
        if not (isinstance(row, dict) and row.get('frame') == 'world'
                and row.get('status') in ('needs_review', 'resolved')
                and all(finite(row.get(k)) for k in ('east', 'north', 'observed_at'))
                and all(isinstance(row.get(k), str) and row[k].strip() for k in ('source', 'text'))):
            raise ValueError('观测格式无效：' + oid)


def inspection_task(number, spec, observations, now, policy):
    area = spec.get('area')
    label = spec['label'].strip()
    if not label or len(label) > 120:
        raise ValueError('任务名应为 1 至 120 个字符')
    if area:
        if 'east' in spec or 'north' in spec or area['east'] <= area['west'] or area['north'] <= area['south']:
            raise ValueError('矩形须有正面积，并与点坐标分开填写')
        x, y = (area['west'] + area['east']) / 2, (area['south'] + area['north']) / 2
    else:
        if 'east' not in spec or 'north' not in spec or 'pattern' in spec:
            raise ValueError('点巡检需要东、北坐标；S 形巡检需要矩形边界')
        x, y = spec['east'], spec['north']
    evidence = []
    for key, row in sorted(observations.items()):
        if row['status'] != 'needs_review' or not 0 <= now-row['observed_at'] <= policy['observation_ttl_s']:
            continue
        px, py = row['east'], row['north']
        matches = (area['west'] <= px <= area['east'] and area['south'] <= py <= area['north']
                   if area else math.hypot(px-x, py-y) <= policy['point_match_m'])
        if matches:
            evidence.append(dict(id=key, **{k: row[k] for k in ('source', 'text', 'observed_at')}))
    level = spec.get('priority_override') or ('high' if evidence else 'normal')
    basis = ('人工指定：' + spec['priority_quote'] if spec.get('priority_override')
             else '存在待复核观测' if evidence else '常规巡检，无匹配的有效异常观测')
    return dict(id='T%d' % number, label=label, east_m=x, north_m=y, area=area,
                pattern=spec.get('pattern', 'area' if area else 'point'), priority=level,
                basis=basis, evidence=evidence, required_capabilities=[spec.get('sensor', 'rgb')])


def grounded_task(spec, text):
    area = spec.get('area')
    if not area:
        return spec
    if ('pattern' not in spec and 'east' not in spec and 'north' not in spec
            and area['west'] == area['east'] and area['north'] == area['south']):
        point = {k: v for k, v in spec.items() if k not in ('area', 'bounds_quote')}
        return dict(point, east=area['east'], north=area['north'])
    quote = spec.get('bounds_quote', '')
    # Require the original bounds. One coordinate pair does not describe an area.
    literals = re.findall(r'([+-]?\d+(?:\.\d+)?)\s*(公里|千米|km|米|m)?', quote, re.I)
    if not quote or quote not in text or len(literals) < 4:
        raise ValueError('矩形需引用用户给出四个边界的原文。每项只给一个位置时，请传 east 和 north')
    # In a range like "0.2 至 0.5 公里", the end unit also applies to the first number.
    values = []
    unit = '米'
    for raw, suffix in reversed(literals):
        unit = suffix.lower() if suffix else unit
        values.append(abs(float(raw)) * (1000 if unit in ('公里', '千米', 'km') else 1))
    for bound in area.values():
        match = next((v for v in values if math.isclose(v, abs(bound))), None)
        if match is None:
            raise ValueError('矩形边界与用户原文不符')
        values.remove(match)
    return spec


def inspection_height(args, previous, user_text, policy):
    default = previous.get('height_m', policy['height_m']) if 'tasks' not in args else policy['height_m']
    requested = args.get('height_m')
    if requested is None or requested == default:
        return default
    # An omitted altitude is a policy choice, including when a model fills optional arguments.
    cue = r'(?:高度|飞高|离地|距地|高(?=\s*[+-]?\d)|altitude|height)'
    if not re.search(cue, user_text, re.I):
        return default
    quote = args.get('height_quote') or user_text
    pattern = cue + r'\s*(?:设为|为|至|到|of|at|=|:|：)?\s*([+-]?\d+(?:\.\d+)?)\s*(公里|千米|km|米|m)?'
    if quote in user_text:
        for match in re.finditer(pattern, quote, re.I):
            scale = 1000 if (match[2] or '').lower() in ('公里', '千米', 'km') else 1
            if math.isclose(float(match[1]) * scale, requested):
                return requested
    raise ValueError('请引用用户明确指定的高度和数值；仅调整机号或任务分工时请省略 height_m')


def plan_inspections(args, previous, states, observations, user_text, now=None, policy=None, allowed_by_label=None):
    validate(args, PLAN_TOOL['wire']['function']['parameters'])
    now = time.time() if now is None else now
    policy = POLICY if policy is None else policy
    previous = previous or {}
    specs = args.get('tasks', previous.get('task_specs'))
    if not specs:
        raise ValueError('请提供巡检任务的名称和 world 点坐标或矩形边界')
    if 'tasks' in args:
        specs = [grounded_task(spec, user_text) for spec in specs]
        for spec in specs:
            level, quote = spec.get('priority_override'), spec.get('priority_quote')
            if (level or quote) and not (level and quote and quote in user_text and
                                         any(word in quote for word in LEVEL_WORDS[level])):
                raise ValueError('人工等级需要本轮明确指定的优先级原话；自动评估请省略等级')
    excluded = args.get('exclude_vehicles', previous.get('excluded_vehicles', []) if 'tasks' not in args else [])
    if set(excluded) - {s['vehicle'] for s in states}:
        raise ValueError('规划排除列表中存在未知机号')
    inspect_observations(observations)
    fleet = [aircraft_input(s, excluded, now, policy) for s in sorted(states, key=lambda s: s['vehicle'])]
    tasks = [inspection_task(i, spec, observations, now, policy) for i, spec in enumerate(specs, 1)]
    for task in tasks:
        if allowed_by_label and task['label'] in allowed_by_label:
            task['allowed_aircraft_ids'] = allowed_by_label[task['label']]
    candidates_by_task = {t['id']: [a['id'] for a in fleet if eligible(a, t)] for t in tasks}
    capacity = policy['capacity']
    if capacity == 'auto':
        # Estimate the load of each eligible group, including tasks reserved for a single aircraft.
        loads = {a['id']: 0 for a in fleet}
        for members in filter(None, candidates_by_task.values()):
            share = Fraction(1, len(members))
            for uid in members:
                loads[uid] += share
        capacity = max(1, math.ceil(max(loads.values(), default=0)))
    result = solve(fleet, tasks, 'cbba', capacity)
    # Overlapping groups can fill the same aircraft even with a balanced initial estimate.
    while policy['capacity'] == 'auto' and capacity < len(tasks):
        full = {uid for uid, route in result['assignments'].items() if len(route) == capacity}
        if not any(full.intersection(candidates_by_task[tid]) for tid in result['unassigned']):
            break
        capacity = min(2 * capacity, len(tasks))
        result = solve(fleet, tasks, 'cbba', capacity)
    assignments = {a['id']: result['assignments'].get(a['id'], []) for a in fleet}
    unassigned = {}
    for t in tasks:
        if t['id'] not in result['unassigned']:
            continue
        candidates = candidates_by_task[t['id']]
        if candidates:
            reason = ('适配飞机本轮容量已满' if all(len(assignments[uid]) >= capacity for uid in candidates)
                      else '在当前时限和收益约束下未中标')
        else:
            if t.get('allowed_aircraft_ids'):
                unassigned[t['id']] = '指定飞机不可用或载荷不匹配'
                continue
            reason = '没有可用飞机' if not any(a['available'] for a in fleet) else '可用飞机载荷不匹配'
        unassigned[t['id']] = reason
    height = inspection_height(args, previous, user_text, policy)
    if not finite(height) or height <= 0:
        raise ValueError('巡检高度须为正数')
    return dict(id=uuid4().hex, created_at=now, algorithm='CBBA', frame='world', status='preview',
                height_m=height, return_home=True,
                task_specs=specs, excluded_vehicles=excluded, tasks=tasks, fleet=fleet, policy=policy,
                capacity=capacity,
                assignments=assignments, unassigned=unassigned, metrics=result['metrics'], rounds=result['rounds'],
                arrival_estimates_s=result.get('arrival_estimates_s', {}), bid_factors=result.get('bid_factors', {}))
