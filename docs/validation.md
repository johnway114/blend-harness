# Validation

Blend validation is mechanical and evidence based. It reports observations and policy failures; it does not claim subjective creative approval.

## Layers

1. **Configuration validation** parses strict versioned schemas, rejects unknown fields, resolves normalized paths, and verifies declarations without executing project Python.
2. **Inspection** opens the retained checkpoint in a clean Blender process and emits deterministic scene evidence.
3. **Profile validation** evaluates that evidence against the selected validation profile and retained capability report.
4. **Export validation** decodes an interchange artifact in a second clean process and compares it with measured source evidence.
5. **Media QC** probes encoded output and checks duration, dimensions, frame rate, codec, pixel format, alpha, and audio.

Validation reports conform to `validation-v1.json`. Inspection reports conform to `inspection-v1.json`. Schema validation is a hard prerequisite; malformed generated evidence is never interpreted leniently.

## Stable rule families

| Family | Examples |
| --- | --- |
| `CONFIG.*` | schema, root overlap, output collision, environment, device, resources |
| `ASSET.*` | missing, undeclared, checksum, license, outside root, catalog/library drift |
| `SCENE.*` | required collection/object/camera/light, duplicate names, empty scene |
| `ANIMATION.*` | frame coverage, keyframes, camera-cut binding, loop closure, duration |
| `RENDER.*` | engine, samples, resolution, format, alpha, color management, view transform |
| `PERF.*` | polygon, object, texture, memory, disk, storage, process, duration budgets |
| `SIM.*` | declared seed, deterministic mode, cache completeness and freshness |
| `MODEL.*` | dimensions, transforms, normals, manifoldness, material and naming policy |
| `EXPORT.*` | selection, hidden helpers, dependencies, units, axes, scale, bounds, animation |
| `MEDIA.*` | codec, pixel format, FPS, duration, alpha, audio, decode failure |

A finding contains rule ID, severity, message, measured evidence, expected value, and remediation when actionable. Warning and error thresholds are separately declared in validation profiles.

## Inspection evidence

Inspection includes at least:

- scene and object counts by type
- deterministic object names and collections
- mesh vertices, polygons, materials, UV layers, and manifold evidence
- lights, cameras, active camera, camera markers, frame range, frame rate, keyframes, actions, and drivers
- modifiers, geometry-node groups, particles, physics, simulations, and cache declarations
- bounds, transforms, material names, texture/image references, missing files, file sizes, and units
- render engine, device, resolution, sampling, output format, alpha, view transform, and color management
- custom properties used for retained provenance

## Cache validation

Simulation cache identity includes project schema and source, simulation definition, profile, variant, frame range, Blender version, device/backend, seed, dependencies, and effective solver settings. Cache inspection distinguishes missing, incomplete, corrupt, stale, and valid. Final validation cannot reuse a preview cache.

## Output collisions

All planned output paths are checked before Blender starts. The validator expands variants and matrices and rejects:

- two jobs resolving to the same frame or artifact path
- a generated root overlapping a source root
- a temporary/staging path outside the declared temporary root
- a direct animation-to-container render
- an existing unowned destination

## Hard resources

Projects declare process, thread, memory, storage, frame, resolution, and duration limits. Planning computes aggregate matrix storage, not only one-job storage. Hard-limit breaches block before expensive work. Runtime timeouts terminate the owned process group.

## Running validation

```sh
blend --json validate-config /absolute/project/path
blend --json inspect /absolute/project/path --profile final --variant graphite --trust
blend --json validate /absolute/project/path --profile final --variant graphite --trust
```

The final command reuses only an inspection and checkpoint matching the complete current fingerprint. Reports live with their operation manifests and are safe for CI parsing.
