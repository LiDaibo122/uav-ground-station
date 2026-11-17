import unittest
from pymavlink.dialects.v20 import common as M
from pymavlink.dialects.v10 import common as V1
from packages.swarm_sim.qgc_bridge import Router,DEFAULT_ENDPOINTS


def command(target,version=M,sign=False):
    encoder=version.MAVLink(None,srcSystem=255,srcComponent=190)
    if sign:
        encoder.signing.secret_key=b'q'*32
        encoder.signing.sign_outgoing=True
        encoder.signing.timestamp=123456
    return bytes(encoder.command_long_encode(target,1,M.MAV_CMD_REQUEST_MESSAGE,0,
                    M.MAVLINK_MSG_ID_AUTOPILOT_VERSION,0,0,0,0,0,0).pack(encoder))


class QgcBridgeTests(unittest.TestCase):
    def test_targeted_command_goes_only_to_named_aircraft(self):
        router=Router();wire=command(3)
        self.assertEqual(router.from_qgc(wire),[(DEFAULT_ENDPOINTS[3],wire)])

    def test_combined_datagram_routes_each_frame_independently(self):
        one,three=command(2),command(4)
        router=Router()
        self.assertEqual(router.from_qgc(one+three),[(DEFAULT_ENDPOINTS[2],one),(DEFAULT_ENDPOINTS[4],three)])
        self.assertEqual(router.qgc_target_systems,{'2':1,'4':1})
        self.assertEqual(router.qgc_target_message_types,{'2':{'COMMAND_LONG':1},'4':{'COMMAND_LONG':1}})

    def test_mavlink_v1_and_signed_v2_are_not_reencoded(self):
        for wire in (command(2,version=V1),command(2,sign=True)):
            with self.subTest(marker=wire[0],size=len(wire)):
                self.assertEqual(Router().from_qgc(wire),[(DEFAULT_ENDPOINTS[2],wire)])

    def test_unknown_target_is_dropped_instead_of_broadcast(self):
        router=Router()
        self.assertFalse(router.from_qgc(command(99)))
        self.assertEqual(router.counts['unknown_target_dropped'],1)

    def test_heartbeat_without_target_is_broadcast_as_original(self):
        encoder=M.MAVLink(None,srcSystem=255,srcComponent=190)
        wire=bytes(encoder.heartbeat_encode(M.MAV_TYPE_GCS,M.MAV_AUTOPILOT_INVALID,0,0,0).pack(encoder))
        self.assertEqual(Router().from_qgc(wire),[(address,wire) for address in DEFAULT_ENDPOINTS.values()])

    def test_manual_control_target_field_is_not_treated_as_broadcast(self):
        encoder=M.MAVLink(None,srcSystem=255,srcComponent=190)
        wire=bytes(encoder.manual_control_encode(4,0,0,0,0,0).pack(encoder))
        self.assertEqual(Router().from_qgc(wire),[(DEFAULT_ENDPOINTS[4],wire)])

    def test_only_expected_px4_endpoint_and_system_are_forwarded(self):
        router=Router();encoder=M.MAVLink(None,srcSystem=2,srcComponent=1)
        wire=bytes(encoder.heartbeat_encode(M.MAV_TYPE_FIXED_WING,M.MAV_AUTOPILOT_PX4,0,0,3).pack(encoder))
        self.assertFalse(router.from_px4(wire,('127.0.0.1',14541)))
        self.assertFalse(router.from_px4(wire,DEFAULT_ENDPOINTS[3]))
        self.assertEqual(router.from_px4(wire,DEFAULT_ENDPOINTS[2]),[wire])
        self.assertIn('2',router.seen)

    def test_corrupt_crc_does_not_reach_aircraft(self):
        wire=bytearray(command(2));wire[-1]^=255
        router=Router();self.assertFalse(router.from_qgc(bytes(wire)))
        self.assertGreater(router.counts['invalid_frames'],0)


if __name__=='__main__':unittest.main()
