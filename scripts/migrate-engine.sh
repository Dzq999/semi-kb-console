#!/usr/bin/env bash
set -euo pipefail

source=""
skip_knowledge=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      source="$2"
      shift 2
      ;;
    --skip-knowledge)
      skip_knowledge=true
      shift
      ;;
    *)
      echo "用法: $0 [--source <路径>] [--skip-knowledge]" >&2
      exit 1
      ;;
  esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
destination="$root/data/engine"
seed_path="$root/engine-seed"
source_input="${source:-$seed_path}"
source_path="$(cd "$source_input" && pwd)"

if [[ "$source_path" == "$(cd "$root" && pwd)" ]]; then
  echo "迁移源不能是 semi-kb-console 自身。" >&2
  exit 1
fi

if [[ ! -f "$source_path/scripts/kb.py" ]]; then
  echo "迁移源不是有效的引擎目录：$source_path" >&2
  exit 1
fi

mkdir -p "$destination"

directories=(ontology business simulation kb mappings output-contracts references sources scripts build tests)
if [[ "$skip_knowledge" == false ]]; then
  directories+=(knowledge)
fi

for dir in "${directories[@]}"; do
  from="$source_path/$dir"
  if [[ -d "$from" ]]; then
    to="$destination/$dir"
    mkdir -p "$to"
    cp -rf "$from"/* "$to/" 2>/dev/null || true
  fi
done

config="$source_path/config.yaml"
if [[ -f "$config" ]]; then
  cp -f "$config" "$destination/config.yaml"
fi

local_python="$root/.venv/bin/python"
if [[ -f "$local_python" ]]; then
  "$local_python" "$destination/scripts/migrate_semantic.py" || {
    echo "本地引擎语义迁移失败。" >&2
    exit 1
  }
fi

source_label="${source:-内置 engine-seed}"
echo "引擎数据已从 $source_label 迁移到：$destination"
echo "运行时将只使用 data/engine，不再读取迁移源。"
