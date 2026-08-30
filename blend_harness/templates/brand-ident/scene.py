from __future__ import annotations

import math

import bpy
from mathutils import Vector

from blend_runtime import ProjectContext
from brand_rig import rgba


def material(name: str, color: tuple[float, float, float, float], *, metallic: float, roughness: float) -> bpy.types.Material:
    value = bpy.data.materials.new(name)
    value.diffuse_color = color
    value.use_nodes = True
    principled = value.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = color
    principled.inputs["Metallic"].default_value = metallic
    principled.inputs["Roughness"].default_value = roughness
    return value


def bevelled_cube(name: str, location: tuple[float, float, float], scale: tuple[float, float, float],
                   value: bpy.types.Material, bevel: float) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(value)
    modifier = obj.modifiers.new("edge-softness", "BEVEL")
    modifier.width = bevel
    modifier.segments = 3
    return obj


def point_camera(camera: bpy.types.Object, target: tuple[float, float, float]) -> None:
    camera.rotation_euler = (Vector(target) - camera.location).to_track_quat("-Z", "Y").to_euler()


def build_scene(context: ProjectContext) -> None:
    accent = rgba(context.parameter("accent", [1.0, 0.28, 0.035, 1.0]))
    plate_color = rgba(context.parameter("plate", [0.012, 0.009, 0.007, 1.0]))
    accent_material = material("engraved-metal", accent, metallic=0.82, roughness=0.21)
    plate_material = material("warm-black", plate_color, metallic=0.2, roughness=0.33)

    plate = bevelled_cube("surface", (0, 0, 0), (2.7, 2.05, 0.18), plate_material, 0.12)
    if (context.variant_name or "").startswith("transparent"):
        plate.hide_render = True

    # Three inset bars form an unambiguous restrained mark. Their tops sit only 8 mm above the surface.
    spine = bevelled_cube("mark-spine", (-0.36, 0, 0.188), (0.13, 0.72, 0.018), accent_material, 0.055)
    upper = bevelled_cube("mark-upper", (0.18, 0.32, 0.188), (0.48, 0.13, 0.018), accent_material, 0.055)
    lower = bevelled_cube("mark-lower", (0.18, -0.32, 0.188), (0.48, 0.13, 0.018), accent_material, 0.055)
    for obj in (spine, upper, lower):
        obj["blend_role"] = "hero-mark"

    world = bpy.data.worlds.new("warm-black-world")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.003, 0.002, 0.0015, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.08

    camera_data = bpy.data.cameras.new("hero")
    camera = bpy.data.objects.new("hero", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (0, -5.7, 4.25 + float(context.parameter("cameraLift", 0)))
    camera_data.lens = 58
    point_camera(camera, (0, 0, 0.05))
    camera.keyframe_insert("location", frame=1)
    camera.location = (0.08, -5.55, 4.15 + float(context.parameter("cameraLift", 0)))
    point_camera(camera, (0, 0, 0.05))
    camera.keyframe_insert("location", frame=18)
    camera.keyframe_insert("rotation_euler", frame=1)
    camera.keyframe_insert("rotation_euler", frame=18)
    bpy.context.scene.camera = camera

    detail_data = bpy.data.cameras.new("detail")
    detail = bpy.data.objects.new("detail", detail_data)
    bpy.context.scene.collection.objects.link(detail)
    detail.location = (1.65, -2.85, 1.55)
    detail_data.lens = 72
    point_camera(detail, (0.1, 0, 0.12))

    key_data = bpy.data.lights.new("amber-sweep", "AREA")
    key = bpy.data.objects.new("amber-sweep", key_data)
    bpy.context.scene.collection.objects.link(key)
    key_data.color = accent[:3]
    key_data.energy = float(context.parameter("lightEnergy", 900)) * 0.45
    key_data.shape = "RECTANGLE"
    key_data.size = 2.4
    key_data.size_y = 0.65
    key.location = (-3.2, -1.2, 3.6)
    point_camera(key, (0, 0, 0))
    key.keyframe_insert("location", frame=1)
    key_data.keyframe_insert("energy", frame=1)
    key.location = (2.6, -0.6, 3.2)
    key_data.energy = float(context.parameter("lightEnergy", 900))
    point_camera(key, (0, 0, 0))
    key.keyframe_insert("location", frame=12)
    key_data.keyframe_insert("energy", frame=12)
    key.location = (2.1, 0.6, 3.4)
    key_data.energy *= 0.72
    key.keyframe_insert("location", frame=18)
    key_data.keyframe_insert("energy", frame=18)

    fill_data = bpy.data.lights.new("soft-fill", "AREA")
    fill = bpy.data.objects.new("soft-fill", fill_data)
    bpy.context.scene.collection.objects.link(fill)
    fill_data.color = (0.18, 0.22, 0.3)
    fill_data.energy = 220
    fill_data.size = 4.0
    fill.location = (-2.5, 2.2, 4.0)
    point_camera(fill, (0, 0, 0))


if __name__ == "__main__":
    context = ProjectContext.from_cli()
    context.reset_scene()
    build_scene(context)
    context.execute_requested_operation()
