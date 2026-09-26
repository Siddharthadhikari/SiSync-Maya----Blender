#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync Phase 1 (GoB-Inspired) - Maya ⇄ Blender FBX Bridge
Unified Export & Import with Reciprocal Axis & Scale Correction and Collapsible Server/Folder Drawer.
"""

bl_info = {
    "name": "SiSync: Maya ⇄ Blender Bridge (GoB Phase 1)",
    "author": "SiSync Team",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar (N) > SiSync & Top Header",
    "description": "GoB-inspired one-click FBX Export/Import between Maya and Blender with Axis & Scale Correction",
    "category": "Import-Export",
}

import bpy
from . import preferences
from . import sync_mesh
from . import server
from . import operators
from . import ui


def register():
    preferences.register()
    operators.register()
    ui.register()
    server.start_server()
    print("[SiSync Phase 1] GoB-style FBX Export/Import & Server (Port 19851) registered.")


def unregister():
    server.stop_server()
    ui.unregister()
    operators.unregister()
    preferences.unregister()
    print("[SiSync Phase 1] Unregistered.")

