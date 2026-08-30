"""Stable evaluated-scene inspection executed inside Blender."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import bmesh
import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector


def _vector(value: Any) -> list[float]:
    return [round(float(item), 8) for item in value]


def _bounds(obj: bpy.types.Object, depsgraph: bpy.types.Depsgraph) -> dict[str, Any]:
    evaluated = obj.evaluated_get(depsgraph)
    world = [evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
    if not world:
        return {"minimum": [0, 0, 0], "maximum": [0, 0, 0], "center": [0, 0, 0], "size": [0, 0, 0]}
    minimum = Vector((min(point[index] for point in world) for index in range(3)))
    maximum = Vector((max(point[index] for point in world) for index in range(3)))
    return {
        "minimum": _vector(minimum),
        "maximum": _vector(maximum),
        "center": _vector((minimum + maximum) / 2),
        "size": _vector(maximum - minimum),
    }


def _mesh_statistics(obj: bpy.types.Object, depsgraph: bpy.types.Depsgraph) -> dict[str, Any]:
    if obj.type != "MESH":
        return {"vertices": 0, "edges": 0, "polygons": 0, "triangles": 0,
                "boundaryEdges": 0, "nonManifoldEdges": 0}
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    try:
        mesh.calc_loop_triangles()
        boundary_edges = 0
        non_manifold_edges = 0
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            boundary_edges = sum(1 for edge in bm.edges if edge.is_boundary)
            non_manifold_edges = sum(1 for edge in bm.edges if not edge.is_manifold)
        finally:
            bm.free()
        return {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
            "triangles": len(mesh.loop_triangles),
            "boundaryEdges": boundary_edges,
            "nonManifoldEdges": non_manifold_edges,
        }
    finally:
        evaluated.to_mesh_clear()


def _animation_for_id(data: Any, owner: str) -> list[dict[str, Any]]:
    animation = getattr(data, "animation_data", None)
    action = animation.action if animation else None
    records: list[dict[str, Any]] = []
    if action:
        slots = getattr(action, "slots", [])
        frame_range = [float(action.frame_range[0]), float(action.frame_range[1])]
        curves = []
        try:
            if hasattr(action, "fcurves"):
                curves = list(action.fcurves)
            elif animation and getattr(animation, "action_slot", None):
                channelbag = action.layers[0].strips[0].channelbag(animation.action_slot)
                curves = list(channelbag.fcurves) if channelbag else []
        except (AttributeError, IndexError, TypeError):
            curves = []
        keyframes = sorted({float(point.co[0]) for curve in curves for point in curve.keyframe_points})
        records.append({
            "owner": owner,
            "action": action.name,
            "frameRange": frame_range,
            "keyframes": keyframes,
            "fCurves": len(curves),
            "slots": [getattr(slot, "identifier", getattr(slot, "name", "")) for slot in slots],
        })
    return records


def _drivers(data: Any, owner: str) -> list[dict[str, Any]]:
    animation = getattr(data, "animation_data", None)
    values: list[dict[str, Any]] = []
    for curve in getattr(animation, "drivers", []) if animation else []:
        values.append({
            "owner": owner,
            "dataPath": curve.data_path,
            "arrayIndex": curve.array_index,
            "expression": curve.driver.expression,
            "type": curve.driver.type,
            "variables": [variable.name for variable in curve.driver.variables],
        })
    return values


def _dependency(path: str, kind: str, owner: str) -> dict[str, Any]:
    absolute = Path(bpy.path.abspath(path)).resolve() if path else Path("")
    return {
        "kind": kind,
        "owner": owner,
        "declaredPath": path,
        "path": str(absolute) if path else "",
        "exists": absolute.is_file() if path else False,
        "bytes": absolute.stat().st_size if path and absolute.is_file() else None,
    }


def _framing(scene: bpy.types.Scene, camera: bpy.types.Object, subjects: list[bpy.types.Object],
             depsgraph: bpy.types.Depsgraph) -> dict[str, Any]:
    projected: list[Vector] = []
    near_clip = False
    far_clip = False
    for subject in subjects:
        evaluated = subject.evaluated_get(depsgraph)
        for corner in evaluated.bound_box:
            world = evaluated.matrix_world @ Vector(corner)
            local = camera.matrix_world.inverted() @ world
            projected.append(world_to_camera_view(scene, camera, world))
            distance = -local.z
            near_clip = near_clip or distance < camera.data.clip_start
            far_clip = far_clip or distance > camera.data.clip_end
    if not projected:
        return {"coverage": 0.0, "fullyVisible": False, "nearClipped": False, "farClipped": False}
    min_x = min(point.x for point in projected)
    max_x = max(point.x for point in projected)
    min_y = min(point.y for point in projected)
    max_y = max(point.y for point in projected)
    clipped_area = max(0.0, min(1.0, max_x) - max(0.0, min_x)) * max(0.0, min(1.0, max_y) - max(0.0, min_y))
    fully_visible = min_x >= 0 and max_x <= 1 and min_y >= 0 and max_y <= 1 and not near_clip and not far_clip
    return {
        "coverage": round(clipped_area, 8),
        "fullyVisible": fully_visible,
        "bounds": [round(min_x, 8), round(min_y, 8), round(max_x, 8), round(max_y, 8)],
        "nearClipped": near_clip,
        "farClipped": far_clip,
    }


def inspect_scene(config: dict[str, Any]) -> dict[str, Any]:
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    objects: list[dict[str, Any]] = []
    total_vertices = 0
    total_triangles = 0
    total_texture_bytes = 0
    animation: list[dict[str, Any]] = []
    drivers: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    simulations: list[dict[str, Any]] = []
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        mesh = _mesh_statistics(obj, depsgraph)
        total_vertices += mesh["vertices"]
        total_triangles += mesh["triangles"]
        materials = [slot.material.name if slot.material else None for slot in obj.material_slots]
        modifiers = []
        for modifier in obj.modifiers:
            modifiers.append({
                "name": modifier.name,
                "type": modifier.type,
                "showRender": modifier.show_render,
                "showViewport": modifier.show_viewport,
                "levels": getattr(modifier, "levels", None),
                "renderLevels": getattr(modifier, "render_levels", None),
            })
            if modifier.type in {"CLOTH", "FLUID", "SOFT_BODY", "PARTICLE_SYSTEM", "NODES"}:
                simulations.append({"owner": obj.name, "name": modifier.name, "type": modifier.type,
                                    "enabledRender": modifier.show_render})
        if obj.rigid_body:
            simulations.append({"owner": obj.name, "name": "rigid_body", "type": "RIGID_BODY",
                                "kinematic": obj.rigid_body.kinematic})
        object_constraints = []
        for constraint in obj.constraints:
            record = {"owner": obj.name, "name": constraint.name, "type": constraint.type,
                      "enabled": not constraint.mute, "influence": float(constraint.influence)}
            constraints.append(record)
            object_constraints.append(record)
        record = {
            "name": obj.name,
            "type": obj.type,
            "collectionNames": sorted(collection.name for collection in obj.users_collection),
            "visibleRender": not obj.hide_render,
            "visibleViewport": not obj.hide_viewport,
            "location": _vector(obj.location),
            "rotationMode": obj.rotation_mode,
            "rotationEuler": _vector(obj.rotation_euler),
            "scale": _vector(obj.scale),
            "dimensions": _vector(obj.dimensions),
            "matrixWorld": [[round(float(value), 8) for value in row] for row in obj.matrix_world],
            "evaluatedBounds": _bounds(obj, depsgraph),
            **mesh,
            "materials": materials,
            "missingMaterialSlots": sum(1 for item in materials if item is None),
            "modifiers": modifiers,
            "constraints": object_constraints,
            "animated": bool(getattr(obj, "animation_data", None)),
            "customProperties": {key: obj[key] for key in obj.keys() if key != "_RNA_UI" and isinstance(obj[key], (str, int, float, bool))},
        }
        objects.append(record)
        animation.extend(_animation_for_id(obj, obj.name))
        drivers.extend(_drivers(obj, obj.name))
        if obj.data:
            animation.extend(_animation_for_id(obj.data, f"{obj.name}.data"))
            drivers.extend(_drivers(obj.data, f"{obj.name}.data"))
    dependencies: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for image in sorted(bpy.data.images, key=lambda item: item.name):
        path = bpy.path.abspath(image.filepath) if image.filepath else ""
        size = list(image.size) if image.size else [0, 0]
        bytes_count = Path(path).stat().st_size if path and Path(path).is_file() else 0
        total_texture_bytes += bytes_count
        images.append({
            "name": image.name,
            "source": image.source,
            "path": path,
            "exists": bool(image.packed_file) or (bool(path) and Path(path).is_file()),
            "packed": bool(image.packed_file),
            "width": size[0],
            "height": size[1],
            "channels": image.channels,
            "colorspace": image.colorspace_settings.name,
            "bytes": bytes_count,
        })
        if path and not image.packed_file:
            dependencies.append(_dependency(image.filepath, "image", image.name))
    fonts: list[dict[str, Any]] = []
    for font in sorted(bpy.data.fonts, key=lambda item: item.name):
        declared_path = font.filepath or ""
        built_in = declared_path in {"", "<builtin>"} or font.name.startswith("Bfont")
        path = "" if built_in else bpy.path.abspath(declared_path)
        fonts.append({
            "name": font.name,
            "path": path,
            "exists": built_in or Path(path).is_file(),
            "builtIn": built_in,
        })
        if path:
            dependencies.append(_dependency(declared_path, "font", font.name))
    for sound in bpy.data.sounds:
        dependencies.append(_dependency(sound.filepath, "audio", sound.name))
    for library in bpy.data.libraries:
        dependencies.append(_dependency(library.filepath, "blend-library", library.name))
    cameras = [{
        "name": obj.name,
        "type": obj.data.type,
        "lens": float(obj.data.lens),
        "clipStart": float(obj.data.clip_start),
        "clipEnd": float(obj.data.clip_end),
        "sensorWidth": float(obj.data.sensor_width),
        "shift": [float(obj.data.shift_x), float(obj.data.shift_y)],
        "location": _vector(obj.location),
        "rotationEuler": _vector(obj.rotation_euler),
    } for obj in sorted(bpy.data.objects, key=lambda item: item.name) if obj.type == "CAMERA"]
    lights = [{
        "name": obj.name,
        "type": obj.data.type,
        "energy": float(obj.data.energy),
        "color": _vector(obj.data.color),
        "size": float(getattr(obj.data, "size", 0.0)),
        "visibleRender": not obj.hide_render,
    } for obj in sorted(bpy.data.objects, key=lambda item: item.name) if obj.type == "LIGHT"]
    materials = []
    for material in sorted(bpy.data.materials, key=lambda item: item.name):
        nodes = []
        if material.use_nodes and material.node_tree:
            nodes = [{"name": node.name, "type": node.bl_idname} for node in material.node_tree.nodes]
        materials.append({
            "name": material.name,
            "useNodes": material.use_nodes,
            "nodes": nodes,
            "blendMethod": getattr(material, "surface_render_method", getattr(material, "blend_method", None)),
        })
        animation.extend(_animation_for_id(material, f"material:{material.name}"))
        drivers.extend(_drivers(material, f"material:{material.name}"))
    collections = [{
        "name": collection.name,
        "objects": sorted(obj.name for obj in collection.objects),
        "children": sorted(child.name for child in collection.children),
        "hideRender": collection.hide_render,
    } for collection in sorted(bpy.data.collections, key=lambda item: item.name)]
    scenes = [{
        "name": item.name,
        "frameStart": item.frame_start,
        "frameEnd": item.frame_end,
        "frameRate": item.render.fps / item.render.fps_base,
        "activeCamera": item.camera.name if item.camera else None,
        "renderEngine": item.render.engine,
    } for item in sorted(bpy.data.scenes, key=lambda value: value.name)]
    framing = []
    for view in config.get("views", []):
        camera_name = view.get("camera") or f"__blend_generated_{view.get('generated')}"
        camera = bpy.data.objects.get(camera_name)
        subject_names = view.get("subjects") or config.get("project", {}).get("heroObjects", [])
        subjects = [bpy.data.objects.get(name) for name in subject_names]
        subjects = [subject for subject in subjects if subject is not None]
        if camera and camera.type == "CAMERA":
            framing.append({"view": view.get("id") or view.get("camera") or view.get("generated"),
                            "camera": camera_name, "subjects": subject_names,
                            **_framing(scene, camera, subjects, depsgraph)})
    compositor = {"enabled": scene.use_nodes, "nodes": [], "fileOutputs": []}
    compositor_tree = getattr(scene, "node_tree", None) or getattr(scene, "compositing_node_group", None)
    if scene.use_nodes and compositor_tree:
        for node in compositor_tree.nodes:
            compositor["nodes"].append({"name": node.name, "type": node.bl_idname})
            if node.bl_idname == "CompositorNodeOutputFile":
                compositor["fileOutputs"].append({"name": node.name, "basePath": bpy.path.abspath(node.base_path)})
    frame_count = max(0, scene.frame_end - scene.frame_start + 1)
    width = round(scene.render.resolution_x * scene.render.resolution_percentage / 100)
    height = round(scene.render.resolution_y * scene.render.resolution_percentage / 100)
    channels = 4 if scene.render.image_settings.color_mode == "RGBA" else 3
    if scene.render.engine == "CYCLES":
        render_samples = int(scene.cycles.samples)
    elif hasattr(scene, "eevee"):
        render_samples = int(
            getattr(scene.eevee, "taa_render_samples", getattr(scene.eevee, "taa_samples", 0))
        )
    else:
        render_samples = 0
    return {
        "schema": 1,
        "scene": scenes[0] if scenes else {},
        "scenes": scenes,
        "collections": collections,
        "objects": objects,
        "cameras": cameras,
        "lights": lights,
        "materials": materials,
        "images": images,
        "fonts": fonts,
        "animation": animation,
        "drivers": drivers,
        "constraints": constraints,
        "simulations": simulations,
        "dependencies": dependencies,
        "timelineMarkers": [
            {"name": marker.name, "frame": marker.frame,
             "camera": marker.camera.name if getattr(marker, "camera", None) else None}
            for marker in scene.timeline_markers
        ],
        "compositor": compositor,
        "framing": framing,
        "statistics": {
            "objects": len(objects),
            "renderableObjects": sum(1 for obj in objects if obj["type"] in {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME"} and obj["visibleRender"]),
            "vertices": total_vertices,
            "triangles": total_triangles,
            "textureBytes": total_texture_bytes,
            "materials": len(materials),
            "images": len(images),
        },
        "render": {
            "engine": scene.render.engine,
            "width": width,
            "height": height,
            "percentage": scene.render.resolution_percentage,
            "imageFormat": scene.render.image_settings.file_format,
            "colorMode": scene.render.image_settings.color_mode,
            "filmTransparent": scene.render.film_transparent,
            "frameRate": scene.render.fps / scene.render.fps_base,
            "frameStart": scene.frame_start,
            "frameEnd": scene.frame_end,
            "samples": render_samples,
        },
        "colorManagement": {
            "display": scene.display_settings.display_device,
            "view": scene.view_settings.view_transform,
            "look": scene.view_settings.look,
            "exposure": scene.view_settings.exposure,
            "gamma": scene.view_settings.gamma,
        },
        "estimatedOutput": {
            "frames": frame_count,
            "pixels": frame_count * width * height,
            "uncompressedBytes": frame_count * width * height * channels * 2,
        },
        "extensions": {
            "blender": {
                "schema": 1,
                "version": bpy.app.version_string,
                "fileVersion": list(bpy.app.version_file),
                "background": bpy.app.background,
            }
        },
    }
