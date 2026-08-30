"""Pinned filesystem project libraries and explicit upgrade comparison/update."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import BlendError, ErrorCategory
from .project import Project, schema_errors
from .util import atomic_write_json, atomic_write_yaml, hash_tree, load_json, load_yaml, sha256_file


def _records(root: Path) -> dict[str, str]:
    ignored = {".git", "__pycache__", ".pytest_cache"}
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not any(part in ignored for part in path.relative_to(root).parts)
    }


def compare_library(project: Project, library_id: str, candidate_path: Path) -> dict[str, Any]:
    current = next((item for item in project.libraries if item.id == library_id), None)
    if current is None:
        raise BlendError(
            code="LIBRARY_UNKNOWN",
            category=ErrorCategory.ASSET,
            message=f"Unknown project library {library_id!r}.",
            remediation="Choose a declared libraries[].id.",
        )
    candidate_path = candidate_path.expanduser().resolve()
    manifest_path = candidate_path / "blend-library.json"
    if not manifest_path.is_file():
        raise BlendError(
            code="LIBRARY_MANIFEST_MISSING",
            category=ErrorCategory.ASSET,
            message=f"Candidate library manifest is missing: {manifest_path}",
            remediation="Provide a versioned filesystem library with blend-library.json.",
        )
    candidate_manifest = load_json(manifest_path)
    errors = schema_errors(candidate_manifest, "library-v1.json")
    if errors:
        raise BlendError(
            code="LIBRARY_SCHEMA_INVALID",
            category=ErrorCategory.ASSET,
            message="Candidate library manifest is invalid.",
            remediation="Correct it against library-v1.json.",
            details={"errors": errors},
        )
    if candidate_manifest["id"] != library_id:
        raise BlendError(
            code="LIBRARY_ID_MISMATCH",
            category=ErrorCategory.ASSET,
            message=f"Candidate library id {candidate_manifest['id']!r} does not match {library_id!r}.",
            remediation="Choose an upgrade of the same library identity.",
        )
    for asset in candidate_manifest["assets"]:
        asset_path = (candidate_path / asset["path"]).resolve()
        try:
            asset_path.relative_to(candidate_path)
        except ValueError as exc:
            raise BlendError(
                code="LIBRARY_ASSET_OUTSIDE_ROOT",
                category=ErrorCategory.ASSET,
                message=f"Candidate library asset escapes its package: {asset_path}",
                remediation="Package every transitive asset under the candidate library root.",
            ) from exc
        if not asset_path.is_file() or asset_path.stat().st_size == 0:
            raise BlendError(
                code="LIBRARY_ASSET_MISSING",
                category=ErrorCategory.ASSET,
                message=f"Candidate library asset is missing or empty: {asset_path}",
                remediation="Restore the complete candidate package before comparison.",
            )
        if sha256_file(asset_path) != asset["checksum"]:
            raise BlendError(
                code="LIBRARY_ASSET_CHECKSUM_DRIFT",
                category=ErrorCategory.ASSET,
                message=f"Candidate library asset checksum drifted: {asset_path}",
                remediation="Restore or explicitly repin the candidate package before comparison.",
            )
    for python_path in candidate_manifest.get("contents", {}).get("python", []):
        source = (candidate_path / python_path).resolve()
        if not source.is_file():
            raise BlendError(
                code="LIBRARY_CONTENT_MISSING",
                category=ErrorCategory.ASSET,
                message=f"Candidate library Python content is missing: {source}",
                remediation="Restore the declared reusable module before comparison.",
            )
    before = _records(current.path)
    after = _records(candidate_path)
    changed = sorted(path for path in set(before) & set(after) if before[path] != after[path])
    report = {
        "schema": 1,
        "library": library_id,
        "current": {"version": current.version, "path": str(current.path), "checksum": current.actual_checksum,
                    "manifest": current.manifest},
        "candidate": {"version": candidate_manifest["version"], "path": str(candidate_path),
                      "checksum": hash_tree(candidate_path, exclude_generated=False), "manifest": candidate_manifest},
        "changes": {
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "changed": changed,
            "unchanged": sorted(path for path in set(before) & set(after) if before[path] == after[path]),
            "assetDeclarations": {
                "before": current.manifest.get("assets", []),
                "after": candidate_manifest.get("assets", []),
            },
        },
        "applied": False,
    }
    destination = project.paths.artifacts / f"library-{library_id}-upgrade-comparison.json"
    atomic_write_json(destination, report)
    report["report"] = str(destination)
    return report


def update_library(project: Project, library_id: str, candidate_path: Path) -> dict[str, Any]:
    comparison = compare_library(project, library_id, candidate_path)
    config = load_yaml(project.paths.config)
    declaration = next((item for item in config.get("libraries", []) if item["id"] == library_id), None)
    if declaration is None:
        raise BlendError(
            code="LIBRARY_UNKNOWN",
            category=ErrorCategory.ASSET,
            message=f"Unknown project library {library_id!r}.",
            remediation="Choose a declared libraries[].id.",
        )
    declaration["version"] = comparison["candidate"]["version"]
    declaration["path"] = str(Path(candidate_path).expanduser().resolve())
    declaration["checksum"] = comparison["candidate"]["checksum"]
    atomic_write_yaml(project.paths.config, config)
    comparison["applied"] = True
    comparison["source"] = str(project.paths.config)
    destination = project.paths.artifacts / f"library-{library_id}-upgrade-applied.json"
    atomic_write_json(destination, comparison)
    comparison["report"] = str(destination)
    return comparison
