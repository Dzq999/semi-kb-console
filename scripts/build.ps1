$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
npm run build --prefix (Join-Path $root 'frontend')

