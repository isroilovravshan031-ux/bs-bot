# Birinchi o'rnatish: PostgreSQL klaster, .env, bazalar, Python muhiti.
# Qayta ishga tushirish xavfsiz: mavjud narsaga tegmaydi.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 1. PostgreSQL binarlari
$initdb = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\initdb.exe' -ErrorAction SilentlyContinue |
    Sort-Object { [int]$_.Directory.Parent.Name } | Select-Object -Last 1
if (-not $initdb) {
    Write-Host "PostgreSQL topilmadi, winget orqali o'rnatilmoqda..."
    winget install --id PostgreSQL.PostgreSQL.18 -e --accept-package-agreements --accept-source-agreements
    $initdb = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\initdb.exe' | Select-Object -Last 1
}
$bin = $initdb.Directory.FullName
$pgData = Join-Path $root '.pg\data'
$port = 5433

# 2. .env
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $root '.env.example') $envFile }

# 3. Botning alohida klasteri (asosiy 5432 serverga tegmaydi)
if (-not (Test-Path (Join-Path $pgData 'PG_VERSION'))) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    $pwFile = Join-Path $env:TEMP ("nk-" + [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($pwFile, $password)
    try {
        New-Item -ItemType Directory -Force (Split-Path $pgData) | Out-Null
        & "$bin\initdb.exe" -D $pgData -U nakladnoy -A scram-sha-256 "--pwfile=$pwFile" -E UTF8 --no-locale | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'initdb xato berdi' }
    } finally { Remove-Item $pwFile -Force }
    [IO.File]::AppendAllText((Join-Path $pgData 'postgresql.conf'), "`nport = $port`nlisten_addresses = '127.0.0.1'`n")
    $url = "postgresql://nakladnoy:$password@127.0.0.1:$port/nakladnoy"
    $content = [IO.File]::ReadAllText($envFile) -replace '(?m)^DATABASE_URL=.*$', "DATABASE_URL=$url"
    [IO.File]::WriteAllText($envFile, $content, (New-Object Text.UTF8Encoding($false)))
    Write-Host "PostgreSQL klaster yaratildi: $pgData (port $port)"
}

# 4. Serverni yoqish
& (Join-Path $PSScriptRoot 'pg-start.ps1')

# 5. Bazalar
$line = (Get-Content $envFile | Where-Object { $_ -match '^DATABASE_URL=' }) -replace '^DATABASE_URL=', ''
$env:PGPASSWORD = ([uri]$line).UserInfo.Split(':')[1]
foreach ($db in 'nakladnoy', 'nakladnoy_test') {
    $exists = & "$bin\psql.exe" -w -h 127.0.0.1 -p $port -U nakladnoy -d postgres -Atc "select 1 from pg_database where datname='$db'"
    if ($LASTEXITCODE -ne 0) { throw 'psql ulana olmadi' }
    if ($exists -ne '1') {
        & "$bin\createdb.exe" -w -h 127.0.0.1 -p $port -U nakladnoy $db
        if ($LASTEXITCODE -ne 0) { throw "$db bazasi yaratilmadi" }
    }
}
Remove-Item Env:PGPASSWORD

# 6. Python muhiti
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { & py -3.12 -m venv (Join-Path $root '.venv') }
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r (Join-Path $root 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install xato berdi' }

Write-Host "Tayyor. Endi .env ga TELEGRAM_BOT_TOKEN va GEMINI_API_KEY yozing, keyin scripts\start.ps1"
