"""Acceptance check for a fresh local dual XD SITL session (no flight publications)."""
import argparse
import json
import os
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

import rospy
from std_msgs.msg import String

BASE = 'http://127.0.0.1:11844'
OBS_ROOT = '/ground_bridge/planning/observations/'
PROMPT = ('请集群自动分配三个巡检任务，只规划。以 world 为基准，'
          '道路东300米北0米，河堤东300米北120米，桥面东500米北120米。请根据观测判定优先级。')
FOLLOWUP = '保持刚才三个任务，假设 uav2 本轮检修，排除它，用最新状态重新分配，只规划。'


def request(path, body=None):
    req = Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=120) as response:
        return json.load(response)


def chat(text, conversation):
    result = request('/api/chat', dict(message=text, mode='xd_plan_only', conversation_id=conversation))
    calls = result['tool_calls']
    print('工具', [(c['tool'], c['status']) for c in calls], flush=True)
    assert not result['submitted'] and not result['execution'], result['reply']
    assert any(c['tool'] == 'xd_allocate' and c['status'] == 'success' for c in calls), result['reply']
    p = result['draft']
    assert p and p['status'] == 'preview', result['reply']
    print('分配', p['assignments'], '未分配', p['unassigned'], flush=True)
    return p


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='将本次真实工具结果保存为 JSON')
    parser.add_argument('--seed-only', action='store_true', help='保留一条模拟道路观测供网页验收使用')
    args = parser.parse_args()
    if os.environ.get('ROS_MASTER_URI') != 'http://127.0.0.1:11321':
        parser.error('请先 source 联调仓库的 scripts/xd_single_env.sh')
    rospy.init_node('verify_cluster_preview', anonymous=True, disable_signals=True)
    if not rospy.get_param('/use_sim_time', False):
        parser.error('需要本机 XD 仿真')
    key = 'demo_road_' + uuid.uuid4().hex[:12]
    report = dict(frame='world', east=300, north=0, status='needs_review',
                  observed_at=time.time(), source='仿真巡检报告', text='道路疑似塌方，待复核')
    if args.seed_only:
        rospy.set_param(OBS_ROOT + key, report)
        print('已注入本轮模拟观测', key, '，30 分钟内有效。')
        return
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            current = request('/api/xd/status').get('vehicles', [])
        except OSError:
            current = []
        if len(current) == 2 and all(s['can_takeoff'] and not s['job'] for s in current):
            break
        time.sleep(1)
    else:
        raise RuntimeError('未等到两架空闲飞机，请在新启动的 XD 双机仿真上验收')
    flight_actions = []
    def observe(msg):
        command = json.loads(msg.data)
        if command.get('action') != 'query_state':
            flight_actions.append(command)
    subscriber = rospy.Subscriber('/ground_bridge/command', String, observe, queue_size=100)
    deadline = time.monotonic() + 5
    while not subscriber.get_num_connections() and time.monotonic() < deadline:
        time.sleep(.05)
    assert subscriber.get_num_connections(), '监视器未连到地面站命令发布者'
    conversation = uuid.uuid4().hex
    rospy.set_param(OBS_ROOT + key, report)
    try:
        two = chat(PROMPT, conversation)
        expected = {(300, 0): 'high', (300, 120): 'normal', (500, 120): 'normal'}
        assert len(two['tasks']) == 3 and all(t['pattern'] == 'point' for t in two['tasks'])
        assert {(t['east_m'], t['north_m']): t['priority'] for t in two['tasks']} == expected
        assert any(e['id'] == key for t in two['tasks'] for e in t['evidence'])
        assigned = [t for route in two['assignments'].values() for t in route]
        assert len(set(assigned)) == len(assigned) == 3 and all(two['assignments'].values()) and not two['unassigned']
        one = chat(FOLLOWUP, conversation)
        assert one['task_specs'] == two['task_specs'] and one['id'] != two['id']
        assert one['assignments']['uav2'] == [] and set(one['assignments']['uav1']) == {'T1','T2','T3'}
        assert not one['unassigned']
        end = request('/api/xd/status')['vehicles']
        assert not flight_actions and all(not s['armed'] and s['flight_phase'] == 'on_ground' and not s['job'] for s in end)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(dict(initial=current, dual=two, excluded=one, final=end,
                                                  flight_actions=flight_actions), ensure_ascii=False, indent=2))
        print('PASS：三项任务分配成功，排除检修机后重新分配，两机保持上锁，飞行命令为零。')
    finally:
        rospy.delete_param(OBS_ROOT + key)
        subscriber.unregister()


if __name__ == '__main__':
    main()
