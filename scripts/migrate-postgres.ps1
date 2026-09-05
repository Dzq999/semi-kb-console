# PostgreSQL 数据库初始化与升级脚本
#
# 用法：
#   .\scripts\migrate-postgres.ps1          # 正常升级（执行 Alembic migrations）

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $venv)) {
  Write-Host "创建虚拟环境 .venv ..."
  python -m venv $venv
}

if (-not (Test-Path $python)) {
  Write-Error "Python 虚拟环境不完整: $python"
  exit 1
}

$reqFile = Join-Path $backend 'requirements.txt'
if (Test-Path $reqFile) {
  Write-Host "安装 Python 依赖 ..."
  & $python -m pip install --quiet --upgrade pip
  & $python -m pip install --quiet -r $reqFile
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 依赖安装失败"
    exit $LASTEXITCODE
  }
}

Push-Location $backend
try {
  Write-Host "执行 Alembic 数据库升级 ..."
  & $python -m alembic upgrade head
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Alembic 升级失败"
    exit $LASTEXITCODE
  }

  Write-Host "验证 PostgreSQL 连接 ..."
  & $python -c "from app.db import SessionLocal; db = SessionLocal(); db.execute('SELECT 1'); db.close(); print('PostgreSQL 连接正常')"
  if ($LASTEXITCODE -ne 0) {
    Write-Error "PostgreSQL 连接验证失败"
    exit $LASTEXITCODE
  }
} finally {
  Pop-Location
}

Write-Host "PostgreSQL 数据库初始化完成" -ForegroundColor Green
