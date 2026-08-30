from __future__ import annotations

import json
from pathlib import Path

from blend_harness.caches import cache_path, inspect_caches, write_cache_manifest
from blend_harness.project import load_project
from blend_harness.validation import validate_project

from conftest import project_copy


def _rules(report: dict) -> set[str]:
    return {finding["ruleId"] for finding in report["findings"] if not finding["suppressed"]}


def test_cache_profiles_manifests_invalidation_and_failure_states(tmp_path: Path) -> None:
    project = load_project(project_copy(tmp_path, "product-turntable"), create_generated=True)
    simulation = project.config["simulations"][0]
    final_root = cache_path(project, simulation, "final-settle")
    preview_root = cache_path(project, simulation, "preview-settle")
    assert final_root != preview_root

    final_root.mkdir(parents=True)
    expected = final_root / "blend-cache-complete.json"
    expected.write_text('{"complete":true}', encoding="utf-8")
    manifest_path = write_cache_manifest(
        project,
        simulation,
        blender_version="5.2.1",
        runtime_record={"operation": "fixture"},
        status="complete",
        duration_seconds=0.1,
        simulation_profile="final-settle",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    assert manifest["simulationProfile"] == "final-settle"
    assert len(manifest["dependencyHash"]) == 64
    assert len(manifest["runtimeHash"]) == 64
    assert manifest["outputs"] == [{
        "path": "blend-cache-complete.json",
        "sha256": manifest["outputs"][0]["sha256"],
        "bytes": expected.stat().st_size,
    }]
    assert inspect_caches(project, "product-settle", "final-settle")["summary"]["complete"] == 1

    expected.unlink()
    partial = validate_project(
        project,
        profile="final",
        variant="graphite",
        operation="render",
        blender_version="5.2.1",
        write_artifact=False,
    )
    assert "SIMULATION.CACHE_PARTIAL" in _rules(partial)

    expected.write_text('{"complete":true}', encoding="utf-8")
    manifest_path = write_cache_manifest(
        project,
        simulation,
        blender_version="4.5.3",
        runtime_record={"operation": "fixture"},
        status="complete",
        duration_seconds=0.1,
        simulation_profile="final-settle",
    )
    incompatible = validate_project(
        project,
        profile="final",
        variant="graphite",
        operation="render",
        blender_version="5.2.1",
        write_artifact=False,
    )
    assert "SIMULATION.CACHE_INCOMPATIBLE" in _rules(incompatible)

    simulation["maximumBytes"] = 1
    oversized = validate_project(
        project,
        profile="final",
        variant="graphite",
        operation="render",
        blender_version="4.5.3",
        write_artifact=False,
    )
    assert "SIMULATION.CACHE_TOO_LARGE" in _rules(oversized)
    simulation.pop("maximumBytes")

    source = project.paths.entrypoint
    source.write_text(source.read_text(encoding="utf-8") + "\n# invalidate cache\n", encoding="utf-8")
    invalidated = inspect_caches(project, "product-settle", "final-settle")
    assert invalidated["summary"] == {"complete": 0, "staleOrMissing": 1}
    assert invalidated["caches"][0]["manifestDependencyHash"] != invalidated["caches"][0]["expectedDependencyHash"]
