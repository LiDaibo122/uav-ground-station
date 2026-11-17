"""Shutdown tests use isolated dummy processes, never PX4 or user services."""
import importlib.util
from pathlib import Path
import subprocess
import shutil
import uuid
import json
import sys
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('stop_system', Path(__file__).resolve().parents[1] / 'scripts/stop_system.py')
shutdown = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shutdown)


class ShutdownTests(unittest.TestCase):
    def test_project_commands_and_unrelated_python(self):
        root = Path('/tmp/isolated-fieldlink')
        def row(args, cwd=root):
            return {'args': args, 'cwd': cwd}
        self.assertTrue(shutdown.owned(row(['python3', '-m', 'packages.swarm_sim.demo', '--hold']), root, True))
        self.assertTrue(shutdown.owned(row(['python3', str(root/'server/app.py')], Path('/tmp')), root, True))
        self.assertFalse(shutdown.owned(row(['python3', '-c', 'import time;time.sleep(99)']), root, True))
        self.assertFalse(shutdown.owned(row(['python3', '-c', 'server/app.py']), root, True))
        self.assertFalse(shutdown.owned(row(['python3', '-m', 'packages.swarm_sim.demo'], Path('/other')), root, True))
        self.assertFalse(shutdown.owned(row(['gzserver'], Path('/tmp/isolated-fieldlink-other/runs')), root, True))
        self.assertTrue(shutdown.owned(row(['gzserver'], root/'runs/interactive/abc'), root, True))

    @unittest.skipUnless(shutil.which('tmux'), 'tmux not installed')
    def test_tmux_marker_leaves_other_session_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = ['tmux', '-L', 'stop-test-'+uuid.uuid4().hex[:12]]
            def run(*args):
                return subprocess.run(prefix+list(args), capture_output=True, text=True, check=True)
            try:
                run('new-session', '-d', '-s', 'owned', 'sleep 90')
                run('set-option', '-t', 'owned', '@fieldlink_root', str(root))
                run('new-session', '-d', '-s', 'unrelated', 'sleep 90')
                sock = run('display-message', '-p', '-t', 'owned', '#{socket_path}').stdout.strip()
                metadata = root/'runs/tmux'
                metadata.mkdir(parents=True)
                (metadata/'test.json').write_text(json.dumps({'root': str(root), 'socket': sock}))
                found = shutdown.tmux_sessions(root)
                self.assertEqual([name for _, name in found], ['owned'])
                for command, name in found:
                    subprocess.run(command+['kill-session', '-t', '='+name], check=True)
                self.assertEqual(run('list-sessions', '-F', '#{session_name}').stdout.strip(), 'unrelated')
            finally:
                subprocess.run(prefix+['kill-server'], capture_output=True)

    def test_stale_identity_is_never_signalled(self):
        p = shutdown.process(__import__('os').getpid())
        p['start'] = 'old-reused-pid'
        self.assertFalse(shutdown.same(p))
        shutdown.send(p, __import__('signal').SIGTERM)

    def test_actual_cleanup_descendants_and_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'server').mkdir()
            (root/'server/app.py').write_text('import signal,subprocess,sys,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(90)"])\nopen("child.pid","w").write(str(p.pid))\ntime.sleep(90)\n')
            owned = subprocess.Popen([sys.executable, str(root/'server/app.py')], cwd=root)
            other = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(90)'], cwd=root)
            child = None
            try:
                for _ in range(100):
                    try:
                        child = int((root/'child.pid').read_text())
                        break
                    except (OSError, ValueError):
                        time.sleep(.02)
                self.assertIsNotNone(child)
                selected = shutdown.discover(root, keep_model=True)
                self.assertIn(owned.pid, selected)
                self.assertIn(child, selected)
                self.assertNotIn(other.pid, selected)
                self.assertEqual(shutdown.stop_processes(selected, root, keep_model=True, grace=.3), [])
                owned.wait(timeout=2)
                self.assertIsNone(other.poll())
                self.assertEqual(shutdown.discover(root, keep_model=True), {})
            finally:
                for p in (owned, other):
                    if p.poll() is None:
                        p.kill()
                    p.wait(timeout=2)
                if child and shutdown.process(child):
                    __import__('os').kill(child, 9)


if __name__ == '__main__':
    unittest.main()
