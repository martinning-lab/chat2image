#!/bin/bash
# 聊天生图 chat2image · 首次进入时的欢迎海报判定（不消耗额度，不发图）
# 用法：bin/welcome.sh            看要不要展示（已展示过 → 零网络请求）
#       bin/welcome.sh --mark-shown --version N   图与说明都发出去之后再调
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${C2I_PYTHON:-python3}"
exec "$PY" "$DIR/scripts/client.py" welcome "$@"
