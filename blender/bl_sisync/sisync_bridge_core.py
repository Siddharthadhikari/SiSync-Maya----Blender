#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync — Production Bi-directional Sync Bridge for Autodesk Maya 2026 & Blender 5.x
Canonical Shared Core: Protocol, Socket Framing, BridgeServer, BridgeClient,
Coordinate Mathematics, Metadata Schema, Echo Suppression, and Structured Logging.
"""

import os
import sys
import json
import time
import uuid
import math
import struct
import socket
import shutil
import tempfile
import threading
from typing import Dict, Any, Optional, Callable, Tuple, List

# ---------------------------------------------------------------------------
# Protocol & Port Constants
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "1.0"
SESSION_ID = str(uuid.uuid4())[:8]
_REVISION_LOCK = threading.Lock()
_CURRENT_REVISION = 0

DEFAULT_HOST = "127.0.0.1"
MAYA_BRIDGE_PORT = 19850
BLENDER_BRIDGE_PORT = 19851
MAYA_COMMAND_PORT = 19852
MAYA_LEGACY_MCP_PORT = 7001

DEFAULT_MAYA_PORT = MAYA_BRIDGE_PORT
DEFAULT_BLENDER_PORT = BLENDER_BRIDGE_PORT

DEFAULT_BRIDGE_DIR_NAME = "sisync_bridge"
ENV_BRIDGE_DIR = "SISYNC_BRIDGE_DIR"

FBX_MAYA_TO_BLENDER = "maya_to_blender.fbx"
FBX_BLENDER_TO_MAYA = "blender_to_maya.fbx"
EXCHANGE_FBX_LEGACY = "SiSync_Exchange.fbx"

OBJ_MAYA_TO_BLENDER = "maya_to_blender.obj"
OBJ_BLENDER_TO_MAYA = "blender_to_maya.obj"

METADATA_FILENAME = "sisync_metadata.json"
EXCHANGE_META_LEGACY = "SiSync_Exchange.json"

TEXTURES_SUBDIR = "textures"
LOGS_SUBDIR = "logs"

FORMAT_FBX = "fbx"
FORMAT_OBJ = "obj"

MAT_MODE_KEEP = "keep"
MAT_MODE_PRESERVE = "preserve"
MAT_MODE_BASE = "base"

# Explicit Protocol Commands
COMMAND_HELLO = "HELLO"
COMMAND_PING = "PING"
COMMAND_PONG = "PONG"
COMMAND_GET_STATUS = "GET_STATUS"
COMMAND_STATUS_REPORT = "STATUS_REPORT"
COMMAND_SYNC_MESH = "SYNC_MESH"
COMMAND_REFRESH_MAPS = "REFRESH_MAPS"
COMMAND_SCENE_SNAPSHOT = "SCENE_SNAPSHOT"
COMMAND_OBJECT_CREATE = "OBJECT_CREATE"
COMMAND_OBJECT_UPDATE = "OBJECT_UPDATE"
COMMAND_OBJECT_DELETE = "OBJECT_DELETE"
COMMAND_ASSET_PUSH = "ASSET_PUSH"
COMMAND_ACK = "ACK"
COMMAND_ERROR = "ERROR"

VALID_COMMANDS = {
    COMMAND_HELLO,
    COMMAND_PING,
    COMMAND_PONG,
    COMMAND_GET_STATUS,
    COMMAND_STATUS_REPORT,
    COMMAND_SYNC_MESH,
    COMMAND_REFRESH_MAPS,
    COMMAND_SCENE_SNAPSHOT,
    COMMAND_OBJECT_CREATE,
    COMMAND_OBJECT_UPDATE,
    COMMAND_OBJECT_DELETE,
    COMMAND_ASSET_PUSH,
    COMMAND_ACK,
    COMMAND_ERROR,
}

# Framing: 4-byte big-endian unsigned integer payload length + UTF-8 JSON payload
HEADER_STRUCT = struct.Struct(">I")
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024  # 32 MB safety limit


def next_revision() -> int:
    """Thread-safe monotonic revision counter for outgoing sync operations."""
    global _CURRENT_REVISION
    with _REVISION_LOCK:
        meta = read_metadata()
        disk_rev = int(meta.get("revision", 0)) if isinstance(meta, dict) else 0
        _CURRENT_REVISION = max(_CURRENT_REVISION, disk_rev) + 1
        return _CURRENT_REVISION


def get_current_revision() -> int:
    return _CURRENT_REVISION


# ---------------------------------------------------------------------------
# Staging Directory & Structured Logging
# ---------------------------------------------------------------------------
def get_bridge_dir(custom_dir: Optional[str] = None) -> str:
    """
    Returns the canonical shared SiSync bridge directory (%TEMP%/sisync_bridge/ or custom_dir).
    Ensures textures/ and logs/ subdirectories exist.
    """
    bridge_dir = custom_dir or os.environ.get(ENV_BRIDGE_DIR)
    if not bridge_dir:
        bridge_dir = os.path.join(tempfile.gettempdir(), DEFAULT_BRIDGE_DIR_NAME)
    bridge_dir = os.path.abspath(bridge_dir).replace("\\", "/")
    os.makedirs(bridge_dir, exist_ok=True)
    os.makedirs(os.path.join(bridge_dir, TEXTURES_SUBDIR), exist_ok=True)
    os.makedirs(os.path.join(bridge_dir, LOGS_SUBDIR), exist_ok=True)
    return bridge_dir


def get_textures_dir(custom_dir: Optional[str] = None) -> str:
    tex_dir = os.path.join(get_bridge_dir(custom_dir), TEXTURES_SUBDIR).replace("\\", "/")
    os.makedirs(tex_dir, exist_ok=True)
    return tex_dir


def get_logs_dir(custom_dir: Optional[str] = None) -> str:
    log_dir = os.path.join(get_bridge_dir(custom_dir), LOGS_SUBDIR).replace("\\", "/")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def log_event(
    log_target: str,
    command: str,
    status: str,
    source: str = "system",
    destination: str = "system",
    revision: Optional[int] = None,
    error: Optional[str] = None,
    extra: Optional[str] = None,
    custom_dir: Optional[str] = None,
) -> None:
    """
    Writes structured production logs to sisync_bridge/logs/{blender,maya,network}.log
    """
    try:
        log_dir = get_logs_dir(custom_dir)
        fname = f"{log_target.lower()}.log" if not log_target.endswith(".log") else log_target
        log_path = os.path.join(log_dir, fname)
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S")
        rev_val = revision if revision is not None else _CURRENT_REVISION
        lines = [
            f"[{ts_str}] "
            f"session={SESSION_ID} "
            f"revision={rev_val} "
            f"source={source} "
            f"destination={destination} "
            f"command={command} "
            f"status={status}"
        ]
        if error:
            lines[0] += f" error={error!r}"
        if extra:
            lines[0] += f" detail={extra!r}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(lines[0] + "\n")
    except Exception as log_err:
        sys.stderr.write(f"[SiSync Log Warning] {log_err}\n")


def get_maya_to_blender_fbx(custom_dir: Optional[str] = None) -> str:
    return os.path.join(get_bridge_dir(custom_dir), FBX_MAYA_TO_BLENDER).replace("\\", "/")


def get_blender_to_maya_fbx(custom_dir: Optional[str] = None) -> str:
    return os.path.join(get_bridge_dir(custom_dir), FBX_BLENDER_TO_MAYA).replace("\\", "/")


def get_legacy_exchange_fbx(custom_dir: Optional[str] = None) -> str:
    return os.path.join(get_bridge_dir(custom_dir), EXCHANGE_FBX_LEGACY).replace("\\", "/")


def get_maya_to_blender_obj(custom_dir: Optional[str] = None) -> str:
    return os.path.join(get_bridge_dir(custom_dir), OBJ_MAYA_TO_BLENDER).replace("\\", "/")


def get_blender_to_maya_obj(custom_dir: Optional[str] = None) -> str:
    return os.path.join(get_bridge_dir(custom_dir), OBJ_BLENDER_TO_MAYA).replace("\\", "/")


def get_payload_path(source_dcc: str, fmt: str = "fbx", custom_dir: Optional[str] = None) -> str:
    fmt = fmt.lower()
    if source_dcc.lower() == "maya":
        return get_maya_to_blender_obj(custom_dir) if fmt == "obj" else get_maya_to_blender_fbx(custom_dir)
    return get_blender_to_maya_obj(custom_dir) if fmt == "obj" else get_blender_to_maya_fbx(custom_dir)


def get_metadata_file_path(custom_dir: Optional[str] = None) -> str:
    return os.path.join(get_bridge_dir(custom_dir), METADATA_FILENAME).replace("\\", "/")


def get_legacy_metadata_file_path(custom_dir: Optional[str] = None) -> str:
    return os.path.join(get_bridge_dir(custom_dir), EXCHANGE_META_LEGACY).replace("\\", "/")


# ---------------------------------------------------------------------------
# Canonical Metadata Manifest IO
# ---------------------------------------------------------------------------
def write_metadata(info: Dict[str, Any], custom_dir: Optional[str] = None) -> str:
    """
    Writes canonical sync metadata manifest to sisync_metadata.json (and mirrors to
    SiSync_Exchange.json for legacy callers).
    """
    meta_path = get_metadata_file_path(custom_dir)
    legacy_path = get_legacy_metadata_file_path(custom_dir)
    now_ts = float(info.get("timestamp") or info.get("export_timestamp") or time.time())
    rev = int(info.get("revision") or next_revision())
    source = str(info.get("source") or info.get("source_dcc") or "unknown").lower()
    destination = str(info.get("destination") or ("maya" if source == "blender" else "blender")).lower()
    file_path = str(info.get("file_path") or info.get("fbx_path") or "").replace("\\", "/")

    payload = {
        "generator": "SiSync",
        "protocol_version": PROTOCOL_VERSION,
        "session_id": str(info.get("session_id") or SESSION_ID),
        "revision": rev,
        "source": source,
        "source_dcc": source,
        "destination": destination,
        "format": str(info.get("format") or FORMAT_FBX),
        "file_path": file_path,
        "fbx_path": file_path,
        "timestamp": now_ts,
        "export_timestamp": now_ts,
        "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
        **info,
    }
    payload["revision"] = rev
    payload["source"] = source
    payload["destination"] = destination
    payload["file_path"] = file_path
    payload["fbx_path"] = file_path
    payload["timestamp"] = now_ts
    payload["export_timestamp"] = now_ts

    for target_p in (meta_path, legacy_path):
        try:
            with open(target_p, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log_event("network", "WRITE_METADATA", "error", source=source, destination=destination, revision=rev, error=str(e))

    return meta_path


def read_metadata(custom_dir: Optional[str] = None) -> Dict[str, Any]:
    """Reads canonical sync metadata manifest from sisync_metadata.json (or fallback SiSync_Exchange.json)."""
    for candidate in (get_metadata_file_path(custom_dir), get_legacy_metadata_file_path(custom_dir)):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                log_event("network", "READ_METADATA", "error", error=str(e), extra=candidate)
    return {}


# ---------------------------------------------------------------------------
# Deterministic Echo Suppression
# ---------------------------------------------------------------------------
class EchoSuppressor:
    """
    Prevents infinite bidirectional sync loops (Blender -> Maya -> Blender -> Maya).
    Uses (session_id, revision, source, destination) as well as per-object bridge_id revisions.
    """
    _recent_updates: Dict[str, Tuple[int, str, float]] = {}
    _last_imported_revision_by_dcc: Dict[str, int] = {"maya": 0, "blender": 0}
    _last_imported_ts_by_dcc: Dict[str, float] = {"maya": 0.0, "blender": 0.0}
    _last_exported_revision_by_dcc: Dict[str, int] = {"maya": 0, "blender": 0}
    _in_import_guard: Dict[str, bool] = {"maya": False, "blender": False}
    SUPPRESSION_WINDOW_SEC = 2.0

    @classmethod
    def begin_import(cls, dcc: str):
        cls._in_import_guard[dcc.lower()] = True

    @classmethod
    def end_import(cls, dcc: str, revision: int = 0, timestamp: float = 0.0):
        d = dcc.lower()
        cls._in_import_guard[d] = False
        if revision > 0:
            cls._last_imported_revision_by_dcc[d] = max(cls._last_imported_revision_by_dcc.get(d, 0), int(revision))
        if timestamp > 0.0:
            cls._last_imported_ts_by_dcc[d] = max(cls._last_imported_ts_by_dcc.get(d, 0.0), float(timestamp))

    @classmethod
    def is_importing(cls, dcc: str) -> bool:
        return bool(cls._in_import_guard.get(dcc.lower(), False))

    @classmethod
    def record_export(cls, dcc: str, revision: int):
        cls._last_exported_revision_by_dcc[dcc.lower()] = int(revision)

    @classmethod
    def should_accept_incoming(cls, target_dcc: str, meta: Dict[str, Any]) -> bool:
        """
        Determines if metadata on disk/socket is genuinely a new incoming package for target_dcc.
        Rejects if:
        - target_dcc is currently inside an import guard
        - source == target_dcc (our own export!)
        - destination != target_dcc (when destination is specified)
        - revision <= already imported revision for target_dcc
        """
        t_dcc = target_dcc.lower()
        if cls.is_importing(t_dcc):
            return False
        if not isinstance(meta, dict) or not meta:
            return False

        src = str(meta.get("source") or meta.get("source_dcc") or "").lower()
        dst = str(meta.get("destination") or "").lower()
        if src == t_dcc:
            return False
        if dst and dst != t_dcc:
            return False

        rev = int(meta.get("revision", 0))
        ts = float(meta.get("timestamp") or meta.get("export_timestamp") or 0.0)
        last_rev = cls._last_imported_revision_by_dcc.get(t_dcc, 0)
        last_ts = cls._last_imported_ts_by_dcc.get(t_dcc, 0.0)

        if rev > 0 and last_rev > 0:
            return rev > last_rev
        if ts > 0.0:
            return ts > (last_ts + 1e-4)
        return True

    @classmethod
    def mark_received(cls, bridge_id: str, revision: int, origin_session: str):
        if bridge_id:
            cls._recent_updates[str(bridge_id)] = (int(revision), str(origin_session), time.time())

    @classmethod
    def should_suppress_outgoing(cls, bridge_id: str, current_revision: int) -> bool:
        if not bridge_id or str(bridge_id) not in cls._recent_updates:
            return False
        rev, _sess, ts = cls._recent_updates[str(bridge_id)]
        if (time.time() - ts) < cls.SUPPRESSION_WINDOW_SEC and int(current_revision) <= rev:
            return True
        return False


def generate_bridge_id(name_hint: str = "") -> str:
    """Generates a deterministic UUID5 from name_hint or random UUID4."""
    if name_hint:
        return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sisync.bridge.object:{name_hint}"))
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Canonical Socket Framing: 4-byte Big-Endian Unsigned Length + UTF-8 JSON
# ---------------------------------------------------------------------------
def build_protocol_message(
    command: str,
    sender: str,
    target: str,
    payload: Optional[Dict[str, Any]] = None,
    revision: Optional[int] = None,
) -> Dict[str, Any]:
    """Constructs a canonical SiSync protocol message dictionary."""
    rev = int(revision) if revision is not None else get_current_revision()
    now_ts = time.time()
    body_payload = dict(payload or {})
    msg: Dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": SESSION_ID,
        "revision": rev,
        "command": command,
        "sender": sender,
        "target": target,
        "timestamp": now_ts,
        "payload": body_payload,
    }
    # Also expose top-level payload keys for direct listener compatibility
    for k, v in body_payload.items():
        if k not in msg:
            msg[k] = v
    return msg


def encode_message(data: Dict[str, Any]) -> bytes:
    """
    Encodes a dictionary into exact canonical frame:
    4-byte big-endian unsigned integer (payload length) + UTF-8 JSON bytes (no trailing newline).
    """
    if not isinstance(data, dict):
        raise TypeError("Message data must be a dictionary.")
    raw_json = json.dumps(data, ensure_ascii=False).encode("utf-8")
    payload_len = len(raw_json)
    if payload_len > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Payload length {payload_len} exceeds maximum limit ({MAX_PAYLOAD_BYTES} bytes).")
    return HEADER_STRUCT.pack(payload_len) + raw_json


def _recv_exact(sock: socket.socket, num_bytes: int) -> Optional[bytes]:
    """Reads exactly num_bytes from sock, handling partial TCP packets."""
    if num_bytes == 0:
        return b""
    buffer = bytearray()
    while len(buffer) < num_bytes:
        try:
            chunk = sock.recv(num_bytes - len(buffer))
        except (socket.timeout, BlockingIOError, ConnectionResetError, OSError):
            return None
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def decode_message(sock: socket.socket) -> Optional[Dict[str, Any]]:
    """
    Reads and validates a single framed message from sock:
    - 4-byte big-endian length header
    - UTF-8 JSON payload
    Raises ValueError on oversized payload or malformed JSON so callers can log/respond with ERROR.
    Returns None on clean EOF / empty disconnect.
    """
    header_bytes = _recv_exact(sock, HEADER_STRUCT.size)
    if not header_bytes:
        return None

    payload_len = HEADER_STRUCT.unpack(header_bytes)[0]
    if payload_len == 0:
        raise ValueError("Empty payload (length 0) received.")
    if payload_len > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Payload length {payload_len} exceeds safe limit ({MAX_PAYLOAD_BYTES} bytes).")

    body_bytes = _recv_exact(sock, payload_len)
    if body_bytes is None or len(body_bytes) < payload_len:
        raise ValueError(f"Connection closed mid-message (expected {payload_len} bytes).")

    try:
        decoded_str = body_bytes.decode("utf-8")
        obj = json.loads(decoded_str)
    except Exception as e:
        raise ValueError(f"Malformed UTF-8 JSON payload: {e}") from e

    if not isinstance(obj, dict):
        raise ValueError("Decoded JSON payload must be a JSON object.")
    return obj


# ---------------------------------------------------------------------------
# Canonical TCP Client & Server
# ---------------------------------------------------------------------------
class BridgeClient:
    """Canonical TCP Socket Client for Maya <-> Blender communication."""

    @staticmethod
    def send(
        host: str,
        port: int,
        command: str,
        sender: str = "system",
        target: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        wait_for_response: bool = True,
        timeout: float = 2.0,
        revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        dst = target or ("maya" if port == MAYA_BRIDGE_PORT else "blender")
        msg = build_protocol_message(
            command=command,
            sender=sender,
            target=dst,
            payload=payload,
            revision=revision,
        )
        try:
            packed = encode_message(msg)
        except Exception as enc_err:
            log_event("network", command, "encode_error", source=sender, destination=dst, error=str(enc_err))
            return {"status": "error", "command": COMMAND_ERROR, "error": str(enc_err)}

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            s.sendall(packed)

            if not wait_for_response:
                log_event("network", command, "sent", source=sender, destination=dst, revision=msg["revision"])
                return {"status": "sent", "command": command}

            resp = decode_message(s)
            if resp is None:
                err_msg = f"Empty/no response from {host}:{port}"
                log_event("network", command, "error", source=sender, destination=dst, error=err_msg)
                return {"status": "error", "command": COMMAND_ERROR, "error": err_msg}

            log_event("network", command, str(resp.get("status", "ok")), source=sender, destination=dst, revision=msg["revision"])
            return resp

        except ConnectionRefusedError:
            err_msg = f"Connection refused at {host}:{port}"
            log_event("network", command, "connection_refused", source=sender, destination=dst, error=err_msg)
            return {"status": "error", "command": COMMAND_ERROR, "error": err_msg}
        except socket.timeout:
            err_msg = f"Timeout communicating with {host}:{port}"
            log_event("network", command, "timeout", source=sender, destination=dst, error=err_msg)
            return {"status": "error", "command": COMMAND_ERROR, "error": err_msg}
        except Exception as err:
            err_msg = f"Socket error: {err}"
            log_event("network", command, "error", source=sender, destination=dst, error=err_msg)
            return {"status": "error", "command": COMMAND_ERROR, "error": err_msg}
        finally:
            try:
                s.close()
            except Exception:
                pass


class BridgeServer:
    """
    Idempotent, thread-safe TCP Server for Maya (19850) and Blender (19851).
    """

    def __init__(
        self,
        name: str,
        port: int,
        host: str = DEFAULT_HOST,
        message_handler: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    ):
        self.name = name
        self.host = host
        self.port = port
        self.message_handler = message_handler
        self._server_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._is_running = False
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> bool:
        with self._lock:
            if self._is_running and self._server_sock is not None:
                return True
            try:
                self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._server_sock.bind((self.host, self.port))
                self._server_sock.listen(8)
                self._server_sock.settimeout(0.5)
                self._is_running = True

                self._thread = threading.Thread(
                    target=self._listen_loop,
                    name=f"SiSyncBridgeServer-{self.name}-{self.port}",
                    daemon=True,
                )
                self._thread.start()
                log_event("network", "SERVER_START", "success", source=self.name.lower(), extra=f"{self.host}:{self.port}")
                return True
            except Exception as e:
                log_event("network", "SERVER_START", "error", source=self.name.lower(), error=str(e), extra=f"{self.host}:{self.port}")
                self._cleanup()
                return False

    def stop(self):
        with self._lock:
            if not self._is_running and self._server_sock is None:
                return
            self._is_running = False
            self._cleanup()

        if self._thread and self._thread.is_alive():
            if threading.current_thread() != self._thread:
                self._thread.join(timeout=1.5)
        self._thread = None
        log_event("network", "SERVER_STOP", "success", source=self.name.lower(), extra=f"{self.host}:{self.port}")

    def _cleanup(self):
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None

    def _listen_loop(self):
        while self._is_running:
            try:
                srv = self._server_sock
                if srv is None:
                    break
                client_sock, client_addr = srv.accept()
            except socket.timeout:
                continue
            except (OSError, ValueError):
                break
            except Exception as e:
                if self._is_running:
                    log_event("network", "ACCEPT", "error", source=self.name.lower(), error=str(e))
                continue

            worker = threading.Thread(
                target=self._handle_client,
                args=(client_sock, client_addr),
                daemon=True,
            )
            worker.start()

    def _handle_client(self, sock: socket.socket, addr: Tuple[str, int]):
        sock.settimeout(3.5)
        try:
            try:
                msg = decode_message(sock)
            except ValueError as val_err:
                err_resp = {
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": SESSION_ID,
                    "status": "error",
                    "command": COMMAND_ERROR,
                    "error": str(val_err),
                    "target": self.name.lower(),
                    "timestamp": time.time(),
                }
                log_event("network", "DECODE", "error", destination=self.name.lower(), error=str(val_err))
                try:
                    sock.sendall(encode_message(err_resp))
                except Exception:
                    pass
                return

            if msg is None:
                return

            raw_cmd = str(msg.get("command", "")).upper()
            sender = str(msg.get("sender", "unknown"))
            rev = int(msg.get("revision", 0))
            req_ver = str(msg.get("protocol_version", PROTOCOL_VERSION))

            if req_ver not in ("1.0", "2.3.0", ""):
                resp = {
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": SESSION_ID,
                    "status": "error",
                    "command": COMMAND_ERROR,
                    "error": f"Unsupported protocol_version: {req_ver}",
                    "target": self.name.lower(),
                    "timestamp": time.time(),
                }
            elif raw_cmd == COMMAND_PING:
                resp = {
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": SESSION_ID,
                    "status": "success",
                    "command": COMMAND_PONG,
                    "server": self.name,
                    "target": self.name.lower(),
                    "port": self.port,
                    "timestamp": time.time(),
                }
            elif raw_cmd == COMMAND_HELLO:
                resp = {
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": SESSION_ID,
                    "status": "success",
                    "command": COMMAND_HELLO,
                    "server": self.name,
                    "target": self.name.lower(),
                    "port": self.port,
                    "timestamp": time.time(),
                }
            elif raw_cmd == COMMAND_GET_STATUS:
                resp = {
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": SESSION_ID,
                    "status": "success",
                    "command": COMMAND_STATUS_REPORT,
                    "server": self.name,
                    "target": self.name.lower(),
                    "port": self.port,
                    "timestamp": time.time(),
                }
                if self.message_handler:
                    try:
                        extra_status = self.message_handler(msg)
                        if isinstance(extra_status, dict):
                            for k, v in extra_status.items():
                                if k != "status" or v == "error":
                                    resp[k] = v
                    except Exception:
                        pass
            elif raw_cmd not in VALID_COMMANDS:
                resp = {
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": SESSION_ID,
                    "status": "error",
                    "command": COMMAND_ERROR,
                    "error": f"Unsupported command: {raw_cmd}",
                    "target": self.name.lower(),
                    "timestamp": time.time(),
                }
            elif self.message_handler:
                try:
                    resp = self.message_handler(msg)
                    if not isinstance(resp, dict):
                        resp = {"status": "ok", "command": COMMAND_ACK, "target": self.name.lower()}
                except Exception as handler_err:
                    log_event("network", raw_cmd, "handler_error", source=sender, destination=self.name.lower(), revision=rev, error=str(handler_err))
                    resp = {
                        "protocol_version": PROTOCOL_VERSION,
                        "session_id": SESSION_ID,
                        "status": "error",
                        "command": COMMAND_ERROR,
                        "error": str(handler_err),
                        "target": self.name.lower(),
                    }
            else:
                resp = {
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": SESSION_ID,
                    "status": "ok",
                    "command": COMMAND_ACK,
                    "target": self.name.lower(),
                }

            log_event("network", raw_cmd, str(resp.get("status", "ok")), source=sender, destination=self.name.lower(), revision=rev)
            sock.sendall(encode_message(resp))

        except Exception as e:
            log_event("network", "CLIENT_HANDLER", "error", destination=self.name.lower(), error=str(e))
        finally:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Coordinate System & Transform Conversion Mathematics (Maya Y-Up <-> Blender Z-Up)
# ---------------------------------------------------------------------------
class CoordinateBasis:
    """
    Canonical Coordinate Conversion Mathematics between Maya (Y-Up, cm) and Blender (Z-Up, m).
    - Maya Y-Up (+X Right, +Y Up, +Z Forward) -> Blender Z-Up (+X Right, +Z Up, -Y Forward):
        x_bl = x_maya * scale
        y_bl = -z_maya * scale
        z_bl = y_maya * scale
    - Blender Z-Up (+X Right, +Z Up, -Y Forward) -> Maya Y-Up (+X Right, +Y Up, +Z Forward):
        x_maya = x_bl / scale
        y_maya = z_bl / scale
        z_maya = -y_bl / scale
    Notice that B_to_M(M_to_B(p)) == p identically!
    """

    BLENDER_DEFAULT = {
        "dcc": "blender",
        "right": "+X",
        "up": "+Z",
        "forward": "-Y",
        "handedness": "RIGHT",
        "unit": "m",
        "unit_scale_to_meters": 1.0,
    }

    MAYA_Y_UP = {
        "dcc": "maya",
        "right": "+X",
        "up": "+Y",
        "forward": "+Z",
        "handedness": "RIGHT",
        "unit": "cm",
        "unit_scale_to_meters": 0.01,
    }

    @staticmethod
    def determinant_3x3(m: List[List[float]]) -> float:
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    @classmethod
    def get_basis_change_matrix_3x3(cls, source_basis: Dict[str, Any], target_basis: Dict[str, Any]) -> Tuple[List[List[float]], float, float]:
        src_up = str(source_basis.get("up", "+Y")).upper()
        dst_up = str(target_basis.get("up", "+Z")).upper()
        src_scale = float(source_basis.get("unit_scale_to_meters", 0.01))
        dst_scale = float(target_basis.get("unit_scale_to_meters", 1.0))
        unit_factor = src_scale / dst_scale if dst_scale != 0 else 1.0

        if src_up == dst_up:
            C = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        elif "Y" in src_up and "Z" in dst_up:
            C = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
        else:
            C = [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]]

        det = cls.determinant_3x3(C)
        return C, det, unit_factor

    @staticmethod
    def blender_point_to_maya(
        pt: Tuple[float, float, float],
        up_axis: str = "Y",
        unit_scale: float = 100.0,
        flip_x: bool = False,
        flip_y: bool = False,
        flip_z: bool = False,
    ) -> Tuple[float, float, float]:
        fx = -1.0 if flip_x else 1.0
        fy = -1.0 if flip_y else 1.0
        fz = -1.0 if flip_z else 1.0
        x = pt[0] * fx * unit_scale
        y = pt[1] * fy * unit_scale
        z = pt[2] * fz * unit_scale
        if up_axis.upper() == "Y":
            return (x, z, -y)
        return (x, y, z)

    @staticmethod
    def maya_point_to_blender(
        pt: Tuple[float, float, float],
        up_axis: str = "Y",
        unit_scale: float = 0.01,
        flip_x: bool = False,
        flip_y: bool = False,
        flip_z: bool = False,
    ) -> Tuple[float, float, float]:
        fx = -1.0 if flip_x else 1.0
        fy = -1.0 if flip_y else 1.0
        fz = -1.0 if flip_z else 1.0
        if up_axis.upper() == "Y":
            bx = pt[0]
            by = -pt[2]
            bz = pt[1]
        else:
            bx, by, bz = pt[0], pt[1], pt[2]
        return (bx * fx * unit_scale, by * fy * unit_scale, bz * fz * unit_scale)
