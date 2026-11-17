"""Read-only terminal views of FieldLink telemetry, tasks and model calls.

Uses HTTP snapshots instead of opening a second MAVLink UDP receiver.  File
fallbacks are explicitly historical; the optional MAVROS view only inspects
an existing ROS master and subscribes to already-published state topics.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xmlrpc.client


ROOT = Path(__file__).resolve().parents[2]
VIEWS = ('overview', 'state', 'position', 'battery', 'mission', 'agent', 'mavros')
CHINA = timezone(timedelta(hours=8))


def clean(value, limit=500):
    """Do not let model/user strings control the terminal."""
    return ''.join(c if c in '\n\t' or ord(c) >= 32 else '?' for c in str(value))[:limit]


def number(value, digits=1, suffix=''):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 'UNKNOWN'
    return ('%.*f' % (digits, value)) + suffix


def mode_name(value):
    if isinstance(value, str):
        return clean(value, 45)
    if not isinstance(value, int):
        return 'UNKNOWN'
    main, sub = (value >> 16) & 255, (value >> 24) & 255
    if main == 4:
        return 'AUTO.' + {1: 'READY', 2: 'TAKEOFF', 3: 'LOITER', 4: 'MISSION', 5: 'RTL', 6: 'LAND'}.get(sub, str(sub))
    return {1: 'MANUAL', 2: 'ALTCTL', 3: 'POSCTL', 5: 'ACRO', 6: 'OFFBOARD', 7: 'STAB', 8: 'RATTITUDE'}.get(main, str(value))


def boolean(value):
    return 'true' if value is True else 'false' if value is False else 'UNKNOWN'


def stage(a):
    if a.get('landed') is True:
        return '地面/已解锁' if a.get('armed') is True else '地面/未解锁' if a.get('armed') is False else '地面/解锁未知'
    if a.get('landed') is False:
        return '已离地'
    return '离地状态待确认'


def record_age(a, now):
    # API state age belongs to the same position/mode fields it provides;
    # do not replace it with the independently fetched fleet's newer age.
    received_age = a.get('age_s')
    if isinstance(received_age, (int, float)) and not isinstance(received_age, bool):
        return max(0, received_age)
    observed = a.get('observed_timestamp', a.get('timestamp'))
    if isinstance(observed, (int, float)):
        return max(0, now-observed)
    return a.get('age_s', a.get('telemetry_age_s'))


def is_fresh(a, snapshot):
    age = record_age(a, snapshot['now'])
    return (snapshot['source'] == 'http' and not a.get('stale', False)
            and isinstance(age, (int, float)) and age <= 5)


def merge_aircraft(state, simulation):
    report = simulation.get('report', {})
    origin = report.get('origin', {})
    merged = {a['id']: dict(a) for a in simulation.get('fleet', []) if a.get('id')}
    for a in state.get('aircraft', []):
        if a.get('id'):
            row = merged.setdefault(a['id'], {})
            row.update(a)
            if a.get('session_id') and simulation.get('id') and a['session_id'] != simulation['id']:
                row['stale'] = True
                row['source_note'] = '旧会话遥测'
            if isinstance(a.get('speed_mps'), (int, float)):
                row['measured_ground_speed_mps'] = a['speed_mps']
            coords = [a.get('longitude'), a.get('latitude'), origin.get('lon'), origin.get('lat')]
            if all(isinstance(v, (int, float)) and math.isfinite(v) for v in coords):
                # Use the same GPS sample for both global coordinates and ENU.
                row['east_m'] = math.radians(coords[0]-coords[2])*6371000*math.cos(math.radians(coords[3]))
                row['north_m'] = math.radians(coords[1]-coords[3])*6371000
    for uid, detail in report.get('aircraft', {}).items():
        a = merged.setdefault(uid, {'id': uid})
        for key in ('armed', 'landed', 'relative_alt_m'):
            if key not in a and key in detail:
                a[key] = detail[key]
        if not a.get('flight_mode'):
            a['flight_mode'] = mode_name(detail.get('mode'))
    return [merged[uid] for uid in sorted(merged)]


def last_telemetry(path):
    """Bounded tail read; large flight logs do not grow monitor memory."""
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size-65536))
            data = stream.read()
        rows = {}
        for line in data.splitlines():
            try:
                row = json.loads(line)
                if row.get('id'):
                    rows[row['id']] = row
            except (ValueError, UnicodeError):
                continue
        return list(rows.values())
    except OSError:
        return []


class Source:
    def __init__(self, root=ROOT, url='http://127.0.0.1:11843'):
        self.root, self.url = Path(root), url.rstrip('/')
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get_json(self, path):
        with self.http.open(self.url+path, timeout=1) as response:
            return json.load(response)

    def snapshot(self):
        now = time.time()
        values, failures = {}, []
        for key, path in (('state', '/api/state?mode=live'), ('simulation', '/api/simulation')):
            try:
                value = self.get_json(path)
                if not isinstance(value, dict):
                    raise ValueError('invalid snapshot')
                values[key] = value
            except (OSError, ValueError) as exc:
                failures.append(key+': '+type(exc).__name__)
        if values:
            state, simulation = values.get('state', {}), values.get('simulation', {})
            return {'source': 'http', 'now': now, 'state': state, 'simulation': simulation,
                    'aircraft': merge_aircraft(state, simulation), 'warnings': failures}
        path = self.root/'runs/interactive-latest.json'
        try:
            report = json.loads(path.read_text())
            if not isinstance(report, dict):
                raise ValueError('invalid report')
            sid = report.get('session_id', '')
            # Session ids are generated locally; keep malformed records in root.
            safe_sid = sid if isinstance(sid, str) and sid.isalnum() and len(sid) <= 64 else ''
            telemetry = last_telemetry(self.root/'runs/interactive'/safe_sid/'telemetry.jsonl') if safe_sid else []
            fleet = []
            for a in telemetry:
                fleet.append(dict(a, longitude=a.get('lon'), latitude=a.get('lat'),
                                  age_s=max(0, now-a.get('timestamp', 0))))
            simulation = {'status': 'HTTP_UNREACHABLE', 'ready': False, 'report': report}
            return {'source': 'file', 'now': now, 'file_age_s': max(0, now-path.stat().st_mtime),
                    'state': {'aircraft': fleet}, 'simulation': simulation,
                    'aircraft': merge_aircraft({'aircraft': fleet}, simulation), 'warnings': failures}
        except (OSError, ValueError):
            return {'source': 'offline', 'now': now, 'state': {}, 'simulation': {}, 'aircraft': [], 'warnings': failures}

    def agent(self):
        directory = self.root/'runs/conversations'
        try:
            paths = sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            if not paths:
                return {'status': 'no_conversation'}
            path = paths[0]
            conversation = json.loads(path.read_text())
            age = max(0, time.time()-path.stat().st_mtime)
            activity = None
            try:
                activity = self.get_json('/api/agent/activity?conversation_id='+urllib.parse.quote(path.stem))
            except (OSError, ValueError):
                pass
            return {'status': 'available', 'conversation': conversation, 'age_s': age, 'activity': activity}
        except (OSError, ValueError):
            return {'status': 'unavailable'}


class TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        return http.client.HTTPConnection(host, timeout=1)


class Mavros:
    """Read existing ROS topics. No publishers, services or MAVLink sockets."""
    def __init__(self, uri=None):
        self.uri = uri or os.environ.get('ROS_MASTER_URI', 'http://localhost:11311')
        self.last = 0
        self.cached = {}

    def snapshot(self):
        if self.cached and time.monotonic()-self.last < 5:
            return self.cached
        self.last = time.monotonic()
        try:
            master = xmlrpc.client.ServerProxy(self.uri, transport=TimeoutTransport(), allow_none=True)
            code, _, published = master.getPublishedTopics('/fieldlink_monitor', '')
            if code != 1:
                raise OSError('master rejected read')
            topics = [(name, kind) for name, kind in published if '/mavros/' in name or name.startswith('/mavros/')]
            state_topics = [name for name, kind in topics if kind == 'mavros_msgs/State']
            result = {'master': True, 'topics': topics, 'messages': {}, 'observed_at': time.time()}
            binary = shutil.which('rostopic')
            if state_topics and binary:
                def read(topic):
                    try:
                        response = subprocess.run([binary, 'echo', '-n', '1', topic], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=2, check=False)
                        return topic, clean(response.stdout, 1200) if response.returncode == 0 else '未读到消息：'+clean(response.stderr, 160)
                    except (OSError, subprocess.TimeoutExpired):
                        return topic, '2秒内未读到新消息'
                with ThreadPoolExecutor(max_workers=3) as pool:
                    result['messages'] = dict(pool.map(read, state_topics[:3]))
            elif state_topics:
                result['note'] = 'rostopic 不在 PATH；仅确认话题已注册，未读取消息内容'
        except (OSError, xmlrpc.client.Error, ValueError):
            result = {'master': False, 'topics': [], 'messages': {}, 'observed_at': time.time()}
        self.cached = result
        return result


def header(snapshot, view):
    stamp = datetime.fromtimestamp(snapshot['now'], CHINA).strftime('%Y-%m-%d %H:%M:%S +0800')
    source = {'http': 'HTTP 服务可达 / MAVLink快照（逐机标注LIVE或STALE；不代表全部在线）', 'file': 'FILE 历史/现场记录：HTTP不可达，未确认当前连接', 'offline': 'OFFLINE 未收到状态'}[snapshot['source']]
    lines = ['FieldLink | '+view.upper()+' | '+stamp, '数据源：'+source]
    if snapshot['source'] == 'file':
        lines.append('记录文件年龄 '+number(snapshot.get('file_age_s'), 1, 's')+'；历史值不能证明当前仍在飞行')
    if snapshot.get('warnings'):
        lines.append('接口：'+'; '.join(snapshot['warnings']))
    return lines


def fleet_lines(snapshot, view):
    lines = []
    for a in snapshot['aircraft']:
        age = record_age(a, snapshot['now'])
        fresh = is_fresh(a, snapshot)
        stamp = 'LIVE' if fresh else 'STALE' if snapshot['source'] == 'http' else 'HISTORY'
        prefix = '%s [%s age=%s]' % (a['id'], stamp, number(age, 1, 's'))
        if view == 'state':
            lines.append(prefix+' connected='+('true' if fresh else 'UNKNOWN' if snapshot['source'] != 'http' else 'false')+
                         ' armed='+boolean(a.get('armed'))+' mode='+mode_name(a.get('flight_mode'))+
                         ' landed='+boolean(a.get('landed'))+' | '+stage(a))
        elif view == 'position':
            lines += [prefix+' 经/纬度 '+number(a.get('longitude'), 7)+', '+number(a.get('latitude'), 7),
                      '  ENU 东 '+number(a.get('east_m'), 1, 'm')+' 北 '+number(a.get('north_m'), 1, 'm')+
                      ' | AMSL '+number(a.get('altitude_m'), 1, 'm')+' 相对起飞点 '+number(a.get('relative_alt_m'), 1, 'm')+
                      ' | 地速 '+number(a.get('measured_ground_speed_mps', a.get('speed_mps')), 1, 'm/s')]
        elif view == 'battery':
            battery_age = a.get('battery_telemetry_age_s', age)
            battery = a.get('battery_pct')
            usable = fresh and isinstance(battery_age, (int, float)) and battery_age < 3
            lines.append(prefix+' 电量 '+(number(battery, 0, '%') if usable else 'UNKNOWN')+
                         ' | 电池消息年龄 '+number(battery_age, 1, 's')+
                         (' | 低于任务阈值25%' if usable and isinstance(battery, (int, float)) and battery < 25 else ''))
    if not lines:
        lines.append('等待飞机遥测；没有用演示数据填充。')
    return lines


def mission_lines(snapshot):
    simulation = snapshot['simulation']
    report = simulation.get('report', {})
    tasks = report.get('tasks', [])
    detail = report.get('aircraft', {})
    visited = sum(len(a.get('visited', {})) for a in detail.values())
    lines = ['仿真 '+str(simulation.get('status', 'UNKNOWN'))+' | 任务 '+str(report.get('status', 'UNKNOWN')),
             'session='+str(report.get('session_id', simulation.get('id', '—')))+
             ' execution='+str(report.get('execution_id', '—')),
             '到点 %d/%d | success=%s | operation=%s' % (visited, len(tasks), boolean(report.get('success')), report.get('operation', '—'))]
    if report.get('error'):
        lines.append('ERROR '+clean(report['error']))
    labels = {t['id']: t.get('label', t['id']) for t in tasks}
    allocation = report.get('allocation', {})
    lines.append('分配算法 '+str(allocation.get('algorithm_id', allocation.get('algorithm', '—'))))
    for uid, route in allocation.get('assignments', {}).items():
        a = detail.get(uid, {})
        control = report.get('controls', {}).get(uid, {})
        mode_ack, arm_ack = a.get('mode_ack') or {}, a.get('arm_ack') or {}
        lines += [uid+' : '+(' → '.join(labels.get(tid, tid) for tid in route) or '待命'),
                  '  upload='+boolean(a.get('mission_accepted'))+' mode_ACK='+str(mode_ack.get('result', '—'))+
                  ' arm_ACK='+str(arm_ack.get('result', '—'))+' visited=%d/%d seq=%s' % (len(a.get('visited', {})), len(route), a.get('mission_current', '—'))]
        if control:
            lines.append('  控制 '+str(control.get('action'))+' ACK='+str((control.get('ack') or {}).get('result', '—')))
    lines.append('ACK只表示接收；完成以到点遥测和任务状态为准。')
    return lines


def agent_lines(value):
    if value.get('status') != 'available':
        return ['尚无可读取的模型会话记录；等待用户在前端发出需求。']
    conversation = value['conversation']
    activity = value.get('activity') or {}
    lines = ['最近持久化会话 '+str(conversation.get('id', '—'))+' | 文件年龄 '+number(value.get('age_s'), 1, 's'),
             '模型活动 '+str(activity.get('status', 'HTTP不可达/未知'))+'；首次工具回执前，新会话可能尚未写入文件。']
    usage=activity.get('model_usage') or conversation.get('last_model_usage',[])
    if usage:
        latest=usage[-1];packing=latest.get('packing',{})
        lines.append('模型实际用量：输入 '+number(latest.get('prompt_eval_count'),0)+
                     ' / 窗口 '+number(latest.get('num_ctx'),0)+' token，生成 '+number(latest.get('eval_count'),0)+
                     '，预留生成 '+number(latest.get('reserved_generation_tokens'),0)+
                     '；结束原因 '+str(latest.get('done_reason','UNKNOWN')))
        lines.append('打包估算输入 '+number(packing.get('estimated_input_tokens'),0)+
                     ' token（估算值）；省略历史回合 '+number(packing.get('history_turns_omitted'),0))
    turns = conversation.get('turns', [])
    turn = conversation.get('pending_turn') or (turns[-1] if turns else [])
    user = next((m.get('content', '') for m in turn if m.get('role') == 'user'), '')
    if user:
        lines.append('用户：'+clean(user, 600))
    events = activity.get('events', [])
    if events:
        unique = {}
        for event in events:
            unique[(event.get('tool'), event.get('started_at'))] = event
        for event in list(unique.values())[-12:]:
            payload = event.get('result') or {}
            lines.append('  '+str(event.get('tool'))+' ['+str(event.get('status'))+'] '+clean(payload.get('error', ''), 280))
    else:
        for message in turn[-24:]:
            if message.get('role') == 'assistant' and message.get('tool_calls'):
                for call in message['tool_calls']:
                    function = call.get('function', {})
                    lines.append('  CALL '+clean(function.get('name', ''), 70))
            if message.get('role') == 'tool':
                try:
                    result = json.loads(message.get('content', '{}'))
                    lines.append('  '+str(message.get('tool_name'))+' ['+('success' if result.get('ok') else 'error')+'] '+clean(result.get('error', ''), 280))
                except ValueError:
                    lines.append('  工具结果无法解析')
            if message.get('role') == 'assistant' and not message.get('tool_calls') and message.get('content'):
                lines.append('回复：'+clean(message['content'], 900))
    return lines


def mavros_lines(value):
    lines = ['此页检查真实 ROS1/MAVROS；不启动 ROS Master/MAVROS，不占用 SITL UDP 端口。',
             '当前主流程使用直接 MAVLink；STATE/POSITION/BATTERY 页使用同一份实际遥测。']
    if not value.get('master'):
        return lines+['ROS Master 未连接；MAVROS state 未订阅，不能将此状态解释为飞机掉线。']
    if not value.get('topics'):
        return lines+['ROS Master 可达，但没有已发布的 MAVROS 话题。']
    lines.append('以下为已注册发布话题；注册不保证消息新鲜：')
    lines += ['  '+clean(name, 120)+' ['+clean(kind, 80)+']' for name, kind in value['topics'][:30]]
    for topic, message in value.get('messages', {}).items():
        lines.append('\n现有状态话题 '+clean(topic, 120)+'：\n'+message)
    if value.get('note'):
        lines.append(value['note'])
    return lines


def render(snapshot, view, agent=None, mavros=None):
    lines = header(snapshot, view)
    if view in ('overview', 'state'):
        lines += ['\nSTATE (MAVROS风格字段，实际数据源为直接 MAVLink)']+fleet_lines(snapshot, 'state')
    if view in ('overview', 'position'):
        lines += ['\nPOSITION | ENU相对仿真原点，AMSL与相对高度分别显示']+fleet_lines(snapshot, 'position')
    if view in ('overview', 'battery'):
        lines += ['\nBATTERY | 未知或过期电池消息不显示为0%']+fleet_lines(snapshot, 'battery')
    if view in ('overview', 'mission'):
        lines += ['\nMISSION']+mission_lines(snapshot)
    if view == 'agent':
        lines += ['\nQWEN 工具调用']+agent_lines(agent or {})
    if view == 'mavros':
        lines += ['\nROS1 / MAVROS']+mavros_lines(mavros or {})
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--view', choices=VIEWS, default='overview')
    parser.add_argument('--url', default='http://127.0.0.1:11843')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval < .2:
        parser.error('--interval must be at least 0.2 seconds')
    source, mavros = Source(args.root, args.url), Mavros()
    try:
        while True:
            began = time.monotonic()
            snapshot = source.snapshot()
            agent = source.agent() if args.view == 'agent' else None
            ros = mavros.snapshot() if args.view == 'mavros' else None
            if sys.stdout.isatty() and not args.once:
                print('\033[H\033[2J', end='')
            print(render(snapshot, args.view, agent, ros), end='', flush=True)
            if args.once:
                return 0
            time.sleep(max(.05, args.interval-(time.monotonic()-began)))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
