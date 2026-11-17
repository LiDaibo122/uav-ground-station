"""Launch/stop only the processes owned by this isolated simulation run."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = ROOT / "runs" / "swarm"


def launch(run=DEFAULT_RUN, px4=Path('/home/ldb/PX4-Autopilot'), count=3, instance_offset=1):
    run = Path(run).resolve()
    run.mkdir(parents=True, exist_ok=True)
    if (run / 'processes.json').exists():
        old = json.loads((run / 'processes.json').read_text())
        if any(Path('/proc', str(p['pid'])).exists() for p in old['processes']):
            raise RuntimeError('This run still has processes; stop it first.')
    build = px4 / 'build/px4_sitl_default'
    assets = px4 / 'Tools/sitl_gazebo'
    env = dict(os.environ)
    env.update(GAZEBO_MASTER_URI='http://127.0.0.1:11359',
               GAZEBO_MODEL_DATABASE_URI='', PX4_SIM_MODEL='plane',
               PX4_ESTIMATOR='ekf2', PX4_HOME_LAT='31.045',
               PX4_HOME_LON='103.565', PX4_HOME_ALT='1000')
    # rcS explicitly supports user px4-rc.params in PATH. Configure before the
    # sensors/controllers start, rather than mutating parameters while flying.
    # Tasks remain bounded to a 2.5 km origin radius. Homes are up to 700 m
    # apart, so a 3.5 km first-waypoint guard covers every valid airborne task.
    (run/'px4-rc.params').write_text('param set-default COM_RC_IN_MODE 1\nparam set-default COM_RCL_EXCEPT 4\nparam set-default MIS_DIST_1WP 3500\n')
    env['PATH']=str(run)+':'+env.get('PATH','/usr/bin:/bin')
    for key, value in [('GAZEBO_MODEL_PATH', assets / 'models'),
                       ('GAZEBO_PLUGIN_PATH', build / 'build_gazebo'),
                       ('LD_LIBRARY_PATH', build / 'build_gazebo')]:
        env[key] = str(value) + (':' + env[key] if env.get(key) else '')
    world = ET.parse(str(assets / 'worlds/empty.world'))
    aircraft = []
    for slot in range(count):
        i=slot+instance_offset
        sdf = run / ('plane_%d.sdf' % i)
        subprocess.run(['python3', str(assets / 'scripts/jinja_gen.py'),
                        str(assets / 'models/plane/plane.sdf.jinja'), str(assets),
                        '--mavlink_tcp_port', str(4560+i),
                        '--mavlink_udp_port', str(14560+i),
                        '--mavlink_id', str(1+i), '--gst_udp_port', str(5600+i),
                        '--video_uri', str(5600+i), '--mavlink_cam_udp_port', str(14530+i),
                        '--output-file', str(sdf)], check=True, capture_output=True)
        model = ET.parse(str(sdf)).getroot().find('model')
        model.set('name', 'plane_%d' % i)
        pose = model.find('pose')
        if pose is None:
            pose = ET.SubElement(model, 'pose')
        pose.text = '0 %d 0.83 0 0 0' % (slot*350)
        world.getroot().find('world').append(model)
        aircraft.append({'id': 'FW-%02d' % (slot+1), 'instance': i,
                         'system_id': i+1, 'port': 14540+i,
                         'east_m': 0, 'north_m': slot*350,
                         'cruise_altitude_m': 120+slot*50})
    # Gazebo's own GUI follows the first simulated aircraft. The browser gives
    # the fleet overview while this camera shows the actual airframe motion.
    gui=ET.SubElement(world.getroot().find('world'),'gui',{'fullscreen':'false'})
    camera=ET.SubElement(gui,'camera',{'name':'user_camera'})
    ET.SubElement(camera,'pose').text='-14 -18 8 0 0.35 0.9'
    ET.SubElement(camera,'view_controller').text='orbit'
    ET.SubElement(camera,'projection_type').text='perspective'
    tracking=ET.SubElement(camera,'track_visual')
    for name,value in [('name','plane_%d'%instance_offset),('static','true'),
                       ('use_model_frame','true'),('xyz','-14 -18 8'),('inherit_yaw','true')]:
        ET.SubElement(tracking,name).text=value
    world_path = run / 'swarm.world'
    world.write(str(world_path), encoding='unicode', xml_declaration=True)
    meta = {'run': str(run), 'px4': str(px4), 'count': count,
            'origin': {'lat': 31.045, 'lon': 103.565, 'altitude_m': 1000},
            'world': 'flat ODE physics; no real terrain imported',
            'aircraft': aircraft, 'processes': [], 'started_at': time.time()}
    try:
        for a in aircraft:
            wd = run / ('instance_%d' % a['instance'])
            wd.mkdir(exist_ok=True)
            cmd = [str(build / 'bin/px4'), '-i', str(a['instance']), '-d',
                   '-w', str(wd), '-s', 'etc/init.d-posix/rcS', str(build / 'etc')]
            with (wd / 'px4.log').open('w') as log:
                p = subprocess.Popen(cmd, env=env, cwd=wd, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            meta['processes'].append({'name': a['id'], 'pid': p.pid})
        with (run / 'gazebo.log').open('w') as log:
            p = subprocess.Popen(['gzserver', '--verbose', str(world_path)],
                                 env=env, cwd=run, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        meta['processes'].append({'name': 'gzserver', 'pid': p.pid})
        (run / 'processes.json').write_text(json.dumps(meta, indent=2))
        return meta
    except Exception:
        for p in meta['processes']:
            os.killpg(p['pid'], signal.SIGTERM)
        raise


def stop(run=DEFAULT_RUN):
    path = Path(run) / 'processes.json'
    if not path.exists():
        return
    meta = json.loads(path.read_text())
    # PID reuse protection: process cwd must belong to this run.
    for p in reversed(meta['processes']):
        proc = Path('/proc', str(p['pid']))
        try:
            cwd = (proc / 'cwd').resolve()
            if str(cwd).startswith(str(Path(run).resolve()) + '/') or cwd == Path(run).resolve():
                os.killpg(p['pid'], signal.SIGTERM)
        except (ProcessLookupError, FileNotFoundError):
            pass
    time.sleep(2)
    for p in reversed(meta['processes']):
        proc = Path('/proc', str(p['pid']))
        try:
            cwd = (proc / 'cwd').resolve()
            if cwd == Path(run).resolve() or Path(run).resolve() in cwd.parents:
                os.killpg(p['pid'], signal.SIGKILL)
        except (ProcessLookupError, FileNotFoundError):
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop'])
    parser.add_argument('--run', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--px4', type=Path, default=Path('/home/ldb/PX4-Autopilot'))
    args = parser.parse_args()
    if args.action == 'start':
        print(json.dumps(launch(args.run, args.px4), indent=2))
    else:
        stop(args.run)
