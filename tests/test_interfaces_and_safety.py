from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import yaml
from jsonschema import Draft202012Validator

from blend_harness.errors import BlendError
from blend_harness.manifests import Manifest
from blend_harness.mcp_server import PROTOCOL_VERSION, TOOLS, Server, _bounded_result
from blend_harness.process import ProcessSupervisor, sanitized_environment
from blend_harness.project import load_project, schema_errors
from blend_harness import operations
from blend_harness.validation import validate_project

from conftest import REPOSITORY, project_copy, run_blend


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_json_cli_returns_schema_valid_success_and_stable_failure(tmp_path: Path) -> None:
    project = project_copy(tmp_path, "empty")
    completed, success = run_blend("validate-config", str(project))
    assert completed.returncode == 0
    assert success["schema"] == 1
    assert success["status"] == "succeeded"
    assert success["operationId"].startswith("validate-config-")
    assert not schema_errors(success, "result-v1.json")

    completed, failure = run_blend(
        "validate-config", str(project), "--profile", "not-declared", check=False
    )
    assert completed.returncode != 0
    assert failure["status"] == "failed"
    assert failure["error"]["code"] == "CONFIG_PROFILE_UNKNOWN"
    assert failure["error"]["remediation"]
    assert not schema_errors(failure, "result-v1.json")


def test_rules_command_returns_the_complete_stable_catalog() -> None:
    _, result = run_blend("rules", "--operation-id", "rules-catalog")
    rules = result["data"]["rules"]
    rule_ids = {rule["id"] for rule in rules}
    assert len(rule_ids) == len(rules)
    assert {
        "CONFIG.SCHEMA_SUPPORTED",
        "ASSET.MISSING",
        "SCENE.NO_ACTIVE_CAMERA",
        "ANIMATION.CACHE_MISSING_OR_STALE",
        "PERF.OUTPUT_STORAGE_LIMIT",
        "SIMULATION.CACHE_PARTIAL",
        "OUTPUT.ALPHA_MISSING",
        "FINAL.VALIDATION_REQUIRED",
    } <= rule_ids
    assert all(rule["remediation"] for rule in rules)


def test_operation_ids_are_validated_before_mutation(tmp_path: Path) -> None:
    project = project_copy(tmp_path, "empty")
    completed, result = run_blend(
        "clean", str(project), "--operation-id", "contains spaces", check=False
    )
    assert completed.returncode != 0
    assert result["error"]["code"] == "OPERATION_ID_INVALID"



def test_cleanup_deletes_only_manifest_owned_generated_artifacts(tmp_path: Path) -> None:
    project_path = project_copy(tmp_path, "empty")
    project = load_project(project_path, create_generated=True)
    source = project_path / "scene.py"
    asset = project_path / "assets" / "keep.dat"
    asset.parent.mkdir(exist_ok=True)
    asset.write_text("keep", encoding="utf-8")
    generated = project.paths.working / "owned.json"
    delivery = project.paths.outputs / "owned.txt"
    unowned = project.paths.working / "unowned.txt"
    for path, content in ((generated, "{}"), (delivery, "delivery"), (unowned, "keep")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = Manifest(project, "fixture", "clean-fixture")
    manifest.add_output(generated, kind="fixture")
    manifest.add_output(delivery, kind="delivery")
    manifest.succeed()
    _, cleaned_generated = run_blend(
        "clean", str(project_path), "--generated", "--operation-id", "clean-generated"
    )
    generated_result = cleaned_generated["data"]
    assert generated_result["sourceDeleted"] is False
    assert generated_result["assetsDeleted"] is False
    assert not generated.exists()
    assert delivery.is_file()
    assert unowned.is_file()
    assert source.is_file() and asset.is_file()

    output_manifest = Manifest(project, "fixture", "clean-output-fixture")
    output_manifest.add_output(delivery, kind="delivery")
    output_manifest.succeed()
    _, cleaned_all = run_blend(
        "clean", str(project_path), "--all", "--operation-id", "clean-all"
    )
    all_result = cleaned_all["data"]
    assert all_result["outputsIncluded"] is True
    assert not delivery.exists()
    assert source.is_file() and asset.is_file() and unowned.is_file()

def test_output_collisions_and_hard_resources_are_preflight_findings(tmp_path: Path) -> None:
    project = project_copy(tmp_path, "brand-ident")
    config_path = project / "blend.yaml"
    config = _load_yaml(config_path)
    duplicate = dict(config["outputs"][0])
    duplicate["id"] = "deliberate-collision"
    config["outputs"].append(duplicate)
    config["resources"]["maxResolutionPixels"] = 1
    _write_yaml(config_path, config)
    report = validate_project(
        load_project(project),
        profile="final",
        variant="square-amber",
        operation="validate-config",
        write_artifact=False,
    )
    rule_ids = {finding["ruleId"] for finding in report["findings"]}
    assert "CONFIG.OUTPUT_COLLISION" in rule_ids
    assert "PERF.RESOLUTION_LIMIT" in rule_ids
    assert not report["summary"]["passed"]


def test_missing_font_and_license_fail_before_blender(tmp_path: Path) -> None:
    project = project_copy(tmp_path, "procedural-explainer")
    (project / "assets" / "fonts" / "Lato-Regular.ttf").unlink()
    report = validate_project(
        load_project(project),
        profile="preview",
        variant="revision-a",
        operation="validate-config",
        write_artifact=False,
    )
    assert "ASSET.MISSING" in {finding["ruleId"] for finding in report["findings"]}
    assert not report["summary"]["passed"]



def test_missing_texture_blocks_final_work_before_blender(tmp_path: Path) -> None:
    project = project_copy(tmp_path, "product-turntable")
    config_path = project / "blend.yaml"
    config = _load_yaml(config_path)
    config["assets"].append({
        "id": "required-surface-texture",
        "type": "texture",
        "path": "assets/textures/required-surface.png",
    })
    _write_yaml(config_path, config)
    report = validate_project(
        load_project(project),
        profile="final",
        variant="graphite",
        operation="render",
        write_artifact=False,
    )
    findings = {finding["ruleId"] for finding in report["findings"] if not finding["suppressed"]}
    assert {"ASSET.MISSING", "FINAL.ASSET_UNPINNED"} <= findings
    assert not report["summary"]["passed"]


def test_policy_promotions_and_scoped_suppressions_are_recorded(tmp_path: Path) -> None:
    project_path = project_copy(tmp_path, "procedural-explainer")
    config_path = project_path / "blend.yaml"
    config = _load_yaml(config_path)
    font = next(asset for asset in config["assets"] if asset["type"] == "font")
    font.pop("fontLicenseMetadata")
    config["policies"]["promote"] = ["ASSET.FONT_LICENSE_METADATA"]
    _write_yaml(config_path, config)

    promoted = validate_project(
        load_project(project_path),
        profile="preview",
        variant="revision-a",
        operation="validate-config",
        write_artifact=False,
    )
    promoted_finding = next(
        finding for finding in promoted["findings"]
        if finding["ruleId"] == "ASSET.FONT_LICENSE_METADATA"
    )
    assert promoted_finding["severity"] == "error"
    assert promoted_finding["originalSeverity"] == "warning"
    assert not promoted_finding["suppressed"]

    config["policies"]["suppress"] = [{
        "rule": "ASSET.FONT_LICENSE_METADATA",
        "reason": "Bundled OFL metadata is tracked in the distribution manifest.",
        "scope": "profile:preview",
    }]
    _write_yaml(config_path, config)
    suppressed = validate_project(
        load_project(project_path),
        profile="preview",
        variant="revision-a",
        operation="validate-config",
        write_artifact=False,
    )
    suppressed_finding = next(
        finding for finding in suppressed["findings"]
        if finding["ruleId"] == "ASSET.FONT_LICENSE_METADATA"
    )
    assert suppressed_finding["suppressed"]
    assert suppressed_finding["suppressionReason"]
    assert suppressed["summary"]["passed"]


def test_cycles_device_fallback_is_never_implicit(tmp_path: Path) -> None:
    project_path = project_copy(tmp_path, "empty")
    config_path = project_path / "blend.yaml"
    config = _load_yaml(config_path)
    config["profiles"]["final"].update({"engine": "CYCLES", "device": "GPU"})
    _write_yaml(config_path, config)
    report = validate_project(
        load_project(project_path),
        profile="final",
        variant=None,
        operation="validate-config",
        capabilities={
            "blender": {
                "engines": ["BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"],
                "cyclesDevices": [{"type": "CPU"}],
            },
            "ffmpeg": {"codecs": {"h264": ["libx264"]}},
        },
        write_artifact=False,
    )
    findings = {finding["ruleId"] for finding in report["findings"] if not finding["suppressed"]}
    assert {"CONFIG.DEVICE_AVAILABLE", "PERF.CYCLES_DEVICE"} <= findings
    assert not report["summary"]["passed"]


def test_direct_container_render_profile_is_rejected_by_schema(tmp_path: Path) -> None:
    project_path = project_copy(tmp_path, "empty")
    config_path = project_path / "blend.yaml"
    config = _load_yaml(config_path)
    config["profiles"]["final"]["format"] = "FFMPEG"
    _write_yaml(config_path, config)
    with pytest.raises(BlendError) as failure:
        load_project(project_path)
    assert failure.value.code == "CONFIG_SCHEMA_INVALID"


def test_concurrency_adapts_to_cpu_gpu_load_and_memory_ceilings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = load_project(project_copy(tmp_path, "empty"))
    project.config["resources"].update({
        "maxProcesses": 8,
        "maxGpuProcesses": 1,
        "maxMemoryMB": 512,
        "thermalAdvisory": True,
    })
    monkeypatch.setattr(operations.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(operations.os, "getloadavg", lambda: (14.0, 10.0, 8.0))
    cpu_workers, cpu_advisories = operations._effective_concurrency(
        project,
        {"engine": "BLENDER_EEVEE_NEXT", "width": 4096, "height": 4096},
        8,
    )
    assert cpu_workers == 2
    assert any("host load" in advisory for advisory in cpu_advisories)
    assert any("fit 512 MiB" in advisory for advisory in cpu_advisories)

    gpu_workers, gpu_advisories = operations._effective_concurrency(
        project,
        {"engine": "CYCLES", "device": "METAL", "width": 512, "height": 512},
        8,
    )
    assert gpu_workers == 1
    assert any("GPU worker limit" in advisory for advisory in gpu_advisories)

def test_environment_allowlist_rejects_secrets_and_ambient_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    environment = sanitized_environment({"BLEND_CREATIVE_LABEL": "safe"})
    assert environment["BLEND_CREATIVE_LABEL"] == "safe"
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    with pytest.raises(BlendError) as failure:
        sanitized_environment({"BLEND_API_TOKEN": "unsafe"})
    assert failure.value.code == "SECURITY_SECRET_ENVIRONMENT_REJECTED"
    with pytest.raises(BlendError) as failure:
        sanitized_environment({"UNDECLARED": "unsafe"})
    assert failure.value.code == "SECURITY_ENVIRONMENT_NOT_ALLOWLISTED"


def test_supervisor_timeout_terminates_owned_process_group(tmp_path: Path) -> None:
    script = tmp_path / "parent.py"
    pid_path = tmp_path / "child.pid"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    with ProcessSupervisor() as supervisor:
        result = supervisor.run(
            [sys.executable, str(script), str(pid_path)],
            cwd=tmp_path,
            log_path=tmp_path / "process.log",
            timeout_seconds=0.4,
        )
        assert result.timed_out
        assert supervisor.active_pids == []
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"owned child process {child_pid} survived process-group timeout")

def test_comparison_and_review_cancellation_discard_atomic_staging(tmp_path: Path) -> None:
    from PIL import Image

    class CancelDuringOperation:
        def __init__(self) -> None:
            self.checks = 0

        @property
        def interrupted(self) -> bool:
            self.checks += 1
            return self.checks >= 2

    left = tmp_path / "left.png"
    right = tmp_path / "right.png"
    Image.new("RGB", (64, 64), "black").save(left)
    Image.new("RGB", (64, 64), "white").save(right)
    comparison = tmp_path / "comparison"
    with pytest.raises(BlendError) as failure:
        operations.compare(
            CancelDuringOperation(), left, right, comparison, "cancel-comparison"
        )
    assert failure.value.code == "PROCESS_INTERRUPTED"
    assert not comparison.exists()
    assert not comparison.with_name(".comparison.part").exists()

    project = project_copy(tmp_path / "project-root", "empty")
    source_manifest = Manifest(
        load_project(project, create_generated=True), "build", "review-source-manifest"
    )
    source_manifest.succeed()
    review = tmp_path / "review"
    with pytest.raises(BlendError) as failure:
        operations.review(
            CancelDuringOperation(), project, review, "cancel-review"
        )
    assert failure.value.code == "PROCESS_INTERRUPTED"
    assert not review.exists()
    assert not review.with_name(".review.part").exists()


def test_project_comparison_uses_authoritative_manifests_and_source_hashes(tmp_path: Path) -> None:
    left_path = project_copy(tmp_path / "left-root", "empty")
    right_path = project_copy(tmp_path / "right-root", "empty")
    right_scene = right_path / "scene.py"
    right_scene.write_text(
        right_scene.read_text(encoding="utf-8") + "\n# compared revision\n",
        encoding="utf-8",
    )
    left = load_project(left_path, create_generated=True)
    right = load_project(right_path, create_generated=True)
    left_manifest = Manifest(left, "build", "compare-left-manifest")
    left_manifest.succeed()
    right_manifest = Manifest(right, "build", "compare-right-manifest")
    right_manifest.succeed()

    destination = tmp_path / "comparison"
    with ProcessSupervisor() as supervisor:
        report = operations.compare(
            supervisor, left_path, right_path, destination, "compare-project-revisions"
        )
    assert report["projectSourceChanges"]
    assert report["manifestChanges"]
    assert report["sourceAssetAndBlenderHashChanges"]
    assert report["conclusion"].startswith("Change evidence only")
    assert Path(report["report"]).is_file()


def test_every_mcp_mutation_requires_operation_id() -> None:
    for tool in TOOLS.values():
        if not tool.mutation:
            continue
        assert (
            "project" in tool.input_schema["required"]
            or "directory" in tool.input_schema["required"]
        ), tool.name
        assert "operationId" in tool.input_schema["required"], tool.name
        errors = list(Draft202012Validator(tool.input_schema).iter_errors({}))
        assert any("operationId" in error.message for error in errors), tool.name


def test_mcp_initialize_list_and_invalid_tool_are_protocol_clean() -> None:
    server = Server()
    messages: list[dict] = []
    server.send = messages.append  # type: ignore[method-assign]
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}})
    server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "not-a-tool", "arguments": {}}})
    assert messages[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
    names = {tool["name"] for tool in messages[1]["result"]["tools"]}
    assert {"blend_plan", "blend_preview", "blend_render", "blend_resume", "blend_export", "blend_artifact"} <= names
    assert messages[2]["error"]["code"] == -32602


def test_mcp_stdio_emits_only_json_rpc_lines() -> None:
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown"}),
        json.dumps({"jsonrpc": "2.0", "method": "exit"}),
        "",
    ])
    completed = subprocess.run(
        [sys.executable, "-m", "blend_harness.mcp_server"],
        cwd=REPOSITORY,
        input=requests,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert not completed.stderr


def test_mcp_bounded_evidence_keeps_full_report_on_disk_pointer() -> None:
    objects = [{"name": f"Object-{index}"} for index in range(100)]
    result = {
        "data": {
            "inspection": {
                "schema": 1,
                "objects": objects,
                "collections": list(range(80)),
                "dependencies": list(range(80)),
            },
            "completeArtifact": "/tmp/inspection.json",
        }
    }
    bounded = _bounded_result("blend_inspect", result)
    inspection = bounded["data"]["inspection"]
    assert inspection["counts"]["objects"] == 100
    assert len(inspection["filteredEvidence"]["objects"]) <= 50
    assert "objects" not in inspection
    assert inspection["completeArtifact"] == "/tmp/inspection.json"


def test_mcp_bounds_large_comparison_and_validation_evidence() -> None:
    changes = [{"path": f"/objects/{index}"} for index in range(150)]
    comparison = _bounded_result("blend_compare", {
        "error": None,
        "data": {
            "images": changes,
            "manifestChanges": changes,
            "report": "/tmp/comparison.json",
        }
    })
    assert len(comparison["data"]["images"]) == 100
    assert comparison["data"]["imagesCount"] == 150
    assert comparison["data"]["imagesTruncated"] is True
    assert len(comparison["data"]["manifestChanges"]) == 100
    assert comparison["data"]["report"] == "/tmp/comparison.json"

    validation = _bounded_result("blend_validate", {
        "error": {
            "details": {
                "report": {
                    "findings": changes,
                    "artifact": "/tmp/validation.json",
                }
            }
        }
    })
    report = validation["error"]["details"]["report"]
    assert len(report["findings"]) == 100
    assert report["findingsCount"] == 150
    assert report["artifact"] == "/tmp/validation.json"
