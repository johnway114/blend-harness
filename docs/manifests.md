# Manifests and integration contracts

## Result envelope

`blend --json` and every MCP tool return `result-v1`:

```json
{
  "schemaVersion": "blend.result/v1",
  "operationId": "caller-or-generated-id",
  "operation": "render",
  "status": "succeeded",
  "startedAt": "2026-08-30T12:00:00.000Z",
  "endedAt": "2026-08-30T12:00:12.400Z",
  "durationSeconds": 12.4,
  "summary": "Rendered 24 frame(s).",
  "artifacts": [],
  "warnings": [],
  "nextActions": [],
  "progress": {"completed": 24, "total": 24}
}
```

Failures use the same envelope with `status: "error"` and a structured error containing stable code, message, retryability, details, and remediation. Consumers must branch on `status` and `error.code`, not parse human prose.

## Operation manifests

Every expensive operation retains `operation-v1.json`. It records:

- operation and caller identifier
- start, update, and completion timestamps
- state: planned, running, interrupted, failed, or complete
- resolved project, profile, variant, matrix, and output identifiers
- complete input fingerprint and component hashes
- Blender, harness, schema, runtime, and capability versions
- seeds, device/backend, engine, color management, and effective settings
- per-frame or per-cache-unit progress
- artifacts with path, byte count, SHA-256, dimensions or media probe where applicable
- warnings, errors, logs, and next action

Writes are atomic. A retained artifact is accepted only when the manifest is complete and its measured bytes still match.

## Provenance fingerprint

The input hash covers:

- `blend.yaml`, brief, scene module, imported project modules, and schema version
- every declared asset and transitive dependency
- catalog ID/version/checksum and reusable library ID/version/checksum
- Blender executable/version/API, FFmpeg capability where applicable, device/backend, and operating-system capability fields
- selected profile, variant, matrix member, output declaration, seeds, effective color management, and operation type

Relative filesystem location and timestamps are excluded when they are not semantic. This allows a clean project copy to reproduce the same source fingerprint.

## Specialized reports

Schemas in `blend_harness/schemas/` define:

- capability/doctor reports
- project configuration and brief
- inspection and validation
- preview, render, cache, encode, export, comparison, search, and review manifests
- library and catalog records
- reference-project expectations

Generated reports are validated before consumption. Schema migrations are explicit and one-way; unknown future versions fail rather than being guessed.

## Progress and resume

Frame-level records store status, claim owner, expected path, signature, byte range, measured hash, and timestamps. Matrix roots store every profile/variant job. After interruption, `resume` reconciles disk with manifest evidence and claims only missing, corrupt, or stale work.

Progress files are append-safe or atomically replaced. A reader never observes a partially written JSON document.

## Logs

Host logs live under the owned operation directory. Subprocess stdout/stderr are bounded in result envelopes and preserved in full log artifacts. Project source must not print secrets. Environment filtering removes non-allowlisted variables before Blender or FFmpeg starts.

## Exit behavior

- success: exit 0
- usage or schema errors: stable nonzero CLI usage/configuration category
- missing host dependency or unsupported capability: stable unavailable category
- trust or policy failure: stable permission/policy category
- runtime, validation, media, and export failures: stable operation category
- interruption: signal-compatible nonzero result after process-group cleanup

Exact numeric mappings are centralized in `blend_harness.errors` and covered by CLI conformance tests.
