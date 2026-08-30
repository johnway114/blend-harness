"""Host-side format signatures, clean import probes, and measurable export limits."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .errors import BlendError, ErrorCategory
from .process import ProcessSupervisor
from .util import atomic_write_json, sha256_file


_SENTINEL = "BLEND_EXPORT_VALIDATION="


def _signature_valid(path: Path, format_name: str) -> bool:
    prefix = path.read_bytes()[:32]
    suffix = path.suffix.lower()
    if format_name == "glb":
        return prefix.startswith(b"glTF")
    if format_name == "gltf":
        return prefix.lstrip().startswith(b"{")
    if format_name == "fbx":
        return prefix.startswith(b"Kaydara FBX Binary") or b"FBX" in prefix
    if format_name == "obj":
        return any(token in prefix for token in (b"#", b"v ", b"o ", b"mtllib"))
    if format_name == "stl":
        return path.stat().st_size >= 84
    if format_name == "abc":
        return path.stat().st_size > 16
    if format_name in {"usd", "usda"}:
        return prefix.startswith(b"#usda") or prefix.startswith(b"PXR-USDC")
    if format_name == "usdc":
        return prefix.startswith(b"PXR-USDC")
    return path.stat().st_size > 0

def _canonical_export_name(value: str, *, object_name: bool = False) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if object_name and normalized.endswith("_mesh"):
        normalized = normalized[:-5]
    return normalized


def validate_export(supervisor: ProcessSupervisor, *, blender: Path, blender_version: str,
                    declaration: dict[str, Any], path: Path, log_root: Path,
                    operation_id: str, offline: bool, source_report: dict[str, Any] | None = None,
                    frame_start: int | None = None, frame_end: int | None = None) -> dict[str, Any]:
    format_name = declaration["format"].lower()
    if not path.is_file() or path.stat().st_size == 0:
        raise BlendError(
            code="EXPORT_ARTIFACT_MISSING",
            category=ErrorCategory.EXPORT,
            message=f"Export artifact is missing or empty: {path}",
            remediation="Inspect the Blender export log and rerun the declared export.",
        )
    if not _signature_valid(path, format_name):
        raise BlendError(
            code="EXPORT_SIGNATURE_INVALID",
            category=ErrorCategory.EXPORT,
            message=f"Export artifact has an invalid {format_name} signature: {path}",
            remediation="Correct the export profile or exporter compatibility.",
        )
    validator = Path(__file__).resolve().parent.parent / "blend_runtime" / "validate_export.py"
    log_path = log_root / f"export-validate-{operation_id}.log"
    completed = supervisor.run(
        [str(blender), "--background", "--factory-startup", "--python-exit-code", "74",
         "--python", str(validator), "--", "--format", format_name, "--path", str(path)],
        cwd=path.parent,
        log_path=log_path,
        timeout_seconds=180,
        offline=offline,
        enforce_offline=offline,
    )
    payload = None
    for line in completed.stdout.splitlines():
        if line.startswith(_SENTINEL):
            payload = line[len(_SENTINEL):]
    if completed.returncode != 0 or not payload:
        raise BlendError(
            code="EXPORT_DECODE_FAILED",
            category=ErrorCategory.EXPORT,
            message=f"A clean Blender process could not decode {path.name}.",
            remediation="Inspect the retained import-probe log and correct the export profile.",
            details={"format": format_name, "log": str(log_path), "logTail": completed.stdout[-8000:]},
            retained_artifacts=[str(path), str(log_path)],
        )
    inspection = json.loads(payload)
    if inspection.get("objects", 0) == 0:
        raise BlendError(
            code="EXPORT_SELECTION_EMPTY",
            category=ErrorCategory.EXPORT,
            message=f"Decoded export contains no objects: {path}",
            remediation="Correct includeCollections/includeObjects and helper visibility.",
            details={"inspection": inspection},
        )
    limits = declaration.get("limits", {})
    failures = []
    for key, measured_key in (("maxTriangles", "triangles"), ("maxVertices", "vertices"),
                              ("maxObjects", "objects"), ("maxMaterials", "materials")):
        if key in limits and inspection.get(measured_key, 0) > limits[key]:
            failures.append({"limit": key, "allowed": limits[key], "measured": inspection.get(measured_key)})
    if declaration.get("animations") is False and inspection.get("actions", 0):
        failures.append({"field": "animations", "expected": 0, "measured": inspection.get("actions")})
    if not declaration.get("cameras", False) and inspection.get("cameras", 0):
        failures.append({"field": "cameras", "expected": 0, "measured": inspection.get("cameras")})
    if not declaration.get("lights", False) and inspection.get("lights", 0):
        failures.append({"field": "lights", "expected": 0, "measured": inspection.get("lights")})
    if not declaration.get("materials", True) and inspection.get("materials", 0):
        failures.append({"field": "materials", "expected": 0, "measured": inspection.get("materials")})
    if declaration.get("materials", True) and inspection.get("meshes", 0) and inspection.get("materials", 0) == 0:
        failures.append({"field": "materials", "expected": "at least one", "measured": 0})
    if declaration.get("materials", True):
        source_materials = set((source_report or {}).get("sourceMaterialNames", []))
        decoded_materials = set(inspection.get("materialNames", []))
        source_material_keys = {_canonical_export_name(name) for name in source_materials}
        decoded_material_keys = {_canonical_export_name(name) for name in decoded_materials}
        missing_materials = sorted(source_material_keys - decoded_material_keys)
        unexpected_materials = sorted(decoded_material_keys - source_material_keys)
        if missing_materials or unexpected_materials:
            failures.append({
                "field": "materialNames",
                "missingCanonical": missing_materials,
                "unexpectedCanonical": unexpected_materials,
                "source": sorted(source_materials),
                "decoded": sorted(decoded_materials),
            })
    source_custom = {
        key
        for keys in (source_report or {}).get("sourceCustomProperties", {}).values()
        for key in keys
    }
    decoded_custom = {
        key
        for keys in inspection.get("customProperties", {}).values()
        for key in keys
    }
    if declaration.get("customProperties") and not source_custom.issubset(decoded_custom):
        failures.append({
            "field": "customProperties",
            "expected": sorted(source_custom),
            "measured": sorted(decoded_custom),
        })
    if not declaration.get("customProperties", False) and decoded_custom:
        failures.append({
            "field": "customProperties",
            "expected": [],
            "measured": sorted(decoded_custom),
        })
    duplicate_names = [name for name in inspection.get("names", []) if re.search(r"\.\d{3}$", name)]
    if duplicate_names:
        failures.append({"field": "stableNames", "duplicates": duplicate_names})
    if declaration.get("applyTransforms"):
        transform_failures = [
            transform
            for transform in (source_report or {}).get("sourceTransforms", [])
            if transform["type"] == "MESH"
            and (
                any(abs(value - 1.0) > 1e-5 for value in transform["scale"])
                or any(abs(value) > 1e-5 for value in transform["rotation"])
            )
        ]
        if transform_failures:
            failures.append({
                "field": "sourceTransforms",
                "expected": "unit scale and zero rotation before export",
                "measured": transform_failures,
            })
    if source_report:
        source_transforms = source_report.get("sourceTransforms", [])
        source_names = {transform["name"] for transform in source_transforms}
        source_meshes = [
            transform
            for transform in source_transforms
            if transform["type"] == "MESH"
        ]
        decoded_names = set(inspection.get("names", []))
        if format_name == "stl":
            if inspection.get("meshes", 0) != len(source_meshes):
                failures.append({
                    "field": "selection",
                    "expectedMeshCount": len(source_meshes),
                    "measuredMeshCount": inspection.get("meshes", 0),
                    "note": "STL has no portable object-name channel.",
                })
        else:
            source_keys: dict[str, list[str]] = {}
            for name in sorted(source_names):
                source_keys.setdefault(
                    _canonical_export_name(name, object_name=True),
                    [],
                ).append(name)
            collisions = {
                key: names
                for key, names in source_keys.items()
                if len(names) > 1
            }
            if collisions:
                failures.append({
                    "field": "stableNames",
                    "canonicalCollisions": collisions,
                })
            required_mesh_keys = {
                _canonical_export_name(transform["name"], object_name=True)
                for transform in source_meshes
            }
            decoded_keys = {
                _canonical_export_name(name, object_name=True)
                for name in decoded_names
            }
            allowed_generated_keys = {"materials"} if format_name in {"usd", "usda", "usdc"} else set()
            missing_meshes = sorted(required_mesh_keys - decoded_keys)
            unexpected_names = sorted(decoded_keys - set(source_keys) - allowed_generated_keys)
            if missing_meshes or unexpected_names:
                failures.append({
                    "field": "selection",
                    "missingMeshCanonicalNames": missing_meshes,
                    "unexpectedCanonicalNames": unexpected_names,
                    "declaredSelection": sorted(source_names),
                    "decodedSelection": sorted(decoded_names),
                })
    if not declaration.get("includeHidden", False):
        hidden = [item["name"] for item in inspection.get("transforms", []) if item.get("hiddenRender")]
        if hidden:
            failures.append({"field": "hiddenHelpers", "objects": hidden})
    if declaration.get("animations") and frame_start is not None and frame_end is not None:
        outside = [
            action for action in inspection.get("actionRanges", [])
            if action["frameStart"] < frame_start or action["frameEnd"] > frame_end
        ]
        if outside:
            failures.append({
                "field": "animationRange",
                "expected": [frame_start, frame_end],
                "measured": outside,
            })
        source_animation = (source_report or {}).get("animationSources", [])
        if not source_animation:
            failures.append({
                "field": "animationSource",
                "expected": "at least one selected animated source",
                "measured": [],
            })
        source_outside = [
            record
            for record in source_animation
            if record["frameStart"] < frame_start or record["frameEnd"] > frame_end
        ]
        if source_outside:
            failures.append({
                "field": "sourceAnimationRange",
                "expected": [frame_start, frame_end],
                "measured": source_outside,
            })
        if not (
            inspection.get("actions", 0)
            or inspection.get("cacheFiles")
            or inspection.get("animationModifiers")
        ):
            failures.append({
                "field": "decodedAnimation",
                "expected": True,
                "measured": False,
            })
    if declaration.get("packageTextures"):
        unpackaged = [
            image for image in inspection.get("imageDependencies", [])
            if image.get("path")
            and not image.get("packed")
            and not Path(image["path"]).resolve().is_relative_to(path.parent.resolve())
        ]
        if unpackaged:
            failures.append({"field": "packagedTextures", "unpackaged": unpackaged})
    resolved_settings = source_report.get("resolvedSettings", {}) if source_report else {}
    if resolved_settings.get("units") != declaration.get("units"):
        failures.append({
            "field": "units",
            "expected": declaration.get("units"),
            "measured": resolved_settings.get("units"),
        })
    declared_units = declaration.get("units")
    decoded_units = inspection.get("units", {}).get("system")
    if declared_units in {"NONE", "METRIC", "IMPERIAL"} and decoded_units != declared_units:
        failures.append({
            "field": "decodedUnits",
            "expected": declared_units,
            "measured": decoded_units,
        })
    if not math.isclose(
        float(resolved_settings.get("scale", 1.0)),
        float(declaration.get("scale", 1.0)),
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        failures.append({
            "field": "scaleDeclaration",
            "expected": declaration.get("scale", 1.0),
            "measured": resolved_settings.get("scale"),
        })
    source_bounds = (source_report or {}).get("before", {}).get("bounds")
    decoded_bounds = inspection.get("bounds")
    if source_bounds and decoded_bounds:
        expected_dimensions = [
            float(value) * float(declaration.get("scale", 1.0))
            for value in source_bounds["dimensions"]
        ]
        measured_dimensions = [float(value) for value in decoded_bounds["dimensions"]]
        if any(
            not math.isclose(expected, measured, rel_tol=1e-4, abs_tol=1e-5)
            for expected, measured in zip(expected_dimensions, measured_dimensions, strict=True)
        ):
            failures.append({
                "field": "decodedScale",
                "expectedDimensions": expected_dimensions,
                "measuredDimensions": measured_dimensions,
            })
    if declaration.get("requireManifold") and inspection.get("nonManifoldEdges", 0):
        failures.append({
            "field": "manifold",
            "expected": True,
            "nonManifoldEdges": inspection.get("nonManifoldEdges"),
            "boundaryEdges": inspection.get("boundaryEdges"),
        })
    if failures:
        raise BlendError(
            code="EXPORT_PROFILE_VALIDATION_FAILED",
            category=ErrorCategory.EXPORT,
            message=f"Export {declaration['id']!r} violates its measurable profile.",
            remediation="Adjust export selection or optimization limits and export again.",
            details={"failures": failures, "inspection": inspection},
            retained_artifacts=[str(path), str(log_path)],
        )
    report = {
        "schema": 1,
        "id": declaration["id"],
        "format": format_name,
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "signatureValid": True,
        "decodable": True,
        "validator": {"tool": "Blender clean import", "version": blender_version},
        "log": str(log_path),
        "inspection": inspection,
        "limits": limits,
        "failures": [],
        "sourceReport": source_report,
        "checks": {
            "selection": True,
            "hiddenHelpers": True,
            "naming": True,
            "units": {
                "declared": declaration.get("units"),
                "decoded": inspection.get("units"),
                "scale": declaration.get("scale", 1.0),
            },
            "transforms": True,
            "geometryLimits": True,
            "manifold": bool(declaration.get("requireManifold")),
            "materials": bool(declaration.get("materials", True)),
            "texturePackaging": bool(declaration.get("packageTextures")),
            "animationRange": [frame_start, frame_end] if frame_start is not None else None,
            "decodability": True,
        },
    }
    report_path = path.with_suffix(path.suffix + ".export.json")
    atomic_write_json(report_path, report)
    report["report"] = str(report_path)
    return report
