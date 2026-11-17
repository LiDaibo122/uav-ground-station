import importlib.util
import math
from pathlib import Path
import unittest
from unittest.mock import patch,MagicMock
import json

spec=importlib.util.spec_from_file_location('ground_app',Path(__file__).resolve().parents[1]/'server/app.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)


class GroundStationTests(unittest.TestCase):
    def setUp(self):app.LIVE.clear()

    def telemetry(self,**changes):
        return dict(id='UAV-01',longitude=103.565,latitude=31.045,altitude_m=2600,heading_deg=25,**changes)

    def test_telemetry_rejects_nonfinite_and_invalid_position(self):
        for field,value in [('latitude',float('nan')),('longitude',181),('altitude_m',True),('heading_deg',-1)]:
            data=self.telemetry();data[field]=value
            with self.assertRaises(ValueError):app.validate_telemetry(data)

    def test_unknown_battery_does_not_become_zero(self):
        self.assertIsNone(app.validate_telemetry(self.telemetry())['battery_pct'])

    def test_demo_never_reports_px4_connected(self):
        state=app.demo_state(15)
        self.assertFalse(state['px4_connected'])
        self.assertEqual(len(state['aircraft']),3)
        self.assertTrue(all(a['source']=='demo' for a in state['aircraft']))

    def test_live_does_not_fall_back_to_demo(self):
        self.assertEqual(app.live_state()['aircraft'],[])

    def test_stale_telemetry_is_not_reported_connected(self):
        item=app.validate_telemetry(self.telemetry())
        item['_received_monotonic']=0
        app.LIVE[item['id']]=item
        with patch.object(app.time,'monotonic',return_value=10):
            state=app.live_state()
        self.assertFalse(state['px4_connected'])
        self.assertTrue(state['aircraft'][0]['stale'])

    def test_invalid_draft_cannot_enter_ledger(self):
        draft=dict(title='巡检',area='A',task_type='execute_motor',priority='normal',notes='',aircraft_ids=[])
        with self.assertRaises(ValueError):app.validate_draft(draft)



if __name__=='__main__':unittest.main()
