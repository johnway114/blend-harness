"""Blender-side project context and operation dispatcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import bpy
from mathutils import Vector

from .exporting import export_declared
from .inspection import inspect_scene


_FORMAT_EXTENSIONS = {"PNG": ".png", "OPEN_EXR": ".exr", "JPEG": ".jpg", "TIFF": ".tif"}


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--blend-runtime-config", required=True)
    return parser.parse_args(argv)


def _look_at(obj: bpy.types.Object, point: Vector) -> None:
    direction = point - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


class ProjectContext:
    """Resolved immutable project inputs plus Blender execution helpers."""

    _current: "ProjectContext | None" = None

    def __init__(self, runtime: dict[str, Any]) -> None:
        self.runtime = runtime
        self.operation = runtime["operation"]
        self.operation_id = runtime.get("operationId")
        self.project_root = Path(runtime["projectRoot"]).resolve()
        self.working_root = Path(runtime["workingRoot"]).resolve()
        self.cache_root = Path(runtime["cacheRoot"]).resolve()
        self.artifact_root = Path(runtime["artifactRoot"]).resolve()
        self.output_root = Path(runtime["outputRoot"]).resolve()
        self.temporary_root = Path(runtime["temporaryRoot"]).resolve()
        self.preview_root = Path(runtime["previewRoot"]).resolve()
        self.render_root = Path(runtime["renderRoot"]).resolve()
        self.checkpoint_path = Path(runtime["checkpoint"]).resolve()
        self.inspection_path = Path(runtime["inspection"]).resolve()
        self.config = runtime["config"]
        self.brief = runtime["brief"]
        self.profile_name = runtime["profileName"]
        self.profile = runtime["profile"]
        self.variant_name = runtime.get("variantName")
        self.variant = runtime.get("variant") or {}
        self.output = runtime.get("output")
        self.assets = {item["id"]: item for item in runtime.get("assets", [])}
        self.libraries = {item["id"]: item for item in runtime.get("libraries", [])}
        self.jobs = runtime.get("jobs", [])
        self.frames = runtime.get("frames", [])
        simulation_job = self.jobs[0] if len(self.jobs) == 1 and "simulation" in self.jobs[0] else {}
        self.simulation_profile = simulation_job.get("simulationProfile")
        self.simulation_profile_settings = simulation_job.get("simulationProfileSettings", {})
        self.cache_dependency_hash = simulation_job.get("cacheDependencyHash")
        self.simulation_cache_path = (
            Path(simulation_job["cacheRoot"]).resolve()
            if simulation_job.get("cacheRoot")
            else None
        )
        self.dependency_hash = runtime["dependencyHash"]
        self._executed = False

    @classmethod
    def from_cli(cls) -> "ProjectContext":
        if cls._current is not None:
            return cls._current
        arguments = _arguments()
        runtime = json.loads(Path(arguments.blend_runtime_config).read_text(encoding="utf-8"))
        cls._current = cls(runtime)
        return cls._current

    @property
    def mode(self) -> str:
        return self.operation

    @property
    def seed(self) -> int:
        return int(self.config["project"]["seed"])

    @property
    def should_save_blend(self) -> bool:
        return self.operation == "build"

    def reset_scene(self) -> None:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        random.seed(self.seed)
        self._apply_project_settings()

    def asset_path(self, asset_id: str) -> Path:
        if asset_id not in self.assets:
            raise KeyError(f"Undeclared asset {asset_id!r}")
        return Path(self.assets[asset_id]["resolvedPath"])

    def library_path(self, library_id: str) -> Path:
        if library_id not in self.libraries:
            raise KeyError(f"Undeclared library {library_id!r}")
        return Path(self.libraries[library_id]["path"])

    def parameter(self, name: str, default: Any = None) -> Any:
        value: Any = self.variant.get("parameters", {})
        for part in name.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def simulation_parameter(self, name: str, default: Any = None) -> Any:
        value: Any = self.simulation_profile_settings
        for part in name.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def _apply_project_settings(self) -> None:
        scene = bpy.context.scene
        project = self.config["project"]
        scene.frame_start = int(project["frameStart"])
        scene.frame_end = int(project["frameEnd"])
        frame_rate = float(project["frameRate"])
        scene.render.fps = round(frame_rate)
        scene.render.fps_base = round(frame_rate) / frame_rate if frame_rate else 1
        if project.get("units"):
            scene.unit_settings.system = project["units"]
        if project.get("unitScale"):
            scene.unit_settings.scale_length = float(project["unitScale"])
        color = project["colorManagement"]
        for owner, attr, key in (
            (scene.display_settings, "display_device", "display"),
            (scene.view_settings, "view_transform", "view"),
            (scene.view_settings, "look", "look"),
            (scene.view_settings, "exposure", "exposure"),
            (scene.view_settings, "gamma", "gamma"),
        ):
            if key in color:
                try:
                    setattr(owner, attr, color[key])
                except (TypeError, ValueError):
                    # Host validation reports unsupported color-management values.
                    pass
        self.apply_profile()

    def apply_profile(self) -> None:
        scene = bpy.context.scene
        profile = self.profile
        requested_engine = profile["engine"]
        try:
            scene.render.engine = requested_engine
        except TypeError:
            aliases = {"BLENDER_EEVEE_NEXT": "BLENDER_EEVEE"}
            if requested_engine not in aliases:
                raise
            scene.render.engine = aliases[requested_engine]
        scene.render.resolution_x = int(profile["width"])
        scene.render.resolution_y = int(profile["height"])
        scene.render.resolution_percentage = int(profile.get("percentage", 100))
        scene.render.image_settings.file_format = profile["format"]
        scene.render.image_settings.color_mode = profile.get("colorMode", "RGBA" if profile.get("transparent") else "RGB")
        scene.render.film_transparent = bool(profile.get("transparent", False))
        scene.render.use_file_extension = False
        if profile["engine"] == "CYCLES":
            scene.cycles.samples = int(profile["samples"])
            scene.cycles.use_denoising = bool(profile.get("denoise", False))
            device = str(profile.get("device", "CPU")).upper()
            if device in {"", "CPU"}:
                scene.cycles.device = "CPU"
            else:
                if device == "GPU":
                    raise RuntimeError(
                        "Cycles device 'GPU' is ambiguous; declare a concrete backend such as METAL, CUDA, HIP, or ONEAPI"
                    )
                preferences = bpy.context.preferences.addons.get("cycles")
                if preferences is None:
                    raise RuntimeError("Cycles preferences are unavailable for explicit device selection")
                cycles_preferences = preferences.preferences
                cycles_preferences.compute_device_type = device
                cycles_preferences.refresh_devices()
                matching = [
                    available
                    for available in cycles_preferences.devices
                    if available.type == device
                ]
                if not matching:
                    raise RuntimeError(f"Requested Cycles device backend is unavailable: {device}")
                for available in cycles_preferences.devices:
                    available.use = available.type == device
                scene.cycles.device = "GPU"
        elif hasattr(scene, "eevee"):
            if hasattr(scene.eevee, "taa_samples"):
                scene.eevee.taa_samples = int(profile["samples"])
            if hasattr(scene.eevee, "taa_render_samples"):
                scene.eevee.taa_render_samples = int(profile["samples"])

    def save_checkpoint(self) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_name(f".{self.checkpoint_path.name}.{os.getpid()}.tmp")
        try:
            bpy.ops.wm.save_as_mainfile(filepath=str(temporary), check_existing=False, compress=True)
            if not temporary.is_file():
                raise RuntimeError(f"Blender did not write checkpoint {temporary}")
            os.replace(temporary, self.checkpoint_path)
        finally:
            temporary.unlink(missing_ok=True)

    def execute_requested_operation(self) -> None:
        if self._executed:
            raise RuntimeError("Requested operation was executed more than once")
        self._executed = True
        self._apply_project_settings()
        if self.operation == "build":
            self.save_checkpoint()
            self._write_runtime_result({"checkpoint": str(self.checkpoint_path)})
        elif self.operation == "preview":
            self._execute_preview()
        elif self.operation in {"inspect", "validate"}:
            self._execute_inspection()
        elif self.operation == "render":
            self._execute_render()
        elif self.operation == "export":
            self._execute_export()
        elif self.operation == "bake":
            self._execute_bake()
        else:
            raise RuntimeError(f"Unsupported Blend runtime operation: {self.operation}")

    def _write_runtime_result(self, value: dict[str, Any]) -> None:
        path = self.temporary_root / f"runtime-result-{self.operation_id}.json"
        _atomic_json(path, {"schema": 1, "operation": self.operation, "operationId": self.operation_id,
                            "dependencyHash": self.dependency_hash, **value})

    def _scene_bounds(self, subjects: list[str] | None = None) -> tuple[Vector, Vector]:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        points: list[Vector] = []
        declared_subjects = set(subjects or [])
        for obj in bpy.context.scene.objects:
            if declared_subjects and obj.name not in declared_subjects:
                continue
            if obj.type in {"CAMERA", "LIGHT", "EMPTY"} or obj.hide_render:
                continue
            evaluated = obj.evaluated_get(depsgraph)
            points.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
        if declared_subjects and not points:
            missing = ", ".join(sorted(declared_subjects))
            raise RuntimeError(f"Generated view subjects do not exist or are not renderable: {missing}")
        if not points:
            return Vector((-1, -1, -1)), Vector((1, 1, 1))
        minimum = Vector((min(point[index] for point in points) for index in range(3)))
        maximum = Vector((max(point[index] for point in points) for index in range(3)))
        return minimum, maximum

    def _generated_camera(self, view: str, subjects: list[str] | None = None) -> bpy.types.Object:
        name = f"__blend_generated_{view}"
        existing = bpy.data.objects.get(name)
        if existing:
            return existing
        minimum, maximum = self._scene_bounds(subjects)
        center = (minimum + maximum) / 2
        size = maximum - minimum
        distance = max(size.length * 1.5, 2.0)
        directions = {
            "front": Vector((0, -1, 0)),
            "rear": Vector((0, 1, 0)),
            "left": Vector((-1, 0, 0)),
            "right": Vector((1, 0, 0)),
            "side": Vector((1, 0, 0)),
            "top": Vector((0, 0, 1)),
            "bottom": Vector((0, 0, -1)),
        }
        direction = directions[view]
        data = bpy.data.cameras.new(name)
        data.type = "ORTHO"
        data.ortho_scale = max(size.x, size.y, size.z, 0.5) * 1.25
        data.clip_start = max(0.001, distance / 1000)
        data.clip_end = distance * 10
        camera = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(camera)
        camera.location = center + direction * distance
        _look_at(camera, center)
        return camera

    def _camera_for_view(self, view: str | None) -> bpy.types.Object:
        if not view or view == "active":
            if bpy.context.scene.camera is None:
                raise RuntimeError("No active camera")
            return bpy.context.scene.camera
        for declaration in self.config.get("views", []):
            identifier = declaration.get("id") or declaration.get("camera") or declaration.get("generated")
            if identifier == view:
                if declaration.get("camera"):
                    camera = bpy.data.objects.get(declaration["camera"])
                    if camera is None or camera.type != "CAMERA":
                        raise RuntimeError(f"Declared camera does not exist: {declaration['camera']}")
                    return camera
                return self._generated_camera(declaration["generated"], declaration.get("subjects"))
        camera = bpy.data.objects.get(view)
        if camera is not None and camera.type == "CAMERA":
            return camera
        if view in {"front", "rear", "left", "right", "side", "top", "bottom"}:
            return self._generated_camera(view)
        raise RuntimeError(f"Unknown preview view: {view}")

    def _new_emission_material(self, name: str, color: tuple[float, float, float, float]) -> bpy.types.Material:
        material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        emission = nodes.new("ShaderNodeEmission") if hasattr(bpy.types, "ShaderNodeEmission") else nodes.new("ShaderNodeBsdfPrincipled")
        if "Color" in emission.inputs:
            emission.inputs["Color"].default_value = color
        if "Base Color" in emission.inputs:
            emission.inputs["Base Color"].default_value = color
            emission.inputs["Roughness"].default_value = 1.0
        material.node_tree.links.new(emission.outputs[0], output.inputs["Surface"])
        return material

    def _apply_diagnostic_mode(self, mode: str) -> None:
        self.apply_profile()
        scene = bpy.context.scene
        view_layer = bpy.context.view_layer
        view_layer.material_override = None
        scene.render.film_transparent = bool(self.profile.get("transparent", False))
        if mode in {"material", "alpha"}:
            if mode == "alpha":
                scene.render.film_transparent = True
                scene.render.image_settings.color_mode = "RGBA"
            return
        if mode == "clay":
            view_layer.material_override = self._new_emission_material("__blend_clay", (0.42, 0.43, 0.45, 1.0))
            return
        if mode in {"wireframe", "object-index"}:
            scene.render.engine = "BLENDER_WORKBENCH"
            shading = scene.display.shading
            shading.light = "STUDIO"
            shading.color_type = "OBJECT" if mode == "object-index" else "MATERIAL"
            shading.show_shadows = False
            shading.show_cavity = mode == "wireframe"
            shading.show_object_outline = mode == "wireframe"
            if hasattr(shading, "show_wireframes"):
                shading.show_wireframes = mode == "wireframe"
            for obj in scene.objects:
                digest = hashlib.sha256(obj.name.encode("utf-8")).digest()
                obj.color = (digest[0] / 255, digest[1] / 255, digest[2] / 255, 1.0)
            return
        material = bpy.data.materials.get(f"__blend_{mode}") or bpy.data.materials.new(f"__blend_{mode}")
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        emission = nodes.new("ShaderNodeEmission") if hasattr(bpy.types, "ShaderNodeEmission") else nodes.new("ShaderNodeBsdfPrincipled")
        if mode == "normal":
            geometry = nodes.new("ShaderNodeNewGeometry")
            multiply = nodes.new("ShaderNodeVectorMath")
            multiply.operation = "MULTIPLY"
            multiply.inputs[1].default_value = (0.5, 0.5, 0.5)
            add = nodes.new("ShaderNodeVectorMath")
            add.operation = "ADD"
            add.inputs[1].default_value = (0.5, 0.5, 0.5)
            material.node_tree.links.new(geometry.outputs["Normal"], multiply.inputs[0])
            material.node_tree.links.new(multiply.outputs["Vector"], add.inputs[0])
            material.node_tree.links.new(add.outputs["Vector"], emission.inputs.get("Color") or emission.inputs.get("Base Color"))
        elif mode == "depth":
            camera_data = nodes.new("ShaderNodeCameraData")
            map_range = nodes.new("ShaderNodeMapRange")
            map_range.inputs[1].default_value = 0.0
            map_range.inputs[2].default_value = max(1.0, bpy.context.scene.camera.data.clip_end if bpy.context.scene.camera else 100.0)
            map_range.inputs[3].default_value = 1.0
            map_range.inputs[4].default_value = 0.0
            material.node_tree.links.new(camera_data.outputs["View Z Depth"], map_range.inputs[0])
            material.node_tree.links.new(map_range.outputs["Result"], emission.inputs.get("Color") or emission.inputs.get("Base Color"))
        else:
            raise RuntimeError(f"Unsupported preview mode: {mode}")
        material.node_tree.links.new(emission.outputs[0], output.inputs["Surface"])
        view_layer.material_override = material

    def _render_still(self, destination: Path) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        root = self.preview_root if self.operation == "preview" else self.render_root
        destination.resolve().relative_to(root.resolve())
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        scene = bpy.context.scene
        scene.render.filepath = str(temporary)
        scene.render.use_file_extension = False
        started = time.monotonic()
        try:
            bpy.ops.render.render(write_still=True)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError(f"Blender did not produce render {temporary}")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(destination), "bytes": destination.stat().st_size,
                "durationSeconds": round(time.monotonic() - started, 6)}

    def _execute_preview(self) -> None:
        jobs = list(self.jobs)
        existing = {
            (str(job.get("view")), int(job["frame"]), str(job.get("mode", "material")))
            for job in jobs
        }
        modes = sorted({str(job.get("mode", "material")) for job in jobs} or {"material"})
        for marker in bpy.context.scene.timeline_markers:
            if marker.camera is None:
                continue
            for mode in modes:
                key = (marker.camera.name, int(marker.frame), mode)
                if key in existing:
                    continue
                safe_view = "".join(
                    character if character.isalnum() or character in "._-" else "-"
                    for character in marker.camera.name
                )
                jobs.append({
                    "view": marker.camera.name,
                    "frame": int(marker.frame),
                    "mode": mode,
                    "path": str(
                        self.preview_root
                        / str(self.operation_id)
                        / (self.variant_name or "base")
                        / safe_view
                        / mode
                        / f"frame-{int(marker.frame):06d}.png"
                    ),
                })
                existing.add(key)
        outputs = []
        for job in jobs:
            camera = self._camera_for_view(job.get("view"))
            bpy.context.scene.camera = camera
            bpy.context.scene.frame_set(int(job["frame"]))
            self._apply_diagnostic_mode(job.get("mode", "material"))
            record = self._render_still(Path(job["path"]))
            record.update({"view": job.get("view"), "camera": camera.name,
                           "frame": int(job["frame"]), "mode": job.get("mode", "material")})
            outputs.append(record)
        inspection = inspect_scene(self.config)
        inspection.update({
            "dependencyHash": self.dependency_hash,
            "profile": self.profile_name,
            "variant": self.variant_name,
        })
        _atomic_json(self.inspection_path, inspection)
        self._write_runtime_result({
            "outputs": outputs,
            "inspection": str(self.inspection_path),
            "sampledFrames": sorted({int(job["frame"]) for job in jobs}),
        })

    def _execute_inspection(self) -> None:
        for view in self.config.get("views", []):
            if view.get("generated"):
                self._generated_camera(view["generated"], view.get("subjects"))
        inspection = inspect_scene(self.config)
        inspection.update({
            "dependencyHash": self.dependency_hash,
            "profile": self.profile_name,
            "variant": self.variant_name,
        })
        _atomic_json(self.inspection_path, inspection)
        self._write_runtime_result({"inspection": str(self.inspection_path)})

    def _execute_render(self) -> None:
        outputs = []
        for job in self.jobs:
            if job.get("view"):
                bpy.context.scene.camera = self._camera_for_view(job["view"])
            elif self.variant.get("camera"):
                bpy.context.scene.camera = self._camera_for_view(self.variant["camera"])
            if bpy.context.scene.camera is None:
                raise RuntimeError("No active camera for render")
            frame = int(job["frame"])
            bpy.context.scene.frame_set(frame)
            self._apply_diagnostic_mode("material")
            record = self._render_still(Path(job["path"]))
            record.update({"frame": frame, "camera": bpy.context.scene.camera.name})
            outputs.append(record)
        self._write_runtime_result({"outputs": outputs})

    def _execute_export(self) -> None:
        if not self.output:
            raise RuntimeError("Export operation requires a declared output")
        declaration = dict(self.output)
        declaration.setdefault("units", self.config["project"].get("units", "NONE"))
        declaration.setdefault("scale", self.config["project"].get("unitScale", 1.0))
        output_root = (
            self.jobs[0].get("outputRoot")
            if self.jobs and self.jobs[0].get("outputRoot")
            else str(self.output_root)
        )
        report = export_declared(declaration, str(output_root))
        report_path = self.artifact_root / f"export-{self.output['id']}-{self.operation_id}.json"
        _atomic_json(report_path, {"schema": 1, "dependencyHash": self.dependency_hash, **report})
        self._write_runtime_result({"outputs": [report], "report": str(report_path)})

    def _redirect_simulation_cache(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        scene = bpy.context.scene
        point_caches = []
        rigid_body_world = getattr(scene, "rigidbody_world", None)
        if rigid_body_world and getattr(rigid_body_world, "point_cache", None):
            point_caches.append(rigid_body_world.point_cache)
        for obj in scene.objects:
            for particle_system in getattr(obj, "particle_systems", []):
                if getattr(particle_system, "point_cache", None):
                    point_caches.append(particle_system.point_cache)
            for modifier in obj.modifiers:
                if getattr(modifier, "point_cache", None):
                    point_caches.append(modifier.point_cache)
                domain = getattr(modifier, "domain_settings", None)
                if domain is not None and hasattr(domain, "cache_directory"):
                    domain.cache_directory = str(destination)
        for point_cache in point_caches:
            try:
                point_cache.use_disk_cache = True
            except (AttributeError, TypeError):
                pass
            if hasattr(point_cache, "filepath"):
                try:
                    point_cache.filepath = str(destination)
                except (AttributeError, TypeError):
                    pass


    def _execute_bake(self) -> None:
        jobs_by_id = {
            job["simulation"]: job for job in self.jobs if "simulation" in job
        }
        requested = set(jobs_by_id)
        records = []
        for simulation in self.config.get("simulations", []):
            if requested and simulation["id"] not in requested:
                continue
            selected_job = jobs_by_id.get(simulation["id"], {})
            cache_path = Path(selected_job.get("cacheRoot") or simulation["cacheRoot"])
            if not cache_path.is_absolute():
                cache_path = self.cache_root / cache_path
            cache_path = cache_path.resolve()
            cache_path.relative_to(self.cache_root.resolve())
            cache_path.mkdir(parents=True, exist_ok=True)
            bpy.context.scene.frame_start = int(simulation["frameStart"])
            self._redirect_simulation_cache(cache_path)
            bpy.context.scene.frame_end = int(simulation["frameEnd"])
            started = time.monotonic()
            simulation_type = simulation["type"]
            if simulation_type == "geometry-nodes" and hasattr(bpy.ops.object, "simulation_nodes_cache_bake"):
                bpy.ops.object.simulation_nodes_cache_bake(selected=False)
            else:
                try:
                    bpy.ops.ptcache.bake_all(bake=True)
                except RuntimeError as exc:
                    if "No active point cache" not in str(exc):
                        raise
            marker = cache_path / "blend-cache-complete.json"
            _atomic_json(marker, {
                "schema": 1,
                "simulation": simulation["id"],
                "dependencyHash": self.cache_dependency_hash or self.dependency_hash,
                "frameStart": simulation["frameStart"],
                "frameEnd": simulation["frameEnd"],
                "seed": simulation.get("seed"),
                "deterministic": simulation["deterministic"],
                "simulationProfile": self.simulation_profile,
                "simulationProfileSettings": self.simulation_profile_settings,
            })
            files = [str(path) for path in sorted(cache_path.rglob("*")) if path.is_file()]
            records.append({"simulation": simulation["id"], "cacheRoot": str(cache_path),
                            "simulationProfile": self.simulation_profile,
                            "simulationProfileSettings": self.simulation_profile_settings,
                            "files": files, "durationSeconds": round(time.monotonic() - started, 6)})
        self._write_runtime_result({"outputs": records})
