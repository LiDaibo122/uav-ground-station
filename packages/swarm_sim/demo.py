"""One command to run the reproducible auction + PX4 fixed-wing experiment."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

from .sitl import DEFAULT_RUN, ROOT, launch, stop
from .verify_flight import run

LOCAL_HTTP=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--px4', type=Path, default=Path('/home/ldb/PX4-Autopilot'))
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--hold', action='store_true', help='Keep flying in loiter and serving telemetry after PASS')
    parser.add_argument('--fixture',action='store_true',help='Run the legacy six-point regression experiment')
    parser.add_argument('--gui',action='store_true',help='Open Gazebo Classic GUI connected to this simulation')
    parser.add_argument('--browser',action='store_true',help='Open a dedicated ground-station browser window')
    parser.add_argument('--qgc-host',help='Windows QGC host IP or auto; adds an independent UDP bridge in tmux')
    console=parser.add_mutually_exclusive_group()
    console.add_argument('--tmux',action='store_true',help='Start the simulation and telemetry windows in tmux')
    console.add_argument('--no-tmux',action='store_true',help='Run directly without creating tmux windows')
    parser.add_argument('--tmux-detached',action='store_true',help='Create tmux windows and return without attaching')
    parser.add_argument('--tmux-session',default='fieldlink',help='Project tmux session name (default: fieldlink)')
    parser.add_argument('--tmux-socket',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.no_tmux and args.qgc_host:
        parser.error('--qgc-host uses tmux; omit --no-tmux or start qgc_bridge separately')
    if not args.no_tmux and (args.tmux or args.tmux_detached or args.qgc_host or (args.gui and not args.fixture)):
        from .tmux_ui import launch as launch_console
        return launch_console(sys.argv[1:],name=args.tmux_session,socket=args.tmux_socket,detached=args.tmux_detached,qgc_host=args.qgc_host)
    def interrupt(signum,frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM,signal.SIGHUP):
        signal.signal(sig,interrupt)
    server = None
    owns_sitl = False
    interactive_id=None
    views=None
    try:
        helper=Path.home()/'.local/bin/ollama-start'
        if helper.is_file():
            subprocess.run([str(helper)],timeout=40,check=False)
        try:
            with LOCAL_HTTP.open('http://127.0.0.1:11843/api/health', timeout=5) as response:
                if json.load(response).get('service') != 'fieldlink':
                    raise RuntimeError('Port 11843 is occupied by another service')
        except OSError:
            (ROOT/'runs').mkdir(exist_ok=True)
            with (ROOT/'runs/swarm-server.log').open('a') as log:
                server = subprocess.Popen(['python3', str(ROOT/'server/app.py')], cwd=ROOT,
                                          stdout=log, stderr=subprocess.STDOUT)
            time.sleep(1)
            if server.poll() is not None:
                raise RuntimeError('Ground server failed to start')
        print('Open http://127.0.0.1:11843/?mode=live&swarm=1', flush=True)
        if not args.fixture:
            request=urllib.request.Request('http://127.0.0.1:11843/api/simulation/start',b'{}',{'Content-Type':'application/json'})
            with LOCAL_HTTP.open(request,timeout=20) as response:
                session=json.load(response)
            if session['created']:
                interactive_id=session['id']
            if args.gui or args.browser:
                from .presentation import DesktopViews
                views=DesktopViews(args.px4)
                views.open(gazebo=args.gui,browser=args.browser)
            print('Waiting for live aircraft. No preloaded inspection points; send tasks through Qwen.',flush=True)
            previous=None
            while True:
                with LOCAL_HTTP.open('http://127.0.0.1:11843/api/simulation',timeout=5) as response:
                    state=json.load(response)
                status=(state['status'],state.get('report',{}).get('status'))
                if status!=previous:
                    print('SESSION',*status,flush=True)
                    previous=status
                if status[0] in ('error','stopped'):
                    return 1
                if status[1]=='completed' and not args.hold:
                    return 0
                time.sleep(1)
        launch(args.run, args.px4)
        owns_sitl = True
        if args.gui or args.browser:
            from .presentation import DesktopViews
            views=DesktopViews(args.px4)
            views.open(gazebo=args.gui,browser=args.browser)
        report = run(args.run, args.timeout, hold=args.hold)
        return 0 if report['success'] else 1
    except KeyboardInterrupt:
        print('Stopping owned simulation processes.', flush=True)
        return 130
    finally:
        if views:
            views.close()
        if interactive_id:
            try:
                request=urllib.request.Request('http://127.0.0.1:11843/api/simulation/stop',
                     json.dumps({'id':interactive_id}).encode(),{'Content-Type':'application/json'})
                LOCAL_HTTP.open(request,timeout=15).close()
            except OSError:
                pass
        if owns_sitl:
            stop(args.run)
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == '__main__':
    raise SystemExit(main())
