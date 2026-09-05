#!/usr/bin/env bash
set -euo pipefail

migrate_sqlite=false
sqlite_source=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --migrate-sqlite)
      migrate_sqlite=true
      shift
      ;;
    --sqlite-source)
      sqlite_source="$2"
      shift 2
      ;;
    *)
      echo "用法: $0 [--migrate-sqlite] [--sqlite-source <路径>]" >&2
      exit 1
      ;;
  esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="$root/backend"
venv="$root/.venv"
python="$venv/bin/python"

if [[ ! -f "$python" ]]; then
  python3 -m venv "$venv"
  "$python" -m pip install -r "$backend/requirements.txt"
fi

if [[ -z "${SEMI_KB_DB_PASSWORD:-}" ]]; then
  echo "错误: SEMI_KB_DB_PASSWORD 环境变量未设置。" >&2
  echo "请设置: export SEMI_KB_DB_PASSWORD='your_password'" >&2
  exit 1
fi

cd "$backend"

if [[ "$migrate_sqlite" == true ]]; then
  if [[ -z "$sqlite_source" ]]; then
    sqlite_source="$root/data/console.db"
  fi

  if [[ ! -f "$sqlite_source" ]]; then
    echo "错误: SQLite 数据库文件不存在: $sqlite_source" >&2
    exit 1
  fi

  echo "从 SQLite 迁移数据到 PostgreSQL..."
  "$python" scripts/migrate_sqlite_to_postgres.py
fi

echo "执行 Alembic 升级..."
"$python" -m alembic upgrade head

echo "验证 PostgreSQL 连接和表结构..."
"$python" scripts/verify_postgres.py

echo "PostgreSQL 迁移完成。"
