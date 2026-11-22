#!/bin/bash
# 聊天生图 chat2image · 查余额（不消耗额度）
# 用法：bin/balance.sh [--json]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${C2I_PYTHON:-python3}"
exec "$PY" "$DIR/scripts/client.py" balance "$@"
