import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from packages.ground_core.inspection_rehearsal import Rehearsal


class InspectionRehearsalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.session = str(uuid4())
        self.engine = Rehearsal(tmp.name)
        self.revision = 0
        self.clock = patch('packages.ground_core.inspection_rehearsal.time.time',return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.point = dict(kind='point',east=10,north=-30)
        self.area = dict(kind='area',west=2,east=18,south=-36,north=-24)

    def run_op(self, op, **kwargs):
        d = dict(session=self.session,revision=self.revision,op=op)
        state = self.engine.request(dict(d,**kwargs),'',None)
        self.revision = state['revision']
        return state

    def test_ack_wait_restart_and_stale_confirmation(self):
        self.run_op('draw',shapes=[self.area,self.point,self.point])
        self.run_op('plan')
        first=self.run_op('start')['lanes']['sim1']['current']
        self.run_op('wait_after')
        self.now.return_value=191
        waiting=self.run_op('read')
        self.assertEqual(waiting['lanes']['sim1']['phase'],'waiting')
        self.assertEqual(waiting['tasks'][first]['status'],'completed')
        next_id=self.run_op('resume')['lanes']['sim1']['current']
        rev=self.revision
        self.run_op('skip')
        with self.assertRaisesRegex(ValueError,'已变化'):
            self.run_op('skip',revision=rev)
        self.assertEqual(self.run_op('read')['lanes']['sim1']['current'],next_id)
        self.now.return_value=194
        after=self.run_op('read')
        self.assertEqual(after['tasks'][next_id]['status'],'skipped')
        self.assertNotEqual(after['lanes']['sim1']['current'],next_id)
        self.now.return_value=300
        self.assertEqual(self.run_op('read')['lanes']['sim1']['phase'],'finished')

    def test_incident_merge_and_offline_reallocation(self):
        for source in ('sim1','sim2'):
            self.run_op('report',location=self.point,source=source,note='观察到积水')
        self.run_op('draw',count=2,shapes=[self.point,self.area])
        s=self.run_op('plan')
        self.assertEqual(len(s['incidents']['W1']['reports']),2)
        self.assertTrue(all(t['priority']=='high' for t in s['tasks'].values()))
        owner=next(uid for uid,lane in s['lanes'].items() if lane['queue'])
        self.run_op('start',vehicle=owner)
        s=self.run_op('offline',vehicle=owner)
        assigned=[tid for lane in s['lanes'].values() for tid in lane['queue']]
        self.assertCountEqual(assigned,['T1','T2'])
        self.run_op('review',incident='W1')
        s=self.run_op('plan')
        self.assertTrue(all(t['priority']=='normal' for t in s['tasks'].values()))

    def test_geometry_atomic_and_preview_is_read_only(self):
        with self.assertRaises(ValueError):
            self.run_op('draw',shapes=[self.point,dict(self.area,north=-99)])
        self.assertFalse(self.run_op('read')['tasks'])
        self.run_op('draw',shapes=[self.area])
        seen=[]
        self.engine.request(dict(session=self.session,op='fleet_preview',revision=self.revision),
                            '',None,lambda rows:seen.extend(rows))
        self.assertEqual(seen[0]['west'],2)
        self.assertEqual(self.run_op('read')['events'][-1]['text'],'已登记地图标绘 1 项')

    def test_restriction_excludes_intersecting_rectangle_and_boundary_point(self):
        self.run_op('draw',shapes=[self.point,self.area,dict(self.point,east=11,north=15)])
        self.run_op('restrict',location=dict(kind='area',west=10,east=11,south=-30,north=-29))
        s=self.run_op('plan')
        self.assertEqual(s['lanes']['sim1']['queue'],['T3'])
        self.assertEqual(s['tasks']['T1']['blocked_by'],['Z1'])
        self.assertEqual(s['tasks']['T2']['blocked_by'],['Z1'])

    def test_points_before_area_across_all_lanes(self):
        other=dict(self.point,north=120)
        s=self.run_op('draw',count=2,shapes=[self.point,dict(self.area,label='河堤东段'),other])
        self.assertEqual([t['label'] for t in s['tasks'].values()],['任务点1','河堤东段','任务点2'])
        s=self.run_op('points_then_areas')
        self.assertEqual(s['stage'],'points')
        for uid,lane in s['lanes'].items():
            self.assertEqual(len(lane['queue']),1)
            self.now.return_value+=10
            self.run_op('start',vehicle=uid)
        self.now.return_value=155
        s=self.run_op('read')
        self.assertEqual(s['tasks']['T2']['status'],'pending')
        self.now.return_value=165
        s=self.run_op('read')
        self.assertEqual(s['stage'],'areas')
        self.assertEqual([t['status'] for t in s['tasks'].values()],['completed','running','completed'])
        self.assertEqual(sum(l['current']=='T2' for l in s['lanes'].values()),1)
        self.now.return_value+=91
        self.assertEqual(self.run_op('read')['tasks']['T2']['status'],'completed')
