import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from packages.swarm_sim.monitor import Source, render, Mavros, mavros_lines


def fixture(now=1000):
    state = {'aircraft': [dict(id='FW-01', age_s=.2, stale=False, armed=False, landed=True,
        flight_mode='AUTO.LOITER', longitude=103.565, latitude=31.045, altitude_m=1000,
        relative_alt_m=0, battery_pct=0, speed_mps=0)]}
    simulation = {'status': 'ready', 'ready': True, 'fleet': [dict(id='FW-01', east_m=0,
        north_m=0, observed_timestamp=now-.2, battery_telemetry_age_s=.3)],
        'report': {'status': 'waiting_for_task', 'session_id': 'abc123', 'tasks': [], 'aircraft': {}}}
    from packages.swarm_sim.monitor import merge_aircraft
    return dict(source='http', now=now, state=state, simulation=simulation,
                aircraft=merge_aircraft(state, simulation), warnings=[])


class MonitorTests(unittest.TestCase):
    def test_model_token_counters_are_distinct_from_packing_estimates(self):
        agent={'status':'available','age_s':0,'conversation':{'id':'test-context','turns':[],
            'last_model_usage':[{'prompt_eval_count':4351,'eval_count':250,'num_ctx':16384,
                'reserved_generation_tokens':4096,'done_reason':'stop',
                'packing':{'estimated_input_tokens':9500,'history_turns_omitted':2}}]}}
        output=render(fixture(),'agent',agent=agent)
        self.assertIn('实际用量：输入 4351 / 窗口 16384',output)
        self.assertIn('生成 250',output)
        self.assertIn('估算输入 9500 token（估算值）',output)

    def test_ground_loiter_is_not_reported_airborne(self):
        output = render(fixture(), 'state')
        self.assertIn('connected=true armed=false mode=AUTO.LOITER landed=true', output)
        self.assertIn('地面/未解锁', output)
        self.assertIn('实际数据源为直接 MAVLink', output)
        self.assertNotIn('已离地', output)

    def test_position_combines_local_and_global_coordinates(self):
        output = render(fixture(), 'position')
        self.assertIn('103.5650000, 31.0450000', output)
        self.assertIn('ENU 东 0.0m 北 0.0m', output)
        self.assertIn('AMSL 1000.0m 相对起飞点 0.0m', output)

    def test_zero_battery_is_preserved_but_stale_or_unknown_is_unknown(self):
        snapshot = fixture()
        self.assertIn('电量 0%', render(snapshot, 'battery'))
        snapshot['aircraft'][0]['battery_pct'] = None
        self.assertIn('电量 UNKNOWN', render(snapshot, 'battery'))
        snapshot['aircraft'][0].update(battery_pct=78, battery_telemetry_age_s=4)
        self.assertIn('电量 UNKNOWN', render(snapshot, 'battery'))
        snapshot['aircraft'][0].update(battery_telemetry_age_s=.2, age_s=20, observed_timestamp=980)
        self.assertIn('[STALE', render(snapshot, 'battery'))
        self.assertIn('电量 UNKNOWN', render(snapshot, 'battery'))

    def test_acceptance_is_not_task_completion(self):
        snapshot = fixture()
        snapshot['simulation']['report'].update(status='executing', success=False,
            tasks=[dict(id='p1', label='巡检点1'), dict(id='p2', label='巡检点2')],
            allocation=dict(algorithm_id='cbba', assignments={'FW-01': ['p1', 'p2']}),
            aircraft={'FW-01': dict(mission_accepted=True, mode_ack={'result': 0},
                visited={'p1': {}}, mission_current=2)})
        output = render(snapshot, 'mission')
        self.assertIn('到点 1/2 | success=false', output)
        self.assertIn('upload=true mode_ACK=0', output)
        self.assertIn('visited=1/2 seq=2', output)
        self.assertIn('ACK只表示接收', output)

    def test_http_failure_fallback_is_never_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'runs/interactive/abc123').mkdir(parents=True)
            report = {'session_id': 'abc123', 'status': 'completed', 'success': True,
                      'aircraft': {'FW-01': {'mode': (4 << 16)+(3 << 24)}}, 'tasks': []}
            (root/'runs/interactive-latest.json').write_text(json.dumps(report))
            (root/'runs/interactive/abc123/telemetry.jsonl').write_text(json.dumps(dict(
                id='FW-01', timestamp=time.time(), lat=31.045, lon=103.565, armed=True,
                landed=False, relative_alt_m=120))+'\n')
            source = Source(root)
            with patch.object(source, 'get_json', side_effect=OSError('offline')):
                snapshot = source.snapshot()
            output = render(snapshot, 'state')
            self.assertEqual(snapshot['source'], 'file')
            self.assertIn('HISTORY', output)
            self.assertIn('connected=UNKNOWN', output)
            self.assertIn('HTTP不可达，未确认当前连接', output)
            self.assertNotIn('[LIVE', output)

    def test_partial_http_failure_does_not_mix_historical_mission(self):
        source = Source()
        def read(path):
            if path.startswith('/api/state'):
                return fixture()['state']
            raise OSError('unavailable')
        with patch.object(source, 'get_json', side_effect=read):
            snapshot = source.snapshot()
        self.assertEqual(snapshot['source'], 'http')
        self.assertEqual(snapshot['simulation'], {})
        self.assertIn('任务 UNKNOWN', render(snapshot, 'mission'))

    def test_state_coordinates_and_age_are_not_overwritten_by_newer_fleet_age(self):
        from packages.swarm_sim.monitor import merge_aircraft, record_age
        values = fixture()
        values['state']['aircraft'][0].update(age_s=8, stale=True, longitude=103.566)
        values['simulation']['report']['origin'] = {'lon': 103.565, 'lat': 31.045}
        values['simulation']['fleet'][0].update(observed_timestamp=999.9, east_m=900)
        merged = merge_aircraft(values['state'], values['simulation'])
        self.assertEqual(record_age(merged[0], 1000), 8)
        self.assertGreater(merged[0]['east_m'], 90)
        self.assertLess(merged[0]['east_m'], 100)
        values['aircraft'] = merged
        self.assertIn('[STALE', render(values, 'state'))
        self.assertIn('不代表全部在线', render(values, 'state'))

    def test_previous_session_state_never_marks_current_fleet_live(self):
        from packages.swarm_sim.monitor import merge_aircraft
        values = fixture()
        values['state']['aircraft'][0]['session_id'] = 'previous'
        values['simulation']['id'] = 'current'
        values['aircraft'] = merge_aircraft(values['state'], values['simulation'])
        self.assertIn('[STALE', render(values, 'state'))
        self.assertNotIn('connected=true', render(values, 'state'))

    def test_latest_agent_repetition_and_error_are_visible_without_full_prompt(self):
        conversation = {'id': 'case1234', 'turns': [[
            {'role': 'user', 'content': '检查区域'},
            {'role': 'assistant', 'tool_calls': [{'function': {'name': 'plan_inspection'}}]},
            {'role': 'tool', 'tool_name': 'plan_inspection', 'content': json.dumps({'ok': False, 'error': '超出2.5公里'})}]]}
        output = render(fixture(), 'agent', {'status': 'available', 'age_s': 1, 'conversation': conversation})
        self.assertIn('CALL plan_inspection', output)
        self.assertIn('plan_inspection [error] 超出2.5公里', output)

    def test_ros_master_failure_is_distinct_from_drone_disconnection(self):
        with patch('xmlrpc.client.ServerProxy', side_effect=OSError('no master')):
            result = Mavros().snapshot()
        text = '\n'.join(mavros_lines(result))
        self.assertIn('ROS Master 未连接', text)
        self.assertIn('不能将此状态解释为飞机掉线', text)


if __name__ == '__main__':
    unittest.main()
