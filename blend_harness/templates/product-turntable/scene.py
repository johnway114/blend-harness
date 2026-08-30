from __future__ import annotations

import math

import bpy
from mathutils import Vector

from blend_runtime import ProjectContext


def principled(name: str, color: tuple[float, float, float, float], metallic: float, roughness: float) -> bpy.types.Material:
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.use_nodes = True
    material.diffuse_color = color
    shader = material.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = color
    shader.inputs["Metallic"].default_value = metallic
    shader.inputs["Roughness"].default_value = roughness
    return material


def look_at(obj: bpy.types.Object, target: tuple[float, float, float]) -> None:
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def area_light(name: str, location: tuple[float, float, float], energy: float,
               size: float, color: tuple[float, float, float]) -> bpy.types.Object:
    data = bpy.data.lights.new(name, "AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    data.color = color
    light = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(light)
    light.location = location
    look_at(light, (0, 0, 0.25))
    return light


def build_scene(context: ProjectContext) -> None:
    product_collection = bpy.data.collections.new("product")
    bpy.context.scene.collection.children.link(product_collection)
    source = str(context.asset_path("product-model"))
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=source)
    else:
        bpy.ops.import_scene.obj(filepath=source)
    imported = sorted(set(bpy.data.objects) - before, key=lambda obj: obj.name)
    if not imported:
        raise RuntimeError("Declared OBJ imported no objects")
    body = imported[0]
    body.name = "product-body"
    body.data.name = "product-body-mesh"
    for collection in list(body.users_collection):
        collection.objects.unlink(body)
    product_collection.objects.link(body)
    body.location = (0, 0, 0.92)
    body.rotation_euler = (0, 0, 0)
    body.scale = (1, 1, 1)
    bpy.context.view_layer.objects.active = body
    body.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    body.data.materials.clear()
    product_color = tuple(context.parameter("productColor", [0.055, 0.075, 0.11, 1.0]))
    body.data.materials.append(principled("product-finish", product_color, metallic=0.58, roughness=0.24))
    body["source_asset"] = "product-model"
    body["units"] = "metres"

    turntable = bpy.data.objects.new("product-turntable", None)
    product_collection.objects.link(turntable)
    body.parent = turntable
    turntable.rotation_euler = (0, 0, 0)
    turntable.keyframe_insert("rotation_euler", frame=1)
    turntable.rotation_euler.z = math.tau
    turntable.keyframe_insert("rotation_euler", frame=21)

    studio = bpy.data.collections.new("studio")
    bpy.context.scene.collection.children.link(studio)
    backdrop_color = tuple(context.parameter("backdrop", [0.025, 0.028, 0.033, 1.0]))
    backdrop_material = principled("seamless-backdrop", backdrop_color, metallic=0, roughness=0.72)
    bpy.ops.mesh.primitive_plane_add(size=18, location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "studio-floor"
    floor.data.materials.append(backdrop_material)
    for collection in list(floor.users_collection):
        collection.objects.unlink(floor)
    studio.objects.link(floor)

    azimuth = float(context.parameter("cameraAzimuth", 0))
    camera_data = bpy.data.cameras.new("hero")
    hero = bpy.data.objects.new("hero", camera_data)
    bpy.context.scene.collection.objects.link(hero)
    hero.location = (4.1 * math.sin(azimuth), -5.4 * math.cos(azimuth), 3.25)
    camera_data.lens = 64
    camera_data.clip_start = 0.05
    camera_data.clip_end = 100
    look_at(hero, (0, 0, 0.86))
    bpy.context.scene.camera = hero

    detail_data = bpy.data.cameras.new("detail")
    detail = bpy.data.objects.new("detail", detail_data)
    bpy.context.scene.collection.objects.link(detail)
    detail.location = (2.65, -3.4, 2.15)
    detail_data.lens = 78
    look_at(detail, (0, 0, 1.05))

    area_light("key", (-3.2, -3.0, 5.2), 920, 3.0, (1.0, 0.83, 0.65))
    area_light("fill", (3.4, -1.2, 3.0), float(context.parameter("fillEnergy", 260)), 2.7, (0.62, 0.75, 1.0))
    area_light("rim", (0.4, 3.0, 4.2), 760, 2.2, (1.0, 0.46, 0.2))

    world = bpy.data.worlds.new("studio-world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.006, 0.007, 0.009, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.12
    bpy.context.scene.world = world


if __name__ == "__main__":
    context = ProjectContext.from_cli()
    context.reset_scene()
    build_scene(context)
    context.execute_requested_operation()
