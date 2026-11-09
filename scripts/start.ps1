# Bazani yoqadi va botni ishga tushiradi.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& (Join-Path $PSScriptRoot 'pg-start.ps1')
& (Join-Path $root '.venv\Scripts\python.exe') -m nakladnoy_bot run
