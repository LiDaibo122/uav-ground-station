"""Send a single mission request to the XD bridge and follow its receipts."""
import argparse
import json
import queue
import time
import uuid

import rospy
from std_msgs.msg import String


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['takeoff','task_path','cancel','land_home','query_state'])
    parser.add_argument('--vehicle', default='uav1')
    parser.add_argument('--mission-id', required=True)
    parser.add_argument('--command-id', default=None, help='Reuse the same ID and payload to check an earlier request')
    parser.add_argument('--altitude', type=float, default=40.0)
    parser.add_argument('--point', nargs=3, type=float, action='append', metavar=('X','Y','Z'))
    parser.add_argument('--goal-id', type=int)
    parser.add_argument('--timeout', type=float, default=320)
    args=parser.parse_args()
    if args.action=='task_path' and len(args.point or [])<2:
        parser.error('task_path 需要至少两个 --point X Y Z')
    if args.action=='cancel' and args.goal_id is None:
        parser.error('cancel 需要 --goal-id')
    payload={'takeoff':{'altitude_m':args.altitude},'task_path':{'points_m':args.point},
             'cancel':{'goal_id':args.goal_id}}.get(args.action,{})
    cid=args.command_id or uuid.uuid4().hex
    rospy.init_node('ground_station_xd_command', anonymous=True, disable_rosout=True)
    events=queue.Queue()
    def receive(msg):
        data=json.loads(msg.data)
        if data.get('command_id')==cid and data.get('vehicle_id')==args.vehicle and data.get('mission_id')==args.mission_id:
            events.put(data)
    sub=rospy.Subscriber('/ground_bridge/result',String,receive,queue_size=50)
    pub=rospy.Publisher('/ground_bridge/command',String,queue_size=1)
    deadline=time.monotonic()+10
    while not (pub.get_num_connections() and sub.get_num_connections()):
        if time.monotonic()>=deadline:
            raise SystemExit('连接单机桥接节点超时，请检查启动状态和 ROS_MASTER_URI')
        time.sleep(0.05)
    now=time.time()
    cmd=dict(schema_version=1,command_id=cid,mission_id=args.mission_id,vehicle_id=args.vehicle,
             action=args.action,payload=payload,issued_at=now,expires_at=now+15)
    if args.action=='task_path':
        cmd['frame_id']='world'
    print('command_id='+cid,flush=True)
    pub.publish(String(data=json.dumps(cmd,allow_nan=False)))
    deadline=time.monotonic()+args.timeout
    while time.monotonic()<deadline:
        try:
            event=events.get(timeout=min(1,max(0,deadline-time.monotonic())))
        except queue.Empty:
            continue
        print(json.dumps(event,ensure_ascii=False),flush=True)
        if event['phase']=='completed':
            return
        if event['phase'] in ('unknown','rejected','failed','cancelled'):
            raise SystemExit(1)
    raise SystemExit('未收到最终回执，请用相同 command-id 和参数查询')


if __name__=='__main__':
    main()
