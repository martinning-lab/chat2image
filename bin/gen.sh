#!/bin/bash
# 聊天生图 chat2image · 出图入口（薄客户端：只调中转站，不含上游 key）
# 用法：bin/gen.sh --prompt "描述" [--resolution 1k|2k|4k] [--aspect 3:4] [--out 路径]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PY="${C2I_PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 /usr/bin/python3 "$HOME/.workbuddy/binaries/python/envs/default/bin/python3"; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo '{"success":false,"error":"NO_PYTHON","message":"找不到 python3（本 Skill 需要 Python 3.8+）"}'
  exit 1
fi

exec "$PY" "$DIR/scripts/client.py" gen "$@"
