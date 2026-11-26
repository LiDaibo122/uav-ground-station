import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from packages.adapters.xd_dispatch import Dispatch, map_flight_payloads
    from packages.adapters.xd_fleet import FleetRuntime, confirms_saved_plan, tool_catalog
    from packages.adapters.xd_web import MissionError
except ModuleNotFoundError as exc:
    if exc.name not in ('rospy','std_msgs','xd_uav_controller','xd_uav_task_allocate'):
        raise
    raise unittest.SkipTest('Load xd_single_env.sh for the XD adapter tests')

from packages.adapters.xd_allocation import plan_inspections
from packages.ground_core.agent import Agent


class DispatchTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory=Path(tmp.name)
        self.stations={}
        states=[]
        for i,uid in enumerate(('uav1','uav2')):
            state=dict(vehicle=uid,connected=True,armed=False,can_takeoff=True,flight_phase='on_ground',
                       world_position_m={'x':0,'y':120*i},localization_valid=True,observed_at=time.time(),
                       battery_pct=90, job=None)
            s=Mock(run_id='sim',lock=threading.RLock())
            s.snapshot.return_value=state
            s.inspection_receipt.return_value=None
            def submit(action, payload, plan_id, return_home, s=s, uid=uid):
                s.inspection_receipt.return_value=dict(execution_id=uid+'job',phase='queued',detail='排队',events=[])
            s.submit.side_effect=submit
            states.append(state)
            self.stations[uid]=s
        self.plan=plan_inspections({'tasks':[dict(label='道路',east=500,north=0),dict(label='桥面',east=500,north=120)]},
                                   None,states,{},'')
        self.plan['run_id']='sim'
        self.pid=self.plan['id']
        self.check=Mock()
        self.dispatch=Dispatch(self.directory,self.stations,self.check)
        self.conversation={'xd_allocation':self.plan,'xd_latest_draft':'fleet'}
        self.env=dict(stations=self.stations,catalog=tool_catalog(self.stations),dispatch=self.dispatch)

    def test_map_mission_compiles_coverage_and_preserves_point_assignment(self):
        plan=self.plan
        plan['kind']='library_flight'
        plan['tasks'][0].update(area=dict(west=650,east=1290,south=-250,north=235),pattern='area')
        with patch('packages.adapters.xd_dispatch.check_gazebo_world') as gazebo:
            self.dispatch.confirm(plan, self.pid)
        gazebo.assert_called_once_with(self.stations)
        area=self.stations['uav1'].submit.call_args.args[1]
        self.assertTrue(area['coverage_entry'])
        self.assertGreater(len(area['points_m']),20)
        self.assertEqual(self.stations['uav2'].submit.call_args.args[1]['points_m'],[[500,120,40]])
        plan['tasks'][0]['area'].update(west=2650, east=3290, south=750, north=1235)
        moved = map_flight_payloads(plan)['uav1']['points_m']
        self.assertEqual(len(moved), len(area['points_m']))
        for (x, y, z), (old_x, old_y, old_z) in zip(moved, area['points_m']):
            self.assertAlmostEqual(x-old_x, 2000)
            self.assertAlmostEqual(y-old_y, 1000)
            self.assertEqual(z, old_z)

    def test_map_points_preserve_submitted_coordinates_outside_old_airfield(self):
        coords = [(-369,496), (413,437), (249,974), (39,871), (915,713),
                  (1040,1564), (-877,1765), (-980,568), (1181,-516), (-542,-725)]
        assignments = {'uav1':[10,9], 'uav2':[4,3], 'uav3':[2,5,6], 'uav4':[1,8,7]}
        plan = dict(tasks=[dict(id=i, label=f'任务点{i}', east_m=x, north_m=y)
                           for i,(x,y) in enumerate(coords,1)],
                    assignments=assignments, unassigned={}, height_m=40)
        payloads = map_flight_payloads(plan)
        for uid, ids in assignments.items():
            self.assertEqual(payloads[uid]['points_m'], [[*coords[i-1], 40] for i in ids])
        plan['tasks'][0]['east_m'] = float('nan')
        with self.assertRaisesRegex(MissionError, '有效的 world 坐标'):
            map_flight_payloads(plan)

    def test_idempotent_two_requests_and_restart(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts=list(pool.map(lambda _: self.dispatch.confirm(self.plan,self.pid), range(2)))
        self.assertEqual(sorted(r['repeated'] for r in receipts),[False,True])
        restarted=Dispatch(self.directory,self.stations,self.check)
        self.assertTrue(restarted.confirm(self.plan,self.pid)['repeated'])
        for s in self.stations.values():
            s.submit.assert_called_once()
        self.check.assert_called_once()

    def test_preflight_rejects_only_stale_member_and_other_can_finish(self):
        self.stations['uav2'].snapshot.return_value['observed_at']-=15
        r=self.dispatch.confirm(self.plan,self.pid)
        self.assertEqual(r['vehicles']['uav2']['phase'],'rejected')
        self.stations['uav2'].submit.assert_not_called()
        self.stations['uav1'].inspection_receipt.return_value.update(phase='completed')
        self.assertEqual(self.dispatch.status(self.pid)['status'],'partial')
        self.stations['uav2'].snapshot.return_value['observed_at']=time.time()
        self.dispatch.confirm(self.plan,self.pid)
        self.stations['uav2'].submit.assert_not_called()

    def test_restart_after_reservation_returns_unknown_without_dispatch(self):
        record=dict(plan_id=self.pid,vehicles={u:dict(task_ids=ids,phase='reserved') for u,ids in self.plan['assignments'].items()},
                    task_labels={},unassigned={})
        self.dispatch.path(self.pid).write_text(json.dumps(record))
        r=self.dispatch.confirm(self.plan,self.pid)
        self.assertEqual(r['status'],'unknown')
        for s in self.stations.values():
            s.submit.assert_not_called()

    def test_independent_terminal_receipts_and_no_reassignment(self):
        self.dispatch.confirm(self.plan,self.pid)
        a,b=self.stations.values()
        a.inspection_receipt.return_value.update(phase='failed',detail='规划器拒绝航线')
        self.assertEqual(self.dispatch.status(self.pid)['status'],'running')
        b.inspection_receipt.return_value.update(phase='completed',detail='已上锁')
        result=self.dispatch.confirm(self.plan,self.pid)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['vehicles']['uav1']['detail'],'规划器拒绝航线')
        for uid,s in self.stations.items():
            s.submit.assert_called_once()
            self.assertEqual(result['vehicles'][uid]['task_ids'],self.plan['assignments'][uid])

    def test_confirmation_scope_and_current_version(self):
        args={'plan_id':self.pid}
        for mode,text in [('live','执行情况如何'),('xd_plan_only','按刚才方案执行'),('live','改完再执行'),
                          ('live','不要执行'),('live','先执行 uav1 的任务'),('live','如果可以就执行')]:
            with self.subTest(text=text):
                rt=FleetRuntime(self.env,self.conversation,mode,text)
                with self.assertRaises(MissionError):
                    rt.call('xd_confirm_allocation',args)
        rt=FleetRuntime(dict(self.env,displayed_plan_id='old'),self.conversation,'live','按刚才的集群方案执行。')
        with self.assertRaisesRegex(MissionError,'页面所见'):
            rt.call('xd_confirm_allocation',args)
        for wrong, version in [('old',self.pid),('sim','1'*32)]:
            with self.assertRaises(MissionError):
                self.dispatch.confirm(dict(self.plan,run_id=wrong),version)
        self.assertTrue(confirms_saved_plan('再次确认，按刚才的集群方案执行。'))
        self.assertEqual(list(self.directory.iterdir()),[])
        self.check.assert_not_called()

    def test_dispatch_drawer_requires_the_open_draft_and_explicit_confirmation(self):
        message = '按刚才的集群方案执行。'
        scoped = FleetRuntime(dict(self.env, displayed_plan_id=self.pid), self.conversation,
                              'xd_dispatch_only', message)
        self.assertEqual(scoped.required_tool, 'xd_confirm_allocation')
        with self.assertRaisesRegex(MissionError, '页面所见'):
            stale = FleetRuntime(dict(self.env, displayed_plan_id='old'), self.conversation, 'xd_dispatch_only', message)
            stale.call('xd_confirm_allocation', {'plan_id': self.pid})
        query = FleetRuntime(self.env, self.conversation, 'xd_dispatch_only', '方案进度如何')
        with self.assertRaises(MissionError):
            query.call('xd_confirm_allocation', {'plan_id': self.pid})
        self.assertEqual(scoped.call('xd_confirm_allocation', {'plan_id': self.pid})['plan_id'], self.pid)

    def test_fleet_confirmation_only_exposes_the_stored_plan(self):
        rt = FleetRuntime(self.env, self.conversation, 'live', '按刚才的集群方案执行。')
        self.assertEqual(rt.context(), {'latest_cluster_plan': {'id': self.pid}})
        self.assertEqual([tool['function']['name'] for tool in rt.definitions()], ['xd_confirm_allocation'])
        with self.assertRaisesRegex(MissionError, 'xd_confirm_allocation'):
            rt.call('xd_execute', {'vehicle_id': 'uav1', 'plan_id': self.pid})
        for station in self.stations.values():
            station.submit.assert_not_called()

    def test_unverified_model_answer_cannot_report_confirmation(self):
        model=Mock(return_value={'message':{'content':'两机已经起飞。'}})
        agent=Agent(self.directory,model,'test',self.env,think=False,runtime_factory=FleetRuntime,
                    registry=self.env['catalog'])
        conv,_=agent._load('test-confirm')
        conv.update(self.conversation)
        result=agent.chat(dict(message='确认执行刚才的集群方案。',conversation_id='test-confirm'))
        self.assertTrue(1 < model.call_count <= 2)
        self.assertFalse(result['submitted'])
        self.assertEqual(result['source'],'controller')
        self.assertNotIn('已经起飞', result['reply'])
        self.check.assert_not_called()
