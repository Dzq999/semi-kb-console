#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="$root/backend"
frontend="$root/frontend"
venv="$root/.venv"
engine="$root/data/engine"

if [[ ! -f "$engine/scripts/kb.py" ]]; then
  echo "缺少控制台本地引擎，请先运行 ./scripts/migrate-engine.sh（默认使用内置 engine-seed；也可显式指定一次性迁移源）。运行时不依赖外部项目。" >&2
  exit 1
fi

if [[ ! -f "$venv/bin/python" ]]; then
  python3 -m venv "$venv"
fi

python="$venv/bin/python"

if [[ -z "${SEMI_KB_DB_PASSWORD:-}" ]]; then
  echo "警告: SEMI_KB_DB_PASSWORD 环境变量未设置，PostgreSQL 连接可能失败。" >&2
fi

if ! "$python" -c 'import alembic, fastapi, langgraph, psycopg, rdflib, sqlalchemy, aibot; import langgraph.checkpoint.postgres, owlrl, pyshacl' 2>/dev/null; then
  "$python" -m pip install -r "$backend/requirements.txt"
fi

if [[ ! -d "$frontend/node_modules" ]]; then
  npm install --prefix "$frontend"
fi

if ! lsof -iTCP:8765 -sTCP:LISTEN -t >/dev/null 2>&1; then
  (cd "$backend" && "$python" run.py &)
fi

if ! lsof -iTCP:5173 -sTCP:LISTEN -t >/dev/null 2>&1; then
  (cd "$frontend" && npm run dev &)
fi

echo "Backend: http://127.0.0.1:8765"
echo "Frontend: http://127.0.0.1:5173"
