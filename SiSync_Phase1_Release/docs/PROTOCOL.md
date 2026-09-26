# SiSync Phase 1 — Wire Protocol & Metadata Specification (`docs/PROTOCOL.md`)

## 1. Socket Framing

Every TCP message sent over `127.0.0.1:19850` (Maya) or `127.0.0.1:19851` (Blender) uses a deterministic length-prefixed binary frame:

```text
+------------------------------------------+------------------------------------------+
| 4-byte Big-Endian Unsigned Length (>I)   | UTF-8 Encoded JSON Object Payload        |
+------------------------------------------+------------------------------------------+
```

- **No trailing newline** (`\n`) is appended after the JSON bytes.
- **Safety Limit**: Payloads exceeding `MAX_PAYLOAD_BYTES` (`32 MB`) or `0` bytes are rejected with `ValueError` and logged to `sisync_bridge/logs/network.log`.

---

## 2. Protocol Message Schema

```json
{
  "protocol_version": "1.0",
  "session_id": "8fa31c90",
  "revision": 42,
  "command": "SYNC_MESH",
  "sender": "blender",
  "target": "maya",
  "timestamp": 1790435000.123,
  "payload": {
    "file_path": "C:/Users/.../AppData/Local/Temp/sisync_bridge/blender_to_maya.fbx",
    "fbx_path": "C:/Users/.../AppData/Local/Temp/sisync_bridge/blender_to_maya.fbx",
    "format": "fbx",
    "revision": 42
  }
}
```

### Supported Commands
- `HELLO` — Initial capability & version handshake
- `PING` / `PONG` — Liveness check (`status: "success"`)
- `GET_STATUS` / `STATUS_REPORT` — Returns active server port, target DCC, and status
- `SYNC_MESH` — Triggers main-thread FBX import on the target DCC
- `REFRESH_MAPS` — Triggers PBR texture map reload from `sisync_bridge/textures/`
- `SCENE_SNAPSHOT`, `OBJECT_CREATE`, `OBJECT_UPDATE`, `OBJECT_DELETE`, `ASSET_PUSH`
- `ACK` / `ERROR` — Explicit acknowledgment or structured error response

---

## 3. Canonical Staging Directory & Metadata (`sisync_metadata.json`)

Default exchange directory: `%TEMP%/sisync_bridge/` (configurable in preferences or via `SISYNC_BRIDGE_DIR`).

- `blender_to_maya.fbx` — Authoritative FBX payload exported by Blender for Maya
- `maya_to_blender.fbx` — Authoritative FBX payload exported by Maya for Blender
- `sisync_metadata.json` — Authoritative metadata manifest (mirrored to `SiSync_Exchange.json`)
- `textures/` — Shared PBR texture map directory
- `logs/` — Structured log files (`blender.log`, `maya.log`, `network.log`)
