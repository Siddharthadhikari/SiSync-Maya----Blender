#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync — Maya One-Click Drag & Drop Installer & Uninstaller
Drag this file into Maya's 3D viewport to install or update SiSync cleanly without duplicate shelves or servers.
"""

import os
import sys
import maya.cmds as cmds


def onMayaDroppedPythonFile(*args, **kwargs):
    install(show_dialog=True)


def install(show_dialog: bool = False):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(script_dir)

    # Remove stale SiSync paths
    for p in list(sys.path):
        if "SiSync" in p and p not in (root_dir, script_dir):
            try:
                sys.path.remove(p)
            except ValueError:
                pass

    for p in (root_dir, script_dir):
        if p not in sys.path:
            sys.path.insert(0, p)

    # Stop existing background server cleanly before module eviction
    if "sisync_maya" in sys.modules:
        try:
            sys.modules["sisync_maya"].stop_server()
        except Exception:
            pass

    for mod in ("sisync_maya", "sisync_bridge_core"):
        if mod in sys.modules:
            del sys.modules[mod]

    import sisync_maya
    sisync_maya.initialize(auto_start_server=True)
    if not cmds.about(batch=True):
        sisync_maya.show_ui()
        if show_dialog:
            cmds.confirmDialog(
                title="SiSync Activated!",
                message=(
                    "SiSync Maya - Blender Bridge is now ACTIVE!\n\n"
                    "1. 'SiSync' Shelf tab created/refreshed\n"
                    "2. Unified UI Window opened\n"
                    "3. Background server running on port 19850"
                ),
                button=["Awesome"],
                defaultButton="Awesome",
            )


def uninstall():
    if "sisync_maya" in sys.modules:
        try:
            sys.modules["sisync_maya"].uninstall()
        except Exception:
            pass


if __name__ == "__main__":
    install(show_dialog=False)
