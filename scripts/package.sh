#!/usr/bin/env bash
# 打包构建产物。多端打包请用 ../firefox-reverse-build/。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_DIR="${UPSTREAM_DIR:-$REPO_ROOT/upstream}"
DIST_DIR="${DIST_DIR:-$REPO_ROOT/dist}"
UPSTREAM_DIR="$(cd "$UPSTREAM_DIR" && pwd)"

cd "$UPSTREAM_DIR"
node "$REPO_ROOT/scripts/check-branding-assets.mjs" "$UPSTREAM_DIR"
objdir=$(./mach environment --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["topobjdir"])')
python3 "$REPO_ROOT/scripts/verify-branding.py" --objdir "$objdir"

if [[ "$(uname -s)" == "Darwin" ]]; then
  "$REPO_ROOT/scripts/macos-adhoc-sign-apps.sh" "$UPSTREAM_DIR" "$objdir"
fi

./mach package
python3 "$REPO_ROOT/scripts/verify-branding.py" --package "$objdir/dist/firefox"

if [[ "$(uname -s)" == "Darwin" ]]; then
  shopt -s nullglob
  dmg_files=("$objdir"/dist/firefox-*.mac.dmg)
  shopt -u nullglob
  if [[ ${#dmg_files[@]} -gt 0 ]]; then
    "$REPO_ROOT/scripts/macos-verify-dmg.sh" "${dmg_files[@]}"
  fi
fi

mkdir -p "$DIST_DIR"
shopt -s nullglob
packages=("$objdir"/dist/firefox-*.{tar.xz,dmg,zip,exe,deb})
shopt -u nullglob
if [[ ${#packages[@]} -eq 0 ]]; then
  echo "[package] no packages in selected object directory: $objdir" >&2
  exit 1
fi
cp -v "${packages[@]}" "$DIST_DIR/"

echo "[package] artifacts → $DIST_DIR"
