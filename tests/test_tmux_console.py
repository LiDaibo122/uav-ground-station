import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from packages.swarm_sim.tmux_ui import ROOT,TmuxConsole,worker_arguments


class TmuxArgumentsTests(unittest.TestCase):
    def test_worker_preserves_simulation_options_without_recursing(self):
        self.assertEqual(worker_arguments(['--hold','--gui','--tmux','--tmux-detached',
            '--tmux-session','custom','--tmux-socket=/tmp/example','--qgc-host','auto','--timeout','123']),
            ['--hold','--gui','--timeout','123','--no-tmux'])


@unittest.skipUnless(os.environ.get('FIELDLINK_RUN_TMUX_TESTS')=='1' and shutil.which('tmux'),
                     'Opt-in isolated tmux process test: FIELDLINK_RUN_TMUX_TESTS=1')
class TmuxProcessTests(unittest.TestCase):
    def test_windows_reuse_and_restart_only_dead_worker(self):
        with tempfile.TemporaryDirectory(prefix='fieldlink-tmux-') as directory:
            directory=Path(directory);name='test-'+uuid4().hex[:8]
            console=TmuxConsole(ROOT,name,socket=directory/'tmux.sock')
            marker=directory/'worker.pid'
            worker=[sys.executable,'-c',
                'import os,time;from pathlib import Path;Path('+repr(str(marker))+').write_text(str(os.getpid()));time.sleep(300)']
            def wait(predicate):
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.05)
                self.fail('tmux worker did not reach expected state')
            try:
                self.assertTrue(console.ensure(worker)['created'])
                wait(marker.exists)
                pid=marker.read_text()
                windows=console.run('list-windows','-t',name,'-F','#{window_index}:#{window_name}').stdout.splitlines()
                self.assertEqual(windows,['0:run','1:overview','2:state','3:position','4:battery','5:mission','6:qwen','7:mavros'])
                self.assertFalse(console.ensure(worker)['created'])
                self.assertEqual(marker.read_text(),pid)
                console.run('send-keys','-t',name+':run','C-c')
                wait(lambda:console.run('display-message','-p','-t',name+':run','#{pane_dead}').stdout.strip()=='1')
                self.assertTrue(console.ensure(worker)['worker_restarted'])
                wait(lambda:marker.read_text()!=pid)
                panes=console.run('list-panes','-s','-t',name,'-F','#{pane_dead}').stdout.splitlines()
                self.assertTrue(all(p=='0' for p in panes))
                # A different project cannot adopt or replace an existing session.
                other=TmuxConsole(directory,name,socket=directory/'tmux.sock')
                with self.assertRaisesRegex(RuntimeError,'不属于此项目'):other.ensure(worker)
            finally:
                console.run('kill-server',check=False)
                (ROOT/'runs/tmux'/(name+'.json')).unlink(missing_ok=True)


if __name__=='__main__':unittest.main()
