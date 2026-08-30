# Operations and recovery

## Command contract

All production commands accept an explicit project path. Mutations have an operation identifier. The CLI generates one when `--operation-id` is omitted; MCP mutation tools require one from the caller. Reusing an identifier with the same resolved inputs returns or continues the retained operation. Reusing it for different inputs fails with `OPERATION.ID_COLLISION`.

`--json` emits one bounded result document to stdout. Human logs and progress go to stderr. Exit codes and structured error codes are stable integration surfaces.

## Roots and ownership

`blend.yaml` declares source, asset, library, build, preview, render, output, cache, and temporary roots. Relative paths are resolved beneath the project. Output-like roots may not overlap source-like roots. Destructive commands reject paths outside the resolved project roots.

Every owned generated directory has a `.blend-owned.json` marker. `blend clean` and `blend cache clean` refuse unowned paths and never delete source. `blend promote` copies only a verified retained artifact into a declared library target and validates the resulting library before atomic promotion.

## Normal production sequence

1. `validate-config`: schema and filesystem contract only; no project Python execution.
2. `trust`: review and record exact executable source hashes, or pass `--trust` to a trusted operation.
3. `plan`: expand variants, matrices, outputs, resources, estimated storage, and blockers.
4. `build`: execute scene source in Blender and atomically retain a checkpoint plus manifest.
5. `preview`: render representative frames, views, passes, and a contact sheet.
6. `inspect`: extract deterministic structural evidence from a clean Blender process.
7. `validate`: apply the selected profile and retain the validation manifest.
8. `render`: write atomic image frames and frame manifests.
9. `encode`, `export`, `compare`, and `review`: package validated retained evidence.

Final animation never renders directly to a movie container. The image-sequence manifest is the resumable unit; encoding is a separate operation.

## Recovery behavior

- **Build:** rerun with the same operation identifier to recover the retained checkpoint, or use a new identifier after source changes.
- **Bake:** incomplete frame caches remain staged. `blend cache inspect` identifies missing, corrupt, and stale frames. A repeated bake resumes only when the exact cache key matches.
- **Render:** `blend resume` verifies frame existence, signature, expected byte range, dimensions, and source fingerprint. It rerenders only invalid frames. Matrix progress is retained at the root so completed variant/profile combinations survive interruption.
- **Encode:** safe to repeat from the same verified frame manifest. Different declared output IDs can encode H.264, ProRes, WebM, GIF, or still delivery from one sequence without rerendering.
- **Export:** staging is discarded unless clean-process decode and profile checks pass. Existing destination files are never silently overwritten.
- **Compare and review:** packages are built in staging and atomically promoted. Existing targets fail unless the caller uses a new operation ID or output declaration.

A failed process reports stderr excerpts, the owned log path, retryability, and a concrete next action. Timeouts and user cancellation terminate the owned Blender or FFmpeg process group and remove incomplete temporary output.

## Configuration changes

Manifest fingerprints include configuration, scene and module source, brief, declared local assets, catalog and library records, Blender capability/version, effective profile, variant, resolved render settings, seeds, and operation type. Any change invalidates affected checkpoints, caches, validation, frames, or exports rather than silently reusing stale artifacts.

`blend config diff <left> <right>` classifies changes as creative-only, operational, or both. Creative-only examples include text, color, camera framing, and motion values. Operational changes include engines, formats, devices, resource limits, roots, trust, and network policy.

## Parallelism and resources

The configured hard limits for Blender processes, CPU threads, memory, storage, frames, resolution, and per-operation time are enforced before or during work. Over-limit plans fail before starting Blender. Matrix workers reserve jobs through atomic claims. No two workers own the same frame.

## Cleaning

```sh
blend --json clean /absolute/project/path --generated
blend --json clean /absolute/project/path --all
blend --json cache clean /absolute/project/path --profile final-settle
```

Generated cleaning retains declared final output; `--all` includes manifest-owned final output. Source, assets, libraries, references, briefs, and configuration are never clean targets.
