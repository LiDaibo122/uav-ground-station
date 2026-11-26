"""Persistent local task-clock playback. Tracks inspection work separately from flight telemetry."""
import json
import re
import threading
import time

from packages.ground_core.inspection_rehearsal import advance, tick


class LibraryReplay:
    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.RLock()

    def path(self, key):
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{32}', key):
            raise ValueError('任务演练编号无效')
        return self.directory / (key + '.json')

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
        tmp.replace(path)

    def start(self, plan):
        with self.lock:
            path = self.path(plan['id'])
            if path.exists():
                return self.status(plan['id'])
            if plan['unassigned'] or not any(plan['assignments'].values()):
                raise ValueError('有任务尚未分配，请调整分组')
            now = time.time()
            state = dict(revision=1, events=[], tasks={}, lanes={})
            for t in plan['tasks']:
                state['tasks'][t['id']] = dict(label=t['label'], kind='area' if t.get('area') else 'point',
                                                    center=[t['east_m'],t['north_m']], area=t.get('area'), status='pending')
            for uid, queue in plan['assignments'].items():
                if queue:
                    state['lanes'][uid] = dict(queue=list(queue), wait=False, route=list(queue),
                                              origin=plan.get('origins', {}).get(uid, [0,0]))
                    advance(state, uid, now)
            self.save(path, state)
            return state

    def status(self, key):
        with self.lock:
            path = self.path(key)
            if not path.exists():
                raise ValueError('本次任务演练还未开始')
            state = json.loads(path.read_text(encoding='utf8'))
            revision = state['revision']
            tick(state, time.time())
            if revision != state['revision']:
                self.save(path, state)
            return state
