#!/bin/bash
# 聊天生图 chat2image · 自检
# 用法：bin/doctor.sh [--json]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${C2I_PYTHON:-python3}"
exec "$PY" "$DIR/scripts/client.py" doctor "$@"
