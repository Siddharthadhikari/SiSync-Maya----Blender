# SiSync — Real-Time Blender ⇄ Maya Production Bridge

SiSync is a non-destructive, real-time bi-directional production bridge for **Blender (4.2+ / 5.x)** and **Autodesk Maya (2024–2026)**. It transfers production character geometry, hierarchy, transforms, Subdivision Surface states, non-destructive Shape Keys / Blend Shapes, UVs, materials (`sisync_material_id`), and multiple vertex color attributes (`BYTE_COLOR` / `FLOAT_COLOR`) without polluting the source scene.

---

## Repository Structure

```text
SiSync_GitHub_Release_Staging/
├── blender/
│   ├── bl_sisync/                # Blender Extension / Add-on source package
│   │   ├── __init__.py
│   │   ├── blender_manifest.toml
│   │   ├── network.py
│   │   ├── operators.py
│   │   ├── preferences.py
│   │   ├── server.py
│   │   ├── sisync_bridge_core.py
│   │   ├── sync_maps.py
│   │   ├── sync_mesh.py
│   │   └── ui.py
│   └── bl_sisync.zip             # Ready-to-install Blender Add-on / Extension archive
├── maya/
│   ├── drag_and_drop_install.py  # Drag-and-drop installer for Maya viewport
│   ├── sisync_bridge_core.py     # Shared protocol & metadata core
│   └── sisync_maya.py            # Maya shelf UI, socket server & FBX/JSON sync engine
├── docs/                         # Automated production validation & edge-case telemetry
│   ├── full_blendshape_accuracy_audit.json
│   ├── phase1_edge_audit.json
│   ├── phase2_hierarchy_vcol_validation_report.json
│   ├── phase2_validation_report.json
│   └── shapekey_export_behavior_validation.json
└── README.md
```

---

## Key Features

1. **Non-Destructive Shape Key / Blend Shape Export**:
   - **Export Blend Shapes = OFF**: Exports the mesh in its exact current viewport Shape Key state (`Basis + active shape keys`) with zero separate BlendShape target objects created in either Blender or Maya.
   - **Export Blend Shapes = ON**: Evaluates the neutral `Basis` mesh for the character hierarchy and evaluates each individual Shape Key target (`1.0` isolated) directly from the dependency graph, exporting them under a dedicated top-level `|BlendShapes|<ShapeKeyName>` group in Maya while leaving the Blender scene 100% untouched (`0` leftover helper meshes or collections).
2. **Subdivision Surface & Armature Transform Invariance**:
   - Evaluates Subdivision Surface modifiers in unposed rest space while preserving exact world/local hierarchy transforms (`0.0 cm` drift).
3. **Driver & Keyframe State Preservation**:
   - Automatically snapshots, mutes during isolated Basis/Target evaluations, and restores all Shape Key and Modifier drivers and FCurves (including Blender 5.x layered/slotted Action channelbags) inside a guaranteed `finally` block.
4. **Hierarchy, Materials & Multi-Layer Vertex Colors**:
   - Preserves full DAG hierarchy (`|Root|Group|Mesh`), material slot order, per-face material assignments (`sisync_material_id`), and multiple vertex color layers across `Blender ⇄ Maya`.

---

## Installation

### Blender (4.2+ / 5.x)
1. Open Blender and go to **Edit → Preferences → Add-ons** (or **Get Extensions**).
2. Click the top-right dropdown arrow and choose **Install from Disk...**.
3. Select `blender/bl_sisync.zip`.
4. Enable **SiSync Bridge** (`bl_sisync`) and open the **3D Viewport → Sidebar (`N`) → SiSync** tab.

### Autodesk Maya (2024–2026)
1. Copy `maya/sisync_maya.py` and `maya/sisync_bridge_core.py` to your Maya scripts directory (e.g., `Documents/maya/2026/scripts/`), or drag `maya/drag_and_drop_install.py` directly into the Maya viewport.
2. Run in a Python tab in the Script Editor:
   ```python
   import sisync_maya
   sisync_maya.show_ui()
   ```
