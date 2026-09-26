# SiSync Phase 1 — Validation & Testing Guide (`docs/TESTING.md`)

## 1. Core Protocol, Framing & Transform Math Suite

Run with standard Python 3:
```powershell
python tests/test_bridge_core.py
```
Validates:
- 4-byte big-endian (`>I`) framing with ASCII, Unicode, and 500-element arrays.
- Partial 1-byte TCP packet reassembly and concatenated multi-packet frames.
- Empty payload (`0` bytes), oversized payload (`>32 MB`), malformed JSON, and mid-stream disconnect handling.
- Idempotent `BridgeServer` `start()` / `stop()`, `PING`/`PONG`, `HELLO`, `GET_STATUS`, `SYNC_MESH`, and invalid command rejection.
- Coordinate conversion (`Maya Y-Up cm ⇄ Blender Z-Up m`), scales (`1, 10, 100, 1000`), and all 8 `Flip X / Y / Z` combinations with `< 1e-6` error and `det(C) = +1.0`.
- Deterministic `EchoSuppressor` loop prevention.

---

## 2. Blender 5.2 Headless Test Suite

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" -b --factory-startup --python tests/test_blender_headless.py
```
Validates:
- Add-on registration/unregistration (`send_mesh`, `pull_mesh`, `refresh_maps`, `toggle_server`, `ping_maya`).
- FBX export & canonical metadata generation (`blender_to_maya.fbx` + `sisync_metadata.json`).
- In-place vertex coordinate updates (`_update_vertices_in_place`).
- Topology relinking (`_relink_mesh_data`) while preserving object transforms and materials.
- PBR material texture map reloading (`refresh_pbr_maps`).
- Live Blender `BridgeServer` lifecycle and `PING`/`PONG`.

---

## 3. Autodesk Maya 2026 Headless Test Suite

```powershell
& "C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe" tests/test_maya_headless.py
```
Validates:
- Headless Maya initialization (`maya.standalone`) and `fbxmaya` plugin loading.
- Mesh export (`maya_to_blender.fbx` + `sisync_metadata.json`).
- File texture node refreshing (`MayaTextureManager.refresh_material_maps`).
- Live Maya `BridgeServer` lifecycle and `PING`/`PONG`.
