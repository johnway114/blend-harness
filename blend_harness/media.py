"""Frame validation, labeled contact sheets, FFmpeg encoding, and media QC."""

from __future__ import annotations

import json
import math
import os
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from .capabilities import find_ffmpeg, find_ffprobe
from .errors import BlendError, ErrorCategory
from .process import ProcessSupervisor
from .util import atomic_write_json, sha256_file


def probe_media(supervisor: ProcessSupervisor, path: Path, *, log_root: Path,
                operation_id: str) -> dict[str, Any]:
    ffprobe = find_ffprobe()
    completed = supervisor.run(
        [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-count_frames", "-of", "json", str(path)],
        cwd=path.parent,
        log_path=log_root / f"ffprobe-{operation_id}.log",
        timeout_seconds=60,
    )
    if completed.returncode != 0:
        raise BlendError(
            code="MEDIA_PROBE_FAILED",
            category=ErrorCategory.ENCODING,
            message=f"Media validation failed for {path}.",
            remediation="Replace the corrupt frame or rerun encoding from validated frames.",
            details={"path": str(path), "log": str(completed.log_path), "logTail": completed.stdout[-4000:]},
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BlendError(
            code="MEDIA_PROBE_JSON_INVALID",
            category=ErrorCategory.ENCODING,
            message=f"FFprobe returned invalid JSON for {path}.",
            remediation="Inspect the retained FFprobe log and verify the FFmpeg installation.",
            details={"log": str(completed.log_path)},
        ) from exc
    streams = value.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = video.get("duration") or value.get("format", {}).get("duration")
    frame_rate_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        frame_rate = float(Fraction(frame_rate_text))
    except (ValueError, ZeroDivisionError):
        frame_rate = 0.0
    frame_count = video.get("nb_read_frames") or video.get("nb_frames")
    return {
        "path": str(path),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": sha256_file(path) if path.is_file() else None,
        "log": str(completed.log_path),
        "codec": video.get("codec_name"),
        "codecLongName": video.get("codec_long_name"),
        "pixelFormat": video.get("pix_fmt"),
        "width": int(video.get("width", 0) or 0),
        "height": int(video.get("height", 0) or 0),
        "channels": video.get("channels"),
        "frameRate": frame_rate,
        "frameCount": int(frame_count) if frame_count not in (None, "N/A") else None,
        "durationSeconds": float(duration) if duration not in (None, "N/A") else None,
        "color": {
            "range": video.get("color_range"),
            "space": video.get("color_space"),
            "transfer": video.get("color_transfer"),
            "primaries": video.get("color_primaries"),
        },
        "alpha": str(video.get("pix_fmt", "")).startswith(
            ("rgba", "argb", "bgra", "abgr", "yuva", "gbrap", "ya")
        ),
        "audio": {
            "codec": audio.get("codec_name"),
            "channels": audio.get("channels"),
            "sampleRate": int(audio.get("sample_rate", 0) or 0),
        } if audio else None,
        "raw": value,
    }


def validate_frame(supervisor: ProcessSupervisor, path: Path, *, width: int, height: int,
                   channels: str, dependency_hash: str, record: dict[str, Any] | None,
                   log_root: Path, operation_id: str) -> tuple[bool, dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return False, {"reason": "missing-or-empty", "path": str(path)}
    if record is None or record.get("dependencyHash") != dependency_hash:
        return False, {"reason": "manifest-input-mismatch", "path": str(path),
                       "expected": dependency_hash, "actual": record.get("dependencyHash") if record else None}
    actual_checksum = sha256_file(path)
    if record.get("sha256") != actual_checksum or record.get("bytes") != path.stat().st_size:
        return False, {
            "reason": "checksum-or-size",
            "path": str(path),
            "expectedSha256": record.get("sha256"),
            "actualSha256": actual_checksum,
            "expectedBytes": record.get("bytes"),
            "actualBytes": path.stat().st_size,
        }
    expected_channels = 4 if channels == "RGBA" else 3 if channels == "RGB" else 1
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            actual_size = image.size
            actual_channels = len(image.getbands())
        valid = actual_size == (width, height) and actual_channels == expected_channels
        return valid, {
            "reason": None if valid else "dimensions-or-channels",
            "path": str(path),
            "width": actual_size[0],
            "height": actual_size[1],
            "channels": actual_channels,
            "expectedWidth": width,
            "expectedHeight": height,
            "expectedChannels": expected_channels,
            "decodable": True,
        }
    except (OSError, UnidentifiedImageError):
        try:
            probe = probe_media(supervisor, path, log_root=log_root, operation_id=operation_id)
            pix_fmt = str(probe.get("pixelFormat") or "")
            alpha = probe.get("alpha", False)
            valid_channels = expected_channels != 4 or alpha
            valid = probe["width"] == width and probe["height"] == height and valid_channels
            return valid, {"reason": None if valid else "dimensions-or-channels", "decodable": True, **probe}
        except BlendError as exc:
            return False, {"reason": "undecodable", "path": str(path), "error": exc.as_dict()}


def make_contact_sheet(entries: list[dict[str, Any]], destination: Path, *,
                       project_id: str, source_revision: str | None, blender_version: str,
                       profile: str, variant: str | None, warnings: list[str | dict[str, Any]] | None = None,
                       maximum_columns: int = 4) -> dict[str, Any]:
    if not entries:
        raise BlendError(
            code="PREVIEW_CONTACT_SHEET_EMPTY",
            category=ErrorCategory.RENDER_ENGINE,
            message="No preview images are available for a contact sheet.",
            remediation="Run blend preview before blend contact-sheet.",
        )
    loaded: list[tuple[dict[str, Any], Image.Image]] = []
    for entry in entries:
        path = Path(entry["path"])
        try:
            image = Image.open(path).convert("RGB")
        except (OSError, UnidentifiedImageError) as exc:
            raise BlendError(
                code="PREVIEW_IMAGE_UNREADABLE",
                category=ErrorCategory.RENDER_ENGINE,
                message=f"Preview image cannot be read: {path}",
                remediation="Rerun preview for the invalid view or frame.",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        loaded.append((entry, image))
    thumb_width = min(480, max(image.width for _, image in loaded))
    thumb_height = min(360, max(image.height for _, image in loaded))
    label_height = 74
    header_height = 90 + (24 if warnings else 0)
    columns = min(maximum_columns, max(1, math.ceil(math.sqrt(len(loaded)))))
    rows = math.ceil(len(loaded) / columns)
    gutter = 12
    sheet_width = columns * thumb_width + (columns + 1) * gutter
    sheet_height = header_height + rows * (thumb_height + label_height) + (rows + 1) * gutter
    sheet = Image.new("RGB", (sheet_width, sheet_height), "#11110f")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=14)
    small = ImageFont.load_default(size=12)
    draw.text((gutter, 14), project_id, fill="#f3efe3", font=ImageFont.load_default(size=24))
    meta = f"revision {source_revision or 'unversioned'}  |  Blender {blender_version}  |  profile {profile}  |  variant {variant or 'base'}"
    draw.text((gutter, 48), meta, fill="#b8b4a8", font=font)
    if warnings:
        draw.text((gutter, 69), f"{len(warnings)} warning(s): review validation.json", fill="#e3a654", font=font)
    fps = None
    for index, (entry, image) in enumerate(loaded):
        column = index % columns
        row = index // columns
        x = gutter + column * thumb_width
        y = header_height + gutter + row * (thumb_height + label_height)
        thumbnail = image.copy()
        thumbnail.thumbnail((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        image_x = x + (thumb_width - thumbnail.width) // 2
        image_y = y + (thumb_height - thumbnail.height) // 2
        sheet.paste(thumbnail, (image_x, image_y))
        frame = entry.get("frame")
        time_text = entry.get("timeSeconds")
        if time_text is None and frame is not None and entry.get("frameRate"):
            time_text = (frame - entry.get("frameStart", 1)) / entry["frameRate"]
        dimensions = f"{image.width}x{image.height}"
        line_one = f"{entry.get('view') or entry.get('camera') or 'active'}  ·  {entry.get('mode', 'material')}  ·  frame {frame}"
        line_two = f"t={time_text:.3f}s  ·  {dimensions}" if time_text is not None else dimensions
        draw.text((x + 4, y + thumb_height + 10), line_one, fill="#f3efe3", font=font)
        draw.text((x + 4, y + thumb_height + 34), line_two, fill="#aaa69a", font=small)
        image.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        sheet.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "bytes": destination.stat().st_size,
        "entries": len(entries),
        "columns": columns,
        "rows": rows,
        "labels": ["projectRevision", "blenderVersion", "profile", "variant", "camera", "frame", "time", "resolution", "warnings"],
    }


def _codec_arguments(codec: str, output: dict[str, Any]) -> list[str]:
    pixel_format = output.get("pixelFormat")
    if codec == "h264":
        return ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", pixel_format or "yuv420p"]
    if codec == "hevc":
        return ["-c:v", "libx265", "-preset", "slow", "-crf", "20", "-pix_fmt", pixel_format or "yuv420p10le"]
    if codec == "prores":
        return ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", pixel_format or "yuv422p10le"]
    if codec == "prores-alpha":
        return ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", pixel_format or "yuva444p10le"]
    if codec == "ffv1":
        return ["-c:v", "ffv1", "-level", "3", "-pix_fmt", pixel_format or "rgba"]
    raise BlendError(
        code="ENCODE_CODEC_UNSUPPORTED",
        category=ErrorCategory.ENCODING,
        message=f"Unsupported codec {codec!r}.",
        remediation="Choose h264, hevc, prores, prores-alpha, or ffv1.",
    )


def encode_sequence(supervisor: ProcessSupervisor, *, frame_pattern: Path, frame_start: int,
                    frame_count: int, frame_rate: float, output: dict[str, Any], destination: Path,
                    log_root: Path, operation_id: str, expected: dict[str, Any]) -> dict[str, Any]:
    ffmpeg = find_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}.{operation_id}.part{destination.suffix}"
    )
    temporary.unlink(missing_ok=True)
    args = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-framerate",
        str(frame_rate),
        "-start_number",
        str(frame_start),
        "-i",
        str(frame_pattern),
    ]
    audio = output.get("audio")
    if audio:
        args.extend(["-protocol_whitelist", "file,pipe", "-i", str(audio)])
    filters: list[str] = []
    final_hold = float(output.get("finalHoldSeconds", 0))
    if final_hold:
        filters.append(f"tpad=stop_mode=clone:stop_duration={final_hold}")
    fade_in = float(output.get("fadeInSeconds", 0))
    if fade_in:
        filters.append(f"fade=t=in:st=0:d={fade_in}")
    fade_out = float(output.get("fadeOutSeconds", 0))
    expected_frame_count = frame_count + round(final_hold * frame_rate)
    expected_duration = expected_frame_count / frame_rate
    if fade_out:
        filters.append(
            f"fade=t=out:st={max(0.0, expected_duration - fade_out)}:d={fade_out}"
        )
    if filters:
        args.extend(["-vf", ",".join(filters)])
    args.extend(_codec_arguments(output.get("codec", "h264"), output))
    color = output.get("colorMetadata", {})
    mapping = {
        "primaries": "-color_primaries",
        "transfer": "-color_trc",
        "space": "-colorspace",
        "range": "-color_range",
    }
    for key, flag in mapping.items():
        if color.get(key):
            args.extend([flag, str(color[key])])
    if audio:
        args.extend(["-c:a", "aac", "-b:a", "192k", "-t", f"{expected_duration:.9f}"])
    if destination.suffix.lower() in {".mp4", ".mov"}:
        args.extend(["-movflags", "+faststart"])
    args.append(str(temporary))
    completed = supervisor.run(
        args,
        cwd=destination.parent,
        log_path=log_root / f"encode-{operation_id}.log",
        timeout_seconds=expected.get("timeoutSeconds"),
    )
    if completed.interrupted:
        temporary.unlink(missing_ok=True)
        raise BlendError(
            code="ENCODE_INTERRUPTED",
            category=ErrorCategory.INTERRUPTED,
            message="Encoding was interrupted; validated source frames were retained.",
            remediation="Run blend encode again; Blender frames do not need rerendering.",
            retained_artifacts=[str(completed.log_path)],
            resume_safe=True,
        )
    if completed.timed_out:
        temporary.unlink(missing_ok=True)
        raise BlendError(
            code="ENCODE_TIMEOUT",
            category=ErrorCategory.RESOURCE,
            message=f"Encoding exceeded its declared timeout for {destination.name}.",
            remediation="Raise the reviewed timeout or retry the same frames on a capable host.",
            retained_artifacts=[str(completed.log_path)],
            resume_safe=True,
        )
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise BlendError(
            code="ENCODE_FFMPEG_FAILED",
            category=ErrorCategory.ENCODING,
            message=f"FFmpeg failed to encode {destination.name}.",
            remediation="Inspect the retained FFmpeg log and verify codec, dimensions, and frame sequence.",
            details={"log": str(completed.log_path), "logTail": completed.stdout[-8000:]},
            retained_artifacts=[str(completed.log_path)],
        )
    staged_report: Path | None = None
    try:
        probe = probe_media(
            supervisor,
            temporary,
            log_root=log_root,
            operation_id=operation_id,
        )
        failures: list[dict[str, Any]] = []
        for key in ("width", "height"):
            if expected.get(key) is not None and probe.get(key) != expected[key]:
                failures.append({
                    "field": key,
                    "expected": expected[key],
                    "actual": probe.get(key),
                })
        if (
            expected.get("frameRate") is not None
            and abs(probe.get("frameRate", 0) - expected["frameRate"]) > 0.01
        ):
            failures.append({
                "field": "frameRate",
                "expected": expected["frameRate"],
                "actual": probe.get("frameRate"),
            })
        if probe.get("frameCount") is not None and probe["frameCount"] != expected_frame_count:
            failures.append({
                "field": "frameCount",
                "expected": expected_frame_count,
                "actual": probe["frameCount"],
            })
        if (
            probe.get("durationSeconds") is not None
            and abs(probe["durationSeconds"] - expected_duration) > (1 / frame_rate + 0.02)
        ):
            failures.append({
                "field": "durationSeconds",
                "expected": expected_duration,
                "actual": probe["durationSeconds"],
            })
        codec_names = {
            "h264": "h264",
            "hevc": "hevc",
            "prores": "prores",
            "prores-alpha": "prores",
            "ffv1": "ffv1",
        }
        expected_codec = codec_names.get(output.get("codec", "h264"))
        if expected_codec and probe.get("codec") != expected_codec:
            failures.append({
                "field": "codec",
                "expected": expected_codec,
                "actual": probe.get("codec"),
            })
        declared_pixel_format = output.get("pixelFormat")
        if declared_pixel_format and probe.get("pixelFormat") != declared_pixel_format:
            failures.append({
                "field": "pixelFormat",
                "expected": declared_pixel_format,
                "actual": probe.get("pixelFormat"),
            })
        for key, expected_value in output.get("colorMetadata", {}).items():
            if expected_value and probe.get("color", {}).get(key) != expected_value:
                failures.append({
                    "field": f"color.{key}",
                    "expected": expected_value,
                    "actual": probe.get("color", {}).get(key),
                })
        if output.get("alpha") and not probe.get("alpha"):
            failures.append({
                "field": "alpha",
                "expected": True,
                "actual": probe.get("alpha"),
            })
        if output.get("audio") and not probe.get("audio"):
            failures.append({"field": "audio", "expected": True, "actual": False})
        if failures:
            raise BlendError(
                code="ENCODE_MEDIA_VALIDATION_FAILED",
                category=ErrorCategory.ENCODING,
                message=f"Encoded media failed validation: {destination}",
                remediation="Correct the encoding profile and encode again from the same frames.",
                details={"failures": failures, "probe": probe},
                retained_artifacts=[str(completed.log_path), str(probe.get("log"))],
            )
        probe["path"] = str(destination)
        qc_expected = {
            **expected,
            "frameCount": expected_frame_count,
            "durationSeconds": expected_duration,
            "codec": expected_codec,
        }
        report_path = destination.with_suffix(destination.suffix + ".media.json")
        staged_report = report_path.with_name(f".{report_path.name}.{operation_id}.part")
        atomic_write_json(
            staged_report,
            {
                "schema": 1,
                "expected": qc_expected,
                "probe": probe,
                "failures": [],
            },
        )
        report_path.unlink(missing_ok=True)
        os.replace(temporary, destination)
        os.replace(staged_report, report_path)
        return {
            "path": str(destination),
            "probe": probe,
            "validation": str(report_path),
            "log": str(completed.log_path),
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        if staged_report is not None:
            staged_report.unlink(missing_ok=True)
        raise
