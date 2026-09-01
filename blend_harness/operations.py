"""Production command implementations over real Blender, FFmpeg, and filesystem tooling."""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from .blender import BlenderExecutor, trust_project
from .caches import (
    cache_path,
    clean_caches,
    inspect_caches,
    resolve_simulation_profile,
    simulation_dependency_hash,
    write_cache_manifest,
)
from .capabilities import doctor_report
from .comparison import compare_inputs
from .errors import BlendError, ErrorCategory
from .exports import validate_export
from .libraries import compare_library, update_library
from .manifests import (
    Manifest,
    artifact_record,
    child_operation_id,
    ensure_operation_id_available,
    new_operation_id,
)
from .media import encode_sequence, make_contact_sheet, probe_media, validate_frame
from .planning import plan_project
from .process import ProcessSupervisor
from .project import Project, load_project, migrate_project, output_paths, schema_errors
from .review import create_review_package, record_disposition
from .search import expand_search, measure_candidate, promote_candidate, rank_candidates, save_search_report
from .templates import compare_template_upgrade, initialize_project
from .util import atomic_write_json, ensure_within, load_json, sha256_file, utc_now
from .validation import rule_catalog, validate_project, validation_record_path

_ANY_VARIANT = object()



def _require_inspection_schema(report: dict[str, Any]) -> None:
    errors = schema_errors(report, "inspection-v1.json")
    if errors:
        raise BlendError(
            code="INSPECTION_SCHEMA_INVALID",
            category=ErrorCategory.INSPECTION,
            message="Generated scene inspection failed its versioned compatibility schema.",
            remediation="Inspect the retained Blender log and report this runtime compatibility failure.",
            details={"errors": errors},
        )


_FORMAT_EXTENSION = {"PNG": ".png", "OPEN_EXR": ".exr", "JPEG": ".jpg", "TIFF": ".tif"}
def _ensure_not_interrupted(
    supervisor: ProcessSupervisor,
    operation: str,
    *,
    resume_safe: bool = False,
) -> None:
    if supervisor.interrupted:
        raise BlendError(
            code="PROCESS_INTERRUPTED",
            category=ErrorCategory.INTERRUPTED,
            message=f"{operation} was interrupted; complete artifacts were retained.",
            operation=operation,
            remediation="Retry with a new operation identifier.",
            resume_safe=resume_safe,
        )




def doctor(supervisor: ProcessSupervisor, *, project_path: Path | None,
           explicit_blender: str | None) -> dict[str, Any]:
    project = None
    configuration_errors = []
    if project_path:
        try:
            project = load_project(project_path)
        except BlendError as exc:
            configuration_errors.append(exc.as_dict())
    report = doctor_report(supervisor, project=project, explicit_blender=explicit_blender)
    report["configurationErrors"] = configuration_errors
    report["ready"] = not configuration_errors and not any(
        warning["code"] in {"SECURITY_OFFLINE_UNAVAILABLE", "FFMPEG_H264_ENCODER_MISSING"}
        for warning in report["warnings"]
    )
    return report


def init_project(template: str, destination: Path) -> dict[str, Any]:
    return initialize_project(template, destination)


def migrate(project_path: Path, *, write: bool) -> dict[str, Any]:
    return migrate_project(project_path, write=write)


def trust(project_path: Path) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    path = trust_project(project)
    return {"schema": 1, "project": project.id, "trustDecision": str(path),
            "networkPermission": False, "notice": "Workspace Python is trusted; network remains independently denied by default."}


def validate_config(project_path: Path, *, profile: str | None = None,
                    variant: str | None = None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    report = validate_project(project, profile=profile, variant=variant, operation="validate-config",
                              write_artifact=False)
    if not report["summary"]["passed"]:
        raise BlendError(
            code="CONFIG_VALIDATION_FAILED",
            category=ErrorCategory.CONFIGURATION,
            message=f"Configuration and declared inputs failed validation ({report['summary']['errors']} error(s)).",
            remediation="Correct the reported findings before launching Blender.",
            details={"report": report},
        )
    return report


def _manifest_blender(executor: BlenderExecutor) -> dict[str, Any]:
    return {
        "executable": str(executor.executable),
        "version": executor.version,
        "offline": executor.offline,
    }


def build(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
          explicit_blender: str | None, timeout: float | None, operation_id: str | None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    op_id = new_operation_id("build", operation_id)
    executor = BlenderExecutor(project, supervisor, trust=trust, allow_network=allow_network,
                               explicit_blender=explicit_blender, timeout_seconds=timeout)
    manifest = Manifest(project, "build", op_id, blender=_manifest_blender(executor))
    build_hash = project.dependency_hash(operation="build")
    manifest.value["inputs"]["dependencyHash"] = build_hash
    manifest.write()
    try:
        runtime = executor.run("build", operation_id=op_id)
        metadata = {
            "schema": 1,
            "dependencyHash": build_hash,
            "blenderVersion": executor.version,
            "runtimeVersion": manifest.value["runtimeVersion"],
            "checkpoint": artifact_record(project.paths.checkpoint, root=project.paths.root, kind="blend-checkpoint"),
            "createdAt": utc_now(),
        }
        metadata_path = project.paths.checkpoint.with_suffix(".blendmeta.json")
        atomic_write_json(metadata_path, metadata)
        manifest.add_output(project.paths.checkpoint, kind="blend-checkpoint")
        manifest.add_output(metadata_path, kind="checkpoint-metadata")
        manifest.add_output(Path(runtime["log"]), kind="log")
        manifest.succeed()
        return {"checkpoint": str(project.paths.checkpoint), "metadata": str(metadata_path),
                "manifest": str(manifest.path), "cacheable": True, "dependencyHash": build_hash,
                "blenderVersion": executor.version}
    except BlendError as exc:
        exc.retained_artifacts.append(str(manifest.path))
        manifest.fail(exc)
        raise


def _preview_frames(project: Project, override: list[int] | None = None) -> list[int]:
    start = project.config["project"]["frameStart"]
    end = project.config["project"]["frameEnd"]
    if override:
        frames = override
    else:
        declared = project.config.get("previewFrames", [])
        span = end - start
        automatic = [round(start + span * fraction / 4) for fraction in range(5)] if span else [start]
        frames = [start, end, *automatic, *declared]
        if project.paths.inspection.is_file():
            inspection = load_json(project.paths.inspection)
            frames.extend(marker["frame"] for marker in inspection.get("timelineMarkers", []) if marker.get("camera"))
    invalid = [frame for frame in frames if frame < start or frame > end]
    if invalid:
        raise BlendError(
            code="PREVIEW_FRAME_OUTSIDE_RANGE",
            category=ErrorCategory.CONFIGURATION,
            message=f"Preview frames lie outside {start}..{end}: {invalid}",
            remediation="Correct previewFrames or the project frame range.",
        )
    return sorted(set(int(frame) for frame in frames))


def _preview_views(project: Project, selected: str | None) -> list[str]:
    declared = [str(view.get("id") or view.get("camera") or view.get("generated")) for view in project.config.get("views", [])]
    views = declared or ["active"]
    if selected:
        if selected not in views and selected != "active":
            raise BlendError(
                code="PREVIEW_VIEW_UNKNOWN",
                category=ErrorCategory.CONFIGURATION,
                message=f"Unknown preview view {selected!r}.",
                remediation=f"Choose one of: {', '.join(views)}.",
            )
        views = [selected]
    return views


def preview(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
            explicit_blender: str | None, timeout: float | None, operation_id: str | None,
            profile: str | None, variant: str | None, view: str | None,
            mode: str | None, frame_override: list[int] | None = None,
            _project: Project | None = None) -> dict[str, Any]:
    project = _project or load_project(project_path, create_generated=True)
    op_id = new_operation_id("preview", operation_id)
    resolved_variant = project.resolved_variant(variant)
    profile_name, profile_value = project.resolved_profile(profile or "preview", resolved_variant)
    frames = _preview_frames(project, frame_override)
    views = _preview_views(project, view)
    modes = [mode] if mode else project.config.get("previewModes", ["material"])
    run_root = project.paths.previews / op_id
    jobs = []
    for selected_view in views:
        for frame in frames:
            for selected_mode in modes:
                path = run_root / (variant or "base") / selected_view / selected_mode / f"frame-{frame:06d}.png"
                jobs.append({"view": selected_view, "frame": frame, "mode": selected_mode, "path": str(path)})
    executor = BlenderExecutor(project, supervisor, trust=trust, allow_network=allow_network,
                               explicit_blender=explicit_blender, timeout_seconds=timeout)
    manifest = Manifest(project, "preview", op_id, profile=profile_name, variant=variant,
                        blender=_manifest_blender(executor), expected_frames=frames)
    try:
        preflight = validate_project(
            project,
            profile=profile_name,
            variant=variant,
            operation="preview",
            write_artifact=False,
        )
        active_findings = [
            finding
            for finding in preflight["findings"]
            if not finding.get("suppressed")
        ]
        if not preflight["summary"]["passed"]:
            raise BlendError(
                code="PREVIEW_PREFLIGHT_FAILED",
                category=ErrorCategory.VALIDATION,
                message=f"Preview is blocked by {preflight['summary']['errors']} preflight error(s).",
                operation="preview",
                remediation="Correct the reported configuration, asset, cache, or resource findings.",
                details={"report": preflight},
            )
        for finding in active_findings:
            if finding["severity"] == "warning":
                manifest.add_warning({
                    "code": finding["ruleId"],
                    "message": finding["message"],
                    "scope": finding["scope"],
                })
        runtime = executor.run("preview", operation_id=op_id, profile=profile_name, variant=variant, jobs=jobs, frames=frames)
        inspection_report = load_json(project.paths.inspection)
        _require_inspection_schema(inspection_report)
        inspection_path = run_root / "inspection.json"
        atomic_write_json(inspection_path, inspection_report)
        entries = []
        sampled_frames = runtime.get("sampledFrames", frames)
        manifest.value["expectedFrames"] = sampled_frames
        for output in runtime["outputs"]:
            path = Path(output["path"])
            entry = {
                **output,
                "frameRate": project.config["project"]["frameRate"],
                "frameStart": project.config["project"]["frameStart"],
                "timeSeconds": (output["frame"] - project.config["project"]["frameStart"]) / project.config["project"]["frameRate"],
            }
            entries.append(entry)
            manifest.add_output(path, kind="preview", metadata={key: entry[key] for key in
                                ("view", "camera", "frame", "mode", "timeSeconds")})
        manifest.add_output(inspection_path, kind="inspection")
        contact_sheet = run_root / "contact-sheet.png"
        sheet = make_contact_sheet(
            entries,
            contact_sheet,
            project_id=project.id,
            source_revision=manifest.value["project"].get("sourceRevision"),
            blender_version=executor.version,
            profile=profile_name,
            variant=variant,
            warnings=manifest.value["warnings"],
        )
        manifest.add_output(contact_sheet, kind="contact-sheet", metadata={"entries": len(entries)})
        manifest.add_output(Path(runtime["log"]), kind="log")
        manifest.value["completedFrames"] = sampled_frames
        manifest.succeed()
        return {"previews": entries, "contactSheet": sheet, "inspection": str(inspection_path),
                "manifest": str(manifest.path), "profile": profile_name, "variant": variant,
                "views": sorted({entry.get("view") for entry in entries}), "frames": sampled_frames,
                "modes": modes, "blenderVersion": executor.version}
    except BlendError as exc:
        exc.retained_artifacts.append(str(manifest.path))
        manifest.fail(exc)
        raise


def _latest_manifest(project: Project, operation: str, *, profile: str | None = None,
                     variant: str | None | object = _ANY_VARIANT,
                     statuses: set[str] | None = None,
                     expected_frames: list[int] | None = None,
                     required_frames: list[int] | None = None,
                     prefer_frames: list[int] | None = None,
                     current_inputs: bool = False) -> tuple[Path, dict[str, Any]] | None:
    candidates = []
    preferred = set(prefer_frames or [])
    if not project.paths.artifacts.is_dir():
        return None
    for path in project.paths.artifacts.glob("*.json"):
        try:
            value = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("operation") != operation:
            continue
        if profile is not None and value.get("resolved", {}).get("profileName") != profile:
            continue
        if variant is not _ANY_VARIANT and variant != value.get("resolved", {}).get("variantName"):
            continue
        if statuses and value.get("status") not in statuses:
            continue
        if expected_frames is not None and value.get("expectedFrames") != expected_frames:
            continue
        if required_frames is not None and not set(required_frames).issubset(value.get("completedFrames", [])):
            continue
        if current_inputs:
            resolved = value.get("resolved", {})
            expected_hash = project.dependency_hash(
                profile=resolved.get("profileName"),
                variant=resolved.get("variantName"),
                operation=operation,
            )
            if value.get("inputs", {}).get("dependencyHash") != expected_hash:
                continue
        overlap = len(preferred.intersection(value.get("completedFrames", [])))
        candidates.append((overlap, path.stat().st_mtime_ns, path, value))
    if not candidates:
        return None
    _, _, path, value = max(candidates, key=lambda item: (item[0], item[1]))
    return path, value


def contact_sheet(project_path: Path, *, operation_id: str | None = None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    latest = _latest_manifest(project, "preview", statuses={"succeeded", "partial"})
    if latest is None:
        raise BlendError(
            code="PREVIEW_MANIFEST_MISSING",
            category=ErrorCategory.RENDER_ENGINE,
            message="No completed preview manifest is available.",
            remediation=f"Run blend preview {project.paths.root} first.",
        )
    manifest_path, value = latest
    entries = []
    for output in value.get("outputs", []):
        if output.get("kind") != "preview":
            continue
        path = project.paths.root / output["path"] if not Path(output["path"]).is_absolute() else Path(output["path"])
        entries.append({**output, "path": str(path),
                        "frameRate": value["resolved"]["frameRate"],
                        "frameStart": value["resolved"]["frameStart"]})
    op_id = new_operation_id("contact-sheet", operation_id)
    operation_manifest = Manifest(project, "contact-sheet", op_id)
    try:
        destination = project.paths.previews / op_id / "contact-sheet.png"
        sheet = make_contact_sheet(
            entries,
            destination,
            project_id=project.id,
            source_revision=value["project"].get("sourceRevision"),
            blender_version=value.get("blender", {}).get("version", "unknown"),
            profile=value["resolved"]["profileName"],
            variant=value["resolved"].get("variantName"),
            warnings=value.get("warnings", []),
        )
        operation_manifest.parent(manifest_path)
        operation_manifest.add_output(destination, kind="contact-sheet")
        operation_manifest.succeed()
        return {
            "contactSheet": sheet,
            "sourceManifest": str(manifest_path),
            "manifest": str(operation_manifest.path),
        }
    except BlendError as exc:
        operation_manifest.fail(exc)
        raise


def inspect(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
            explicit_blender: str | None, timeout: float | None, operation_id: str | None,
            profile: str | None, variant: str | None, object_filter: str | None,
            collection_filter: str | None, dependency_filter: str | None,
            view_filter: str | None, finding_filter: str | None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    op_id = new_operation_id("inspect", operation_id)
    executor = BlenderExecutor(project, supervisor, trust=trust, allow_network=allow_network,
                               explicit_blender=explicit_blender, timeout_seconds=timeout)
    manifest = Manifest(project, "inspect", op_id, profile=profile, variant=variant, blender=_manifest_blender(executor))
    try:
        runtime = executor.run("inspect", operation_id=op_id, profile=profile, variant=variant)
        report = load_json(project.paths.inspection)
        _require_inspection_schema(report)
        manifest.add_output(project.paths.inspection, kind="inspection")
        manifest.add_output(Path(runtime["log"]), kind="log")
        manifest.succeed()
        filtered = _filter_inspection(report, object_filter=object_filter, collection_filter=collection_filter,
                                      dependency_filter=dependency_filter, view_filter=view_filter,
                                      finding_filter=finding_filter)
        if finding_filter and project.paths.validation.is_file():
            validation = load_json(project.paths.validation)
            filtered["findings"] = [
                finding
                for finding in validation.get("findings", [])
                if finding_filter.lower() in json.dumps(finding).lower()
            ]
            filtered["validationArtifact"] = str(project.paths.validation)
        return {"inspection": filtered, "completeArtifact": str(project.paths.inspection),
                "manifest": str(manifest.path), "blenderVersion": executor.version}
    except BlendError as exc:
        exc.retained_artifacts.append(str(manifest.path))
        manifest.fail(exc)
        raise


def _filter_inspection(report: dict[str, Any], *, object_filter: str | None,
                       collection_filter: str | None, dependency_filter: str | None,
                       view_filter: str | None, finding_filter: str | None) -> dict[str, Any]:
    if not any((object_filter, collection_filter, dependency_filter, view_filter, finding_filter)):
        return report
    result = {"schema": report["schema"], "filters": {}, "completeReportRetained": True}
    if object_filter:
        result["filters"]["object"] = object_filter
        result["objects"] = [item for item in report.get("objects", []) if object_filter.lower() in item["name"].lower()]
    if collection_filter:
        result["filters"]["collection"] = collection_filter
        result["collections"] = [item for item in report.get("collections", []) if collection_filter.lower() in item["name"].lower()]
    if dependency_filter:
        result["filters"]["dependency"] = dependency_filter
        result["dependencies"] = [item for item in report.get("dependencies", [])
                                   if dependency_filter.lower() in json.dumps(item).lower()]
    if view_filter:
        result["filters"]["view"] = view_filter
        result["framing"] = [item for item in report.get("framing", []) if view_filter.lower() in str(item.get("view", "")).lower()]
    if finding_filter:
        result["filters"]["finding"] = finding_filter
        result["note"] = "Finding filters apply to blend validate; the complete inspection remains retained."
    return result


def validate(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
             explicit_blender: str | None, timeout: float | None, operation_id: str | None,
             profile: str | None, variant: str | None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    op_id = new_operation_id("validate", operation_id)
    executor = BlenderExecutor(project, supervisor, trust=trust, allow_network=allow_network,
                               explicit_blender=explicit_blender, timeout_seconds=timeout)
    manifest = Manifest(project, "validate", op_id, profile=profile, variant=variant, blender=_manifest_blender(executor))
    try:
        runtime = executor.run("validate", operation_id=op_id, profile=profile, variant=variant)
        inspection_report = load_json(project.paths.inspection)
        _require_inspection_schema(inspection_report)
        capabilities = doctor_report(supervisor, project=project, explicit_blender=explicit_blender)
        media_probes = {}
        for asset in project.assets:
            if asset.type == "audio" and asset.path.is_file():
                try:
                    media_probes[asset.id] = probe_media(supervisor, asset.path, log_root=project.paths.logs,
                                                         operation_id=f"{op_id}-{asset.id}")
                    media_probes[str(asset.path)] = media_probes[asset.id]
                except BlendError:
                    pass
        report = validate_project(project, profile=profile, variant=variant, operation="validate",
                                  inspection=inspection_report, capabilities=capabilities,
                                  media_probes=media_probes, blender_version=executor.version)
        retained_validation = validation_record_path(
            project,
            profile=report["profile"],
            variant=report["variant"],
            dependency_hash=report["dependencyHash"],
        )
        manifest.set_validation(report, retained_validation)
        manifest.add_output(project.paths.inspection, kind="inspection")
        manifest.add_output(retained_validation, kind="validation")
        manifest.add_output(project.paths.validation, kind="validation-current")
        manifest.add_output(Path(runtime["log"]), kind="log")
        if not report["summary"]["passed"]:
            error = BlendError(
                code="VALIDATION_FAILED",
                category=ErrorCategory.VALIDATION,
                message=f"Validation found {report['summary']['errors']} blocking error(s).",
                operation="validate",
                remediation=f"Correct findings in {project.paths.validation} and validate again.",
                details={"summary": report["summary"], "report": str(project.paths.validation)},
                retained_artifacts=[str(project.paths.inspection), str(project.paths.validation), str(manifest.path)],
            )
            manifest.fail(error)
            raise error
        manifest.succeed()
        return {"report": report, "inspection": str(project.paths.inspection),
                "validation": str(retained_validation), "validationCurrent": str(project.paths.validation),
                "manifest": str(manifest.path), "blenderVersion": executor.version}
    except BlendError as exc:
        if manifest.value["status"] == "running":
            exc.retained_artifacts.append(str(manifest.path))
            manifest.fail(exc)
        raise


def plan(supervisor: ProcessSupervisor, project_path: Path, *, target: str, profile: str | None,
         variant: str | None, matrix: str | None, output: str | None,
         explicit_blender: str | None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    capabilities = doctor_report(supervisor, project=project, explicit_blender=explicit_blender)
    return plan_project(project, target=target, profile=profile, variant=variant, matrix=matrix,
                        output=output, capabilities=capabilities)


def _render_directory(project: Project, profile: str, variant: str | None) -> Path:
    return project.paths.renders / "final" / (variant or "base") / profile / "frames"


def _effective_concurrency(project: Project, profile: dict[str, Any], requested: int | None) -> tuple[int, list[str]]:
    resources = project.config.get("resources", {})
    declared = int(resources.get("maxProcesses", 1))
    value = min(requested or declared, declared, max(1, (os.cpu_count() or 2) - 1))
    advisories = []
    if profile["engine"] == "CYCLES" and str(profile.get("device", "CPU")).upper() != "CPU":
        gpu_bound = int(resources.get("maxGpuProcesses", 1))
        if value > gpu_bound:
            value = gpu_bound
            advisories.append(f"Concurrency reduced to the declared GPU worker limit of {gpu_bound}.")
    if resources.get("thermalAdvisory", True):
        try:
            pressure = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
            if pressure > 0.8 and value > 1:
                value = max(1, value // 2)
                advisories.append(f"Concurrency reduced because normalized host load is {pressure:.2f}.")
        except OSError:
            pass
    memory_mb = resources.get("maxMemoryMB")
    if memory_mb:
        per_process_mb = max(256, math.ceil(profile["width"] * profile["height"] * 16 / 1024 / 1024))
        memory_bound = max(1, int(memory_mb) // per_process_mb)
        if value > memory_bound:
            value = memory_bound
            advisories.append(f"Concurrency reduced to fit {memory_mb} MiB advisory.")
    return max(1, value), advisories


def _ensure_current_final_validation(
    project: Project, *, profile: str, variant: str | None, operation: str
) -> tuple[dict[str, Any], Path]:
    gate = validate_project(
        project,
        profile=profile,
        variant=variant,
        operation=operation,
        inspection=None,
        require_current_validation=True,
        write_artifact=False,
    )
    if not gate["summary"]["passed"]:
        raise BlendError(
            code="FINAL_PREREQUISITES_FAILED",
            category=ErrorCategory.VALIDATION,
            message=f"{operation} is blocked by {gate['summary']['errors']} final-profile prerequisite error(s).",
            operation=operation,
            remediation=(
                f"Run blend build, then blend validate {project.paths.root} "
                f"--profile {profile}" + (f" --variant {variant}." if variant else ".")
            ),
            details={"report": gate},
        )
    expected = project.dependency_hash(profile=profile, variant=variant, operation="validate")
    retained = validation_record_path(
        project, profile=profile, variant=variant, dependency_hash=expected
    )
    for path in (retained, project.paths.validation):
        if not path.is_file():
            continue
        report = load_json(path)
        if report.get("dependencyHash") == expected and report.get("summary", {}).get("passed"):
            return report, path
    raise AssertionError("successful validation gate did not retain matching evidence")


def render(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
           explicit_blender: str | None, timeout: float | None, operation_id: str | None,
           profile: str | None, variant: str | None, frames: list[int] | None,
           concurrency: int | None, resume_only: bool) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    resolved_variant = project.resolved_variant(variant)
    profile_name, profile_value = project.resolved_profile(profile or "final", resolved_variant)
    validation_report, validation_path = _ensure_current_final_validation(
        project, profile=profile_name, variant=variant, operation="render"
    )
    expected_frames = frames or list(range(project.config["project"]["frameStart"], project.config["project"]["frameEnd"] + 1))
    start = project.config["project"]["frameStart"]
    end = project.config["project"]["frameEnd"]
    outside = [frame for frame in expected_frames if frame < start or frame > end]
    if outside:
        raise BlendError(
            code="RENDER_FRAME_OUTSIDE_RANGE",
            category=ErrorCategory.CONFIGURATION,
            message=f"Requested render frames lie outside {start}..{end}: {outside}",
            remediation="Correct --frames or the project frame range.",
        )
    expected_frames = sorted(set(expected_frames))
    op_id = new_operation_id("resume" if resume_only else "render", operation_id)
    executor = BlenderExecutor(project, supervisor, trust=trust, allow_network=allow_network,
                               explicit_blender=explicit_blender, timeout_seconds=timeout)
    previous = _latest_manifest(
        project,
        "render",
        profile=profile_name,
        variant=variant,
        statuses={"succeeded", "failed", "interrupted", "partial"},
        prefer_frames=expected_frames,
    )
    manifest = Manifest(project, "render", op_id, profile=profile_name, variant=variant,
                        blender=_manifest_blender(executor), expected_frames=expected_frames)
    manifest.set_validation(validation_report, validation_path)
    extension = _FORMAT_EXTENSION[profile_value["format"]]
    render_root = _render_directory(project, profile_name, variant)
    render_root.mkdir(parents=True, exist_ok=True)
    dependency_hash = manifest.dependency_hash
    previous_records = {item["frame"]: item for item in previous[1].get("frames", [])} if previous else {}
    frames_to_render = []
    reused = []
    for frame in expected_frames:
        path = render_root / f"frame-{frame:06d}{extension}"
        if resume_only:
            valid, evidence = validate_frame(
                supervisor, path, width=profile_value["width"], height=profile_value["height"],
                channels=profile_value.get("colorMode", "RGB"), dependency_hash=dependency_hash,
                record=previous_records.get(frame), log_root=project.paths.logs,
                operation_id=f"{op_id}-frame-{frame}",
            )
            if valid:
                manifest.add_frame(frame, path, {"width": profile_value["width"], "height": profile_value["height"],
                                                 "channels": profile_value.get("colorMode", "RGB"),
                                                 "reused": True, "validation": evidence})
                reused.append(frame)
                continue
        frames_to_render.append(frame)
    if previous:
        manifest.parent(previous[0])
    effective_concurrency, advisories = _effective_concurrency(project, profile_value, concurrency)
    for advisory in advisories:
        manifest.add_warning({"code": "RESOURCE_CONCURRENCY_REDUCED", "message": advisory})
    completed = []
    failures: list[BlendError] = []

    def render_one(frame: int) -> tuple[int, Path, dict[str, Any]]:
        path = render_root / f"frame-{frame:06d}{extension}"
        child_id = child_operation_id(op_id, f"frame-{frame:06d}")
        runtime = executor.run("render", operation_id=child_id, profile=profile_name, variant=variant,
                               jobs=[{"frame": frame, "path": str(path)}], frames=[frame])
        output = runtime["outputs"][0]
        return frame, path, output | {"log": runtime["log"]}

    try:
        with ThreadPoolExecutor(max_workers=effective_concurrency, thread_name_prefix="blend-frame") as pool:
            futures = {pool.submit(render_one, frame): frame for frame in frames_to_render}
            for future in as_completed(futures):
                frame = futures[future]
                try:
                    rendered_frame, path, output = future.result()
                    record = {
                        "dependencyHash": dependency_hash,
                        "sha256": sha256_file(path),
                        "bytes": path.stat().st_size,
                    }
                    valid, evidence = validate_frame(
                        supervisor, path, width=profile_value["width"], height=profile_value["height"],
                        channels=profile_value.get("colorMode", "RGB"), dependency_hash=dependency_hash,
                        record=record, log_root=project.paths.logs, operation_id=f"{op_id}-validate-{frame}")
                    if not valid:
                        raise BlendError(
                            code="RENDER_FRAME_INVALID",
                            category=ErrorCategory.RENDER_FRAME,
                            message=f"Rendered frame {frame} failed content validation.",
                            remediation=f"Inspect frame evidence and run blend resume {project.paths.root}.",
                            details={"frame": frame, "evidence": evidence, "path": str(path)},
                            retained_artifacts=[str(path), output["log"]],
                            resume_safe=True,
                        )
                    manifest.add_frame(rendered_frame, path, {
                        "width": profile_value["width"], "height": profile_value["height"],
                        "channels": profile_value.get("colorMode", "RGB"), "reused": False,
                        "durationSeconds": output.get("durationSeconds"), "camera": output.get("camera"),
                        "validation": evidence, "log": output["log"],
                    })
                    completed.append(frame)
                except BlendError as exc:
                    failures.append(exc)
                except Exception as exc:
                    failures.append(BlendError(
                        code="RENDER_FRAME_WORKER_FAILED",
                        category=ErrorCategory.RENDER_FRAME,
                        message=f"Frame {frame} worker failed: {exc}",
                        remediation=f"Inspect logs and run blend resume {project.paths.root}.",
                        details={"frame": frame, "exception": type(exc).__name__},
                        resume_safe=True,
                    ))
        if failures:
            error = failures[0]
            error.details["failedFrames"] = sorted(futures[future] for future in futures if future.exception() is not None) if 'futures' in locals() else []
            error.details["completedFrames"] = sorted(completed)
            error.retained_artifacts.append(str(manifest.path))
            manifest.fail(error)
            raise error
        manifest.value["completedFrames"] = sorted(set(reused + completed))
        manifest.succeed()
        return {
            "manifest": str(manifest.path),
            "frameDirectory": str(render_root),
            "framePattern": str(render_root / f"frame-%06d{extension}"),
            "expectedFrames": expected_frames,
            "renderedFrames": sorted(completed),
            "reusedFrames": sorted(reused),
            "concurrency": effective_concurrency,
            "advisories": advisories,
            "dependencyHash": dependency_hash,
            "blenderVersion": executor.version,
        }
    except BlendError as exc:
        if manifest.value["status"] == "running":
            exc.retained_artifacts.append(str(manifest.path))
            manifest.fail(exc)
        raise


def render_matrix(supervisor: ProcessSupervisor, project_path: Path, *, matrix: str,
                  trust: bool, allow_network: bool, explicit_blender: str | None,
                  timeout: float | None, operation_id: str | None,
                  concurrency: int | None, resume_only: bool) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    members = project.matrix_members(matrix)
    capabilities = doctor_report(supervisor, project=project, explicit_blender=explicit_blender)
    planned = plan_project(project, target="resume" if resume_only else "render", profile=None, variant=None,
                           matrix=matrix, output=None, capabilities=capabilities)
    if not planned["ready"]:
        raise BlendError(
            code="MATRIX_PLAN_BLOCKED",
            category=ErrorCategory.VALIDATION,
            message=f"Matrix {matrix!r} is blocked before rendering.",
            remediation="Correct collisions and blocking validation findings, then plan again.",
            details={"plan": planned},
        )
    root_id = new_operation_id("matrix-resume" if resume_only else "matrix-render", operation_id)
    first = members[0]
    root_manifest = Manifest(
        project,
        "matrix-resume" if resume_only else "matrix-render",
        root_id,
        profile=str(first["profile"]) if first["profile"] else None,
        variant=str(first["variant"]) if first["variant"] else None,
        expected_frames=list(range(len(members))),
    )
    root_manifest.value["inputs"]["matrix"] = {"id": matrix, "members": members}
    root_manifest.write()
    results = []
    failures = []
    interruption: BlendError | None = None
    for index, member in enumerate(members):
        try:
            result = render(
                supervisor, project_path, trust=trust, allow_network=allow_network,
                explicit_blender=explicit_blender, timeout=timeout,
                operation_id=child_operation_id(root_id, f"{index:03d}"),
                profile=str(member["profile"]) if member["profile"] else None,
                variant=str(member["variant"]) if member["variant"] else None,
                frames=None, concurrency=concurrency, resume_only=resume_only,
            )
            results.append({"member": member, "result": result})
            root_manifest.parent(Path(result["manifest"]))
            root_manifest.value["completedFrames"] = [
                *root_manifest.value["completedFrames"],
                index,
            ]
            root_manifest.write()
        except BlendError as exc:
            failures.append({"member": member, "error": exc.as_dict()})
            if exc.category is ErrorCategory.INTERRUPTED or supervisor.interrupted:
                interruption = exc
                break
    report = {
        "schema": 1,
        "matrix": matrix,
        "operationId": root_id,
        "members": len(members),
        "succeeded": results,
        "failed": failures,
        "pending": members[len(results) + len(failures):],
        "selectiveResume": True,
    }
    path = project.paths.artifacts / f"matrix-{root_id}.json"
    atomic_write_json(path, report)
    root_manifest.add_output(path, kind="matrix-report")
    report["report"] = str(path)
    report["manifest"] = str(root_manifest.path)
    if interruption is not None:
        error = BlendError(
            code="PROCESS_INTERRUPTED",
            category=ErrorCategory.INTERRUPTED,
            message=f"Matrix {matrix!r} was interrupted; completed members and frames were retained.",
            remediation=f"Run blend resume {project.paths.root} --matrix {matrix}.",
            details={"report": str(path), "failures": failures, "pending": report["pending"]},
            retained_artifacts=[
                str(path),
                str(root_manifest.path),
                *interruption.retained_artifacts,
                *[item["result"]["manifest"] for item in results],
            ],
            resume_safe=True,
        )
        root_manifest.fail(error)
        raise error
    if failures:
        error = BlendError(
            code="MATRIX_PARTIAL_FAILURE",
            category=ErrorCategory.RENDER_FRAME,
            message=f"{len(failures)} of {len(members)} matrix member(s) failed; valid members were retained.",
            remediation=f"Run blend resume {project.paths.root} --matrix {matrix}.",
            details={"report": str(path), "failures": failures},
            retained_artifacts=[
                str(path),
                str(root_manifest.path),
                *[item["result"]["manifest"] for item in results],
            ],
            resume_safe=True,
        )
        root_manifest.fail(error)
        raise error
    root_manifest.succeed()
    return report


def _resolve_declared_output(project: Project, output: dict[str, Any], variant: str | None) -> Path:
    text = str(output["path"]).replace("{variant}", variant or "base")
    path = Path(text)
    if not path.is_absolute():
        parts = path.parts[1:] if path.parts and path.parts[0] == "output" else path.parts
        path = project.paths.outputs.joinpath(*parts)
    try:
        return ensure_within(path, project.paths.outputs, label=f"output {output['id']}")
    except ValueError as exc:
        raise BlendError(
            code="CONFIG_OUTPUT_ROOT",
            category=ErrorCategory.CONFIGURATION,
            message=str(exc),
            remediation="Move the output under roots.outputs.",
        ) from exc


def _promote_output_stage(stage: Path, output_root: Path, operation_id: str) -> list[Path]:
    staged_files = [path for path in sorted(stage.rglob("*")) if path.is_file()]
    backup = output_root / f".blend-backup-{operation_id}"
    shutil.rmtree(backup, ignore_errors=True)
    promoted: list[Path] = []
    backed_up: list[tuple[Path, Path]] = []
    try:
        for source in staged_files:
            relative = source.relative_to(stage)
            destination = output_root / relative
            if destination.exists():
                backup_path = backup / relative
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup_path)
                backed_up.append((destination, backup_path))
        for source in staged_files:
            destination = output_root / source.relative_to(stage)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            promoted.append(destination)
    except Exception:
        for destination in reversed(promoted):
            destination.unlink(missing_ok=True)
        for destination, backup_path in reversed(backed_up):
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup_path, destination)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
    return promoted


def encode(supervisor: ProcessSupervisor, project_path: Path, *, operation_id: str | None,
           output_id: str | None, profile: str | None, variant: str | None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    outputs = [
        output
        for output in project.config.get("outputs", [])
        if output.get("type") in {"video", "preview-video", "still", "sequence"}
    ]
    if output_id:
        outputs = [output for output in outputs if output["id"] == output_id]
    if not outputs:
        raise BlendError(
            code="ENCODE_OUTPUT_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"No matching delivery output is declared{f' for {output_id!r}' if output_id else ''}.",
            remediation="Declare a video, preview-video, still, or sequence output.",
        )
    results = []
    for index, output in enumerate(outputs):
        output_type = output["type"]
        selected_variant = variant or output.get("variant")
        resolved_variant = project.resolved_variant(selected_variant)
        default_profile = "preview" if output_type == "preview-video" else "final"
        profile_name, profile_value = project.resolved_profile(
            profile or output.get("profile") or default_profile,
            resolved_variant,
        )
        derived_id = (
            child_operation_id(operation_id, f"{index:02d}")
            if operation_id and len(outputs) > 1
            else operation_id
        )
        op_id = new_operation_id("encode", derived_id)
        extension = _FORMAT_EXTENSION[profile_value["format"]]
        destination = _resolve_declared_output(project, output, selected_variant)
        ensure_operation_id_available(project, "encode", op_id)

        if output_type == "preview-video":
            preview_manifest = _latest_manifest(
                project,
                "preview",
                current_inputs=True,
                profile=profile_name,
                variant=selected_variant,
                statuses={"succeeded"},
            )
            if preview_manifest is None:
                raise BlendError(
                    code="ENCODE_PREVIEW_MANIFEST_MISSING",
                    category=ErrorCategory.ENCODING,
                    message=f"No successful preview exists for {profile_name}/{selected_variant or 'base'}.",
                    remediation=f"Run blend preview {project.paths.root} --profile {profile_name}.",
                )
            source_manifest_path, source_manifest = preview_manifest
            preview_records = [
                record
                for record in source_manifest.get("outputs", [])
                if record.get("kind") == "preview"
                and (output.get("view") is None or record.get("view") == output["view"])
                and (output.get("mode") is None or record.get("mode") == output["mode"])
            ]
            if not preview_records:
                raise BlendError(
                    code="ENCODE_PREVIEW_FRAMES_MISSING",
                    category=ErrorCategory.ENCODING,
                    message=f"Preview evidence does not contain frames for output {output['id']!r}.",
                    remediation="Render the declared preview view and mode before encoding.",
                )
            selected_view = output.get("view") or preview_records[0].get("view")
            selected_mode = output.get("mode") or preview_records[0].get("mode")
            preview_records = sorted(
                (
                    record for record in preview_records
                    if record.get("view") == selected_view and record.get("mode") == selected_mode
                ),
                key=lambda record: record["frame"],
            )
            manifest = Manifest(
                project,
                "encode",
                op_id,
                profile=profile_name,
                variant=selected_variant,
                expected_frames=[record["frame"] for record in preview_records],
            )
            manifest.parent(source_manifest_path)
            stage = project.paths.temporary / f"preview-sequence-{op_id}"
            try:
                shutil.rmtree(stage, ignore_errors=True)
                stage.mkdir(parents=True)
                for sequence_index, record in enumerate(preview_records, 1):
                    _ensure_not_interrupted(supervisor, "encode", resume_safe=True)
                    source = Path(record["path"])
                    if not source.is_absolute():
                        source = project.paths.root / source
                    if not source.is_file() or sha256_file(source) != record.get("sha256"):
                        raise BlendError(
                            code="ENCODE_PREVIEW_FRAME_INVALID",
                            category=ErrorCategory.ENCODING,
                            message=f"Preview frame failed checksum validation: {source}",
                            remediation="Rerun the preview before encoding its review video.",
                        )
                    shutil.copy2(source, stage / f"frame-{sequence_index:06d}{extension}")
                encoded = encode_sequence(
                    supervisor,
                    frame_pattern=stage / f"frame-%06d{extension}",
                    frame_start=1,
                    frame_count=len(preview_records),
                    frame_rate=float(output.get("frameRate", 6)),
                    output=output,
                    destination=destination,
                    log_root=project.paths.logs,
                    operation_id=op_id,
                    expected={
                        "width": profile_value["width"] * max(1, int(output.get("pixelUpscale", 1) or 1)),
                        "height": profile_value["height"] * max(1, int(output.get("pixelUpscale", 1) or 1)),
                        "frameRate": float(output.get("frameRate", 6)),
                        "frameCount": len(preview_records),
                        "timeoutSeconds": project.config.get("resources", {}).get("timeoutSeconds"),
                    },
                )
                manifest.add_output(destination, kind="preview-video", metadata={
                    "codec": output.get("codec"),
                    "sourceManifest": str(source_manifest_path),
                    "view": selected_view,
                    "mode": selected_mode,
                })
                manifest.add_output(Path(encoded["validation"]), kind="media-validation")
                manifest.add_output(Path(encoded["log"]), kind="log")
                manifest.add_output(Path(encoded["probe"]["log"]), kind="log")
                manifest.succeed()
                results.append({
                    "output": output["id"],
                    **encoded,
                    "manifest": str(manifest.path),
                    "frameManifest": str(source_manifest_path),
                })
            except BlendError as exc:
                exc.retained_artifacts.append(str(manifest.path))
                manifest.fail(exc)
                raise
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            continue

        validation_report, validation_path = _ensure_current_final_validation(
            project, profile=profile_name, variant=selected_variant, operation="encode"
        )
        full_timeline = list(
            range(
                project.config["project"]["frameStart"],
                project.config["project"]["frameEnd"] + 1,
            )
        )
        target_frames = (
            [int(output.get("frame", project.config["project"]["frameEnd"]))]
            if output_type == "still"
            else full_timeline
        )
        render_manifest = _latest_manifest(
            project,
            "render",
            profile=profile_name,
            variant=selected_variant,
            statuses={"succeeded"},
            expected_frames=full_timeline if output_type in {"video", "sequence"} else None,
            required_frames=target_frames,
            current_inputs=True,
        )
        if render_manifest is None:
            raise BlendError(
                code="ENCODE_FRAME_MANIFEST_MISSING",
                category=ErrorCategory.ENCODING,
                message=f"No suitable successful render manifest exists for {profile_name}/{selected_variant or 'base'}.",
                remediation=(
                    f"Run blend render {project.paths.root} --profile {profile_name}"
                    + (f" --variant {selected_variant}." if selected_variant else ".")
                ),
            )
        render_manifest_path, render_value = render_manifest
        frame_records = {record["frame"]: record for record in render_value.get("frames", [])}
        frame_root = _render_directory(project, profile_name, selected_variant)
        _ensure_not_interrupted(supervisor, "encode", resume_safe=True)
        frame_evidence: dict[int, dict[str, Any]] = {}
        for frame in target_frames:
            _ensure_not_interrupted(supervisor, "encode", resume_safe=True)
            path = frame_root / f"frame-{frame:06d}{extension}"
            valid, evidence = validate_frame(
                supervisor,
                path,
                width=profile_value["width"],
                height=profile_value["height"],
                channels=profile_value.get("colorMode", "RGB"),
                dependency_hash=render_value["inputs"]["dependencyHash"],
                record=frame_records.get(frame),
                log_root=project.paths.logs,
                operation_id=child_operation_id(op_id, f"input-{frame}"),
            )
            if not valid:
                raise BlendError(
                    code="ENCODE_FRAME_INVALID",
                    category=ErrorCategory.ENCODING,
                    message=f"Frame {frame} is invalid for output {output['id']!r}.",
                    remediation=f"Run blend resume {project.paths.root} before encoding.",
                    details={"frame": frame, "evidence": evidence},
                )
            frame_evidence[frame] = evidence

        manifest = Manifest(
            project,
            "encode",
            op_id,
            profile=profile_name,
            variant=selected_variant,
            expected_frames=target_frames,
        )
        manifest.parent(render_manifest_path)
        manifest.set_validation(validation_report, validation_path)
        try:
            if output_type == "still":
                source = frame_root / f"frame-{target_frames[0]:06d}{extension}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(f".{destination.name}.{op_id}.part")
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
                validation = destination.with_suffix(destination.suffix + ".media.json")
                atomic_write_json(validation, {
                    "schema": 1,
                    "expected": {
                        "width": profile_value["width"],
                        "height": profile_value["height"],
                        "channels": profile_value.get("colorMode", "RGB"),
                        "frame": target_frames[0],
                    },
                    "probe": {
                        **frame_evidence[target_frames[0]],
                        "sha256": sha256_file(destination),
                        "bytes": destination.stat().st_size,
                    },
                    "failures": [],
                })
                encoded = {
                    "path": str(destination),
                    "validation": str(validation),
                    "frame": target_frames[0],
                }
                manifest.add_output(destination, kind="still", metadata={
                    "frameManifest": str(render_manifest_path),
                    "frame": target_frames[0],
                })
                manifest.add_output(validation, kind="media-validation")
            elif output_type == "sequence":
                stage = destination.with_name(f".{destination.name}.{op_id}.part")
                shutil.rmtree(stage, ignore_errors=True)
                stage.mkdir(parents=True)
                records = []
                for frame in target_frames:
                    _ensure_not_interrupted(supervisor, "encode", resume_safe=True)
                    source = frame_root / f"frame-{frame:06d}{extension}"
                    copied = stage / source.name
                    shutil.copy2(source, copied)
                    records.append({
                        "frame": frame,
                        "path": copied.name,
                        "sha256": sha256_file(copied),
                        "bytes": copied.stat().st_size,
                    })
                atomic_write_json(stage / "sequence-manifest.json", {
                    "schema": 1,
                    "project": project.id,
                    "profile": profile_name,
                    "variant": selected_variant,
                    "sourceManifest": str(render_manifest_path),
                    "frames": records,
                })
                backup = destination.with_name(f".{destination.name}.{op_id}.previous")
                shutil.rmtree(backup, ignore_errors=True)
                if destination.exists():
                    os.replace(destination, backup)
                os.replace(stage, destination)
                shutil.rmtree(backup, ignore_errors=True)
                encoded = {
                    "path": str(destination),
                    "validation": str(destination / "sequence-manifest.json"),
                    "frames": len(target_frames),
                }
                manifest.add_output(destination / "sequence-manifest.json", kind="sequence", metadata={
                    "frameManifest": str(render_manifest_path),
                    "frames": len(target_frames),
                })
                manifest.add_outputs(
                    (
                        destination / record["path"],
                        "sequence-frame",
                        {"frame": record["frame"], "frameManifest": str(render_manifest_path)},
                    )
                    for record in records
                )
            else:
                resolved_output = copy.deepcopy(output)
                audio_metadata = None
                if output.get("audio"):
                    asset = next((asset for asset in project.assets if asset.id == output["audio"]), None)
                    if asset:
                        resolved_output["audio"] = str(asset.path)
                        audio_metadata = {
                            "asset": asset.id,
                            "sha256": asset.actual_checksum,
                            "bytes": asset.path.stat().st_size,
                        }
                    else:
                        audio = Path(output["audio"])
                        resolved_output["audio"] = str(audio if audio.is_absolute() else project.paths.root / audio)
                encoded = encode_sequence(
                    supervisor,
                    frame_pattern=frame_root / f"frame-%06d{extension}",
                    frame_start=target_frames[0],
                    frame_count=len(target_frames),
                    frame_rate=project.config["project"]["frameRate"],
                    output=resolved_output,
                    destination=destination,
                    log_root=project.paths.logs,
                    operation_id=op_id,
                    expected={
                        "width": profile_value["width"] * max(1, int(resolved_output.get("pixelUpscale", 1) or 1)),
                        "height": profile_value["height"] * max(1, int(resolved_output.get("pixelUpscale", 1) or 1)),
                        "frameRate": project.config["project"]["frameRate"],
                        "frameCount": len(target_frames),
                        "timeoutSeconds": project.config.get("resources", {}).get("timeoutSeconds"),
                    },
                )
                media_metadata = {
                    "codec": output.get("codec"),
                    "frameManifest": str(render_manifest_path),
                }
                if audio_metadata:
                    media_metadata["audio"] = audio_metadata
                manifest.add_output(
                    destination,
                    kind="encoded-media",
                    metadata=media_metadata,
                )
                manifest.add_output(Path(encoded["validation"]), kind="media-validation")
                manifest.add_output(Path(encoded["log"]), kind="log")
                manifest.add_output(Path(encoded["probe"]["log"]), kind="log")
            manifest.succeed()
            results.append({
                "output": output["id"],
                **encoded,
                "manifest": str(manifest.path),
                "frameManifest": str(render_manifest_path),
            })
        except BlendError as exc:
            exc.retained_artifacts.append(str(manifest.path))
            manifest.fail(exc)
            raise
        except Exception as exc:
            error = BlendError(
                code="ENCODE_FINALIZATION_FAILED",
                category=ErrorCategory.ENCODING,
                message=f"Failed to finalize output {output['id']!r}: {exc}",
                remediation="Inspect retained frames and retry the output finalization.",
                details={"exception": type(exc).__name__, "destination": str(destination)},
            )
            manifest.fail(error)
            raise error from exc
    return {"outputs": results, "rerenderedBlenderFrames": 0}


def export(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
           explicit_blender: str | None, timeout: float | None, operation_id: str | None,
           output_id: str | None, profile: str | None, variant: str | None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    declarations = project.config.get("exports", [])
    if output_id:
        declarations = [item for item in declarations if item["id"] == output_id]
    if not declarations:
        raise BlendError(
            code="EXPORT_OUTPUT_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"No matching export is declared{f' for {output_id!r}' if output_id else ''}.",
            remediation="Declare exports[] with a supported format and selection profile.",
        )
    executor: BlenderExecutor | None = None
    results = []
    for index, declaration in enumerate(declarations):
        resolved_declaration = copy.deepcopy(declaration)
        resolved_declaration.setdefault("units", project.config["project"].get("units", "NONE"))
        resolved_declaration.setdefault("scale", project.config["project"].get("unitScale", 1.0))
        selected_variant = variant or declaration.get("variant")
        profile_name, _ = project.resolved_profile(profile or declaration.get("profile") or "final",
                                                    project.resolved_variant(selected_variant))
        validation_report, validation_path = _ensure_current_final_validation(
            project, profile=profile_name, variant=selected_variant, operation="export"
        )
        if executor is None:
            executor = BlenderExecutor(
                project,
                supervisor,
                trust=trust,
                allow_network=allow_network,
                explicit_blender=explicit_blender,
                timeout_seconds=timeout,
            )
        derived_id = (
            child_operation_id(operation_id, f"{index:02d}")
            if operation_id and len(declarations) > 1
            else operation_id
        )
        op_id = new_operation_id("export", derived_id)
        manifest = Manifest(project, "export", op_id, profile=profile_name, variant=selected_variant,
                            blender=_manifest_blender(executor))
        manifest.set_validation(validation_report, validation_path)
        stage_root = project.paths.outputs / f".blend-stage-{op_id}"
        shutil.rmtree(stage_root, ignore_errors=True)
        stage_root.mkdir(parents=True, exist_ok=True)
        try:
            runtime = executor.run(
                "export",
                operation_id=op_id,
                profile=profile_name,
                variant=selected_variant,
                output=declaration["id"],
                jobs=[{"outputRoot": str(stage_root)}],
            )
            path = Path(runtime["outputs"][0]["path"])
            source_report = runtime["outputs"][0]
            validation = validate_export(
                supervisor,
                blender=executor.executable,
                blender_version=executor.version,
                declaration=resolved_declaration,
                path=path,
                log_root=project.paths.logs,
                operation_id=op_id,
                offline=executor.offline,
                source_report=source_report,
                frame_start=project.config["project"]["frameStart"],
                frame_end=project.config["project"]["frameEnd"],
            )
            stage_prefix = str(stage_root)
            output_prefix = str(project.paths.outputs)
            validation_report_stage = Path(validation["report"])
            validation = json.loads(
                json.dumps(validation).replace(stage_prefix, output_prefix)
            )
            source_report = json.loads(
                json.dumps(source_report).replace(stage_prefix, output_prefix)
            )
            atomic_write_json(validation_report_stage, validation)
            runtime_report_path = Path(runtime["report"])
            runtime_report = json.loads(
                json.dumps(load_json(runtime_report_path)).replace(
                    stage_prefix,
                    output_prefix,
                )
            )
            atomic_write_json(runtime_report_path, runtime_report)
            promoted = _promote_output_stage(
                stage_root,
                project.paths.outputs,
                op_id,
            )
            path = Path(str(path).replace(stage_prefix, output_prefix))
            runtime["outputs"][0] = source_report
            manifest.add_output(path, kind="model-export", metadata={
                "format": declaration["format"],
                "selection": source_report.get("selection"),
            })
            manifest.add_output(Path(validation["report"]), kind="export-validation")
            manifest.add_output(Path(runtime["report"]), kind="optimization-report")
            sidecars = [
                sidecar
                for sidecar in promoted
                if sidecar not in {path, Path(validation["report"])}
            ]
            manifest.add_outputs(
                (sidecar, "model-export-sidecar", {"format": declaration["format"]})
                for sidecar in sidecars
            )
            manifest.add_output(Path(runtime["log"]), kind="log")
            manifest.add_output(Path(validation["log"]), kind="log")
            manifest.succeed()
            results.append({
                "id": declaration["id"],
                "path": str(path),
                "runtime": source_report,
                "validation": validation,
                "manifest": str(manifest.path),
            })
        except BlendError as exc:
            if stage_root.is_dir():
                failed_root = project.paths.working / "failed-exports" / op_id
                shutil.rmtree(failed_root, ignore_errors=True)
                shutil.copytree(stage_root, failed_root)
                shutil.rmtree(stage_root, ignore_errors=True)
                exc.retained_artifacts.extend(
                    str(path) for path in failed_root.rglob("*") if path.is_file()
                )
            exc.retained_artifacts.append(str(manifest.path))
            manifest.fail(exc)
            raise
        except Exception as exc:
            shutil.rmtree(stage_root, ignore_errors=True)
            error = BlendError(
                code="EXPORT_FINALIZATION_FAILED",
                category=ErrorCategory.EXPORT,
                message=f"Failed to atomically finalize export {declaration['id']!r}: {exc}",
                remediation="Inspect retained logs and retry with a new operation identifier.",
                details={"exception": type(exc).__name__},
                retained_artifacts=[str(manifest.path)],
            )
            manifest.fail(error)
            raise error from exc
    return {"exports": results}


def _cache_stage_complete(stage: Path, expected_hash: str) -> bool:
    marker = stage / "blend-cache-complete.json"
    if not marker.is_file():
        return False
    try:
        value = load_json(marker)
    except (OSError, json.JSONDecodeError):
        return False
    return value.get("dependencyHash") == expected_hash


def _promote_cache_stage(stage: Path, destination: Path, operation_id: str) -> None:
    backup = destination.with_name(f".{destination.name}.{operation_id}.previous")
    shutil.rmtree(backup, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(stage, destination)
    except Exception:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def bake(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
         explicit_blender: str | None, timeout: float | None, operation_id: str | None,
         simulation_id: str | None, simulation_profile: str | None) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    simulations = [item for item in project.config.get("simulations", [])
                   if simulation_id is None or item["id"] == simulation_id]
    if not simulations:
        raise BlendError(
            code="CACHE_SIMULATION_UNKNOWN",
            category=ErrorCategory.SIMULATION,
            message=f"No matching simulation is declared{f' for {simulation_id!r}' if simulation_id else ''}.",
            remediation="Declare simulations[] or choose an existing simulation id.",
        )
    resolved_profiles = {
        simulation["id"]: resolve_simulation_profile(project, simulation, simulation_profile)
        for simulation in simulations
    }
    op_id = new_operation_id("bake", operation_id)
    executor = BlenderExecutor(project, supervisor, trust=trust, allow_network=allow_network,
                               explicit_blender=explicit_blender, timeout_seconds=timeout)
    manifest = Manifest(project, "bake", op_id, blender=_manifest_blender(executor))
    manifest.value["inputs"]["simulations"] = [
        {
            "id": simulation["id"],
            "profile": resolved_profiles[simulation["id"]][0],
            "settings": resolved_profiles[simulation["id"]][1],
        }
        for simulation in simulations
    ]
    manifest.write()
    records = []
    active: dict[str, Any] | None = None
    active_stage: Path | None = None
    active_hash: str | None = None
    try:
        for index, simulation in enumerate(simulations):
            active = simulation
            profile_id, settings = resolved_profiles[simulation["id"]]
            child_id = child_operation_id(op_id, f"{index:03d}")
            root = cache_path(project, simulation, profile_id)
            stage = root.with_name(f".{root.name}.{child_id}.part")
            shutil.rmtree(stage, ignore_errors=True)
            stage.parent.mkdir(parents=True, exist_ok=True)
            dependency_hash = simulation_dependency_hash(
                project, simulation, simulation_profile=profile_id
            )
            active_stage = stage
            active_hash = dependency_hash
            runtime = executor.run(
                "bake",
                operation_id=child_id,
                jobs=[{
                    "simulation": simulation["id"],
                    "simulationProfile": profile_id,
                    "simulationProfileSettings": settings,
                    "cacheDependencyHash": dependency_hash,
                    "cacheRoot": str(stage),
                }],
            )
            if not _cache_stage_complete(stage, dependency_hash):
                raise BlendError(
                    code="CACHE_BAKE_INCOMPLETE",
                    category=ErrorCategory.SIMULATION,
                    message=f"Simulation {simulation['id']!r} did not produce a valid completion marker.",
                    remediation="Inspect the bake log and retry; the previous complete cache remains unchanged.",
                    retained_artifacts=[str(runtime["log"])],
                    resume_safe=True,
                )
            _promote_cache_stage(stage, root, child_id)
            record = next(
                (item for item in runtime.get("outputs", []) if item["simulation"] == simulation["id"]),
                {"durationSeconds": 0},
            )
            record["cacheRoot"] = str(root)
            record["files"] = [
                str(root / Path(path).relative_to(stage))
                for path in record.get("files", [])
            ]
            cache_manifest = write_cache_manifest(
                project,
                simulation,
                blender_version=executor.version,
                runtime_record=record,
                status="complete",
                duration_seconds=float(record.get("durationSeconds", 0)),
                simulation_profile=profile_id,
            )
            active_stage = None
            active_hash = None
            manifest.add_output(
                cache_manifest,
                kind="simulation-cache-manifest",
                metadata={"simulation": simulation["id"], "simulationProfile": profile_id},
            )
            manifest.add_output(Path(runtime["log"]), kind="log")
            records.append({
                "simulation": simulation["id"],
                "simulationProfile": profile_id,
                "cacheManifest": str(cache_manifest),
                "runtime": record,
            })
            active = None
        manifest.succeed()
        return {"caches": records, "manifest": str(manifest.path)}
    except BlendError as exc:
        if active is not None and active_stage is not None:
            profile_id, _ = resolved_profiles[active["id"]]
            root = cache_path(project, active, profile_id)
            if active_hash and _cache_stage_complete(active_stage, active_hash):
                _promote_cache_stage(active_stage, root, op_id)
            if active_hash and _cache_stage_complete(root, active_hash):
                retained = write_cache_manifest(
                    project,
                    active,
                    blender_version=executor.version,
                    runtime_record={},
                    status="complete",
                    duration_seconds=0,
                    simulation_profile=profile_id,
                )
                exc.retained_artifacts.append(str(retained))
            else:
                shutil.rmtree(active_stage, ignore_errors=True)
        exc.retained_artifacts.append(str(manifest.path))
        manifest.fail(exc)
        raise


def cache_inspect(project_path: Path, simulation_id: str | None,
                  simulation_profile: str | None) -> dict[str, Any]:
    return inspect_caches(
        load_project(project_path, create_generated=True),
        simulation_id,
        simulation_profile,
    )


def cache_clean(project_path: Path, simulation_id: str | None) -> dict[str, Any]:
    return clean_caches(load_project(project_path, create_generated=True), simulation_id)


def compare(
    supervisor: ProcessSupervisor,
    left: Path,
    right: Path,
    artifact_root: Path | None,
    operation_id: str,
) -> dict[str, Any]:
    left = left.expanduser().resolve()
    if artifact_root:
        destination = artifact_root.expanduser().resolve()
    else:
        anchor = left if left.is_dir() else left.parent
        project_root = next(
            (candidate for candidate in (anchor, *anchor.parents) if (candidate / "blend.yaml").is_file()),
            None,
        )
        base = project_root / "build" if project_root else Path.cwd() / "build"
        destination = base / "comparisons" / operation_id
    if destination.exists():
        raise BlendError(
            code="COMPARE_DESTINATION_EXISTS",
            category=ErrorCategory.COMPARISON,
            message=f"Comparison destination already exists: {destination}",
            remediation="Use a new operation identifier or an empty explicit artifact root.",
            retained_artifacts=[str(destination)],
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".{destination.name}.part")
    shutil.rmtree(stage, ignore_errors=True)
    try:
        report = compare_inputs(
            left,
            right,
            stage,
            operation_id=operation_id,
            cancelled=lambda: supervisor.interrupted,
        )
        encoded = json.dumps(report).replace(str(stage), str(destination))
        report = json.loads(encoded)
        atomic_write_json(stage / "comparison.json", report)
        os.replace(stage, destination)
        return report
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def review(
    supervisor: ProcessSupervisor,
    project_path: Path,
    destination: Path | None,
    operation_id: str,
) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    target = destination or project.paths.outputs / f"review-{operation_id}"
    if target.expanduser().resolve().exists():
        raise BlendError(
            code="REVIEW_DESTINATION_EXISTS",
            category=ErrorCategory.REVIEW,
            message=f"Review destination already exists: {target}",
            remediation="Use a new operation identifier or a new explicit output path.",
            retained_artifacts=[str(target)],
        )
    return create_review_package(
        project,
        target,
        cancelled=lambda: supervisor.interrupted,
    )


def review_record(package: Path, *, decision: str, comments: str,
                  selected_variant: str | None) -> dict[str, Any]:
    return record_disposition(package, decision=decision, comments=comments, selected_variant=selected_variant)


def search(supervisor: ProcessSupervisor, project_path: Path, *, trust: bool, allow_network: bool,
           explicit_blender: str | None, timeout: float | None, operation_id: str | None,
           search_id: str) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    op_id = new_operation_id("search", operation_id)
    expanded = expand_search(project, search_id)
    declaration = expanded["declaration"]
    profile_name = declaration.get("profile", "preview")
    base_variant = declaration["baseVariant"]
    manifest = Manifest(
        project,
        "search",
        op_id,
        profile=profile_name,
        variant=base_variant,
    )
    manifest.value["inputs"]["search"] = expanded
    manifest.write()
    checkpoint_files = (
        project.paths.checkpoint,
        project.paths.checkpoint.with_suffix(".blendmeta.json"),
    )
    backups: dict[Path, Path] = {}
    for source in checkpoint_files:
        if source.is_file():
            backup = project.paths.temporary / f"{op_id}-{source.name}.backup"
            shutil.copy2(source, backup)
            backups[source] = backup
    evidence = []
    blender_version = "unknown"
    try:
        for index, candidate in enumerate(expanded["candidates"]):
            candidate_project = Project(
                project.paths,
                copy.deepcopy(project.config),
                project.brief,
                project.assets,
                project.libraries,
            )
            candidate_project.config.setdefault("variants", {})[candidate["id"]] = candidate["variant"]
            result = preview(
                supervisor,
                project_path,
                trust=trust,
                allow_network=allow_network,
                explicit_blender=explicit_blender,
                timeout=timeout,
                operation_id=child_operation_id(op_id, f"{index:03d}"),
                profile=profile_name,
                variant=candidate["id"],
                view=declaration.get("view"),
                mode="material",
                frame_override=[project.config["project"]["frameStart"]],
                _project=candidate_project,
            )
            manifest.parent(Path(result["manifest"]))
            image = Path(result["previews"][0]["path"]) if result["previews"] else None
            inspection_report = (
                load_json(project.paths.inspection)
                if project.paths.inspection.is_file()
                else None
            )
            metrics = measure_candidate(
                candidate,
                image=image,
                inspection=inspection_report,
                render_seconds=sum(
                    float(item.get("durationSeconds", 0))
                    for item in result["previews"]
                ),
            )
            evidence.append({
                "candidateId": candidate["id"],
                "metrics": metrics,
                "artifacts": [
                    *(str(item["path"]) for item in result["previews"]),
                    result["contactSheet"]["path"],
                    result["manifest"],
                ],
            })
            blender_version = result["blenderVersion"]
        ranked = rank_candidates(expanded, evidence)
        ranked["operationId"] = op_id
        ranked_entries = []
        evidence_by_id = {item["candidateId"]: item for item in evidence}
        for candidate in ranked["ranking"]:
            artifacts = evidence_by_id[candidate["id"]]["artifacts"]
            image = next((
                Path(path)
                for path in artifacts
                if Path(path).suffix.lower() == ".png"
                and Path(path).name.startswith("frame-")
            ), None)
            if image:
                ranked_entries.append({
                    "path": str(image),
                    "view": f"rank {candidate['rank']} · {candidate['id']}",
                    "mode": "mechanical-search",
                    "frame": project.config["project"]["frameStart"],
                    "frameRate": project.config["project"]["frameRate"],
                    "frameStart": project.config["project"]["frameStart"],
                })
        if ranked_entries:
            ranked_sheet_path = project.paths.previews / f"search-{search_id}-{op_id}-ranked.png"
            ranked["rankedContactSheet"] = make_contact_sheet(
                ranked_entries,
                ranked_sheet_path,
                project_id=project.id,
                source_revision=None,
                blender_version=blender_version,
                profile=profile_name,
                variant=f"search:{search_id}",
                warnings=["Scores are declared mechanical measurements only."],
            )
            manifest.add_output(ranked_sheet_path, kind="search-contact-sheet")
        report_path = save_search_report(project, search_id, ranked, op_id)
        manifest.add_output(report_path, kind="search-report")
        manifest.value["blender"] = {"version": blender_version}
        manifest.succeed()
        return {
            "search": ranked,
            "report": str(report_path),
            "manifest": str(manifest.path),
            "blenderVersion": blender_version,
        }
    except BlendError as exc:
        exc.retained_artifacts.append(str(manifest.path))
        manifest.fail(exc)
        raise
    except Exception as exc:
        error = BlendError(
            code="SEARCH_EXECUTION_FAILED",
            category=ErrorCategory.INTERNAL,
            message=f"Bounded search failed: {exc}",
            remediation="Inspect the retained root and candidate manifests before retrying.",
            details={"exception": type(exc).__name__},
            retained_artifacts=[str(manifest.path)],
        )
        manifest.fail(error)
        raise error from exc
    finally:
        for source in checkpoint_files:
            backup = backups.get(source)
            if backup and backup.is_file():
                temporary = source.with_name(f".{source.name}.{op_id}.restore")
                shutil.copy2(backup, temporary)
                os.replace(temporary, source)
                backup.unlink(missing_ok=True)
            else:
                source.unlink(missing_ok=True)


def promote(project_path: Path, *, search_report: Path, candidate_id: str,
            variant_name: str) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    return promote_candidate(project, search_report=search_report, candidate_id=candidate_id,
                             variant_name=variant_name)


def library_compare(project_path: Path, library_id: str, candidate_path: Path) -> dict[str, Any]:
    return compare_library(load_project(project_path, create_generated=True), library_id, candidate_path)


def library_update(project_path: Path, library_id: str, candidate_path: Path) -> dict[str, Any]:
    return update_library(load_project(project_path, create_generated=True), library_id, candidate_path)


def template_upgrade(project_path: Path) -> dict[str, Any]:
    return compare_template_upgrade(project_path)


def clean(project_path: Path, *, include_outputs: bool) -> dict[str, Any]:
    project = load_project(project_path, create_generated=True)
    roots = [project.paths.working, project.paths.previews, project.paths.renders]
    if include_outputs:
        roots.append(project.paths.outputs)
    removed = []
    manifests = list(project.paths.artifacts.glob("*.json")) if project.paths.artifacts.is_dir() else []
    candidates: set[Path] = set(manifests)
    for manifest_path in manifests:
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        for output in manifest.get("outputs", []):
            path = Path(output.get("path", ""))
            if not path.is_absolute():
                path = project.paths.root / path
            candidates.add(path.resolve())
        error = manifest.get("error", {})
        for path_text in error.get("retainedArtifacts", []):
            candidates.add(Path(path_text).resolve())
    for path in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        if not path.is_file():
            continue
        if any(_is_within(path, root) for root in roots):
            removed.append({"path": str(path), "bytes": path.stat().st_size})
            path.unlink()
    for root in roots:
        if root.is_dir():
            for directory in sorted((item for item in root.rglob("*") if item.is_dir()),
                                    key=lambda item: len(item.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
    return {"schema": 1, "project": project.id, "removed": removed,
            "bytes": sum(item["bytes"] for item in removed), "sourceDeleted": False,
            "assetsDeleted": False, "outputsIncluded": include_outputs}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
