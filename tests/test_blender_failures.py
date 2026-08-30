from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import project_copy, require_blender, run_blend


def _rules(project: Path) -> set[str]:
    report = json.loads((project / "build" / "validation.json").read_text(encoding="utf-8"))
    return {finding["ruleId"] for finding in report["findings"] if not finding.get("suppressed")}


@pytest.mark.blender
def test_network_permission_is_rejected_before_project_source_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())
    project = project_copy(tmp_path, "empty")
    marker = project / "source-executed"
    scene = project / "scene.py"
    scene.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n" + scene.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    completed, result = run_blend(
        "build", str(project), "--trust", "--allow-network",
        "--operation-id", "reject-undeclared-network", check=False,
    )
    assert completed.returncode != 0
    assert result["error"]["code"] == "SECURITY_NETWORK_NOT_DECLARED"
    assert not marker.exists()


@pytest.mark.blender
def test_missing_active_camera_and_clipped_subject_block_final_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())

    missing = project_copy(tmp_path / "missing", "brand-ident")
    scene = missing / "scene.py"
    source = scene.read_text(encoding="utf-8").replace("    bpy.context.scene.camera = camera\n", "")
    scene.write_text(source, encoding="utf-8")
    run_blend("build", str(missing), "--trust", "--operation-id", "missing-camera-build")
    completed, result = run_blend(
        "validate", str(missing), "--profile", "final", "--variant", "square-amber",
        "--trust", "--operation-id", "missing-camera-validate", check=False,
    )
    assert completed.returncode != 0
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert "SCENE.NO_ACTIVE_CAMERA" in _rules(missing)

    clipped = project_copy(tmp_path / "clipped", "brand-ident")
    scene = clipped / "scene.py"
    source = scene.read_text(encoding="utf-8").replace(
        "    bpy.context.scene.camera = camera\n",
        "    bpy.context.scene.camera = camera\n    camera.data.clip_start = 7.0\n",
    )
    scene.write_text(source, encoding="utf-8")
    run_blend("build", str(clipped), "--trust", "--operation-id", "clipped-camera-build")
    completed, result = run_blend(
        "validate", str(clipped), "--profile", "final", "--variant", "square-amber",
        "--trust", "--operation-id", "clipped-camera-validate", check=False,
    )
    assert completed.returncode != 0
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert "SCENE.CAMERA_CLIPPING" in _rules(clipped)


@pytest.mark.blender
def test_stale_checkpoint_and_cache_are_detected_after_source_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())
    project = project_copy(tmp_path, "product-turntable")
    run_blend("build", str(project), "--trust", "--operation-id", "stale-build")
    run_blend(
        "bake", str(project), "--simulation", "product-settle", "--profile", "final-settle",
        "--trust", "--operation-id", "stale-cache-bake",
    )
    scene = project / "scene.py"
    scene.write_text(scene.read_text(encoding="utf-8") + "\n# dependency change\n", encoding="utf-8")

    _, cache = run_blend(
        "cache", "inspect", str(project), "--simulation", "product-settle", "--profile", "final-settle",
    )
    assert cache["data"]["caches"][0]["current"] is False
    assert (
        cache["data"]["caches"][0]["manifestDependencyHash"]
        != cache["data"]["caches"][0]["expectedDependencyHash"]
    )
    completed, result = run_blend(
        "validate", str(project), "--profile", "final", "--variant", "graphite",
        "--trust", "--operation-id", "stale-final-validate", check=False,
    )
    assert completed.returncode != 0
    rules = _rules(project)
    assert "CHECKPOINT.STALE" in rules
    assert "ANIMATION.CACHE_MISSING_OR_STALE" in rules


@pytest.mark.blender
def test_entrypoint_failure_returns_retained_relevant_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())
    project = project_copy(tmp_path, "empty")
    scene = project / "scene.py"
    scene.write_text("raise RuntimeError('deliberate entrypoint failure')\n", encoding="utf-8")
    completed, result = run_blend(
        "build", str(project), "--trust", "--operation-id", "entrypoint-failure", check=False,
    )
    assert completed.returncode != 0
    assert result["error"]["code"] == "ENTRYPOINT_PYTHON_FAILED"
    retained = [Path(path) for path in result["error"]["retainedArtifacts"]]
    logs = [path for path in retained if path.suffix == ".log"]
    assert logs and logs[0].is_file()
    assert "deliberate entrypoint failure" in logs[0].read_text(encoding="utf-8", errors="replace")
