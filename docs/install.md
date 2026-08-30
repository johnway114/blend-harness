# Install, upgrade, and uninstall

## Supported hosts

Blend supports local macOS and Linux hosts where Python 3.11 or newer, a compatible Blender executable, FFmpeg, and FFprobe are available. It does not install system packages or start a service.

Install host dependencies with the platform package manager. Examples:

```sh
# macOS with Homebrew
brew install --cask blender
brew install ffmpeg python@3.13

# Debian or Ubuntu when the repository Blender line is compatible
sudo apt install blender ffmpeg python3 python3-venv
```

Run `blend doctor --json` after installation. The report, not the package-manager name, is authoritative for Blender version, Python API, engines, devices, codecs, color management, writable roots, disk, optional validators, fonts, and offline enforcement.
Python runtime dependencies are exactly pinned in `pyproject.toml`; the built wheel resolves the same JSON Schema, image-decoding, and YAML behavior on every install. This rejects silent dependency drift at the cost of requiring a reviewed Blend release to take dependency security or compatibility updates. Blender and FFmpeg remain host capabilities because their binaries are too large and platform-specific to vendor.


## Isolated user install

```sh
git clone <repository-url> blend-harness
cd blend-harness
./scripts/install.sh
$HOME/.local/bin/blend doctor --json
```

Defaults:

- prefix: `$HOME/.local`
- virtual environment: `$HOME/.local/share/blend-harness/venv`
- commands: `$HOME/.local/bin/blend` and `$HOME/.local/bin/blend-mcp`

Override only when required:

```sh
BLEND_PREFIX="$HOME/tools" PYTHON=python3.12 ./scripts/install.sh
```

The installer refuses to replace unrelated command paths. It records an ownership marker inside the managed virtual environment. It creates no daemon, launch agent, system service, or shell startup modification.

## Upgrade

Pull or unpack the reviewed release, then run the same installer:

```sh
git pull --ff-only
./scripts/install.sh
blend doctor --json
```

Before upgrading a project:

```sh
blend --json migrate /absolute/project/path
blend --json template-upgrade /absolute/project/path
```

`migrate` previews exact schema-only changes. Add `--write` only after review. A backup is retained before the first write. `template-upgrade` produces a comparison and never overwrites project source or creative settings.

## Uninstall

```sh
./scripts/uninstall.sh
```

The uninstaller removes only links pointing to the managed environment and only an environment containing Blend's ownership marker. Project source and generated project artifacts remain untouched.

Custom installations must pass the same variables used during installation:

```sh
BLEND_PREFIX="$HOME/tools" ./scripts/uninstall.sh
```

## Direct package install

For an already managed Python environment:

```sh
python3 -m pip install .
python3 -m pip install --upgrade .
python3 -m pip uninstall blend-harness
```

## Host conformance

The CI definitions exercise package installation, schema migration, reference-project configuration, and uninstall on macOS and Linux. Real Blender and FFmpeg tests run when those binaries are installed and report skips rather than replacing the production contract with mocked renders.
