#!/usr/bin/env python3
"""Stop FieldLink-owned Linux processes, checking identity before each signal."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def inside(path, directory):
    return path == directory or directory in path.parents


def process(pid):
    try:
        proc = Path('/proc', str(pid))
        if proc.stat().st_uid != os.getuid():
            return None
        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        return {'pid': int(pid), 'ppid': int(stat[1]), 'start': stat[19],
                'cwd': (proc / 'cwd').resolve(strict=True),
                'args': (proc / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')}
    except (OSError, ValueError, IndexError):
        return None


def owned(p, root, keep_model):
    args = p['args']
    if not args:
        return False
    cwd = p['cwd']
    executable = Path(args[0]).name
    project = inside(cwd, root)
    if executable.startswith('python'):
        modules = ('packages.swarm_sim.demo', 'packages.swarm_sim.monitor', 'packages.swarm_sim.qgc_bridge')
        if any(args[i] == '-m' and args[i+1] in modules for i in range(len(args)-1)) and project:
            return True
        # Only the script argument is executable code; later arguments may be data.
        script_args = args[1:]
        while script_args and script_args[0] in ('-u', '-B', '-E', '-s'):
            script_args = script_args[1:]
        if script_args and not script_args[0].startswith('-'):
            path = (cwd / script_args[0]).resolve()
            if path in (root / 'server/app.py', root / 'scripts/ros_telemetry_bridge.py'):
                return True
    # Simulator and private desktop profiles run in dedicated project directories.
    if inside(cwd, root / 'runs'):
        if executable in ('px4', 'gzserver', 'gzclient'):
            return True
        if inside(cwd, root / 'runs/views') and executable in ('chrome', 'chromium', 'chromium-browser'):
            return True
    model_process = (executable == 'ollama' and len(args)>1 and args[1] in ('serve', 'runner')) or executable == 'llama-server'
    if not keep_model and model_process:
        try:
            env = dict(item.split('=', 1) for item in Path('/proc', str(p['pid']), 'environ').read_text().split('\0') if '=' in item)
            # Exact endpoint used by this machine's ollama-start helper.
            return env.get('OLLAMA_HOST') == '127.0.0.1:11434' and env.get('OLLAMA_MODELS') == str(Path.home() / '.ollama/models')
        except OSError:
            pass
    return False


def discover(root=ROOT, keep_model=False):
    table = {p['pid']: p for entry in Path('/proc').iterdir() if entry.name.isdigit()
             for p in [process(int(entry.name))] if p}
    excluded = {os.getpid()}
    parent = os.getppid()
    while parent in table and parent not in excluded:
        excluded.add(parent)
        parent = table[parent]['ppid']
    chosen = {pid: p for pid, p in table.items() if pid not in excluded and owned(p, root, keep_model)}
    while True:
        children = {pid: p for pid, p in table.items() if pid not in chosen and pid not in excluded and p['ppid'] in chosen}
        if not children:
            break
        chosen.update(children)
    return chosen


def same(p):
    current = process(p['pid'])
    return current is not None and current['start'] == p['start']


def send(p, sig):
    if same(p):
        try:
            os.kill(p['pid'], sig)
        except ProcessLookupError:
            pass


def tmux_sessions(root=ROOT):
    sockets = {None}
    for file in (root / 'runs/tmux').glob('*.json'):
        try:
            meta = json.loads(file.read_text())
            if meta.get('root') == str(root) and meta.get('socket'):
                sockets.add(meta['socket'])
        except (OSError, ValueError):
            pass
    found = {}
    for sock in sockets:
        prefix = ['tmux'] + (['-S', sock] if sock else [])
        try:
            result = subprocess.run(prefix + ['list-sessions', '-F', '#{session_name}'], capture_output=True, text=True, timeout=3)
            for name in result.stdout.splitlines():
                # tmux 3.0a show-options does not resolve the '=' exact-match prefix.
                marker = subprocess.run(prefix + ['show-options', '-t', name, '-qv', '@fieldlink_root'], capture_output=True, text=True, timeout=3)
                if marker.stdout.strip() == str(root):
                    found[(sock, name)] = prefix
        except (OSError, subprocess.TimeoutExpired):
            continue
    return [(prefix, name) for (_, name), prefix in found.items()]


def stop_processes(targets, root=ROOT, keep_model=False, grace=8):
    # Parents first: prevent a supervisor from spawning replacement children.
    for p in targets.values():
        send(p, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        # Capture children born during shutdown and preserve their identities.
        for pid, p in discover(root, keep_model).items():
            if pid not in targets or not same(targets[pid]):
                targets[pid] = p
                send(p, signal.SIGTERM)
        if not any(same(p) for p in targets.values()):
            break
        time.sleep(.2)
    for p in targets.values():
        send(p, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while any(same(p) for p in targets.values()) and time.monotonic() < deadline:
        time.sleep(.1)
    return [p for p in targets.values() if same(p)]


def main():
    parser = argparse.ArgumentParser(description='停止本项目的仿真、地面站、监视进程和本地 Qwen 服务')
    parser.add_argument('--status', '--dry-run', action='store_true', help='只列出状态，不停止进程')
    parser.add_argument('--keep-model', action='store_true', help='保留本机 Ollama/Qwen 服务')
    args = parser.parse_args()
    targets = discover(keep_model=args.keep_model)
    sessions = tmux_sessions()
    print('发现 %d 个相关进程，%d 个项目 tmux 会话。' % (len(targets), len({name for _, name in sessions})), flush=True)
    for p in targets.values():
        print('  PID %-7d %s' % (p['pid'], ' '.join(p['args'])[:220]), flush=True)
    for name in sorted({name for _, name in sessions}):
        print('  tmux 会话：'+name, flush=True)
    if args.status:
        print('仅查看状态，未停止任何进程。' if targets or sessions else '当前没有本系统运行进程。')
        return 0
    remaining = stop_processes(targets, keep_model=args.keep_model)
    # Write the result before removing the terminal session, which may contain the caller.
    report = {'time': time.time(), 'stopped_pids': [p['pid'] for p in targets.values() if not same(p)],
              'remaining_pids': [p['pid'] for p in remaining], 'keep_model': args.keep_model,
              'tmux_sessions': sorted({name for _, name in sessions})}
    directory = ROOT / 'runs'
    directory.mkdir(exist_ok=True)
    report_path = directory / 'shutdown-latest.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    errors = []
    for prefix, name in sessions:
        result = subprocess.run(prefix + ['kill-session', '-t', '='+name], capture_output=True, text=True, timeout=5)
        if result.returncode and 'no server running' not in result.stderr and 'no such file' not in result.stderr.lower() and "can't find session" not in result.stderr:
            errors.append(result.stderr.strip())
    remaining = [p for p in remaining if same(p)]
    report.update(remaining_pids=[p['pid'] for p in remaining], tmux_errors=errors)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if remaining or errors:
        print('部分进程未能停止，请查看：'+str(report_path), flush=True)
        return 1
    print('已停止本系统相关进程。Windows QGC 和普通浏览器窗口由你自行关闭。', flush=True)
    print('停止记录：'+str(report_path), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
