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
from typing import List, Optional, Dict, Any, Tuple
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


# ---------------------------------------------------------------------------
# Phase 2: Canonical Vertex Color & Material / PBR / Texture / UV Sync Layer
# ---------------------------------------------------------------------------
def _get_or_set_blender_material_id(mat: bpy.types.Material) -> str:
    mat_id = str(mat.get("sisync_material_id") or "")
    if not mat_id:
        mat_id = core.generate_material_id(mat.name)
        mat["sisync_material_id"] = mat_id
    return mat_id


def _find_matching_blender_material(mat_name: str, mat_id: str) -> bpy.types.Material:
    if mat_id:
        for m in bpy.data.materials:
            if str(m.get("sisync_material_id") or "") == str(mat_id):
                if mat_name and m.name != mat_name:
                    if re.sub(r"\d+$", "", _sanitize_name(mat_name)) != _sanitize_name(m.name):
                        try:
                            other_m = bpy.data.materials.get(mat_name)
                            if other_m is not None and other_m != m and not other_m.get("sisync_material_id"):
                                bpy.data.materials.remove(other_m)
                            m.name = mat_name
                        except Exception:
                            pass
                return m
    if mat_name:
        cand = bpy.data.materials.get(mat_name)
        if cand:
            if mat_id:
                cand["sisync_material_id"] = mat_id
            return cand
        san = _sanitize_name(mat_name)
        for m in bpy.data.materials:
            if _sanitize_name(m.name) == san:
                if mat_id:
                    m["sisync_material_id"] = mat_id
                return m
    new_mat = bpy.data.materials.new(name=mat_name or "SiSync_Material")
    new_mat.use_nodes = True
    new_mat["sisync_material_id"] = mat_id or core.generate_material_id(new_mat.name)
    return new_mat


def _extract_blender_material_def(mat: bpy.types.Material, uv_sets: List[str], ex_dir: str) -> Dict[str, Any]:
    mat_id = _get_or_set_blender_material_id(mat)
    mdef: Dict[str, Any] = {
        "material_id": mat_id,
        "name": mat.name,
        "base_color": [float(c) for c in getattr(mat, "diffuse_color", (0.8, 0.8, 0.8, 1.0))[:4]],
        "roughness": float(getattr(mat, "roughness", 0.5)),
        "metallic": float(getattr(mat, "metallic", 0.0)),
        "specular": float(getattr(mat, "specular_intensity", 0.5)),
        "ior": 1.45,
        "emission_color": [0.0, 0.0, 0.0, 1.0],
        "emission_strength": 0.0,
        "opacity": 1.0,
        "normal_strength": 1.0,
        "bump_strength": 0.5,
        "uv_sets": list(uv_sets),
        "textures": [],
    }
    if not getattr(mat, "use_nodes", False) or not getattr(mat, "node_tree", None):
        return mdef

    nodes = mat.node_tree.nodes
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf:
        def _sock_val(names, default):
            for nm in names:
                if nm in bsdf.inputs:
                    val = bsdf.inputs[nm].default_value
                    try:
                        return [float(x) for x in val]
                    except TypeError:
                        return float(val)
            return default

        bc = _sock_val(["Base Color"], mdef["base_color"])
        if isinstance(bc, list):
            mdef["base_color"] = (bc + [1.0])[:4]
        mdef["roughness"] = float(_sock_val(["Roughness"], mdef["roughness"]))
        mdef["metallic"] = float(_sock_val(["Metallic"], mdef["metallic"]))
        mdef["specular"] = float(_sock_val(["Specular IOR Level", "Specular"], mdef["specular"]))
        mdef["ior"] = float(_sock_val(["IOR"], mdef["ior"]))
        em = _sock_val(["Emission Color", "Emission"], mdef["emission_color"])
        if isinstance(em, list):
            mdef["emission_color"] = (em + [1.0])[:4]
        mdef["emission_strength"] = float(_sock_val(["Emission Strength"], 0.0))
        mdef["opacity"] = float(_sock_val(["Alpha"], 1.0))

        # Inspect Normal / Bump strength if connected
        if "Normal" in bsdf.inputs and bsdf.inputs["Normal"].is_linked:
            from_node = bsdf.inputs["Normal"].links[0].from_node
            if from_node.type == 'NORMAL_MAP' and "Strength" in from_node.inputs:
                mdef["normal_strength"] = float(from_node.inputs["Strength"].default_value)
            elif from_node.type == 'BUMP' and "Strength" in from_node.inputs:
                mdef["bump_strength"] = float(from_node.inputs["Strength"].default_value)

    # Extract connected or present TEX_IMAGE nodes
    channel_map = {
        "Base Color": "base_color",
        "Roughness": "roughness",
        "Metallic": "metallic",
        "Specular IOR Level": "specular",
        "Specular": "specular",
        "Emission Color": "emission",
        "Emission": "emission",
        "Alpha": "opacity",
        "Normal": "normal",
        "Height": "bump",
    }
    seen_tex = set()
    for node in nodes:
        if node.type != 'TEX_IMAGE' or not node.image:
            continue
        img = node.image
        raw_p = bpy.path.abspath(img.filepath) if img.filepath else ""
        norm_p = os.path.normpath(raw_p).replace("\\", "/") if raw_p else ""
        staged_p = ""
        if norm_p and os.path.exists(norm_p) and os.path.isfile(norm_p):
            staged_p = core.stage_texture_to_bridge(norm_p, ex_dir)
        elif getattr(img, "packed_file", None) is not None:
            # Real production asset has packed texture in .blend: stage packed bytes directly to sisync_bridge/textures/
            tex_dir = core.get_textures_dir(ex_dir)
            fname = os.path.basename(norm_p) if norm_p else (img.name if "." in img.name else f"{img.name}.png")
            dst_p = os.path.join(tex_dir, fname).replace("\\", "/")
            try:
                if not os.path.exists(dst_p):
                    with open(dst_p, "wb") as fp_out:
                        fp_out.write(img.packed_file.data)
                staged_p = dst_p
            except Exception:
                staged_p = norm_p
        else:
            staged_p = norm_p
        cspace = "sRGB"
        if hasattr(img, "colorspace_settings") and hasattr(img.colorspace_settings, "name"):
            cspace = str(img.colorspace_settings.name or "sRGB")

        # Determine UV set feeding this texture node
        uv_set_name = uv_sets[0] if uv_sets else "UVMap"
        if "Vector" in node.inputs and node.inputs["Vector"].is_linked:
            vec_node = node.inputs["Vector"].links[0].from_node
            if vec_node.type == 'UVMAP' and getattr(vec_node, "uv_map", ""):
                uv_set_name = str(vec_node.uv_map)

        # Trace target channel(s)
        detected_channels = []
        stack = [node]
        visited = set()
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            for out_s in getattr(curr, "outputs", []):
                for lk in getattr(out_s, "links", []):
                    to_s = lk.to_socket
                    to_n = lk.to_node
                    if to_n and to_n.type == 'BUMP' and to_s and to_s.name == "Height":
                        detected_channels.append("bump")
                    elif to_n and to_n.type == 'NORMAL_MAP' and to_s and to_s.name == "Color":
                        detected_channels.append("normal")
                    elif to_s and to_s.name in channel_map:
                        detected_channels.append(channel_map[to_s.name])
                    if to_n and to_n not in visited and to_n.type not in ('BSDF_PRINCIPLED', 'OUTPUT_MATERIAL'):
                        stack.append(to_n)

        if not detected_channels:
            detected_channels = ["base_color"]

        for ch in detected_channels:
            key = (ch, staged_p or img.name)
            if key in seen_tex:
                continue
            seen_tex.add(key)
            mdef["textures"].append({
                "channel": ch,
                "image_name": img.name,
                "path": staged_p or norm_p,
                "original_path": norm_p,
                "colorspace": cspace,
                "uv_set": uv_set_name,
            })
    return mdef


def _apply_blender_material_def(mat: bpy.types.Material, mdef: Dict[str, Any], ex_dir: str) -> None:
    if not isinstance(mdef, dict):
        return
    if mdef.get("material_id"):
        mat["sisync_material_id"] = str(mdef["material_id"])
    mat.use_nodes = True
    nt = mat.node_tree
    if not nt:
        return
    nodes = nt.nodes
    links = nt.links

    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    out_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if not out_node:
        out_node = nodes.new(type='ShaderNodeOutputMaterial')
    if not bsdf:
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        if "BSDF" in bsdf.outputs and "Surface" in out_node.inputs:
            links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])

    bc = mdef.get("base_color", [0.8, 0.8, 0.8, 1.0])
    if isinstance(bc, (list, tuple)) and len(bc) >= 3:
        rgba = [float(bc[0]), float(bc[1]), float(bc[2]), float(bc[3]) if len(bc) >= 4 else 1.0]
        if "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = rgba
        try:
            mat.diffuse_color = rgba
        except Exception:
            pass

    if "roughness" in mdef:
        r_val = float(mdef["roughness"])
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = r_val
        try:
            mat.roughness = r_val
        except Exception:
            pass

    if "metallic" in mdef:
        m_val = float(mdef["metallic"])
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = m_val
        try:
            mat.metallic = m_val
        except Exception:
            pass

    if "specular" in mdef:
        s_val = float(mdef["specular"])
        for sn in ("Specular IOR Level", "Specular"):
            if sn in bsdf.inputs:
                bsdf.inputs[sn].default_value = s_val
                break

    if "ior" in mdef and "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = float(mdef["ior"])

    em_col = mdef.get("emission_color")
    if isinstance(em_col, (list, tuple)) and len(em_col) >= 3:
        em_rgba = [float(em_col[0]), float(em_col[1]), float(em_col[2]), float(em_col[3]) if len(em_col) >= 4 else 1.0]
        for en in ("Emission Color", "Emission"):
            if en in bsdf.inputs:
                bsdf.inputs[en].default_value = em_rgba
                break

    if "emission_strength" in mdef and "Emission Strength" in bsdf.inputs:
        bsdf.inputs["Emission Strength"].default_value = float(mdef["emission_strength"])

    if "opacity" in mdef and "Alpha" in bsdf.inputs:
        bsdf.inputs["Alpha"].default_value = float(mdef["opacity"])

    # Connect/update textures
    tex_list = mdef.get("textures", [])
    if not isinstance(tex_list, list):
        return
    active_tex_channels = {str(t.get("channel") or "").lower() for t in tex_list if isinstance(t, dict)}
    for ch_key, sock_nm in (("base_color", "Base Color"), ("roughness", "Roughness"), ("metallic", "Metallic")):
        if ch_key not in active_tex_channels and sock_nm in bsdf.inputs and bsdf.inputs[sock_nm].is_linked:
            for lk in list(bsdf.inputs[sock_nm].links):
                links.remove(lk)
    tex_dir = core.get_textures_dir(ex_dir)
    for tinfo in tex_list:
        if not isinstance(tinfo, dict):
            continue
        ch = str(tinfo.get("channel") or "base_color").lower()
        raw_p = str(tinfo.get("path") or tinfo.get("original_path") or "").replace("\\", "/")
        cand_p = raw_p
        if raw_p and not os.path.exists(cand_p):
            alt_p = os.path.join(tex_dir, os.path.basename(raw_p)).replace("\\", "/")
            if os.path.exists(alt_p):
                cand_p = alt_p

        img = None
        if cand_p and os.path.exists(cand_p):
            try:
                img = bpy.data.images.load(cand_p, check_existing=True)
            except Exception:
                img = None
        elif tinfo.get("image_name") and tinfo["image_name"] in bpy.data.images:
            img = bpy.data.images[tinfo["image_name"]]

        if not img and cand_p:
            # Create placeholder image reference so missing path is preserved and reportable
            img = bpy.data.images.new(name=os.path.basename(cand_p) or "SiSync_Tex", width=4, height=4)
            img.source = 'FILE'
            img.filepath = cand_p

        if not img:
            continue

        cspace = str(tinfo.get("colorspace") or ("sRGB" if ch in ("base_color", "emission") else "Non-Color"))
        if hasattr(img, "colorspace_settings"):
            try:
                img.colorspace_settings.name = cspace
            except Exception:
                pass

        tex_node = next((n for n in nodes if n.type == 'TEX_IMAGE' and n.image == img), None)
        if not tex_node:
            tex_node = nodes.new(type='ShaderNodeTexImage')
            tex_node.image = img

        uv_name = str(tinfo.get("uv_set") or "")
        if uv_name and "Vector" in tex_node.inputs:
            uv_node = next((n for n in nodes if n.type == 'UVMAP' and getattr(n, "uv_map", "") == uv_name), None)
            if not uv_node:
                uv_node = nodes.new(type='ShaderNodeUVMap')
                uv_node.uv_map = uv_name
            if "UV" in uv_node.outputs:
                links.new(uv_node.outputs["UV"], tex_node.inputs["Vector"])

        if ch == "base_color" and "Base Color" in bsdf.inputs:
            links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
        elif ch == "roughness" and "Roughness" in bsdf.inputs:
            links.new(tex_node.outputs["Color"], bsdf.inputs["Roughness"])
        elif ch == "metallic" and "Metallic" in bsdf.inputs:
            links.new(tex_node.outputs["Color"], bsdf.inputs["Metallic"])
        elif ch == "specular":
            for sn in ("Specular IOR Level", "Specular"):
                if sn in bsdf.inputs:
                    links.new(tex_node.outputs["Color"], bsdf.inputs[sn])
                    break
        elif ch == "emission":
            for en in ("Emission Color", "Emission"):
                if en in bsdf.inputs:
                    links.new(tex_node.outputs["Color"], bsdf.inputs[en])
                    break
        elif ch == "opacity" and "Alpha" in bsdf.inputs:
            links.new(tex_node.outputs["Color"], bsdf.inputs["Alpha"])
        elif ch == "normal" and "Normal" in bsdf.inputs:
            nmap = next((n for n in nodes if n.type == 'NORMAL_MAP'), None)
            if not nmap:
                nmap = nodes.new(type='ShaderNodeNormalMap')
            if "normal_strength" in mdef and "Strength" in nmap.inputs:
                nmap.inputs["Strength"].default_value = float(mdef["normal_strength"])
            links.new(tex_node.outputs["Color"], nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
        elif ch == "bump" and "Normal" in bsdf.inputs:
            bnode = next((n for n in nodes if n.type == 'BUMP'), None)
            if not bnode:
                bnode = nodes.new(type='ShaderNodeBump')
            if "bump_strength" in mdef and "Strength" in bnode.inputs:
                bnode.inputs["Strength"].default_value = float(mdef["bump_strength"])
            links.new(tex_node.outputs["Color"], bnode.inputs["Height"])
            links.new(bnode.outputs["Normal"], bsdf.inputs["Normal"])


def _get_active_transfer_flags(context=None, ex_dir: Optional[str] = None) -> Dict[str, bool]:
    prefs = get_preferences(context)
    all_on = bool(getattr(prefs, "sync_all", False)) if prefs else False
    hier_on = bool(getattr(prefs, "sync_hierarchy", False)) if prefs else False
    vcol_on = bool(getattr(prefs, "sync_vertex_color", True)) if prefs else True
    bs_on = bool(getattr(prefs, "sync_blendshapes", False)) if prefs else False
    return core.resolve_transfer_flags(all_on, hier_on, vcol_on, blendshapes_enabled=bs_on)


def _is_fbx_yup_cm_wrapper_matrix(w_mat: mathutils.Matrix, maya_up: str = "Y") -> bool:
    """
    Detects if a world matrix in Blender carries the standard FBX Y-Up centimeter wrapper
    (scale ~ 0.01 and X rotation ~ +90 deg), which maps Maya cm Y-Up coordinates into
    Blender m Z-Up space. When exporting back to Maya Y-Up cm, this wrapper cancels out
    to Identity rotation (0,0,0) and unit scale (1,1,1).
    """
    if maya_up.upper() != "Y":
        return False
    w_scl = w_mat.to_scale()
    w_rot_deg = [math.degrees(a) for a in w_mat.to_euler('XYZ')]
    is_cm_scale = (
        abs(float(w_scl.x) - 0.01) < 1e-3
        and abs(float(w_scl.y) - 0.01) < 1e-3
        and abs(float(w_scl.z) - 0.01) < 1e-3
    )
    is_x90_rot = (
        abs(float(w_rot_deg[0]) - 90.0) < 0.5
        and abs(float(w_rot_deg[1])) < 0.5
        and abs(float(w_rot_deg[2])) < 0.5
    )
    return bool(is_cm_scale and is_x90_rot)


def _evaluate_mesh_without_armature(
    context,
    obj: bpy.types.Object,
    active_shapekey_name: Optional[str] = None,
    shapekey_mode: str = "CURRENT",
) -> bpy.types.Mesh:
    """
    Evaluates mesh modifiers that are active in viewport (show_viewport=True), while ignoring
    disabled modifiers and temporarily muting ARMATURE modifiers so enabling Subdivision Surface
    (SUBSURF) or other geometry modifiers never alters object transform, scale, or rest alignment.

    shapekey_mode:
      - "CURRENT": Evaluates the exact currently displayed Blender Shape Key state (e.g. Smile=0.5,
                   Smile=1.0, or combined Smile=1.0 + Mouth_Open=1.0) via the dependency graph
                   without modifying the user's Shape Key values.
      - "BASIS":   Evaluates the Basis (rest) state while ignoring active Shape Key deformations,
                   used for the base mesh when Export Blend Shapes = ON.
      - "TARGET":  Evaluates an isolated target Shape Key (active_shapekey_name = 1.0, all others = 0.0),
                   used for individual Blend Shape target exports when Export Blend Shapes = ON.
    """
    if active_shapekey_name is not None and shapekey_mode == "CURRENT":
        shapekey_mode = "TARGET"

    active_non_arm_mods = [
        m for m in obj.modifiers
        if m.type != 'ARMATURE' and getattr(m, "show_viewport", True)
    ]
    has_sk = bool(obj.data and obj.data.shape_keys and obj.data.shape_keys.key_blocks)

    if len(active_non_arm_mods) == 0:
        if not has_sk:
            return obj.data.copy()
        if shapekey_mode == "BASIS":
            m_copy = obj.data.copy()
            if "Basis" in obj.data.shape_keys.key_blocks:
                kb_basis = obj.data.shape_keys.key_blocks["Basis"]
                if len(kb_basis.data) == len(m_copy.vertices):
                    coords = [0.0] * (len(kb_basis.data) * 3)
                    kb_basis.data.foreach_get("co", coords)
                    m_copy.vertices.foreach_set("co", coords)
                    m_copy.update()
            return m_copy
        if shapekey_mode == "TARGET" and active_shapekey_name and active_shapekey_name in obj.data.shape_keys.key_blocks:
            m_copy = obj.data.copy()
            kb = obj.data.shape_keys.key_blocks[active_shapekey_name]
            if len(kb.data) == len(m_copy.vertices):
                coords = [0.0] * (len(kb.data) * 3)
                kb.data.foreach_get("co", coords)
                m_copy.vertices.foreach_set("co", coords)
                m_copy.update()
            return m_copy

    saved_arm_mods = []
    for m in obj.modifiers:
        if m.type == 'ARMATURE' and getattr(m, "show_viewport", False):
            saved_arm_mods.append(m)
            m.show_viewport = False

    saved_fcurves = []
    def _mute_anim_data(anim_data):
        if not anim_data:
            return
        if getattr(anim_data, "drivers", None):
            for fc in anim_data.drivers:
                saved_fcurves.append((fc, bool(fc.mute)))
                fc.mute = True
        act = getattr(anim_data, "action", None)
        if act:
            if getattr(act, "fcurves", None):
                for fc in act.fcurves:
                    saved_fcurves.append((fc, bool(fc.mute)))
                    fc.mute = True
            for layer in getattr(act, "layers", []):
                for strip in getattr(layer, "strips", []):
                    for cb in getattr(strip, "channelbags", []):
                        for fc in getattr(cb, "fcurves", []):
                            saved_fcurves.append((fc, bool(fc.mute)))
                            fc.mute = True

    if saved_arm_mods:
        _mute_anim_data(getattr(obj, "animation_data", None))

    saved_kb_state = []
    saved_show_only_sk = bool(obj.show_only_shape_key) if has_sk else False
    saved_active_sk_idx = int(obj.active_shape_key_index or 0) if has_sk else 0
    modified_sk = False
    if has_sk:
        if shapekey_mode in ("BASIS", "TARGET"):
            modified_sk = True
            _mute_anim_data(getattr(obj.data.shape_keys, "animation_data", None))
            target_sk_idx = 0
            for idx_k, kb in enumerate(obj.data.shape_keys.key_blocks):
                saved_kb_state.append((kb, float(kb.value), bool(kb.mute)))
                if shapekey_mode == "BASIS" or active_shapekey_name is None:
                    kb.mute = (idx_k > 0)
                    kb.value = 0.0
                else:
                    if kb.name == active_shapekey_name:
                        target_sk_idx = idx_k
                        kb.mute = False
                        kb.value = 1.0
                    else:
                        kb.mute = (idx_k > 0)
                        kb.value = 0.0
            obj.active_shape_key_index = target_sk_idx
            obj.show_only_shape_key = True
        elif shapekey_mode == "CURRENT" and saved_show_only_sk:
            modified_sk = True
            obj.show_only_shape_key = False

    try:
        obj.data.update()
        obj.data.update_tag()
        obj.update_tag()
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        obj_eval = obj.evaluated_get(depsgraph)
        return bpy.data.meshes.new_from_object(obj_eval)
    finally:
        if saved_fcurves:
            for fc, orig_fc_mute in saved_fcurves:
                try:
                    fc.mute = orig_fc_mute
                except Exception:
                    pass
        if modified_sk:
            for kb, val, mute_state in saved_kb_state:
                kb.value = val
                kb.mute = mute_state
            obj.active_shape_key_index = saved_active_sk_idx
            obj.show_only_shape_key = saved_show_only_sk
        if saved_arm_mods:
            for m in saved_arm_mods:
                m.show_viewport = True
        if modified_sk or saved_arm_mods or saved_fcurves:
            obj.data.update()
            obj.data.update_tag()
            obj.update_tag()
            context.view_layer.update()


def _cleanup_blender_blendshapes_group() -> None:
    """Removes any temporary staging collections or generated BlendShapes objects in Blender."""
    to_remove_objs = []
    for o in list(bpy.data.objects):
        if (
            bool(o.get("sisync_is_blendshape_mesh"))
            or bool(o.get("sisync_is_blendshapes_group"))
            or o.name == "BlendShapes"
            or o.name.startswith("__SiSync_TEMP_")
        ):
            to_remove_objs.append(o)
    for o in to_remove_objs:
        try:
            m_data = o.data if o.type == 'MESH' else None
            bpy.data.objects.remove(o, do_unlink=True)
            if m_data and m_data.users == 0:
                bpy.data.meshes.remove(m_data, do_unlink=True)
        except Exception:
            pass
    for col in list(bpy.data.collections):
        if col.name.startswith("__SiSync_EXPORT_TEMP__"):
            for co in list(col.objects):
                try:
                    m_d = co.data if co.type == 'MESH' else None
                    bpy.data.objects.remove(co, do_unlink=True)
                    if m_d and m_d.users == 0:
                        bpy.data.meshes.remove(m_d, do_unlink=True)
                except Exception:
                    pass
            try:
                bpy.data.collections.remove(col, do_unlink=True)
            except Exception:
                pass


def _collect_blender_hierarchy_data(
    seed_objects: List[bpy.types.Object],
    maya_up: str = "Y",
    prefs=None,
) -> Tuple[List[Dict[str, Any]], List[bpy.types.Object]]:
    """
    Given a list of selected/seed Blender objects (meshes or empties), climbs to their root transform(s)
    and traverses the full hierarchy subtree top-down (depth 0 -> leaves).
    Preserves Empty transform groups (even those with no mesh attached) as well as Mesh children.
    Assigns deterministic sisync_bridge_id to every participating node.
    """
    def _is_generated_bs(o: bpy.types.Object) -> bool:
        return bool(
            o.get("sisync_is_blendshape_mesh")
            or o.get("sisync_is_blendshapes_group")
            or o.name == "BlendShapes"
            or (o.parent and (o.parent.name == "BlendShapes" or bool(o.parent.get("sisync_is_blendshapes_group"))))
        )

    valid_seeds = [
        o for o in seed_objects
        if o is not None and o.type in ('EMPTY', 'MESH') and not o.name.startswith("WGT-") and not _is_generated_bs(o)
    ]
    if not valid_seeds:
        valid_seeds = [
            o for o in bpy.data.objects
            if o.type in ('EMPTY', 'MESH') and not o.name.startswith("WGT-") and o.name not in ("Cube", "Camera", "Light") and not _is_generated_bs(o)
        ]

    roots: List[bpy.types.Object] = []
    seen_roots = set()
    for o in valid_seeds:
        curr = o
        while (
            curr.parent is not None
            and curr.parent.type in ('EMPTY', 'MESH')
            and not curr.parent.name.startswith("WGT-")
            and not _is_generated_bs(curr.parent)
        ):
            curr = curr.parent
        if curr.name not in seen_roots:
            seen_roots.add(curr.name)
            roots.append(curr)

    ordered_nodes: List[Tuple[bpy.types.Object, int]] = []
    visited = set()

    def _walk(node: bpy.types.Object, depth: int):
        if node.name in visited or node.type not in ('EMPTY', 'MESH') or node.name.startswith("WGT-") or _is_generated_bs(node):
            return
        visited.add(node.name)
        ordered_nodes.append((node, depth))
        children = sorted(
            [c for c in node.children if c.type in ('EMPTY', 'MESH') and not c.name.startswith("WGT-") and not _is_generated_bs(c)],
            key=lambda x: x.name,
        )
        for ch in children:
            _walk(ch, depth + 1)

    for r in roots:
        _walk(r, 0)

    node_set = {n for n, _ in ordered_nodes}
    unit_scale = 100.0
    if prefs and getattr(prefs, "scale_mode", "BUNITS") == "MANUAL":
        unit_scale *= float(getattr(prefs, "manual_scale", 1.0))
    fx = bool(getattr(prefs, "flip_x_axis", False)) if prefs else False
    fy = bool(getattr(prefs, "flip_y_axis", False)) if prefs else False
    fz = bool(getattr(prefs, "flip_z_axis", False)) if prefs else False

    hierarchy_list: List[Dict[str, Any]] = []
    mesh_list: List[bpy.types.Object] = []

    for node, depth in ordered_nodes:
        b_id = str(node.get("sisync_bridge_id") or "")
        if not b_id:
            b_id = core.generate_bridge_id(node.name)
            node["sisync_bridge_id"] = b_id

        parent_obj = node.parent if (node.parent and node.parent in node_set) else None
        parent_bid = ""
        parent_name = ""
        if parent_obj:
            parent_bid = str(parent_obj.get("sisync_bridge_id") or "")
            if not parent_bid:
                parent_bid = core.generate_bridge_id(parent_obj.name)
                parent_obj["sisync_bridge_id"] = parent_bid
            parent_name = str(parent_obj.name)

        w_mat = node.matrix_world.copy()
        l_mat = (parent_obj.matrix_world.inverted_safe() @ w_mat) if parent_obj else w_mat

        w_loc = w_mat.to_translation()
        w_rot_deg = [math.degrees(a) for a in w_mat.to_euler('XYZ')]
        w_scl = w_mat.to_scale()

        l_loc = l_mat.to_translation()
        l_rot_deg = [math.degrees(a) for a in l_mat.to_euler('XYZ')]
        l_scl = l_mat.to_scale()

        m_pos = core.CoordinateBasis.blender_point_to_maya(
            (float(w_loc.x), float(w_loc.y), float(w_loc.z)),
            up_axis=maya_up,
            unit_scale=unit_scale,
            flip_x=fx,
            flip_y=fy,
            flip_z=fz,
        )
        if _is_fbx_yup_cm_wrapper_matrix(w_mat, maya_up=maya_up):
            m_rot = (0.0, 0.0, 0.0)
            maya_w_scl = [1.0, 1.0, 1.0]
            maya_l_scl = [1.0, 1.0, 1.0]
        else:
            m_rot = core.CoordinateBasis.blender_rot_to_maya(
                (float(w_rot_deg[0]), float(w_rot_deg[1]), float(w_rot_deg[2])),
                up_axis=maya_up,
            )
            maya_w_scl = [round(float(w_scl.x), 6), round(float(w_scl.y), 6), round(float(w_scl.z), 6)]
            maya_l_scl = [round(float(l_scl.x), 6), round(float(l_scl.y), 6), round(float(l_scl.z), 6)]

        safe_nm = _sanitize_name(node.name)
        if node.name.endswith(".003") and not safe_nm.endswith("_003"):
            safe_nm = f"{safe_nm}_003"

        parent_safe = _sanitize_name(parent_name) if parent_name else ""
        if parent_name.endswith(".003") and not parent_safe.endswith("_003"):
            parent_safe = f"{parent_safe}_003"

        is_frozen = bool(getattr(prefs, "freeze_location", False)) or bool(getattr(prefs, "freeze_rotation", False))
        hierarchy_list.append({
            "name": str(node.name),
            "original_blender_name": str(node.name),
            "sanitized_name": safe_nm,
            "node_type": "EMPTY" if node.type == 'EMPTY' else "MESH",
            "bridge_id": str(b_id),
            "parent_name": parent_name,
            "parent_sanitized_name": parent_safe,
            "parent_bridge_id": parent_bid,
            "depth": int(depth),
            "frozen": is_frozen,
            "matrix_world": [round(w_mat[r][c], 6) for r in range(4) for c in range(4)],
            "world_location_blender_m": [round(float(w_loc.x), 6), round(float(w_loc.y), 6), round(float(w_loc.z), 6)],
            "world_rotation_blender_deg": [round(float(w_rot_deg[0]), 6), round(float(w_rot_deg[1]), 6), round(float(w_rot_deg[2]), 6)],
            "world_scale": maya_w_scl,
            "local_location_blender_m": [round(float(l_loc.x), 6), round(float(l_loc.y), 6), round(float(l_loc.z), 6)],
            "local_rotation_blender_deg": [round(float(l_rot_deg[0]), 6), round(float(l_rot_deg[1]), 6), round(float(l_rot_deg[2]), 6)],
            "local_scale": maya_l_scl,
            "world_location_maya_cm": [round(float(m_pos[0]), 5), round(float(m_pos[1]), 5), round(float(m_pos[2]), 5)],
            "world_rotation_maya_deg": [round(float(m_rot[0]), 5), round(float(m_rot[1]), 5), round(float(m_rot[2]), 5)],
        })
        if node.type == 'MESH':
            mesh_list.append(node)

    return hierarchy_list, mesh_list


def _apply_blender_hierarchy(
    context,
    hierarchy_list: List[Dict[str, Any]],
    maya_up: str = "Y",
) -> List[bpy.types.Object]:
    """
    Reconstructs or updates the parent/group/Empty and Mesh hierarchy in Blender top-down (depth 0 -> leaves).
    Matches nodes by sisync_bridge_id first, then exact/sanitized name, preventing duplicate Empties or Meshes.
    Preserves world & parent-relative local transforms without baking parent transforms into child mesh vertices.
    """
    if not isinstance(hierarchy_list, list) or not hierarchy_list:
        return []

    sorted_entries = sorted(hierarchy_list, key=lambda x: int(x.get("depth", 0)))
    resolved_by_bid: Dict[str, bpy.types.Object] = {}
    resolved_by_name: Dict[str, bpy.types.Object] = {}
    touched_objects: List[bpy.types.Object] = []

    target_col = context.scene.collection

    for h in sorted_entries:
        if not isinstance(h, dict):
            continue
        bid = str(h.get("bridge_id") or "")
        orig_name = str(h.get("original_blender_name") or h.get("name") or "")
        san_name = str(h.get("sanitized_name") or _sanitize_name(orig_name))
        ntype = str(h.get("node_type") or "EMPTY").upper()
        if ntype not in ("EMPTY", "MESH"):
            ntype = "EMPTY"

        obj: Optional[bpy.types.Object] = None
        if bid:
            for cand in bpy.data.objects:
                if str(cand.get("sisync_bridge_id") or "") == bid and cand.type == ntype:
                    obj = cand
                    break
        if obj is None and orig_name:
            cand = bpy.data.objects.get(orig_name)
            if cand and cand.type == ntype and not cand.name.startswith("WGT-"):
                obj = cand
        if obj is None and san_name:
            for cand in bpy.data.objects:
                if cand.type == ntype and not cand.name.startswith("WGT-"):
                    if _sanitize_name(cand.name) == san_name:
                        obj = cand
                        break

        if obj is None and ntype == "EMPTY":
            obj = bpy.data.objects.new(orig_name or san_name or "SiSync_Node", None)
            obj.empty_display_type = 'PLAIN_AXES'
            obj.empty_display_size = 0.2
            target_col.objects.link(obj)

        if obj is None:
            continue

        if bid:
            obj["sisync_bridge_id"] = bid

        # Preserve exact production name if it isn't just a sanitized Maya version of an existing hyphenated/dotted Blender name
        if orig_name and obj.name != orig_name:
            if re.sub(r"[^a-zA-Z0-9_]", "_", orig_name) != re.sub(r"[^a-zA-Z0-9_]", "_", obj.name):
                try:
                    obj.name = orig_name
                except Exception:
                    pass

        # Resolve parent
        p_bid = str(h.get("parent_bridge_id") or "")
        p_name = str(h.get("parent_name") or "")
        p_san = str(h.get("parent_sanitized_name") or _sanitize_name(p_name))
        parent_obj: Optional[bpy.types.Object] = None
        if p_bid and p_bid in resolved_by_bid:
            parent_obj = resolved_by_bid[p_bid]
        elif p_bid:
            parent_obj = next((o for o in bpy.data.objects if str(o.get("sisync_bridge_id") or "") == p_bid), None)
        if parent_obj is None and p_name:
            parent_obj = resolved_by_name.get(p_name) or bpy.data.objects.get(p_name)
        if parent_obj is None and p_san:
            parent_obj = resolved_by_name.get(p_san)

        # Compute target Blender world matrix
        if "world_location_blender_m" in h and isinstance(h["world_location_blender_m"], (list, tuple)):
            w_loc = [float(x) for x in h["world_location_blender_m"][:3]]
        elif "world_location_maya_cm" in h and isinstance(h["world_location_maya_cm"], (list, tuple)):
            mp = h["world_location_maya_cm"]
            w_loc = list(core.CoordinateBasis.maya_point_to_blender((float(mp[0]), float(mp[1]), float(mp[2])), up_axis=maya_up))
        else:
            w_loc = [0.0, 0.0, 0.0]

        if "world_rotation_blender_deg" in h and isinstance(h["world_rotation_blender_deg"], (list, tuple)):
            w_rot_deg = [float(x) for x in h["world_rotation_blender_deg"][:3]]
        elif "world_rotation_maya_deg" in h and isinstance(h["world_rotation_maya_deg"], (list, tuple)):
            mr = h["world_rotation_maya_deg"]
            w_rot_deg = list(core.CoordinateBasis.maya_rot_to_blender((float(mr[0]), float(mr[1]), float(mr[2])), up_axis=maya_up))
        else:
            w_rot_deg = [0.0, 0.0, 0.0]

        w_scl = [1.0, 1.0, 1.0]
        if "world_scale" in h and isinstance(h["world_scale"], (list, tuple)) and len(h["world_scale"]) >= 3:
            w_scl = [float(h["world_scale"][0]), float(h["world_scale"][1]), float(h["world_scale"][2])]

        rot_mat = mathutils.Euler(
            (math.radians(w_rot_deg[0]), math.radians(w_rot_deg[1]), math.radians(w_rot_deg[2])),
            'XYZ',
        ).to_matrix().to_4x4()
        scl_mat = mathutils.Matrix.Diagonal((w_scl[0], w_scl[1], w_scl[2], 1.0))
        target_w_mat = mathutils.Matrix.Translation(mathutils.Vector(w_loc)) @ rot_mat @ scl_mat

        context.view_layer.update()
        curr_w_mat = obj.matrix_world.copy()

        # If this is a MESH and its world transform is changing from curr_w_mat to target_w_mat,
        # un-bake the delta from mesh.vertices ONLY if the mesh is not marked as frozen!
        is_incoming_frozen = bool(h.get("frozen", False))
        if obj.type == 'MESH' and obj.data and not is_incoming_frozen:
            delta_m = target_w_mat.inverted_safe() @ curr_w_mat
            diff_sq = sum((delta_m[r][c] - (1.0 if r == c else 0.0)) ** 2 for r in range(4) for c in range(4))
            if diff_sq > 1e-8:
                obj.data.transform(delta_m)
                obj.data.update()

        obj.parent = parent_obj
        obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
        obj.matrix_world = target_w_mat
        context.view_layer.update()

        # Reconstruct / update Blender Shape Keys if blendshapes metadata is present (Maya -> Blender)
        ex_dir = get_exchange_dir(context)
        sidecar_all = core.read_phase2_sidecar(ex_dir)
        bs_info = h.get("blendshapes") or (sidecar_all.get("blendshapes", {}).get(san_name) if isinstance(sidecar_all.get("blendshapes"), dict) else None)
        if bs_info and isinstance(bs_info, dict) and obj.type == 'MESH' and obj.data:
            key_blocks = bs_info.get("key_blocks", [])
            if key_blocks and isinstance(key_blocks, list):
                if not obj.data.shape_keys:
                    obj.shape_key_add(name="Basis", from_mix=False)
                for kb_entry in key_blocks:
                    if not isinstance(kb_entry, dict):
                        continue
                    kb_name = str(kb_entry.get("name", ""))
                    if not kb_name or kb_name == "Basis":
                        continue
                    kb_val = float(kb_entry.get("value", 1.0))
                    kb_target = obj.data.shape_keys.key_blocks.get(kb_name)
                    if not kb_target:
                        kb_target = obj.shape_key_add(name=kb_name, from_mix=False)
                    pts = kb_entry.get("points")
                    if pts and isinstance(pts, list) and len(pts) == len(obj.data.vertices):
                        flat_co = [c for p in pts for c in p]
                        kb_target.data.foreach_set("co", flat_co)
                    kb_target.value = kb_val
                obj.data.update()

        if bid:
            resolved_by_bid[bid] = obj
        if orig_name:
            resolved_by_name[orig_name] = obj
        if san_name:
            resolved_by_name[san_name] = obj
        resolved_by_name[obj.name] = obj
        touched_objects.append(obj)

    return touched_objects


def _extract_blender_phase2_payload(
    obj: bpy.types.Object,
    eval_mesh: bpy.types.Mesh,
    world_xform: mathutils.Matrix,
    maya_up: str,
    ex_dir: str,
    vertex_color_enabled: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Extracts canonical Phase 2 Vertex Color / Corner Color attributes and Material / PBR / UV data
    from a Blender object and its evaluated mesh.
    Respects vertex_color_enabled: when False, skips extracting vertex color attributes while preserving materials/UVs/textures.
    Returns (vertex_data_meta, materials_meta, sidecar_entry).
    """
    # Compute vertex positions & polygon centroids in both Blender World Space (m) and Maya World Space (cm)
    has_valid_vcols = bool(
        vertex_color_enabled
        and hasattr(eval_mesh, "color_attributes")
        and eval_mesh.color_attributes
        and any(not ca.name.startswith(".") for ca in eval_mesh.color_attributes)
    )
    has_multi_mats = len([s for s in obj.material_slots if s.material]) > 1
    needs_spatial_maps = has_valid_vcols or has_multi_mats

    v_bl = []
    v_maya = []
    p_bl = []
    p_maya = []
    corner_face_ids = []
    corner_vertex_ids = []
    if needs_spatial_maps:
        for v in eval_mesh.vertices:
            wp = world_xform @ v.co
            bx, by, bz = float(wp.x), float(wp.y), float(wp.z)
            v_bl.append([round(bx, 6), round(by, 6), round(bz, 6)])
            mx, my, mz = core.CoordinateBasis.blender_point_to_maya((bx, by, bz), up_axis=maya_up)
            v_maya.append([round(float(mx), 5), round(float(my), 5), round(float(mz), 5)])

        for p_idx, poly in enumerate(eval_mesh.polygons):
            wc = world_xform @ poly.center
            bx, by, bz = float(wc.x), float(wc.y), float(wc.z)
            p_bl.append([round(bx, 6), round(by, 6), round(bz, 6)])
            mx, my, mz = core.CoordinateBasis.blender_point_to_maya((bx, by, bz), up_axis=maya_up)
            p_maya.append([round(float(mx), 5), round(float(my), 5), round(float(mz), 5)])
            for l_idx in poly.loop_indices:
                corner_face_ids.append(int(p_idx))
                corner_vertex_ids.append(int(eval_mesh.loops[l_idx].vertex_index))

    active_ca_name = ""
    meta_color_attrs = []
    sidecar_color_attrs = []
    if has_valid_vcols:
        if eval_mesh.color_attributes.active_color:
            active_ca_name = str(eval_mesh.color_attributes.active_color.name)
        else:
            active_ca_name = str(eval_mesh.color_attributes[0].name)

        for ca in eval_mesh.color_attributes:
            # Skip internal Blender sculpt mask / face set attributes starting with '.'
            if ca.name.startswith("."):
                continue
            domain = str(ca.domain).upper()
            dtype = str(ca.data_type).upper()
            count = len(ca.data)
            raw_buf = [0.0] * (count * 4)
            try:
                ca.data.foreach_get("color", raw_buf)
                vals = [
                    [
                        round(raw_buf[i], 6),
                        round(raw_buf[i + 1], 6),
                        round(raw_buf[i + 2], 6),
                        round(raw_buf[i + 3], 6),
                    ]
                    for i in range(0, count * 4, 4)
                ]
            except Exception:
                vals = []
                for elem in ca.data:
                    c = getattr(elem, "color", (1.0, 1.0, 1.0, 1.0))
                    vals.append([round(float(c[0]), 6), round(float(c[1]), 6), round(float(c[2]), 6), round(float(c[3]), 6)])
            attr_full = {
                "name": str(ca.name),
                "domain": domain,
                "data_type": dtype,
                "channels": 4,
                "count": count,
                "values": vals,
            }
            sidecar_color_attrs.append(attr_full)
            meta_entry = dict(attr_full)
            if count > 4096:
                meta_entry.pop("values", None)
            meta_color_attrs.append(meta_entry)

    vertex_data_meta = {
        "vertex_color_enabled": bool(vertex_color_enabled),
        "active_color_attribute": active_ca_name,
        "sidecar_file": core.get_phase2_sidecar_path(ex_dir),
        "color_attributes": meta_color_attrs,
    }

    # Extract UV sets, material slots, per-polygon material indices, and material definitions
    uv_sets = [str(uv.name) for uv in eval_mesh.uv_layers] if hasattr(eval_mesh, "uv_layers") else []
    active_uv = (
        str(eval_mesh.uv_layers.active.name)
        if (hasattr(eval_mesh, "uv_layers") and eval_mesh.uv_layers and eval_mesh.uv_layers.active)
        else (uv_sets[0] if uv_sets else "UVMap")
    )

    slots_list = []
    mat_defs = {}
    for s_idx, slot in enumerate(obj.material_slots):
        mat = slot.material
        if mat:
            m_id = _get_or_set_blender_material_id(mat)
            slots_list.append({
                "slot_index": int(s_idx),
                "material_name": str(mat.name),
                "material_id": m_id,
            })
            mat_defs[str(mat.name)] = _extract_blender_material_def(mat, uv_sets, ex_dir)
        else:
            slots_list.append({
                "slot_index": int(s_idx),
                "material_name": "",
                "material_id": "",
            })

    face_mat_indices = [int(p.material_index) for p in eval_mesh.polygons]
    materials_meta = {
        "slots": slots_list,
        "face_material_indices": face_mat_indices if len(face_mat_indices) <= 8192 else [],
        "uv_sets": uv_sets,
        "active_uv_set": active_uv,
        "definitions": mat_defs,
    }

    sidecar_entry = {
        "vertex_positions_blender": v_bl,
        "vertex_positions_maya": v_maya,
        "poly_centroids_blender": p_bl,
        "poly_centroids_maya": p_maya,
        "corner_face_ids": corner_face_ids,
        "corner_vertex_ids": corner_vertex_ids,
        "vertex_data": {
            "active_color_attribute": active_ca_name,
            "color_attributes": sidecar_color_attrs,
        },
        "materials": {
            "slots": slots_list,
            "face_material_indices": face_mat_indices,
            "uv_sets": uv_sets,
            "active_uv_set": active_uv,
            "definitions": mat_defs,
        },
    }
    return vertex_data_meta, materials_meta, sidecar_entry


def _apply_blender_phase2_payload(
    target_obj: bpy.types.Object,
    obj_meta: Dict[str, Any],
    sidecar_entry: Dict[str, Any],
    ex_dir: str,
    vertex_color_enabled: bool = True,
) -> None:
    """
    Applies Phase 2 Vertex Color / Corner Color attributes and Material / PBR / UV data
    onto target_obj.data in Blender.
    Handles both unchanged topology (with automatic spatial de-scrambling if FBX reordered vertices/faces)
    and remeshed topology (nearest-vertex/face-centroid KDTree projection).
    """
    mesh = target_obj.data
    if not mesh:
        return

    v_data = (sidecar_entry.get("vertex_data") if isinstance(sidecar_entry, dict) else None) or obj_meta.get("vertex_data") or {}
    m_data = (sidecar_entry.get("materials") if isinstance(sidecar_entry, dict) else None) or obj_meta.get("materials") or {}

    saved_v_bl = sidecar_entry.get("vertex_positions_blender", []) if isinstance(sidecar_entry, dict) else []
    saved_p_bl = sidecar_entry.get("poly_centroids_blender", []) if isinstance(sidecar_entry, dict) else []
    corner_f_ids = sidecar_entry.get("corner_face_ids", []) if isinstance(sidecar_entry, dict) else []
    corner_v_ids = sidecar_entry.get("corner_vertex_ids", []) if isinstance(sidecar_entry, dict) else []

    world_mat = target_obj.matrix_world
    curr_v_bl = [world_mat @ v.co for v in mesh.vertices]
    curr_p_bl = [world_mat @ p.center for p in mesh.polygons]

    # Build target_v -> source_v mapping
    v_map: List[int] = list(range(len(mesh.vertices)))
    if saved_v_bl:
        same_order = (
            len(saved_v_bl) == len(curr_v_bl)
            and all(
                (curr_v_bl[i] - mathutils.Vector(saved_v_bl[i])).length_squared < 1e-6
                for i in range(len(curr_v_bl))
            )
        )
        if not same_order:
            kd_v = mathutils.kdtree.KDTree(len(saved_v_bl))
            for idx, pt in enumerate(saved_v_bl):
                kd_v.insert(pt, idx)
            kd_v.balance()
            v_map = [kd_v.find(pt)[1] for pt in curr_v_bl]

    # Build target_p -> source_p mapping
    p_map: List[int] = list(range(len(mesh.polygons)))
    if saved_p_bl:
        same_poly_order = (
            len(saved_p_bl) == len(curr_p_bl)
            and all(
                (curr_p_bl[i] - mathutils.Vector(saved_p_bl[i])).length_squared < 1e-6
                for i in range(len(curr_p_bl))
            )
        )
        if not same_poly_order:
            kd_p = mathutils.kdtree.KDTree(len(saved_p_bl))
            for idx, pt in enumerate(saved_p_bl):
                kd_p.insert(pt, idx)
            kd_p.balance()
            p_map = [kd_p.find(pt)[1] for pt in curr_p_bl]

    # 1. Apply Vertex Color / Corner Color Attributes (only when vertex_color_enabled is True)
    color_attrs = v_data.get("color_attributes", []) if (vertex_color_enabled and isinstance(v_data, dict)) else []
    active_ca_name = str(v_data.get("active_color_attribute") or "") if vertex_color_enabled else ""
    if vertex_color_enabled and isinstance(color_attrs, list) and color_attrs and hasattr(mesh, "color_attributes"):
        # Remove default FBX vertex color attributes if they aren't in incoming color_attrs
        incoming_names = {str(a.get("name")) for a in color_attrs if isinstance(a, dict) and a.get("name")}
        for existing_ca in list(mesh.color_attributes):
            if existing_ca.name not in incoming_names and not existing_ca.name.startswith("."):
                try:
                    mesh.color_attributes.remove(existing_ca)
                except Exception:
                    pass

        for attr in color_attrs:
            if not isinstance(attr, dict):
                continue
            ca_name = str(attr.get("name") or "Col")
            domain = str(attr.get("domain") or "POINT").upper()
            if domain not in ("POINT", "CORNER"):
                domain = "POINT"
            dtype = str(attr.get("data_type") or "FLOAT_COLOR").upper()
            if dtype not in ("FLOAT_COLOR", "BYTE_COLOR"):
                dtype = "FLOAT_COLOR"
            vals = attr.get("values", [])
            if not isinstance(vals, list) or not vals:
                continue

            ca = mesh.color_attributes.get(ca_name)
            if ca and (str(ca.domain).upper() != domain or str(ca.data_type).upper() != dtype):
                try:
                    mesh.color_attributes.remove(ca)
                    ca = None
                except Exception:
                    pass
            if not ca:
                ca = mesh.color_attributes.new(name=ca_name, type=dtype, domain=domain)

            if domain == "POINT":
                n_vals = len(vals)
                if n_vals == len(mesh.vertices) and v_map == list(range(n_vals)):
                    flat_c = []
                    for c in vals:
                        flat_c.extend((float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) >= 4 else 1.0))
                    try:
                        ca.data.foreach_set("color", flat_c)
                    except Exception:
                        for t_v, c in enumerate(vals):
                            ca.data[t_v].color = (float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) >= 4 else 1.0)
                else:
                    for t_v in range(len(mesh.vertices)):
                        s_v = v_map[t_v] if t_v < len(v_map) else t_v
                        if 0 <= s_v < n_vals:
                            c = vals[s_v]
                            ca.data[t_v].color = (
                                float(c[0]),
                                float(c[1]),
                                float(c[2]),
                                float(c[3]) if len(c) >= 4 else 1.0,
                            )
            else:
                if len(vals) == len(mesh.loops) and v_map == list(range(len(mesh.vertices))) and p_map == list(range(len(mesh.polygons))):
                    flat_c = []
                    for c in vals:
                        flat_c.extend((float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) >= 4 else 1.0))
                    try:
                        ca.data.foreach_set("color", flat_c)
                        continue
                    except Exception:
                        pass
                # CORNER domain: map (s_p, s_v) -> rgba, and also build per-source-polygon corner fallback list
                corner_lookup: Dict[Tuple[int, int], List[float]] = {}
                poly_corners_fallback: Dict[int, List[Tuple[int, List[float]]]] = {}
                c_f_list = attr.get("corner_face_ids") or corner_f_ids
                c_v_list = attr.get("corner_vertex_ids") or corner_v_ids
                if c_f_list and c_v_list and len(c_f_list) == len(vals) and len(c_v_list) == len(vals):
                    for idx_c, rgba in enumerate(vals):
                        sp = int(c_f_list[idx_c])
                        sv = int(c_v_list[idx_c])
                        c4 = [float(rgba[0]), float(rgba[1]), float(rgba[2]), float(rgba[3]) if len(rgba) >= 4 else 1.0]
                        corner_lookup[(sp, sv)] = c4
                        poly_corners_fallback.setdefault(sp, []).append((sv, c4))

                for t_p, poly in enumerate(mesh.polygons):
                    s_p = p_map[t_p] if t_p < len(p_map) else t_p
                    for l_idx in poly.loop_indices:
                        t_v = int(mesh.loops[l_idx].vertex_index)
                        s_v = v_map[t_v] if t_v < len(v_map) else t_v
                        rgba_val = corner_lookup.get((s_p, s_v))
                        if rgba_val is None and s_p in poly_corners_fallback and saved_v_bl:
                            # Pick corner of s_p closest to curr_v_bl[t_v]
                            best_dist = 1e30
                            t_pt = curr_v_bl[t_v]
                            for cand_sv, cand_c4 in poly_corners_fallback[s_p]:
                                if 0 <= cand_sv < len(saved_v_bl):
                                    d2 = (t_pt - mathutils.Vector(saved_v_bl[cand_sv])).length_squared
                                    if d2 < best_dist:
                                        best_dist = d2
                                        rgba_val = cand_c4
                        if rgba_val is None and l_idx < len(vals):
                            c = vals[l_idx]
                            rgba_val = [float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) >= 4 else 1.0]
                        if rgba_val is not None:
                            ca.data[l_idx].color = (rgba_val[0], rgba_val[1], rgba_val[2], rgba_val[3])

        if active_ca_name and mesh.color_attributes.get(active_ca_name):
            mesh.color_attributes.active_color = mesh.color_attributes.get(active_ca_name)
        elif len(mesh.color_attributes) > 0:
            mesh.color_attributes.active_color = mesh.color_attributes[0]

    # 2. Apply UV set names
    if isinstance(m_data, dict):
        uv_sets = m_data.get("uv_sets", [])
        if isinstance(uv_sets, list) and uv_sets and hasattr(mesh, "uv_layers"):
            for idx_u, uv_nm in enumerate(uv_sets):
                if not uv_nm:
                    continue
                if idx_u < len(mesh.uv_layers):
                    try:
                        mesh.uv_layers[idx_u].name = str(uv_nm)
                    except Exception:
                        pass
                elif str(uv_nm) not in mesh.uv_layers:
                    try:
                        mesh.uv_layers.new(name=str(uv_nm))
                    except Exception:
                        pass

        # 3. Apply Material Slots, Definitions, and Per-Polygon Material Assignments
        slots = m_data.get("slots", [])
        defs = m_data.get("definitions", {}) if isinstance(m_data.get("definitions"), dict) else {}
        face_indices = m_data.get("face_material_indices", [])

        if isinstance(slots, list) and slots:
            existing_slot_ids = [str(m.get("sisync_material_id") or "") if m else "" for m in mesh.materials]
            mesh.materials.clear()
            ordered_slots = sorted(
                slots,
                key=lambda s: (
                    existing_slot_ids.index(str(s.get("material_id") or ""))
                    if (str(s.get("material_id") or "") and str(s.get("material_id") or "") in existing_slot_ids)
                    else 1000 + int(s.get("slot_index", 0))
                ),
            )
            slot_remap: Dict[int, int] = {}
            for new_s_idx, s_info in enumerate(ordered_slots):
                orig_s_idx = int(s_info.get("slot_index", new_s_idx))
                slot_remap[orig_s_idx] = new_s_idx
                m_name = str(s_info.get("material_name") or "")
                m_id = str(s_info.get("material_id") or "")
                if not m_name and not m_id:
                    mesh.materials.append(None)
                    continue
                bl_mat = _find_matching_blender_material(m_name, m_id)
                mdef = defs.get(m_name) or next(
                    (d for d in defs.values() if isinstance(d, dict) and str(d.get("material_id", "")) == m_id),
                    None,
                )
                if mdef:
                    _apply_blender_material_def(bl_mat, mdef, ex_dir)
                mesh.materials.append(bl_mat)

            if isinstance(face_indices, list) and face_indices:
                max_slot = max(0, len(mesh.materials) - 1)
                n_src_faces = len(face_indices)
                for t_p, poly in enumerate(mesh.polygons):
                    s_p = p_map[t_p] if t_p < len(p_map) else t_p
                    if 0 <= s_p < n_src_faces:
                        mapped_idx = slot_remap.get(int(face_indices[s_p]), int(face_indices[s_p]))
                        poly.material_index = max(0, min(max_slot, mapped_idx))

    mesh.update()


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

        context.view_layer.update()
        prefs = get_preferences(context)
        ex_dir = get_exchange_dir(context)
        b2m_fbx = core.get_blender_to_maya_fbx(ex_dir)
        legacy_fbx = core.get_legacy_exchange_fbx(ex_dir)

        flags = _get_active_transfer_flags(context, ex_dir)
        hier_enabled = bool(flags["hierarchy"])
        vcol_enabled = bool(flags["vertex_color"])
        bs_enabled = bool(flags.get("blendshapes", False))
        core.write_transfer_toggles(
            all_enabled=bool(flags["all"]),
            hierarchy_enabled=bool(getattr(prefs, "sync_hierarchy", False)) if prefs else hier_enabled,
            vertex_color_enabled=bool(getattr(prefs, "sync_vertex_color", True)) if prefs else vcol_enabled,
            custom_dir=ex_dir,
            blendshapes_enabled=bs_enabled,
        )

        _cleanup_blender_blendshapes_group()

        remap_mat = _build_gob_remap_matrix(prefs)
        maya_up = prefs.maya_up_axis if prefs else "Y"

        if hier_enabled:
            seed_objs = list(context.selected_objects)
            if active_obj and active_obj not in seed_objs:
                seed_objs.insert(0, active_obj)
            hierarchy_entries, hier_meshes = _collect_blender_hierarchy_data(seed_objs, maya_up=maya_up, prefs=prefs)
            selected_meshes = hier_meshes
            do_freeze_loc = False
            do_freeze_rot = False
        else:
            hierarchy_entries = []
            selected_meshes = [
                o for o in context.selected_objects
                if o.type == 'MESH' and not bool(o.get("sisync_is_blendshape_mesh"))
            ]
            if active_obj and active_obj.type == 'MESH' and not bool(active_obj.get("sisync_is_blendshape_mesh")) and active_obj not in selected_meshes:
                selected_meshes.insert(0, active_obj)
            do_freeze_loc = bool(prefs.freeze_location) if prefs else False
            do_freeze_rot = bool(prefs.freeze_rotation) if prefs else True

        if not selected_meshes and not (hier_enabled and hierarchy_entries):
            return None

        temp_records = []
        staging_col = None
        meta_objects: Dict[str, Any] = {}
        sidecar_objects: Dict[str, Any] = {}
        rev = core.next_revision()
        added_bs_group_entry = False

        if selected_meshes:
            try:
                staging_col = bpy.data.collections.new("__SiSync_EXPORT_TEMP__")
                context.scene.collection.children.link(staging_col)

                for obj in selected_meshes:
                    if bool(obj.get("sisync_is_blendshape_mesh")):
                        continue
                    orig_name = obj.name
                    safe = _sanitize_name(orig_name)
                    if orig_name.endswith(".003") and not safe.endswith("_003"):
                        safe = f"{safe}_003"

                    # Ensure stable bridge_id is stamped on the Blender object
                    b_id = str(obj.get("sisync_bridge_id") or "")
                    if not b_id:
                        b_id = core.generate_bridge_id(orig_name)
                        obj["sisync_bridge_id"] = b_id
                    obj["sisync_revision"] = rev

                    world_xform = remap_mat @ obj.matrix_world
                    wloc = world_xform.to_translation()
                    if _is_fbx_yup_cm_wrapper_matrix(obj.matrix_world, maya_up=maya_up):
                        wrot_quat = mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
                        wrot_deg = [0.0, 0.0, 0.0]
                    else:
                        wrot_quat = obj.matrix_world.to_quaternion()
                        wrot_deg = [math.degrees(a) for a in wrot_quat.to_euler('XYZ')]
                    has_parent = obj.parent is not None

                    # Ensure any Multires modifier exports the sculpted level
                    for mod in obj.modifiers:
                        if mod.type == 'MULTIRES' and hasattr(mod, "sculpt_levels"):
                            if mod.levels < mod.sculpt_levels:
                                mod.levels = mod.sculpt_levels

                    # When Export Blend Shapes = ON, base mesh is evaluated as Basis (rest) so target deformations
                    # are not double-applied; when Export Blend Shapes = OFF, base mesh is evaluated with the
                    # exact currently displayed Shape Key state via Blender's dependency graph.
                    base_sk_mode = "BASIS" if bs_enabled else "CURRENT"
                    temp_mesh = _evaluate_mesh_without_armature(context, obj, shapekey_mode=base_sk_mode)

                    # Extract Phase 2 canonical Vertex Color / Corner Color & Material / PBR / UV payload BEFORE temp_mesh is transformed
                    v_data_meta, m_data_meta, sidecar_entry = _extract_blender_phase2_payload(
                        obj=obj,
                        eval_mesh=temp_mesh,
                        world_xform=world_xform,
                        maya_up=maya_up,
                        ex_dir=ex_dir,
                        vertex_color_enabled=vcol_enabled,
                    )
                    sidecar_objects[safe] = sidecar_entry

                    obj.name = f"{safe}__sisync_orig"
                    temp_obj = bpy.data.objects.new(safe, temp_mesh)
                    staging_col.objects.link(temp_obj)
                    try:
                        if temp_obj.data.shape_keys and temp_obj.data.shape_keys.key_blocks:
                            for kb_rem in reversed(list(temp_obj.data.shape_keys.key_blocks)):
                                temp_obj.shape_key_remove(kb_rem)
                    except Exception:
                        pass

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

                    meta_objects[safe] = {
                        "bridge_id": b_id,
                        "blender_orig_name": orig_name,
                        "revision": rev,
                        "location_blender_m": [float(target_loc.x), float(target_loc.y), float(target_loc.z)],
                        "rotation_blender_deg": [0.0, 0.0, 0.0] if do_freeze_rot else wrot_deg,
                        "location_maya_cm": maya_loc_cm,
                        "rotation_maya_deg": maya_rot_deg,
                        "freeze_location": do_freeze_loc,
                        "freeze_rotation": do_freeze_rot,
                        "has_parent": has_parent,
                        "vertex_count": len(temp_mesh.vertices),
                        "polygon_count": len(temp_mesh.polygons),
                        "vertex_data": v_data_meta,
                        "materials": m_data_meta,
                    }

                    # Export Blend Shapes into dedicated Maya 'BlendShapes' group when Export Blend Shapes is ON
                    # Temporary staging meshes are linked ONLY to staging_col (__SiSync_EXPORT_TEMP__) and deleted in finally.
                    if bs_enabled and obj.data and obj.data.shape_keys and len(obj.data.shape_keys.key_blocks) > 1:
                        root_parent = obj.parent
                        while root_parent and root_parent.parent and root_parent.parent.type in ('EMPTY', 'MESH'):
                            root_parent = root_parent.parent

                        bs_group_bid = core.generate_bridge_id("group:BlendShapes")

                        if not added_bs_group_entry:
                            p_name = str(root_parent.name) if root_parent else ""
                            p_safe = _sanitize_name(p_name) if p_name else ""
                            p_bid = str(root_parent.get("sisync_bridge_id") or "") if root_parent else ""
                            if p_name and not hier_enabled:
                                hierarchy_entries.append({
                                    "name": p_name,
                                    "original_blender_name": p_name,
                                    "sanitized_name": p_safe,
                                    "node_type": "EMPTY",
                                    "bridge_id": p_bid,
                                    "parent_name": "",
                                    "parent_sanitized_name": "",
                                    "parent_bridge_id": "",
                                    "depth": 0,
                                    "world_location_blender_m": [0.0, 0.0, 0.0],
                                    "world_rotation_blender_deg": [0.0, 0.0, 0.0],
                                    "world_scale": [1.0, 1.0, 1.0],
                                    "local_location_blender_m": [0.0, 0.0, 0.0],
                                    "local_rotation_blender_deg": [0.0, 0.0, 0.0],
                                    "local_scale": [1.0, 1.0, 1.0],
                                    "world_location_maya_cm": [0.0, 0.0, 0.0],
                                    "world_rotation_maya_deg": [0.0, 0.0, 0.0],
                                })
                            hierarchy_entries.append({
                                "name": "BlendShapes",
                                "original_blender_name": "BlendShapes",
                                "sanitized_name": "BlendShapes",
                                "node_type": "EMPTY",
                                "bridge_id": bs_group_bid,
                                "parent_name": p_name,
                                "parent_sanitized_name": p_safe,
                                "parent_bridge_id": p_bid,
                                "depth": 1 if p_name else 0,
                                "world_location_blender_m": [0.0, 0.0, 0.0],
                                "world_rotation_blender_deg": [0.0, 0.0, 0.0],
                                "world_scale": [1.0, 1.0, 1.0],
                                "local_location_blender_m": [0.0, 0.0, 0.0],
                                "local_rotation_blender_deg": [0.0, 0.0, 0.0],
                                "local_scale": [1.0, 1.0, 1.0],
                                "world_location_maya_cm": [0.0, 0.0, 0.0],
                                "world_rotation_maya_deg": [0.0, 0.0, 0.0],
                                "is_blendshapes_group": True,
                            })
                            added_bs_group_entry = True

                        all_kbs = list(obj.data.shape_keys.key_blocks)
                        target_kbs = [kb for kb in all_kbs[1:] if not kb.mute]
                        if not target_kbs:
                            target_kbs = list(all_kbs[1:])

                        saved_kb_vals = [(k, float(k.value), bool(k.mute)) for k in all_kbs]
                        saved_show_only = bool(obj.show_only_shape_key)
                        saved_active_sk_idx = int(obj.active_shape_key_index or 0)
                        try:
                            for kb in target_kbs:
                                bs_eval_mesh = _evaluate_mesh_without_armature(
                                    context,
                                    obj,
                                    active_shapekey_name=kb.name,
                                    shapekey_mode="TARGET",
                                )
                                bs_raw_name = str(kb.name)
                                bs_safe = _sanitize_name(bs_raw_name)
                                if bs_safe in meta_objects:
                                    bs_safe = f"{safe}_{bs_safe}"
                                bs_bid = core.generate_bridge_id(f"blendshape:{orig_name}:{bs_raw_name}")

                                temp_bs_obj = bpy.data.objects.new(bs_safe, bs_eval_mesh)
                                staging_col.objects.link(temp_bs_obj)
                                try:
                                    if temp_bs_obj.data.shape_keys and temp_bs_obj.data.shape_keys.key_blocks:
                                        for kb_rem in reversed(list(temp_bs_obj.data.shape_keys.key_blocks)):
                                            temp_bs_obj.shape_key_remove(kb_rem)
                                except Exception:
                                    pass
                                temp_bs_obj.matrix_world = temp_obj_mat
                                bs_eval_mesh.transform(vert_xform)
                                if remap_mat.determinant() < 0.0:
                                    bs_eval_mesh.flip_normals()
                                bs_eval_mesh.update()
                                temp_records.append((None, "", temp_bs_obj, bs_eval_mesh))

                                meta_objects[bs_safe] = {
                                    "bridge_id": bs_bid,
                                    "blender_orig_name": bs_safe,
                                    "revision": rev,
                                    "location_blender_m": [float(target_loc.x), float(target_loc.y), float(target_loc.z)],
                                    "rotation_blender_deg": [0.0, 0.0, 0.0] if do_freeze_rot else wrot_deg,
                                    "location_maya_cm": maya_loc_cm,
                                    "rotation_maya_deg": maya_rot_deg,
                                    "freeze_location": do_freeze_loc,
                                    "freeze_rotation": do_freeze_rot,
                                    "has_parent": True,
                                    "is_blendshape": True,
                                    "source_mesh": orig_name,
                                    "shapekey_name": bs_raw_name,
                                    "parent_group": "BlendShapes",
                                    "vertex_count": len(bs_eval_mesh.vertices),
                                    "polygon_count": len(bs_eval_mesh.polygons),
                                    "vertex_data": {"has_vertex_color": False, "attributes": []},
                                    "materials": m_data_meta,
                                }
                                sidecar_objects[bs_safe] = {
                                    "bridge_id": bs_bid,
                                    "vertex_data": {"has_vertex_color": False, "attributes": []},
                                    "materials": sidecar_entry.get("materials", {}),
                                }
                                hierarchy_entries.append({
                                    "name": bs_safe,
                                    "original_blender_name": bs_safe,
                                    "sanitized_name": bs_safe,
                                    "node_type": "MESH",
                                    "bridge_id": bs_bid,
                                    "parent_name": "BlendShapes",
                                    "parent_sanitized_name": "BlendShapes",
                                    "parent_bridge_id": bs_group_bid,
                                    "depth": 2 if root_parent else 1,
                                    "world_location_blender_m": [float(target_loc.x), float(target_loc.y), float(target_loc.z)],
                                    "world_rotation_blender_deg": [0.0, 0.0, 0.0] if do_freeze_rot else wrot_deg,
                                    "world_scale": [1.0, 1.0, 1.0],
                                    "local_location_blender_m": [0.0, 0.0, 0.0],
                                    "local_rotation_blender_deg": [0.0, 0.0, 0.0],
                                    "local_scale": [1.0, 1.0, 1.0],
                                    "world_location_maya_cm": maya_loc_cm,
                                    "world_rotation_maya_deg": maya_rot_deg,
                                    "is_blendshape_mesh": True,
                                })
                        finally:
                            obj.show_only_shape_key = saved_show_only
                            obj.active_shape_key_index = saved_active_sk_idx
                            for k, orig_val, orig_mute in saved_kb_vals:
                                k.value = orig_val
                                k.mute = orig_mute
                            obj.data.update()
                            obj.update_tag()
                            context.view_layer.update()

                for o in list(context.selected_objects):
                    o.select_set(False)
                for _, _, temp_obj, _ in temp_records:
                    temp_obj.select_set(True)
                context.view_layer.objects.active = temp_records[0][2]

                exp_forward = '-Z' if maya_up == 'Y' else '-Y'
                exp_up = 'Y' if maya_up == 'Y' else 'Z'
                b2m_rev_fbx = os.path.join(ex_dir, f"blender_to_maya_r{rev % 8}.fbx").replace("\\", "/")

                bpy.ops.export_scene.fbx(
                    filepath=b2m_rev_fbx,
                    check_existing=False,
                    use_selection=True,
                    global_scale=1.0,
                    apply_unit_scale=True,
                    apply_scale_options='FBX_SCALE_NONE',
                    bake_space_transform=True,
                    object_types={'MESH'},
                    use_mesh_modifiers=False,
                    mesh_smooth_type='FACE',
                    colors_type='LINEAR' if vcol_enabled else 'NONE',
                    axis_forward=exp_forward,
                    axis_up=exp_up,
                )
                try:
                    shutil.copy2(b2m_rev_fbx, b2m_fbx)
                except Exception:
                    pass
                b2m_fbx = b2m_rev_fbx
            finally:
                # Unconditional restoration of original objects, names, temporary staging collection, selection, and mode
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
                        if orig_obj is not None and orig_name:
                            orig_obj.name = orig_name
                    except Exception:
                        pass

                if staging_col is not None:
                    try:
                        for co in list(staging_col.objects):
                            m_d = co.data if co.type == 'MESH' else None
                            bpy.data.objects.remove(co, do_unlink=True)
                            if m_d and m_d.users == 0:
                                bpy.data.meshes.remove(m_d, do_unlink=True)
                        bpy.data.collections.remove(staging_col, do_unlink=True)
                    except Exception:
                        pass

                _cleanup_blender_blendshapes_group()

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
        sidecar_path = core.write_phase2_sidecar(
            {
                "revision": rev,
                "source": "blender",
                "destination": "maya",
                "timestamp": export_ts,
                "sync_all": flags["all"],
                "sync_hierarchy": hier_enabled,
                "sync_vertex_color": vcol_enabled,
                "sync_blendshapes": bs_enabled,
                "hierarchy": hierarchy_entries,
                "objects": sidecar_objects,
            },
            custom_dir=ex_dir,
        )
        core.write_metadata(
            {
                "revision": rev,
                "source": "blender",
                "destination": "maya",
                "format": "fbx",
                "file_path": b2m_fbx,
                "fbx_path": b2m_fbx,
                "phase2_sidecar": sidecar_path,
                "timestamp": export_ts,
                "export_timestamp": export_ts,
                "maya_up_axis": maya_up,
                "freeze_location": do_freeze_loc,
                "freeze_rotation": do_freeze_rot,
                "sync_all": flags["all"],
                "sync_hierarchy": hier_enabled,
                "sync_vertex_color": vcol_enabled,
                "sync_blendshapes": bs_enabled,
                "hierarchy": hierarchy_entries,
                "objects": meta_objects,
            },
            custom_dir=ex_dir,
        )
        core.log_event("blender", "EXPORT_TO_MAYA", "success", source="blender", destination="maya", revision=rev, extra=b2m_fbx)
        print(f"[SiSync] Exported {len(meta_objects)} mesh(es) (hier={hier_enabled}, vcol={vcol_enabled}, blendshapes={bs_enabled}) -> {b2m_fbx} (rev {rev})")
        return b2m_fbx

    @classmethod
    def import_from_maya(cls, context) -> List[bpy.types.Object]:
        """
        Imports maya_to_blender.fbx (or metadata-specified file_path) from Maya into Blender.
        Matches by bridge_id, exact name, sanitized name, or single-mesh fallback.
        Preserves and synchronizes Phase 2 Vertex Colors, Corner Colors, Material Slots, Face Assignments, PBR & UVs.
        """
        global LAST_IMPORT_TIMESTAMP
        ex_dir = get_exchange_dir(context)
        meta = core.read_metadata(ex_dir)
        meta_objects = meta.get("objects", {}) if isinstance(meta.get("objects"), dict) else {}
        sidecar_all = core.read_phase2_sidecar(ex_dir)
        sidecar_objects = sidecar_all.get("objects", {}) if isinstance(sidecar_all.get("objects"), dict) else {}
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
        maya_up = prefs.maya_up_axis if prefs else "Y"

        flags = _get_active_transfer_flags(context, ex_dir)
        hier_enabled = bool(meta.get("sync_hierarchy", False)) if "sync_hierarchy" in meta else bool(flags["hierarchy"])
        vcol_enabled = bool(meta.get("sync_vertex_color", True)) if "sync_vertex_color" in meta else bool(flags["vertex_color"])
        if not flags["vertex_color"]:
            vcol_enabled = False
        if not flags["hierarchy"] and "sync_hierarchy" not in meta:
            hier_enabled = False

        if hier_enabled:
            do_freeze_loc = False
            do_freeze_rot = False
        else:
            do_freeze_loc = bool(prefs.freeze_location) if prefs else False
            do_freeze_rot = bool(prefs.freeze_rotation) if prefs else True

        # If exporting an Empty-only hierarchy from Maya with 0 meshes in metadata
        if hier_enabled and meta.get("hierarchy") and not meta_objects:
            hier_objs = _apply_blender_hierarchy(context, meta.get("hierarchy", []), maya_up=maya_up)
            core.log_event("blender", "IMPORT_FROM_MAYA", "success", source="maya", destination="blender", extra=f"hierarchy_only={len(hier_objs)}")
            return hier_objs

        pre_objs = set(bpy.data.objects.keys())
        pre_mats = set(bpy.data.materials)
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
        for nm_obj in new_meshes:
            try:
                nm_obj.data.materials.clear()
            except Exception:
                pass
        for fbx_mat in list(set(bpy.data.materials) - pre_mats):
            try:
                bpy.data.materials.remove(fbx_mat)
            except Exception:
                pass

        result_objects: List[bpy.types.Object] = []
        single_mesh_mode = len(new_meshes) == 1

        for imp_obj in new_meshes:
            base_name = _strip_duplicate_suffix(imp_obj.name)
            obj_meta = meta_objects.get(base_name, {}) if isinstance(meta_objects, dict) else {}
            if not obj_meta and single_mesh_mode and len(meta_objects) == 1:
                obj_meta = next(iter(meta_objects.values()))

            sidecar_entry = sidecar_objects.get(base_name, {}) if isinstance(sidecar_objects, dict) else {}
            if not sidecar_entry and single_mesh_mode and len(sidecar_objects) == 1:
                sidecar_entry = next(iter(sidecar_objects.values()))

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
                # Capture fallback Phase 2 data from target_obj BEFORE replacing target_obj.data
                fallback_sidecar = None
                if target_obj.data and (
                    (hasattr(target_obj.data, "color_attributes") and len(target_obj.data.color_attributes) > 0)
                    or len(target_obj.material_slots) > 0
                ):
                    try:
                        _, _, fallback_sidecar = _extract_blender_phase2_payload(
                            obj=target_obj,
                            eval_mesh=target_obj.data,
                            world_xform=target_obj.matrix_world,
                            maya_up=maya_up,
                            ex_dir=ex_dir,
                            vertex_color_enabled=True,
                        )
                    except Exception:
                        fallback_sidecar = None

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

                # Merge fallback color attributes/materials if Maya export did not include them (e.g. remeshed in Maya)
                effective_sidecar = dict(sidecar_entry) if isinstance(sidecar_entry, dict) else {}
                if fallback_sidecar and isinstance(fallback_sidecar, dict):
                    inc_ca = effective_sidecar.get("vertex_data", {}).get("color_attributes", [])
                    if (not vcol_enabled or not inc_ca) and fallback_sidecar.get("vertex_data", {}).get("color_attributes"):
                        effective_sidecar["vertex_data"] = fallback_sidecar["vertex_data"]
                        effective_sidecar["vertex_positions_blender"] = fallback_sidecar.get("vertex_positions_blender", [])
                        effective_sidecar["poly_centroids_blender"] = fallback_sidecar.get("poly_centroids_blender", [])
                        effective_sidecar["corner_face_ids"] = fallback_sidecar.get("corner_face_ids", [])
                        effective_sidecar["corner_vertex_ids"] = fallback_sidecar.get("corner_vertex_ids", [])
                    inc_slots = effective_sidecar.get("materials", {}).get("slots", [])
                    if not inc_slots and fallback_sidecar.get("materials", {}).get("slots"):
                        effective_sidecar["materials"] = fallback_sidecar["materials"]
                        if not effective_sidecar.get("poly_centroids_blender"):
                            effective_sidecar["poly_centroids_blender"] = fallback_sidecar.get("poly_centroids_blender", [])

                # When vcol_enabled is False, only restore pre-existing fallback color attributes if target_obj had them, never Maya edits
                apply_vcol = vcol_enabled or bool(fallback_sidecar and fallback_sidecar.get("vertex_data", {}).get("color_attributes"))
                if not vcol_enabled and not (fallback_sidecar and fallback_sidecar.get("vertex_data", {}).get("color_attributes")):
                    if hasattr(target_obj.data, "color_attributes"):
                        for existing_ca in list(target_obj.data.color_attributes):
                            if not existing_ca.name.startswith("."):
                                try:
                                    target_obj.data.color_attributes.remove(existing_ca)
                                except Exception:
                                    pass
                _apply_blender_phase2_payload(target_obj, obj_meta, effective_sidecar, ex_dir, vertex_color_enabled=apply_vcol)

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
                if not vcol_enabled and hasattr(imp_mesh, "color_attributes"):
                    for existing_ca in list(imp_mesh.color_attributes):
                        if not existing_ca.name.startswith("."):
                            try:
                                imp_mesh.color_attributes.remove(existing_ca)
                            except Exception:
                                pass
                _apply_blender_phase2_payload(imp_obj, obj_meta, sidecar_entry, ex_dir, vertex_color_enabled=vcol_enabled)
                imp_mesh.update_tag()
                imp_obj.update_tag()
                result_objects.append(imp_obj)

        for emp in new_empties:
            if emp.name in bpy.data.objects:
                try:
                    bpy.data.objects.remove(emp, do_unlink=True)
                except Exception:
                    pass

        if hier_enabled and meta.get("hierarchy"):
            hier_objs = _apply_blender_hierarchy(context, meta.get("hierarchy", []), maya_up=maya_up)
            for ho in hier_objs:
                if ho not in result_objects:
                    result_objects.append(ho)

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
