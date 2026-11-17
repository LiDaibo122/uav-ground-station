#!/usr/bin/env bash
# 在任意 WSL 终端执行；--status 只查看，--keep-model 保留 Qwen。
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$project_root/scripts/stop_system.py" "$@"
