#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync — Blender Adapter FBX Export / Import & Coordinate Matrix Engine
- Supports Object Mode, Edit Mode, Sculpt Mode (PBVH flush), and Multires
- Uses canonical sisync_bridge_core metadata, direction-specific FBX files, bridge_id tracking, and EchoSuppressor
- Guarantees full scene state restoration via try / finally
"""

import os
import re
import json
import math
import time
import shutil
from typing import List, Optional, Dict, Any
import bpy
import mathutils

from .preferences import get_preferences, get_exchange_dir
from . import sisync_bridge_core as core

EXCHANGE_FBX_NAME = core.EXCHANGE_FBX_LEGACY
EXCHANGE_META_NAME = core.METADATA_FILENAME

LAST_EXPORT_TIMESTAMP = 0.0
LAST_IMPORT_TIMESTAMP = 0.0


def get_exchange_fbx_path(context=None, direction: str = "b2m") -> str:
    ex_dir = get_exchange_dir(context)
    if direction == "m2b":
        return core.get_maya_to_blender_fbx(ex_dir)
    elif direction == "b2m":
        return core.get_blender_to_maya_fbx(ex_dir)
    return core.get_legacy_exchange_fbx(ex_dir)


def get_exchange_meta_path(context=None) -> str:
    ex_dir = get_exchange_dir(context)
    return core.get_metadata_file_path(ex_dir)


def _build_gob_remap_matrix(prefs) -> mathutils.Matrix:
    if not prefs:
        return mathutils.Matrix.Identity(4)

    sx = -1.0 if prefs.flip_x_axis else 1.0
    sy = -1.0 if prefs.flip_y_axis else 1.0
    sz = -1.0 if prefs.flip_z_axis else 1.0
    s_manual = float(prefs.manual_scale) if prefs.scale_mode == "MANUAL" else 1.0

    mat = mathutils.Matrix.Identity(4)
    mat[0][0] = sx * s_manual
    mat[1][1] = sy * s_manual
    mat[2][2] = sz * s_manual
    return mat


def _strip_duplicate_suffix(name: str) -> str:
    if ":" in name:
        name = name.split(":")[-1]
    n = re.sub(r"__sisync_(?:dup|orig|target)\d*$", "", name)
    m = re.match(r"^(.*?)(?:\.\d{3,})?$", n)
    return m.group(1) if m else n


def _sanitize_name(name: str) -> str:
    n = _strip_duplicate_suffix(name)
    n = re.sub(r"(?i)\.fbx$", "", n)
    n = re.sub(r"(?i)\.obj$", "", n)
    n = re.sub(r"[^a-zA-Z0-9_]", "_", n)
    if n and n[0].isdigit():
        n = "m_" + n
    return n


def _find_matching_blender_object(
    base_name: str,
    bridge_id: str,
    imp_obj: bpy.types.Object,
    single_mesh_fallback: bool = False,
) -> Optional[bpy.types.Object]:
    """
    Finds the existing Blender target mesh object using:
    1. Stable sisync_bridge_id custom property (survives object renames in Maya!)
    2. Exact name match
    3. Sanitized / stripped-suffix name match
    4. Prefix/similarity fallback when exporting a single mesh (e.g. Lio01_L_eyes_eye01_ge -> Lio01_L_eyes_eye01_geo)
    """
    # 1. Match by stable bridge_id
    if bridge_id:
        for o in bpy.data.objects:
            if o != imp_obj and o.type == 'MESH' and str(o.get("sisync_bridge_id", "")) == str(bridge_id):
                return o

    # 2. Exact name match
    cand = bpy.data.objects.get(base_name)
    if cand and cand != imp_obj and cand.type == 'MESH':
        return cand

    # 3. Sanitized name match
    san_target = _sanitize_name(base_name)
    for o in bpy.data.objects:
        if o != imp_obj and o.type == 'MESH':
            if _sanitize_name(o.name) == san_target:
                return o

    # 4. Single-mesh active/prefix fallback (handles truncated/tweaked names on single-mesh sync)
    if single_mesh_fallback:
        act = bpy.context.view_layer.objects.active
        if act and act != imp_obj and act.type == 'MESH':
            san_act = _sanitize_name(act.name)
            if san_act.startswith(san_target) or san_target.startswith(san_act):
                return act
        for o in bpy.data.objects:
            if o != imp_obj and o.type == 'MESH' and len(san_target) >= 6:
                san_o = _sanitize_name(o.name)
                if san_o.startswith(san_target) or san_target.startswith(san_o):
                    return o

    return None


class BlenderMeshSync:
    """Production FBX Exporter & Importer between Blender 5.x and Maya 2026."""

    @classmethod
    def export_to_maya(cls, context) -> Optional[str]:
        """
        Exports selected or actively sculpted Blender mesh objects to blender_to_maya.fbx
        (and mirrors to SiSync_Exchange.fbx).
        Always restores temporary objects, names, selection, and mode in a finally block.
        """
        global LAST_EXPORT_TIMESTAMP

        active_obj = (
            getattr(context, "sculpt_object", None)
            or getattr(context, "active_object", None)
            or bpy.context.view_layer.objects.active
        )
        previous_mode = active_obj.mode if active_obj else 'OBJECT'
        initial_selection = [o for o in context.selected_objects]

        # Flush Sculpt Mode PBVH / Edit Mode changes into mesh datablock
        if active_obj and active_obj.mode != 'OBJECT':
            flushed = False
            try:
                for win in bpy.context.window_manager.windows:
                    for area in win.screen.areas:
                        if area.type == 'VIEW_3D':
                            win_reg = next((r for r in area.regions if r.type == 'WINDOW'), area.regions[0])
                            with bpy.context.temp_override(
                                window=win,
                                area=area,
                                region=win_reg,
                                active_object=active_obj,
                                object=active_obj,
                            ):
                                bpy.ops.object.mode_set(mode='OBJECT')
                                flushed = True
                            break
                    if flushed:
                        break
            except Exception:
                pass
            if not flushed:
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except Exception:
                    pass
            try:
                active_obj.update_from_editmode()
            except Exception:
                pass

        selected_meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if active_obj and active_obj.type == 'MESH' and active_obj not in selected_meshes:
            selected_meshes.insert(0, active_obj)

        if not selected_meshes:
            return None

        context.view_layer.update()
        prefs = get_preferences(context)
        ex_dir = get_exchange_dir(context)
        b2m_fbx = core.get_blender_to_maya_fbx(ex_dir)
        legacy_fbx = core.get_legacy_exchange_fbx(ex_dir)

        remap_mat = _build_gob_remap_matrix(prefs)
        do_freeze_loc = bool(prefs.freeze_location) if prefs else False
        do_freeze_rot = bool(prefs.freeze_rotation) if prefs else True
        maya_up = prefs.maya_up_axis if prefs else "Y"

        temp_records = []
        meta_objects: Dict[str, Any] = {}
        rev = core.next_revision()

        try:
            for obj in selected_meshes:
                safe = _sanitize_name(obj.name)
                if obj.name != safe:
                    try:
                        obj.name = safe
                    except Exception:
                        pass
                orig_name = obj.name

                # Ensure stable bridge_id is stamped on the Blender object
                b_id = str(obj.get("sisync_bridge_id") or "")
                if not b_id:
                    b_id = core.generate_bridge_id(orig_name)
                    obj["sisync_bridge_id"] = b_id
                obj["sisync_revision"] = rev

                world_xform = remap_mat @ obj.matrix_world
                wloc = world_xform.to_translation()
                wrot_quat = obj.matrix_world.to_quaternion()
                wrot_deg = [math.degrees(a) for a in wrot_quat.to_euler('XYZ')]
                has_parent = obj.parent is not None

                # Ensure any Multires modifier exports the sculpted level
                for mod in obj.modifiers:
                    if mod.type == 'MULTIRES' and hasattr(mod, "sculpt_levels"):
                        if mod.levels < mod.sculpt_levels:
                            mod.levels = mod.sculpt_levels

                obj.data.update()
                obj.data.update_tag()
                obj.update_tag()
                context.view_layer.update()

                # Check if only Armature modifier is present so we don't double-bake armature or lose modifiers
                non_armature_mods = [m for m in obj.modifiers if m.type != 'ARMATURE']
                if len(non_armature_mods) == 0:
                    temp_mesh = obj.data.copy()
                else:
                    depsgraph = context.evaluated_depsgraph_get()
                    obj_eval = obj.evaluated_get(depsgraph)
                    temp_mesh = bpy.data.meshes.new_from_object(obj_eval)

                obj.name = f"{orig_name}__sisync_orig"
                temp_obj = bpy.data.objects.new(orig_name, temp_mesh)
                context.collection.objects.link(temp_obj)

                # Respect Freeze Location and Freeze Rotation accurately (even for parented objects!)
                target_loc = mathutils.Vector((0.0, 0.0, 0.0)) if do_freeze_loc else wloc
                if not do_freeze_rot:
                    target_rot_mat = wrot_quat.to_matrix().to_4x4()
                else:
                    target_rot_mat = mathutils.Matrix.Identity(4)

                temp_obj_mat = mathutils.Matrix.Translation(target_loc) @ target_rot_mat
                temp_obj.matrix_world = temp_obj_mat

                # Transform mesh vertices into temp_obj's local space
                vert_xform = temp_obj_mat.inverted() @ world_xform
                temp_mesh.transform(vert_xform)

                if remap_mat.determinant() < 0.0:
                    temp_mesh.flip_normals()

                temp_mesh.update()
                temp_records.append((obj, orig_name, temp_obj, temp_mesh))

                if maya_up == "Y":
                    maya_loc_cm = [float(target_loc.x * 100.0), float(target_loc.z * 100.0), float(-target_loc.y * 100.0)]
                    maya_rot_deg = [0.0, 0.0, 0.0] if do_freeze_rot else [float(wrot_deg[0]), float(wrot_deg[2]), float(-wrot_deg[1])]
                else:
                    maya_loc_cm = [float(target_loc.x * 100.0), float(target_loc.y * 100.0), float(target_loc.z * 100.0)]
                    maya_rot_deg = [0.0, 0.0, 0.0] if do_freeze_rot else [float(wrot_deg[0]), float(wrot_deg[1]), float(wrot_deg[2])]

                meta_objects[orig_name] = {
                    "bridge_id": b_id,
                    "revision": rev,
                    "location_blender_m": [float(target_loc.x), float(target_loc.y), float(target_loc.z)],
                    "rotation_blender_deg": [0.0, 0.0, 0.0] if do_freeze_rot else wrot_deg,
                    "location_maya_cm": maya_loc_cm,
                    "rotation_maya_deg": maya_rot_deg,
                    "freeze_location": do_freeze_loc,
                    "freeze_rotation": do_freeze_rot,
                    "has_parent": has_parent,
                    "vertex_count": len(temp_mesh.vertices),
                }

            for o in list(context.selected_objects):
                o.select_set(False)
            for _, _, temp_obj, _ in temp_records:
                temp_obj.select_set(True)
            context.view_layer.objects.active = temp_records[0][2]

            exp_forward = '-Z' if maya_up == 'Y' else '-Y'
            exp_up = 'Y' if maya_up == 'Y' else 'Z'

            bpy.ops.export_scene.fbx(
                filepath=b2m_fbx,
                check_existing=False,
                use_selection=True,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_NONE',
                bake_space_transform=True,
                object_types={'MESH'},
                use_mesh_modifiers=False,
                mesh_smooth_type='FACE',
                axis_forward=exp_forward,
                axis_up=exp_up,
            )
        finally:
            # Unconditional restoration of original objects, names, selection, and mode
            for orig_obj, orig_name, temp_obj, temp_mesh in temp_records:
                try:
                    if temp_obj and temp_obj.name in bpy.data.objects:
                        bpy.data.objects.remove(temp_obj, do_unlink=True)
                except Exception:
                    pass
                try:
                    if temp_mesh and temp_mesh.users == 0:
                        bpy.data.meshes.remove(temp_mesh, do_unlink=True)
                except Exception:
                    pass
                try:
                    orig_obj.name = orig_name
                except Exception:
                    pass

            for o in list(context.selected_objects):
                o.select_set(False)
            for o in initial_selection:
                try:
                    if o.name in bpy.data.objects:
                        o.select_set(True)
                except Exception:
                    pass
            if active_obj and active_obj.name in bpy.data.objects:
                context.view_layer.objects.active = active_obj
            elif selected_meshes and selected_meshes[0].name in bpy.data.objects:
                context.view_layer.objects.active = selected_meshes[0]

            if previous_mode in ('SCULPT', 'EDIT') and context.view_layer.objects.active:
                try:
                    bpy.ops.object.mode_set(mode=previous_mode)
                except Exception:
                    pass

        # Mirror to SiSync_Exchange.fbx for backward compatibility
        try:
            shutil.copy2(b2m_fbx, legacy_fbx)
        except Exception:
            pass

        export_ts = time.time()
        LAST_EXPORT_TIMESTAMP = export_ts
        core.EchoSuppressor.record_export("blender", rev)
        core.write_metadata(
            {
                "revision": rev,
                "source": "blender",
                "destination": "maya",
                "format": "fbx",
                "file_path": b2m_fbx,
                "fbx_path": b2m_fbx,
                "timestamp": export_ts,
                "export_timestamp": export_ts,
                "maya_up_axis": maya_up,
                "freeze_location": do_freeze_loc,
                "freeze_rotation": do_freeze_rot,
                "objects": meta_objects,
            },
            custom_dir=ex_dir,
        )
        core.log_event("blender", "EXPORT_TO_MAYA", "success", source="blender", destination="maya", revision=rev, extra=b2m_fbx)
        print(f"[SiSync] Exported {len(selected_meshes)} mesh(es) -> {b2m_fbx} (rev {rev})")
        return b2m_fbx

    @classmethod
    def import_from_maya(cls, context) -> List[bpy.types.Object]:
        """
        Imports maya_to_blender.fbx (or metadata-specified file_path) from Maya into Blender.
        Matches by bridge_id, exact name, sanitized name, or single-mesh fallback.
        Preserves Blender materials when replacing remeshed topology.
        """
        global LAST_IMPORT_TIMESTAMP
        ex_dir = get_exchange_dir(context)
        meta = core.read_metadata(ex_dir)
        meta_objects = meta.get("objects", {}) if isinstance(meta.get("objects"), dict) else {}
        if "export_timestamp" in meta or "timestamp" in meta:
            LAST_IMPORT_TIMESTAMP = float(meta.get("timestamp") or meta.get("export_timestamp") or time.time())

        # Determine authoritative FBX file from metadata first, then maya_to_blender.fbx, then legacy
        meta_file_path = str(meta.get("file_path") or meta.get("fbx_path") or "").replace("\\", "/")
        candidates = [
            os.path.join(ex_dir, core.FBX_MAYA_TO_BLENDER).replace("\\", "/"),
            os.path.join(ex_dir, EXCHANGE_FBX_NAME).replace("\\", "/"),
        ]
        if meta_file_path and os.path.exists(meta_file_path) and str(meta.get("source", "")).lower() == "maya":
            fbx_path = meta_file_path
        else:
            existing_files = [p for p in candidates if os.path.exists(p)]
            if not existing_files:
                print(f"[SiSync] No FBX file found in: {ex_dir}")
                return []
            fbx_path = max(existing_files, key=lambda p: os.path.getmtime(p)).replace("\\", "/")

        active_obj = getattr(context, "sculpt_object", None) or getattr(context, "active_object", None)
        previous_mode = active_obj.mode if active_obj else 'OBJECT'
        if active_obj and active_obj.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        prefs = get_preferences(context)
        remap_mat = _build_gob_remap_matrix(prefs)
        do_freeze_loc = bool(prefs.freeze_location) if prefs else False
        do_freeze_rot = bool(prefs.freeze_rotation) if prefs else True
        maya_up = prefs.maya_up_axis if prefs else "Y"

        pre_objs = set(bpy.data.objects.keys())
        for o in list(context.selected_objects):
            o.select_set(False)

        imp_forward = '-Z' if maya_up == 'Y' else '-Y'
        imp_up = 'Y' if maya_up == 'Y' else 'Z'

        bpy.ops.import_scene.fbx(
            filepath=fbx_path,
            use_manual_orientation=False,
            axis_forward=imp_forward,
            axis_up=imp_up,
        )

        context.view_layer.update()
        new_names = set(bpy.data.objects.keys()) - pre_objs
        new_all = [bpy.data.objects[n] for n in new_names if n in bpy.data.objects]
        new_meshes = [o for o in new_all if o.type == 'MESH']
        new_empties = [o for o in new_all if o.type == 'EMPTY']

        result_objects: List[bpy.types.Object] = []
        single_mesh_mode = len(new_meshes) == 1

        for imp_obj in new_meshes:
            base_name = _strip_duplicate_suffix(imp_obj.name)
            obj_meta = meta_objects.get(base_name, {}) if isinstance(meta_objects, dict) else {}
            if not obj_meta and single_mesh_mode and len(meta_objects) == 1:
                obj_meta = next(iter(meta_objects.values()))

            bridge_id = str(obj_meta.get("bridge_id") or "")
            target_obj = _find_matching_blender_object(
                base_name=base_name,
                bridge_id=bridge_id,
                imp_obj=imp_obj,
                single_mesh_fallback=single_mesh_mode,
            )

            imp_mesh = imp_obj.data

            # Transform imported mesh vertices into Blender World Space (Z-Up, meters)
            world_mat = remap_mat @ imp_obj.matrix_world
            imp_mesh.transform(world_mat)
            if remap_mat.determinant() < 0.0:
                imp_mesh.flip_normals()

            # Compute accurate target world location
            new_wloc = world_mat.to_translation()
            if "location_blender_m" in obj_meta:
                loc_m = obj_meta["location_blender_m"]
                meta_vec = remap_mat @ mathutils.Vector((float(loc_m[0]), float(loc_m[1]), float(loc_m[2])))
                if new_wloc.length < 1e-6 or meta_vec.length > 1e-6:
                    new_wloc = meta_vec
            elif "maya_pivot" in obj_meta and new_wloc.length < 1e-6:
                mpiv = obj_meta["maya_pivot"]
                b_pt = core.CoordinateBasis.maya_point_to_blender((float(mpiv[0]), float(mpiv[1]), float(mpiv[2])), up_axis=maya_up)
                new_wloc = remap_mat @ mathutils.Vector(b_pt)

            # Compute accurate target world rotation
            if do_freeze_rot:
                new_rot_mat = mathutils.Matrix.Identity(4)
            else:
                if "rotation_blender_deg" in obj_meta:
                    r_deg = obj_meta["rotation_blender_deg"]
                    new_rot_mat = mathutils.Euler(
                        (math.radians(r_deg[0]), math.radians(r_deg[1]), math.radians(r_deg[2])), 'XYZ'
                    ).to_matrix().to_4x4()
                elif "maya_rot" in obj_meta:
                    mr = obj_meta["maya_rot"]
                    if maya_up == "Y":
                        b_deg = (float(mr[0]), float(-mr[2]), float(mr[1]))
                    else:
                        b_deg = (float(mr[0]), float(mr[1]), float(mr[2]))
                    new_rot_mat = mathutils.Euler(
                        (math.radians(b_deg[0]), math.radians(b_deg[1]), math.radians(b_deg[2])), 'XYZ'
                    ).to_matrix().to_4x4()
                else:
                    basis_fix = mathutils.Matrix.Rotation(math.radians(-90.0 if maya_up == 'Y' else 0.0), 4, 'X')
                    pure_rot = (imp_obj.matrix_world.to_3x3().normalized().to_4x4()) @ basis_fix
                    new_rot_mat = pure_rot

            desired_world_mat = (
                mathutils.Matrix.Identity(4) if do_freeze_loc else mathutils.Matrix.Translation(new_wloc)
            ) @ new_rot_mat

            if target_obj and target_obj.type == 'MESH':
                if bridge_id:
                    target_obj["sisync_bridge_id"] = bridge_id
                target_obj.matrix_world = desired_world_mat
                context.view_layer.update()

                imp_mesh.transform(target_obj.matrix_world.inverted())
                imp_mesh.update()

                has_armature = any(m.type == 'ARMATURE' for m in target_obj.modifiers)
                has_shapekeys = target_obj.data.shape_keys is not None
                if (has_armature or has_shapekeys) and len(imp_mesh.vertices) == len(target_obj.data.vertices):
                    coords = [0.0] * (len(imp_mesh.vertices) * 3)
                    imp_mesh.vertices.foreach_get("co", coords)
                    target_obj.data.vertices.foreach_set("co", coords)
                    if has_shapekeys and "Basis" in target_obj.data.shape_keys.key_blocks:
                        target_obj.data.shape_keys.key_blocks["Basis"].data.foreach_set("co", coords)
                    target_obj.data.update()
                    target_obj.data.update_tag()
                    target_obj.update_tag()
                    print(f"[SiSync] Updated '{target_obj.name}' in-place ({len(imp_mesh.vertices)} verts).")
                else:
                    old_mesh = target_obj.data
                    old_materials = [m for m in old_mesh.materials] if old_mesh else []
                    target_obj.data = imp_mesh
                    target_obj.data.name = f"{target_obj.name}_Mesh"
                    # Preserve existing Blender materials when topology changes (e.g. after Maya Remesh)
                    if old_materials:
                        target_obj.data.materials.clear()
                        for mat in old_materials:
                            target_obj.data.materials.append(mat)
                    target_obj.data.update()
                    target_obj.data.update_tag()
                    target_obj.update_tag()
                    if old_mesh and old_mesh.users == 0:
                        bpy.data.meshes.remove(old_mesh, do_unlink=True)
                    print(f"[SiSync] Updated '{target_obj.name}' mesh topology ({len(imp_mesh.vertices)} verts).")

                temp_m = imp_obj.data if imp_obj.data != target_obj.data else None
                bpy.data.objects.remove(imp_obj, do_unlink=True)
                if temp_m and temp_m.users == 0:
                    bpy.data.meshes.remove(temp_m, do_unlink=True)

                result_objects.append(target_obj)
            else:
                imp_obj.parent = None
                imp_obj.name = base_name
                imp_obj.data.name = f"{base_name}_Mesh"
                if bridge_id:
                    imp_obj["sisync_bridge_id"] = bridge_id
                imp_obj.matrix_world = desired_world_mat
                imp_mesh.transform(desired_world_mat.inverted())
                imp_mesh.update()
                imp_mesh.update_tag()
                imp_obj.update_tag()
                result_objects.append(imp_obj)

        for emp in new_empties:
            if emp.name in bpy.data.objects:
                try:
                    bpy.data.objects.remove(emp, do_unlink=True)
                except Exception:
                    pass

        for o in result_objects:
            if o.name in bpy.data.objects:
                o.select_set(True)
        if result_objects:
            context.view_layer.objects.active = result_objects[0]
            if previous_mode == 'SCULPT':
                try:
                    bpy.ops.object.mode_set(mode='SCULPT')
                except Exception:
                    pass
        context.view_layer.update()
        core.log_event("blender", "IMPORT_FROM_MAYA", "success", source="maya", destination="blender", extra=f"count={len(result_objects)}")
        return result_objects

    @classmethod
    def export_selection(cls, context=None) -> Optional[Dict[str, Any]]:
        """Exports selected mesh(es) and returns the canonical metadata dictionary."""
        ctx = context or bpy.context
        fbx_p = cls.export_to_maya(ctx)
        if not fbx_p:
            return None
        return core.read_metadata(get_exchange_dir(ctx))

    @staticmethod
    def _update_vertices_in_place(target_obj: bpy.types.Object, source_obj: bpy.types.Object) -> None:
        """Updates target_obj's vertices in-place from source_obj's vertices."""
        if len(source_obj.data.vertices) == len(target_obj.data.vertices):
            coords = [0.0] * (len(source_obj.data.vertices) * 3)
            source_obj.data.vertices.foreach_get("co", coords)
            target_obj.data.vertices.foreach_set("co", coords)
            target_obj.data.update()
            target_obj.data.update_tag()
            target_obj.update_tag()

    @staticmethod
    def _relink_mesh_data(target_obj: bpy.types.Object, source_obj: bpy.types.Object) -> None:
        """Re-links target_obj's mesh datablock to source_obj.data while preserving target_obj transform and materials."""
        old_mesh = target_obj.data
        old_mats = [m for m in old_mesh.materials] if old_mesh else []
        target_obj.data = source_obj.data
        if old_mats:
            target_obj.data.materials.clear()
            for m in old_mats:
                target_obj.data.materials.append(m)
        target_obj.data.update()
        target_obj.data.update_tag()
        target_obj.update_tag()
