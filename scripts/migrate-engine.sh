#!/usr/bin/env bash
# 把 engine-seed 的初始数据复制到 data/engine（首次部署用）
#
# 用法：
#   ./scripts/migrate-engine.sh              # 复制所有目录
#   ./scripts/migrate-engine.sh --skip-knowledge  # 跳过 knowledge 目录

set -euo pipefail

skip_knowledge=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-knowledge)
      skip_knowledge=true
      shift
      ;;
    *)
      echo "用法: $0 [--skip-knowledge]" >&2
      exit 1
      ;;
  esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
seed_path="$root/engine-seed"
data_engine="$root/data/engine"

if [[ ! -d "$seed_path" ]]; then
  echo "错误: engine-seed 目录不存在: $seed_path" >&2
  exit 1
fi

mkdir -p "$data_engine"

directories=("ontology" "business" "simulation" "scripts" "sources")
if [[ "$skip_knowledge" != "true" ]]; then
  directories+=("knowledge")
fi

for dir in "${directories[@]}"; do
  src="$seed_path/$dir"
  dst="$data_engine/$dir"
  if [[ -d "$src" ]]; then
    echo "复制 $dir ..."
    cp -r "$src" "$dst"
  else
    echo "警告: 跳过不存在的目录: $dir" >&2
  fi
done

echo "引擎数据初始化完成: $data_engine"
