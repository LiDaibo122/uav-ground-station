"""Versioned map tasks for a single XD run."""
import json
import re
import threading

from packages.ground_core.inspection_rehearsal import geometry

MAX_LIBRARY_TASKS = 100


class TaskLibraryConflict(ValueError):
    pass


class TaskLibrary:
    def __init__(self, path, run_id):
        self.path = path
        self.run_id = run_id
        self.lock = threading.RLock()

    def _read(self):
        if self.path.exists():
            return json.loads(self.path.read_text(encoding='utf-8'))
        return {'run_id': self.run_id, 'revision': 0, 'tasks': []}

    def read(self, run_id=None, revision=None):
        with self.lock:
            current = self._read()
            if run_id is not None and (run_id != self.run_id or current['revision'] != revision):
                raise TaskLibraryConflict('任务库版本或仿真场次已变化，请核对当前列表后重试')
            return current

    def save(self, data):
        tasks = data.get('tasks')
        if not isinstance(tasks, list) or len(tasks) > MAX_LIBRARY_TASKS:
            raise ValueError(f'任务库最多可保存 {MAX_LIBRARY_TASKS} 项')
        records = []
        ids, labels = set(), set()
        for task in tasks:
            shape = geometry(task)
            key, label = task.get('id'), task.get('label')
            if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f-]{36}', key):
                raise ValueError('任务编号无效')
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40 or not label.isprintable():
                raise ValueError('任务名称须为 1 至 40 个可见字符')
            label = label.strip()
            if key in ids or label in labels:
                raise ValueError('任务编号或名称重复')
            ids.add(key)
            labels.add(label)
            records.append(dict(shape, id=key, label=label))

        with self.lock:
            current = self._read()
            if (data.get('run_id') != self.run_id or type(data.get('revision')) is not int
                    or data['revision'] != current['revision']):
                raise TaskLibraryConflict('任务库已更新。请重新加载最新版本后再编辑')
            if records == current['tasks']:
                return current
            saved = dict(run_id=self.run_id, revision=current['revision'] + 1, tasks=records)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_suffix('.tmp')
            pending.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
            pending.replace(self.path)
            return saved

def map_text(library):
    lines = []
    for task in library['tasks']:
        if task['kind'] == 'area':
            lines.append('{label}（巡逻区） 西 {west}、东 {east}、南 {south}、北 {north}'.format(**task))
        else:
            lines.append('{label}（任务点） 东 {east}、北 {north}'.format(**task))
    return '仿真 world，单位米。任务库版本 %d。\n' % library['revision'] + '\n'.join(lines)


def library_selection(tasks, groups, vehicles, selected=None):
    """Each explicit group restricts the auction for exactly the named work."""
    by_name = {t['label']: t for t in tasks}
    allowed = {}
    for group in groups:
        members = group['vehicles']
        if not members or set(members) - set(vehicles) or (selected and set(members) != {selected}):
            raise ValueError('分组包含未知机号或超出当前单机窗口')
        names = list(group.get('labels', []))
        kind = group.get('kind')
        if names and kind and any(by_name.get(name, {}).get('kind') != kind for name in names):
            raise ValueError('任务名称与指定类型不匹配，请核对点和区域')
        if not names and kind:
            names = [t['label'] for t in tasks if t['kind'] == kind]
        if not names or set(names) - set(by_name):
            raise ValueError('任务分组为空或名称未在当前任务库中找到')
        for name in names:
            if name in allowed and set(allowed[name]) != set(members):
                raise ValueError(name + ' 被指定给两个不同的组')
            allowed[name] = list(dict.fromkeys(members))
    chosen = [t for t in tasks if not groups or t['label'] in allowed]
    if selected:
        allowed = {t['label']: [selected] for t in chosen}
    return chosen, allowed
