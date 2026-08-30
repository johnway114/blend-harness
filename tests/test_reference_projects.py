from __future__ import annotations

import json
from pathlib import Path
import os
import re
import shlex
import subprocess
import sys
import time

from PIL import Image, ImageChops, ImageStat
import pytest

from blend_harness.project import load_project, schema_errors
from blend_harness.util import sha256_file

from conftest import REPOSITORY, require_blender, require_ffmpeg, run_blend
from mcp_client import McpClient


def _init_reference(tmp_path: Path, template: str) -> Path:
    destination = tmp_path / template
    _, result = run_blend(
        "init", template, str(destination), "--operation-id", f"accept-init-{template}"
    )
    assert result["data"]["project"] == str(destination.resolve())
    return destination


def _reference_commands(project: Path) -> list[dict]:
    results = []
    for line in (project / "COMMANDS.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        argv = shlex.split(line)
        assert argv[:2] == ["blend", "--json"]
        completed = subprocess.run(
            [sys.executable, "-m", "blend_harness.cli", "--json", *argv[2:]],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
            env=os.environ.copy(),
        )
        result = json.loads(completed.stdout)
        assert completed.returncode == 0, (line, result, completed.stderr[-4000:])
        assert result["status"] == "succeeded", (line, result)
        assert not schema_errors(result, "result-v1.json"), line
        results.append(result)
    return results


def _assert_expectation(project: Path) -> dict:
    expected = json.loads((project / "expected.json").read_text(encoding="utf-8"))
    inspection = json.loads((project / "build" / "inspection.json").read_text(encoding="utf-8"))
    expectation = expected["inspection"]
    object_names = {item["name"] for item in inspection["objects"]}
    camera_names = {item["name"] for item in inspection["cameras"]}
    assert set(expectation["requiredObjects"]) <= object_names
    assert set(expectation["requiredCameras"]) <= camera_names
    assert inspection["statistics"]["objects"] >= expectation["minimumObjects"]
    assert inspection["statistics"]["triangles"] >= expectation["minimumTriangles"]
    for relative in expected["outputs"]:
        output = project / relative
        assert output.is_file() and output.stat().st_size > 0, relative
    baseline = project / expected["visualBaseline"]
    assert baseline.is_file() and baseline.stat().st_size > 0
    return expected


def _assert_reference_manifests(project: Path) -> None:
    manifests = []
    for path in sorted((project / "build" / "manifests").glob("*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if "blendVersion" not in manifest:
            continue
        manifests.append(path)
        assert not schema_errors(manifest, "manifest-v1.json"), path.name
        assert manifest["status"] == "succeeded"
        assert len(manifest["inputs"]["dependencyHash"]) == 64
        assert len(manifest["inputs"]["runtimeHash"]) == 64
        assert manifest["inputs"]["files"]
        for source in manifest["inputs"]["files"]:
            assert source["path"]
            assert len(source["sha256"]) == 64
            assert source["bytes"] >= 0
        for asset in manifest["inputs"]["assets"]:
            assert asset["id"] and asset["path"]
            assert asset["checksum"]
        assert manifest["resolved"]["profileName"]
        assert manifest["resolved"]["frameStart"] <= manifest["resolved"]["frameEnd"]
        assert manifest["timing"]["durationSeconds"] >= 0
        if manifest["blender"]:
            assert manifest["blender"]["offline"] is True
        for output in manifest["outputs"]:
            if output.get("exists"):
                assert len(output["sha256"]) == 64
                assert output["bytes"] > 0
    assert manifests




def _assert_preview_coverage(project: Path, results: list[dict]) -> None:
    config = load_project(project).config
    expected_frames = set(config["previewFrames"])
    expected_views = {
        view.get("id") or view.get("camera") or view.get("generated")
        for view in config["views"]
    }
    expected_modes = set(config["previewModes"])
    previews = [result for result in results if result["operation"] == "preview"]
    assert previews
    for result in previews:
        evidence = result["data"]["previews"]
        actual_frames = {item["frame"] for item in evidence}
        assert expected_frames <= actual_frames
        assert {config["project"]["frameStart"], config["project"]["frameEnd"]} <= actual_frames
        assert {item["view"] for item in evidence} == expected_views
        assert {item["mode"] for item in evidence} == expected_modes
        assert len(evidence) == len(actual_frames) * len(expected_views) * len(expected_modes)
        assert Path(result["data"]["contactSheet"]["path"]).is_file()

@pytest.mark.blender
@pytest.mark.ffmpeg
@pytest.mark.slow
def test_brand_ident_clean_workstation_outputs_and_selective_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())
    require_ffmpeg()
    project = _init_reference(tmp_path, "brand-ident")
    results = _reference_commands(project)
    expected = _assert_expectation(project)
    _assert_reference_manifests(project)

    render_results = [result for result in results if result["operation"] == "render"]
    matrix_plan = next(result["data"] for result in results if result["operation"] == "plan")
    assert matrix_plan["ready"]
    assert matrix_plan["matrix"]["size"] == 3
    assert matrix_plan["collisions"] == []
    assert matrix_plan["estimate"]["renderedFrames"] == 72
    assert matrix_plan["estimate"]["uncompressedStorageBytes"] > 0
    multi_profile_plan = next(
        result["data"]
        for result in results
        if result["operation"] == "plan" and result["data"]["matrix"]["id"] == "multi-profile"
    )
    assert multi_profile_plan["ready"]
    assert {
        member["profile"] for member in multi_profile_plan["matrix"]["members"]
    } == {"final", "transparent"}
    assert render_results
    square_sequence = project / "renders" / "final" / "square-amber" / "final" / "frames"
    _assert_preview_coverage(project, results)
    frame_name = "frame-000012.png"
    _, profile_comparison = run_blend(
        "compare",
        str(project / "renders" / "final" / "square-amber" / "final" / "frames" / frame_name),
        str(project / "renders" / "final" / "square-amber" / "transparent" / "frames" / frame_name),
        "--operation-id", "accept-brand-profile-comparison",
    )
    assert len(profile_comparison["data"]["images"]) == 1
    assert (
        profile_comparison["data"]["images"][0]["leftSha256"]
        != profile_comparison["data"]["images"][0]["rightSha256"]
    )
    _, variant_comparison = run_blend(
        "compare",
        str(project / "renders" / "final" / "square-amber" / "final" / "frames" / frame_name),
        str(project / "renders" / "final" / "vertical-ivory" / "final" / "frames" / frame_name),
        "--operation-id", "accept-brand-variant-comparison",
    )
    assert variant_comparison["data"]["images"][0]["metrics"]["changedPixelFraction"] > 0

    frames = sorted(square_sequence.glob("*.png"))
    assert len(frames) == 24
    damaged = frames[len(frames) // 2]
    with Image.open(damaged) as image:
        original_pixels = image.convert("RGB").copy()
    damaged_frame = int(damaged.stem.removeprefix("frame-"))
    damaged.write_bytes(b"corrupt")
    _, resumed = run_blend(
        "resume", str(project), "--matrix", "delivery",
        "--trust", "--jobs", "2", "--operation-id", "accept-brand-selective-resume", timeout=1200,
    )
    assert resumed["data"]["selectiveResume"] is True
    members = {
        item["member"]["variant"]: item["result"]
        for item in resumed["data"]["succeeded"]
    }
    assert members["square-amber"]["renderedFrames"] == [damaged_frame]
    assert len(members["square-amber"]["reusedFrames"]) == 23
    for variant in ("vertical-ivory", "landscape-oxide"):
        assert members[variant]["renderedFrames"] == []
        assert len(members[variant]["reusedFrames"]) == 24
    with Image.open(damaged) as image:
        resumed_pixels = image.convert("RGB")
        difference = ImageChops.difference(original_pixels, resumed_pixels)
        mean_difference = sum(ImageStat.Stat(difference).mean) / 3
    assert mean_difference <= 0.5

    encoded = {
        result["data"]["outputs"][0]["output"]: result["data"]["outputs"][0]
        for result in results
        if result["operation"] == "encode"
    }
    assert encoded["square-film"]["frameManifest"] == encoded["square-master"]["frameManifest"]
    assert all(
        result["data"]["rerenderedBlenderFrames"] == 0
        for result in results
        if result["operation"] == "encode"
    )

    # Two delivery codecs came from the same retained square sequence.
    assert (project / "output" / "brand-ident-square.mp4").is_file()
    assert (project / "output" / "brand-ident-square-master.mov").is_file()
    assert expected["validation"]["passed"]

@pytest.mark.blender
@pytest.mark.ffmpeg
@pytest.mark.slow
def test_product_turntable_clean_workstation_all_preview_modes_and_exports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())
    require_ffmpeg()
    project = _init_reference(tmp_path, "product-turntable")
    results = _reference_commands(project)
    _assert_expectation(project)

    _assert_reference_manifests(project)
    export_result = next(result for result in results if result["operation"] == "export")
    assert len(export_result["data"]["exports"]) == 6
    assert all(
        item["validation"]["decodable"] and not item["validation"]["failures"]
        for item in export_result["data"]["exports"]
    )
    _assert_preview_coverage(project, results)

    modes = ("material", "clay", "depth", "normal", "object-index", "wireframe", "alpha")
    for mode in modes:
        _, preview = run_blend(
            "preview", str(project), "--profile", "preview", "--variant", "graphite",
            "--view", "hero", "--mode", mode, "--frames", "1", "--trust",
            "--operation-id", f"accept-product-mode-{mode}", timeout=600,
        )
        assert len(preview["data"]["previews"]) == 1
        image_path = Path(preview["data"]["previews"][0]["path"])
        with Image.open(image_path) as image:
            assert image.size == (256, 256)
            if mode == "alpha":
                assert image.mode == "RGBA"
                alpha = image.getchannel("A")
                extrema = alpha.getextrema()
                assert extrema[0] < 255 and extrema[1] == 255

    _, contact = run_blend(
        "contact-sheet", str(project),
        "--operation-id", "accept-product-contact-sheet",
    )
    assert Path(contact["data"]["contactSheet"]["path"]).is_file()
    contact_manifest = json.loads(Path(contact["data"]["manifest"]).read_text(encoding="utf-8"))
    assert contact_manifest["status"] == "succeeded"
    duplicate, duplicate_result = run_blend(
        "contact-sheet", str(project),
        "--operation-id", "accept-product-contact-sheet",
        check=False,
    )
    assert duplicate.returncode != 0
    assert duplicate_result["error"]["code"] == "OPERATION_ID_ALREADY_USED"

    review_directory = project / "output" / "review-reference-product-review"
    review_html = (review_directory / "index.html").read_text(encoding="utf-8")
    resource_references = re.findall(r'(?:src|href)="([^"]+)"', review_html)
    assert resource_references
    assert not any(reference.startswith(("/", "file:")) for reference in resource_references)
    package_manifest = json.loads((review_directory / "package-manifest.json").read_text(encoding="utf-8"))
    assert package_manifest["files"]
    review_data = json.loads((review_directory / "review-data.json").read_text(encoding="utf-8"))
    assert review_data["manifests"]
    assert all(
        record["value"].get("blendVersion") and record["value"].get("inputs")
        for record in review_data["manifests"]
    )
    source_config_hash = sha256_file(project / "blend.yaml")
    _, disposition = run_blend(
        "review", "--record", str(review_directory), "--decision", "approved",
        "--comments", "Mechanical evidence and visual review accepted.",
        "--variant", "graphite", "--operation-id", "accept-product-disposition",
    )
    disposition_path = Path(disposition["data"]["path"])
    assert disposition_path.is_file()
    assert disposition["data"]["record"]["decision"] == "approved"
    assert sha256_file(project / "blend.yaml") == source_config_hash

    with McpClient() as client:
        absolute = str(project.resolve())
        plan = client.call("blend_plan", {
            "project": absolute,
            "target": "render",
            "profile": "final",
            "variant": "graphite",
        })
        assert plan["structuredContent"]["status"] == "succeeded"
        preview = client.call("blend_preview", {
            "project": absolute,
            "profile": "preview",
            "variant": "graphite",
            "view": "hero",
            "mode": "material",
            "frames": [1],
            "trust": True,
            "operationId": "mcp-product-preview",
        })
        assert preview["structuredContent"]["data"]["previewCount"] == 1
        cache = client.call("blend_cache_inspect", {
            "project": absolute,
            "simulation": "product-settle",
            "profile": "final-settle",
        })
        assert cache["structuredContent"]["data"]["summary"]["complete"] == 1
        artifact = client.call("blend_artifact", {
            "project": absolute,
            "path": preview["structuredContent"]["data"]["contactSheet"]["path"],
            "maxBytes": 1048576,
        })
        assert artifact["structuredContent"]["bytes"] > 0
        assert artifact["structuredContent"]["encoding"] == "base64"
        inspected = client.call("blend_inspect", {
            "project": absolute,
            "profile": "final",
            "variant": "graphite",
            "trust": True,
            "operationId": "mcp-product-inspect",
        })
        assert inspected["structuredContent"]["data"]["inspection"]["counts"]["objects"] >= 8
        validated = client.call("blend_validate", {
            "project": absolute,
            "profile": "final",
            "variant": "graphite",
            "trust": True,
            "operationId": "mcp-product-validate",
        })
        assert validated["structuredContent"]["status"] == "succeeded"
        rendered = client.call("blend_render", {
            "project": absolute,
            "profile": "final",
            "variant": "graphite",
            "frames": [1],
            "jobs": 1,
            "trust": True,
            "operationId": "mcp-product-render-one",
        })
        assert rendered["structuredContent"]["data"]["renderedFrames"] == [1]
        resumed = client.call("blend_resume", {
            "project": absolute,
            "profile": "final",
            "variant": "graphite",
            "frames": [1],
            "jobs": 1,
            "trust": True,
            "operationId": "mcp-product-resume-one",
        })
        assert resumed["structuredContent"]["data"]["reusedFrames"] == [1]
        recovered_full_sequence = client.call("blend_resume", {
            "project": absolute,
            "profile": "final",
            "variant": "graphite",
            "jobs": 1,
            "trust": True,
            "operationId": "mcp-product-resume-full",
        })
        assert recovered_full_sequence["structuredContent"]["data"]["renderedFrames"] == [1]
        assert recovered_full_sequence["structuredContent"]["data"]["reusedFrames"] == list(
            range(2, 25)
        )


        for path in (
            project / "output" / "product-turntable.mp4",
            project / "output" / "product-turntable.mp4.media.json",
        ):
            path.unlink(missing_ok=True)
        encoded = client.call("blend_encode", {
            "project": absolute,
            "output": "turntable-film",
            "operationId": "mcp-product-encode",
        })
        assert encoded["structuredContent"]["status"] == "succeeded"

        for path in (
            project / "output" / "product.stl",
            project / "output" / "product.stl.export.json",
        ):
            path.unlink(missing_ok=True)
        exported = client.call("blend_export", {
            "project": absolute,
            "profile": "final",
            "variant": "graphite",
            "output": "stl-model",
            "trust": True,
            "operationId": "mcp-product-export",
        })
        assert exported["structuredContent"]["status"] == "succeeded"

        compared = client.call("blend_compare", {
            "project": absolute,
            "left": str(project / "references" / "baselines" / "contact-sheet.png"),
            "right": preview["structuredContent"]["data"]["contactSheet"]["path"],
            "operationId": "mcp-product-compare",
        })
        assert compared["structuredContent"]["data"]["images"]

        request_id = client.begin("tools/call", {
            "name": "blend_render",
            "arguments": {
                "project": absolute,
                "profile": "final",
                "variant": "graphite",
                "frames": list(range(1, 25)),
                "jobs": 2,
                "trust": True,
                "operationId": "mcp-product-cancel-render",
            },
            "_meta": {"progressToken": "product-render-progress"},
        })
        time.sleep(0.75)
        client.notify("notifications/cancelled", {"requestId": request_id})
        cancelled = client.finish(request_id)
        assert cancelled["result"]["isError"]
        assert cancelled["result"]["structuredContent"]["status"] in {"interrupted", "failed", "partial"}
        progress = [
            message["params"]
            for message in client.notifications
            if message.get("method") == "notifications/progress"
            and message.get("params", {}).get("progressToken") == "product-render-progress"
        ]
        assert progress
        assert all(0 <= item["progress"] <= (item.get("total") or 24) for item in progress)

        recovered = client.call("blend_resume", {
            "project": absolute,
            "profile": "final",
            "variant": "graphite",
            "frames": list(range(1, 25)),
            "jobs": 2,
            "trust": True,
            "operationId": "mcp-product-recover-render",
        })
        assert recovered["structuredContent"]["status"] == "succeeded"
        assert len(recovered["structuredContent"]["data"]["renderedFrames"]) + len(
            recovered["structuredContent"]["data"]["reusedFrames"]
        ) == 24

    _, cache_state = run_blend(
        "cache", "inspect", str(project), "--simulation", "product-settle",
        "--profile", "final-settle",
    )
    assert cache_state["data"]["summary"] == {"complete": 1, "staleOrMissing": 0}
    source_model = project / "assets" / "models" / "product.obj"
    source_model_hash = sha256_file(source_model)
    _, cleaned = run_blend(
        "cache", "clean", str(project), "--simulation", "product-settle",
        "--operation-id", "accept-product-cache-clean",
    )
    assert cleaned["data"]["removed"]
    assert cleaned["data"]["sourceDeleted"] is False
    assert source_model.is_file() and sha256_file(source_model) == source_model_hash
    _, missing_cache = run_blend(
        "cache", "inspect", str(project), "--simulation", "product-settle",
        "--profile", "final-settle",
    )
    assert missing_cache["data"]["summary"] == {"complete": 0, "staleOrMissing": 1}


@pytest.mark.blender
@pytest.mark.ffmpeg
@pytest.mark.slow
def test_procedural_explainer_two_revisions_comparison_search_and_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())
    require_ffmpeg()
    project = _init_reference(tmp_path, "procedural-explainer")
    results = _reference_commands(project)
    _assert_expectation(project)
    _assert_reference_manifests(project)
    _assert_preview_coverage(project, results)

    comparison = next(result for result in results if result["operation"] == "compare")
    report = comparison["data"]
    assert report["images"]
    assert any(
        item["metrics"]["changedPixelFraction"] > 0
        for item in report["images"]
    )
    assert report["structuralChanges"]

    _, search = run_blend(
        "search", str(project), "layout", "--trust", "--operation-id", "accept-explainer-search", timeout=1800,
    )
    search_data = search["data"]
    ranking = search_data["search"]["ranking"]
    assert len(ranking) == 6
    assert ranking[0]["rank"] == 1
    assert Path(search_data["search"]["rankedContactSheet"]["path"]).is_file()
    selected = ranking[0]
    _, promoted = run_blend(
        "promote", str(project), search_data["report"], selected["id"], "accepted-layout",
        "--operation-id", "accept-explainer-promote",
    )
    assert promoted["data"]["variant"] == "accepted-layout"
    completed, result = run_blend("validate-config", str(project))
    assert completed.returncode == 0 and result["data"]["summary"]["passed"]
