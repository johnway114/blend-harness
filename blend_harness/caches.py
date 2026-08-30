"""Declared simulation cache inspection, manifests, invalidation, and cleanup."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import RUNTIME_VERSION
from .errors import BlendError, ErrorCategory
from .project import Project, schema_errors
from .util import (
    atomic_write_json,
    canonical_json,
    hash_tree,
    load_json,
    sha256_bytes,
    sha256_file,
    utc_now,
)


def cache_path(
    project: Project,
    simulation: dict[str, Any],
    simulation_profile: str | None = None,
) -> Path:
    value = Path(simulation["cacheRoot"])
    base = value if value.is_absolute() else project.paths.cache / value
    path = (base / simulation_profile if simulation_profile else base).resolve()
    try:
        path.relative_to(project.paths.cache.resolve())
    except ValueError as exc:
        raise BlendError(
            code="CACHE_PATH_OUTSIDE_ROOT",
            category=ErrorCategory.SIMULATION,
            message=f"Simulation cache escapes the configured cache root: {path}",
            remediation="Move cacheRoot under roots.cache.",
        ) from exc
    return path


def resolve_simulation_profile(
    project: Project, simulation: dict[str, Any], requested: str | None
) -> tuple[str | None, dict[str, Any]]:
    profile_id = requested or simulation.get("finalProfile") or simulation.get("previewProfile")
    if profile_id is None:
        return None, {}
    profiles = project.config.get("simulationProfiles", {})
    if profile_id not in profiles:
        raise BlendError(
            code="CACHE_PROFILE_UNKNOWN",
            category=ErrorCategory.SIMULATION,
            message=f"Unknown simulation profile {profile_id!r} for {simulation['id']!r}.",
            remediation="Declare simulationProfiles or correct the simulation profile reference.",
        )
    return str(profile_id), profiles[profile_id]


def simulation_dependency_records(
    project: Project, simulation: dict[str, Any]
) -> list[dict[str, Any]]:
    records = []
    inputs = {record["path"]: record for record in project.input_records()}
    for dependency in simulation.get("dependencies", []):
        if dependency in inputs:
            records.append(inputs[dependency])
            continue
        path = (project.paths.root / dependency).resolve()
        try:
            display = path.relative_to(project.paths.root).as_posix()
        except ValueError:
            display = str(path)
        if path.is_file():
            records.append({
                "path": display,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            })
        else:
            records.append({"path": display, "missing": True})
    return records


def simulation_dependency_hash(
    project: Project,
    simulation: dict[str, Any],
    *,
    simulation_profile: str | None = None,
) -> str:
    dependency_records = simulation_dependency_records(project, simulation)
    profile_id, settings = resolve_simulation_profile(project, simulation, simulation_profile)
    payload = {
        "schema": 1,
        "runtimeVersion": RUNTIME_VERSION,
        "runtimeHash": hash_tree(
            Path(__file__).resolve().parent.parent / "blend_runtime",
            exclude_generated=False,
        ),
        "simulation": simulation["id"],
        "dependencies": dependency_records,
        "settings": simulation,
        "simulationProfile": profile_id,
        "simulationProfileSettings": settings,
    }
    return sha256_bytes(canonical_json(payload))


def write_cache_manifest(project: Project, simulation: dict[str, Any], *, blender_version: str,
                         runtime_record: dict[str, Any], status: str, duration_seconds: float,
                         simulation_profile: str | None = None) -> Path:
    root = cache_path(project, simulation, simulation_profile)
    root.mkdir(parents=True, exist_ok=True)
    outputs = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "cache-manifest.json":
            outputs.append({"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    manifest = {
        "schema": 1,
        "simulation": simulation["id"],
        "project": project.id,
        "dependencyHash": simulation_dependency_hash(
            project, simulation, simulation_profile=simulation_profile
        ),
        "runtimeVersion": RUNTIME_VERSION,
        "runtimeHash": hash_tree(
            Path(__file__).resolve().parent.parent / "blend_runtime",
            exclude_generated=False,
        ),
        "settings": simulation,
        "simulationProfile": simulation_profile,
        "sources": simulation_dependency_records(project, simulation),
        "simulationProfileSettings": project.config.get("simulationProfiles", {}).get(simulation_profile, {}),
        "blenderVersion": blender_version,
        "frameStart": simulation["frameStart"],
        "frameEnd": simulation["frameEnd"],
        "seed": simulation.get("seed"),
        "deterministic": simulation["deterministic"],
        "nondeterminism": [] if simulation["deterministic"] else project.config.get("nondeterminism", []),
        "outputs": outputs,
        "status": status,
        "timing": {"endedAt": utc_now(), "durationSeconds": duration_seconds},
    }
    errors = schema_errors(manifest, "cache-manifest-v1.json")
    if errors:
        raise BlendError(
            code="CACHE_MANIFEST_SCHEMA_INVALID",
            category=ErrorCategory.INTERNAL,
            message="Generated cache manifest failed its own schema.",
            remediation="Retain the staged cache and report the schema errors.",
            details={"errors": errors},
        )
    path = root / "cache-manifest.json"
    atomic_write_json(path, manifest)
    return path


def inspect_caches(project: Project, simulation_id: str | None = None,
                   simulation_profile: str | None = None) -> dict[str, Any]:
    records = []
    for simulation in project.config.get("simulations", []):
        if simulation_id and simulation["id"] != simulation_id:
            continue
        profile_id, _ = resolve_simulation_profile(project, simulation, simulation_profile)
        root = cache_path(project, simulation, profile_id)
        manifest_path = root / "cache-manifest.json"
        present = [path for path in sorted(root.rglob("*")) if path.is_file()] if root.is_dir() else []
        manifest_error = None
        try:
            manifest = load_json(manifest_path) if manifest_path.is_file() else None
        except (OSError, ValueError) as exc:
            manifest = None
            manifest_error = str(exc)
        expected_hash = simulation_dependency_hash(
            project, simulation, simulation_profile=profile_id
        )
        missing = [item for item in simulation.get("expectedFiles", []) if not (root / item).is_file()]
        output_failures = []
        for output in manifest.get("outputs", []) if manifest else []:
            output_path = root / output["path"]
            if not output_path.is_file():
                output_failures.append({"path": output["path"], "reason": "missing"})
            elif sha256_file(output_path) != output.get("sha256"):
                output_failures.append({"path": output["path"], "reason": "checksum"})
            elif output_path.stat().st_size != output.get("bytes"):
                output_failures.append({"path": output["path"], "reason": "size"})
        range_current = bool(
            manifest
            and manifest.get("frameStart") == simulation["frameStart"]
            and manifest.get("frameEnd") == simulation["frameEnd"]
            and manifest.get("seed") == simulation.get("seed")
            and manifest.get("deterministic") == simulation["deterministic"]
            and manifest.get("simulationProfile") == profile_id
        )
        current = bool(
            manifest
            and manifest.get("dependencyHash") == expected_hash
            and manifest.get("status") == "complete"
            and range_current
            and not missing
            and not output_failures
            and len(manifest.get("outputs", [])) > 0
        )
        records.append({
            "id": simulation["id"],
            "type": simulation["type"],
            "root": str(root),
            "exists": root.is_dir(),
            "manifest": str(manifest_path) if manifest_path.is_file() else None,
            "status": manifest.get("status") if manifest else "missing",
            "expectedDependencyHash": expected_hash,
            "manifestDependencyHash": manifest.get("dependencyHash") if manifest else None,
            "current": current,
            "missingExpectedFiles": missing,
            "manifestError": manifest_error,
            "outputFailures": output_failures,
            "rangeAndSettingsCurrent": range_current,
            "declaredFrameRange": [simulation["frameStart"], simulation["frameEnd"]],
            "manifestFrameRange": (
                [manifest.get("frameStart"), manifest.get("frameEnd")] if manifest else None
            ),
            "files": len(present),
            "bytes": sum(path.stat().st_size for path in present),
            "deterministic": simulation["deterministic"],
            "simulationProfile": profile_id,
        })
    if simulation_id and not records:
        raise BlendError(
            code="CACHE_SIMULATION_UNKNOWN",
            category=ErrorCategory.SIMULATION,
            message=f"Unknown simulation {simulation_id!r}.",
            remediation="Choose a declared simulations[].id.",
        )
    return {"schema": 1, "project": project.id, "caches": records,
            "summary": {"complete": sum(1 for item in records if item["current"]),
                        "staleOrMissing": sum(1 for item in records if not item["current"])}}


def clean_caches(project: Project, simulation_id: str | None = None) -> dict[str, Any]:
    removed = []
    for simulation in project.config.get("simulations", []):
        if simulation_id and simulation["id"] != simulation_id:
            continue
        root = cache_path(project, simulation)
        if root.is_dir():
            # The whole directory is an explicitly declared cache root, never a wildcard match.
            removed.append({"id": simulation["id"], "path": str(root),
                            "bytes": sum(path.stat().st_size for path in root.rglob("*") if path.is_file())})
            shutil.rmtree(root)
    if simulation_id and not any(item["id"] == simulation_id for item in removed) and not any(
        item["id"] == simulation_id for item in inspect_caches(project).get("caches", [])
    ):
        raise BlendError(
            code="CACHE_SIMULATION_UNKNOWN",
            category=ErrorCategory.SIMULATION,
            message=f"Unknown simulation {simulation_id!r}.",
            remediation="Choose a declared simulations[].id.",
        )
    return {"schema": 1, "project": project.id, "removed": removed, "sourceDeleted": False}
