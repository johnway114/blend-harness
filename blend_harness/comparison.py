"""Image, scene, source, asset, camera, and manifest comparison evidence."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops, ImageEnhance, ImageStat, UnidentifiedImageError

from .errors import BlendError, ErrorCategory
from .util import atomic_write_json, load_json, sha256_file, utc_now


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
_GENERATED_PARTS = {"build", "previews", "renders", "output", "__pycache__", ".git"}


def _images(path: Path) -> dict[str, Path]:
    if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
        return {path.name: path}
    if path.is_file() and path.suffix.lower() == ".json":
        try:
            manifest = load_json(path)
        except (OSError, json.JSONDecodeError):
            return {}
        project_root = Path(manifest.get("project", {}).get("root", path.parent))
        records: dict[str, Path] = {}
        for output in manifest.get("outputs", []):
            candidate = Path(output.get("path", ""))
            if not candidate.is_absolute():
                candidate = project_root / candidate
            if candidate.is_file() and candidate.suffix.lower() in _IMAGE_SUFFIXES:
                records[f"{output.get('kind', 'image')}-{candidate.name}"] = candidate
        return records
    if not path.is_dir():
        return {}
    return {item.relative_to(path).as_posix(): item for item in sorted(path.rglob("*"))
            if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES}


def _inspection(path: Path) -> dict[str, Any] | None:
    candidates = [path] if path.is_file() and path.suffix == ".json" else [
        path / "inspection.json",
        path / "build" / "inspection.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                value = load_json(candidate)
                if "objects" in value and "scene" in value:
                    return value
            except (OSError, json.JSONDecodeError):
                pass
    return None


def _manifests(path: Path) -> list[dict[str, Any]]:
    if path.is_file() and path.suffix.lower() == ".json":
        try:
            value = load_json(path)
        except (OSError, json.JSONDecodeError):
            value = {}
        if (
            value.get("schema") == 1
            and value.get("operation")
            and value.get("blendVersion")
            and value.get("inputs")
        ):
            return [value]
    roots = [path / "build" / "manifests", path / "manifests"] if path.is_dir() else [path.parent]
    values: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for item in sorted(root.glob("*.json")):
            if item in seen:
                continue
            seen.add(item)
            try:
                value = load_json(item)
            except (OSError, json.JSONDecodeError):
                continue
            if (
                value.get("schema") == 1
                and value.get("operation")
                and value.get("blendVersion")
                and value.get("inputs")
            ):
                values.append(value)
    values.sort(key=lambda value: value.get("timing", {}).get("startedAt", ""))
    return values

def _source_records(path: Path) -> dict[str, dict[str, Any]]:
    if not (path.is_dir() and (path / "blend.yaml").is_file()):
        return {}
    records = {}
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if not item.is_file() or any(part in _GENERATED_PARTS for part in relative.parts):
            continue
        records[relative.as_posix()] = {
            "sha256": sha256_file(item),
            "bytes": item.stat().st_size,
        }
    return records



def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            result.update(_flatten(value[key], f"{prefix}/{key}"))
    elif isinstance(value, list):
        if all(isinstance(item, dict) and "name" in item for item in value):
            for item in value:
                result.update(_flatten(item, f"{prefix}/{item['name']}"))
        else:
            for index, item in enumerate(value):
                result.update(_flatten(item, f"{prefix}/{index}"))
    else:
        result[prefix or "/"] = value
    return result


def structural_difference(left: dict[str, Any] | None, right: dict[str, Any] | None) -> list[dict[str, Any]]:
    left_flat = _flatten(left or {})
    right_flat = _flatten(right or {})
    differences = []
    for path in sorted(set(left_flat) | set(right_flat)):
        before = left_flat.get(path)
        after = right_flat.get(path)
        if before != after:
            differences.append({"path": path, "before": before, "after": after,
                                "kind": "added" if path not in left_flat else "removed" if path not in right_flat else "changed"})
    return differences


def _compare_image(left_path: Path, right_path: Path, destination: Path) -> dict[str, Any]:
    try:
        left = Image.open(left_path).convert("RGB")
        right = Image.open(right_path).convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise BlendError(
            code="COMPARE_IMAGE_UNREADABLE",
            category=ErrorCategory.COMPARISON,
            message=f"Comparison image cannot be decoded: {exc}",
            remediation="Regenerate or replace the invalid comparison input.",
        ) from exc
    canvas_size = (max(left.width, right.width), max(left.height, right.height))
    left_canvas = Image.new("RGB", canvas_size, "black")
    right_canvas = Image.new("RGB", canvas_size, "black")
    left_canvas.paste(left, (0, 0))
    right_canvas.paste(right, (0, 0))
    difference = ImageChops.difference(left_canvas, right_canvas)
    stat = ImageStat.Stat(difference)
    mean = sum(stat.mean) / len(stat.mean)
    extrema = difference.getextrema()
    maximum = max(channel[1] for channel in extrema)
    histogram = difference.convert("L").histogram()
    changed_pixels = sum(histogram[1:])
    total_pixels = canvas_size[0] * canvas_size[1]
    heatmap = ImageEnhance.Contrast(difference).enhance(3.0)
    heatmap = ImageEnhance.Brightness(heatmap).enhance(2.0)
    side_by_side = Image.new("RGB", (canvas_size[0] * 2, canvas_size[1]), "#11110f")
    side_by_side.paste(left_canvas, (0, 0))
    side_by_side.paste(right_canvas, (canvas_size[0], 0))
    destination.mkdir(parents=True, exist_ok=True)
    stem = left_path.stem
    side_path = destination / f"{stem}-side-by-side.png"
    heatmap_path = destination / f"{stem}-heatmap.png"
    side_by_side.save(side_path, format="PNG", optimize=True)
    heatmap.save(heatmap_path, format="PNG", optimize=True)
    left.close()
    right.close()
    return {
        "left": str(left_path),
        "right": str(right_path),
        "leftSha256": sha256_file(left_path),
        "rightSha256": sha256_file(right_path),
        "dimensions": list(canvas_size),
        "metrics": {
            "meanAbsoluteChannelDifference": round(mean, 8),
            "maximumChannelDifference": maximum,
            "changedPixelFraction": round(changed_pixels / total_pixels if total_pixels else 0.0, 8),
        },
        "sideBySide": str(side_path),
        "heatmap": str(heatmap_path),
        "interpretation": "Pixel difference is evidence of change, not evidence of regression.",
    }


def _comparison_sheet(records: list[dict[str, Any]], destination: Path) -> None:
    images = [Image.open(record["sideBySide"]).convert("RGB") for record in records]
    if not images:
        return
    width = max(image.width for image in images)
    height = max(image.height for image in images)
    columns = min(2, len(images))
    rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (width * columns, height * rows), "#11110f")
    for index, image in enumerate(images):
        sheet.paste(image, ((index % columns) * width, (index // columns) * height))
        image.close()
    sheet.save(destination, format="PNG", optimize=True)


def compare_inputs(
    left: Path,
    right: Path,
    artifact_root: Path,
    *,
    operation_id: str,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    left = left.expanduser().resolve()
    right = right.expanduser().resolve()
    if not left.exists() or not right.exists():
        raise BlendError(
            code="COMPARE_INPUT_MISSING",
            category=ErrorCategory.COMPARISON,
            message="One or both comparison inputs do not exist.",
            remediation="Pass existing preview, render, manifest, or project paths.",
            details={"left": str(left), "right": str(right)},
        )
    artifact_root.mkdir(parents=True, exist_ok=True)
    left_images = _images(left)
    right_images = _images(right)
    common = sorted(set(left_images) & set(right_images))
    if not common and len(left_images) == len(right_images) == 1:
        common = [next(iter(left_images))]
        right_images[common[0]] = next(iter(right_images.values()))
    image_records = []
    for name in common:
        if cancelled and cancelled():
            raise BlendError(
                code="PROCESS_INTERRUPTED",
                category=ErrorCategory.INTERRUPTED,
                message="Comparison was interrupted; incomplete staging artifacts were discarded.",
                remediation="Retry with a new operation identifier.",
            )
        image_records.append(
            _compare_image(left_images[name], right_images[name], artifact_root / "images")
        )
    sheet_path = artifact_root / "side-by-side-contact-sheet.png"
    _comparison_sheet(image_records, sheet_path)
    left_inspection = _inspection(left)
    right_inspection = _inspection(right)
    scene_changes = structural_difference(left_inspection, right_inspection)
    left_manifests = _manifests(left)
    right_manifests = _manifests(right)
    manifest_changes = structural_difference(
        left_manifests[-1] if left_manifests else None,
        right_manifests[-1] if right_manifests else None,
    )
    source_changes = structural_difference(_source_records(left), _source_records(right))
    report = {
        "schema": 1,
        "operationId": operation_id,
        "createdAt": utc_now(),
        "left": str(left),
        "right": str(right),
        "images": image_records,
        "unmatchedImages": {
            "leftOnly": [
                {"path": name, "sha256": sha256_file(left_images[name])}
                for name in sorted(set(left_images) - set(right_images))
            ],
            "rightOnly": [
                {"path": name, "sha256": sha256_file(right_images[name])}
                for name in sorted(set(right_images) - set(left_images))
            ],
        },
        "contactSheet": str(sheet_path) if sheet_path.is_file() else None,
        "structuralChanges": scene_changes,
        "manifestChanges": manifest_changes,
        "cameraAndFramingChanges": [item for item in scene_changes if "/cameras/" in item["path"] or "/framing/" in item["path"]],
        "renderSettingChanges": [item for item in scene_changes if "/render/" in item["path"] or "/colorManagement/" in item["path"]],
        "sourceAssetAndBlenderHashChanges": [item for item in manifest_changes if any(token in item["path"] for token in
                                                ("/inputs/", "/blender/", "/project/sourceRevision"))],
        "projectSourceChanges": source_changes,
        "dependencyChanges": [
            item for item in manifest_changes
            if any(token in item["path"] for token in ("/dependencies/", "/inputs/files/", "/inputs/libraries/"))
        ],
        "nondeterminismChanges": [
            item for item in manifest_changes if "/nondeterminism/" in item["path"]
        ],
        "conclusion": "Change evidence only; no aesthetic or regression verdict is inferred.",
    }
    if cancelled and cancelled():
        raise BlendError(
            code="PROCESS_INTERRUPTED",
            category=ErrorCategory.INTERRUPTED,
            message="Comparison was interrupted; incomplete staging artifacts were discarded.",
            remediation="Retry with a new operation identifier.",
        )
    report["artifacts"] = [
        {
            "path": path.relative_to(artifact_root).as_posix(),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(artifact_root.rglob("*"))
        if path.is_file()
    ]
    report_path = artifact_root / "comparison.json"
    atomic_write_json(report_path, report)
    report["report"] = str(report_path)
    return report
