from __future__ import annotations

from pathlib import Path

import pytest

from conftest import require_blender, require_ffmpeg, run_blend


@pytest.mark.blender
@pytest.mark.ffmpeg
def test_doctor_reports_complete_supported_host_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    blender = require_blender()
    require_ffmpeg()
    monkeypatch.setenv("BLENDER_BIN", blender)
    _, result = run_blend("doctor", "--operation-id", "accept-doctor", timeout=120)
    report = result["data"]
    assert report["schema"] == 1
    assert report["ready"] is True
    assert report["blender"]["path"] == str(Path(blender).resolve())
    assert report["blender"]["version"]
    assert report["blender"]["pythonApi"]["python"]
    assert report["blender"]["engines"]
    assert "cyclesDevices" in report["blender"]
    assert report["ffmpeg"]["available"] is True
    assert Path(report["ffmpeg"]["ffprobe"]).is_file()
    assert report["ffmpeg"]["codecs"]["h264"]
    assert report["blender"]["colorManagement"]["views"]
    assert "offline" in report
    assert report["blend"]["compatibility"]["status"] == "supported"
    assert report["blend"]["supportedSchemas"]["configuration"] == [1]
    assert report["blend"]["version"]
    assert report["blend"]["runtimeVersion"]
