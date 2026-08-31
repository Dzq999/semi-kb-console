param(
  [string]$Source = '',
  [switch]$SkipKnowledge
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $root 'data\engine'
$seedPath = Join-Path $root 'engine-seed'
$sourceInput = if ([string]::IsNullOrWhiteSpace($Source)) { $seedPath } else { $Source }
$sourcePath = (Resolve-Path -LiteralPath $sourceInput -ErrorAction Stop).Path

if ($sourcePath -eq (Resolve-Path -LiteralPath $root).Path) {
  throw '迁移源不能是 semi-kb-console 自身。'
}
if (-not (Test-Path (Join-Path $sourcePath 'scripts\kb.py'))) {
  throw "迁移源不是有效的引擎目录：$sourcePath"
}

New-Item -ItemType Directory -Force -Path $destination | Out-Null
$directories = @('ontology', 'business', 'simulation', 'kb', 'mappings', 'output-contracts', 'references', 'sources', 'scripts', 'build', 'tests')
if (-not $SkipKnowledge) { $directories += 'knowledge' }
foreach ($directory in $directories) {
  $from = Join-Path $sourcePath $directory
  if (Test-Path -LiteralPath $from) {
    $to = Join-Path $destination $directory
    New-Item -ItemType Directory -Force -Path $to | Out-Null
    Get-ChildItem -LiteralPath $from -Force | Copy-Item -Destination $to -Recurse -Force
  }
}
$config = Join-Path $sourcePath 'config.yaml'
if (Test-Path -LiteralPath $config) { Copy-Item -LiteralPath $config -Destination (Join-Path $destination 'config.yaml') -Force }

$localPython = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $localPython) {
  & $localPython (Join-Path $destination 'scripts\migrate_semantic.py')
  if ($LASTEXITCODE -ne 0) { throw '本地引擎语义迁移失败。' }
}
$sourceLabel = if ([string]::IsNullOrWhiteSpace($Source)) { '内置 engine-seed' } else { $sourcePath }
Write-Host "引擎数据已从 $sourceLabel 迁移到：$destination"
Write-Host '运行时将只使用 data\engine，不再读取迁移源。'
