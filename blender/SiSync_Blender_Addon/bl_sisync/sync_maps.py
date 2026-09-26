#creator siddhartha
# -*- coding: utf-8 -*-
"""
SiSync — Blender PBR Material Map Synchronization & Reload Engine
Selectively reloads only PBR texture images connected to supported shader channels
and associated with the SiSync bridge texture directory, logging missing textures clearly.
"""

import os
import sys
from typing import Set
import bpy

from . import sisync_bridge_core as core


class BlenderMapManager:
    """Manages PBR texture node inspection and selective viewport texture reloading."""

    TARGET_CHANNELS = {
        "Base Color", "Color", "Albedo", "Diffuse",
        "Roughness", "Rough",
        "Metallic", "Metalness",
        "Normal", "Normal Map", "Bump",
        "Alpha", "Opacity",
    }

    @classmethod
    def _is_connected_to_supported_channel(cls, tex_node: bpy.types.Node) -> bool:
        """Verifies that the image texture node feeds a supported PBR channel."""
        visited = set()
        stack = [tex_node]
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            for out_sock in getattr(curr, "outputs", []):
                for link in getattr(out_sock, "links", []):
                    to_sock = link.to_socket
                    to_node = link.to_node
                    if to_sock and to_sock.name in cls.TARGET_CHANNELS:
                        return True
                    if to_node and to_node.type in ('BSDF_PRINCIPLED', 'OUTPUT_MATERIAL'):
                        return True
                    if to_node and to_node not in visited:
                        stack.append(to_node)
        return False

    @classmethod
    def refresh_pbr_maps(cls, context, only_selected: bool = False) -> int:
        """
        Reloads only relevant SiSync PBR texture images connected to supported shader channels
        that exist in the bridge texture directory or are tagged for SiSync.
        Logs missing textures explicitly.
        """
        materials_to_check: Set[bpy.types.Material] = set()

        if only_selected and getattr(context, "selected_objects", None):
            for obj in context.selected_objects:
                if obj.type == 'MESH':
                    for slot in obj.material_slots:
                        if slot.material:
                            materials_to_check.add(slot.material)
        else:
            materials_to_check = set(bpy.data.materials)

        images_to_reload: Set[bpy.types.Image] = set()
        tex_dir = os.path.normpath(core.get_textures_dir())

        for mat in materials_to_check:
            if not getattr(mat, "node_tree", None):
                continue

            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    has_links = any(len(out.links) > 0 for out in getattr(node, "outputs", []))
                    if not has_links or cls._is_connected_to_supported_channel(node):
                        images_to_reload.add(node.image)

        reloaded_count = 0
        for img in images_to_reload:
            try:
                raw_path = bpy.path.abspath(img.filepath) if img.filepath else ""
                norm_path = os.path.normpath(raw_path) if raw_path else ""
                basename = os.path.basename(norm_path) if norm_path else f"{img.name}"
                bridge_cand = os.path.join(tex_dir, basename)

                target_file = norm_path if (norm_path and os.path.exists(norm_path)) else (bridge_cand if os.path.exists(bridge_cand) else "")
                if target_file and os.path.exists(target_file):
                    if norm_path != target_file:
                        img.filepath = target_file
                    img.reload()
                    reloaded_count += 1
                    core.log_event("blender", "REFRESH_MAP", "success", extra=target_file)
                elif not raw_path:
                    # In-memory / packed image refresh
                    img.reload()
                    reloaded_count += 1
                    core.log_event("blender", "REFRESH_MAP", "in_memory_reload", extra=img.name)
                else:
                    msg = f"Missing SiSync texture file for image '{img.name}': expected at '{bridge_cand}' or '{norm_path}'"
                    core.log_event("blender", "REFRESH_MAP", "missing_texture", error=msg)
                    sys.stderr.write(f"[SiSync Blender] {msg}\n")
            except Exception as e:
                core.log_event("blender", "REFRESH_MAP", "error", error=str(e), extra=img.name)

        cls.tag_viewport_redraw()
        return reloaded_count

    @classmethod
    def tag_viewport_redraw(cls):
        try:
            wm = bpy.context.window_manager
            for window in wm.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
        except Exception:
            pass
