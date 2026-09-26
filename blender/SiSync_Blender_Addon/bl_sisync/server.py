#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync — Blender Adapter Network & Main-Thread Event Queue
Delegates all TCP framing, BridgeServer, BridgeClient, metadata, and echo suppression
to the canonical sisync_bridge_core module.
Never touches bpy.data or bpy.ops from a background socket thread.
"""

import os
import json
import queue
import socket
from typing import Optional, Dict, Any
import bpy

from . import sisync_bridge_core as core
from . import sync_mesh
from .sync_mesh import BlenderMeshSync, get_exchange_meta_path
from .sync_maps import BlenderMapManager

BLENDER_PORT = core.BLENDER_BRIDGE_PORT
MAYA_PORT = core.MAYA_BRIDGE_PORT

_SERVER_INSTANCE: Optional[core.BridgeServer] = None
_TASK_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_LAST_SEEN_MAYA_TS: float = 0.0
_LAST_SEEN_MAYA_REV: int = 0


def send_bridge_command(
    host: str = core.DEFAULT_HOST,
    port: int = MAYA_PORT,
    command: str = core.COMMAND_GET_STATUS,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 1.5,
) -> Optional[Dict[str, Any]]:
    """Dispatches a canonical framed command via BridgeClient."""
    resp = core.BridgeClient.send(
        host=host,
        port=port,
        command=command,
        sender="blender",
        target="maya" if port == MAYA_PORT else "blender",
        payload=payload,
        timeout=timeout,
    )
    if isinstance(resp, dict) and resp.get("status") != "error":
        return resp
    return None


def notify_maya_immediate(fbx_path: str, revision: Optional[int] = None) -> bool:
    """
    Notifies Maya via:
    1. Canonical SiSync TCP Bridge on port 19850 (COMMAND_SYNC_MESH)
    2. Dedicated Maya Python commandPort (:19852 / :7001) restricted to sisync_maya.pull_from_blender
    """
    notified = False
    rev = revision if revision is not None else core.get_current_revision()

    # 1. Canonical TCP Bridge (:19850)
    resp = core.BridgeClient.send(
        host="127.0.0.1",
        port=MAYA_PORT,
        command=core.COMMAND_SYNC_MESH,
        sender="blender",
        target="maya",
        payload={"file_path": fbx_path, "fbx_path": fbx_path, "format": "fbx", "revision": rev},
        timeout=0.8,
        revision=rev,
    )
    if isinstance(resp, dict) and resp.get("status") in ("scheduled", "queued", "success", "ok"):
        notified = True

    # 2. Also trigger Maya's unfocused main-thread commandPort (:19852 / :7001)
    safe_path = fbx_path.replace("\\", "/")
    py_cmd = (
        "import sys\n"
        "if 'sisync_maya' in sys.modules and hasattr(sys.modules['sisync_maya'], 'pull_from_blender'):\n"
        f"    sys.modules['sisync_maya'].pull_from_blender({safe_path!r})\n"
    ).encode("utf-8")
    for cport in (core.MAYA_COMMAND_PORT, core.MAYA_LEGACY_MCP_PORT):
        try:
            with socket.create_connection(("127.0.0.1", cport), timeout=0.35) as s:
                s.sendall(py_cmd)
                notified = True
                break
        except Exception:
            pass

    return notified


def _blender_message_handler(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes in BridgeServer worker thread.
    NEVER calls bpy.data or bpy.ops directly; strictly enqueues onto _TASK_QUEUE for _main_thread_timer.
    """
    cmd = str(msg.get("command", "")).upper()
    payload = msg.get("payload", {}) if isinstance(msg.get("payload"), dict) else {}
    fbx_path = str(payload.get("file_path") or payload.get("fbx_path") or msg.get("fbx_path") or msg.get("file_path") or "")
    rev = int(msg.get("revision", 0))

    if cmd in (core.COMMAND_SYNC_MESH, core.COMMAND_OBJECT_CREATE, core.COMMAND_OBJECT_UPDATE):
        _TASK_QUEUE.put({
            "action": "import_from_maya",
            "fbx_path": fbx_path,
            "revision": rev,
            "sender": msg.get("sender", "maya"),
        })
        return {
            "status": "scheduled",
            "command": core.COMMAND_ACK,
            "target": "blender",
            "revision": rev,
        }
    elif cmd == core.COMMAND_REFRESH_MAPS:
        _TASK_QUEUE.put({
            "action": "refresh_maps",
            "revision": rev,
        })
        return {
            "status": "scheduled",
            "command": core.COMMAND_ACK,
            "target": "blender",
        }
    elif cmd == core.COMMAND_GET_STATUS:
        return {
            "status": "success",
            "command": core.COMMAND_STATUS_REPORT,
            "target": "blender",
            "port": BLENDER_PORT,
        }
    return {
        "status": "ok",
        "command": core.COMMAND_ACK,
        "target": "blender",
    }


def _main_thread_timer() -> Optional[float]:
    """
    Runs on Blender's main thread via bpy.app.timers.
    Drains _TASK_QUEUE and checks sisync_metadata.json with deterministic EchoSuppressor.
    """
    global _LAST_SEEN_MAYA_TS, _LAST_SEEN_MAYA_REV

    if not is_server_running():
        return None

    # 1. Process socket-queued commands on the main thread
    if not _TASK_QUEUE.empty():
        should_import = False
        should_refresh_maps = False
        latest_rev = 0
        while not _TASK_QUEUE.empty():
            try:
                item = _TASK_QUEUE.get_nowait()
                act = item.get("action")
                if act == "import_from_maya":
                    should_import = True
                    latest_rev = max(latest_rev, int(item.get("revision", 0)))
                elif act == "refresh_maps":
                    should_refresh_maps = True
            except queue.Empty:
                break

        if should_import and not core.EchoSuppressor.is_importing("blender"):
            try:
                core.EchoSuppressor.begin_import("blender")
                objs = BlenderMeshSync.import_from_maya(bpy.context)
                _LAST_SEEN_MAYA_TS = max(_LAST_SEEN_MAYA_TS, sync_mesh.LAST_IMPORT_TIMESTAMP)
                _LAST_SEEN_MAYA_REV = max(_LAST_SEEN_MAYA_REV, latest_rev)
                core.EchoSuppressor.end_import("blender", revision=_LAST_SEEN_MAYA_REV, timestamp=_LAST_SEEN_MAYA_TS)
                core.log_event("blender", "AUTO_IMPORT_SOCKET", "success", source="maya", destination="blender", revision=_LAST_SEEN_MAYA_REV, extra=f"imported={len(objs)}")
            except Exception as e:
                core.EchoSuppressor.end_import("blender")
                core.log_event("blender", "AUTO_IMPORT_SOCKET", "error", source="maya", destination="blender", error=str(e))

        if should_refresh_maps:
            try:
                BlenderMapManager.refresh_pbr_maps(bpy.context)
            except Exception as e:
                core.log_event("blender", "REFRESH_MAPS", "error", source="maya", destination="blender", error=str(e))

        return 0.25

    # 2. Check sisync_metadata.json / SiSync_Exchange.json fallback with EchoSuppressor
    try:
        meta = core.read_metadata(sync_mesh.get_exchange_dir(bpy.context))
        if meta and str(meta.get("source", "")).lower() == "maya":
            ts = float(meta.get("timestamp") or meta.get("export_timestamp") or 0.0)
            rev = int(meta.get("revision", 0))
            is_newer_ts = ts > 0.0 and ts > (_LAST_SEEN_MAYA_TS + 1e-4) and ts > (sync_mesh.LAST_IMPORT_TIMESTAMP + 1e-4)
            is_newer_rev = rev > 0 and rev > _LAST_SEEN_MAYA_REV
            if (is_newer_ts or is_newer_rev) and core.EchoSuppressor.should_accept_incoming("blender", meta):
                _LAST_SEEN_MAYA_TS = ts
                _LAST_SEEN_MAYA_REV = max(_LAST_SEEN_MAYA_REV, rev)
                core.EchoSuppressor.begin_import("blender")
                try:
                    objs = BlenderMeshSync.import_from_maya(bpy.context)
                    core.EchoSuppressor.end_import("blender", revision=_LAST_SEEN_MAYA_REV, timestamp=ts)
                    core.log_event("blender", "AUTO_IMPORT_WATCHER", "success", source="maya", destination="blender", revision=_LAST_SEEN_MAYA_REV, extra=f"imported={len(objs)}")
                except Exception as imp_err:
                    core.EchoSuppressor.end_import("blender")
                    core.log_event("blender", "AUTO_IMPORT_WATCHER", "error", source="maya", destination="blender", error=str(imp_err))
    except Exception:
        pass

    return 0.30


def is_server_running() -> bool:
    return bool(_SERVER_INSTANCE and _SERVER_INSTANCE.is_running)


def start_server(port: int = BLENDER_PORT) -> bool:
    """Idempotent startup of the canonical Blender BridgeServer and main-thread timer."""
    global _SERVER_INSTANCE, _LAST_SEEN_MAYA_TS, _LAST_SEEN_MAYA_REV
    try:
        meta = core.read_metadata()
        if meta:
            _LAST_SEEN_MAYA_TS = max(_LAST_SEEN_MAYA_TS, float(meta.get("timestamp") or meta.get("export_timestamp") or 0.0))
            _LAST_SEEN_MAYA_REV = max(_LAST_SEEN_MAYA_REV, int(meta.get("revision", 0)))
            core.EchoSuppressor.end_import("blender", revision=_LAST_SEEN_MAYA_REV, timestamp=_LAST_SEEN_MAYA_TS)
    except Exception:
        pass

    if _SERVER_INSTANCE and _SERVER_INSTANCE.is_running:
        if _SERVER_INSTANCE.port == port:
            if not bpy.app.timers.is_registered(_main_thread_timer):
                try:
                    bpy.app.timers.register(_main_thread_timer, persistent=True)
                except Exception:
                    pass
            return True
        else:
            _SERVER_INSTANCE.stop()
            _SERVER_INSTANCE = None

    _SERVER_INSTANCE = core.BridgeServer(
        name="Blender",
        port=port,
        host=core.DEFAULT_HOST,
        message_handler=_blender_message_handler,
    )
    ok = _SERVER_INSTANCE.start()
    try:
        if not bpy.app.timers.is_registered(_main_thread_timer):
            bpy.app.timers.register(_main_thread_timer, persistent=True)
    except Exception as e:
        core.log_event("blender", "TIMER_REGISTER", "error", error=str(e))
    return ok


def stop_server():
    """Idempotent shutdown of the canonical Blender BridgeServer and main-thread timer."""
    global _SERVER_INSTANCE
    try:
        if bpy.app.timers.is_registered(_main_thread_timer):
            bpy.app.timers.unregister(_main_thread_timer)
    except Exception:
        pass

    if _SERVER_INSTANCE:
        _SERVER_INSTANCE.stop()
        _SERVER_INSTANCE = None


def toggle_server() -> bool:
    if is_server_running():
        stop_server()
        return False
    return start_server(BLENDER_PORT)
