#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync Maya - Blender — Autodesk Maya 2026 Production Adapter
- Delegates TCP framing, BridgeServer, BridgeClient, metadata, EchoSuppressor, and logging to sisync_bridge_core
- Guarantees scene state restoration via strict try / except / finally semantics
- Preserves UI layout, single SiSync shelf, and clean install/update/uninstall lifecycle
"""

import os
import gc
import re
import sys
import time
import json
import math
import shutil
import socket
import tempfile
from typing import List, Optional, Dict, Any

import maya.cmds as cmds
import maya.mel as mel
import maya.utils as utils

try:
    import maya.api.OpenMaya as om
    HAS_OPENMAYA = True
except ImportError:
    HAS_OPENMAYA = False

try:
    from PySide6 import QtCore
    HAS_QT = True
except ImportError:
    try:
        from PySide2 import QtCore
        HAS_QT = True
    except ImportError:
        HAS_QT = False

# Ensure canonical sisync_bridge_core is importable from maya/ or repo root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_THIS_DIR)
for _p in (_THIS_DIR, _ROOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sisync_bridge_core as core

# ---------------------------------------------------------------------------
# Explicit Port Architecture (Section 8 & 9)
# ---------------------------------------------------------------------------
MAYA_BRIDGE_PORT = core.MAYA_BRIDGE_PORT          # 19850: Canonical SiSync TCP Bridge
BLENDER_BRIDGE_PORT = core.BLENDER_BRIDGE_PORT    # 19851: Blender SiSync TCP Bridge
MAYA_COMMAND_PORT = core.MAYA_COMMAND_PORT        # 19852: Maya commandPort for main-thread dispatch
MAYA_LEGACY_MCP_PORT = core.MAYA_LEGACY_MCP_PORT  # 7001:  Legacy / external MCP automation port

MAYA_PORT = MAYA_BRIDGE_PORT
BLENDER_PORT = BLENDER_BRIDGE_PORT
MAYA_CMD_PORT = f":{MAYA_COMMAND_PORT}"

DEFAULT_EXCHANGE_DIR = core.get_bridge_dir()
EXCHANGE_FBX_NAME = core.EXCHANGE_FBX_LEGACY
EXCHANGE_META_NAME = core.METADATA_FILENAME

_SERVER_INSTANCE: Optional[core.BridgeServer] = None
_PENDING_SYNC_PATHS: List[str] = []
_LAST_SEEN_BLENDER_TS: float = 0.0
_LAST_SEEN_BLENDER_REV: int = 0
_LAST_IMPORT_TS: float = 0.0
_QT_WATCH_TIMER = None


# ---------------------------------------------------------------------------
# Preferences & OptionVar Helpers
# ---------------------------------------------------------------------------
def get_pref(key: str, default):
    if cmds.optionVar(exists=key):
        val = cmds.optionVar(query=key)
        if isinstance(default, bool):
            return bool(int(val))
        if isinstance(default, float):
            return float(val)
        return val
    return default


def set_pref(key: str, val):
    if isinstance(val, bool):
        cmds.optionVar(intValue=(key, 1 if val else 0))
    elif isinstance(val, int):
        cmds.optionVar(intValue=(key, val))
    elif isinstance(val, float):
        cmds.optionVar(floatValue=(key, float(val)))
    else:
        cmds.optionVar(stringValue=(key, str(val)))


def get_exchange_dir() -> str:
    path = str(get_pref("sisync_exchange_dir", DEFAULT_EXCHANGE_DIR)).replace("\\", "/")
    if not path:
        path = DEFAULT_EXCHANGE_DIR
    return core.get_bridge_dir(path)


def get_exchange_fbx_path(direction: str = "m2b") -> str:
    ex_dir = get_exchange_dir()
    if direction == "b2m":
        return core.get_blender_to_maya_fbx(ex_dir)
    elif direction == "m2b":
        return core.get_maya_to_blender_fbx(ex_dir)
    return core.get_legacy_exchange_fbx(ex_dir)


def get_exchange_meta_path() -> str:
    return core.get_metadata_file_path(get_exchange_dir())


def ensure_fbx_plugin() -> bool:
    try:
        if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
            cmds.loadPlugin("fbxmaya", quiet=True)
        return bool(cmds.pluginInfo("fbxmaya", query=True, loaded=True))
    except Exception as e:
        core.log_event("maya", "LOAD_FBX_PLUGIN", "error", error=str(e))
        cmds.warning(f"[SiSync Maya] Could not load fbxmaya plugin: {e}")
        return False


def show_hud(msg: str):
    print(f"[SiSync Maya] {msg}")
    try:
        if not cmds.about(batch=True):
            cmds.inViewMessage(
                amg=f"<hl>SiSync:</hl> {msg}",
                pos="topCenter",
                fade=True,
                fadeStayTime=1800,
            )
    except Exception:
        pass


def _get_or_set_maya_bridge_id(node: str, name_hint: str = "", force_id: str = "") -> str:
    """Reads or stamps sisync_bridge_id string attribute on a Maya transform node."""
    try:
        if not cmds.attributeQuery("sisync_bridge_id", node=node, exists=True):
            cmds.addAttr(node, longName="sisync_bridge_id", dataType="string")
        curr = cmds.getAttr(f"{node}.sisync_bridge_id") or ""
        if force_id:
            cmds.setAttr(f"{node}.sisync_bridge_id", str(force_id), type="string")
            return str(force_id)
        if not curr:
            curr = core.generate_bridge_id(name_hint or node.split("|")[-1].split(":")[-1])
            cmds.setAttr(f"{node}.sisync_bridge_id", str(curr), type="string")
        return str(curr)
    except Exception:
        return force_id or core.generate_bridge_id(name_hint)


# ---------------------------------------------------------------------------
# Canonical TCP Bridge Server, Client & Main-Thread Auto-Watch Timer
# ---------------------------------------------------------------------------
def send_bridge_command(
    host: str = core.DEFAULT_HOST,
    port: int = BLENDER_PORT,
    command: str = core.COMMAND_GET_STATUS,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 1.5,
) -> Optional[Dict[str, Any]]:
    resp = core.BridgeClient.send(
        host=host,
        port=port,
        command=command,
        sender="maya",
        target="blender" if port == BLENDER_PORT else "maya",
        payload=payload,
        timeout=timeout,
    )
    if isinstance(resp, dict) and resp.get("status") != "error":
        return resp
    return None


def _maya_message_handler(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs inside BridgeServer background worker thread.
    NEVER calls Maya scene modification directly; queues path and schedules via executeDeferred / QTimer.
    """
    cmd = str(msg.get("command", "")).upper()
    payload = msg.get("payload", {}) if isinstance(msg.get("payload"), dict) else {}
    fbx_path = str(payload.get("file_path") or payload.get("fbx_path") or msg.get("fbx_path") or msg.get("file_path") or "")
    rev = int(msg.get("revision", 0))

    if cmd in (core.COMMAND_SYNC_MESH, core.COMMAND_OBJECT_CREATE, core.COMMAND_OBJECT_UPDATE):
        _PENDING_SYNC_PATHS.append(fbx_path)
        try:
            utils.executeDeferred(pull_from_blender, fbx_path)
        except Exception:
            pass
        return {
            "status": "scheduled",
            "command": core.COMMAND_ACK,
            "target": "maya",
            "revision": rev,
        }
    elif cmd == core.COMMAND_REFRESH_MAPS:
        try:
            utils.executeDeferred(refresh_maps_action)
        except Exception:
            pass
        return {
            "status": "scheduled",
            "command": core.COMMAND_ACK,
            "target": "maya",
        }
    elif cmd == core.COMMAND_GET_STATUS:
        return {
            "status": "success",
            "command": core.COMMAND_STATUS_REPORT,
            "target": "maya",
            "port": MAYA_PORT,
        }
    return {
        "status": "ok",
        "command": core.COMMAND_ACK,
        "target": "maya",
    }


def _auto_watch_tick():
    """
    Runs every 350ms on Maya's main Qt thread.
    Drains _PENDING_SYNC_PATHS and checks sisync_metadata.json with deterministic EchoSuppressor.
    """
    global _LAST_SEEN_BLENDER_TS, _LAST_SEEN_BLENDER_REV
    if not is_server_running():
        return

    if _PENDING_SYNC_PATHS:
        fbx_p = _PENDING_SYNC_PATHS.pop(0)
        while _PENDING_SYNC_PATHS:
            fbx_p = _PENDING_SYNC_PATHS.pop(0)
        if not core.EchoSuppressor.is_importing("maya"):
            try:
                pull_from_blender(fbx_p)
            except Exception as e:
                core.log_event("maya", "AUTO_IMPORT_QUEUE", "error", source="blender", destination="maya", error=str(e))
        return

    try:
        meta = core.read_metadata(get_exchange_dir())
        if meta and str(meta.get("source", "")).lower() == "blender":
            ts = float(meta.get("timestamp") or meta.get("export_timestamp") or 0.0)
            rev = int(meta.get("revision", 0))
            is_newer_ts = ts > 0.0 and ts > (_LAST_SEEN_BLENDER_TS + 1e-4) and ts > (_LAST_IMPORT_TS + 1e-4)
            is_newer_rev = rev > 0 and rev > _LAST_SEEN_BLENDER_REV
            if (is_newer_ts or is_newer_rev) and core.EchoSuppressor.should_accept_incoming("maya", meta):
                _LAST_SEEN_BLENDER_TS = ts
                _LAST_SEEN_BLENDER_REV = max(_LAST_SEEN_BLENDER_REV, rev)
                pull_from_blender(meta.get("file_path") or meta.get("fbx_path"))
    except Exception:
        pass


def _start_auto_watch_timer():
    """Starts the Qt QTimer on Maya's main thread without creating duplicate timers."""
    global _QT_WATCH_TIMER, _LAST_SEEN_BLENDER_TS, _LAST_SEEN_BLENDER_REV
    try:
        meta = core.read_metadata(get_exchange_dir())
        if meta:
            _LAST_SEEN_BLENDER_TS = max(_LAST_SEEN_BLENDER_TS, float(meta.get("timestamp") or meta.get("export_timestamp") or 0.0))
            _LAST_SEEN_BLENDER_REV = max(_LAST_SEEN_BLENDER_REV, int(meta.get("revision", 0)))
            core.EchoSuppressor.end_import("maya", revision=_LAST_SEEN_BLENDER_REV, timestamp=_LAST_SEEN_BLENDER_TS)
    except Exception:
        pass

    if HAS_QT and not cmds.about(batch=True):
        try:
            if _QT_WATCH_TIMER is not None:
                try:
                    _QT_WATCH_TIMER.stop()
                    _QT_WATCH_TIMER.deleteLater()
                except Exception:
                    pass
                _QT_WATCH_TIMER = None
            app = QtCore.QCoreApplication.instance()
            if app:
                _QT_WATCH_TIMER = QtCore.QTimer(app)
                _QT_WATCH_TIMER.setInterval(350)
                _QT_WATCH_TIMER.timeout.connect(_auto_watch_tick)
                _QT_WATCH_TIMER.start()
        except Exception as e:
            core.log_event("maya", "QTIMER_START", "error", error=str(e))


def is_server_running() -> bool:
    return bool(_SERVER_INSTANCE and _SERVER_INSTANCE.is_running)


def start_server(port: int = MAYA_PORT) -> bool:
    """Idempotent startup of Maya's BridgeServer (19850), commandPort (19852), and QTimer."""
    global _SERVER_INSTANCE

    # Stop any stale BridgeServer instances from previous reloads
    try:
        for obj in gc.get_objects():
            if type(obj).__name__ == "BridgeServer" and obj is not _SERVER_INSTANCE and getattr(obj, "is_running", False):
                if getattr(obj, "port", None) == port:
                    try:
                        obj.stop()
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        if not cmds.commandPort(MAYA_CMD_PORT, query=True):
            cmds.commandPort(name=MAYA_CMD_PORT, sourceType="python")
    except Exception:
        pass

    if _SERVER_INSTANCE and _SERVER_INSTANCE.is_running:
        if _SERVER_INSTANCE.port == port:
            _start_auto_watch_timer()
            _refresh_server_ui_label()
            return True
        else:
            _SERVER_INSTANCE.stop()
            _SERVER_INSTANCE = None

    _SERVER_INSTANCE = core.BridgeServer(
        name="Maya",
        port=port,
        host=core.DEFAULT_HOST,
        message_handler=_maya_message_handler,
    )
    ok = _SERVER_INSTANCE.start()
    _start_auto_watch_timer()
    _refresh_server_ui_label()
    return ok


def stop_server():
    """Idempotent shutdown of Maya's BridgeServer and QTimer."""
    global _SERVER_INSTANCE, _QT_WATCH_TIMER
    if _QT_WATCH_TIMER is not None:
        try:
            _QT_WATCH_TIMER.stop()
            _QT_WATCH_TIMER.deleteLater()
        except Exception:
            pass
        _QT_WATCH_TIMER = None

    if _SERVER_INSTANCE:
        _SERVER_INSTANCE.stop()
        _SERVER_INSTANCE = None

    _refresh_server_ui_label()


def toggle_server(*_):
    if is_server_running():
        stop_server()
        show_hud("Maya Server stopped.")
    else:
        start_server(MAYA_PORT)
        show_hud(f"Maya Server active on Port {MAYA_PORT}.")
    _refresh_server_ui_label()


def ping_blender(*_):
    resp = send_bridge_command("127.0.0.1", BLENDER_PORT, core.COMMAND_GET_STATUS)
    if resp is not None:
        show_hud(f"Connected to Blender on Port {BLENDER_PORT}!")
    else:
        show_hud(f"Blender server not responding on Port {BLENDER_PORT}.")


def _refresh_server_ui_label():
    try:
        if cmds.about(batch=True):
            return
        if cmds.text("sisync_srv_status_lbl", exists=True):
            active = is_server_running()
            txt = f"Server: Active (Port {MAYA_PORT})" if active else f"Server: Stopped (Port {MAYA_PORT})"
            cmds.text("sisync_srv_status_lbl", edit=True, label=txt)
        if cmds.button("sisync_srv_toggle_btn", exists=True):
            active = is_server_running()
            cmds.button("sisync_srv_toggle_btn", edit=True, label="Stop Server" if active else "Start Server")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Export: Maya -> Blender (Strict try / except / finally Restoration)
# ---------------------------------------------------------------------------
def _strip_sisync_suffix(name: str) -> str:
    n = name.split("|")[-1].split(":")[-1]
    n = re.sub(r"__sisync_(?:dup|orig|target)\d*$", "", n)
    return n


def _sanitize_name(name: str) -> str:
    n = _strip_sisync_suffix(name)
    n = n.split(".")[0]
    return n.replace(" ", "_").replace("-", "_")


def send_to_blender(*_) -> Optional[str]:
    """
    Exports selected Maya polygon mesh(es) to maya_to_blender.fbx (and mirrors to SiSync_Exchange.fbx).
    Guarantees scene restoration (names, temporary duplicates, selection) in a finally block.
    """
    if not ensure_fbx_plugin():
        return None

    initial_sel = cmds.ls(selection=True, long=True) or []
    mesh_transforms = []
    for node in initial_sel:
        if cmds.nodeType(node) == "mesh":
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            if parents and parents[0] not in mesh_transforms:
                mesh_transforms.append(parents[0])
        elif cmds.nodeType(node) == "transform":
            shapes = cmds.listRelatives(node, shapes=True, fullPath=True, noIntermediate=True) or []
            if any(cmds.nodeType(s) == "mesh" for s in shapes):
                mesh_transforms.append(node)
            else:
                desc_meshes = cmds.listRelatives(node, allDescendents=True, type="mesh", fullPath=True) or []
                for dm in desc_meshes:
                    if not cmds.getAttr(f"{dm}.intermediateObject"):
                        p = (cmds.listRelatives(dm, parent=True, fullPath=True) or [None])[0]
                        if p and p not in mesh_transforms:
                            mesh_transforms.append(p)

    if not mesh_transforms:
        cmds.warning("[SiSync Maya] Select at least one polygon mesh to export.")
        show_hud("Select at least one mesh to export.")
        return None

    ex_dir = get_exchange_dir()
    m2b_fbx = core.get_maya_to_blender_fbx(ex_dir)
    legacy_fbx = core.get_legacy_exchange_fbx(ex_dir)

    up_axis_mode = str(get_pref("sisync_up_axis", "Y"))
    if up_axis_mode not in ("Y", "Z"):
        up_axis_mode = "Y"
    scale_mode = str(get_pref("sisync_scale_mode", "BUNITS"))
    manual_scale = float(get_pref("sisync_manual_scale", 1.0)) if scale_mode == "MANUAL" else 1.0

    flip_x = bool(get_pref("sisync_flip_x", False))
    flip_y = bool(get_pref("sisync_flip_y", False))
    flip_z = bool(get_pref("sisync_flip_z", False))
    freeze_loc = bool(get_pref("sisync_freeze_loc", False))
    freeze_rot = bool(get_pref("sisync_freeze_rot", True))

    sx = (-1.0 if flip_x else 1.0) * manual_scale
    sy = (-1.0 if flip_y else 1.0) * manual_scale
    sz = (-1.0 if flip_z else 1.0) * manual_scale

    temp_exports: List[str] = []
    renamed_originals: List[ tuple ] = []
    meta_objects: Dict[str, Any] = {}
    rev = core.next_revision()
    export_succeeded = False

    try:
        for obj in mesh_transforms:
            short_name = _sanitize_name(cmds.ls(obj, shortNames=True)[0])
            b_id = _get_or_set_maya_bridge_id(obj, short_name)
            piv = cmds.xform(obj, query=True, worldSpace=True, rotatePivot=True)
            rot_deg = cmds.xform(obj, query=True, worldSpace=True, rotation=True)

            # Compute corresponding Blender location (meters, Z-Up) and rotation
            bl_loc_m = list(
                core.CoordinateBasis.maya_point_to_blender(
                    (float(piv[0]), float(piv[1]), float(piv[2])),
                    up_axis=up_axis_mode,
                    unit_scale=0.01 * manual_scale,
                    flip_x=flip_x,
                    flip_y=flip_y,
                    flip_z=flip_z,
                )
            )
            if freeze_loc:
                bl_loc_m = [0.0, 0.0, 0.0]

            if freeze_rot:
                bl_rot_deg = [0.0, 0.0, 0.0]
            elif up_axis_mode == "Y":
                bl_rot_deg = [float(rot_deg[0]), float(-rot_deg[2]), float(rot_deg[1])]
            else:
                bl_rot_deg = [float(rot_deg[0]), float(rot_deg[1]), float(rot_deg[2])]

            dup = cmds.duplicate(obj, returnRootsOnly=True)[0]
            if cmds.listRelatives(dup, parent=True):
                dup = cmds.parent(dup, world=True)[0]

            # Rename original temporarily so duplicate carries the exact canonical short_name in FBX
            orig_temp_name = cmds.rename(obj, f"{short_name}__sisync_orig")
            dup = cmds.rename(dup, short_name)
            renamed_originals.append((orig_temp_name, short_name, dup))
            temp_exports.append(dup)

            try:
                cmds.delete(dup, constructionHistory=True)
            except Exception:
                pass

            for attr in ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"):
                try:
                    cmds.setAttr(f"{dup}.{attr}", lock=False)
                except Exception:
                    pass

            if HAS_OPENMAYA:
                s_list = om.MSelectionList()
                s_list.add(orig_temp_name)
                s_list.add(dup)
                src_dag = s_list.getDagPath(0)
                dup_dag = s_list.getDagPath(1)
                src_dag.extendToShape()
                dup_dag.extendToShape()
                src_fn = om.MFnMesh(src_dag)
                dup_fn = om.MFnMesh(dup_dag)
                world_pts = src_fn.getPoints(om.MSpace.kWorld)
                for i in range(len(world_pts)):
                    p = world_pts[i]
                    world_pts[i] = om.MPoint(p.x * sx, p.y * sy, p.z * sz, p.w)
                dup_fn.setPoints(world_pts, om.MSpace.kWorld)
                dup_fn.updateSurface()
                if (sx * sy * sz) < 0.0:
                    cmds.polyNormal(dup, normalMode=0, userNormalMode=0, ch=False)

            vtx_count = int(cmds.polyEvaluate(dup, vertex=True) or 0)
            meta_objects[short_name] = {
                "bridge_id": b_id,
                "revision": rev,
                "maya_pivot": [float(piv[0]), float(piv[1]), float(piv[2])],
                "maya_rot": [float(rot_deg[0]), float(rot_deg[1]), float(rot_deg[2])],
                "location_blender_m": bl_loc_m,
                "rotation_blender_deg": bl_rot_deg,
                "freeze_location": freeze_loc,
                "freeze_rotation": freeze_rot,
                "vertex_count": vtx_count,
            }

        cmds.select(temp_exports, replace=True)
        mel.eval(f'FBXExport -f "{m2b_fbx}" -s')
        export_succeeded = True

    except Exception as e:
        core.log_event("maya", "EXPORT_TO_BLENDER", "error", source="maya", destination="blender", revision=rev, error=str(e))
        print(f"[SiSync Maya] Export error: {e}")
        return None

    finally:
        # Unconditional cleanup of temporary duplicates and restoration of original object names & selection
        for orig_temp, short_name, dup_name in reversed(renamed_originals):
            try:
                if dup_name and cmds.objExists(dup_name):
                    cmds.delete(dup_name)
            except Exception as del_err:
                core.log_event("maya", "CLEANUP_DUP", "error", error=str(del_err), extra=str(dup_name))
            try:
                if orig_temp and cmds.objExists(orig_temp):
                    cmds.rename(orig_temp, short_name)
            except Exception as ren_err:
                core.log_event("maya", "RESTORE_NAME", "error", error=str(ren_err), extra=str(orig_temp))

        try:
            valid_sel = [n for n in initial_sel if cmds.objExists(n)]
            if valid_sel:
                cmds.select(valid_sel, replace=True)
        except Exception:
            pass

    if not export_succeeded:
        return None

    # Mirror to SiSync_Exchange.fbx for backward compatibility
    try:
        shutil.copy2(m2b_fbx, legacy_fbx)
    except Exception:
        pass

    export_ts = time.time()
    core.EchoSuppressor.record_export("maya", rev)
    core.write_metadata(
        {
            "revision": rev,
            "source": "maya",
            "destination": "blender",
            "format": "fbx",
            "file_path": m2b_fbx,
            "fbx_path": m2b_fbx,
            "timestamp": export_ts,
            "export_timestamp": export_ts,
            "maya_up_axis": up_axis_mode,
            "freeze_location": freeze_loc,
            "freeze_rotation": freeze_rot,
            "objects": meta_objects,
        },
        custom_dir=ex_dir,
    )

    show_hud(f"Exported {len(mesh_transforms)} mesh(es) -> {m2b_fbx}")
    send_bridge_command(
        "127.0.0.1",
        BLENDER_PORT,
        core.COMMAND_SYNC_MESH,
        {"file_path": m2b_fbx, "fbx_path": m2b_fbx, "format": "fbx", "revision": rev},
    )
    core.log_event("maya", "EXPORT_TO_BLENDER", "success", source="maya", destination="maya", revision=rev, extra=m2b_fbx)
    return m2b_fbx


# ---------------------------------------------------------------------------
# Import: Blender -> Maya (With bridge_id & Sculpt/Remesh Topology Support)
# ---------------------------------------------------------------------------
def pull_from_blender(custom_fbx_path: Optional[str] = None, *_) -> Optional[List[str]]:
    """
    Imports blender_to_maya.fbx (or metadata-specified FBX) into Maya, matches by
    bridge_id or sanitized name, clears .pnts tweaks on remesh, and updates in-place without duplicating.
    """
    global _LAST_IMPORT_TS, _LAST_SEEN_BLENDER_TS, _LAST_SEEN_BLENDER_REV

    if core.EchoSuppressor.is_importing("maya"):
        return []

    if not ensure_fbx_plugin():
        return []

    ex_dir = get_exchange_dir()
    meta = core.read_metadata(ex_dir)
    meta_objects = meta.get("objects", {}) if isinstance(meta.get("objects"), dict) else {}
    rev = int(meta.get("revision", 0))
    ts = float(meta.get("timestamp") or meta.get("export_timestamp") or time.time())

    # Resolve authoritative FBX path
    candidates = [
        str(custom_fbx_path or "").replace("\\", "/"),
        str(meta.get("file_path") or meta.get("fbx_path") or "").replace("\\", "/"),
        core.get_blender_to_maya_fbx(ex_dir),
        core.get_legacy_exchange_fbx(ex_dir),
    ]
    temp_fbx = ""
    for c in candidates:
        if c and os.path.exists(c) and os.path.isfile(c):
            temp_fbx = c
            break
    if not temp_fbx:
        return []

    core.EchoSuppressor.begin_import("maya")
    _LAST_IMPORT_TS = time.time()
    _LAST_SEEN_BLENDER_TS = max(_LAST_SEEN_BLENDER_TS, ts)
    _LAST_SEEN_BLENDER_REV = max(_LAST_SEEN_BLENDER_REV, rev)

    is_manual = str(get_pref("sisync_scale_mode", "BUNITS")) == "MANUAL"
    manual_scale = float(get_pref("sisync_manual_scale", 1.0)) if is_manual else 1.0
    flip_x = bool(get_pref("sisync_flip_x", False))
    flip_y = bool(get_pref("sisync_flip_y", False))
    flip_z = bool(get_pref("sisync_flip_z", False))
    freeze_loc = bool(get_pref("sisync_freeze_loc", False))
    freeze_rot = bool(get_pref("sisync_freeze_rot", True))

    sx = (-1.0 if flip_x else 1.0) * manual_scale
    sy = (-1.0 if flip_y else 1.0) * manual_scale
    sz = (-1.0 if flip_z else 1.0) * manual_scale

    # Build lookup of candidate names and bridge_ids from metadata
    candidate_names = {_sanitize_name(k) for k in meta_objects.keys()} if isinstance(meta_objects, dict) else set()
    bridge_id_to_meta_name: Dict[str, str] = {}
    for k, v in meta_objects.items():
        if isinstance(v, dict) and v.get("bridge_id"):
            bridge_id_to_meta_name[str(v["bridge_id"])] = _sanitize_name(k)

    stash_map: Dict[str, str] = {}
    bridge_id_stash_map: Dict[str, str] = {}
    all_transforms = cmds.ls(type="transform", long=True) or []
    for t_node in all_transforms:
        shapes = cmds.listRelatives(t_node, shapes=True, fullPath=True, noIntermediate=True) or []
        if not any(cmds.nodeType(s) == "mesh" for s in shapes):
            continue
        short_n = _sanitize_name(t_node.split("|")[-1].split(":")[-1])
        node_bid = ""
        if cmds.attributeQuery("sisync_bridge_id", node=t_node, exists=True):
            try:
                node_bid = str(cmds.getAttr(f"{t_node}.sisync_bridge_id") or "")
            except Exception:
                node_bid = ""

        should_stash = (
            not candidate_names
            or short_n in candidate_names
            or (node_bid and node_bid in bridge_id_to_meta_name)
        )
        if should_stash:
            try:
                stashed = cmds.rename(t_node, f"{short_n}__sisync_target")
                stash_map[short_n] = stashed
                if node_bid:
                    bridge_id_stash_map[node_bid] = stashed
            except Exception:
                pass

    updated_objects: List[str] = []
    try:
        transforms_before = set(cmds.ls(type="transform", long=True) or [])
        try:
            cmds.file(temp_fbx, i=True, type="FBX", ignoreVersion=True, mergeNamespacesOnClash=False)
        except Exception as imp_err:
            core.log_event("maya", "FBX_IMPORT", "error", error=str(imp_err), extra=temp_fbx)

        transforms_after = set(cmds.ls(type="transform", long=True) or [])
        imported_transforms = list(transforms_after - transforms_before)
        imported_meshes = [
            node for node in imported_transforms if cmds.listRelatives(node, shapes=True, type="mesh")
        ]

        for imp_obj in imported_meshes:
            raw_short = imp_obj.split("|")[-1].split(":")[-1]
            base_name = _sanitize_name(raw_short)
            # Strip trailing auto-increment digits if base_name isn't directly in meta_objects
            if base_name not in meta_objects:
                stripped_digits = re.sub(r"\d+$", "", base_name)
                if stripped_digits in meta_objects or stripped_digits in stash_map:
                    base_name = stripped_digits

            obj_meta = meta_objects.get(base_name, {}) if isinstance(meta_objects, dict) else {}
            if not obj_meta and len(imported_meshes) == 1 and len(meta_objects) == 1:
                base_name, obj_meta = next(iter(meta_objects.items()))
                base_name = _sanitize_name(base_name)

            inc_bid = str(obj_meta.get("bridge_id") or "")

            raw_trans = cmds.xform(imp_obj, query=True, worldSpace=True, translation=True)
            raw_rot = cmds.xform(imp_obj, query=True, worldSpace=True, rotation=True)
            target_piv = [0.0, 0.0, 0.0] if freeze_loc else [raw_trans[0] * sx, raw_trans[1] * sy, raw_trans[2] * sz]
            target_rot = [0.0, 0.0, 0.0] if freeze_rot else raw_rot

            stashed_target = stash_map.pop(base_name, None)
            if not stashed_target and inc_bid and inc_bid in bridge_id_stash_map:
                stashed_target = bridge_id_stash_map.pop(inc_bid, None)
                # Also remove from stash_map so finally block doesn't double-rename
                for k_s, v_s in list(stash_map.items()):
                    if v_s == stashed_target:
                        stash_map.pop(k_s, None)

            existing = [stashed_target] if (stashed_target and cmds.objExists(stashed_target)) else []

            if existing and HAS_OPENMAYA:
                target_obj = existing[0]
                hist = cmds.listHistory(target_obj) or []
                has_skin = bool(cmds.ls(hist, type="skinCluster"))
                target_vtx = cmds.polyEvaluate(target_obj, vertex=True)
                imp_vtx = cmds.polyEvaluate(imp_obj, vertex=True)

                if abs(sx - 1.0) > 1e-5 or abs(sy - 1.0) > 1e-5 or abs(sz - 1.0) > 1e-5:
                    cmds.xform(imp_obj, scale=(sx, sy, sz))

                cmds.makeIdentity(imp_obj, apply=True, translate=freeze_loc, rotate=freeze_rot, scale=True, normal=False)
                try:
                    cmds.delete(imp_obj, constructionHistory=True)
                except Exception:
                    pass

                if not has_skin:
                    try:
                        cmds.delete(target_obj, constructionHistory=True)
                    except Exception:
                        pass
                    old_shapes = cmds.listRelatives(target_obj, shapes=True, fullPath=True, noIntermediate=True) or []
                    new_shapes = cmds.listRelatives(imp_obj, shapes=True, fullPath=True, noIntermediate=True) or []
                    if old_shapes and new_shapes:
                        try:
                            # Clear Maya vertex tweaks (.pnts) before topology replacement so remeshed meshes never explode
                            try:
                                tweak_count = cmds.getAttr(f"{old_shapes[0]}.pnts", size=True)
                                for i in range(tweak_count):
                                    cmds.setAttr(f"{old_shapes[0]}.pnts[{i}]", 0.0, 0.0, 0.0)
                            except Exception:
                                pass

                            cmds.connectAttr(f"{new_shapes[0]}.outMesh", f"{old_shapes[0]}.inMesh", force=True)
                            cmds.refresh()
                            cmds.disconnectAttr(f"{new_shapes[0]}.outMesh", f"{old_shapes[0]}.inMesh")
                        except Exception as e:
                            core.log_event("maya", "TOPOLOGY_TRANSFER", "error", error=str(e), extra=base_name)
                else:
                    if target_vtx != imp_vtx:
                        show_hud(f"Skipped topology change on rigged mesh: {base_name}")
                        cmds.delete(imp_obj)
                        cmds.rename(target_obj, base_name)
                        continue
                    s_list = om.MSelectionList()
                    s_list.add(imp_obj)
                    imp_dag = s_list.getDagPath(0)
                    imp_dag.extendToShape()
                    world_pts = om.MFnMesh(imp_dag).getPoints(om.MSpace.kWorld)

                    t_list = om.MSelectionList()
                    t_list.add(target_obj)
                    t_dag = t_list.getDagPath(0)
                    t_dag.extendToShape()
                    t_fn = om.MFnMesh(t_dag)
                    try:
                        t_fn.setPoints(world_pts, om.MSpace.kWorld)
                        t_fn.updateSurface()
                    except Exception as e:
                        core.log_event("maya", "SET_POINTS", "error", error=str(e), extra=base_name)

                cmds.xform(target_obj, worldSpace=True, translation=target_piv, rotation=target_rot, scale=(1.0, 1.0, 1.0))

                if (sx * sy * sz) < 0.0:
                    cmds.polyNormal(target_obj, normalMode=0, userNormalMode=0, ch=False)

                try:
                    cmds.dgdirty(target_obj)
                except Exception:
                    pass
                cmds.delete(imp_obj)
                target_obj = cmds.rename(target_obj, base_name)
                _get_or_set_maya_bridge_id(target_obj, base_name, force_id=inc_bid)
                updated_objects.append(target_obj)
                continue

            # New object path
            if existing:
                cmds.delete(existing[0])
            if cmds.listRelatives(imp_obj, parent=True):
                imp_obj = cmds.parent(imp_obj, world=True)[0]
            new_obj = cmds.rename(imp_obj, f":{base_name}") if ":" in imp_obj else cmds.rename(imp_obj, base_name)
            for shp in (cmds.listRelatives(new_obj, shapes=True, fullPath=True) or []):
                if ":" in shp:
                    try:
                        cmds.rename(shp, f":{base_name}Shape")
                    except Exception:
                        pass

            if abs(sx - 1.0) > 1e-5 or abs(sy - 1.0) > 1e-5 or abs(sz - 1.0) > 1e-5:
                cmds.xform(new_obj, scale=(sx, sy, sz))

            cmds.makeIdentity(new_obj, apply=True, translate=freeze_loc, rotate=freeze_rot, scale=True, normal=False)
            try:
                cmds.delete(new_obj, constructionHistory=True)
            except Exception:
                pass

            if not freeze_loc:
                cmds.xform(new_obj, worldSpace=True, translation=target_piv)
            if not freeze_rot:
                cmds.xform(new_obj, worldSpace=True, rotation=target_rot)

            if (sx * sy * sz) < 0.0:
                cmds.polyNormal(new_obj, normalMode=0, userNormalMode=0, ch=False)

            _get_or_set_maya_bridge_id(new_obj, base_name, force_id=inc_bid)
            updated_objects.append(new_obj)

    finally:
        # Always restore any remaining stashed target objects and end the import guard
        for orig_short, stashed_node in stash_map.items():
            if cmds.objExists(stashed_node):
                try:
                    cmds.rename(stashed_node, orig_short)
                except Exception:
                    pass
        core.EchoSuppressor.end_import("maya", revision=rev, timestamp=ts)

    if updated_objects:
        cmds.select(updated_objects, replace=True)
        cmds.refresh(force=True)
        show_hud(f"Auto-Synced {len(updated_objects)} mesh(es) from Blender.")
        core.log_event("maya", "IMPORT_FROM_BLENDER", "success", source="blender", destination="maya", revision=rev, extra=f"count={len(updated_objects)}")

    return updated_objects


def refresh_maps_action(*_):
    """Reloads file texture nodes in Maya that reside in the SiSync bridge textures directory."""
    tex_dir = os.path.normpath(core.get_textures_dir(get_exchange_dir()))
    reloaded = 0
    for fnode in (cmds.ls(type="file") or []):
        try:
            fpath = cmds.getAttr(f"{fnode}.fileTextureName") or ""
            if not fpath:
                continue
            norm_p = os.path.normpath(fpath)
            cand_p = os.path.join(tex_dir, os.path.basename(norm_p))
            if os.path.exists(norm_p):
                cmds.setAttr(f"{fnode}.fileTextureName", norm_p.replace("\\", "/"), type="string")
                reloaded += 1
            elif os.path.exists(cand_p):
                cmds.setAttr(f"{fnode}.fileTextureName", cand_p.replace("\\", "/"), type="string")
                reloaded += 1
        except Exception as e:
            core.log_event("maya", "REFRESH_MAP", "error", error=str(e), extra=fnode)
    show_hud(f"Refreshed {reloaded} texture node(s).")
    return reloaded


# Legacy class aliases for backward compatibility
class MayaExporter:
    export_selection_to_blender = staticmethod(send_to_blender)

    @staticmethod
    def export_selection(*_):
        fbx_p = send_to_blender()
        if not fbx_p:
            return None
        return core.read_metadata(get_exchange_dir())


class MayaImporter:
    import_from_blender = staticmethod(pull_from_blender)


class MayaTextureManager:
    refresh_material_maps = staticmethod(refresh_maps_action)


# ---------------------------------------------------------------------------
# Unified Maya UI Window (Preserving Current Design)
# ---------------------------------------------------------------------------
def _browse_exchange_dir(*_):
    res = cmds.fileDialog2(fileMode=3, caption="Select SiSync Shared Exchange Folder", startingDirectory=get_exchange_dir())
    if res and res[0]:
        folder = res[0].replace("\\", "/")
        set_pref("sisync_exchange_dir", folder)
        if cmds.textField("sisync_dir_tf", exists=True):
            cmds.textField("sisync_dir_tf", edit=True, text=folder)


def open_bridge_folder(*_):
    folder = get_exchange_dir()
    if sys.platform.startswith("win"):
        os.startfile(folder)
    elif sys.platform == "darwin":
        os.system(f'open "{folder}"')
    else:
        os.system(f'xdg-open "{folder}"')


def _on_up_axis_change(val: str):
    set_pref("sisync_up_axis", "Z" if val.startswith("Z-Up") else "Y")


def _on_scale_mode_change(val: str):
    is_manual = "Manual" in val
    set_pref("sisync_scale_mode", "MANUAL" if is_manual else "BUNITS")
    if cmds.floatField("sisync_manual_scale_ff", exists=True):
        cmds.floatField("sisync_manual_scale_ff", edit=True, enable=is_manual)


def show_ui(*_):
    """Opens the unified GoB-Style Maya Window matching Blender's UI."""
    if cmds.about(batch=True):
        return

    set_pref("sisync_up_axis", "Y")

    win_name = "SiSyncWindow"
    if cmds.window(win_name, exists=True):
        cmds.deleteUI(win_name)

    win = cmds.window(
        win_name,
        title="SiSync Maya - Blender",
        widthHeight=(370, 380),
        sizeable=True,
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6, columnOffset=("both", 10))
    cmds.separator(height=4, style="none")

    cmds.button(
        label="▶  EXPORT TO BLENDER (FBX)",
        backgroundColor=(0.15, 0.65, 0.35),
        height=44,
        command=send_to_blender,
    )
    cmds.button(
        label="◀  IMPORT FROM BLENDER (FBX)",
        backgroundColor=(0.18, 0.50, 0.78),
        height=44,
        command=pull_from_blender,
    )

    cmds.separator(height=4, style="none")
    cmds.frameLayout(label="Axis & Scale Correction (FBX)", collapsable=False, marginWidth=8, marginHeight=6)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=5)

    cmds.optionMenu("sisync_up_axis_menu", label="Up-Axis: ", changeCommand=_on_up_axis_change)
    cmds.menuItem(label="Y-Up (Maya Standard)")
    cmds.menuItem(label="Z-Up")
    cmds.optionMenu("sisync_up_axis_menu", edit=True, select=1)

    is_manual = str(get_pref("sisync_scale_mode", "BUNITS")) == "MANUAL"
    cmds.rowLayout(numberOfColumns=2, adjustableColumn=1, columnWidth2=(250, 80))
    cmds.optionMenu("sisync_scale_mode_menu", label="Scale:    ", changeCommand=_on_scale_mode_change)
    cmds.menuItem(label="Auto Scene Units (m ⇄ cm)")
    cmds.menuItem(label="Manual Scale Factor")
    if is_manual:
        cmds.optionMenu("sisync_scale_mode_menu", edit=True, select=2)
    cmds.floatField(
        "sisync_manual_scale_ff",
        value=float(get_pref("sisync_manual_scale", 1.0)),
        precision=3,
        enable=is_manual,
        changeCommand=lambda v: set_pref("sisync_manual_scale", float(v)),
    )
    cmds.setParent("..")

    cmds.rowLayout(numberOfColumns=4, columnWidth4=(75, 80, 80, 80))
    cmds.text(label="Flip Axis:")
    cmds.checkBox(
        label="Flip X",
        value=get_pref("sisync_flip_x", False),
        changeCommand=lambda v: set_pref("sisync_flip_x", bool(v)),
    )
    cmds.checkBox(
        label="Flip Y",
        value=get_pref("sisync_flip_y", False),
        changeCommand=lambda v: set_pref("sisync_flip_y", bool(v)),
    )
    cmds.checkBox(
        label="Flip Z",
        value=get_pref("sisync_flip_z", False),
        changeCommand=lambda v: set_pref("sisync_flip_z", bool(v)),
    )
    cmds.setParent("..")

    cmds.checkBox(
        "sisync_freeze_loc_cb",
        label="Freeze Location to (0,0,0)",
        value=get_pref("sisync_freeze_loc", False),
        changeCommand=lambda v: set_pref("sisync_freeze_loc", bool(v)),
    )
    cmds.checkBox(
        "sisync_freeze_rot",
        label="Freeze Rotation to (0,0,0)",
        value=get_pref("sisync_freeze_rot", True),
        changeCommand=lambda v: set_pref("sisync_freeze_rot", bool(v)),
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=4, style="none")
    cmds.frameLayout(
        label="Connection, Server & Folder",
        collapsable=True,
        collapse=True,
        marginWidth=8,
        marginHeight=6,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=5)

    cmds.text(label="Export / Exchange Folder:", align="left")
    cmds.rowLayout(numberOfColumns=2, adjustableColumn=1, columnWidth2=(260, 75))
    cmds.textField(
        "sisync_dir_tf",
        text=get_exchange_dir(),
        changeCommand=lambda val: set_pref("sisync_exchange_dir", val.replace("\\", "/")),
    )
    cmds.button(label="Browse...", command=_browse_exchange_dir)
    cmds.setParent("..")
    cmds.button(label="Open Bridge Folder", height=24, command=open_bridge_folder)

    cmds.separator(height=4, style="in")
    active = is_server_running()
    srv_label = f"Server: Active (Port {MAYA_PORT})" if active else f"Server: Stopped (Port {MAYA_PORT})"
    cmds.text("sisync_srv_status_lbl", label=srv_label, align="left", font="boldLabelFont")
    cmds.text(label=f"Blender Target Port: {BLENDER_PORT}", align="left")

    cmds.rowLayout(numberOfColumns=2, columnWidth2=(165, 165))
    cmds.button(
        "sisync_srv_toggle_btn",
        label="Stop Server" if active else "Start Server",
        height=26,
        command=toggle_server,
    )
    cmds.button(
        label="Ping Blender",
        height=26,
        command=ping_blender,
    )
    cmds.setParent("..")

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.showWindow(win)


# ---------------------------------------------------------------------------
# Shelf, Menu, Initialization & Clean Uninstall (Section 24 & 25)
# ---------------------------------------------------------------------------
def _reload_and_run_cmd(func_name: str) -> str:
    return f"import sisync_maya\nsisync_maya.{func_name}()"


def _ensure_shelf():
    """Ensures exactly one 'SiSync' shelf exists and refreshes its buttons cleanly."""
    if cmds.about(batch=True):
        return
    try:
        top_shelf = mel.eval("$tmpVar=$gShelfTopLevel")
        if not cmds.tabLayout(top_shelf, exists=True):
            return
        shelf_name = "SiSync"
        if cmds.shelfLayout(shelf_name, exists=True):
            children = cmds.shelfLayout(shelf_name, query=True, childArray=True) or []
            for c in children:
                cmds.deleteUI(c)
        else:
            cmds.shelfLayout(shelf_name, parent=top_shelf)

        cmds.shelfButton(
            parent=shelf_name,
            label="SiSync UI",
            imageOverlayLabel="SiSync",
            annotation="Open SiSync: Maya <-> Blender Window",
            image1="pythonFamily.png",
            sourceType="python",
            command=_reload_and_run_cmd("show_ui"),
        )
        cmds.shelfButton(
            parent=shelf_name,
            label="Export",
            imageOverlayLabel="Export",
            annotation="Export selected mesh(es) to Blender (FBX)",
            image1="polySphere.png",
            sourceType="python",
            command=_reload_and_run_cmd("send_to_blender"),
        )
        cmds.shelfButton(
            parent=shelf_name,
            label="Import",
            imageOverlayLabel="Import",
            annotation="Import mesh(es) from Blender (FBX)",
            image1="polyCube.png",
            sourceType="python",
            command=_reload_and_run_cmd("pull_from_blender"),
        )
        cmds.shelfButton(
            parent=shelf_name,
            label="Folder",
            imageOverlayLabel="Folder",
            annotation="Open Shared Exchange Folder",
            image1="fileOpen.png",
            sourceType="python",
            command=_reload_and_run_cmd("open_bridge_folder"),
        )
    except Exception as e:
        core.log_event("maya", "ENSURE_SHELF", "error", error=str(e))


def uninstall():
    """
    Cleanly stops the SiSync server, unregisters timers, removes the SiSyncWindow UI,
    and deletes the SiSync shelf without leaving ghost callbacks or duplicate shelves.
    """
    stop_server()
    try:
        if cmds.window("SiSyncWindow", exists=True):
            cmds.deleteUI("SiSyncWindow")
    except Exception:
        pass
    try:
        if not cmds.about(batch=True) and cmds.shelfLayout("SiSync", exists=True):
            cmds.deleteUI("SiSync", layout=True)
    except Exception:
        pass
    core.log_event("maya", "UNINSTALL", "success")


def initialize(auto_start_server: bool = True):
    """Initializes SiSync in Maya: opens commandPort :19852 & :7001, starts BridgeServer on 19850, and builds shelf."""
    for p in (f":{MAYA_LEGACY_MCP_PORT}", MAYA_CMD_PORT):
        try:
            if not cmds.commandPort(p, query=True):
                cmds.commandPort(name=p, sourceType="python")
        except Exception:
            pass

    set_pref("sisync_up_axis", "Y")
    if auto_start_server:
        start_server(MAYA_PORT)

    if not cmds.about(batch=True):
        _ensure_shelf()
