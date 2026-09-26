#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync — Unified Network Compatibility Module
Aliases all network operations directly to the canonical server.py & sisync_bridge_core.py
so there is strictly ONE network runtime in Blender.
"""

from . import sisync_bridge_core as core
from .server import (
    BLENDER_PORT,
    MAYA_PORT,
    start_server,
    stop_server,
    toggle_server,
    is_server_running,
    send_bridge_command,
    notify_maya_immediate,
)


def send_to_maya(
    maya_host: str = core.DEFAULT_HOST,
    maya_port: int = MAYA_PORT,
    command: str = core.COMMAND_SYNC_MESH,
    payload=None,
    timeout: float = 2.0,
):
    return core.BridgeClient.send(
        host=maya_host,
        port=maya_port,
        command=command,
        sender="blender",
        target="maya",
        payload=payload,
        timeout=timeout,
    )
