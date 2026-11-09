# Botning PostgreSQL klasterini yoqadi (yoqilgan bo'lsa, tegmaydi).
# setup.ps1 va start.ps1 shu faylni chaqiradi.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pgCtl = (Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\pg_ctl.exe' |
    Sort-Object { [int]$_.Directory.Parent.Name } | Select-Object -Last 1).FullName
$pgData = Join-Path $root '.pg\data'
$log = Join-Path $root '.pg\server.log'

function Test-Port {
    $client = New-Object Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', 5433); return $true }
    catch { return $false }
    finally { $client.Close() }
}

if (Test-Port) { exit 0 }

# Start-Process kerak: "pg_ctl | Out-Null" yozilsa, postgres quvurni meros qilib oladi
# va PowerShell abadiy kutib qoladi. -Wait ham jarayon daraxtini kutadi, shuning uchun WaitForExit.
$p = Start-Process -FilePath $pgCtl -ArgumentList @('start', '-D', "`"$pgData`"", '-l', "`"$log`"", '-w') -WindowStyle Hidden -PassThru
$p.WaitForExit()
if (-not (Test-Port)) { throw "PostgreSQL ishga tushmadi: $log ni qarang" }
