"""Route the existing PX4 GCS links to Windows QGC without taking API ports.

PX4 GCS endpoints are localhost:18571..18573 -> localhost:14550.  A separate
connected UDP uplink uses a fresh port to reach Windows:14550.  The original
MAVLink frame bytes, including signatures, are forwarded unchanged.  This bridge
does not generate heartbeats, parameters, modes, missions, or arm commands.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import time

from pymavlink.dialects.v20 import common as M

ROOT=Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINTS={2:('127.0.0.1',18571),3:('127.0.0.1',18572),4:('127.0.0.1',18573)}


def windows_host(value='auto'):
    if value!='auto':
        return socket.gethostbyname(value),'explicit Windows host'
    if not os.environ.get('WSL_DISTRO_NAME'):
        raise ValueError('auto 仅用于 WSL NAT；请用 --windows-host 指定 Windows IPv4')
    binary=shutil.which('wslinfo')
    if not binary:
        raise ValueError('无法确认 WSL 网络模式；请明确指定 --windows-host，不使用 DNS nameserver 猜测')
    mode=subprocess.check_output([binary,'--networking-mode'],text=True,timeout=3).strip().lower()
    if mode!='nat':
        raise ValueError('WSL 模式为 '+mode+'；不自动创建 localhost 自指桥，请先验证直连或明确指定 Windows 可达 IPv4')
    route=subprocess.check_output(['ip','-4','route','show','default'],text=True,timeout=3)
    for line in route.splitlines():
        parts=line.split()
        if 'via' in parts:
            address=parts[parts.index('via')+1]
            ipaddress.IPv4Address(address)
            return address,'WSL NAT default gateway (not resolv.conf nameserver)'
    raise ValueError('找不到 Windows NAT 网关，请用 --windows-host 指定')


class Router:
    def __init__(self,endpoints=None):
        self.endpoints=dict(DEFAULT_ENDPOINTS if endpoints is None else endpoints)
        self.parsers={}
        self.seen={}
        self.qgc_message_types={}
        self.qgc_command_ids={}
        self.qgc_target_systems={}
        self.qgc_target_message_types={}
        self.counts={'px4_frames':0,'qgc_frames':0,'invalid_frames':0,
                     'untrusted_source_dropped':0,'unknown_target_dropped':0,'uplink_errors':0}

    def frames(self,data,key):
        parser=self.parsers.get(key)
        if parser is None:
            parser=M.MAVLink(None);parser.robust_parsing=True;self.parsers[key]=parser
        try:messages=parser.parse_buffer(data) or []
        except (ValueError,M.MAVError):
            self.counts['invalid_frames']+=1;return []
        valid=[]
        for message in messages:
            if message.get_type()=='BAD_DATA':self.counts['invalid_frames']+=1
            else:valid.append(message)
        return valid

    def from_px4(self,data,source):
        expected=next((sid for sid,endpoint in self.endpoints.items() if tuple(source)==tuple(endpoint)),None)
        if expected is None:
            self.counts['untrusted_source_dropped']+=1;return []
        result=[]
        for message in self.frames(data,('px4',source)):
            if message.get_srcSystem()!=expected:
                # Ignore forwarded GCS/other-system packets, preventing feedback.
                continue
            self.counts['px4_frames']+=1
            row=self.seen.setdefault(str(expected),{'business_id':'FW-%02d'%(expected-1),'endpoint':list(source)})
            row['last_seen']=time.time()
            if message.get_type()=='HEARTBEAT':
                row['heartbeat']={'autopilot':message.autopilot,'type':message.type,
                                  'armed':bool(message.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED)}
            result.append(bytes(message.get_msgbuf()))
        return result

    def from_qgc(self,data):
        result=[]
        for message in self.frames(data,('qgc',)):
            self.counts['qgc_frames']+=1
            kind=message.get_type()
            self.qgc_message_types[kind]=self.qgc_message_types.get(kind,0)+1
            if hasattr(message,'command'):
                command=str(message.command)
                self.qgc_command_ids[command]=self.qgc_command_ids.get(command,0)+1
            target=getattr(message,'target_system',None)
            if target is None and message.get_type()=='MANUAL_CONTROL':target=message.target
            target=target or 0
            key=str(target)
            self.qgc_target_systems[key]=self.qgc_target_systems.get(key,0)+1
            target_types=self.qgc_target_message_types.setdefault(key,{})
            target_types[kind]=target_types.get(kind,0)+1
            destinations=list(self.endpoints.values()) if target==0 else [self.endpoints[target]] if target in self.endpoints else []
            if not destinations:self.counts['unknown_target_dropped']+=1
            raw=bytes(message.get_msgbuf())
            result.extend((destination,raw) for destination in destinations)
        return result


def bridge(host,port=14550,listen_address='127.0.0.1',listen_port=14550,
           uplink_address='0.0.0.0',uplink_port=0,duration=0,report=None):
    local=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    uplink=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        # No SO_REUSEADDR: an occupied QGC port must fail, not steal packets.
        local.bind((listen_address,listen_port));uplink.bind((uplink_address,uplink_port))
        uplink.connect((host,port))
        if uplink.getpeername()==local.getsockname():
            raise ValueError('桥接目标不能指向自身监听地址与端口')
        router=Router();began=time.monotonic();last_report=0;result={}
        print('PX4 GCS '+str(local.getsockname())+' -> Windows QGC '+str((host,port)),flush=True)
        print('Windows 回复目的（WSL uplink）：'+str(uplink.getsockname()),flush=True)
        print('API 14541/14542/14543 保持由地面内核独占；此桥不生成飞行指令。',flush=True)
        while not duration or time.monotonic()-began<duration:
            ready,_,_=select.select([local,uplink],[],[],.5)
            for channel in ready:
                try:data,source=channel.recvfrom(65535)
                except (ConnectionRefusedError,ConnectionResetError):
                    router.counts['uplink_errors']+=1;continue
                if channel is local:
                    for raw in router.from_px4(data,source):
                        try:uplink.send(raw)
                        except OSError:router.counts['uplink_errors']+=1
                else:
                    # Connected UDP already filters the remote endpoint; retain
                    # this explicit condition in case socket setup is changed.
                    if source!=(host,port):
                        router.counts['untrusted_source_dropped']+=1;continue
                    for destination,raw in router.from_qgc(data):local.sendto(raw,destination)
            if time.monotonic()-last_report>=2:
                result={'source':'raw MAVLink GCS bridge','windows_qgc':[host,port],
                    'local_listener':list(local.getsockname()),'uplink':list(uplink.getsockname()),
                    'aircraft':router.seen,'counts':router.counts,
                    'qgc_message_types':router.qgc_message_types,'qgc_command_ids':router.qgc_command_ids,
                    'qgc_target_systems':router.qgc_target_systems,
                    'qgc_target_message_types':router.qgc_target_message_types,
                    'qgc_reply_observed':router.counts['qgc_frames']>0,
                    'new_messages_generated_by_bridge':False,'observed_at':time.time()}
                if report:
                    report=Path(report);report.parent.mkdir(parents=True,exist_ok=True)
                    temp=report.with_suffix('.tmp');temp.write_text(json.dumps(result,ensure_ascii=False,indent=2));temp.replace(report)
                print('机号 '+','.join(sorted(router.seen))+' | PX4帧 '+str(router.counts['px4_frames'])+
                      ' | QGC回包 '+str(router.counts['qgc_frames'])+' | Windows回复已验证 '+str(result['qgc_reply_observed']),flush=True)
                last_report=time.monotonic()
        return result
    finally:
        local.close();uplink.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows-host',default='auto')
    parser.add_argument('--windows-port',type=int,default=14550)
    parser.add_argument('--listen-address',default='127.0.0.1')
    parser.add_argument('--listen-port',type=int,default=14550)
    parser.add_argument('--uplink-address',default='0.0.0.0')
    parser.add_argument('--uplink-port',type=int,default=0)
    parser.add_argument('--duration',type=float,default=0)
    parser.add_argument('--report',type=Path,default=ROOT/'runs/qgc-bridge-latest.json')
    args=parser.parse_args(argv)
    if any(not 0<value<65536 for value in (args.windows_port,args.listen_port)) or not 0<=args.uplink_port<65536:
        parser.error('UDP端口范围不正确')
    if args.duration<0:parser.error('--duration不能为负')
    try:
        host,origin=windows_host(args.windows_host)
        print('Windows 地址：'+host+'（'+origin+'）',flush=True)
        bridge(host,args.windows_port,args.listen_address,args.listen_port,args.uplink_address,args.uplink_port,args.duration,args.report)
        return 0
    except KeyboardInterrupt:return 0
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print('QGC 桥未就绪：'+str(exc),flush=True);return 1


if __name__=='__main__':raise SystemExit(main())
