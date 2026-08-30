"""Project loading, schema migration, path resolution, variants, and assets."""

from __future__ import annotations

import copy
import itertools
import json
import os
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker

from . import RUNTIME_VERSION, SCHEMA_VERSION, __version__
from .errors import BlendError, ErrorCategory, require
from .util import (
    atomic_write_json,
    atomic_write_yaml,
    canonical_json,
    ensure_within,
    hash_tree,
    load_json,
    load_yaml,
    safe_id,
    sha256_bytes,
    sha256_file,
)


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(value)
    if not match:
        raise ValueError(f"Invalid version {value!r}")
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _schema(name: str) -> dict[str, Any]:
    resource = files("blend_harness").joinpath("schemas", name)
    return json.loads(resource.read_text(encoding="utf-8"))


def schema_errors(value: Any, schema_name: str) -> list[dict[str, str]]:
    validator = Draft202012Validator(_schema(schema_name), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    return [
        {
            "path": "/" + "/".join(str(part) for part in error.absolute_path),
            "message": error.message,
            "validator": str(error.validator),
        }
        for error in errors
    ]


@dataclass(slots=True, frozen=True)
class ProjectPaths:
    root: Path
    config: Path
    brief: Path
    entrypoint: Path
    assets: Path
    references: Path
    working: Path
    cache: Path
    artifacts: Path
    outputs: Path
    temporary: Path
    previews: Path
    renders: Path
    checkpoint: Path
    inspection: Path
    validation: Path
    logs: Path

    def create_generated(self) -> None:
        for path in (
            self.working,
            self.cache,
            self.artifacts,
            self.outputs,
            self.temporary,
            self.previews,
            self.renders,
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class ResolvedAsset:
    id: str
    type: str
    path: Path
    checksum: str | None
    actual_checksum: str | None
    version: str | None = None
    catalog: str | None = None
    license: str | None = None
    color_space: str | None = None
    units: str | None = None
    declared: dict[str, Any] = field(default_factory=dict)

    def as_manifest(self, root: Path) -> dict[str, Any]:
        try:
            display_path = self.path.relative_to(root).as_posix()
        except ValueError:
            display_path = str(self.path)
        dependencies = []
        for dependency in self.declared.get("resolvedDependencies", []):
            dependency_path = Path(dependency["path"])
            dependencies.append({
                "path": str(dependency_path),
                "declaredChecksum": dependency.get("checksum"),
                "checksum": sha256_file(dependency_path) if dependency_path.is_file() else None,
                "bytes": dependency_path.stat().st_size if dependency_path.is_file() else None,
            })
        return {
            "id": self.id,
            "type": self.type,
            "path": display_path,
            "version": self.version,
            "catalog": self.catalog,
            "source": self.declared.get("source"),
            "license": self.license,
            "colorSpace": self.color_space,
            "units": self.units,
            "coordinateSystem": self.declared.get("coordinateSystem"),
            "preview": self.declared.get("resolvedPreview"),
            "dependencies": dependencies,
            "declaredChecksum": self.checksum,
            "checksum": self.actual_checksum,
            "bytes": self.path.stat().st_size if self.path.is_file() else None,
        }


@dataclass(slots=True)
class ResolvedLibrary:
    id: str
    version: str
    path: Path
    declared_checksum: str
    actual_checksum: str
    manifest: dict[str, Any]

    def as_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "path": str(self.path),
            "declaredChecksum": self.declared_checksum,
            "checksum": self.actual_checksum,
            "manifest": self.manifest,
        }


@dataclass(slots=True)
class Project:
    paths: ProjectPaths
    config: dict[str, Any]
    brief: dict[str, Any]
    assets: list[ResolvedAsset]
    libraries: list[ResolvedLibrary]

    @property
    def id(self) -> str:
        return str(self.config["id"])

    def resolved_variant(self, name: str | None) -> dict[str, Any]:
        if name is None:
            return {}
        variants = self.config.get("variants", {})
        require(
            name in variants,
            code="CONFIG_VARIANT_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unknown variant {name!r}",
            remediation=f"Choose one of: {', '.join(sorted(variants)) or '(none)' }.",
        )
        resolved: dict[str, Any] = {}
        active: set[str] = set()
        visited: set[str] = set()

        def merge(current: str) -> None:
            if current in active:
                chain = " -> ".join([*active, current])
                raise BlendError(
                    code="CONFIG_VARIANT_CYCLE",
                    category=ErrorCategory.CONFIGURATION,
                    message=f"Variant inheritance cycle: {chain}",
                    remediation="Remove the cycle from variant extends declarations.",
                )
            if current in visited:
                return
            require(
                current in variants,
                code="CONFIG_VARIANT_PARENT_UNKNOWN",
                category=ErrorCategory.CONFIGURATION,
                message=f"Variant {name!r} extends unknown variant {current!r}",
                remediation="Declare the parent variant or remove extends.",
            )
            active.add(current)
            value = variants[current]
            parent = value.get("extends")
            if parent:
                merge(str(parent))
            _deep_merge(resolved, {key: copy.deepcopy(item) for key, item in value.items() if key != "extends"})
            active.remove(current)
            visited.add(current)

        merge(name)
        resolved["id"] = name
        return resolved

    def resolved_profile(self, profile: str | None, variant: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        profiles = self.config.get("profiles", {})
        selected = profile or (variant or {}).get("profile") or ("preview" if "preview" in profiles else next(iter(profiles), None))
        require(
            selected in profiles,
            code="CONFIG_PROFILE_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unknown render profile {selected!r}",
            remediation=f"Choose one of: {', '.join(sorted(profiles)) or '(none)' }.",
        )
        value = copy.deepcopy(profiles[selected])
        for key in ("width", "height"):
            if variant and key in variant:
                value[key] = variant[key]
        return str(selected), value

    def matrix_members(self, matrix: str) -> list[dict[str, str | None]]:
        matrices = self.config.get("matrices", {})
        require(
            matrix in matrices,
            code="CONFIG_MATRIX_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unknown matrix {matrix!r}",
            remediation=f"Choose one of: {', '.join(sorted(matrices)) or '(none)' }.",
        )
        declared = matrices[matrix]
        variants = declared["variants"]
        profiles = declared.get("profiles") or [None]
        outputs = declared.get("outputs") or [None]
        members = [
            {"matrix": matrix, "variant": variant, "profile": profile, "output": output}
            for variant, profile, output in itertools.product(variants, profiles, outputs)
        ]
        for member in members:
            self.resolved_variant(str(member["variant"]))
            if member["profile"] is not None:
                self.resolved_profile(str(member["profile"]))
            if member["output"] is not None:
                self.output(str(member["output"]))
        return members

    def output(self, output_id: str) -> dict[str, Any]:
        for output in self.config.get("outputs", []):
            if output.get("id") == output_id:
                return copy.deepcopy(output)
        for output in self.config.get("exports", []):
            if output.get("id") == output_id:
                return copy.deepcopy(output)
        raise BlendError(
            code="CONFIG_OUTPUT_UNKNOWN",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unknown output {output_id!r}",
            remediation="Choose a declared output or export identifier.",
        )

    def source_files(self) -> list[Path]:
        files_to_hash = [self.paths.config, self.paths.brief, self.paths.entrypoint]
        excluded_roots = {
            self.paths.assets.resolve(),
            self.paths.working.resolve(),
            self.paths.outputs.resolve(),
            self.paths.previews.resolve(),
            self.paths.renders.resolve(),
            self.paths.references.resolve(),
            (self.paths.root / "libraries").resolve(),
        }
        for path in sorted(self.paths.root.rglob("*.py")):
            resolved = path.resolve()
            if any(part.startswith(".") for part in path.relative_to(self.paths.root).parts):
                continue
            if any(resolved == excluded or excluded in resolved.parents for excluded in excluded_roots):
                continue
            files_to_hash.append(resolved)
        files_to_hash.extend(asset.path for asset in self.assets if asset.path.is_file())
        if self.paths.references.exists():
            files_to_hash.extend(
                path
                for path in sorted(self.paths.references.rglob("*"))
                if path.is_file()
            )
        for library in self.libraries:
            files_to_hash.extend(
                path
                for path in sorted(library.path.rglob("*"))
                if path.is_file()
                and not any(
                    part in {".git", "__pycache__", ".pytest_cache"}
                    for part in path.relative_to(library.path).parts
                )
            )
        return list(dict.fromkeys(path.resolve() for path in files_to_hash if path.exists()))

    def input_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self.source_files():
            try:
                display_path = path.relative_to(self.paths.root).as_posix()
            except ValueError:
                display_path = str(path)
            records.append({"path": display_path, "sha256": sha256_file(path), "bytes": path.stat().st_size})
        return sorted(records, key=lambda item: item["path"])

    def dependency_hash(self, *, profile: str | None = None, variant: str | None = None,
                        operation: str | None = None, extra: Any = None) -> str:
        runtime_root = Path(__file__).resolve().parent.parent / "blend_runtime"
        payload = {
            "files": self.input_records(),
            "blendVersion": __version__,
            "schemaVersion": SCHEMA_VERSION,
            "runtimeVersion": RUNTIME_VERSION,
            "runtimeHash": hash_tree(runtime_root, exclude_generated=False),
            "profile": self.resolved_profile(profile, self.resolved_variant(variant))[1] if profile or variant else None,
            "variant": self.resolved_variant(variant) if variant else None,
            "operation": operation,
            "extra": extra,
        }
        return sha256_bytes(canonical_json(payload))


def _deep_merge(destination: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(destination.get(key), dict):
            _deep_merge(destination[key], value)
        else:
            destination[key] = value


def migrate_config(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    migrated = copy.deepcopy(value)
    changes: list[dict[str, Any]] = []
    version = migrated.get("schema", migrated.get("schemaVersion", 0))
    if version == SCHEMA_VERSION:
        return migrated, changes
    if version != 0:
        raise BlendError(
            code="CONFIG_SCHEMA_UNSUPPORTED",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unsupported project schema {version!r}",
            remediation=f"Use a Blend version that supports schema {version!r} before migrating to {SCHEMA_VERSION}.",
        )
    if "schemaVersion" in migrated:
        migrated.pop("schemaVersion")
        changes.append({"path": "/schemaVersion", "action": "removed", "reason": "renamed to schema"})
    migrated["schema"] = 1
    changes.append({"path": "/schema", "action": "set", "value": 1})
    renames = {"renderProfiles": "profiles", "scene": "entrypoint", "briefPath": "brief"}
    for old, new in renames.items():
        if old in migrated and new not in migrated:
            migrated[new] = migrated.pop(old)
            changes.append({"path": f"/{old}", "action": "renamed", "to": f"/{new}"})
    project = migrated.setdefault("project", {})
    for old, new in (("fps", "frameRate"), ("startFrame", "frameStart"), ("endFrame", "frameEnd")):
        if old in migrated and new not in project:
            project[new] = migrated.pop(old)
            changes.append({"path": f"/{old}", "action": "moved", "to": f"/project/{new}"})
    return migrated, changes


def migrate_manifest(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    migrated = copy.deepcopy(value)
    changes: list[dict[str, Any]] = []
    version = migrated.get("schema", 0)
    if version == 1:
        return migrated, changes
    if version != 0:
        raise BlendError(
            code="MANIFEST_SCHEMA_UNSUPPORTED",
            category=ErrorCategory.CONFIGURATION,
            message=f"Unsupported manifest schema {version!r}",
            remediation="Use the Blend version that created this manifest before migration.",
        )
    migrated["schema"] = 1
    if "blend" in migrated and "blendVersion" not in migrated:
        migrated["blendVersion"] = migrated.pop("blend")
        changes.append({"path": "/blend", "action": "renamed", "to": "/blendVersion"})
    changes.append({"path": "/schema", "action": "set", "value": 1})
    return migrated, changes


def _resolve_project_path(root: Path, value: str, *, label: str, allowed_root: Path | None = None) -> Path:
    path = Path(value).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve(strict=False)
    if allowed_root is not None:
        try:
            ensure_within(resolved, allowed_root, label=label)
        except ValueError as exc:
            raise BlendError(
                code="CONFIG_PATH_OUTSIDE_ROOT",
                category=ErrorCategory.CONFIGURATION,
                message=str(exc),
                remediation="Move the path under its declared root or explicitly configure an allowed root.",
                details={"path": str(resolved), "root": str(allowed_root)},
            ) from exc
    return resolved


def _project_paths(root: Path, config: dict[str, Any]) -> ProjectPaths:
    roots = config.get("roots", {})

    def declared(name: str, default: Path) -> Path:
        value = roots.get(name)
        return _resolve_project_path(root, value, label=f"{name} root") if value else default.resolve()

    assets = declared("assets", root / "assets")
    working = declared("working", root / "build")
    cache = declared("cache", working / "cache")
    artifacts = declared("artifacts", working / "manifests")
    outputs = declared("outputs", root / "output")
    temporary = declared("temporary", working / "tmp")
    entrypoint = _resolve_project_path(root, str(config.get("entrypoint", "scene.py")), label="entry point", allowed_root=root)
    brief = _resolve_project_path(root, str(config.get("brief", "brief.json")), label="brief", allowed_root=root)
    return ProjectPaths(
        root=root,
        config=root / "blend.yaml",
        brief=brief,
        entrypoint=entrypoint,
        assets=assets,
        references=root / "references",
        working=working,
        cache=cache,
        artifacts=artifacts,
        outputs=outputs,
        temporary=temporary,
        previews=root / "previews",
        renders=root / "renders",
        checkpoint=working / "scene.blend",
        inspection=working / "inspection.json",
        validation=working / "validation.json",
        logs=working / "logs",
    )


def _catalog_entries(
    root: Path,
    config: dict[str, Any],
    assets_root: Path,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for catalog_ref in config.get("catalogs", []):
        path = _resolve_project_path(
            root,
            catalog_ref["path"],
            label=f"catalog {catalog_ref['id']}",
            allowed_root=assets_root,
        )
        require(
            path.is_file(),
            code="ASSET_CATALOG_MISSING",
            category=ErrorCategory.ASSET,
            message=f"Asset catalog does not exist: {path}",
            remediation="Restore the declared catalog or update its path.",
        )
        if catalog_ref.get("checksum") and sha256_file(path) != catalog_ref["checksum"]:
            raise BlendError(
                code="ASSET_CATALOG_CHECKSUM_DRIFT",
                category=ErrorCategory.ASSET,
                message=f"Asset catalog checksum drifted: {path}",
                remediation="Review the catalog update and pin its new checksum explicitly.",
            )
        catalog = load_json(path)
        errors = schema_errors(catalog, "catalog-v1.json")
        if errors:
            raise BlendError(
                code="ASSET_CATALOG_SCHEMA_INVALID",
                category=ErrorCategory.ASSET,
                message=f"Asset catalog is invalid: {path}",
                remediation="Correct the catalog against catalog-v1.json.",
                details={"errors": errors},
            )
        require(
            catalog.get("id") == catalog_ref["id"],
            code="ASSET_CATALOG_ID_MISMATCH",
            category=ErrorCategory.ASSET,
            message=f"Catalog identity does not match its project declaration: {path}",
            remediation="Correct the declared catalog id or the catalog manifest id.",
        )
        for entry in catalog["assets"]:
            resolved_entry = copy.deepcopy(entry)
            entry_path = Path(entry["path"])
            resolved_entry_path = (
                entry_path if entry_path.is_absolute() else path.parent / entry_path
            ).resolve()
            try:
                ensure_within(resolved_entry_path, assets_root, label=f"catalog asset {entry['id']}")
            except ValueError as exc:
                raise BlendError(
                    code="ASSET_CATALOG_PATH_OUTSIDE_ROOT",
                    category=ErrorCategory.ASSET,
                    message=str(exc),
                    remediation="Package catalog assets and dependencies under the declared asset root.",
                ) from exc
            resolved_entry["resolvedPath"] = str(resolved_entry_path)
            resolved_entry["resolvedDependencies"] = []
            for dependency in entry.get("dependencies", []):
                dependency_record = dependency if isinstance(dependency, dict) else {"path": dependency}
                dependency_path = Path(dependency_record["path"])
                resolved_dependency = (
                    dependency_path
                    if dependency_path.is_absolute()
                    else path.parent / dependency_path
                ).resolve()
                try:
                    ensure_within(
                        resolved_dependency,
                        assets_root,
                        label=f"catalog asset {entry['id']} dependency",
                    )
                except ValueError as exc:
                    raise BlendError(
                        code="ASSET_CATALOG_PATH_OUTSIDE_ROOT",
                        category=ErrorCategory.ASSET,
                        message=str(exc),
                        remediation="Package catalog assets and dependencies under the declared asset root.",
                    ) from exc
                resolved_entry["resolvedDependencies"].append({
                    **dependency_record,
                    "path": str(resolved_dependency),
                })
            if entry.get("preview"):
                preview = Path(entry["preview"])
                resolved_preview = (
                    preview if preview.is_absolute() else path.parent / preview
                ).resolve()
                try:
                    ensure_within(
                        resolved_preview,
                        assets_root,
                        label=f"catalog asset {entry['id']} preview",
                    )
                except ValueError as exc:
                    raise BlendError(
                        code="ASSET_CATALOG_PATH_OUTSIDE_ROOT",
                        category=ErrorCategory.ASSET,
                        message=str(exc),
                        remediation="Package catalog previews under the declared asset root.",
                    ) from exc
                resolved_entry["resolvedPreview"] = str(resolved_preview)
            key = (catalog_ref["id"], entry["id"], entry["version"])
            require(
                key not in result,
                code="ASSET_CATALOG_ENTRY_DUPLICATE",
                category=ErrorCategory.ASSET,
                message=f"Duplicate catalog entry: {catalog_ref['id']}/{entry['id']}@{entry['version']}",
                remediation="Keep exactly one record for each catalog asset version.",
            )
            result[(catalog_ref["id"], entry["id"], entry["version"])] = resolved_entry
    return result


def _resolve_assets(root: Path, paths: ProjectPaths, config: dict[str, Any]) -> list[ResolvedAsset]:
    catalog = _catalog_entries(root, config, paths.assets)
    result: list[ResolvedAsset] = []
    for declared in config.get("assets", []):
        selected = declared
        catalog_id: str | None = None
        version: str | None = None
        if "catalog" in declared:
            catalog_id = str(declared["catalog"])
            version = str(declared["version"])
            key = (catalog_id, declared["id"], version)
            require(
                key in catalog,
                code="ASSET_CATALOG_VERSION_MISSING",
                category=ErrorCategory.ASSET,
                message=f"Catalog asset not found: {catalog_id}/{declared['id']}@{version}",
                remediation="Add the exact version to the catalog or update the project pin.",
            )
            selected = {**catalog[key], **declared, "path": catalog[key]["resolvedPath"]}
        asset_path = _resolve_project_path(root, str(selected["path"]), label=f"asset {declared['id']}")
        actual = sha256_file(asset_path) if asset_path.is_file() else None
        result.append(ResolvedAsset(
            id=str(declared["id"]),
            type=str(declared["type"]),
            path=asset_path,
            checksum=selected.get("checksum"),
            actual_checksum=actual,
            version=version,
            catalog=catalog_id,
            license=selected.get("license"),
            color_space=selected.get("colorSpace"),
            units=selected.get("units"),
            declared=copy.deepcopy(selected),
        ))
    return result


def _resolve_libraries(root: Path, config: dict[str, Any]) -> list[ResolvedLibrary]:
    result: list[ResolvedLibrary] = []
    for declared in config.get("libraries", []):
        path = _resolve_project_path(root, declared["path"], label=f"library {declared['id']}")
        manifest_path = path / "blend-library.json"
        require(
            manifest_path.is_file(),
            code="LIBRARY_MANIFEST_MISSING",
            category=ErrorCategory.ASSET,
            message=f"Library manifest is missing: {manifest_path}",
            remediation="Restore blend-library.json or remove the library declaration.",
        )
        manifest = load_json(manifest_path)
        errors = schema_errors(manifest, "library-v1.json")
        if errors:
            raise BlendError(
                code="LIBRARY_SCHEMA_INVALID",
                category=ErrorCategory.ASSET,
                message=f"Library manifest is invalid: {manifest_path}",
                remediation="Correct the library against library-v1.json.",
                details={"errors": errors},
            )
        actual = hash_tree(path, exclude_generated=False)
        require(
            manifest["id"] == declared["id"],
            code="LIBRARY_ID_MISMATCH",
            category=ErrorCategory.ASSET,
            message=f"Library identity does not match its project declaration: {path}",
            remediation="Pin the matching library id or correct blend-library.json.",
            details={"declared": declared["id"], "measured": manifest["id"]},
        )
        require(
            manifest["version"] == declared["version"],
            code="LIBRARY_VERSION_DRIFT",
            category=ErrorCategory.ASSET,
            message=f"Library version drifted at {path}.",
            remediation="Run blend library compare, then explicitly update the pin if approved.",
            details={"declared": declared["version"], "measured": manifest["version"]},
        )
        require(
            actual == declared["checksum"],
            code="LIBRARY_CHECKSUM_DRIFT",
            category=ErrorCategory.ASSET,
            message=f"Library content checksum drifted at {path}.",
            remediation="Run blend library compare, then explicitly update the pin if approved.",
            details={"declared": declared["checksum"], "measured": actual},
        )
        for asset in manifest["assets"]:
            asset_path = (path / asset["path"]).resolve()
            try:
                ensure_within(asset_path, path, label=f"library asset {asset['id']}")
            except ValueError as exc:
                raise BlendError(
                    code="LIBRARY_ASSET_OUTSIDE_ROOT",
                    category=ErrorCategory.ASSET,
                    message=str(exc),
                    remediation="Package every transitive library asset under the pinned library root.",
                ) from exc
            require(
                asset_path.is_file() and asset_path.stat().st_size > 0,
                code="LIBRARY_ASSET_MISSING",
                category=ErrorCategory.ASSET,
                message=f"Library asset is missing or empty: {asset_path}",
                remediation="Restore the exact pinned library package.",
                details={"library": declared["id"], "asset": asset["id"]},
            )
            require(
                sha256_file(asset_path) == asset["checksum"],
                code="LIBRARY_ASSET_CHECKSUM_DRIFT",
                category=ErrorCategory.ASSET,
                message=f"Library transitive asset drifted: {asset_path}",
                remediation="Restore the pinned asset or compare and explicitly update the library.",
                details={"library": declared["id"], "asset": asset["id"]},
            )
        result.append(ResolvedLibrary(
            id=declared["id"],
            version=declared["version"],
            path=path,
            declared_checksum=declared["checksum"],
            actual_checksum=actual,
            manifest=manifest,
        ))
    return result


def load_project(project: str | Path, *, allow_migration: bool = False, create_generated: bool = False) -> Project:
    root = Path(project).expanduser().resolve()
    require(
        root.is_dir(),
        code="CONFIG_PROJECT_NOT_FOUND",
        category=ErrorCategory.CONFIGURATION,
        message=f"Project directory does not exist: {root}",
        remediation="Pass a project directory containing blend.yaml.",
    )
    config_path = root / "blend.yaml"
    require(
        config_path.is_file(),
        code="CONFIG_FILE_MISSING",
        category=ErrorCategory.CONFIGURATION,
        message=f"Project configuration does not exist: {config_path}",
        remediation="Run blend init or restore blend.yaml.",
    )
    raw = load_yaml(config_path)
    require(
        isinstance(raw, dict),
        code="CONFIG_ROOT_TYPE",
        category=ErrorCategory.CONFIGURATION,
        message="blend.yaml must contain a mapping at its root.",
        remediation="Replace the root value with a configuration object.",
    )
    config, changes = migrate_config(raw)
    if changes and not allow_migration:
        raise BlendError(
            code="CONFIG_MIGRATION_REQUIRED",
            category=ErrorCategory.CONFIGURATION,
            message=f"Project schema {raw.get('schema', raw.get('schemaVersion', 0))} requires migration.",
            remediation=f"Run blend migrate {root} and review the exact structural changes.",
            details={"changes": changes},
        )
    errors = schema_errors(config, "config-v1.json")
    if errors:
        raise BlendError(
            code="CONFIG_SCHEMA_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=f"Project configuration failed schema validation ({len(errors)} finding(s)).",
            remediation="Correct blend.yaml using the reported JSON-pointer paths.",
            details={"errors": errors},
        )
    try:
        safe_id(str(config["id"]), label="project id")
    except ValueError as exc:
        raise BlendError(
            code="CONFIG_PROJECT_ID_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=str(exc),
            remediation="Use letters, numbers, dots, underscores, or hyphens.",
        ) from exc
    paths = _project_paths(root, config)
    require(
        paths.brief.is_file(),
        code="BRIEF_FILE_MISSING",
        category=ErrorCategory.CONFIGURATION,
        message=f"Brief does not exist: {paths.brief}",
        remediation="Restore the declared brief or update blend.yaml.",
    )
    require(
        paths.entrypoint.is_file(),
        code="CONFIG_ENTRYPOINT_MISSING",
        category=ErrorCategory.CONFIGURATION,
        message=f"Scene entry point does not exist: {paths.entrypoint}",
        remediation="Restore the declared scene.py or update blend.yaml.",
    )
    brief = load_json(paths.brief)
    brief_errors = schema_errors(brief, "brief-v1.json")
    if brief_errors:
        raise BlendError(
            code="BRIEF_SCHEMA_INVALID",
            category=ErrorCategory.CONFIGURATION,
            message=f"Creative brief failed schema validation ({len(brief_errors)} finding(s)).",
            remediation="Correct the brief using the reported JSON-pointer paths.",
            details={"errors": brief_errors},
        )
    for collection_name in ("assets", "catalogs", "libraries", "views", "simulations", "exports", "outputs"):
        identifiers = [
            item["id"]
            for item in config.get(collection_name, [])
            if isinstance(item, dict) and "id" in item
        ]
        require(
            len(identifiers) == len(set(identifiers)),
            code="CONFIG_IDENTIFIER_DUPLICATE",
            category=ErrorCategory.CONFIGURATION,
            message=f"Duplicate identifiers are not allowed in {collection_name}.",
            remediation=f"Give every {collection_name} entry a unique id.",
            details={"collection": collection_name, "identifiers": identifiers},
        )
    frame_start = config["project"]["frameStart"]
    frame_end = config["project"]["frameEnd"]
    require(
        frame_end >= frame_start,
        code="CONFIG_FRAME_RANGE_INVALID",
        category=ErrorCategory.CONFIGURATION,
        message=f"Frame range is reversed: {frame_start}..{frame_end}",
        remediation="Set frameEnd greater than or equal to frameStart.",
    )
    provisional = Project(paths, config, brief, [], [])
    for name in config.get("variants", {}):
        # Resolve every variant now so inheritance failures are configuration failures.
        provisional.resolved_variant(name)
    for output in [*config.get("outputs", []), *config.get("exports", [])]:
        if output.get("variant"):
            provisional.resolved_variant(output["variant"])
        if output.get("profile"):
            provisional.resolved_profile(output["profile"])
        if output.get("audio"):
            audio_asset = next(
                (
                    asset
                    for asset in config.get("assets", [])
                    if asset.get("id") == output["audio"] and asset.get("type") == "audio"
                ),
                None,
            )
            require(
                audio_asset is not None,
                code="CONFIG_OUTPUT_AUDIO_UNDECLARED",
                category=ErrorCategory.CONFIGURATION,
                message=f"Output {output['id']!r} references undeclared audio {output['audio']!r}.",
                remediation="Declare the audio in assets with type audio and reference its id.",
            )
        if output.get("type") in {"video", "preview-video"}:
            require(
                bool(output.get("codec")),
                code="CONFIG_OUTPUT_CODEC_MISSING",
                category=ErrorCategory.CONFIGURATION,
                message=f"Output {output['id']!r} requires a codec.",
                remediation="Declare h264, hevc, or prores for video outputs.",
            )
        if output.get("type") == "still":
            frame = output.get("frame", frame_start)
            require(
                frame_start <= frame <= frame_end,
                code="CONFIG_OUTPUT_FRAME_INVALID",
                category=ErrorCategory.CONFIGURATION,
                message=f"Still output {output['id']!r} frame {frame} is outside the project range.",
                remediation=f"Choose a frame from {frame_start} through {frame_end}.",
            )
        output_type = output.get("type")
        if output_type in {"video", "preview-video"}:
            suffix = Path(output["path"]).suffix.lower()
            allowed_extensions = {
                "h264": {".mp4", ".mov", ".mkv"},
                "hevc": {".mp4", ".mov", ".mkv"},
                "prores": {".mov"},
                "prores-alpha": {".mov"},
                "ffv1": {".mkv", ".mov"},
            }
            require(
                suffix in allowed_extensions.get(output["codec"], set()),
                code="CONFIG_OUTPUT_CONTAINER_INVALID",
                category=ErrorCategory.CONFIGURATION,
                message=f"Output {output['id']!r} container is incompatible with {output['codec']}.",
                remediation="Use a supported path extension for the declared codec.",
            )
            if output.get("alpha"):
                require(
                    output["codec"] in {"prores-alpha", "ffv1"},
                    code="CONFIG_OUTPUT_ALPHA_CODEC_INVALID",
                    category=ErrorCategory.CONFIGURATION,
                    message=f"Output {output['id']!r} requests alpha with an opaque codec.",
                    remediation="Use prores-alpha or ffv1 for alpha video.",
                )
        if output_type == "still":
            resolved_output_variant = provisional.resolved_variant(output.get("variant"))
            _, output_profile = provisional.resolved_profile(
                output.get("profile") or "final",
                resolved_output_variant,
            )
            expected_suffix = {
                "PNG": ".png",
                "JPEG": ".jpg",
                "TIFF": ".tif",
                "OPEN_EXR": ".exr",
            }[output_profile["format"]]
            require(
                Path(output["path"]).suffix.lower() in {
                    expected_suffix,
                    ".jpeg" if expected_suffix == ".jpg" else expected_suffix,
                    ".tiff" if expected_suffix == ".tif" else expected_suffix,
                },
                code="CONFIG_OUTPUT_STILL_EXTENSION_INVALID",
                category=ErrorCategory.CONFIGURATION,
                message=f"Still output {output['id']!r} extension does not match its render profile.",
                remediation=f"Use {expected_suffix} or change the declared render format.",
            )
        if output.get("type") == "preview-video" and output.get("view"):
            view_ids = {view["id"] for view in config.get("views", [])}
            require(
                output["view"] in view_ids,
                code="CONFIG_OUTPUT_VIEW_UNKNOWN",
                category=ErrorCategory.CONFIGURATION,
                message=f"Preview-video output {output['id']!r} references an unknown view.",
                remediation=f"Choose one of: {', '.join(sorted(view_ids)) or '(none)'}.",
            )
    all_output_ids = [item["id"] for item in [*config.get("outputs", []), *config.get("exports", [])]]
    require(
        len(all_output_ids) == len(set(all_output_ids)),
        code="CONFIG_OUTPUT_ID_DUPLICATE",
        category=ErrorCategory.CONFIGURATION,
        message="Output and export identifiers must be unique.",
        remediation="Rename duplicate output or export identifiers.",
    )
    for matrix_name in config.get("matrices", {}):
        provisional.matrix_members(matrix_name)
    simulation_profiles = config.get("simulationProfiles", {})
    referenced_simulation_profiles = [
        (f"profiles.{name}.simulationProfile", profile.get("simulationProfile"))
        for name, profile in config["profiles"].items()
        if profile.get("simulationProfile")
    ]
    for simulation in config.get("simulations", []):
        for key in ("previewProfile", "finalProfile"):
            if simulation.get(key):
                referenced_simulation_profiles.append(
                    (f"simulations.{simulation['id']}.{key}", simulation[key])
                )
    missing_simulation_profiles = [
        {"path": path, "profile": profile}
        for path, profile in referenced_simulation_profiles
        if profile not in simulation_profiles
    ]
    require(
        not missing_simulation_profiles,
        code="CONFIG_SIMULATION_PROFILE_UNKNOWN",
        category=ErrorCategory.CONFIGURATION,
        message="Configuration references undeclared simulation profiles.",
        remediation="Declare each referenced simulationProfiles entry or correct the reference.",
        details={"missing": missing_simulation_profiles},
    )
    if create_generated:
        paths.create_generated()
    return Project(paths, config, brief, _resolve_assets(root, paths, config), _resolve_libraries(root, config))


def migrate_project(project: str | Path, *, write: bool) -> dict[str, Any]:
    root = Path(project).expanduser().resolve()
    config_path = root / "blend.yaml"
    require(
        config_path.is_file(),
        code="CONFIG_FILE_MISSING",
        category=ErrorCategory.CONFIGURATION,
        message=f"Project configuration does not exist: {config_path}",
        remediation="Pass a project containing blend.yaml.",
    )
    raw = load_yaml(config_path)
    migrated, changes = migrate_config(raw)
    errors = schema_errors(migrated, "config-v1.json")
    if errors:
        raise BlendError(
            code="CONFIG_MIGRATION_INVALID_RESULT",
            category=ErrorCategory.CONFIGURATION,
            message="The migrated configuration would remain invalid; nothing was written.",
            remediation="Correct the reported legacy fields before migration.",
            details={"changes": changes, "errors": errors},
        )
    artifact_root = _project_paths(root, migrated).artifacts
    manifest_migrations = []
    if artifact_root.is_dir():
        for path in sorted(artifact_root.glob("*.json")):
            try:
                value = load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or not value.get("operation") or value.get("schema") == 1:
                continue
            migrated_manifest, manifest_changes = migrate_manifest(value)
            manifest_errors = schema_errors(migrated_manifest, "manifest-v1.json")
            if manifest_errors:
                raise BlendError(
                    code="MANIFEST_MIGRATION_INVALID_RESULT",
                    category=ErrorCategory.CONFIGURATION,
                    message=f"Manifest migration would remain invalid: {path}",
                    remediation="Use the Blend version that created the manifest or correct its legacy structure.",
                    details={
                        "path": str(path),
                        "changes": manifest_changes,
                        "errors": manifest_errors,
                    },
                )
            manifest_migrations.append({
                "path": str(path),
                "fromSchema": value.get("schema", 0),
                "toSchema": 1,
                "changes": manifest_changes,
                "value": migrated_manifest,
            })
    if write and changes:
        backup = config_path.with_suffix(".yaml.pre-migration")
        if not backup.exists():
            atomic_write_yaml(backup, raw)
        atomic_write_yaml(config_path, migrated)
    if write:
        for record in manifest_migrations:
            path = Path(record["path"])
            backup = path.with_suffix(path.suffix + ".pre-migration")
            if not backup.exists():
                atomic_write_json(backup, load_json(path))
            atomic_write_json(path, record["value"])
    return {
        "fromSchema": raw.get("schema", raw.get("schemaVersion", 0)),
        "toSchema": 1,
        "changes": changes,
        "manifests": [
            {key: value for key, value in record.items() if key != "value"}
            for record in manifest_migrations
        ],
        "written": bool(write and (changes or manifest_migrations)),
    }


def write_resolved_runtime_config(project: Project, *, operation: str, profile: str | None,
                                  variant: str | None, output: str | None = None,
                                  jobs: list[dict[str, Any]] | None = None,
                                  frames: list[int] | None = None,
                                  operation_id: str | None = None) -> Path:
    project.paths.create_generated()
    resolved_variant = project.resolved_variant(variant)
    profile_name, resolved_profile = project.resolved_profile(profile, resolved_variant)
    value = {
        "schema": 1,
        "operation": operation,
        "operationId": operation_id,
        "projectRoot": str(project.paths.root),
        "workingRoot": str(project.paths.working),
        "cacheRoot": str(project.paths.cache),
        "artifactRoot": str(project.paths.artifacts),
        "outputRoot": str(project.paths.outputs),
        "temporaryRoot": str(project.paths.temporary),
        "previewRoot": str(project.paths.previews),
        "renderRoot": str(project.paths.renders),
        "checkpoint": str(project.paths.checkpoint),
        "inspection": str(project.paths.inspection),
        "validation": str(project.paths.validation),
        "config": project.config,
        "brief": project.brief,
        "profileName": profile_name,
        "profile": resolved_profile,
        "variantName": variant,
        "variant": resolved_variant,
        "output": project.output(output) if output else None,
        "assets": [asset.as_manifest(project.paths.root) | {"resolvedPath": str(asset.path)} for asset in project.assets],
        "libraries": [library.as_manifest() for library in project.libraries],
        "jobs": jobs or [],
        "frames": frames or [],
        "dependencyHash": project.dependency_hash(profile=profile_name, variant=variant, operation=operation,
                                                   extra={"output": output, "jobs": jobs, "frames": frames}),
    }
    path = project.paths.temporary / f"runtime-{operation_id or os.getpid()}.json"
    atomic_write_json(path, value)
    return path


def output_paths(project: Project, *, variants: Iterable[str | None] | None = None) -> list[dict[str, Any]]:
    selected_variants = list(variants) if variants is not None else [None]
    records: list[dict[str, Any]] = []
    for output in [*project.config.get("outputs", []), *project.config.get("exports", [])]:
        output_variant = output.get("variant")
        candidates = [output_variant] if output_variant else selected_variants
        for variant in candidates:
            path_text = str(output["path"])
            if variant:
                path_text = path_text.replace("{variant}", str(variant))
            profile = output.get("profile")
            path = _resolve_project_path(project.paths.root, path_text, label=f"output {output['id']}",
                                         allowed_root=project.paths.outputs)
            records.append({"id": output["id"], "variant": variant, "profile": profile, "path": path})
    return records
