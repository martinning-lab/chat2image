#!/bin/bash
# 聊天生图 chat2image · 升级到最新版
# 🔴 只有用户**明确说「升级」**之后才许跑这条；不许静默升级（老马 2026-11-20 拍）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${C2I_PYTHON:-python3}"
exec "$PY" "$DIR/scripts/client.py" upgrade "$@"
