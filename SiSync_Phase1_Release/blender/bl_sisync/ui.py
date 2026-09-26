# -*- coding: utf-8 -*-
"""
SiSync Maya - Blender (GoB-Inspired) - Blender 3D Viewport Top Header & N-Panel UI
Unified with Autodesk Maya 2026 UI:
1. Top: Big Export to Maya / Import from Maya (FBX) buttons
2. Middle: Axis & Scale Correction (FBX) (Up-Axis, Scale, Flip X/Y/Z, Freeze Location, Freeze Rotation)
3. Bottom Collapsible Drawer: Connection, Server & Folder (Closed by default)
"""

import bpy
from bpy.types import Panel
from .preferences import get_preferences
from . import server


def draw_header_buttons(self, context):
    """GoB-style Export / Import buttons on the 3D Viewport Top Header."""
    prefs = get_preferences(context)
    if prefs and not prefs.show_header_buttons:
        return

    layout = self.layout
    row = layout.row(align=True)
    sub = row.row(align=True)
    sub.scale_x = 1.05
    sub.operator("sisync.send_mesh", text="Export Maya", icon='EXPORT')
    sub.operator("sisync.pull_mesh", text="Import Maya", icon='IMPORT')


class VIEW3D_PT_sisync_main(Panel):
    """Main SiSync Maya - Blender Panel (GoB-Style)"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'SiSync'
    bl_label = 'SiSync Maya - Blender'

    def draw(self, context):
        layout = self.layout
        prefs = get_preferences(context)

        # 1. Primary Export / Import Buttons FIRST (Top of UI)
        col_main = layout.column(align=True)
        col_main.scale_y = 1.6
        col_main.operator("sisync.send_mesh", text="EXPORT TO MAYA (FBX)", icon='EXPORT')
        col_main.operator("sisync.pull_mesh", text="IMPORT FROM MAYA (FBX)", icon='IMPORT')

        layout.separator()

        # 2. Axis & Scale Correction Box (Identical options to Maya UI)
        if prefs:
            box_axis = layout.box()
            box_axis.label(text="Axis & Scale Correction (FBX)", icon='ORIENTATION_GLOBAL')
            col_a = box_axis.column(align=True)
            col_a.prop(prefs, "maya_up_axis", text="Up-Axis")
            col_a.prop(prefs, "scale_mode", text="Scale")
            if prefs.scale_mode == "MANUAL":
                col_a.prop(prefs, "manual_scale", text="Factor")

            row_flip = box_axis.row(align=True)
            row_flip.label(text="Flip Axis:")
            row_flip.prop(prefs, "flip_x_axis", text="Flip X", toggle=True)
            row_flip.prop(prefs, "flip_y_axis", text="Flip Y", toggle=True)
            row_flip.prop(prefs, "flip_z_axis", text="Flip Z", toggle=True)

            col_frz = box_axis.column(align=True)
            col_frz.prop(prefs, "freeze_location", text="Freeze Location to (0,0,0)", icon='EMPTY_AXIS')
            col_frz.prop(prefs, "freeze_rotation", text="Freeze Rotation to (0,0,0)", icon='DRIVER_ROTATIONAL_DIFFERENCE')


class VIEW3D_PT_sisync_drawer(Panel):
    """Bottom Collapsible Drawer for Folder & Server Connection (Closed by default)"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'SiSync'
    bl_parent_id = 'VIEW3D_PT_sisync_main'
    bl_label = 'Connection, Server & Folder'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        prefs = get_preferences(context)

        # A. Export / Exchange Folder Location
        box_loc = layout.box()
        row_hdr = box_loc.row(align=True)
        row_hdr.label(text="Export / Exchange Folder", icon='FILE_FOLDER')
        if prefs:
            box_loc.prop(prefs, "exchange_dir", text="")
        box_loc.operator("sisync.open_bridge_dir", text="Open Bridge Folder", icon='EXTERNAL_DRIVE')

        # B. Connection & Server Status (Ports 19851 / 19850)
        box_srv = layout.box()
        running = server.is_server_running()
        status_icon = 'PLAY' if running else 'PAUSE'
        status_txt = f"Server: Active (Port {server.BLENDER_PORT})" if running else f"Server: Stopped (Port {server.BLENDER_PORT})"
        box_srv.label(text=status_txt, icon=status_icon)
        box_srv.label(text=f"Maya Target Port: {server.MAYA_PORT}", icon='LINKED')

        row_srv = box_srv.row(align=True)
        toggle_lbl = "Stop Server" if running else "Start Server"
        row_srv.operator("sisync.toggle_server", text=toggle_lbl, icon='FILE_REFRESH')
        row_srv.operator("sisync.ping_maya", text="Ping Maya", icon='URL')


classes = (
    VIEW3D_PT_sisync_main,
    VIEW3D_PT_sisync_drawer,
)


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass

    try:
        if draw_header_buttons not in bpy.types.VIEW3D_HT_header._draw_funcs:
            bpy.types.VIEW3D_HT_header.append(draw_header_buttons)
    except Exception:
        pass


def unregister():
    try:
        bpy.types.VIEW3D_HT_header.remove(draw_header_buttons)
    except Exception:
        pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
