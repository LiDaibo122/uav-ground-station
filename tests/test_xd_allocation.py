import unittest

from packages.adapters.xd_allocation import plan_inspections


NOW = 10000
TASKS = [dict(label='道路', east=300, north=0), dict(label='河堤', east=300, north=120),
         dict(label='桥面', east=500, north=120)]
OBS = {'road': dict(frame='world', east=300, north=0, observed_at=NOW-12, status='needs_review',
                    source='模拟传感器', text='疑似塌方')}


def aircraft(i):
    return dict(vehicle='uav%d' % (i+1), connected=True, localization_valid=True,
                world_position_m=dict(x=0, y=i*120), observed_at=NOW-1, battery_pct=95,
                can_takeoff=True, flight_phase='on_ground')


class AllocationTests(unittest.TestCase):
    def test_point_group_can_exceed_fleet_average(self):
        tasks = [dict(label=f'点{i}', east=100+i*12, north=10) for i in range(14)]
        allowed = {task['label']: ['uav2'] for task in tasks[:9]}
        states = [aircraft(i) for i in range(4)]
        plan = plan_inspections(dict(tasks=tasks), None, states, {}, '', NOW, allowed_by_label=allowed)
        self.assertFalse(plan['unassigned'])
        self.assertTrue({f'T{i}' for i in range(1,10)}.issubset(plan['assignments']['uav2']))
        flat = [key for route in plan['assignments'].values() for key in route]
        self.assertCountEqual(flat, [f'T{i}' for i in range(1, 15)])

    def test_excluding_an_aircraft_preserves_unmentioned_height(self):
        states = [aircraft(0), aircraft(1)]
        args = dict(tasks=TASKS, height_m=10)
        base = plan_inspections(args, None, states, {}, '分配这三个点', NOW)
        self.assertEqual(base['height_m'], 40)
        declared = plan_inspections(dict(tasks=TASKS, height_m=60), None, states, {}, '高度60米', NOW)
        followup = plan_inspections(dict(height_m=50, exclude_vehicles=['uav1']), declared, states,
                                    {}, '排除 uav1，重新分配', NOW)
        self.assertEqual(declared['height_m'], 60)
        self.assertEqual(followup['height_m'], 60)

    def test_allocation_and_snapshot_replay(self):
        states = [aircraft(i) for i in range(12)]
        base = plan_inspections({'tasks': TASKS}, None, states, OBS, '', NOW)
        self.assertEqual([t['priority'] for t in base['tasks']], ['high', 'normal', 'normal'])
        self.assertEqual(base['tasks'][0]['evidence'][0]['id'], 'road')
        replay = plan_inspections({}, base, states, OBS, '', NOW)
        self.assertEqual(base['assignments'], replay['assignments'])
        self.assertEqual(len(base['fleet']), 12)
        self.assertEqual(sorted(t for route in base['assignments'].values() for t in route), ['T1','T2','T3'])

    def test_fresh_snapshot_for_each_replan(self):
        states = [aircraft(0), aircraft(1)]
        base = plan_inspections({'tasks': TASKS}, None, states, OBS, '', NOW)
        for change in (dict(connected=False), dict(battery_pct=None), dict(observed_at=NOW-10),
                       dict(job={'phase':'unknown'}), dict(localization_valid=False),
                       dict(flight_phase='landing'), dict(battery_pct=24)):
            with self.subTest(change=change):
                new = plan_inspections({}, base, [states[0], dict(states[1], **change)], OBS, '', NOW)
                self.assertEqual(new['assignments']['uav2'], [])
                self.assertFalse(new['unassigned'])
                self.assertCountEqual(new['assignments']['uav1'], ['T1', 'T2', 'T3'])
                self.assertTrue(new['fleet'][1]['reasons'])
        self.assertTrue(base['fleet'][1]['available'])

    def test_no_available_or_capable_aircraft(self):
        p = plan_inspections({'tasks': TASKS, 'exclude_vehicles':['uav1']}, None, [aircraft(0)], {}, '', NOW)
        self.assertEqual(list(p['unassigned'].values()), ['没有可用飞机']*3)
        p = plan_inspections({'tasks':[dict(TASKS[0], sensor='thermal')]}, None, [aircraft(0)], {}, '', NOW)
        self.assertEqual(p['unassigned']['T1'], '可用飞机载荷不匹配')

    def test_observation_scope_and_age(self):
        text = '东270至330米，北-20至20米'
        task = dict(label='北区', area=dict(west=270,east=330,south=-20,north=20),pattern='sweep',bounds_quote=text)
        for change, expected in [({},'high'),(dict(status='resolved'),'normal'),
                                 (dict(observed_at=NOW-1801),'normal'),(dict(observed_at=NOW+1),'normal'),
                                 (dict(north=80),'normal')]:
            p = plan_inspections({'tasks':[task]}, None, [aircraft(0)], {'o':dict(OBS['road'], **change)}, text, NOW)
            self.assertEqual(p['tasks'][0]['priority'], expected)
            self.assertEqual(p['tasks'][0]['pattern'], 'sweep')

    def test_point_stays_a_point_after_model_rectangle_encoding(self):
        spec = dict(label='道路', area=dict(west=300,east=300,south=0,north=0))
        p = plan_inspections({'tasks':[spec]}, None, [aircraft(0)], {}, '道路东300米北0米', NOW)
        self.assertEqual(p['tasks'][0]['pattern'], 'point')
        spec['area'].update(west=290,east=310,south=-10,north=10)
        with self.assertRaisesRegex(ValueError,'四个边界'):
            plan_inspections({'tasks':[spec]}, None, [aircraft(0)], {}, '道路东300米北0米', NOW)

    def test_human_override_requires_evidence(self):
        task = dict(TASKS[0], priority_override='low',priority_quote='道路低优先级')
        with self.assertRaisesRegex(ValueError,'原话'):
            plan_inspections({'tasks':[task]}, None, [aircraft(0)], OBS, '按观测评估', NOW)
        p = plan_inspections({'tasks':[task]}, None, [aircraft(0)], OBS, '道路低优先级', NOW)
        self.assertEqual(p['tasks'][0]['priority'],'low')
        self.assertEqual(p['tasks'][0]['evidence'][0]['id'],'road')
