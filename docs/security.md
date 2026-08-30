# Security and trust model

## Boundary

Blender Python is executable code. A trusted `scene.py`, imported project module, or declared reusable library can access the current user's files with the permissions of Blender. Blend does not claim a complete Python sandbox.

Trust means: the operator reviewed the exact executable bytes and accepts that boundary. It does not mean the code is safe in the abstract.

## Trust records

`blend trust` records SHA-256 hashes for the entry module, imported project modules, and executable library files. Any byte change invalidates that record. `--trust` performs an explicit trust decision for the current operation; unattended production should use a reviewed retained trust record.

Configuration validation never executes project Python.

## Network policy

Projects are offline by default. During Blender execution, a bootstrap network guard denies common Python socket and URL entry points. Network access is allowed only when both are true:

1. `blender.offline` is `false` in reviewed configuration.
2. The caller passes `--allow-network` or MCP `allowNetwork: true`.

The guard is defense in depth, not a complete operating-system sandbox. Run untrusted projects in an OS-level container or separate user account with network and filesystem restrictions.

## Environment

Blender and FFmpeg receive a minimal environment plus explicitly allowlisted names. Secret-like variable names are rejected even if requested. Ambient tokens, cloud credentials, SSH agents, and application secrets are not forwarded by default.

## Filesystem

- Every path is normalized before use.
- Source, asset, library, reference, build, preview, render, output, cache, and temporary roots are explicit.
- Generated roots may not overlap source-like roots.
- Assets outside the declared asset root fail validation.
- Clean operations require ownership markers and cannot target source roots.
- Atomic staging remains inside the temporary or destination parent root.
- Symlink and traversal behavior is checked against resolved paths.
- Output collision checks run before Blender starts.

## External processes

Blender, FFmpeg, and FFprobe are invoked with argument arrays, not an interpolated shell. The supervisor applies hard timeouts, bounded captured output, process-group ownership, cancellation, and child cleanup. It terminates only the group it created.

Downloaded tools, auto-installers, and background services are outside the product model. Operators install trusted Blender and FFmpeg packages themselves.

## Assets and dependencies

All local inputs require hashes. Catalog assets pin ID, version, primary file, coordinate assumptions, license, and transitive dependency checksums. Reusable libraries pin ID, semantic version, and whole-directory checksum. Drift is an error, not a warning.

Font declarations include the exact font bytes and local license text. Audio used by an encoder must be a declared asset. Exported texture packages contain only resolved declared dependencies.

## Output integrity

A path alone is not evidence of completion. Complete manifests retain byte sizes and SHA-256 hashes. Resume verifies outputs before reuse. Export promotion requires clean-process decode and profile validation. Encoded media is probed after creation.

## Reporting vulnerabilities

Do not attach a hostile project or secret-bearing log to a public report. Include the stable error code, harness version, Blender version, operating system, redacted configuration, and a minimal non-sensitive reproducer.
