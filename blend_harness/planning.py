"""Complete, collision-aware operation planning before expensive work."""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

from .caches import inspect_caches
from .project import Project, output_paths
from .util import load_json
from .validation import validate_project


def _frames(project: Project) -> list[int]:
    return list(range(project.config["project"]["frameStart"], project.config["project"]["frameEnd"] + 1))


def _operation_graph(target: str, project: Project, profile: str, variant: str | None,
                     output: str | None) -> list[dict[str, Any]]:
    command = lambda verb: f"blend {verb} {project.paths.root} --profile {profile}" + (f" --variant {variant}" if variant else "")
    graph = [
        {"id": "config", "operation": "validate-config", "dependsOn": [], "command": f"blend validate-config {project.paths.root}"},
        {"id": "build", "operation": "build", "dependsOn": ["config"], "command": f"blend build {project.paths.root} --trust"},
        {"id": "inspect", "operation": "inspect", "dependsOn": ["build"], "command": f"blend inspect {project.paths.root} --trust"},
        {"id": "validate", "operation": "validate", "dependsOn": ["inspect"], "command": command("validate") + " --trust"},
    ]
    if target in {"preview", "contact-sheet", "compare", "review"}:
        graph.append({"id": "preview", "operation": "preview", "dependsOn": ["config"],
                      "command": command("preview") + " --trust"})
    if target in {"render", "resume", "encode", "review"}:
        graph.append({"id": "render", "operation": "render", "dependsOn": ["validate"],
                      "command": command("render") + " --trust"})
    if target in {"encode", "review"}:
        suffix = f" --output {output}" if output else ""
        graph.append({"id": "encode", "operation": "encode", "dependsOn": ["render"],
                      "command": command("encode") + suffix})
    if target == "export":
        suffix = f" --output {output}" if output else ""
        graph.append({"id": "export", "operation": "export", "dependsOn": ["validate"],
                      "command": command("export") + suffix + " --trust"})
    if project.config.get("simulations") and target in {"render", "resume", "encode", "export", "review"}:
        graph.insert(2, {"id": "bake", "operation": "bake", "dependsOn": ["build"],
                         "command": f"blend bake {project.paths.root} --trust"})
        for node in graph:
            if node["id"] == "inspect" and "bake" not in node["dependsOn"]:
                node["dependsOn"].append("bake")
    if target == "compare":
        graph.append({"id": "compare", "operation": "compare", "dependsOn": ["preview"],
                      "command": "blend compare <left> <right>"})
    if target == "review":
        graph.append({"id": "review", "operation": "review", "dependsOn": ["preview", "render"],
                      "command": f"blend review {project.paths.root}"})
    return graph


def plan_project(project: Project, *, target: str, profile: str | None, variant: str | None,
                 matrix: str | None, output: str | None, capabilities: dict[str, Any] | None) -> dict[str, Any]:
    resolved_variant = project.resolved_variant(variant)
    profile_name, profile_value = project.resolved_profile(profile, resolved_variant)
    members = project.matrix_members(matrix) if matrix else [{"matrix": None, "variant": variant,
                                                               "profile": profile_name, "output": output}]
    resolved_members = []
    for member in members:
        member_variant = str(member["variant"]) if member["variant"] else None
        member_profile = str(member["profile"]) if member["profile"] else None
        member_variant_value = project.resolved_variant(member_variant)
        member_profile_name, member_profile_value = project.resolved_profile(
            member_profile, member_variant_value
        )
        resolved_members.append({
            "member": member,
            "variant": member_variant_value,
            "profileName": member_profile_name,
            "profile": member_profile_value,
        })
    frame_list = _frames(project)
    outputs = output_paths(project, variants=[str(member["variant"]) if member["variant"] else None for member in members])
    collisions: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in outputs:
        groups[str(record["path"])].append({key: value for key, value in record.items() if key != "path"})
    for path, owners in groups.items():
        if len(owners) > 1:
            collisions.append({"path": path, "owners": owners})
    estimated_frames = len(frame_list) * len(members)
    estimated_storage = 0
    estimated_pixels = 0
    estimated_cost = 0
    for resolved_member in resolved_members:
        member_profile_value = resolved_member["profile"]
        channels = 4 if member_profile_value.get("colorMode") == "RGBA" else 3
        bytes_per_channel = 4 if member_profile_value["format"] == "OPEN_EXR" else 1
        pixels = member_profile_value["width"] * member_profile_value["height"] * len(frame_list)
        estimated_pixels += pixels
        estimated_storage += pixels * channels * bytes_per_channel
        estimated_cost += pixels * member_profile_value["samples"]
    resource = project.config.get("resources", {})
    member_reports = []
    for resolved_member in resolved_members:
        member = resolved_member["member"]
        member_report = validate_project(
            project,
            profile=resolved_member["profileName"],
            variant=str(member["variant"]) if member["variant"] else None,
            operation=target,
            inspection=(
                load_json(project.paths.inspection)
                if not matrix and project.paths.inspection.is_file()
                else None
            ),
            capabilities=capabilities,
            blender_version=capabilities.get("blender", {}).get("version") if capabilities else None,
            write_artifact=False,
        )
        member_reports.append({"member": member, "report": member_report})
    if matrix:
        findings = [
            {**finding, "matrixMember": item["member"]}
            for item in member_reports
            for finding in item["report"]["findings"]
        ]
        active = [finding for finding in findings if not finding.get("suppressed")]
        report = {
            "schema": 1,
            "project": project.id,
            "profile": "matrix",
            "variant": None,
            "summary": {
                "errors": sum(1 for finding in active if finding["severity"] == "error"),
                "warnings": sum(1 for finding in active if finding["severity"] == "warning"),
                "info": sum(1 for finding in active if finding["severity"] == "info"),
            },
            "findings": findings,
            "mechanicalOnly": True,
            "memberReports": member_reports,
        }
        report["summary"]["passed"] = report["summary"]["errors"] == 0
    else:
        report = member_reports[0]["report"]
    if collisions:
        report["findings"].append({
            "ruleId": "CONFIG.OUTPUT_COLLISION",
            "severity": "error",
            "message": "Planned matrix resolves colliding output paths.",
            "evidence": {"collisions": collisions},
            "remediation": "Include {variant} or another distinguishing value in every matrix output path.",
            "scope": "matrix",
            "suppressed": False,
        })
        report["summary"]["errors"] += 1
        report["summary"]["passed"] = False
    checkpoint_meta = project.paths.checkpoint.with_suffix(".blendmeta.json")
    checkpoint_hit = False
    if project.paths.checkpoint.is_file() and checkpoint_meta.is_file():
        metadata = load_json(checkpoint_meta)
        checkpoint_hit = metadata.get("dependencyHash") == project.dependency_hash(operation="build")
    available_storage = (
        capabilities.get("workspace", {}).get("availableBytes")
        if capabilities
        else None
    )
    if available_storage is not None and estimated_storage > available_storage:
        report["findings"].append({
            "ruleId": "PERF.OUTPUT_STORAGE_LIMIT",
            "severity": "error",
            "message": "Estimated output exceeds currently available workspace storage.",
            "evidence": {"estimatedBytes": estimated_storage, "availableBytes": available_storage},
            "remediation": "Reduce output scope or free storage before rendering.",
            "scope": "workspace",
            "suppressed": False,
        })
        report["summary"]["errors"] += 1
        report["summary"]["passed"] = False
    declared_disk_budget = resource.get("maxDiskBytes")
    if declared_disk_budget is not None and estimated_storage > declared_disk_budget:
        report["findings"].append({
            "ruleId": "PERF.OUTPUT_STORAGE_LIMIT",
            "severity": "error",
            "message": "Planned aggregate output exceeds the declared hard disk budget.",
            "evidence": {
                "estimatedBytes": estimated_storage,
                "limit": declared_disk_budget,
                "matrixMembers": len(members),
            },
            "remediation": "Reduce the matrix, resolution, or frame range, or explicitly raise the reviewed budget.",
            "scope": "matrix" if matrix else f"profile:{profile_name}",
            "suppressed": False,
        })
        report["summary"]["errors"] += 1
        report["summary"]["passed"] = False
    if project.config.get("simulations"):
        cache_reports = [
            {
                "member": item["member"],
                "report": inspect_caches(
                    project,
                    simulation_profile=item["profile"].get("simulationProfile"),
                ),
            }
            for item in resolved_members
        ]
        cache_report = {"byMember": cache_reports}
    else:
        cache_report = {"caches": [], "summary": {}}
    graph = _operation_graph(target, project, profile_name, variant, output)
    matrix_graphs = [
        {
            "member": item["member"],
            "graph": _operation_graph(
                target,
                project,
                item["profileName"],
                str(item["member"]["variant"]) if item["member"]["variant"] else None,
                str(item["member"]["output"]) if item["member"]["output"] else None,
            ),
        }
        for item in resolved_members
    ] if matrix else []
    return {
        "schema": 1,
        "project": project.id,
        "target": target,
        "profile": {"id": profile_name, "resolved": profile_value},
        "variant": {"id": variant, "resolved": resolved_variant},
        "matrix": {"id": matrix, "size": len(members), "members": members,
                   "resolvedMembers": resolved_members},
        "graph": graph,
        "matrixGraphs": matrix_graphs,
        "commands": (
            [node["command"] for item in matrix_graphs for node in item["graph"]]
            if matrix_graphs
            else [node["command"] for node in graph]
        ),
        "dependencies": project.input_records(),
        "assets": [asset.as_manifest(project.paths.root) for asset in project.assets],
        "frames": {"start": frame_list[0], "end": frame_list[-1], "count": len(frame_list), "expected": frame_list},
        "variants": sorted(project.config.get("variants", {})),
        "outputs": [{**{key: value for key, value in record.items() if key != "path"}, "path": str(record["path"])} for record in outputs],
        "collisions": collisions,
        "estimate": {
            "matrixMembers": len(members),
            "renderedFrames": estimated_frames,
            "pixels": estimated_pixels,
            "samples": sorted({item["profile"]["samples"] for item in resolved_members}),
            "uncompressedStorageBytes": estimated_storage,
            "costUnits": estimated_cost,
            "declaredDiskBudget": resource.get("maxDiskBytes"),
            "availableStorageBytes": available_storage,
        },
        "devices": {
            "engines": capabilities.get("blender", {}).get("engines", []) if capabilities else [],
            "cycles": capabilities.get("blender", {}).get("cyclesDevices", []) if capabilities else [],
        },
        "cacheHits": {"checkpoint": checkpoint_hit, "simulations": cache_report},
        "blockingValidation": report,
        "ready": report["summary"]["passed"] and not collisions,
    }
