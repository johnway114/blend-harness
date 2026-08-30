"""Blend command-line interface. JSON results are authoritative."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from .errors import BlendError, ErrorCategory
from .manifests import CommandResult, new_operation_id
from .process import ProcessSupervisor
from . import operations


COMMANDS = (
    "doctor", "init", "validate-config", "migrate", "trust", "build", "preview",
    "contact-sheet", "inspect", "validate", "plan", "render", "resume", "encode",
    "export", "compare", "clean", "bake", "cache", "review", "search", "promote",
    "library", "template-upgrade", "rules", "mcp",
)


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


class BlendArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BlendError(
            code="CLI_ARGUMENT_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=message,
            operation="cli",
            remediation=f"Run {self.prog} --help and correct the command arguments.",
        )


def _frames(value: str) -> list[int]:
    result: set[int] = set()
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            start_text, end_text = chunk.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise argparse.ArgumentTypeError(f"Reversed frame range: {chunk}")
            result.update(range(start, end + 1))
        else:
            result.add(int(chunk))
    if not result:
        raise argparse.ArgumentTypeError("Frame selection is empty")
    return sorted(result)


def _execution_options(parser: argparse.ArgumentParser, *, profile: bool = False,
                       variant: bool = False) -> None:
    parser.add_argument("--trust", action="store_true", help="Trust project Python for this command")
    parser.add_argument("--allow-network", action="store_true", help="Permit network only when project also declares it")
    parser.add_argument("--blender", help="Explicit Blender executable")
    parser.add_argument("--timeout", type=float, help="Owned process timeout in seconds")
    parser.add_argument("--operation-id", help="Explicit idempotency and progress identifier")
    if profile:
        parser.add_argument("--profile")
    if variant:
        parser.add_argument("--variant")


def parser() -> argparse.ArgumentParser:
    root = BlendArgumentParser(prog="blend", description="Agent-native source-controlled Blender harness")
    root.add_argument("--version", action="version", version="Blend 1.0.0")
    sub = root.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Report host, Blender, FFmpeg, and project capabilities")
    doctor.add_argument("project", nargs="?", type=_path)
    doctor.add_argument("--blender")
    doctor.add_argument("--operation-id")

    init = sub.add_parser("init", help="Create a versioned project from a built-in template")
    init.add_argument("template", choices=("brand-ident", "product-turntable", "procedural-explainer", "empty"))
    init.add_argument("directory", type=_path)
    init.add_argument("--operation-id")

    validate_config = sub.add_parser("validate-config", help="Validate configuration, brief, paths, and declared inputs")
    validate_config.add_argument("project", type=_path)
    validate_config.add_argument("--profile")
    validate_config.add_argument("--variant")
    validate_config.add_argument("--operation-id")

    migrate = sub.add_parser("migrate", help="Preview or write a supported schema migration")
    migrate.add_argument("project", type=_path)
    migrate.add_argument("--write", action="store_true")
    migrate.add_argument("--operation-id")

    trust = sub.add_parser("trust", help="Record a local trusted-workspace decision")
    trust.add_argument("project", type=_path)
    trust.add_argument("--operation-id")

    build = sub.add_parser("build", help="Build authoritative scene source and save a checkpoint")
    build.add_argument("project", type=_path)
    _execution_options(build)

    preview = sub.add_parser("preview", help="Render low-cost declared visual evidence and a contact sheet")
    preview.add_argument("project", type=_path)
    preview.add_argument("--view")
    preview.add_argument("--mode", choices=("material", "clay", "depth", "normal", "object-index", "wireframe", "alpha"))
    preview.add_argument("--frames", type=_frames)
    _execution_options(preview, profile=True, variant=True)

    contact = sub.add_parser("contact-sheet", help="Regenerate a contact sheet from retained preview evidence")
    contact.add_argument("project", type=_path)
    contact.add_argument("--operation-id")

    inspect = sub.add_parser("inspect", help="Inspect the complete evaluated scene")
    inspect.add_argument("project", type=_path)
    inspect.add_argument("--object", dest="object_filter")
    inspect.add_argument("--collection", dest="collection_filter")
    inspect.add_argument("--dependency", dest="dependency_filter")
    inspect.add_argument("--view", dest="view_filter")
    inspect.add_argument("--finding", dest="finding_filter")
    _execution_options(inspect, profile=True, variant=True)

    validate = sub.add_parser("validate", help="Run complete mechanical preflight validation")
    validate.add_argument("project", type=_path)
    _execution_options(validate, profile=True, variant=True)

    plan = sub.add_parser("plan", help="Resolve graph, matrix, cost, devices, caches, and blockers")
    plan.add_argument("project", type=_path)
    plan.add_argument("--target", choices=("build", "preview", "inspect", "validate", "render", "resume", "encode", "export", "compare", "review"), default="render")
    plan.add_argument("--profile")
    plan.add_argument("--variant")
    plan.add_argument("--matrix")
    plan.add_argument("--output")
    plan.add_argument("--blender")
    plan.add_argument("--operation-id")

    for name in ("render", "resume"):
        render = sub.add_parser(name, help=("Render restartable image frames" if name == "render" else "Render only missing, corrupt, or stale frames"))
        render.add_argument("project", type=_path)
        render.add_argument("--frames", type=_frames)
        render.add_argument("--matrix")
        render.add_argument("--jobs", type=int)
        _execution_options(render, profile=True, variant=True)

    encode = sub.add_parser("encode", help="Encode validated frames without rerendering Blender")
    encode.add_argument("project", type=_path)
    encode.add_argument("--output")
    encode.add_argument("--profile")
    encode.add_argument("--variant")
    encode.add_argument("--operation-id")

    export = sub.add_parser("export", help="Produce and independently validate declared model exports")
    export.add_argument("project", type=_path)
    export.add_argument("--output")
    _execution_options(export, profile=True, variant=True)

    compare = sub.add_parser("compare", help="Compare images, projects, reports, profiles, or variants")
    compare.add_argument("left", type=_path)
    compare.add_argument("right", type=_path)
    compare.add_argument("--artifact-root", type=_path)
    compare.add_argument("--operation-id")

    clean = sub.add_parser("clean", help="Delete only manifest-owned generated artifacts")
    clean.add_argument("project", type=_path)
    clean_group = clean.add_mutually_exclusive_group()
    clean_group.add_argument("--generated", action="store_true", help="Keep final output")
    clean_group.add_argument("--all", action="store_true", help="Include manifest-owned final output")
    clean.add_argument("--operation-id")

    bake = sub.add_parser("bake", help="Bake declared simulation caches")
    bake.add_argument("project", type=_path)
    bake.add_argument("--simulation")
    bake.add_argument("--profile", help="Declared simulation profile identifier")
    _execution_options(bake)

    cache = sub.add_parser("cache", help="Inspect or clean declared simulation caches")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    for name in ("inspect", "clean"):
        cache_action = cache_sub.add_parser(name)
        cache_action.add_argument("project", type=_path)
        cache_action.add_argument("--simulation")
        cache_action.add_argument("--profile", help="Simulation profile to evaluate")
        cache_action.add_argument("--operation-id")

    review = sub.add_parser("review", help="Generate or record a portable static review package")
    review.add_argument("project", nargs="?", type=_path)
    review.add_argument("--output", type=_path)
    review.add_argument("--record", dest="package", type=_path, help="Record disposition in an existing package")
    review.add_argument("--decision", choices=("approved", "changes-requested", "selected", "no-decision"))
    review.add_argument("--comments", default="")
    review.add_argument("--variant")
    review.add_argument("--operation-id")

    search = sub.add_parser("search", help="Evaluate a declared bounded parameter search")
    search.add_argument("project", type=_path)
    search.add_argument("search_id")
    _execution_options(search)

    promote = sub.add_parser("promote", help="Promote a ranked candidate to source configuration")
    promote.add_argument("project", type=_path)
    promote.add_argument("search_report", type=_path)
    promote.add_argument("candidate_id")
    promote.add_argument("variant_name")
    promote.add_argument("--operation-id")

    library = sub.add_parser("library", help="Compare or explicitly update a pinned project library")
    library_sub = library.add_subparsers(dest="library_command", required=True)
    for name in ("compare", "update"):
        action = library_sub.add_parser(name)
        action.add_argument("project", type=_path)
        action.add_argument("library_id")
        action.add_argument("candidate", type=_path)
        action.add_argument("--operation-id")

    template = sub.add_parser("template-upgrade", help="Compare a project with its current built-in template")
    template.add_argument("project", type=_path)
    template.add_argument("--operation-id")

    rules = sub.add_parser("rules", help="List stable validation rules and remediation")
    rules.add_argument("--operation-id")

    mcp = sub.add_parser("mcp", help="Start the typed MCP stdio interface")
    return root


def _project_of(args: argparse.Namespace) -> Path | None:
    return getattr(args, "project", None)


def _dispatch(args: argparse.Namespace, supervisor: ProcessSupervisor, operation_id: str) -> dict[str, Any]:
    command = args.command
    if command == "doctor":
        return operations.doctor(supervisor, project_path=args.project, explicit_blender=args.blender)
    if command == "init":
        return operations.init_project(args.template, args.directory)
    if command == "validate-config":
        return operations.validate_config(args.project, profile=args.profile, variant=args.variant)
    if command == "migrate":
        return operations.migrate(args.project, write=args.write)
    if command == "trust":
        return operations.trust(args.project)
    if command == "build":
        return operations.build(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                                explicit_blender=args.blender, timeout=args.timeout, operation_id=operation_id)
    if command == "preview":
        return operations.preview(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                                  explicit_blender=args.blender, timeout=args.timeout, operation_id=operation_id,
                                  profile=args.profile, variant=args.variant, view=args.view, mode=args.mode,
                                  frame_override=args.frames)
    if command == "contact-sheet":
        return operations.contact_sheet(args.project, operation_id=operation_id)
    if command == "inspect":
        return operations.inspect(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                                  explicit_blender=args.blender, timeout=args.timeout, operation_id=operation_id,
                                  profile=args.profile, variant=args.variant, object_filter=args.object_filter,
                                  collection_filter=args.collection_filter, dependency_filter=args.dependency_filter,
                                  view_filter=args.view_filter, finding_filter=args.finding_filter)
    if command == "validate":
        return operations.validate(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                                   explicit_blender=args.blender, timeout=args.timeout, operation_id=operation_id,
                                   profile=args.profile, variant=args.variant)
    if command == "plan":
        return operations.plan(supervisor, args.project, target=args.target, profile=args.profile,
                               variant=args.variant, matrix=args.matrix, output=args.output,
                               explicit_blender=args.blender)
    if command in {"render", "resume"}:
        if args.jobs is not None and args.jobs < 1:
            raise BlendError(code="RESOURCE_CONCURRENCY_INVALID", category=ErrorCategory.CONFIGURATION,
                             message="--jobs must be at least 1.", remediation="Use a positive conservative worker count.")
        if args.matrix:
            return operations.render_matrix(supervisor, args.project, matrix=args.matrix, trust=args.trust,
                                            allow_network=args.allow_network, explicit_blender=args.blender,
                                            timeout=args.timeout, operation_id=operation_id,
                                            concurrency=args.jobs, resume_only=command == "resume")
        return operations.render(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                                 explicit_blender=args.blender, timeout=args.timeout, operation_id=operation_id,
                                 profile=args.profile, variant=args.variant, frames=args.frames,
                                 concurrency=args.jobs, resume_only=command == "resume")
    if command == "encode":
        return operations.encode(supervisor, args.project, operation_id=operation_id, output_id=args.output,
                                 profile=args.profile, variant=args.variant)
    if command == "export":
        return operations.export(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                                 explicit_blender=args.blender, timeout=args.timeout, operation_id=operation_id,
                                 output_id=args.output, profile=args.profile, variant=args.variant)
    if command == "compare":
        return operations.compare(
            supervisor,
            args.left,
            args.right,
            args.artifact_root,
            operation_id,
        )
    if command == "clean":
        return operations.clean(args.project, include_outputs=args.all)
    if command == "bake":
        return operations.bake(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                               explicit_blender=args.blender, timeout=args.timeout, operation_id=operation_id,
                               simulation_id=args.simulation, simulation_profile=args.profile)
    if command == "cache":
        return (
            operations.cache_inspect(args.project, args.simulation, args.profile)
            if args.cache_command == "inspect"
            else operations.cache_clean(args.project, args.simulation)
        )
    if command == "review":
        if args.package:
            if not args.decision:
                raise BlendError(code="REVIEW_DECISION_REQUIRED", category=ErrorCategory.CONFIGURATION,
                                 message="--record requires --decision.",
                                 remediation="Choose approved, changes-requested, selected, or no-decision.")
            return operations.review_record(args.package, decision=args.decision, comments=args.comments,
                                            selected_variant=args.variant)
        if args.project is None:
            raise BlendError(code="REVIEW_PROJECT_REQUIRED", category=ErrorCategory.CONFIGURATION,
                             message="review requires a project path.", remediation="Run blend review <project>.")
        return operations.review(supervisor, args.project, args.output, operation_id)
    if command == "search":
        return operations.search(supervisor, args.project, trust=args.trust, allow_network=args.allow_network,
                                 explicit_blender=args.blender, timeout=args.timeout,
                                 operation_id=operation_id, search_id=args.search_id)
    if command == "promote":
        return operations.promote(args.project, search_report=args.search_report,
                                  candidate_id=args.candidate_id, variant_name=args.variant_name)
    if command == "library":
        return (operations.library_compare(args.project, args.library_id, args.candidate)
                if args.library_command == "compare" else
                operations.library_update(args.project, args.library_id, args.candidate))
    if command == "template-upgrade":
        return operations.template_upgrade(args.project)
    if command == "rules":
        return operations.rule_catalog()
    if command == "mcp":
        from .mcp_server import main as mcp_main
        return {"exitCode": mcp_main()}
    raise AssertionError(command)


def _artifacts(value: Any) -> list[str]:
    results = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"path", "manifest", "report", "validation", "inspection", "index", "contactsheet"} and isinstance(item, str):
                path = Path(item)
                if path.exists():
                    results.append(str(path.resolve()))
            else:
                results.extend(_artifacts(item))
    elif isinstance(value, list):
        for item in value:
            results.extend(_artifacts(item))
    return list(dict.fromkeys(results))


def _human(result: dict[str, Any]) -> str:
    lines = [f"{result['operation']}: {result['status']}"]
    if result.get("summary"):
        lines.append(result["summary"])
    lines.append(f"Duration: {float(result.get('durationSeconds', 0)):.3f}s")
    if result.get("artifacts"):
        lines.append("Artifacts:")
        lines.extend(f"  {path}" for path in result["artifacts"][:12])
        if len(result["artifacts"]) > 12:
            lines.append(f"  … {len(result['artifacts']) - 12} more")
    if result.get("warnings"):
        lines.append(f"Warnings: {len(result['warnings'])}")
    if result.get("nextActions"):
        lines.append("Next: " + result["nextActions"][0])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    json_output = "--json" in raw
    raw = [item for item in raw if item != "--json"]
    try:
        args = parser().parse_args(raw)
    except BlendError as error:
        failed = CommandResult("cli", new_operation_id("cli"), None)
        failed.fail(error)
        value = failed.as_dict()
        stream = sys.stdout if json_output else sys.stderr
        print(
            json.dumps(value, sort_keys=True, ensure_ascii=False)
            if json_output
            else _human(value),
            file=stream,
        )
        return error.exit_code
    if args.command == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()
    supplied_id = getattr(args, "operation_id", None)
    try:
        operation_id = new_operation_id(args.command.replace(" ", "-"), supplied_id)
    except BlendError as error:
        failed = CommandResult(args.command, new_operation_id(args.command.replace(" ", "-")), _project_of(args))
        failed.fail(error)
        value = failed.as_dict()
        stream = sys.stdout if json_output else sys.stderr
        print(
            json.dumps(value, sort_keys=True, ensure_ascii=False)
            if json_output
            else _human(value),
            file=stream,
        )
        return error.exit_code
    result = CommandResult(args.command, operation_id, _project_of(args))
    exit_code = 0
    with ProcessSupervisor() as supervisor:
        try:
            data = _dispatch(args, supervisor, operation_id)
            result.data = data
            result.artifacts = _artifacts(data)
            result.summary = _summary(args.command, data)
            result.next_actions = _next_actions(args.command, args, data)
            if isinstance(data, dict) and "warnings" in data and isinstance(data["warnings"], list):
                result.warnings = data["warnings"]
            result.progress = _progress(args.command, data)
        except BlendError as exc:
            result.fail(exc)
            exit_code = exc.exit_code
        except KeyboardInterrupt:
            error = BlendError(
                code="COMMAND_INTERRUPTED",
                category=ErrorCategory.INTERRUPTED,
                message="Command was interrupted; owned processes were stopped and valid artifacts retained.",
                operation=args.command,
                remediation="Use the reported resume command where safe.",
            )
            result.fail(error)
            exit_code = error.exit_code
        except Exception as exc:
            details = {"exception": type(exc).__name__}
            if os.environ.get("BLEND_DEBUG"):
                details["traceback"] = traceback.format_exc()
            error = BlendError(
                code="INTERNAL_ERROR",
                category=ErrorCategory.INTERNAL,
                message=f"Unexpected internal error: {exc}",
                operation=args.command,
                remediation="Rerun with BLEND_DEBUG=1 and report the retained structured failure.",
                details=details,
            )
            result.fail(error)
            exit_code = error.exit_code
    value = result.as_dict()
    stream = sys.stdout if json_output else (sys.stderr if exit_code else sys.stdout)
    if json_output:
        print(json.dumps(value, sort_keys=True, ensure_ascii=False), file=stream)
    else:
        print(_human(value), file=stream)
    return exit_code


def _summary(command: str, data: dict[str, Any]) -> str:
    if command == "doctor":
        return "Capability report completed without mutating the project."
    if command == "init":
        return f"Initialized {data.get('template')} project."
    if command == "build":
        return "Authoritative scene source built and checkpointed."
    if command == "preview":
        return f"Rendered {len(data.get('previews', []))} preview image(s) and a contact sheet."
    if command == "inspect":
        return "Retained complete evaluated-scene inspection."
    if command == "validate":
        report = data.get("report")
        warning_count = (
            report.get("summary", {}).get("warnings", 0)
            if isinstance(report, dict)
            else 0
        )
        return f"Validation passed with {warning_count} warning(s)."
    if command == "render":
        return f"Rendered {len(data.get('renderedFrames', []))} frame(s)."
    if command == "resume":
        return (
            f"Rendered {len(data.get('renderedFrames', []))} invalid or missing frame(s); "
            f"reused {len(data.get('reusedFrames', []))}."
        )
    if command == "encode":
        return f"Encoded {len(data.get('outputs', []))} output(s) without rerendering Blender frames."
    if command == "export":
        return f"Produced and validated {len(data.get('exports', []))} export(s)."
    return f"{command} completed."


def _next_actions(command: str, args: argparse.Namespace, data: dict[str, Any]) -> list[str]:
    project = getattr(args, "project", None)
    if not project:
        return []
    if command == "init":
        return [f"Review scene.py, then run blend trust {project}."]
    if command == "build":
        return [f"Run blend preview {project} --trust."]
    if command == "preview":
        return [f"Inspect the contact sheet, then run blend inspect {project} --trust."]
    if command == "inspect":
        return [f"Run blend validate {project} --profile final --trust."]
    if command == "validate":
        return [f"Run blend render {project} --profile final --trust."]
    if command in {"render", "resume"}:
        return [f"Run blend encode {project}."]
    return []


def _progress(command: str, data: dict[str, Any]) -> dict[str, Any]:
    if command in {"render", "resume"}:
        expected = len(data.get("expectedFrames", []))
        completed = len(data.get("renderedFrames", [])) + len(data.get("reusedFrames", []))
        return {"unit": "frame", "completed": completed, "total": expected}
    if command == "preview":
        return {"unit": "preview", "completed": len(data.get("previews", [])),
                "total": len(data.get("previews", []))}
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
