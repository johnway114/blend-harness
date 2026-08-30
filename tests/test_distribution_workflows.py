from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

from blend_harness.manifests import Manifest
from blend_harness.project import load_project
from blend_harness.util import hash_tree, sha256_file

from conftest import project_copy, run_blend


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_cli_migration_previews_then_writes_with_backup(tmp_path: Path) -> None:
    project = project_copy(tmp_path, "empty")
    config_path = project / "blend.yaml"
    current = _yaml(config_path)
    legacy_manifest_path = Manifest(
        load_project(project, create_generated=True), "fixture", "legacy-manifest"
    )
    legacy_manifest_path.succeed()
    legacy_manifest = json.loads(legacy_manifest_path.path.read_text(encoding="utf-8"))
    legacy_manifest["schema"] = 0
    legacy_manifest["blend"] = legacy_manifest.pop("blendVersion")
    legacy_manifest_path.path.write_text(
        json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    original_manifest = legacy_manifest_path.path.read_bytes()
    legacy = dict(current)
    legacy.pop("schema")
    legacy["schemaVersion"] = 0
    legacy["renderProfiles"] = legacy.pop("profiles")
    legacy["scene"] = legacy.pop("entrypoint")
    legacy["briefPath"] = legacy.pop("brief")
    _write_yaml(config_path, legacy)
    original = config_path.read_bytes()

    _, preview = run_blend("migrate", str(project), "--operation-id", "migration-preview")
    assert preview["data"]["written"] is False
    assert config_path.read_bytes() == original
    assert preview["data"]["changes"]
    assert len(preview["data"]["manifests"]) == 1
    assert legacy_manifest_path.path.read_bytes() == original_manifest

    _, written = run_blend("migrate", str(project), "--write", "--operation-id", "migration-write")
    assert written["data"]["written"] is True
    backup = project / "blend.yaml.pre-migration"
    assert backup.read_bytes() == original
    _, validation = run_blend("validate-config", str(project))
    assert validation["data"]["summary"]["passed"]
    manifest_backup = legacy_manifest_path.path.with_suffix(".json.pre-migration")
    assert manifest_backup.read_bytes() == original_manifest
    migrated_manifest = json.loads(legacy_manifest_path.path.read_text(encoding="utf-8"))
    assert migrated_manifest["schema"] == 1
    assert migrated_manifest["blendVersion"]


def test_template_upgrade_is_comparison_only(tmp_path: Path) -> None:
    project = project_copy(tmp_path, "procedural-explainer")
    scene = project / "scene.py"
    before = sha256_file(scene)
    scene.write_text(scene.read_text(encoding="utf-8") + "\n# intentional project-only note\n", encoding="utf-8")
    changed = sha256_file(scene)
    _, result = run_blend(
        "template-upgrade", str(project), "--operation-id", "template-upgrade-comparison"
    )
    report = result["data"]
    assert report["applied"] is False
    assert report["creativeSettingsChanged"] is False
    assert "scene.py" in report["different"]
    assert sha256_file(scene) == changed and changed != before


def test_library_compare_then_explicit_atomic_update(tmp_path: Path) -> None:
    project_root = project_copy(tmp_path, "brand-ident")
    project = load_project(project_root, create_generated=True)
    source_library = project_root / "libraries" / "brand-rig"
    candidate = tmp_path / "brand-rig-1.1"
    shutil.copytree(source_library, candidate, ignore=shutil.ignore_patterns("__pycache__"))
    python = candidate / "python" / "brand_rig.py"
    python.write_text(python.read_text(encoding="utf-8") + "\nPALETTE_VERSION = '1.1'\n", encoding="utf-8")
    manifest_path = candidate / "blend-library.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _, compared = run_blend(
        "library", "compare", str(project_root), "brand-rig", str(candidate),
        "--operation-id", "library-comparison",
    )
    comparison = compared["data"]
    assert comparison["changes"]["changed"]
    assert comparison["candidate"]["version"] == "1.1.0"
    old_hash = hash_tree(source_library, exclude_generated=False)
    _, update = run_blend(
        "library", "update", str(project_root), "brand-rig", str(candidate),
        "--operation-id", "library-update",
    )
    updated = update["data"]
    assert updated["applied"] is True
    assert updated["current"]["version"] == "1.0.0"
    assert updated["candidate"]["version"] == "1.1.0"
    updated_project = load_project(project_root)
    assert updated_project.libraries[0].version == "1.1.0"
    assert updated_project.libraries[0].actual_checksum != old_hash
    assert updated_project.libraries[0].actual_checksum == updated_project.libraries[0].declared_checksum


def test_clean_install_migration_upgrade_fixture_and_uninstall(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    prefix = tmp_path / "prefix"
    environment = {
        **os.environ,
        "BLEND_PREFIX": str(prefix),
        "PYTHON": sys.executable,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    installer = repository / "scripts" / "install.sh"
    uninstaller = repository / "scripts" / "uninstall.sh"
    command = prefix / "bin" / "blend"
    managed_environment = prefix / "share" / "blend-harness" / "venv"

    try:
        first_install = subprocess.run(
            [str(installer)],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert command.is_symlink()
        assert "Installed Blend" in first_install.stdout

        fixture = tmp_path / "installed-fixture"
        subprocess.run(
            [
                str(command), "--json", "init", "empty", str(fixture),
                "--operation-id", "installed-fixture-init",
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        config_path = fixture / "blend.yaml"
        legacy = _yaml(config_path)
        legacy.pop("schema")
        legacy["schemaVersion"] = 0
        legacy["renderProfiles"] = legacy.pop("profiles")
        legacy["scene"] = legacy.pop("entrypoint")
        legacy["briefPath"] = legacy.pop("brief")
        _write_yaml(config_path, legacy)
        subprocess.run(
            [
                str(command), "--json", "migrate", str(fixture), "--write",
                "--operation-id", "installed-fixture-migration",
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

        upgraded = subprocess.run(
            [str(installer)],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert "Installed Blend" in upgraded.stdout
        validated = subprocess.run(
            [str(command), "--json", "validate-config", str(fixture)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert json.loads(validated.stdout)["data"]["summary"]["passed"]
    finally:
        if (managed_environment / ".blend-harness-install").is_file():
            subprocess.run(
                [str(uninstaller)],
                cwd=repository,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )

    assert not command.exists()
    assert not managed_environment.exists()
