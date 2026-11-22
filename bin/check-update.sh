#!/bin/bash
# 聊天生图 chat2image · 看有没有新版（**只读**：不下载、不安装、不消耗额度）
# 用法：bin/check-update.sh [--json]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${C2I_PYTHON:-python3}"
exec "$PY" "$DIR/scripts/client.py" check-update "$@"
