#!/usr/bin/env python3
"""Local ground station with Qwen tasks and an owned localhost SITL executor."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import threading
import sys
import signal
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from urllib.parse import urlsplit, parse_qs, unquote
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from packages.ground_core.tasks import validate_targets, normalize_targets
from packages.ground_core.task_library import MAX_LIBRARY_TASKS, TaskLibrary, TaskLibraryConflict
SCENE = json.loads((ROOT / 'config/scene.json').read_text())
MAX_BODY = 128 * 1024
MODEL = os.environ.get('GROUND_QWEN_MODEL', 'qwen3.5:4b')
OLLAMA_URL = os.environ.get('GROUND_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOCK = threading.Lock()
MODEL_LOCK = threading.Lock()
LIVE = {}
STARTED = time.monotonic()
HEALTH = {'at': 0, 'value': None}
TILE_CACHE = OrderedDict()
TILE_LOCK = threading.Lock()
SESSION_LOCK = threading.Lock()
SESSION = None
XD = None
XD_STATIONS = {}
XD_INTERFACES = {}
XD_DISPATCH = None
XD_MONITOR = False
XD_BRIDGE = '/ground_bridge'
XD_DATUM = None
RIVER_TRIAL = None
TASK_LIBRARY = None
LIBRARY_REPLAY = None
from packages.ground_core.inspection_rehearsal import Rehearsal
REHEARSAL = Rehearsal(ROOT/"runs"/"inspection_rehearsals")


def simulation_state():
    return SESSION.snapshot() if SESSION else {'status':'offline','ready':False,'fleet':[]}


def start_simulation():
    global SESSION
    with SESSION_LOCK:
        if SESSION and SESSION.snapshot()['status'] in ('starting','ready'):
            return {'created':False,**SESSION.snapshot()}
        previous_id=SESSION.id if SESSION else None
        if previous_id:
            with LOCK:
                for uid in list(LIVE):
                    if LIVE[uid].get('session_id')==previous_id:
                        del LIVE[uid]
        from packages.swarm_sim.session import Session
        def receive(packet):
            item=validate_telemetry(packet)
            with LOCK:
                LIVE[item['id']]=item
        SESSION=Session(receive)
        SESSION.start()
        return {'created':True,**SESSION.snapshot()}


def now():
    return datetime.now(timezone.utc).isoformat()


def request_json(url, payload=None, timeout=3):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with LOCAL_HTTP.open(req, timeout=timeout) as response:
        return json.load(response)


def finite(value, low, high, field):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(field + ' 超出范围')
    return value


def validate_telemetry(data):
    if not isinstance(data, dict):
        raise ValueError('需要对象')
    uid = data.get('id', '')
    if not isinstance(uid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', uid):
        raise ValueError('设备标识无效')
    result = {'id': uid, 'source': 'px4', 'received_at': now(), '_received_monotonic': time.monotonic()}
    for field, bounds in [('longitude', (-180, 180)), ('latitude', (-85, 85)), ('altitude_m', (-500, 30000)), ('heading_deg', (0, 360))]:
        result[field] = finite(data.get(field), *bounds, field)
    for field, bounds in [('speed_mps', (0, 500)), ('battery_pct', (0, 100))]:
        value = data.get(field)
        result[field] = None if value is None else finite(value, *bounds, field)
    result['flight_mode'] = str(data.get('flight_mode', 'UNKNOWN'))[:40]
    result['task'] = str(data.get('task', '机载任务未上报'))[:120]
    result['altitude_reference'] = str(data.get('altitude_reference', 'WGS84_ellipsoid'))[:40]
    result['observed_at'] = str(data.get('observed_at', ''))[:64]
    result['session_id'] = str(data.get('session_id',''))[:48]
    for field in ('armed','landed'):
        value=data.get(field)
        if value is not None and not isinstance(value,bool):
            raise ValueError(field+' 必须为布尔值')
        result[field]=value
    result['relative_alt_m']=None if data.get('relative_alt_m') is None else finite(data['relative_alt_m'],-500,30000,'relative_alt_m')
    return result


def demo_state(elapsed=None):
    t = (time.monotonic() - STARTED) if elapsed is None else elapsed
    aircraft = []
    for i, (lon, lat, radius, height, color, task) in enumerate([
        (103.551,31.034,1050,2600,'#99cfb0','A 区 · 河谷巡检'),
        (103.569,31.074,950,3400,'#e2b777','B 区 · 山地巡检'),
        (103.591,31.045,1200,2800,'#9dbbe0','区域外围 · 航线演示')
    ]):
        angle = t * 22 / radius + i * 2.1
        aircraft.append({'id': 'UAV-0'+str(i+1), 'source': 'demo', 'longitude': lon + radius*math.cos(angle)/(111320*math.cos(math.radians(lat))),
                         'latitude': lat + radius*math.sin(angle)/111320, 'altitude_m': height,
                         'heading_deg': (360-math.degrees(angle)) % 360, 'speed_mps': 22,
                         'battery_pct': [86,72,91][i], 'flight_mode':'演示巡航', 'task':task,
                         'color':color, 'stale':False, 'age_s':0, 'altitude_reference':'display_height',
                         'orbit': {'longitude':lon,'latitude':lat,'radius_m':radius}, 'received_at':now()})
    return {'mode':'demo','aircraft':aircraft,'timestamp':now(),'simulation_time_s':round(t,1), 'px4_connected':False}


def live_state():
    with LOCK:
        aircraft = []
        for value in LIVE.values():
            item = {k:v for k,v in value.items() if not k.startswith('_')}
            age = time.monotonic()-value['_received_monotonic']
            item.update(age_s=round(age,1), stale=age>5)
            aircraft.append(item)
    return {'mode':'live','aircraft':aircraft,'timestamp':now(),'px4_connected':any(not a['stale'] for a in aircraft)}


def swarm_snapshot():
    if SESSION:
        return {'available':True,'report':SESSION.snapshot()['report'],'age_s':0}
    path = ROOT/'runs/interactive-latest.json'
    if not path.exists():
        path=ROOT/'runs/swarm/flight-report.json'
    try:
        report = json.loads(path.read_text())
        age = max(0, time.time()-path.stat().st_mtime)
        return {'available':True,'report':report,'age_s':round(age,1)}
    except (ValueError,OSError):
        return {'available':False}


def model_health():
    if time.monotonic()-HEALTH['at']<8 and HEALTH['value']:
        return HEALTH['value']
    try:
        models = request_json(OLLAMA_URL+'/api/tags').get('models',[])
        names = [m.get('name','') for m in models]
        value = {'available':MODEL in names,'model':MODEL,'models':names,'reason':'' if MODEL in names else '本地未找到所选模型'}
    except Exception:
        value = {'available':False,'model':MODEL,'models':[],'reason':'Ollama 服务未连接'}
    HEALTH.update(at=time.monotonic(),value=value)
    return value


def validate_draft(draft):
    if not isinstance(draft,dict):
        raise ValueError('任务草案格式无效')
    fields = ('title','area','task_type','priority','notes')
    for name in fields:
        if not isinstance(draft.get(name),str) or len(draft[name])>1200:
            raise ValueError('草案字段无效：'+name)
    if not draft['title'].strip() or not draft['area'].strip():
        raise ValueError('草案缺少标题或区域')
    if draft['task_type'] not in ('survey','inspect','review') or draft['priority'] not in ('normal','high','low'):
        raise ValueError('草案类型或优先级无效')
    ids = draft.get('aircraft_ids')
    if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(v,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}',v) for v in ids):
        raise ValueError('草案设备列表无效')
    return {**{k:draft[k] for k in (*fields,'aircraft_ids')},'targets':validate_targets(draft.get('targets',[]))}


AGENT = None
AGENT_LOCK = threading.Lock()


def get_agent():
    global AGENT
    with AGENT_LOCK:
        if AGENT is None:
            from packages.ground_core.agent import Agent
            from packages.ground_core.model_stream import OllamaStream
            options = {'request_stream':OllamaStream(OLLAMA_URL+'/api/chat',LOCAL_HTTP)}
            env = {'scene':SCENE,'live_state':live_state,'demo_state':demo_state,
                   'simulation_state':simulation_state,'session':lambda:SESSION}
            directory = ROOT
            if XD is not None:
                from packages.adapters.xd_web import Runtime, SYSTEM, CATALOG
                directory = ROOT/'runs'/('xd_monitor' if XD_MONITOR else 'xd_single')/XD.run_id
                env = {'station': XD, 'interfaces': XD_INTERFACES}
                from packages.adapters.xd_fleet import FleetRuntime, FLEET_SYSTEM, tool_catalog
                registry = tool_catalog(XD_STATIONS, planning_only=XD_MONITOR)
                fleet_env = dict(stations=XD_STATIONS, catalog=registry, interfaces=XD_INTERFACES, dispatch=XD_DISPATCH)
                import rospy
                fleet_env['observations'] = lambda: rospy.get_param(XD_BRIDGE + '/planning/observations', {})

                def runtime_factory(environment, conversation, mode, text):
                    if len(XD_STATIONS) == 1 and not XD_MONITOR and mode == 'live':
                        runtime = Runtime(environment, conversation, mode, text)
                        runtime.system = SYSTEM
                        return runtime
                    if mode in ('xd_vehicle', 'xd_task_library', 'xd_plan_only'):
                        options = dict(fleet_env, workbench=True, vehicle_id=environment.get('vehicle_id'))
                        if 'library_revision' in environment:
                            options.update(read_library=lambda: TASK_LIBRARY.read(environment['library_run_id'],
                                                                                  environment['library_revision']),
                                           preview_library=preview_sketch_fleet)
                        return FleetRuntime(options, conversation, mode, text)
                    return FleetRuntime(dict(fleet_env, **{
                        key: environment[key] for key in ('vehicle_id', 'displayed_plan_id') if key in environment
                    }), conversation, mode, text)

                options.update(think=False, runtime_factory=runtime_factory, system=FLEET_SYSTEM, registry=registry)
            AGENT=Agent(directory,lambda payload: request_json(OLLAMA_URL+'/api/chat',payload,timeout=180),
                        MODEL,env,**options)
        return AGENT


def preview_sketch_fleet(shapes, allowed=None):
    """Read-only preview, independent of all dispatch conversations."""
    import rospy
    from packages.adapters.xd_allocation import plan_inspections
    specs, quotes = [], []
    for t in shapes:
        if t['kind']=='area':
            area = {k:t[k] for k in ('west','south','east','north')}
            quote = '西{west}米 南{south}米 东{east}米 北{north}米'.format(**area)
            specs.append(dict(label=t.get('label',t['id']),area=area,bounds_quote=quote))
            quotes.append(quote)
        else:
            specs.append(dict(label=t.get('label',t['id']),east=t['east'],north=t['north']))
    states = [station.snapshot() for station in XD_STATIONS.values()]
    observations = rospy.get_param(XD_BRIDGE+'/planning/observations',{})
    plan = plan_inspections({'tasks':specs},None,states,observations,'。'.join(quotes), allowed_by_label=allowed)
    if allowed is not None:
        fields = ('tasks','assignments','unassigned','height_m','task_specs','frame','policy','capacity','excluded_vehicles')
        return dict({key: plan[key] for key in fields},
                    fleet=[{key: a[key] for key in ('id','available','reasons')} for a in plan['fleet']],
                    origins={a['id']:[a['east_m'],a['north_m']] for a in plan['fleet']})
    return {k:plan[k] for k in ('assignments','unassigned','created_at','fleet','tasks')}


def chat(data,emit=None):
    if not MODEL_LOCK.acquire(blocking=False):
        raise RuntimeError('Qwen 正在处理另一条消息，请稍后继续')
    try:
        if XD is not None and data.get('mode') == 'xd_vehicle':
            vehicle = data.get('vehicle_id')
            if not isinstance(vehicle, str) or vehicle not in XD_STATIONS:
                raise ValueError('未找到单机窗口绑定的飞机')
        if data.get('mode') == 'xd_task_library' or 'library_revision' in data:
            if (TASK_LIBRARY is None or not isinstance(data.get('library_run_id'), str)
                    or type(data.get('library_revision')) is not int):
                raise ValueError('请先读取当前仿真场次的任务库')
            TASK_LIBRARY.read(data['library_run_id'],data['library_revision'])
        if XD_MONITOR and data.get('mode') not in ('xd_task_library', 'xd_vehicle'):
            data = dict(data, mode='xd_plan_only')
        return get_agent().chat(data, emit=emit)
    finally:
        MODEL_LOCK.release()


def confirm_workbench(data):
    key = data.get('conversation_id')
    if not isinstance(key, str):
        raise ValueError('缺少当前对话编号')
    conversation, lock = get_agent()._load(key)
    with lock:
        plan = conversation.get('workbench_draft') or {}
        if (not plan.get('id') or data.get('plan_id') != plan['id']
                or data.get('run_id') != XD.run_id or plan['run_id'] != XD.run_id
                or data.get('window') != plan.get('window')):
            raise ValueError('草案已变化，请核对当前窗口的最新方案')
        if plan.get('kind') == 'library_replay':
            with TASK_LIBRARY.lock:
                TASK_LIBRARY.read(plan['run_id'], plan['library_revision'])
                return dict(kind='library_replay', state=LIBRARY_REPLAY.start(plan))
        if not XD_DISPATCH:
            raise ValueError('当前连接仅提供遥测')
        if plan.get('kind') in ('local_action', 'library_points'):
            from packages.adapters.xd_dispatch import check_gazebo_world
            XD_DISPATCH.check_session(XD.run_id)
            check_gazebo_world(XD_STATIONS)
        if plan.get('kind') in ('library_points', 'library_flight'):
            with TASK_LIBRARY.lock:
                TASK_LIBRARY.read(plan['run_id'], plan['library_revision'])
                if plan.get('flight_blocked'):
                    raise ValueError(plan['flight_blocked'])
                if plan.get('unassigned'):
                    raise ValueError('还有未分配的任务，请调整分工')
                return dict(kind='flight', receipt=XD_DISPATCH.confirm(plan, plan['id']))
        if plan.get('kind') == 'local_action':
            station = XD_STATIONS[plan['vehicle']]
            with station.lock:
                prior = next((j for j in station.history if j.get('plan_id') == plan['id']), None)
                if prior:
                    return dict(kind='local_action', vehicle=station.vehicle, status=prior['phase'], execution_id=prior['id'])
                payload = {'altitude_m':40} if plan['action']=='takeoff' else {}
                return dict(kind='local_action', **station.submit(plan['action'], payload, plan['id']))
        return dict(kind='flight', receipt=XD_DISPATCH.confirm(plan, plan['id']))


class Handler(BaseHTTPRequestHandler):
    server_version = 'FieldLink/0.1'

    def log_message(self, fmt, *args):
        # URLs never carry model credentials; Google keys remain browser memory only.
        if not self.path.startswith('/vendor/'):
            super().log_message(fmt,*args)

    def send_json(self, payload, status=200):
        encoded = json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlsplit(self.path)
        replay = re.fullmatch(r'/api/xd/replays/([0-9a-f]{32})', parsed.path)
        if replay and LIBRARY_REPLAY:
            try:
                return self.send_json(LIBRARY_REPLAY.status(replay[1]))
            except ValueError as exc:
                return self.send_json({'error':str(exc)}, 404)
        if parsed.path == '/api/xd/task-library' and TASK_LIBRARY is not None:
            return self.send_json(dict(TASK_LIBRARY.read(), max_items=MAX_LIBRARY_TASKS))
        if parsed.path == '/api/xd/river-trial':
            return self.send_json(RIVER_TRIAL.read() if RIVER_TRIAL else {'phase':'unavailable'})
        if XD is not None:
            if parsed.path=='/api/xd/interfaces':
                return self.send_json(XD_INTERFACES)
            execution = re.fullmatch(r'/api/xd/executions/([0-9a-f]{32})', parsed.path)
            if execution and XD_DISPATCH:
                return self.send_json(XD_DISPATCH.status(execution[1]))
            if parsed.path == '/api/xd/map-sources':
                from server.map_sources import read_map_sources
                try:
                    return self.send_json(read_map_sources(ROOT / 'config/map_sources.local.json'))
                except (OSError, ValueError) as exc:
                    reason = '无法读取底图配置' if isinstance(exc, OSError) else str(exc)
                    return self.send_json({'error': reason}, 400)
            if parsed.path == '/api/xd/console':
                try:
                    since = float(parse_qs(parsed.query).get('since', ['0'])[0])
                    if not math.isfinite(since) or since < 0:
                        raise ValueError('航迹时间无效')
                except ValueError as exc:
                    return self.send_json({'error': str(exc)}, 400)
                return self.send_json(dict(run_id=XD.run_id, datum=XD_DATUM,
                                           dispatch_available=XD_DISPATCH is not None, river_trial=RIVER_TRIAL is not None,
                                           vehicles=[station.snapshot() for station in XD_STATIONS.values()],
                                           trails={uid: station.trail(since) for uid, station in XD_STATIONS.items()}))
            if parsed.path=='/api/xd/status':
                if XD_MONITOR or len(XD_STATIONS) > 1:
                    return self.send_json({'vehicles':[station.snapshot() for station in XD_STATIONS.values()]})
                return self.send_json(XD.snapshot())
            if parsed.path=='/api/health':
                return self.send_json({'service':'fieldlink','backend':'xd_monitor' if XD_MONITOR else 'xd_fleet' if len(XD_STATIONS)>2 else 'xd_dual' if len(XD_STATIONS)==2 else 'xd_single','model':model_health()})
            if parsed.path.startswith('/api/') and parsed.path!='/api/agent/activity':
                return self.send_json({'error':'XD 单机接口不存在'},404)
        tile_match = re.fullmatch(r'/api/tiles/(terrain|imagery)/(\d+)/(\d+)/(\d+)\.(png|jpg)',parsed.path)
        if tile_match:
            kind, z, x, y, _ = tile_match.groups()
            z,x,y = int(z),int(x),int(y)
            if not 0<=z<=19 or not 0<=x<2**z or not 0<=y<2**z:
                return self.send_json({'error':'瓦片坐标无效'},400)
            cache_key = (kind,z,x,y)
            with TILE_LOCK:
                cached = TILE_CACHE.get(cache_key)
            try:
                if cached is None:
                    url = ('https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{}/{}/{}.png'.format(z,x,y) if kind=='terrain'
                           else 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{}/{}/{}'.format(z,y,x))
                    with urllib.request.urlopen(url,timeout=25) as response:
                        cached = (response.read(2*1024*1024),response.headers.get('Content-Type','image/png'))
                    with TILE_LOCK:
                        TILE_CACHE[cache_key] = cached
                        while len(TILE_CACHE)>384:
                            TILE_CACHE.popitem(last=False)
                self.send_response(200)
                self.send_header('Content-Type',cached[1])
                self.send_header('Content-Length',str(len(cached[0])))
                self.send_header('Cache-Control','private, max-age=300')
                self.end_headers()
                self.wfile.write(cached[0])
            except (OSError,urllib.error.URLError):
                return self.send_json({'error':'地图数据源暂时不可达'},502)
            return
        if parsed.path=='/api/health':
            return self.send_json({'service':'fieldlink','model':model_health(),'px4':live_state()['px4_connected'],'flight_command_transport':simulation_state()['ready']})
        if parsed.path=='/api/agent/activity':
            key=parse_qs(parsed.query).get('conversation_id',[''])[0]
            return self.send_json(get_agent().activity(key))
        if parsed.path=='/api/simulation':
            return self.send_json(simulation_state())
        if parsed.path=='/api/scene':
            return self.send_json(SCENE)
        if parsed.path=='/api/state':
            mode = parse_qs(parsed.query).get('mode',['demo'])[0]
            return self.send_json(demo_state() if mode=='demo' else live_state())
        if parsed.path=='/api/tasks':
            with LOCK:
                return self.send_json(self.read_tasks())
        if parsed.path=='/api/swarm':
            return self.send_json(swarm_snapshot())
        if parsed.path=='/api/scene/manifest':
            return self.send_json({**SCENE,'version':'1.0','rendering_only':True,
                'sensor_connected':False,'requirements':['仿真器中的场景加载','WGS84 / ENU / NED 坐标对齐','高程基准转换','相机内外参与时间同步','ROS 图像发布'],
                'google_tiles':'仅在线渲染；未下载或导出 Google 数据'})
        if parsed.path.startswith('/api/'):
            return self.send_json({'error':'接口不存在'},404)
        path = unquote(parsed.path)
        base = ROOT/'dist' if self.server.built else ROOT/'web'
        if path.startswith('/vendor/maplibre/') and not self.server.built:
            package = ROOT/'node_modules/maplibre-gl'
            path = path[len('/vendor/maplibre/'):]
            if path not in ('maplibre-gl.mjs', 'maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs',
                            'maplibre-gl.css', 'LICENSE.txt'):
                return self.send_json({'error': '文件不存在'}, 404)
            base = package if path == 'LICENSE.txt' else package/'dist'
        elif path.startswith('/vendor/cesium/') and not self.server.built:
            base = ROOT/'node_modules/cesium/Build/Cesium'
            path = path[len('/vendor/cesium/'):]
        else:
            path = path.lstrip('/') or ('chat.html' if XD is not None else 'index.html')
        if XD is not None and path == 'chat.html':
            path = 'xd-console.html'
        file = (base/path).resolve()
        if base.resolve() not in file.parents or not file.is_file():
            return self.send_json({'error':'文件不存在'},404)
        content = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type',mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(len(content)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
        self.send_header('Cache-Control','no-cache')
        self.end_headers()
        self.wfile.write(content)

    def read_tasks(self):
        path = ROOT/'runs/tasks.json'
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def send_chat_stream(self,data):
        self.send_response(200)
        self.send_header('Content-Type','application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Accel-Buffering','no')
        self.send_header('Connection','close')
        self.end_headers();self.close_connection=True;disconnected=False
        self.connection.settimeout(30)
        def emit(event):
            nonlocal disconnected
            if disconnected:return
            try:
                self.wfile.write((json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n').encode())
                self.wfile.flush()
            except OSError:disconnected=True
        try:chat(data,emit=emit)
        except (ValueError,RuntimeError,OSError) as exc:
            emit({'type':'error','error':str(exc)})

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.send_json({'error':'仅接受同源请求'},403)
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0<size<=MAX_BODY:
                return self.send_json({'error':'请求大小无效'},413)
            data = json.loads(self.rfile.read(size),parse_constant=lambda v: (_ for _ in ()).throw(ValueError('非法数值')))
            if not isinstance(data,dict):
                raise ValueError('请求需要 JSON 对象')
            path = urlsplit(self.path).path
            if path == '/api/xd/confirm' and XD is not None:
                return self.send_json(confirm_workbench(data))
            if path == '/api/xd/task-library' and TASK_LIBRARY is not None:
                return self.send_json(dict(TASK_LIBRARY.save(data), max_items=MAX_LIBRARY_TASKS))
            if path == '/api/xd/river-trial' and RIVER_TRIAL:
                if data.get('op')=='interpret':
                    from packages.adapters.xd_handoff_trial import interpret_trial
                    if not MODEL_LOCK.acquire(blocking=False):
                        raise RuntimeError('Qwen 正在处理另一条消息')
                    try:
                        result=interpret_trial(data,RIVER_TRIAL.read(),MODEL,lambda payload:request_json(
                            OLLAMA_URL+'/api/chat',payload,timeout=90))
                        return self.send_json(result)
                    finally:
                        MODEL_LOCK.release()
                return self.send_json(RIVER_TRIAL.command(data.get('op'),data.get('run_id'),data.get('primary')))
            if path == '/api/inspection-rehearsal':
                def ask(payload):
                    if not MODEL_LOCK.acquire(blocking=False):
                        raise RuntimeError('Qwen 正在处理其他请求')
                    try:
                        return request_json(OLLAMA_URL+'/api/chat',payload,timeout=90)
                    finally:
                        MODEL_LOCK.release()
                return self.send_json(REHEARSAL.request(data, MODEL, ask, preview_sketch_fleet if XD else None))
            if XD is not None and path not in ('/api/chat','/api/chat/stream'):
                return self.send_json({'error':'XD 单机只接受对话请求'},404)
            if path=='/api/simulation/start':
                return self.send_json(start_simulation(),202)
            if path=='/api/simulation/stop':
                if SESSION and (not data.get('id') or data['id']==SESSION.id):
                    SESSION.stop()
                return self.send_json(simulation_state())
            if path=='/api/auction/preview':
                if not SESSION:
                    raise RuntimeError('请先启动固定翼仿真')
                draft=validate_draft(data.get('draft'))
                mission_tasks=normalize_targets(draft,SESSION.report['origin'],SCENE['zones'])
                return self.send_json({'tasks':mission_tasks,'allocation':SESSION.plan(mission_tasks)})
            if path=='/api/chat':
                return self.send_json(chat(data))
            if path=='/api/chat/stream':
                return self.send_chat_stream(data)
            if path=='/api/telemetry':
                item = validate_telemetry(data)
                with LOCK:
                    LIVE[item['id']] = item
                return self.send_json({'received':True,'id':item['id']})
            if path=='/api/tasks':
                draft = validate_draft(data.get('draft'))
                task = {**draft,'id':str(uuid4()),'created_at':now(),'status':'draft','dispatched':False,
                        'data_mode':'demo' if data.get('mode')=='demo' else 'live'}
                with LOCK:
                    tasks = self.read_tasks()
                    tasks.append(task)
                    directory = ROOT/'runs'; directory.mkdir(exist_ok=True)
                    temporary = directory/'tasks.tmp'
                    temporary.write_text(json.dumps(tasks,ensure_ascii=False,indent=2))
                    temporary.replace(directory/'tasks.json')
                return self.send_json(task,201)
            return self.send_json({'error':'接口不存在'},404)
        except TaskLibraryConflict as e:
            return self.send_json({'error':str(e)},409)
        except (ValueError,TypeError,KeyError) as e:
            return self.send_json({'error':str(e)},400)
        except (urllib.error.URLError,TimeoutError,OSError):
            return self.send_json({'error':'无法连接本地 Qwen 或推理超时。请检查 Ollama 服务；本次没有生成或下发任务。'},503)
        except RuntimeError as e:
            return self.send_json({'error':str(e)},502)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=11843)
    parser.add_argument('--built',action='store_true')
    xd_mode = parser.add_mutually_exclusive_group()
    xd_mode.add_argument('--xd-single',action='store_true')
    xd_mode.add_argument('--xd-monitor', metavar='BRIDGE_NAMESPACE', help='只读 ROS 遥测与集群规划')
    args=parser.parse_args()
    server=ThreadingHTTPServer((args.host,args.port),Handler)
    global XD, XD_STATIONS, XD_INTERFACES, XD_DISPATCH, XD_MONITOR, XD_BRIDGE, TASK_LIBRARY
    if args.xd_single or args.xd_monitor:
        XD_MONITOR = bool(args.xd_monitor)
        XD_BRIDGE = args.xd_monitor or XD_BRIDGE
        if args.host!='127.0.0.1' or (args.xd_single and os.environ.get('ROS_MASTER_URI')!='http://127.0.0.1:11321'):
            parser.error('需要本机 XD 单机仿真环境')
        import rospy
        from packages.adapters.xd_web import Station
        rospy.init_node('xd_qwen_web',anonymous=True,disable_signals=True)
        from packages.adapters.xd_web import CATALOG
        from packages.adapters.xd_fleet import tool_catalog
        vehicles = rospy.get_param(XD_BRIDGE + '/bridge/vehicles')
        if XD_MONITOR:
            from packages.adapters.xd_web import TelemetryStation
            XD_STATIONS = {key: TelemetryStation(key, ns, XD_BRIDGE) for key, ns in sorted(vehicles.items())}
        else:
            XD_STATIONS = {key: Station(ROOT/'runs/xd_single/jobs', key) for key in sorted(vehicles)}
        XD = next(iter(XD_STATIONS.values()))
        mode_directory = 'xd_monitor' if XD_MONITOR else 'xd_single'
        TASK_LIBRARY = TaskLibrary(ROOT/'runs'/mode_directory/XD.run_id/'task-library.json',XD.run_id)
        global LIBRARY_REPLAY
        from packages.ground_core.library_replay import LibraryReplay
        LIBRARY_REPLAY = LibraryReplay(TASK_LIBRARY.path.parent/'map-replays')
        if not XD_MONITOR:
            from packages.adapters.xd_dispatch import Dispatch
            from sensor_msgs.msg import NavSatFix
            XD_DISPATCH = Dispatch(ROOT/'runs/xd_single'/XD.run_id/'executions', XD_STATIONS)

            if set(XD_STATIONS)=={'uav1','uav2'}:
                global RIVER_TRIAL
                from packages.adapters.xd_handoff_trial import RiverTrial
                RIVER_TRIAL = RiverTrial(XD_STATIONS, XD_DISPATCH.directory.parent)

            def set_datum(message):
                global XD_DATUM
                if all(math.isfinite(x) for x in (message.latitude, message.longitude, message.altitude)):
                    XD_DATUM = dict(lat=message.latitude, lon=message.longitude, altitude_m=message.altitude)

            rospy.Subscriber('/world/datum', NavSatFix, set_datum, queue_size=1)
        registry = tool_catalog(vehicles, planning_only=XD_MONITOR) if XD_MONITOR or len(vehicles)>1 else CATALOG
        XD_INTERFACES = dict(schema_version=1, vehicles=vehicles, frame='world ENU 米',
                             endpoints=rospy.get_param(XD_BRIDGE + '/bridge/xd_interfaces'),
                             telemetry=rospy.get_param(XD_BRIDGE + '/bridge/topics'),
                             tools=[d['wire'] for d in registry.values()])
    for sig in (signal.SIGTERM,signal.SIGHUP):
        signal.signal(sig,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    server.built=args.built
    print('FieldLink: http://{}:{}'.format(args.host,args.port),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        if SESSION:
            SESSION.stop()
        server.server_close()


if __name__=='__main__':main()
