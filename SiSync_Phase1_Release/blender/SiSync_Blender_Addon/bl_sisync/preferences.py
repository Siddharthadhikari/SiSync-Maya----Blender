# -*- coding: utf-8 -*-
"""
SiSync Phase 1 (GoB-Inspired) - Add-on Preferences
Unified options matching Autodesk Maya 2026:
1. Shared Export/Import Exchange Folder Location
2. Reciprocal Axis & Scale Matrix Conversion (Y-Up Maya Standard <-> Blender)
3. GoB-style Axis Remap & Flip Toggles (Flip X / Y / Z)
4. Freeze Location to (0,0,0) & Freeze Rotation to (0,0,0)
"""

import os
import tempfile
import bpy
from bpy.types import AddonPreferences
from bpy.props import StringProperty, EnumProperty, FloatProperty, BoolProperty


DEFAULT_EXCHANGE_DIR = os.path.join(tempfile.gettempdir(), "sisync_bridge").replace("\\", "/")


class SiSyncPreferences(AddonPreferences):
    bl_idname = __package__ if __package__ else "bl_sisync"

    exchange_dir: StringProperty(
        name="Export Location",
        description="Shared folder where SiSync writes and reads FBX files between Maya and Blender",
        subtype='DIR_PATH',
        default=DEFAULT_EXCHANGE_DIR,
    )

    scale_mode: EnumProperty(
        name="Scale Mode",
        description="How unit scale is converted between Blender (meters) and Maya (centimeters)",
        items=[
            ("BUNITS", "Auto Scene Units (m ⇄ cm)", "Automatically converts Blender meters (1.0) <-> Maya centimeters (100.0)"),
            ("MANUAL", "Manual Scale Factor", "Use a custom manual multiplier"),
        ],
        default="BUNITS",
    )

    manual_scale: FloatProperty(
        name="Manual Scale",
        description="Custom scale multiplier when Scale Mode is set to Manual",
        default=1.0,
        min=0.0001,
        max=10000.0,
        precision=4,
    )

    maya_up_axis: EnumProperty(
        name="Up-Axis",
        description="Target world Up-Axis (Maya Y-Up Standard)",
        items=[
            ("Y", "Y-Up (Maya Standard)", "Maya uses Y-Up (+Y Up, +Z Front) with automatic conversion"),
            ("Z", "Z-Up", "Keep Z-Up without Y-Up conversion"),
        ],
        default="Y",
    )

    flip_x_axis: BoolProperty(
        name="Flip X",
        description="Invert X axis during Export/Import (automatically flips normals to keep surface valid)",
        default=False,
    )

    flip_y_axis: BoolProperty(
        name="Flip Y",
        description="Invert Y axis during Export/Import",
        default=False,
    )

    flip_z_axis: BoolProperty(
        name="Flip Z",
        description="Invert Z axis during Export/Import",
        default=False,
    )

    freeze_location: BoolProperty(
        name="Freeze Location to (0,0,0)",
        description="Bake world location into mesh vertices and keep object origin at (0,0,0)",
        default=False,
    )

    freeze_rotation: BoolProperty(
        name="Freeze Rotation to (0,0,0)",
        description="Bake rotation into mesh vertices and keep object rotation at (0,0,0)",
        default=True,
    )

    show_header_buttons: BoolProperty(
        name="Show Header Buttons",
        description="Show GoB-style Export and Import buttons in the 3D Viewport top header",
        default=True,
    )

    def draw(self, context):
        layout = self.layout

        box_path = layout.box()
        box_path.label(text="Exchange Folder Location", icon='FILE_FOLDER')
        box_path.prop(self, "exchange_dir", text="Folder")

        box_axis = layout.box()
        box_axis.label(text="Axis & Scale Correction (GoB Engine)", icon='ORIENTATION_GLOBAL')
        box_axis.prop(self, "maya_up_axis")
        box_axis.prop(self, "scale_mode")
        if self.scale_mode == "MANUAL":
            box_axis.prop(self, "manual_scale")

        row_flip = box_axis.row(align=True)
        row_flip.label(text="Flip Axis:")
        row_flip.prop(self, "flip_x_axis", toggle=True)
        row_flip.prop(self, "flip_y_axis", toggle=True)
        row_flip.prop(self, "flip_z_axis", toggle=True)

        box_axis.prop(self, "freeze_location")
        box_axis.prop(self, "freeze_rotation")
        box_axis.prop(self, "show_header_buttons")


def get_preferences(context=None):
    if context is None:
        context = bpy.context
    pkg = __package__ if __package__ else "bl_sisync"
    addon = context.preferences.addons.get(pkg)
    if addon:
        return addon.preferences
    for k, v in context.preferences.addons.items():
        if k.endswith("bl_sisync"):
            return v.preferences
    return None


def get_exchange_dir(context=None) -> str:
    prefs = get_preferences(context)
    path = prefs.exchange_dir if (prefs and prefs.exchange_dir) else DEFAULT_EXCHANGE_DIR
    path = os.path.abspath(bpy.path.abspath(path))
    os.makedirs(path, exist_ok=True)
    return path


classes = (SiSyncPreferences,)


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
