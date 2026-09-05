# 把 engine-seed 的初始数据复制到 data/engine（首次部署用）
#
# 用法：
#   .\scripts\migrate-engine.ps1              # 复制所有目录
#   .\scripts\migrate-engine.ps1 -SkipKnowledge  # 跳过 knowledge 目录

param(
  [switch]$SkipKnowledge
)

$root = Split-Path -Parent $PSScriptRoot
$seedPath = Join-Path $root 'engine-seed'
$dataEngine = Join-Path $root 'data\engine'

if (-not (Test-Path $seedPath)) {
  Write-Error "engine-seed 目录不存在: $seedPath"
  exit 1
}

New-Item -ItemType Directory -Force -Path $dataEngine | Out-Null

$directories = @('ontology', 'business', 'simulation', 'scripts', 'sources')
if (-not $SkipKnowledge) { $directories += 'knowledge' }

foreach ($dir in $directories) {
  $src = Join-Path $seedPath $dir
  $dst = Join-Path $dataEngine $dir
  if (Test-Path $src) {
    Write-Host "复制 $dir ..."
    Copy-Item -Recurse -Force $src $dst
  } else {
    Write-Warning "跳过不存在的目录: $dir"
  }
}

Write-Host "引擎数据初始化完成: $dataEngine" -ForegroundColor Green
