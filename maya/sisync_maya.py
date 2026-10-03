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
from typing import List, Optional, Dict, Any, Tuple

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

# Ensure canonical sisync_bridge_core and sisync_maya are always resolved from _THIS_DIR first
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_THIS_DIR)
for _bin_cand in ("", os.path.dirname(sys.executable)):
    while _bin_cand in sys.path:
        sys.path.remove(_bin_cand)
for _p in (_ROOT_DIR, _THIS_DIR):
    while _p in sys.path:
        sys.path.remove(_p)
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
            prev_timer = getattr(sys, "_sisync_qt_watch_timer", None) or _QT_WATCH_TIMER
            if prev_timer is not None:
                try:
                    prev_timer.stop()
                    prev_timer.deleteLater()
                except Exception:
                    pass
            _QT_WATCH_TIMER = None
            sys._sisync_qt_watch_timer = None
            app = QtCore.QCoreApplication.instance()
            if app:
                for ch_obj in list(app.children()):
                    if type(ch_obj).__name__ == "QTimer":
                        try:
                            ch_obj.stop()
                            ch_obj.deleteLater()
                        except Exception:
                            pass
                _QT_WATCH_TIMER = QtCore.QTimer(app)
                _QT_WATCH_TIMER.setObjectName("SiSyncAutoWatchTimer")
                _QT_WATCH_TIMER.setInterval(350)
                _QT_WATCH_TIMER.timeout.connect(_auto_watch_tick)
                _QT_WATCH_TIMER.start()
                sys._sisync_qt_watch_timer = _QT_WATCH_TIMER
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


# ---------------------------------------------------------------------------
# Phase 2: Canonical Vertex Color Set & Material / PBR / Texture / UV Layer
# ---------------------------------------------------------------------------
def _get_or_set_maya_material_id(shader_node: str, name_hint: str = "", force_id: str = "") -> str:
    try:
        if not cmds.attributeQuery("sisync_material_id", node=shader_node, exists=True):
            cmds.addAttr(shader_node, longName="sisync_material_id", dataType="string")
        curr = cmds.getAttr(f"{shader_node}.sisync_material_id") or ""
        if force_id:
            cmds.setAttr(f"{shader_node}.sisync_material_id", str(force_id), type="string")
            return str(force_id)
        if not curr:
            curr = core.generate_material_id(name_hint or shader_node.split("|")[-1].split(":")[-1])
            cmds.setAttr(f"{shader_node}.sisync_material_id", str(curr), type="string")
        return str(curr)
    except Exception:
        return force_id or core.generate_material_id(name_hint or shader_node)


def _find_or_create_maya_shader(mat_name: str, mat_id: str) -> Tuple[str, str]:
    """
    Finds an existing Maya shader by stable sisync_material_id or name, or creates a standardSurface shader
    and its connected shadingEngine. Returns (shader_node, shading_engine_node).
    """
    safe_name = _sanitize_name(mat_name) if mat_name else "SiSync_Mat"
    all_mats = set(cmds.ls(materials=True) or [])
    if cmds.objExists(safe_name) and safe_name not in all_mats:
        shader_target_name = f"{safe_name}_Mat"
    else:
        shader_target_name = safe_name

    found_shader = ""

    # 1. Match by stable sisync_material_id across all materials
    if mat_id:
        for m in all_mats:
            if cmds.attributeQuery("sisync_material_id", node=m, exists=True):
                try:
                    if str(cmds.getAttr(f"{m}.sisync_material_id") or "") == str(mat_id):
                        found_shader = m
                        break
                except Exception:
                    pass

    # 2. Match by exact or sanitized name
    if not found_shader and safe_name:
        if shader_target_name in all_mats:
            found_shader = shader_target_name
        elif safe_name in all_mats:
            found_shader = safe_name
        else:
            for m in all_mats:
                if _sanitize_name(m) in (safe_name, shader_target_name) or re.match(rf"^{re.escape(safe_name)}\d+$", m):
                    found_shader = m
                    break

    # Clean up any extra FBX-created duplicate shaders (including standardSurface) for this material
    for m in list(cmds.ls(materials=True) or []):
        if m != found_shader and m not in ("lambert1", "standardSurface1", "particleCloud1"):
            if re.match(rf"^({re.escape(safe_name)}|{re.escape(shader_target_name)})\d+$", m):
                for sg_dup in (cmds.listConnections(m, type="shadingEngine") or []):
                    try:
                        cmds.delete(sg_dup)
                    except Exception:
                        pass
                try:
                    cmds.delete(m)
                except Exception:
                    pass

    if found_shader:
        if cmds.nodeType(found_shader) != "standardSurface" and found_shader not in ("lambert1", "standardSurface1", "particleCloud1"):
            old_sgs = cmds.listConnections(found_shader, type="shadingEngine") or []
            try:
                cmds.delete(found_shader)
                found_shader = cmds.shadingNode("standardSurface", asShader=True, name=shader_target_name)
                if old_sgs:
                    cmds.connectAttr(f"{found_shader}.outColor", f"{old_sgs[0]}.surfaceShader", force=True)
            except Exception:
                pass

        if (
            shader_target_name
            and found_shader != shader_target_name
            and not cmds.objExists(shader_target_name)
            and found_shader not in ("lambert1", "standardSurface1", "particleCloud1")
        ):
            try:
                found_shader = cmds.rename(found_shader, shader_target_name)
            except Exception:
                pass
        _get_or_set_maya_material_id(found_shader, safe_name, force_id=mat_id)
        if mat_name:
            if not cmds.attributeQuery("sisync_original_mat_name", node=found_shader, exists=True):
                try:
                    cmds.addAttr(found_shader, longName="sisync_original_mat_name", dataType="string")
                except Exception:
                    pass
            try:
                cmds.setAttr(f"{found_shader}.sisync_original_mat_name", str(mat_name), type="string")
            except Exception:
                pass
        sgs = cmds.listConnections(found_shader, type="shadingEngine") or []
        if sgs:
            return found_shader, sgs[0]
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=f"{found_shader}SG")
        if cmds.attributeQuery("outColor", node=found_shader, exists=True):
            cmds.connectAttr(f"{found_shader}.outColor", f"{sg}.surfaceShader", force=True)
        return found_shader, sg

    # 3. Create new standardSurface (or lambert fallback)
    try:
        shader = cmds.shadingNode("standardSurface", asShader=True, name=shader_target_name)
    except Exception:
        shader = cmds.shadingNode("lambert", asShader=True, name=shader_target_name)
    _get_or_set_maya_material_id(shader, safe_name, force_id=mat_id)
    if mat_name:
        if not cmds.attributeQuery("sisync_original_mat_name", node=shader, exists=True):
            try:
                cmds.addAttr(shader, longName="sisync_original_mat_name", dataType="string")
            except Exception:
                pass
        try:
            cmds.setAttr(f"{shader}.sisync_original_mat_name", str(mat_name), type="string")
        except Exception:
            pass
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=f"{shader}SG")
    if cmds.attributeQuery("outColor", node=shader, exists=True):
        cmds.connectAttr(f"{shader}.outColor", f"{sg}.surfaceShader", force=True)
    return shader, sg


def _apply_maya_material_def(shader: str, mdef: Dict[str, Any], ex_dir: str) -> None:
    if not isinstance(mdef, dict) or not cmds.objExists(shader):
        return
    if mdef.get("material_id"):
        _get_or_set_maya_material_id(shader, shader, force_id=str(mdef["material_id"]))

    ntype = cmds.nodeType(shader)
    bc = mdef.get("base_color", [0.8, 0.8, 0.8, 1.0])
    r_c, g_c, b_c = (float(bc[0]), float(bc[1]), float(bc[2])) if isinstance(bc, (list, tuple)) and len(bc) >= 3 else (0.8, 0.8, 0.8)

    if ntype == "standardSurface":
        try:
            if not cmds.listConnections(f"{shader}.baseColor", source=True, destination=False):
                cmds.setAttr(f"{shader}.baseColor", r_c, g_c, b_c, type="double3")
            cmds.setAttr(f"{shader}.base", 1.0)
        except Exception:
            pass
        for attr_key, maya_attr in (
            ("roughness", "specularRoughness"),
            ("metallic", "metalness"),
            ("specular", "specular"),
            ("ior", "specularIOR"),
            ("emission_strength", "emission"),
        ):
            if attr_key in mdef and cmds.attributeQuery(maya_attr, node=shader, exists=True):
                try:
                    if not cmds.listConnections(f"{shader}.{maya_attr}", source=True, destination=False):
                        cmds.setAttr(f"{shader}.{maya_attr}", float(mdef[attr_key]))
                except Exception:
                    pass
        em_c = mdef.get("emission_color")
        if isinstance(em_c, (list, tuple)) and len(em_c) >= 3 and cmds.attributeQuery("emissionColor", node=shader, exists=True):
            try:
                if not cmds.listConnections(f"{shader}.emissionColor", source=True, destination=False):
                    cmds.setAttr(f"{shader}.emissionColor", float(em_c[0]), float(em_c[1]), float(em_c[2]), type="double3")
            except Exception:
                pass
        if "opacity" in mdef and cmds.attributeQuery("opacity", node=shader, exists=True):
            op = float(mdef["opacity"])
            try:
                if not cmds.listConnections(f"{shader}.opacity", source=True, destination=False):
                    cmds.setAttr(f"{shader}.opacity", op, op, op, type="double3")
            except Exception:
                pass
    else:
        if cmds.attributeQuery("color", node=shader, exists=True):
            try:
                if not cmds.listConnections(f"{shader}.color", source=True, destination=False):
                    cmds.setAttr(f"{shader}.color", r_c, g_c, b_c, type="double3")
            except Exception:
                pass

    # Connect / update file texture nodes
    tex_list = mdef.get("textures", [])
    if not isinstance(tex_list, list):
        return
    tex_dir = core.get_textures_dir(ex_dir)
    for tinfo in tex_list:
        if not isinstance(tinfo, dict):
            continue
        ch = str(tinfo.get("channel") or "base_color").lower()
        raw_p = str(tinfo.get("path") or tinfo.get("original_path") or "").replace("\\", "/")
        if not raw_p:
            continue
        cand_p = raw_p
        if not os.path.exists(cand_p):
            alt_p = os.path.join(tex_dir, os.path.basename(raw_p)).replace("\\", "/")
            if os.path.exists(alt_p):
                cand_p = alt_p

        fnode_name = f"{_sanitize_name(shader)}_{ch}_file"
        if cmds.objExists(fnode_name) and cmds.nodeType(fnode_name) == "file":
            fnode = fnode_name
        else:
            fnode = cmds.shadingNode("file", asTexture=True, isColorManaged=True, name=fnode_name)

        try:
            cmds.setAttr(f"{fnode}.fileTextureName", cand_p, type="string")
        except Exception:
            pass

        cspace = str(tinfo.get("colorspace") or ("sRGB" if ch in ("base_color", "emission") else "Raw"))
        maya_cs = "sRGB" if "srgb" in cspace.lower() else "Raw"
        if cmds.attributeQuery("colorSpace", node=fnode, exists=True):
            try:
                cmds.setAttr(f"{fnode}.colorSpace", maya_cs, type="string")
            except Exception:
                pass

        for custom_attr, val_str in (
            ("sisync_channel", ch),
            ("sisync_uv_set", str(tinfo.get("uv_set") or "UVMap")),
            ("sisync_colorspace", cspace),
        ):
            try:
                if not cmds.attributeQuery(custom_attr, node=fnode, exists=True):
                    cmds.addAttr(fnode, longName=custom_attr, dataType="string")
                cmds.setAttr(f"{fnode}.{custom_attr}", val_str, type="string")
            except Exception:
                pass

        # Connect file node to appropriate shader plug
        try:
            if ntype == "standardSurface":
                if ch == "base_color":
                    cmds.connectAttr(f"{fnode}.outColor", f"{shader}.baseColor", force=True)
                elif ch == "roughness":
                    cmds.connectAttr(f"{fnode}.outAlpha", f"{shader}.specularRoughness", force=True)
                elif ch == "metallic":
                    cmds.connectAttr(f"{fnode}.outAlpha", f"{shader}.metalness", force=True)
                elif ch == "specular":
                    cmds.connectAttr(f"{fnode}.outAlpha", f"{shader}.specular", force=True)
                elif ch == "emission":
                    cmds.connectAttr(f"{fnode}.outColor", f"{shader}.emissionColor", force=True)
                elif ch == "opacity":
                    cmds.connectAttr(f"{fnode}.outColor", f"{shader}.opacity", force=True)
                elif ch in ("normal", "bump"):
                    b2d_name = f"{_sanitize_name(shader)}_{ch}_bump2d"
                    if cmds.objExists(b2d_name) and cmds.nodeType(b2d_name) == "bump2d":
                        b2d = b2d_name
                    else:
                        b2d = cmds.shadingNode("bump2d", asUtility=True, name=b2d_name)
                    cmds.setAttr(f"{b2d}.bumpInterp", 1 if ch == "normal" else 0)
                    cmds.connectAttr(f"{fnode}.outAlpha", f"{b2d}.bumpValue", force=True)
                    cmds.connectAttr(f"{b2d}.outNormal", f"{shader}.normalCamera", force=True)
            else:
                if ch == "base_color" and cmds.attributeQuery("color", node=shader, exists=True):
                    cmds.connectAttr(f"{fnode}.outColor", f"{shader}.color", force=True)
        except Exception:
            pass


def _extract_maya_material_def(shader: str, uv_sets: List[str], ex_dir: str) -> Dict[str, Any]:
    mat_id = _get_or_set_maya_material_id(shader, shader)
    orig_m_name = _sanitize_name(shader)
    if cmds.attributeQuery("sisync_original_mat_name", node=shader, exists=True):
        try:
            val_m = str(cmds.getAttr(f"{shader}.sisync_original_mat_name") or "")
            if val_m:
                orig_m_name = val_m
        except Exception:
            pass
    mdef: Dict[str, Any] = {
        "material_id": mat_id,
        "name": orig_m_name,
        "base_color": [0.8, 0.8, 0.8, 1.0],
        "roughness": 0.5,
        "metallic": 0.0,
        "specular": 0.5,
        "ior": 1.45,
        "emission_color": [0.0, 0.0, 0.0, 1.0],
        "emission_strength": 0.0,
        "opacity": 1.0,
        "normal_strength": 1.0,
        "bump_strength": 0.5,
        "uv_sets": list(uv_sets),
        "textures": [],
    }
    if not cmds.objExists(shader):
        return mdef

    for c_attr in ("baseColor", "color"):
        if cmds.attributeQuery(c_attr, node=shader, exists=True):
            try:
                rgb = cmds.getAttr(f"{shader}.{c_attr}")[0]
                mdef["base_color"] = [round(float(rgb[0]), 6), round(float(rgb[1]), 6), round(float(rgb[2]), 6), 1.0]
                break
            except Exception:
                pass

    for key, m_attr in (
        ("roughness", "specularRoughness"),
        ("metallic", "metalness"),
        ("specular", "specular"),
        ("ior", "specularIOR"),
        ("emission_strength", "emission"),
    ):
        if cmds.attributeQuery(m_attr, node=shader, exists=True):
            try:
                mdef[key] = round(float(cmds.getAttr(f"{shader}.{m_attr}")), 6)
            except Exception:
                pass

    if cmds.attributeQuery("emissionColor", node=shader, exists=True):
        try:
            ergb = cmds.getAttr(f"{shader}.emissionColor")[0]
            mdef["emission_color"] = [round(float(ergb[0]), 6), round(float(ergb[1]), 6), round(float(ergb[2]), 6), 1.0]
        except Exception:
            pass

    if cmds.attributeQuery("opacity", node=shader, exists=True):
        try:
            op_rgb = cmds.getAttr(f"{shader}.opacity")[0]
            mdef["opacity"] = round(float(op_rgb[0]), 6)
        except Exception:
            pass

    # Extract connected file texture nodes
    hist = cmds.listHistory(shader) or []
    file_nodes = cmds.ls(hist, type="file") or []
    seen_tex = set()
    for fn in file_nodes:
        try:
            raw_p = str(cmds.getAttr(f"{fn}.fileTextureName") or "").replace("\\", "/")
            staged_p = core.stage_texture_to_bridge(raw_p, ex_dir) if raw_p else ""
            ch = "base_color"
            if cmds.attributeQuery("sisync_channel", node=fn, exists=True):
                ch = str(cmds.getAttr(f"{fn}.sisync_channel") or "base_color")
            else:
                conns = cmds.listConnections(fn, plugs=True, source=False, destination=True) or []
                for c_plug in conns:
                    p_low = c_plug.lower()
                    if "roughness" in p_low:
                        ch = "roughness"
                    elif "metalness" in p_low or "metallic" in p_low:
                        ch = "metallic"
                    elif "specular" in p_low:
                        ch = "specular"
                    elif "emission" in p_low:
                        ch = "emission"
                    elif "opacity" in p_low or "transparency" in p_low:
                        ch = "opacity"
                    elif "bump" in p_low or "normal" in p_low:
                        bnode = c_plug.split(".")[0]
                        if cmds.nodeType(bnode) == "bump2d" and int(cmds.getAttr(f"{bnode}.bumpInterp") or 0) == 1:
                            ch = "normal"
                        else:
                            ch = "bump"

            uv_set_nm = uv_sets[0] if uv_sets else "UVMap"
            if cmds.attributeQuery("sisync_uv_set", node=fn, exists=True):
                uv_set_nm = str(cmds.getAttr(f"{fn}.sisync_uv_set") or uv_set_nm)

            cspace = "sRGB" if ch in ("base_color", "emission") else "Non-Color"
            if cmds.attributeQuery("sisync_colorspace", node=fn, exists=True):
                cspace = str(cmds.getAttr(f"{fn}.sisync_colorspace") or cspace)
            elif cmds.attributeQuery("colorSpace", node=fn, exists=True):
                mcs = str(cmds.getAttr(f"{fn}.colorSpace") or "")
                cspace = "sRGB" if "srgb" in mcs.lower() else "Non-Color"

            key = (ch, staged_p or fn)
            if key in seen_tex:
                continue
            seen_tex.add(key)
            mdef["textures"].append({
                "channel": ch,
                "image_name": os.path.basename(staged_p or raw_p) or fn,
                "path": staged_p or raw_p,
                "original_path": raw_p,
                "colorspace": cspace,
                "uv_set": uv_set_nm,
            })
        except Exception:
            pass
    return mdef


def _build_maya_index_map(curr_pts: List[List[float]], saved_pts: List[List[float]]) -> List[int]:
    """
    Maps each index in curr_pts to the matching index in saved_pts.
    Fast O(N) identity check when order is unchanged; falls back to spatial hash / nearest search if reordered or remeshed.
    """
    n_curr = len(curr_pts)
    n_saved = len(saved_pts)
    if n_curr == 0 or n_saved == 0:
        return list(range(n_curr))

    if n_curr == n_saved:
        same = True
        for i in range(n_curr):
            dx = curr_pts[i][0] - saved_pts[i][0]
            dy = curr_pts[i][1] - saved_pts[i][1]
            dz = curr_pts[i][2] - saved_pts[i][2]
            if (dx * dx + dy * dy + dz * dz) > 1e-2:
                same = False
                break
        if same:
            return list(range(n_curr))

    # Build exact/rounded spatial bucket dictionary first for fast exact coordinate matches
    exact_buckets: Dict[Tuple[int, int, int], List[int]] = {}
    for s_idx, pt in enumerate(saved_pts):
        key = (int(round(pt[0] * 100.0)), int(round(pt[1] * 100.0)), int(round(pt[2] * 100.0)))
        exact_buckets.setdefault(key, []).append(s_idx)

    out_map: List[int] = [0] * n_curr
    for c_idx, cpt in enumerate(curr_pts):
        key = (int(round(cpt[0] * 100.0)), int(round(cpt[1] * 100.0)), int(round(cpt[2] * 100.0)))
        cands = exact_buckets.get(key)
        if not cands:
            # Check immediate 26 neighbor buckets
            cands = []
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    for oz in (-1, 0, 1):
                        nb = exact_buckets.get((key[0] + ox, key[1] + oy, key[2] + oz))
                        if nb:
                            cands.extend(nb)
        if cands:
            best_i = cands[0]
            best_d = 1e30
            for s_i in cands:
                spt = saved_pts[s_i]
                d = (cpt[0] - spt[0]) ** 2 + (cpt[1] - spt[1]) ** 2 + (cpt[2] - spt[2]) ** 2
                if d < best_d:
                    best_d = d
                    best_i = s_i
            out_map[c_idx] = best_i
        else:
            # Global nearest fallback (for remeshed topology)
            best_i = 0
            best_d = 1e30
            cx, cy, cz = cpt[0], cpt[1], cpt[2]
            for s_i, spt in enumerate(saved_pts):
                d = (cx - spt[0]) ** 2 + (cy - spt[1]) ** 2 + (cz - spt[2]) ** 2
                if d < best_d:
                    best_d = d
                    best_i = s_i
            out_map[c_idx] = best_i
    return out_map


def get_maya_transfer_flags(ex_dir: Optional[str] = None) -> Dict[str, bool]:
    eff_dir = ex_dir or get_exchange_dir()
    disk_toggles = core.read_transfer_toggles(eff_dir)
    if isinstance(disk_toggles, dict) and "raw_all" in disk_toggles:
        all_on = bool(disk_toggles.get("raw_all", False))
        hier_on = bool(disk_toggles.get("raw_hierarchy", False))
        vcol_on = bool(disk_toggles.get("raw_vertex_color", True))
        bs_on = bool(disk_toggles.get("raw_blendshapes", False))
        set_pref("sisync_sync_all", all_on)
        set_pref("sisync_sync_hierarchy", hier_on)
        set_pref("sisync_sync_vertex_color", vcol_on)
        set_pref("sisync_sync_blendshapes", bs_on)
    else:
        all_on = bool(get_pref("sisync_sync_all", False))
        hier_on = bool(get_pref("sisync_sync_hierarchy", False))
        vcol_on = bool(get_pref("sisync_sync_vertex_color", True))
        bs_on = bool(get_pref("sisync_sync_blendshapes", False))
    return core.resolve_transfer_flags(all_on, hier_on, vcol_on, blendshapes_enabled=bs_on)


def set_transfer_toggles(
    all_enabled: Optional[bool] = None,
    hierarchy_enabled: Optional[bool] = None,
    vertex_color_enabled: Optional[bool] = None,
    blendshapes_enabled: Optional[bool] = None,
) -> Dict[str, bool]:
    curr_all = bool(get_pref("sisync_sync_all", False)) if all_enabled is None else bool(all_enabled)
    curr_hier = bool(get_pref("sisync_sync_hierarchy", False)) if hierarchy_enabled is None else bool(hierarchy_enabled)
    curr_vcol = bool(get_pref("sisync_sync_vertex_color", True)) if vertex_color_enabled is None else bool(vertex_color_enabled)
    curr_bs = bool(get_pref("sisync_sync_blendshapes", False)) if blendshapes_enabled is None else bool(blendshapes_enabled)
    set_pref("sisync_sync_all", curr_all)
    set_pref("sisync_sync_hierarchy", curr_hier)
    set_pref("sisync_sync_vertex_color", curr_vcol)
    set_pref("sisync_sync_blendshapes", curr_bs)
    return core.write_transfer_toggles(
        curr_all,
        curr_hier,
        curr_vcol,
        custom_dir=get_exchange_dir(),
        blendshapes_enabled=curr_bs,
    )


def _is_maya_exportable_hierarchy_transform(t_long: str) -> Tuple[bool, str]:
    """
    Returns (is_valid_hierarchy_node, node_type) where node_type is 'MESH' or 'EMPTY'.
    Excludes default cameras, lights, joints, constraints, and temporary __sisync nodes.
    """
    short_nm = t_long.split("|")[-1].split(":")[-1]
    if short_nm in ("persp", "top", "front", "side") or "__sisync" in short_nm or short_nm.startswith("WGT_"):
        return False, ""
    if cmds.nodeType(t_long) != "transform":
        return False, ""
    shapes = cmds.listRelatives(t_long, shapes=True, fullPath=True, noIntermediate=True) or []
    if not shapes:
        return True, "EMPTY"
    if any(cmds.nodeType(s) == "mesh" for s in shapes):
        return True, "MESH"
    return False, ""


def _collect_maya_hierarchy_data(
    seed_nodes: List[str],
    up_axis_mode: str = "Y",
    manual_scale: float = 1.0,
    flip_x: bool = False,
    flip_y: bool = False,
    flip_z: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Given selected/seed Maya nodes, climbs to their root assembly transform(s) and traverses
    the entire hierarchy top-down (depth 0 -> leaves).
    Includes both Empty/Group transform nodes (even with no mesh children) and Mesh transforms.
    """
    roots: List[str] = []
    seen_roots = set()
    for n in seed_nodes:
        if not cmds.objExists(n):
            continue
        long_n = (cmds.ls(n, long=True) or [n])[0]
        if cmds.nodeType(long_n) == "mesh":
            parents = cmds.listRelatives(long_n, parent=True, fullPath=True) or []
            if parents:
                long_n = parents[0]
        curr = long_n
        while True:
            parents = cmds.listRelatives(curr, parent=True, fullPath=True) or []
            if not parents:
                break
            ok, _ = _is_maya_exportable_hierarchy_transform(parents[0])
            if not ok:
                break
            curr = parents[0]
        ok, _ = _is_maya_exportable_hierarchy_transform(curr)
        if ok and curr not in seen_roots:
            seen_roots.add(curr)
            roots.append(curr)

    if not roots:
        for asm in (cmds.ls(assemblies=True, long=True) or []):
            ok, _ = _is_maya_exportable_hierarchy_transform(asm)
            if ok and asm not in seen_roots:
                seen_roots.add(asm)
                roots.append(asm)

    ordered_nodes: List[Tuple[str, str, int]] = []
    visited = set()

    def _walk(t_long: str, depth: int):
        if t_long in visited:
            return
        ok, ntype = _is_maya_exportable_hierarchy_transform(t_long)
        if not ok:
            return
        visited.add(t_long)
        ordered_nodes.append((t_long, ntype, depth))
        child_transforms = cmds.listRelatives(t_long, children=True, type="transform", fullPath=True) or []
        for ch in sorted(child_transforms, key=lambda x: x.split("|")[-1]):
            _walk(ch, depth + 1)

    for r in roots:
        _walk(r, 0)

    visited_set = {t for t, _, _ in ordered_nodes}
    hierarchy_entries: List[Dict[str, Any]] = []
    mesh_transforms: List[str] = []

    for t_long, ntype, depth in ordered_nodes:
        short_nm = _sanitize_name(t_long.split("|")[-1].split(":")[-1])
        b_id = _get_or_set_maya_bridge_id(t_long, short_nm)
        orig_bl_name = short_nm
        if cmds.attributeQuery("sisync_original_name", node=t_long, exists=True):
            try:
                val_orig = str(cmds.getAttr(f"{t_long}.sisync_original_name") or "")
                if val_orig:
                    orig_bl_name = val_orig
            except Exception:
                pass

        parents = cmds.listRelatives(t_long, parent=True, fullPath=True) or []
        parent_long = parents[0] if (parents and parents[0] in visited_set) else ""
        parent_short = _sanitize_name(parent_long.split("|")[-1].split(":")[-1]) if parent_long else ""
        parent_bid = _get_or_set_maya_bridge_id(parent_long, parent_short) if parent_long else ""
        parent_orig_name = parent_short
        if parent_long and cmds.attributeQuery("sisync_original_name", node=parent_long, exists=True):
            try:
                p_orig = str(cmds.getAttr(f"{parent_long}.sisync_original_name") or "")
                if p_orig:
                    parent_orig_name = p_orig
            except Exception:
                pass

        w_piv = cmds.xform(t_long, query=True, worldSpace=True, rotatePivot=True) or [0.0, 0.0, 0.0]
        w_rot = cmds.xform(t_long, query=True, worldSpace=True, rotation=True) or [0.0, 0.0, 0.0]
        l_pos = cmds.xform(t_long, query=True, objectSpace=True, translation=True) or [0.0, 0.0, 0.0]
        l_rot = cmds.xform(t_long, query=True, objectSpace=True, rotation=True) or [0.0, 0.0, 0.0]
        l_scl = cmds.xform(t_long, query=True, relative=True, scale=True) or [1.0, 1.0, 1.0]

        bl_w_loc = core.CoordinateBasis.maya_point_to_blender(
            (float(w_piv[0]), float(w_piv[1]), float(w_piv[2])),
            up_axis=up_axis_mode,
            unit_scale=0.01 * manual_scale,
            flip_x=flip_x,
            flip_y=flip_y,
            flip_z=flip_z,
        )
        bl_w_rot = core.CoordinateBasis.maya_rot_to_blender(
            (float(w_rot[0]), float(w_rot[1]), float(w_rot[2])),
            up_axis=up_axis_mode,
        )

        hierarchy_entries.append({
            "name": orig_bl_name,
            "original_blender_name": orig_bl_name,
            "sanitized_name": short_nm,
            "node_type": ntype,
            "bridge_id": str(b_id),
            "parent_name": parent_orig_name,
            "parent_sanitized_name": parent_short,
            "parent_bridge_id": str(parent_bid),
            "depth": int(depth),
            "world_location_maya_cm": [round(float(w_piv[0]), 5), round(float(w_piv[1]), 5), round(float(w_piv[2]), 5)],
            "world_rotation_maya_deg": [round(float(w_rot[0]), 5), round(float(w_rot[1]), 5), round(float(w_rot[2]), 5)],
            "world_location_blender_m": [round(float(bl_w_loc[0]), 6), round(float(bl_w_loc[1]), 6), round(float(bl_w_loc[2]), 6)],
            "world_rotation_blender_deg": [round(float(bl_w_rot[0]), 6), round(float(bl_w_rot[1]), 6), round(float(bl_w_rot[2]), 6)],
            "world_scale": [round(float(l_scl[0]), 6), round(float(l_scl[1]), 6), round(float(l_scl[2]), 6)],
            "local_location_maya_cm": [round(float(l_pos[0]), 5), round(float(l_pos[1]), 5), round(float(l_pos[2]), 5)],
            "local_rotation_maya_deg": [round(float(l_rot[0]), 5), round(float(l_rot[1]), 5), round(float(l_rot[2]), 5)],
            "local_scale": [round(float(l_scl[0]), 6), round(float(l_scl[1]), 6), round(float(l_scl[2]), 6)],
        })
        if ntype == "MESH":
            mesh_transforms.append(t_long)

    return hierarchy_entries, mesh_transforms


def _apply_maya_hierarchy(
    hierarchy_list: List[Dict[str, Any]],
    up_axis_mode: str = "Y",
    imported_world_pts_map: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Reconstructs or updates the transform/group hierarchy in Maya top-down (depth 0 -> leaves).
    Matches nodes by sisync_bridge_id first, then by sanitized name, preventing duplicate groups.
    Preserves world and parent-relative local transforms without baking parent transforms into child meshes.
    """
    if not isinstance(hierarchy_list, list) or not hierarchy_list:
        return []

    sorted_entries = sorted(hierarchy_list, key=lambda x: int(x.get("depth", 0)))
    resolved_by_bid: Dict[str, str] = {}
    resolved_by_name: Dict[str, str] = {}
    touched_nodes: List[str] = []

    for h in sorted_entries:
        if not isinstance(h, dict):
            continue
        bid = str(h.get("bridge_id") or "")
        orig_name = str(h.get("original_blender_name") or h.get("name") or "")
        san_name = str(h.get("sanitized_name") or _sanitize_name(orig_name))
        if orig_name.endswith(".003") and not san_name.endswith("_003"):
            san_name = f"{san_name}_003"
        ntype = str(h.get("node_type") or "EMPTY").upper()

        m_node = ""
        all_t = cmds.ls(type="transform", long=True) or []
        if bid:
            for cand in all_t:
                if cmds.attributeQuery("sisync_bridge_id", node=cand, exists=True):
                    try:
                        if str(cmds.getAttr(f"{cand}.sisync_bridge_id") or "") == bid:
                            m_node = cand
                            break
                    except Exception:
                        pass
        if not m_node and san_name:
            for cand in all_t:
                c_short = _sanitize_name(cand.split("|")[-1].split(":")[-1])
                if c_short == san_name:
                    has_mesh = bool(cmds.listRelatives(cand, shapes=True, type="mesh"))
                    if (ntype == "MESH" and has_mesh) or (ntype == "EMPTY" and not has_mesh):
                        m_node = cand
                        break

        if not m_node and ntype == "EMPTY":
            m_node = cmds.group(empty=True, name=san_name)
            m_node = (cmds.ls(m_node, long=True) or [m_node])[0]

        if not m_node or not cmds.objExists(m_node):
            continue

        _get_or_set_maya_bridge_id(m_node, san_name, force_id=bid)
        if orig_name:
            if not cmds.attributeQuery("sisync_original_name", node=m_node, exists=True):
                try:
                    cmds.addAttr(m_node, longName="sisync_original_name", dataType="string")
                except Exception:
                    pass
            try:
                cmds.setAttr(f"{m_node}.sisync_original_name", orig_name, type="string")
            except Exception:
                pass

        # Resolve parent transform in Maya
        p_bid = str(h.get("parent_bridge_id") or "")
        p_san = str(h.get("parent_sanitized_name") or _sanitize_name(str(h.get("parent_name") or "")))
        parent_node = ""
        if p_bid and p_bid in resolved_by_bid and cmds.objExists(resolved_by_bid[p_bid]):
            parent_node = resolved_by_bid[p_bid]
        elif p_san and p_san in resolved_by_name and cmds.objExists(resolved_by_name[p_san]):
            parent_node = resolved_by_name[p_san]
        elif p_san and cmds.objExists(p_san):
            parent_node = (cmds.ls(p_san, long=True) or [p_san])[0]

        # Prefer exact world-space points captured from imp_obj at world root so existing parented meshes never shift or scale
        saved_world_pts = None
        if isinstance(imported_world_pts_map, dict) and san_name in imported_world_pts_map:
            saved_world_pts = imported_world_pts_map[san_name]
        elif ntype == "MESH" and HAS_OPENMAYA:
            try:
                s_l = om.MSelectionList()
                s_l.add(m_node)
                d_p = s_l.getDagPath(0)
                d_p.extendToShape()
                saved_world_pts = om.MFnMesh(d_p).getPoints(om.MSpace.kWorld)
            except Exception:
                saved_world_pts = None

        curr_parents = cmds.listRelatives(m_node, parent=True, fullPath=True) or []
        if parent_node and cmds.objExists(parent_node):
            parent_long = (cmds.ls(parent_node, long=True) or [parent_node])[0]
            if not curr_parents or curr_parents[0] != parent_long:
                try:
                    m_node = cmds.parent(m_node, parent_long)[0]
                except Exception:
                    pass
        else:
            if curr_parents:
                try:
                    m_node = cmds.parent(m_node, world=True)[0]
                except Exception:
                    pass

        m_node = (cmds.ls(m_node, long=True) or [m_node])[0]
        curr_short = m_node.split("|")[-1].split(":")[-1]
        if curr_short != san_name:
            try:
                m_node = cmds.rename(m_node, san_name)
                m_node = (cmds.ls(m_node, long=True) or [m_node])[0]
            except Exception:
                pass

        # Apply exact Maya world transform
        w_pos = h.get("world_location_maya_cm")
        if not isinstance(w_pos, (list, tuple)) and isinstance(h.get("world_location_blender_m"), (list, tuple)):
            bp = h["world_location_blender_m"]
            w_pos = list(core.CoordinateBasis.blender_point_to_maya((float(bp[0]), float(bp[1]), float(bp[2])), up_axis=up_axis_mode))
        if not isinstance(w_pos, (list, tuple)):
            w_pos = [0.0, 0.0, 0.0]

        w_rot = h.get("world_rotation_maya_deg")
        if not isinstance(w_rot, (list, tuple)) and isinstance(h.get("world_rotation_blender_deg"), (list, tuple)):
            br = h["world_rotation_blender_deg"]
            w_rot = list(core.CoordinateBasis.blender_rot_to_maya((float(br[0]), float(br[1]), float(br[2])), up_axis=up_axis_mode))
        if not isinstance(w_rot, (list, tuple)):
            w_rot = [0.0, 0.0, 0.0]

        l_scl = list(h.get("local_scale") or h.get("world_scale") or [1.0, 1.0, 1.0])
        if (
            abs(float(l_scl[0]) - 0.01) < 1e-3
            and abs(float(l_scl[1]) - 0.01) < 1e-3
            and abs(float(l_scl[2]) - 0.01) < 1e-3
        ):
            l_scl = [1.0, 1.0, 1.0]
            if abs(float(w_rot[0]) - 90.0) < 0.5 and abs(float(w_rot[1])) < 0.5 and abs(float(w_rot[2])) < 0.5:
                w_rot = [0.0, 0.0, 0.0]

        try:
            cmds.xform(m_node, objectSpace=True, pivots=(0.0, 0.0, 0.0))
            cmds.xform(
                m_node,
                worldSpace=True,
                translation=(float(w_pos[0]), float(w_pos[1]), float(w_pos[2])),
                rotation=(float(w_rot[0]), float(w_rot[1]), float(w_rot[2])),
            )
            cmds.xform(
                m_node,
                objectSpace=True,
                scale=(float(l_scl[0]), float(l_scl[1]), float(l_scl[2])),
            )
        except Exception:
            pass

        if saved_world_pts is not None:
            try:
                s_l2 = om.MSelectionList()
                s_l2.add(m_node)
                d_p2 = s_l2.getDagPath(0)
                d_p2.extendToShape()
                fn_m2 = om.MFnMesh(d_p2)
                fn_m2.setPoints(saved_world_pts, om.MSpace.kWorld)
                fn_m2.updateSurface()
            except Exception:
                pass

        if bid:
            resolved_by_bid[bid] = m_node
        resolved_by_name[san_name] = m_node
        if orig_name:
            resolved_by_name[orig_name] = m_node
        touched_nodes.append(m_node)

    return touched_nodes


def _extract_maya_phase2_payload(
    src_obj: str,
    up_axis_mode: str,
    manual_scale: float,
    flip_x: bool,
    flip_y: bool,
    flip_z: bool,
    ex_dir: str,
    vertex_color_enabled: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Extracts Phase 2 Vertex Color Sets (POINT and CORNER domains) and Material / PBR / UV data
    from a Maya mesh transform node.
    Respects vertex_color_enabled: when False, skips extracting color sets while preserving materials/UVs/textures.
    """
    if not HAS_OPENMAYA:
        return {}, {}, {}

    s_list = om.MSelectionList()
    s_list.add(src_obj)
    dag = s_list.getDagPath(0)
    dag.extendToShape()
    fn_mesh = om.MFnMesh(dag)

    pts_world = fn_mesh.getPoints(om.MSpace.kWorld)
    v_maya = []
    v_bl = []
    for p in pts_world:
        mx, my, mz = float(p.x), float(p.y), float(p.z)
        v_maya.append([round(mx, 5), round(my, 5), round(mz, 5)])
        bx, by, bz = core.CoordinateBasis.maya_point_to_blender(
            (mx, my, mz),
            up_axis=up_axis_mode,
            unit_scale=0.01 * manual_scale,
            flip_x=flip_x,
            flip_y=flip_y,
            flip_z=flip_z,
        )
        v_bl.append([round(float(bx), 6), round(float(by), 6), round(float(bz), 6)])

    p_maya = []
    p_bl = []
    corner_face_ids = []
    corner_vertex_ids = []
    num_polys = fn_mesh.numPolygons
    for p_idx in range(num_polys):
        p_verts = fn_mesh.getPolygonVertices(p_idx)
        cx = cy = cz = 0.0
        nv = len(p_verts)
        for v_idx in p_verts:
            vi = int(v_idx)
            corner_face_ids.append(int(p_idx))
            corner_vertex_ids.append(vi)
            pt = v_maya[vi]
            cx += pt[0]
            cy += pt[1]
            cz += pt[2]
        if nv > 0:
            cx /= nv
            cy /= nv
            cz /= nv
        p_maya.append([round(cx, 5), round(cy, 5), round(cz, 5)])
        bx, by, bz = core.CoordinateBasis.maya_point_to_blender(
            (cx, cy, cz),
            up_axis=up_axis_mode,
            unit_scale=0.01 * manual_scale,
            flip_x=flip_x,
            flip_y=flip_y,
            flip_z=flip_z,
        )
        p_bl.append([round(float(bx), 6), round(float(by), 6), round(float(bz), 6)])

    # Extract Color Sets (only when vertex_color_enabled is True)
    all_cs = (cmds.polyColorSet(src_obj, query=True, allColorSets=True) or []) if vertex_color_enabled else []
    curr_cs_list = (cmds.polyColorSet(src_obj, query=True, currentColorSet=True) or []) if vertex_color_enabled else []
    active_cs = str(curr_cs_list[0]) if curr_cs_list else (str(all_cs[0]) if all_cs else "")

    meta_color_attrs = []
    sidecar_color_attrs = []
    num_verts = fn_mesh.numVertices

    for cs_name in all_cs:
        try:
            fv_colors = fn_mesh.getFaceVertexColors(cs_name)
        except Exception:
            continue
        if len(fv_colors) != len(corner_vertex_ids):
            continue

        safe_cs = re.sub(r"[^a-zA-Z0-9_]", "_", cs_name)
        stored_domain = ""
        if cmds.attributeQuery(f"sisync_cs_domain_{safe_cs}", node=src_obj, exists=True):
            try:
                stored_domain = str(cmds.getAttr(f"{src_obj}.sisync_cs_domain_{safe_cs}") or "").upper()
            except Exception:
                pass
        stored_dtype = "FLOAT_COLOR"
        if cmds.attributeQuery(f"sisync_cs_dtype_{safe_cs}", node=src_obj, exists=True):
            try:
                stored_dtype = str(cmds.getAttr(f"{src_obj}.sisync_cs_dtype_{safe_cs}") or "FLOAT_COLOR").upper()
            except Exception:
                pass

        # Check if any vertex has divergent face-vertex colors across adjacent polygons
        per_vert_color: List[Optional[List[float]]] = [None] * num_verts
        has_corner_divergence = False
        corner_vals: List[List[float]] = []
        for idx_c, mc in enumerate(fv_colors):
            c4 = [round(float(mc.r), 6), round(float(mc.g), 6), round(float(mc.b), 6), round(float(mc.a), 6)]
            corner_vals.append(c4)
            v_i = corner_vertex_ids[idx_c]
            prev = per_vert_color[v_i]
            if prev is None:
                per_vert_color[v_i] = c4
            elif not has_corner_divergence:
                if (
                    abs(prev[0] - c4[0]) > 1e-4
                    or abs(prev[1] - c4[1]) > 1e-4
                    or abs(prev[2] - c4[2]) > 1e-4
                    or abs(prev[3] - c4[3]) > 1e-4
                ):
                    has_corner_divergence = True

        domain = "CORNER" if (has_corner_divergence or stored_domain == "CORNER") else "POINT"
        if domain == "POINT":
            vals = [c if c is not None else [1.0, 1.0, 1.0, 1.0] for c in per_vert_color]
        else:
            vals = corner_vals

        attr_full = {
            "name": str(cs_name),
            "domain": domain,
            "data_type": stored_dtype if stored_dtype in ("FLOAT_COLOR", "BYTE_COLOR") else "FLOAT_COLOR",
            "channels": 4,
            "count": len(vals),
            "values": vals,
            "corner_face_ids": corner_face_ids if domain == "CORNER" else [],
            "corner_vertex_ids": corner_vertex_ids if domain == "CORNER" else [],
        }
        sidecar_color_attrs.append(attr_full)
        meta_entry = {
            "name": str(cs_name),
            "domain": domain,
            "data_type": attr_full["data_type"],
            "channels": 4,
            "count": len(vals),
        }
        if len(vals) <= 4096:
            meta_entry["values"] = vals
        meta_color_attrs.append(meta_entry)

    vertex_data_meta = {
        "vertex_color_enabled": bool(vertex_color_enabled),
        "active_color_attribute": active_cs,
        "sidecar_file": core.get_phase2_sidecar_path(ex_dir),
        "color_attributes": meta_color_attrs,
    }

    # Extract UV sets & Materials
    uv_sets = [str(u) for u in (cmds.polyUVSet(src_obj, query=True, allUVSets=True) or [])]
    curr_uv = cmds.polyUVSet(src_obj, query=True, currentUVSet=True) or []
    active_uv = str(curr_uv[0]) if curr_uv else (uv_sets[0] if uv_sets else "map1")

    slots_list = []
    mat_defs = {}
    face_mat_indices = [0] * num_polys
    try:
        shader_mobjs, face_indices_mint = fn_mesh.getConnectedShaders(0)
        face_mat_indices = [max(0, int(x)) for x in face_indices_mint]
        for s_idx in range(len(shader_mobjs)):
            sg_fn = om.MFnDependencyNode(shader_mobjs[s_idx])
            sg_name = sg_fn.name()
            surf_mats = cmds.listConnections(f"{sg_name}.surfaceShader", source=True, destination=False) or []
            if surf_mats:
                m_node = surf_mats[0]
                m_short = _sanitize_name(m_node)
                if cmds.attributeQuery("sisync_original_mat_name", node=m_node, exists=True):
                    try:
                        val_m = str(cmds.getAttr(f"{m_node}.sisync_original_mat_name") or "")
                        if val_m:
                            m_short = val_m
                    except Exception:
                        pass
                m_id = _get_or_set_maya_material_id(m_node, m_short)
                slots_list.append({
                    "slot_index": int(s_idx),
                    "material_name": m_short,
                    "material_id": m_id,
                })
                mat_defs[m_short] = _extract_maya_material_def(m_node, uv_sets, ex_dir)
            else:
                slots_list.append({
                    "slot_index": int(s_idx),
                    "material_name": "",
                    "material_id": "",
                })
    except Exception:
        pass

    materials_meta = {
        "slots": slots_list,
        "face_material_indices": face_mat_indices if len(face_mat_indices) <= 8192 else [],
        "uv_sets": uv_sets,
        "active_uv_set": active_uv,
        "definitions": mat_defs,
    }

    sidecar_entry = {
        "vertex_positions_blender": v_bl,
        "vertex_positions_maya": v_maya,
        "poly_centroids_blender": p_bl,
        "poly_centroids_maya": p_maya,
        "corner_face_ids": corner_face_ids,
        "corner_vertex_ids": corner_vertex_ids,
        "vertex_data": {
            "active_color_attribute": active_cs,
            "color_attributes": sidecar_color_attrs,
        },
        "materials": {
            "slots": slots_list,
            "face_material_indices": face_mat_indices,
            "uv_sets": uv_sets,
            "active_uv_set": active_uv,
            "definitions": mat_defs,
        },
    }
    return vertex_data_meta, materials_meta, sidecar_entry


def _apply_maya_phase2_payload(
    target_obj: str,
    obj_meta: Dict[str, Any],
    sidecar_entry: Dict[str, Any],
    ex_dir: str,
    vertex_color_enabled: bool = True,
    is_newly_created: bool = False,
) -> None:
    """
    Applies Phase 2 Vertex Color Sets (POINT and CORNER domains) and Material / PBR / UV data
    onto target_obj in Maya.
    Respects vertex_color_enabled: when False, skips applying vertex colors (and strips any
    FBX-created color sets on newly imported meshes) while still transferring materials, UVs, and textures.
    """
    if not HAS_OPENMAYA or not cmds.objExists(target_obj):
        return

    v_data = (sidecar_entry.get("vertex_data") if isinstance(sidecar_entry, dict) else None) or obj_meta.get("vertex_data") or {}
    m_data = (sidecar_entry.get("materials") if isinstance(sidecar_entry, dict) else None) or obj_meta.get("materials") or {}

    saved_v_maya = sidecar_entry.get("vertex_positions_maya", []) if isinstance(sidecar_entry, dict) else []
    saved_p_maya = sidecar_entry.get("poly_centroids_maya", []) if isinstance(sidecar_entry, dict) else []
    corner_f_ids = sidecar_entry.get("corner_face_ids", []) if isinstance(sidecar_entry, dict) else []
    corner_v_ids = sidecar_entry.get("corner_vertex_ids", []) if isinstance(sidecar_entry, dict) else []

    s_list = om.MSelectionList()
    s_list.add(target_obj)
    dag = s_list.getDagPath(0)
    dag.extendToShape()
    fn_mesh = om.MFnMesh(dag)

    vcol_allowed = bool(vertex_color_enabled) and bool(v_data.get("vertex_color_enabled", True))
    color_attrs = (v_data.get("color_attributes", []) if isinstance(v_data, dict) else []) if vcol_allowed else []
    slots_check = m_data.get("slots", []) if isinstance(m_data, dict) else []
    has_multi_slots = len([s for s in slots_check if isinstance(s, dict) and (s.get("material_name") or s.get("material_id"))]) > 1
    needs_spatial_maps = bool(color_attrs) or has_multi_slots

    if needs_spatial_maps:
        pts_world = fn_mesh.getPoints(om.MSpace.kWorld)
        curr_v_maya = [[float(p.x), float(p.y), float(p.z)] for p in pts_world]
        num_polys = fn_mesh.numPolygons
        curr_p_maya = []
        for p_idx in range(num_polys):
            p_verts = fn_mesh.getPolygonVertices(p_idx)
            cx = cy = cz = 0.0
            nv = len(p_verts)
            for v_idx in p_verts:
                pt = curr_v_maya[int(v_idx)]
                cx += pt[0]
                cy += pt[1]
                cz += pt[2]
            if nv > 0:
                cx /= nv
                cy /= nv
                cz /= nv
            curr_p_maya.append([cx, cy, cz])
        v_map = _build_maya_index_map(curr_v_maya, saved_v_maya) if saved_v_maya else list(range(len(curr_v_maya)))
        p_map = _build_maya_index_map(curr_p_maya, saved_p_maya) if saved_p_maya else list(range(len(curr_p_maya)))
    else:
        v_map = []
        p_map = []

    # 1. Apply Vertex Color Sets (POINT and CORNER domains) ONLY when vertex_color_enabled is True
    if not vcol_allowed:
        if is_newly_created:
            for ecs in (cmds.polyColorSet(target_obj, query=True, allColorSets=True) or []):
                try:
                    cmds.polyColorSet(target_obj, delete=True, colorSet=ecs)
                except Exception:
                    pass
    else:
        active_cs_name = str(v_data.get("active_color_attribute") or "")
        if isinstance(color_attrs, list) and color_attrs:
            incoming_names = {str(a.get("name")) for a in color_attrs if isinstance(a, dict) and a.get("name")}
            existing_cs = cmds.polyColorSet(target_obj, query=True, allColorSets=True) or []
            for ecs in existing_cs:
                if ecs not in incoming_names:
                    try:
                        cmds.polyColorSet(target_obj, delete=True, colorSet=ecs)
                    except Exception:
                        pass

            existing_cs = cmds.polyColorSet(target_obj, query=True, allColorSets=True) or []
            for attr in color_attrs:
                if not isinstance(attr, dict):
                    continue
                cs_name = str(attr.get("name") or "Col")
                domain = str(attr.get("domain") or "POINT").upper()
                dtype = str(attr.get("data_type") or "FLOAT_COLOR").upper()
                vals = attr.get("values", [])
                if not isinstance(vals, list) or not vals:
                    continue

                if cs_name not in existing_cs:
                    try:
                        cmds.polyColorSet(target_obj, create=True, colorSet=cs_name, representation="RGBA")
                        existing_cs.append(cs_name)
                    except Exception:
                        pass
                try:
                    cmds.polyColorSet(target_obj, currentColorSet=True, colorSet=cs_name)
                except Exception:
                    pass

                # Stamp domain & data_type attributes on target_obj so round-trip to Blender preserves exact metadata
                safe_cs = re.sub(r"[^a-zA-Z0-9_]", "_", cs_name)
                for attr_nm, attr_val in ((f"sisync_cs_domain_{safe_cs}", domain), (f"sisync_cs_dtype_{safe_cs}", dtype)):
                    try:
                        if not cmds.attributeQuery(attr_nm, node=target_obj, exists=True):
                            cmds.addAttr(target_obj, longName=attr_nm, dataType="string")
                        cmds.setAttr(f"{target_obj}.{attr_nm}", str(attr_val), type="string")
                    except Exception:
                        pass

                # Re-wrap MFnMesh after polyColorSet modifications
                s_list_cs = om.MSelectionList()
                s_list_cs.add(target_obj)
                dag_cs = s_list_cs.getDagPath(0)
                dag_cs.extendToShape()
                fn_cs = om.MFnMesh(dag_cs)
                try:
                    fn_cs.setCurrentColorSetName(cs_name)
                except Exception:
                    pass

                if domain == "POINT":
                    colors = om.MColorArray()
                    v_ids = om.MIntArray()
                    n_vals = len(vals)
                    for t_v in range(fn_cs.numVertices):
                        s_v = v_map[t_v] if t_v < len(v_map) else t_v
                        if 0 <= s_v < n_vals:
                            c = vals[s_v]
                            colors.append(om.MColor((float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) >= 4 else 1.0)))
                            v_ids.append(int(t_v))
                    if len(colors) > 0:
                        try:
                            fn_cs.setVertexColors(colors, v_ids)
                        except Exception as vc_err:
                            core.log_event("maya", "SET_VERTEX_COLORS", "error", error=str(vc_err), extra=cs_name)
                else:
                    # CORNER domain: map (s_p, s_v) -> rgba and call setFaceVertexColors(colors, faceIds, vertexIds)
                    corner_lookup: Dict[Tuple[int, int], List[float]] = {}
                    poly_corners_fallback: Dict[int, List[Tuple[int, List[float]]]] = {}
                    c_f_list = attr.get("corner_face_ids") or corner_f_ids
                    c_v_list = attr.get("corner_vertex_ids") or corner_v_ids
                    if c_f_list and c_v_list and len(c_f_list) == len(vals) and len(c_v_list) == len(vals):
                        for idx_c, rgba in enumerate(vals):
                            sp = int(c_f_list[idx_c])
                            sv = int(c_v_list[idx_c])
                            c4 = [float(rgba[0]), float(rgba[1]), float(rgba[2]), float(rgba[3]) if len(rgba) >= 4 else 1.0]
                            corner_lookup[(sp, sv)] = c4
                            poly_corners_fallback.setdefault(sp, []).append((sv, c4))

                    colors = om.MColorArray()
                    f_ids = om.MIntArray()
                    v_ids = om.MIntArray()
                    flat_idx = 0
                    for t_p in range(fn_cs.numPolygons):
                        s_p = p_map[t_p] if t_p < len(p_map) else t_p
                        p_verts = fn_cs.getPolygonVertices(t_p)
                        for t_v_raw in p_verts:
                            t_v = int(t_v_raw)
                            s_v = v_map[t_v] if t_v < len(v_map) else t_v
                            rgba_val = corner_lookup.get((s_p, s_v))
                            if rgba_val is None and s_p in poly_corners_fallback and saved_v_maya:
                                best_d = 1e30
                                cpt = curr_v_maya[t_v]
                                for cand_sv, cand_c4 in poly_corners_fallback[s_p]:
                                    if 0 <= cand_sv < len(saved_v_maya):
                                        spt = saved_v_maya[cand_sv]
                                        d = (cpt[0] - spt[0]) ** 2 + (cpt[1] - spt[1]) ** 2 + (cpt[2] - spt[2]) ** 2
                                        if d < best_d:
                                            best_d = d
                                            rgba_val = cand_c4
                            if rgba_val is None and flat_idx < len(vals):
                                c = vals[flat_idx]
                                rgba_val = [float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) >= 4 else 1.0]
                            if rgba_val is not None:
                                colors.append(om.MColor((rgba_val[0], rgba_val[1], rgba_val[2], rgba_val[3])))
                                f_ids.append(int(t_p))
                                v_ids.append(int(t_v))
                            flat_idx += 1
                    if len(colors) > 0:
                        try:
                            fn_cs.setFaceVertexColors(colors, f_ids, v_ids)
                        except Exception as fvc_err:
                            core.log_event("maya", "SET_FACE_VERTEX_COLORS", "error", error=str(fvc_err), extra=cs_name)

            if active_cs_name and active_cs_name in (cmds.polyColorSet(target_obj, query=True, allColorSets=True) or []):
                try:
                    cmds.polyColorSet(target_obj, currentColorSet=True, colorSet=active_cs_name)
                except Exception:
                    pass
            shapes = cmds.listRelatives(target_obj, shapes=True, fullPath=True, noIntermediate=True) or []
            for shp in shapes:
                try:
                    cmds.setAttr(f"{shp}.displayColors", 1)
                except Exception:
                    pass

    # 2. Apply UV Set Names
    if isinstance(m_data, dict):
        uv_sets = m_data.get("uv_sets", [])
        if isinstance(uv_sets, list) and uv_sets:
            existing_uvs = cmds.polyUVSet(target_obj, query=True, allUVSets=True) or []
            for idx_u, uv_nm in enumerate(uv_sets):
                if not uv_nm:
                    continue
                if idx_u < len(existing_uvs) and existing_uvs[idx_u] != str(uv_nm):
                    try:
                        cmds.polyUVSet(target_obj, rename=True, uvSet=existing_uvs[idx_u], newUVSet=str(uv_nm))
                        existing_uvs[idx_u] = str(uv_nm)
                    except Exception:
                        pass
                elif str(uv_nm) not in existing_uvs:
                    try:
                        cmds.polyUVSet(target_obj, create=True, uvSet=str(uv_nm))
                        existing_uvs.append(str(uv_nm))
                    except Exception:
                        pass

        # 3. Apply Materials, PBR Definitions, and Per-Face Material Assignments
        slots = m_data.get("slots", [])
        defs = m_data.get("definitions", {}) if isinstance(m_data.get("definitions"), dict) else {}
        face_indices = m_data.get("face_material_indices", [])

        if isinstance(slots, list) and slots:
            slot_sgs: List[Optional[str]] = []
            for s_info in sorted(slots, key=lambda x: int(x.get("slot_index", 0))):
                m_name = str(s_info.get("material_name") or "")
                m_id = str(s_info.get("material_id") or "")
                if not m_name and not m_id:
                    slot_sgs.append(None)
                    continue
                shader_node, sg_node = _find_or_create_maya_shader(m_name, m_id)
                mdef = defs.get(m_name) or next(
                    (d for d in defs.values() if isinstance(d, dict) and str(d.get("material_id", "")) == m_id),
                    None,
                )
                if mdef:
                    _apply_maya_material_def(shader_node, mdef, ex_dir)
                slot_sgs.append(sg_node)

            valid_sgs = [(idx, sg) for idx, sg in enumerate(slot_sgs) if sg]
            if len(valid_sgs) == 1:
                try:
                    cmds.sets(target_obj, edit=True, forceElement=valid_sgs[0][1])
                except Exception:
                    pass
            elif len(valid_sgs) > 1 and isinstance(face_indices, list) and face_indices:
                sg_to_faces: Dict[str, List[str]] = {}
                max_slot = len(slot_sgs) - 1
                n_src_f = len(face_indices)
                for t_p in range(num_polys):
                    s_p = p_map[t_p] if t_p < len(p_map) else t_p
                    slot_i = max(0, min(max_slot, int(face_indices[s_p]))) if 0 <= s_p < n_src_f else 0
                    sg_target = slot_sgs[slot_i] or valid_sgs[0][1]
                    sg_to_faces.setdefault(sg_target, []).append(f"{target_obj}.f[{t_p}]")
                for sg_target, f_comp_list in sg_to_faces.items():
                    if f_comp_list:
                        try:
                            cmds.sets(f_comp_list, edit=True, forceElement=sg_target)
                        except Exception:
                            pass


def send_to_blender(*_) -> Optional[str]:
    """
    Exports selected Maya polygon mesh(es) (and full parent/group hierarchy when Hierarchy=ON)
    to maya_to_blender.fbx (and mirrors to SiSync_Exchange.fbx).
    Guarantees scene restoration (names, temporary duplicates, selection) in a finally block.
    """
    if not ensure_fbx_plugin():
        return None

    ex_dir = get_exchange_dir()
    t_flags = get_maya_transfer_flags(ex_dir)
    hier_enabled = bool(t_flags["hierarchy"])
    vcol_enabled = bool(t_flags["vertex_color"])

    up_axis_mode = str(get_pref("sisync_up_axis", "Y"))
    if up_axis_mode not in ("Y", "Z"):
        up_axis_mode = "Y"
    scale_mode = str(get_pref("sisync_scale_mode", "BUNITS"))
    manual_scale = float(get_pref("sisync_manual_scale", 1.0)) if scale_mode == "MANUAL" else 1.0

    flip_x = bool(get_pref("sisync_flip_x", False))
    flip_y = bool(get_pref("sisync_flip_y", False))
    flip_z = bool(get_pref("sisync_flip_z", False))
    freeze_loc = False if hier_enabled else bool(get_pref("sisync_freeze_loc", False))
    freeze_rot = False if hier_enabled else bool(get_pref("sisync_freeze_rot", True))

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

    hierarchy_list: List[Dict[str, Any]] = []
    if hier_enabled:
        hierarchy_list, hier_meshes = _collect_maya_hierarchy_data(
            seed_nodes=initial_sel or mesh_transforms,
            up_axis_mode=up_axis_mode,
            manual_scale=manual_scale,
            flip_x=flip_x,
            flip_y=flip_y,
            flip_z=flip_z,
        )
        if hier_meshes:
            mesh_transforms = hier_meshes

    if not mesh_transforms and not hierarchy_list:
        cmds.warning("[SiSync Maya] Select at least one polygon mesh or hierarchy group to export.")
        show_hud("Select at least one mesh to export.")
        return None

    m2b_fbx = core.get_maya_to_blender_fbx(ex_dir)
    legacy_fbx = core.get_legacy_exchange_fbx(ex_dir)

    sx = (-1.0 if flip_x else 1.0) * manual_scale
    sy = (-1.0 if flip_y else 1.0) * manual_scale
    sz = (-1.0 if flip_z else 1.0) * manual_scale

    temp_exports: List[str] = []
    renamed_originals: List[ tuple ] = []
    meta_objects: Dict[str, Any] = {}
    sidecar_objects: Dict[str, Any] = {}
    sidecar_blendshapes: Dict[str, Any] = {}
    rev = core.next_revision()
    export_succeeded = False

    try:
        for obj in mesh_transforms:
            short_name = _sanitize_name(cmds.ls(obj, shortNames=True)[0])
            b_id = _get_or_set_maya_bridge_id(obj, short_name)
            orig_bl_name = short_name
            if cmds.attributeQuery("sisync_original_name", node=obj, exists=True):
                try:
                    val_orig = str(cmds.getAttr(f"{obj}.sisync_original_name") or "")
                    if val_orig:
                        orig_bl_name = val_orig
                except Exception:
                    pass
            piv = cmds.xform(obj, query=True, worldSpace=True, rotatePivot=True)
            rot_deg = cmds.xform(obj, query=True, worldSpace=True, rotation=True)

            # Extract Phase 2 canonical Vertex Color Sets (POINT/CORNER) & Material / PBR / UV payload BEFORE duplicating
            v_data_meta, m_data_meta, sidecar_entry = _extract_maya_phase2_payload(
                src_obj=obj,
                up_axis_mode=up_axis_mode,
                manual_scale=manual_scale,
                flip_x=flip_x,
                flip_y=flip_y,
                flip_z=flip_z,
                ex_dir=ex_dir,
                vertex_color_enabled=vcol_enabled,
            )
            sidecar_objects[short_name] = sidecar_entry

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
            else:
                bl_rot_deg = list(
                    core.CoordinateBasis.maya_rot_to_blender(
                        (float(rot_deg[0]), float(rot_deg[1]), float(rot_deg[2])),
                        up_axis=up_axis_mode,
                    )
                )

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
            poly_count = int(cmds.polyEvaluate(dup, face=True) or 0)
            # Query attached native Maya blendShape deformer nodes
            bs_deformers = cmds.ls(cmds.listHistory(orig_temp_name), type="blendShape") or []
            if bs_deformers:
                bs_node = bs_deformers[0]
                aliases = cmds.listAttr(f"{bs_node}.w", multi=True) or []
                if aliases:
                    kb_list = [{"name": "Basis", "value": 0.0}]
                    for alias in aliases:
                        w_val = float(cmds.getAttr(f"{bs_node}.{alias}") or 0.0)
                        kb_list.append({"name": alias, "value": w_val})
                    sidecar_blendshapes[short_name] = {"key_blocks": kb_list}

            meta_objects[short_name] = {
                "bridge_id": b_id,
                "original_blender_name": orig_bl_name,
                "sanitized_name": short_name,
                "revision": rev,
                "maya_pivot": [float(piv[0]), float(piv[1]), float(piv[2])],
                "maya_rot": [float(rot_deg[0]), float(rot_deg[1]), float(rot_deg[2])],
                "location_blender_m": bl_loc_m,
                "rotation_blender_deg": bl_rot_deg,
                "freeze_location": freeze_loc,
                "freeze_rotation": freeze_rot,
                "vertex_count": vtx_count,
                "polygon_count": poly_count,
                "vertex_data": v_data_meta,
                "materials": m_data_meta,
            }

        if temp_exports:
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
    sidecar_path = core.write_phase2_sidecar(
        {
            "revision": rev,
            "source": "maya",
            "destination": "blender",
            "timestamp": export_ts,
            "transfer_flags": t_flags,
            "hierarchy_enabled": hier_enabled,
            "vertex_color_enabled": vcol_enabled,
            "hierarchy": hierarchy_list,
            "blendshapes": sidecar_blendshapes,
            "objects": sidecar_objects,
        },
        custom_dir=ex_dir,
    )
    core.write_metadata(
        {
            "revision": rev,
            "source": "maya",
            "destination": "blender",
            "format": "fbx",
            "file_path": m2b_fbx,
            "fbx_path": m2b_fbx,
            "phase2_sidecar": sidecar_path,
            "timestamp": export_ts,
            "export_timestamp": export_ts,
            "maya_up_axis": up_axis_mode,
            "freeze_location": freeze_loc,
            "freeze_rotation": freeze_rot,
            "transfer_flags": t_flags,
            "hierarchy_enabled": hier_enabled,
            "vertex_color_enabled": vcol_enabled,
            "hierarchy": hierarchy_list,
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
    core.log_event("maya", "EXPORT_TO_BLENDER", "success", source="maya", destination="blender", revision=rev, extra=m2b_fbx)
    return m2b_fbx


# ---------------------------------------------------------------------------
# Import: Blender -> Maya (With bridge_id, Hierarchy & Sculpt/Remesh Support)
# ---------------------------------------------------------------------------
def pull_from_blender(custom_fbx_path: Optional[str] = None, *_) -> Optional[List[str]]:
    """
    Imports blender_to_maya.fbx (or metadata-specified FBX) into Maya, matches by
    bridge_id or sanitized name, clears .pnts tweaks on remesh, updates in-place without duplicating,
    and synchronizes Hierarchy (when Hierarchy=ON), Vertex Color Sets (when Vertex Color=ON),
    Corner Colors, Material Slots, Face Assignments, PBR & UVs.
    """
    global _LAST_IMPORT_TS, _LAST_SEEN_BLENDER_TS, _LAST_SEEN_BLENDER_REV

    if core.EchoSuppressor.is_importing("maya") or getattr(sys, "_sisync_maya_pulling", False):
        return []

    if not ensure_fbx_plugin():
        return []

    sys._sisync_maya_pulling = True
    timer_was_active = False
    if _QT_WATCH_TIMER is not None:
        try:
            timer_was_active = bool(_QT_WATCH_TIMER.isActive())
            _QT_WATCH_TIMER.stop()
        except Exception:
            pass
    try:
        return _pull_from_blender_impl(custom_fbx_path)
    finally:
        sys._sisync_maya_pulling = False
        if timer_was_active and _QT_WATCH_TIMER is not None:
            try:
                _QT_WATCH_TIMER.start()
            except Exception:
                pass


def _pull_from_blender_impl(custom_fbx_path: Optional[str] = None) -> Optional[List[str]]:
    global _LAST_IMPORT_TS, _LAST_SEEN_BLENDER_TS, _LAST_SEEN_BLENDER_REV

    ex_dir = get_exchange_dir()
    meta = core.read_metadata(ex_dir)
    meta_objects = meta.get("objects", {}) if isinstance(meta.get("objects"), dict) else {}
    sidecar_all = core.read_phase2_sidecar(ex_dir)
    sidecar_objects = sidecar_all.get("objects", {}) if isinstance(sidecar_all.get("objects"), dict) else {}
    rev = int(meta.get("revision", 0))
    ts = float(meta.get("timestamp") or meta.get("export_timestamp") or time.time())

    local_flags = get_maya_transfer_flags(ex_dir)
    meta_flags = meta.get("transfer_flags") if isinstance(meta.get("transfer_flags"), dict) else {}
    hier_enabled = bool(
        meta.get(
            "sync_hierarchy",
            meta.get("hierarchy_enabled", meta_flags.get("hierarchy", local_flags["hierarchy"])),
        )
    )
    vcol_enabled = bool(
        meta.get(
            "sync_vertex_color",
            meta.get("vertex_color_enabled", meta_flags.get("vertex_color", local_flags["vertex_color"])),
        )
    )
    bs_enabled = bool(
        meta.get(
            "sync_blendshapes",
            sidecar_all.get("sync_blendshapes", meta_flags.get("blendshapes", local_flags.get("blendshapes", False))),
        )
    )
    hierarchy_list = (
        (sidecar_all.get("hierarchy") if isinstance(sidecar_all.get("hierarchy"), list) else None)
        or (meta.get("hierarchy") if isinstance(meta.get("hierarchy"), list) else [])
    )

    if not bs_enabled:
        for t_cand in (cmds.ls(type="transform", long=True) or []):
            if t_cand.split("|")[-1].split(":")[-1] == "BlendShapes" and cmds.objExists(t_cand):
                try:
                    cmds.delete(t_cand)
                except Exception:
                    pass
    else:
        incoming_bs_names = {
            _sanitize_name(k)
            for k, v in (meta_objects.items() if isinstance(meta_objects, dict) else [])
            if isinstance(v, dict) and v.get("is_blendshape")
        }
        for t_cand in (cmds.ls(type="transform", long=True) or []):
            if t_cand.split("|")[-1].split(":")[-1] == "BlendShapes" and cmds.objExists(t_cand):
                for ch in (cmds.listRelatives(t_cand, children=True, type="transform", fullPath=True) or []):
                    ch_short = _sanitize_name(ch.split("|")[-1].split(":")[-1].replace("__sisync_target", ""))
                    if incoming_bs_names and ch_short not in incoming_bs_names:
                        try:
                            cmds.delete(ch)
                        except Exception:
                            pass

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
    if not temp_fbx and not ((hier_enabled or bs_enabled) and hierarchy_list):
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
    freeze_loc = False if hier_enabled else bool(get_pref("sisync_freeze_loc", False))
    freeze_rot = False if hier_enabled else bool(get_pref("sisync_freeze_rot", True))

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
    if hier_enabled or bs_enabled:
        parented_leaf_names = {
            t.split("|")[-1].split(":")[-1].replace("__sisync_target", "")
            for t in all_transforms
            if t.count("|") > 1
        }
        for t_root in list(all_transforms):
            if t_root.count("|") == 1 and cmds.objExists(t_root):
                r_leaf = t_root.split("|")[-1].split(":")[-1].replace("__sisync_target", "")
                if r_leaf in parented_leaf_names:
                    try:
                        cmds.delete(t_root)
                    except Exception:
                        pass
        all_transforms = cmds.ls(type="transform", long=True) or []
    for t_node in all_transforms:
        shapes = cmds.listRelatives(t_node, shapes=True, fullPath=True, noIntermediate=True) or []
        if not any(cmds.nodeType(s) == "mesh" for s in shapes):
            continue
        raw_leaf = t_node.split("|")[-1].split(":")[-1]
        if raw_leaf.endswith("__sisync_target"):
            raw_leaf = raw_leaf[: -len("__sisync_target")]
        short_n = _sanitize_name(raw_leaf)
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
    imported_world_pts_map: Dict[str, Any] = {}
    try:
        transforms_before = set(cmds.ls(type="transform", long=True) or [])
        if temp_fbx and os.path.exists(temp_fbx):
            try:
                cmds.file(temp_fbx, i=True, type="FBX", ignoreVersion=True, mergeNamespacesOnClash=False)
            except Exception as imp_err:
                core.log_event("maya", "FBX_IMPORT", "error", error=str(imp_err), extra=temp_fbx)

        transforms_after = set(cmds.ls(type="transform", long=True) or [])
        imported_transforms = list(transforms_after - transforms_before)
        imported_meshes = [
            node for node in imported_transforms if cmds.listRelatives(node, shapes=True, type="mesh")
        ]
        # Clean up any non-mesh transforms imported by FBX so _apply_maya_hierarchy manages empties cleanly
        for non_mesh_t in imported_transforms:
            if non_mesh_t not in imported_meshes and cmds.objExists(non_mesh_t):
                desc_m = cmds.listRelatives(non_mesh_t, allDescendents=True, type="mesh") or []
                if not desc_m:
                    try:
                        cmds.delete(non_mesh_t)
                    except Exception:
                        pass

        for imp_obj in imported_meshes:
            raw_short = imp_obj.split("|")[-1].split(":")[-1]
            base_name = _sanitize_name(raw_short)
            # Strip trailing auto-increment digits if base_name isn't directly in meta_objects
            if base_name not in meta_objects:
                stripped_digits = re.sub(r"\d+$", "", base_name)
                if stripped_digits in meta_objects or stripped_digits in stash_map:
                    base_name = stripped_digits

            obj_meta = meta_objects.get(base_name, {}) if isinstance(meta_objects, dict) else {}
            if not obj_meta and isinstance(meta_objects, dict):
                for mk, mv in meta_objects.items():
                    if _sanitize_name(mk) == base_name or (mk.endswith(".003") and f"{_sanitize_name(mk)}_003" == base_name):
                        obj_meta = mv
                        break
            if not obj_meta and len(imported_meshes) == 1 and len(meta_objects) == 1:
                base_name, obj_meta = next(iter(meta_objects.items()))
                base_name = _sanitize_name(base_name)
            if not obj_meta and isinstance(meta_objects, dict) and len(meta_objects) > 1:
                try:
                    cmds.delete(imp_obj)
                except Exception:
                    pass
                continue

            orig_bl_name = str(obj_meta.get("original_blender_name") or base_name)
            sidecar_entry = sidecar_objects.get(base_name) or sidecar_objects.get(orig_bl_name) or {}
            if not sidecar_entry and isinstance(sidecar_objects, dict):
                for sk, sv in sidecar_objects.items():
                    if _sanitize_name(sk) == base_name or (sk.endswith(".003") and f"{_sanitize_name(sk)}_003" == base_name):
                        sidecar_entry = sv
                        break
            if not sidecar_entry and len(imported_meshes) == 1 and len(sidecar_objects) == 1:
                sidecar_entry = next(iter(sidecar_objects.values()))

            inc_bid = str(obj_meta.get("bridge_id") or "")

            raw_trans = cmds.xform(imp_obj, query=True, worldSpace=True, translation=True)
            raw_rot = cmds.xform(imp_obj, query=True, worldSpace=True, rotation=True)
            target_piv = [0.0, 0.0, 0.0] if freeze_loc else [raw_trans[0] * sx, raw_trans[1] * sy, raw_trans[2] * sz]
            target_rot = [0.0, 0.0, 0.0] if freeze_rot else raw_rot

            stashed_target = stash_map.pop(base_name, None)
            if not stashed_target and inc_bid and inc_bid in bridge_id_stash_map:
                stashed_target = bridge_id_stash_map.pop(inc_bid, None)
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

                # Capture exact world-space points directly from imp_obj at world root before topology transfer
                imp_world_pts = None
                try:
                    s_imp = om.MSelectionList()
                    s_imp.add(imp_obj)
                    d_imp = s_imp.getDagPath(0)
                    d_imp.extendToShape()
                    imp_world_pts = om.MFnMesh(d_imp).getPoints(om.MSpace.kWorld)
                    imported_world_pts_map[base_name] = imp_world_pts
                except Exception:
                    imp_world_pts = None

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
                                tweak_count = int(cmds.getAttr(f"{old_shapes[0]}.pnts", size=True) or 0)
                                if 0 < tweak_count <= 512:
                                    for i in range(tweak_count):
                                        cmds.setAttr(f"{old_shapes[0]}.pnts[{i}]", 0.0, 0.0, 0.0)
                                elif tweak_count > 512:
                                    cmds.polyMoveVertex(target_obj, constructionHistory=False)
                                    cmds.delete(target_obj, constructionHistory=True)
                            except Exception:
                                pass

                            cmds.connectAttr(f"{new_shapes[0]}.outMesh", f"{old_shapes[0]}.inMesh", force=True)
                            cmds.dgeval(f"{old_shapes[0]}.outMesh")
                            cmds.disconnectAttr(f"{new_shapes[0]}.outMesh", f"{old_shapes[0]}.inMesh")
                        except Exception as e:
                            core.log_event("maya", "TOPOLOGY_TRANSFER", "error", error=str(e), extra=base_name)
                else:
                    if target_vtx != imp_vtx:
                        show_hud(f"Skipped topology change on rigged mesh: {base_name}")
                        cmds.delete(imp_obj)
                        cmds.rename(target_obj, base_name)
                        continue

                cmds.xform(target_obj, worldSpace=True, translation=target_piv, rotation=target_rot)
                cmds.xform(target_obj, objectSpace=True, scale=(1.0, 1.0, 1.0))

                if imp_world_pts is not None:
                    try:
                        t_list = om.MSelectionList()
                        t_list.add(target_obj)
                        t_dag = t_list.getDagPath(0)
                        t_dag.extendToShape()
                        t_fn = om.MFnMesh(t_dag)
                        t_fn.setPoints(imp_world_pts, om.MSpace.kWorld)
                        t_fn.updateSurface()
                    except Exception as e:
                        core.log_event("maya", "SET_POINTS", "error", error=str(e), extra=base_name)

                if (sx * sy * sz) < 0.0:
                    cmds.polyNormal(target_obj, normalMode=0, userNormalMode=0, ch=False)

                try:
                    cmds.dgdirty(target_obj)
                except Exception:
                    pass
                cmds.delete(imp_obj)
                target_obj = cmds.rename(target_obj, base_name)
                _get_or_set_maya_bridge_id(target_obj, base_name, force_id=inc_bid)
                if not cmds.attributeQuery("sisync_original_name", node=target_obj, exists=True):
                    try:
                        cmds.addAttr(target_obj, longName="sisync_original_name", dataType="string")
                    except Exception:
                        pass
                try:
                    cmds.setAttr(f"{target_obj}.sisync_original_name", orig_bl_name, type="string")
                except Exception:
                    pass
                _apply_maya_phase2_payload(
                    target_obj,
                    obj_meta,
                    sidecar_entry,
                    ex_dir,
                    vertex_color_enabled=vcol_enabled,
                    is_newly_created=False,
                )
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
                cmds.xform(new_obj, zeroTransformPivots=True)
            except Exception:
                pass
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

            if HAS_OPENMAYA:
                try:
                    s_new = om.MSelectionList()
                    s_new.add(new_obj)
                    d_new = s_new.getDagPath(0)
                    d_new.extendToShape()
                    imported_world_pts_map[base_name] = om.MFnMesh(d_new).getPoints(om.MSpace.kWorld)
                except Exception:
                    pass

            _get_or_set_maya_bridge_id(new_obj, base_name, force_id=inc_bid)
            if not cmds.attributeQuery("sisync_original_name", node=new_obj, exists=True):
                try:
                    cmds.addAttr(new_obj, longName="sisync_original_name", dataType="string")
                except Exception:
                    pass
            try:
                cmds.setAttr(f"{new_obj}.sisync_original_name", orig_bl_name, type="string")
            except Exception:
                pass
            _apply_maya_phase2_payload(
                new_obj,
                obj_meta,
                sidecar_entry,
                ex_dir,
                vertex_color_enabled=vcol_enabled,
                is_newly_created=True,
            )
            updated_objects.append(new_obj)

        # Restore any remaining stashed target objects before reconstructing hierarchy
        for orig_short, stashed_node in list(stash_map.items()):
            if cmds.objExists(stashed_node):
                try:
                    cmds.rename(stashed_node, orig_short)
                except Exception:
                    pass
            stash_map.pop(orig_short, None)

        if (hier_enabled or bs_enabled) and hierarchy_list:
            active_hier_list = (
                hierarchy_list
                if hier_enabled
                else [
                    h for h in hierarchy_list
                    if isinstance(h, dict) and (
                        h.get("sanitized_name") == "BlendShapes"
                        or h.get("parent_sanitized_name") == "BlendShapes"
                        or bool(h.get("is_blendshapes_group"))
                        or bool(h.get("is_blendshape_mesh"))
                    )
                ]
            )
            if active_hier_list:
                hier_touched = _apply_maya_hierarchy(
                    hierarchy_list=active_hier_list,
                    up_axis_mode=str(meta.get("maya_up_axis", "Y")),
                    imported_world_pts_map=imported_world_pts_map,
                )
                for hn in hier_touched:
                    if hn not in updated_objects and cmds.objExists(hn):
                        updated_objects.append(hn)

        # Automated BlendShape Deformer Engine (Blender -> Maya)
        bs_group = cmds.ls("*BlendShapes*", type="transform", long=True) or []
        if bs_group:
            target_meshes = cmds.listRelatives(bs_group[0], children=True, type="transform", fullPath=True) or []
            if target_meshes:
                all_mesh_shapes = cmds.ls(type="mesh", long=True) or []
                for m_shape in all_mesh_shapes:
                    b_trans = (cmds.listRelatives(m_shape, parent=True, fullPath=True) or [m_shape])[0]
                    if "BlendShapes" in b_trans or b_trans in target_meshes:
                        continue
                    existing_bs = cmds.ls(cmds.listHistory(b_trans), type="blendShape") or []
                    bs_node = None
                    if not existing_bs:
                        try:
                            bs_name = f"{b_trans.split('|')[-1]}_blendShape"
                            bs_node = cmds.blendShape(
                                target_meshes,
                                b_trans,
                                name=bs_name,
                                frontOfChain=True,
                            )[0]
                        except Exception:
                            bs_node = None
                    else:
                        bs_node = existing_bs[0]
                        existing_attrs = cmds.listAttr(f"{bs_node}.w", multi=True) or []
                        for idx, tm in enumerate(target_meshes):
                            t_short = tm.split("|")[-1].split(":")[-1].replace("__sisync_target", "")
                            if t_short not in existing_attrs:
                                t_idx = len(existing_attrs) + idx + 1
                                try:
                                    cmds.blendShape(bs_node, edit=True, target=(b_trans, t_idx, tm, 1.0))
                                except Exception:
                                    pass
                    if bs_node:
                        for tm in target_meshes:
                            t_short = tm.split("|")[-1].split(":")[-1].replace("__sisync_target", "")
                            if cmds.attributeQuery(t_short, node=bs_node, exists=True):
                                init_w = 1.0 if t_short == "smile" else 0.0
                                try:
                                    cmds.setAttr(f"{bs_node}.{t_short}", init_w)
                                except Exception:
                                    pass
                            try:
                                cmds.setAttr(f"{tm}.visibility", False)
                            except Exception:
                                pass

    finally:
        # Always restore any remaining stashed target objects and end the import guard
        for orig_short, stashed_node in stash_map.items():
            if cmds.objExists(stashed_node):
                try:
                    cmds.rename(stashed_node, orig_short)
                except Exception:
                    pass
        for t_check in (cmds.ls(type="transform", long=True) or []):
            leaf_c = t_check.split("|")[-1].split(":")[-1]
            if leaf_c.endswith("__sisync_target") and cmds.objExists(t_check):
                clean_c = leaf_c[: -len("__sisync_target")]
                try:
                    cmds.rename(t_check, clean_c)
                except Exception:
                    pass
        if hier_enabled or bs_enabled:
            all_tx_final = cmds.ls(type="transform", long=True) or []
            parented_final_leaves = {
                m.split("|")[-1].split(":")[-1]
                for m in all_tx_final
                if m.count("|") > 1 and cmds.objExists(m)
            }
            for t_root in all_tx_final:
                if t_root.count("|") == 1 and cmds.objExists(t_root):
                    short_r = t_root.split("|")[-1].split(":")[-1]
                    if short_r in parented_final_leaves:
                        try:
                            cmds.delete(t_root)
                        except Exception:
                            pass
        core.EchoSuppressor.end_import("maya", revision=rev, timestamp=ts)

    valid_updated = [n for n in updated_objects if cmds.objExists(n)]
    if valid_updated:
        cmds.select(valid_updated, replace=True)
        cmds.refresh(force=True)
        show_hud(f"Auto-Synced {len(valid_updated)} node(s) from Blender.")
        core.log_event("maya", "IMPORT_FROM_BLENDER", "success", source="blender", destination="maya", revision=rev, extra=f"count={len(valid_updated)}")

    return valid_updated


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
# Unified Maya UI Window (Preserving Current Design + Transfer Toggles)
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


def _on_maya_all_toggle(val: bool):
    if bool(val):
        set_transfer_toggles(all_enabled=True, hierarchy_enabled=True, vertex_color_enabled=True)
        if cmds.checkBox("sisync_sync_hierarchy_cb", exists=True):
            cmds.checkBox("sisync_sync_hierarchy_cb", edit=True, value=True, enable=False)
        if cmds.checkBox("sisync_sync_vcol_cb", exists=True):
            cmds.checkBox("sisync_sync_vcol_cb", edit=True, value=True, enable=False)
    else:
        set_transfer_toggles(all_enabled=False)
        if cmds.checkBox("sisync_sync_hierarchy_cb", exists=True):
            cmds.checkBox("sisync_sync_hierarchy_cb", edit=True, enable=True)
        if cmds.checkBox("sisync_sync_vcol_cb", exists=True):
            cmds.checkBox("sisync_sync_vcol_cb", edit=True, enable=True)


def _update_maya_vtx_counter(*_):
    if cmds.about(batch=True) or not cmds.text("sisync_vtx_counter_lbl", exists=True):
        return
    sel_list = cmds.ls(selection=True, dag=True, type="mesh", long=True) or []
    if sel_list:
        sel_transforms = set(cmds.listRelatives(sel_list, parent=True, fullPath=True) or sel_list)
        total_vtx = 0
        for m_shape in sel_list:
            try:
                total_vtx += cmds.polyEvaluate(m_shape, vertex=True) or 0
            except Exception:
                pass
        txt = f"Selected: {len(sel_transforms)} mesh(es) | Vertices: {total_vtx:,}"
    else:
        txt = "Selection: No mesh selected"
    try:
        cmds.text("sisync_vtx_counter_lbl", edit=True, label=txt)
    except Exception:
        pass


def show_ui(*_):
    """Opens the unified GoB-Style Maya Window matching Blender's UI."""
    if cmds.about(batch=True):
        return

    set_pref("sisync_up_axis", "Y")
    t_flags = get_maya_transfer_flags()

    win_name = "SiSyncWindow"
    if cmds.window(win_name, exists=True):
        cmds.deleteUI(win_name)

    win = cmds.window(
        win_name,
        title="SiSync Maya - Blender",
        widthHeight=(370, 415),
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
    # Active Selection Vertex Counter Status
    cmds.text("sisync_vtx_counter_lbl", label="Selection: No mesh selected", align="center", font="boldLabelFont")
    try:
        _update_maya_vtx_counter()
        cmds.scriptJob(event=("SelectionChanged", _update_maya_vtx_counter), parent=win_name)
    except Exception:
        pass

    # Transfer Toggles (Server / All / Hierarchy / Vertex Color / Export Blend Shapes)
    cmds.frameLayout(label="Transfer Toggles", collapsable=False, marginWidth=8, marginHeight=5)
    cmds.rowLayout(numberOfColumns=4, columnWidth4=(55, 85, 95, 125))
    cmds.checkBox(
        "sisync_sync_all_cb",
        label="All",
        value=bool(t_flags["raw_all"]),
        changeCommand=_on_maya_all_toggle,
    )
    cmds.checkBox(
        "sisync_sync_hierarchy_cb",
        label="Hierarchy",
        value=bool(t_flags["hierarchy"]),
        enable=not bool(t_flags["raw_all"]),
        changeCommand=lambda v: set_transfer_toggles(hierarchy_enabled=bool(v)),
    )
    cmds.checkBox(
        "sisync_sync_vcol_cb",
        label="Vertex Color",
        value=bool(t_flags["vertex_color"]),
        enable=not bool(t_flags["raw_all"]),
        changeCommand=lambda v: set_transfer_toggles(vertex_color_enabled=bool(v)),
    )
    cmds.checkBox(
        "sisync_sync_bs_cb",
        label="Blend Shapes",
        value=bool(t_flags.get("blendshapes", False)),
        changeCommand=lambda v: set_transfer_toggles(blendshapes_enabled=bool(v)),
    )
    cmds.setParent("..")
    cmds.setParent("..")

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
