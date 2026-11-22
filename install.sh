#!/bin/bash
# 聊天生图 chat2image · 安装收尾（解压后跑一次）
#
# 只做三件事：给脚本执行权限、建 .env、建本地配置目录。
# 不需要注册、不需要填 key、不需要装任何依赖（只用 Python 3.8+ 标准库）。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

chmod +x bin/*.sh 2>/dev/null || true

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "已生成 .env（内置中转站地址，无需填写）"
  else
    echo "❌ 缺 .env.example，安装包不完整"
    exit 1
  fi
fi
chmod 600 .env 2>/dev/null || true

mkdir -p "${C2I_CONFIG_DIR:-$HOME/.config/chat2image}"
chmod 700 "${C2I_CONFIG_DIR:-$HOME/.config/chat2image}" 2>/dev/null || true

echo
echo "安装完成。下一步跑自检："
echo "  $DIR/bin/doctor.sh"
