"""End-to-end confirmation and receipt check for the owned local XD fixed-wing SITL."""
import argparse
import json
import os
import time
import uuid
from pathlib import Path

import rospy
from std_msgs.msg import String
from verify_xd_fleet_planning import request


def verify_report(report, names):
    plan, result = report['plan'], report['latest']
    actions = report['commands']
    assert set(result['vehicles']) == names and {s['vehicle'] for s in report['end_states']} == names
    assert report['first']['plan_id'] == report['repeat']['plan_id'] == plan['id']
    assert not report['first']['repeated'] and report['repeat']['repeated']
    assert result['status']=='completed', result['status']
    flight_commands=[c for c in actions if c['action'] != 'query_state']
    assert len(flight_commands)==len({c['command_id'] for c in flight_commands})==4*len(names)
    by_id={t['id']:t for t in plan['tasks']}
    for uid, row in result['vehicles'].items():
        sent=[c for c in flight_commands if c['vehicle_id']==uid]
        assert [c['action'] for c in sent]==['takeoff','task_path','cancel','land_home']
        assert all(c['mission_id']==row['execution_id'] for c in sent)
        assert row['task_ids']==plan['assignments'][uid]
        expected=[[by_id[k]['east_m'],by_id[k]['north_m'],plan['height_m']] for k in row['task_ids']]
        assert sent[1]['payload']['points_m'][-len(expected):]==expected
        assert [e['action'] for e in row['events'] if e['phase']=='completed']==['takeoff','task_path','cancel','land_home']
    assert all(not s['armed'] and s['flight_phase']=='on_ground' for s in report['end_states'])
    print('PASS 全部仿真飞机按草案完成航线并返航上锁，重复确认无重复飞行指令',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('runs/xd_cluster_execution.json'))
    parser.add_argument('--vehicles', type=int, choices=range(2,5), default=2)
    parser.add_argument('--replay', type=Path, help='离线核对已有 JSON 回执')
    args = parser.parse_args()
    names = {'uav%d' % (i+1) for i in range(args.vehicles)}
    if args.replay:
        verify_report(json.loads(args.replay.read_text()), names)
        return
    if os.environ.get('ROS_MASTER_URI') != 'http://127.0.0.1:11321':
        parser.error('请加载本地 XD 多机仿真环境')
    rospy.init_node('verify_cluster_execution', anonymous=True, disable_signals=True)
    deadline = time.monotonic()+90
    while time.monotonic()<deadline:
        try:
            states=request('/api/xd/status').get('vehicles',[])
            if {s['vehicle'] for s in states}==names and all(s['can_takeoff'] and not s['job'] for s in states):
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('需要新启动的空闲多机仿真')
    actions=[]
    sub=rospy.Subscriber('/ground_bridge/command',String,lambda msg: actions.append(json.loads(msg.data)),queue_size=100)
    conversation=uuid.uuid4().hex
    def chat(message, mode):
        result=request('/api/chat', dict(message=message,mode=mode,conversation_id=conversation))
        print(result['reply'],[(r['tool'],r['status']) for r in result['tool_calls']],flush=True)
        return result
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        tmp=args.output.with_suffix('.tmp')
        tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        tmp.replace(args.output)
    labels=['道路','河堤','学校','水库']
    points=[(labels[i],650,120*i) for i in range(args.vehicles)]
    points.append(('桥面',900,120*(args.vehicles-1)))
    prompt='请集群只规划以下点巡检任务，仿真 world 米：'+ '，'.join(
        '%s东%d北%d' % p for p in points)+'。自动分配。'
    plan=chat(prompt, 'xd_plan_only')['draft']
    assert plan and len(plan['tasks'])==len(points) and set(plan['assignments'])==names
    assert all(plan['assignments'].values()) and not plan['unassigned']
    assert {(t['label'],t['east_m'],t['north_m']) for t in plan['tasks']}==set(points)
    assigned=[task for route in plan['assignments'].values() for task in route]
    assert sorted(assigned)==sorted(t['id'] for t in plan['tasks'])
    assert all(c['action'] == 'query_state' for c in actions) and sub.get_num_connections()>0
    report=dict(plan=plan,commands=actions,conversation_id=conversation)
    save()
    first=chat('按刚才的集群方案执行。','live')
    receipt=first['execution']
    assert receipt and receipt['plan_id']==plan['id'] and not receipt['repeated']
    report['first']=receipt
    save()
    duplicate=chat('再次确认，按刚才的集群方案执行。','live')
    replay=next(c['result']['result'] for c in duplicate['tool_calls']
                if c['tool']=='xd_confirm_allocation' and c['status']=='success')
    assert replay['repeated'] and not duplicate['submitted']
    report['repeat']=replay
    end=time.monotonic()+620
    previous=None
    while time.monotonic()<end:
        result=request('/api/xd/executions/'+plan['id'])
        report['latest']=result
        status=[(uid,row['phase'],row.get('detail','')) for uid,row in result['vehicles'].items()]
        if status!=previous:
            print(status,flush=True)
            previous=status
            save()
        if result['status'] not in ('running','preview'):
            break
        time.sleep(1)
    report['end_states']=request('/api/xd/status')['vehicles']
    save()
    verify_report(report, names)
    sub.unregister()


if __name__=='__main__':
    main()
