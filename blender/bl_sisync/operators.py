#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync Phase 1 (GoB-Inspired) - Blender Operators
Includes Export to Maya (works in Object, Edit, and Sculpt Mode!), Import from Maya, Refresh Maps, Open Bridge Folder, Toggle Server, and Ping Maya.
"""

import os
import sys
import bpy
from bpy.types import Operator
from .sync_mesh import BlenderMeshSync, get_exchange_fbx_path
from .sync_maps import BlenderMapManager
from .preferences import get_exchange_dir
from . import server


class SISYNC_OT_send_mesh(Operator):
    """Export selected or actively sculpted mesh(es) to Maya and auto-update Maya viewport"""
    bl_idname = "sisync.send_mesh"
    bl_label = "Export to Maya"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if getattr(context, "sculpt_object", None) is not None:
            return True
        if context.active_object and context.active_object.type == 'MESH':
            return True
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        fbx_path = BlenderMeshSync.export_to_maya(context)
        if not fbx_path:
            self.report({'WARNING'}, "SiSync: Select at least one mesh to export.")
            return {'CANCELLED'}

        notified = server.notify_maya_immediate(fbx_path)
        if notified:
            self.report({'INFO'}, f"SiSync: Exported & Auto-Updated in Maya -> {fbx_path}")
        else:
            self.report({'INFO'}, f"SiSync: Exported FBX -> {fbx_path}")
        return {'FINISHED'}


class SISYNC_OT_pull_mesh(Operator):
    """Import mesh(es) from shared FBX Exchange Location from Maya (GoB-Style)"""
    bl_idname = "sisync.pull_mesh"
    bl_label = "Import from Maya"
    bl_options = {'REGISTER', 'UNDO'}

    source_path: bpy.props.StringProperty(name="Source Path", default="")

    def execute(self, context):
        objs = BlenderMeshSync.import_from_maya(context)
        if not objs:
            fbx_path = get_exchange_fbx_path(context)
            self.report({'WARNING'}, f"SiSync: No FBX found at {fbx_path}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"SiSync: Imported {len(objs)} mesh(es) from Maya.")
        return {'FINISHED'}


class SISYNC_OT_refresh_maps(Operator):
    """Refresh PBR Material Texture Maps from the SiSync Bridge Textures folder"""
    bl_idname = "sisync.refresh_maps"
    bl_label = "Refresh PBR Maps"

    def execute(self, context):
        count = BlenderMapManager.refresh_pbr_maps(context)
        self.report({'INFO'}, f"SiSync: Refreshed {count} PBR texture map(s).")
        return {'FINISHED'}


class SISYNC_OT_open_bridge_dir(Operator):
    """Open the shared FBX Exchange Folder in File Explorer"""
    bl_idname = "sisync.open_bridge_dir"
    bl_label = "Open Bridge Folder"

    def execute(self, context):
        folder = get_exchange_dir(context)
        if sys.platform.startswith("win"):
            os.startfile(folder)
        elif sys.platform == "darwin":
            os.system(f'open "{folder}"')
        else:
            os.system(f'xdg-open "{folder}"')
        return {'FINISHED'}


class SISYNC_OT_toggle_server(Operator):
    """Start or stop the background SiSync Blender listener on port 19851"""
    bl_idname = "sisync.toggle_server"
    bl_label = "Toggle Server"

    def execute(self, context):
        running = server.toggle_server()
        state_str = f"Active (Port {server.BLENDER_PORT})" if running else "Stopped"
        self.report({'INFO'}, f"SiSync Blender Server: {state_str}")
        return {'FINISHED'}


class SISYNC_OT_ping_maya(Operator):
    """Ping the Maya SiSync listener on port 19850"""
    bl_idname = "sisync.ping_maya"
    bl_label = "Ping Maya"

    def execute(self, context):
        resp = server.send_bridge_command("127.0.0.1", server.MAYA_PORT, "GET_STATUS")
        if resp is not None:
            self.report({'INFO'}, f"SiSync: Connected to Maya on Port {server.MAYA_PORT}!")
        else:
            self.report({'WARNING'}, f"SiSync: Maya server not responding on Port {server.MAYA_PORT}.")
        return {'FINISHED'}


classes = (
    SISYNC_OT_send_mesh,
    SISYNC_OT_pull_mesh,
    SISYNC_OT_refresh_maps,
    SISYNC_OT_open_bridge_dir,
    SISYNC_OT_toggle_server,
    SISYNC_OT_ping_maya,
)


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass


def unregister():
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
