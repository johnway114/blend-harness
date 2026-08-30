# Project and template authoring

## Project layout

A project is identified by `blend.yaml`. The built-in templates use:

```text
blend.yaml
brief.yaml
scene.py
modules/
assets/
libraries/
references/
build/
previews/
renders/
output/
```

Only configuration, brief, scene source, modules, declared assets, libraries, and references are authoritative. Generated roots may be removed and rebuilt.

## Configuration contract

`blend.yaml` declares:

- schema version and project identity
- all source and generated roots
- entry-point scene module
- deterministic global, procedural, and simulation seeds
- Blender executable, offline policy, and environment allowlist
- color management and alpha policy
- render profiles with engine, resolution, frame range, sampling, device, output format, view transform, and limits
- named variants and Cartesian matrices
- local assets with exact type, path, checksum, license, and coordinate assumptions
- asset catalogs and reusable libraries with identifiers, semantic versions, and checksums
- simulation caches and validation profiles
- still, sequence, movie, and interchange outputs
- bounded parameter searches and measurable ranking metrics
- hard resources for processes, memory, storage, resolution, frames, threads, and operation duration

Validate without executing source:

```sh
blend --json validate-config /absolute/project/path
```

Unknown fields fail. Paths are normalized before use. Outputs may not escape or overlap source roots. Every locally consumed asset must be declared and checksum pinned.

## Brief

`brief.yaml` records the creative and deliverable contract in data rather than hidden script constants:

- intent and audience
- target duration and aspect ratios
- visual hierarchy and style constraints
- exact required outputs
- validation profile
- review criteria

Put reusable creative dimensions in variants. Put host and delivery mechanics in profiles and outputs. `blend config diff` can then distinguish creative-only changes from operational changes.

## Scene entry point

The default runtime API is intentionally small:

```python
from blend_runtime import scene


def build(context):
    # Create collections, objects, materials, cameras, lights, and animation.
    scene.record_provenance("model", context.asset("model"))


def validate(context):
    # Optional project-specific measurable facts, not subjective approvals.
    return {"expectedCollection": "Product"}
```

The entry point executes inside Blender with a resolved context containing configuration, brief, profile, variant, assets, libraries, seeds, and operation paths. It must not discover undeclared files or read secrets from the ambient environment.

Use `context.asset(id)` and `context.library(id)` rather than hardcoded repository-relative paths. Keep expensive simulation in declared `bake` operations. Build must remain deterministic from resolved inputs.

## Assets, catalogs, and libraries

A local asset declaration pins its bytes and metadata. Fonts additionally declare the font file, license identifier, and license text path. Audio used by an output must be a declared audio asset.

Catalog records pin an asset ID and version, source file, coordinate system and units, preview, primary checksum, and every transitive dependency. A changed MTL or texture must invalidate an OBJ catalog entry even if the OBJ did not change.

A reusable library contains `blend-library.json`. The project pins its ID, version, and whole-directory checksum. Executable Python inside a library is part of the trust boundary. Use `blend library compare` and explicit `blend library update`; silent drift fails.

## Profiles, variants, and matrices

- **Profile:** operational render settings and validation mode.
- **Variant:** named creative values.
- **Matrix:** declared profile and variant combinations.

Do not encode variant choice in filenames inside scene source. The harness derives collision-safe output keys and rejects two jobs that would write the same destination.

Preview settings are independent of final settings. A profile can specify preview resolution, samples, engine, frame sample strategy, views, passes, modes, and alpha mode without mutating its final render contract.

## Exports

An export output declares format, source checkpoint/profile/variant, selected objects, transforms, units, scale, axes, animation, materials, textures, hidden-helper policy, stable-name policy, format options, and a validation profile. The exporter selects only declared objects, stages dependencies, decodes the produced file in a clean Blender process, and compares structural measurements before promotion.

## Authoring a template

A complete template contains:

1. valid `blend.yaml` and `brief.yaml`
2. deterministic `scene.py` and any modules
3. only declared, pinned assets and libraries
4. `COMMANDS.txt` for a clean workstation
5. `expected.json` conforming to `reference-expectation-v1.json`
6. a retained visual contact-sheet baseline
7. structural, validation, delivery, restartability, and error-path expectations

Initialize from a built-in template with `blend init`. The destination must not exist or must be empty. Template upgrade is comparison-only; project creative source is never silently replaced.
