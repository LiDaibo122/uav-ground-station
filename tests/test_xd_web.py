import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

try:
    from packages.adapters.xd_web import MissionError, Runtime, Station
except ModuleNotFoundError as error:
    if error.name not in ('rospy', 'std_msgs', 'xd_uav_controller', 'xd_uav_task_allocate'):
        raise
    raise unittest.SkipTest('Load xd_single_env.sh for the XD adapter tests')

from packages.adapters.xd_fleet import FleetRuntime, tool_catalog


class DualAircraftWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stations = {key: Mock(run_id='sim') for key in ('uav1', 'uav2')}
        for key, station in self.stations.items():
            station.snapshot.return_value = dict(vehicle=key, flight_phase='on_ground', job=None)
        self.env = dict(stations=self.stations, catalog=tool_catalog(self.stations))

    def test_state_query_and_explicit_vehicle_match(self):
        runtime = FleetRuntime(self.env, {}, 'live', '只给uav2起飞')
        self.assertEqual([s['vehicle'] for s in runtime.call('xd_state', {})['vehicles']], ['uav1', 'uav2'])
        with self.assertRaisesRegex(MissionError, '指定机号不匹配'):
            runtime.call('xd_takeoff', dict(vehicle_id='uav1', altitude_m=40))
        with self.assertRaisesRegex(ValueError, 'vehicle_id'):
            runtime.call('xd_takeoff', dict(altitude_m=40))
        for station in self.stations.values():
            station.submit.assert_not_called()

    def test_preview_page_rejects_write_tool_even_if_called_by_name(self):
        runtime = FleetRuntime(self.env, {}, 'xd_plan_only', '起飞')
        tools = [t['function']['name'] for t in runtime.definitions()]
        self.assertEqual(set(tools), {'xd_allocate','xd_state','xd_capabilities'})
        with self.assertRaisesRegex(MissionError, '仅用于集群分配规划'):
            runtime.call('xd_takeoff', {'vehicle_id': 'uav1','altitude_m': 40})
        self.stations['uav1'].submit.assert_not_called()

    def test_library_planning_reads_saved_tasks_and_cannot_dispatch(self):
        tasks = [dict(id='saved', label='河堤东段', kind='area', west=2, east=42, south=-10, north=0)]
        read = Mock(return_value=dict(run_id='sim', revision=4, tasks=tasks))
        preview = Mock(return_value={'assignments': {'uav1': ['T1']}})
        conversation = {}
        runtime = FleetRuntime(dict(self.env, read_library=read, preview_library=preview),
                               conversation, 'xd_task_library', '用库里的任务分工')
        tools = {t['function']['name'] for t in runtime.definitions()}
        self.assertEqual(tools, {'xd_library_allocate', 'xd_library_tasks', 'xd_state'})
        result = runtime.call('xd_library_allocate', {})
        self.assertEqual(result['library_revision'], 4)
        preview.assert_called_once_with(tasks)
        self.assertNotIn('xd_allocation', conversation)
        with self.assertRaises(MissionError):
            runtime.call('xd_confirm_allocation', {'plan_id': 'saved'})
        self.stations['uav1'].submit.assert_not_called()

    def test_vehicle_panel_queries_bound_vehicle_only(self):
        runtime = FleetRuntime(dict(self.env, vehicle_id='uav2'), {}, 'xd_vehicle', '当前位置')
        tools = runtime.definitions()
        self.assertEqual([t['function']['name'] for t in tools], ['xd_state'])
        self.assertEqual(runtime.call('xd_state', {})['vehicle'], 'uav2')
        with self.assertRaises(MissionError):
            runtime.call('xd_state', {'vehicle_id': 'uav1'})
        with self.assertRaises(MissionError):
            runtime.call('xd_takeoff', {'vehicle_id': 'uav2', 'altitude_m': 40})
        self.stations['uav2'].submit.assert_not_called()

    def test_workbench_named_task_and_vehicle_group_are_both_binding(self):
        tasks=[dict(kind='point',label='任务点'+str(i),east=600+i,north=120) for i in (1,2)]
        tasks.append(dict(kind='area',label='河堤',west=600,east=800,south=120,north=500))
        def preview(selected,allowed):
            tasks = []
            assignments = {}
            for i, task in enumerate(selected):
                tid = 'T'+str(i+1)
                area = ({k:task[k] for k in ('west','east','south','north')} if task['kind']=='area' else None)
                tasks.append(dict(id=tid,label=task['label'],east_m=task['east'],north_m=task['north'],area=area))
                assignments.setdefault(allowed[task['label']][0], []).append(tid)
            return dict(tasks=tasks,allowed=allowed,assignments=assignments,unassigned={},height_m=40)
        env=dict(self.env,workbench=True,dispatch=Mock(),read_library=lambda:{'tasks':tasks,'revision':2,'run_id':'sim'},
                 preview_library=preview)
        rt=FleetRuntime(env,{},'xd_task_library','uav1 负责所有点，uav2 负责河堤')
        plan=rt.call('xd_library_allocate',dict(groups=[dict(kind='point',vehicles=['uav1']),
                                                        dict(labels=['河堤'],vehicles=['uav2'])]))
        self.assertEqual(plan['allowed'],{'任务点1':['uav1'],'任务点2':['uav1'],'河堤':['uav2']})
        self.assertIn('短边',plan['flight_blocked'])
        rt=FleetRuntime(dict(env,vehicle_id='uav2'),{},'xd_vehicle','只去任务点1')
        plan=rt.call('xd_library_allocate',dict(groups=[dict(kind='point',labels=['任务点1'],vehicles=['uav2'])]))
        self.assertEqual(plan['allowed'],{'任务点1':['uav2']})
        self.assertNotIn('flight_blocked',plan)
        self.assertIn('东 601、北 120',rt.context()['submitted_map_text'])
        with self.assertRaisesRegex(MissionError,'本轮已经生成草案'):
            rt.call('xd_library_allocate',dict(groups=[dict(labels=['任务点1'],vehicles=['uav2'])]))
        rt=FleetRuntime(dict(env,vehicle_id='uav2'),{},'xd_vehicle','起飞盘旋')
        with self.assertRaises(MissionError):
            rt.call('xd_local_action',dict(vehicle_id='uav1',action='takeoff'))
        self.assertEqual(rt.call('xd_local_action',dict(action='takeoff'))['window'],'uav2')
        for station in self.stations.values():
            station.submit.assert_not_called()

    def test_route_progress_and_return_phase_use_distinct_evidence(self):
        station = Station.__new__(Station)
        station.route_progress = dict(goal_id=9, percent=44.5, received=time.monotonic())
        station.point_observation = None
        job = dict(id='inspection', action='inspection', phase='running', detail='路径跟随中',
                   return_home=True, assigned_points=[[650, 0, 40], [900, 0, 40]],
                   task_labels=['道路', '桥面'], events=[dict(action='task_path', phase='running', goal_id=9)])
        station.history = [job]
        station.observe_inspection_points(dict(x=651, y=0, z=40))
        result = station.mission_snapshot(job, time.monotonic())
        self.assertEqual(result['route_progress_pct'], 44.5)
        self.assertEqual(result['next_point_estimate']['label'], '桥面')
        self.assertIsNone(station.mission_snapshot(job, time.monotonic() + 5)['route_progress_pct'])
        job['events'] += [dict(action='task_path', phase='completed'), dict(action='land_home', phase='accepted')]
        result = station.mission_snapshot(job, time.monotonic())
        self.assertEqual(result['route_progress_pct'], 100)
        self.assertEqual(result['stage'], '返航降落')
        self.assertEqual(result['next_action'], '落地并上锁')
        self.assertIsNone(result['next_point_estimate'])

    def test_draft_is_private_to_vehicle_across_turns(self):
        conversation = {}
        planning = FleetRuntime(self.env, conversation, 'live', '为 uav1 保存草案')
        points = [dict(east=x, north=0, height=40) for x in (300, 500)]
        plan = planning.call('xd_plan', dict(vehicle_id='uav1', points=points))
        execution = FleetRuntime(self.env, conversation, 'live', '执行 uav2 的草案')
        with self.assertRaisesRegex(MissionError, '没有巡检草案'):
            execution.call('xd_execute', dict(vehicle_id='uav2'))
        self.assertEqual(conversation['xd_vehicles']['uav1']['xd_plan']['id'], plan['id'])
        self.stations['uav2'].submit.assert_not_called()


class SingleAircraftWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.station = Station.__new__(Station)
        self.station.vehicle = "uav1"
        self.station.lock = threading.RLock()
        self.station.history = []
        self.station.run_id = 'current-simulation'
        self.station.inbox = {}
        self.station.persist = Mock()
        self.station.pub = Mock()
        self.station.pub.get_num_connections.return_value = 1

    def test_save_plan_does_not_dispatch(self):
        self.station.submit = Mock()
        conversation = {}
        runtime = Runtime({'station': self.station}, conversation, 'live', '')
        points = [dict(east=x, north=0, height=40) for x in (300, 500, 700)]
        draft = runtime.call('xd_plan', {'points': points})
        self.assertEqual(draft['points_m'], [[300, 0, 40], [500, 0, 40], [700, 0, 40]])
        self.assertEqual(conversation['xd_plan'], draft)
        self.station.submit.assert_not_called()

    def test_execute_new_route_from_airborne_state_in_one_call(self):
        self.station.submit = Mock(return_value={'status': 'queued'})
        self.station.snapshot = Mock(return_value={'flight_phase': 'in_air'})
        conversation = {}
        runtime = Runtime({'station': self.station}, conversation, 'live', '')
        tools = [tool['function']['name'] for tool in runtime.definitions()]
        self.assertNotIn('xd_takeoff', tools)
        self.assertIn('xd_execute', tools)
        points = [dict(east=x, north=0, height=40) for x in (300, 500, 700)]
        runtime.call('xd_execute', {'points': points, 'return_home': True})
        draft = conversation['xd_plan']
        self.station.submit.assert_called_once_with('task_path', {'points_m': [[300, 0, 40], [500, 0, 40], [700, 0, 40]]},
                                                    draft['id'], True)

    def test_execute_saved_route_inherits_return_option(self):
        self.station.submit = Mock()
        conversation = {}
        runtime = Runtime({'station': self.station}, conversation, 'live', '')
        draft = runtime.call('xd_plan', {'points': [dict(east=x, north=0, height=40) for x in (300, 500)],
                                         'return_home': True})
        self.station.submit.assert_not_called()
        runtime.call('xd_execute', {})
        self.station.submit.assert_called_once_with('task_path', {'points_m': draft['points_m']}, draft['id'], True)

    def test_coverage_preview_then_execute_reuses_the_generated_path(self):
        self.station.snapshot = Mock(return_value={
            'flight_phase': 'in_air',
            'world_position_m': {'x': 200, 'y': 0, 'z': 60},
            'world_heading_rad': 0.0,
        })
        self.station.submit = Mock(return_value={'status': 'queued', 'return_home': True})
        area = dict(east_min_m=300, east_max_m=920, north_min_m=-305, north_max_m=305,
                    altitude_m=60, lane_spacing_m=210)
        conversation = {}
        planning = Runtime({'station': self.station}, conversation, 'live', '仅规划 S 形巡检')
        preview = planning.call('xd_plan_coverage', {'coverage': area, 'return_home': True})
        path = conversation['xd_plan']['points_m']
        self.assertNotIn('points_m', preview)
        self.assertEqual(preview['point_count'], len(path))
        self.assertEqual(planning.context()['latest_plan'], preview)
        self.assertTrue(all(p[2] == 60 for p in path))
        self.assertEqual([(a[0], b[0], a[1]) for a, b in zip(path, path[1:])
                          if {a[0], b[0]} == {300, 920} and a[1] == b[1]],
                         [(300, 920, -200), (920, 300, 220), (300, 920, 10)])
        self.station.submit.assert_not_called()
        execution = Runtime({'station': self.station}, conversation, 'live', '执行刚才的区域巡检')
        execution.call('xd_execute', {})
        self.station.submit.assert_called_once()
        action, payload, plan_id, return_home = self.station.submit.call_args.args
        self.assertEqual((action, plan_id, return_home), ('task_path', preview['id'], True))
        sent = payload['points_m']
        self.assertEqual(sent[-len(path):], path)
        self.assertGreater(len(sent), len(path))
        self.assertLess(abs(sent[0][0] - 200), 11)

    def test_coverage_rejects_excessive_scan_density(self):
        self.station.submit = Mock()
        area = dict(east_min_m=300, east_max_m=920, north_min_m=-305, north_max_m=305,
                    altitude_m=60, lane_spacing_m=0.01)
        runtime = Runtime({'station': self.station}, {}, 'live', '')
        with self.assertRaisesRegex(MissionError, '扫线过密'):
            runtime.call('xd_execute', {'coverage': area})
        self.station.submit.assert_not_called()

    def test_takeoff_rejected_if_airborne_even_for_stale_tool_call(self):
        self.station.active = False
        self.station.snapshot = Mock(return_value={'flight_phase': 'in_air', 'can_takeoff': False})
        runtime = Runtime({'station': self.station}, {}, 'live', '')
        with self.assertRaisesRegex(MissionError, 'uav1 已在空中'):
            runtime.call('xd_takeoff', {'altitude_m': 40})
        self.station.persist.assert_not_called()

    def test_do_not_dispatch_an_existing_plan_when_user_says_not_to_execute(self):
        self.station.snapshot = Mock(return_value={'flight_phase': 'in_air'})
        self.station.submit = Mock()
        conversation = {'xd_plan': {'id': 'saved', 'points_m': [[300, 0, 40], [500, 0, 40]],
                                    'run_id': self.station.run_id, 'return_home': True}}
        runtime = Runtime({'station': self.station}, conversation, 'live', '只把飞机下降至40m，先不要执行巡检。')
        self.assertNotIn('xd_execute', [tool['function']['name'] for tool in runtime.definitions()])
        with self.assertRaisesRegex(MissionError, '本轮仅允许查询或规划'):
            runtime.call('xd_execute', {})
        self.station.submit.assert_not_called()

    def test_altitude_only_request_does_not_dispatch_the_saved_plan(self):
        self.station.snapshot = Mock(return_value={'flight_phase': 'in_air'})
        self.station.submit = Mock()
        conversation = {'xd_plan': {'id': 'saved', 'points_m': [[300, 0, 40], [500, 0, 40]],
                                    'run_id': self.station.run_id, 'return_home': True}}
        runtime = Runtime({'station': self.station}, conversation, 'live', '只把飞机下降至40m。')
        with self.assertRaisesRegex(MissionError, '当前没有独立的空中改高度功能'):
            runtime.call('xd_execute', {})
        self.station.submit.assert_not_called()

    def run_route(self, route_receipt):
        previous = dict(action='task_path', phase='completed', goal_id=11)
        self.station.history.append(dict(events=[previous]))
        job = dict(id='new-route', action='task_path', return_home=True, events=[])
        self.station.history.append(job)
        sent = []

        def respond(message):
            command = json.loads(message.data)
            sent.append(command)
            receipt = dict(command, phase='completed', goal_id=command['payload'].get('goal_id'), message='XD reply')
            if command['action'] == 'task_path':
                receipt.update(route_receipt)
            message.data = json.dumps(receipt)
            self.station.receipt(message)

        self.station.pub.publish.side_effect = respond
        with patch('rospy.get_param', return_value=self.station.run_id), patch('rospy.is_shutdown', return_value=False):
            self.station.work(job, {'points_m': [[300, 0, 40], [500, 0, 40]]})
        return job, sent

    def test_two_routes_release_distinct_goals(self):
        job, sent = self.run_route(dict(goal_id=12))
        self.assertEqual(job['phase'], 'completed')
        self.assertEqual([c['action'] for c in sent], ['cancel', 'task_path', 'cancel', 'land_home'])
        self.assertEqual([sent[0]['payload'], sent[2]['payload']], [{'goal_id': 11}, {'goal_id': 12}])
        self.assertEqual(len({c['command_id'] for c in sent}), 4)

    def test_no_return_after_failed_or_uncorrelated_route(self):
        for receipt, phase in [(dict(phase='failed'), 'failed'), (dict(goal_id=None), 'unknown')]:
            with self.subTest(receipt=receipt):
                self.station.history.clear()
                job, sent = self.run_route(receipt)
                self.assertEqual(job['phase'], phase)
                self.assertEqual([c['action'] for c in sent], ['cancel', 'task_path'])
