#!/bin/bash
# 聊天生图 chat2image · 首次进入时的欢迎判定（不消耗额度，不发图）
# 用法：bin/welcome.sh        看要不要给用户看欢迎页（服务端说已欢迎过 → 零网络请求）
#       bin/welcome.sh --ack  已退役：保留只为旧调用不报错（回 deprecated，不写任何东西）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${C2I_PYTHON:-python3}"
exec "$PY" "$DIR/scripts/client.py" welcome "$@"
