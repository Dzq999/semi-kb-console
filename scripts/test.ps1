$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw '缺少项目 .venv，请先运行 scripts\dev.ps1 或安装 requirements.txt' }
$total = [System.Diagnostics.Stopwatch]::StartNew()
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Push-Location $backend
try {
  & $python -m pytest tests -q
  $backendExit = $LASTEXITCODE
} finally {
  Pop-Location
}
if ($backendExit -ne 0) { exit $backendExit }
$sw.Stop(); Write-Host "Backend tests: $([math]::Round($sw.Elapsed.TotalSeconds,2))s"
$sw.Restart()
npm run test --prefix $frontend
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm run build --prefix $frontend
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$sw.Stop(); Write-Host "Frontend test/build: $([math]::Round($sw.Elapsed.TotalSeconds,2))s"
$total.Stop(); Write-Host "Total: $([math]::Round($total.Elapsed.TotalSeconds,2))s"
