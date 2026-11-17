#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_root"
if [ ! -f node_modules/cesium/Build/Cesium/Cesium.js ]; then
  printf '请先在 %s 运行 npm ci\n' "$project_root"
  exit 1
fi
if [ -x "$HOME/.local/bin/ollama-start" ]; then
  "$HOME/.local/bin/ollama-start" || printf 'Qwen 暂不可用，地图仍可使用。\n'
fi
exec python3 server/app.py "$@"
