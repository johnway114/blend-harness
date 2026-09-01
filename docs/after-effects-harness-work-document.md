# After Effects Harness Work Document

Status: implementation handoff

Reference project: `/Users/johnconway/Documents/blender`
Reference product specification: `/Users/johnconway/Documents/agent-blender-harness-product-spec.md`
Target implementation: a new sibling project at `/Users/johnconway/Documents/after-effects` unless the implementing session is given another destination.

## 1. Purpose

Build an After Effects-native equivalent of the Blend harness. The result is a local, source-controlled, agent-facing command-line harness around Adobe After Effects, not a Blender compatibility layer and not a conversion of a `.blend` file.

The implementing session must be able to use this document without reopening the Blender source project for design decisions. It may read the reference implementation for generic patterns, schemas, tests, and result-envelope behavior, but it must replace Blender-specific runtime code rather than preserve dead adapters.

The target workflow is:

```text
brief + ae.yaml + scene.jsx + declared assets
    -> plan
    -> build an .aep checkpoint
    -> preview and inspect
    -> validate
    -> render restartable image frames with aerender
    -> encode delivery media with FFmpeg
    -> compare and review
```

Source is authoritative. Generated `.aep` files, previews, renders, manifests, and delivery files are artifacts. A generated `.aep` must never become an undocumented source of creative state.

## 2. Port decision summary

| Blend concept | After Effects implementation | Decision |
| --- | --- | --- |
| Blender executable | `AfterFX` scripting host plus `aerender` | Required capability, detected by `doctor` |
| `blend.yaml` | `ae.yaml` | Same strict versioned configuration role |
| `brief.json` | `brief.json` | Preserve the structured creative contract |
| `scene.py` | `scene.jsx` or declared `.jsx` entrypoint | ExtendScript source is authoritative |
| `.blend` checkpoint | `build/project.aep` | Generated checkpoint, never authoritative by default |
| Python runtime context | Bootstrap JSX plus a small host-resolved context object | Keep helpers thin; use native AE scripting objects |
| Scene collections and objects | Project folders, compositions, layers, properties, masks, effects, markers | Inspect stable names and measurable properties |
| Cameras and lights | Named compositions and, only when used, AE camera and light layers | Do not require cameras for ordinary 2D projects |
| Render engine, samples, Cycles device | AE renderer, render settings, quality, motion blur, frame blending, MFR policy | No Blender fields or fake equivalents |
| Geometry and model export | Layer graph and media inspection | Do not implement model export |
| Simulation bake and Blender cache | Optional AE pre-render extension only | No `bake` or `cache` command in the first product |
| Final frame rendering | `aerender` to an image sequence | Required; never render a final animation directly to a container |
| FFmpeg encode | FFmpeg from validated frames, with optional declared audio | Preserve independent, repeatable encoding |
| Scene preview views | Named comps or declared comp views | Render every declared view and sample frame |
| Scene inspection | JSX serializer to `inspection.json` | Run against a clean generated `.aep` |
| Blender trust boundary | JSX trust boundary and AE file/network preferences | Explicit trust, no hidden script execution |
| Libraries | Versioned local `.jsxinc` libraries and assets | Hash the complete dependency tree |
| Geometry asset catalog | Footage, audio, data, image, video, font, and reference catalog | Pin bytes and metadata |
| `export` | No direct analogue | Omit rather than expose a no-op |

Do not cargo-cult 3D-only requirements. An AE project can be structurally valid without a camera, mesh, light, simulation cache, or model export.

## 3. Product boundary

### 3.1 Required initial product

Implement these real commands:

```text
aeh doctor
aeh init <template> <directory>
aeh validate-config <project>
aeh migrate <project> [--write]
aeh trust <project>
aeh build <project>
aeh preview <project>
aeh contact-sheet <project>
aeh inspect <project>
aeh validate <project>
aeh plan <project>
aeh render <project>
aeh resume <project>
aeh encode <project>
aeh compare <left> <right>
aeh clean <project> [--generated|--all]
aeh review <project>
aeh search <project> <search-id>
aeh promote <project> <search-report> <candidate-id> <variant-name>
aeh library compare <project> <library-id> <candidate>
aeh library update <project> <library-id> <candidate>
aeh template-upgrade <project>
aeh rules
```

Every command supports `--json`. JSON is the integration contract. Human output is short and points to retained artifacts and the next safe action.

Every mutating operation accepts `--operation-id`, either directly or through the same command-specific options as the reference CLI. Reusing an operation ID with identical resolved inputs is idempotent. Reusing it with different inputs fails with an operation collision.

### 3.2 Explicit non-goals

- Recreating Blender, Cinema 4D, or a 3D modeling engine.
- Importing `.blend` files or translating arbitrary Blender scenes.
- Implementing an AE GUI, panel, node editor, nonlinear editor, or render farm.
- Depending on Adobe Media Encoder for core output; FFmpeg remains the repeatable encoder.
- Treating the AE disk cache as authoritative or reproducible source.
- Automatically installing fonts, plugins, or third-party effects.
- Silently replacing a missing plugin/effect with a visually similar built-in effect.
- Running scripts in an already open user-owned AE instance by default.
- Adding a model export, mesh validation, simulation bake, or fake placeholder command.
- Claiming pixel-identical output across AE versions, plugin versions, operating systems, or GPU modes.

## 4. Target repository and layout

The new implementation should be a separate Python package and executable. Do not mutate the Blender harness package name or make the Blender package import After Effects modules.

Recommended layout:

```text
after-effects/
  pyproject.toml
  README.md
  LICENSE
  aeharness/
    __init__.py
    cli.py
    operations.py
    project.py
    planning.py
    validation.py
    comparison.py
    review.py
    media.py
    manifests.py
    capabilities.py
    process.py
    afterfx.py
    scripting.py
    templates.py
    search.py
    libraries.py
    errors.py
    util.py
    schemas/
      config-v1.json
      brief-v1.json
      result-v1.json
      manifest-v1.json
      inspection-v1.json
      validation-v1.json
      reference-expectation-v1.json
  templates/
    brand-ident/
    footage-composite/
    procedural-explainer/
    empty/
  tests/
  docs/
```

A generated project has this layout:

```text
aeh.yaml
brief.json
scene.jsx
lib/
  *.jsxinc
assets/
  images/
  video/
  audio/
  data/
  fonts/
references/
previews/
build/
  project.aep
  project.aepmeta.json
  inspection.json
  validation.json
  manifests/
  logs/
renders/
  preview/
  final/
    <variant>/<profile>/frames/
output/
working/
temporary/
```

Only configuration, brief, entrypoint source, local libraries, declared assets, and intentional references are authoritative. Generated roots must have ownership markers and may be removed by `clean` only through retained manifests.

Use the existing Blend path and manifest discipline as the reference for atomic writes, bounded logs, operation IDs, input fingerprints, and safe cleanup. Change all product names, executable names, schema IDs, and Blender-specific fields.

## 5. Host and automation architecture

### 5.1 Host-side roles

Python owns:

- CLI parsing and JSON result envelopes.
- Strict config and brief validation.
- Path resolution and root ownership.
- Asset, font, library, and reference checksums.
- Variant and matrix expansion.
- Operation manifests and dependency fingerprints.
- Process groups, timeouts, cancellation, bounded logs, and cleanup.
- Invoking the AE scripting adapter and `aerender`.
- Image contact sheets, media probing, comparison reports, and FFmpeg encoding.

After Effects owns:

- Creating and saving compositions, folders, layers, properties, masks, markers, effects, expressions, and render settings.
- Importing declared footage through native AE APIs.
- Evaluating expressions and effects.
- Serializing the evaluated project into an inspection report.
- Rendering AE frames through `aerender`.

Do not reimplement AE layer, effect, expression, text, or compositing behavior in Python.

### 5.2 Script execution strategy

The adapter must support a dedicated owned AE process for operations that execute JSX. It must not mutate a currently open human project unless the caller explicitly requests a reuse mode and accepts the warning.

The preferred flow is:

1. Discover a concrete After Effects installation and matching `aerender` binary.
2. Validate the project without running JSX.
3. Write an operation-scoped resolved runtime JSON file under `temporary/`.
4. Generate an operation-scoped bootstrap JSX file that reads only that resolved file and the declared entrypoint.
5. Launch a dedicated AE instance using the supported macOS automation path for the installed version.
6. The bootstrap creates a new project or opens only the declared checkpoint, evaluates the entrypoint, writes a result sentinel and operation log, saves only on success, and exits or closes without saving on failure.
7. Python waits for the sentinel and process exit, then validates the sentinel schema and retained artifacts.

Adobe documents ExtendScript `.jsx` files and the `afterfx -r` script invocation. On macOS, the installed AE version may require an AppleScript `DoScript` bridge to launch or communicate with the application. Implement this as an adapter with capability detection, not as hard-coded UI clicks. The adapter must record the selected launch method and exact AE build in the manifest.

A script operation is successful only when all of these are true:

- AE returns a success status through the adapter or sentinel.
- The expected artifact exists and has nonzero bytes.
- The artifact can be opened by a subsequent clean AE inspection operation.
- The operation manifest is complete and its hashes match.

If a dedicated process cannot be owned and closed safely, fail with an actionable unavailable error. Never leave an AE process running as an undocumented daemon.

### 5.3 Render strategy

Use `aerender` for frame rendering. Adobe documents `-project`, `-comp`, `-s`, `-e`, `-output`, `-RStemplate`, `-OMtemplate`, `-log`, and verbose progress options. The adapter must call `aerender -help` during capability discovery and must not assume localized render-setting template names.

Render operations use an image sequence path such as:

```text
renders/final/square-amber/final/frames/frame-[####].png
```

Use a temporary frame name or staging directory and atomically promote each completed frame. Do not trust filename presence alone.

## 6. Configuration contract

`ae.yaml` is strict, versioned, and rejects unknown fields. Preserve the reference project convention of YAML configuration plus JSON brief.

Minimum shape:

```yaml
schema: 1
id: brand-ident
template:
  id: brand-ident
  version: 1
afterEffects:
  minimumVersion: "25.0"
  maximumVersionExclusive: "27.0"
  scriptEngine: extendscript
  factoryStartup: true
  offline: true
entrypoint: scene.jsx
brief: brief.json
project:
  frameRate: 24
  frameStart: 1
  frameEnd: 24
  colorManagement:
    workingSpace: "sRGB IEC61966-2.1"
    bitsPerChannel: 8
    linearizeWorkingSpace: false
profiles:
  preview:
    width: 256
    height: 256
    quality: draft
    motionBlur: false
    frameBlending: false
    channels: RGB
    format: PNG
    final: false
  final:
    width: 384
    height: 384
    quality: best
    motionBlur: true
    frameBlending: false
    channels: RGB
    format: PNG
    final: true
views:
  - id: hero
    composition: hero
    subjects: [mark-spine]
    framing:
      minimumCoverage: 0.05
      maximumCoverage: 0.80
      requireFullyVisible: true
previewFrames: [1, 8, 16, 24]
previewModes: [full]
variants:
  amber:
    parameters:
      accent: [1.0, 0.28, 0.035, 1.0]
      plate: [0.012, 0.009, 0.007, 1.0]
  square-amber:
    extends: amber
    profile: final
    width: 384
    height: 384
matrices:
  delivery:
    variants: [square-amber]
    profiles: [final]
    concurrency: 1
outputs:
  - id: square-film
    variant: square-amber
    profile: final
    type: video
    codec: h264
    path: output/brand-ident-square.mp4
assets: []
libraries: []
policies:
  requireStableNames: true
  requireDeclaredEffects: true
resources:
  maxProcesses: 1
  maxMemoryMB: 4096
  maxDiskBytes: 1073741824
  maxResolutionPixels: 1000000
  maxFrames: 240
  timeoutSeconds: 180
  thermalAdvisory: true
nondeterminism: []
```

Required behavior:

- `profile` contains operational AE render settings, dimensions, channels, alpha, and format.
- `variant` contains creative values such as aspect ratio, palette, text, data revision, and timing.
- Variant inheritance is acyclic, deep-merged, inspectable, and included in the dependency hash.
- A matrix expands profile and variant combinations before any AE process starts.
- Every output path is normalized beneath the declared output root and checked for collisions before rendering.
- The preview profile is never written into the final source composition or checkpoint as an implicit mutation. Prefer profile-specific wrapper comps or operation-time render settings with explicit inspection evidence.
- `samples`, Blender engine names, mesh limits, unit systems, and simulation settings are invalid AE configuration fields.

### 6.1 Output declarations

Support at least:

- still PNG with RGB or RGBA;
- H.264 delivery through FFmpeg;
- ProRes master where the local FFmpeg build supports it;
- alpha-capable PNG frame sequence and ProRes 4444 output where supported;
- optional WebM and GIF when FFmpeg capability checks pass;
- optional declared audio muxing with checksum and duration validation.

A video output references a validated frame manifest. Encoding the same frame manifest to two codecs must not invoke AE or rerender frames.

### 6.2 Brief contract

Preserve the existing fields:

```json
{
  "schema": 1,
  "title": "Restrained engraved identity",
  "purpose": "Executable acceptance fixture and reusable motion ident",
  "durationSeconds": 1,
  "creativeDirection": [
    "warm near-black surface",
    "precise inset geometric mark",
    "amber light reveals form",
    "restrained camera movement"
  ],
  "avoid": [
    "fantasy runes",
    "excessive particles",
    "neon glow",
    "generic cinematic fog"
  ],
  "deliverables": [
    "square animation",
    "vertical animation",
    "landscape animation",
    "transparent still mark"
  ],
  "acceptance": [
    "mark remains legible at mobile size",
    "first meaningful movement begins within 500 ms",
    "final frame reads as a stable resolved identity"
  ],
  "mechanicalAcceptance": [
    {"id": "timeline-duration", "measurement": "durationSeconds", "operator": "gte", "value": 1},
    {"id": "subject-visible", "measurement": "heroCoverage", "operator": "gte", "value": 0.05}
  ],
  "constraints": [
    "No external assets or GUI-authored state",
    "All variants share one source scene"
  ]
}
```

Creative statements remain review criteria unless a measurable metric is declared. Validation must never claim that a visual mood or aesthetic statement passed merely because the layer graph is valid.

## 7. Entrypoint and AE source contract

`scene.jsx` must expose a deterministic `build(context)` function. The runtime owns process setup, config resolution, operation dispatch, project save, inspection, and manifest writing.

Conceptual contract:

```javascript
function build(context) {
    // Use native After Effects scripting APIs.
    // Create folders, comps, layers, properties, masks, effects, markers,
    // expressions, and output wrapper comps from context.variant and context.profile.
}
```

The context must contain only resolved, declared data:

```javascript
{
  operation: "build",
  operationId: "...",
  project: {...},
  brief: {...},
  profile: {...},
  variant: {...},
  paths: {
    projectRoot: "...",
    checkpoint: "...",
    result: "...",
    log: "..."
  },
  assets: {
    byId: {
      logo: {id: "logo", path: "...", checksum: "...", type: "image"}
    }
  },
  data: {...}
}
```

Entrypoint rules:

- Do not discover arbitrary files, read secrets, or use ambient current-directory assumptions.
- Do not use network calls. The host must reject network permission unless configuration and the explicit command both allow it; the default is offline.
- Do not use `eval` to parse project data. Use a strict JSON parser or host-resolved data.
- Do not depend on layer indices for identity. Find layers by stable names and validate duplicates.
- Do not rely on a manually edited `.aep` or an existing open project.
- Use stable internal effect match names when applying effects; record display names and versions for inspection.
- Use shape layers, text layers, footage layers, adjustment layers, cameras, lights, and nulls only when declared by the creative source.
- Declare temporal effects and expressions that depend on neighboring frames. Resume must account for those dependencies.
- Save only after successful build. On any exception, write the failure sentinel and close without saving.

The runtime should expose thin helpers for folder creation, comp creation, layer lookup, asset import, marker creation, property keyframing, expression assignment, and color conversion. It must not hide the native AE object model behind a new scene language.

## 8. Inspection contract

`inspect` opens the retained `.aep` in a dedicated clean AE scripting operation and writes a deterministic `build/inspection.json`. It must preserve the complete report on disk while returning bounded JSON in the CLI result.

Minimum report shape:

```json
{
  "schema": 1,
  "project": {
    "name": "brand-ident.aep",
    "file": "build/project.aep",
    "afterEffectsVersion": "...",
    "bitsPerChannel": 8,
    "workingSpace": "sRGB IEC61966-2.1"
  },
  "compositions": [
    {
      "name": "hero",
      "width": 384,
      "height": 384,
      "durationSeconds": 1,
      "frameRate": 24,
      "layers": 9,
      "activeCamera": null
    }
  ],
  "folders": [],
  "layers": [
    {
      "comp": "hero",
      "name": "mark-spine",
      "index": 1,
      "type": "ShapeLayer",
      "enabled": true,
      "solo": false,
      "guide": false,
      "threeDLayer": false,
      "inPoint": 0,
      "outPoint": 1,
      "startTime": 0,
      "sourceRect": {"top": 0, "left": 0, "width": 100, "height": 100},
      "animated": true,
      "effects": [],
      "masks": [],
      "markers": [],
      "expressions": []
    }
  ],
  "cameras": [],
  "lights": [],
  "fonts": [],
  "footage": [],
  "effects": [],
  "animation": [],
  "dependencies": [],
  "renderQueue": [],
  "statistics": {
    "compositions": 1,
    "layers": 9,
    "animatedProperties": 3,
    "effects": 0,
    "missingFootage": 0,
    "missingFonts": 0
  },
  "extensions": {}
}
```

Inspection must include, where available:

- project file and AE build;
- project color management and bit depth;
- folders and stable item IDs/names;
- all compositions, dimensions, duration, frame rate, renderer, work area, and markers;
- layer type, name, order, enabled/solo/guide state, shy state, 2D/3D state, timing, transform values, bounds, parent, track matte, blending mode, effects, masks, expressions, and animation;
- cameras and lights when present;
- imported footage, file path, missing state, dimensions, duration, frame rate, alpha, interpretation, and checksum;
- fonts used by text layers, installed/missing state, and declared license metadata;
- effect internal match name, display name, version, and whether it is declared/built-in;
- render-queue items and output modules;
- external dependencies and expression errors;
- estimated output volume and frame coverage.

Normalize ordering and omit volatile UI state. Keep version-specific fields under `extensions.afterEffects` rather than destabilizing the portable report.

## 9. Validation contract

Validation is mechanical and evidence based. Every finding has a stable rule ID, severity, message, evidence, remediation, scope, and suppression state. The report contains `mechanicalOnly: true`.

### 9.1 Configuration rules

Implement at least:

```text
CONFIG.SCHEMA_INVALID
CONFIG.ROOT_OVERLAP
CONFIG.ENTRYPOINT_MISSING
CONFIG.PROFILE_UNKNOWN
CONFIG.VARIANT_UNKNOWN
CONFIG.VARIANT_CYCLE
CONFIG.MATRIX_UNKNOWN
CONFIG.OUTPUT_UNKNOWN
CONFIG.OUTPUT_COLLISION
CONFIG.INVALID_FRAME_RANGE
CONFIG.INVALID_DIMENSIONS
CONFIG.INVALID_FRAME_RATE
CONFIG.UNSUPPORTED_OUTPUT_FORMAT
CONFIG.AE_VERSION_UNSUPPORTED
CONFIG.AERENDER_UNAVAILABLE
CONFIG.RESOURCE_LIMIT
```

### 9.2 Asset and dependency rules

```text
ASSET.MISSING
ASSET.ZERO_BYTES
ASSET.UNREADABLE
ASSET.PATH_OUTSIDE_ROOT
ASSET.UNDECLARED_DEPENDENCY
ASSET.CHECKSUM_DRIFT
ASSET.UNSUPPORTED_FORMAT
ASSET.MISSING_FONT
ASSET.FONT_LICENSE_MISSING
ASSET.MISSING_AUDIO
ASSET.AUDIO_DURATION_SHORT
LIBRARY.MISSING
LIBRARY.VERSION_DRIFT
LIBRARY.CHECKSUM_DRIFT
EFFECT.MISSING
EFFECT.UNDECLARED
EFFECT.VERSION_DRIFT
```

Use declared paths and SHA-256 checksums. For footage, validate decodability, dimensions, duration, frame rate, pixel aspect, channels, and alpha where applicable. For audio, validate stream presence, duration, sample rate, and channel count. Font installation is a host capability, not something the harness silently changes.

### 9.3 Project and layer rules

```text
PROJECT.EMPTY
PROJECT.CHECKPOINT_STALE
PROJECT.MISSING_COMPOSITION
PROJECT.DUPLICATE_STABLE_NAME
PROJECT.MISSING_ACTIVE_COMP
LAYER.MISSING_REQUIRED
LAYER.DUPLICATE_NAME
LAYER.DISABLED_FOR_RENDER
LAYER.OUTSIDE_TIMELINE
LAYER.MISSING_SOURCE
LAYER.MISSING_PARENT
LAYER.INVALID_TRACK_MATTE
LAYER.INVALID_BLEND_MODE
LAYER.MISSING_EFFECT
LAYER.EXPRESSION_ERROR
LAYER.UNDECLARED_PLUGIN
```

Required layer and comp names come from the reference expectation file and template declarations. Stable names must be unique within their composition or explicitly scoped by composition.

### 9.4 Framing and animation rules

```text
FRAME.SUBJECT_NOT_VISIBLE
FRAME.SUBJECT_CLIPPED
FRAME.SUBJECT_COVERAGE_LOW
FRAME.SUBJECT_COVERAGE_HIGH
FRAME.TITLE_SAFE_VIOLATION
ANIMATION.NO_KEYFRAMES
ANIMATION.KEYFRAME_OUTSIDE_RANGE
ANIMATION.NO_MEANINGFUL_CHANGE
ANIMATION.FINAL_HOLD_SHORT
ANIMATION.CAMERA_MISSING_AT_CUT
ANIMATION.TEMPORAL_DEPENDENCY_UNDECLARED
```

Use `sourceRectAtTime`, masks, layer bounds, and comp-space transforms for measurable coverage. For 3D layers, use camera-space bounds only when the comp declares a camera. For 2D comps, calculate transformed layer bounds against the composition rectangle.

### 9.5 Render and media rules

```text
RENDER.PROFILE_MISMATCH
RENDER.COLOR_MANAGEMENT_MISMATCH
RENDER.ALPHA_MISMATCH
RENDER.FRAME_MISSING
RENDER.FRAME_CORRUPT
RENDER.FRAME_STALE
RENDER.OUTPUT_COLLISION
RENDER.TEMPORAL_RESUME_UNSAFE
MEDIA.CODEC_MISMATCH
MEDIA.PIXEL_FORMAT_MISMATCH
MEDIA.FPS_MISMATCH
MEDIA.DURATION_MISMATCH
MEDIA.DIMENSIONS_MISMATCH
MEDIA.ALPHA_MISMATCH
MEDIA.AUDIO_MISMATCH
MEDIA.DECODE_FAILURE
```

Do not report aesthetic acceptance as a passed mechanical rule. A human review package must carry those statements separately.

## 10. Preview and contact-sheet contract

`preview` must be cheap, repeatable, and useful for correcting composition, timing, layer visibility, and effects.

For every selected variant, profile, declared view, preview mode, and sample frame:

1. Resolve the comp name without guessing.
2. Render a still through `aerender` to a staging PNG or other declared preview format.
3. Validate dimensions, channels, and decodability.
4. Record frame number, time, comp/view, variant, profile, mode, path, hash, and warnings.
5. Build one labeled contact sheet.

Always include the first and final frame, even when `previewFrames` omits them. A contact sheet label must include:

```text
project revision | AE version | profile | variant | view/comp | frame | time | dimensions | warnings
```

Initial preview modes:

- `full`: normal evaluated render;
- `draft`: explicit low-cost render settings;
- `alpha`: alpha visualization or RGBA output where applicable.

Do not invent Blender clay, depth, normal, wireframe, or object-index modes. They may be added later as AE-specific diagnostic modes only when they produce real evidence.

`contact-sheet` must regenerate from retained previews without invoking AE. Missing source previews are an error, not a reason to fabricate placeholders.

## 11. Rendering, resume, and encoding

### 11.1 Frame sequence first

Final animation rendering always writes still frames before encoding. The frame manifest is the resumable unit and records, per frame:

- expected frame and time;
- exact output path;
- profile, variant, comp, and source dependency hash;
- dimensions, channels, format, byte count, and SHA-256;
- decodability result;
- start/end timestamps;
- process and AE version;
- status: pending, running, succeeded, corrupt, stale, failed, or interrupted.

Frames are atomically promoted only after successful validation. Partial files are not valid completed frames.

### 11.2 Resume

`resume` must:

- load the current planned frame set;
- verify every existing frame by dimensions, channels, decodability, byte range, hash, and manifest fingerprint;
- identify missing, corrupt, stale, or dependency-invalid frames;
- render only invalid frames, grouped into contiguous ranges when safe;
- retain valid frames and report `reusedFrames` and `renderedFrames`;
- never infer success from filename presence;
- never rerender an entire matrix member because a different member failed.

A project must declare whether its expressions/effects are frame-independent. For temporal effects, time remapping, frame blending, or expressions that read neighboring time, either:

- declare a dependency window and rerender the required surrounding frames, or
- mark resume as unsafe and rerender the declared range.

The manifest must report which policy was used. Do not silently produce mixed stale/updated frames.

### 11.3 Encoding

`encode` consumes only a validated frame manifest and optional declared audio. It must not invoke AE. Use FFmpeg for:

- H.264 delivery;
- ProRes master and ProRes 4444 alpha where supported;
- other declared formats only after `doctor` capability detection.

Run media validation after encoding with FFprobe and, where practical, a decode/read check. Validate duration, dimensions, frame rate, frame count, codec, pixel format, alpha, audio, and color metadata. Preserve the source frame-manifest ID and audio checksum in the output manifest.

## 12. Variants, matrices, and search

Variants are named parameter sets applied by the same `scene.jsx` source. Supported creative dimensions:

- composition width and height;
- aspect ratio and safe-area policy;
- palette and colors;
- text and language;
- data revision;
- layer timing and hold duration;
- camera treatment when a camera exists;
- effect intensity and motion treatment;
- source footage choice;
- profile selection.

Do not fork `scene.jsx` for each variant. The resolved variant must be included in every checkpoint, preview, validation, render, encode, comparison, and review manifest.

A matrix plans all members before execution, detects output collisions, estimates frame count/pixels/storage, reports available AE and FFmpeg capabilities, and exposes blocking validation. Concurrency defaults to one. Multiple `aerender` processes are opt-in and must honor memory, disk, thermal, and process limits. Do not assume AE can safely render multiple instances on every host.

Bounded search operates only over declared parameters and budgets. It renders low-cost previews, computes declared mechanical metrics such as subject coverage or render seconds, produces a ranked contact sheet, and never claims that a subjective visual preference was proven. `promote` writes a selected candidate into named source configuration without copying generated `.aep` state.

## 13. Compare and review

`compare` must support:

- preview images and rendered frames;
- variant and profile outputs;
- project revisions;
- inspection reports;
- validation reports;
- manifests.

Comparison evidence includes:

- labeled side-by-side images;
- difference heatmap and changed-pixel metrics when image dimensions match;
- changed compositions, layers, properties, effects, masks, expressions, and markers;
- changed source, asset, library, profile, variant, AE, FFmpeg, and manifest hashes;
- camera/framing changes when cameras are present;
- output and media metadata changes.

Pixel differences prove that two artifacts differ, not that one is worse.

`review` produces a self-contained static package containing:

- project and source fingerprints;
- brief and declared acceptance statements;
- contact sheet and selected previews;
- inspection and validation summaries;
- comparison evidence;
- output paths, checksums, and media probes;
- exact artifacts reviewed;
- disposition fields: approved, changes-requested, selected, or no-decision;
- comments and selected variant without mutating creative source.

## 14. Manifests and result envelopes

Keep the result-envelope contract from Blend, changing its schema ID and runtime names:

```json
{
  "schema": 1,
  "operation": "render",
  "operationId": "render-2026-09-01T120000Z",
  "project": "/absolute/project",
  "status": "succeeded",
  "startedAt": "...",
  "endedAt": "...",
  "durationSeconds": 12.4,
  "summary": "Rendered 24 frame(s).",
  "data": {},
  "artifacts": [],
  "warnings": [],
  "nextActions": [],
  "progress": {"completed": 24, "total": 24},
  "error": null
}
```

Failures use the same envelope with a stable error object containing category, code, operation, message, retryability, details, retained artifacts, and remediation. Consumers branch on `status` and `error.code`, not human log prose.

Every build, preview, inspect, validate, render, resume, encode, compare, search, and review operation retains an atomic `manifest-v1` artifact containing:

- harness and runtime version;
- AE and `aerender` executable paths and exact versions;
- host platform and architecture;
- project ID and source revision when supplied;
- config, brief, entrypoint, library, asset, and reference checksums;
- resolved profile, variant, matrix member, comp, and output;
- color management and render settings;
- expected/completed frames and per-frame records;
- output checksums, bytes, dimensions, and media probe;
- validation summary;
- warnings, overrides, nondeterminism, logs, and failure details.

The dependency fingerprint must include source bytes, declared assets and transitive dependencies, libraries, config, brief, AE version/build, harness runtime, selected profile, variant, matrix member, output, and operation type. Relative locations and timestamps are excluded when they are not semantic.

## 15. Security, trust, and process hygiene

After Effects JSX is executable code with access to the application, filesystem, and potentially network-enabled features. Treat it as a trust boundary equivalent to Blender Python.

Required controls:

- `trust` records a local project decision and exact source hash set.
- Build, inspect, and any JSX mutation fail unless the source is trusted or the caller passes `--trust`.
- Offline is the default. Reject `--allow-network` when `afterEffects.offline: true`.
- Do not enable AE's “Allow Scripts to Write Files And Access Network” preference silently. If file access is disabled, fail with a clear remediation that names the required user preference.
- Network policy applies to harness-owned processes. Document that AE/plugins may have application-level network behavior that the harness cannot fully sandbox.
- Pass only an allowlisted environment to child processes. Reject secret-like variable names from project configuration and logs.
- Constrain temporary, working, preview, render, output, and log paths to the project roots.
- Treat imported media, expressions, fonts, and plugins as untrusted input. Do not execute undeclared scripts or expressions from arbitrary files.
- Capture raw AE and `aerender` logs under owned operation directories; return only bounded tails in JSON.
- Track every AE, `aerender`, FFmpeg, FFprobe, and helper process started by the operation.
- On timeout, cancellation, failure, or normal completion, close the dedicated AE process and terminate only the owned process group.
- Never kill an existing user-owned AE instance. If ownership cannot be proven, refuse cleanup and fail safely.
- Cleanup follows ownership markers and manifests. Never wildcard-delete source, assets, references, or unowned files.

The process supervisor must report whether the operation was interrupted, timed out, failed, or completed, whether valid work is retained, and the exact safe next command.

## 16. `doctor` capability report

`aeh doctor` must not mutate source or generated artifacts. It reports:

- operating system, architecture, writable roots, available storage, and temp directory;
- discovered After Effects app, `AfterFX` executable, version, build number, and scripting capability;
- discovered `aerender`, `aerender -help` result, and render-only capability;
- whether the dedicated macOS script-launch strategy is available;
- whether script file writes are enabled or can be detected;
- available GPU acceleration modes exposed by AE, without silently selecting a different mode;
- installed fonts requested by the project, with version/API support;
- required built-in and third-party effect match names and versions;
- FFmpeg/FFprobe paths, versions, codecs, pixel formats, and alpha support;
- optional tools and unavailable capabilities;
- config errors that can be checked without executing JSX;
- offline-wrapper capability for harness subprocesses;
- whether the selected project is compatible with the discovered AE version.

Missing optional capabilities are warnings. A missing capability required by the selected profile is an error before build or render.

## 17. Reference templates

The templates are executable conformance fixtures, not aesthetic defaults for unrelated projects. Each includes `ae.yaml`, `brief.json`, `scene.jsx`, any local libraries/assets, `COMMANDS.txt`, `expected.json`, and a retained contact-sheet baseline after acceptance.

### 17.1 `brand-ident`

Port the existing restrained brand-ident brief using AE-native techniques:

- shape layers for `mark-spine`, `mark-upper`, and `mark-lower`;
- a warm near-black surface layer;
- animated trim paths, masks, track mattes, or gradients for the restrained reveal;
- no third-party effects required;
- named `hero` and `detail` comps;
- square, vertical, and landscape output comps;
- transparent still output at the final frame;
- at least three palette variants: amber, ivory, and oxide;
- one source script for all variants;
- no hidden GUI-authored state.

Required expected names: `surface`, `mark-spine`, `mark-upper`, `mark-lower`, `hero`, `detail`. The expectation file must state minimum compositions/layers, required animation, output list, and baseline path.

### 17.2 `footage-composite`

Replace Blender's product-turntable fixture with an AE-native footage/compositing fixture:

- one small redistributable still or video asset and optional audio asset;
- declared import interpretation and checksums;
- hero and detail compositions;
- masks and a track matte or equivalent compositing relationship;
- at least one adjustment layer with a declared built-in effect;
- optional 3D layer and camera only if the fixture genuinely exercises them;
- still, image-sequence, and video output;
- missing footage, checksum drift, unsupported media, and audio-duration failure paths;
- no model export claims.

This fixture proves AE's real strengths rather than pretending an OBJ or GLB pipeline exists.

### 17.3 `procedural-explainer`

Preserve the data-driven fixture with AE-native layers:

- two declared JSON data revisions;
- deterministic shape-layer bars, guides, and labels;
- declared font and license metadata;
- named color palette and timing controls;
- animated chart transitions and markers;
- structural and visual comparison between revisions;
- bounded parameter search over layout or timing values;
- a ranked contact sheet and explicit candidate promotion.

If text uses an installed font, the fixture must fail clearly when that font is missing. Do not silently substitute another font.

### 17.4 `empty`

Provide only the project/configuration/runtime contract and a minimal no-content source. It must validate configuration but fail final render validation with a stable, expected error such as `PROJECT.EMPTY`.

## 18. Test and acceptance plan

Use real AE and real `aerender` for integration tests. Unit tests may use synthetic manifests and generated PNGs for pure Python logic, but must not make fake AE output satisfy integration acceptance.

### 18.1 Contract tests

Cover:

- every template and expectation file validates against strict schemas;
- unknown config fields fail closed;
- schema migration previews before writing and creates a backup when writing;
- variant inheritance is acyclic and deterministic;
- project source/library/asset changes invalidate dependency hashes;
- output roots and collisions are rejected before AE starts;
- result and manifest envelopes validate;
- operation ID collisions are rejected;
- JSON CLI output contains no human log noise;
- cleanup removes only owned generated artifacts;
- environment sanitization rejects secret-like values;
- review and comparison staging is atomic.

### 18.2 AE integration tests

On a clean supported workstation:

1. `doctor` returns a complete capability report.
2. `init` creates each template in an empty directory.
3. `validate-config` succeeds without launching AE.
4. `trust` records source trust.
5. `build` creates a new `.aep`, checkpoint metadata, manifest, and bounded log.
6. `inspect` opens that checkpoint in a dedicated clean AE process and writes schema-valid inspection evidence.
7. `preview` covers every declared view, mode, first/final frame, and `previewFrames` sample.
8. `contact-sheet` regenerates a valid labeled sheet from retained previews.
9. `validate` catches missing required comps/layers, missing footage, missing fonts, missing effects, clipped subjects, bad timing, stale checkpoints, and profile mismatches before final render.
10. `plan` reports graph, matrix, output paths, estimated frames/pixels/storage, capabilities, checkpoint hits, any declared pre-render artifact hits, and blocking findings.
11. `render` retains a complete image sequence and frame manifest.
12. Forced interruption leaves completed frames valid and no owned AE or helper processes running.
13. `resume` rerenders only missing, corrupt, stale, or dependency-invalid frames.
14. `encode` produces H.264 and a second declared output from the same frame manifest without invoking AE.
15. Media probes reject wrong dimensions, FPS, duration, codec, pixel format, alpha, audio, and corrupt output.
16. `compare` emits image and structural evidence.
17. `review` creates a portable package and records a disposition without changing source.
18. `clean --generated` retains source and final outputs; `clean --all` removes only manifest-owned generated files and outputs.
19. A script exception produces a nonzero operation result, retained traceback, no saved partial checkpoint, and a safe remediation.
20. A missing or unsupported AE capability fails before expensive work.

### 18.3 Required failure scenarios

Exercise each with a real or deliberately modified fixture:

- missing entrypoint;
- malformed config or unknown field;
- unsupported AE version;
- missing `aerender`;
- missing asset;
- asset checksum drift;
- asset outside declared root;
- missing font or license metadata;
- missing effect/plugin;
- missing required composition or layer;
- duplicate stable layer name;
- expression error;
- clipped subject or unsafe framing;
- animation outside frame range;
- missing final hold;
- stale `.aep` checkpoint;
- output collision;
- hard resource violation;
- corrupt frame;
- temporal resume dependency;
- encode media mismatch;
- script failure;
- forced interruption during build, inspect, preview, render, encode, compare, and review;
- network request while offline;
- untrusted source;
- operation ID collision.

Acceptance is based on stable rule/error codes, retained evidence, correct preflight timing, and owned-process cleanup. It is not based only on exit status.

## 19. Implementation sequence for the next session

The implementing session should complete these in order without stopping at a shell or mocked scaffold:

1. Copy the generic product shape from the reference harness into a new After Effects package and rename all public identities.
2. Implement strict AE project loading, path roots, config/brief schemas, variants, matrices, assets, libraries, trust, fingerprints, and migrations.
3. Implement the process supervisor and AE adapter. Prove dedicated macOS launch, JSX execution, sentinel result, log retention, timeout, cancellation, and cleanup before building templates.
4. Implement `doctor` against the installed After Effects, `aerender`, FFmpeg, FFprobe, fonts, effects, and filesystem.
5. Implement `build` with the bootstrap JSX contract and generated `.aep` checkpoint metadata.
6. Implement clean-process `inspect` and the AE layer/composition serializer.
7. Implement mechanical validation and stable AE-specific rule catalog.
8. Implement previews, contact sheets, render manifests, atomic frame rendering, selective resume, and media validation.
9. Implement FFmpeg encoding from validated frames only.
10. Implement comparison, review packages, bounded search, library operations, migration, template upgrade comparison, and safe cleanup.
11. Implement all four reference templates and retained expectations/baselines.
12. Add contract tests, AE integration tests, interruption tests, and clean-workstation `COMMANDS.txt` runs.
13. Run the full supported-host acceptance sequence. Do not claim completion from unit tests, a generated `.aep`, or a single preview.

The next session must not:

- leave Blender imports in the new package;
- add no-op handlers for `export`, `bake`, or `cache`;
- use fake `.aep` files, fake inspection, or mocked render output for acceptance;
- depend on an existing open AE project;
- silently fall back from missing effects, fonts, codecs, or AE versions;
- silently render a movie directly when a frame sequence is declared;
- stop after creating a CLI shell or one template.

## 20. Handoff command sequence

Each template's `COMMANDS.txt` must be executable from a clean workstation and use explicit operation IDs. The brand-ident sequence should be equivalent to:

```sh
aeh --json validate-config .
aeh --json trust .
aeh --json build . --trust --operation-id reference-brand-build
aeh --json preview . --profile preview --variant amber --trust --operation-id reference-brand-preview
aeh --json inspect . --profile final --variant square-amber --trust --operation-id reference-brand-inspect
aeh --json validate . --profile final --variant square-amber --trust --operation-id reference-brand-validate-square
aeh --json validate . --profile final --variant vertical-ivory --trust --operation-id reference-brand-validate-vertical
aeh --json validate . --profile final --variant landscape-oxide --trust --operation-id reference-brand-validate-landscape
aeh --json plan . --matrix delivery --target render --operation-id reference-brand-plan
aeh --json render . --matrix delivery --trust --operation-id reference-brand-delivery
aeh --json validate . --profile transparent --variant transparent-amber --trust --operation-id reference-brand-validate-transparent
aeh --json render . --profile transparent --variant transparent-amber --trust --operation-id reference-brand-render-transparent
aeh --json encode . --output square-film --operation-id reference-brand-square
aeh --json encode . --output square-master --operation-id reference-brand-master
aeh --json review . --operation-id reference-brand-review
```

The exact command options may follow the final CLI parser, but the workflow and operation dependencies must remain explicit. `plan` must show the graph before render, `render` must produce frames, and `encode` must show zero AE rerendered frames.

## 21. Known risks and required honesty

- After Effects scripting is versioned by application release, not by a stable cross-version server API. Pin and record the exact build used for each operation.
- Adobe's macOS script-launch behavior differs from Windows command-line behavior. The adapter must prove the installed path and process ownership rather than assuming `afterfx -r` alone is a new headless instance.
- AE's render-setting templates can be localized or user-modified. Prefer explicit API settings or project-owned wrapper comps; never assume a template name exists without capability validation.
- Third-party effects and fonts are external dependencies. Missing versions must fail or warn according to policy, never silently substitute.
- AE expressions and effects can be temporal. Resume correctness requires declared temporal dependency windows.
- GPU, Multi-Frame Rendering, plugin versions, color management, and AE version can change pixels. Promise reproducibility only for the normalized manifest and a declared compatible environment.
- AE's own disk cache is an optimization, not a source artifact. Do not use its cache presence as evidence of a valid frame.
- A script that can write a project can write arbitrary files if AE permissions allow it. The trust, allowlist, offline, path, timeout, and process-ownership controls are mandatory.

When a capability cannot be proven on the installed host, return a stable unavailable result and document the exact probe and remediation. Do not guess.

## 22. Source references

- Adobe, [Scripts in After Effects](https://helpx.adobe.com/after-effects/desktop/automate-in-after-effects/automate-animation/scripts.html): ExtendScript file types, file/network permission, and command-line script execution.
- Adobe, [Automated rendering and network rendering](https://helpx.adobe.com/after-effects/desktop/render-and-export/automate-rendering/automated-rendering-network-rendering.html): `aerender` location, arguments, frame ranges, output paths, and render-only behavior.
- After Effects Scripting Guide, [Object model](https://ae-scripting.docsforadobe.dev/introduction/objectmodel/): project, compositions, layers, footage, properties, and render queue model.
- After Effects Scripting Guide, [Application object](https://ae-scripting.docsforadobe.dev/general/application/): application version, effects, fonts, GPU capabilities, exit status, and project access.
- After Effects Scripting Guide, [RenderQueue object](https://ae-scripting.docsforadobe.dev/renderqueue/renderqueue/): render queue status, render, pause, and stop APIs.
- Blend reference implementation and docs under `/Users/johnconway/Documents/blender`: generic project layout, strict schemas, manifests, validation, restartable rendering, review, and cleanup patterns.
