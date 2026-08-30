"""Versioned operation manifests and authoritative command results."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import RUNTIME_VERSION, __version__
from .errors import BlendError, ErrorCategory
from .project import Project, schema_errors
from .util import atomic_write_json, hash_tree, host_facts, load_json, safe_id, sha256_file, utc_now


def new_operation_id(operation: str, supplied: str | None = None) -> str:
    if supplied:
        try:
            return safe_id(supplied, label="operation id")
        except ValueError as exc:
            raise BlendError(
                code="OPERATION_ID_INVALID",
                category=ErrorCategory.CONFIGURATION,
                message=str(exc),
                remediation="Use 1-128 letters, numbers, dots, underscores, or hyphens.",
            ) from exc
    return f"{operation}-{uuid.uuid4().hex[:16]}"

def child_operation_id(parent: str, suffix: str) -> str:
    candidate = f"{parent}-{suffix}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
    return f"{parent[:114]}-{digest}"
def ensure_operation_id_available(project: Project, operation: str, operation_id: str) -> None:
    path = project.paths.artifacts / f"{operation_id}.json"
    if not path.exists():
        return
    previous = load_json(path)
    raise BlendError(
        code="OPERATION_ID_ALREADY_USED",
        category=ErrorCategory.CONFIGURATION,
        message=f"Operation identifier {operation_id!r} already has a retained manifest.",
        operation=operation,
        remediation="Reuse the retained result or provide a new --operation-id; Blend will not duplicate or overwrite work.",
        details={
            "manifest": str(path),
            "status": previous.get("status"),
            "operation": previous.get("operation"),
        },
        retained_artifacts=[str(path)],
        resume_safe=previous.get("operation") in {"render", "bake"},
    )




def artifact_record(path: Path, *, root: Path | None = None, kind: str | None = None) -> dict[str, Any]:
    display = str(path.resolve())
    if root:
        try:
            display = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    value: dict[str, Any] = {"path": display, "exists": path.is_file(), "kind": kind}
    if path.is_file():
        value.update({"sha256": sha256_file(path), "bytes": path.stat().st_size})
    return {key: item for key, item in value.items() if item is not None}


@dataclass(slots=True)
class Manifest:
    project: Project
    operation: str
    operation_id: str
    profile: str | None = None
    variant: str | None = None
    blender: dict[str, Any] = field(default_factory=dict)
    expected_frames: list[int] = field(default_factory=list)
    path: Path = field(init=False)
    value: dict[str, Any] = field(init=False)
    _started_monotonic: float = field(init=False)

    def __post_init__(self) -> None:
        self.project.paths.create_generated()
        self.path = self.project.paths.artifacts / f"{self.operation_id}.json"
        ensure_operation_id_available(self.project, self.operation, self.operation_id)
        self._started_monotonic = time.monotonic()
        variant_value = self.project.resolved_variant(self.variant)
        profile_name, profile_value = self.project.resolved_profile(self.profile, variant_value)
        self.value = {
            "schema": 1,
            "kind": self.operation,
            "blendVersion": __version__,
            "runtimeVersion": RUNTIME_VERSION,
            "operation": self.operation,
            "operationId": self.operation_id,
            "project": {
                "id": self.project.id,
                "root": str(self.project.paths.root),
                "sourceRevision": os.environ.get("BLEND_SOURCE_REVISION"),
            },
            "host": host_facts(),
            "blender": self.blender,
            "inputs": {
                "dependencyHash": self.project.dependency_hash(profile=profile_name, variant=self.variant,
                                                                operation=self.operation),
                "runtimeHash": hash_tree(Path(__file__).resolve().parent.parent / "blend_runtime",
                                         exclude_generated=False),
                "files": self.project.input_records(),
                "assets": [asset.as_manifest(self.project.paths.root) for asset in self.project.assets],
                "libraries": [library.as_manifest() for library in self.project.libraries],
            },
            "resolved": {
                "profileName": profile_name,
                "profile": profile_value,
                "variantName": self.variant,
                "variant": variant_value,
                "roots": {
                    "project": str(self.project.paths.root),
                    "working": str(self.project.paths.working),
                    "cache": str(self.project.paths.cache),
                    "artifacts": str(self.project.paths.artifacts),
                    "outputs": str(self.project.paths.outputs),
                    "temporary": str(self.project.paths.temporary),
                },
                "seed": self.project.config["project"]["seed"],
                "frameStart": self.project.config["project"]["frameStart"],
                "frameEnd": self.project.config["project"]["frameEnd"],
                "frameRate": self.project.config["project"]["frameRate"],
                "colorManagement": self.project.config["project"]["colorManagement"],
            },
            "timing": {"startedAt": utc_now()},
            "status": "running",
            "exitStatus": None,
            "expectedFrames": list(self.expected_frames),
            "completedFrames": [],
            "frames": [],
            "outputs": [],
            "validation": {},
            "nondeterminism": self.project.config.get("nondeterminism", []),
            "warnings": [],
            "overrides": [],
            "parentManifests": [],
        }
        self.write()

    @property
    def dependency_hash(self) -> str:
        return str(self.value["inputs"]["dependencyHash"])

    def write(self) -> None:
        atomic_write_json(self.path, self.value)

    def add_output(self, path: Path, *, kind: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        record = artifact_record(path, root=self.project.paths.root, kind=kind)
        if metadata:
            record.update(metadata)
        self.value["outputs"].append(record)
        self.write()
    def add_outputs(
        self,
        outputs: Iterable[tuple[Path, str | None, dict[str, Any] | None]],
    ) -> None:
        records = []
        for path, kind, metadata in outputs:
            record = artifact_record(path, root=self.project.paths.root, kind=kind)
            if metadata:
                record.update(metadata)
            records.append(record)
        self.value["outputs"].extend(records)
        self.write()

    def add_frame(self, frame: int, path: Path, metadata: dict[str, Any]) -> None:
        record = artifact_record(path, root=self.project.paths.root, kind="frame")
        record.update({"frame": frame, "dependencyHash": self.dependency_hash, **metadata})
        frames = [item for item in self.value["frames"] if item.get("frame") != frame]
        frames.append(record)
        self.value["frames"] = sorted(frames, key=lambda item: item["frame"])
        self.value["completedFrames"] = [item["frame"] for item in self.value["frames"] if item.get("exists")]
        self.write()

    def add_warning(self, warning: str | dict[str, Any]) -> None:
        self.value["warnings"].append(warning)
        self.write()

    def parent(self, path: Path) -> None:
        self.value["parentManifests"].append(artifact_record(path, root=self.project.paths.root, kind="manifest"))
        self.write()

    def set_validation(self, report: dict[str, Any], path: Path | None = None) -> None:
        summary = dict(report.get("summary", {}))
        if path:
            summary["artifact"] = artifact_record(path, root=self.project.paths.root, kind="validation")
        self.value["validation"] = summary
        self.write()

    def succeed(self) -> None:
        self._finish("succeeded", 0)

    def partial(self) -> None:
        self._finish("partial", 0)

    def fail(self, error: BlendError) -> None:
        self.value["error"] = error.as_dict()
        self._finish("interrupted" if error.category.value == "interrupted" else "failed", error.exit_code)

    def _finish(self, status: str, exit_status: int) -> None:
        self.value["status"] = status
        self.value["exitStatus"] = exit_status
        self.value["timing"].update({
            "endedAt": utc_now(),
            "durationSeconds": round(time.monotonic() - self._started_monotonic, 6),
        })
        errors = schema_errors(self.value, "manifest-v1.json")
        if errors:
            # The invalid value remains inspectable, but callers receive a stable internal failure.
            self.value["status"] = "failed"
            self.value["error"] = {
                "code": "MANIFEST_SCHEMA_INVALID",
                "category": "internal",
                "message": "Generated manifest failed its own schema.",
                "details": {"errors": errors},
            }
            self.value["exitStatus"] = 70
        self.write()


@dataclass(slots=True)
class CommandResult:
    operation: str
    operation_id: str
    project: Path | None
    started_at: str = field(default_factory=utc_now)
    _started_monotonic: float = field(default_factory=time.monotonic)
    status: str = "succeeded"
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    warnings: list[str | dict[str, Any]] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def fail(self, error: BlendError) -> None:
        self.status = "interrupted" if error.category.value == "interrupted" else "failed"
        self.error = error.as_dict()
        self.summary = error.message
        self.artifacts.extend(item for item in error.retained_artifacts if item not in self.artifacts)
        if error.remediation:
            self.next_actions.append(error.remediation)

    def as_dict(self) -> dict[str, Any]:
        ended = utc_now()
        value: dict[str, Any] = {
            "schema": 1,
            "operation": self.operation,
            "operationId": self.operation_id,
            "project": str(self.project) if self.project else None,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": ended,
            "durationSeconds": round(time.monotonic() - self._started_monotonic, 6),
            "summary": self.summary,
            "data": self.data,
            "artifacts": list(dict.fromkeys(self.artifacts)),
            "warnings": self.warnings,
            "nextActions": self.next_actions,
            "progress": self.progress,
            "error": self.error,
        }
        errors = schema_errors(value, "result-v1.json")
        if errors:
            value["status"] = "failed"
            value["error"] = {
                "code": "RESULT_SCHEMA_INVALID",
                "category": "internal",
                "message": "Generated command result failed its own schema.",
                "details": {"errors": errors},
            }
        return value
