#!/bin/bash
# 聊天生图 chat2image · 充值取付款码（余额不足时用，不消耗额度）
# 用法：bin/recharge.sh [--points 1] [--channel alipay|wxpay] [--force]
# 输出 JSON：pay_url（付款链接/二维码内容）、say（原样转述给用户的整句）、订单号
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

exec "$PY" "$DIR/scripts/client.py" recharge "$@"
