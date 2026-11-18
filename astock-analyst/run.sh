#!/usr/bin/env bash
# 启动 A 股分析工具（macOS / Linux）
set -e

cd "$(dirname "$0")"

NODE_BIN="node"
if ! command -v node >/dev/null 2>&1; then
  for d in "$HOME/.workbuddy/binaries/node/versions"/*/node; do
    if [ -x "$d" ]; then NODE_BIN="$d"; break; fi
  done
fi

if ! command -v "$NODE_BIN" >/dev/null 2>&1 && [ ! -x "$NODE_BIN" ]; then
  echo "没找到 Node.js。请先安装 Node.js 18 或更高版本：https://nodejs.org"
  exit 1
fi

PORT="${ASTOCK_PORT:-8790}"
echo ""
echo "  正在启动 A 股分析工具，稍等两秒…"
echo ""

( sleep 1.5; command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT" || \
  command -v xdg-open >/dev/null 2>&1 && xdg-open "http://127.0.0.1:$PORT" ) >/dev/null 2>&1 &

exec "$NODE_BIN" server.js
