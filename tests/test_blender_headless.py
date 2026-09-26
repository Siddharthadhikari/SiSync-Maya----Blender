# -*- coding: utf-8 -*-
r"""
Headless unit & integration tests for SiSync Blender Add-on.
Run via: & "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" -b --python tests/test_blender_headless.py
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLENDER_ADDONS = os.path.join(PROJECT_ROOT, "blender")
for p in [PROJECT_ROOT, BLENDER_ADDONS]:
    if p not in sys.path:
        sys.path.insert(0, p)

import bpy
import bl_sisync
import bl_sisync.sync_mesh as sync_mesh
import bl_sisync.sync_maps as sync_maps
import bl_sisync.network as network
import sisync_bridge_core as core


class TestBlenderSync(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Register the add-on
        bl_sisync.register()

    @classmethod
    def tearDownClass(cls):
        bl_sisync.unregister()

    def setUp(self):
        # Reset scene to clean state
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_addon_registration(self):
        # Check operators exist in bpy.ops
        self.assertTrue(hasattr(bpy.ops.sisync, "send_mesh"))
        self.assertTrue(hasattr(bpy.ops.sisync, "pull_mesh"))
        self.assertTrue(hasattr(bpy.ops.sisync, "refresh_maps"))
        self.assertTrue(hasattr(bpy.ops.sisync, "toggle_server"))

    def test_mesh_export(self):
        # Add a cube
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cube = bpy.context.active_object
        self.assertIsNotNone(cube)
        cube.name = "SyncTestCube"

        # Test Export Selection
        meta = sync_mesh.BlenderMeshSync.export_selection(bpy.context)
        self.assertIsNotNone(meta, "Export should produce metadata manifest")
        self.assertIn("SyncTestCube", meta.get("objects", []))
        self.assertEqual(meta.get("source_dcc"), "blender")

        fbx_path = core.get_blender_to_maya_fbx()
        self.assertTrue(os.path.isfile(fbx_path), f"FBX file must exist: {fbx_path}")
        self.assertGreater(os.path.getsize(fbx_path), 500)

    def test_in_place_vertex_update(self):
        # Create target cube
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        target = bpy.context.active_object
        target.name = "MyHeroMesh"

        # Create source cube and move a vertex
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        source = bpy.context.active_object
        source.name = "ImportedSource"
        source.data.vertices[0].co.x += 5.0  # Offset vertex 0

        # Perform in-place vertex update
        orig_v0_x = target.data.vertices[0].co.x
        sync_mesh.BlenderMeshSync._update_vertices_in_place(target, source)
        new_v0_x = target.data.vertices[0].co.x

        self.assertAlmostEqual(new_v0_x, orig_v0_x + 5.0, places=4, msg="Vertex coordinate should be updated in-place")

    def test_relink_mesh_data(self):
        # Create target cube (8 verts)
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        target = bpy.context.active_object
        target.name = "TargetObj"
        target.location = (10.0, 20.0, 30.0)

        # Create cylinder (different topology, 34 verts)
        bpy.ops.mesh.primitive_cylinder_add()
        cylinder = bpy.context.active_object
        cyl_v_count = len(cylinder.data.vertices)

        # Re-link data block
        sync_mesh.BlenderMeshSync._relink_mesh_data(target, cylinder)

        # Verify target object kept its transform and name, but has cylinder's mesh data
        self.assertEqual(len(target.data.vertices), cyl_v_count)
        self.assertEqual(target.location.x, 10.0)
        self.assertEqual(target.location.y, 20.0)
        self.assertEqual(target.location.z, 30.0)

    def test_material_map_refresh(self):
        # Create material with image texture
        mat = bpy.data.materials.new(name="PBR_Material")
        if bpy.app.version < (5, 0, 0) and hasattr(mat, "use_nodes"):
            mat.use_nodes = True
        nodes = mat.node_tree.nodes
        tex_node = nodes.new('ShaderNodeTexImage')
        
        # Create dummy image
        img = bpy.data.images.new(name="BaseColorMap.png", width=64, height=64)
        tex_node.image = img

        reloaded = sync_maps.BlenderMapManager.refresh_pbr_maps(bpy.context)
        self.assertGreaterEqual(reloaded, 1)

    def test_network_server(self):
        test_port = 19892
        started = network.start_server(port=test_port)
        self.assertTrue(started)
        self.assertTrue(network.is_server_running())

        # Test PING
        res = core.BridgeClient.send(
            host="127.0.0.1",
            port=test_port,
            command=core.COMMAND_PING,
            sender="unit_test"
        )
        self.assertEqual(res.get("command"), core.COMMAND_PONG)
        self.assertEqual(res.get("server"), "Blender")

        network.stop_server()
        self.assertFalse(network.is_server_running())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
