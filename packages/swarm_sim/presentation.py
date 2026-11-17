"""Owned desktop views for the already-running Gazebo/PX4 experiment."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from uuid import uuid4

from .sitl import ROOT

URL='http://127.0.0.1:11843/?mode=live&swarm=1'


class DesktopViews:
    def __init__(self, px4=Path('/home/ldb/PX4-Autopilot')):
        self.px4=Path(px4)
        self.directory=ROOT/'runs/views'/uuid4().hex[:12]
        self.directory.mkdir(parents=True,exist_ok=True)
        self.processes=[]

    def _spawn(self, name, command, env=None):
        with (self.directory/(name+'.log')).open('w') as log:
            process=subprocess.Popen(command,cwd=self.directory,env=env,
                         stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        self.processes.append((name,process))
        (self.directory/'processes.json').write_text(json.dumps({
            'url':URL,'processes':[{'name':n,'pid':p.pid} for n,p in self.processes]},indent=2))
        return process

    def open(self, gazebo=False, browser=False):
        if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
            raise RuntimeError('没有图形显示。请在 Ubuntu 桌面终端运行 --gui --browser；SSH 下可先使用远程桌面。')
        if gazebo:
            if not shutil.which('gzclient'):
                raise RuntimeError('未找到 gzclient，请安装 Gazebo Classic 图形客户端。')
            env=dict(os.environ)
            env.update(GAZEBO_MASTER_URI='http://127.0.0.1:11359',GAZEBO_MODEL_DATABASE_URI='')
            if env.get('DISPLAY'):
                env['QT_QPA_PLATFORM']='xcb'
            for key,path in [('GAZEBO_MODEL_PATH',self.px4/'Tools/sitl_gazebo/models'),
                             ('GAZEBO_PLUGIN_PATH',self.px4/'build/px4_sitl_default/build_gazebo'),
                             ('LD_LIBRARY_PATH',self.px4/'build/px4_sitl_default/build_gazebo')]:
                env[key]=str(path)+(':'+env[key] if env.get(key) else '')
            self._spawn('gzclient',['gzclient','--verbose'],env)
        if browser:
            # A private profile makes this window independently closable without
            # affecting any of the operator's existing browser windows.
            candidates=[shutil.which(n) for n in ['chromium','chromium-browser','google-chrome']]
            candidates+=list((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux/chrome'))
            binary=next((str(p) for p in candidates if p and Path(p).is_file()),None)
            if binary is None:
                raise RuntimeError('未找到 Chromium。请手动打开 '+URL)
            self._spawn('browser',[binary,'--no-sandbox','--disable-dev-shm-usage',
                         '--no-first-run','--no-default-browser-check',
                         '--user-data-dir='+str(self.directory/'browser-profile'),
                         '--new-window','--window-size=1100,1000','--window-position=0,0',URL])
        time.sleep(2)
        failures=[name for name,p in self.processes if p.poll() is not None]
        if failures:
            raise RuntimeError('窗口启动后退出：'+', '.join(failures)+'；日志见 '+str(self.directory))
        print('Desktop view logs: '+str(self.directory),flush=True)

    def close(self):
        for _,process in reversed(self.processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for _,process in reversed(self.processes):
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
