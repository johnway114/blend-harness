# Blend

Blend is a local command-line harness for using Blender as a source-controlled 3D compiler and rendering engine. Python scene source, a structured brief, declared assets, and `blend.yaml` are authoritative. Generated `.blend` checkpoints, previews, frame sequences, exports, and delivery media are artifacts.

## Requirements

- macOS or Linux
- Python 3.11 or newer
- Blender from a supported line in [`blend_harness/compatibility.json`](blend_harness/compatibility.json)
- FFmpeg and FFprobe for encoded media

Blend currently maintains Blender 4.5 LTS and Blender 5.2 stable compatibility contracts. `blend doctor` reports the exact capability result for the installed host.

## Install

From a source checkout:

```sh
python3 -m pip install .
blend doctor --json
```

For an isolated per-user installation:

```sh
./scripts/install.sh
~/.local/bin/blend doctor --json
```

The same installer upgrades an existing managed installation. Remove only that managed installation with `./scripts/uninstall.sh`. See [`docs/install.md`](docs/install.md).

## Start a project

```sh
blend --json init brand-ident ./my-ident
cd ./my-ident
blend --json validate-config .
blend --json trust .
blend --json build . --trust
blend --json preview . --profile preview --variant amber --trust
blend --json inspect . --profile final --variant square-amber --trust
blend --json validate . --profile final --variant square-amber --trust
blend --json render . --profile final --variant square-amber --trust
blend --json encode . --output square-film
blend --json review .
```

`--json` is the authoritative interface. Every result contains a schema version, operation identifier, status, duration, bounded summary, artifact paths, warnings, next actions, progress, and a stable structured error when the command fails.

## Production workflow

```text
brief + configuration + scene.py + declared assets
    -> plan
    -> build
    -> preview + inspect
    -> validate
    -> restartable frame render
    -> encode or export
    -> compare + review
```

Use `blend plan` before expensive work. Final animation always renders atomic image frames first. `blend resume` verifies file content and manifest inputs, then renders only missing, corrupt, or stale frames. Encoding is independent and repeatable from the same validated frame manifest.

Simulation projects use `blend bake`, `blend cache inspect`, and `blend cache clean`. Model projects can declare GLB or glTF, USD, FBX, OBJ, Alembic, and STL exports. Export happens in staging, is decoded in a clean Blender process, and is promoted only after measurable profile checks pass.

## Security boundary

`scene.py` and declared project libraries are executable Python with the current user's filesystem privileges. Blend does not claim to sandbox Blender Python completely.

- Review source before `--trust` or `blend trust`.
- Network access is denied by default with a host process wrapper.
- Network requires both `blender.offline: false` and `--allow-network`.
- Environment variables are allowlisted and secret-like names are rejected.
- Output, cache, and temporary paths are constrained to declared roots.
- Blender and FFmpeg run in owned process groups with timeouts and cleanup.

See [`docs/security.md`](docs/security.md).

## Reference projects

The built-in `brand-ident`, `product-turntable`, and `procedural-explainer` projects are executable conformance fixtures. Each includes a brief, source, pinned assets, expected structural evidence, output declarations, a clean-workstation command sequence, and a retained visual contact-sheet baseline. `empty` provides only the project and runtime contract.

## Documentation

- [`docs/operations.md`](docs/operations.md): command behavior, roots, recovery, and idempotency
- [`docs/configuration.md`](docs/configuration.md): project and template authoring
- [`docs/validation.md`](docs/validation.md): mechanical policy and stable rules
- [`docs/manifests.md`](docs/manifests.md): provenance and integration contracts
- [`docs/mcp.md`](docs/mcp.md): typed MCP stdio tools and cancellation
- [`docs/migrations.md`](docs/migrations.md): schema and template upgrade workflow
- [`docs/reference-projects.md`](docs/reference-projects.md): acceptance fixtures
- [`docs/compatibility.md`](docs/compatibility.md): supported Blender and host lines

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests tagged `blender`, `ffmpeg`, or `slow` exercise installed production tooling. They are not mocks for the Blender contract.
