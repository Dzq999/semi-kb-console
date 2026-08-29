$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$venv = Join-Path $root '.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
  python -m venv $venv
}
$python = Join-Path $venv 'Scripts\python.exe'
if (-not $env:SEMI_KB_DB_PASSWORD) {
  $env:SEMI_KB_DB_PASSWORD = [Environment]::GetEnvironmentVariable('SEMI_KB_DB_PASSWORD', 'User')
}
& $python -c 'import alembic, fastapi, langgraph, psycopg, rdflib, sqlalchemy; import langgraph.checkpoint.postgres, owlrl, pyshacl'
if ($LASTEXITCODE -ne 0) {
  & $python -m pip install -r (Join-Path $backend 'requirements.txt')
}
if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
  npm install --prefix $frontend
}
if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) {
  Start-Process -FilePath $python -ArgumentList 'run.py' -WorkingDirectory $backend -WindowStyle Hidden
}
if (-not (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue)) {
  Start-Process -FilePath 'npm.cmd' -ArgumentList 'run','dev','--prefix',$frontend -WorkingDirectory $frontend -WindowStyle Hidden
}
Write-Host 'Backend: http://127.0.0.1:8765'
Write-Host 'Frontend: http://127.0.0.1:5173'
