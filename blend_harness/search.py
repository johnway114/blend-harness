"""Declared bounded parameter expansion, mechanical scoring, ranking, and promotion."""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from .errors import BlendError, ErrorCategory
from .project import Project, schema_errors
from .util import atomic_write_json, atomic_write_yaml, load_json, load_yaml, safe_id, utc_now


def expand_search(project: Project, search_id: str) -> dict[str, Any]:
    searches = project.config.get("searches", {})
    if search_id not in searches:
        raise BlendError(
            code="SEARCH_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unknown parameter search {search_id!r}.",
            remediation=f"Choose one of: {', '.join(sorted(searches)) or '(none)' }.",
        )
    declaration = searches[search_id]
    project.resolved_variant(declaration["baseVariant"])
    names = sorted(declaration["parameters"])
    value_sets = [list(declaration["parameters"][name]) for name in names]
    parameter_space_size = math.prod(len(values) for values in value_sets)
    budget = declaration["budget"]
    sampled = parameter_space_size > budget
    if sampled:
        generator = random.Random(project.config["project"]["seed"])
        indexes = sorted(generator.sample(range(parameter_space_size), budget))
    else:
        indexes = range(parameter_space_size)
    combinations = []
    for flat_index in indexes:
        cursor = flat_index
        selected: dict[str, Any] = {}
        for name, values in reversed(list(zip(names, value_sets, strict=True))):
            cursor, offset = divmod(cursor, len(values))
            selected[name] = values[offset]
        combinations.append({name: selected[name] for name in names})
    candidates = []
    for parameters in combinations:
        digest = hashlib.sha256(repr(sorted(parameters.items())).encode("utf-8")).hexdigest()[:10]
        candidates.append({
            "id": f"{search_id}-{digest}",
            "baseVariant": declaration["baseVariant"],
            "parameters": parameters,
            "variant": {
                "extends": declaration["baseVariant"],
                "parameters": parameters,
            },
        })
    return {"schema": 1, "project": project.id, "search": search_id, "declaration": declaration,
            "parameterSpaceSize": parameter_space_size,
            "sampled": sampled, "candidates": candidates}


def measure_candidate(candidate: dict[str, Any], *, image: Path | None,
                      inspection: dict[str, Any] | None, render_seconds: float | None) -> dict[str, float]:
    metrics: dict[str, float] = {}
    if inspection:
        framing = inspection.get("framing", [])
        metrics["coverage"] = float(framing[0].get("coverage", 0)) if framing else 0.0
        metrics["triangleCount"] = float(inspection.get("statistics", {}).get("triangles", 0))
        metrics["objectCount"] = float(inspection.get("statistics", {}).get("objects", 0))
    if render_seconds is not None:
        metrics["renderSeconds"] = float(render_seconds)
    if image and image.is_file():
        with Image.open(image) as opened:
            rgba = opened.convert("RGBA")
            luminance = ImageStat.Stat(rgba.convert("L")).mean[0] / 255
            histogram = rgba.getchannel("A").histogram()
            alpha = 1.0 - (histogram[0] / max(1, rgba.width * rgba.height))
        metrics["luminance"] = float(luminance)
        metrics["alphaCoverage"] = float(alpha)
    return metrics


def rank_candidates(search: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    scores = search["declaration"]["scores"]
    by_id = {item["candidateId"]: item for item in evidence}
    ranges: dict[str, tuple[float, float]] = {}
    for score in scores:
        values = [float(item.get("metrics", {}).get(score["metric"], 0)) for item in evidence]
        ranges[score["metric"]] = (min(values, default=0), max(values, default=0))
    ranked = []
    for candidate in search["candidates"]:
        item = by_id.get(candidate["id"], {"metrics": {}, "artifacts": []})
        components = []
        total = 0.0
        for score in scores:
            metric = score["metric"]
            raw = float(item.get("metrics", {}).get(metric, 0))
            minimum, maximum = ranges[metric]
            normalized = (raw - minimum) / (maximum - minimum) if maximum > minimum else 0.5
            if score["direction"] == "minimize":
                component = 1 - normalized
            elif score["direction"] == "maximize":
                component = normalized
            else:
                target = float(score.get("target", 0))
                span = max(abs(maximum - target), abs(minimum - target), 1e-9)
                component = max(0.0, 1 - abs(raw - target) / span)
            weight = float(score.get("weight", 1))
            total += component * weight
            components.append({"id": score["id"], "metric": metric, "raw": raw,
                               "normalized": round(component, 8), "weight": weight})
        ranked.append({**candidate, "metrics": item.get("metrics", {}), "artifacts": item.get("artifacts", []),
                       "score": round(total, 8), "scoreComponents": components})
    ranked.sort(key=lambda item: (-item["score"], item["id"]))
    for index, item in enumerate(ranked, 1):
        item["rank"] = index
    return {
        "schema": 1,
        "project": search["project"],
        "search": search["search"],
        "createdAt": utc_now(),
        "parameterSpaceSize": search["parameterSpaceSize"],
        "evaluatedCandidates": len(ranked),
        "budget": search["declaration"]["budget"],
        "mechanicalScoresOnly": True,
        "ranking": ranked,
        "notice": "Ranking covers only declared measurable criteria and does not prove aesthetic quality.",
    }


def save_search_report(
    project: Project,
    search_id: str,
    report: dict[str, Any],
    operation_id: str,
) -> Path:
    path = project.paths.artifacts / f"search-{search_id}-{operation_id}.json"
    atomic_write_json(path, report)
    return path


def promote_candidate(project: Project, *, search_report: Path, candidate_id: str,
                      variant_name: str) -> dict[str, Any]:
    try:
        safe_id(variant_name, label="promoted variant")
    except ValueError as exc:
        raise BlendError(
            code="SEARCH_PROMOTION_NAME_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=str(exc),
            remediation="Use letters, numbers, dots, underscores, or hyphens.",
        ) from exc
    if not search_report.is_file():
        raise BlendError(
            code="SEARCH_REPORT_MISSING",
            category=ErrorCategory.CONFIGURATION,
            message=f"Search report does not exist: {search_report}",
            remediation="Pass the immutable report returned by blend search.",
        )
    report = load_json(search_report)
    if report.get("schema") != 1 or report.get("project") != project.id:
        raise BlendError(
            code="SEARCH_REPORT_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message="Search report schema or project identity does not match the target project.",
            remediation="Promote from an unmodified search report generated for this project.",
        )
    candidate = next(
        (item for item in report.get("ranking", []) if item["id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise BlendError(
            code="SEARCH_CANDIDATE_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"Candidate {candidate_id!r} is not in {search_report}.",
            remediation="Choose a candidate identifier from the retained search report.",
        )
    if not isinstance(candidate.get("baseVariant"), str) or not isinstance(candidate.get("parameters"), dict):
        raise BlendError(
            code="SEARCH_REPORT_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message="Selected search candidate is structurally invalid.",
            remediation="Promote from an unmodified Blend search report.",
        )
    project.resolved_variant(candidate["baseVariant"])
    config = load_yaml(project.paths.config)
    variants = config.setdefault("variants", {})
    if variant_name in variants:
        raise BlendError(
            code="SEARCH_PROMOTION_COLLISION",
            category=ErrorCategory.CONFIGURATION,
            message=f"Variant {variant_name!r} already exists.",
            remediation="Choose a new variant name; Blend never overwrites source variants.",
        )
    variants[variant_name] = {
        "extends": candidate["baseVariant"],
        "parameters": candidate["parameters"],
    }
    errors = schema_errors(config, "config-v1.json")
    if errors:
        raise BlendError(
            code="SEARCH_PROMOTION_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message="Promoted candidate would make blend.yaml invalid; source was not changed.",
            remediation="Inspect the candidate parameters and search declaration.",
            details={"errors": errors},
        )
    atomic_write_yaml(project.paths.config, config)
    promotion = {
        "schema": 1,
        "project": project.id,
        "searchReport": str(search_report),
        "candidate": candidate_id,
        "variant": variant_name,
        "source": str(project.paths.config),
        "generatedBlendCopied": False,
        "parameters": candidate["parameters"],
    }
    path = project.paths.artifacts / f"promotion-{variant_name}.json"
    atomic_write_json(path, promotion)
    promotion["report"] = str(path)
    return promotion
