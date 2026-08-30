# Compatibility policy

The machine-readable policy is [`blend_harness/compatibility.json`](../blend_harness/compatibility.json).

## Blender

Blend maintains contracts for:

- the current Blender LTS line: 4.5.x
- the current supported stable line: 5.2.x

The runtime uses public Blender Python APIs available across those lines and applies narrowly scoped version branches only where operator names or import/export option names changed. `blend doctor --json` reports `supported: false` outside the declared range. Unsupported hosts fail before project source executes.

Every supported line must pass:

- configuration and schema conformance
- clean project initialization
- deterministic build and inspection
- preview modes and contact-sheet generation
- restartable image-sequence render
- FFmpeg delivery and probe QC when FFmpeg is available
- applicable interchange exports and clean-process decode
- process interruption cleanup

## Python

The host CLI supports Python 3.11 and newer. Blender runs its bundled Python; project scene code must not depend on packages installed only in the host CLI environment.

## FFmpeg

FFmpeg and FFprobe are required for movie, WebM, ProRes, and GIF delivery. Capability detection verifies declared encoders and pixel formats before a render starts. The harness passes explicit color metadata and probes the finished file.

## Operating systems

macOS and Linux are supported. CI uses both host families for installation, CLI/schema, MCP protocol, path ownership, and uninstall conformance. Real rendering lanes run only on hosts with a compatible Blender binary.

Windows is not currently a declared supported host. The process-group, installer, path, and executable discovery contracts are POSIX-specific.

## Optional validators

Optional external model/media validators are reported by doctor. Their absence does not silently weaken a validation profile: a profile that requires one is blocked; a profile that does not require it continues with built-in clean-process decode and structural checks.

## Capability-first behavior

Version numbers alone are insufficient. Planning also checks:

- render engines and explicit compute backends
- import/export operator availability
- codecs and pixel formats
- color-management views and looks
- writable roots and free disk
- optional validators and fonts
- offline-enforcement readiness

A project that requests an unavailable capability fails in planning with a stable blocker and remediation.
