from __future__ import annotations

import json

import bpy
from mathutils import Vector

from blend_runtime import ProjectContext


def material(name: str, color: tuple[float, float, float, float], roughness: float = 0.45) -> bpy.types.Material:
    value = bpy.data.materials.new(name)
    value.diffuse_color = color
    value.use_nodes = True
    shader = value.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = color
    shader.inputs["Roughness"].default_value = roughness
    return value


def look_at(obj: bpy.types.Object, target: tuple[float, float, float]) -> None:
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def build_scene(context: ProjectContext) -> None:
    asset_id = context.parameter("dataAsset", "data-revision-a")
    data = json.loads(context.asset_path(asset_id).read_text(encoding="utf-8"))
    palette_name = context.parameter("palette", "warm")
    colors = (
        [(0.92, 0.25, 0.07, 1), (0.96, 0.54, 0.09, 1), (0.72, 0.12, 0.04, 1)]
        if palette_name == "warm" else
        [(0.08, 0.36, 0.62, 1), (0.08, 0.62, 0.59, 1), (0.28, 0.21, 0.68, 1)]
    )
    label_material = material("label-ivory", (0.78, 0.74, 0.64, 1), roughness=0.7)
    label_font = bpy.data.fonts.load(str(context.asset_path("label-font")))
    floor_material = material("diagram-ground", (0.012, 0.016, 0.022, 1), roughness=0.8)
    gap = float(context.parameter("gap", 1.25))
    series = data["series"]
    span = gap * (len(series) - 1)

    for index, item in enumerate(series):
        slug = item["label"].lower().replace(" ", "-")
        x = index * gap - span / 2
        height = float(item["value"]) / 18
        bar_material = material(f"bar-{slug}-material", colors[index % len(colors)], roughness=0.28)
        bpy.ops.mesh.primitive_cube_add(location=(x, 0, height / 2))
        bar = bpy.context.object
        bar.name = f"bar-{slug}"
        bar.scale = (0.38, 0.38, height / 2)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bar.data.materials.append(bar_material)
        bar["data_value"] = float(item["value"])
        bar["data_label"] = item["label"]
        final_scale = bar.scale.copy()
        bar.scale.z = 0.02
        bar.keyframe_insert("scale", frame=1 + index * 2)
        bar.scale = final_scale
        bar.keyframe_insert("scale", frame=10 + index * 3)

        curve = bpy.data.curves.new(f"label-{slug}-curve", "FONT")
        curve.font = label_font
        curve.body = f"{item['label']}  {item['value']}%"
        curve.align_x = "CENTER"
        curve.size = 0.24
        curve.extrude = 0.004
        label = bpy.data.objects.new(f"label-{slug}", curve)
        bpy.context.scene.collection.objects.link(label)
        label.location = (x, -0.52, 0.08)
        label.rotation_euler = (1.5707963268, 0, 0)
        curve.materials.append(label_material)

    bpy.ops.mesh.primitive_plane_add(size=14, location=(0, 0, -0.01))
    floor = bpy.context.object
    floor.name = "diagram-floor"
    floor.data.materials.append(floor_material)

    title_curve = bpy.data.curves.new("title-curve", "FONT")
    title_curve.body = data["title"]
    title_curve.font = label_font
    title_curve.align_x = "CENTER"
    title_curve.size = 0.38
    title = bpy.data.objects.new("title", title_curve)
    bpy.context.scene.collection.objects.link(title)
    title.location = (0, 0.62, 3.45)
    title.rotation_euler = (1.5707963268, 0, 0)
    title_curve.materials.append(label_material)

    camera_data = bpy.data.cameras.new("hero")
    camera = bpy.data.objects.new("hero", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (4.6, -6.8, float(context.parameter("cameraHeight", 4.2)))
    camera_data.lens = 60
    look_at(camera, (0, 0, 1.35))
    bpy.context.scene.camera = camera

    key_data = bpy.data.lights.new("diagram-key", "AREA")
    key_data.energy = 950
    key_data.shape = "RECTANGLE"
    key_data.size = 5
    key_data.size_y = 3
    key = bpy.data.objects.new("diagram-key", key_data)
    bpy.context.scene.collection.objects.link(key)
    key.location = (-3.5, -3.2, 6.5)
    look_at(key, (0, 0, 1.2))

    world = bpy.data.worlds.new("diagram-world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.004, 0.006, 0.009, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.12
    bpy.context.scene.world = world


if __name__ == "__main__":
    context = ProjectContext.from_cli()
    context.reset_scene()
    build_scene(context)
    context.execute_requested_operation()
