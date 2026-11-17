"""Own a project tmux session without replacing existing simulator processes."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
WINDOWS=(('overview','overview'),('state','state'),('position','position'),
         ('battery','battery'),('mission','mission'),('qwen','agent'),('mavros','mavros'))
ENVIRONMENT=('PATH','DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','QT_QPA_PLATFORM',
             'LIBGL_ALWAYS_SOFTWARE','GAZEBO_IP','GAZEBO_MASTER_URI','GROUND_QWEN_MODEL',
             'GROUND_OLLAMA_URL','ROS_MASTER_URI','ROS_IP','ROS_HOSTNAME','PYTHONPATH')


def worker_arguments(arguments):
    result=[];skip=False
    for argument in arguments:
        if skip:skip=False;continue
        if argument in ('--tmux-session','--tmux-socket','--qgc-host'):
            skip=True;continue
        if argument.startswith(('--tmux-session=','--tmux-socket=','--qgc-host=')):
            continue
        if argument in ('--tmux','--tmux-detached','--no-tmux'):
            continue
        result.append(argument)
    return result+['--no-tmux']


def shell_command(command,environment=None):
    environment=os.environ if environment is None else environment
    prefix=['env']+[key+'='+environment[key] for key in ENVIRONMENT if key in environment]
    return 'exec '+' '.join(shlex.quote(str(value)) for value in prefix+list(command))


class TmuxConsole:
    def __init__(self,root=ROOT,name='fieldlink',socket=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',name):
            raise ValueError('tmux 会话名仅支持字母、数字、下划线和短横线')
        if not shutil.which('tmux'):
            raise RuntimeError('未找到 tmux，请先安装 tmux；临时使用 --no-tmux 可按原方式启动')
        self.root=Path(root).resolve();self.name=name
        self.prefix=['tmux']+(['-S',str(socket)] if socket else [])

    def run(self,*arguments,check=True):
        result=subprocess.run(self.prefix+list(arguments),text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if check and result.returncode:
            raise RuntimeError('tmux 操作失败：'+result.stderr.strip())
        return result

    def ensure_qgc(self,host):
        """Add the independent Windows UDP bridge; never touch the flight worker."""
        command=[sys.executable,'-m','packages.swarm_sim.qgc_bridge','--windows-host',host]
        windows=self.run('list-windows','-t',self.name,'-F','#{window_name}').stdout.splitlines()
        if 'qgc' in windows:
            target=self.name+':qgc'
            previous=self.run('show-options','-w','-t',target,'-qv','@fieldlink_qgc_host').stdout.strip()
            dead=self.run('display-message','-p','-t',target,'#{pane_dead}').stdout.strip()=='1'
            if not previous:
                raise RuntimeError('已有 qgc 窗口没有项目桥标识，请保留该窗口并使用其他会话名')
            if not dead and previous!=host:
                raise RuntimeError('QGC 桥正在连接 '+previous+'，请先在 qgc 窗口停止它，再切换地址')
            if dead:self.run('respawn-pane','-k','-t',target,'-c',str(self.root),shell_command(command))
        else:
            occupied=set(self.run('list-windows','-t',self.name,'-F','#{window_index}').stdout.splitlines())
            index=next(i for i in range(8,100) if str(i) not in occupied)
            target=self.name+':'+str(index)
            self.run('new-window','-d','-t',target,'-n','qgc','-c',str(self.root),shell_command(command))
            self.run('set-window-option','-t',target,'remain-on-exit','on')
            self.run('set-window-option','-t',target,'automatic-rename','off')
        self.run('set-window-option','-t',self.name+':qgc','@fieldlink_qgc_host',host)

    def ensure(self,worker_command,qgc_host=None):
        exists=self.run('has-session','-t','='+self.name,check=False).returncode==0
        if exists:
            marker=self.run('show-options','-t',self.name,'-qv','@fieldlink_root').stdout.strip()
            if marker!=str(self.root):
                raise RuntimeError('tmux 会话 '+self.name+' 不属于此项目，请用 --tmux-session 指定其他名称')
            dead=self.run('display-message','-p','-t',self.name+':run','#{pane_dead}').stdout.strip()
            if dead=='1':
                self.run('respawn-pane','-k','-t',self.name+':run','-c',str(self.root),shell_command(worker_command))
            if qgc_host:
                self.ensure_qgc(qgc_host)
                path=self.root/'runs/tmux'/(self.name+'.json')
                if path.exists():
                    metadata=json.loads(path.read_text())
                    metadata.update(qgc_host=qgc_host)
                    if 'qgc' not in metadata.get('windows',[]):metadata.setdefault('windows',[]).append('qgc')
                    path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
            return {'created':False,'session':self.name,'worker_restarted':dead=='1'}
        # Keep the initial pane alive until options and monitors are installed.
        placeholder=shell_command([sys.executable,'-c','import time; time.sleep(3600)'])
        self.run('new-session','-d','-s',self.name,'-n','run','-c',str(self.root),'-x','160','-y','48',placeholder)
        try:
            self.run('set-option','-t',self.name,'@fieldlink_root',str(self.root))
            self.run('set-option','-t',self.name,'base-index','0')
            index=self.run('display-message','-p','-t',self.name+':run','#{window_index}').stdout.strip()
            if index!='0':self.run('move-window','-s',self.name+':run','-t',self.name+':0')
            self.run('set-option','-t',self.name,'prefix','C-b')
            self.run('set-option','-t',self.name,'mouse','on')
            self.run('set-option','-t',self.name,'status-interval','1')
            self.run('set-option','-t',self.name,'status-left',' FieldLink | ')
            self.run('set-option','-t',self.name,'status-right',' Ctrl-b 数字 切换 | Ctrl-b d 脱离 | %H:%M:%S ')
            self.run('set-window-option','-t',self.name,'automatic-rename','off')
            self.run('set-window-option','-t',self.name,'remain-on-exit','on')
            for index,(name,view) in enumerate(WINDOWS,1):
                command=[sys.executable,'-m','packages.swarm_sim.monitor','--view',view,'--root',str(self.root)]
                self.run('new-window','-d','-t',self.name+':'+str(index),'-n',name,'-c',str(self.root),shell_command(command))
                self.run('set-window-option','-t',self.name+':'+str(index),'remain-on-exit','on')
                self.run('set-window-option','-t',self.name+':'+str(index),'automatic-rename','off')
            self.run('respawn-pane','-k','-t',self.name+':run','-c',str(self.root),shell_command(worker_command))
            if qgc_host:self.ensure_qgc(qgc_host)
            self.run('select-window','-t',self.name+':overview')
        except Exception:
            # Only this newly-created, marked session is ours to remove.
            self.run('kill-session','-t',self.name,check=False)
            raise
        metadata={'created':True,'session':self.name,'root':str(self.root),'started_at':time.time(),
                  'windows':['run']+[name for name,_ in WINDOWS],
                  'socket':self.run('display-message','-p','-t',self.name,'#{socket_path}').stdout.strip()}
        directory=self.root/'runs/tmux';directory.mkdir(parents=True,exist_ok=True)
        if qgc_host:metadata.update(qgc_host=qgc_host,windows=metadata['windows']+['qgc'])
        (directory/(self.name+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
        return metadata

    def show(self,detached=False):
        target=self.prefix+['attach-session','-t',self.name]
        print('tmux 会话：'+self.name+'；窗口：0 run / 1 overview / 2 state / 3 position / 4 battery / 5 mission / 6 qwen / 7 mavros',flush=True)
        print('重新进入：'+' '.join(shlex.quote(part) for part in target),flush=True)
        if 'qgc' in self.run('list-windows','-t',self.name,'-F','#{window_name}').stdout.splitlines():
            print('Windows QGC 桥位于 qgc 窗口（默认第 8 个）；Qwen 调度端口与 QGC 独立。',flush=True)
        if detached or not sys.stdin.isatty() or not sys.stdout.isatty():
            print('已在后台运行；脱离窗口不会停止仿真。',flush=True)
            return 0
        if os.environ.get('TMUX') and len(self.prefix)==1:
            target=self.prefix+['switch-client','-t',self.name]
        return subprocess.call(target)


def launch(arguments,name='fieldlink',socket=None,detached=False,qgc_host=None):
    console=TmuxConsole(name=name,socket=socket)
    command=[sys.executable,'-m','packages.swarm_sim.demo']+worker_arguments(arguments)
    console.ensure(command,qgc_host=qgc_host)
    return console.show(detached=detached)
