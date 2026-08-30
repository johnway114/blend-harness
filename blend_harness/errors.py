"""Stable failure categories and command-result errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    BLENDER_DEPENDENCY = "blender_dependency"
    ASSET = "asset"
    ENTRYPOINT_PYTHON = "entrypoint_python"
    SCENE_BUILD = "scene_build"
    INSPECTION = "inspection"
    VALIDATION = "validation"
    RENDER_ENGINE = "render_engine"
    RENDER_FRAME = "render_frame"
    SIMULATION = "simulation"
    ENCODING = "encoding"
    EXPORT = "export"
    RESOURCE = "resource"
    INTERRUPTED = "interrupted"
    SECURITY = "security"
    COMPARISON = "comparison"
    REVIEW = "review"
    INTERNAL = "internal"


_EXIT_CODES = {
    ErrorCategory.CONFIGURATION: 2,
    ErrorCategory.BLENDER_DEPENDENCY: 3,
    ErrorCategory.ASSET: 4,
    ErrorCategory.ENTRYPOINT_PYTHON: 5,
    ErrorCategory.SCENE_BUILD: 6,
    ErrorCategory.INSPECTION: 7,
    ErrorCategory.VALIDATION: 8,
    ErrorCategory.RENDER_ENGINE: 9,
    ErrorCategory.RENDER_FRAME: 10,
    ErrorCategory.SIMULATION: 11,
    ErrorCategory.ENCODING: 12,
    ErrorCategory.EXPORT: 13,
    ErrorCategory.RESOURCE: 14,
    ErrorCategory.INTERRUPTED: 130,
    ErrorCategory.SECURITY: 15,
    ErrorCategory.COMPARISON: 16,
    ErrorCategory.REVIEW: 17,
    ErrorCategory.INTERNAL: 70,
}


@dataclass(slots=True)
class BlendError(Exception):
    code: str
    category: ErrorCategory
    message: str
    operation: str | None = None
    remediation: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    blender_exit_code: int | None = None
    retained_artifacts: list[str] = field(default_factory=list)
    resume_safe: bool = False

    def __str__(self) -> str:
        return self.message

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self.category]

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "operation": self.operation,
            "remediation": self.remediation,
            "details": self.details,
            "blenderExitCode": self.blender_exit_code,
            "retainedArtifacts": self.retained_artifacts,
            "resumeSafe": self.resume_safe,
        }
        return {key: item for key, item in value.items() if item not in (None, [], {})}


def require(condition: bool, *, code: str, category: ErrorCategory, message: str,
            operation: str | None = None, remediation: str | None = None,
            details: dict[str, Any] | None = None) -> None:
    if not condition:
        raise BlendError(
            code=code,
            category=category,
            message=message,
            operation=operation,
            remediation=remediation,
            details=details or {},
        )
