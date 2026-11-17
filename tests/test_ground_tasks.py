import unittest
from packages.ground_core.tasks import normalize_targets,ground_targets
from packages.swarm_sim.auction import allocate


class GroundTaskTests(unittest.TestCase):
    def target(self,x=1100,y=50,**changes):
        return dict(label='检查点',frame='local',x=x,y=y,zone_id='',priority='normal',sensor='rgb',**changes)

    def normalize(self,targets,text='东1100米、北50米'):
        return normalize_targets({'targets':targets,'aircraft_ids':[]},{'lon':103.565,'lat':31.045},[],text)

    def test_unmentioned_coordinates_rejected(self):
        with self.assertRaises(ValueError):self.normalize([self.target(900,0)])

    def test_pairing_cannot_be_recombined_by_model(self):
        with self.assertRaises(ValueError):self.normalize([self.target(1100,400)],'东1100米、北50米；东1350米、北400米')

    def test_unknown_and_nonfinite_positions_rejected(self):
        for x in [float('nan'),float('inf'),True]:
            with self.assertRaises(ValueError):self.normalize([self.target(x,50)])

    def test_no_fallback_to_old_six_points(self):
        tasks=self.normalize([self.target()])
        self.assertEqual([(t['east_m'],t['north_m']) for t in tasks],[(1100,50)])
        self.assertTrue(all(t['source']=='qwen_user_request' for t in tasks))
        with self.assertRaises(ValueError):self.normalize([])

    def test_three_coordinates_mislabeled_as_zones_remain_three_points(self):
        pairs=[(1100,50),(1350,400),(1650,750)]
        text='坐标以仿真原点为基准：东1100米、北50米；东1350米、北400米；东1650米、北750米'
        targets=[]
        for x,y in pairs:
            target=self.target(x,y);target.update(frame='zone',zone_id='S')
            targets.append(target)
        tasks=self.normalize(targets,text)
        self.assertEqual([(t['east_m'],t['north_m']) for t in tasks],pairs)
        self.assertTrue(all(t['request_target']['frame']=='local' for t in tasks))

    def test_unmentioned_coordinates_cannot_repair_a_zone(self):
        target=self.target(900,0);target.update(frame='zone',zone_id='S')
        with self.assertRaises(ValueError):ground_targets([target],'东1100米、北50米')

    def test_current_area_request_does_not_reuse_previous_point_instruction(self):
        target=self.target();target.update(frame='zone',zone_id='S')
        with self.assertRaises(ValueError):
            normalize_targets({'targets':[target]}, {'lon':103.565,'lat':31.045},[],
                source_text='之前：东1100米、北50米；现在只巡检 S 区',instruction_text='现在只巡检 S 区')

    def test_same_task_changes_owner_when_nearest_aircraft_has_low_battery(self):
        tasks=self.normalize([self.target()])
        fleet=[dict(id='near',east_m=1000,north_m=50,battery_pct=80),
               dict(id='far',east_m=0,north_m=50,battery_pct=80)]
        initial=allocate(fleet,tasks,1)
        self.assertEqual(initial['assignments']['near'],[tasks[0]['id']])
        fleet[0]['battery_pct']=20
        changed=allocate(fleet,tasks,1)
        self.assertNotIn('near',changed['assignments'])
        self.assertEqual(changed['assignments']['far'],[tasks[0]['id']])

    def test_unknown_battery_and_stale_aircraft_cannot_win(self):
        tasks=self.normalize([self.target()])
        fleet=[dict(id='unknown',east_m=1000,north_m=50,battery_pct=None),
               dict(id='stale',east_m=1000,north_m=50,battery_pct=80,available=False)]
        self.assertEqual(allocate(fleet,tasks,1)['unassigned'],[tasks[0]['id']])

    def test_sensor_requirement_limits_eligible_aircraft(self):
        target=self.target();target['sensor']='thermal'
        tasks=self.normalize([target])
        fleet=[dict(id='rgb',east_m=1100,north_m=50,battery_pct=90,capabilities=['rgb']),
               dict(id='thermal',east_m=0,north_m=50,battery_pct=90,capabilities=['rgb','thermal'])]
        plan=allocate(fleet,tasks,1)
        self.assertEqual(plan['assignments']['rgb'],[])
        self.assertEqual(plan['assignments']['thermal'],[tasks[0]['id']])

    def test_battery_also_changes_bid_before_low_threshold(self):
        tasks=self.normalize([self.target()])
        fleet=[dict(id='A',east_m=1000,north_m=50,battery_pct=90),dict(id='B',east_m=1000,north_m=50,battery_pct=30)]
        plan=allocate(fleet,tasks,1)
        self.assertEqual(plan['assignments']['A'],[tasks[0]['id']])
        fleet[0]['battery_pct'],fleet[1]['battery_pct']=30,90
        self.assertEqual(allocate(fleet,tasks,1)['assignments']['B'],[tasks[0]['id']])


if __name__=='__main__':unittest.main()
