"""Localhost-only MAVLink transport for the owned PX4 SITL instances."""
from collections import deque
import math
import struct
import threading
import time
from pymavlink import mavutil

M = mavutil.mavlink


class Vehicle:
    def __init__(self, spec):
        self.spec = spec
        self.system = spec['system_id']
        self.connection = mavutil.mavlink_connection('udpin:127.0.0.1:%d' % spec['port'],
                                                    source_system=245, source_component=190)
        self.state = {}
        self.events = deque(maxlen=8192)
        self.tx_lock = threading.Lock()
        self.condition = threading.Condition()
        self.running = True
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        next_heartbeat = 0
        while self.running:
            now = time.monotonic()
            if now >= next_heartbeat:
                with self.tx_lock:
                    self.connection.mav.heartbeat_send(M.MAV_TYPE_GCS, M.MAV_AUTOPILOT_INVALID, 0, 0, 0)
                next_heartbeat = now + 0.5
            msg = self.connection.recv_match(blocking=False)
            if msg is None:
                time.sleep(0.003)
                continue
            if msg.get_srcSystem() != self.system:
                continue
            kind = msg.get_type()
            with self.condition:
                self.events.append((now, msg))
                self.state[kind] = msg.to_dict()
                self.state['received_at'] = now
                if kind == 'GLOBAL_POSITION_INT':
                    self.state['position_received_at'] = now
                if kind in ('HEARTBEAT','SYS_STATUS','BATTERY_STATUS'):
                    self.state[kind+'_received_at'] = now
                self.condition.notify_all()

    def wait(self, kind, predicate=lambda m: True, after=0, timeout=15):
        deadline = time.monotonic()+timeout
        with self.condition:
            while True:
                for when, msg in self.events:
                    if when >= after and msg.get_type() == kind and predicate(msg):
                        return msg
                left = deadline-time.monotonic()
                if left <= 0:
                    raise TimeoutError('%s: %s' % (self.spec['id'], kind))
                self.condition.wait(min(left, 0.5))

    def command(self, command, *params, timeout=10):
        params = list(params)+[0]*(7-len(params))
        sent = time.monotonic()
        with self.tx_lock:
            self.connection.mav.command_long_send(self.system, 1, command, 0, *params)
        ack = self.wait('COMMAND_ACK', lambda m: m.command == command, sent, timeout)
        if ack.result not in (M.MAV_RESULT_ACCEPTED, M.MAV_RESULT_IN_PROGRESS):
            raise RuntimeError('%s command %s rejected: %s' % (self.spec['id'], command, ack))
        return ack.to_dict()

    def set_param(self, name, value):
        queried = time.monotonic()
        with self.tx_lock:
            self.connection.mav.param_request_read_send(self.system, 1, name.encode(), -1)
        current = self.wait('PARAM_VALUE', lambda m: m.param_id.rstrip('\x00') == name, queried)
        dtype = current.param_type
        actual=(struct.unpack('<i',struct.pack('<f',current.param_value))[0]
                if dtype==M.MAV_PARAM_TYPE_INT32 else current.param_value)
        if abs(actual-value)<.01:
            return
        if dtype == M.MAV_PARAM_TYPE_INT32:
            wire_value = struct.unpack('<f', struct.pack('<i', int(value)))[0]
        elif dtype == M.MAV_PARAM_TYPE_REAL32:
            wire_value = float(value)
        else:
            raise ValueError('Unsupported PX4 parameter type %s' % dtype)
        sent = time.monotonic()
        with self.tx_lock:
            self.connection.mav.param_set_send(self.system, 1, name.encode(), wire_value, dtype)
        msg = self.wait('PARAM_VALUE', lambda m: m.param_id.rstrip('\x00') == name, sent)
        actual = (struct.unpack('<i', struct.pack('<f', msg.param_value))[0]
                  if dtype == M.MAV_PARAM_TYPE_INT32 else msg.param_value)
        if abs(actual-value) > 0.01:
            raise RuntimeError('parameter %s not applied: %s' % (name, msg.param_value))

    def upload(self, items):
        # Protocol state machine handles both REQUEST and REQUEST_INT.
        sent = time.monotonic()
        with self.tx_lock:
            self.connection.mav.mission_count_send(self.system, 1, len(items))
        cursor = sent
        deadline = sent+45
        requested = set()
        while time.monotonic() < deadline:
            with self.condition:
                events = [(t, m) for t, m in self.events if t >= cursor and m.get_type() in
                          ('MISSION_REQUEST', 'MISSION_REQUEST_INT', 'MISSION_ACK')]
            if not events:
                time.sleep(0.02)
                continue
            for when, msg in events:
                cursor = when+1e-9
                if msg.get_type() == 'MISSION_ACK':
                    if msg.type != M.MAV_MISSION_ACCEPTED or len(requested) != len(items):
                        raise RuntimeError('mission rejected/incomplete: %s' % msg)
                    return {'ack': msg.to_dict(), 'items': len(items), 'requested_sequences': sorted(requested)}
                seq = msg.seq
                if not 0 <= seq < len(items):
                    raise RuntimeError('invalid mission sequence request')
                item = items[seq]
                requested.add(seq)
                with self.tx_lock:
                    args = [self.system, 1, seq, item.get('frame', M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT),
                            item['command'], int(seq == 0), 1,
                            item.get('p1', 0), item.get('p2', 0), item.get('p3', 0), float('nan'),
                            int(item['lat']*1e7), int(item['lon']*1e7), item['alt']]
                    if msg.get_type() == 'MISSION_REQUEST_INT':
                        self.connection.mav.mission_item_int_send(*args)
                    else:
                        args[3] = item.get('frame', M.MAV_FRAME_GLOBAL_RELATIVE_ALT)
                        args[-3], args[-2] = item['lat'], item['lon']
                        self.connection.mav.mission_item_send(*args)
        raise TimeoutError('mission upload timed out')

    def snapshot(self):
        with self.condition:
            return dict(self.state)

    def close(self):
        self.running = False
        self.thread.join(timeout=2)
        self.connection.close()


def to_global(east, north, origin):
    lat = origin['lat'] + math.degrees(north/6371000.0)
    lon = origin['lon'] + math.degrees(east/(6371000.0*math.cos(math.radians(origin['lat']))))
    return lat, lon


def to_local(lat, lon, origin):
    return (math.radians(lon-origin['lon'])*6371000.0*math.cos(math.radians(origin['lat'])),
            math.radians(lat-origin['lat'])*6371000.0)
