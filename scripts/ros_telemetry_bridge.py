#!/usr/bin/env python3
"""Read-only ROS1 MAVROS telemetry -> FieldLink. Never publishes flight commands."""
import argparse
from datetime import datetime, timezone
import json
import math
import threading
import time
import urllib.request


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace',default='/uav1/mavros')
    parser.add_argument('--id',default='UAV-01')
    parser.add_argument('--ground',default='http://127.0.0.1:11843')
    args=parser.parse_args()
    import rospy
    from sensor_msgs.msg import NavSatFix, BatteryState
    from mavros_msgs.msg import State, VFR_HUD
    rospy.init_node('fieldlink_telemetry_'+args.id.replace('-','_'),anonymous=True)
    lock=threading.Lock()
    current={'id':args.id,'battery_pct':None,'speed_mps':None,'flight_mode':'UNKNOWN','heading_deg':0,
             'altitude_reference':'WGS84_ellipsoid','task':'任务状态未上报'}
    received={'gps':0,'hud':0,'battery':0,'mode':0}
    def gps(msg):
        with lock:
            if msg.status.status<0 or not all(math.isfinite(v) for v in (msg.latitude,msg.longitude,msg.altitude)):
                received['gps']=0;return
            current.update(longitude=msg.longitude,latitude=msg.latitude,altitude_m=msg.altitude,
                observed_at=datetime.fromtimestamp(msg.header.stamp.to_sec(),timezone.utc).isoformat())
            received['gps']=time.monotonic()
    def hud(msg):
        with lock:
            current.update(speed_mps=msg.groundspeed if math.isfinite(msg.groundspeed) else None,heading_deg=msg.heading%360)
            received['hud']=time.monotonic()
    def battery(msg):
        with lock:
            current['battery_pct']=msg.percentage*100 if math.isfinite(msg.percentage) and 0<=msg.percentage<=1 else None
            received['battery']=time.monotonic()
    def flight(msg):
        with lock:
            current['flight_mode']=msg.mode if msg.connected else 'DISCONNECTED'
            received['mode']=time.monotonic()
    ns=args.namespace.rstrip('/')
    rospy.Subscriber(ns+'/global_position/global',NavSatFix,gps,queue_size=1)
    rospy.Subscriber(ns+'/vfr_hud',VFR_HUD,hud,queue_size=1)
    rospy.Subscriber(ns+'/battery',BatteryState,battery,queue_size=1)
    rospy.Subscriber(ns+'/state',State,flight,queue_size=1)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Wall clock loop: a paused simulation must not refresh the age of old GPS.
    while not rospy.is_shutdown():
        with lock:
            age=time.monotonic()-received['gps']
            payload=dict(current) if age<2 and 'longitude' in current else None
            if payload:
                if time.monotonic()-received['battery']>5:payload['battery_pct']=None
                if time.monotonic()-received['hud']>5:payload['speed_mps']=None
                if time.monotonic()-received['mode']>5:payload['flight_mode']='UNKNOWN'
        if payload:
            try:
                req=urllib.request.Request(args.ground.rstrip('/')+'/api/telemetry',data=json.dumps(payload,allow_nan=False).encode(),headers={'Content-Type':'application/json'})
                with opener.open(req,timeout=1) as response:response.read()
            except Exception as e:rospy.logwarn_throttle(10,'FieldLink telemetry: %s',type(e).__name__)
        time.sleep(.5)


if __name__=='__main__':main()
