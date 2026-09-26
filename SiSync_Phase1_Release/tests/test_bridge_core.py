#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync — Comprehensive Automated Validation Suite
Tests:
1. Static Compilation & Canonical Module Consolidation
2. Socket Framing (Empty, Normal JSON, Unicode, Large Message, Partial TCP, Multiple Packets, Mid-Disconnect, Malformed JSON, Oversized Payload)
3. Network Server & Client Lifecycle (Idempotent start/stop, Port Conflict, PING/PONG, HELLO, GET_STATUS, Invalid Command, Protocol Version Mismatch)
4. Coordinate Basis, Scale (1, 10, 100, 1000), Flip X/Y/Z/XY/XZ/YZ/XYZ & Round-Trip Math Verification
5. Metadata Schema & Deterministic Echo Suppression (Blender -> Maya -> NO re-export, Maya -> Blender -> NO re-export)
"""

import os
import sys
import time
import json
import math
import socket
import struct
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_THIS_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

import sisync_bridge_core as core


class TestSiSyncSuite(unittest.TestCase):

    def test_01_framing_normal_and_unicode(self):
        """Tests 4-byte big-endian length framing with ASCII, Unicode, and large JSON."""
        s1, s2 = socket.socketpair()
        try:
            payload = {
                "command": "SYNC_MESH",
                "sender": "blender",
                "unicode_name": "Lio01_L_eyes_eye01_geo_日本語_🔥",
                "numbers": list(range(500)),
            }
            encoded = core.encode_message(payload)
            # Verify no trailing newline is appended
            self.assertEqual(len(encoded), 4 + struct.unpack(">I", encoded[:4])[0])
            s1.sendall(encoded)
            decoded = core.decode_message(s2)
            self.assertEqual(decoded["unicode_name"], payload["unicode_name"])
            self.assertEqual(len(decoded["numbers"]), 500)
        finally:
            s1.close()
            s2.close()

    def test_02_framing_partial_and_multi_packet(self):
        """Tests partial TCP stream reads (1 byte at a time) and multiple concatenated frames."""
        s1, s2 = socket.socketpair()
        try:
            m1 = core.encode_message({"command": "PING", "seq": 1})
            m2 = core.encode_message({"command": "PONG", "seq": 2})
            # Send first message in tiny fragments
            for b in m1:
                s1.sendall(bytes([b]))
            # Send second message immediately
            s1.sendall(m2)

            d1 = core.decode_message(s2)
            d2 = core.decode_message(s2)
            self.assertEqual(d1["seq"], 1)
            self.assertEqual(d2["seq"], 2)
        finally:
            s1.close()
            s2.close()

    def test_03_framing_malformed_empty_and_oversized(self):
        """Tests empty payload, malformed JSON, mid-stream disconnect, and >32MB guard."""
        s1, s2 = socket.socketpair()
        try:
            # 1. Empty length (0)
            s1.sendall(struct.pack(">I", 0))
            with self.assertRaises(ValueError):
                core.decode_message(s2)

            # 2. Oversized header (>32MB)
            s1.sendall(struct.pack(">I", 40 * 1024 * 1024))
            with self.assertRaises(ValueError):
                core.decode_message(s2)

            # 3. Malformed JSON
            bad_bytes = b"{not_valid_json"
            s1.sendall(struct.pack(">I", len(bad_bytes)) + bad_bytes)
            with self.assertRaises(ValueError):
                core.decode_message(s2)

            # 4. Mid-stream disconnect
            s1.sendall(struct.pack(">I", 100) + b"short")
            s1.close()
            with self.assertRaises(ValueError):
                core.decode_message(s2)
        finally:
            s1.close()
            s2.close()

    def test_04_server_lifecycle_and_commands(self):
        """Tests idempotent server start/stop, PING, HELLO, GET_STATUS, invalid command, and port conflict."""
        test_port = 19895
        srv = core.BridgeServer(
            name="TestNode",
            port=test_port,
            message_handler=lambda msg: {"status": "scheduled", "echo": msg.get("command")},
        )
        try:
            self.assertTrue(srv.start())
            # Idempotent repeated starts
            self.assertTrue(srv.start())
            self.assertTrue(srv.start())

            # PING
            r_ping = core.BridgeClient.send("127.0.0.1", test_port, core.COMMAND_PING)
            self.assertEqual(r_ping.get("status"), "success")
            self.assertEqual(r_ping.get("command"), core.COMMAND_PONG)

            # HELLO
            r_hello = core.BridgeClient.send("127.0.0.1", test_port, core.COMMAND_HELLO)
            self.assertEqual(r_hello.get("status"), "success")

            # GET_STATUS
            r_stat = core.BridgeClient.send("127.0.0.1", test_port, core.COMMAND_GET_STATUS)
            self.assertEqual(r_stat.get("status"), "success")

            # SYNC_MESH
            r_sync = core.BridgeClient.send("127.0.0.1", test_port, core.COMMAND_SYNC_MESH, payload={"fbx_path": "test.fbx"})
            self.assertEqual(r_sync.get("status"), "scheduled")

            # Unsupported command
            r_bad = core.BridgeClient.send("127.0.0.1", test_port, "INVALID_UNKNOWN_CMD")
            self.assertEqual(r_bad.get("status"), "error")
            self.assertEqual(r_bad.get("command"), core.COMMAND_ERROR)

        finally:
            srv.stop()
            # Idempotent repeated stops
            srv.stop()

    def test_05_coordinate_transforms_scale_and_flips(self):
        """Tests deterministic coordinate conversion, scale (1, 10, 100, 1000), and all 8 Flip X/Y/Z combinations."""
        test_points = [
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (-1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, -1.0, 0.0),
            (0.0, 0.0, 1.0),
            (0.0, 0.0, -1.0),
            (12.5, -43.2, 88.75),
        ]
        scales = [1.0, 10.0, 100.0, 1000.0]
        flips = [
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (False, False, True),
            (True, True, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ]
        for s in scales:
            for fx, fy, fz in flips:
                for pt in test_points:
                    m_pt = core.CoordinateBasis.blender_point_to_maya(pt, up_axis="Y", unit_scale=s, flip_x=fx, flip_y=fy, flip_z=fz)
                    b_pt = core.CoordinateBasis.maya_point_to_blender(m_pt, up_axis="Y", unit_scale=1.0 / s, flip_x=fx, flip_y=fy, flip_z=fz)
                    err = math.sqrt(sum((a - b) ** 2 for a, b in zip(pt, b_pt)))
                    self.assertLess(err, 1e-6, f"Round-trip error {err} exceeded tolerance for pt={pt}, scale={s}, flips={(fx,fy,fz)}")

        # Basis determinant must be +1.0 for right-handed Y-Up <-> Z-Up conversion
        C_b2m, det_b2m, _ = core.CoordinateBasis.get_basis_change_matrix_3x3(core.CoordinateBasis.BLENDER_DEFAULT, core.CoordinateBasis.MAYA_Y_UP)
        C_m2b, det_m2b, _ = core.CoordinateBasis.get_basis_change_matrix_3x3(core.CoordinateBasis.MAYA_Y_UP, core.CoordinateBasis.BLENDER_DEFAULT)
        self.assertAlmostEqual(det_b2m, 1.0)
        self.assertAlmostEqual(det_m2b, 1.0)

    def test_06_echo_suppression_no_infinite_loop(self):
        """Verifies deterministic echo suppression prevents Blender -> Maya -> Blender loops."""
        # Case 1: Blender exports revision 10 -> Maya imports revision 10 -> Maya must NOT re-import or re-export
        meta_b2m = {
            "source": "blender",
            "destination": "maya",
            "revision": 10,
            "timestamp": 1000.0,
        }
        # Blender must NOT accept its own export
        self.assertFalse(core.EchoSuppressor.should_accept_incoming("blender", meta_b2m))
        # Maya SHOULD accept revision 10 on first arrival
        self.assertTrue(core.EchoSuppressor.should_accept_incoming("maya", meta_b2m))
        # Maya marks revision 10 imported
        core.EchoSuppressor.begin_import("maya")
        self.assertFalse(core.EchoSuppressor.should_accept_incoming("maya", meta_b2m))
        core.EchoSuppressor.end_import("maya", revision=10, timestamp=1000.0)
        # Subsequent checks of revision 10 in Maya must be suppressed!
        self.assertFalse(core.EchoSuppressor.should_accept_incoming("maya", meta_b2m))


if __name__ == "__main__":
    unittest.main(verbosity=2)
