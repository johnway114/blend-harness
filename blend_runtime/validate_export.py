"""Clean-Blender import probe for exported model decodability."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bmesh
import bpy


def _args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", required=True)
    parser.add_argument("--path", required=True)
    return parser.parse_args(argv)


def _parameters(operator: object) -> set[str]:
    try:
        return {item.identifier for item in operator.get_rna_type().properties if item.identifier != "rna_type"}
    except Exception:
        return set()


def _call(operator: object, **kwargs: object) -> None:
    parameters = _parameters(operator)
    filtered = {key: value for key, value in kwargs.items() if not parameters or key in parameters}
    result = operator(**filtered)
    if "FINISHED" not in result:
        raise RuntimeError(f"Importer did not finish: {result}")


def main() -> None:
    args = _args()
    path = str(Path(args.path).resolve())
    bpy.ops.wm.read_factory_settings(use_empty=True)
    format_name = args.format.lower()
    if format_name in {"glb", "gltf"}:
        _call(bpy.ops.import_scene.gltf, filepath=path)
    elif format_name in {"usd", "usdc", "usda"}:
        _call(bpy.ops.wm.usd_import, filepath=path)
    elif format_name == "fbx":
        _call(bpy.ops.import_scene.fbx, filepath=path)
    elif format_name == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            _call(bpy.ops.wm.obj_import, filepath=path)
        else:
            _call(bpy.ops.import_scene.obj, filepath=path)
    elif format_name == "abc":
        _call(bpy.ops.wm.alembic_import, filepath=path)
    elif format_name == "stl":
        if hasattr(bpy.ops.wm, "stl_import"):
            _call(bpy.ops.wm.stl_import, filepath=path)
        else:
            _call(bpy.ops.import_mesh.stl, filepath=path)
    else:
        raise RuntimeError(f"Unsupported import probe format {format_name}")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    triangles = 0
    vertices = 0
    non_manifold_edges = 0
    boundary_edges = 0
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
        try:
            mesh.calc_loop_triangles()
            triangles += len(mesh.loop_triangles)
            vertices += len(mesh.vertices)
            for vertex in mesh.vertices:
                coordinate = evaluated.matrix_world @ vertex.co
                for axis in range(3):
                    minimum[axis] = min(minimum[axis], float(coordinate[axis]))
                    maximum[axis] = max(maximum[axis], float(coordinate[axis]))
            topology = bmesh.new()
            try:
                topology.from_mesh(mesh)
                bmesh.ops.remove_doubles(topology, verts=list(topology.verts), dist=1e-6)
                non_manifold_edges += sum(1 for edge in topology.edges if not edge.is_manifold)
                boundary_edges += sum(1 for edge in topology.edges if edge.is_boundary)
            finally:
                topology.free()
        finally:
            evaluated.to_mesh_clear()
    result = {
        "objects": len(bpy.context.scene.objects),
        "meshes": len(meshes),
        "vertices": vertices,
        "triangles": triangles,
        "nonManifoldEdges": non_manifold_edges,
        "bounds": (
            {
                "minimum": minimum,
                "maximum": maximum,
                "dimensions": [maximum[axis] - minimum[axis] for axis in range(3)],
            }
            if vertices
            else None
        ),
        "boundaryEdges": boundary_edges,
        "materials": len(bpy.data.materials),
        "images": len(bpy.data.images),
        "names": sorted(obj.name for obj in bpy.context.scene.objects),
        "transforms": [
            {
                "name": obj.name,
                "type": obj.type,
                "location": [float(value) for value in obj.location],
                "rotation": [float(value) for value in obj.rotation_euler],
                "scale": [float(value) for value in obj.scale],
                "hiddenRender": bool(obj.hide_render),
            }
            for obj in bpy.context.scene.objects
        ],
        "actionRanges": [
            {
                "name": action.name,
                "frameStart": float(action.frame_range[0]),
                "frameEnd": float(action.frame_range[1]),
            }
            for action in bpy.data.actions
        ],
        "customProperties": {
            obj.name: sorted(key for key in obj.keys() if key != "_RNA_UI")
            for obj in bpy.context.scene.objects
            if any(key != "_RNA_UI" for key in obj.keys())
        },
        "materialNames": sorted(material.name for material in bpy.data.materials),
        "imageDependencies": [
            {
                "name": image.name,
                "path": bpy.path.abspath(image.filepath) if image.filepath else None,
                "packed": image.packed_file is not None,
            }
            for image in bpy.data.images
        ],
        "units": {
            "system": bpy.context.scene.unit_settings.system,
            "scaleLength": float(bpy.context.scene.unit_settings.scale_length),
        },
        "actions": len(bpy.data.actions),
        "cacheFiles": [
            {
                "name": cache.name,
                "path": bpy.path.abspath(cache.filepath) if cache.filepath else None,
                "frame": float(cache.frame),
                "frameOffset": float(cache.frame_offset),
            }
            for cache in bpy.data.cache_files
        ],
        "animationModifiers": [
            {
                "object": obj.name,
                "modifier": modifier.name,
                "type": modifier.type,
            }
            for obj in meshes
            for modifier in obj.modifiers
            if modifier.type == "MESH_SEQUENCE_CACHE"
        ],
        "cameras": len(bpy.data.cameras),
        "lights": len(bpy.data.lights),
    }
    print("BLEND_EXPORT_VALIDATION=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
