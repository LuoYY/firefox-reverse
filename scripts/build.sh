#!/usr/bin/env bash
# 调用 firefox 标准 mach 构建流程。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_DIR="${UPSTREAM_DIR:-$REPO_ROOT/upstream}"

if [[ ! -d "$UPSTREAM_DIR" ]]; then
  echo "[build] $UPSTREAM_DIR 不存在" >&2
  exit 1
fi

node "$REPO_ROOT/scripts/check-branding-assets.mjs" "$UPSTREAM_DIR"

cd "$UPSTREAM_DIR"

# 安装 build 依赖（首次）
if [[ ! -f .bootstrap-done ]]; then
  ./mach --no-interactive bootstrap --application-choice browser
  touch .bootstrap-done
fi

./mach configure --with-branding=browser/branding/unofficial
objdir=$(./mach environment --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["topobjdir"])')
python3 "$REPO_ROOT/scripts/verify-branding.py" --objdir "$objdir"
./mach build "$@"

echo "[build] done. 产物位于 $UPSTREAM_DIR/obj-*/dist/"
