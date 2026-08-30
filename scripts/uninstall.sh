#!/bin/sh
set -eu

PREFIX=${BLEND_PREFIX:-"$HOME/.local"}
VENV=${BLEND_VENV:-"$PREFIX/share/blend-harness/venv"}
BIN_DIR=${BLEND_BIN_DIR:-"$PREFIX/bin"}

if [ ! -f "$VENV/.blend-harness-install" ]; then
  printf '%s\n' "Refusing to remove an unowned environment: $VENV" >&2
  exit 64
fi
IFS= read -r owner < "$VENV/.blend-harness-install"
if [ "$owner" != 'managed-by=blend-harness' ]; then
  printf '%s\n' "Refusing to remove an unowned environment: $VENV" >&2
  exit 64
fi

for name in blend blend-mcp; do
  link="$BIN_DIR/$name"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$VENV/bin/$name" ]; then
    rm -f "$link"
  fi
done
rm -rf "$VENV"
printf '%s\n' "Removed the managed Blend installation at $VENV"
