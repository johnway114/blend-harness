"""Blender, FFmpeg, device, font, filesystem, and compatibility discovery."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import BlendError, ErrorCategory, require
from .process import ProcessSupervisor, offline_capability
from .project import Project, version_tuple
from . import RUNTIME_VERSION, SCHEMA_VERSION, __version__
from .util import available_bytes, host_facts


_VERSION_OUTPUT_RE = re.compile(r"Blender\s+(\d+\.\d+(?:\.\d+)?)")
_CAPABILITY_SENTINEL = "BLEND_CAPABILITIES="


def find_blender(project: Project | None = None, explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    configured = project.config.get("blender", {}).get("executable") if project else None
    for value in (explicit, os.environ.get("BLENDER_BIN"), configured, shutil.which("blender")):
        if value:
            candidates.append(Path(value).expanduser())
    candidates.extend([
        Path("/Applications/Blender.app/Contents/MacOS/Blender"),
        Path.home() / "Applications/Blender.app/Contents/MacOS/Blender",
        Path("/snap/bin/blender"),
        Path("/usr/bin/blender"),
        Path("/usr/local/bin/blender"),
    ])
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise BlendError(
        code="BLENDER_EXECUTABLE_NOT_FOUND",
        category=ErrorCategory.BLENDER_DEPENDENCY,
        message="No Blender executable was found.",
        remediation="Install a supported Blender release or set BLENDER_BIN to its executable.",
        details={"searched": [str(path) for path in candidates]},
    )


def find_ffmpeg() -> Path:
    executable = shutil.which("ffmpeg")
    require(
        bool(executable),
        code="FFMPEG_EXECUTABLE_NOT_FOUND",
        category=ErrorCategory.BLENDER_DEPENDENCY,
        message="FFmpeg is not installed or not on PATH.",
        remediation="Install FFmpeg with the codecs required by the selected output.",
    )
    return Path(str(executable)).resolve()


def find_ffprobe() -> Path:
    executable = shutil.which("ffprobe")
    require(
        bool(executable),
        code="FFPROBE_EXECUTABLE_NOT_FOUND",
        category=ErrorCategory.BLENDER_DEPENDENCY,
        message="FFprobe is not installed or not on PATH.",
        remediation="Install FFmpeg, which normally supplies FFprobe.",
    )
    return Path(str(executable)).resolve()


def blender_version(supervisor: ProcessSupervisor, executable: Path, log_root: Path) -> str:
    process = supervisor.run([str(executable), "--version"], cwd=Path.cwd(),
                             log_path=log_root / "blender-version.log", timeout_seconds=20)
    match = _VERSION_OUTPUT_RE.search(process.stdout)
    if process.returncode != 0 or not match:
        raise BlendError(
            code="BLENDER_VERSION_UNREADABLE",
            category=ErrorCategory.BLENDER_DEPENDENCY,
            message=f"Could not read Blender version from {executable}.",
            remediation="Run the executable manually and verify that it is a supported Blender distribution.",
            details={"log": str(process.log_path), "logTail": process.stdout[-4000:]},
            blender_exit_code=process.returncode,
        )
    return match.group(1)


def ensure_blender_compatible(project: Project, version: str) -> None:
    installed = version_tuple(version)
    minimum = version_tuple(project.config["blender"]["minimumVersion"])
    maximum_value = project.config["blender"].get("maximumVersionExclusive")
    maximum = version_tuple(maximum_value) if maximum_value else None
    if installed < minimum or (maximum and installed >= maximum):
        raise BlendError(
            code="BLENDER_VERSION_INCOMPATIBLE",
            category=ErrorCategory.BLENDER_DEPENDENCY,
            message=f"Blender {version} is outside the project's supported range.",
            remediation=f"Install Blender >= {project.config['blender']['minimumVersion']}" +
                        (f" and < {maximum_value}." if maximum_value else "."),
            details={"installed": version, "minimum": project.config["blender"]["minimumVersion"],
                     "maximumExclusive": maximum_value},
        )


def _blender_capability_expression() -> str:
    return r'''import bpy, json, platform
scene=bpy.context.scene
original_engine=scene.render.engine
engines=[]
for candidate in ('BLENDER_EEVEE','BLENDER_EEVEE_NEXT','BLENDER_WORKBENCH','CYCLES'):
    try:
        scene.render.engine=candidate
        engines.append(candidate)
    except Exception:
        pass
try:
    scene.render.engine=original_engine
except Exception:
    pass
devices=[]
try:
    prefs=bpy.context.preferences.addons['cycles'].preferences
    prefs.refresh_devices()
    devices=[{'name':d.name,'type':d.type,'id':d.id,'use':bool(d.use)} for d in prefs.devices]
except Exception:
    pass
views=[]
looks=[]
try:
    views=list(scene.view_settings.bl_rna.properties['view_transform'].enum_items.keys())
    looks=list(scene.view_settings.bl_rna.properties['look'].enum_items.keys())
except Exception:
    pass
result={'version':bpy.app.version_string,'versionTuple':list(bpy.app.version),'python':platform.python_version(),'engines':engines,'cyclesDevices':devices,'colorManagement':{'views':views,'looks':looks},'background':bpy.app.background}
print('BLEND_CAPABILITIES='+json.dumps(result,sort_keys=True))'''


def blender_capabilities(supervisor: ProcessSupervisor, executable: Path, log_root: Path,
                         *, offline: bool) -> dict[str, Any]:
    process = supervisor.run(
        [str(executable), "--background", "--factory-startup", "--python-exit-code", "74",
         "--python-expr", _blender_capability_expression()],
        cwd=log_root,
        log_path=log_root / "blender-capabilities.log",
        timeout_seconds=60,
        offline=offline,
        enforce_offline=offline,
    )
    payload = None
    for line in process.stdout.splitlines():
        if line.startswith(_CAPABILITY_SENTINEL):
            payload = line[len(_CAPABILITY_SENTINEL):]
    if process.returncode != 0 or not payload:
        raise BlendError(
            code="BLENDER_CAPABILITY_PROBE_FAILED",
            category=ErrorCategory.BLENDER_DEPENDENCY,
            message="Blender failed its background Python capability probe.",
            remediation="Inspect the retained probe log and verify factory-startup background execution.",
            details={"log": str(process.log_path), "logTail": process.stdout[-4000:]},
            blender_exit_code=process.returncode,
        )
    return json.loads(payload)


def ffmpeg_capabilities(supervisor: ProcessSupervisor, log_root: Path) -> dict[str, Any]:
    executable = find_ffmpeg()
    version_process = supervisor.run([str(executable), "-version"], cwd=log_root,
                                     log_path=log_root / "ffmpeg-version.log", timeout_seconds=20)
    encoder_process = supervisor.run([str(executable), "-hide_banner", "-encoders"], cwd=log_root,
                                     log_path=log_root / "ffmpeg-encoders.log", timeout_seconds=20)
    version_line = version_process.stdout.splitlines()[0] if version_process.stdout else ""
    encoders = set(re.findall(r"^\s*[A-Z.]{6}\s+([\w-]+)\s", encoder_process.stdout, re.MULTILINE))
    relevant = {
        "h264": sorted(encoders & {"libx264", "h264_videotoolbox", "h264_nvenc"}),
        "hevc": sorted(encoders & {"libx265", "hevc_videotoolbox", "hevc_nvenc"}),
        "prores": sorted(encoders & {"prores_ks", "prores_aw", "prores_videotoolbox"}),
        "alpha": sorted(encoders & {"ffv1", "prores_ks", "qtrle"}),
    }
    return {
        "available": version_process.returncode == 0,
        "path": str(executable),
        "version": version_line,
        "ffprobe": str(find_ffprobe()),
        "codecs": relevant,
    }


def compatibility_for(version: str) -> dict[str, Any] | None:
    matrix = json.loads(files("blend_harness").joinpath("compatibility.json").read_text(encoding="utf-8"))
    installed = version_tuple(version)
    for line in matrix["lines"]:
        if version_tuple(line["minimum"]) <= installed < version_tuple(line["maximumExclusive"]):
            return line
    return None


def doctor_report(supervisor: ProcessSupervisor, *, project: Project | None = None,
                  explicit_blender: str | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="blend-doctor-") as temporary:
        log_root = Path(temporary)
        blender = find_blender(project, explicit_blender)
        version = blender_version(supervisor, blender, log_root)
        if project:
            ensure_blender_compatible(project, version)
        offline = offline_capability()
        blender_info = blender_capabilities(supervisor, blender, log_root, offline=bool(offline["available"]))
        ffmpeg = ffmpeg_capabilities(supervisor, log_root)
    requested_fonts: list[dict[str, Any]] = []
    if project:
        for asset in project.assets:
            if asset.type == "font":
                requested_fonts.append({
                    "id": asset.id,
                    "path": str(asset.path),
                    "available": asset.path.is_file() and asset.path.stat().st_size > 0,
                    "checksumMatches": asset.checksum is None or asset.checksum == asset.actual_checksum,
                    "licenseMetadata": asset.declared.get("fontLicenseMetadata"),
                })
    workspace = project.paths.root if project else Path.cwd()
    temporary_root = project.paths.temporary if project else Path(tempfile.gettempdir())
    optional_tools = {
        name: shutil.which(name)
        for name in ("oiiotool", "gltf-validator", "usdcat", "assimp", "bwrap", "unshare")
    }
    warnings: list[dict[str, Any]] = []
    compatibility = compatibility_for(version)
    if not compatibility:
        warnings.append({"code": "BLENDER_VERSION_UNTESTED", "message": f"Blender {version} is not in the maintained matrix."})
    if not offline["available"]:
        warnings.append({"code": "SECURITY_OFFLINE_UNAVAILABLE", "message": offline["claim"]})
    if not ffmpeg["codecs"]["h264"]:
        warnings.append({"code": "FFMPEG_H264_ENCODER_MISSING", "message": "No supported H.264 encoder is available."})
    for font in requested_fonts:
        if not font["available"]:
            warnings.append({"code": "ASSET_FONT_MISSING", "message": f"Requested font is missing: {font['id']}"})
    return {
        "schema": 1,
        "host": host_facts(),
        "blend": {
            "version": __version__,
            "schema": SCHEMA_VERSION,
            "runtimeVersion": RUNTIME_VERSION,
            "supportedSchemas": {
                "configuration": [1],
                "brief": [1],
                "manifest": [1],
                "commandResult": [1],
            },
            "compatibility": compatibility,
        },
        "blender": {
            "path": str(blender),
            "version": version,
            "pythonApi": blender_info,
            "engines": blender_info.get("engines", []),
            "cyclesDevices": blender_info.get("cyclesDevices", []),
            "colorManagement": blender_info.get("colorManagement", {}),
        },
        "ffmpeg": ffmpeg,
        "workspace": {
            "path": str(workspace.resolve()),
            "writable": os.access(workspace, os.W_OK),
            "availableBytes": available_bytes(workspace),
            "declaredDiskBudget": (
                project.config.get("resources", {}).get("maxDiskBytes")
                if project
                else None
            ),
        },
        "temporary": {
            "path": str(temporary_root.resolve()),
            "writable": os.access(temporary_root if temporary_root.exists() else temporary_root.parent, os.W_OK),
            "availableBytes": available_bytes(temporary_root if temporary_root.exists() else temporary_root.parent),
        },
        "fonts": requested_fonts,
        "offline": offline,
        "optionalTools": optional_tools,
        "configurationErrors": [],
        "warnings": warnings,
    }
