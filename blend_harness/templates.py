"""Versioned template initialization and non-destructive upgrade comparison."""

from __future__ import annotations

import json
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import BlendError, ErrorCategory
from .util import atomic_write_json, atomic_write_yaml, load_yaml, safe_id, sha256_file


TEMPLATE_IDS = ("brand-ident", "product-turntable", "procedural-explainer", "empty")
_GENERATED = {"build", "previews", "renders", "output", "__pycache__"}


def template_root(template: str) -> Path:
    if template not in TEMPLATE_IDS:
        raise BlendError(
            code="TEMPLATE_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unknown template {template!r}.",
            remediation=f"Choose one of: {', '.join(TEMPLATE_IDS)}.",
        )
    return Path(str(files("blend_harness").joinpath("templates", template)))


def initialize_project(template: str, destination: Path) -> dict[str, Any]:
    source = template_root(template)
    destination = destination.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise BlendError(
            code="INIT_DESTINATION_NOT_EMPTY",
            category=ErrorCategory.CONFIGURATION,
            message=f"Project destination is not empty: {destination}",
            remediation="Choose a new or empty directory; Blend never overwrites existing source.",
        )
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in _GENERATED:
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                dirs_exist_ok=False,
                ignore=shutil.ignore_patterns(*_GENERATED, ".pytest_cache", ".DS_Store"),
            )
        else:
            shutil.copy2(item, target)
    config_path = destination / "blend.yaml"
    config = load_yaml(config_path)
    candidate_id = destination.name.lower().replace(" ", "-")
    try:
        safe_id(candidate_id, label="project id")
        config["id"] = candidate_id
    except ValueError:
        pass
    atomic_write_yaml(config_path, config)
    for generated in ("build", "previews", "renders", "output"):
        (destination / generated).mkdir(parents=True, exist_ok=True)
    return {
        "template": template,
        "templateVersion": config.get("template", {}).get("version"),
        "project": str(destination),
        "sourceFiles": sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*")
                              if path.is_file() and not any(part in _GENERATED for part in path.relative_to(destination).parts)),
    }


def _tree_records(root: Path) -> dict[str, dict[str, Any]]:
    records = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in _GENERATED or part == ".git" for part in relative.parts):
            continue
        records[relative.as_posix()] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    return records


def compare_template_upgrade(project_root: Path) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    config = load_yaml(project_root / "blend.yaml")
    declaration = config.get("template")
    if not declaration or declaration.get("id") not in TEMPLATE_IDS:
        raise BlendError(
            code="TEMPLATE_ORIGIN_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message="Project does not declare a supported built-in template origin.",
            remediation="Record template.id and template.version before comparing upgrades.",
        )
    current = template_root(declaration["id"])
    project_records = _tree_records(project_root)
    template_records = _tree_records(current)
    added = sorted(set(template_records) - set(project_records))
    removed = sorted(set(project_records) - set(template_records))
    changed = sorted(path for path in set(project_records) & set(template_records)
                     if project_records[path]["sha256"] != template_records[path]["sha256"])
    unchanged = sorted(path for path in set(project_records) & set(template_records)
                       if project_records[path]["sha256"] == template_records[path]["sha256"])
    report = {
        "schema": 1,
        "template": declaration["id"],
        "projectTemplateVersion": declaration["version"],
        "availableTemplateVersion": load_yaml(current / "blend.yaml").get("template", {}).get("version"),
        "addedByTemplate": added,
        "projectOnly": removed,
        "different": changed,
        "unchanged": unchanged,
        "creativeSettingsChanged": False,
        "applied": False,
        "notice": "Comparison only. Blend does not overwrite project source or creative settings.",
    }
    destination = project_root / "build" / "template-upgrade-comparison.json"
    atomic_write_json(destination, report)
    report["report"] = str(destination)
    return report
