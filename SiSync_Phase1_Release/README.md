# SiSync — Maya 2026 ⇄ Blender 5.x Production Bridge (Phase 1)

**SiSync** is a bidirectional geometry, transform, and PBR texture reload bridge connecting **Autodesk Maya 2026** and **Blender 5.2 LTS**.

Created by **Siddhartha Adhikari**.

---

## Phase 1 Capabilities

- **Bidirectional FBX Mesh Synchronization**:
  - One-click **Export to Maya** and **Import from Maya** in Blender (`View3D > Sidebar (N) > SiSync` and 3D Viewport Header).
  - One-click **Export to Blender** and **Import from Blender** in Maya (`SiSync` Window and `SiSync` Shelf).
  - Automatic live background synchronization over localhost TCP sockets (`19850` Maya ⇄ `19851` Blender) and Maya's main-thread `commandPort` (`19852`).
- **Canonical Coordinate & Scale Conversion**:
  - Automatic conversion between **Maya (`Y-Up`, centimeters)** and **Blender (`Z-Up`, meters)** (`det(C) = +1.0`).
  - Support for **Auto Scene Units (`m ⇄ cm`)** and **Manual Scale Factor**.
  - Reciprocal **Flip X / Flip Y / Flip Z** toggles with automatic surface winding/normal correction on negative determinants.
  - **Freeze Location to `(0, 0, 0)`** and **Freeze Rotation to `(0, 0, 0)`** (baking transforms into mesh geometry while setting object origin/rotation to identity).
  - Full support for parented meshes (`Parent -> Child` hierarchies).
- **Stable Object Identity (`sisync_bridge_id`)**:
  - Deterministic UUID5 `sisync_bridge_id` stored on both Blender objects (`obj["sisync_bridge_id"]`) and Maya transform nodes (`node.sisync_bridge_id`).
  - Survives object renames and remeshing across round-trips without creating duplicate meshes.
- **Sculpt, Edit & Remesh Support**:
  - Automatically flushes Blender **Sculpt Mode** (PBVH), **Edit Mode**, and **Multires** levels prior to export.
  - Clears Maya vertex tweaks (`.pnts`) prior to `outMesh -> inMesh` topology replacement so remeshed geometry imports cleanly without vertex spikes.
  - Preserves existing Blender material assignments when updating remeshed topology from Maya.
- **Deterministic Echo Suppression**:
  - Uses `(session_id, revision, source, destination)` and import guards to prevent `Blender → Maya → Blender` infinite bounce loops.

---

## Roadmap & Phase Boundary

```text
SiSync Phase 1 (Current Release — v1.0.0)
Foundation / Mesh, Transform & Live TCP Bridge
        ↓
SiSync Phase 2 (Next Planned Release)
Vertex Color (CPV) Transfer + Cross-DCC Material Manifest & PBR Map Sync
        ↓
Future Phases
Advanced Shader Network & Rigging Synchronization
```

---

## Documentation Index

- [Architecture Overview](docs/ARCHITECTURE.md)
- [Network & Framing Protocol](docs/PROTOCOL.md)
- [Installation, Update & Uninstall Guide](docs/INSTALLATION.md)
- [Automated & Live Validation Suite](docs/TESTING.md)
- [Phase 1 Known Limitations](docs/LIMITATIONS.md)
- [Changelog](CHANGELOG.md)
- [License](LICENSE)
