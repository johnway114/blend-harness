# Reference projects

The built-in projects are executable acceptance fixtures. Initialize into an empty directory; do not run acceptance against package template source because successful runs intentionally create generated roots.

```sh
blend --json init brand-ident /tmp/blend-brand
blend --json init product-turntable /tmp/blend-product
blend --json init procedural-explainer /tmp/blend-explainer
```

Each fixture contains `COMMANDS.txt` and `expected.json`. The expectation file is validated by `reference-expectation-v1.json` and defines structural facts, validation profile, output declarations, command coverage, and retained contact-sheet baseline.

## Brand ident

Coverage:

- reusable `brand-rig` library pinned by ID, semantic version, whole-directory hash, and transitive palette dependency
- deterministic text and vector-like geometry
- at least three creative variants
- square, vertical, landscape, and transparent deliverables
- final image sequences encoded as H.264, ProRes with alpha, WebM, and GIF where declared
- one rendered sequence encoded to more than one codec without rerendering
- portable review package and recorded disposition

Expected structural evidence includes active camera, named brand collection, object/material/light counts, keyframes, and transparent output policy.

## Product turntable

Coverage:

- filesystem asset catalog entry pinned by ID, version, checksum, units, coordinate assumptions, license, preview, and transitive MTL checksum
- imported model provenance stored in the checkpoint
- deterministic rigid-body settling cache with separate preview and final profiles
- beauty, geometry, and transparency preview modes
- final still and turntable media
- GLB or glTF, USD, FBX, OBJ, Alembic, and STL exports
- clean-process export decode checking selection, helpers, transforms, names, materials, dependencies, units, scale, bounds, animation where supported, and manifoldness
- stale catalog, cache, export-stage, and resource-limit rejection paths

## Procedural explainer

Coverage:

- pinned local font bytes and local license text
- deterministic data-driven layout
- two named data revisions
- geometry-only, clay, normal, and beauty preview modes
- structural validation of labels, bars, collections, camera, and animation
- visual and structural comparison between revisions
- bounded parameter search with measurable metrics and explicit candidate promotion

## Baselines

The retained baseline is a contact sheet, not a substitute for structural evidence. It supports human review and image comparison while deterministic inspection and validation catch hidden scene errors.

When intentionally updating a baseline:

1. run the complete clean-workstation command sequence
2. inspect the validation and comparison reports
3. open the review package in a browser
4. copy the accepted generated contact sheet to `references/baselines/contact-sheet.png`
5. rerun from a fresh initialized project because the retained reference changes the source fingerprint
6. commit source, expectation, and baseline together

## Acceptance failures

Conformance tests deliberately cover missing assets, missing font/license, missing camera, clipped framing, stale checkpoint, stale cache, output collision, hard resource violation, catalog drift, library drift, forced interruption, and malformed protocol requests. A failure is accepted only when it occurs before inappropriate expensive work and returns the expected stable rule or error code.
