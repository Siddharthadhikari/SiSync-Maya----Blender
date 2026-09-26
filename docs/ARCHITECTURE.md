# SiSync Phase 1 — System Architecture (`docs/ARCHITECTURE.md`)

## 1. High-Level Topology

```text
Autodesk Maya 2026                                      Blender 5.2 LTS
      │                                                       │
      ▼                                                       ▼
Maya Adapter (maya/sisync_maya.py)                      Blender Adapter (blender/bl_sisync/)
 • Maya UI & Single 'SiSync' Shelf                       • N-Panel & Header UI (ui.py)
 • OpenMaya 2.0 World/Local Evaluation                   • Sculpt PBVH & Depsgraph Flush
 • .pnts Tweak Reset & outMesh->inMesh                   • C-Level Mesh.transform() & Material Keep
 • Qt QTimer + executeDeferred Main-Thread               • bpy.app.timers + Queue Main-Thread
 • Dedicated commandPort (:19852 / :7001)                • Selective PBR Texture Reloader
      │                                                       │
      ▼                                                       ▼
Shared Core (sisync_bridge_core.py)                     Shared Core (sisync_bridge_core.py)
 • BridgeServer (:19850) / BridgeClient                  • BridgeServer (:19851) / BridgeClient
 • 4-Byte Big-Endian Framing (>I + UTF-8 JSON)           • 4-Byte Big-Endian Framing (>I + UTF-8 JSON)
 • Canonical Metadata (sisync_metadata.json)             • Canonical Metadata (sisync_metadata.json)
 • Deterministic EchoSuppressor & UUID5 bridge_id        • Deterministic EchoSuppressor & UUID5 bridge_id
 • Structured Logs (sisync_bridge/logs/*.log)            • Structured Logs (sisync_bridge/logs/*.log)
      │                                                       │
      └─────────────── Localhost TCP Framing ─────────────────┘
                    (19850 ⇄ 19851 + FBX Staging)
```

---

## 2. Port Architecture

| Port | Owner | Purpose |
| :--- | :--- | :--- |
| `19850` | `sisync_maya.py` (`BridgeServer`) | Canonical Maya SiSync TCP listener (`COMMAND_SYNC_MESH`, `PING`, `HELLO`, `GET_STATUS`, `REFRESH_MAPS`). |
| `19851` | `bl_sisync/server.py` (`BridgeServer`) | Canonical Blender SiSync TCP listener (`COMMAND_SYNC_MESH`, `PING`, `HELLO`, `GET_STATUS`, `REFRESH_MAPS`). |
| `19852` | Autodesk Maya `commandPort` | Dedicated Maya Python commandPort used for immediate unfocused main-thread invocation of `sisync_maya.pull_from_blender` and automated validation. |
| `7001` | Autodesk Maya `commandPort` | Legacy / external MCP automation fallback port. |

---

## 3. Thread Safety Model

- **Blender (`bl_sisync/server.py`)**:
  - Background socket threads (`BridgeServer`) never touch `bpy.data` or `bpy.ops`.
  - Incoming network commands are pushed onto a thread-safe `queue.Queue` (`_TASK_QUEUE`) and executed on Blender's main thread via `bpy.app.timers` (`_main_thread_timer`).
- **Maya (`maya/sisync_maya.py`)**:
  - Background socket threads (`BridgeServer`) never modify the Maya DG/DAG directly.
  - Incoming sync tasks are queued into `_PENDING_SYNC_PATHS` and dispatched onto Maya's main thread via `maya.utils.executeDeferred` and a main-thread Qt `QTimer` (`_auto_watch_tick`).
