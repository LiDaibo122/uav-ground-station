"""Deterministic sensor-to-text adapter, not an implemented world model."""
from datetime import datetime, timezone


def flight_observation(aircraft_id, sample, battery_pct=None):
    observed_at = datetime.fromtimestamp(sample['timestamp'], timezone.utc).isoformat()
    battery = '未知' if battery_pct is None or battery_pct < 0 else '%d%%' % battery_pct
    flight_state='未解锁' if sample.get('armed') is False else '已解锁' if sample.get('armed') is True else '解锁状态未知'
    flight_state+='，在地面' if sample.get('landed') is True else '，已离地' if sample.get('landed') is False else '，离地状态未知'
    return {'aircraft_id': aircraft_id, 'kind': 'flight_state', 'observed_at': observed_at,
            'source': 'PX4 SITL MAVLink telemetry', 'simulated': True,
            'text': flight_state+'。'+('%s：经度 %.6f，纬度 %.6f；AMSL 高度 %.1f 米，相对起飞点高度 %.1f 米；'
                     '地速 %.1f 米/秒，航向 %.1f 度；电量 %s；当前任务序号 %s。'
                     '此信息仅描述仿真飞行状态，没有灾害识别结果。') %
                    (aircraft_id, sample['lon'], sample['lat'], sample['altitude_m'],
                     sample['relative_alt_m'], sample['speed_mps'], sample['heading_deg'],
                     battery, sample['mission_current']),
            'disaster_detections': []}
