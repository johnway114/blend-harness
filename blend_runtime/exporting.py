"""Declared, selection-safe, nondestructive export operations inside Blender."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import bpy


def _operator_parameters(operator: Callable[..., Any]) -> set[str]:
    try:
        return {item.identifier for item in operator.get_rna_type().properties if item.identifier != "rna_type"}
    except Exception:
        return set()


def _invoke(operator: Callable[..., Any], **kwargs: Any) -> set[str]:
    parameters = _operator_parameters(operator)
    filtered = {key: value for key, value in kwargs.items() if not parameters or key in parameters}
    result = operator(**filtered)
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender exporter did not finish: {result}")
    return set(result)


def _selected_objects(declaration: dict[str, Any]) -> list[bpy.types.Object]:
    include_objects = set(declaration.get("includeObjects", []))
    include_collections = set(declaration.get("includeCollections", []))
    selected: list[bpy.types.Object] = []
    for obj in bpy.context.scene.objects:
        if include_objects and obj.name in include_objects:
            selected.append(obj)
            continue
        if include_collections and any(collection.name in include_collections for collection in obj.users_collection):
            selected.append(obj)
            continue
        if not include_objects and not include_collections and not obj.hide_render:
            selected.append(obj)
    allow_cameras = declaration.get("cameras", False)
    if not declaration.get("includeHidden", False):
        selected = [obj for obj in selected if not obj.hide_render]
    allow_lights = declaration.get("lights", False)
    return [obj for obj in selected if (obj.type != "CAMERA" or allow_cameras) and (obj.type != "LIGHT" or allow_lights)]


def _prepare_selection(
    declaration: dict[str, Any], selected: list[bpy.types.Object] | None = None
) -> list[bpy.types.Object]:
    bpy.ops.object.select_all(action="DESELECT")
    selected = selected if selected is not None else _selected_objects(declaration)
    for obj in selected:
        obj.hide_set(False)
        obj.select_set(True)
    if selected:
        bpy.context.view_layer.objects.active = selected[0]
    if declaration.get("applyTransforms") and selected:
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    if declaration.get("triangulate"):
        for obj in selected:
            if obj.type == "MESH":
                modifier = obj.modifiers.new(name="__blend_export_triangulate", type="TRIANGULATE")
                modifier.keep_custom_normals = True
    if not declaration.get("materials", True):
        for obj in selected:
            if obj.type == "MESH":
                obj.data.materials.clear()
    optimize = declaration.get("optimize", {})
    ratio = optimize.get("decimateRatio")
    if ratio is not None and ratio < 1:
        for obj in selected:
            if obj.type == "MESH":
                modifier = obj.modifiers.new(name="__blend_export_decimate", type="DECIMATE")
                modifier.ratio = float(ratio)
    weld_distance = optimize.get("weldDistance")
    if weld_distance:
        for obj in selected:
            if obj.type == "MESH":
                modifier = obj.modifiers.new(name="__blend_export_weld", type="WELD")
                modifier.merge_threshold = float(weld_distance)
    return selected


def _animation_sources(objects: list[bpy.types.Object]) -> list[dict[str, Any]]:
    records = []
    for obj in objects:
        action = getattr(getattr(obj, "animation_data", None), "action", None)
        if action is not None:
            records.append({
                "object": obj.name,
                "kind": "action",
                "name": action.name,
                "frameStart": float(action.frame_range[0]),
                "frameEnd": float(action.frame_range[1]),
            })
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        shape_action = getattr(getattr(shape_keys, "animation_data", None), "action", None)
        if shape_action is not None:
            records.append({
                "object": obj.name,
                "kind": "shape-key-action",
                "name": shape_action.name,
                "frameStart": float(shape_action.frame_range[0]),
                "frameEnd": float(shape_action.frame_range[1]),
            })
    return records


def _custom_properties(objects: list[bpy.types.Object]) -> dict[str, list[str]]:
    return {
        obj.name: sorted(key for key in obj.keys() if key != "_RNA_UI")
        for obj in objects
        if any(key != "_RNA_UI" for key in obj.keys())
    }


def _material_names(objects: list[bpy.types.Object]) -> list[str]:
    return sorted({
        slot.material.name
        for obj in objects
        for slot in obj.material_slots
        if slot.material is not None
    })


def _object_transforms(objects: list[bpy.types.Object]) -> list[dict[str, Any]]:
    return [
        {
            "name": obj.name,
            "type": obj.type,
            "location": [float(value) for value in obj.location],
            "rotation": [float(value) for value in obj.rotation_euler],
            "scale": [float(value) for value in obj.scale],
        }
        for obj in objects
    ]


def _mesh_counts(objects: list[bpy.types.Object]) -> dict[str, Any]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    vertices = triangles = 0
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    for obj in objects:
        if obj.type != "MESH":
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
        try:
            mesh.calc_loop_triangles()
            vertices += len(mesh.vertices)
            triangles += len(mesh.loop_triangles)
            for vertex in mesh.vertices:
                coordinate = evaluated.matrix_world @ vertex.co
                for axis in range(3):
                    minimum[axis] = min(minimum[axis], float(coordinate[axis]))
                    maximum[axis] = max(maximum[axis], float(coordinate[axis]))
        finally:
            evaluated.to_mesh_clear()
    bounds = None
    if vertices:
        bounds = {
            "minimum": minimum,
            "maximum": maximum,
            "dimensions": [maximum[axis] - minimum[axis] for axis in range(3)],
        }
    return {
        "objects": len(objects),
        "vertices": vertices,
        "triangles": triangles,
        "bounds": bounds,
    }


def export_declared(declaration: dict[str, Any], output_root: str) -> dict[str, Any]:
    path = Path(declaration["path"])
    if not path.is_absolute():
        path = (
            Path(output_root).joinpath(*path.parts[1:])
            if path.parts and path.parts[0] == "output"
            else Path(output_root) / path
        )
    path = path.resolve()
    root = Path(output_root).resolve()
    path.relative_to(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.part{path.suffix}")
    temporary.unlink(missing_ok=True)
    selected = _selected_objects(declaration)
    before = _mesh_counts(selected)
    animation_sources = _animation_sources(selected)
    custom_properties = _custom_properties(selected)
    source_material_names = _material_names(selected)
    selected = _prepare_selection(declaration, selected)
    source_transforms = _object_transforms(selected)
    if not selected:
        raise RuntimeError("Export selection is empty")
    format_name = declaration["format"].lower()
    common = {
        "filepath": str(temporary),
        "check_existing": False,
        "use_selection": True,
        "selected_objects_only": True,
        "export_selected_objects": True,
        "apply_modifiers": declaration.get("applyModifiers", True),
        "export_apply": declaration.get("applyModifiers", True),
        "export_animations": declaration.get("animations", False),
        "export_animation": declaration.get("animations", False),
        "use_triangles": declaration.get("triangulate", False),
        "export_cameras": declaration.get("cameras", False),
        "export_lights": declaration.get("lights", False),
        "global_scale": declaration.get("scale", 1.0),
    }
    if format_name in {"glb", "gltf"}:
        _invoke(
            bpy.ops.export_scene.gltf,
            **common,
            export_format="GLB" if format_name == "glb" else "GLTF_SEPARATE",
            export_texcoords=True,
            export_normals=True,
            export_materials="EXPORT" if declaration.get("materials", True) else "NONE",
            export_extras=declaration.get("customProperties", False),
        )
    elif format_name in {"usd", "usdc", "usda"}:
        _invoke(
            bpy.ops.wm.usd_export,
            **common,
            export_materials=declaration.get("materials", True),
            export_textures=declaration.get("packageTextures", False),
            relative_paths=True,
            evaluation_mode="RENDER",
            export_custom_properties=declaration.get("customProperties", False),
        )
    elif format_name == "fbx":
        _invoke(
            bpy.ops.export_scene.fbx,
            **common,
            axis_forward=declaration.get("forwardAxis", "-Z"),
            axis_up=declaration.get("upAxis", "Y"),
            bake_anim=declaration.get("animations", False),
            path_mode="COPY" if declaration.get("packageTextures", False) else "AUTO",
            embed_textures=declaration.get("packageTextures", False),
            use_custom_props=declaration.get("customProperties", False),
        )
    elif format_name == "obj":
        if hasattr(bpy.ops.wm, "obj_export"):
            _invoke(
                bpy.ops.wm.obj_export,
                **common,
                export_materials=declaration.get("materials", True),
                forward_axis=declaration.get("forwardAxis", "NEGATIVE_Z"),
                up_axis=declaration.get("upAxis", "Y"),
            )
        else:
            _invoke(bpy.ops.export_scene.obj, **common, axis_forward="-Z", axis_up="Y")
    elif format_name == "abc":
        _invoke(
            bpy.ops.wm.alembic_export,
            **common,
            selected=True,
            start=bpy.context.scene.frame_start,
            end=bpy.context.scene.frame_end,
            export_custom_properties=declaration.get("customProperties", False),
        )
    elif format_name == "stl":
        if hasattr(bpy.ops.wm, "stl_export"):
            _invoke(
                bpy.ops.wm.stl_export,
                **common,
                forward_axis=declaration.get("forwardAxis", "Y"),
                up_axis=declaration.get("upAxis", "Z"),
            )
        else:
            _invoke(bpy.ops.export_mesh.stl, **common)
    else:
        raise RuntimeError(f"Unsupported export format: {format_name}")
    if not temporary.is_file():
        # Some exporters normalize suffixes despite an explicit filepath.
        candidates = sorted(temporary.parent.glob(f"{temporary.stem}*"))
        files = [candidate for candidate in candidates if candidate.is_file()]
        if len(files) == 1:
            temporary = files[0]
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise RuntimeError(f"Exporter did not produce a non-empty artifact at {temporary}")
    if format_name in {"obj", "gltf"}:
        sidecars = [
            candidate
            for candidate in temporary.parent.glob(f"{temporary.stem}.*")
            if candidate.is_file() and candidate != temporary
        ]
        replacements = {
            sidecar.name: f"{path.stem}{sidecar.suffix}"
            for sidecar in sidecars
        }
        if replacements:
            content = temporary.read_text(encoding="utf-8")
            for old_name, new_name in replacements.items():
                content = content.replace(old_name, new_name)
            temporary.write_text(content, encoding="utf-8")
            for sidecar in sidecars:
                os.replace(sidecar, sidecar.with_name(replacements[sidecar.name]))
    os.replace(temporary, path)
    bpy.context.view_layer.update()
    after = _mesh_counts(selected)
    return {
        "id": declaration["id"],
        "format": format_name,
        "path": str(path),
        "bytes": path.stat().st_size,
        "selection": sorted(obj.name for obj in selected),
        "before": before,
        "resolvedSettings": {
            "units": declaration.get("units"),
            "scale": declaration.get("scale", 1.0),
            "forwardAxis": declaration.get("forwardAxis") or {
                "fbx": "-Z",
                "obj": "NEGATIVE_Z",
                "stl": "Y",
            }.get(format_name, "FORMAT_DEFAULT"),
            "upAxis": declaration.get("upAxis") or {
                "fbx": "Y",
                "obj": "Y",
                "stl": "Z",
            }.get(format_name, "FORMAT_DEFAULT"),
            "applyTransforms": declaration.get("applyTransforms", False),
            "applyModifiers": declaration.get("applyModifiers", True),
            "triangulate": declaration.get("triangulate", False),
            "materials": declaration.get("materials", True),
            "packageTextures": declaration.get("packageTextures", False),
            "animations": declaration.get("animations", False),
        },
        "animationSources": animation_sources,
        "after": after,
        "sourceCustomProperties": custom_properties,
        "sourceMaterialNames": source_material_names,
        "sourceTransforms": source_transforms,
        "optimization": declaration.get("optimize"),
        "sourceGeometryMutated": False,
    }
