"""Execute auction output in PX4 and verify waypoint visits from telemetry."""
import argparse
import json
import math
from pathlib import Path
import time
import urllib.request

from packages.task_allocation.cbba import allocate, inspection_tasks
from .mavlink_io import M, Vehicle, to_global, to_local
from .observations import flight_observation
from .sitl import DEFAULT_RUN


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def mission(spec, task_ids, tasks, origin, airborne=False):
    alt = spec['cruise_altitude_m']
    lat, lon = to_global(350, spec['north_m'], origin)
    items = [] if airborne else [{'command': M.MAV_CMD_NAV_TAKEOFF, 'p1': 15, 'lat': lat, 'lon': lon, 'alt': alt}]
    for task_id in task_ids:
        task = next(t for t in tasks if t['id'] == task_id)
        lat, lon = to_global(task['east_m'], task['north_m'], origin)
        items.append({'command': M.MAV_CMD_NAV_WAYPOINT, 'p2': 65, 'lat': lat, 'lon': lon,
                      'alt': alt, 'task_id': task_id})
    # Hold after the inspection route. No landing controller is part of this step.
    items.append({'command': M.MAV_CMD_NAV_LOITER_UNLIM, 'p3': 110,
                  'lat': lat, 'lon': lon, 'alt': alt})
    # PX4 fixed-wing feasibility requires a valid landing pattern. It remains
    # after unlimited loiter, so this verification still ends in an airborne hold.
    lat, lon = to_global(2300, spec['north_m'], origin)
    items.append({'command': M.MAV_CMD_DO_LAND_START, 'frame': M.MAV_FRAME_MISSION,
                  'lat': lat, 'lon': lon, 'alt': 80})
    items.append({'command': M.MAV_CMD_NAV_WAYPOINT, 'p2': 65, 'lat': lat, 'lon': lon, 'alt': 80})
    lat, lon = to_global(4000, spec['north_m'], origin)
    items.append({'command': M.MAV_CMD_NAV_LAND, 'lat': lat, 'lon': lon, 'alt': 0})
    return items


def run(run_dir=DEFAULT_RUN, timeout=600, forward=True, hold=False):
    run_dir = Path(run_dir)
    meta = json.loads((run_dir/'processes.json').read_text())
    aircraft = meta['aircraft']
    tasks = inspection_tasks()
    report = {'kind': 'PX4 v1.13.3 + Gazebo Classic fixed-wing flight verification',
              'started_at': time.time(), 'origin': meta['origin'], 'tasks': tasks,
              'success': False, 'aircraft': {},
              'limits': ['flat simulated ground; real mountain terrain not imported',
                         'CBBA consensus emulated in one ground process',
                         'altitude separation only; no dynamic avoidance implemented',
                         'waypoint arrival verifies routing, not disaster recognition']}
    vehicles = [Vehicle(a) for a in aircraft]
    telemetry = (run_dir/'telemetry.jsonl').open('w')
    try:
        for v in vehicles:
            hb = v.wait('HEARTBEAT', timeout=30)
            if hb.type != M.MAV_TYPE_FIXED_WING or hb.autopilot != M.MAV_AUTOPILOT_PX4:
                raise RuntimeError('Expected PX4 fixed-wing simulator')
            pos = v.wait('GLOBAL_POSITION_INT', timeout=30)
            v.spec['east_m'], v.spec['north_m'] = to_local(pos.lat/1e7, pos.lon/1e7, meta['origin'])
            report['aircraft'][v.spec['id']] = {'mission_accepted': False, 'visited': {},
                'minimum_distance_m': {}, 'max_relative_alt_m': 0, 'max_speed_mps': 0,
                'mode': 'initializing', 'armed': False}
        allocation = allocate(aircraft, tasks)
        if allocation['unassigned']:
            raise RuntimeError('Not all tasks could be allocated')
        report['allocation'] = allocation
        print('ALLOCATION', json.dumps(allocation['assignments']), flush=True)
        for v in vehicles:
            for name, value in [('COM_RC_IN_MODE', 1), ('COM_RCL_EXCEPT', 4)]:
                v.set_param(name, value)
            items = mission(v.spec, allocation['assignments'][v.spec['id']], tasks, meta['origin'])
            a = report['aircraft'][v.spec['id']]
            a['mission'] = items
            a['upload'] = v.upload(items)
            a['mission_accepted'] = True
            print(v.spec['id'], 'mission accepted', flush=True)
        time.sleep(1.5)  # Navigator performs feasibility checks after protocol ACK.
        for v in vehicles:
            a = report['aircraft'][v.spec['id']]
            a['mode_ack'] = v.command(M.MAV_CMD_DO_SET_MODE, M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, 4, 4)
            a['arm_ack'] = v.command(M.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            print(v.spec['id'], 'armed, AUTO.MISSION', flush=True)
        began = time.monotonic()
        last_print = 0
        while time.monotonic()-began < timeout or (hold and report['success']):
            now = time.time()
            for v in vehicles:
                state = v.snapshot()
                pos = state.get('GLOBAL_POSITION_INT')
                hb = state.get('HEARTBEAT', {})
                if not pos or time.monotonic()-state.get('position_received_at', 0) > 2:
                    continue
                east, north = to_local(pos['lat']/1e7, pos['lon']/1e7, meta['origin'])
                alt, speed = pos['relative_alt']/1000, math.hypot(pos['vx'], pos['vy'])/100
                a = report['aircraft'][v.spec['id']]
                a['max_relative_alt_m'] = max(a['max_relative_alt_m'], alt)
                a['max_speed_mps'] = max(a['max_speed_mps'], speed)
                a['armed'] = bool(hb.get('base_mode', 0) & M.MAV_MODE_FLAG_SAFETY_ARMED)
                a['mode'] = hb.get('custom_mode', 0)
                a['mission_current'] = state.get('MISSION_CURRENT', {}).get('seq')
                a['last_reached_seq'] = state.get('MISSION_ITEM_REACHED', {}).get('seq')
                sample = {'timestamp': now, 'id': v.spec['id'], 'east_m': east, 'north_m': north,
                          'relative_alt_m': alt, 'speed_mps': speed,
                          'lat': pos['lat']/1e7, 'lon': pos['lon']/1e7,
                          'altitude_m': pos['alt']/1000, 'heading_deg': pos['hdg']/100,
                          'armed': a['armed'], 'mission_current': a['mission_current'],
                          'last_reached_seq': a['last_reached_seq']}
                telemetry.write(json.dumps(sample)+'\n')
                battery = state.get('SYS_STATUS', {}).get('battery_remaining')
                report.setdefault('observations', {})[v.spec['id']] = flight_observation(v.spec['id'], sample, battery)
                for task_id in allocation['assignments'][v.spec['id']]:
                    task = next(t for t in tasks if t['id'] == task_id)
                    distance = math.hypot(east-task['east_m'], north-task['north_m'])
                    a['minimum_distance_m'][task_id] = min(distance, a['minimum_distance_m'].get(task_id, float('inf')))
                    if distance <= 85 and alt > 50 and speed > 10 and task_id not in a['visited']:
                        seq = allocation['assignments'][v.spec['id']].index(task_id)+1
                        # Both physical arrival and PX4 navigation progress are required.
                        passed_by_px4 = ((a['mission_current'] is not None and a['mission_current'] > seq)
                                         or (a['last_reached_seq'] is not None and a['last_reached_seq'] >= seq))
                        if passed_by_px4:
                            a['visited'][task_id] = {'timestamp': now, 'distance_m': distance,
                                                    'relative_alt_m': alt, 'speed_mps': speed,
                                                    'mission_seq': seq}
                            print(v.spec['id'], 'VISITED', task_id, round(distance, 1), flush=True)
                if forward:
                    packet = {'id': v.spec['id'], 'latitude': sample['lat'], 'longitude': sample['lon'],
                              'altitude_m': sample['altitude_m'], 'heading_deg': sample['heading_deg'],
                              'speed_mps': speed, 'battery_pct': battery if battery is not None and battery >= 0 else None,
                              'source': 'px4', 'altitude_reference': 'AMSL', 'timestamp': now,
                              'flight_mode': mav_mode(hb.get('custom_mode', 0)),
                              'task': ' / '.join(allocation['assignments'][v.spec['id']])}
                    try:
                        request = urllib.request.Request('http://127.0.0.1:11843/api/telemetry',
                                  json.dumps(packet).encode(), {'Content-Type': 'application/json'})
                        urllib.request.urlopen(request, timeout=0.3).close()
                    except Exception:
                        pass
            telemetry.flush()
            report['elapsed_s'] = round(time.monotonic()-began, 2)
            passed = all(len(a['visited']) == len(allocation['assignments'][aid])
                         for aid, a in report['aircraft'].items())
            if passed and not report['success']:
                report['completed_at'] = now
                report['verification_duration_s'] = report['elapsed_s']
                print('PASS: all assigned inspection points physically reached and PX4-confirmed', flush=True)
            report['success'] = passed
            write_json(run_dir/'flight-report.json', report)
            write_json(run_dir/'status.json', {'report': report, 'telemetry_timestamp': now})
            if report['elapsed_s']-last_print > 15:
                last_print = report['elapsed_s']
                print('FLIGHT', round(report['elapsed_s']),
                      {k: {'alt': round(a['max_relative_alt_m']), 'visited': len(a['visited']),
                           'seq': a['mission_current']} for k, a in report['aircraft'].items()}, flush=True)
            if report['success'] and not hold:
                break
            time.sleep(0.5)
        return report
    except Exception as exc:
        report['error'] = str(exc)
        write_json(run_dir/'flight-report.json', report)
        raise
    finally:
        telemetry.close()
        for v in vehicles:
            v.close()


def mav_mode(custom_mode):
    main, sub = (custom_mode >> 16) & 255, (custom_mode >> 24) & 255
    if main == 4:
        return {4: 'AUTO.MISSION', 3: 'AUTO.LOITER', 5: 'AUTO.RTL'}.get(sub, 'AUTO.%d' % sub)
    return 'PX4.%d' % main


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--hold', action='store_true', help='Keep telemetry/heartbeat alive after success until Ctrl+C')
    args = parser.parse_args()
    report = run(args.run, args.timeout, hold=args.hold)
    raise SystemExit(0 if report['success'] else 1)
