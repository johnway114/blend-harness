#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PREFIX=${BLEND_PREFIX:-"$HOME/.local"}
VENV=${BLEND_VENV:-"$PREFIX/share/blend-harness/venv"}
BIN_DIR=${BLEND_BIN_DIR:-"$PREFIX/bin"}
PYTHON=${PYTHON:-python3}

case "$VENV" in
  /|"$HOME"|"$PREFIX")
    printf '%s\n' "Refusing unsafe BLEND_VENV: $VENV" >&2
    exit 64
    ;;
esac

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  printf '%s\n' 'Blend requires Python 3.11 or newer.' >&2
  exit 69
}

mkdir -p "$BIN_DIR" "$(dirname -- "$VENV")"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install --upgrade "$REPOSITORY"
printf '%s\n' 'managed-by=blend-harness' > "$VENV/.blend-harness-install"
for name in blend blend-mcp; do
  link="$BIN_DIR/$name"
  target="$VENV/bin/$name"
  if [ -e "$link" ] || [ -L "$link" ]; then
    if [ ! -L "$link" ] || [ "$(readlink "$link")" != "$target" ]; then
      printf '%s\n' "Refusing to overwrite unrelated executable: $link" >&2
      exit 64
    fi
  fi
  ln -sfn "$target" "$link"
done
printf '%s\n' "Installed Blend at $BIN_DIR/blend"
printf '%s\n' "Run: $BIN_DIR/blend doctor --json"
