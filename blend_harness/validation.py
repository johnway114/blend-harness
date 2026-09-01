"""Conservative mechanical validation with stable documented rule identifiers."""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError

from .caches import inspect_caches, simulation_dependency_records
from .errors import BlendError, ErrorCategory
from .project import Project, output_paths, schema_errors
from .util import atomic_write_json, load_json, sha256_file, utc_now


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    severity: str
    description: str
    evidence: str
    configuration: str
    remediation: str


_RULES = [
    Rule("CONFIG.SCHEMA_SUPPORTED", "error", "Project schema must be supported.", "Schema validator result.", "schema", "Migrate with a compatible Blend release."),
    Rule("CONFIG.REQUIRED_PROFILE", "error", "Preview and final profiles are required.", "Declared profile names.", "profiles", "Declare preview and final profiles."),
    Rule("CONFIG.FRAME_RANGE", "error", "Frame range must be ordered.", "frameStart and frameEnd.", "project.frameStart/project.frameEnd", "Correct the frame range."),
    Rule("CONFIG.OUTPUT_ROOT", "error", "Outputs must remain inside the output root.", "Resolved absolute path and root.", "roots.outputs", "Move output under the configured root."),
    Rule("CONFIG.CODEC_AVAILABLE", "error", "Requested codecs must be available.", "FFmpeg encoder capability report.", "outputs[].codec", "Install an encoder or choose an available codec."),
    Rule("CONFIG.ENGINE_AVAILABLE", "error", "Render engines must exist in Blender.", "Blender engine capability report.", "profiles[].engine", "Choose an available engine."),
    Rule("CONFIG.DEVICE_AVAILABLE", "error", "Requested render device must exist.", "Cycles device capability report.", "profiles[].device", "Choose an available device or CPU."),
    Rule("CONFIG.DIMENSIONS_VALID", "error", "Render dimensions and aspect ratio must be positive.", "Resolved width and height.", "profiles/variants", "Set positive dimensions."),
    Rule("CONFIG.ENTRYPOINT_EXISTS", "error", "Scene entry point must exist.", "Resolved entry-point path.", "entrypoint", "Restore scene.py or update entrypoint."),
    Rule("CONFIG.OUTPUT_COLLISION", "error", "Declared outputs must not collide.", "Resolved output path groups.", "outputs/exports/matrices", "Give each output a unique resolved path."),
    Rule("CONFIG.DIRECT_CONTAINER_FINAL", "error", "Final animation must render frame sequences first.", "Final output and profile declarations.", "outputs/profiles", "Render an image sequence and encode separately."),
    Rule("ASSET.MISSING", "error", "Required assets must exist.", "Resolved asset filesystem status.", "assets", "Restore or repin the asset."),
    Rule("ASSET.PATH_OUTSIDE_ROOT", "error", "Asset paths must remain in declared roots.", "Resolved path containment.", "roots.assets", "Move the asset or configure an explicit root."),
    Rule("ASSET.UNDECLARED_EXTERNAL", "error", "Evaluated external dependencies must be declared.", "Blender dependency report compared with asset paths.", "assets/libraries", "Declare and pin the dependency."),
    Rule("ASSET.FORMAT_UNSUPPORTED", "error", "Asset format must be supported for its type.", "File suffix and declared asset type.", "assets[].type", "Convert the asset or declare the correct type."),
    Rule("ASSET.UNREADABLE", "error", "Assets must be readable and decodable where supported.", "Filesystem access and decoder result.", "assets", "Replace the unreadable asset."),
    Rule("ASSET.ZERO_BYTES", "error", "Assets must not be empty.", "Filesystem byte count.", "assets", "Restore a non-empty asset."),
    Rule("ASSET.CHECKSUM_DRIFT", "error", "Pinned asset checksum must match.", "Declared and actual SHA-256.", "assets[].checksum", "Review and repin the intended version."),
    Rule("ASSET.DUPLICATE_LARGE", "warning", "Large duplicate assets waste storage.", "Matching SHA-256 and size.", "assets", "Reference one canonical asset."),
    Rule("ASSET.TEXTURE_TOO_LARGE", "warning", "Texture resolution exceeds the declared limit.", "Decoded texture dimensions.", "assets[].maximumTextureDimension", "Use a suitable texture resolution."),
    Rule("ASSET.FONT_LICENSE_METADATA", "warning", "Declared fonts should include licensing metadata.", "Font asset declaration.", "assets[].fontLicenseMetadata", "Record font license metadata."),
    Rule("SCENE.NO_ACTIVE_CAMERA", "error", "Scene must have an active camera.", "Evaluated active camera.", "scene", "Assign an active camera."),
    Rule("SCENE.DECLARED_CAMERA_MISSING", "error", "Every declared camera must exist.", "View declarations and evaluated camera names.", "views", "Build or rename the camera."),
    Rule("SCENE.NO_RENDERABLE_OBJECT", "error", "Scene needs a renderable visible object.", "Evaluated renderable object count.", "scene", "Add or enable renderable geometry."),
    Rule("SCENE.CAMERA_CLIPPING", "error", "Subject bounds must not cross camera clip planes.", "Projected evaluated bounds.", "views[].framing", "Move the camera or adjust clipping planes."),
    Rule("SCENE.SUBJECT_OUTSIDE_FRAME", "warning", "Declared subjects should satisfy framing coverage.", "Projected camera coverage and bounds.", "views[].framing", "Reframe the camera or subject."),
    Rule("SCENE.CAMERA_CLIP_RANGE", "error", "Camera clip range must be positive and ordered.", "Camera clipStart and clipEnd.", "camera", "Correct the near and far clip values."),
    Rule("SCENE.NEGATIVE_SCALE", "warning", "Negative scale is forbidden by policy.", "Evaluated object scale.", "policies.requireAppliedScale", "Apply or correct transforms."),
    Rule("SCENE.UNAPPLIED_SCALE", "warning", "Non-unit scale is forbidden by policy.", "Evaluated object scale.", "policies.requireAppliedScale", "Apply object scale."),
    Rule("SCENE.NON_MANIFOLD", "error", "Closed meshes must be manifold.", "Boundary and non-manifold edge counts.", "policies.requireClosedMeshes/exports[].requireManifold", "Repair mesh topology."),
    Rule("SCENE.MISSING_MATERIAL", "warning", "Renderable meshes should have assigned materials.", "Material slots and assignments.", "scene", "Assign a material or document the intentional default."),
    Rule("SCENE.EXPORT_MODIFIER_UNSUPPORTED", "error", "Exported objects cannot use unsupported live state.", "Modifier and export profile data.", "exports", "Apply, bake, or exclude the modifier."),
    Rule("SCENE.DISABLED_FOR_RENDER", "warning", "Declared hero objects must be render-enabled.", "Object render visibility.", "project.heroObjects", "Enable the object or remove it from heroObjects."),
    Rule("SCENE.DUPLICATE_STABLE_NAME", "error", "Stable names must not use automatic duplicate suffixes.", "Object names ending in numeric duplicate suffixes.", "policies.requireStableNames", "Give objects explicit stable names."),
    Rule("SCENE.TITLE_SAFE", "warning", "Declared title-safe margin must contain subjects.", "Projected bounds and title-safe margin.", "views[].framing.titleSafe", "Move title content inside the margin."),
    Rule("SCENE.SUBJECT_SAFE", "warning", "Declared subject-safe margin must contain subjects.", "Projected bounds and subject-safe margin.", "views[].framing.subjectSafe", "Reframe the subject."),
    Rule("ANIMATION.KEYFRAMES_OUTSIDE_RANGE", "warning", "Keyframes should lie within the project frame range.", "Evaluated action keyframes.", "project frame range", "Move or remove out-of-range keyframes."),
    Rule("ANIMATION.ENDS_BEFORE_DURATION", "error", "Timeline must meet brief duration.", "Frame range, frame rate, and durationSeconds.", "brief.durationSeconds", "Extend the timeline or correct the brief."),
    Rule("ANIMATION.NO_MOVEMENT", "error", "Animation deliverables require animated change.", "Evaluated animation curves.", "brief.deliverables", "Add intended animation or remove the deliverable."),
    Rule("ANIMATION.FINAL_HOLD_SHORT", "warning", "Final keyframe must leave the declared hold.", "Last keyframe and required hold frames.", "outputs[].finalHoldSeconds/brief.acceptance", "Move the last change earlier or extend the timeline."),
    Rule("ANIMATION.CAMERA_CUT_MISSING", "error", "Every declared camera cut needs a valid camera.", "Timeline marker camera bindings.", "camera cuts", "Assign a valid camera at each cut."),
    Rule("ANIMATION.CACHE_MISSING_OR_STALE", "error", "Required simulation cache must be complete and current.", "Cache manifest dependency hash and expected files.", "simulations", "Run blend bake and validate the cache."),
    Rule("ANIMATION.SEED_UNDECLARED", "error", "Nondeterministic systems require a declared seed.", "Simulation and project seed declarations.", "project.seed/simulations[].seed", "Declare a stable seed or nondeterminism reason."),
    Rule("ANIMATION.AUDIO_TOO_SHORT", "error", "Required audio must cover the timeline.", "Media duration and timeline duration.", "outputs[].audioRequired", "Provide longer audio or shorten the timeline."),
    Rule("PERF.TRIANGLE_LIMIT", "error", "Triangle count must respect the hard limit.", "Evaluated triangle count.", "resources.maxTriangles", "Reduce geometry or raise the reviewed budget."),
    Rule("PERF.OBJECT_LIMIT", "error", "Object count must respect the hard limit.", "Evaluated object count.", "resources.maxObjects", "Reduce objects or raise the reviewed budget."),
    Rule("PERF.TEXTURE_MEMORY_LIMIT", "error", "Texture bytes must respect the hard limit.", "Evaluated texture byte count.", "resources.maxTextureBytes", "Reduce textures or raise the reviewed budget."),
    Rule("PERF.SAMPLE_LIMIT", "error", "Samples must respect the hard limit.", "Resolved profile samples.", "resources.maxSamples", "Reduce samples or raise the reviewed budget."),
    Rule("PERF.FRAME_LIMIT", "error", "Frame count must respect the hard limit.", "Resolved frame count.", "resources.maxFrames", "Reduce frames or raise the reviewed budget."),
    Rule("PERF.RESOLUTION_LIMIT", "error", "Resolution must respect the hard pixel limit.", "Resolved width times height.", "resources.maxResolutionPixels", "Reduce dimensions or raise the reviewed budget."),
    Rule("PERF.CYCLES_DEVICE", "error", "Cycles needs the declared available device.", "Capability report and profile.", "profiles[].device", "Choose an available device."),
    Rule("PERF.SUBDIVISION_HIGH", "warning", "Subdivision render level may be unexpectedly high.", "Evaluated subdivision modifiers.", "scene modifiers", "Reduce render levels or document the cost."),
    Rule("PERF.PREVIEW_SIMULATION_HIGH_COST", "warning", "Preview must not use undeclared final simulation state.", "Profile simulationProfile and simulations.", "profiles[].simulationProfile", "Declare a low-cost preview simulation profile."),
    Rule("PERF.FINAL_PROFILE_IN_PREVIEW", "error", "Preview cannot accidentally resolve a final profile.", "Operation and resolved profile.final.", "profiles", "Select a non-final preview profile."),
    Rule("PERF.OUTPUT_STORAGE_LIMIT", "error", "Estimated output must fit declared disk budget.", "Estimated output bytes and available storage.", "resources.maxDiskBytes", "Reduce output or raise the budget."),
    Rule("PERF.MEMORY_LIMIT", "error", "A render worker must fit the declared memory ceiling.", "Estimated minimum worker memory.", "resources.maxMemoryMB", "Reduce resolution or raise the reviewed memory budget."),
    Rule("SIMULATION.CACHE_PARTIAL", "error", "Simulation caches must not be partial.", "Expected and present cache files.", "simulations[].expectedFiles", "Resume or rebuild the cache."),
    Rule("SIMULATION.CACHE_INCOMPATIBLE", "error", "Cache Blender version and range must be compatible.", "Cache manifest version and range.", "simulations", "Rebake with the selected Blender version."),
    Rule("SIMULATION.CACHE_TOO_LARGE", "error", "Cache must respect its byte ceiling.", "Cache file byte total.", "simulations[].maximumBytes", "Reduce or explicitly raise the cache budget."),
    Rule("SIMULATION.DEPENDENCY_MISSING", "error", "Every declared simulation dependency must exist.", "Declared dependency paths and file state.", "simulations[].dependencies", "Restore the dependency or correct its declared path."),
    Rule("COMPOSITOR.DEPENDENCY_MISSING", "error", "Compositor file dependencies must exist.", "Compositor and dependency report.", "scene compositor", "Restore or declare the dependency."),
    Rule("COLOR.SPACE_INCONSISTENT", "warning", "Declared asset color space should match Blender interpretation.", "Asset colorSpace and image colorspace.", "assets[].colorSpace", "Set a consistent color space."),
    Rule("OUTPUT.ALPHA_MISSING", "error", "Alpha outputs require RGBA and transparent film.", "Profile color mode and film setting.", "outputs[].alpha/profiles", "Enable RGBA and transparency."),
    Rule("CHECKPOINT.STALE", "error", "Generated checkpoint must match authoritative inputs.", "Checkpoint sidecar dependency hash.", "build artifacts", "Run blend build before final work."),
    Rule("FINAL.ASSET_UNPINNED", "error", "Final and export profiles require pinned assets.", "Asset checksum declarations.", "assets[].checksum", "Pin every asset and font SHA-256."),
    Rule("FINAL.VALIDATION_REQUIRED", "error", "Final work requires a current successful validation.", "Validation dependency hash and summary.", "validation artifact", "Run blend validate with the final profile."),
]
RULES = {rule.id: rule for rule in _RULES}


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".hdr", ".webp"}
_MODEL_SUFFIXES = {".blend", ".glb", ".gltf", ".fbx", ".obj", ".abc", ".stl", ".usd", ".usda", ".usdc"}
_FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}
_AUDIO_SUFFIXES = {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg"}
_DATA_SUFFIXES = {".json", ".csv", ".tsv", ".yaml", ".yml", ".txt", ".xml"}


def rule_catalog() -> dict[str, Any]:
    return {
        "schema": 1,
        "rules": [
            {
                "id": rule.id,
                "defaultSeverity": rule.severity,
                "description": rule.description,
                "evidence": rule.evidence,
                "configuration": rule.configuration,
                "remediation": rule.remediation,
            }
            for rule in _RULES
        ],
    }


def _finding(rule_id: str, *, message: str | None = None, evidence: dict[str, Any] | None = None,
             scope: str = "project", severity: str | None = None) -> dict[str, Any]:
    rule = RULES[rule_id]
    return {
        "ruleId": rule_id,
        "severity": severity or rule.severity,
        "message": message or rule.description,
        "evidence": evidence or {},
        "remediation": rule.remediation,
        "scope": scope,
        "suppressed": False,
    }


def _profile_is_final(name: str | None, value: dict[str, Any]) -> bool:
    return bool(value.get("final")) or name in {"final", "export"}


def _apply_policy(project: Project, findings: list[dict[str, Any]], *, profile: str | None,
                  variant: str | None) -> list[dict[str, Any]]:
    policies = project.config.get("policies", {})
    promote = set(policies.get("promote", []))
    suppressions = policies.get("suppress", [])
    active_scopes = {"*", "project"}
    if profile:
        active_scopes.add(f"profile:{profile}")
    if variant:
        active_scopes.add(f"variant:{variant}")
    for finding in findings:
        if finding["ruleId"] in promote and finding["severity"] != "error":
            finding["originalSeverity"] = finding["severity"]
            finding["severity"] = "error"
        for suppression in suppressions:
            # A suppression may target the run (project/profile/variant) or the
            # finding's own scope, e.g. "view:hero" or "object:headland". Without
            # the latter the only way to silence one view is to silence the rule
            # across the whole project, which is strictly worse.
            if suppression["rule"] == finding["ruleId"] and (
                suppression["scope"] in active_scopes
                or suppression["scope"] == finding["scope"]
            ):
                finding["suppressed"] = True
                finding["suppressionReason"] = suppression["reason"]
                break
    return findings


def _asset_allowed_suffix(asset_type: str, suffix: str) -> bool:
    groups = {
        "texture": _IMAGE_SUFFIXES,
        "reference": _IMAGE_SUFFIXES | _MODEL_SUFFIXES | _AUDIO_SUFFIXES | _DATA_SUFFIXES,
        "model": _MODEL_SUFFIXES,
        "font": _FONT_SUFFIXES,
        "audio": _AUDIO_SUFFIXES,
        "data": _DATA_SUFFIXES,
    }
    return asset_type not in groups or suffix.lower() in groups[asset_type]


def _validate_assets(project: Project, findings: list[dict[str, Any]], inspection: dict[str, Any] | None) -> None:
    checksum_groups: dict[tuple[str, int], list[str]] = collections.defaultdict(list)
    declared_paths: set[Path] = set()
    for asset in project.assets:
        if asset.catalog is None and not _is_within(asset.path.resolve(), project.paths.assets.resolve()):
            findings.append(_finding(
                "ASSET.PATH_OUTSIDE_ROOT",
                message=f"Local asset {asset.id!r} is outside the configured asset root.",
                evidence={"path": str(asset.path), "root": str(project.paths.assets)},
                scope=f"asset:{asset.id}",
            ))
        for dependency_record in asset.declared.get("resolvedDependencies", []):
            dependency = Path(dependency_record["path"])
            declared_paths.add(dependency.resolve())
            if not dependency.is_file() or dependency.stat().st_size == 0:
                findings.append(_finding(
                    "ASSET.MISSING",
                    message=f"Catalog dependency for {asset.id!r} is missing or empty.",
                    evidence={"asset": asset.id, "path": str(dependency)},
                    scope=f"asset:{asset.id}",
                ))
                continue
            declared_checksum = dependency_record.get("checksum")
            actual_checksum = sha256_file(dependency)
            if declared_checksum and declared_checksum != actual_checksum:
                findings.append(_finding(
                    "ASSET.CHECKSUM_DRIFT",
                    message=f"Catalog dependency for {asset.id!r} drifted.",
                    evidence={
                        "asset": asset.id,
                        "path": str(dependency),
                        "declared": declared_checksum,
                        "actual": actual_checksum,
                    },
                    scope=f"asset:{asset.id}",
                ))
        declared_paths.add(asset.path.resolve())
        if not asset.path.is_file():
            findings.append(_finding("ASSET.MISSING", message=f"Asset {asset.id!r} is missing.",
                                     evidence={"id": asset.id, "path": str(asset.path)}, scope=f"asset:{asset.id}"))
            continue
        try:
            with asset.path.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            findings.append(_finding("ASSET.UNREADABLE", message=f"Asset {asset.id!r} is unreadable.",
                                     evidence={"path": str(asset.path), "error": str(exc)}, scope=f"asset:{asset.id}"))
            continue
        size = asset.path.stat().st_size
        if size == 0:
            findings.append(_finding("ASSET.ZERO_BYTES", message=f"Asset {asset.id!r} is empty.",
                                     evidence={"path": str(asset.path)}, scope=f"asset:{asset.id}"))
        if not _asset_allowed_suffix(asset.type, asset.path.suffix):
            findings.append(_finding("ASSET.FORMAT_UNSUPPORTED", message=f"Asset {asset.id!r} has unsupported {asset.path.suffix} format.",
                                     evidence={"type": asset.type, "suffix": asset.path.suffix}, scope=f"asset:{asset.id}"))
        if asset.checksum and asset.checksum != asset.actual_checksum:
            findings.append(_finding("ASSET.CHECKSUM_DRIFT", message=f"Asset {asset.id!r} checksum drifted.",
                                     evidence={"declared": asset.checksum, "actual": asset.actual_checksum,
                                               "path": str(asset.path)}, scope=f"asset:{asset.id}"))
        if asset.actual_checksum and size >= 1024 * 1024:
            checksum_groups[(asset.actual_checksum, size)].append(asset.id)
        if asset.type in {"texture", "reference"} and asset.path.suffix.lower() in _IMAGE_SUFFIXES:
            try:
                with Image.open(asset.path) as image:
                    image.verify()
                    dimensions = image.size
                limit = asset.declared.get("maximumTextureDimension")
                if limit and max(dimensions) > limit:
                    findings.append(_finding("ASSET.TEXTURE_TOO_LARGE", message=f"Texture {asset.id!r} exceeds {limit}px.",
                                             evidence={"dimensions": dimensions, "limit": limit}, scope=f"asset:{asset.id}"))
            except (OSError, UnidentifiedImageError) as exc:
                findings.append(_finding("ASSET.UNREADABLE", message=f"Image asset {asset.id!r} cannot be decoded.",
                                         evidence={"path": str(asset.path), "error": str(exc)}, scope=f"asset:{asset.id}"))
        if asset.type == "font" and not asset.declared.get("fontLicenseMetadata"):
            findings.append(_finding("ASSET.FONT_LICENSE_METADATA", message=f"Font {asset.id!r} has no license metadata.",
                                     evidence={"path": str(asset.path)}, scope=f"asset:{asset.id}"))
    for (_, size), ids in checksum_groups.items():
        if len(ids) > 1:
            findings.append(_finding("ASSET.DUPLICATE_LARGE", evidence={"assets": sorted(ids), "bytesEach": size}))
    for library in project.libraries:
        if library.actual_checksum != library.declared_checksum:
            findings.append(_finding("ASSET.CHECKSUM_DRIFT", message=f"Library {library.id!r} checksum drifted.",
                                     evidence={"declared": library.declared_checksum, "actual": library.actual_checksum},
                                     scope=f"library:{library.id}"))
        if library.manifest.get("id") != library.id or library.manifest.get("version") != library.version:
            findings.append(_finding("ASSET.CHECKSUM_DRIFT", message=f"Library {library.id!r} identity or version drifted.",
                                     evidence={"declaredVersion": library.version,
                                               "manifestId": library.manifest.get("id"),
                                               "manifestVersion": library.manifest.get("version")},
                                     scope=f"library:{library.id}"))
    if inspection:
        for dependency in inspection.get("dependencies", []):
            path_text = dependency.get("path")
            if not path_text or not dependency.get("exists"):
                if not dependency.get("exists"):
                    findings.append(_finding("ASSET.MISSING", message=f"Blender dependency is missing: {path_text or dependency.get('declaredPath')}",
                                             evidence=dependency, scope=f"dependency:{dependency.get('owner')}"))
                continue
            dependency_path = Path(path_text).resolve()
            if dependency_path not in declared_paths and not any(
                _is_within(dependency_path, library.path) for library in project.libraries
            ):
                findings.append(_finding("ASSET.UNDECLARED_EXTERNAL", message=f"Undeclared Blender dependency: {dependency_path}",
                                         evidence=dependency, scope=f"dependency:{dependency.get('owner')}"))
        image_by_path = {Path(image["path"]).resolve(): image for image in inspection.get("images", []) if image.get("path")}
        for asset in project.assets:
            if asset.type == "texture" and asset.path.resolve() in image_by_path and asset.color_space:
                actual = image_by_path[asset.path.resolve()].get("colorspace")
                if actual != asset.color_space:
                    findings.append(_finding("COLOR.SPACE_INCONSISTENT", message=f"Texture {asset.id!r} uses {actual!r}, expected {asset.color_space!r}.",
                                             evidence={"declared": asset.color_space, "actual": actual}, scope=f"asset:{asset.id}"))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_config(project: Project, findings: list[dict[str, Any]], *, profile_name: str,
                     profile: dict[str, Any], capabilities: dict[str, Any] | None,
                     operation: str) -> None:
    profiles = project.config["profiles"]
    missing_profiles = [name for name in ("preview", "final") if name not in profiles]
    if missing_profiles:
        findings.append(_finding("CONFIG.REQUIRED_PROFILE", evidence={"missing": missing_profiles, "declared": sorted(profiles)}))
    start = project.config["project"]["frameStart"]
    end = project.config["project"]["frameEnd"]
    if end < start:
        findings.append(_finding("CONFIG.FRAME_RANGE", evidence={"frameStart": start, "frameEnd": end}))
    if profile["width"] <= 0 or profile["height"] <= 0:
        findings.append(_finding("CONFIG.DIMENSIONS_VALID", evidence={"profile": profile_name,
                                                                       "width": profile["width"], "height": profile["height"]}))
    if operation == "preview" and _profile_is_final(profile_name, profile):
        findings.append(_finding("PERF.FINAL_PROFILE_IN_PREVIEW", evidence={"profile": profile_name}))
    simulation_profile = profile.get("simulationProfile")
    if (
        operation == "preview"
        and simulation_profile
        and project.config.get("simulationProfiles", {}).get(simulation_profile, {}).get("quality") == "final"
    ):
        findings.append(_finding(
            "PERF.PREVIEW_SIMULATION_HIGH_COST",
            evidence={"profile": profile_name, "simulationProfile": simulation_profile},
            scope=f"profile:{profile_name}",
        ))
    path_groups: dict[str, list[str]] = collections.defaultdict(list)
    try:
        for record in output_paths(project, variants=[None]):
            path_groups[str(record["path"])].append(f"{record['id']}:{record['variant']}")
    except Exception as exc:
        findings.append(_finding("CONFIG.OUTPUT_ROOT", evidence={"error": str(exc)}))
    for path, owners in path_groups.items():
        if len(owners) > 1:
            findings.append(_finding("CONFIG.OUTPUT_COLLISION", evidence={"path": path, "owners": owners}))
    for output in project.config.get("outputs", []):
        if output.get("type") == "video" and output.get("profile"):
            selected = profiles.get(output["profile"], {})
            if selected.get("format") in {"FFMPEG", "AVI_JPEG", "AVI_RAW"}:
                findings.append(_finding("CONFIG.DIRECT_CONTAINER_FINAL", evidence={"output": output["id"], "profile": output["profile"]}))
    if capabilities:
        engines = set(capabilities.get("blender", {}).get("engines", []))
        devices = capabilities.get("blender", {}).get("cyclesDevices", [])
        device_types = {str(device.get("type", "")).upper() for device in devices}
        aliases = {"BLENDER_EEVEE_NEXT": "BLENDER_EEVEE"}
        for name, declared in profiles.items():
            available_engine = declared["engine"] in engines or aliases.get(declared["engine"]) in engines
            if engines and not available_engine:
                findings.append(_finding("CONFIG.ENGINE_AVAILABLE", evidence={"profile": name, "engine": declared["engine"],
                                                                               "available": sorted(engines)}, scope=f"profile:{name}"))
            device = str(declared.get("device", "")).upper()
            cycles_device_error = None
            if declared["engine"] == "CYCLES" and device == "GPU":
                cycles_device_error = "Cycles profiles must declare a concrete device backend rather than generic GPU."
            elif declared["engine"] == "CYCLES" and device not in {"", "CPU"} and device not in device_types:
                cycles_device_error = f"Cycles device {device!r} is unavailable."
            if cycles_device_error:
                evidence = {"profile": name, "device": device, "available": sorted(device_types)}
                findings.append(_finding(
                    "CONFIG.DEVICE_AVAILABLE",
                    message=cycles_device_error,
                    evidence=evidence,
                    scope=f"profile:{name}",
                ))
                findings.append(_finding(
                    "PERF.CYCLES_DEVICE",
                    message=cycles_device_error,
                    evidence=evidence,
                    scope=f"profile:{name}",
                ))
        codecs = capabilities.get("ffmpeg", {}).get("codecs", {})
        for output in project.config.get("outputs", []):
            codec = output.get("codec")
            group = "alpha" if codec in {"prores-alpha", "ffv1"} else codec
            if codec and not codecs.get(group):
                findings.append(_finding("CONFIG.CODEC_AVAILABLE", evidence={"output": output["id"], "codec": codec,
                                                                             "available": codecs}, scope=f"output:{output['id']}"))


def _validate_scene(project: Project, findings: list[dict[str, Any]], inspection: dict[str, Any]) -> None:
    scene = inspection.get("scene", {})
    cameras = {camera["name"]: camera for camera in inspection.get("cameras", [])}
    objects = inspection.get("objects", [])
    object_map = {obj["name"]: obj for obj in objects}
    if not scene.get("activeCamera"):
        findings.append(_finding("SCENE.NO_ACTIVE_CAMERA"))
    for view in project.config.get("views", []):
        camera_name = view.get("camera")
        if camera_name and camera_name not in cameras:
            findings.append(_finding("SCENE.DECLARED_CAMERA_MISSING", message=f"Declared camera {camera_name!r} is missing.",
                                     evidence={"camera": camera_name}, scope=f"view:{camera_name}"))
    if inspection.get("statistics", {}).get("renderableObjects", 0) == 0:
        findings.append(_finding("SCENE.NO_RENDERABLE_OBJECT"))
    for camera in cameras.values():
        if camera.get("clipStart", 0) <= 0 or camera.get("clipEnd", 0) <= camera.get("clipStart", 0):
            findings.append(_finding("SCENE.CAMERA_CLIP_RANGE", evidence=camera, scope=f"camera:{camera['name']}"))
    framing_by_view = {record.get("view"): record for record in inspection.get("framing", [])}
    for view in project.config.get("views", []):
        view_id = view.get("id") or view.get("camera") or view.get("generated")
        record = framing_by_view.get(view_id)
        if not record:
            continue
        if record.get("nearClipped") or record.get("farClipped"):
            findings.append(_finding("SCENE.CAMERA_CLIPPING", evidence=record, scope=f"view:{view_id}"))
        framing = view.get("framing", {})
        coverage = record.get("coverage", 0)
        if (("minimumCoverage" in framing and coverage < framing["minimumCoverage"]) or
                ("maximumCoverage" in framing and coverage > framing["maximumCoverage"]) or
                (framing.get("requireFullyVisible") and not record.get("fullyVisible"))):
            findings.append(_finding("SCENE.SUBJECT_OUTSIDE_FRAME", evidence={"measured": record, "required": framing},
                                     scope=f"view:{view_id}"))
        bounds = record.get("bounds")
        if bounds:
            for key, rule_id in (("titleSafe", "SCENE.TITLE_SAFE"), ("subjectSafe", "SCENE.SUBJECT_SAFE")):
                margin = framing.get(key)
                if margin is not None and (bounds[0] < margin or bounds[1] < margin or bounds[2] > 1 - margin or bounds[3] > 1 - margin):
                    findings.append(_finding(rule_id, evidence={"bounds": bounds, "margin": margin}, scope=f"view:{view_id}"))
    policies = project.config.get("policies", {})
    manifold_objects: set[str] = set()
    manifold_collections: set[str] = set()
    manifold_all_exports = False
    for export in project.config.get("exports", []):
        if not export.get("requireManifold"):
            continue
        manifold_objects.update(export.get("includeObjects", []))
        manifold_collections.update(export.get("includeCollections", []))
        manifold_all_exports = manifold_all_exports or not (
            export.get("includeObjects") or export.get("includeCollections")
        )
    for obj in objects:
        if policies.get("requireAppliedScale"):
            scale = obj.get("scale", [1, 1, 1])
            if any(value < 0 for value in scale):
                findings.append(_finding("SCENE.NEGATIVE_SCALE", evidence={"object": obj["name"], "scale": scale}, scope=f"object:{obj['name']}"))
            if any(abs(abs(value) - 1) > 1e-5 for value in scale):
                findings.append(_finding("SCENE.UNAPPLIED_SCALE", evidence={"object": obj["name"], "scale": scale}, scope=f"object:{obj['name']}"))
        requires_manifold = (
            policies.get("requireClosedMeshes", False)
            or manifold_all_exports
            or obj["name"] in manifold_objects
            or bool(set(obj.get("collectionNames", [])) & manifold_collections)
        )
        if requires_manifold and obj.get("type") == "MESH" and obj.get("nonManifoldEdges", 0):
            findings.append(_finding("SCENE.NON_MANIFOLD", evidence={"object": obj["name"],
                                                                      "nonManifoldEdges": obj.get("nonManifoldEdges"),
                                                                      "boundaryEdges": obj.get("boundaryEdges")}, scope=f"object:{obj['name']}"))
        if obj.get("type") == "MESH" and obj.get("visibleRender") and (not obj.get("materials") or obj.get("missingMaterialSlots")):
            findings.append(_finding("SCENE.MISSING_MATERIAL", evidence={"object": obj["name"], "materials": obj.get("materials")}, scope=f"object:{obj['name']}"))
        for modifier in obj.get("modifiers", []):
            if modifier.get("type") in {"FLUID", "CLOTH", "PARTICLE_SYSTEM", "NODES"} and project.config.get("exports"):
                findings.append(_finding("SCENE.EXPORT_MODIFIER_UNSUPPORTED", severity="warning",
                                         evidence={"object": obj["name"], "modifier": modifier}, scope=f"object:{obj['name']}"))
            level = modifier.get("renderLevels")
            if level is not None and level > 3:
                findings.append(_finding("PERF.SUBDIVISION_HIGH", evidence={"object": obj["name"], "modifier": modifier}, scope=f"object:{obj['name']}"))
    for hero in project.config["project"].get("heroObjects", []):
        if hero in object_map and not object_map[hero].get("visibleRender"):
            findings.append(_finding("SCENE.DISABLED_FOR_RENDER", evidence={"object": hero}, scope=f"object:{hero}"))
    if policies.get("requireStableNames"):
        for obj in objects:
            if obj["name"].rsplit(".", 1)[-1].isdigit() and len(obj["name"].rsplit(".", 1)[-1]) == 3:
                findings.append(_finding("SCENE.DUPLICATE_STABLE_NAME", evidence={"object": obj["name"]}, scope=f"object:{obj['name']}"))
    for output in project.config.get("outputs", []):
        if output.get("alpha"):
            variant = project.resolved_variant(output.get("variant")) if output.get("variant") else {}
            _, profile = project.resolved_profile(output.get("profile"), variant)
            if profile.get("colorMode") != "RGBA" or not profile.get("transparent"):
                findings.append(_finding("OUTPUT.ALPHA_MISSING", evidence={"output": output["id"], "profile": profile}, scope=f"output:{output['id']}"))


def _validate_animation(project: Project, findings: list[dict[str, Any]], inspection: dict[str, Any],
                        media_probes: dict[str, dict[str, Any]] | None) -> None:
    start = project.config["project"]["frameStart"]
    end = project.config["project"]["frameEnd"]
    fps = float(project.config["project"]["frameRate"])
    keyframes = sorted({frame for record in inspection.get("animation", []) for frame in record.get("keyframes", [])})
    outside = [frame for frame in keyframes if frame < start or frame > end]
    if outside:
        findings.append(_finding("ANIMATION.KEYFRAMES_OUTSIDE_RANGE", evidence={"keyframes": outside, "range": [start, end]}))
    duration = (end - start + 1) / fps
    brief_duration = project.brief.get("durationSeconds")
    if brief_duration and duration + 1e-6 < brief_duration:
        findings.append(_finding("ANIMATION.ENDS_BEFORE_DURATION", evidence={"timelineSeconds": duration, "briefSeconds": brief_duration}))
    wants_animation = any(any(word in deliverable.lower() for word in ("animation", "video", "film")) for deliverable in project.brief.get("deliverables", []))
    if wants_animation and len(keyframes) < 2 and not inspection.get("drivers") and not inspection.get("simulations"):
        findings.append(_finding("ANIMATION.NO_MOVEMENT", evidence={"keyframes": keyframes, "drivers": len(inspection.get("drivers", [])),
                                                                     "simulations": len(inspection.get("simulations", []))}))
    holds = [float(output.get("finalHoldSeconds", 0)) for output in project.config.get("outputs", []) if output.get("finalHoldSeconds")]
    required_hold = max(holds, default=0)
    if required_hold and keyframes:
        available = (end - max(keyframes)) / fps
        if available + 1e-6 < required_hold:
            findings.append(_finding("ANIMATION.FINAL_HOLD_SHORT", evidence={"requiredSeconds": required_hold,
                                                                             "availableSeconds": available,
                                                                             "lastKeyframe": max(keyframes)}))
    missing_cut_cameras = [
        marker
        for marker in inspection.get("timelineMarkers", [])
        if marker.get("name", "").lower().startswith(("camera:", "cut:"))
        and not marker.get("camera")
    ]
    for marker in missing_cut_cameras:
        findings.append(_finding(
            "ANIMATION.CAMERA_CUT_MISSING",
            evidence=marker,
            scope=f"frame:{marker.get('frame')}",
        ))
    for simulation in project.config.get("simulations", []):
        if simulation.get("seed") is None and project.config["project"].get("seed") is None and not simulation.get("deterministic"):
            findings.append(_finding("ANIMATION.SEED_UNDECLARED", evidence={"simulation": simulation["id"]}, scope=f"simulation:{simulation['id']}"))
    for output in project.config.get("outputs", []):
        if output.get("audioRequired") and output.get("audio"):
            probe = (media_probes or {}).get(output["audio"])
            if probe and probe.get("durationSeconds", 0) + 1e-6 < duration:
                findings.append(_finding("ANIMATION.AUDIO_TOO_SHORT", evidence={"output": output["id"],
                                                                               "audioSeconds": probe.get("durationSeconds"),
                                                                               "timelineSeconds": duration}, scope=f"output:{output['id']}"))


def _validate_resources(project: Project, findings: list[dict[str, Any]], inspection: dict[str, Any] | None,
                        profile_name: str, profile: dict[str, Any]) -> None:
    resources = {**project.config.get("resources", {}), **profile.get("limits", {})}
    frames = project.config["project"]["frameEnd"] - project.config["project"]["frameStart"] + 1
    checks = [
        ("maxSamples", profile["samples"], "PERF.SAMPLE_LIMIT"),
        ("maxFrames", frames, "PERF.FRAME_LIMIT"),
        ("maxResolutionPixels", profile["width"] * profile["height"], "PERF.RESOLUTION_LIMIT"),
    ]
    if inspection:
        stats = inspection.get("statistics", {})
        checks.extend([
            ("maxTriangles", stats.get("triangles", 0), "PERF.TRIANGLE_LIMIT"),
            ("maxObjects", stats.get("objects", 0), "PERF.OBJECT_LIMIT"),
            ("maxTextureBytes", stats.get("textureBytes", 0), "PERF.TEXTURE_MEMORY_LIMIT"),
        ])
    channels = 4 if profile.get("colorMode") == "RGBA" else 3
    bytes_per_channel = 4 if profile["format"] == "OPEN_EXR" else 1
    estimated_bytes = frames * profile["width"] * profile["height"] * channels * bytes_per_channel
    checks.append(("maxDiskBytes", estimated_bytes, "PERF.OUTPUT_STORAGE_LIMIT"))
    for key, measured, rule_id in checks:
        limit = resources.get(key)
        if limit is not None and measured > limit:
            findings.append(_finding(rule_id, evidence={"profile": profile_name, "measured": measured, "limit": limit}, scope=f"profile:{profile_name}"))
    minimum_worker_mb = max(
        256,
        math.ceil(profile["width"] * profile["height"] * channels * 16 / 1024 / 1024),
    )
    if resources.get("maxMemoryMB") is not None and minimum_worker_mb > resources["maxMemoryMB"]:
        findings.append(_finding(
            "PERF.MEMORY_LIMIT",
            evidence={
                "profile": profile_name,
                "estimatedWorkerMB": minimum_worker_mb,
                "limitMB": resources["maxMemoryMB"],
            },
            scope=f"profile:{profile_name}",
        ))


def _validate_caches(project: Project, findings: list[dict[str, Any]], blender_version: str | None,
                     simulation_profile: str | None) -> None:
    report = inspect_caches(project, simulation_profile=simulation_profile)
    simulations = {item["id"]: item for item in project.config.get("simulations", [])}
    for record in report["caches"]:
        simulation = simulations[record["id"]]
        cache_root = Path(record["root"])
        manifest_path = cache_root / "cache-manifest.json"
        manifest = (
            load_json(manifest_path)
            if manifest_path.is_file() and not record.get("manifestError")
            else {}
        )
        missing_dependencies = [
            source
            for source in simulation_dependency_records(project, simulation)
            if source.get("missing")
        ]
        if missing_dependencies:
            findings.append(_finding(
                "SIMULATION.DEPENDENCY_MISSING",
                evidence={"simulation": simulation["id"], "missing": missing_dependencies},
                scope=f"simulation:{simulation['id']}",
            ))
        if not record["current"]:
            findings.append(_finding(
                "ANIMATION.CACHE_MISSING_OR_STALE",
                evidence={
                    "simulation": simulation["id"],
                    "manifest": record["manifest"],
                    "declaredHash": record["expectedDependencyHash"],
                    "manifestHash": record["manifestDependencyHash"],
                    "status": record["status"],
                    "rangeAndSettingsCurrent": record["rangeAndSettingsCurrent"],
                    "outputFailures": record["outputFailures"],
                },
                scope=f"simulation:{simulation['id']}",
            ))
        if record["missingExpectedFiles"] or record["outputFailures"]:
            findings.append(_finding(
                "SIMULATION.CACHE_PARTIAL",
                evidence={
                    "simulation": simulation["id"],
                    "missing": record["missingExpectedFiles"],
                    "outputFailures": record["outputFailures"],
                },
                scope=f"simulation:{simulation['id']}",
            ))
        if blender_version and manifest and manifest.get("blenderVersion") != blender_version:
            findings.append(_finding(
                "SIMULATION.CACHE_INCOMPATIBLE",
                evidence={
                    "simulation": simulation["id"],
                    "cacheVersion": manifest.get("blenderVersion"),
                    "blenderVersion": blender_version,
                },
                scope=f"simulation:{simulation['id']}",
            ))
        maximum = simulation.get("maximumBytes")
        if maximum is not None and record["bytes"] > maximum:
            findings.append(_finding(
                "SIMULATION.CACHE_TOO_LARGE",
                evidence={"simulation": simulation["id"], "bytes": record["bytes"], "limit": maximum},
                scope=f"simulation:{simulation['id']}",
            ))


def validation_record_path(project: Project, *, profile: str, variant: str | None,
                           dependency_hash: str) -> Path:
    return (
        project.paths.working
        / "validations"
        / profile
        / (variant or "base")
        / f"{dependency_hash}.json"
    )


def validate_project(project: Project, *, profile: str | None, variant: str | None,
                     operation: str, inspection: dict[str, Any] | None = None,
                     capabilities: dict[str, Any] | None = None,
                     media_probes: dict[str, dict[str, Any]] | None = None,
                     blender_version: str | None = None,
                     require_current_validation: bool = False,
                     write_artifact: bool = True) -> dict[str, Any]:
    resolved_variant = project.resolved_variant(variant)
    profile_name, profile_value = project.resolved_profile(profile, resolved_variant)
    findings: list[dict[str, Any]] = []
    _validate_config(project, findings, profile_name=profile_name, profile=profile_value,
                     capabilities=capabilities, operation=operation)
    _validate_assets(project, findings, inspection)
    _validate_resources(project, findings, inspection, profile_name, profile_value)
    if inspection:
        _validate_scene(project, findings, inspection)
        _validate_animation(project, findings, inspection, media_probes)
        for dependency in inspection.get("dependencies", []):
            if dependency.get("kind") == "compositor" and not dependency.get("exists"):
                findings.append(_finding("COMPOSITOR.DEPENDENCY_MISSING", evidence=dependency))
    if operation != "validate-config":
        _validate_caches(
            project,
            findings,
            blender_version,
            profile_value.get("simulationProfile"),
        )
    final = _profile_is_final(profile_name, profile_value) or operation in {"render", "encode", "export", "bake"}
    if final:
        for asset in project.assets:
            if not asset.checksum and not (operation == "preview" and asset.declared.get("allowUnpinnedPreview")):
                findings.append(_finding("FINAL.ASSET_UNPINNED", evidence={"asset": asset.id}, scope=f"asset:{asset.id}"))
        checkpoint_meta = project.paths.checkpoint.with_suffix(".blendmeta.json")
        expected_build_hash = project.dependency_hash(operation="build")
        if not checkpoint_meta.is_file():
            findings.append(_finding("CHECKPOINT.STALE", evidence={"checkpoint": str(project.paths.checkpoint), "metadata": str(checkpoint_meta)}))
        else:
            metadata = load_json(checkpoint_meta)
            if metadata.get("dependencyHash") != expected_build_hash or not project.paths.checkpoint.is_file():
                findings.append(_finding("CHECKPOINT.STALE", evidence={"expected": expected_build_hash,
                                                                       "actual": metadata.get("dependencyHash"),
                                                                       "checkpointExists": project.paths.checkpoint.is_file()}))
        if require_current_validation:
            expected = project.dependency_hash(profile=profile_name, variant=variant, operation="validate")
            retained = validation_record_path(
                project,
                profile=profile_name,
                variant=variant,
                dependency_hash=expected,
            )
            candidates = [path for path in (project.paths.validation, retained) if path.is_file()]
            current = next(
                (
                    value
                    for path in candidates
                    if (value := load_json(path)).get("dependencyHash") == expected
                    and value.get("summary", {}).get("passed")
                ),
                None,
            )
            if current is None:
                findings.append(_finding("FINAL.VALIDATION_REQUIRED", evidence={
                    "expected": expected,
                    "checked": [str(project.paths.validation), str(retained)],
                }))
    findings = _apply_policy(project, findings, profile=profile_name, variant=variant)
    active = [finding for finding in findings if not finding["suppressed"]]
    summary = {
        "errors": sum(1 for finding in active if finding["severity"] == "error"),
        "warnings": sum(1 for finding in active if finding["severity"] == "warning"),
        "info": sum(1 for finding in active if finding["severity"] == "info"),
    }
    summary["passed"] = summary["errors"] == 0
    report = {
        "schema": 1,
        "project": project.id,
        "profile": profile_name,
        "variant": variant,
        "createdAt": utc_now(),
        "dependencyHash": project.dependency_hash(profile=profile_name, variant=variant, operation="validate"),
        "summary": summary,
        "findings": findings,
        "mechanicalOnly": True,
        "visualReviewStatements": list(project.brief.get("acceptance", [])),
    }
    errors = schema_errors(report, "validation-v1.json")
    if errors:
        raise BlendError(
            code="VALIDATION_REPORT_SCHEMA_INVALID",
            category=ErrorCategory.INTERNAL,
            message="Generated validation evidence failed its versioned schema.",
            remediation="Report this Blend runtime compatibility failure.",
            details={"errors": errors},
        )
    if write_artifact:
        atomic_write_json(project.paths.validation, report)
        atomic_write_json(
            validation_record_path(
                project,
                profile=profile_name,
                variant=variant,
                dependency_hash=report["dependencyHash"],
            ),
            report,
        )
    return report
