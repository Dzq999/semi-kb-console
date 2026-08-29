param(
  [switch]$MigrateSqlite,
  [string]$SqliteSource
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not $env:SEMI_KB_DB_PASSWORD) {
  $env:SEMI_KB_DB_PASSWORD = [Environment]::GetEnvironmentVariable('SEMI_KB_DB_PASSWORD', 'User')
}
if (-not $env:SEMI_KB_DB_PASSWORD -and -not $env:DATABASE_URL) {
  throw 'SEMI_KB_DB_PASSWORD or DATABASE_URL is not configured'
}
Push-Location $backend
try {
  & $python -m alembic -c alembic.ini upgrade head
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  if ($MigrateSqlite) {
    if (-not $SqliteSource) { $SqliteSource = Join-Path $root 'data\console.db' }
    $resolvedSource = Resolve-Path -LiteralPath $SqliteSource -ErrorAction Stop
    & $python -m scripts.migrate_sqlite_to_postgres --source $resolvedSource.Path
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  & $python -m scripts.verify_postgres
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
