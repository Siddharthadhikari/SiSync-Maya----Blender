# -*- coding: utf-8 -*-
"""
Headless unit & integration tests for SiSync Maya module.
Run via: & "C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe" tests/test_maya_headless.py
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAYA_DIR = os.path.join(PROJECT_ROOT, "maya")
for p in [PROJECT_ROOT, MAYA_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import maya.standalone
maya.standalone.initialize(name="python")

import maya.cmds as cmds
import sisync_bridge_core as core
import sisync_maya


class TestMayaSync(unittest.TestCase):

    def setUp(self):
        cmds.file(new=True, force=True)
        sisync_maya.ensure_fbx_plugin()

    def test_export_selection(self):
        # Create a test poly cube
        cube_nodes = cmds.polyCube(name="SyncCube", width=10, height=10, depth=10)
        cube_transform = cube_nodes[0]
        cmds.select(cube_transform, replace=True)

        # Export selection
        meta = sisync_maya.MayaExporter.export_selection()
        self.assertIsNotNone(meta, "Export should return metadata dictionary")
        self.assertIn("SyncCube", meta.get("objects", []))
        self.assertEqual(meta.get("source_dcc"), "maya")

        # Verify FBX file on disk
        fbx_path = core.get_maya_to_blender_fbx()
        self.assertTrue(os.path.isfile(fbx_path), f"FBX file must exist at {fbx_path}")
        self.assertGreater(os.path.getsize(fbx_path), 500, "FBX file should not be empty")

    def test_texture_refresh(self):
        # Create a test file texture node
        file_node = cmds.shadingNode("file", asTexture=True, name="test_diffuse_map")
        dummy_tex_path = os.path.join(core.get_textures_dir(), "basecolor_test.png")
        # Touch dummy file
        with open(dummy_tex_path, "wb") as f:
            f.write(b"dummy")
        cmds.setAttr(f"{file_node}.fileTextureName", dummy_tex_path, type="string")

        refreshed = sisync_maya.MayaTextureManager.refresh_material_maps()
        self.assertGreaterEqual(refreshed, 1, "Should refresh at least 1 texture node")

    def test_server_lifecycle(self):
        test_port = 19890
        started = sisync_maya.start_server(port=test_port)
        self.assertTrue(started, "Maya server should start")
        self.assertTrue(sisync_maya.is_server_running())

        # Test PING from client
        res = core.BridgeClient.send(
            host="127.0.0.1",
            port=test_port,
            command=core.COMMAND_PING,
            sender="test_client"
        )
        self.assertEqual(res.get("command"), core.COMMAND_PONG)

        sisync_maya.stop_server()
        self.assertFalse(sisync_maya.is_server_running())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
