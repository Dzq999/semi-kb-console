#!/usr/bin/env bash
# PostgreSQL 数据库初始化与升级脚本
#
# 用法：
#   ./scripts/migrate-postgres.sh          # 正常升级（执行 Alembic migrations）

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="$root/backend"
venv="$root/.venv"
python="$venv/bin/python"

if [[ ! -d "$venv" ]]; then
  echo "创建虚拟环境 .venv ..."
  python3 -m venv "$venv"
fi

if [[ ! -f "$python" ]]; then
  echo "错误: Python 虚拟环境不完整: $python" >&2
  exit 1
fi

req_file="$backend/requirements.txt"
if [[ -f "$req_file" ]]; then
  echo "安装 Python 依赖 ..."
  "$python" -m pip install --quiet --upgrade pip
  "$python" -m pip install --quiet -r "$req_file"
fi

cd "$backend"
echo "执行 Alembic 数据库升级 ..."
"$python" -m alembic upgrade head

echo "验证 PostgreSQL 连接 ..."
"$python" -c "from app.db import SessionLocal; db = SessionLocal(); db.execute('SELECT 1'); db.close(); print('PostgreSQL 连接正常')"

echo "PostgreSQL 数据库初始化完成"
