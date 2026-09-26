# SiSync Phase 1 — Known Limitations & Scope Boundary (`docs/LIMITATIONS.md`)

## 1. Rigged (SkinCluster) Topology Replacement in Maya

- When a target mesh in Maya has an active **`skinCluster`** (rigged character mesh) and the incoming mesh from Blender has a **different vertex count** (e.g., after Remeshing or adding/deleting vertices), `sisync_maya.pull_from_blender()` intentionally skips replacing the topology (`Skipped topology change on rigged mesh`) to prevent destroying Maya skin weights.
- **Supported**: Same-vertex-count sculpt deformations on rigged meshes update in-place via `MFnMesh.setPoints` while preserving the `skinCluster`.
- **Supported**: Unrigged meshes support arbitrary topology replacement (Remesh, Subdivision, Extrude, Retopology) in both directions (`Blender → Maya` and `Maya → Blender`).

---

## 2. Procedural Shader Networks (Phase 1 vs. Phase 2)

- **Phase 1 Scope**:
  - Preserves existing Blender material assignments on `target_obj` when updating remeshed topology from Maya.
  - Selectively refreshes file-based PBR texture maps (`Base Color`, `Roughness`, `Metallic`, `Normal`, `Bump`, `Alpha`) residing in `sisync_bridge/textures/`.
- **Not in Phase 1**:
  - Phase 1 does not cross-compile procedural Blender Cycles/EEVEE shader node graphs into Maya Arnold (`aiStandardSurface`) shader networks or vice versa.
  - Full cross-DCC Vertex Color (`CPV`) and portable PBR material manifest synthesis are scheduled for **SiSync Phase 2**.
