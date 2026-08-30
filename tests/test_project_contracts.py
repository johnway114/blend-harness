from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from blend_harness.errors import BlendError
from blend_harness.project import load_project, migrate_config, schema_errors
from blend_harness.validation import validate_project

from conftest import TEMPLATES, project_copy


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_every_template_and_reference_expectation_matches_strict_schema() -> None:
    for template in sorted(TEMPLATES.iterdir()):
        if not template.is_dir():
            continue
        project = load_project(template)
        report = validate_project(
            project,
            profile=None,
            variant=None,
            operation="validate-config",
            write_artifact=False,
        )
        assert report["summary"]["passed"], (template.name, report["findings"])
        expectation = template / "expected.json"
        if expectation.is_file():
            value = json.loads(expectation.read_text(encoding="utf-8"))
            assert not schema_errors(value, "reference-expectation-v1.json"), template.name


def test_unknown_configuration_fields_fail_closed(tmp_path: Path) -> None:
    root = project_copy(tmp_path, "empty")
    config = _yaml(root / "blend.yaml")
    config["inventedCompatibilityField"] = True
    _write_yaml(root / "blend.yaml", config)
    with pytest.raises(BlendError) as failure:
        load_project(root)
    assert failure.value.code == "CONFIG_SCHEMA_INVALID"


def test_schema_zero_migration_is_deterministic_and_one_way() -> None:
    legacy = {
        "schemaVersion": 0,
        "renderProfiles": {"preview": {}},
        "scene": "scene.py",
        "briefPath": "brief.yaml",
        "fps": 24,
        "startFrame": 1,
        "endFrame": 48,
    }
    migrated, changes = migrate_config(legacy)
    repeated, repeated_changes = migrate_config(legacy)
    assert migrated == repeated
    assert changes == repeated_changes
    assert migrated["schema"] == 1
    assert migrated["entrypoint"] == "scene.py"
    assert migrated["project"] == {"frameRate": 24, "frameStart": 1, "frameEnd": 48}
    with pytest.raises(BlendError, match="Unsupported project schema"):
        migrate_config({"schema": 999})


def test_variant_inheritance_matrix_expansion_and_unknown_ids(tmp_path: Path) -> None:
    project = load_project(project_copy(tmp_path, "brand-ident"))
    vertical = project.resolved_variant("vertical-ivory")
    assert vertical["width"] == 216
    assert vertical["height"] == 384
    assert vertical["id"] == "vertical-ivory"
    members = project.matrix_members("delivery")
    assert len(members) >= 3
    assert {member["variant"] for member in members} >= {"square-amber", "vertical-ivory", "landscape-oxide"}
    with pytest.raises(BlendError) as failure:
        project.resolved_variant("not-declared")
    assert failure.value.code == "CONFIG_VARIANT_UNKNOWN"


def test_variant_inheritance_is_acyclic_and_merges_declared_parameter_surface(tmp_path: Path) -> None:
    root = project_copy(tmp_path, "empty")
    config_path = root / "blend.yaml"
    config = _yaml(config_path)
    config["variants"] = {
        "base": {
            "width": 320,
            "height": 180,
            "camera": "hero",
            "text": {"headline": "Base"},
            "palette": {"background": [0.1, 0.2, 0.3, 1.0]},
            "material": {"roughness": 0.4},
            "model": {"id": "base-model"},
            "dataSource": "assets/data/base.json",
            "lighting": {"energy": 100},
            "timing": {"hold": 4},
            "parameters": {"spacing": 0.5},
        },
        "child": {
            "extends": "base",
            "text": {"headline": "Child"},
            "parameters": {"scale": 1.25},
        },
    }
    _write_yaml(config_path, config)
    child = load_project(root).resolved_variant("child")
    assert child["width"] == 320 and child["height"] == 180
    assert child["text"]["headline"] == "Child"
    assert child["parameters"] == {"spacing": 0.5, "scale": 1.25}
    assert {"camera", "palette", "material", "model", "dataSource", "lighting", "timing"} <= child.keys()

    config["variants"] = {
        "first": {"extends": "second"},
        "second": {"extends": "first"},
    }
    _write_yaml(config_path, config)
    with pytest.raises(BlendError) as failure:
        load_project(root).resolved_variant("first")
    assert failure.value.code == "CONFIG_VARIANT_CYCLE"


def test_project_module_bytes_invalidate_dependency_and_trust_fingerprint(tmp_path: Path) -> None:
    root = project_copy(tmp_path, "empty")
    module = root / "modules" / "helper.py"
    module.parent.mkdir()
    module.write_text("VALUE = 1\n", encoding="utf-8")
    first = load_project(root).dependency_hash(operation="build")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    second = load_project(root).dependency_hash(operation="build")
    assert first != second


def test_catalog_primary_and_transitive_checksum_drift_fail(tmp_path: Path) -> None:
    root = project_copy(tmp_path, "product-turntable")
    model = root / "assets" / "models" / "product.obj"
    model.write_text(model.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    report = validate_project(
        load_project(root),
        profile=None,
        variant=None,
        operation="validate-config",
        write_artifact=False,
    )
    assert "ASSET.CHECKSUM_DRIFT" in {finding["ruleId"] for finding in report["findings"]}

    root = project_copy(tmp_path / "second", "product-turntable")
    material = root / "assets" / "models" / "product.mtl"
    material.write_text(material.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    report = validate_project(
        load_project(root),
        profile=None,
        variant=None,
        operation="validate-config",
        write_artifact=False,
    )
    assert "ASSET.CHECKSUM_DRIFT" in {finding["ruleId"] for finding in report["findings"]}


def test_library_identity_version_content_and_transitive_asset_are_pinned(tmp_path: Path) -> None:
    root = project_copy(tmp_path, "brand-ident")
    library_python = root / "libraries" / "brand-rig" / "python" / "brand_rig.py"
    library_python.write_text(library_python.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    with pytest.raises(BlendError) as failure:
        load_project(root)
    assert failure.value.code == "LIBRARY_CHECKSUM_DRIFT"

    root = project_copy(tmp_path / "second", "brand-ident")
    config = _yaml(root / "blend.yaml")
    config["libraries"][0]["version"] = "99.0.0"
    _write_yaml(root / "blend.yaml", config)
    with pytest.raises(BlendError) as failure:
        load_project(root)
    assert failure.value.code == "LIBRARY_VERSION_DRIFT"


def test_asset_outside_declared_root_is_reported(tmp_path: Path) -> None:
    root = project_copy(tmp_path, "empty")
    external = tmp_path / "outside.dat"
    external.write_bytes(b"declared but outside root")
    import hashlib

    config = _yaml(root / "blend.yaml")
    config["assets"] = [{
        "id": "outside",
        "type": "data",
        "path": str(external),
        "checksum": hashlib.sha256(external.read_bytes()).hexdigest(),
        "license": "internal",
    }]
    _write_yaml(root / "blend.yaml", config)
    report = validate_project(
        load_project(root),
        profile=None,
        variant=None,
        operation="validate-config",
        write_artifact=False,
    )
    assert "ASSET.PATH_OUTSIDE_ROOT" in {finding["ruleId"] for finding in report["findings"]}
