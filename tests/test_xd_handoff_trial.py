import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

try:
    from packages.adapters.xd_handoff_trial import RiverTrial, LEAD, SPARE
except ImportError as exc:
    raise unittest.SkipTest(str(exc))


class HandoffTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory=Path(tmp.name)
        self.stations={}
        for uid in (LEAD,SPARE):
            st=Mock(run_id='test',history=[],active=False)
            st.snapshot.return_value=dict(connected=True,can_takeoff=True,armed=False,flight_phase='on_ground',battery_pct=90)
            self.stations[uid]=st
        self.trial=RiverTrial(self.stations,self.directory)
        self.sent=[]

    def flight(self,uid,action,index=None):
        self.sent.append((uid,action,index))
        st=self.stations[uid]
        st.history.append({'action':action})
        landing=action=='land_home'
        st.snapshot.return_value.update(can_takeoff=landing,armed=not landing,
                                        flight_phase='on_ground' if landing else 'in_air')
        if (uid,action,index)==(LEAD,'inspection',1):
            self.trial.state['recall']=True
        return str(len(self.sent))

    def test_transfer_only_uncollected_after_landing(self):
        self.trial.fly=self.flight
        self.trial.run()
        self.assertEqual([t['receipt']['vehicle'] for t in self.trial.state['samples']],[LEAD,SPARE,SPARE])
        self.assertEqual(self.sent,[(LEAD,'inspection',0),(LEAD,'inspection',1),(LEAD,'land_home',None),
                                    (SPARE,'inspection',1),(SPARE,'inspection',2),(SPARE,'land_home',None)])
        self.assertEqual(self.trial.state['landing_receipts'],{LEAD:'3',SPARE:'6'})
        self.assertEqual(RiverTrial(self.stations,self.directory).state['phase'],'complete')

    def test_unavailable_spare_and_uncertain_restart(self):
        self.trial.fly=self.flight
        self.stations[SPARE].snapshot.return_value['battery_pct']=12
        self.trial.run()
        self.assertEqual(self.trial.state['phase'],'stopped')
        self.assertFalse(any(uid==SPARE for uid,_,_ in self.sent))
        self.assertEqual(sum(t['receipt'] is not None for t in self.trial.state['samples']),1)
        self.trial.record('returning','等待回执')
        recovered=RiverTrial(self.stations,self.directory)
        self.assertEqual(recovered.state['phase'],'unknown')
        self.assertEqual(json.loads(recovered.path.read_text())['phase'],'unknown')

    def test_landing_failure_does_not_release_work(self):
        def fail_landing(uid,action,index=None):
            if action=='land_home':
                raise ValueError('未收到返航完成回执')
            return self.flight(uid,action,index)
        self.trial.fly=fail_landing
        self.trial.run()
        self.assertEqual(self.trial.state['phase'],'stopped')
        self.assertFalse(any(uid==SPARE for uid,_,_ in self.sent))
        self.assertTrue(all(s['owner']==LEAD for s in self.trial.state['samples']))

    def test_select_uav1_as_primary(self):
        self.trial.state['primary']=SPARE
        for s in self.trial.state['samples']:
            s['owner']=SPARE
        def fly(uid,action,index=None):
            if uid==SPARE and index==1:
                self.trial.state['recall']=True
            return self.flight(uid,action,index)
        self.trial.fly=fly
        self.trial.run()
        self.assertEqual(self.trial.state['phase'],'complete')
        self.assertEqual([s['receipt']['vehicle'] for s in self.trial.state['samples']],[SPARE,LEAD,LEAD])
