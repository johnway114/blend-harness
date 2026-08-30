from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
TEMPLATES = REPOSITORY / "blend_harness" / "templates"


def installed_blender() -> str | None:
    configured = os.environ.get("BLENDER_BIN") or os.environ.get("BLENDER_EXECUTABLE")
    candidates = [
        configured,
        shutil.which("blender"),
        "/Applications/Blender.app/Contents/MacOS/Blender",
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


def require_blender() -> str:
    executable = installed_blender()
    if not executable:
        pytest.skip("a real Blender executable is not installed")
    return executable


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are not installed")
    return ffmpeg, ffprobe


def project_copy(tmp_path: Path, template: str) -> Path:
    destination = tmp_path / template
    ignored = shutil.ignore_patterns(
        "build", "previews", "renders", "output", ".blend-trust.json", "__pycache__", ".pytest_cache"
    )
    shutil.copytree(TEMPLATES / template, destination, ignore=ignored)
    return destination


def run_blend(*arguments: str, check: bool = True, timeout: float = 180.0) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    command = [sys.executable, "-m", "blend_harness.cli", "--json", *arguments]
    completed = subprocess.run(
        command,
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )
    result = json.loads(completed.stdout)
    if check and completed.returncode != 0:
        raise AssertionError(f"command failed ({completed.returncode}): {command}\n{result}\n{completed.stderr}")
    return completed, result
