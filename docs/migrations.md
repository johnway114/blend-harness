# Schema and template migrations

## Principles

- Schema versions are explicit in configuration and every retained report.
- Current readers never guess how to interpret an unknown future version.
- Migrations are deterministic, reviewable, and preserve project creative source.
- A migration preview is non-mutating.
- A write creates a backup before replacing configuration.
- There are no deprecated runtime aliases or silent compatibility shims.

## Project schema migration

Preview:

```sh
blend --json migrate /absolute/project/path
```

Apply:

```sh
blend --json migrate /absolute/project/path --write
blend --json validate-config /absolute/project/path
```

The result lists source version, target version, changed fields, backup path when written, and the migrated configuration path. If no registered migration path exists, the operation fails with a stable schema-version error.

Generated artifacts made by an older semantic fingerprint are not rewritten. The next plan reports them stale and a new build, bake, validation, render, or export creates current evidence.

## Template upgrade

Built-in templates are starting points, not live dependencies. Compare a project with its current built-in template:

```sh
blend --json template-upgrade /absolute/project/path
```

The operation writes a comparison report showing source additions, removals, and changes. It never overwrites `scene.py`, modules, brief, variants, assets, or creative settings. Apply selected changes manually, then run `validate-config` and `config diff`.

## Library update

Reusable project libraries are pinned independently:

```sh
blend --json library compare /absolute/project/path brand-rig /path/to/candidate
blend --json library update /absolute/project/path brand-rig /path/to/candidate
```

Update is explicit and atomic. Candidate ID, version, every transitive dependency, and whole-directory checksum must validate. Executable library Python changes invalidate trust and all downstream fingerprints.

## Rollback

For configuration migration, restore the recorded backup and rerun `validate-config`. For a library update, restore its versioned source and pin from version control. Generated outputs need no rollback mechanism because their manifests are immutable evidence; select an earlier complete artifact or rebuild from the restored source revision.
