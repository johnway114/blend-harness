"""Dependency-free MCP stdio server mapping typed tools to the authoritative JSON CLI."""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from .errors import BlendError
from .project import load_project


PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "blend", "version": "1.0.0"}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _project_properties(*, execution: bool = False, profile: bool = False,
                        variant: bool = False, operation_id: bool = False) -> tuple[dict[str, Any], list[str]]:
    properties: dict[str, Any] = {"project": {"type": "string", "description": "Absolute project directory"}}
    required = ["project"]
    if execution:
        properties.update({
            "trust": {"type": "boolean", "default": False},
            "allowNetwork": {"type": "boolean", "default": False},
            "blender": {"type": "string"},
            "timeout": {"type": "number", "exclusiveMinimum": 0},
        })
    if profile:
        properties["profile"] = {"type": "string"}
    if variant:
        properties["variant"] = {"type": "string"}
    if operation_id:
        properties["operationId"] = {"type": "string", "minLength": 1,
                                     "description": "Required idempotency and progress identifier"}
        required.append("operationId")
    return properties, required


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    argv: Callable[[dict[str, Any]], list[str]]
    mutation: bool = False

    def declaration(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


def _append_common(argv: list[str], arguments: dict[str, Any]) -> list[str]:
    mapping = {
        "profile": "--profile", "variant": "--variant", "blender": "--blender",
        "timeout": "--timeout", "operationId": "--operation-id", "output": "--output",
        "matrix": "--matrix", "view": "--view", "mode": "--mode", "simulation": "--simulation",
    }
    for key, flag in mapping.items():
        if arguments.get(key) is not None:
            argv.extend([flag, str(arguments[key])])
    if arguments.get("trust"):
        argv.append("--trust")
    if arguments.get("allowNetwork"):
        argv.append("--allow-network")
    return argv


def _project_tool(name: str, description: str, command: str, *, execution: bool = False,
                  profile: bool = False, variant: bool = False, operation_id: bool = False,
                  extras: dict[str, Any] | None = None, required_extras: list[str] | None = None,
                  mutation: bool = False) -> Tool:
    properties, required = _project_properties(execution=execution, profile=profile, variant=variant,
                                                operation_id=operation_id)
    properties.update(extras or {})
    required.extend(required_extras or [])
    return Tool(
        name,
        description,
        _object(properties, required),
        lambda arguments: _append_common([command, arguments["project"]], arguments),
        mutation=mutation,
    )


TOOLS: dict[str, Tool] = {}
for tool in [
    Tool("blend_doctor", "Report Blender, Python API, engines, devices, FFmpeg, fonts, roots, offline enforcement, and compatibility.",
         _object({"project": {"type": "string"}, "blender": {"type": "string"}}),
         lambda a: _append_common(["doctor", *([a["project"]] if a.get("project") else [])], a)),
    Tool("blend_init", "Initialize a complete versioned project from a built-in template.",
         _object({"template": {"enum": ["brand-ident", "product-turntable", "procedural-explainer", "empty"]},
                  "directory": {"type": "string"}, "operationId": {"type": "string", "minLength": 1}},
                 ["template", "directory", "operationId"]),
         lambda a: ["init", a["template"], a["directory"], "--operation-id", a["operationId"]], mutation=True),
    _project_tool("blend_validate_config", "Validate configuration, brief, paths, assets, and declared policies without Blender.",
                  "validate-config", profile=True, variant=True),
    _project_tool("blend_build", "Execute authoritative scene source in background Blender and save a generated checkpoint.",
                  "build", execution=True, operation_id=True, mutation=True),
    _project_tool("blend_preview", "Render declared cameras, generated views, diagnostic modes, sample frames, and a contact sheet.",
                  "preview", execution=True, profile=True, variant=True, operation_id=True,
                  extras={"view": {"type": "string"}, "mode": {"enum": ["material", "clay", "depth", "normal", "object-index", "wireframe", "alpha"]},
                          "frames": {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}}, mutation=True),
    _project_tool("blend_contact_sheet", "Regenerate a labeled contact sheet from retained preview evidence.",
                  "contact-sheet", operation_id=True, mutation=True),
    _project_tool("blend_inspect", "Return filtered evaluated-scene facts while retaining the complete inspection artifact.",
                  "inspect", execution=True, profile=True, variant=True, operation_id=True,
                  extras={"object": {"type": "string"}, "collection": {"type": "string"},
                          "dependency": {"type": "string"}, "view": {"type": "string"}, "finding": {"type": "string"}}, mutation=True),
    _project_tool("blend_validate", "Run complete conservative mechanical validation with stable rule identifiers.",
                  "validate", execution=True, profile=True, variant=True, operation_id=True, mutation=True),
    _project_tool("blend_plan", "Resolve the complete graph, matrix, assets, frames, storage, devices, caches, and blockers.",
                  "plan", profile=True, variant=True,
                  extras={"target": {"enum": ["build", "preview", "inspect", "validate", "render", "resume", "encode", "export", "compare", "review"]},
                          "matrix": {"type": "string"}, "output": {"type": "string"}, "blender": {"type": "string"}}),
    _project_tool("blend_render", "Render an atomic restartable image sequence under local resource ceilings.",
                  "render", execution=True, profile=True, variant=True, operation_id=True,
                  extras={"frames": {"type": "array", "items": {"type": "integer"}, "uniqueItems": True},
                          "matrix": {"type": "string"}, "jobs": {"type": "integer", "minimum": 1}}, mutation=True),
    _project_tool("blend_resume", "Validate planned frames and render only missing, corrupt, stale, or invalid units.",
                  "resume", execution=True, profile=True, variant=True, operation_id=True,
                  extras={"frames": {"type": "array", "items": {"type": "integer"}, "uniqueItems": True},
                          "matrix": {"type": "string"}, "jobs": {"type": "integer", "minimum": 1}}, mutation=True),
    _project_tool("blend_encode", "Encode and validate declared delivery media from retained frames without Blender rerendering.",
                  "encode", profile=True, variant=True, operation_id=True,
                  extras={"output": {"type": "string"}}, mutation=True),
    _project_tool("blend_export", "Produce selection-safe declared model exports and validate them in clean Blender processes.",
                  "export", execution=True, profile=True, variant=True, operation_id=True,
                  extras={"output": {"type": "string"}}, mutation=True),
    _project_tool("blend_bake", "Bake declared simulation caches with dependency and interruption manifests.",
                  "bake", execution=True, profile=True, operation_id=True,
                  extras={"simulation": {"type": "string"}}, mutation=True),
    _project_tool("blend_cache_inspect", "Inspect declared simulation cache completeness and staleness.",
                  "cache", profile=True, extras={"simulation": {"type": "string"}}),
    Tool("blend_compare", "Generate image heatmaps, side-by-side evidence, and structural, camera, source, asset, and manifest differences.",
         _object({"project": {"type": "string"}, "left": {"type": "string"}, "right": {"type": "string"},
                  "artifactRoot": {"type": "string"}, "operationId": {"type": "string", "minLength": 1}},
                 ["project", "left", "right", "operationId"]),
         lambda a: ["compare", a["left"], a["right"], "--operation-id", a["operationId"],
                    *(["--artifact-root", a["artifactRoot"]] if a.get("artifactRoot") else [])], mutation=True),
    _project_tool("blend_review", "Generate a portable self-contained static review package with immutable checksums.",
                  "review", operation_id=True, extras={"output": {"type": "string"}}, mutation=True),
    _project_tool("blend_search", "Evaluate and mechanically rank a declared bounded parameter search.",
                  "search", execution=True, operation_id=True,
                  extras={"searchId": {"type": "string", "minLength": 1}},
                  required_extras=["searchId"], mutation=True),
    Tool("blend_artifact", "Retrieve bounded generated-artifact bytes or UTF-8 text by explicit path.",
         _object({"project": {"type": "string"}, "path": {"type": "string"},
                  "maxBytes": {"type": "integer", "minimum": 1, "maximum": 1048576}},
                 ["project", "path"]),
         lambda a: []),
]:
    TOOLS[tool.name] = tool


def _bound_list(container: dict[str, Any], key: str, limit: int) -> None:
    values = container.get(key)
    if not isinstance(values, list) or len(values) <= limit:
        return
    container[f"{key}Count"] = len(values)
    container[key] = values[:limit]
    container[f"{key}Truncated"] = True


def _bounded_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    bounded = json.loads(json.dumps(result))
    _bound_list(bounded, "artifacts", 100)
    data = bounded.get("data", {})
    if tool_name == "blend_inspect" and isinstance(data.get("inspection"), dict):
        inspection = data["inspection"]
        data["inspection"] = {
            "schema": inspection.get("schema"),
            "scene": inspection.get("scene"),
            "statistics": inspection.get("statistics"),
            "render": inspection.get("render"),
            "colorManagement": inspection.get("colorManagement"),
            "estimatedOutput": inspection.get("estimatedOutput"),
            "counts": {
                key: len(inspection.get(key, []))
                for key in (
                    "scenes", "collections", "objects", "cameras", "lights",
                    "materials", "images", "fonts", "animation", "drivers",
                    "constraints", "simulations", "dependencies", "framing",
                )
            },
            "filteredEvidence": {
                key: value[:50]
                for key, value in inspection.items()
                if key in {"objects", "collections", "dependencies", "framing", "findings"}
                and isinstance(value, list)
            },
            "completeArtifact": data.get("completeArtifact"),
        }
    if tool_name == "blend_preview" and isinstance(data.get("previews"), list):
        previews = data["previews"]
        data["previewCount"] = len(previews)
        data["previews"] = previews[:32]
        if len(previews) > 32:
            data["previewEvidenceTruncated"] = True
    if tool_name in {"blend_render", "blend_resume"}:
        for key in ("expectedFrames", "renderedFrames", "reusedFrames"):
            values = data.get(key)
            if isinstance(values, list) and len(values) > 64:
                data[f"{key}Count"] = len(values)
                data[key] = [*values[:32], *values[-32:]]
                data[f"{key}Truncated"] = True
    if tool_name == "blend_plan":
        for key in ("commands", "dependencies", "outputs"):
            _bound_list(data, key, 100)
    if tool_name == "blend_compare":
        for key in (
            "images", "structuralChanges", "manifestChanges", "cameraAndFramingChanges",
            "renderSettingChanges", "sourceAssetAndBlenderHashChanges",
            "projectSourceChanges", "dependencyChanges", "nondeterminismChanges", "artifacts",
        ):
            _bound_list(data, key, 100)
        unmatched = data.get("unmatchedImages")
        if isinstance(unmatched, dict):
            _bound_list(unmatched, "leftOnly", 100)
            _bound_list(unmatched, "rightOnly", 100)
    if tool_name == "blend_search" and isinstance(data.get("search"), dict):
        _bound_list(data["search"], "candidates", 100)
        _bound_list(data["search"], "ranking", 100)
    if tool_name in {"blend_export", "blend_encode"}:
        _bound_list(data, "exports" if tool_name == "blend_export" else "outputs", 100)
    if tool_name == "blend_cache_inspect":
        _bound_list(data, "caches", 100)
    validation_reports = []
    if tool_name == "blend_validate_config":
        validation_reports.append(data)
    if tool_name == "blend_validate" and isinstance(data.get("report"), dict):
        validation_reports.append(data["report"])
    error_report = (bounded.get("error") or {}).get("details", {}).get("report")
    if isinstance(error_report, dict):
        validation_reports.append(error_report)
    for report in validation_reports:
        _bound_list(report, "findings", 100)
    return bounded



def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


class Server:
    def __init__(self) -> None:
        self._write_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._processes: dict[Any, subprocess.Popen[str]] = {}
        self._threads: set[threading.Thread] = set()
        self._closing = threading.Event()

    def send(self, value: dict[str, Any]) -> None:
        with self._write_lock:
            sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def response(self, request_id: Any, result: Any) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def cancel(self, request_id: Any) -> None:
        with self._process_lock:
            process = self._processes.get(request_id)
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method in {"notifications/cancelled", "$/cancelRequest"}:
            self.cancel(params.get("requestId"))
            return
        if method in {"notifications/initialized", "notifications/progress"}:
            return
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.response(request_id, {
                "protocolVersion": requested if requested in {PROTOCOL_VERSION, "2025-03-26", "2024-11-05"} else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": "Use explicit absolute project paths and operationId values. Project Python requires trust; network is denied unless both project and request permit it.",
            })
            return
        if method == "ping":
            self.response(request_id, {})
            return
        if method == "tools/list":
            self.response(request_id, {"tools": [tool.declaration() for tool in TOOLS.values()]})
            return
        if method == "tools/call":
            name = params.get("name")
            tool = TOOLS.get(name)
            if tool is None:
                self.error(request_id, -32602, f"Unknown tool {name!r}")
                return
            arguments = params.get("arguments") or {}
            validation_errors = sorted(
                Draft202012Validator(tool.input_schema).iter_errors(arguments),
                key=lambda error: list(error.absolute_path),
            )
            if validation_errors:
                self.error(request_id, -32602, "Invalid tool arguments", {
                    "errors": [
                        {
                            "path": "/" + "/".join(str(part) for part in error.absolute_path),
                            "message": error.message,
                        }
                        for error in validation_errors
                    ]
                })
                return
            if name == "blend_artifact":
                self._artifact(request_id, arguments)
                return
            thread = threading.Thread(target=self._run_tool, args=(request_id, tool, arguments, params.get("_meta") or {}),
                                      daemon=False, name=f"blend-mcp-{name}")
            self._threads.add(thread)
            thread.start()
            return
        if method == "shutdown":
            self.response(request_id, {})
            self._closing.set()
            return
        if method == "exit":
            self._closing.set()
            return
        if request_id is not None:
            self.error(request_id, -32601, f"Method not found: {method}")

    def _artifact(self, request_id: Any, arguments: dict[str, Any]) -> None:
        try:
            project = load_project(arguments["project"])
        except BlendError as exc:
            self.error(request_id, -32602, exc.message, exc.as_dict())
            return
        path = Path(arguments["path"]).expanduser().resolve()
        allowed_roots = (
            project.paths.working,
            project.paths.previews,
            project.paths.renders,
            project.paths.outputs,
        )
        if not any(_within(path, root) for root in allowed_roots):
            self.error(request_id, -32602, "Artifact path is outside declared generated roots", {
                "path": str(path),
                "roots": [str(root) for root in allowed_roots],
            })
            return
        maximum = min(int(arguments.get("maxBytes", 262144)), 1048576)
        if not path.is_file():
            self.error(request_id, -32602, f"Artifact does not exist: {path}")
            return
        size = path.stat().st_size
        if size > maximum:
            self.error(request_id, -32602, f"Artifact is {size} bytes; maxBytes is {maximum}",
                       {"path": str(path), "bytes": size, "maxBytes": maximum})
            return
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
            content = {"type": "text", "text": text}
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = {"type": "text", "text": base64.b64encode(raw).decode("ascii")}
            encoding = "base64"
        self.response(request_id, {"content": [content], "structuredContent": {
            "path": str(path), "bytes": size, "encoding": encoding,
        }})

    def _run_tool(self, request_id: Any, tool: Tool, arguments: dict[str, Any], meta: dict[str, Any]) -> None:
        try:
            argv = tool.argv(arguments)
            if tool.name == "blend_preview" and arguments.get("frames"):
                argv.extend(["--frames", ",".join(str(frame) for frame in arguments["frames"])])
            if tool.name in {"blend_render", "blend_resume"}:
                if arguments.get("frames"):
                    argv.extend(["--frames", ",".join(str(frame) for frame in arguments["frames"])])
                if arguments.get("jobs"):
                    argv.extend(["--jobs", str(arguments["jobs"])])
            if tool.name == "blend_plan" and arguments.get("target"):
                argv.extend(["--target", arguments["target"]])
            if tool.name == "blend_inspect":
                for key in ("object", "collection", "dependency", "view", "finding"):
                    if arguments.get(key):
                        argv.extend([f"--{key}", arguments[key]])
            if tool.name == "blend_search":
                argv.insert(2, arguments["searchId"])
            if tool.name == "blend_cache_inspect":
                argv = ["cache", "inspect", arguments["project"]]
                if arguments.get("simulation"):
                    argv.extend(["--simulation", arguments["simulation"]])
                if arguments.get("profile"):
                    argv.extend(["--profile", arguments["profile"]])
            argv.append("--json")
            process = subprocess.Popen(
                [sys.executable, "-m", "blend_harness.cli", *argv],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with self._process_lock:
                self._processes[request_id] = process
            progress_token = meta.get("progressToken")
            last_progress = None
            operation_id = arguments.get("operationId")
            project = Path(arguments["project"]).expanduser().resolve() if arguments.get("project") else None
            manifest = None
            if operation_id and project:
                try:
                    manifest = load_project(project).paths.artifacts / f"{operation_id}.json"
                except BlendError:
                    manifest = project / "build" / "manifests" / f"{operation_id}.json"
            while process.poll() is None:
                if self._closing.wait(0.25):
                    self.cancel(request_id)
                if progress_token is not None and manifest is not None:
                    try:
                        value = json.loads(manifest.read_text(encoding="utf-8"))
                        progress = len(value.get("completedFrames", []))
                        total = len(value.get("expectedFrames", []))
                        marker = (progress, total, value.get("status"))
                        if marker != last_progress:
                            self.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {
                                "progressToken": progress_token, "progress": progress, "total": total or None,
                                "message": f"{tool.name}: {value.get('status')} {progress}/{total or '?'}",
                            }})
                            last_progress = marker
                    except (OSError, json.JSONDecodeError):
                        pass
            stdout, stderr = process.communicate()
            with self._process_lock:
                self._processes.pop(request_id, None)
            try:
                result = json.loads(stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                self.error(request_id, -32603, "Blend CLI did not return valid JSON",
                           {"exitCode": process.returncode, "stderr": stderr[-4000:], "stdout": stdout[-4000:]})
                return
            result = _bounded_result(tool.name, result)
            self.response(request_id, {
                "content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}],
                "structuredContent": result,
                "isError": process.returncode != 0,
            })
        except KeyError as exc:
            self.error(request_id, -32602, f"Missing required argument: {exc.args[0]}")
        except Exception as exc:
            self.error(request_id, -32603, f"Tool execution failed: {exc}", {"exception": type(exc).__name__})
        finally:
            with self._process_lock:
                self._processes.pop(request_id, None)
            self._threads.discard(threading.current_thread())

    def close(self) -> None:
        self._closing.set()
        with self._process_lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        for process in processes:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for thread in list(self._threads):
            thread.join(timeout=1)

    def serve(self) -> int:
        try:
            for line in sys.stdin:
                if self._closing.is_set():
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.error(None, -32700, f"Parse error: {exc}")
                    continue
                self.handle(message)
        finally:
            self.close()
        return 0


def main() -> int:
    return Server().serve()


if __name__ == "__main__":
    raise SystemExit(main())
