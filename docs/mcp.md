# MCP stdio server

Run:

```sh
blend-mcp
# equivalent
blend mcp serve
```

The server implements JSON-RPC over stdio. Protocol output is written only to stdout; diagnostic logs go to stderr. Tool results use the same versioned result envelope as `blend --json`.

## Tools

The server exposes typed schemas for:

- `blend_doctor`
- `blend_init`
- `blend_validate_config`
- `blend_plan`
- `blend_build`
- `blend_preview`
- `blend_contact_sheet`
- `blend_inspect`
- `blend_validate`
- `blend_bake`
- `blend_cache_inspect`
- `blend_render`
- `blend_resume`
- `blend_encode`
- `blend_export`
- `blend_compare`
- `blend_review`
- `blend_search`
- `blend_artifact`
Arguments mirror CLI operation inputs. Filesystem-bearing tools require explicit absolute or caller-resolved project paths. Every mutating tool requires a caller-provided `operationId`, enabling cancellation, unambiguous retained results, and duplicate-mutation rejection.

## Bounded responses

MCP responses do not inline whole logs, full inspection object arrays, every search candidate, or every render frame. They return counts, a bounded preview, key findings, progress, artifact paths, and manifest locations. The complete schema-validated report remains on disk for an agent to read deliberately.

## Trust and network

MCP does not bypass local policy:

- project Python still requires reviewed trust
- default execution is offline
- network requires both project opt-in and explicit `allowNetwork: true`
- roots, limits, output ownership, catalog checksums, and library checksums remain enforced
- direct CLI and MCP invoke the same operation implementation

## Cancellation

Send an MCP cancellation notification for the active request. The server sends `SIGINT` to the owned CLI process group, which propagates interruption to owned Blender or FFmpeg groups, records interruption state, and returns a bounded cancelled error. It does not kill unrelated Blender processes.

An operation identifier is single-use. A duplicate identifier is rejected and points to its retained manifest instead of accidentally duplicating or overwriting work. Resume uses a new operation identifier and reuses only units whose resolved fingerprints still match.

## Example request

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"blend_plan","arguments":{"project":"/absolute/project","target":"render","profile":"final","variant":"graphite"}}}
```

Before starting expensive work, an agent should call `blend_doctor`, `blend_validate_config`, and `blend_plan`. It should surface blockers and estimated resources rather than invoking Blender optimistically.
