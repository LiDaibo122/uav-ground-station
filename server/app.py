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
            AGENT=Agent(ROOT,lambda payload: request_json(OLLAMA_URL+'/api/chat',payload,timeout=180),MODEL,{
                'scene':SCENE,'live_state':live_state,'demo_state':demo_state,
                'simulation_state':simulation_state,'session':lambda:SESSION},
                request_stream=OllamaStream(OLLAMA_URL+'/api/chat',LOCAL_HTTP))
        return AGENT


def chat(data,emit=None):
    if not MODEL_LOCK.acquire(blocking=False):
        raise RuntimeError('Qwen 正在处理另一条消息，请稍后继续')
    try:
        return get_agent().chat(data,emit=emit)
    finally:
        MODEL_LOCK.release()


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
        if path.startswith('/vendor/cesium/') and not self.server.built:
            base = ROOT/'node_modules/cesium/Build/Cesium'
            path = path[len('/vendor/cesium/'):]
        else:
            path = path.lstrip('/') or 'index.html'
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
    args=parser.parse_args()
    server=ThreadingHTTPServer((args.host,args.port),Handler)
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
