# SiSync User & Architecture Decisions (`USER_DECISIONS.md`)

Recorded automatically from live workstation inspection and user project configuration.

## 1. Environment & DCC Versions
- **Operating System:** Windows (x64)
- **Blender Version:** Blender 5.2.0 LTS
- **Maya Version:** Autodesk Maya 2026.2 (Python 3 / OpenMaya API 2.0)
- **Sync Mode:** Both (Live Socket Trigger + Manual Push/Pull with Echo Suppression)
- **MCP Role:** Verification, scene inspection, and high-level bridge orchestration (`connect`, `inspect_scene`, `list_objects`, `push_selection`, `pull_selection`)

## 2. Canonical Coordinate & Unit Basis
- **Canonical Bridge Basis:**
  - `right`: `+X` (Shared horizontal anchor between Maya and Blender)
  - `up`: `+Z`
  - `forward`: `-Y`
  - `handedness`: `RIGHT` ($\det(C) = +1.0$)
  - `unit`: `METERS` (`1.0 m = 100.0 cm`)
- **Maya Adapter Default Basis (`Y-Up`):**
  - `right`: `+X`, `up`: `+Y`, `forward`: `+Z`, `handedness`: `RIGHT`, `unit`: `cm` (`0.01 m`)
  - Dynamically detects `cmds.upAxis(q=True, axis=True)` (`y` or `z`) and `cmds.currentUnit(q=True, linear=True)`.
- **Blender Adapter Default Basis (`Z-Up`):**
  - `right`: `+X`, `up`: `+Z`, `forward`: `-Y`, `handedness`: `RIGHT`, `unit`: `METERS` (`1.0 m`)

## 3. Identity, Geometry, Materials & Transport
- **Object Identity:** Stable `bridge_id` (UUID4) stored on Blender (`obj["sisync_bridge_id"]`) and Maya (`node.sisync_bridge_id`) plus monotonic `sisync_revision` integer and `session_id` for echo suppression.
- **Mesh & Vertex Colors:** Preserves vertex order for in-place sculpting (`Lio01` character pipeline), UVs, normals, winding validation ($\det = +1$), and explicit vertex color metadata (`name`, `domain`, `data_type`, `color_space="sRGB"`).
- **Material Depth:** Portable PBR manifest (`base_color`, `metallic`, `roughness`, `specular_ior`, `emission`, `opacity`, `normal_map`) + DCC-native material preservation (`Keep Scene Material` / `Base Material + CPV` / `Portable PBR`).
- **Transport & Interchange:** Localhost TCP framed JSON socket (`127.0.0.1:19850` Maya, `127.0.0.1:19851` Blender) with `HELLO`, `PING`/`PONG`, `SYNC_MESH`, `OBJECT_UPDATE`, `ACK`/`ERROR`, backed by FBX (primary hierarchy/mesh payload) and OBJ (static mesh/vertex color fallback).
