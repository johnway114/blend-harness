from __future__ import annotations

from pathlib import Path
import subprocess

from PIL import Image
import pytest

from blend_harness.media import encode_sequence
from blend_harness.process import ProcessSupervisor

from conftest import require_ffmpeg


@pytest.mark.ffmpeg
def test_ffmpeg_encoding_validates_audio_fades_hold_streams_and_metadata(tmp_path: Path) -> None:
    ffmpeg, _ = require_ffmpeg()
    frames = tmp_path / "frames"
    frames.mkdir()
    for frame in range(1, 5):
        Image.new("RGB", (64, 48), (frame * 40, 30, 120)).save(frames / f"frame-{frame:06d}.png")
    audio = tmp_path / "tone.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(audio)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    destination = tmp_path / "delivery.mp4"
    with ProcessSupervisor() as supervisor:
        result = encode_sequence(
            supervisor,
            frame_pattern=frames / "frame-%06d.png",
            frame_start=1,
            frame_count=4,
            frame_rate=8,
            output={
                "codec": "h264",
                "audio": str(audio),
                "fadeInSeconds": 0.125,
                "fadeOutSeconds": 0.125,
                "finalHoldSeconds": 0.25,
            },
            destination=destination,
            log_root=tmp_path / "logs",
            operation_id="media-audio-qc",
            expected={"width": 64, "height": 48, "frameRate": 8, "channels": "RGB"},
        )
    assert destination.is_file()
    assert result["probe"]["codec"] == "h264"
    assert result["probe"]["audio"]["codec"] == "aac"
    assert result["probe"]["frameCount"] == 6
    assert abs(result["probe"]["durationSeconds"] - 0.75) < 0.15
    assert Path(result["validation"]).is_file()
