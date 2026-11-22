#!/bin/bash
# 聊天生图 chat2image · 任务列表（换会话/崩溃后恢复上下文用）
# 用法：bin/jobs.sh [--limit 20]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${C2I_PYTHON:-python3}"
exec "$PY" "$DIR/scripts/client.py" jobs "$@"
