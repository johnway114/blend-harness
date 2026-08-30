from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from conftest import REPOSITORY, project_copy, require_blender, require_ffmpeg, run_blend


def _interrupt_operation(
    project: Path,
    operation_id: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    command = [sys.executable, "-m", "blend_harness.cli", *arguments, "--operation-id", operation_id, "--json"]
    process = subprocess.Popen(
        command,
        cwd=REPOSITORY,
        env={**os.environ, **(environment or {})},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    manifest_path = project / "build" / "manifests" / f"{operation_id}.json"
    deadline = time.monotonic() + 20
    while not manifest_path.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert manifest_path.is_file(), f"{operation_id} never created an operation manifest"
    time.sleep(0.1)
    assert process.poll() is None, f"{operation_id} completed before it could be interrupted"
    os.killpg(process.pid, signal.SIGINT)
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode != 0, (stdout, stderr)
    result = json.loads(stdout.strip().splitlines()[-1])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["error"]["category"] == "interrupted", result
    assert manifest["status"] == "interrupted", manifest
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        pass
    else:
        pytest.fail(f"owned process group {process.pid} survived interrupted {operation_id}")
    return result, manifest


@pytest.mark.blender
@pytest.mark.ffmpeg
@pytest.mark.slow
def test_forced_interruptions_retain_recoverable_work_and_leave_no_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blender = require_blender()
    real_ffmpeg, _ = require_ffmpeg()
    monkeypatch.setenv("BLENDER_BIN", blender)
    project = project_copy(tmp_path, "product-turntable")

    _interrupt_operation(
        project,
        "interrupt-build",
        "build", str(project), "--trust",
    )
    run_blend("build", str(project), "--trust", "--operation-id", "recover-build")

    _interrupt_operation(
        project,
        "interrupt-bake",
        "bake", str(project), "--simulation", "product-settle", "--profile", "final-settle", "--trust",
    )
    _, interrupted_cache = run_blend(
        "cache", "inspect", str(project), "--simulation", "product-settle", "--profile", "final-settle",
    )
    assert interrupted_cache["data"]["summary"]["staleOrMissing"] == 1
    run_blend(
        "bake", str(project), "--simulation", "product-settle", "--profile", "preview-settle",
        "--trust", "--operation-id", "recover-bake-preview",
    )
    run_blend(
        "bake", str(project), "--simulation", "product-settle", "--profile", "final-settle",
        "--trust", "--operation-id", "recover-bake-final",
    )

    _interrupt_operation(
        project,
        "interrupt-preview",
        "preview", str(project), "--profile", "preview", "--variant", "graphite", "--trust",
    )
    run_blend(
        "validate", str(project), "--profile", "final", "--variant", "graphite", "--trust",
        "--operation-id", "interruptions-validate",
    )

    _, interrupted_render = _interrupt_operation(
        project,
        "interrupt-render",
        "render", str(project), "--profile", "final", "--variant", "graphite", "--jobs", "2", "--trust",
    )
    completed_before_interrupt = interrupted_render["completedFrames"]
    _, recovered = run_blend(
        "resume", str(project), "--profile", "final", "--variant", "graphite", "--jobs", "2", "--trust",
        "--operation-id", "recover-render", timeout=600,
    )
    recovered_frames = recovered["data"]["renderedFrames"] + recovered["data"]["reusedFrames"]
    assert sorted(recovered_frames) == list(range(1, 25))
    assert set(completed_before_interrupt) <= set(recovered["data"]["reusedFrames"])

    wrapper_root = tmp_path / "slow-ffmpeg"
    wrapper_root.mkdir()
    wrapper = wrapper_root / "ffmpeg"
    wrapper.write_text(
        "#!/bin/sh\n/bin/sleep 30\nexec " + str(Path(real_ffmpeg).resolve()) + " \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    _interrupt_operation(
        project,
        "interrupt-encode",
        "encode", str(project), "--output", "turntable-film",
        environment={"PATH": f"{wrapper_root}{os.pathsep}{os.environ['PATH']}"},
    )
    assert not any(project.joinpath("output").glob("*.interrupt-encode.part"))

    _interrupt_operation(
        project,
        "interrupt-export",
        "export", str(project), "--profile", "final", "--variant", "graphite", "--output", "web-model", "--trust",
    )

    for executable in ("[Bb]lender", "ffmpeg"):
        completed = subprocess.run(
            ["pgrep", "-f", f"{executable}.*{project}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 1, completed.stdout


@pytest.mark.blender
@pytest.mark.slow
def test_interrupted_variant_matrix_retains_frames_and_resumes_pending_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLENDER_BIN", require_blender())
    project = project_copy(tmp_path, "brand-ident")
    run_blend(
        "build", str(project), "--trust", "--operation-id", "matrix-interruption-build"
    )
    for variant in ("square-amber", "vertical-ivory", "landscape-oxide"):
        run_blend(
            "validate", str(project), "--profile", "final", "--variant", variant,
            "--trust", "--operation-id", f"matrix-interruption-validate-{variant}",
        )

    operation_id = "interrupt-matrix-render"
    process = subprocess.Popen(
        [
            sys.executable, "-m", "blend_harness.cli",
            "render", str(project), "--matrix", "delivery", "--jobs", "2", "--trust",
            "--operation-id", operation_id, "--json",
        ],
        cwd=REPOSITORY,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    root_manifest_path = project / "build" / "manifests" / f"{operation_id}.json"
    child_manifest_path = project / "build" / "manifests" / f"{operation_id}-000.json"
    completed_before_interrupt: list[int] = []
    deadline = time.monotonic() + 60
    while process.poll() is None and time.monotonic() < deadline:
        if child_manifest_path.is_file():
            try:
                child = json.loads(child_manifest_path.read_text(encoding="utf-8"))
                completed_before_interrupt = child.get("completedFrames", [])
            except json.JSONDecodeError:
                completed_before_interrupt = []
            if completed_before_interrupt:
                break
        time.sleep(0.01)
    assert root_manifest_path.is_file()
    assert completed_before_interrupt
    assert process.poll() is None
    os.killpg(process.pid, signal.SIGINT)
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode != 0, (stdout, stderr)
    result = json.loads(stdout.strip().splitlines()[-1])
    root_manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
    assert result["status"] == "interrupted"
    assert result["error"]["code"] == "PROCESS_INTERRUPTED"
    assert root_manifest["status"] == "interrupted"
    report = json.loads(
        (project / "build" / "manifests" / f"matrix-{operation_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["pending"]

    _, recovered = run_blend(
        "resume", str(project), "--matrix", "delivery", "--jobs", "2", "--trust",
        "--operation-id", "recover-matrix-render", timeout=1200,
    )
    members = recovered["data"]["succeeded"]
    assert not recovered["data"]["failed"]
    assert sum(
        len(member["result"]["renderedFrames"]) + len(member["result"]["reusedFrames"])
        for member in members
    ) == 72
    first_member = next(
        member for member in members if member["member"]["variant"] == "square-amber"
    )
    assert set(completed_before_interrupt) <= set(first_member["result"]["reusedFrames"])
