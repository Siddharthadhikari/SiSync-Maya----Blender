# Changelog — SiSync

All notable changes to **SiSync (Maya 2026 ⇄ Blender 5.x Bridge)** are documented in this file.

## [1.0.0] — Phase 1 Final Release (2026-09-26)

### Added
- **Unified Shared Core (`sisync_bridge_core.py`)**:
  - Consolidated `BridgeServer`, `BridgeClient`, 4-byte big-endian (`>I`) socket framing, 32 MB payload guard, `CoordinateBasis`, `EchoSuppressor`, `sisync_metadata.json` manifest IO, and structured logging (`sisync_bridge/logs/{blender,maya,network}.log`).
- **Stable Object Identity (`sisync_bridge_id`)**:
  - Deterministic UUID5 object identity stamped onto Blender objects (`obj["sisync_bridge_id"]`) and Maya transform nodes (`node.sisync_bridge_id`), allowing renamed and remeshed objects to update in-place without duplicating.
- **Sculpt, Edit, Multires & Remesh Support**:
  - Automatic PBVH & `update_from_editmode()` flushing in Blender (`sync_mesh.py`).
  - Automatic Maya `.pnts` vertex tweak reset prior to `outMesh -> inMesh` topology transfer (`sisync_maya.py`).
  - Preservation of Blender material slots across Maya topology remeshing.
- **Clean Lifecycle & Uninstaller**:
  - Idempotent `start_server()`, `stop_server()`, `_ensure_shelf()`, and `uninstall()` in Maya (`sisync_maya.py` and `drag_and_drop_install.py`).
  - Guaranteed `try / finally` restoration of temporary export objects, original object names, selection, and viewport modes in both Maya and Blender.
- **Automated Test Suites**:
  - `tests/test_bridge_core.py` (protocol, framing, transforms, echo suppression).
  - `tests/test_blender_headless.py` (Blender 5.2 headless validation).
  - `tests/test_maya_headless.py` (Maya 2026 `mayapy.exe` headless validation).
