#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "正在停止后端和前端服务..."

backend_pids=$(lsof -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "$backend_pids" ]]; then
  echo "停止后端 (PID: $backend_pids)"
  kill $backend_pids
fi

frontend_pids=$(lsof -iTCP:5173 -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "$frontend_pids" ]]; then
  echo "停止前端 (PID: $frontend_pids)"
  kill $frontend_pids
fi

echo "服务已停止。"
