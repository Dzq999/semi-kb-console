#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="$root/backend"
venv="$root/.venv"
python="$venv/bin/python"

if [[ ! -f "$python" ]]; then
  echo "虚拟环境不存在，请先运行 ./scripts/dev.sh" >&2
  exit 1
fi

cd "$backend"
"$python" -m pytest tests/ -v
