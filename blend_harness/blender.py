"""Host-side Blender invocation and trusted-workspace enforcement."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .capabilities import blender_version, ensure_blender_compatible, find_blender
from .errors import BlendError, ErrorCategory
from .process import ProcessSupervisor
from .project import Project, write_resolved_runtime_config
from .util import atomic_write_json, load_json, sha256_file, utc_now


TRUST_FILE = ".blend-trust.json"


def trust_project(project: Project) -> Path:
    path = project.paths.root / TRUST_FILE
    atomic_write_json(path, {
        "schema": 1,
        "projectRoot": str(project.paths.root),
        "projectId": project.id,
        "trustedAt": utc_now(),
        "notice": "Project Python executes with this user's filesystem privileges. Network policy is separate.",
    })
    return path


def is_project_trusted(project: Project) -> bool:
    path = project.paths.root / TRUST_FILE
    if not path.is_file():
        return False
    try:
        value = load_json(path)
        return value.get("schema") == 1 and value.get("projectRoot") == str(project.paths.root) and value.get("projectId") == project.id
    except (OSError, ValueError, json.JSONDecodeError):
        return False


class BlenderExecutor:
    def __init__(self, project: Project, supervisor: ProcessSupervisor, *, trust: bool,
                 allow_network: bool, explicit_blender: str | None = None,
                 timeout_seconds: float | None = None) -> None:
        self.project = project
        self.supervisor = supervisor
        self.executable = find_blender(project, explicit_blender)
        self._trust = trust
        self._allow_network = allow_network
        self.timeout_seconds = timeout_seconds
        if not trust and not is_project_trusted(project):
            raise BlendError(
                code="SECURITY_PROJECT_NOT_TRUSTED",
                category=ErrorCategory.SECURITY,
                message=f"Project Python is not trusted: {project.paths.root}",
                remediation="Review scene.py and reusable libraries, then rerun with --trust or run blend trust <project>.",
            )
        if allow_network and project.config["blender"].get("offline", True):
            raise BlendError(
                code="SECURITY_NETWORK_NOT_DECLARED",
                category=ErrorCategory.SECURITY,
                message="Runtime network permission was requested but the project declares offline mode.",
                remediation="Keep the command offline, or explicitly set blender.offline: false after reviewing the script.",
            )
        self.version = blender_version(supervisor, self.executable, project.paths.logs)
        ensure_blender_compatible(project, self.version)

    @property
    def offline(self) -> bool:
        return not (self._allow_network and not self.project.config["blender"].get("offline", True))

    def run(self, operation: str, *, operation_id: str, profile: str | None = None,
            variant: str | None = None, output: str | None = None,
            jobs: list[dict[str, Any]] | None = None, frames: list[int] | None = None) -> dict[str, Any]:
        runtime_config = write_resolved_runtime_config(
            self.project,
            operation=operation,
            profile=profile,
            variant=variant,
            output=output,
            jobs=jobs,
            frames=frames,
            operation_id=operation_id,
        )
        result_path = self.project.paths.temporary / f"runtime-result-{operation_id}.json"
        result_path.unlink(missing_ok=True)
        runner = Path(__file__).resolve().parent.parent / "blend_runtime" / "runner.py"
        command = [str(self.executable), "--background"]
        blend_source = self.project.config.get("blendSource")
        if blend_source:
            source_path = Path(blend_source["path"])
            if not source_path.is_absolute():
                source_path = self.project.paths.root / source_path
            if sha256_file(source_path) != blend_source["checksum"]:
                raise BlendError(
                    code="BLEND_SOURCE_CHECKSUM_DRIFT",
                    category=ErrorCategory.ASSET,
                    message=f"Authoritative .blend source checksum drifted: {source_path}",
                    remediation="Review the GUI-authored source and pin its new checksum explicitly.",
                )
            command.append(str(source_path))
        elif self.project.config["blender"].get("factoryStartup", True):
            command.append("--factory-startup")
        command.extend(["--python-exit-code", "74", "--python", str(runner), "--",
                        "--blend-runtime-config", str(runtime_config)])
        timeout = self.timeout_seconds or self.project.config.get("resources", {}).get("timeoutSeconds")
        timeout = timeout or self.project.resolved_profile(profile, self.project.resolved_variant(variant))[1].get("timeoutSeconds")
        timeout = float(timeout) if timeout else None
        log_path = self.project.paths.logs / f"{operation_id}.log"
        environment = {**self.project.config.get("environment", {}),
                       "BLEND_OPERATION_ID": operation_id,
                       "BLEND_OFFLINE": "1" if self.offline else "0"}
        started = time.monotonic()
        try:
            completed = self.supervisor.run(
                command,
                cwd=self.project.paths.root,
                log_path=log_path,
                timeout_seconds=timeout,
                environment=environment,
                offline=self.offline,
                enforce_offline=self.offline,
            )
        finally:
            runtime_config.unlink(missing_ok=True)
        if completed.interrupted:
            raise BlendError(
                code="PROCESS_INTERRUPTED",
                category=ErrorCategory.INTERRUPTED,
                message=f"{operation} was interrupted; valid completed artifacts were retained.",
                operation=operation,
                remediation=f"Run blend resume {self.project.paths.root} when the operation reports resumeSafe.",
                details={"log": str(log_path), "durationSeconds": time.monotonic() - started},
                blender_exit_code=completed.returncode,
                retained_artifacts=[str(log_path)],
                resume_safe=operation in {"render", "bake"},
            )
        if completed.timed_out:
            raise BlendError(
                code="PROCESS_TIMEOUT",
                category=ErrorCategory.RESOURCE,
                message=f"{operation} exceeded its {timeout}-second timeout.",
                operation=operation,
                remediation="Reduce work, raise the declared timeout, or run the safe resume command.",
                details={"log": str(log_path), "logTail": completed.stdout[-4000:]},
                blender_exit_code=completed.returncode,
                retained_artifacts=[str(log_path)],
                resume_safe=operation in {"render", "bake"},
            )
        if completed.returncode != 0:
            category = _operation_category(operation)
            code = "ENTRYPOINT_PYTHON_FAILED" if completed.returncode == 74 else f"{operation.upper()}_PROCESS_FAILED"
            raise BlendError(
                code=code,
                category=ErrorCategory.ENTRYPOINT_PYTHON if completed.returncode == 74 else category,
                message=f"Blender failed during {operation} with exit code {completed.returncode}.",
                operation=operation,
                remediation=f"Inspect {log_path} near the final Python traceback and correct scene.py.",
                details={"log": str(log_path), "logTail": completed.stdout[-8000:]},
                blender_exit_code=completed.returncode,
                retained_artifacts=[str(log_path)],
                resume_safe=operation == "render",
            )
        if not result_path.is_file():
            raise BlendError(
                code="BLENDER_RUNTIME_RESULT_MISSING",
                category=_operation_category(operation),
                message=f"Blender exited successfully without the required {operation} result artifact.",
                operation=operation,
                remediation=f"Inspect {log_path}; ensure scene.py calls context.execute_requested_operation().",
                details={"log": str(log_path), "logTail": completed.stdout[-4000:]},
                retained_artifacts=[str(log_path)],
            )
        value = load_json(result_path)
        value["log"] = str(log_path)
        value["blenderVersion"] = self.version
        value["durationSeconds"] = completed.duration_seconds
        result_path.unlink(missing_ok=True)
        return value


def _operation_category(operation: str) -> ErrorCategory:
    return {
        "build": ErrorCategory.SCENE_BUILD,
        "preview": ErrorCategory.RENDER_ENGINE,
        "inspect": ErrorCategory.INSPECTION,
        "validate": ErrorCategory.VALIDATION,
        "render": ErrorCategory.RENDER_FRAME,
        "export": ErrorCategory.EXPORT,
        "bake": ErrorCategory.SIMULATION,
    }.get(operation, ErrorCategory.INTERNAL)
